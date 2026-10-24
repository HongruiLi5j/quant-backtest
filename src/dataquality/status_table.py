"""时点状态表:某一天、某只股票"当时"是否上市/退市/ST/停牌/可交易。

原则:缺信息写 None,不伪造。None 表示"不知道",不等于 False。
本模块只做纯计算:不联网、不 import allmarket/datasource 等有副作用的模块,
不主动读 data/;写文件只在调用者显式给路径时发生,且默认拒绝写入仓库 data/ 目录。
"""
from __future__ import annotations

import os
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import pandas as pd

# 仓库根目录下的 data/:默认禁止把状态表写进去,避免污染真实数据
_DATA_DIR = Path(__file__).resolve().parents[2] / "data"


# ---------------------------------------------------------------- 数据类

@dataclass(frozen=True)
class StockInfo:
    """清单里的一只股票;除 code 外都允许缺失。"""
    code: str
    ipo: date | None = None       # 上市日;空串/非法 -> None
    delist: date | None = None    # 终止上市日;未知 -> None
    board: str | None = None      # 缺省按代码前缀推断
    delist_invalid: bool = False  # 退市日有值但解析失败:不能当成"不在退市名单里"


@dataclass(frozen=True)
class StatusRow:
    code: str
    day: date
    listed: bool | None           # day >= ipo;ipo 未知 -> None
    listed_days: int | None       # 上市以来的交易日数(上市当天算第 1 天);未知 -> None
    delisted: bool | None         # day >= delist;没有退市信息 -> None(不是 False)
    is_st: bool | None            # 提供了 ST 区间才有 True/False,否则 None
    suspended: bool | None        # 当天是否停牌
    susp_source: str              # "recorded"(停牌名单)/"inferred"(由 K 线有无推断)/"unknown"
    tradable: bool | None         # 已上市、未退市、未停牌;任一未知且其余为真 -> None
    reasons: tuple[str, ...] = ()  # 不可交易/不确定的原因,供审计


# 表的列顺序(长表)
COLUMNS = ["code", "day", "listed", "listed_days", "delisted", "is_st",
           "suspended", "susp_source", "tradable", "reasons"]
_BOOL_COLS = ["listed", "delisted", "is_st", "suspended", "tradable"]


# ---------------------------------------------------------------- 日期与输入整理

def parse_date(v) -> date | None:
    """把各种写法转成 date;空值/非法一律返回 None(不抛、不猜)。"""
    if v is None or v is pd.NaT:  # NaT 也是 datetime 的实例,必须先排除
        return None
    if isinstance(v, datetime):  # 含 pd.Timestamp
        return v.date()
    if isinstance(v, date):
        return v
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    s = str(v).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(s[:10] if fmt != "%Y%m%d" else s[:8], fmt).date()
        except ValueError:
            continue
    return None


def board_of(code: str) -> str | None:
    """按代码前缀粗略推断板块;不认识的返回 None。"""
    c = str(code)
    if c.startswith("688"):
        return "科创板"
    if c.startswith(("300", "301")):
        return "创业板"
    if c.startswith(("60", "000", "001", "002", "003")):
        return "主板"
    return None


def infos_from_frame(df: pd.DataFrame) -> list[StockInfo]:
    """从清单 DataFrame 构造 StockInfo。需要 code 列;ipo/delist/board 列可缺。"""
    out = []
    for r in df.to_dict("records"):
        code = str(r["code"]).strip()
        if not (len(code) == 6 and code.isdigit()):
            # 读清单没加 dtype=str 时 000001 会变成 1,之后和 K 线代码对不上,会被误判停牌
            raise ValueError(f"股票代码必须是 6 位数字字符串,读清单时请加 dtype=str:{code!r}")
        board = r.get("board")
        if board is None or (isinstance(board, float) and pd.isna(board)) or board == "":
            board = board_of(code)
        raw_delist = r.get("delist")
        delist = parse_date(raw_delist)
        # 有值却解析不出来(如 2024/13/01):保留这个事实,不退化成"没有退市日"
        bad_delist = delist is None and not _is_blank(raw_delist)
        out.append(StockInfo(code, parse_date(r.get("ipo")), delist, board, bad_delist))
    return out


def _is_blank(v) -> bool:
    """None / NaN / NaT / 纯空白字符串视为"没给"。"""
    if v is None or v is pd.NaT:
        return True
    try:
        if pd.isna(v):
            return True
    except (TypeError, ValueError):
        pass
    return isinstance(v, str) and not v.strip()


def _calendar(calendar) -> list[date]:
    """日历规整为去重后升序的 date 列表。"""
    return sorted({d for d in (parse_date(x) for x in calendar) if d is not None})


def _norm_map(m) -> dict[date, set[str]] | None:
    """{日期: 代码集合} 规整;None 原样返回(表示"没有这份信息")。"""
    if m is None:
        return None
    out = {}
    for k, v in m.items():
        d = parse_date(k)
        if d is not None:
            out[d] = {str(c) for c in v}
    return out


def _norm_spans(spans) -> dict[str, list[tuple[date, date | None]]] | None:
    """{代码: [(起, 止或None), ...]};止为 None 表示至今仍是 ST。起止当天都算在区间内。

    起点非法,或"给了结束日但解析失败"的区间无法使用:记成 (None, None) 作为"坏区间"标记,
    不丢弃也不当成"至今仍是 ST",由 _status_one 把该股 is_st 降为 None。
    """
    if spans is None:
        return None
    out = {}
    for code, lst in spans.items():
        norm = []
        for s, e in lst:
            sd = parse_date(s)
            ed = parse_date(e)
            if sd is None or (ed is None and not _is_blank(e)):
                norm.append((None, None))
            else:
                norm.append((sd, ed))
        out[str(code)] = norm
    return out


def _and3(vals: list[bool | None]) -> bool | None:
    """三值逻辑的"与":有 False -> False;无 False 但有 None -> None;全 True -> True。"""
    if any(v is False for v in vals):
        return False
    if any(v is None for v in vals):
        return None
    return True


# ---------------------------------------------------------------- 核心

def _status_one(day, info, cal, susp, st, bars, min_listed_days,
                calendar_trusted, delist_info_complete) -> StatusRow:
    reasons: list[str] = []

    # 上市
    if info.ipo is None:
        listed, listed_days = None, None
        reasons.append("ipo_unknown")
    elif day < info.ipo:
        listed, listed_days = False, 0
        reasons.append("not_listed")
    else:
        listed = True
        if not calendar_trusted:
            # 日历只是工作日、含节假日:数出来的天数偏大,不当事实
            listed_days = None
            reasons.append("listed_days_unknown_calendar_untrusted")
        elif cal and info.ipo < cal[0]:
            # 日历起点晚于上市日:数出来的天数是低估值,不当事实
            listed_days = None
            reasons.append("listed_days_unknown_calendar_short")
        else:
            listed_days = bisect_right(cal, day) - bisect_left(cal, info.ipo)
            if listed_days < min_listed_days:
                reasons.append("listed_lt_min_days")  # 仅提示,不影响 tradable

    # 退市
    if info.delist_invalid:
        delisted = None  # 原始数据有退市日、只是写错了:不知道,不是 False
        reasons.append("delist_invalid")
    elif info.delist is not None:
        delisted = day >= info.delist
        if delisted:
            reasons.append("delisted")
    elif delist_info_complete:
        delisted = False  # 调用者声明退市名单完整:不在名单里 = 未退市
    else:
        delisted = None
        reasons.append("delist_unknown")

    # ST
    if st is None:
        is_st = None
    else:
        spans = st.get(info.code, ())
        if any(s is not None and s <= day and (e is None or day <= e) for s, e in spans):
            is_st = True  # 有一段完好的区间覆盖当天,坏区间不影响这个事实
        elif any(s is None for s, _ in spans):
            is_st = None  # 有坏区间,可能漏掉了真正的 ST 段
            reasons.append("st_span_invalid")
        else:
            is_st = False

    # 停牌:记录在案 > 由 K 线推断 > 未知
    suspended, source = None, "unknown"
    if susp is not None and day in susp:
        suspended, source = info.code in susp[day], "recorded"
    elif bars is not None and day in bars and calendar_trusted:
        if info.code in bars[day]:
            suspended, source = False, "inferred"
        elif listed is True and delisted is False:
            suspended, source = True, "inferred"  # 缺 K 线也可能是数据缺口,只是推断
    if suspended is True:
        reasons.append("suspended")
    elif suspended is None:
        reasons.append("suspension_unknown")
    if source == "inferred":
        reasons.append("suspension_inferred")

    not_delisted = None if delisted is None else not delisted
    not_suspended = None if suspended is None else not suspended
    tradable = _and3([listed, not_delisted, not_suspended])
    return StatusRow(info.code, day, listed, listed_days, delisted, is_st,
                     suspended, source, tradable, tuple(reasons))


def build_status(day, infos, calendar, *, suspended=None, st_spans=None,
                 bars_present=None, min_listed_days=250, calendar_trusted=True,
                 delist_info_complete=False) -> dict[str, StatusRow]:
    """某一天的状态:{code: StatusRow}。非交易日返回空字典。

    suspended:     {日期: 当天停牌代码集合};有该日条目即视为"记录在案"
    st_spans:      {代码: [(起, 止或None)]};给了就视为完整(不在里面 = 不是 ST)
    bars_present:  {日期: 当天有 K 线的代码集合},用于推断停牌
    calendar_trusted=False(日历只是工作日、不含节假日)时,推断一律降级为 unknown
    delist_info_complete=True:调用者声明退市名单完整,没有 delist 的股票视为未退市
    min_listed_days: 只用于在 reasons 里提示,不并入 tradable
    """
    d = parse_date(day)
    cal = _calendar(calendar)
    if d is None or d not in set(cal):
        return {}
    susp, st, bars = _norm_map(suspended), _norm_spans(st_spans), _norm_map(bars_present)
    return {i.code: _status_one(d, i, cal, susp, st, bars, min_listed_days,
                                calendar_trusted, delist_info_complete)
            for i in infos}


def build_range(start, end, infos, calendar, *, suspended=None, st_spans=None,
                bars_present=None, min_listed_days=250, calendar_trusted=True,
                delist_info_complete=False) -> pd.DataFrame:
    """[start, end] 内每个交易日的状态长表(列见 COLUMNS)。布尔列用可空类型保留 None。

    参数含义同 build_status;写错参数名会直接 TypeError,不会被静默忽略。
    """
    s, e = parse_date(start), parse_date(end)
    cal = _calendar(calendar)
    infos = list(infos)
    susp, st, bars = _norm_map(suspended), _norm_spans(st_spans), _norm_map(bars_present)
    rows = []
    if s is not None and e is not None:
        for d in cal[bisect_left(cal, s):bisect_right(cal, e)]:
            for i in infos:
                r = _status_one(d, i, cal, susp, st, bars, min_listed_days,
                                calendar_trusted, delist_info_complete)
                rows.append((r.code, r.day, r.listed, r.listed_days, r.delisted, r.is_st,
                             r.suspended, r.susp_source, r.tradable, "|".join(r.reasons)))
    df = pd.DataFrame(rows, columns=COLUMNS)
    for c in _BOOL_COLS:
        df[c] = df[c].astype("boolean")
    df["listed_days"] = df["listed_days"].astype("Int64")
    return df


# ---------------------------------------------------------------- 摘要与落盘

def summarize(table: pd.DataFrame) -> dict:
    """各字段的缺失率(None 比例)、reason 计数、tradable/停牌来源分布。"""
    n = len(table)
    fields = _BOOL_COLS + ["listed_days"]
    missing = {c: (float(table[c].isna().sum()) / n if n else 0.0) for c in fields}
    reasons: dict[str, int] = {}
    for s in table["reasons"].fillna(""):
        for r in filter(None, str(s).split("|")):
            reasons[r] = reasons.get(r, 0) + 1
    return {
        "rows": n,
        "codes": int(table["code"].nunique()) if n else 0,
        "missing_rate": missing,
        "reason_counts": dict(sorted(reasons.items())),
        "tradable_counts": {"true": int((table["tradable"] == True).sum()),  # noqa: E712
                            "false": int((table["tradable"] == False).sum()),  # noqa: E712
                            "none": int(table["tradable"].isna().sum())},
        "susp_source_counts": {k: int(v) for k, v in table["susp_source"].value_counts().items()},
    }


def _inside_data_dir(p: Path) -> bool:
    """p 是否在仓库 data/ 里。macOS(APFS)默认不分大小写,DATA/ 和 data/ 是同一个目录,
    所以:①路径各段按大小写不敏感比前缀;②沿父目录向上用 samefile 比 inode。"""
    dp = [x.casefold() for x in _DATA_DIR.parts]
    if [x.casefold() for x in p.parts[:len(dp)]] == dp:
        return True
    if _DATA_DIR.exists():
        for q in (p, *p.parents):
            try:
                if q.exists() and os.path.samefile(q, _DATA_DIR):
                    return True
            except OSError:
                continue
    return False


def write_table(table: pd.DataFrame, path, *, allow_data_dir=False, overwrite=False) -> Path:
    """写到调用者给的路径(.parquet 或 .csv)。

    默认拒绝:①仓库 data/ 目录内的路径;②已存在的文件(overwrite=False)。
    """
    p = Path(path).expanduser().resolve()
    if not allow_data_dir and _inside_data_dir(p):
        raise ValueError(f"默认不允许写入 data/ 目录:{p}")
    if p.suffix not in (".parquet", ".csv"):
        raise ValueError("只支持 .parquet 或 .csv")
    if p.exists() and not overwrite:
        raise FileExistsError(f"文件已存在,未覆盖:{p}")
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")  # 先写临时文件再原子替换
    if p.suffix == ".parquet":
        table.to_parquet(tmp, index=False)
    else:
        table.to_csv(tmp, index=False)
    os.replace(tmp, p)
    return p
