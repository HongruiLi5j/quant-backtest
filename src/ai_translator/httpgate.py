"""HTTP 入口的纯判定函数：不读文件、不认证令牌、不记录网络地址。

调用方按顺序取得判定结果；认证失败时不得继续检查来源或消耗限额。
返回 None 表示放行，否则返回 (状态码, 纯文本字节或 JSON 对象)。
"""


def check_access(method, *, is_local, authenticated=False, requested_with=False,
                 origin_ok=False, rate_limited=False):
    if not is_local:
        return 404, b"not found"
    if not authenticated or (method == "POST" and not requested_with):
        return 401, b"unauthorized"
    if method == "POST" and not origin_ok:
        return 403, {"error": "TRANSLATOR_BAD_ORIGIN", "message": "TRANSLATOR_BAD_ORIGIN"}
    if rate_limited:
        return 429, {"error": "TRANSLATOR_RATE_LIMITED", "message": "TRANSLATOR_RATE_LIMITED"}
    return None
