"""翻译后端的统一接口和错误类型。网站其余代码只依赖这个文件，不知道后端是本地模型还是 API。"""
from abc import ABC, abstractmethod
from .plainview import LABELS, REJECT_CODES

MAX_MESSAGE_CHARS = 200


def output_kind(doc):
    """只检查输出协议形状；配置取值、必填项和组合仍由原闸门判断。"""
    def bad():
        raise TranslatorError("TRANSLATOR_BAD_OUTPUT")

    if not isinstance(doc, dict):
        bad()
    controls = set(doc) & {"reject", "clarify"}
    if controls:
        if len(controls) != 1:
            bad()
        kind = next(iter(controls))
        if set(doc) != {"schema_version", kind} or doc["schema_version"] != "1.0":
            bad()
        body = doc[kind]
        if not isinstance(body, dict) or set(body) != ({"code", "message"} if kind == "reject" else {"message"}):
            bad()
        message = body["message"]
        if not isinstance(message, str) or not message.strip() or len(message) > MAX_MESSAGE_CHARS:
            bad()
        if kind == "reject" and body["code"] not in REJECT_CODES:
            bad()
        return kind
    allowed = {path.split(".")[0] for path in LABELS}
    if set(doc) - allowed:
        bad()
    for section, body in doc.items():
        if isinstance(body, dict):
            keys = {path.split(".")[1] for path in LABELS if path.startswith(section + ".")}
            if set(body) - keys:
                bad()
    return "config"

# status().state 的取值
STATES = ("disabled", "unavailable", "idle", "loading", "ready", "busy", "cooldown")

# 解析方式：本版只接受严格 JSON，没有宽松提取
PARSE_MODES = ("strict", "none")

# 接口错误码 -> HTTP 状态码（设计文档 2.7）。错误响应里只放码，不放模型原文、堆栈、路径
ERROR_HTTP = {
    "TRANSLATOR_BAD_ORIGIN": 403,
    "TRANSLATOR_INPUT_INVALID": 400,
    "TRANSLATOR_INPUT_TOO_LONG": 400,
    "TRANSLATOR_DISABLED": 503,
    "TRANSLATOR_UNAVAILABLE": 503,
    "TRANSLATOR_BUSY": 429,
    "TRANSLATOR_RATE_LIMITED": 429,
    "TRANSLATOR_DAILY_LIMIT": 429,
    "TRANSLATOR_BUDGET_EXCEEDED": 429,
    "TRANSLATOR_TIMEOUT": 504,
    "TRANSLATOR_BACKEND_FAILED": 502,
    "TRANSLATOR_BAD_OUTPUT": 502,
    "TRANSLATOR_NOT_CONFIRMABLE": 409,
    "TRANSLATOR_ACK_REQUIRED": 409,
    "TRANSLATOR_ALREADY_CONFIRMED": 409,
    "TRANSLATOR_REQUEST_EXPIRED": 410,
}


class TranslatorError(Exception):
    """后端或流程失败。str(e) 只有错误码，所以不会把内部细节带到响应里。"""

    def __init__(self, code: str):
        if code not in ERROR_HTTP:
            raise ValueError(f"未知错误码：{code}")
        super().__init__(code)
        self.code = code

    @property
    def http_status(self) -> int:
        return ERROR_HTTP[self.code]


def make_result(*, config=None, refusal=None, raw_output="", parse_mode="none",
                backend_id, model_version, latency_ms=0) -> dict:
    """config 为解析文档（配置/reject/clarify），refusal 为后端错误；二选一。"""
    if (config is None) == (refusal is None):
        raise ValueError("config 和 refusal 必须二选一")
    if config is not None and not isinstance(config, dict):
        raise ValueError("config 必须是 dict")
    if refusal is not None and not (isinstance(refusal, dict) and isinstance(refusal.get("code"), str)):
        raise ValueError("refusal 必须是带 code 的 dict")
    if parse_mode not in PARSE_MODES:
        raise ValueError(f"parse_mode 必须是 {PARSE_MODES}")
    return {
        "config": config,
        "refusal": refusal,
        "raw_output": raw_output,          # 只写日志，不返回给前端
        "parse_mode": parse_mode,
        "backend_id": backend_id,
        "model_version": model_version,
        "latency_ms": int(latency_ms),
    }


class TranslatorBackend(ABC):
    backend_id: str = ""

    @abstractmethod
    def status(self) -> dict:
        """不触发加载。至少返回 {"state": STATES 之一, "problem": 代码或 None}。"""

    @abstractmethod
    def translate(self, idea_text: str, *, task: str = "idea_to_config") -> dict:
        """返回 make_result(...) 的结构。task 目前只有 idea_to_config。"""

    @abstractmethod
    def close(self) -> None:
        """回收资源（比如子进程）。可重复调用。"""
