"""AI 翻译的配置：加载、校验、损坏时回退 disabled、原子写。

原则：
- 默认关闭。文件不存在、`enabled` 不是严格的 true、后端是 disabled，都按关闭处理。
- 加载失败不崩：任何异常都在 ConfigLoader.load() 里吞掉，转成 disabled + problem="CONFIG_INVALID"。
- 密钥不进配置：任何名字含 key / secret / token / password 的字段都判整份配置损坏，
  唯一的例外是 api_llm.api_key_env，它只能写环境变量的名字。
- 审计只写原因类别和布尔值，不写文件内容、不写想法原文。
"""
import hashlib
import json
import math
import os
import re
import tempfile
import threading
from urllib.parse import urlsplit
from dataclasses import dataclass

from . import paths

SCHEMA = 1
BACKENDS = ("disabled", "local_lora", "api_llm")
TEST_BACKENDS = ("fake",)               # 只在 QB_TESTING=1 时允许；生产配置里出现就判损坏
MAX_FILE_BYTES = 64 * 1024
SECRET_WORDS = ("key", "secret", "token", "password")
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")      # 环境变量名：全大写，粘贴进来的密钥通常过不了这一关
_VERSION = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

# 本版的默认值：空闲回收 120 秒、日志保留 90 天；ai_layer_dir 默认为空（没配置就不可用，不猜位置）；没有影子模式配置
DEFAULTS = {
    "schema": SCHEMA,
    "enabled": False,
    "backend": "disabled",
    "expect_model_version": None,
    "ai_layer_dir": None,
    "statarb_src_dir": None,            # 校验器所在项目的 src 目录；闸门子进程用，没配置就不可用
    "limits": {"max_input_chars": 500, "daily_limit": 200},
    "timeouts": {"load_s": 120, "request_s": 60, "idle_s": 120},
    "api_llm": {"provider": None, "model": "claude-haiku-5-5", "base_url": "https://api.anthropic.com",
                "api_key_env": "AI_TRANSLATOR_API_KEY", "timeout_s": 30,
                "max_monthly_usd": 2.00, "max_daily_requests": 100,
                "price_input_per_mtok": 0.10, "price_output_per_mtok": 0.50},
    "log": {"retention_days": 90},
}


class ConfigError(Exception):
    """配置不合法。reason 是原因类别（syntax / type / range / ...），不含文件内容。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ConfigState:
    enabled: bool               # 有效开关：enabled 为 true 且后端不是 disabled，并且配置没损坏
    backend: str
    cfg: dict                   # 校验过、补全了默认值的配置；损坏时是默认值
    problem: str | None         # None 或 "CONFIG_INVALID"
    reason: str | None          # 损坏的原因类别
    digest: str | None          # 文件内容 sha256；文件不存在是 None
    exists: bool


def defaults() -> dict:
    return json.loads(json.dumps(DEFAULTS))


# ---------------------------------------------------------------- 校验
def _scan_secret(node, path=()):
    if isinstance(node, dict):
        for k, v in node.items():
            if not isinstance(k, str):
                raise ConfigError("type")
            low = k.lower()
            if any(w in low for w in SECRET_WORDS) and path + (k,) != ("api_llm", "api_key_env"):
                raise ConfigError("secret_field")
            _scan_secret(v, path + (k,))
    elif isinstance(node, list):
        for v in node:
            _scan_secret(v, path)


def _merge(base: dict, raw: dict) -> dict:
    out = {}
    for k in raw:
        if k not in base:
            raise ConfigError("unknown_key")
    for k, dv in base.items():
        if k not in raw:
            out[k] = json.loads(json.dumps(dv))
        elif isinstance(dv, dict):
            if not isinstance(raw[k], dict):
                raise ConfigError("type")
            out[k] = _merge(dv, raw[k])
        else:
            out[k] = raw[k]
    return out


def _num(v, *, hi, integer=False, lo=None):
    """bool 不算数字；有限；整数项必须是整数。lo 给了就是闭下界，否则要求 > 0。"""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ConfigError("type")
    if isinstance(v, float) and not math.isfinite(v):
        raise ConfigError("range")
    if integer and not isinstance(v, int):
        raise ConfigError("type")
    if (v < lo) if lo is not None else (v <= 0):
        raise ConfigError("range")
    if v > hi:
        raise ConfigError("range")


def _opt_str(v, maxlen):
    if v is None:
        return
    if not isinstance(v, str) or not v or len(v) > maxlen or "\0" in v:
        raise ConfigError("type")


def api_base_url(value):
    """精确限制目的地；本机假服务器只在测试模式开放，不允许凭据、路径或查询串。"""
    if value == "https://api.anthropic.com":
        return value
    try:
        url = urlsplit(value) if isinstance(value, str) else None
        if (os.environ.get("QB_TESTING") == "1" and url and url.scheme == "http"
                and url.hostname in ("127.0.0.1", "localhost") and url.port != 0
                and url.netloc in (url.hostname, f"{url.hostname}:{url.port}") and not url.path
                and not url.query and not url.fragment):
            return value
    except ValueError:
        pass
    raise ConfigError("range")


def validate(raw) -> dict:
    """返回补全默认值后的新配置；不合法抛 ConfigError。不修改入参。"""
    if not isinstance(raw, dict):
        raise ConfigError("type")
    _scan_secret(raw)
    cfg = _merge(DEFAULTS, raw)
    if isinstance(cfg["schema"], bool) or cfg["schema"] != SCHEMA:
        raise ConfigError("schema")
    if not isinstance(cfg["enabled"], bool):
        raise ConfigError("type")
    allowed = BACKENDS + (TEST_BACKENDS if os.environ.get("QB_TESTING") == "1" else ())
    if not isinstance(cfg["backend"], str) or cfg["backend"] not in allowed:
        raise ConfigError("unknown_backend")
    ev = cfg["expect_model_version"]
    if ev is not None and not (isinstance(ev, str) and _VERSION.match(ev)):
        raise ConfigError("type")
    _opt_str(cfg["ai_layer_dir"], 1024)
    _opt_str(cfg["statarb_src_dir"], 1024)
    lim, tmo, api, log = cfg["limits"], cfg["timeouts"], cfg["api_llm"], cfg["log"]
    _num(lim["max_input_chars"], hi=4000, integer=True, lo=1)
    _num(lim["daily_limit"], hi=100000, integer=True, lo=1)
    _num(tmo["load_s"], hi=3600)
    _num(tmo["request_s"], hi=600)
    _num(tmo["idle_s"], hi=86400)
    for k in ("provider", "model"):
        _opt_str(api[k], 128)
    _opt_str(api["base_url"], 512)
    # 旧版本保存的本地配置含两个 null 占位；加载时补默认值，不让本地后端意外停用。
    if cfg["backend"] != "api_llm":
        for k in ("model", "base_url"):
            if api[k] is None:
                api[k] = DEFAULTS["api_llm"][k]
    if api["base_url"] is not None and "@" in api["base_url"]:      # 网址里不许夹带账号密码
        raise ConfigError("secret_field")
    api_base_url(api["base_url"])
    if not isinstance(api["model"], str) or not _VERSION.fullmatch(api["model"]):
        raise ConfigError("type")
    _num(api["max_monthly_usd"], hi=50, lo=0)
    _num(api["max_daily_requests"], hi=10000, lo=0, integer=True)
    for k in ("price_input_per_mtok", "price_output_per_mtok"):
        _num(api[k], hi=1000, lo=0)
    if not (isinstance(api["api_key_env"], str) and _ENV_NAME.match(api["api_key_env"])):
        raise ConfigError("secret_field")
    _num(api["timeout_s"], hi=600)
    _num(log["retention_days"], hi=3650, integer=True, lo=1)
    return cfg


def _no_constant(name):
    raise ValueError(name)          # 拒绝 NaN / Infinity


def _no_duplicates(pairs):
    d = {}
    for k, v in pairs:
        if k in d:
            raise ValueError("duplicate key")
        d[k] = v
    return d


def _state_from(cfg: dict, digest: str) -> ConfigState:
    on = cfg["enabled"] is True and cfg["backend"] != "disabled"
    return ConfigState(on, cfg["backend"], cfg, None, None, digest, True)


def _invalid(reason: str, digest: str | None, exists: bool = True) -> ConfigState:
    return ConfigState(False, "disabled", defaults(), "CONFIG_INVALID", reason, digest, exists)


def _missing() -> ConfigState:
    return ConfigState(False, "disabled", defaults(), None, None, None, False)


# ---------------------------------------------------------------- 加载与保存
class ConfigLoader:
    """每次请求调用 load()：只做一次 stat（mtime+size），变了才重新读。"""

    def __init__(self, root=None, audit=None):
        self._path = paths.config_path(root)        # 测试模式下，指向真实 data/ 会在这里抛 TripwireError
        self._audit = audit                          # 签名同 admin.audit(action, detail, ok, error)
        self._lock = threading.Lock()
        self._sig = None
        self._state: ConfigState | None = None
        self._reported_bad = None                    # 已经审计过的损坏内容，避免每次请求都写一条

    @property
    def path(self):
        return self._path

    def _emit(self, action, detail=None, ok=True, error=None):
        if self._audit is None:
            return
        try:
            self._audit(action, detail, ok=ok, error=error)
        except Exception:
            pass                                      # 审计失败不能影响网站

    def _read(self) -> ConfigState:
        try:
            with open(self._path, "rb") as f:
                data = f.read(MAX_FILE_BYTES + 1)
        except FileNotFoundError:
            return _missing()
        except OSError:
            return _invalid("unreadable", None)
        digest = hashlib.sha256(data).hexdigest()
        if len(data) > MAX_FILE_BYTES:
            return _invalid("too_large", digest)
        try:
            raw = json.loads(data.decode("utf-8"), parse_constant=_no_constant, object_pairs_hook=_no_duplicates)
        except Exception:
            return _invalid("syntax", digest)
        try:
            return _state_from(validate(raw), digest)
        except ConfigError as e:
            return _invalid(e.reason, digest)
        except Exception:
            return _invalid("internal", digest)

    def _apply(self, new: ConfigState, via: str):
        """记录状态变化并写审计。via="file" 表示改动来源不明（可能是手改文件）。"""
        prev = self._state
        if new.problem:
            key = new.digest or new.reason
            if key != self._reported_bad:
                self._reported_bad = key
                self._emit("ai_translator_config_invalid", {"reason": new.reason}, ok=False)
        else:
            self._reported_bad = None
        if prev is not None:
            if via == "file" and new.digest != prev.digest:
                self._emit("ai_translator_config_changed", {"via": "file"})
            if new.enabled != prev.enabled:
                self._emit("ai_translator_toggle", {"enabled": new.enabled, "via": via})
        self._state = new

    def load(self) -> ConfigState:
        """永远不抛异常。"""
        try:
            with self._lock:
                try:
                    st = os.stat(self._path)
                    sig = (st.st_mtime_ns, st.st_size)
                except FileNotFoundError:
                    sig = "missing"
                except OSError:
                    sig = None
                if sig is not None and sig == self._sig and self._state is not None:
                    return self._state
                new = self._read()
                self._sig = sig
                self._apply(new, "file")
                return self._state
        except Exception:
            return _invalid("internal", None)

    def save(self, cfg: dict, via: str = "admin") -> ConfigState:
        """校验后原子写：临时文件 -> os.replace，权限 600。不合法抛 ConfigError，原文件不变。"""
        norm = validate(cfg)
        self.load()                                   # 先建立“改动前”的状态，这样开关变化能被审计
        parent = self._path.parent
        parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=parent, prefix=".ai_translator.", suffix=".tmp")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(norm, ensure_ascii=False, indent=2) + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._path)
            os.chmod(self._path, 0o600)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        with self._lock:
            self._sig = None                          # 下次 load 重新 stat
            self._apply(self._read(), via)
            return self._state
