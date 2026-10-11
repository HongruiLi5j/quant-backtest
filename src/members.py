"""会员 + 模拟交易（只用标准库）。数据在 data/members.db（SQLite）。

- 密码用 scrypt 加盐哈希保存，从不存明文；登录后发随机会话令牌，库里只存它的 SHA-256。
- 每个会员每个市场一个模拟账户（A 股 100 万元，美股 10 万美元）。成交价 = 最新收盘价（前复权显示价），
  不模拟盘中滑点、涨跌停和成交量限制。A 股买入按 100 股一手、卖出受 T+1 限制。
- 价格、手续费率由调用方（app.py）通过 quote_fn 提供，方便单独测试。
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "members.db"
SESSION_SECONDS = 30 * 86400
INITIAL_CASH = {"cn": 1_000_000.0, "us": 100_000.0}
MAX_USERS = 200
MAX_POSITIONS = 50
MAX_WEIGHT = 0.30      # 单只股票买入后不超过总资产的 30%
HALT_DD = 0.20         # 账户从最高点回撤超过 20%，暂停买入（只能卖）
USER_RE = re.compile(r"^[A-Za-z0-9_一-龥]{3,20}$")
_lock = threading.RLock()
_hits: dict = {}   # (kind, ip) -> [时间戳]


class MemberError(Exception):
    pass


def set_db(path: Path):
    global DB
    DB = Path(path)


def _conn():
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    try:
        DB.chmod(0o600)
    except OSError:
        pass
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT UNIQUE COLLATE NOCASE, salt BLOB, pw BLOB,
        created TEXT, disabled INTEGER DEFAULT 0, last_login TEXT);
    CREATE TABLE IF NOT EXISTS sessions(th TEXT PRIMARY KEY, uid INTEGER REFERENCES users(id) ON DELETE CASCADE, exp REAL);
    CREATE TABLE IF NOT EXISTS accounts(uid INTEGER REFERENCES users(id) ON DELETE CASCADE, market TEXT, cash REAL,
        initial REAL, created TEXT, PRIMARY KEY(uid, market));
    CREATE TABLE IF NOT EXISTS positions(uid INTEGER REFERENCES users(id) ON DELETE CASCADE, market TEXT, code TEXT,
        name TEXT, qty INTEGER, cost REAL, PRIMARY KEY(uid, market, code));
    CREATE TABLE IF NOT EXISTS trades(id INTEGER PRIMARY KEY, uid INTEGER REFERENCES users(id) ON DELETE CASCADE, ts TEXT,
        day TEXT, market TEXT, code TEXT, name TEXT, side TEXT, qty INTEGER, price REAL, fee REAL, pnl REAL,
        strategy TEXT, signal TEXT);
    CREATE TABLE IF NOT EXISTS activity(id INTEGER PRIMARY KEY, uid INTEGER, name TEXT, ts TEXT, ip TEXT, country TEXT, ua TEXT,
        action TEXT, detail TEXT);
    CREATE TABLE IF NOT EXISTS equity(uid INTEGER REFERENCES users(id) ON DELETE CASCADE, market TEXT, day TEXT,
        value REAL, PRIMARY KEY(uid, market, day));
    """)
    return c


def hit(kind: str, ip: str, limit: int, window: int) -> bool:
    """记一次尝试；超过 limit 次（window 秒内）返回 True（应拒绝）。"""
    now = time.time()
    with _lock:
        a = [t for t in _hits.get((kind, ip), []) if t > now - window]
        a.append(now)
        _hits[(kind, ip)] = a
        return len(a) > limit


def _hash(pw: str, salt: bytes) -> bytes:
    return hashlib.scrypt(pw.encode("utf-8"), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def register(name: str, pw: str) -> dict:
    name = (name or "").strip()
    if not USER_RE.match(name):
        raise MemberError("用户名 3–20 位，只能用字母、数字、下划线或汉字")
    if not (8 <= len(pw or "") <= 64):
        raise MemberError("密码需要 8–64 位")
    if pw.lower() == name.lower():
        raise MemberError("密码不能和用户名一样")
    salt = secrets.token_bytes(16)
    with _lock, _conn() as c:
        if c.execute("SELECT COUNT(*) FROM users").fetchone()[0] >= MAX_USERS:
            raise MemberError("会员已满，请联系站长")
        try:
            cur = c.execute("INSERT INTO users(name,salt,pw,created) VALUES(?,?,?,?)", (name, salt, _hash(pw, salt), _now()))
        except sqlite3.IntegrityError:
            raise MemberError("用户名已被使用")
        uid = cur.lastrowid
        for m, cash in INITIAL_CASH.items():
            c.execute("INSERT INTO accounts VALUES(?,?,?,?,?)", (uid, m, cash, cash, _now()))
    return {"id": uid, "name": name}


def _new_session(c, uid: int) -> str:
    tok = secrets.token_urlsafe(32)
    c.execute("DELETE FROM sessions WHERE exp < ?", (time.time(),))
    c.execute("INSERT INTO sessions VALUES(?,?,?)", (hashlib.sha256(tok.encode()).hexdigest(), uid, time.time() + SESSION_SECONDS))
    return tok


def login(name: str, pw: str) -> tuple[dict, str]:
    with _lock, _conn() as c:
        r = c.execute("SELECT * FROM users WHERE name=?", ((name or "").strip(),)).fetchone()
        # 即使用户不存在也算一次哈希，避免用耗时判断用户名是否存在
        salt = r["salt"] if r else b"\0" * 16
        ok = hmac.compare_digest(_hash(pw or "", salt), r["pw"] if r else b"\0" * 32) and r is not None
        if not ok:
            raise MemberError("用户名或密码不对")
        if r["disabled"]:
            raise MemberError("这个账号已被停用，请联系站长")
        c.execute("UPDATE users SET last_login=? WHERE id=?", (_now(), r["id"]))
        return {"id": r["id"], "name": r["name"]}, _new_session(c, r["id"])


def user_for(token: str | None) -> dict | None:
    if not token:
        return None
    with _lock, _conn() as c:
        r = c.execute("SELECT u.id, u.name, u.disabled, s.exp FROM sessions s JOIN users u ON u.id=s.uid WHERE s.th=?",
                      (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
        if not r or r["exp"] < time.time() or r["disabled"]:
            return None
        return {"id": r["id"], "name": r["name"]}


def logout(token: str | None):
    if token:
        with _lock, _conn() as c:
            c.execute("DELETE FROM sessions WHERE th=?", (hashlib.sha256(token.encode()).hexdigest(),))


def change_password(uid: int, old: str, new: str):
    if not (8 <= len(new or "") <= 64):
        raise MemberError("新密码需要 8–64 位")
    with _lock, _conn() as c:
        r = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if not r or not hmac.compare_digest(_hash(old or "", r["salt"]), r["pw"]):
            raise MemberError("旧密码不对")
        salt = secrets.token_bytes(16)
        c.execute("UPDATE users SET salt=?, pw=? WHERE id=?", (salt, _hash(new, salt), uid))
        c.execute("DELETE FROM sessions WHERE uid=?", (uid,))   # 改密码后其他设备要重新登录


# ------------------------------------------------------------ 交易
def order(uid: int, market: str, code: str, side: str, qty, q: dict, strategy: str = "", signal: str = "") -> dict:
    """q = {price, name, day, fee_bp, lot}。成交价是 q['price']（最新收盘价）。"""
    if market not in INITIAL_CASH:
        raise MemberError("未知市场")
    if side not in ("buy", "sell"):
        raise MemberError("方向只能是买入或卖出")
    try:
        qty = int(qty)
    except (TypeError, ValueError):
        raise MemberError("数量要是整数")
    if qty <= 0 or qty > 100_000_000:
        raise MemberError("数量不对")
    price, lot = float(q["price"]), int(q.get("lot", 1))
    if not price > 0:
        raise MemberError("没有可用的价格")
    amount = price * qty
    fee = round(amount * float(q.get("fee_bp", 0)) / 1e4, 2)
    with _lock, _conn() as c:
        acc = c.execute("SELECT * FROM accounts WHERE uid=? AND market=?", (uid, market)).fetchone()
        pos = c.execute("SELECT * FROM positions WHERE uid=? AND market=? AND code=?", (uid, market, code)).fetchone()
        pnl = None
        if side == "buy":
            if qty % lot:
                raise MemberError(f"买入要是 {lot} 股的整数倍")
            if acc["cash"] < amount + fee:
                raise MemberError(f"现金不够：需要 {amount + fee:,.2f}，可用 {acc['cash']:,.2f}")
            if not pos and c.execute("SELECT COUNT(*) FROM positions WHERE uid=? AND market=?", (uid, market)).fetchone()[0] >= MAX_POSITIONS:
                raise MemberError(f"同一市场最多持有 {MAX_POSITIONS} 只")
            oq, oc = (pos["qty"], pos["cost"]) if pos else (0, 0.0)
            eqt = float(q.get("equity") or 0)
            if eqt > 0:
                if (oq * price + amount) / eqt > MAX_WEIGHT + 1e-9:
                    cap = max(0, int((MAX_WEIGHT * eqt - oq * price) // price // lot * lot))
                    raise MemberError(f"风控：单只股票不能超过总资产的 {MAX_WEIGHT:.0%}，这只最多还能买 {cap} 股")
                pk = c.execute("SELECT MAX(value) FROM equity WHERE uid=? AND market=?", (uid, market)).fetchone()[0] or acc["initial"]
                pk = max(pk, acc["initial"])
                if eqt < pk * (1 - HALT_DD):
                    raise MemberError(f"风控：账户已从最高点回撤 {(1 - eqt / pk):.0%}（超过 {HALT_DD:.0%}），暂停买入，只能卖出")
            nq = oq + qty
            ncost = (oq * oc + amount + fee) / nq      # 成本里含买入手续费
            c.execute("INSERT OR REPLACE INTO positions VALUES(?,?,?,?,?,?)", (uid, market, code, q.get("name", code), nq, ncost))
            c.execute("UPDATE accounts SET cash=cash-? WHERE uid=? AND market=?", (amount + fee, uid, market))
        else:
            if not pos or pos["qty"] < qty:
                raise MemberError("持仓不够")
            if market == "cn":
                held_today = c.execute("SELECT COALESCE(SUM(qty),0) FROM trades WHERE uid=? AND market=? AND code=? AND side='buy' AND day=?",
                                       (uid, market, code, q["day"])).fetchone()[0]
                if pos["qty"] - held_today < qty:
                    raise MemberError(f"A 股 T+1：今天买入的 {held_today} 股今天不能卖，可卖 {pos['qty'] - held_today} 股")
            if qty % lot and qty != pos["qty"]:
                raise MemberError(f"卖出要是 {lot} 股的整数倍（或一次全部卖出）")
            pnl = round((price * qty - fee) - pos["cost"] * qty, 2)
            if pos["qty"] == qty:
                c.execute("DELETE FROM positions WHERE uid=? AND market=? AND code=?", (uid, market, code))
            else:
                c.execute("UPDATE positions SET qty=qty-? WHERE uid=? AND market=? AND code=?", (qty, uid, market, code))
            c.execute("UPDATE accounts SET cash=cash+? WHERE uid=? AND market=?", (amount - fee, uid, market))
        c.execute("INSERT INTO trades(uid,ts,day,market,code,name,side,qty,price,fee,pnl,strategy,signal) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (uid, _now(), q["day"], market, code, q.get("name", code), side, qty, price, fee, pnl, strategy[:40], signal[:200]))
    return {"ok": True, "price": price, "qty": qty, "fee": fee, "pnl": pnl}


def reset_account(uid: int, market: str):
    if market not in INITIAL_CASH:
        raise MemberError("未知市场")
    with _lock, _conn() as c:
        for t in ("positions", "trades", "equity"):
            c.execute(f"DELETE FROM {t} WHERE uid=? AND market=?", (uid, market))
        c.execute("UPDATE accounts SET cash=initial WHERE uid=? AND market=?", (uid, market))


def position_codes(uid: int, market: str) -> list:
    with _conn() as c:
        return [r["code"] for r in c.execute("SELECT code FROM positions WHERE uid=? AND market=?", (uid, market))]


def portfolio(uid: int, market: str, last_price_fn, today: str) -> dict:
    """last_price_fn(code) -> 最新价或 None。顺便记录当天的净值快照。"""
    with _lock, _conn() as c:
        acc = c.execute("SELECT * FROM accounts WHERE uid=? AND market=?", (uid, market)).fetchone()
        if not acc:
            raise MemberError("没有这个账户")
        rows = []
        mv = 0.0
        for p in c.execute("SELECT * FROM positions WHERE uid=? AND market=? ORDER BY code", (uid, market)):
            r = last_price_fn(p["code"])
            lp, day = (r if isinstance(r, tuple) else (r, None))
            lp = float(lp) if lp else p["cost"]
            v = lp * p["qty"]
            mv += v
            sellable = p["qty"]
            if market == "cn" and day:     # T+1：最新交易日买入的不能卖
                sellable -= c.execute("SELECT COALESCE(SUM(qty),0) FROM trades WHERE uid=? AND market=? AND code=? AND side='buy' AND day=?",
                                      (uid, market, p["code"], day)).fetchone()[0]
            first = c.execute("SELECT MIN(ts) FROM trades WHERE uid=? AND market=? AND code=?", (uid, market, p["code"])).fetchone()[0]
            rows.append({"code": p["code"], "name": p["name"], "qty": p["qty"], "sellable": max(0, sellable), "cost": p["cost"], "last": lp,
                         "value": v, "pnl": v - p["cost"] * p["qty"], "pnl_pct": (lp / p["cost"] - 1) * 100 if p["cost"] else 0, "first": first})
        total = acc["cash"] + mv
        c.execute("INSERT OR REPLACE INTO equity VALUES(?,?,?,?)", (uid, market, today, total))
        curve = [[r["day"], r["value"]] for r in c.execute("SELECT day, value FROM equity WHERE uid=? AND market=? ORDER BY day", (uid, market))]
        trades = [dict(r) for r in c.execute("SELECT * FROM trades WHERE uid=? AND market=? ORDER BY id DESC LIMIT 200", (uid, market))]
        realized = c.execute("SELECT COALESCE(SUM(pnl),0) FROM trades WHERE uid=? AND market=?", (uid, market)).fetchone()[0]
    for r in rows:
        r["weight"] = r["value"] / total * 100 if total else 0
    with _lock, _conn() as c:
        real = {r["code"]: (r["name"], r["s"]) for r in c.execute(
            "SELECT code, MAX(name) name, COALESCE(SUM(pnl),0) s FROM trades WHERE uid=? AND market=? AND side='sell' GROUP BY code", (uid, market))}
        cl = c.execute("SELECT COUNT(*) n, COALESCE(SUM(pnl>0),0) w FROM trades WHERE uid=? AND market=? AND side='sell'", (uid, market)).fetchone()
    attr = {}
    for k, (nm, v) in real.items():
        attr[k] = {"code": k, "name": nm, "realized": v, "unrealized": 0.0}
    for r in rows:
        a = attr.setdefault(r["code"], {"code": r["code"], "name": r["name"], "realized": 0.0, "unrealized": 0.0})
        a["unrealized"] = r["pnl"]
    for a in attr.values():
        a["total"] = a["realized"] + a["unrealized"]
    pk = max([x[1] for x in curve] + [acc["initial"], total])
    risk = {"peak": pk, "dd_pct": (total / pk - 1) * 100, "halted": total < pk * (1 - HALT_DD),
            "max_weight": MAX_WEIGHT * 100, "halt_dd": HALT_DD * 100,
            "closed": cl["n"], "win_pct": (cl["w"] / cl["n"] * 100) if cl["n"] else None}
    return {"risk": risk, "attribution": sorted(attr.values(), key=lambda a: -abs(a["total"])), **_pf(
        acc, mv, total, realized, rows, trades, curve, market)}


def _pf(acc, mv, total, realized, rows, trades, curve, market):
    return _pf_body(acc, mv, total, realized, rows, trades, curve, market)


def _pf_body(acc, mv, total, realized, rows, trades, curve, market):
    return {"market": market, "cash": acc["cash"], "initial": acc["initial"], "market_value": mv, "total": total,
            "ret_pct": (total / acc["initial"] - 1) * 100, "realized": realized, "positions": rows, "trades": trades, "curve": curve}


# ------------------------------------------------------------ 活动记录（站长可以看到会员做了什么）
def log(uid, name: str, ip: str, country: str, ua: str, action: str, detail: str = ""):
    try:
        with _lock, _conn() as c:
            c.execute("INSERT INTO activity(uid,name,ts,ip,country,ua,action,detail) VALUES(?,?,?,?,?,?,?,?)",
                      (uid, (name or "")[:40], _now(), (ip or "")[:60], (country or "")[:8], (ua or "")[:200], action, (detail or "")[:300]))
            c.execute("DELETE FROM activity WHERE id <= (SELECT MAX(id) FROM activity) - 20000")
    except Exception:
        pass


def activity(uid=None, limit: int = 300) -> list:
    with _conn() as c:
        if uid is None:
            rows = c.execute("SELECT * FROM activity ORDER BY id DESC LIMIT ?", (limit,))
        else:
            rows = c.execute("SELECT * FROM activity WHERE uid=? ORDER BY id DESC LIMIT ?", (uid, limit))
        return [dict(r) for r in rows]


def user_detail(uid: int):
    with _conn() as c:
        u = c.execute("SELECT id,name,created,last_login,disabled FROM users WHERE id=?", (uid,)).fetchone()
        if not u:
            return None
        n = c.execute("SELECT COUNT(*) FROM activity WHERE uid=? AND action IN ('login','register')", (uid,)).fetchone()[0]
        return dict(u) | {"logins": n}


# ------------------------------------------------------------ 站长用
def list_users() -> list:
    with _conn() as c:
        c.execute("DELETE FROM activity WHERE uid IS NOT NULL AND uid NOT IN (SELECT id FROM users)")   # 已删除会员的记录一并清掉
        return [dict(r) for r in c.execute(
            "SELECT u.id, u.name, u.created, u.last_login, u.disabled, "
            "(SELECT COUNT(*) FROM trades t WHERE t.uid=u.id) AS trades, "
            "(SELECT COUNT(*) FROM activity a WHERE a.uid=u.id AND a.action IN ('login','register')) AS logins, "
            "(SELECT ip FROM activity a WHERE a.uid=u.id ORDER BY a.id DESC LIMIT 1) AS last_ip, "
            "(SELECT country FROM activity a WHERE a.uid=u.id ORDER BY a.id DESC LIMIT 1) AS last_country, "
            "(SELECT MAX(ts) FROM activity a WHERE a.uid=u.id) AS last_active FROM users u ORDER BY u.id")]


def set_disabled(uid: int, disabled: bool):
    with _lock, _conn() as c:
        c.execute("UPDATE users SET disabled=? WHERE id=?", (1 if disabled else 0, uid))
        if disabled:
            c.execute("DELETE FROM sessions WHERE uid=?", (uid,))


def delete_user(uid: int):
    with _lock, _conn() as c:
        c.execute("DELETE FROM users WHERE id=?", (uid,))
        c.execute("DELETE FROM activity WHERE uid=?", (uid,))


def leaderboard(market: str, uid: int | None = None, limit: int = 20) -> dict:
    """按最近一次净值快照的收益率排名；名字只显示首字，只算已经交易过的会员。"""
    if market not in INITIAL_CASH:
        raise MemberError("未知市场")
    with _conn() as c:
        rows = c.execute("""
          SELECT u.id, u.name, a.initial, e.value FROM users u JOIN accounts a ON a.uid=u.id AND a.market=?
          JOIN equity e ON e.uid=u.id AND e.market=? AND e.day=(SELECT MAX(day) FROM equity WHERE uid=u.id AND market=?)
          WHERE u.disabled=0 AND EXISTS(SELECT 1 FROM trades t WHERE t.uid=u.id AND t.market=?)""",
                         (market, market, market, market)).fetchall()
    lst = sorted(({"id": r["id"], "name": r["name"], "ret": (r["value"] / r["initial"] - 1) * 100} for r in rows), key=lambda x: -x["ret"])
    out = []
    for i, x in enumerate(lst, 1):
        if i <= limit or x["id"] == uid:
            out.append({"rank": i, "name": x["name"][0] + "***", "ret": x["ret"], "me": x["id"] == uid})
    return {"market": market, "total": len(lst), "rows": out}
