"""C12 涨跌停与停牌「想卖卖不掉」：对回测结果的影响量化。

问题：回测默认「信号出在收盘、次日开盘成交」。但 A 股次日开盘可能是一字涨停（想买买不进）、一字跌停（想卖卖不掉），
  也可能停牌（根本没有成交价）。回测里这些情况如果不处理（理想成交），收益会被高估；处理方式不同，高估的幅度也不同。
做法（不改任何已有文件、不改策略规则；一字板识别只读调用 trade_list 里的 board_limit 和同一个容差）：
  1. 行情读进来立刻截到 CUTOFF（2026-03-31），之后才算信号：registry.target_weights（收盘后定仓位）
     → portfolio.apply_band（10% 调仓阈值）→ 次日开盘成交，口径同 registry.run / scan.py；个股单边 8bp，空仓按 511880 计息。
  2. 一字板识别 = src/trade_list.limit_flags 的同一条规则：当天 O/H/L/C 在 1e-10 容差内相等，且相对昨收涨/跌幅达到
     板块涨跌停幅度减 TRADE_LIST_LIMIT_TOLERANCE（0.002）。limit_flags 一次只判最后一根 K 线（逐根调用太慢），这里用 numpy 向量化复刻，
     并在运行时对每只股票抽样若干根 K 线逐根调用原函数核对（结果写进 meta.limit_flags_check，不一致必须为 0）。
     唯一的偏离：board_limit 对 30 开头一律给 20%，而创业板 2020-08-24 之前是 10%，所以对 30 开头、此日期前的 K 线改用 10%（否则 2020 年前的创业板一字板全部漏判）。
  3. 四种成交口径（每只股票 × 每个策略都跑）：
       ideal      理想成交：次日开盘一律成交，不管是不是一字板（run_weights 不传 limits）；
       lock       顺延：次日开盘若是一字涨停，买入不成交；一字跌停，卖出不成交；仓位保持原样，下一根 K 线按当时的目标仓位再试
                  （信号已经变了就按新信号，不是死守旧单）；
       lock_buy   只有「买不进」生效；lock_sell 只有「卖不掉」生效 —— 用来拆开两边各贡献多少；
       project    项目现有口径（run_weights 的 limits：开盘跳空达到涨跌停幅度−0.2% 就不成交，不要求一字），只用来对照。
     「顺延」口径的实现：先自己走一遍仓位路径（execute），再把这条路径当作目标仓位喂给 run_weights（band=0），
     这样手续费、隔夜/日内收益拆分、现金计息全部沿用 run_weights，没有另写一套收益计算。没有任何一字板时它与 ideal 逐位相同（测试里验）。
  4. 停牌：行情里停牌日没有 K 线。用交易所日历（cn_trade_calendar.csv）对比：信号次日（自然成交日）在日历里、但这只股票没有那根 K 线，
     就算「停牌无法成交」，回测会在复牌日开盘成交（所以停牌本身在日线回测里已是「顺延」，没有「理想成交」可比，只能统计次数、停多久、
     以及信号收盘 → 复牌开盘这段跳空，也就是被困期间持仓者实际承担的价格变动）。数据缺口与真停牌无法区分（见报告局限）。
  5. 指标：信号次数 = 理想成交路径上的每一次仓位变动；命中一字涨停/跌停/停牌次数及占比；
     「被卡」的连续天数、最终成交价相对「当初该成交的价格」的差（买：多付，卖：少收，正数 = 对持有人不利）、被卡后信号取消的比例；
     收益：每只股票年化收益（复利）、理想 − 顺延的差（百分点/年）、对股票做自助法的 95% 区间；等权组合的年化差；分年；
     按「规模」分组：用旁表的静态 ADV（日均成交额）五等分（Q1 最小）和板块。
  6. ADV 来自 2026-09/10 的旁表（strategy_capacity.load_adv，C07 同一用法），只当股票的「规模标签」，不进信号、不进成交、不进收益；
     日期晚于 CUTOFF，所以只能做分组标签，报告里写明。

只读 data/ 下的行情和旁表，不写 data/；不联网。

用法：
  .venv/bin/python -B scripts/limit_lock_impact.py --out-dir DIR [--data-dir data] [--n-stocks 0] [--seed 6] [--workers 6] [--boot 2000]
  （--n-stocks 0 = 全部合格股票）
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import multiprocessing as mp
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from config import MARKETS, TRADE_LIST_LIMIT_TOLERANCE  # noqa: E402
from src import registry  # noqa: E402
from src.datasource import limit_pct  # noqa: E402
from src.metrics import TRADING_DAYS  # noqa: E402
from src.portfolio import apply_band, run_weights  # noqa: E402
from src.trade_list import board_limit, limit_flags  # noqa: E402  一字板识别：只读调用
from deflated_sharpe_table import check_end, find_daily_dir  # noqa: E402  C04：截止日检查、找行情目录
from strategy_overlap import BAND, CUTOFF, ORDER, STOCK_COST, WARM  # noqa: E402  C06：同一套截止日/预热/调仓阈值/成本/策略清单
from strategy_capacity import load_adv  # noqa: E402  C07：旁表静态 ADV

WIN_START = "2013-01-01"                    # 与 C11 相同的窗口起点
RECENT_START = "2020-08-24"                 # 创业板注册制（20% 涨跌停）生效日；此日之后 board_limit 的幅度对所有板块都准确
CYB_20PCT_START = pd.Timestamp(RECENT_START)
YEARS = list(range(2013, 2027))
SCEN = ["ideal", "lock", "lock_buy", "lock_sell", "project"]
SCEN_ZH = {"ideal": "理想成交", "lock": "顺延(一字板)", "lock_buy": "只限买入", "lock_sell": "只限卖出", "project": "项目现有口径"}
MIN_BARS = WARM + 250                       # 至少有预热 + 一年的数据才纳入
CHECK_PER_STOCK = 30                        # 每只股票核对 limit_flags 的 K 线数（一字板 K 线和非一字板 K 线各最多这么多根）
REL_TOL = 1e-10                             # 与 trade_list.limit_flags 里 math.isclose 的容差相同
ABS_TOL = 1e-10
EVENT_COLS = ["sig_buy", "sig_sell", "buy_lock", "sell_lock", "buy_susp", "sell_susp", "buy_susp_lock", "sell_susp_lock",
              "blk_days_buy", "blk_days_sell", "susp_days_buy", "susp_days_sell",
              "gap_sum_buy_susp", "gap_sum_buy_free", "gap_sum_sell_susp", "gap_sum_sell_free", "up_days", "dn_days", "bars"]


# ------------------------------------------------------------------ 纯函数（测试里单独验）
def limit_array(code: str, dates) -> np.ndarray:
    """每根 K 线适用的涨跌停幅度：board_limit(code)；30 开头（创业板）在注册制之前是 10%，不是 board_limit 给的 20%。"""
    dates = pd.DatetimeIndex(dates)
    lim = np.full(len(dates), board_limit(code), dtype=float)
    if code.startswith("30") and board_limit(code) == 0.20:
        lim[np.asarray(dates < CYB_20PCT_START)] = 0.10
    return lim


def lock_flags(df: pd.DataFrame, lim) -> tuple[np.ndarray, np.ndarray]:
    """limit_flags 的向量化版：返回每根 K 线 (一字涨停, 一字跌停) 两个布尔数组。
    一字 = O、H、L、C 都与 O 在 math.isclose(rel=1e-10, abs=1e-10) 意义下相等；涨跌幅 = 收盘 / 昨收 − 1；
    涨停：≥ lim − TRADE_LIST_LIMIT_TOLERANCE；跌停：≤ −lim + TRADE_LIST_LIMIT_TOLERANCE。第一根没有昨收，一律 False。"""
    o, h, l, c = (df[k].to_numpy(float) for k in ("Open", "High", "Low", "Close"))
    prev = np.concatenate([[np.nan], c[:-1]])
    with np.errstate(invalid="ignore", divide="ignore"):
        same = np.ones(len(df), dtype=bool)
        for x in (h, l, c):                 # math.isclose(a, b): |a−b| ≤ max(rel·max(|a|,|b|), abs)
            same &= np.abs(x - o) <= np.maximum(REL_TOL * np.maximum(np.abs(x), np.abs(o)), ABS_TOL)
        ok = same & np.isfinite(o) & np.isfinite(prev) & (prev > 0)
        chg = c / prev - 1
        up = ok & (chg >= lim - TRADE_LIST_LIMIT_TOLERANCE)
        dn = ok & (chg <= -lim + TRADE_LIST_LIMIT_TOLERANCE)
    return up, dn


def suspension_gaps(dates64: np.ndarray, cal64: np.ndarray) -> np.ndarray:
    """每根 K 线之前，交易所日历里有、但这只股票没有 K 线的天数（= 这根 K 线之前停牌/缺数据的交易日数）。第一根为 0。"""
    pos = np.searchsorted(cal64, dates64)
    gaps = np.diff(pos, prepend=pos[0]) - 1
    gaps[0] = 0
    return np.clip(gaps, 0, None)


def execute(intended: np.ndarray, buy_block: np.ndarray, sell_block: np.ndarray, opens: np.ndarray):
    """「顺延」口径的仓位路径。intended[i] = 第 i 根开盘想要的仓位（= 调仓阈值处理后的目标仓位晚一根）。
    想加仓且 buy_block[i]（一字涨停）→ 不成交；想减仓且 sell_block[i]（一字跌停）→ 不成交；不成交就保持上一根的仓位，下一根再试。
    判断条件与 portfolio.run_weights 的 limits 分支完全同构（w > prev / w < prev），只是触发条件换成一字板。
    返回 (actual 实际仓位, att 想成交的方向 +1/−1/0, blk 是否被卡, runs)。
    runs：一段「同方向、连续被卡」为一条 (方向, 起点, 连续被卡根数, 结局, 价差)；
      结局 0 = 之后成交了，价差 = 实际成交开盘价 / 当初该成交的开盘价 − 1；1 = 取消（信号变了，没再想同方向成交）；2 = 数据结束时仍被卡。"""
    n = len(intended)
    actual, att, blk = np.zeros(n), np.zeros(n, np.int8), np.zeros(n, bool)
    runs, prev = [], 0.0
    r_side, r_start, r_len = 0, -1, 0
    for i in range(n):
        w = intended[i]
        side = 1 if w > prev else (-1 if w < prev else 0)
        blocked = (side == 1 and bool(buy_block[i])) or (side == -1 and bool(sell_block[i]))
        att[i], blk[i] = side, blocked
        if r_side != 0:
            if side == r_side and blocked:
                r_len += 1
            else:                                   # 这一段在 i 结束
                if side == r_side:
                    runs.append((r_side, r_start, r_len, 0, float(opens[i] / opens[r_start] - 1)))
                else:
                    runs.append((r_side, r_start, r_len, 1, float("nan")))
                r_side = 0
        if r_side == 0 and blocked:
            r_side, r_start, r_len = side, i, 1
        if blocked:
            w = prev
        actual[i] = prev = w
    if r_side != 0:
        runs.append((r_side, r_start, r_len, 2, float("nan")))
    return actual, att, blk, runs


def cagr(x) -> float:
    """日收益序列 → 年化复合收益 (∏(1+r))^(252/n) − 1。"""
    x = np.asarray(x, float)
    x = x[np.isfinite(x)]
    return float(np.prod(1.0 + x) ** (TRADING_DAYS / len(x)) - 1.0) if len(x) else float("nan")


def adverse(side: int, slip: float) -> float:
    """对持有人不利的价差：买入多付 = +slip，卖出少收 = −slip（正数 = 不利）。"""
    return slip if side > 0 else -slip


def event_counts(intended, att, blk, up, dn, susp, opens, closes, in_win, yr_idx) -> dict:
    """窗口内按年份汇总的事件计数（数组长度 = len(YEARS)）。
    信号 = 理想成交路径上的每次仓位变动（intended 与上一根不同），发生在第 i 根开盘；命中 = 那根是一字板 / 前面有停牌缺口。"""
    n = len(intended)
    prev_i = np.concatenate([[0.0], intended[:-1]])
    buy, sell = (intended > prev_i) & in_win, (intended < prev_i) & in_win
    free_gap = np.concatenate([[np.nan], opens[1:] / closes[:-1] - 1])
    sp = susp > 0
    ny = len(YEARS)

    def cnt(mask, weights=None):
        return np.bincount(yr_idx[mask], weights=None if weights is None else weights[mask], minlength=ny)[:ny]

    gap0 = np.nan_to_num(free_gap)
    return {
        "sig_buy": cnt(buy), "sig_sell": cnt(sell),
        "buy_lock": cnt(buy & up), "sell_lock": cnt(sell & dn),
        "buy_susp": cnt(buy & sp), "sell_susp": cnt(sell & sp),
        "buy_susp_lock": cnt(buy & sp & up), "sell_susp_lock": cnt(sell & sp & dn),
        "blk_days_buy": cnt((att > 0) & blk & in_win), "blk_days_sell": cnt((att < 0) & blk & in_win),
        "susp_days_buy": cnt(buy & sp, susp.astype(float)), "susp_days_sell": cnt(sell & sp, susp.astype(float)),
        "gap_sum_buy_susp": cnt(buy & sp, gap0), "gap_sum_buy_free": cnt(buy & ~sp, gap0),
        "gap_sum_sell_susp": cnt(sell & sp, gap0), "gap_sum_sell_free": cnt(sell & ~sp, gap0),
        "up_days": cnt(up & in_win), "dn_days": cnt(dn & in_win), "bars": cnt(in_win),
    }


def boot_mean_ci(x, reps: int, seed: int) -> tuple[float, float]:
    """对股票做自助法：重抽样 reps 次，取均值的 2.5% / 97.5% 分位。"""
    x = np.asarray(x, float)
    x = x[np.isfinite(x)]
    if len(x) < 2 or reps <= 0:
        return float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    means = x[rng.integers(0, len(x), size=(reps, len(x)))].mean(axis=1)
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


# ------------------------------------------------------------------ 单只股票
_CASH: pd.Series | None = None
_CAL: np.ndarray | None = None
_MASTER: np.ndarray | None = None


def _init_worker(cash: pd.Series, cal64: np.ndarray, master64: np.ndarray) -> None:
    global _CASH, _CAL, _MASTER
    _CASH, _CAL, _MASTER = cash, cal64, master64


def check_limit_flags(code: str, df: pd.DataFrame, up: np.ndarray, dn: np.ndarray, rng) -> tuple[int, int]:
    """抽样核对向量化判定与 trade_list.limit_flags（逐根调用原函数，只传相邻两根）。
    向量化版用的是「日期相关」的幅度，这里临时换成 board_limit 的固定幅度重算一遍再比，保证比的是同一条规则。
    返回 (核对根数, 不一致根数)。"""
    up0, dn0 = lock_flags(df, np.full(len(df), board_limit(code)))
    locked = np.flatnonzero((up0 | dn0))
    locked = locked[locked >= 1]
    free = np.flatnonzero(~(up0 | dn0))
    free = free[free >= 1]
    pick = np.concatenate([rng.permutation(locked)[:CHECK_PER_STOCK], rng.permutation(free)[:CHECK_PER_STOCK]]).astype(int)
    bad = 0
    for i in pick:
        flags = limit_flags(code, df.iloc[i - 1:i + 1])
        want = ("up" if up0[i] else "dn" if dn0[i] else "none")
        got = "up" if any("涨停" in f for f in flags) else "dn" if any("跌停" in f for f in flags) else "none"
        bad += want != got
    return int(len(pick)), int(bad)


def stock_job(args):
    code, name, path = args
    raw = Path(path).read_bytes()
    df = pd.read_parquet(io.BytesIO(raw)).sort_index().loc[:CUTOFF]        # 读进来立刻截断
    out = stock_panel(df, code, name, _CASH, _CAL, _MASTER)
    if out is not None:
        out["sha256"] = hashlib.sha256(raw).hexdigest()
    return out


def stock_panel(df: pd.DataFrame, code: str, name: str, cash: pd.Series, cal64: np.ndarray, master64: np.ndarray) -> dict | None:
    if len(df) < MIN_BARS:
        return None
    check_end(str(df.index[-1].date()))
    dates = df.index
    opens, closes = df["Open"].to_numpy(float), df["Close"].to_numpy(float)
    up, dn = lock_flags(df, limit_array(code, dates))
    susp = suspension_gaps(dates.values, cal64)
    start = max(pd.Timestamp(WIN_START), dates[min(WARM, len(dates) - 1)])
    in_win = np.asarray(dates >= start)
    yr_idx = np.clip(dates.year.to_numpy() - YEARS[0], 0, len(YEARS) - 1)
    prices, zeros = {code: df}, np.zeros(len(df), bool)
    limits = {code: limit_pct(code, name)}
    n_chk, n_bad = check_limit_flags(code, df, up, dn, np.random.default_rng(int(code) if code.isdigit() else 0))
    pos = np.searchsorted(master64, dates.values[in_win])
    out = {"code": code, "name": name, "start": str(start.date()), "n_days": int(in_win.sum()), "pos": pos,
           "check": (n_chk, n_bad), "rets": {}, "cagr": {}, "events": [], "runs": [], "not_in_cal": int((~np.isin(dates.values, cal64)).sum())}
    base = event_counts(np.zeros(len(df)), np.zeros(len(df), np.int8), zeros, up, dn, susp, opens, closes, in_win, yr_idx)
    out["events"].append(("_all", {k: base[k] for k in ("up_days", "dn_days", "bars")}))
    for strat in ORDER:
        W = registry.target_weights(strat, prices, code, registry.clean_params(strat, {}), MARKETS["cn"])
        banded = apply_band(W.reindex(dates).fillna(0.0).clip(lower=0.0), BAND)
        intended = banded.shift(1).fillna(0.0).iloc[:, 0].to_numpy()
        res = {"ideal": run_weights(prices, W, cost=STOCK_COST, band=BAND, cash_ret=cash, limits=None),
               "project": run_weights(prices, W, cost=STOCK_COST, band=BAND, cash_ret=cash, limits=limits)}
        for key, (bb, sb) in {"lock": (up, dn), "lock_buy": (up, zeros), "lock_sell": (zeros, dn)}.items():
            actual, att, blk, runs = execute(intended, bb, sb, opens)
            tgt = pd.DataFrame({code: np.append(actual[1:], 0.0)}, index=dates)      # held = tgt.shift(1) = actual
            res[key] = run_weights(prices, tgt, cost=STOCK_COST, band=0.0, cash_ret=cash)
            if key == "lock":
                ev = event_counts(intended, att, blk, up, dn, susp, opens, closes, in_win, yr_idx)
                out["events"].append((strat, ev))
                out["runs"] += [(strat, s, int(yr_idx[st]), ln, status, slip) for s, st, ln, status, slip in runs if in_win[st]]
        for key in SCEN:
            r = res[key].returns.loc[start:]
            out["rets"][(strat, key)] = r.to_numpy(float)
            out["cagr"][(strat, key)] = cagr(r.to_numpy(float))
    return out


# ------------------------------------------------------------------ 股票池与分组
def load_universe(daily: Path, n_stocks: int, seed: int) -> pd.DataFrame:
    """cn_all_list 里去掉名字含 ST 的（ST 涨跌停幅度 5%，历史上哪天戴帽无从得知；与 C11 一致），行情文件存在的。
    n_stocks=0 取全部；否则固定种子随机抽。注意：名字含「退」的保留（数据里没有已退市股票，现存的这 2 只仍是最接近「卖不掉」的样本）。"""
    lst = pd.read_csv(daily / "cn_all_list.csv", dtype=str).fillna("")
    lst = lst[~lst["name"].str.upper().str.contains("ST")]
    lst = lst[[(daily / "cn_all" / f"{c}.parquet").exists() for c in lst["code"]]].sort_values("code").reset_index(drop=True)
    if n_stocks and n_stocks < len(lst):
        lst = lst.iloc[np.sort(np.random.default_rng(seed).permutation(len(lst))[:n_stocks])].reset_index(drop=True)
    return lst


def assign_groups(codes: list[str], boards: dict, adv: pd.Series, adv_all: pd.Series) -> pd.DataFrame:
    """规模五等分（Q1 最小）：分位点按「全部有 ADV 的非 ST 股票」定，再套到样本里的股票；没有 ADV 的标 NA。"""
    cuts = adv_all.quantile([0.2, 0.4, 0.6, 0.8]).to_numpy()
    p5 = float(adv_all.quantile(0.05))                     # 最小 5%：五等分太粗，「小盘」再细看一档
    rows = []
    for c in codes:
        q = f"Q{int(np.searchsorted(cuts, adv[c], side='right')) + 1}" if c in adv.index else "NA"
        rows.append({"code": c, "adv_q": q, "board": boards.get(c, "NA"), "adv": float(adv[c]) if c in adv.index else float("nan"),
                     "p5": bool(c in adv.index and adv[c] <= p5)})
    return pd.DataFrame(rows)


def group_defs(meta: pd.DataFrame) -> dict[str, np.ndarray]:
    g = {"ALL": meta["code"].to_numpy()}
    for q in ("Q1", "Q2", "Q3", "Q4", "Q5"):
        g[f"ADV_{q}"] = meta.loc[meta.adv_q == q, "code"].to_numpy()
    if "p5" in meta:
        g["ADV_最小5%"] = meta.loc[meta.p5.astype(bool), "code"].to_numpy()
    for b in sorted(meta["board"].unique()):
        if b != "NA":
            g[f"板块_{b}"] = meta.loc[meta.board == b, "code"].to_numpy()
    return g


# ------------------------------------------------------------------ 汇总
def pool_returns(res: list[dict], groups: dict, T: int) -> tuple[dict, dict]:
    """等权组合：对每个分组累加每天的日收益之和 sums[(分组, 策略, 口径)] 和当天有数据的股票数 counts[分组]（均值 = sums / counts）。"""
    sums = {(g, s, k): np.zeros(T) for g in groups for s in ORDER for k in SCEN}
    counts = {g: np.zeros(T) for g in groups}
    code_groups: dict[str, list[str]] = {}
    for g, codes in groups.items():
        for c in codes:
            code_groups.setdefault(c, []).append(g)
    for r in res:
        for g in code_groups[r["code"]]:
            counts[g][r["pos"]] += 1
            for (s, k), arr in r["rets"].items():
                sums[(g, s, k)][r["pos"]] += arr
    return sums, counts


def pooled_series(sums: dict, counts: dict, group: str, strat: str, scen: str) -> pd.Series:
    cnt = counts[group]
    with np.errstate(invalid="ignore", divide="ignore"):
        r = np.where(cnt > 0, sums[(group, strat, scen)] / cnt, np.nan)
    return pd.Series(r)


def build_tables(res: list[dict], meta: pd.DataFrame, master: pd.DatetimeIndex, boot: int, seed: int) -> dict:
    groups = group_defs(meta)
    member = {g: set(c) for g, c in groups.items()}
    # --- 事件
    ev_rows = []
    for r in res:
        for strat, ev in r["events"]:
            for yi, y in enumerate(YEARS):
                ev_rows.append({"code": r["code"], "strategy": strat, "year": y, **{k: float(v[yi]) for k, v in ev.items()}})
    ev = pd.DataFrame(ev_rows)
    ev_g = []
    for g, codes in member.items():
        sub = ev[ev.code.isin(codes)]
        tot = sub.groupby(["strategy", "year"], as_index=False)[[c for c in EVENT_COLS if c in sub.columns]].sum()
        tot.insert(0, "group", g)
        ev_g.append(tot)
    events = pd.concat(ev_g, ignore_index=True)
    # --- 被卡的段
    run_rows = [{"code": r["code"], "strategy": s, "side": "buy" if side > 0 else "sell", "year": YEARS[yi], "run_days": ln, "status": status,
                 "slip": slip, "adverse": adverse(side, slip) if status == 0 else float("nan")}
                for r in res for s, side, yi, ln, status, slip in r["runs"]]
    runs = pd.DataFrame(run_rows, columns=["code", "strategy", "side", "year", "run_days", "status", "slip", "adverse"])
    # --- 每只股票的年化
    ps_rows = []
    for r in res:
        for strat in ORDER:
            ps_rows.append({"code": r["code"], "strategy": strat, "n_days": r["n_days"], "start": r["start"],
                            **{f"cagr_{k}": r["cagr"][(strat, k)] for k in SCEN}})
    per_stock = pd.DataFrame(ps_rows).merge(meta, on="code")
    return {"events": events, "runs": runs, "per_stock": per_stock}


def return_compare(per_stock: pd.DataFrame, sums: dict, counts: dict, master: pd.DatetimeIndex, boot: int, seed: int,
                   groups: dict, since: str | None = None) -> pd.DataFrame:
    """策略 × 分组：每只股票年化的平均差（理想 − 各口径，百分点/年）+ 自助法区间 + 等权组合年化差。
    since 给定时只算等权组合（子窗口里没有逐股年化）。"""
    rows = []
    for g, codes in groups.items():
        for strat in ORDER:
            row = {"group": g, "strategy": strat}
            if since is None:
                sub = per_stock[(per_stock.strategy == strat) & per_stock.code.isin(set(codes))]
                row["n_stocks"] = len(sub)
                for k in SCEN:
                    row[f"mean_cagr_{k}_pct"] = sub[f"cagr_{k}"].mean() * 100
                for k in ("lock", "lock_buy", "lock_sell", "project"):
                    d = (sub["cagr_ideal"] - sub[f"cagr_{k}"]) * 100
                    row[f"diff_{k}_mean_pp"] = d.mean()
                    row[f"diff_{k}_median_pp"] = d.median()
                    if k == "lock":
                        row["diff_lock_ci_lo"], row["diff_lock_ci_hi"] = boot_mean_ci(d, boot, seed)
                        row["diff_lock_share_pos"] = float((d > 1e-9).mean())
                        row["diff_lock_share_ge1pp"] = float((d >= 1.0).mean())
                        row["diff_lock_p90_pp"] = float(d.quantile(0.9))
                        row["diff_lock_p99_pp"] = float(d.quantile(0.99))
                        row["diff_lock_max_pp"] = float(d.max())
                        row["diff_lock_share_neg"] = float((d < -1e-9).mean())          # 顺延反而更好的股票占比
            else:
                row["n_stocks"] = len(codes)
            ser = {k: pooled_series(sums, counts, g, strat, k) for k in SCEN}
            mask = (master >= pd.Timestamp(since)) if since else np.ones(len(master), bool)
            for k in SCEN:
                row[f"pooled_cagr_{k}_pct"] = cagr(ser[k][mask].dropna().to_numpy()) * 100
            for k in ("lock", "lock_buy", "lock_sell", "project"):
                row[f"pooled_diff_{k}_pp"] = row["pooled_cagr_ideal_pct"] - row[f"pooled_cagr_{k}_pct"]
            rows.append(row)
    return pd.DataFrame(rows)


def by_year_table(sums: dict, counts: dict, master: pd.DatetimeIndex, groups: dict) -> pd.DataFrame:
    rows = []
    yrs = master.year.to_numpy()
    for g in groups:
        for strat in ORDER:
            ser = {k: pooled_series(sums, counts, g, strat, k) for k in ("ideal", "lock")}
            for y in YEARS:
                m = yrs == y
                if not m.any():
                    continue
                ri = float(np.prod(1 + ser["ideal"][m].dropna().to_numpy()) - 1) * 100
                rl = float(np.prod(1 + ser["lock"][m].dropna().to_numpy()) - 1) * 100
                rows.append({"group": g, "strategy": strat, "year": y, "ideal_pct": ri, "lock_pct": rl, "diff_pp": ri - rl})
    return pd.DataFrame(rows)


def event_summary(events: pd.DataFrame, since_year: int | None = None) -> pd.DataFrame:
    """策略 × 分组：把年份加起来，算占比。"""
    e = events[(events.strategy != "_all")]
    if since_year:
        e = e[e.year >= since_year]
    s = e.groupby(["group", "strategy"], as_index=False)[[c for c in EVENT_COLS if c in e.columns]].sum()
    s["sell_lock_pct"] = 100 * s.sell_lock / s.sig_sell
    s["buy_lock_pct"] = 100 * s.buy_lock / s.sig_buy
    s["sell_susp_pct"] = 100 * s.sell_susp / s.sig_sell
    s["buy_susp_pct"] = 100 * s.buy_susp / s.sig_buy
    s["avg_susp_days_sell"] = s.susp_days_sell / s.sell_susp
    s["avg_susp_days_buy"] = s.susp_days_buy / s.buy_susp
    s["gap_ret_sell_susp_pct"] = 100 * s.gap_sum_sell_susp / s.sell_susp
    s["gap_ret_sell_free_pct"] = 100 * s.gap_sum_sell_free / (s.sig_sell - s.sell_susp)
    s["gap_ret_buy_susp_pct"] = 100 * s.gap_sum_buy_susp / s.buy_susp
    s["gap_ret_buy_free_pct"] = 100 * s.gap_sum_buy_free / (s.sig_buy - s.buy_susp)
    return s


def lock_frequency(events: pd.DataFrame) -> pd.DataFrame:
    """不依赖策略：每个分组里一字涨停/跌停的 K 线占比（每万根）。"""
    a = events[events.strategy == "_all"].groupby("group", as_index=False)[["up_days", "dn_days", "bars"]].sum()
    a["up_per_10k"] = 1e4 * a.up_days / a.bars
    a["dn_per_10k"] = 1e4 * a.dn_days / a.bars
    return a


def run_summary(runs: pd.DataFrame, groups: dict) -> pd.DataFrame:
    rows = []
    for g, codes in groups.items():
        sub = runs[runs.code.isin(set(codes))]
        for strat in [*ORDER, "ALL_STRATS"]:
            q = sub if strat == "ALL_STRATS" else sub[sub.strategy == strat]
            for side in ("buy", "sell"):
                z = q[q.side == side]
                ex = z[z.status == 0]
                rows.append({"group": g, "strategy": strat, "side": side, "n_runs": len(z),
                             "share_executed": float((z.status == 0).mean()) if len(z) else float("nan"),
                             "share_cancelled": float((z.status == 1).mean()) if len(z) else float("nan"),
                             "share_censored": float((z.status == 2).mean()) if len(z) else float("nan"),
                             "run_days_mean": float(z.run_days.mean()) if len(z) else float("nan"),
                             "run_days_median": float(z.run_days.median()) if len(z) else float("nan"),
                             "run_days_p90": float(z.run_days.quantile(0.9)) if len(z) else float("nan"),
                             "run_days_max": float(z.run_days.max()) if len(z) else float("nan"),
                             "adverse_mean_pct": float(ex.adverse.mean() * 100) if len(ex) else float("nan"),
                             "adverse_median_pct": float(ex.adverse.median() * 100) if len(ex) else float("nan"),
                             "adverse_p10_pct": float(ex.adverse.quantile(0.1) * 100) if len(ex) else float("nan"),
                             "adverse_p90_pct": float(ex.adverse.quantile(0.9) * 100) if len(ex) else float("nan")})
    return pd.DataFrame(rows)


# ------------------------------------------------------------------ 输出
def _f(v, nd=2):
    if v is None or (isinstance(v, float) and (np.isnan(v))):
        return "—"
    if isinstance(v, float) and np.isinf(v):
        return "∞"
    return f"{v:.{nd}f}"


def markdown(t: dict, meta: dict) -> str:
    es, es_recent, rc, rc_recent = t["event_summary"], t["event_summary_recent"], t["return_compare"], t["return_compare_recent"]
    rs, lf = t["run_summary"], t["lock_frequency"]
    L = [f"# 涨跌停与停牌「想卖卖不掉」对回测的影响（C12）", "",
         f"生成时间 {meta['generated']}；窗口 {WIN_START}~{CUTOFF}（每只股票前 {WARM} 根作预热）；股票 {meta['n_stocks_used']} 只（非 ST；固定种子 {meta['seed']}；"
         f"无已退市股票）；个股单边 {STOCK_COST * 1e4:.0f}bp、10% 调仓阈值、空仓按 511880 计息。「差」= 理想成交 − 顺延，**正数 = 理想成交高估了收益**。", ""]
    L += ["## 1. 信号命中一字板 / 停牌的次数（全部股票，窗口全程）", "",
          "| 策略 | 卖出信号数 | 卖出遇一字跌停 | % | 卖出遇停牌 | % | 买入信号数 | 买入遇一字涨停 | % | 买入遇停牌 | % |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    a = es[es.group == "ALL"].set_index("strategy")
    for s in ORDER:
        r = a.loc[s]
        L.append(f"| {s} | {r.sig_sell:.0f} | {r.sell_lock:.0f} | {_f(r.sell_lock_pct)} | {r.sell_susp:.0f} | {_f(r.sell_susp_pct)} | "
                 f"{r.sig_buy:.0f} | {r.buy_lock:.0f} | {_f(r.buy_lock_pct)} | {r.buy_susp:.0f} | {_f(r.buy_susp_pct)} |")
    L += ["", "## 2. 按规模（ADV 五等分，Q1 最小）：卖出遇一字跌停 % / 买入遇一字涨停 %（单元格 = 卖/买）", "",
          "| 策略 | " + " | ".join(f"ADV_Q{i}" for i in range(1, 6)) + " |", "|---|---|---|---|---|---|"]
    for s in ORDER:
        cells = []
        for q in range(1, 6):
            r = es[(es.group == f"ADV_Q{q}") & (es.strategy == s)]
            cells.append(f"{_f(r.sell_lock_pct.iloc[0])} / {_f(r.buy_lock_pct.iloc[0])}" if len(r) else "—")
        L.append(f"| {s} | " + " | ".join(cells) + " |")
    pooled = es.groupby("group")[["sig_sell", "sell_lock", "sell_susp", "sig_buy", "buy_lock", "buy_susp"]].sum()
    L += ["", "各分组（7 个策略合并）：信号命中一字板 / 停牌的占比 %：", "",
          "| 分组 | 卖出信号数 | 卖遇一字跌停 % | 卖遇停牌 % | 买入信号数 | 买遇一字涨停 % | 买遇停牌 % |", "|---|---|---|---|---|---|---|"]
    for g, r in pooled.iterrows():
        L.append(f"| {g} | {r.sig_sell:.0f} | {_f(100 * r.sell_lock / r.sig_sell, 3)} | {_f(100 * r.sell_susp / r.sig_sell, 3)} | "
                 f"{r.sig_buy:.0f} | {_f(100 * r.buy_lock / r.sig_buy, 3)} | {_f(100 * r.buy_susp / r.sig_buy, 3)} |")
    L += ["", "一字板 K 线占全部 K 线的比例（每万根）：", "", "| 分组 | 一字涨停 | 一字跌停 | K 线数 |", "|---|---|---|---|"]
    for _, r in lf.iterrows():
        L.append(f"| {r.group} | {_f(r.up_per_10k, 1)} | {_f(r.dn_per_10k, 1)} | {r.bars:.0f} |")
    L += ["", "## 3. 收益差：理想成交 vs 顺延（全部股票）", "",
          "每只股票先算年化再平均。「差」= 理想 − 顺延（百分点/年），方括号 = 对股票自助法的 95% 区间；「组合差」= 等权组合年化的差。", "",
          "| 策略 | 理想 % | 顺延 % | 项目现有 % | 平均差 pp [95%区间] | 中位差 pp | 差>0 的股票占比 | 差≥1pp 占比 | 只限买入 差 pp | 只限卖出 差 pp | 组合差 pp |",
          "|---|---|---|---|---|---|---|---|---|---|---|"]
    a = rc[rc.group == "ALL"].set_index("strategy")
    for s in ORDER:
        r = a.loc[s]
        L.append(f"| {s} | {_f(r.mean_cagr_ideal_pct)} | {_f(r.mean_cagr_lock_pct)} | {_f(r.mean_cagr_project_pct)} | {_f(r.diff_lock_mean_pp, 3)} "
                 f"[{_f(r.diff_lock_ci_lo, 3)}, {_f(r.diff_lock_ci_hi, 3)}] | {_f(r.diff_lock_median_pp, 3)} | {_f(100 * r.diff_lock_share_pos, 1)}% | "
                 f"{_f(100 * r.diff_lock_share_ge1pp, 1)}% | {_f(r.diff_lock_buy_mean_pp, 3)} | {_f(r.diff_lock_sell_mean_pp, 3)} | {_f(r.pooled_diff_lock_pp, 3)} |")
    L += ["", "差的分布（每只股票的 理想 − 顺延，pp/年）：大多数股票一点没有，少数股票很大：", "",
          "| 策略 | 中位 | P90 | P99 | 最大 | 顺延反而更好的股票占比 |", "|---|---|---|---|---|---|"]
    for s in ORDER:
        r = a.loc[s]
        L.append(f"| {s} | {_f(r.diff_lock_median_pp, 3)} | {_f(r.diff_lock_p90_pp, 3)} | {_f(r.diff_lock_p99_pp, 2)} | {_f(r.diff_lock_max_pp, 1)} | "
                 f"{_f(100 * r.diff_lock_share_neg, 1)}% |")
    gs = [g for g in rc.group.unique() if g.startswith("ADV") or g == "ALL"]
    gs = ["ALL", *[g for g in gs if g != "ALL"]]
    L += ["", "## 4. 按规模：平均差 pp/年（理想 − 顺延），行 = 策略，列 = 分组（ADV_最小5% = 全市场 ADV 最小的 5%）", "",
          "| 策略 | " + " | ".join(gs) + " |"]
    L.append("|---|" + "---|" * len(gs))
    for s in ORDER:
        L.append(f"| {s} | " + " | ".join(_f(rc[(rc.group == g) & (rc.strategy == s)].diff_lock_mean_pp.iloc[0], 3) for g in gs) + " |")
    L += ["", f"## 5. 子窗口 {RECENT_START}~{CUTOFF}（等权组合年化差 pp，理想 − 顺延；板块涨跌停幅度在此之后全部准确）", "",
          "| 策略 | " + " | ".join(gs) + " |", "|---|" + "---|" * len(gs)]
    for s in ORDER:
        L.append(f"| {s} | " + " | ".join(_f(rc_recent[(rc_recent.group == g) & (rc_recent.strategy == s)].pooled_diff_lock_pp.iloc[0], 3) for g in gs) + " |")
    L += ["", "## 6. 被卡住的时长和代价（全部股票、7 个策略合并）", "",
          "| 分组 | 方向 | 段数 | 之后成交 | 信号取消 | 数据结束仍卡 | 平均卡几根 | 中位 | P90 | 最长 | 成交价不利幅度 均值% | 中位% | P10% | P90% |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for g in ("ALL", "ADV_Q1", "ADV_Q3", "ADV_Q5"):
        for side in ("sell", "buy"):
            r = rs[(rs.group == g) & (rs.strategy == "ALL_STRATS") & (rs.side == side)]
            if r.empty:
                continue
            r = r.iloc[0]
            L.append(f"| {g} | {'卖' if side == 'sell' else '买'} | {r.n_runs:.0f} | {_f(100 * r.share_executed, 1)}% | {_f(100 * r.share_cancelled, 1)}% | "
                     f"{_f(100 * r.share_censored, 1)}% | {_f(r.run_days_mean)} | {_f(r.run_days_median, 1)} | {_f(r.run_days_p90, 1)} | {_f(r.run_days_max, 0)} | "
                     f"{_f(r.adverse_mean_pct)} | {_f(r.adverse_median_pct)} | {_f(r.adverse_p10_pct)} | {_f(r.adverse_p90_pct)} |")
    L += ["", "「成交价不利幅度」= 实际成交开盘价相对当初（被卡那天）该成交开盘价：买 = 多付的比例，卖 = 少收的比例；正数 = 对持有人不利。只统计最终成交的段。", "",
          "## 7. 停牌：信号次日是停牌日（全部股票、7 个策略合并）", "",
          "| 方向 | 次数 | 占信号数 % | 平均停牌交易日数 | 信号收盘→复牌开盘 平均跳空 % | 同口径：非停牌的平均隔夜跳空 % |", "|---|---|---|---|---|---|"]
    tot = es[es.group == "ALL"][[c for c in EVENT_COLS]].sum()
    for side, zh in (("sell", "卖出"), ("buy", "买入")):
        n_sig, n_sp = tot[f"sig_{side}"], tot[f"{side}_susp"]
        L.append(f"| {zh} | {n_sp:.0f} | {_f(100 * n_sp / n_sig)} | {_f(tot[f'susp_days_{side}'] / n_sp) if n_sp else '—'} | "
                 f"{_f(100 * tot[f'gap_sum_{side}_susp'] / n_sp) if n_sp else '—'} | {_f(100 * tot[f'gap_sum_{side}_free'] / (n_sig - n_sp))} |")
    ev, by = t["events"], t["by_year"]
    e = ev[(ev.group == "ALL") & (ev.strategy != "_all")].groupby("year")[["sig_sell", "sell_lock", "sig_buy", "buy_lock"]].sum()
    L += ["", "## 8. 分年（全部股票）：7 个策略合并的命中率，以及等权组合每年的 理想 − 顺延（pp）", "",
          "| 年 | 卖出遇一字跌停 % | 买入遇一字涨停 % | " + " | ".join(ORDER) + " |", "|---|---|---|" + "---|" * len(ORDER)]
    for y, r in e.iterrows():
        cells = [by[(by.group == "ALL") & (by.strategy == s) & (by.year == y)].diff_pp for s in ORDER]
        L.append(f"| {y} | {_f(100 * r.sell_lock / r.sig_sell, 3)} | {_f(100 * r.buy_lock / r.sig_buy, 3)} | "
                 + " | ".join(_f(float(c.iloc[0]), 3) if len(c) else "—" for c in cells) + " |")
    return "\n".join(L) + "\n"


COLORS = {"blend2": "#0072B2", "trend_ens": "#E69F00", "rsi2": "#009E73", "donchian_s1": "#CC79A7",
          "trend_vol": "#D55E00", "ma_adx_original": "#56B4E9", "ma_adx_plus": "#444444"}


def make_figures(t: dict, out: Path) -> list[str]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams["font.sans-serif"] = ["PingFang SC", "Heiti SC", "Arial Unicode MS", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    files, qs = [], [f"ADV_Q{i}" for i in range(1, 6)]
    es, rc = t["event_summary"], t["return_compare"]
    fig, axes = plt.subplots(1, 2, figsize=(10, 4.2), constrained_layout=True)
    for ax, col, title in ((axes[0], "sell_lock_pct", "卖出信号遇一字跌停 %"), (axes[1], "buy_lock_pct", "买入信号遇一字涨停 %")):
        for s in ORDER:
            v = [es[(es.group == q) & (es.strategy == s)][col].iloc[0] for q in qs]
            ax.plot(range(5), v, marker="o", ms=3, color=COLORS[s], label=s, lw=1.5)
        ax.set_xticks(range(5))
        ax.set_xticklabels(["Q1\n最小", "Q2", "Q3", "Q4", "Q5\n最大"])
        ax.set_title(title)
        ax.grid(alpha=0.25)
    axes[0].legend(frameon=False, fontsize=7)
    fig.savefig(out / "fig_lock_rate_by_size.png", dpi=140)
    plt.close(fig)
    files.append("fig_lock_rate_by_size.png")
    fig, ax = plt.subplots(figsize=(6.4, 4.2), constrained_layout=True)
    for s in ORDER:
        v = [rc[(rc.group == q) & (rc.strategy == s)].diff_lock_mean_pp.iloc[0] for q in qs]
        ax.plot(range(5), v, marker="o", ms=3, color=COLORS[s], label=s, lw=1.5)
    ax.axhline(0, color="#555", lw=1)
    ax.set_xticks(range(5))
    ax.set_xticklabels(["Q1\n最小", "Q2", "Q3", "Q4", "Q5\n最大"])
    ax.set_ylabel("理想 − 顺延 年化差（百分点）")
    ax.set_title("理想成交高估的年化收益，按规模")
    ax.grid(alpha=0.25)
    ax.legend(frameon=False, fontsize=7)
    fig.savefig(out / "fig_return_gap_by_size.png", dpi=140)
    plt.close(fig)
    files.append("fig_return_gap_by_size.png")
    ev = t["events"]
    fig, ax = plt.subplots(figsize=(7.2, 4.2), constrained_layout=True)
    for g, c in (("ALL", "#444444"), ("ADV_Q1", "#D55E00"), ("ADV_Q5", "#0072B2")):
        e = ev[(ev.group == g) & (ev.strategy != "_all")].groupby("year")[["sig_sell", "sell_lock"]].sum()
        ax.plot(e.index, 100 * e.sell_lock / e.sig_sell, marker="o", ms=3, color=c, label=g)
    ax.set_ylabel("卖出信号遇一字跌停 %")
    ax.set_title("分年：卖出信号被一字跌停卡住的比例（7 策略合并）")
    ax.grid(alpha=0.25)
    ax.legend(frameon=False)
    fig.savefig(out / "fig_sell_lock_by_year.png", dpi=140)
    plt.close(fig)
    files.append("fig_sell_lock_by_year.png")
    return files


# ------------------------------------------------------------------ 主流程
def run(data_dir: Path, out_dir: Path, n_stocks: int, seed: int, workers: int, boot: int, log=print) -> dict:
    check_end(CUTOFF)
    t0 = time.time()
    daily = find_daily_dir(data_dir.resolve())
    log(f"行情目录 {daily}")
    cal = pd.read_csv(daily / "cn_trade_calendar.csv", parse_dates=["trade_date"])["trade_date"]
    cal = cal[cal <= pd.Timestamp(CUTOFF)].sort_values()
    master = pd.DatetimeIndex(cal[cal >= pd.Timestamp(WIN_START)])
    cash_path = daily / "cn" / f"{MARKETS['cn']['cash']}.csv"
    cash = pd.read_csv(cash_path, index_col=0, parse_dates=True).sort_index().loc[:CUTOFF]["Close"].pct_change()
    uni = load_universe(daily, n_stocks, seed)
    lst_all = pd.read_csv(daily / "cn_all_list.csv", dtype=str).fillna("")
    adv, adv_info = load_adv(daily)
    adv_all = adv[adv.index.isin(lst_all[~lst_all["name"].str.upper().str.contains("ST")]["code"])]
    meta_df = assign_groups(list(uni["code"]), dict(zip(lst_all["code"], lst_all["board"])), adv, adv_all)
    log(f"股票 {len(uni)} 只；ADV 覆盖 {int((meta_df.adv_q != 'NA').sum())} 只（{time.time() - t0:.0f}s）")
    jobs = [(c, n, str(daily / "cn_all" / f"{c}.parquet")) for c, n in zip(uni["code"], uni["name"])]
    init = (cash, cal.values, master.values)
    res = []
    if workers > 1:
        with mp.get_context("spawn").Pool(workers, initializer=_init_worker, initargs=init) as pool:
            for k, r in enumerate(pool.imap_unordered(stock_job, jobs, chunksize=4), 1):
                if r is not None:
                    res.append(r)
                if k % 200 == 0:
                    log(f"  已处理 {k}/{len(jobs)}（{time.time() - t0:.0f}s）")
    else:
        _init_worker(*init)
        for k, j in enumerate(jobs, 1):
            r = stock_job(j)
            if r is not None:
                res.append(r)
            if k % 50 == 0:
                log(f"  已处理 {k}/{len(jobs)}（{time.time() - t0:.0f}s）")
    res.sort(key=lambda r: r["code"])
    meta_df = meta_df[meta_df.code.isin({r["code"] for r in res})].reset_index(drop=True)
    log(f"回测完成：{len(res)} 只纳入（{time.time() - t0:.0f}s）")
    groups = group_defs(meta_df)
    sums, counts = pool_returns(res, groups, len(master))
    tabs = build_tables(res, meta_df, master, boot, seed)
    out_t = {"events": tabs["events"], "runs": tabs["runs"], "per_stock": tabs["per_stock"]}
    out_t["event_summary"] = event_summary(tabs["events"])
    out_t["event_summary_recent"] = event_summary(tabs["events"], since_year=int(RECENT_START[:4]))
    out_t["lock_frequency"] = lock_frequency(tabs["events"])
    out_t["return_compare"] = return_compare(tabs["per_stock"], sums, counts, master, boot, seed, groups)
    out_t["return_compare_recent"] = return_compare(tabs["per_stock"], sums, counts, master, boot, seed, groups, since=RECENT_START)
    out_t["by_year"] = by_year_table(sums, counts, master, groups)
    out_t["run_summary"] = run_summary(tabs["runs"], groups)
    out_dir.mkdir(parents=True, exist_ok=True)
    assert tabs["per_stock"]["start"].max() <= CUTOFF and master.max() <= pd.Timestamp(CUTOFF), "有结果超过研究截止日"
    for name, df in out_t.items():
        if name not in ("runs",):
            df.to_csv(out_dir / f"{name}.csv", index=False)
    out_t["runs"].to_csv(out_dir / "runs.csv.gz", index=False)
    n_chk, n_bad = (sum(r["check"][i] for r in res) for i in (0, 1))
    digests = {r["code"]: r["sha256"] for r in res}
    meta = {"generated": time.strftime("%Y-%m-%d %H:%M:%S"), "cutoff": CUTOFF, "window_start": WIN_START, "recent_start": RECENT_START,
            "warm": WARM, "band": BAND, "stock_cost": STOCK_COST, "n_stocks_requested": n_stocks, "n_stocks_used": len(res), "seed": seed,
            "limit_flags_check": {"checked_bars": n_chk, "mismatches": n_bad},
            "dates_not_in_calendar": int(sum(r["not_in_cal"] for r in res)),
            "limit_tolerance": TRADE_LIST_LIMIT_TOLERANCE, "adv": {k: v for k, v in adv_info.items() if k != "common_days"},
            "adv_groups": meta_df.adv_q.value_counts().sort_index().to_dict(), "boards": meta_df.board.value_counts().to_dict(),
            "daily_dir": str(daily), "versions": {"pandas": pd.__version__, "numpy": np.__version__},
            "input_sha256": {"stocks_combined": hashlib.sha256(json.dumps(digests, sort_keys=True).encode()).hexdigest(),
                             "calendar": hashlib.sha256((daily / "cn_trade_calendar.csv").read_bytes()).hexdigest(),
                             "cash_511880": hashlib.sha256(cash_path.read_bytes()).hexdigest()}}
    meta["figures"] = make_figures(out_t, out_dir)
    (out_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
    (out_dir / "tables.md").write_text(markdown(out_t, meta), encoding="utf-8")
    log(f"limit_flags 核对 {n_chk} 根，不一致 {n_bad}；写出 {out_dir}（{time.time() - t0:.0f}s）")
    return {"tables": out_t, "meta": meta}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out-dir", required=True, type=Path)
    ap.add_argument("--data-dir", type=Path, default=ROOT / "data")
    ap.add_argument("--n-stocks", type=int, default=0)
    ap.add_argument("--seed", type=int, default=6)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--boot", type=int, default=2000)
    a = ap.parse_args(argv)
    a.out_dir.mkdir(parents=True, exist_ok=True)

    def log(msg):                       # 同时打印并追加到输出目录里的 run.log
        print(msg, flush=True)
        with (a.out_dir / "run.log").open("a", encoding="utf-8") as f:
            f.write(msg + "\n")

    run(a.data_dir, a.out_dir, a.n_stocks, a.seed, a.workers, a.boot, log=log)


if __name__ == "__main__":
    main()
