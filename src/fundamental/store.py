"""基本面数据的 sqlite 存储(S1:只做结构,不下载真实数据)。

约束:
  - 导入模块、构造 FundamentalStore、读空库/不存在的库都不创建任何文件;第一次写入才创建,权限 0600;
  - 拒绝符号链接与非普通文件;拒绝项目内受保护路径(data/daily/ 等);每次打开写连接都把权限改回 0600;
  - 财报按版本保存(point-in-time):同一报告重述不覆盖旧版本,公告日必填,
    查询用 get_metrics_asof(code, as_of_date, metric) 只看 announce_date <= as_of_date 的版本,杜绝未来函数;
  - 所有 SQL 都是参数化查询,表名/列名为常量,不拼接用户输入;
  - 本模块不联网;外部数据只能经调用者注入的 provider_fn 进入(默认抛 NotImplementedError);
  - 错误只抛固定错误码(FundamentalError),不回显用户输入。
"""
import math
import os
import re
import sqlite3
import stat
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# 默认路径只是一个路径,不会在导入时创建
_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_PATH = _ROOT / "data" / "fundamental.db"

# 项目内受保护路径(相对项目根,小写比较;macOS 默认大小写不敏感):其他模块的数据库,绝不允许本模块写入
FORBIDDEN_PATH_PREFIXES = ("data/daily/", "data/signal_ledger", "data/simtrade",
                           "data/main_strategy", "data/market_monitor/")

# 报告期必须是季末;fiscal_period 与季末月日一一对应(累计口径:Q1=一季报,H1=半年报,Q3=三季报,FY=年报)
_PERIOD_BY_MMDD = dict([("03-31", "Q1"), ("06-30", "H1"), ("09-30", "Q3"), ("12-31", "FY")])
EARLIEST_DATE = date(1990, 12, 19)  # A 股最早上市日,更早的报告期不可能有数据

_CODE_RE = re.compile(r"^[0-9]{6}$")  # 不用 \d:它会匹配全角数字
_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_METRIC_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SOURCE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")

_LOCK = threading.Lock()  # 进程内串行化写入
_last_ts = datetime.min  # 上一次发出的 inserted_at,保证严格递增(主键含 inserted_at)

# 主键 (code, report_date, metric, inserted_at):同一报告多次更新各占一行,保留历史。
# announce_date 是"市场何时知道这个数"——所有回测查询都以它为准,而不是 report_date。
_SCHEMA = """
CREATE TABLE IF NOT EXISTS financial_reports (
    code TEXT NOT NULL,
    report_date TEXT NOT NULL,
    fiscal_period TEXT NOT NULL,
    metric TEXT NOT NULL,
    value REAL,
    announce_date TEXT NOT NULL,
    source TEXT NOT NULL,
    inserted_at TEXT NOT NULL,
    PRIMARY KEY (code, report_date, metric, inserted_at)
);
CREATE INDEX IF NOT EXISTS idx_asof ON financial_reports (code, announce_date);
CREATE TABLE IF NOT EXISTS stock_info (
    code TEXT PRIMARY KEY,
    name TEXT,
    industry TEXT,
    listed_date TEXT,
    delisted_date TEXT
);
"""

_COLUMNS = "code, report_date, fiscal_period, metric, value, announce_date, source, inserted_at"


class FundamentalError(Exception):
    """固定错误码,不带用户输入。"""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class StorageError(FundamentalError):
    """路径被守卫拒绝(受保护路径)。"""

    def __init__(self, code="forbidden_path"):
        super().__init__(code)


def default_provider_fn(*args, **kwargs):
    """默认数据源:不联网,直接报错。真实下载由所有者另开阶段、注入自己的函数。"""
    raise NotImplementedError("no provider_fn injected; network access is disabled by default")


# ---------- 输入校验 ----------

def _check_code(code):
    if not isinstance(code, str) or not _CODE_RE.fullmatch(code):
        raise FundamentalError("bad_code")
    return code


def _check_date(d, err="bad_date"):
    """接受 date 或 YYYY-MM-DD 字符串,返回 date;非法日期(如 2024-02-30)拒绝。"""
    if isinstance(d, datetime):
        raise FundamentalError(err)
    if isinstance(d, date):
        out = d
    elif isinstance(d, str) and _DATE_RE.fullmatch(d):
        try:
            out = date.fromisoformat(d)
        except ValueError:
            raise FundamentalError(err) from None
    else:
        raise FundamentalError(err)
    if out < EARLIEST_DATE:
        raise FundamentalError(err)
    return out


def _check_report_date(d):
    out = _check_date(d, "bad_report_date")
    if out.strftime("%m-%d") not in _PERIOD_BY_MMDD:
        raise FundamentalError("bad_report_date")
    return out


def _check_value(v):
    """值可为 None(缺失);其余必须是有限数,bool 不算数。"""
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise FundamentalError("bad_value")
    f = float(v)
    if not math.isfinite(f):
        raise FundamentalError("bad_value")
    return f


def _check_metric(m):
    if not isinstance(m, str) or not _METRIC_RE.fullmatch(m):
        raise FundamentalError("bad_metric")
    return m


def _check_source(s):
    if not isinstance(s, str) or not _SOURCE_RE.fullmatch(s):
        raise FundamentalError("bad_source")
    return s


def _check_text(s, maxlen=64):
    if s is None:
        return None
    if not isinstance(s, str) or len(s) > maxlen or any(ord(c) < 32 for c in s):
        raise FundamentalError("bad_text")
    return s


def _next_ts():
    """微秒精度、进程内严格递增的 UTC 时间戳(调用方须持有 _LOCK)。"""
    global _last_ts
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if now <= _last_ts:
        now = _last_ts + timedelta(microseconds=1)
    _last_ts = now
    return now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def check_path_allowed(path):
    """路径黑名单守卫:落在项目内受保护目录/文件前缀下就抛 StorageError。纯计算,不碰文件内容。

    先 realpath 父目录(挡住 ../ 和指向受保护目录的父目录符号链接),再小写后比前缀。
    """
    p = Path(path)
    try:
        full = os.path.normpath(os.path.join(os.path.realpath(p.parent), p.name))
        root = os.path.realpath(_ROOT)
    except (OSError, ValueError):
        raise FundamentalError("bad_path") from None
    if full.startswith(root + os.sep):
        rel = full[len(root) + 1:].replace(os.sep, "/").lower()
        if any(rel.startswith(pre) for pre in FORBIDDEN_PATH_PREFIXES):
            raise StorageError("forbidden_path")


def _check_path(path):
    """先过黑名单守卫;再拒绝符号链接和非普通文件;不存在则返回 False(不创建)。"""
    check_path_allowed(path)
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError:
        raise FundamentalError("bad_path") from None
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise FundamentalError("bad_path")
    return True


def _check_schema(con):
    """旧结构(没有 announce_date 列)的库不能当 point-in-time 数据用:明确报错,不静默迁移。"""
    cols = [r[1] for r in con.execute("PRAGMA table_info(financial_reports)").fetchall()]
    if cols and "announce_date" not in cols:
        raise FundamentalError("legacy_schema")


class FundamentalStore:
    """基本面 sqlite 存储。path 由调用者指定,默认 data/fundamental.db。"""

    def __init__(self, path=None):
        self.path = Path(path) if path is not None else DEFAULT_PATH

    # ---------- 连接 ----------
    def _connect_read(self):
        """只读连接;库不存在返回 None(不创建文件)。"""
        if not _check_path(self.path):
            return None
        try:
            con = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        except sqlite3.Error:
            raise FundamentalError("db_error") from None
        con.row_factory = sqlite3.Row
        try:
            _check_schema(con)
        except FundamentalError:
            con.close()
            raise
        except sqlite3.Error:
            con.close()
            raise FundamentalError("db_error") from None
        return con

    def _connect_write(self):
        """写连接;文件不存在则以 0600 创建(O_NOFOLLOW + O_EXCL,杜绝符号链接竞争)。"""
        if not _check_path(self.path):
            if not self.path.parent.is_dir():
                raise FundamentalError("bad_path")
            try:
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                os.close(os.open(self.path, flags, 0o600))
            except FileExistsError:
                _check_path(self.path)  # 竞争:别人刚创建,重新检查是否合规
            except OSError:
                raise FundamentalError("bad_path") from None
        con = None
        try:
            os.chmod(self.path, 0o600)  # 不管现有权限(含 0700)一律设成 0600
            con = sqlite3.connect(self.path)
            con.row_factory = sqlite3.Row
            _check_schema(con)  # 必须在建表前:旧表上建 idx_asof 会因缺列失败
            con.executescript(_SCHEMA)
        except FundamentalError:
            if con is not None:
                con.close()
            raise
        except (OSError, sqlite3.Error):
            if con is not None:
                con.close()
            raise FundamentalError("db_error") from None
        return con

    # ---------- 写入 ----------
    def upsert_report(self, code, report_date, metric, value, source, fiscal_period=None, announce_date=None):
        """写一条财务指标的一个版本。announce_date(公告日)必填,缺失抛 missing_announce_date。

        同一 (code, report_date, metric, source) 在同一公告日下值没变 -> 不重复写(幂等);
        值变了或公告日不同 -> 追加新版本,旧版本保留(重述不覆盖)。
        fiscal_period 缺省时由报告期推出;若给了但与报告期不符则拒绝。
        """
        return self.upsert_reports([dict(
            code=code, report_date=report_date, metric=metric, value=value,
            source=source, fiscal_period=fiscal_period, announce_date=announce_date)])

    def upsert_reports(self, rows):
        """批量写入,全部校验通过才落库(一个事务,要么全进要么全不进)。返回新写入的版本行数。"""
        clean = []
        for r in rows:
            if not isinstance(r, dict):
                raise FundamentalError("bad_row")
            code = _check_code(r.get("code"))
            rd = _check_report_date(r.get("report_date"))
            expected = _PERIOD_BY_MMDD[rd.strftime("%m-%d")]
            fp = r.get("fiscal_period") or expected
            if fp != expected:
                raise FundamentalError("bad_fiscal_period")
            metric = _check_metric(r.get("metric"))
            value = _check_value(r.get("value"))
            source = _check_source(r.get("source"))
            if r.get("announce_date") is None:
                raise FundamentalError("missing_announce_date")
            ad = _check_date(r.get("announce_date"), "bad_announce_date")
            if ad < rd:  # 公告日不可能早于报告期末
                raise FundamentalError("bad_announce_date")
            clean.append((code, rd.isoformat(), fp, metric, value, ad.isoformat(), source))
        if not clean:
            return 0
        written = 0
        with _LOCK:
            con = self._connect_write()
            try:
                with con:  # 异常时整体回滚
                    for code, rd, fp, metric, value, ad, source in clean:
                        last = con.execute(
                            "SELECT value FROM financial_reports WHERE code = ? AND report_date = ? "
                            "AND metric = ? AND source = ? AND announce_date = ? "
                            "ORDER BY inserted_at DESC LIMIT 1",
                            (code, rd, metric, source, ad)).fetchone()
                        if last is not None and last["value"] == value:
                            continue  # 同公告日同值:重复写入,跳过
                        con.execute(
                            "INSERT INTO financial_reports (" + _COLUMNS + ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (code, rd, fp, metric, value, ad, source, _next_ts()))
                        written += 1
            except sqlite3.Error:
                raise FundamentalError("db_error") from None
            finally:
                con.close()
        return written

    def upsert_stock_info(self, code, name=None, industry=None, listed_date=None, delisted_date=None):
        """写/更新股票基础信息;日期可为 None。退市日不得早于上市日。"""
        code = _check_code(code)
        listed = _check_date(listed_date).isoformat() if listed_date is not None else None
        delisted = _check_date(delisted_date).isoformat() if delisted_date is not None else None
        if listed and delisted and delisted < listed:
            raise FundamentalError("bad_date")
        name, industry = _check_text(name), _check_text(industry)
        with _LOCK:
            con = self._connect_write()
            try:
                with con:
                    con.execute(
                        "INSERT INTO stock_info (code, name, industry, listed_date, delisted_date) VALUES (?, ?, ?, ?, ?) "
                        "ON CONFLICT (code) DO UPDATE SET name = excluded.name, industry = excluded.industry, "
                        "listed_date = excluded.listed_date, delisted_date = excluded.delisted_date",
                        (code, name, industry, listed, delisted),
                    )
            except sqlite3.Error:
                raise FundamentalError("db_error") from None
            finally:
                con.close()

    # ---------- 查询 ----------
    def _query(self, sql, params):
        con = self._connect_read()
        if con is None:
            return []
        try:
            return [dict(r) for r in con.execute(sql, params).fetchall()]
        except sqlite3.Error:
            raise FundamentalError("db_error") from None
        finally:
            con.close()

    def get_report_asof(self, code, as_of_date, metric, source=None):
        """截至 as_of_date(含)已公告的、报告期最新的那条记录(dict);没有则 None。

        只看 announce_date <= as_of_date 的版本:先取报告期最新的一期,同一期内取公告日最新、再取入库最晚的版本。
        (先按报告期排序,是为了让"对旧报告期的迟到重述"不会盖过更新一期的数据。)
        """
        code, metric = _check_code(code), _check_metric(metric)
        as_of = _check_date(as_of_date, "bad_as_of_date").isoformat()
        sql = ("SELECT " + _COLUMNS + " FROM financial_reports "
               "WHERE code = ? AND metric = ? AND announce_date <= ?")
        params = [code, metric, as_of]
        if source is not None:
            sql += " AND source = ?"
            params.append(_check_source(source))
        rows = self._query(sql + " ORDER BY report_date DESC, announce_date DESC, inserted_at DESC LIMIT 1", params)
        return rows[0] if rows else None

    def get_metrics_asof(self, code, as_of_date, metric, source=None):
        """get_report_asof 的值;截至该日没有已公告记录返回 None(值本身缺失也是 None,要区分用 get_report_asof)。"""
        row = self.get_report_asof(code, as_of_date, metric, source)
        return row["value"] if row else None

    def get_history(self, code, report_date, metric, source=None):
        """某报告期某指标的全部版本(含被重述的旧值),按公告日、入库时间升序。"""
        code, metric = _check_code(code), _check_metric(metric)
        rd = _check_report_date(report_date).isoformat()
        sql = ("SELECT " + _COLUMNS + " FROM financial_reports "
               "WHERE code = ? AND report_date = ? AND metric = ?")
        params = [code, rd, metric]
        if source is not None:
            sql += " AND source = ?"
            params.append(_check_source(source))
        return self._query(sql + " ORDER BY announce_date, inserted_at", params)

    def get_metrics(self, code, report_date, source=None):
        """某股票某报告期的全部指标的"当前最新版本",返回 指标名->值 的 dict;无数据返回空 dict。

        注意:这是"现在库里最新"的视图,不是 point-in-time;回测必须用 get_metrics_asof。
        多个来源同时有同一指标时必须指定 source,否则报 ambiguous_source(不静默挑一个)。
        """
        code = _check_code(code)
        rd = _check_report_date(report_date).isoformat()
        sql = "SELECT metric, value, source FROM financial_reports WHERE code = ? AND report_date = ?"
        params = [code, rd]
        if source is not None:
            sql += " AND source = ?"
            params.append(_check_source(source))
        rows = self._query(sql + " ORDER BY metric, source, announce_date, inserted_at", params)
        latest = {}  # (指标, 来源) -> 值;升序遍历,后面的版本覆盖前面的
        for r in rows:
            latest[(r["metric"], r["source"])] = r["value"]
        out = {}
        for (metric, _src), value in latest.items():
            if metric in out:
                raise FundamentalError("ambiguous_source")
            out[metric] = value
        return out

    def list_reports(self, code, start=None, end=None, metric=None):
        """某股票的全部报告行(含历史版本),按报告期、指标、公告日排序;start/end 为报告期闭区间。"""
        code = _check_code(code)
        sql = "SELECT " + _COLUMNS + " FROM financial_reports WHERE code = ?"
        params = [code]
        if start is not None:
            sql += " AND report_date >= ?"
            params.append(_check_date(start).isoformat())
        if end is not None:
            sql += " AND report_date <= ?"
            params.append(_check_date(end).isoformat())
        if metric is not None:
            sql += " AND metric = ?"
            params.append(_check_metric(metric))
        return self._query(sql + " ORDER BY report_date, metric, source, announce_date, inserted_at", params)

    def list_codes(self):
        """库里有财报的全部代码(升序)。"""
        return [r["code"] for r in self._query(
            "SELECT DISTINCT code FROM financial_reports ORDER BY code", [])]

    def get_stock_info(self, code):
        rows = self._query(
            "SELECT code, name, industry, listed_date, delisted_date FROM stock_info WHERE code = ?",
            [_check_code(code)])
        return rows[0] if rows else None

    def count(self):
        """各表行数(financial_reports 含历史版本);库不存在返回 0,不创建文件。"""
        out = {"financial_reports": 0, "stock_info": 0}
        con = self._connect_read()
        if con is None:
            return out
        try:
            for t in out:  # 表名来自上面的常量,不是用户输入
                out[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except sqlite3.Error:
            raise FundamentalError("db_error") from None
        finally:
            con.close()
        return out

    # ---------- 清空 ----------
    def wipe(self, confirm=False):
        """清空两张表的全部行(保留文件与表结构)。必须显式 confirm=True;库不存在则什么也不做。"""
        if confirm is not True:
            raise FundamentalError("confirm_required")
        if not _check_path(self.path):
            return
        with _LOCK:
            con = self._connect_write()
            try:
                with con:
                    con.execute("DELETE FROM financial_reports")
                    con.execute("DELETE FROM stock_info")
            except sqlite3.Error:
                raise FundamentalError("db_error") from None
            finally:
                con.close()


def ingest_reports(store, codes, report_dates, provider_fn=default_provider_fn):
    """用调用者注入的 provider_fn 拉取并入库。

    provider_fn(code, report_date: date) -> list[dict]
        每个 dict 含 metric、value、source、announce_date(可选 fiscal_period);code/report_date 由本函数补上。
    默认的 provider_fn 直接抛 NotImplementedError,不会意外联网。返回写入行数。
    """
    total = 0
    for code in codes:
        _check_code(code)
        for rd in report_dates:
            rd = _check_report_date(rd)
            rows = provider_fn(code, rd)
            batch = [dict(r, code=code, report_date=rd) for r in (rows or [])]
            total += store.upsert_reports(batch)
    return total
