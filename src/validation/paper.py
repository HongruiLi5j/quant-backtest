"""模拟盘日志(T007 / S5):SQLite 存储信号、成交、漏单、每日估值价,并与回测收益对比。

不联网、不读 data/;数据库路径必须由调用者传入(没有默认路径,导入模块时不创建任何文件,
只有实例化 PaperJournal 才会创建/打开数据库文件)。

表:
  signals(id, date, code, side, theory_price, strategy_id)   信号与"理论价"(信号当根收盘价或计划价)
  fills(id, signal_id, price, shares, ts, fee, note)         实际成交(一个信号可有多笔,即分批成交)
  misses(id, signal_id, reason, note)                        漏单(涨跌停/停牌/没来得及/其他)
  marks(date, code, price, source)                           每日估值价(由调用者提供)

幂等规则(重复提交同一条记录不会产生重复行):
  - add_signal:自然键 (date, code, side, strategy_id) 相同:理论价相同 → 返回已有 id;理论价不同 → PaperError。
  - add_fill:键 (signal_id, ts, price, shares) 相同:fee 相同 → 返回已有 id(note 以先登记的为准);fee 不同 → PaperError。
    注意:同一时刻同价同量的两笔真实成交会被当作重复;如确有此事,请在 ts 里带上秒或序号区分。
  - add_miss:一个信号至多一条漏单:原因相同 → 返回已有 id;原因不同 → PaperError。
    一个信号不能既有成交又有漏单(部分成交的剩余部分不记漏单,按"部分成交"处理)。
  - add_mark:(date, code) 相同:价格相同 → 幂等忽略;价格不同 → PaperError(不覆盖)。
所有写入都在单个事务里完成,校验失败或出错整体回滚,不会留下半条记录。

净值口径:equity[d] = cash[d] + Σ 持仓股数 × 当日估值价;cash 由 fills 累计(买入扣 价×量+fee,卖出加 价×量-fee)。
持仓期间没有成交的日子也按 marks 重估。report 假设窗口起点(start 之前)没有持仓,只有 initial_cash。
"""
from __future__ import annotations

import math
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import date as _date, datetime as _datetime
from typing import Any, Iterable, Mapping, Optional, Sequence

__all__ = ["PaperJournal", "PaperError", "MISS_REASONS", "SIDES"]

SIDES = ("buy", "sell")
# 漏单原因:涨跌停 / 停牌 / 没来得及 / 其他
MISS_REASONS = ("limit", "suspended", "too_late", "other")

_CODE_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
# 保护已有的库:禁止把模拟盘日志建在这些文件上
_FORBIDDEN_DB_NAMES = ("simtrade.db", "signal_ledger.db")

# 本模块四张表的列(顺序与 _SCHEMA 一致),用于识别"这是不是我们自己的库"
_EXPECTED_COLUMNS = {
    "signals": ["id", "date", "code", "side", "theory_price", "strategy_id"],
    "fills": ["id", "signal_id", "price", "shares", "ts", "fee", "note"],
    "misses": ["id", "signal_id", "reason", "note"],
    "marks": ["date", "code", "price", "source"],
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    code TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('buy','sell')),
    theory_price REAL NOT NULL CHECK (theory_price > 0),
    strategy_id TEXT NOT NULL,
    UNIQUE (date, code, side, strategy_id)
);
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER NOT NULL REFERENCES signals(id) ON DELETE CASCADE,
    price REAL NOT NULL CHECK (price > 0),
    shares INTEGER NOT NULL CHECK (shares > 0),
    ts TEXT NOT NULL,
    fee REAL NOT NULL DEFAULT 0 CHECK (fee >= 0),
    note TEXT NOT NULL DEFAULT '',
    UNIQUE (signal_id, ts, price, shares)
);
CREATE TABLE IF NOT EXISTS misses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER NOT NULL UNIQUE REFERENCES signals(id) ON DELETE CASCADE,
    reason TEXT NOT NULL CHECK (reason IN ('limit','suspended','too_late','other')),
    note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS marks (
    date TEXT NOT NULL,
    code TEXT NOT NULL,
    price REAL NOT NULL CHECK (price > 0),
    source TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (date, code)
);
"""


class PaperError(ValueError):
    """输入非法、与已有记录冲突、或报告缺少必要数据。"""


# ---------------------------------------------------------------- 输入校验

def _d(value: Any, name: str = "date") -> str:
    """日期 → 'YYYY-MM-DD' 字符串;接受 date 对象或 ISO 字符串,其他一律拒绝。"""
    if isinstance(value, _date) and not hasattr(value, "hour"):
        return value.isoformat()
    if hasattr(value, "date") and hasattr(value, "hour"):  # datetime / Timestamp:取日期部分
        return value.date().isoformat()
    if isinstance(value, str):
        try:
            return _date.fromisoformat(value.strip()).isoformat()
        except ValueError:
            pass
    raise PaperError(f"{name} 必须是 YYYY-MM-DD 日期,收到 {value!r}")


def _code(value: Any) -> str:
    if not isinstance(value, str) or not _CODE_RE.match(value):
        raise PaperError(f"code 只能含字母数字和 ._-,长度 1~32,收到 {value!r}")
    return value


def _price(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PaperError(f"{name} 必须是数字,收到 {value!r}")
    v = float(value)
    if not math.isfinite(v) or v <= 0:
        raise PaperError(f"{name} 必须是有限正数,收到 {value!r}")
    return v


def _text(value: Any, name: str, allow_empty: bool, maxlen: int = 200) -> str:
    if not isinstance(value, str):
        raise PaperError(f"{name} 必须是字符串,收到 {value!r}")
    value = value.strip()
    if (not value and not allow_empty) or len(value) > maxlen:
        raise PaperError(f"{name} 不能为空且长度 ≤ {maxlen}")
    return value


def _ts(value: Any) -> str:
    """成交时间:ISO 日期或日期时间字符串(前 10 位必须是合法日期)。"""
    if hasattr(value, "isoformat") and not isinstance(value, str):
        value = value.isoformat(sep=" ") if hasattr(value, "hour") else value.isoformat()
    s = _text(value, "ts", allow_empty=False, maxlen=40)
    _d(s[:10], "ts 的日期部分")
    if len(s) == 10:
        return s  # 只有日期:排在同一天所有带时间的成交之前
    # 带时间:统一解析并规范成 'YYYY-MM-DD HH:MM:SS','T' 与空格两种写法等价,避免字符串排序颠倒先后
    try:
        dt = _datetime.fromisoformat(s)
    except ValueError:
        raise PaperError(f"ts 不是合法的 ISO 日期时间,收到 {value!r}")
    if dt.tzinfo is not None:
        raise PaperError("ts 不支持带时区的时间,请换算成本地(交易所)时间")
    return dt.isoformat(sep=" ")


def _calendar(calendar: Iterable[Any]) -> list[str]:
    """交易日历:严格升序、无重复、无非法日期;否则报错(不静默排序/去重)。"""
    days = [_d(x, "calendar 中的日期") for x in calendar]
    if not days:
        raise PaperError("calendar 不能为空")
    for a, b in zip(days, days[1:]):
        if b == a:
            raise PaperError(f"calendar 含重复日期 {b}")
        if b < a:
            raise PaperError(f"calendar 未按升序排列:{a} 之后出现 {b}")
    return days


def _std(xs: Sequence[float]) -> Optional[float]:
    n = len(xs)
    if n < 2:
        return None
    m = sum(xs) / n
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))


# ---------------------------------------------------------------- 主类

class PaperJournal:
    """模拟盘日志。用法:PaperJournal(路径) 或 with PaperJournal(路径) as j: ..."""

    def __init__(self, db_path: str):
        if not isinstance(db_path, (str, os.PathLike)):
            raise PaperError("db_path 必须由调用者指定(字符串或路径)")
        path = os.fspath(db_path)
        if path != ":memory:":
            # 文件名按小写比较,并同时检查符号链接指向的真实路径,防止大小写/软链绕过保护
            for p in (path, os.path.realpath(path)):
                if os.path.basename(p).casefold() in _FORBIDDEN_DB_NAMES:
                    raise PaperError(f"不能使用已有的库 {os.path.basename(p)} 作为模拟盘日志")
            parent = os.path.dirname(os.path.abspath(path))
            if not os.path.isdir(parent):
                raise PaperError(f"目录不存在:{parent}(不会自动创建目录)")
        self.db_path = path
        # isolation_level=None:由我们自己用 BEGIN IMMEDIATE / COMMIT / ROLLBACK 控制事务
        self._conn = sqlite3.connect(path, isolation_level=None)
        try:
            self._conn.row_factory = sqlite3.Row
            # 建表之前先确认:库是空的,或者本来就是 PaperJournal 的库;别人的库一律不动
            self._check_ownership()
            self._conn.execute("PRAGMA foreign_keys = ON")
            with self._tx():
                for stmt in _SCHEMA.split(";"):
                    if stmt.strip():
                        self._conn.execute(stmt)
        except BaseException:
            self._conn.close()
            raise

    def _check_ownership(self) -> None:
        """已有表时,表名和列必须与本模块完全一致,否则拒绝(不执行任何写入)。"""
        try:
            tables = {r[0] for r in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            if not tables:
                return
            if tables != set(_EXPECTED_COLUMNS):
                raise PaperError(f"{self.db_path} 不是模拟盘日志库(已有表 {sorted(tables)}),拒绝使用")
            for t, cols in _EXPECTED_COLUMNS.items():
                have = [r[1] for r in self._conn.execute(f"PRAGMA table_info({t})")]
                if have != cols:
                    raise PaperError(f"{self.db_path} 的表 {t} 列不匹配,拒绝使用")
        except sqlite3.DatabaseError as exc:
            raise PaperError(f"{self.db_path} 不是可用的 SQLite 库:{exc}")

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "PaperJournal":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @contextmanager
    def _tx(self):
        """事务:成功提交,任何异常整体回滚。"""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            self._conn.execute("COMMIT")  # 提交也在 try 内:提交失败同样回滚,连接可继续使用
        except BaseException:
            if self._conn.in_transaction:
                try:
                    self._conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise

    # ------------------------------------------------------------ 写入

    def add_signal(self, date: Any, code: str, side: str, theory_price: float, strategy_id: str) -> int:
        d, c = _d(date), _code(code)
        if side not in SIDES:
            raise PaperError(f"side 必须是 {SIDES},收到 {side!r}")
        tp = _price(theory_price, "theory_price")
        sid = _text(strategy_id, "strategy_id", allow_empty=False, maxlen=64)
        with self._tx():
            row = self._conn.execute(
                "SELECT id, theory_price FROM signals WHERE date=? AND code=? AND side=? AND strategy_id=?",
                (d, c, side, sid)).fetchone()
            if row:
                if row["theory_price"] != tp:
                    raise PaperError(f"信号已存在但理论价不同({row['theory_price']} vs {tp}),不覆盖")
                return row["id"]
            cur = self._conn.execute(
                "INSERT INTO signals(date, code, side, theory_price, strategy_id) VALUES (?,?,?,?,?)",
                (d, c, side, tp, sid))
            return cur.lastrowid

    def add_fill(self, signal_id: int, price: float, shares: int, ts: Any,
                 fee: float = 0.0, note: str = "") -> int:
        p = _price(price, "price")
        if isinstance(shares, bool) or not isinstance(shares, int) or shares <= 0:
            raise PaperError(f"shares 必须是正整数,收到 {shares!r}")
        t = _ts(ts)
        if isinstance(fee, bool) or not isinstance(fee, (int, float)) or not math.isfinite(fee) or fee < 0:
            raise PaperError(f"fee 必须是非负有限数,收到 {fee!r}")
        fee = float(fee)
        note = _text(note, "note", allow_empty=True, maxlen=500)
        with self._tx():
            sig = self._require_signal(signal_id)
            if t[:10] < sig["date"]:
                raise PaperError(f"成交日期 {t[:10]} 早于信号日期 {sig['date']}")
            if self._conn.execute("SELECT 1 FROM misses WHERE signal_id=?", (signal_id,)).fetchone():
                raise PaperError(f"信号 {signal_id} 已登记漏单,不能再登记成交")
            row = self._conn.execute(
                "SELECT id, fee FROM fills WHERE signal_id=? AND ts=? AND price=? AND shares=?",
                (signal_id, t, p, shares)).fetchone()
            if row:
                if row["fee"] != fee:
                    raise PaperError("同一笔成交已存在但费用不同,不覆盖")
                return row["id"]
            cur = self._conn.execute(
                "INSERT INTO fills(signal_id, price, shares, ts, fee, note) VALUES (?,?,?,?,?,?)",
                (signal_id, p, shares, t, fee, note))
            return cur.lastrowid

    def add_miss(self, signal_id: int, reason: str, note: str = "") -> int:
        if reason not in MISS_REASONS:
            raise PaperError(f"reason 必须是 {MISS_REASONS},收到 {reason!r}")
        note = _text(note, "note", allow_empty=True, maxlen=500)
        with self._tx():
            self._require_signal(signal_id)
            if self._conn.execute("SELECT 1 FROM fills WHERE signal_id=?", (signal_id,)).fetchone():
                raise PaperError(f"信号 {signal_id} 已有成交,不能再登记漏单")
            row = self._conn.execute("SELECT id, reason FROM misses WHERE signal_id=?", (signal_id,)).fetchone()
            if row:
                if row["reason"] != reason:
                    raise PaperError(f"信号 {signal_id} 已登记漏单原因 {row['reason']},与 {reason} 不同,不覆盖")
                return row["id"]
            cur = self._conn.execute(
                "INSERT INTO misses(signal_id, reason, note) VALUES (?,?,?)", (signal_id, reason, note))
            return cur.lastrowid

    def add_mark(self, date: Any, code: str, price: float, source: str = "") -> None:
        d, c, p = _d(date), _code(code), _price(price, "price")
        source = _text(source, "source", allow_empty=True, maxlen=100)
        with self._tx():
            row = self._conn.execute("SELECT price FROM marks WHERE date=? AND code=?", (d, c)).fetchone()
            if row:
                if row["price"] != p:
                    raise PaperError(f"{d} {c} 已有估值价 {row['price']},与 {p} 不同,不覆盖")
                return
            self._conn.execute("INSERT INTO marks(date, code, price, source) VALUES (?,?,?,?)", (d, c, p, source))

    # ------------------------------------------------------------ 删除(级联)

    def delete_signal(self, signal_id: int) -> bool:
        """删除信号及其成交/漏单(外键级联)。返回是否真的删了一行。"""
        with self._tx():
            return self._conn.execute("DELETE FROM signals WHERE id=?", (signal_id,)).rowcount > 0

    def delete_fill(self, fill_id: int) -> bool:
        with self._tx():
            return self._conn.execute("DELETE FROM fills WHERE id=?", (fill_id,)).rowcount > 0

    def delete_miss(self, miss_id: int) -> bool:
        with self._tx():
            return self._conn.execute("DELETE FROM misses WHERE id=?", (miss_id,)).rowcount > 0

    # ------------------------------------------------------------ 查询

    def _require_signal(self, signal_id: Any):
        if isinstance(signal_id, bool) or not isinstance(signal_id, int):
            raise PaperError(f"signal_id 必须是整数,收到 {signal_id!r}")
        row = self._conn.execute("SELECT * FROM signals WHERE id=?", (signal_id,)).fetchone()
        if row is None:
            raise PaperError(f"信号 {signal_id} 不存在")
        return row

    def get_signal(self, signal_id: int) -> Optional[dict]:
        row = self._conn.execute("SELECT * FROM signals WHERE id=?", (signal_id,)).fetchone()
        return dict(row) if row else None

    def list_signals(self, start: Any = None, end: Any = None) -> list[dict]:
        sql, args = "SELECT * FROM signals", []
        conds = []
        if start is not None:
            conds.append("date >= ?"); args.append(_d(start, "start"))
        if end is not None:
            conds.append("date <= ?"); args.append(_d(end, "end"))
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        return [dict(r) for r in self._conn.execute(sql + " ORDER BY date, id", args)]

    def list_fills(self, signal_id: Optional[int] = None) -> list[dict]:
        if signal_id is None:
            rows = self._conn.execute("SELECT * FROM fills ORDER BY ts, id")
        else:
            rows = self._conn.execute("SELECT * FROM fills WHERE signal_id=? ORDER BY ts, id", (signal_id,))
        return [dict(r) for r in rows]

    def list_misses(self, signal_id: Optional[int] = None) -> list[dict]:
        if signal_id is None:
            rows = self._conn.execute("SELECT * FROM misses ORDER BY id")
        else:
            rows = self._conn.execute("SELECT * FROM misses WHERE signal_id=? ORDER BY id", (signal_id,))
        return [dict(r) for r in rows]

    def list_marks(self, code: Optional[str] = None) -> list[dict]:
        if code is None:
            rows = self._conn.execute("SELECT * FROM marks ORDER BY date, code")
        else:
            rows = self._conn.execute("SELECT * FROM marks WHERE code=? ORDER BY date", (_code(code),))
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ 报告

    def report(self, start: Any, end: Any, initial_cash: float, bt_returns: Mapping[Any, float],
               calendar: Iterable[Any], missing_mark: str = "error") -> dict:
        """汇总 [start, end] 的模拟盘表现并与回测日收益对比。

        - 滑点(正数 = 对自己不利):买入 (成交价-理论价)/理论价,卖出 (理论价-成交价)/理论价;
          统计信号日在窗口内的所有成交,给逐笔平均 avg_slippage 和按成交额加权的 weighted_slippage。
        - 漏单率 = 漏单信号数 / (有成交信号数 + 漏单信号数);既无成交也无漏单的信号算"待定",不进分母。
        - 净值:calendar 中 [start, end] 的每个交易日都有值;窗口起点假设无持仓、现金 = initial_cash。
          成交日期必须在 calendar 内且卖出不得超过持仓,否则 PaperError。
        - 跟踪误差:只比较"calendar 窗口内且 bt_returns 有值"的日子;diff = 模拟盘日收益 - 回测日收益;
          tracking_error = diff 样本标准差 × √252(少于 2 天为 None);cum_deviation = 两边累计收益之差。
        - missing_mark:"error"(默认)缺估值价即抛错;"ffill" 沿用最近估值价并列入 stale_mark_days。
        """
        s, e = _d(start, "start"), _d(end, "end")
        if s > e:
            raise PaperError("start 不能晚于 end")
        cash0 = _price(initial_cash, "initial_cash")
        if missing_mark not in ("error", "ffill"):
            raise PaperError("missing_mark 只能是 'error' 或 'ffill'")
        if not hasattr(bt_returns, "items"):
            raise PaperError("bt_returns 必须是 {日期: 日收益} 映射")
        bt: dict[str, float] = {}
        for k, v in bt_returns.items():
            kd = _d(k, "bt_returns 的日期")
            if kd in bt:
                raise PaperError(f"bt_returns 含重复日期 {kd}")
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= -1:
                raise PaperError(f"bt_returns[{kd}] 必须是有限数且 > -1,收到 {v!r}")
            bt[kd] = float(v)
        days = [x for x in _calendar(calendar) if s <= x <= e]
        if not days:
            raise PaperError("calendar 在 [start, end] 内没有交易日")
        dayset = set(days)

        # ---- 滑点、漏单(按信号日)
        # 窗口起点假设无持仓:start 之前若已有成交,净值会算错,所以直接拒绝
        for sg in self.list_signals():
            for f in self.list_fills(sg["id"]):
                if f["ts"][:10] < s:
                    raise PaperError(f"{f['ts'][:10]} 的成交早于 start={s},report 不支持带初始持仓的窗口"
                                     "(请从第一笔成交日起算)")
        sigs = self.list_signals(s, e)
        slips, notional_w, notional = [], 0.0, 0.0
        n_filled = n_missed = 0
        reasons = {r: 0 for r in MISS_REASONS}
        for sg in sigs:
            # 只统计成交日 ≤ end 的成交,与净值口径一致(报告截止日之后的成交视为尚未发生)
            fl = [f for f in self.list_fills(sg["id"]) if f["ts"][:10] <= e]
            ms = self.list_misses(sg["id"])
            if fl:
                n_filled += 1
            elif ms:
                n_missed += 1
                reasons[ms[0]["reason"]] += 1
            for f in fl:
                sl = ((f["price"] - sg["theory_price"]) if sg["side"] == "buy"
                      else (sg["theory_price"] - f["price"])) / sg["theory_price"]
                slips.append(sl)
                amt = f["price"] * f["shares"]
                notional += amt
                notional_w += sl * amt
        resolved = n_filled + n_missed

        # ---- 逐日净值:按日累计窗口内的成交
        fills_by_day: dict[str, list[dict]] = {}
        for sg in self.list_signals():  # 成交日可能晚于信号日,所以取全部信号再按成交日过滤
            for f in self.list_fills(sg["id"]):
                fd = f["ts"][:10]
                if s <= fd <= e:
                    fills_by_day.setdefault(fd, []).append({**f, "code": sg["code"], "side": sg["side"],
                                                            "theory": sg["theory_price"]})
        for fd in fills_by_day:
            if fd not in dayset:
                raise PaperError(f"成交日期 {fd} 不在 calendar 的 [start, end] 交易日内")
        marks: dict[str, list[tuple[str, float]]] = {}
        for m in self.list_marks():
            marks.setdefault(m["code"], []).append((m["date"], m["price"]))  # 已按日期升序

        cash, pos = cash0, {}
        equity, rets, stale, attrib = {}, {}, [], {}
        prev = cash0
        for d in days:
            slip_cost = fees = 0.0
            for f in sorted(fills_by_day.get(d, []), key=lambda x: (x["ts"], x["id"])):
                amt = f["price"] * f["shares"]
                if f["side"] == "buy":
                    cash -= amt + f["fee"]
                    pos[f["code"]] = pos.get(f["code"], 0) + f["shares"]
                    slip_cost += (f["price"] - f["theory"]) * f["shares"]
                else:
                    if pos.get(f["code"], 0) < f["shares"]:
                        raise PaperError(f"{d} 卖出 {f['code']} {f['shares']} 股超过持仓 {pos.get(f['code'], 0)}")
                    cash += amt - f["fee"]
                    pos[f["code"]] -= f["shares"]
                    slip_cost += (f["theory"] - f["price"]) * f["shares"]
                fees += f["fee"]
            value, day_stale = 0.0, []
            for code, sh in pos.items():
                if sh == 0:
                    continue
                px = next((p for md, p in marks.get(code, []) if md == d), None)
                if px is None:
                    if missing_mark == "error":
                        raise PaperError(f"{d} 持仓 {code} 缺估值价(可用 add_mark 补,或 missing_mark='ffill')")
                    prior = [p for md, p in marks.get(code, []) if md < d]
                    if not prior:
                        raise PaperError(f"{d} 持仓 {code} 没有任何可沿用的估值价")
                    px = prior[-1]
                    day_stale.append(code)
                    stale.append((d, code))
                value += sh * px
            equity[d] = cash + value
            if equity[d] <= 0:
                raise PaperError(f"{d} 净值 {equity[d]} ≤ 0(资金不足或费用过大),无法计算收益率")
            rets[d] = equity[d] / prev - 1
            n_miss_day = sum(1 for sg in sigs if sg["date"] == d and self.list_misses(sg["id"]))
            attrib[d] = {"slippage_cost": slip_cost, "fees": fees,
                         "slippage_cost_ratio": slip_cost / prev, "fee_ratio": fees / prev,
                         "missed_signals": n_miss_day, "stale_codes": day_stale}
            prev = equity[d]

        # ---- 与回测对比
        common = [d for d in days if d in bt]
        diff = {d: rets[d] - bt[d] for d in common}
        dv = list(diff.values())
        te_std = _std(dv)
        cum_p = cum_b = 1.0
        for d in common:
            cum_p *= 1 + rets[d]
            cum_b *= 1 + bt[d]
        for d in days:
            attrib[d]["diff"] = diff.get(d)

        return {
            "start": s, "end": e, "n_days": len(days),
            "n_signals": len(sigs), "n_filled": n_filled, "n_missed": n_missed,
            "n_pending": len(sigs) - resolved,
            "avg_slippage": sum(slips) / len(slips) if slips else None,
            "weighted_slippage": notional_w / notional if notional else None,
            "n_fills": len(slips),
            "miss_rate": n_missed / resolved if resolved else None,
            "miss_reasons": reasons,
            "equity": equity, "daily_returns": rets,
            "final_equity": equity[days[-1]],
            "total_return": equity[days[-1]] / cash0 - 1,
            "stale_mark_days": stale,
            "bt_missing_days": [d for d in days if d not in bt],
            "diff": diff,
            "n_compared": len(common),
            "tracking_error": te_std * math.sqrt(252) if te_std is not None else None,
            "cum_deviation": (cum_p - cum_b) if common else None,
            "attribution": attrib,
            "warnings": ([f"{len(stale)} 天使用了陈旧估值价"] if stale else [])
                        + ([f"模拟盘覆盖仅 {len(days)} 天(< 20),统计意义有限"] if len(days) < 20 else []),
        }
