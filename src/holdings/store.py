"""持仓台账的 sqlite 存储(只记录,不连接券商、不下单)。

隐私约束(见 docs/holdings_design.md 第 1、5 节):
  - 导入模块、构造 HoldingsStore、读空库都不创建任何文件;第一次写入才创建,权限 0600;
  - 拒绝符号链接与非普通文件;已有文件权限宽于 0600 时收紧;
  - 本模块不使用 print/logging、不联网;审计表只记"动作 + 流水号",不记代码/数量/价格/费用/资金;
  - 所有错误只抛固定错误码(HoldingsError),不带用户输入或异常文本。
"""
import os
import re
import sqlite3
import stat
import threading
import unicodedata
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .calc import HoldingsError, replay, default_fee_estimate

# 默认路径固定为仓库下的 data/holdings.db;只是一个路径,不会在导入时创建
DEFAULT_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "holdings.db"

BEIJING = timezone(timedelta(hours=8))  # 中国没有夏令时,固定 +8 即可
EARLIEST_DATE = date(1990, 12, 19)
MAX_QTY = 100_000_000
SIDES = ("buy", "sell", "bonus", "dividend")
STOCK_PREFIXES = ("60", "68", "00", "30")
ETF_PREFIXES = ("510", "511", "512", "513", "515", "516", "517", "518", "56", "588", "589", "159")

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_NUM_RE = re.compile(r"^\d+(\.\d+)?$")
_CLIENT_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_SIGNAL_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\|[A-Za-z0-9_]{1,32}\|(\d{6})$")

_LOCK = threading.Lock()  # 进程内串行化写入;跨进程由 BEGIN IMMEDIATE 保证

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id TEXT UNIQUE,
    trade_date TEXT NOT NULL,
    code TEXT NOT NULL,
    side TEXT NOT NULL,
    qty INTEGER NOT NULL,
    price_milli INTEGER NOT NULL,
    fee_cent INTEGER NOT NULL DEFAULT 0,
    fee_estimated INTEGER NOT NULL DEFAULT 0,
    signal_ref TEXT,
    note TEXT NOT NULL DEFAULT '',
    voided INTEGER NOT NULL DEFAULT 0,
    voided_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trades_code_date ON trades (code, trade_date, id);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('add_trade','void_trade','set_capital','init_db')),
    trade_id INTEGER
);
"""

_COLS = ("id, client_id, trade_date, code, side, qty, price_milli, fee_cent, fee_estimated, "
         "signal_ref, note, voided, voided_at, created_at")


def beijing_today():
    """服务器可能在美西,所以"今天"一律取北京日期。"""
    return datetime.now(BEIJING).date()


def _utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------- 输入校验(全部只抛固定错误码) ----------

def _decimal(value, code, field, max_decimals):
    """严格解析数字:int / float / 纯数字字符串;拒绝科学计数法、符号、NaN、布尔。"""
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise HoldingsError(code, field)
    s = str(value) if not isinstance(value, Decimal) else format(value, "f")
    if not _NUM_RE.match(s):
        raise HoldingsError(code, field)
    try:
        d = Decimal(s)
        # 小数位超限(末尾补 0 不算)就拒绝
        if -d.as_tuple().exponent > max_decimals and d != d.quantize(Decimal(1).scaleb(-max_decimals)):
            raise HoldingsError(code, field)
    except InvalidOperation:
        raise HoldingsError(code, field) from None
    return d


def _check_code(code):
    if not isinstance(code, str) or not re.fullmatch(r"\d{6}", code):
        raise HoldingsError("HOLDINGS_BAD_CODE", "code")
    if not code.startswith(STOCK_PREFIXES + ETF_PREFIXES):
        raise HoldingsError("HOLDINGS_BAD_CODE", "code")
    return code


def _check_date(value, today):
    if not isinstance(value, str) or not _DATE_RE.match(value):
        raise HoldingsError("HOLDINGS_BAD_DATE", "trade_date")
    try:
        d = date.fromisoformat(value)
    except ValueError:
        raise HoldingsError("HOLDINGS_BAD_DATE", "trade_date") from None
    if d < EARLIEST_DATE:
        raise HoldingsError("HOLDINGS_BAD_DATE", "trade_date")
    if d > today:
        raise HoldingsError("HOLDINGS_FUTURE_DATE", "trade_date")
    return value


def _check_qty(value, side, code):
    if isinstance(value, bool):
        raise HoldingsError("HOLDINGS_BAD_QTY", "qty")
    if isinstance(value, str) and re.fullmatch(r"\d{1,9}", value):
        value = int(value)
    if not isinstance(value, int) or value <= 0 or value > MAX_QTY:
        raise HoldingsError("HOLDINGS_BAD_QTY", "qty")
    if side == "buy":
        if code.startswith(("688", "689")):
            if value < 200:  # 科创板:起买 200 股,之后任意整数股
                raise HoldingsError("HOLDINGS_BAD_LOT", "qty")
        elif value % 100 != 0:
            raise HoldingsError("HOLDINGS_BAD_LOT", "qty")
    return value


def _check_price(value, side):
    if side == "bonus":
        if value is None or _decimal(value, "HOLDINGS_BAD_PRICE", "price", 3) == 0:
            return Decimal(0)
        raise HoldingsError("HOLDINGS_BAD_PRICE", "price")
    if value is None:
        raise HoldingsError("HOLDINGS_BAD_PRICE", "price")
    d = _decimal(value, "HOLDINGS_BAD_PRICE", "price", 3)
    if d <= 0 or d > 100000:
        raise HoldingsError("HOLDINGS_BAD_PRICE", "price")
    return d


def _check_note(value):
    if value is None:
        return ""
    if not isinstance(value, str):
        raise HoldingsError("HOLDINGS_BAD_NOTE", "note")
    cleaned = "".join(ch for ch in value if not unicodedata.category(ch).startswith("C"))
    if len(cleaned) > 200:
        raise HoldingsError("HOLDINGS_BAD_NOTE", "note")
    return cleaned


def _check_signal_ref(value, code):
    if value is None:
        return None
    m = _SIGNAL_RE.match(value) if isinstance(value, str) else None
    if not m or m.group(2) != code:
        raise HoldingsError("HOLDINGS_BAD_SIGNAL_REF", "signal_ref")
    return value


def _check_client_id(value):
    if value is None:
        return None
    if not isinstance(value, str) or not _CLIENT_RE.match(value):
        raise HoldingsError("HOLDINGS_BAD_CLIENT_ID", "client_id")
    return value


def _row(r):
    keys = [c.strip() for c in _COLS.split(",")]
    d = dict(zip(keys, r))
    d["price"] = Decimal(d["price_milli"]) / 1000
    d["fee"] = Decimal(d["fee_cent"]) / 100
    d["fee_estimated"] = bool(d["fee_estimated"])
    d["voided"] = bool(d["voided"])
    return d


class HoldingsStore:
    """路径由调用者指定;构造函数不做任何文件操作。"""

    def __init__(self, path=DEFAULT_PATH):
        self.path = Path(path)

    # ---------- 文件安全 ----------

    def _lstat(self):
        try:
            return os.lstat(self.path)
        except FileNotFoundError:
            return None
        except OSError:
            raise HoldingsError("HOLDINGS_STORE_UNSAFE") from None

    def _check_safe(self, st):
        if st is not None and (stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode)):
            raise HoldingsError("HOLDINGS_STORE_UNSAFE")

    def _open_write(self):
        """返回 (连接, 是否刚创建)。首次写入才创建目录和文件(0600,O_EXCL|O_NOFOLLOW)。"""
        st = self._lstat()
        self._check_safe(st)
        created = False
        try:
            if st is None:
                self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
                os.close(fd)
                created = True
            elif st.st_mode & 0o077:
                os.chmod(self.path, 0o600)
            conn = sqlite3.connect(str(self.path), isolation_level=None, timeout=10)
        except HoldingsError:
            raise
        except (OSError, sqlite3.Error):
            raise HoldingsError("HOLDINGS_STORE_UNSAFE") from None
        return conn, created

    def _open_read(self):
        """文件不存在返回 None(不创建);符号链接/非普通文件拒绝。"""
        st = self._lstat()
        if st is None:
            return None
        self._check_safe(st)
        try:
            conn = sqlite3.connect(str(self.path), isolation_level=None, timeout=10)
            conn.execute("PRAGMA query_only=ON")
        except sqlite3.Error:
            raise HoldingsError("HOLDINGS_STORE_UNSAFE") from None
        return conn

    def _write(self, fn):
        """在 BEGIN IMMEDIATE 事务里执行 fn(conn, created);失败整体回滚,只抛 HoldingsError。"""
        with _LOCK:
            conn, created = self._open_write()
            try:
                conn.execute("PRAGMA journal_mode=DELETE")
                conn.execute("BEGIN IMMEDIATE")
                try:
                    for stmt in _SCHEMA.split(";"):
                        if stmt.strip():
                            conn.execute(stmt)
                    if created:
                        conn.execute("INSERT INTO audit (ts, action, trade_id) VALUES (?, 'init_db', NULL)",
                                     (_utc_now(),))
                    result = fn(conn)
                    conn.execute("COMMIT")
                    return result
                except BaseException:
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                    raise
            except HoldingsError:
                raise
            except sqlite3.Error:
                raise HoldingsError("HOLDINGS_INTERNAL") from None
            finally:
                conn.close()

    def _read(self, fn, empty):
        conn = self._open_read()
        if conn is None:
            return empty
        try:
            has = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='trades'").fetchone()
            return fn(conn) if has else empty
        except sqlite3.Error:
            raise HoldingsError("HOLDINGS_INTERNAL") from None
        finally:
            conn.close()

    # ---------- 读取 ----------

    def load_trades(self):
        """全部记录(含已撤销),按 (日期, 编号) 升序,供 calc 重放使用。"""
        def fn(conn):
            rows = conn.execute(f"SELECT {_COLS} FROM trades ORDER BY trade_date, id").fetchall()
            return [_row(r) for r in rows]
        return self._read(fn, [])

    def list_trades(self, limit=200, include_voided=False):
        """成交列表,倒序;limit 夹在 1..1000。"""
        limit = max(1, min(int(limit), 1000))

        def fn(conn):
            where = "" if include_voided else "WHERE voided = 0"
            rows = conn.execute(
                f"SELECT {_COLS} FROM trades {where} ORDER BY trade_date DESC, id DESC LIMIT ?",
                (limit,)).fetchall()
            return [_row(r) for r in rows]
        return self._read(fn, [])

    def get_capital(self):
        def fn(conn):
            r = conn.execute("SELECT value FROM settings WHERE key='capital_cent'").fetchone()
            return Decimal(int(r[0])) / 100 if r else None
        return self._read(fn, None)

    # ---------- 写入 ----------

    def set_capital(self, capital):
        d = _decimal(capital, "HOLDINGS_BAD_CAPITAL", "capital", 2)
        if d <= 0 or d > 1_000_000_000:
            raise HoldingsError("HOLDINGS_BAD_CAPITAL", "capital")
        cents = int(d * 100)
        now = _utc_now()

        def fn(conn):
            conn.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES ('capital_cent', ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (str(cents), now))
            conn.execute("INSERT INTO audit (ts, action, trade_id) VALUES (?, 'set_capital', NULL)", (now,))
            return Decimal(cents) / 100
        return self._write(fn)

    def add_trade(self, *, trade_date, code, side, qty, price=None, fee=None,
                  signal_ref=None, note=None, client_id=None, today=None, fee_fn=None):
        """新增一笔成交或调整记录,返回 {"trade": 记录, "duplicate": 是否幂等命中}。

        fee 为 None 时用 fee_fn(code, side, 成交金额) 估算(默认 calc.default_fee_estimate),
        并标 fee_estimated=1;bonus 费用固定 0,dividend 不估算。
        同一 client_id 且内容相同 → 返回已有记录;内容不同 → HOLDINGS_CLIENT_ID_CONFLICT。
        补录过去日期同样会对全部记录整体重放,任一时点超卖则整笔拒绝。
        """
        today = today or beijing_today()
        code = _check_code(code)
        if side not in SIDES:
            raise HoldingsError("HOLDINGS_BAD_SIDE", "side")
        trade_date = _check_date(trade_date, today)
        qty = _check_qty(qty, side, code)
        price_d = _check_price(price, side)
        amount = qty * price_d
        explicit_fee = fee is not None
        if side == "bonus":
            if explicit_fee and _decimal(fee, "HOLDINGS_BAD_FEE", "fee", 2) != 0:
                raise HoldingsError("HOLDINGS_BAD_FEE", "fee")
            fee_d, estimated = Decimal(0), 0
        elif explicit_fee:
            fee_d = _decimal(fee, "HOLDINGS_BAD_FEE", "fee", 2)
            if fee_d > amount:
                raise HoldingsError("HOLDINGS_BAD_FEE", "fee")
            estimated = 0
        elif side == "dividend":
            fee_d, estimated = Decimal(0), 0
        else:
            try:
                fee_d = Decimal(str((fee_fn or default_fee_estimate)(code, side, amount)))
            except (InvalidOperation, ValueError, TypeError, ArithmeticError):
                raise HoldingsError("HOLDINGS_BAD_FEE", "fee") from None
            if not fee_d.is_finite() or fee_d < 0 or fee_d > amount:
                raise HoldingsError("HOLDINGS_BAD_FEE", "fee")
            fee_d = fee_d.quantize(Decimal("0.01"))
            estimated = 1
        signal_ref = _check_signal_ref(signal_ref, code)
        note = _check_note(note)
        client_id = _check_client_id(client_id)
        price_milli, fee_cent = int(price_d * 1000), int(fee_d * 100)
        now = _utc_now()

        def fn(conn):
            if client_id is not None:
                r = conn.execute(f"SELECT {_COLS} FROM trades WHERE client_id = ?", (client_id,)).fetchone()
                if r is not None:
                    old = _row(r)
                    same = (old["trade_date"], old["code"], old["side"], old["qty"], old["price_milli"],
                            old["signal_ref"], old["note"]) == \
                           (trade_date, code, side, qty, price_milli, signal_ref, note)
                    if same and explicit_fee:
                        same = old["fee_cent"] == fee_cent
                    if not same:
                        raise HoldingsError("HOLDINGS_CLIENT_ID_CONFLICT")
                    return {"trade": old, "duplicate": True}
            existing = [_row(r) for r in conn.execute(f"SELECT {_COLS} FROM trades WHERE voided = 0")]
            candidate = {"id": 10 ** 18, "trade_date": trade_date, "code": code, "side": side,
                         "qty": qty, "price_milli": price_milli, "fee_cent": fee_cent, "voided": False}
            replay(existing + [candidate])  # 超卖等会抛 HoldingsError,整笔回滚
            cur = conn.execute(
                "INSERT INTO trades (client_id, trade_date, code, side, qty, price_milli, fee_cent, "
                "fee_estimated, signal_ref, note, voided, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,0,?)",
                (client_id, trade_date, code, side, qty, price_milli, fee_cent, estimated,
                 signal_ref, note, now))
            tid = cur.lastrowid
            conn.execute("INSERT INTO audit (ts, action, trade_id) VALUES (?, 'add_trade', ?)", (now, tid))
            row = conn.execute(f"SELECT {_COLS} FROM trades WHERE id = ?", (tid,)).fetchone()
            return {"trade": _row(row), "duplicate": False}
        return self._write(fn)

    def void_trade(self, trade_id):
        """软删除一笔记录(不物理删除)。撤销会让后续记录非法时拒绝。"""
        if isinstance(trade_id, bool) or not isinstance(trade_id, int) or trade_id <= 0:
            raise HoldingsError("HOLDINGS_BAD_ID", "id")
        now = _utc_now()

        def fn(conn):
            r = conn.execute("SELECT voided FROM trades WHERE id = ?", (trade_id,)).fetchone()
            if r is None:
                raise HoldingsError("HOLDINGS_TRADE_NOT_FOUND", "id")
            if r[0]:
                raise HoldingsError("HOLDINGS_ALREADY_VOIDED", "id")
            rest = [_row(x) for x in conn.execute(
                f"SELECT {_COLS} FROM trades WHERE voided = 0 AND id != ?", (trade_id,))]
            try:
                replay(rest)
            except HoldingsError:
                raise HoldingsError("HOLDINGS_VOID_BREAKS_HISTORY") from None
            conn.execute("UPDATE trades SET voided = 1, voided_at = ? WHERE id = ?", (now, trade_id))
            conn.execute("INSERT INTO audit (ts, action, trade_id) VALUES (?, 'void_trade', ?)",
                         (now, trade_id))
            return trade_id
        return self._write(fn)
