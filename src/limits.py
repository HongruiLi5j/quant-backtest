"""涨跌停幅度的唯一来源（D17）。以前 datasource / simtrade / signal_ledger / intraday / consistency 各写一份，口径互相矛盾
（例如 ST 一律 5%、创业板 ST 也按 5%、北交所 30% 有的地方漏了）。

规则写成一张按日期生效的表 LIMIT_RULES：(规则名, 起始日, 涨跌幅)。同一规则名取「起始日 <= 当天」中最晚的一行；
当天早于最早一行时沿用最早一行（那时板块还不存在，不替它编幅度）。date=None 表示「现在」（取最新一行）。

| 规则名   | 适用                         | 幅度                                                    |
|----------|------------------------------|---------------------------------------------------------|
| 主板     | 沪深主板（含 B 股）非 ST     | 10%                                                     |
| ST       | 沪深主板 ST / *ST            | 5%；2026-07-06 起 10%                                   |
| 创业板   | 30 开头，非 ST               | 10%；2020-08-24 起 20%                                  |
| 创业板ST | 30 开头，ST                  | 5%（改革前按主板规则）；2020-08-24 起 20%               |
| 科创板   | 688/689（ST 同）             | 20%                                                     |
| 北交所   | 8 / 4 / 92 开头（ST 同）     | 30%                                                     |

来源：主板 ST 5%→10%：沪深交易所 2026-04-24 发布的修订版交易规则，2026-07-06 起施行（北京商报 2026-07-06、
新浪财经 2026-07-04/05 报道，三家一致；交易所公告原文未抓取）。创业板 2020-08-24 注册制改革存量股起 20%（含 ST）。

新股不设涨跌停（limit_pct 返回 None，调用方应当「不标涨停/跌停」）：
- 科创板（2019-07-22 起）、创业板（2020-08-24 起）、主板注册制（2023-04-10 起）：上市前 5 个交易日；
- 北交所：上市首日；
- 2023-04-10 前的主板新股首日是 +44% / −36% 的不对称限制，不是对称幅度，这里同样返回 None。
只有调用方给了 listed_days（上市第几个交易日，首日 = 1）才判断；不知道就按正常幅度。

纯标准库：intraday 等模块不导入 pandas 也能用。
"""
from __future__ import annotations

import csv
from bisect import bisect_right
from datetime import date as _date, datetime as _datetime
from functools import lru_cache
from pathlib import Path

LIMIT_RULES = (
    ("主板", _date(1996, 12, 16), 0.10),
    ("ST", _date(1998, 4, 1), 0.05),            # 仅沪深主板 ST
    ("ST", _date(2026, 7, 6), 0.10),            # 主板 ST 5%→10%，2026-07-06 起施行
    ("创业板", _date(2009, 10, 30), 0.10),
    ("创业板", _date(2020, 8, 24), 0.20),
    ("创业板ST", _date(2009, 10, 30), 0.05),    # 改革前创业板按主板规则：ST 5%
    ("创业板ST", _date(2020, 8, 24), 0.20),
    ("科创板", _date(2019, 7, 22), 0.20),
    ("北交所", _date(2021, 11, 15), 0.30),
)
CHINEXT_20_FROM = _date(2020, 8, 24)
ST_MAIN_10PCT_FROM = _date(2026, 7, 6)
MAIN_REG_FROM = _date(2023, 4, 10)          # 主板注册制首批上市
NO_LIMIT_DAYS = 5                            # 注册制新股前 5 个交易日不设涨跌停

ROOT = Path(__file__).resolve().parent.parent
LIST_CSV = ROOT / "data" / "daily" / "cn_all_list.csv"
CAL_CSV = ROOT / "data" / "daily" / "cn_trade_calendar.csv"


def board_of_code(code) -> str:
    """按代码前缀：北交所 8/4/92，科创板 688/689，创业板 30，其余（60/00/B 股 200/900 等）按沪深主板。"""
    c = str(code)
    if c.startswith(("8", "4", "92")):
        return "北交所"
    if c.startswith(("688", "689")):
        return "科创板"
    if c.startswith("30"):
        return "创业板"
    return "主板"


def is_st(name) -> bool:
    """名称含 ST（含 *ST、小写 st）。退市整理期的「xx退」不含 ST，按板块普通幅度。"""
    return "ST" in str(name or "").upper()


def rule_name(code, name="") -> str:
    board = board_of_code(code)
    if is_st(name):
        if board == "主板":
            return "ST"
        if board == "创业板":
            return "创业板ST"
    return board


def _as_date(d):
    if d is None:
        return None
    if isinstance(d, _datetime):
        return d.date()
    if isinstance(d, _date):
        return d
    if hasattr(d, "date") and callable(d.date):     # pandas.Timestamp
        return d.date()
    return _date.fromisoformat(str(d)[:10])


def limit_for(rule: str, day, rules=LIMIT_RULES, default: float = 0.10) -> float:
    """某规则名某天的涨跌幅。day=None 取最新；规则名不存在返回 default。"""
    rows = sorted((s, p) for b, s, p in rules if b == rule)
    if not rows:
        return default
    d = _as_date(day)
    if d is None:
        return rows[-1][1]
    cand = [p for s, p in rows if s <= d]
    return cand[-1] if cand else rows[0][1]


def no_limit(code, day, listed_days) -> bool:
    """新股上市初期不设涨跌停？listed_days = 上市第几个交易日（首日 = 1）。"""
    if listed_days is None or listed_days < 1:
        return False
    d = _as_date(day) or _date.today()
    board = board_of_code(code)
    if board == "北交所":
        return listed_days == 1
    if board == "科创板":
        return listed_days <= NO_LIMIT_DAYS
    if board == "创业板":
        return listed_days <= (NO_LIMIT_DAYS if d >= CHINEXT_20_FROM else 1)
    return listed_days <= (NO_LIMIT_DAYS if d >= MAIN_REG_FROM else 1)       # 主板：注册制前只有首日（±44%/36% 不对称）


def limit_pct(code, name="", date=None, listed_days=None):
    """涨跌停幅度（小数，0.10 = 10%）；新股不设涨跌停的日子返回 None。"""
    if no_limit(code, date, listed_days):
        return None
    return limit_for(rule_name(code, name), date)


def limit_series(code, name, index):
    """按日期给出该股每天的涨跌停幅度（回测逐日判断一字板时用；不处理新股）。"""
    import pandas as pd
    return pd.Series([limit_pct(code, name, d) for d in index], index=index, dtype=float)


# ---------------------------------------------------------------- 上市天数（实时盯盘用，读本地清单 + 交易日历，只读）
@lru_cache(maxsize=1)
def _ipo_map(path: str = str(LIST_CSV)) -> dict:
    out = {}
    try:
        with open(path, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r.get("code") and r.get("ipo"):
                    out[r["code"].zfill(6)] = r["ipo"][:10]
    except OSError:
        pass
    return out


@lru_cache(maxsize=1)
def _calendar(path: str = str(CAL_CSV)) -> tuple:
    try:
        with open(path, encoding="utf-8") as f:
            return tuple(sorted(r["trade_date"][:10] for r in csv.DictReader(f) if r.get("trade_date")))
    except OSError:
        return ()


def listed_days(code, day=None, ipo_map=None, cal=None):
    """上市第几个交易日（上市首日 = 1）。清单里没有上市日期或日历缺失时返回 None（调用方按正常幅度处理）。"""
    ipo = (ipo_map if ipo_map is not None else _ipo_map()).get(str(code).zfill(6))
    cal = cal if cal is not None else _calendar()
    if not ipo or not cal:
        return None
    d = (_as_date(day) or _date.today()).isoformat()
    if d < ipo:
        return None
    return bisect_right(cal, d) - bisect_right(cal, ipo) + 1 if ipo in cal else bisect_right(cal, d) - bisect_right(cal, ipo)


def live_limit_pct(code, name="", day=None):
    """实时盯盘用：带上市天数判断。新股前几日返回 None。"""
    return limit_pct(code, name, day, listed_days(code, day))
