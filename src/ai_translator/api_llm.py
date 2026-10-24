"""Anthropic Messages 后端：无重试，密钥仅在发送时从指定环境变量读取。"""
import datetime
import fcntl
import json
import math
import os
import socket
import tempfile
import time
from contextlib import contextmanager
from decimal import Decimal, ROUND_CEILING
from urllib import error, request

from . import config, paths
from .api_prompt import SYSTEM_PROMPT
from .backend import TranslatorBackend, TranslatorError, make_result, output_kind
from .local_lora import _json_object

BUDGET_ERROR = "TRANSLATOR_BUDGET_EXCEEDED"


class UsageStore:
    """稳定的独立锁保护原子替换；发送前预记最大费用，中断也不会漏计。"""

    def __init__(self, cfg, root=None, *, today=None):
        self.cfg = cfg["api_llm"]
        self.directory = paths.state_dir(root)
        self.path = self.directory / "usage.json"
        self.today = today or datetime.date.today

    @contextmanager
    def locked(self):
        try:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(self.directory, 0o700)
            fd = os.open(self.directory / "usage.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "a+b") as stream:
                os.fchmod(stream.fileno(), 0o600)
                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise TranslatorError("TRANSLATOR_BUSY") from None
                try:
                    yield self.read()
                finally:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        except TranslatorError:
            raise
        except Exception:
            raise TranslatorError(BUDGET_ERROR) from None

    def read(self):
        now = self.today()
        blank = {"schema": 1, "month": now.strftime("%Y-%m"), "day": now.isoformat(),
                 "estimated_usd": 0.0, "requests": 0}
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return blank
        with os.fdopen(fd, "rb") as stream:
            payload = stream.read(4097)
        row = _json_object(payload.decode("utf-8"))
        if len(payload) > 4096 or set(row) != set(blank) or type(row["schema"]) is not int or row["schema"] != 1:
            raise ValueError()
        day = datetime.date.fromisoformat(row["day"])
        if row["day"] != day.isoformat() or row["month"] != day.strftime("%Y-%m"):
            raise ValueError()
        cost, count = row["estimated_usd"], row["requests"]
        if (type(cost) not in (float, int) or not math.isfinite(cost) or cost < 0
                or type(count) is not int or count < 0 or day > now):
            raise ValueError()
        if row["month"] != blank["month"]:
            return blank
        if row["day"] != blank["day"]:
            row.update(day=blank["day"], requests=0)
        return row

    def write(self, row):
        fd, tmp = tempfile.mkstemp(prefix=".usage.", dir=self.directory)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(row, stream, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, self.path)
        # mkstemp 的 600 权限随原子替换保留。

    def summary(self, row):
        return {"used_usd": row["estimated_usd"], "max_monthly_usd": self.cfg["max_monthly_usd"],
                "requests_today": row["requests"], "max_daily_requests": self.cfg["max_daily_requests"]}

    def snapshot(self):
        # 账本只经原子替换发布；只读状态可看到完整的预留或结算版本，
        # 不与在途请求争锁。发送前检查仍必须持 locked() 的独占锁。
        try:
            return self.summary(self.read())
        except Exception:
            raise TranslatorError(BUDGET_ERROR) from None

    def cost(self, inputs, outputs):
        cost = (Decimal(inputs) * Decimal(str(self.cfg["price_input_per_mtok"]))
                + Decimal(outputs) * Decimal(str(self.cfg["price_output_per_mtok"]))) / Decimal(1000000)
        return float(cost.quantize(Decimal("0.000000000001"), rounding=ROUND_CEILING))


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ApiLlmBackend(TranslatorBackend):
    def __init__(self, cfg=None, *, root=None, today=None):
        self.cfg = config.validate(cfg or {})
        self.api = self.cfg["api_llm"]
        self.model_version = self.api["model"]
        self.backend_id = "api_llm:" + self.model_version
        self.model = {"version": self.model_version}
        self.usage = UsageStore(self.cfg, root, today=today)
        self._closed = False
        # 不使用系统代理，也不跟随重定向，避免密钥被转发到其他目的地。
        self._opener = request.build_opener(request.ProxyHandler({}), _NoRedirect())

    def status(self):
        problem, budget = None, None
        if self._closed or not os.environ.get(self.api["api_key_env"]):
            problem = "TRANSLATOR_UNAVAILABLE"
        try:
            budget = self.usage.snapshot()
            if budget["used_usd"] >= budget["max_monthly_usd"] or budget["requests_today"] >= budget["max_daily_requests"]:
                problem = BUDGET_ERROR
        except TranslatorError as exc:
            problem = exc.code
        return {"state": "unavailable" if problem or self._closed else "ready", "problem": problem,
                "model_version": self.model_version, "budget": budget}

    def _send(self, payload, key):
        req = request.Request(config.api_base_url(self.api["base_url"]) + "/v1/messages",
                              data=payload, method="POST",
                              headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                                       "content-type": "application/json"})
        try:
            timeout = min(self.cfg["timeouts"]["request_s"], self.api["timeout_s"])
            with self._opener.open(req, timeout=timeout) as response:
                data = response.read(256 * 1024 + 1)
            if len(data) > 256 * 1024 or key.encode() in data:
                return None, "TRANSLATOR_BAD_OUTPUT"
            return _json_object(data.decode("utf-8")), None
        except error.HTTPError as exc:
            exc.close()                       # 响应体不读取，不进入日志或错误文本。
            return None, "TRANSLATOR_BACKEND_FAILED"
        except (TimeoutError, socket.timeout):
            return None, "TRANSLATOR_TIMEOUT"
        except error.URLError as exc:
            return None, "TRANSLATOR_TIMEOUT" if isinstance(exc.reason, TimeoutError) else "TRANSLATOR_BACKEND_FAILED"
        except (ValueError, UnicodeError):
            return None, "TRANSLATOR_BAD_OUTPUT"
        except Exception:
            return None, "TRANSLATOR_BACKEND_FAILED"

    def translate(self, idea_text, *, task="idea_to_config"):
        started = time.monotonic()
        raw, doc, mode, code, usage = "", None, "none", None, None
        try:
            if self._closed:
                raise TranslatorError("TRANSLATOR_UNAVAILABLE")
            if task != "idea_to_config" or not isinstance(idea_text, str) or not idea_text.strip():
                raise TranslatorError("TRANSLATOR_INPUT_INVALID")
            if len(idea_text) > self.cfg["limits"]["max_input_chars"]:
                raise TranslatorError("TRANSLATOR_INPUT_TOO_LONG")
            key = os.environ.get(self.api["api_key_env"])
            if not key:
                raise TranslatorError("TRANSLATOR_UNAVAILABLE")
            # Haiku 5.5 不接受非默认采样参数；显式关闭 thinking 并固定允许的 effort，
            # 不依赖服务端默认值，保留 600 tokens 给现有的纯文本 JSON 协议。
            payload = json.dumps({"model": self.model_version, "max_tokens": 600,
                                  "thinking": {"type": "disabled"}, "output_config": {"effort": "high"},
                                  "system": SYSTEM_PROMPT,
                                  "messages": [{"role": "user", "content": idea_text}]}, ensure_ascii=False).encode("utf-8")
            # UTF-8 字节数高于文本 token 数，另留 4096 个协议 token 的余量；不发送其他本机数据。
            max_input = len(SYSTEM_PROMPT.encode("utf-8")) + len(idea_text.encode("utf-8")) + 4096
            reserved = self.usage.cost(max_input, 600)
            with self.usage.locked() as row:
                before = Decimal(str(row["estimated_usd"]))
                cap = Decimal(str(self.api["max_monthly_usd"]))
                if (before >= cap or row["requests"] >= self.api["max_daily_requests"]
                        or before + Decimal(str(reserved)) > cap):
                    raise TranslatorError(BUDGET_ERROR)
                row.update(estimated_usd=float(before + Decimal(str(reserved))), requests=row["requests"] + 1)
                self.usage.write(row)         # 先持久化；崩溃或通信不明按最大值计费。
                usage = {"input_tokens": max_input, "output_tokens": 600,
                         "estimated_usd": reserved, "estimated": True}
                envelope, code = self._send(payload, key)
                actual = envelope.get("usage") if isinstance(envelope, dict) else None
                if (isinstance(actual, dict) and all(type(actual.get(k)) is int and actual[k] >= 0
                                                     for k in ("input_tokens", "output_tokens"))):
                    inputs, outputs = actual["input_tokens"], actual["output_tokens"]
                    charge = self.usage.cost(inputs, outputs)
                    usage = {"input_tokens": inputs, "output_tokens": outputs, "estimated_usd": charge, "estimated": False}
                    row["estimated_usd"] = float(before + Decimal(str(charge)))
                    self.usage.write(row)
                if code:
                    raise TranslatorError(code)
                try:
                    blocks = envelope["content"]
                    if not isinstance(blocks, list) or not blocks or any(b.get("type") != "text" for b in blocks):
                        raise ValueError()
                    if envelope.get("stop_reason") != "end_turn":
                        raise ValueError()
                    raw = "".join(b["text"] for b in blocks)
                    # 转义后的密钥也不能通过 JSON 解析后流入日志、闸门或响应。
                    doc = _json_object(raw)
                    if key in raw or key in json.dumps(doc, ensure_ascii=False):
                        raw, doc = "", None
                        raise ValueError()
                    if output_kind(doc) != "config":
                        raw = ""             # 控制文档只记解析后的字段，不保存原始输出。
                    mode = "strict"
                except Exception:
                    raise TranslatorError("TRANSLATOR_BAD_OUTPUT") from None
        except TranslatorError as exc:
            code = exc.code
        except Exception:
            code = "TRANSLATOR_BACKEND_FAILED"
        if code:
            doc, raw, mode = None, "", "none"
        out = make_result(config=doc, refusal={"code": code} if code else None, raw_output=raw,
                          parse_mode=mode, backend_id=self.backend_id, model_version=self.model_version,
                          latency_ms=(time.monotonic() - started) * 1000)
        out["usage"] = usage
        return out

    def close(self):
        self._closed = True
