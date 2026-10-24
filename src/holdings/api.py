"""持仓台账接口处理函数(T013 · S2):纯函数,不接 app.py,不联网,不用 logging/print。

每个函数返回 (HTTP 状态码, 响应体):
  - 响应体是 dict(调用者序列化成 JSON),或固定纯文本 "not found" / "unauthorized"(鉴权失败时);
  - 所有金额/比例在响应里是定点小数字符串(如 "1234.50"),缺失为 None,绝不用 0 顶替;
  - 响应只含白名单字段,绝不回显请求原文;错误只含固定错误码、固定中文提示、可选字段名。

访问控制顺序(先命中先返回,三种失败的响应都是固定的,不泄露库里有没有数据):
  1. 非可信本机            -> 404 "not found"
  2. 会话无效(未认证)      -> 401 "unauthorized"
  3. 缺 X-Requested-With    -> 403 HOLDINGS_FORBIDDEN
  4. (POST)Origin 不对     -> 403 HOLDINGS_FORBIDDEN
Origin 与频率限制的真实判断在 app.py(S3);这里只接收 ctx.origin_ok 布尔值。
"""
import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Optional

from .calc import HoldingsError, MESSAGES, positions
from .store import HoldingsStore, beijing_today

MAX_BODY_BYTES = 16384
MAX_STR_LEN = 1000  # 任何字符串字段的硬上限(备注另有 200 字上限,由 store 校验)
DEFAULT_TRADES_LIMIT = 200

ADD_FIELDS = ("trade_date", "code", "side", "qty", "price", "fee", "signal_ref", "note", "client_id")
ADD_REQUIRED = ("trade_date", "code", "side", "qty")
VOID_FIELDS = ("id",)
CAPITAL_FIELDS = ("capital",)

# 字符串超长时,按字段报对应的固定错误码
_FIELD_ERRORS = {
    "trade_date": "HOLDINGS_BAD_DATE", "code": "HOLDINGS_BAD_CODE", "side": "HOLDINGS_BAD_SIDE",
    "qty": "HOLDINGS_BAD_QTY", "price": "HOLDINGS_BAD_PRICE", "fee": "HOLDINGS_BAD_FEE",
    "signal_ref": "HOLDINGS_BAD_SIGNAL_REF", "note": "HOLDINGS_BAD_NOTE",
    "client_id": "HOLDINGS_BAD_CLIENT_ID", "id": "HOLDINGS_BAD_ID", "capital": "HOLDINGS_BAD_CAPITAL",
}
_STR_LIMITS = {"note": MAX_STR_LEN}  # 其余字符串字段 ≤ 64
_SHORT_STR = 64

# 额外的接口层错误码(固定提示)
_API_MESSAGES = {
    "HOLDINGS_FORBIDDEN": "请求被拒绝",
    "HOLDINGS_BAD_JSON": "请求体不是合法的 JSON 对象",
    "HOLDINGS_UNKNOWN_FIELD": "请求包含不支持的字段",
    "HOLDINGS_MISSING_FIELD": "缺少必填字段",
    "HOLDINGS_TOO_LARGE": "请求体过大",
    "HOLDINGS_RATE_LIMITED": "请求过于频繁,请稍后再试",
}

_STATUS = {
    "HOLDINGS_TRADE_NOT_FOUND": 404,
    "HOLDINGS_CLIENT_ID_CONFLICT": 409,
    "HOLDINGS_TOO_LARGE": 413,
    "HOLDINGS_RATE_LIMITED": 429,
    "HOLDINGS_STORE_UNSAFE": 500,
    "HOLDINGS_INTERNAL": 500,
    "HOLDINGS_FORBIDDEN": 403,
}

NOT_FOUND = (404, "not found")
UNAUTHORIZED = (401, "unauthorized")


@dataclass
class Ctx:
    """调用者(app.py)在每个请求里构造一份。除 db_path 外的字段都由调用者判定,这里只消费。"""
    db_path: Any = None                 # 持仓库路径;None 用固定的 data/holdings.db
    trusted_local: bool = False         # 可信本机(Host 合法、无代理/隧道头、非分享/trycloudflare)
    dashboard_header: bool = False      # 带 X-Requested-With: dashboard
    authed: bool = False                # 网站令牌 cookie 有效
    last_price_fn: Optional[Callable] = None  # code -> None | 价格 | (价格, 日期);来自本地日线只读缓存
    today: Any = None                   # 北京日期(date 或 YYYY-MM-DD);None 取 beijing_today()
    origin_ok: bool = False             # POST 的 Origin 检查结果(S3 必须显式填写;默认拒绝)
    name_fn: Optional[Callable] = None  # code -> 名称;取不到时用代码
    fee_fn: Optional[Callable] = None   # (code, side, amount) -> 费用估算;None 用默认估算
    log_fn: Optional[Callable] = None   # 只会收到固定动作名,从不收到数量/价格/代码


# ---------- 公共辅助 ----------

def _err(code, status=None, field=None):
    msg = MESSAGES.get(code) or _API_MESSAGES.get(code) or MESSAGES["HOLDINGS_INTERNAL"]
    if code not in MESSAGES and code not in _API_MESSAGES:
        code = "HOLDINGS_INTERNAL"
    body = {"error": code, "message": msg}
    if field in _FIELD_ERRORS:  # 字段名只来自固定白名单
        body["field"] = field
    return (status or _STATUS.get(code, 400), body)


def _from_exc(e):
    if isinstance(e, HoldingsError):
        return _err(e.code, field=e.field)
    return _err("HOLDINGS_INTERNAL")  # 任何意外异常:只给固定码,绝不带异常文本


def _gate(ctx, write):
    """鉴权;通过返回 None,否则返回固定 (状态码, 响应体)。"""
    if not ctx.trusted_local:
        return NOT_FOUND
    if not ctx.authed:
        return UNAUTHORIZED
    if not ctx.dashboard_header:
        return _err("HOLDINGS_FORBIDDEN")
    if write and not ctx.origin_ok:
        return _err("HOLDINGS_FORBIDDEN")
    return None


def _log(ctx, action):
    if ctx.log_fn is not None:
        try:
            ctx.log_fn(action)
        except Exception:
            pass


def _store(ctx):
    return HoldingsStore(ctx.db_path) if ctx.db_path is not None else HoldingsStore()


def _today(ctx):
    t = ctx.today
    if t is None:
        return beijing_today()
    return t if isinstance(t, date) else date.fromisoformat(str(t))


def _money(x, places="0.01"):
    """Decimal -> 定点字符串;None 保持 None。"""
    if x is None:
        return None
    return format(Decimal(x).quantize(Decimal(places)), "f")


def _name(ctx, code):
    if ctx.name_fn is None:
        return code
    try:
        n = ctx.name_fn(code)
        return n[:40] if isinstance(n, str) and n else code
    except Exception:
        return code


def _parse_body(raw, allowed, required=()):
    """校验请求体:对象、无未知键、必填齐全、字符串长度。返回 dict,失败抛 HoldingsError。"""
    if isinstance(raw, (bytes, bytearray, str)):
        size = len(raw) if isinstance(raw, (bytes, bytearray)) else len(raw.encode("utf-8", "replace"))
        if size > MAX_BODY_BYTES:
            raise HoldingsError("HOLDINGS_TOO_LARGE")
        try:
            raw = json.loads(raw)
        except (ValueError, RecursionError):
            raise HoldingsError("HOLDINGS_BAD_JSON") from None
    if not isinstance(raw, dict):
        raise HoldingsError("HOLDINGS_BAD_JSON")
    if any(k not in allowed for k in raw):
        raise HoldingsError("HOLDINGS_UNKNOWN_FIELD")
    for k in required:
        if k not in raw:
            raise HoldingsError("HOLDINGS_MISSING_FIELD")
    for k, v in raw.items():
        if v is not None and not isinstance(v, (str, int, float)):  # 不接受列表/对象等嵌套类型
            raise HoldingsError(_FIELD_ERRORS[k], k)
        if isinstance(v, str) and len(v) > _STR_LIMITS.get(k, _SHORT_STR):
            raise HoldingsError(_FIELD_ERRORS[k], k)
    return raw


def _trade_view(ctx, t):
    return {
        "id": t["id"], "trade_date": t["trade_date"], "code": t["code"], "name": _name(ctx, t["code"]),
        "side": t["side"], "qty": t["qty"], "price": _money(t["price"], "0.001"),
        "fee": _money(t["fee"]), "fee_estimated": bool(t["fee_estimated"]),
        "signal_ref": t["signal_ref"], "note": t["note"], "voided": bool(t["voided"]),
        "created_at": t["created_at"],
    }


def _query_int(query, key, default, lo, hi):
    try:
        v = int(str((query or {}).get(key, default)))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


# ---------- 四个处理函数 ----------

def handle_get(ctx, query=None):
    """GET /api/holdings。query 可含 trades_limit(1..1000,默认 200)、include_voided(0/1)。"""
    denied = _gate(ctx, write=False)
    if denied:
        return denied
    try:
        store = _store(ctx)
        limit = _query_int(query, "trades_limit", DEFAULT_TRADES_LIMIT, 1, 1000)
        include_voided = _query_int(query, "include_voided", 0, 0, 1) == 1
        today = _today(ctx)
        capital = store.get_capital()
        res = positions(store.load_trades(), ctx.last_price_fn, today, capital)
        trades = store.list_trades(limit=limit, include_voided=include_voided)
        s = res["summary"]
        body = {
            "ok": True,
            "as_of": today.isoformat(),
            "capital": _money(capital),
            "summary": {
                "market_value": _money(s["market_value"]), "total_cost": _money(s["total_cost"]),
                "floating_pnl": _money(s["floating_pnl"]), "realized": _money(s["realized"]),
                "dividends": _money(s["dividends"]), "invested_ratio": _money(s["invested_ratio"], "0.0001"),
                "missing_price_count": s["missing_price_count"],
            },
            "positions": [{
                "code": p["code"], "name": _name(ctx, p["code"]), "shares": p["shares"],
                "avg_cost": _money(p["avg_cost"], "0.001"), "first_buy": p["first_buy"],
                "held_days": p["held_days"], "price": _money(p["price"], "0.001"),
                "price_date": p["price_date"], "market_value": _money(p["market_value"]),
                "floating_pnl": _money(p["floating_pnl"]),
                "floating_pct": _money(p["floating_pct"], "0.0001"),
                "weight": _money(p["weight"], "0.0001"),
                "realized": _money(p["realized"]), "dividends": _money(p["dividends"]),
            } for p in res["positions"]],
            "closed": [{
                "code": c["code"], "name": _name(ctx, c["code"]), "first_buy": c["first_buy"],
                "last_sell": c["last_sell"], "held_days": c["held_days"], "pnl": _money(c["pnl"]),
            } for c in res["closed"]],
            "trades": [_trade_view(ctx, t) for t in trades],
        }
        _log(ctx, "holdings_get")
        return 200, body
    except Exception as e:  # noqa: BLE001 —— 统一收敛成固定错误码
        return _from_exc(e)


def handle_add(ctx, body):
    """POST /api/holdings/add。body 为已解析的 dict(也接受原始 bytes/str)。"""
    denied = _gate(ctx, write=True)
    if denied:
        return denied
    try:
        b = _parse_body(body, ADD_FIELDS, ADD_REQUIRED)
        r = _store(ctx).add_trade(
            trade_date=b["trade_date"], code=b["code"], side=b["side"], qty=b["qty"],
            price=b.get("price"), fee=b.get("fee"), signal_ref=b.get("signal_ref"),
            note=b.get("note"), client_id=b.get("client_id"),
            today=_today(ctx), fee_fn=ctx.fee_fn)
        _log(ctx, "holdings_add")
        return 200, {"ok": True, "duplicate": bool(r["duplicate"]), "trade": _trade_view(ctx, r["trade"])}
    except Exception as e:  # noqa: BLE001
        return _from_exc(e)


def handle_void(ctx, body):
    """POST /api/holdings/void:{"id": 整数}。"""
    denied = _gate(ctx, write=True)
    if denied:
        return denied
    try:
        b = _parse_body(body, VOID_FIELDS, VOID_FIELDS)
        store = _store(ctx)
        # 库文件还不存在时任何 id 都不可能存在:直接报找不到,不为撤销而建库(id 格式仍先校验)
        p = Path(store.path)
        if not (p.is_symlink() or p.exists()):
            if not isinstance(b["id"], int) or isinstance(b["id"], bool) or b["id"] < 1:
                raise HoldingsError("HOLDINGS_BAD_ID", "id")
            raise HoldingsError("HOLDINGS_TRADE_NOT_FOUND")
        store.void_trade(b["id"])
        _log(ctx, "holdings_void")
        return 200, {"ok": True}
    except Exception as e:  # noqa: BLE001
        return _from_exc(e)


def handle_capital(ctx, body):
    """POST /api/holdings/capital:{"capital": 数字}。"""
    denied = _gate(ctx, write=True)
    if denied:
        return denied
    try:
        b = _parse_body(body, CAPITAL_FIELDS, CAPITAL_FIELDS)
        cap = _store(ctx).set_capital(b["capital"])
        _log(ctx, "holdings_capital")
        return 200, {"ok": True, "capital": _money(cap)}
    except Exception as e:  # noqa: BLE001
        return _from_exc(e)
