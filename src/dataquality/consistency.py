"""单只股票 OHLCV 序列的一致性与异常检测:返回结构化问题列表(Issue),不抛异常。

检查项:
  1. 复权:前复权/后复权换算是否一致、复权因子是否单调、由原始价推出除权日 (check_adjustment)
  2. 缺失交易日:对照交易日历,命中停牌名单的降为 info (check_calendar_gaps)
  3. 异常跳变:单日涨跌幅超过该板块涨跌停阈值(含放宽量)且非除权日 (check_jumps)
  4. 零成交量但有价格变动 (check_zero_volume)
  5. 坏数据:最高<最低、价格<=0、NaN、High/Low 不包住 Open/Close、成交量为负、日期重复/乱序 (check_ohlc)

约束:纯函数,不联网,不读写 data/,不 import allmarket/datasource 等有副作用的模块。
所有比较都用比值(收益率、因子比),所以整体乘一个常数(合并来源时的重定基准)不改变结果。
涨跌幅制度的生效日期是凭记忆写的默认值 [需核实],可通过 Thresholds 修改。
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import date

import pandas as pd

from .status_table import board_of, parse_date

SEVERITY_ORDER = {"error": 0, "warn": 1, "info": 2}
OHLC = ["Open", "High", "Low", "Close"]

# (规则名, 起始日, 涨跌幅);同一规则名取"起始日 <= 当天"中最晚的一条。表只在 src/limits.py 维护一份(D17),这里直接引用。
# "ST" = 主板 ST(5%,2026-07-06 起 10%);"创业板ST" = 创业板 ST(改革前 5%,2020-08-24 起 20%)。
from ..limits import LIMIT_RULES as DEFAULT_LIMIT_RULES  # noqa: E402


@dataclass(frozen=True)
class Thresholds:
    """全部阈值可配置(构造时校验;非法值抛 ValueError)。

    limit_rules/default_limit: 涨跌幅制度表与板块未知时的幅度(比例,0.10=10%)。
    slack: 涨跌停之外再放宽的绝对收益率。
    new_listing_free_days: 上市后前 N 根 K 线免查跳变(根数)。
    min_factor_ratio / ex_factor_change: 复权因子相邻比值的回退下限与除权日判定,
        都是比值,必须能吸收价格四舍五入(两位小数)带来的抖动,故取 0.5% 量级。
    conv_tol / ohlc_tol: 相对误差容忍。max_gap_days: 缺口天数下限(天)。
    zero_vol_move: 零量时允许的相对价格变动。max_reports_per_kind: 每类最多报告条数。
    默认值多为经验值 [需核实]。
    """
    limit_rules: tuple = DEFAULT_LIMIT_RULES
    default_limit: float = 0.10     # 板块未知时使用的涨跌幅(偏严)
    slack: float = 0.02             # 在涨跌停幅度上再放宽的绝对量(四舍五入、复权舍入)
    new_listing_free_days: int = 5  # 上市前 N 个交易日不设涨跌停,不报跳变(需提供 ipo)
    min_factor_ratio: float = 0.995  # 复权因子相邻比值低于它视为回退(留出两位小数舍入的抖动)
    ex_factor_change: float = 5e-3  # 因子相邻比值偏离 1 超过它视为除权日(低价股舍入抖动可达 3e-3)
    conv_tol: float = 1e-4          # 前复权/后复权换算、因子对账的相对误差容忍
    ohlc_tol: float = 1e-8          # High/Low 包络比较的相对容忍
    max_gap_days: int = 0           # 连续缺失交易日超过 N 天才报(0=任何缺口)
    zero_vol_move: float = 0.0      # 零成交量时允许的相对价格变动
    max_reports_per_kind: int = 200  # 同一类问题最多报告条数,超出另记一条 info

    def __post_init__(self):
        for name in ("default_limit", "slack", "conv_tol", "ohlc_tol", "zero_vol_move",
                     "ex_factor_change"):
            v = getattr(self, name)
            if not (isinstance(v, (int, float)) and math.isfinite(v) and v >= 0):
                raise ValueError(f"Thresholds.{name} 必须是非负有限数,收到 {v!r}")
        if not (0 < self.min_factor_ratio <= 1):
            raise ValueError(f"Thresholds.min_factor_ratio 必须在 (0,1],收到 {self.min_factor_ratio!r}")
        for name in ("new_listing_free_days", "max_gap_days"):
            if not isinstance(getattr(self, name), int) or getattr(self, name) < 0:
                raise ValueError(f"Thresholds.{name} 必须是非负整数")
        if not isinstance(self.max_reports_per_kind, int) or self.max_reports_per_kind < 1:
            raise ValueError("Thresholds.max_reports_per_kind 必须是正整数")


@dataclass(frozen=True)
class Issue:
    code: str
    day: date | None
    kind: str
    severity: str  # "error" | "warn" | "info"
    detail: str
    value: float | None = None

    def to_dict(self) -> dict:
        """可直接 json.dumps:日期转成 ISO 字符串。"""
        d = asdict(self)
        d["day"] = self.day.isoformat() if self.day is not None else None
        v = d["value"]
        if v is not None and not math.isfinite(v):
            d["value"] = None  # NaN/inf 不是合法 JSON
        return d


# ---------------------------------------------------------------- 内部工具

def _num(x) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return math.nan
    return v


def _prep(df):
    """返回 (按日期索引的 DataFrame 副本, None) 或 (None, 错误说明)。不排序,保留原顺序以便查乱序。"""
    if df is None or not isinstance(df, pd.DataFrame):
        return None, "输入不是 DataFrame"
    try:
        idx = pd.DatetimeIndex(pd.to_datetime(df.index))
    except Exception as e:  # noqa: BLE001
        return None, f"索引无法转成日期:{e}"
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    out = df.copy()
    out.index = idx.normalize()
    return out, None


def _limit_for(board, day, is_st, th: Thresholds) -> float:
    """某板块某天的涨跌幅(不含 slack)。主板 ST 用 ST 规则;其他板块的 ST 沿用板块幅度。"""
    name = board
    if is_st and board in ("主板", "创业板"):
        name = "ST" if board == "主板" else "创业板ST"
        if not any(b == name for b, _, _ in th.limit_rules):      # 自定义规则表里没有这一行:沿用板块幅度
            name = board
    cand = [(s, p) for b, s, p in th.limit_rules if b == name and s <= day]
    if not cand:
        return th.default_limit
    return max(cand)[1]


def _in_spans(day, spans) -> bool:
    for s, e in spans or ():
        sd, ed = parse_date(s), parse_date(e)
        if sd is None:
            continue
        if sd <= day and (ed is None or day <= ed):
            return True
    return False


def _cap(issues: list[Issue], code: str, th: Thresholds) -> list[Issue]:
    """每类问题最多保留 max_reports_per_kind 条,超出的合并成一条 info。"""
    cnt: dict[str, int] = {}
    over: dict[str, int] = {}
    out = []
    for it in issues:
        cnt[it.kind] = cnt.get(it.kind, 0) + 1
        if cnt[it.kind] <= th.max_reports_per_kind:
            out.append(it)
        else:
            over[it.kind] = over.get(it.kind, 0) + 1
    for k, n in over.items():
        out.append(Issue(code, None, k + "_truncated", "info",
                         f"{k} 超出上限,另有 {n} 条未列出", float(n)))
    return out


def _sort(issues: list[Issue]) -> list[Issue]:
    return sorted(issues, key=lambda i: (SEVERITY_ORDER.get(i.severity, 9),
                                         i.day or date.min, i.kind))


# ---------------------------------------------------------------- 5. 坏数据

def check_ohlc(df, code: str = "", th: Thresholds = Thresholds()) -> list[Issue]:
    """坏行情:NaN、价格<=0、High<Low、High/Low 没包住 Open/Close、成交量为负、日期重复或乱序。"""
    d, err = _prep(df)
    if d is None:
        return [Issue(code, None, "bad_input", "error", err)]
    out: list[Issue] = []
    if len(d) == 0:
        return out
    missing = [c for c in OHLC if c not in d.columns]
    if missing:
        return [Issue(code, None, "missing_column", "error", f"缺少列:{missing}")]

    if d.index.has_duplicates:
        for ts in d.index[d.index.duplicated()].unique():
            out.append(Issue(code, ts.date(), "duplicate_date", "error", "日期重复"))
    if not d.index.is_monotonic_increasing:
        out.append(Issue(code, None, "unsorted_index", "error", "日期不是升序"))

    tol = th.ohlc_tol
    for ts, row in zip(d.index, d[OHLC].itertuples(index=False)):
        day = ts.date()
        o, h, lo, c = (_num(v) for v in row)
        vals = (o, h, lo, c)
        if any(math.isnan(v) or math.isinf(v) for v in vals):
            out.append(Issue(code, day, "nan_price", "error", "OHLC 含 NaN/inf"))
            continue
        if any(v <= 0 for v in vals):
            out.append(Issue(code, day, "nonpositive_price", "error",
                             f"价格<=0:O={o} H={h} L={lo} C={c}", min(vals)))
            continue
        if h < lo * (1 - tol):
            out.append(Issue(code, day, "high_below_low", "error",
                             f"最高<最低:H={h} L={lo}", h / lo - 1))
            continue
        if h < max(o, c) * (1 - tol):
            out.append(Issue(code, day, "high_not_max", "error",
                             f"最高低于开/收:H={h} O={o} C={c}", h / max(o, c) - 1))
        if lo > min(o, c) * (1 + tol):
            out.append(Issue(code, day, "low_not_min", "error",
                             f"最低高于开/收:L={lo} O={o} C={c}", lo / min(o, c) - 1))
    if "Volume" in d.columns:
        for ts, v in zip(d.index, d["Volume"]):
            x = _num(v)
            if x < 0:
                out.append(Issue(code, ts.date(), "negative_volume", "error",
                                 f"成交量为负:{x}", x))
            elif math.isnan(x):
                out.append(Issue(code, ts.date(), "nan_volume", "warn", "成交量为空"))
    return _cap(out, code, th)


# ---------------------------------------------------------------- 2. 缺失交易日

def check_calendar_gaps(df, calendar, code: str = "", suspended=None,
                        calendar_trusted: bool = True,
                        th: Thresholds = Thresholds()) -> list[Issue]:
    """序列首尾之间缺失的交易日(只查中间缺口,不查末尾)。

    calendar:      交易日列表(date/str/Timestamp);为空或 None 则无法检查。
    suspended:     {日期: 含该股的代码集合/列表} 的停牌名单;命中的缺失日降为 info。
    calendar_trusted=False(日历退化为周一到周五,不含节假日)时不判断缺口,只给一条 info。
    """
    d, err = _prep(df)
    if d is None:
        return [Issue(code, None, "bad_input", "error", err)]
    if not calendar_trusted:
        return [Issue(code, None, "calendar_untrusted", "info", "日历不可信,未检查缺失交易日")]
    cal = sorted({x for x in (parse_date(c) for c in (calendar or [])) if x is not None})
    if not cal or len(d) == 0:
        return []
    have = {ts.date() for ts in d.index}
    first, last = min(have), max(have)
    susp = {}
    for k, v in (suspended or {}).items():
        kd = parse_date(k)
        if kd is not None:
            susp[kd] = {str(x) for x in v}

    out: list[Issue] = []
    unexplained: list[date] = []
    for day in cal:
        if day < first or day > last or day in have:
            continue
        if code and code in susp.get(day, ()):
            out.append(Issue(code, day, "gap_suspended", "info", "缺 K 线,在停牌名单中"))
        else:
            unexplained.append(day)
    # 把连续的缺失交易日(在日历上相邻)合并成一段
    pos = {d_: i for i, d_ in enumerate(cal)}
    runs: list[list[date]] = []
    for day in unexplained:
        if runs and pos[day] == pos[runs[-1][-1]] + 1:
            runs[-1].append(day)
        else:
            runs.append([day])
    for run in runs:
        if len(run) > th.max_gap_days:
            out.append(Issue(code, run[0], "missing_trading_days", "warn",
                             f"缺失 {len(run)} 个交易日:{run[0]} ~ {run[-1]}(不在停牌名单)",
                             float(len(run))))
    for day in sorted(have - set(cal)):
        if first <= day <= last:
            out.append(Issue(code, day, "bar_not_in_calendar", "warn", "有 K 线但不在交易日历里"))
    return _cap(out, code, th)


# ---------------------------------------------------------------- 3. 异常跳变

def check_jumps(df, code: str = "", board: str | None = None, ex_days=None, st_spans=None,
                ipo=None, th: Thresholds = Thresholds()) -> list[Issue]:
    """单日(相邻两根 K 线)收盘涨跌幅超过 涨跌停+slack 且当天不是除权日 -> warn。

    board:    "主板"/"创业板"/"科创板";None 时按代码前缀推断,仍未知则用 default_limit。
    ex_days:  除权日集合,当天不报(后复权序列在除权日本身应连续,这里是给原始/混合口径留的豁免)。
    st_spans: [(起, 止或None)];主板 ST 用 5%。
    ipo:      上市日;从上市日起的前 new_listing_free_days 个 K 线不报(新股不设涨跌停)。
    相邻两根 K 线之间若缺了交易日,收益跨多日,仍按单日阈值判断(由缺口检查另行报告)。
    """
    d, err = _prep(df)
    if d is None:
        return [Issue(code, None, "bad_input", "error", err)]
    if len(d) < 2 or "Close" not in d.columns:
        return []
    board = board or board_of(code)
    ex = {x for x in (parse_date(e) for e in (ex_days or [])) if x is not None}
    ipo_d = parse_date(ipo)
    d = d[~d.index.duplicated(keep="first")].sort_index()
    closes = [_num(v) for v in d["Close"]]
    days = [ts.date() for ts in d.index]
    # 上市后第几根 K 线:从第一根 >= ipo 的 K 线开始数,第 1 根是 1
    start_pos = None
    if ipo_d is not None:
        for i, dy in enumerate(days):
            if dy >= ipo_d:
                start_pos = i
                break
    out: list[Issue] = []
    for i in range(1, len(days)):
        p, c = closes[i - 1], closes[i]
        if not (p > 0 and c > 0) or math.isinf(p) or math.isinf(c):
            continue  # 坏价格由 check_ohlc 报
        if days[i] in ex:
            continue
        if start_pos is not None and start_pos <= i < start_pos + th.new_listing_free_days:
            continue
        ret = c / p - 1
        lim = _limit_for(board, days[i], _in_spans(days[i], st_spans), th)
        if abs(ret) > lim + th.slack:
            out.append(Issue(code, days[i], "price_jump", "warn",
                             f"单日涨跌 {ret:+.2%} 超过阈值 {lim + th.slack:.2%}"
                             f"(板块={board},涨跌停={lim:.0%})", ret))
    return _cap(out, code, th)


# ---------------------------------------------------------------- 4. 零成交量

def check_zero_volume(df, code: str = "", th: Thresholds = Thresholds()) -> list[Issue]:
    """成交量为 0 但价格有变动(日内有振幅,或收盘相对前收变动超过 zero_vol_move)-> warn。"""
    d, err = _prep(df)
    if d is None:
        return [Issue(code, None, "bad_input", "error", err)]
    if len(d) == 0 or "Volume" not in d.columns or any(c not in d.columns for c in OHLC):
        return []
    out: list[Issue] = []
    prev_close = math.nan
    for ts, row in zip(d.index, d[OHLC + ["Volume"]].itertuples(index=False)):
        o, h, lo, c, v = (_num(x) for x in row)
        if v == 0:
            moved = []
            if all(x > 0 for x in (h, lo)) and h / lo - 1 > th.zero_vol_move:
                moved.append(f"日内振幅 {h / lo - 1:.2%}")
            if prev_close > 0 and c > 0 and abs(c / prev_close - 1) > th.zero_vol_move:
                moved.append(f"收盘较前收 {c / prev_close - 1:+.2%}")
            if o > 0 and c > 0 and abs(c / o - 1) > th.zero_vol_move:
                moved.append(f"开收不等 {c / o - 1:+.2%}")
            if moved:
                out.append(Issue(code, ts.date(), "zero_volume_price_move", "warn",
                                 "成交量为 0 但" + ";".join(moved), None))
        if not math.isnan(c):
            prev_close = c
    return _cap(out, code, th)


# ---------------------------------------------------------------- 1. 复权

def _series(x, name):
    """把输入转成按日期索引的 float Series;失败返回 None。"""
    if x is None:
        return None
    try:
        s = pd.Series(x) if not isinstance(x, pd.Series) else x.copy()
        s.index = pd.DatetimeIndex(pd.to_datetime(s.index)).normalize()
        s = pd.to_numeric(s, errors="coerce")
        s = s[~s.index.duplicated(keep="first")].sort_index()
        return s
    except Exception:  # noqa: BLE001
        return None


def infer_ex_days(hfq_close, raw_close, th: Thresholds = Thresholds()) -> list[date]:
    """由原始收盘价推除权日:隐含后复权因子 F=hfq/raw 在相邻两日发生变化的那一天。

    只在两个序列都有值的日期上比较;F 的绝对水平无意义(合并来源会乘常数),只看相邻比值。
    """
    h, r = _series(hfq_close, "hfq"), _series(raw_close, "raw")
    if h is None or r is None:
        return []
    f = (h / r).replace([math.inf, -math.inf], math.nan).dropna()
    f = f[f > 0]
    ratio = f / f.shift(1)
    return [ts.date() for ts, x in ratio.items()
            if not math.isnan(x) and abs(x - 1) > th.ex_factor_change]


def check_adjustment(hfq_close, code: str = "", raw_close=None, factors=None, qfq_close=None,
                     th: Thresholds = Thresholds()) -> list[Issue]:
    """复权一致性。所有输入都是按日期索引的 Series;提供哪些就检查哪些:

    raw_close:  未复权收盘价。隐含因子 F=hfq/raw 应单调不减(后复权只会因分红送股抬升);
                相邻比值 < min_factor_ratio -> error "factor_decrease"。F 的变化日即除权日(见 infer_ex_days)。
    factors:    外部后复权因子序列。要求 >0 且单调不减;若同时给了 raw_close,还要求 F 与 factors
                的比值保持常数(差一个常数是合并来源造成的,可接受)-> 否则 warn "factor_mismatch"。
    qfq_close:  前复权收盘价。后复权=原始*因子*c1,前复权=原始*因子/最新因子,两者之比应为常数;
                偏离中位数超过 conv_tol -> error "adj_conversion_mismatch"。
    都没提供时无从检验,返回一条 info。
    """
    out: list[Issue] = []
    h = _series(hfq_close, "hfq")
    if h is None:
        return [Issue(code, None, "bad_input", "error", "后复权收盘价序列无法解析")]
    if raw_close is None and factors is None and qfq_close is None:
        return [Issue(code, None, "adjustment_unverifiable", "info",
                      "未提供原始价/因子/前复权价,无法检验复权一致性(仅靠 check_jumps)")]

    if raw_close is not None:
        r = _series(raw_close, "raw")
        if r is None:
            out.append(Issue(code, None, "bad_input", "error", "原始收盘价序列无法解析"))
        else:
            f = (h / r).replace([math.inf, -math.inf], math.nan)
            f = f[f > 0].dropna()
            ratio = f / f.shift(1)
            for ts, x in ratio.items():
                if not math.isnan(x) and x < th.min_factor_ratio:
                    out.append(Issue(code, ts.date(), "factor_decrease", "error",
                                     f"隐含后复权因子回退 {x - 1:+.4%}(后复权因子应单调不减)", x - 1))

    if factors is not None:
        fa = _series(factors, "factors")
        if fa is None:
            out.append(Issue(code, None, "bad_input", "error", "因子序列无法解析"))
        else:
            bad = fa[~(fa > 0)]
            for ts, x in bad.items():
                out.append(Issue(code, ts.date(), "factor_invalid", "error", f"因子非正或为空:{x}"))
            fa = fa[fa > 0]
            ratio = fa / fa.shift(1)
            for ts, x in ratio.items():
                if not math.isnan(x) and x < th.min_factor_ratio:
                    out.append(Issue(code, ts.date(), "factor_series_decrease", "error",
                                     f"外部因子序列回退 {x - 1:+.4%}", x - 1))
            if raw_close is not None and r is not None:
                imp = (h / r).replace([math.inf, -math.inf], math.nan)
                both = pd.concat([imp, fa], axis=1, join="inner").dropna()
                both = both[(both.iloc[:, 0] > 0)]
                if len(both):
                    q = both.iloc[:, 0] / both.iloc[:, 1]
                    base = float(q.median())
                    for ts, x in q.items():
                        dev = x / base - 1
                        if abs(dev) > th.conv_tol:
                            out.append(Issue(code, ts.date(), "factor_mismatch", "warn",
                                             f"隐含因子与给定因子之比偏离常数 {dev:+.4%}", dev))

    if qfq_close is not None:
        q = _series(qfq_close, "qfq")
        if q is None:
            out.append(Issue(code, None, "bad_input", "error", "前复权收盘价序列无法解析"))
        else:
            both = pd.concat([h, q], axis=1, join="inner").dropna()
            both = both[(both.iloc[:, 0] > 0) & (both.iloc[:, 1] > 0)]
            if len(both):
                k = both.iloc[:, 0] / both.iloc[:, 1]
                base = float(k.median())
                for ts, x in k.items():
                    dev = x / base - 1
                    if abs(dev) > th.conv_tol:
                        out.append(Issue(code, ts.date(), "adj_conversion_mismatch", "error",
                                         f"后复权/前复权之比偏离常数 {dev:+.4%}", dev))
    return _cap(out, code, th)


# ---------------------------------------------------------------- 汇总

def check_series(code: str, df, *, calendar=None, suspended=None, calendar_trusted: bool = True,
                 board: str | None = None, ex_days=None, st_spans=None, ipo=None,
                 raw_close=None, factors=None, qfq_close=None,
                 th: Thresholds = Thresholds()) -> list[Issue]:
    """汇总全部检查,按严重级别(error>warn>info)再按日期排序。任何一项内部出错只记一条 error。

    给了 raw_close 时,先由它推出除权日并并入 ex_days 再查跳变。
    """
    out: list[Issue] = []

    def run(name, fn):
        try:
            out.extend(fn())
        except Exception as e:  # noqa: BLE001
            out.append(Issue(code, None, "check_failed", "error", f"{name} 内部出错:{e!r}"))

    ex = list(ex_days or [])
    d, err = _prep(df)
    if d is not None and raw_close is not None and "Close" in d.columns:
        try:
            ex += infer_ex_days(d["Close"], raw_close, th)
        except Exception:  # noqa: BLE001
            pass

    run("check_ohlc", lambda: check_ohlc(df, code, th))
    if calendar is not None:
        run("check_calendar_gaps", lambda: check_calendar_gaps(
            df, calendar, code, suspended, calendar_trusted, th))
    run("check_jumps", lambda: check_jumps(df, code, board, ex, st_spans, ipo, th))
    run("check_zero_volume", lambda: check_zero_volume(df, code, th))
    if d is not None and "Close" in d.columns and not (
            raw_close is None and factors is None and qfq_close is None):
        run("check_adjustment", lambda: check_adjustment(
            d["Close"], code, raw_close, factors, qfq_close, th))
    return _sort(out)


def summarize_issues(issues: list[Issue]) -> dict:
    """按 kind 与 severity 计数,便于日报使用。"""
    by_kind: dict[str, int] = {}
    by_sev: dict[str, int] = {}
    for i in issues:
        by_kind[i.kind] = by_kind.get(i.kind, 0) + 1
        by_sev[i.severity] = by_sev.get(i.severity, 0) + 1
    return {"total": len(issues), "by_severity": by_sev, "by_kind": by_kind}
