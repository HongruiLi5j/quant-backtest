"""C11 交易成本敏感性：七个正式策略在单边 0 / 8 / 15 / 30bp 成本下的净表现，以及「盈亏平衡成本」。

问题：回测默认只扣 5bp（ETF）或 8bp（个股）的单边成本。真实成本可能更高；策略的优势（超额收益）能扛住多高的成本？
  「盈亏平衡成本」= 让策略净超额年化收益刚好为 0 的单边成本（bp）。越高越稳；低于真实成本说明策略在现实里是亏钱的。
做法（全程复用项目已有函数，不改任何策略规则，不改任何已有文件）：
  1. 行情读进来立刻截到 CUTOFF（2026-03-31），之后才算信号：registry.target_weights（每日目标仓位，收盘后定、次日成交）
     → portfolio.run_weights(cost=…)（次日开盘成交、10% 调仓阈值、空仓现金计息、A 股一字板）。
     成本只通过 run_weights 的 cost 参数传入；registry._detail 里网页「成本敏感性」就是用同一个参数乘 0/1/2/4 倍，本脚本复用这个机制，
     但换成任务要求的绝对成本网格（它是网页单品种、乘数网格、不能独立调用，所以没法直接复用函数本身）。
  2. 窗口 2013-01-01 ~ 2026-03-31，每个品种前 WARM=260 根作预热（同 registry.run），窗口起点 = max(2013-01-01, 第 260 根)。
  3. 五组对象：SPY、510300、us_ew（美股 ETF 池除现金外等权）、cn_ew（A 股 ETF 池除现金外等权）、cn_stock（固定种子抽的 A 股个股等权）。
     等权 = 每天对当天有行情的品种取日收益平均。成本网格对所有组一视同仁（ETF 和个股用同一组 bp），便于比较。
  4. 指标（每组 × 策略 × 成本）：
       年化收益 = 日收益连乘^(252/天数) − 1；
       净超额（对现金）= 年化收益 − 同窗口现金 ETF（美 SHY、A 股 511880）的年化收益；
       夏普 = 日超额收益（策略 − 现金）均值/标准差 × √252（metrics.sharpe，同 C04/C08）；
       换手率 = 年化单边换手 = portfolio.run_weights 返回的 turnover（每天 Σ|仓位变化|）的日均值 × 252，单位「倍净值/年」。
       另给「对买入持有」的超额（同一成本下的买入持有，买入持有只在预热期付一次成本，窗口内几乎不付）。
  5. 盈亏平衡成本：对 4 个成本点上的净超额做线性插值，找第一次穿过 0 的单边成本。
       若 30bp 时仍 > 0，用最后两点（15、30bp）线性外推并标「外推」，再用真实回测在该成本重算一遍核对（残差应接近 0）；
       若 0bp 时净超额已 ≤ 0，标「0成本已亏」，不存在盈亏平衡点（再便宜也不赚）。
     不确定性：对日收益做循环区块自助法（块长 20，同一批抽样日同时用于 4 个成本点），给盈亏平衡成本的 95% 区间。
  6. 理论核对：成本在 run_weights 里是 (1 − cost·turnover) 逐日连乘，每天的对数收益少 cost·turnover，
     所以年化「对数」收益的下降 = cost × 年化换手，折算成年化「复利收益」（CAGR）再乘 (1+R)。
     输出里给 (R(0)−R(30)) / (30bp × 换手) 的比值，应约等于 1+R（R 是年化收益，几个百分点 → 比值略大于 1）。

只读 data/ 下的行情，不写 data/；不联网。

用法：
  .venv/bin/python -B scripts/cost_breakeven.py --out-dir DIR [--data-dir data] [--n-stocks 300] [--seed 6] [--boot 500]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from config import MARKETS  # noqa: E402
from src import registry  # noqa: E402
from src.datasource import limit_pct  # noqa: E402
from src.metrics import TRADING_DAYS, sharpe  # noqa: E402
from src.portfolio import run_weights  # noqa: E402
from deflated_sharpe_table import check_end, find_daily_dir, load_prices  # noqa: E402  C04：截止日检查、找行情目录、读行情
from strategy_overlap import BAND, CUTOFF, ORDER, WARM  # noqa: E402  C06：同一套截止日/预热/调仓阈值/策略清单

WIN_START = "2013-01-01"                    # 任务指定的窗口起点
COSTS_BP = [0, 8, 15, 30]                   # 任务指定的单边成本网格（bp）
BH = "buy_hold"                             # 参照，不进 7 个正式策略
GROUPS = ["SPY", "510300", "us_ew", "cn_ew", "cn_stock"]
BENCHES = ["cash", "buy_hold"]              # 净超额的两种口径
BLOCK = 20                                  # 自助法区块长度（约一个交易月）
BE_CAP = 1000.0                             # 自助法里「外推出的盈亏平衡成本」的上限（bp），只用于区间，防止斜率≈0 时出现天文数字
STOCK_LISTED_BY = "2011-12-31"              # 个股要在这天之前上市，才能在 2013-01-01 就有完整的 260 根预热


# ------------------------------------------------------------------ 公式（纯函数，测试里单独验）
def cagr(x) -> float:
    """日收益序列 → 年化复合收益 (∏(1+r))^(252/n) − 1。"""
    x = np.asarray(x, float)
    x = x[np.isfinite(x)]
    return float(np.prod(1.0 + x) ** (TRADING_DAYS / len(x)) - 1.0) if len(x) else float("nan")


def annual_turnover(turn) -> float:
    """逐日单边换手（Σ|仓位变化|）→ 年化单边换手 = 日均 × 252（倍净值/年）。"""
    t = np.asarray(turn, float)
    t = t[np.isfinite(t)]
    return float(TRADING_DAYS * t.mean()) if len(t) else float("nan")


def breakeven(costs, excess) -> tuple[float, str]:
    """净超额随成本下降；返回 (盈亏平衡单边成本 bp, 方式)。
    方式：interp = 在网格内线性插值；extrap = 网格最后两点外推（30bp 时仍 > 0）；
          loss_at_zero = 0 成本时净超额已 ≤ 0（无盈亏平衡点，返回 nan）；flat = 超额不随成本下降（斜率 ≥ 0，永远不亏平）；
          nodata = 有缺失值。"""
    c = np.asarray(costs, float)
    v = np.asarray(excess, float)
    if len(c) < 2 or not np.all(np.isfinite(v)):
        return float("nan"), "nodata"
    if v[0] <= 0:
        return float("nan"), "loss_at_zero"
    for i in range(len(c) - 1):
        if v[i + 1] <= 0:                      # 第一次穿过 0：在 [c_i, c_{i+1}] 内插值
            return float(c[i] + v[i] * (c[i + 1] - c[i]) / (v[i] - v[i + 1])), "interp"
    slope = (v[-1] - v[-2]) / (c[-1] - c[-2])
    if slope >= 0:
        return float("inf"), "flat"
    return float(c[-1] + v[-1] / (-slope)), "extrap"


def breakeven_capped(costs, excess, cap: float = BE_CAP) -> float:
    """自助法用：把 breakeven 的各种情形变成一个有限数。0 成本已亏 → 0；永远不亏平或超过 cap → cap。"""
    be, kind = breakeven(costs, excess)
    if kind == "loss_at_zero":
        return 0.0
    if kind == "nodata":
        return float("nan")
    return float(min(be, cap))


def drag_ratio(r0: float, r_hi: float, cost_bp: float, turn: float) -> float:
    """(R(0) − R(cost)) / (cost × 换手)：理论上 ≈ 1（成本逐日扣，复利下略有偏离）。"""
    d = cost_bp / 1e4 * turn
    return float((r0 - r_hi) / d) if d > 0 else float("nan")


def approx_breakeven_bp(excess0: float, turn: float) -> float:
    """一阶近似：成本每多 1bp，年化收益少 turn × 1bp（再乘 1+R，忽略），所以盈亏平衡 ≈ 0 成本净超额 ÷ 换手。
    excess0 是小数（0.05 = 5%），返回 bp；换手 ≤ 0 或超额 ≤ 0 返回 nan。"""
    return float(excess0 / turn * 1e4) if turn > 0 and excess0 > 0 else float("nan")


def block_bootstrap_breakeven(R: np.ndarray, rf: np.ndarray, costs, reps: int, seed: int, block: int = BLOCK) -> np.ndarray:
    """R：T×len(costs)，每列是同一策略在一个成本下的日收益；rf：T 日现金收益。
    循环区块自助法：同一批抽样日同时取所有成本列和 rf（保持成本之间的配对关系），每次重算净超额再算盈亏平衡成本。"""
    rng = np.random.default_rng(seed)
    T = len(R)
    nb = int(np.ceil(T / block))
    out = np.empty(reps)
    for k in range(reps):
        starts = rng.integers(0, T, size=nb)
        idx = ((starts[:, None] + np.arange(block)[None, :]) % T).reshape(-1)[:T]
        rf_ann = cagr(rf[idx])
        ex = [cagr(R[idx, j]) - rf_ann for j in range(R.shape[1])]
        out[k] = breakeven_capped(costs, ex)
    return out


# ------------------------------------------------------------------ 单个品种：目标仓位只算一次，成本随便换
class Inst:
    """一个品种：df 已截到 CUTOFF。W[策略] = 目标仓位（收盘后定、次日成交，和成本无关）；每个成本只重跑 run_weights。"""

    def __init__(self, df: pd.DataFrame, symbol: str, mcfg: dict, cash: pd.Series | None, limit: float | None):
        check_end(str(df.index[-1].date()))
        self.symbol, self.df, self.cash, self.limit = symbol, df, cash, limit
        self.prices = {symbol: df}
        self.W = {}
        for name in [*ORDER, BH]:
            if name == BH:
                self.W[name] = pd.Series(1.0, index=df.index, name=symbol).to_frame()
            else:
                self.W[name] = registry.target_weights(name, self.prices, symbol, registry.clean_params(name, {}), mcfg)
        idx = df.index.intersection(self.W[ORDER[0]].index)
        self.start = max(pd.Timestamp(WIN_START), idx[min(WARM, len(idx) - 1)])      # 预热 + 窗口起点

    def run(self, strat: str, cost_bp: float) -> tuple[pd.Series, pd.Series]:
        """返回窗口内 (日总收益, 日单边换手)。"""
        lim = {self.symbol: self.limit} if self.limit else None
        res = run_weights(self.prices, self.W[strat], cost=cost_bp / 1e4, band=0.0 if strat == BH else BAND,
                          cash_ret=self.cash, limits=lim)
        sl = slice(self.start, pd.Timestamp(CUTOFF))
        return res.returns.loc[sl], res.turnover.loc[sl]


def group_series(insts: list, strat: str, cost_bp: float) -> tuple[pd.Series, pd.Series]:
    """组内每个品种各跑各的，日收益和日换手对当天有行情的品种等权平均。"""
    rets, turns = {}, {}
    for it in insts:
        r, t = it.run(strat, cost_bp)
        rets[it.symbol], turns[it.symbol] = r, t
    return pd.DataFrame(rets).mean(axis=1, skipna=True), pd.DataFrame(turns).mean(axis=1, skipna=True)


def group_cash(cash: pd.Series | None, index: pd.Index) -> pd.Series:
    return (cash.reindex(index).fillna(0.0) if cash is not None else pd.Series(0.0, index=index))


def group_table(name: str, insts: list, cash: pd.Series | None, costs=COSTS_BP, boot: int = 0, seed: int = 6,
                strategies=ORDER) -> tuple[list[dict], list[dict]]:
    """一个组 → (指标行, 盈亏平衡行)。指标行：策略 × 成本；盈亏平衡行：策略 × 基准(现金/买入持有)。"""
    rows, be_rows = [], []
    bh = {c: group_series(insts, BH, c) for c in costs}
    idx = bh[costs[0]][0].index
    rf = group_cash(cash, idx)
    rf_ann = cagr(rf.to_numpy())
    for strat in strategies:
        series = {c: group_series(insts, strat, c) for c in costs}
        ann = {c: cagr(series[c][0].to_numpy()) for c in costs}
        turn = {c: annual_turnover(series[c][1].to_numpy()) for c in costs}
        ex_cash = {c: ann[c] - rf_ann for c in costs}
        ex_bh = {c: ann[c] - cagr(bh[c][0].to_numpy()) for c in costs}
        for c in costs:
            r = series[c][0]
            rows.append({"group": name, "strategy": strat, "cost_bp": c, "ann_ret_pct": ann[c] * 100,
                         "excess_cash_pct": ex_cash[c] * 100, "excess_bh_pct": ex_bh[c] * 100,
                         "sharpe": sharpe(r - group_cash(cash, r.index)), "turnover_oneway": turn[c],
                         "bh_ann_ret_pct": cagr(bh[c][0].to_numpy()) * 100, "cash_ann_pct": rf_ann * 100,
                         "n_days": len(r), "start": str(r.index[0].date()), "end": str(r.index[-1].date())})
        hi = costs[-1]
        dr = drag_ratio(ann[costs[0]], ann[hi], hi - costs[0], turn[costs[0]])
        for bench, ex in (("cash", ex_cash), ("buy_hold", ex_bh)):
            be, kind = breakeven(costs, [ex[c] for c in costs])
            row = {"group": name, "strategy": strat, "bench": bench,
                   **{f"excess_{c}bp_pct": ex[c] * 100 for c in costs},
                   "breakeven_bp": be, "kind": kind, "turnover_at_0": turn[costs[0]], "drag_ratio": dr,
                   "approx_bp": approx_breakeven_bp(ex[costs[0]], turn[costs[0]]),
                   "ci_lo": float("nan"), "ci_hi": float("nan"), "check_excess_pct": float("nan")}
            # 用真实回测在盈亏平衡成本重算一遍：残差应接近 0（插值/外推的误差）
            if kind in ("interp", "extrap") and np.isfinite(be) and be < 5000:
                r_chk, _ = group_series(insts, strat, be)
                base = cagr(group_cash(cash, r_chk.index).to_numpy()) if bench == "cash" else cagr(group_series(insts, BH, be)[0].to_numpy())
                row["check_excess_pct"] = (cagr(r_chk.to_numpy()) - base) * 100
            if bench == "cash" and boot > 0:
                R = np.column_stack([series[c][0].to_numpy() for c in costs])
                bs = block_bootstrap_breakeven(R, rf.to_numpy(), costs, boot, seed)
                row["ci_lo"], row["ci_hi"] = float(np.nanpercentile(bs, 2.5)), float(np.nanpercentile(bs, 97.5))
                row["boot_share_loss_at_zero"] = float((bs <= 0).mean())
            be_rows.append(row)
    return rows, be_rows


# ------------------------------------------------------------------ 数据：品种和个股样本
def load_etf_groups(daily: Path, digests: dict) -> dict:
    """美股、A 股 ETF 池 → {组名: (品种列表, 现金日收益)}。SPY / 510300 是各自池里的子集。"""
    out = {}
    for market in ("us", "cn"):
        mcfg = MARKETS[market]
        prices, dg = load_prices(daily, market, mcfg["universe"])
        digests.update(dg)
        cash = prices[mcfg["cash"]]["Close"].pct_change() if mcfg["cash"] in prices else None
        insts = {}
        for sym in mcfg["universe"]:
            if sym == mcfg["cash"] or sym not in prices:
                continue
            insts[sym] = Inst(prices[sym], sym, mcfg, cash, limit_pct(sym) if market == "cn" else None)
        out[market] = {"insts": insts, "cash": cash, "main": mcfg["main"]}
    return out


def sample_stocks(daily: Path, n: int, seed: int, digests: dict, cash: pd.Series | None) -> tuple[list, pd.DataFrame]:
    """cn_all 里按固定种子打乱后依次取，直到凑够 n 只。合格 = 非 ST/退、STOCK_LISTED_BY 前已上市、行情到 CUTOFF 附近。
    只含现存股票 → 有幸存者偏差（报告里写明）。"""
    lst = pd.read_csv(daily / "cn_all_list.csv", dtype=str).fillna("")
    lst = lst[~lst["name"].str.upper().str.contains("ST") & ~lst["name"].str.contains("退")]
    codes = sorted(lst["code"])
    names = dict(zip(lst["code"], lst["name"]))
    insts, rows, tried = [], [], 0
    for i in np.random.default_rng(seed).permutation(len(codes)):
        if len(insts) >= n:
            break
        code = codes[i]
        p = daily / "cn_all" / f"{code}.parquet"
        if not p.exists():
            continue
        tried += 1
        df = pd.read_parquet(p).sort_index().loc[:CUTOFF]          # 读进来立刻截断
        if len(df) < WARM + 260 or df.index[0] > pd.Timestamp(STOCK_LISTED_BY) or df.index[-1] < pd.Timestamp(CUTOFF) - pd.Timedelta(days=10):
            continue
        digests[f"cn_all/{code}.parquet"] = hashlib.sha256(p.read_bytes()).hexdigest()
        insts.append(Inst(df, code, MARKETS["cn"], cash, limit_pct(code, names[code])))
        rows.append({"code": code, "name": names[code], "first": str(df.index[0].date()), "last": str(df.index[-1].date())})
    return insts, pd.DataFrame(rows).assign(tried=tried)


# ------------------------------------------------------------------ 输出
def _f(v, nd=2):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "—"
    if isinstance(v, float) and np.isinf(v):
        return "∞"
    return f"{v:.{nd}f}"


KIND_ZH = {"interp": "", "extrap": "（外推）", "loss_at_zero": "（0成本已亏）", "flat": "（不随成本降）", "nodata": "（无数据）"}


def fmt_be(r) -> str:
    if r.kind == "loss_at_zero":
        return "0成本已亏"
    return f"{_f(r.breakeven_bp, 1)}{KIND_ZH.get(r.kind, '')}"


def markdown(metrics_df: pd.DataFrame, be_df: pd.DataFrame, meta: dict) -> str:
    L = ["# 七个正式策略的交易成本敏感性与盈亏平衡成本（C11）", "",
         f"生成时间 {meta['generated']}；窗口 {WIN_START}~{CUTOFF}（各品种实际起点见 cost_metrics.csv 的 start）；成本网格 {COSTS_BP}bp 单边；"
         f"个股 {meta['n_stocks_used']} 只（固定种子 {meta['seed']}，{STOCK_LISTED_BY} 前上市，现存股票有幸存者偏差）；自助法 {meta['boot']} 次、块长 {BLOCK}。", "",
         "净超额 = 年化收益 − 同窗口现金 ETF 年化收益。盈亏平衡 = 净超额降到 0 的单边成本（bp），线性插值；"
         "「外推」= 30bp 时仍为正，用 15、30bp 两点外推；方括号 = 自助法 95% 区间（把「0成本已亏」记 0、超过 1000bp 记 1000）。", ""]
    for g in GROUPS:
        m = metrics_df[metrics_df.group == g]
        if m.empty:
            continue
        L += [f"## {g}", "",
              f"窗口 {m.start.min()}~{m.end.max()}；现金年化 {_f(m.cash_ann_pct.iloc[0])}%；买入持有年化 {_f(m[m.cost_bp == 0].bh_ann_ret_pct.iloc[0])}%（0bp）。", "",
              "| 策略 | 年化单边换手 | " + " | ".join(f"年化% @{c}" for c in COSTS_BP) + " | " + " | ".join(f"夏普 @{c}" for c in COSTS_BP) + " |",
              "|---|---|" + "---|" * (2 * len(COSTS_BP))]
        for s in ORDER:
            q = m[m.strategy == s].set_index("cost_bp")
            L.append(f"| {s} | {_f(q.loc[0, 'turnover_oneway'])} | " + " | ".join(_f(q.loc[c, 'ann_ret_pct']) for c in COSTS_BP) + " | "
                     + " | ".join(_f(q.loc[c, 'sharpe']) for c in COSTS_BP) + " |")
        b = be_df[be_df.group == g]
        L += ["", "| 策略 | 净超额(对现金)% @0 / 8 / 15 / 30bp | 盈亏平衡(对现金) bp [95%区间] | 净超额(对买入持有)% @0 / 8 / 15 / 30bp | 盈亏平衡(对买入持有) bp | (R0−R30)/(30bp×换手) |",
              "|---|---|---|---|---|---|"]
        for s in ORDER:
            c_ = b[(b.strategy == s) & (b.bench == "cash")].iloc[0]
            h_ = b[(b.strategy == s) & (b.bench == "buy_hold")].iloc[0]
            ci = f" [{_f(c_.ci_lo, 0)}, {_f(c_.ci_hi, 0)}]" if np.isfinite(c_.ci_lo) else ""
            L.append(f"| {s} | " + " / ".join(_f(c_[f'excess_{c}bp_pct']) for c in COSTS_BP) + f" | {fmt_be(c_)}{ci} | "
                     + " / ".join(_f(h_[f'excess_{c}bp_pct']) for c in COSTS_BP) + f" | {fmt_be(h_)} | {_f(c_.drag_ratio, 3)} |")
        L.append("")
    return "\n".join(L)


COLORS = {"blend2": "#0072B2", "trend_ens": "#E69F00", "rsi2": "#009E73", "donchian_s1": "#CC79A7",
          "trend_vol": "#D55E00", "ma_adx_original": "#56B4E9", "ma_adx_plus": "#444444"}


def make_figures(metrics_df: pd.DataFrame, be_df: pd.DataFrame, out: Path) -> list[str]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams["font.sans-serif"] = ["PingFang SC", "Heiti SC", "Arial Unicode MS", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    files = []
    gs = [g for g in GROUPS if (metrics_df.group == g).any()]
    fig, axes = plt.subplots(1, len(gs), figsize=(3.6 * len(gs), 4.2), constrained_layout=True, sharey=False)
    axes = np.atleast_1d(axes)
    for ax, g in zip(axes, gs):
        m = metrics_df[metrics_df.group == g]
        for s in ORDER:
            q = m[m.strategy == s].sort_values("cost_bp")
            ax.plot(q.cost_bp, q.excess_cash_pct, marker="o", ms=3, color=COLORS[s], label=s, lw=1.6)
        ax.axhline(0, color="#555", lw=1)
        ax.set_title(g)
        ax.set_xlabel("单边成本 bp")
        ax.grid(alpha=0.25)
    axes[0].set_ylabel("净超额年化收益 %（对现金）")
    axes[0].legend(frameon=False, fontsize=7)
    fig.savefig(out / "fig_excess_vs_cost.png", dpi=140)
    plt.close(fig)
    files.append("fig_excess_vs_cost.png")

    b = be_df[(be_df.bench == "cash") & np.isfinite(be_df.breakeven_bp) & (be_df.turnover_at_0 > 0)]
    fig, ax = plt.subplots(figsize=(7, 4.6), constrained_layout=True)
    for s in ORDER:
        q = b[b.strategy == s]
        ax.scatter(q.turnover_at_0, q.breakeven_bp, color=COLORS[s], label=s, s=28)
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlabel("年化单边换手（倍净值/年，0bp）")
    ax.set_ylabel("盈亏平衡单边成本 bp")
    ax.set_title("换手越高，盈亏平衡成本越低（每个点 = 一组 × 一个策略）")
    ax.grid(alpha=0.25, which="both")
    ax.legend(frameon=False, fontsize=7)
    fig.savefig(out / "fig_breakeven_vs_turnover.png", dpi=140)
    plt.close(fig)
    files.append("fig_breakeven_vs_turnover.png")
    return files


def turnover_relation(be_df: pd.DataFrame) -> dict:
    """换手和盈亏平衡成本的关系（对现金口径，只看有有限盈亏平衡点的行）：
    pooled = 全部 组×策略 的秩相关（同时混着各策略毛超额不同的影响）；
    within_group_mean = 每组内部（同一个市场/品种，只有策略不同）的秩相关取平均；
    approx_ratio_median = 实际盈亏平衡 ÷ 一阶近似（0成本净超额÷换手）的中位数，≈1 说明近似可用。"""
    b = be_df[(be_df.bench == "cash") & np.isfinite(be_df.breakeven_bp) & (be_df.turnover_at_0 > 0)]
    per = [float(g.turnover_at_0.corr(g.breakeven_bp, method="spearman")) for _, g in b.groupby("group") if len(g) >= 4]
    ratio = (b.breakeven_bp / b.approx_bp).replace([np.inf, -np.inf], np.nan).dropna()
    return {"n": int(len(b)), "pooled_spearman": float(b.turnover_at_0.corr(b.breakeven_bp, method="spearman")),
            "within_group_mean_spearman": float(np.mean(per)) if per else float("nan"),
            "approx_ratio_median": float(ratio.median()) if len(ratio) else float("nan")}


# ------------------------------------------------------------------ 主流程
def run(data_dir: Path, out_dir: Path, n_stocks: int, seed: int, boot: int, log=print) -> dict:
    check_end(CUTOFF)
    t0 = time.time()
    daily = find_daily_dir(data_dir.resolve())
    log(f"行情目录 {daily}")
    digests: dict = {}
    etf = load_etf_groups(daily, digests)
    metrics_rows, be_rows = [], []
    members = {"SPY": ("us", ["SPY"]), "510300": ("cn", ["510300"]), "us_ew": ("us", list(etf["us"]["insts"])),
               "cn_ew": ("cn", list(etf["cn"]["insts"]))}
    for g, (mk, syms) in members.items():
        insts = [etf[mk]["insts"][s] for s in syms]
        r, b = group_table(g, insts, etf[mk]["cash"], boot=boot, seed=seed)
        metrics_rows += r
        be_rows += b
        log(f"{g} 完成（{len(insts)} 个品种，{time.time() - t0:.0f}s）")
    stocks, sample = [], pd.DataFrame()
    if n_stocks > 0:
        stocks, sample = sample_stocks(daily, n_stocks, seed, digests, etf["cn"]["cash"])
        log(f"个股样本 {len(stocks)} 只（候选已看 {int(sample.tried.iloc[0]) if len(sample) else 0}，{time.time() - t0:.0f}s）")
        if stocks:
            r, b = group_table("cn_stock", stocks, etf["cn"]["cash"], boot=boot, seed=seed)
            metrics_rows += r
            be_rows += b
            log(f"cn_stock 完成（{time.time() - t0:.0f}s）")
    metrics_df, be_df = pd.DataFrame(metrics_rows), pd.DataFrame(be_rows)
    assert metrics_df.end.max() <= CUTOFF, "有结果超过研究截止日"
    out_dir.mkdir(parents=True, exist_ok=True)
    metrics_df.to_csv(out_dir / "cost_metrics.csv", index=False)
    be_df.to_csv(out_dir / "cost_breakeven.csv", index=False)
    if len(sample):
        sample.drop(columns="tried").to_csv(out_dir / "cost_stock_sample.csv", index=False)
    meta = {"generated": time.strftime("%Y-%m-%d %H:%M:%S"), "cutoff": CUTOFF, "window_start": WIN_START, "costs_bp": COSTS_BP,
            "warm": WARM, "band": BAND, "n_stocks_requested": n_stocks, "n_stocks_used": len(stocks), "seed": seed, "boot": boot,
            "block": BLOCK, "be_cap": BE_CAP, "stock_listed_by": STOCK_LISTED_BY, "daily_dir": str(daily),
            "versions": {"pandas": pd.__version__, "numpy": np.__version__}, "input_sha256": digests}
    meta["turnover_vs_breakeven"] = turnover_relation(be_df)
    meta["figures"] = make_figures(metrics_df, be_df, out_dir)
    (out_dir / "cost_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
    (out_dir / "cost_tables.md").write_text(markdown(metrics_df, be_df, meta), encoding="utf-8")
    log(f"写出 {out_dir}（{time.time() - t0:.0f}s）")
    return {"metrics": metrics_df, "breakeven": be_df, "meta": meta}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out-dir", required=True, type=Path)
    ap.add_argument("--data-dir", type=Path, default=ROOT / "data")
    ap.add_argument("--n-stocks", type=int, default=300)
    ap.add_argument("--seed", type=int, default=6)
    ap.add_argument("--boot", type=int, default=500)
    a = ap.parse_args(argv)
    a.out_dir.mkdir(parents=True, exist_ok=True)

    def log(msg):                       # 同时打印并追加到输出目录里的 run.log
        print(msg, flush=True)
        with (a.out_dir / "run.log").open("a", encoding="utf-8") as f:
            f.write(msg + "\n")

    run(a.data_dir, a.out_dir, a.n_stocks, a.seed, a.boot, log=log)


if __name__ == "__main__":
    main()
