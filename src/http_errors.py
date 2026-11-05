"""HTTP 请求体读取与统一的错误响应格式(B15)。

app.py 里的 POST 接口以前各自读请求体：Content-Length 写成 "abc" 会抛 ValueError 变成 500，
JSON 坏了会把解析器的英文报错原样回给调用方，体过大返回 400 而不是 413，JSON 是数组时被悄悄当成空对象。
这里把「读体 + 校验」收成一处，失败统一抛 BodyError，由接线处转成：

    {"ok": false, "error": "<固定错误码>", "message": "<中文说明>"}

error 保持字符串，因为现有前端（admin.js、app.js 等）都把 j.error 当文本读；
ok / message 是新增字段，老前端忽略它们不受影响。纯标准库，不碰文件和网络。
"""
import json

MAX_JSON_BODY_BYTES = 1_000_000     # JSON 请求体上限（原来 app.py._body 里写死的 1_000_000）


class BodyError(ValueError):
    """请求体不合格：带 HTTP 状态码和固定错误码。
    继承 ValueError：没加专门处理的老调用点（except ValueError / except Exception）照旧能接住，不会变成 500。"""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message

    @property
    def payload(self) -> dict:
        return {"ok": False, "error": self.code, "message": self.message}


def content_length(headers) -> int:
    """Content-Length 转整数；缺省或空串 = 0；不是整数返回 -1（调用方的 0 <= n <= 上限 检查会拒掉，不再抛 ValueError）。"""
    try:
        return int(headers.get("Content-Length") or 0)
    except ValueError:
        return -1


def read_json_object(headers, rfile, limit: int = MAX_JSON_BODY_BYTES) -> dict:
    """读一个 JSON 对象请求体。没有请求体（Content-Length 缺省或 0）返回 {}。
    失败抛 BodyError：400 BAD_CONTENT_LENGTH / 413 BODY_TOO_LARGE / 400 BAD_JSON / 400 BODY_NOT_OBJECT。
    体过大时一个字节都不读（避免被大请求拖着读）。"""
    n = content_length(headers)
    if n < 0:
        raise BodyError(400, "BAD_CONTENT_LENGTH", "Content-Length 不合法")
    if n == 0:
        return {}
    if n > limit:
        raise BodyError(413, "BODY_TOO_LARGE", "请求体太大")
    try:
        obj = json.loads(rfile.read(n).decode("utf-8"))
    except ValueError:      # JSONDecodeError 和 UnicodeDecodeError 都是它的子类；不把解析器的英文报错回显给调用方
        raise BodyError(400, "BAD_JSON", "请求体不是合法的 JSON")
    if not isinstance(obj, dict):
        raise BodyError(400, "BODY_NOT_OBJECT", "请求体必须是 JSON 对象")
    return obj
