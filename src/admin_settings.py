"""后台设置（BACKEND_SPEC_admin_switches §2.1、§2.8）：存在 members 库的 settings 表里，value 是 JSON。

- 只接受白名单里的键；一次可以改多个，先全部校验、再在一个事务里写入，要么全成功要么全不改。
- 所有 SQL 都用 ? 参数；建表只用 CREATE TABLE IF NOT EXISTS，可以重复执行。
- 读不到（库坏了、被锁）时：先用上次成功读到的值，再不行用安全默认（注册关）。
- 开关只在服务端判断：前端只负责显示。
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone

from src import members

# 能被分区开关管的页面 id（和 dashboard/ui/nav.js 里已做好的新页面一一对应，文件在 dashboard/ui/w/<id>.html）
SECTION_IDS = ("today", "signals", "market", "watch", "backtest", "holdings", "monitor", "research", "settings")

DEFAULTS = {
    "default_tier": "free",
    "membership_on": True,
    "share_links_on": True,
    "registration_open": False,     # 默认关：所有者在后台打开之前，任何人都不能注册
    "maintenance_on": False,        # 维护模式：默认关（读不到设置时网站保持开放）
    "maintenance_until": "",        # 预计恢复时间（文本，≤ 40 字）；用来算 Retry-After
    "features": {"member_backtest": True, "engines": True, "translate": True},
    "quota": {"free_backtest": 3, "pro_backtest": 30, "free_translate": 5, "pro_translate": 50},
    # 分区开关（§1 表“分区”一行）：新界面 /ui/w/<id>.html 每页一个；on=false 时非所有者看到“暂未开放”页（不是 404）
    "sections": {sid: {"on": True} for sid in SECTION_IDS},
}
TIERS = ("free", "pro")
_BOOL_KEYS = ("membership_on", "share_links_on", "registration_open", "maintenance_on")
UNTIL_MAX = 40
DEFAULT_RETRY_AFTER = 3600
_lock = threading.RLock()
_last_good: dict | None = None


class SettingError(Exception):
    """校验失败；key 是出错的键名（嵌套的写成 quota.free_backtest）。"""

    def __init__(self, key: str, msg: str = ""):
        super().__init__(msg or key)
        self.key = key


def _ensure(c):
    c.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT)")


def _copy(d: dict) -> dict:
    return json.loads(json.dumps(d))


def get_all() -> dict:
    """返回完整设置（库里没有的键补默认值）。读失败时回退到上次成功的值，再回退到默认。"""
    global _last_good
    try:
        out = _copy(DEFAULTS)
        with _lock, members._conn() as c:
            _ensure(c)
            for k, v in c.execute("SELECT key, value FROM settings").fetchall():
                if k not in DEFAULTS:
                    continue                      # 库里的陌生键一律忽略（不在白名单）
                val = json.loads(v)
                if k == "sections":
                    if isinstance(val, dict):      # 只认 {"on": true/false}；脏数据保持默认（开放）
                        for a, b in val.items():
                            if a in DEFAULTS[k] and isinstance(b, dict) and isinstance(b.get("on"), bool):
                                out[k][a] = {"on": b["on"]}
                elif isinstance(DEFAULTS[k], dict):
                    if isinstance(val, dict):
                        out[k].update({a: b for a, b in val.items() if a in DEFAULTS[k]})
                else:
                    out[k] = val
        _last_good = _copy(out)
        return out
    except Exception:
        return _copy(_last_good) if _last_good else _copy(DEFAULTS)


def registration_open() -> bool:
    """注册接口用：只有明确等于 True 才放行，任何异常/脏数据都当作关闭。"""
    return get_all().get("registration_open") is True


def section_open(sid: str) -> bool:
    """分区开关用：只有明确关掉（on 为 False）的页面才算关；不认识的 id、读失败都当作开放。"""
    sec = get_all().get("sections", {}).get(sid)
    return not (isinstance(sec, dict) and sec.get("on") is False)


def maintenance() -> tuple[bool, str]:
    """维护模式用：只有明确等于 True 才算开启；读失败时沿用上次的值，再不行就是关（网站开放）。"""
    st = get_all()
    until = st.get("maintenance_until")
    return st.get("maintenance_on") is True, until if isinstance(until, str) else ""


def retry_after_seconds(until: str, now: float | None = None) -> int:
    """Retry-After 的秒数：until 能解析成时间（YYYY-MM-DD[ HH:MM]，按北京时间）且在未来就取差值，
    否则（空、写成“今晚”之类、已过期）给 3600 秒。限制在 60 秒到 7 天之间。"""
    now = time.time() if now is None else now
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            t = datetime.strptime(str(until).strip(), fmt).replace(tzinfo=timezone(timedelta(hours=8)))
        except ValueError:
            continue
        diff = int(t.timestamp() - now)
        if diff > 0:
            return max(60, min(diff, 7 * 86400))
        break
    return DEFAULT_RETRY_AFTER


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def validate(patch: dict) -> dict:
    """校验并规范化 patch，返回要写入的 {键: 完整新值}。未知键、类型不对、数字越界都抛 SettingError(键名)。"""
    if not isinstance(patch, dict) or not patch:
        raise SettingError("body", "没有要修改的内容")
    cur = get_all()
    out = {}
    for k, v in patch.items():
        if k not in DEFAULTS:
            raise SettingError(str(k)[:40], "不认识的设置项")
        if k in _BOOL_KEYS:
            if not isinstance(v, bool):
                raise SettingError(k, "必须是 true 或 false")
            out[k] = v
        elif k == "maintenance_until":
            if not isinstance(v, str) or len(v.strip()) > UNTIL_MAX or any(ord(ch) < 32 for ch in v):
                raise SettingError(k, f"必须是不超过 {UNTIL_MAX} 字的文本")
            out[k] = v.strip()
        elif k == "default_tier":
            if v not in TIERS:
                raise SettingError(k, "只能是 free 或 pro")
            out[k] = v
        elif k == "sections":                       # {页面id: {"on": bool}}，允许只改其中几页
            if not isinstance(v, dict) or not v:
                raise SettingError(k, "必须是对象")
            merged = dict(cur[k])
            for a, b in v.items():
                if a not in DEFAULTS[k]:
                    raise SettingError(f"{k}.{str(a)[:40]}", "不认识的页面")
                if not isinstance(b, dict) or set(b) != {"on"} or not isinstance(b["on"], bool):
                    raise SettingError(f"{k}.{a}", "必须是 {\"on\": true 或 false}")
                merged[a] = {"on": b["on"]}
            out[k] = merged
        else:                                       # features / quota：字典，允许只改其中几项
            if not isinstance(v, dict) or not v:
                raise SettingError(k, "必须是对象")
            merged = dict(cur[k])
            for a, b in v.items():
                if a not in DEFAULTS[k]:
                    raise SettingError(f"{k}.{str(a)[:40]}", "不认识的设置项")
                if k == "features":
                    if not isinstance(b, bool):
                        raise SettingError(f"{k}.{a}", "必须是 true 或 false")
                elif not (_is_int(b) and 0 <= b <= 1000):
                    raise SettingError(f"{k}.{a}", "必须是 0–1000 的整数")
                merged[a] = b
            out[k] = merged
    return out


def update(patch: dict, audit_fn=None) -> dict:
    """写入设置；对真正变化的键各写一条审计（audit_fn(key, old, new)）。返回 {"changed": [键...], "settings": 新的完整设置}。"""
    new = validate(patch)
    old = get_all()
    changed = [k for k, v in new.items() if v != old[k]]
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    with _lock, members._conn() as c:
        _ensure(c)
        for k in changed:
            c.execute("INSERT INTO settings(key, value, updated_at) VALUES(?,?,?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                      (k, json.dumps(new[k], ensure_ascii=False), now))
    if audit_fn:
        for k in changed:
            audit_fn(k, old[k], new[k])
    return {"changed": changed, "settings": get_all()}


# ---------------------------------------------------------------------------
# 我的登录列表（BACKEND_SPEC §2.10 第 6 条，OWASP ASVS V3：能看到并终止自己的 session）
# 后台登录 cookie 本身是无状态的签名串，服务器原来记不住"有哪几台"；这里在 owner_sessions 表里登记每个 cookie 的
# 哈希（不存 cookie 原文），列表只给随机 id，退出 = 标记 revoked，之后带这个 cookie 的请求在 _admin_authed 里被拒。
# 只加表、CREATE TABLE IF NOT EXISTS，可重复执行；SQL 全部参数化。
# ---------------------------------------------------------------------------
import hashlib
import secrets

SESSION_SECONDS = 8 * 3600          # 和 admin.SESSION_SECONDS 一致（cookie 8 小时过期）
TOUCH_INTERVAL = 60                 # last_active 最多每分钟写一次，免得每个请求都写库
SESSIONS_MAX = 50


class SessionError(Exception):
    """code 是给接口的错误码：NOT_FOUND / CANNOT_REVOKE_CURRENT / BAD_ID"""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _ensure_sessions(c):
    c.execute("CREATE TABLE IF NOT EXISTS owner_sessions(id TEXT PRIMARY KEY, th TEXT NOT NULL UNIQUE, device TEXT, ip TEXT, "
              "country TEXT, created_at REAL, last_active REAL, exp REAL, revoked INTEGER NOT NULL DEFAULT 0)")


def session_hash(cookie_value: str) -> str:
    return hashlib.sha256(str(cookie_value).encode()).hexdigest()


def device_summary(ua: str) -> str:
    """把 User-Agent 概括成“iPhone · Safari”这种短文本；不存原始头。顺序很重要：iPhone 的 UA 里也有 Mac OS X。"""
    ua = str(ua or "")
    osn = next((n for k, n in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"), ("Windows", "Windows"),
                               ("Mac OS X", "Mac"), ("Linux", "Linux")) if k in ua), "未知系统")
    br = next((n for k, n in (("Edg/", "Edge"), ("Firefox/", "Firefox"), ("Chrome/", "Chrome"), ("CriOS/", "Chrome"),
                              ("Safari/", "Safari")) if k in ua), "未知浏览器")
    return f"{osn} · {br}"


def _clean(v, n: int) -> str:
    return "".join(ch for ch in str(v or "") if 32 <= ord(ch) != 127).strip()[:n]


def _origin(headers, client_ip: str = "") -> tuple[str, str, str]:
    """(device, ip, country)：优先用 Cloudflare 给的来源，没有就用连接地址；都当文本、截断。"""
    ip = (headers.get("CF-Connecting-IP") or "").split(",")[0].strip() or client_ip
    return device_summary(headers.get("User-Agent", "")), _clean(ip, 45), _clean(headers.get("CF-IPCountry", ""), 4).upper()


def _cookie_exp(cookie_value: str, now: float) -> float:
    """cookie 形如 "<时间戳>.<签名>"，到期 = 时间戳 + 8 小时；解析不了就从现在起算。"""
    try:
        return int(str(cookie_value).split(".", 1)[0]) + SESSION_SECONDS
    except ValueError:
        return now + SESSION_SECONDS


def register_session(cookie_value: str, headers, client_ip: str = "", now: float | None = None) -> None:
    """登录成功时登记；已登记的（同一秒内重复登录得到同一个 cookie）不重复插入。"""
    now = time.time() if now is None else now
    device, ip, cc = _origin(headers, client_ip)
    with _lock, members._conn() as c:
        _ensure_sessions(c)
        c.execute("DELETE FROM owner_sessions WHERE exp < ?", (now,))
        c.execute("INSERT OR IGNORE INTO owner_sessions(id, th, device, ip, country, created_at, last_active, exp, revoked) "
                  "VALUES(?,?,?,?,?,?,?,?,0)",
                  (secrets.token_hex(8), session_hash(cookie_value), device, ip, cc, now, now, _cookie_exp(cookie_value, now)))


def session_allowed(cookie_value: str, headers, client_ip: str = "", now: float | None = None) -> bool:
    """每个带后台 cookie 的请求都问一次：被退出的返回 False；没登记过的（本功能上线前签发的）顺手登记；
    登记库读写失败时放行（拒绝只发生在明确读到 revoked=1 时，免得库出问题把所有者锁在外面）。"""
    now = time.time() if now is None else now
    try:
        th = session_hash(cookie_value)
        with _lock, members._conn() as c:
            _ensure_sessions(c)
            row = c.execute("SELECT revoked, last_active FROM owner_sessions WHERE th=?", (th,)).fetchone()
            if row is not None:
                if row[0]:
                    return False
                if now - (row[1] or 0) >= TOUCH_INTERVAL:
                    c.execute("UPDATE owner_sessions SET last_active=? WHERE th=?", (now, th))
                return True
        register_session(cookie_value, headers, client_ip, now)      # 没登记过：在上面的连接关掉之后再写
        return True
    except Exception:
        return True


def list_sessions(current_cookie: str = "", now: float | None = None) -> list[dict]:
    """没过期、没被退出的登录，最近活跃的在前。current 标出发起这次请求的那一台（没带 cookie 的主人通道请求没有 current）。"""
    now = time.time() if now is None else now
    cur = session_hash(current_cookie) if current_cookie else ""
    with _lock, members._conn() as c:
        _ensure_sessions(c)
        rows = c.execute("SELECT id, th, device, ip, country, last_active FROM owner_sessions "
                         "WHERE revoked=0 AND exp >= ? ORDER BY last_active DESC LIMIT ?", (now, SESSIONS_MAX)).fetchall()
    return [{"id": r[0], "device": r[2] or "", "ip": r[3] or "", "country": r[4] or "",
             "last_active": int(r[5] or 0), "current": bool(cur) and r[1] == cur} for r in rows]


def revoke_session(sid: str, current_cookie: str = "") -> dict:
    """退出单台。不能退出当前这一台（防止把自己锁在外面）；id 不存在或已退出 → NOT_FOUND。"""
    sid = str(sid or "")
    if not (0 < len(sid) <= 32 and all(ch in "0123456789abcdef" for ch in sid)):
        raise SessionError("BAD_ID")
    cur = session_hash(current_cookie) if current_cookie else ""
    with _lock, members._conn() as c:
        _ensure_sessions(c)
        row = c.execute("SELECT th, device, revoked FROM owner_sessions WHERE id=?", (sid,)).fetchone()
        if row is None or row[2]:
            raise SessionError("NOT_FOUND")
        if cur and row[0] == cur:
            raise SessionError("CANNOT_REVOKE_CURRENT")
        c.execute("UPDATE owner_sessions SET revoked=1 WHERE id=?", (sid,))
    return {"id": sid, "device": row[1] or ""}
