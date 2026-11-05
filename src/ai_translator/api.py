"""HTTP 无关的翻译流程。handle 返回 (状态码, 响应对象)。

网站在 S5 校验本机、认证、来源后传入结果；此处不接收请求头，不导入 app。
每个网站进程复用一个 API 实例；测试注入临时目录、假后端、时钟和校验器。
"""
import copy
import datetime
import json
import os
import sys
import threading
import time
import unicodedata

from . import api_llm, config, consistency, gate, local_lora, paths, plainview, precheck, registry, unused
from .backend import TranslatorBackend, TranslatorError, make_result, output_kind
from .logstore import LogStore


class FakeBackend(TranslatorBackend):
    """仅测试模式可创建。按顺序返回配置、拒绝文档或接口错误，不启动进程。"""
    backend_id = "fake:fixture-v1"

    def __init__(self, outcomes=(), *, model=None):
        if os.environ.get("QB_TESTING") != "1":
            raise ValueError("fake backend requires testing mode")
        self.outcomes = list(outcomes)
        self.calls = []
        self.closed = False
        self.model = model or {"version": "fixture-v1", "adapter_path": "adapters/fixture",
                               "adapter_sha256": "a" * 64, "base_model_sha256": "b" * 64,
                               "schema_sha256": "c" * 64}

    def status(self):
        return {"state": "ready", "problem": None}

    def translate(self, idea_text, *, task="idea_to_config"):
        self.calls.append((idea_text, task))
        if not self.outcomes:
            raise TranslatorError("TRANSLATOR_BACKEND_FAILED")
        item = self.outcomes.pop(0)
        if isinstance(item, str):
            raise TranslatorError(item)
        return make_result(config=copy.deepcopy(item), raw_output=json.dumps(item), parse_mode="strict",
                           backend_id=self.backend_id, model_version=self.model["version"], latency_ms=1)

    def close(self):
        self.closed = True


def _body(data):
    try:
        if not isinstance(data, bytes) or len(data) > 64 * 1024:
            raise ValueError()
        obj = json.loads(data.decode("utf-8"), parse_constant=lambda _: None)
        if not isinstance(obj, dict):
            raise ValueError()
        return obj
    except (ValueError, UnicodeError, RecursionError):
        raise TranslatorError("TRANSLATOR_INPUT_INVALID")


class API:
    CACHE_SIZE = 20
    TTL = 30 * 60

    def __init__(self, root=None, *, loader=None, backend_factory=None, oracle=None, logs=None,
                 clock=None, today=None, rate_hit=None, audit_blocked=None, model_provider=None):
        self.root = root
        self.loader = loader or config.ConfigLoader(root)
        self.logs = logs or LogStore(root)
        self.oracle = oracle
        self.model_provider = model_provider
        self._factory = backend_factory or self._default_backend
        self._clock = clock or time.monotonic
        self._today = today or datetime.date.today
        self._rate_hit = rate_hit
        self._audit_blocked = audit_blocked
        self._lock = threading.RLock()
        self._flight = threading.Lock()
        self._backend = None
        self._cfg = None
        self._cache = {}
        self._hits = []
        self._day = None
        self._count = 0

    def _default_backend(self, cfg):
        if cfg["backend"] == "local_lora":
            provider = self.model_provider or registry.Registry(cfg["ai_layer_dir"], cfg["expect_model_version"])
            return local_lora.Supervisor(cfg, root=self.root, model_provider=provider,
                                         config_loader=self.loader, audit=getattr(self.loader, "_audit", None))
        if cfg["backend"] == "api_llm":
            return api_llm.ApiLlmBackend(cfg, root=self.root, today=self._today)
        raise TranslatorError("TRANSLATOR_UNAVAILABLE")

    def _sync(self, state):
        with self._lock:
            if state.cfg != self._cfg:
                if self._backend is not None:
                    if state.enabled and self._cfg and state.backend == self._cfg["backend"] and hasattr(self._backend, "update_config"):
                        if self.model_provider is None and isinstance(self._backend, local_lora.Supervisor):
                            self._backend._provider = registry.Registry(state.cfg["ai_layer_dir"], state.cfg["expect_model_version"])
                        self._backend.update_config(state.cfg)
                    else:
                        self._backend.close()
                        self._backend = None
                self._cfg = copy.deepcopy(state.cfg)

    def _get_backend(self, cfg):
        with self._lock:
            # 另一个请求可能已读到关闭开关，不能让旧请求随后用旧配置启动后端。
            if cfg != self._cfg:
                code = "TRANSLATOR_DISABLED" if not self._cfg["enabled"] or self._cfg["backend"] == "disabled" else "TRANSLATOR_UNAVAILABLE"
                raise TranslatorError(code)
            if self._backend is None:
                self._backend = self._factory(cfg)
            return self._backend

    def _remaining(self, cfg):
        with self._lock:
            if self._day != self._today():
                self._day, self._count = self._today(), 0
            return max(0, cfg["limits"]["daily_limit"] - self._count)

    def _cached(self, rid):
        now = self._clock()
        for key in list(self._cache):
            if now - self._cache[key]["created"] >= self.TTL:
                self._cache.pop(key)
        if not isinstance(rid, str) or rid not in self._cache:
            raise TranslatorError("TRANSLATOR_REQUEST_EXPIRED")
        item = self._cache.pop(rid)
        self._cache[rid] = item
        return item

    def _status(self, state):
        cfg = state.cfg
        info = self._get_backend(cfg).status() if state.enabled else {"state": "disabled", "problem": state.problem}
        version = info.get("model_version")
        model = getattr(self._backend, "model", {}) or {}
        result = {"enabled": state.enabled, "backend": state.backend, "state": info["state"],
                "problem": info.get("problem"), "experimental": True,
                "model": {"version": version or model.get("version"), "grade": "experimental",
                          "adapter_sha256_short": (model.get("adapter_sha256") or "")[:8]},
                "limits": {"max_input_chars": cfg["limits"]["max_input_chars"],
                           "remaining_today": self._remaining(cfg)}, "notice_key": "ai.notice"}
        if state.backend == "api_llm":
            if not state.enabled:
                try:
                    info["budget"] = api_llm.UsageStore(cfg, self.root, today=self._today).snapshot()
                except TranslatorError:
                    info["budget"] = None
            result["budget"] = info.get("budget")
        return result

    def handle(self, method, path, body_bytes=b"", *, is_local=False, authenticated=False,
               requested_with=False, origin_ok=False):
        """访问控制结果由网站传入，默认拒绝。非本机检查始终优先于其他操作。

        S5 将现有 members.hit 作为 rate_hit 注入，签名和超限语义保持一致。
        未注入时用同样的固定 local 键语义在内存限速，不保存网络地址。
        """
        try:
            if is_local is not True:
                if self._audit_blocked:
                    try:
                        self._audit_blocked(path if path in _ROUTES else "/api/ai/")
                    except Exception:
                        pass  # 审计失败也不能改变外网请求一律 404 的行为
                return 404, {"error": "not found"}
            if authenticated is not True or (method == "POST" and requested_with is not True):
                return 401, {"error": "unauthorized"}
            if method == "POST" and origin_ok is not True:
                raise TranslatorError("TRANSLATOR_BAD_ORIGIN")
            if _ROUTES.get(path) != method:
                return 404, {"error": "not found"}
            state = self.loader.load()
            self._sync(state)
            if method == "GET":
                return 200, self._status(state)
            if not state.enabled:
                raise TranslatorError("TRANSLATOR_DISABLED")
            body = _body(body_bytes)
            if path.endswith("/translate"):
                return 200, self._translate(body, state.cfg)
            with self._lock:
                if path.endswith("/confirm"):
                    item = self._cached(body.get("request_id"))
                    result = item["result"]
                    if not result["can_confirm"]:
                        raise TranslatorError("TRANSLATOR_NOT_CONFIRMABLE")
                    if item["confirmed"]:
                        raise TranslatorError("TRANSLATOR_ALREADY_CONFIRMED")
                    if result["needs_ack"] and body.get("ack_flags") is not True:
                        raise TranslatorError("TRANSLATOR_ACK_REQUIRED")
                    self.logs.confirm(request_id=result["request_id"], ack_flags=body.get("ack_flags"))
                    item["confirmed"] = True
                    return 200, {"ok": True, "request_id": result["request_id"],
                                 "config": copy.deepcopy(result["config"]), "note_key": "ai.confirmed.note"}
                if body.get("verdict") != "wrong" or not isinstance(body.get("note", ""), str):
                    raise TranslatorError("TRANSLATOR_INPUT_INVALID")
                rid = body.get("request_id")
                try:
                    self._cached(rid)
                except TranslatorError:
                    if not self.logs.contains(rid):
                        raise TranslatorError("TRANSLATOR_REQUEST_EXPIRED")
                self.logs.feedback(request_id=rid, note=body.get("note", ""))
                return 200, {"ok": True}
        except TranslatorError as exc:
            return exc.http_status, {"error": exc.code, "message": exc.code}
        except Exception:
            return 502, {"error": "TRANSLATOR_BACKEND_FAILED", "message": "TRANSLATOR_BACKEND_FAILED"}

    def _translate(self, body, cfg):
        idea = body.get("idea")
        if not isinstance(idea, str) or not idea.strip():
            raise TranslatorError("TRANSLATOR_INPUT_INVALID")
        if len(idea) > cfg["limits"]["max_input_chars"]:
            raise TranslatorError("TRANSLATOR_INPUT_TOO_LONG")
        idea = "".join(c for c in idea if not unicodedata.category(c).startswith("C")).strip()
        if not idea:
            raise TranslatorError("TRANSLATOR_INPUT_INVALID")
        if not self._flight.acquire(blocking=False):
            raise TranslatorError("TRANSLATOR_BUSY")
        try:
            now = self._clock()
            if self._rate_hit:
                limited = self._rate_hit("ai_translate", "local", 10, 60)
            else:
                self._hits = [t for t in self._hits if now - t < 60]
                self._hits.append(now)
                limited = len(self._hits) > 10
            if limited:
                raise TranslatorError("TRANSLATOR_RATE_LIMITED")
            if self._remaining(cfg) == 0:
                raise TranslatorError("TRANSLATOR_DAILY_LIMIT")
            with self._lock:
                self._count += 1
            return self._run(idea, cfg, original_idea=body["idea"])
        finally:
            self._flight.release()

    def _run(self, idea, cfg, *, original_idea):
        rid = os.urandom(8).hex()
        check = precheck.precheck(idea)
        flags, view, doc, validation, clarify = [], [], None, None, None
        raw, mode, backend_id, version, latency = "", "none", "precheck", None, 0
        model = {}
        usage = None
        if cfg["backend"] == "api_llm":
            key = os.environ.get(cfg["api_llm"]["api_key_env"])
            if key:
                original_idea = original_idea.replace(key, "[redacted]")
        refusal = check["refusal"]
        error = None
        try:
            if not check["blocked"]:
                worker = self._get_backend(cfg)
                backend_id = worker.backend_id
                model = copy.deepcopy(getattr(worker, "model", {}) or {})
                if self.model_provider:
                    model = copy.deepcopy(self.model_provider() or {})
                elif isinstance(worker, local_lora.Supervisor):
                    model = copy.deepcopy(worker._selected() or {})
                version = model.get("version")
                out = worker.translate(idea, task="idea_to_config")
                usage = out.get("usage")
                if isinstance(worker, local_lora.Supervisor):
                    model["schema_sha256"] = worker.schema_sha256
                raw, mode = out["raw_output"], out["parse_mode"]
                backend_id, version, latency = out["backend_id"], out["model_version"], out["latency_ms"]
                # 元数据在翻译前取快照；翻译期间切换模型时，不把新模型的哈希记到旧结果上。
                if model.get("version") != version:
                    model = {}
                model["version"] = version
                if out["refusal"]:
                    raise TranslatorError(out["refusal"]["code"])
                doc = out["config"]
                if not isinstance(doc, dict) or mode != "strict":
                    raise TranslatorError("TRANSLATOR_BAD_OUTPUT")
                if "clarify" in doc or "reject" in doc:
                    raw = ""
                kind = output_kind(doc)
                if kind == "clarify":
                    clarify = copy.deepcopy(doc["clarify"])
                else:
                    # 在交给闸门前先清掉模型给"用不到的参数"填的值，并留下 warn（用户确认时要看到）。
                    doc, flags = unused.prune_unused(doc)
                    if self.oracle:
                        oracle = self.oracle
                    elif cfg["backend"] == "api_llm":
                        # API 模式不需要模型项目；配置和拒绝仍经过原来的隔离闸门。
                        if not cfg["statarb_src_dir"]:
                            raise TranslatorError("TRANSLATOR_UNAVAILABLE")
                        oracle = gate.Gate(sys.executable, paths.guard(cfg["statarb_src_dir"]))
                    else:
                        oracle = gate.Gate.from_config(cfg)
                    validation = oracle.validate(doc)
                    if not validation["legal"]:
                        # 只带白名单里的字段名和规则码；不回显校验器自由文本或内部路径。
                        fields, rules = plainview.gate_fields(validation.get("errors"))
                        refusal = plainview.make_refusal(validation["code"], "gate", fields=fields, rules=rules)
                    elif kind == "reject":
                        refusal = plainview.make_refusal(doc["reject"]["code"], "model")
                        refusal["message"] = doc["reject"]["message"]
                    else:
                        comparison = consistency.compare(idea, doc, oracle)
                        refusal, flags = comparison["refusal"], flags + comparison["flags"]
                        if not refusal:
                            view = plainview.build_view(doc, comparison["sources"])
        except TranslatorError as exc:
            error = exc
        except Exception:
            error = TranslatorError("TRANSLATOR_BACKEND_FAILED")
        status = "error" if error else ("clarify" if clarify else "rejected" if refusal else "ok")
        if refusal or clarify:
            raw = ""
        if error and error.code == "TRANSLATOR_BAD_OUTPUT":
            doc, raw = None, ""
        self.logs.translate(request_id=rid, backend_id=backend_id, model=model, idea=original_idea,
                            precheck={"blocked": check["blocked"], "rules": check["rules"]}, raw_output=raw,
                            parse_mode=mode, config=doc, gate=validation, flags=flags, status=status, latency_ms=latency,
                            usage=usage)
        if error:
            raise error
        result = {"request_id": rid, "status": status, "backend_id": backend_id, "model_version": version,
                  "experimental": True, "latency_ms": latency, "config": None if refusal or clarify else doc,
                  "refusal": refusal, "view": view, "flags": flags, "can_confirm": not (refusal or clarify),
                  "needs_ack": any(f["level"] == "warn" for f in flags)}
        if clarify:
            result["clarify"] = clarify
        with self._lock:
            self._cache[rid] = {"created": self._clock(), "confirmed": False, "result": copy.deepcopy(result)}
            while len(self._cache) > self.CACHE_SIZE:
                self._cache.pop(next(iter(self._cache)))
        return result

    def close(self):
        with self._lock:
            if self._backend is not None:
                self._backend.close()
                self._backend = None


_ROUTES = {"/api/ai/status": "GET", "/api/ai/translate": "POST",
           "/api/ai/confirm": "POST", "/api/ai/feedback": "POST"}
_default = None
_default_lock = threading.Lock()


def handle(method, path, body_bytes=b"", *, service=None, **access):
    """默认实例懒建；测试和其他宿主通过 service 注入，永不在 import 时读配置。"""
    global _default
    if service is None:
        with _default_lock:
            if _default is None:
                _default = API()
            service = _default
    return service.handle(method, path, body_bytes, **access)
