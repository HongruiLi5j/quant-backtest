"""C08 样本外逐年衰减：7 个正式策略，样本内（2013~2019）vs 样本外（2020~2025）每个自然年的表现。

问题：策略在样本内看起来不错，到了样本外是不是系统性变差？「衰减」有多大、统计上分得清吗？
做法（全程复用项目已有函数，不改任何策略规则）：
  1. 行情读进来立刻截到 CUTOFF（2026-03-31），之后才算信号：registry.target_weights（收盘出信号）
     → portfolio.run_weights（次日开盘成交、扣费、10% 调仓阈值、空仓现金计息、A 股一字板），口径同 C04 / registry.run。
     每个品种前 260 根作预热，不进统计（与 registry.run 相同）。
  2. 四组对象：SPY、510300（两个市场的主信号品种）、us_ew / cn_ew（各市场 ETF 池里除现金外的品种，策略各自独立跑，
     日收益等权平均，品种当天有行情才计入；品种池随上市时间变化，见报告局限）。
  3. 每个自然年 2013~2025（2026 年只有一季度，只在 CSV 里作参考行，不进图和检验）算：
       年化收益 = (年内日收益连乘)^(252/天数) − 1（整年 ≈ 当年收益）；
       夏普     = 日超额收益（策略 − 现金 ETF：美股 SHY、A 股 511880）均值 / 标准差 × √252，同 metrics.sharpe；
                  整年空仓（收益恰好等于现金，标准差为 0）夏普记 0 并标 flat，不丢弃（丢弃会让样本外均值偏低）；
       最大回撤 = 年内净值曲线（年初 = 1）的最大回撤；
       超额     = 策略年化收益 − 基准年化收益。基准 = 同一市场的买入持有：SPY 组用 SPY、510300 组用 510300（沪深 300 ETF），
                  ew 组用同一批品种的等权买入持有。数据里有 510300，所以 A 股基准是沪深 300；美股不拿沪深 300 比，
                  货币和市场都不同，没有意义。
     交易日少于 MIN_DAYS（200）的年份（A 股品种上市晚 + 预热期 → 2013 年）标记为不完整，只展示，不进检验。
  4. 检验：样本内年份（2013~2019）vs 样本外年份（2020~2025）的 Welch t 检验（不假设两段方差相等），
     对 年化收益 / 夏普 / 超额 三个指标各做一次；同时给精确置换检验（把年份标签打乱所有可能的分法）当作不依赖正态假设的对照。
     「衰减」是单边假设（样本内均值 > 样本外均值），同时给双边 p。7 个策略之间用 Holm 校正。
     功效（power）：n 只有 6~7 个年份，给出在 80% 功效下能检出的最小差异（MDE）和需要多少年才能检出 1 个标准差的差异。
     另有 avg7 行（7 个策略的年度指标先平均再检验）和买入持有参照行（看市场本身有没有变）。

只读 data/ 下的行情，不写 data/；不联网。

用法：
  .venv/bin/python -B scripts/oos_decay.py --out-dir DIR [--data-dir data]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
from functools import lru_cache
from itertools import combinations
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats as sps
from scipy.optimize import brentq

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from config import MARKETS  # noqa: E402
from src import registry  # noqa: E402
from src.datasource import limit_pct  # noqa: E402
from src.metrics import TRADING_DAYS  # noqa: E402
from src.portfolio import run_weights  # noqa: E402
from src.scan import STRATS  # noqa: E402  7 个正式策略，和全市场扫描、信号账本同一份清单
from deflated_sharpe_table import check_end, find_daily_dir, load_prices  # noqa: E402  C04 里的读行情/截止日检查

CUTOFF = "2026-03-31"                       # 队列红线：任何研究的结束日期不得晚于此
YEARS = list(range(2013, 2026))             # 2013~2025
IS_YEARS = list(range(2013, 2020))          # 样本内 2013~2019（与 C04 的划分一致）
OOS_YEARS = list(range(2020, 2026))         # 样本外 2020~2025
WARM = 260                                  # 预热期根数，与 registry.run 相同
BAND = 0.10                                 # 调仓阈值，与 registry.run / scan.py 相同
MIN_DAYS = 200                              # 一年里至少这么多个交易日才算「完整年」，进检验
ORDER = ["blend2", "trend_ens", "rsi2", "donchian_s1", "trend_vol", "ma_adx_original", "ma_adx_plus"]
BH = "buy_hold"                             # 基准 / 参照，不进 7 个正式策略
METRICS = ["ann_ret", "sharpe", "excess"]   # 做检验的指标（最大回撤只展示）
ALPHA = 0.05
POWER = 0.80
GROUPS = ["SPY", "510300", "us_ew", "cn_ew"]
assert sorted(ORDER) == sorted(STRATS)


# ------------------------------------------------------------------ 年度指标（纯函数，测试里单独验）
def year_stats(ret: pd.Series, rf: pd.Series, bench: pd.Series) -> dict:
    """一个自然年的指标。ret：策略日总收益（已扣费）；rf：同日现金日收益；bench：基准日总收益。三者同索引。"""
    n = len(ret)
    ann = lambda r: float((1.0 + r).prod() ** (TRADING_DAYS / n) - 1.0) if n else np.nan   # noqa: E731
    ex = ret - rf
    sd = ex.std()
    nav = (1.0 + ret).cumprod()
    peak = np.maximum.accumulate(np.concatenate([[1.0], nav.to_numpy()]))[1:]    # 年初净值 = 1 也算峰值
    mdd = float((nav.to_numpy() / peak - 1.0).min()) if n else np.nan
    a, b = ann(ret), ann(bench)
    flat = bool(n > 1 and sd == 0)         # 整年空仓：日收益恰好等于现金收益，超额收益恒为 0，标准差 0，夏普没有定义
    sharpe = 0.0 if flat else (float(ex.mean() / sd * math.sqrt(TRADING_DAYS)) if n > 1 and sd > 0 else np.nan)   # 空仓年记 0，不丢弃
    return {"n_days": n, "ann_ret": a, "sharpe": sharpe, "mdd": mdd, "bench_ret": b, "excess": a - b, "flat": flat}


def yearly_table(ret: pd.Series, rf: pd.Series, bench: pd.Series, last_year: int = 2026) -> pd.DataFrame:
    """按自然年切开逐年算。最后一年（2026，只有一季度）打上 partial 标记。"""
    rows = []
    for y in range(YEARS[0], last_year + 1):
        m = ret.index.year == y
        if not m.any():
            continue
        s = year_stats(ret[m], rf[m], bench[m])
        s |= {"year": y, "period": "IS" if y in IS_YEARS else ("OOS" if y in OOS_YEARS else "ref"),
              "complete": bool(s["n_days"] >= MIN_DAYS and y <= YEARS[-1])}
        rows.append(s)
    return pd.DataFrame(rows)


# ------------------------------------------------------------------ 检验（纯函数）
def welch(a: np.ndarray, b: np.ndarray) -> dict:
    """Welch t 检验：a = 样本内，b = 样本外。diff = mean(a) − mean(b)；p_decay 是单边（a 的均值 > b 的均值）。"""
    a, b = np.asarray(a, float), np.asarray(b, float)
    n1, n2 = len(a), len(b)
    out = {"n_is": n1, "n_oos": n2, "mean_is": float(a.mean()) if n1 else np.nan, "mean_oos": float(b.mean()) if n2 else np.nan}
    out["diff"] = out["mean_is"] - out["mean_oos"]
    nan = {"se": np.nan, "t": np.nan, "df": np.nan, "p_two": np.nan, "p_decay": np.nan, "cohen_d": np.nan}
    if n1 < 2 or n2 < 2:
        return out | nan
    v1, v2 = a.var(ddof=1), b.var(ddof=1)
    se2 = v1 / n1 + v2 / n2
    if se2 <= 0:
        return out | nan
    t = out["diff"] / math.sqrt(se2)
    df = se2 ** 2 / ((v1 / n1) ** 2 / (n1 - 1) + (v2 / n2) ** 2 / (n2 - 1))     # Welch–Satterthwaite 自由度
    sp = math.sqrt(((n1 - 1) * v1 + (n2 - 1) * v2) / (n1 + n2 - 2))
    return out | {"se": math.sqrt(se2), "t": t, "df": df, "p_two": float(2 * sps.t.sf(abs(t), df)),
                  "p_decay": float(sps.t.sf(t, df)), "cohen_d": out["diff"] / sp if sp > 0 else np.nan}


def _welch_t(a: np.ndarray, b: np.ndarray) -> float:
    se2 = a.var(ddof=1) / len(a) + b.var(ddof=1) / len(b)
    return (a.mean() - b.mean()) / math.sqrt(se2) if se2 > 0 else 0.0


def perm_pvalues(a: np.ndarray, b: np.ndarray) -> tuple[float, float]:
    """精确置换检验：把 n1+n2 个年份值的「样本内/样本外」标签所有可能的分法都试一遍（C(13,7)=1716 种），
    看真实的 Welch t 在其中有多极端。返回 (双边 p, 单边 p_decay)。零假设 = 年份可交换（没有时间趋势/衰减）。"""
    a, b = np.asarray(a, float), np.asarray(b, float)
    n1, n = len(a), len(a) + len(b)
    if n1 < 2 or len(b) < 2:
        return np.nan, np.nan
    pool = np.concatenate([a, b])
    t0 = _welch_t(a, b)
    ts = np.array([_welch_t(pool[list(ix)], np.delete(pool, list(ix))) for ix in combinations(range(n), n1)])
    eps = 1e-9
    return float((np.abs(ts) >= abs(t0) - eps).mean()), float((ts >= t0 - eps).mean())


def holm(pvals: list[float]) -> list[float]:
    """Holm–Bonferroni 校正后的 p（nan 不参与也不占名额）。"""
    p = np.asarray(pvals, float)
    ok = np.isfinite(p)
    out = np.full(len(p), np.nan)
    idx = np.where(ok)[0][np.argsort(p[ok])]
    m, run = len(idx), 0.0
    for rank, i in enumerate(idx):
        run = max(run, min(1.0, (m - rank) * p[i]))
        out[i] = run
    return out.tolist()


def power_two_sample(d: float, n1: int, n2: int, alpha: float = ALPHA) -> float:
    """标准化差异 d（差异 / 合并标准差）下，双边两样本 t 检验的功效（等方差近似，非中心 t 分布）。"""
    df = n1 + n2 - 2
    ncp = d * math.sqrt(n1 * n2 / (n1 + n2))
    tc = sps.t.ppf(1 - alpha / 2, df)
    p = float(sps.nct.sf(tc, df, ncp) + sps.nct.cdf(-tc, df, ncp))
    return p if np.isfinite(p) else (1.0 if ncp > 3 else 0.0)      # scipy 的非中心 t 在个别大参数上会给 nan；那里功效本来就≈1


@lru_cache(maxsize=None)
def min_detectable_d(n1: int, n2: int, power: float = POWER, alpha: float = ALPHA) -> float:
    """功效达到 power 所需的最小标准化差异 d（MDE）。"""
    return float(brentq(lambda d: power_two_sample(d, n1, n2, alpha) - power, 1e-6, 6.0))


def n_needed(d: float, power: float = POWER, alpha: float = ALPHA) -> int:
    """每组各需要多少个年份，才能以 power 的功效检出标准化差异 d。"""
    for n in range(2, 5000):
        if power_two_sample(d, n, n, alpha) >= power:
            return n
    return 5000


def test_rows(group: str, name: str, table: pd.DataFrame) -> list[dict]:
    """一个 (组, 策略) 在三个指标上的检验结果。只用 complete 的年份。"""
    t = table[table["complete"]]
    rows = []
    for met in METRICS:
        a = t.loc[t["period"] == "IS", met].dropna().to_numpy()
        b = t.loc[t["period"] == "OOS", met].dropna().to_numpy()
        if met == "excess" and name == BH:        # 买入持有对自己的超额恒为 0，没有检验意义
            continue
        w = welch(a, b)
        pp2, ppd = perm_pvalues(a, b)
        n1, n2 = w["n_is"], w["n_oos"]
        sd_pool = (w["diff"] / w["cohen_d"]) if np.isfinite(w["cohen_d"]) and w["cohen_d"] != 0 else np.nan
        mde_d = min_detectable_d(n1, n2) if n1 >= 2 and n2 >= 2 else np.nan
        rows.append({"group": group, "strategy": name, "metric": met, **w, "p_perm_two": pp2, "p_perm_decay": ppd,
                     "mde_d": mde_d, "mde_raw": mde_d * sd_pool if np.isfinite(sd_pool) else np.nan,
                     "power_d1": power_two_sample(1.0, n1, n2) if n1 >= 2 and n2 >= 2 else np.nan})
    return rows


def add_holm(tests: pd.DataFrame) -> pd.DataFrame:
    """在每个 (组, 指标) 里，对 7 个正式策略的单边 p_decay 做 Holm 校正（avg7 和买入持有不计入 7 个）。"""
    tests = tests.copy()
    tests["p_decay_holm"] = np.nan
    for (g, met), sub in tests[tests["strategy"].isin(ORDER)].groupby(["group", "metric"]):
        tests.loc[sub.index, "p_decay_holm"] = holm(sub["p_decay"].tolist())
    return tests


# ------------------------------------------------------------------ 单品种回测
def instrument_returns(df: pd.DataFrame, symbol: str, mcfg: dict, cash: pd.Series | None, cost: float,
                       limit: float | None) -> dict:
    """一个品种：7 个正式策略 + 买入持有的日总收益（已扣费），去掉前 WARM 根预热。df 必须已截到 CUTOFF。"""
    check_end(str(df.index[-1].date()))
    prices = {symbol: df}
    lim = {symbol: limit} if limit else None
    out = {}
    for name in [*ORDER, BH]:
        if name == BH:
            W = pd.Series(1.0, index=df.index, name=symbol).to_frame()
        else:
            W = registry.target_weights(name, prices, symbol, registry.clean_params(name, {}), mcfg)
        res = run_weights(prices, W, cost=cost, band=0.0 if name == BH else BAND, cash_ret=cash, limits=lim)
        out[name] = res.returns
    idx = out[ORDER[0]].index
    start = idx[min(WARM, len(idx) - 1)]
    return {k: v.loc[start:pd.Timestamp(CUTOFF)] for k, v in out.items()}


def market_groups(daily: Path, market: str, digests: dict) -> dict:
    """一个市场 → {组名: {策略: 日总收益}, "_rf": 现金日收益}。组 = 主品种 + 等权组合。"""
    mcfg = MARKETS[market]
    prices, dg = load_prices(daily, market, mcfg["universe"])
    digests |= dg
    cash = prices[mcfg["cash"]]["Close"].pct_change() if mcfg["cash"] in prices else None
    cost = mcfg["cost_bp"] / 1e4
    per = {}
    for sym in mcfg["universe"]:
        if sym == mcfg["cash"] or sym not in prices:
            continue
        per[sym] = instrument_returns(prices[sym], sym, mcfg, cash, cost, limit_pct(sym) if market == "cn" else None)
    ew_name = f"{market}_ew"
    ew = {n: pd.DataFrame({s: per[s][n] for s in per}).mean(axis=1, skipna=True) for n in [*ORDER, BH]}   # 当天有行情的品种等权
    groups = {mcfg["main"]: per[mcfg["main"]], ew_name: ew}
    rf = (cash if cash is not None else pd.Series(0.0, index=ew[BH].index)).fillna(0.0)
    return {"groups": groups, "rf": rf, "n_inst": len(per), "instruments": sorted(per)}


# ------------------------------------------------------------------ 汇总
def analyse_group(group: str, rets: dict, rf: pd.Series) -> tuple[pd.DataFrame, list[dict]]:
    """一个组：逐年长表 + 检验行（含 avg7 和买入持有参照）。"""
    bench = rets[BH]
    frames = {}
    for name in [*ORDER, BH]:
        r = rets[name]
        idx = r.index
        frames[name] = yearly_table(r, rf.reindex(idx).fillna(0.0), bench.reindex(idx).fillna(0.0))
    # avg7：7 个策略的年度指标逐年平均（complete 取并，7 个策略起止相同，所以一致）
    base = frames[ORDER[0]][["year", "period", "complete", "n_days"]].copy()
    avg = base.copy()
    for met in ["ann_ret", "sharpe", "mdd", "bench_ret", "excess"]:
        avg[met] = np.mean([frames[n].set_index("year")[met].reindex(avg["year"]).to_numpy() for n in ORDER], axis=0)
    avg["flat"] = np.all([frames[n].set_index("year")["flat"].reindex(avg["year"]).to_numpy(bool) for n in ORDER], axis=0)
    frames["avg7"] = avg
    long, tests = [], []
    for name, tab in frames.items():
        long.append(tab.assign(group=group, strategy=name))
        tests += test_rows(group, name, tab)
    return pd.concat(long, ignore_index=True), tests


# ------------------------------------------------------------------ 输出：表
def _f(v, nd=2, pct=False):
    if v is None or (isinstance(v, float) and not np.isfinite(v)):
        return "—"
    return f"{v * 100:.{nd}f}%" if pct else f"{v:.{nd}f}"


def markdown(yearly: pd.DataFrame, tests: pd.DataFrame, meta: dict) -> str:
    L = ["# 样本外逐年衰减（C08）", "",
         f"生成时间 {meta['generated']}；样本内 {IS_YEARS[0]}~{IS_YEARS[-1]}，样本外 {OOS_YEARS[0]}~{OOS_YEARS[-1]}"
         f"（2026 年只有一季度，只在 CSV 里作参考）；收益口径：次日开盘成交、单边 5bp、10% 调仓带、空仓按现金 ETF 计息。", ""]
    names = {"SPY": "SPY（美股，基准 = SPY 买入持有）", "510300": "510300（沪深 300 ETF，基准 = 510300 买入持有）",
             "us_ew": "美股 ETF 等权组合（基准 = 同批 ETF 等权买入持有）", "cn_ew": "A 股 ETF 等权组合（基准 = 同批 ETF 等权买入持有）"}
    for g in GROUPS:
        L += [f"## {names[g]}", ""]
        t = tests[tests["group"] == g]
        for met, title in (("ann_ret", "年化收益"), ("sharpe", "夏普（相对现金的日超额收益）"), ("excess", "相对基准的年度超额收益")):
            pct = met != "sharpe"
            L += [f"### 检验：{title}（样本内 vs 样本外，Welch t；p_decay = 单边「样本内 > 样本外」）", "",
                  "| 策略 | 样本内均值 | 样本外均值 | 差 | t | df | p 双边 | p_decay | p_decay(Holm) | p 置换双边 | 年数 IS/OOS | 80%功效 MDE |",
                  "|---|---|---|---|---|---|---|---|---|---|---|---|"]
            for _, r in t[t["metric"] == met].iterrows():
                L.append(f"| {r['strategy']} | {_f(r['mean_is'], 2, pct)} | {_f(r['mean_oos'], 2, pct)} | {_f(r['diff'], 2, pct)} | "
                         f"{_f(r['t'])} | {_f(r['df'], 1)} | {_f(r['p_two'], 3)} | {_f(r['p_decay'], 3)} | {_f(r['p_decay_holm'], 3)} | "
                         f"{_f(r['p_perm_two'], 3)} | {int(r['n_is'])}/{int(r['n_oos'])} | {_f(r['mde_raw'], 2, pct)} |")
            L.append("")
        for met, title in (("ann_ret", "年化收益"), ("sharpe", "夏普"), ("mdd", "最大回撤"), ("excess", "相对基准超额")):
            pct = met != "sharpe"
            sub = yearly[(yearly["group"] == g) & (yearly["year"] <= YEARS[-1])]
            piv = sub.pivot(index="strategy", columns="year", values=met).reindex([*ORDER, "avg7", BH])
            if met == "excess":
                piv = piv.drop(index=BH)
            L += [f"### 逐年：{title}", "", "| 策略 | " + " | ".join(str(y) for y in piv.columns) + " |", "|---" * (len(piv.columns) + 1) + "|"]
            for s, row in piv.iterrows():
                L.append(f"| {s} | " + " | ".join(_f(float(v), 2, pct) for v in row) + " |")
            L.append("")
        part = yearly[(yearly["group"] == g) & (yearly["strategy"] == "blend2") & (~yearly["complete"]) & (yearly["year"] <= YEARS[-1])]
        if len(part):
            L += [f"> 不完整年份（交易日 < {MIN_DAYS}，只展示、不进检验）：" + "、".join(f"{int(r.year)}（{int(r.n_days)} 天）" for r in part.itertuples()), ""]
    return "\n".join(L)


# ------------------------------------------------------------------ 输出：图
def make_figures(yearly: pd.DataFrame, tests: pd.DataFrame, out: Path) -> list[str]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams["font.sans-serif"] = ["PingFang SC", "Heiti SC", "Arial Unicode MS", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    col = {"IS": "#1b6ca8", "OOS": "#e08a1e"}          # 样本内蓝、样本外橙（色盲下亮度也有差）
    meta = {"ann_ret": ("年化收益", True), "sharpe": ("夏普", False), "mdd": ("最大回撤", True), "excess": ("相对基准超额收益", True)}
    sets = {"main": ["SPY", "510300"], "ew": ["us_ew", "cn_ew"]}
    files = []
    for sname, groups in sets.items():
        for met, (title, pct) in meta.items():
            fig, axes = plt.subplots(len(ORDER), len(groups), figsize=(6.4 * len(groups), 1.55 * len(ORDER) + 1.2),
                                     sharex=True, constrained_layout=True, squeeze=False)
            for j, g in enumerate(groups):
                for i, s in enumerate(ORDER):
                    ax = axes[i, j]
                    d = yearly[(yearly["group"] == g) & (yearly["strategy"] == s) & (yearly["year"] <= YEARS[-1])]
                    v = d[met].to_numpy() * (100 if pct else 1)
                    ok = d["complete"].to_numpy()
                    ax.bar(d["year"], v, width=0.7, color=[col[p] for p in d["period"]], alpha=1.0)
                    for y, vv, o in zip(d["year"], v, ok):             # 不完整年份：画成空心，提醒不进检验
                        if not o:
                            ax.bar(y, vv, width=0.7, color="white", edgecolor=col["IS" if y in IS_YEARS else "OOS"], hatch="///", lw=0.8)
                    for per, ys in (("IS", IS_YEARS), ("OOS", OOS_YEARS)):
                        m = d[(d["period"] == per) & d["complete"]][met].mean() * (100 if pct else 1)
                        if np.isfinite(m):
                            ax.hlines(m, ys[0] - 0.4, ys[-1] + 0.4, colors="#222", lw=1.4, linestyles="--")
                    ax.axvline(2019.5, color="#555", lw=1.0)
                    ax.axhline(0, color="#999", lw=0.6)
                    ax.set_ylabel(s, rotation=0, ha="right", va="center", fontsize=8)
                    ax.tick_params(labelsize=7)
                    ax.grid(axis="y", alpha=0.2)
                    for sp in ("top", "right"):
                        ax.spines[sp].set_visible(False)
                axes[0, j].set_title(g, fontsize=10)
                axes[-1, j].set_xticks(YEARS, [str(y)[2:] for y in YEARS])
            unit = "%" if pct else ""
            fig.suptitle(f"{title}（{unit or '无单位'}）逐年｜蓝 = 样本内 2013–2019，橙 = 样本外 2020–2025，竖线 = 分界，"
                         f"虚线 = 各段均值，斜线空心 = 不完整年份（不进均值）", fontsize=9)
            fn = f"fig_yearly_{met}_{sname}.png"
            fig.savefig(out / fn, dpi=130)
            plt.close(fig)
            files.append(fn)
    # 哑铃图：样本内均值 → 样本外均值（夏普和超额），含 Welch 单边 p
    for met in ("sharpe", "excess"):
        fig, axes = plt.subplots(1, len(GROUPS), figsize=(4.0 * len(GROUPS), 4.2), sharey=True, constrained_layout=True)
        for ax, g in zip(axes, GROUPS):
            t = tests[(tests["group"] == g) & (tests["metric"] == met)].set_index("strategy")
            rows = [*ORDER, "avg7"] if met == "excess" else [*ORDER, "avg7", BH]
            for k, s in enumerate(rows):
                if s not in t.index:
                    continue
                r = t.loc[s]
                f = 100 if met != "sharpe" else 1
                ax.plot([r["mean_is"] * f, r["mean_oos"] * f], [k, k], color="#888", lw=1.2, zorder=1)
                ax.scatter(r["mean_is"] * f, k, color=col["IS"], s=36, zorder=2)
                ax.scatter(r["mean_oos"] * f, k, color=col["OOS"], s=36, zorder=2)
            ax.set_yticks(range(len(rows)), rows, fontsize=8)
            ax.invert_yaxis()
            ax.axvline(0, color="#999", lw=0.6)
            ax.set_title(g, fontsize=10)
            ax.grid(axis="x", alpha=0.2)
            ax.set_xlabel("夏普" if met == "sharpe" else "超额收益（%/年）", fontsize=8)
        fig.suptitle("样本内均值（蓝）→ 样本外均值（橙）｜" + ("年度夏普" if met == "sharpe" else "年度相对基准超额收益"), fontsize=10)
        fn = f"fig_dumbbell_{met}.png"
        fig.savefig(out / fn, dpi=130)
        plt.close(fig)
        files.append(fn)
    return files


# ------------------------------------------------------------------ 主流程
def run(data_dir: Path, out_dir: Path, log=print) -> dict:
    check_end(CUTOFF)
    t0 = time.time()
    daily = find_daily_dir(data_dir.resolve())
    log(f"行情目录 {daily}")
    digests: dict = {}
    yearly, tests, info = [], [], {}
    for market in ("us", "cn"):
        mg = market_groups(daily, market, digests)
        info[market] = {"n_inst": mg["n_inst"], "instruments": mg["instruments"]}
        for g, rets in mg["groups"].items():
            y, t = analyse_group(g, rets, mg["rf"])
            yearly.append(y)
            tests += t
            log(f"  {g}: 逐年 {len(y)} 行 ({time.time() - t0:.0f}s)")
    yearly = pd.concat(yearly, ignore_index=True)
    tests = add_holm(pd.DataFrame(tests))
    out_dir.mkdir(parents=True, exist_ok=True)
    yearly.to_csv(out_dir / "oos_decay_yearly.csv", index=False)
    tests.to_csv(out_dir / "oos_decay_tests.csv", index=False)
    # 功效：不同年数下的 MDE 和检出 d=1 所需年数
    pw = pd.DataFrame([{"n_is": a, "n_oos": b, "mde_d_80pct": min_detectable_d(a, b), "power_d1": power_two_sample(1.0, a, b),
                        "power_d0_5": power_two_sample(0.5, a, b)} for a, b in ((7, 6), (6, 6), (7, 7), (10, 10), (20, 20))])
    pw["years_each_needed_d1"] = n_needed(1.0)
    pw["years_each_needed_d0_5"] = n_needed(0.5)
    pw.to_csv(out_dir / "oos_decay_power.csv", index=False)
    figs = make_figures(yearly, tests, out_dir)
    meta = {"generated": time.strftime("%Y-%m-%d %H:%M:%S"), "cutoff": CUTOFF, "is_years": IS_YEARS, "oos_years": OOS_YEARS,
            "warm": WARM, "band": BAND, "min_days": MIN_DAYS, "markets": info, "daily_dir": str(daily),
            "inputs_digest": hashlib.sha256(json.dumps(digests, sort_keys=True).encode()).hexdigest(), "n_input_files": len(digests),
            "figures": figs, "versions": {"pandas": pd.__version__, "numpy": np.__version__}}
    (out_dir / "oos_decay_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / "oos_decay_tables.md").write_text(markdown(yearly, tests, meta), encoding="utf-8")
    log(f"写出 {out_dir}（{time.time() - t0:.0f}s）")
    return {"yearly": yearly, "tests": tests, "power": pw}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out-dir", required=True, type=Path)
    ap.add_argument("--data-dir", type=Path, default=ROOT / "data")
    a = ap.parse_args(argv)
    run(a.data_dir, a.out_dir)


if __name__ == "__main__":
    main()
