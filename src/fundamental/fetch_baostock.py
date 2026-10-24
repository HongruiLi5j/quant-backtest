"""BaoStock 季频财务数据 provider(T014 S5):把 baostock 的返回整理成 FundamentalStore 认的记录格式。

为什么选 BaoStock:免费、公开、不需要 key;项目已在 src/allmarket.py 用它拉行业分类。

时点性(point-in-time,防未来函数)是本模块的第一原则:
  - 每条记录 announce_date = baostock 的 pubDate(公告日),report_date = statDate(报告期末);
  - pubDate 缺失 / 不是合法日期 / 早于报告期 / 晚于今天 的整行丢弃并计数,**绝不用报告期冒充公告日**;
  - 分红只记"已实施"的:announce_date = 除权除息日(dividOperateDate),缺失同样丢弃并计数。

BaoStock 季频接口大多给的是"比率",不是三大表的绝对金额,所以入库时只存原始值(见 PROFIT_FIELDS 等映射),
导出时再用恒等式还原(assemble_asof):
    营业收入_累计   = 净利润_累计 / 销售净利率(npMargin)
    营业成本_累计   = 营业收入_累计 × (1 − 毛利率 gpMargin)
    经营现金流_累计 = 经营现金流/净利润(CFOToNP) × 净利润_累计
    TTM            = 本期累计 + 上年年报 − 上年同期累计(Q1/H1/Q3 累计口径;年报本身就是 TTM)
  归母净资产、总资产、总负债、营业利润这几个绝对值 baostock 季频接口不给,导出为 NaN,不猜。
  [需核实] baostock 的 netProfit 口径(是否含少数股东损益)、npMargin/gpMargin 是小数还是百分数,见 docs/fundamental_howto.md。

联网约束:login/logout 在本模块内管理(惰性登录,进程退出时登出);每次请求之间至少间隔 0.2 秒;
连续失败 5 次抛 BaostockUnavailable,由调用者停下报错。测试通过 bs_module= 注入假模块,不联网。
"""
from __future__ import annotations

import atexit
import math
import re
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

SOURCE = "baostock"
MIN_INTERVAL = 0.2          # 两次请求之间的最小间隔(秒)
MAX_CONSECUTIVE_FAILURES = 5
BJ = ZoneInfo("Asia/Shanghai")

QUARTER_END = {1: "03-31", 2: "06-30", 3: "09-30", 4: "12-31"}
_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")

# baostock 字段 -> 入库指标名(全部是 baostock 原样的数值,不做换算)
PROFIT_FIELDS = {
    "netProfit": "net_profit",        # 净利润(元,累计)
    "npMargin": "np_margin",          # 销售净利率 = 净利润 / 营业收入
    "gpMargin": "gp_margin",          # 毛利率 = (营业收入 − 营业成本) / 营业收入
    "MBRevenue": "mb_revenue",        # 主营营业收入(元,累计;季报常为空,只存档不参与计算)
    "roeAvg": "roe_avg",              # 净资产收益率(平均)
    "epsTTM": "eps_ttm",              # 每股收益 TTM
    "totalShare": "total_share",      # 总股本(股)
    "liqaShare": "liqa_share",        # 流通股本(股)
}
BALANCE_FIELDS = {
    "liabilityToAsset": "liability_to_asset",  # 资产负债率
    "assetToEquity": "asset_to_equity",        # 权益乘数 = 总资产 / 股东权益
    "currentRatio": "current_ratio",
    "YOYLiability": "yoy_liability",
}
CASH_FLOW_FIELDS = {
    "CFOToNP": "cfo_to_np",           # 经营现金流净额 / 净利润
    "CFOToOR": "cfo_to_or",           # 经营现金流净额 / 营业收入
}
DIVIDEND_METRIC = "div_cash_ps"           # 每股税前现金分红(元),announce_date = 除权除息日
DIVIDEND_YEAR_MARKER = "div_year_queried"  # 标记:某个除权除息年份已经查询过(用来区分"没分红"和"没查过")


class FetchError(Exception):
    """单次请求失败(网络/接口报错)。调用者可跳过这一季,下次断点续跑再补。"""


class BaostockUnavailable(FetchError):
    """连续失败达到上限:停下报错,不要继续刷请求。"""


def bs_code(code: str) -> str:
    return ("sh." if code.startswith("6") else "sz.") + code


def _num(s):
    """baostock 返回的字符串 -> float;空串/非数/非有限 -> None。"""
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    return v if math.isfinite(v) else None


def _day(s):
    """'YYYY-MM-DD' -> date;其他 -> None。"""
    if not isinstance(s, str) or not _DATE_RE.fullmatch(s.strip()):
        return None
    try:
        return date.fromisoformat(s.strip())
    except ValueError:
        return None


def _today_bj():
    return datetime.now(BJ).date()


def quarter_end(year: int, quarter: int) -> date:
    return date.fromisoformat("%04d-%s" % (year, QUARTER_END[quarter]))


def quarter_end_on_or_before(d: date) -> date:
    """d 当天或之前最近的季末日(分红记录的 report_date,保证 announce_date >= report_date)。"""
    for q in (4, 3, 2, 1):
        qe = quarter_end(d.year, q)
        if qe <= d:
            return qe
    return quarter_end(d.year - 1, 4)


class BaostockClient:
    """baostock 会话 + 限速 + 连续失败计数。bs_module/sleep/clock 可注入(测试用假的,不联网)。"""

    def __init__(self, bs_module=None, *, interval=MIN_INTERVAL, max_failures=MAX_CONSECUTIVE_FAILURES,
                 sleep=time.sleep, clock=time.monotonic, today=_today_bj):
        self._bs = bs_module
        self.interval = interval
        self.max_failures = max_failures
        self._sleep = sleep
        self._clock = clock
        self._today = today
        self._logged_in = False
        self._last_request = None
        self.consecutive_failures = 0
        self.stats = {"requests": 0, "failures": 0, "records": 0,
                      "dropped_no_pubdate": 0, "dropped_bad_pubdate": 0, "dropped_stat_mismatch": 0,
                      "dropped_no_exdate": 0, "dropped_bad_dividend": 0}

    # ---------- 会话 ----------
    def _module(self):
        if self._bs is None:
            import baostock as bs   # 只有真正发请求时才导入
            self._bs = bs
        return self._bs

    def login(self):
        if self._logged_in:
            return
        import socket
        socket.setdefaulttimeout(60)   # baostock 用裸 socket 且默认不超时,卡住就永远等(同 allmarket.BaoStock)
        lg = self._module().login()
        if getattr(lg, "error_code", "1") != "0":
            raise FetchError("baostock login failed: %s" % getattr(lg, "error_msg", ""))
        self._logged_in = True

    def logout(self):
        if not self._logged_in:
            return
        self._logged_in = False
        try:
            self._module().logout()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.logout()

    # ---------- 请求 ----------
    def _wait(self):
        if self._last_request is not None:
            gap = self.interval - (self._clock() - self._last_request)
            if gap > 0:
                self._sleep(gap)
        self._last_request = self._clock()

    def _fail(self, msg):
        self.stats["failures"] += 1
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.max_failures:
            raise BaostockUnavailable("baostock: %d consecutive failures, last: %s" % (self.consecutive_failures, msg))
        raise FetchError(msg)

    def query(self, fn_name, **kwargs):
        """调一个 baostock 查询接口,返回 list[dict](字段名 -> 字符串)。失败抛 FetchError / BaostockUnavailable。"""
        try:
            self.login()
        except Exception as e:   # 登录失败(含网络异常)同样计入连续失败
            self._fail("login: %s" % (e if isinstance(e, FetchError) else type(e).__name__))
        self._wait()
        self.stats["requests"] += 1
        try:
            rs = getattr(self._module(), fn_name)(**kwargs)
            if getattr(rs, "error_code", "1") != "0":
                raise FetchError("%s: %s" % (fn_name, getattr(rs, "error_msg", "")))
            fields = list(rs.fields)
            rows = []
            while rs.next():
                rows.append(dict(zip(fields, rs.get_row_data())))
        except Exception as e:   # 网络超时、socket 断开、接口报错都算一次失败
            self._logged_in = False   # 下次请求重新登录
            self._fail("%s: %s" % (fn_name, type(e).__name__ if not isinstance(e, FetchError) else e))
        self.consecutive_failures = 0
        return rows

    # ---------- 解析 ----------
    def _pit_rows(self, rows, fields, code, rd):
        """把一个接口的返回转成入库记录;pubDate 有问题的整行丢弃并计数。"""
        out = []
        today = self._today()
        for row in rows:
            stat_d = _day(row.get("statDate"))
            if stat_d != rd:
                self.stats["dropped_stat_mismatch"] += 1
                continue
            raw_pub = (row.get("pubDate") or "").strip()
            if not raw_pub:
                self.stats["dropped_no_pubdate"] += 1   # 不用报告期冒充公告日
                continue
            pub = _day(raw_pub)
            if pub is None or pub < rd or pub > today:
                self.stats["dropped_bad_pubdate"] += 1
                continue
            for bs_field, metric in fields.items():
                if bs_field not in row:
                    continue
                out.append(dict(code=code, report_date=rd.isoformat(), metric=metric, value=_num(row[bs_field]),
                                source=SOURCE, announce_date=pub.isoformat()))
        return out

    def fetch_quarterly(self, code, year, quarter):
        """一只股票一个报告期:利润表 + 资产负债 + 现金流 三个接口,返回 FundamentalStore.upsert_reports 的记录。

        任一接口请求失败就整体抛 FetchError(不返回半份数据),由调用者下次断点续跑。
        """
        rd = quarter_end(int(year), int(quarter))
        kw = dict(code=bs_code(code), year=int(year), quarter=int(quarter))
        out = []
        for fn_name, fields in (("query_profit_data", PROFIT_FIELDS),
                                ("query_balance_data", BALANCE_FIELDS),
                                ("query_cash_flow_data", CASH_FLOW_FIELDS)):
            out += self._pit_rows(self.query(fn_name, **kw), fields, code, rd)
        self.stats["records"] += len(out)
        return out

    def fetch_dividends(self, code, year):
        """某个除权除息年份(yearType=operate)已实施的现金分红。

        返回记录:metric=div_cash_ps,announce_date=除权除息日,report_date=该日当天或之前最近的季末;
        同一除权日的多条合并求和。另附一条 div_year_queried 标记(value=1),表示这一年查过了。
        """
        rows = self.query("query_dividend_data", code=bs_code(code), year=str(int(year)), yearType="operate")
        today = self._today()
        by_ex = {}
        for row in rows:
            ex = _day(row.get("dividOperateDate"))
            if ex is None:
                self.stats["dropped_no_exdate"] += 1   # 还没实施 / 日期缺失:不计入
                continue
            if ex > today or ex.year != int(year):
                self.stats["dropped_bad_dividend"] += 1
                continue
            cash = _num(row.get("dividCashPsBeforeTax"))
            if cash is None:
                cash = 0.0 if (row.get("dividCashPsBeforeTax") or "").strip() == "" else None
            if cash is None or cash < 0:
                self.stats["dropped_bad_dividend"] += 1
                continue
            by_ex[ex] = by_ex.get(ex, 0.0) + cash
        out = [dict(code=code, report_date=quarter_end_on_or_before(ex).isoformat(), metric=DIVIDEND_METRIC,
                    value=v, source=SOURCE, announce_date=ex.isoformat()) for ex, v in sorted(by_ex.items())]
        ye = quarter_end(int(year), 4).isoformat()
        out.append(dict(code=code, report_date=ye, metric=DIVIDEND_YEAR_MARKER, value=1.0,
                        source=SOURCE, announce_date=ye))
        self.stats["records"] += len(out)
        return out

    def fetch_close_raw(self, code, asof):
        """asof 当天的未复权收盘价(adjustflag=3);当天停牌/无数据返回 None。

        本地 data/daily/cn_all 是后复权价,不能用来算市值(会把市值放大几倍到几十倍),所以单独取未复权价。
        """
        d = asof if isinstance(asof, date) else date.fromisoformat(str(asof))
        rows = self.query("query_history_k_data_plus", code=bs_code(code), fields="date,close,tradestatus",
                          start_date=(d - timedelta(days=10)).isoformat(), end_date=d.isoformat(),
                          frequency="d", adjustflag="3")
        for row in rows:
            if _day(row.get("date")) == d and str(row.get("tradestatus", "1")).strip() == "1":
                v = _num(row.get("close"))
                return v if v is not None and v > 0 else None
        return None


# ---------- 模块级默认会话(真实运行用;测试一律传自己的 client) ----------
_default = None


def default_client():
    global _default
    if _default is None:
        _default = BaostockClient()
        atexit.register(_default.logout)
    return _default


def fetch_quarterly(code, year, quarter, client=None):
    """见 BaostockClient.fetch_quarterly;client 缺省用模块内的默认会话。"""
    return (client or default_client()).fetch_quarterly(code, year, quarter)


def fetch_dividends(code, year, client=None):
    return (client or default_client()).fetch_dividends(code, year)


def fetch_close_raw(code, asof, client=None):
    return (client or default_client()).fetch_close_raw(code, asof)


def logout():
    if _default is not None:
        _default.logout()


# ---------- 导出:按 asof 组装 factors.py 要的字段(纯读库,不联网) ----------

_PERIOD_Q = {"03-31": 1, "06-30": 2, "09-30": 3, "12-31": 4}
NAN = float("nan")


def _pit_series(store, code, asof, metric):
    """截至 asof(含)已公告的 {报告期: 值};同一报告期取公告日最新、入库最晚的版本(与 store.get_report_asof 同规则)。

    返回 (值字典, 公告日字典)。
    """
    vals, anns = {}, {}
    asof_s = asof.isoformat()
    for r in store.list_reports(code, metric=metric):   # 已按 报告期, 来源, 公告日, 入库时间 升序
        if r["source"] != SOURCE or r["announce_date"] > asof_s:
            continue
        rd = date.fromisoformat(r["report_date"])
        vals[rd], anns[rd] = r["value"], r["announce_date"]
    return vals, anns


def _ttm(cum, rd):
    """TTM = 本期累计 + 上年年报 − 上年同期累计;年报直接取年报。缺任何一期 -> NaN(不插值、不猜)。"""
    q = _PERIOD_Q[rd.strftime("%m-%d")]
    cur = cum.get(rd)
    if q == 4:
        return cur if cur is not None else NAN
    prev_fy = cum.get(quarter_end(rd.year - 1, 4))
    prev_same = cum.get(quarter_end(rd.year - 1, q))
    if cur is None or prev_fy is None or prev_same is None:
        return NAN
    return cur + prev_fy - prev_same


def _one_year_before(d):
    try:
        return d.replace(year=d.year - 1)
    except ValueError:      # 2 月 29 日
        return d.replace(year=d.year - 1, day=28)


def dps_ttm_asof(store, code, asof):
    """过去 12 个月(除权除息日 ∈ (asof−1年, asof])已实施的每股税前现金分红合计。

    这两年(asof 所在年和上一年)只要有一年没查过分红,就返回 NaN(没查过 ≠ 没分红)。
    """
    queried = {date.fromisoformat(r["report_date"]).year
               for r in store.list_reports(code, metric=DIVIDEND_YEAR_MARKER) if r["source"] == SOURCE}
    start = _one_year_before(asof)
    if not {start.year, asof.year} <= queried:
        return NAN
    latest = {}   # 除权日 -> 值(同一除权日取最晚入库的版本)
    for r in store.list_reports(code, metric=DIVIDEND_METRIC):
        if r["source"] != SOURCE:
            continue
        ex = date.fromisoformat(r["announce_date"])
        if start < ex <= asof and r["value"] is not None:
            latest[ex] = r["value"]
    return float(sum(latest.values()))


def assemble_asof(store, code, asof, close_raw=None):
    """截至 asof 可见的数据 -> factors.py 文档要求的一行(dict)。没有任何已公告财报返回 None。

    最新报告期由 store.get_report_asof(net_profit) 决定(只看 announce_date <= asof);
    MRQ 字段(总股本)用 store.get_metrics_asof;TTM 用各期累计值差分。
    available_date = 本行用到的所有记录里最晚的公告日(必然 <= asof)。
    """
    asof = asof if isinstance(asof, date) else date.fromisoformat(str(asof))
    head = store.get_report_asof(code, asof, "net_profit", source=SOURCE)
    if head is None:
        return None
    rd = date.fromisoformat(head["report_date"])
    prev_rd = quarter_end(rd.year - 1, _PERIOD_Q[rd.strftime("%m-%d")])

    np_cum, np_ann = _pit_series(store, code, asof, "net_profit")
    npm, npm_ann = _pit_series(store, code, asof, "np_margin")
    gpm, gpm_ann = _pit_series(store, code, asof, "gp_margin")
    cfo_np, cfo_ann = _pit_series(store, code, asof, "cfo_to_np")

    def derived(fn, *series):
        out = {}
        for p in series[0]:
            args = [s.get(p) for s in series]
            if all(a is not None for a in args):
                v = fn(*args)
                if v is not None and math.isfinite(v):
                    out[p] = v
        return out

    rev_cum = derived(lambda n, m: n / m if m != 0 else None, np_cum, npm)
    cost_cum = derived(lambda r, g: r * (1.0 - g), rev_cum, gpm)
    cfo_cum = derived(lambda c, n: c * n, cfo_np, np_cum)

    used = [head["announce_date"]]
    for p in {rd, prev_rd, quarter_end(rd.year - 1, 4), quarter_end(rd.year - 2, 4),
              quarter_end(rd.year - 2, _PERIOD_Q[rd.strftime("%m-%d")])}:
        for anns in (np_ann, npm_ann, gpm_ann, cfo_ann):
            if p in anns:
                used.append(anns[p])

    total_share = store.get_metrics_asof(code, asof, "total_share", source=SOURCE)
    share_row = store.get_report_asof(code, asof, "total_share", source=SOURCE)
    if share_row is not None:
        used.append(share_row["announce_date"])
    close = close_raw if close_raw is not None and close_raw > 0 else NAN
    mcap = close * total_share if total_share is not None and total_share > 0 else NAN

    return {
        "code": code,
        "market_cap": mcap,
        "close_raw": close,
        "net_profit_ttm": _ttm(np_cum, rd),
        "net_profit_ttm_prev": _ttm(np_cum, prev_rd),
        "revenue_ttm": _ttm(rev_cum, rd),
        "revenue_ttm_prev": _ttm(rev_cum, prev_rd),
        "operating_cost_ttm": _ttm(cost_cum, rd),
        "operating_profit_ttm": NAN,       # baostock 季频接口无营业利润
        "equity_parent": NAN,              # baostock 季频接口只给比率,无绝对金额
        "total_assets": NAN,
        "total_liabilities": NAN,
        "cfo_ttm": _ttm(cfo_cum, rd),
        "dps_ttm": dps_ttm_asof(store, code, asof),
        "available_date": max(used),
        "report_date": rd.isoformat(),
    }
