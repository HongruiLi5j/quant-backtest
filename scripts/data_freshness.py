#!/usr/bin/env python3
"""数据新鲜度自检(只读):每只股票最后一根 K 线的日期分布、相对交易日历缺了多少个交易日、
连续缺失超过 N 个交易日的股票(多半是停牌或退市)。

和 src/dataquality 的关系(复用而不是重造):
  - 读文件、读停牌名单、识别数据目录、15:30 规则、原子写文件都直接用 src/dataquality.report 里的现成函数;
  - dataquality 的 check_calendar_gaps 只查「序列首尾之间」的中间缺口,不看末尾;
    scan_dir 只给「最后日期 -> 只数」的分布和 stale 总数,没有逐只清单。
    本脚本补的正是这两块:末尾缺了几个交易日(停牌/退市的主要信号)和逐只的问题清单。
  - 日历口径不同:dataquality 读 cn_trade_calendar.csv;本脚本按任务要求用「所有股票出现过的日期并集」近似,
    再拿官方日历文件做交叉核对(见报告里的「日历近似的核对」)。

约束:对数据目录只读;不联网;输出目录在数据目录之内则拒绝写入;
北京时间 15:30 前当天的 K 线是盘中半天数据,一律忽略。
示例:.venv/bin/python scripts/data_freshness.py --data-dir data/data/daily/cn_all --out-dir <输出目录>
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import market_clock  # noqa: E402
from src.dataquality.report import (  # noqa: E402
    BEIJING, _atomic_write, _inside, _read_calendar,
    _read_suspended, _resolve_dir, beijing_now)
from src.dataquality.status_table import parse_date  # noqa: E402

REPORT_MD = "data_freshness.md"
REPORT_JSON = "data_freshness.json"
REPORT_CSV = "data_freshness_problems.csv"

# 问题类别(按严重程度从高到低);同一只股票只归入一类
CAT_TAIL_OUT = "tail_not_in_suspend_list"   # 末尾缺 > N 日,且最新交易日不在停牌名单:疑似退市/数据源断更
CAT_TAIL_SUSP = "tail_in_suspend_list"      # 末尾缺 > N 日,最新交易日在停牌名单:长期停牌
CAT_MID = "mid_gap"                         # 回看窗口内中间出现连续缺失 > N 日(末尾正常):近期停牌或补数据留下的洞
CAT_HIST = "hist_gap"                       # 缺口只出现在回看窗口之前:历史上的长停牌(如 2015 年集体停牌),不算问题
CAT_LAG = "lag_short"                       # 末尾缺 1~N 日:短期滞后(未问题,仅统计)
CAT_OK = "ok"
PROBLEM_CATS = (CAT_TAIL_OUT, CAT_TAIL_SUSP, CAT_MID)
CAT_ZH = {
    CAT_TAIL_OUT: "末尾断更(不在停牌名单)",
    CAT_TAIL_SUSP: "末尾断更(在停牌名单)",
    CAT_MID: "近期中间缺口",
    CAT_HIST: "仅历史缺口(不算问题)",
    CAT_LAG: "短期滞后",
    CAT_OK: "正常",
}


# ---------------------------------------------------------------- 读取

def _read_index(path: Path) -> np.ndarray:
    """只读 parquet 的日期索引(不读价格列,快),返回去重排序后的 datetime64[D] 数组。

    索引不是日期时回退到 date/Date/日期 列,和 dataquality 的 _load_frame 口径一致。
    """
    df = pd.read_parquet(path, columns=[])
    idx = df.index
    if not isinstance(idx, pd.DatetimeIndex):
        full = pd.read_parquet(path)
        for c in ("date", "Date", "日期"):
            if c in full.columns:
                idx = pd.DatetimeIndex(pd.to_datetime(full[c]))
                break
        else:
            idx = pd.DatetimeIndex(pd.to_datetime(full.index))
    return np.unique(idx.values.astype("datetime64[D]"))


def _read_names(path: Path) -> dict:
    """code -> 名称;读不到返回空字典。"""
    try:
        if not path.is_file():
            return {}
        df = pd.read_csv(path, dtype=str)
        if "code" not in df.columns:
            return {}
        names = df["name"] if "name" in df.columns else pd.Series([""] * len(df))
        return {str(c).strip().zfill(6): ("" if pd.isna(n) else str(n)) for c, n in zip(df["code"], names)}
    except Exception:  # noqa: BLE001
        return {}


# ---------------------------------------------------------------- 纯函数:单只股票的新鲜度

def union_calendar(day_arrays) -> np.ndarray:
    """所有股票出现过的日期的并集(升序,datetime64[D])。"""
    arrs = [a for a in day_arrays if len(a)]
    if not arrs:
        return np.array([], dtype="datetime64[D]")
    return np.unique(np.concatenate(arrs))


def _longest_gap(gaps: np.ndarray, pos: np.ndarray, cal: np.ndarray):
    """gaps 里最长的一段缺口 -> (长度, 起始日字符串);没有缺口返回 (0, None)。"""
    if len(gaps) == 0 or gaps.max() <= 0:
        return 0, None
    k = int(gaps.argmax())
    return int(gaps[k]), str(cal[pos[k] + 1])


def freshness_of(days: np.ndarray, cal: np.ndarray, lookback: int = 0) -> dict | None:
    """一只股票相对日历 cal 的新鲜度。days、cal 都是升序去重的 datetime64[D];days 必须是 cal 的子集。

    返回:
      first/last:     第一根/最后一根 K 线日期;
      n_bars:         K 线根数;
      tail_missing:   最后一根之后、日历上还有几个交易日(最新交易日 = cal[-1]);0 表示追平;
      mid_missing:    首尾之间缺的交易日总数;
      mid_longest:    首尾之间最长的一段连续缺失(按日历相邻,全历史);mid_longest_start 是那一段的起始日;
      mid_recent_longest/_start: 只看「缺口结束(K 线恢复)落在日历最后 lookback 个交易日内」的缺口里最长的一段;
                      lookback=0 表示不限窗口,此时等于全历史;
      longest:        max(tail_missing, mid_recent_longest) —— 排序和阈值判断用。
    空序列返回 None。
    """
    if len(days) == 0 or len(cal) == 0:
        return None
    pos = np.searchsorted(cal, days)
    gaps = np.diff(pos) - 1              # 相邻两根 K 线之间缺的日历交易日数
    mid_missing = int(gaps.sum()) if len(gaps) else 0
    mid_longest, mid_start = _longest_gap(gaps, pos, cal)
    if lookback > 0 and len(gaps):
        # pos[1:] 是每个缺口之后恢复的那根 K 线在日历上的位置;恢复得足够新才算「近期」
        recent = pos[1:] >= len(cal) - lookback
        rec_longest, rec_start = _longest_gap(np.where(recent, gaps, 0), pos, cal)
    else:
        rec_longest, rec_start = mid_longest, mid_start
    tail = int(len(cal) - 1 - pos[-1])
    return {
        "first": str(days[0]), "last": str(days[-1]), "n_bars": int(len(days)),
        "tail_missing": tail, "mid_missing": mid_missing,
        "mid_longest": mid_longest, "mid_longest_start": mid_start,
        "mid_recent_longest": rec_longest, "mid_recent_start": rec_start,
        "longest": max(tail, rec_longest),
    }


def classify(f: dict, in_suspend_latest: bool, max_gap: int) -> str:
    """分类。「超过 max_gap」是严格大于,和 dataquality 的 max_gap_days 口径一致。"""
    if f["tail_missing"] > max_gap:
        return CAT_TAIL_SUSP if in_suspend_latest else CAT_TAIL_OUT
    if f["mid_recent_longest"] > max_gap:
        return CAT_MID
    if f["mid_longest"] > max_gap:
        return CAT_HIST
    if f["tail_missing"] > 0:
        return CAT_LAG
    return CAT_OK


# ---------------------------------------------------------------- 扫描

def scan(data_dir, *, max_gap: int = 10, lookback: int = 250, as_of: date | None = None, today: date | None = None,
         now: datetime | None = None, max_seconds: float | None = None) -> dict:
    """只读扫描 data_dir 下的 *.parquet,返回可 JSON 序列化的结果字典。不抛异常:问题记入 errors/unreadable。

    as_of: 只用 <= as_of 的 K 线和日历(例如 2026-03-31 以便与研究截止日对齐);None 表示不截断。
    """
    t0 = time.time()
    now = now or beijing_now()
    today = today or now.astimezone(BEIJING).date()
    res: dict = {"status": "ok", "day": today.isoformat(), "as_of": as_of.isoformat() if as_of else None,
                 "max_gap": max_gap, "lookback": lookback, "data_dir": "", "files_total": 0, "scanned": 0,
                 "unreadable": [], "empty": [], "errors": [], "intraday_dropped_count": 0,
                 "timed_out": False}
    try:
        d = _resolve_dir(data_dir)
        res["data_dir"] = str(d)
        files = sorted(d.glob("*.parquet")) if d.is_dir() else []
        res["files_total"] = len(files)
        if not files:
            res["status"] = "no_data"
            return res

        # 15:30 前不用当天 K 线(和 dataquality 同一口径)
        drop_today = (today == now.astimezone(BEIJING).date()
                      and not market_clock.is_close_ready(now))
        res["intraday_dropped"] = drop_today
        cut = np.datetime64(as_of, "D") if as_of else None
        today64 = np.datetime64(today, "D")

        arrays: dict[str, np.ndarray] = {}
        for f in files:
            if max_seconds is not None and time.time() - t0 > max_seconds:
                res["timed_out"] = True
                break
            try:
                a = _read_index(f)
                if drop_today:
                    n = int((a == today64).sum())
                    if n:
                        res["intraday_dropped_count"] += n
                        a = a[a != today64]
                if cut is not None:
                    a = a[a <= cut]
            except Exception as e:  # noqa: BLE001
                res["unreadable"].append([f.name, f"{type(e).__name__}: {e}"[:200]])
                continue
            if len(a) == 0:
                res["empty"].append(f.stem)
                continue
            arrays[f.stem] = a
        res["scanned"] = len(arrays)
        if not arrays:
            res["status"] = "no_data"
            return res

        cal = union_calendar(arrays.values())
        latest = cal[-1]
        res["latest_day"] = str(latest)
        res["calendar_days"] = int(len(cal))
        res["calendar_first"] = str(cal[0])

        aux = d.parent
        suspended = _read_suspended(aux / "cn_all_suspended.json")
        names = _read_names(aux / "cn_all_list.csv")
        official = _read_calendar(aux / "cn_trade_calendar.csv")
        res["suspend_list_days"] = len(suspended) if suspended else 0
        res["suspend_list_range"] = ([min(suspended).isoformat(), max(suspended).isoformat()]
                                     if suspended else None)
        latest_d = date.fromisoformat(str(latest))
        susp_latest = (suspended or {}).get(latest_d, set())
        res["suspend_latest_count"] = len(susp_latest)

        # 当天(最新交易日)有多少只股票有 K 线,用来核对「并集日历」有没有被个别异常日期污染
        counts = pd.Series(np.concatenate(list(arrays.values()))).value_counts().sort_index()
        res["coverage_min_recent"] = _min_coverage_recent(counts, 30)

        stocks: dict = {}
        last_dist: dict = {}
        cat_count: dict = {}
        for code, a in arrays.items():
            fr = freshness_of(a, cal, lookback)
            cat = classify(fr, code in susp_latest, max_gap)
            fr["category"] = cat
            fr["name"] = names.get(code, "")
            # 末尾缺失的交易日里,有几天在停牌名单(名单只覆盖最近一段,故可能为 0)
            fr["tail_in_suspend_days"] = _tail_susp_days(code, cal, fr["tail_missing"], suspended)
            stocks[code] = fr
            last_dist[fr["last"]] = last_dist.get(fr["last"], 0) + 1
            cat_count[cat] = cat_count.get(cat, 0) + 1
        res["stocks"] = stocks
        res["last_date_dist"] = dict(sorted(last_dist.items()))
        res["category_count"] = cat_count
        res["at_latest"] = last_dist.get(str(latest), 0)
        # 停牌标记 vs 缺 K 线:最新交易日没有 K 线的股票,有多少在停牌名单;名单里的股票是否真的没有当天 K 线
        miss_latest = {c for c, s in stocks.items() if s["last"] != str(latest)}
        in_data = set(stocks)
        res["suspend_check"] = {
            "missing_latest": len(miss_latest),
            "missing_in_list": len(miss_latest & susp_latest),
            "missing_not_in_list": sorted(miss_latest - susp_latest)[:20],
            "list_has_bar": sorted((susp_latest & in_data) - miss_latest)[:20],
            "list_not_in_data": sorted(susp_latest - in_data)[:20],
        }
        res["calendar_check"] = _calendar_check(cal, official, latest_d, as_of)
        res["list_vs_files"] = _list_vs_files(aux / "cn_all_list.csv", set(arrays) | set(res["empty"]))
    except Exception as e:  # noqa: BLE001
        res["status"] = "error"
        res["errors"].append(f"{type(e).__name__}: {e}")
    finally:
        res["elapsed"] = round(time.time() - t0, 2)
    return res


def _min_coverage_recent(counts: pd.Series, n: int) -> dict:
    """日历最后 n 个交易日里,有 K 线的股票数的最小值及日期。"""
    tail = counts.iloc[-n:]
    if tail.empty:
        return {}
    k = tail.idxmin()
    return {"day": str(np.datetime64(k, "D")), "stocks": int(tail.min()), "max": int(tail.max())}


def _tail_susp_days(code, cal, tail_missing, suspended) -> int:
    if not suspended or tail_missing <= 0:
        return 0
    n = 0
    for day in cal[-tail_missing:]:
        if code in suspended.get(date.fromisoformat(str(day)), ()):
            n += 1
    return n


def _calendar_check(cal: np.ndarray, official, latest_d: date, as_of) -> dict:
    """并集日历 vs 官方日历文件:并集里有而官方没有 / 官方有而并集没有(只比到最新交易日)。"""
    if not official:
        return {"official_available": False}
    off = {x for x in official if x <= latest_d}
    first = date.fromisoformat(str(cal[0]))
    off = {x for x in off if x >= first}
    uni = {date.fromisoformat(str(x)) for x in cal}
    extra = sorted(uni - off)       # 数据里出现过、官方日历没有:疑似错日期的 K 线
    miss = sorted(off - uni)        # 官方日历有、没有任何股票有 K 线:全市场缺数据的交易日
    return {"official_available": True, "union_only": [x.isoformat() for x in extra][:30],
            "union_only_n": len(extra), "official_only": [x.isoformat() for x in miss][:30],
            "official_only_n": len(miss)}


def _list_vs_files(list_path: Path, have: set) -> dict:
    names = _read_names(list_path)
    if not names:
        return {"available": False}
    listed = set(names)
    return {"available": True, "listed": len(listed), "files": len(have),
            "listed_no_file": len(listed - have), "file_not_listed": len(have - listed),
            "listed_no_file_sample": sorted(listed - have)[:10],
            "file_not_listed_sample": sorted(have - listed)[:10]}


# ---------------------------------------------------------------- 渲染

def problems(res: dict) -> list:
    """问题股票按「最长连续缺失」降序,同值按类别严重程度、代码排序。"""
    order = {c: i for i, c in enumerate(PROBLEM_CATS)}
    items = [(c, s) for c, s in res.get("stocks", {}).items() if s["category"] in PROBLEM_CATS]
    items.sort(key=lambda cs: (-cs[1]["longest"], order[cs[1]["category"]], cs[0]))
    return items


def _esc(s) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ")


def render_markdown(res: dict, top_n: int = 30) -> str:
    L = ["# 数据新鲜度自检", ""]
    L.append(f"- 运行日期:{res['day']}(北京时间);状态:{res['status']};耗时 {res.get('elapsed', 0)} 秒")
    if res["status"] != "ok":
        L.append(f"- 没有可用数据或出错:{res.get('errors')}")
        return "\n".join(L) + "\n"
    L.append(f"- 数据目录:`{res['data_dir']}`;parquet {res['files_total']} 个,成功读取 {res['scanned']} 只,"
             f"读取失败 {len(res['unreadable'])} 只,空文件 {len(res['empty'])} 只")
    if res.get("as_of"):
        L.append(f"- **截断日期 as_of={res['as_of']}**:只使用此日及以前的 K 线与日历")
    if res.get("intraday_dropped"):
        L.append(f"- 北京时间 15:30 前:已忽略当天的盘中半天 K 线 {res['intraday_dropped_count']} 根")
    if res.get("timed_out"):
        L.append("- ⚠ 扫描超时被中断,结果只覆盖已读取的文件")
    L.append(f"- 交易日历:全部股票出现过的日期并集,{res['calendar_first']} ~ {res['latest_day']},共 {res['calendar_days']} 个交易日"
             "(近似,局限见文末说明)")
    L.append(f"- 最新交易日 **{res['latest_day']}**:{res['at_latest']} / {res['scanned']} 只追平"
             f"({res['at_latest'] / res['scanned']:.1%})")
    mc = res.get("coverage_min_recent") or {}
    if mc:
        L.append(f"- 最近 30 个交易日里,单日有 K 线的股票数:最少 {mc['stocks']} 只({mc['day']}),最多 {mc['max']} 只")
    L.append("")

    L += ["## 汇总表", "", "| 类别 | 只数 | 占比 |", "|---|---:|---:|"]
    total = res["scanned"]
    cc = res["category_count"]
    for cat in (CAT_OK, CAT_LAG, CAT_HIST, CAT_TAIL_SUSP, CAT_TAIL_OUT, CAT_MID):
        n = cc.get(cat, 0)
        L.append(f"| {CAT_ZH[cat]} | {n} | {n / total:.1%} |")
    L.append(f"| **合计** | {total} | 100% |")
    lb = res["lookback"]
    L += ["", f"问题股票 = 末尾连续缺失、或最近 {lb if lb else '全部'} 个交易日内恢复的中间缺口,超过 {res['max_gap']} 个交易日(严格大于)的股票:"
              f"{sum(cc.get(c, 0) for c in PROBLEM_CATS)} 只。「仅历史缺口」是更早以前出现过同样长缺口、"
              "但近期已恢复正常的股票,只作统计。", ""]

    L += ["## 最后一根 K 线日期分布", "", "| 最后一根日期 | 只数 | 落后最新交易日(交易日) |", "|---|---:|---:|"]
    dist = res["last_date_dist"]
    keys = sorted(dist, reverse=True)
    shown = keys[:15]
    pos = {}
    # 落后天数用 stocks 里任一条代表值反推,避免再传 cal:同一 last 的 tail_missing 相同
    for s in res["stocks"].values():
        pos.setdefault(s["last"], s["tail_missing"])
    for k in shown:
        L.append(f"| {k} | {dist[k]} | {pos.get(k, '')} |")
    rest = keys[15:]
    if rest:
        L.append(f"| 更早的 {len(rest)} 个日期(最早 {rest[-1]}) | {sum(dist[k] for k in rest)} | ≥ {pos.get(rest[0], '')} |")
    L.append("")

    items = problems(res)
    L += [f"## 问题股票前 {min(top_n, len(items))} 只(共 {len(items)} 只,按最长连续缺失降序)", ""]
    if items:
        L += ["| 代码 | 名称 | 类别 | 最后一根 | 末尾缺(日) | 近期中间最长缺口(日) | 缺口起点 | 首尾间缺失合计(日) | 末尾缺失日在停牌名单中的天数 | K线根数 |",
              "|---|---|---|---|---:|---:|---|---:|---:|---:|"]
        for code, s in items[:top_n]:
            L.append(f"| {code} | {_esc(s['name'])} | {CAT_ZH[s['category']]} | {s['last']} | {s['tail_missing']} | "
                     f"{s['mid_recent_longest']} | {s['mid_recent_start'] or ''} | {s['mid_missing']} | "
                     f"{s['tail_in_suspend_days']} | {s['n_bars']} |")
    else:
        L.append("无。")
    L.append("")

    cck = res.get("calendar_check", {})
    L += ["## 日历近似的核对", ""]
    if cck.get("official_available"):
        L.append(f"- 并集日历里有、官方 cn_trade_calendar.csv 里没有的日期:{cck['union_only_n']} 个"
                 + (f"(前几个:{', '.join(cck['union_only'][:10])})" if cck["union_only_n"] else ""))
        L.append(f"- 官方日历里有(≤ 最新交易日)、但没有任何股票有 K 线的日期:{cck['official_only_n']} 个"
                 + (f"(前几个:{', '.join(cck['official_only'][:10])})" if cck["official_only_n"] else ""))
    else:
        L.append("- 没有找到官方日历文件,无法核对。")
    lv = res.get("list_vs_files", {})
    if lv.get("available"):
        L.append(f"- 股票列表 cn_all_list.csv {lv['listed']} 只 vs parquet {lv['files']} 个:"
                 f"列表有但没文件 {lv['listed_no_file']}(例:{', '.join(lv['listed_no_file_sample'])}),"
                 f"有文件但不在列表 {lv['file_not_listed']}(例:{', '.join(lv['file_not_listed_sample'])})")
    sl = res.get("suspend_list_range")
    L.append(f"- 停牌名单 cn_all_suspended.json 只覆盖 {res['suspend_list_days']} 个交易日"
             + (f"({sl[0]} ~ {sl[1]})" if sl else "")
             + f";最新交易日在名单里的有 {res['suspend_latest_count']} 只")
    sc = res.get("suspend_check")
    if sc:
        L.append(f"- 停牌标记与缺 K 线的对照:最新交易日没有 K 线的 {sc['missing_latest']} 只里,"
                 f"{sc['missing_in_list']} 只在停牌名单;不在名单的有 {len(sc['missing_not_in_list'])} 只"
                 f"({', '.join(sc['missing_not_in_list'][:10])});名单里却有当天 K 线的 {len(sc['list_has_bar'])} 只;"
                 f"名单里但数据目录没有文件的 {len(sc['list_not_in_data'])} 只")
    L += ["", "## 局限", "",
          "- 日历是「任一股票出现过的日期」的并集:如果某个交易日全市场都没抓到数据,这一天不会被当成缺失;"
          "个别股票带了错误日期的 K 线会让日历多出假交易日(上面「核对」会列出)。",
          "- 一只股票最后一根停在很早之前,可能是退市、也可能是数据源断更,仅凭 K 线无法区分;"
          "停牌名单只覆盖最近一段,更早的停牌无法标记。",
          "- 新上市股票首根 K 线之前不算缺失(只看首尾之间和末尾)。",
          "- 中间缺口默认只看最近 250 个交易日内恢复的;更早的长停牌(2015 年集体停牌等)归入「仅历史缺口」,"
          "用 `--lookback 0` 可改看全历史。",
          "- 只看日期是否存在,不检查价格/成交量是否合理(价格一致性见 data_quality_report.py)。",
          ""]
    if res["unreadable"]:
        L += ["## 读取失败", ""] + [f"- {n}: {_esc(r)}" for n, r in res["unreadable"][:20]] + [""]
    return "\n".join(L)


def write_outputs(res: dict, out_dir, *, top_n: int = 30) -> dict:
    """写 md / json / csv。输出目录在数据目录之内则拒绝。失败只返回,不抛。"""
    try:
        out = Path(out_dir)
        if res.get("data_dir") and _inside(out, Path(res["data_dir"]).parent):
            return {"ok": False, "error": "输出目录位于数据目录之内,拒绝写入"}
        out.mkdir(parents=True, exist_ok=True)
        md = out / REPORT_MD
        _atomic_write(md, render_markdown(res, top_n))
        slim = {k: v for k, v in res.items() if k != "stocks"}
        slim["problems"] = [{"code": c, **s} for c, s in problems(res)]
        _atomic_write(out / REPORT_JSON, json.dumps(slim, ensure_ascii=False, indent=2))
        csv_path = out / REPORT_CSV
        tmp = csv_path.with_suffix(".csv.tmp")
        with open(tmp, "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["code", "name", "category", "last", "tail_missing", "mid_recent_longest",
                        "mid_recent_start", "mid_missing", "tail_in_suspend_days", "n_bars"])
            for c, s in problems(res):
                w.writerow([c, s["name"], s["category"], s["last"], s["tail_missing"], s["mid_recent_longest"],
                            s["mid_recent_start"] or "", s["mid_missing"], s["tail_in_suspend_days"],
                            s["n_bars"]])
        os.replace(tmp, csv_path)
        return {"ok": True, "md": str(md), "json": str(out / REPORT_JSON), "csv": str(csv_path)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def main(argv=None) -> int:
    try:
        ap = argparse.ArgumentParser(description="数据新鲜度自检(只读)")
        ap.add_argument("--data-dir", default=os.path.join(ROOT, "data", "daily", "cn_all"),
                        help="parquet 所在目录(只读)")
        ap.add_argument("--out-dir", default=os.path.join(ROOT, "reports", "data_freshness"),
                        help="报告输出目录,不存在则创建")
        ap.add_argument("--max-gap", type=int, default=10, help="连续缺失超过 N 个交易日算问题(默认 10)")
        ap.add_argument("--lookback", type=int, default=250,
                        help="中间缺口只看最近 N 个交易日内恢复的(默认 250,约一年;0=看全历史)")
        ap.add_argument("--top", type=int, default=30, help="问题清单显示前 N 只")
        ap.add_argument("--as-of", default=None, help="只用此日及以前的数据 YYYY-MM-DD,默认不截断")
        ap.add_argument("--date", default=None, help="运行日期 YYYY-MM-DD,默认北京时间今天")
        ap.add_argument("--max-seconds", type=float, default=None, help="扫描耗时上限(秒)")
        a = ap.parse_args(argv)
        if a.max_gap < 0 or a.lookback < 0:
            ap.error("--max-gap 和 --lookback 必须是非负整数")
        today = parse_date(a.date) if a.date else None
        as_of = parse_date(a.as_of) if a.as_of else None
        res = scan(a.data_dir, max_gap=a.max_gap, lookback=a.lookback, as_of=as_of, today=today, max_seconds=a.max_seconds)
        w = write_outputs(res, a.out_dir, top_n=a.top)
        if w["ok"]:
            cc = res.get("category_count", {})
            print(f"状态={res['status']} 已检查={res['scanned']} 最新交易日={res.get('latest_day')} "
                  f"问题股票={sum(cc.get(c, 0) for c in PROBLEM_CATS)} 报告={w['md']}")
        else:
            print(f"状态={res['status']} 报告写入失败:{w['error']}")
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        print(f"数据新鲜度自检失败(已忽略):{type(e).__name__}: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
