"""策略研究报告：一条命令跑完所有检验，输出 results/research/report.md + 图表。

  python research.py                 # 下载数据（第一次）并跑全部检验
  python research.py --refresh       # 重新下载最新数据
  python research.py --synthetic     # 用随机数据测试流程（结果没有意义）

检验流程（顺序很重要）：
  1. 主结果：所有策略在 SPY 上，分 全区间 / 样本内 / 样本外 三段
  2. 跨品种：同样的规则、同样的参数，换到 QQQ、IWM、EFA、GLD、TLT 上还有效吗？
  3. 参数稳健性：参数附近一片都好 → 可信；只有一个点好 → 过拟合
  4. 交易成本：手续费+滑点翻倍、翻五倍，策略还活着吗？
  5. Bootstrap：Sharpe 的置信区间有多宽？比买入持有好是不是运气？
  6. Walk-forward：每年只用过去的数据选参数，模拟真实使用
"""
from __future__ import annotations

import argparse
import itertools
import warnings
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
warnings.filterwarnings("ignore", message="Glyph .* missing")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from src import data, metrics
from src import strategies as S
from src.portfolio import run_weights

OUT = Path(__file__).resolve().parent / "results" / "research"
UNIVERSE = ["SPY", "QQQ", "IWM", "EFA", "GLD", "TLT", "IEF", "SHY"]
CROSS_ASSETS = ["SPY", "QQQ", "IWM", "EFA", "GLD", "TLT"]
COST = 0.0005     # 单边 0.05%（手续费 + 滑点），ETF 比较现实的估计
BAND = 0.10       # 仓位变化小于 10% 不调
CASH = {"ret": None}  # 空仓现金的日收益（main 里用 SHY 填上；--no-cash 则为 None）

# 名称 → (函数, 稳健性检验用的两个参数网格)
SINGLE = {
    "trend_vol":       (S.trend_vol_target,  {"sma_len": [100, 150, 200, 250, 300],
                                              "buffer": [0.0, 0.01, 0.02, 0.03]}),
    "donchian":        (S.donchian_breakout, {"entry_len": [20, 40, 55, 80, 100],
                                              "atr_mult": [2.0, 2.5, 3.0, 4.0, 5.0]}),
    "rsi2":            (S.rsi2_reversion,    {"entry_th": [5, 10, 15, 20, 25],
                                              "exit_ma": [3, 5, 7, 10]}),
    "ma_adx_plus":     (S.ma_adx_plus,       {"fast": [3, 5, 8, 10],
                                              "slow": [20, 30, 50, 100]}),
    "ma_adx_original": (S.ma_adx_original,   None),
    "trend_ens":       (S.trend_ensemble,    {"target_vol": [0.10, 0.12, 0.15, 0.20],
                                              "vol_len": [10, 20, 40, 60]}),
    "donchian_s1":     (S.donchian_s1,       {"entry_len": [10, 15, 20, 30],
                                              "exit_len": [5, 10, 15]}),
}
LABELS = {
    "buy_hold": "买入持有",
    "trend_vol": "趋势+波动率目标",
    "donchian": "唐奇安突破",
    "rsi2": "RSI(2) 均值回归",
    "ma_adx_plus": "MA+ADX 改进版",
    "ma_adx_original": "MA+ADX 原版",
    "blend": "组合 50%趋势+50%回归",
    "trend_ens": "趋势集成(4条均线)",
    "donchian_s1": "海龟系统1(20/10)",
    "blend2": "组合2 趋势集成+RSI2+海龟",
    "dual_momentum": "双动量轮动",
}
# 分类色板（固定顺序，不循环）；买入持有用中性灰
PALETTE = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7"]
GRAY = "#8a8a85"

plt.rcParams.update({
    "font.sans-serif": ["PingFang SC", "Heiti SC", "Arial Unicode MS", "Noto Sans CJK SC",
                        "WenQuanYi Zen Hei", "DejaVu Sans"],
    "axes.unicode_minus": False,
    "axes.spines.top": False, "axes.spines.right": False,
    "axes.grid": True, "grid.color": "#e6e6e3", "grid.linewidth": 0.6,
    "axes.edgecolor": "#b5b5b0", "axes.labelcolor": "#444", "xtick.color": "#666",
    "ytick.color": "#666", "lines.linewidth": 1.4,
})


# ------------------------------------------------------------------ 工具
def md_table(df: pd.DataFrame, floatfmt: str = "{:.2f}") -> str:
    """不依赖 tabulate 的 markdown 表格。"""
    def f(v, col=""):
        if v is None or (not isinstance(v, str) and pd.isna(v)):
            return "—"
        if col == "Trades":
            return f"{int(v)}"
        if isinstance(v, (float, np.floating)):
            return "—" if np.isnan(v) else ("∞" if np.isinf(v) else floatfmt.format(v))
        return str(v)
    cols = [df.index.name or ""] + list(map(str, df.columns))
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for idx, row in df.iterrows():
        lines.append("| " + " | ".join([str(idx)] + [f(v, c) for c, v in row.items()]) + " |")
    return "\n".join(lines)


def run_single(name: str, df: pd.DataFrame, sym: str, cost=COST, **params):
    fn = SINGLE[name][0]
    return run_weights({sym: df}, fn(df, **params).rename(sym).to_frame(), cost=cost, band=BAND,
                       cash_ret=CASH["ret"])


def buy_hold(df: pd.DataFrame, sym: str, cost=COST):
    return run_weights({sym: df}, pd.Series(1.0, index=df.index, name=sym).to_frame(), cost=cost)


def block_bootstrap_idx(n: int, B: int, L: int, rng) -> np.ndarray:
    """循环块 bootstrap：每次抽若干段长度为 L 的连续日期拼起来，保留收益的短期相关性。"""
    k = int(np.ceil(n / L))
    starts = rng.integers(0, n, size=(B, k))
    idx = (starts[:, :, None] + np.arange(L)[None, None, :]) % n
    return idx.reshape(B, -1)[:, :n]


# ------------------------------------------------------------------ 数据
def load_prices(a) -> dict[str, pd.DataFrame]:
    if a.synthetic:
        out = {}
        for i, s in enumerate(UNIVERSE):
            df = data.synthetic(n=5400, seed=i + 1, start=a.start)
            out[s] = df
        return out
    return {s: data.load(s, a.start, None, a.refresh) for s in UNIVERSE}


# ------------------------------------------------------------------ 1. 主结果
def section_main(P, is_end, report):
    spy = P["SPY"]
    res = {"buy_hold": buy_hold(spy, "SPY")}
    for name in SINGLE:
        res[name] = run_single(name, spy, "SPY")
    w_blend = S.blend(S.trend_vol_target(spy), S.rsi2_reversion(spy)).rename("SPY").to_frame()
    res["blend"] = run_weights({"SPY": spy}, w_blend, cost=COST, band=BAND, cash_ret=CASH["ret"])
    w_blend2 = S.blend(S.trend_ensemble(spy), S.rsi2_reversion(spy), S.donchian_s1(spy))
    res["blend2"] = run_weights({"SPY": spy}, w_blend2.rename("SPY").to_frame(), cost=COST,
                                band=BAND, cash_ret=CASH["ret"])
    res["dual_momentum"] = run_weights(P, S.dual_momentum(P), cost=COST, cash_ret=CASH["ret"])

    # 所有策略从同一天开始比（等最长的 252 日回看期预热完）
    common = res["dual_momentum"].returns.index
    for r in res.values():
        common = common.intersection(r.returns.index)
    start = common[260]
    periods = {"全区间": (start, common[-1]),
               "样本内": (start, pd.Timestamp(is_end)),
               "样本外": (pd.Timestamp(is_end) + pd.Timedelta(days=1), common[-1])}

    tables = {}
    for pname, (a, b) in periods.items():
        rows = {}
        for k, r in res.items():
            rows[LABELS[k]] = metrics.stats(r.returns.loc[a:b], r.trades, r.weights, rf=CASH["ret"])
        tables[pname] = pd.DataFrame(rows).T
        tables[pname].index.name = "策略"

    report.append(f"## 1. 主结果（SPY，{start.date()} → {common[-1].date()}）\n")
    report.append(f"样本内：{start.date()} → {is_end}；样本外：{is_end} 之后。"
                  "所有参数都是文献默认值，**没有在任何一段数据上优化过**。\n")
    report.append("**ExSharpe（超额 Sharpe）= 扣掉无风险利率（短债收益）后的 Sharpe，是业界标准口径**；"
                  "Sharpe 列是不扣的旧口径，只作对照。空仓多的策略，两者差得最多。\n")
    cols = ["CAGR%", "Vol%", "ExSharpe", "Sharpe", "Sortino", "MaxDD%", "Calmar", "Exposure%",
            "Trades", "Win%", "PF", "AvgHold"]
    for pname, t in tables.items():
        report.append(f"### {pname}\n")
        report.append(md_table(t[[c for c in cols if c in t]]) + "\n")

    # 图：净值（对数坐标）+ 回撤
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 8), sharex=True,
                                   gridspec_kw={"height_ratios": [3, 1.4]})
    order = ["trend_vol", "trend_ens", "donchian_s1", "rsi2", "blend2", "dual_momentum"]
    for i, k in enumerate(["buy_hold"] + order):
        r = res[k].returns.loc[start:]
        eq = (1 + r).cumprod()
        color = GRAY if k == "buy_hold" else PALETTE[i - 1]
        lw = 1.2 if k == "buy_hold" else 1.6
        ax1.plot(eq.index, eq, color=color, lw=lw, label=LABELS[k])
        ax1.annotate(LABELS[k], (eq.index[-1], eq.iloc[-1]), xytext=(4, 0),
                     textcoords="offset points", fontsize=8, color="#333", va="center")
        if k in ("buy_hold", "trend_ens", "blend2", "dual_momentum"):
            dd = (eq / eq.cummax() - 1) * 100
            ax2.plot(dd.index, dd, color=color, lw=1.0, label=LABELS[k])
    ax1.set_yscale("log")
    ax1.axvline(pd.Timestamp(is_end), color="#999", ls="--", lw=0.8)
    ax1.text(pd.Timestamp(is_end), ax1.get_ylim()[1], " 样本外 →", fontsize=8, va="top", color="#666")
    ax1.set_ylabel("净值（对数）")
    ax1.legend(loc="upper left", fontsize=8, frameon=False)
    ax1.set_title("SPY：各策略净值", loc="left", fontsize=12)
    ax2.set_ylabel("回撤 %")
    ax2.legend(loc="lower left", fontsize=7, frameon=False, ncol=4)
    fig.tight_layout()
    fig.savefig(OUT / "1_equity.png", dpi=130)
    plt.close(fig)
    report.append("![净值](1_equity.png)\n")

    # 策略之间的相关性（为什么组合有用）
    R = pd.DataFrame({LABELS[k]: res[k].returns.loc[start:] for k in ["buy_hold", *order]})
    corr = R.corr()
    corr.index.name = "日收益相关系数"
    report.append("### 策略之间的相关性\n")
    report.append("相关性越低，组合在一起分散风险的效果越好。\n")
    report.append(md_table(corr) + "\n")
    return res, periods, tables


# ------------------------------------------------------------------ 2. 跨品种
def section_cross_asset(P, periods, report):
    a, b = periods["全区间"]
    oa, ob = periods["样本外"]
    rows_full, rows_oos = {}, {}
    for name in ["buy_hold", *SINGLE]:
        rf, ro = {}, {}
        for sym in CROSS_ASSETS:
            r = buy_hold(P[sym], sym) if name == "buy_hold" else run_single(name, P[sym], sym)
            rf[sym] = metrics.sharpe(r.returns.loc[a:b])
            ro[sym] = metrics.sharpe(r.returns.loc[oa:ob])
        rows_full[LABELS[name]], rows_oos[LABELS[name]] = rf, ro
    full, oos = pd.DataFrame(rows_full).T, pd.DataFrame(rows_oos).T
    full.index.name = oos.index.name = "Sharpe"

    report.append("## 2. 跨品种检验（参数完全不变）\n")
    report.append("一个真正抓到市场规律的策略，换个品种不应该完全失效。"
                  "重点看：策略在多少个品种上 Sharpe 高于买入持有。\n")
    report.append("### 全区间 Sharpe\n" + md_table(full) + "\n")
    report.append("### 样本外 Sharpe\n" + md_table(oos) + "\n")
    beats = (full.drop(index=LABELS["buy_hold"]) > full.loc[LABELS["buy_hold"]]).sum(axis=1)
    report.append("全区间 Sharpe 超过买入持有的品种数（共 %d 个）：\n\n" % len(CROSS_ASSETS)
                  + "\n".join(f"- {k}：{v}" for k, v in beats.items()) + "\n")

    fig, ax = plt.subplots(figsize=(8, 4.2))
    im = ax.imshow(full.values, cmap="Blues", aspect="auto")
    ax.set_xticks(range(len(full.columns)), full.columns)
    ax.set_yticks(range(len(full.index)), full.index)
    for (y, x), v in np.ndenumerate(full.values):
        ax.text(x, y, f"{v:.2f}", ha="center", va="center", fontsize=8,
                color="white" if v > np.nanmax(full.values) * 0.6 else "#222")
    ax.grid(False)
    fig.colorbar(im, label="Sharpe（全区间）")
    ax.set_title("跨品种 Sharpe", loc="left")
    fig.tight_layout()
    fig.savefig(OUT / "2_cross_asset.png", dpi=130)
    plt.close(fig)
    report.append("![跨品种](2_cross_asset.png)\n")


# ------------------------------------------------------------------ 3. 参数稳健性
def section_robustness(P, periods, report):
    ia, ib = periods["样本内"]
    oa, ob = periods["样本外"]
    spy = P["SPY"]
    report.append("## 3. 参数稳健性（SPY）\n")
    report.append("左图样本内、右图样本外，★ 是默认参数。好的策略：默认参数附近一大片颜色都差不多；"
                  "而且样本内最好的格子，到了样本外不应该掉得很惨。\n")
    for name, (fn, grid) in SINGLE.items():
        if not grid:
            continue
        (p1, v1), (p2, v2) = grid.items()
        M_is = np.full((len(v1), len(v2)), np.nan)
        M_oos = M_is.copy()
        for i, x in enumerate(v1):
            for j, y in enumerate(v2):
                if name == "ma_adx_plus" and x >= y:
                    continue
                r = run_single(name, spy, "SPY", **{p1: x, p2: y}).returns
                M_is[i, j] = metrics.sharpe(r.loc[ia:ib])
                M_oos[i, j] = metrics.sharpe(r.loc[oa:ob])
        import inspect
        d = inspect.signature(fn).parameters
        di, dj = v1.index(d[p1].default), v2.index(d[p2].default)
        bi, bj = np.unravel_index(np.nanargmax(M_is), M_is.shape)

        fig, axes = plt.subplots(1, 2, figsize=(10, 3.8))
        vmin, vmax = np.nanmin([M_is, M_oos]), np.nanmax([M_is, M_oos])
        for ax, M, title in zip(axes, [M_is, M_oos], ["样本内", "样本外"]):
            im = ax.imshow(M, cmap="Blues", vmin=vmin, vmax=vmax, aspect="auto")
            ax.set_xticks(range(len(v2)), v2)
            ax.set_yticks(range(len(v1)), v1)
            ax.set_xlabel(p2); ax.set_ylabel(p1)
            ax.grid(False)
            for (y, x), v in np.ndenumerate(M):
                if not np.isnan(v):
                    star = "★" if (y, x) == (di, dj) else ""
                    ax.text(x, y, f"{star}{v:.2f}", ha="center", va="center", fontsize=8,
                            color="white" if v > vmin + (vmax - vmin) * 0.65 else "#222")
            ax.set_title(f"{LABELS[name]} — {title}", loc="left", fontsize=10)
        fig.colorbar(im, ax=axes, label="Sharpe", shrink=0.9)
        fig.savefig(OUT / f"3_robust_{name}.png", dpi=130, bbox_inches="tight")
        plt.close(fig)

        report.append(f"### {LABELS[name]}\n")
        report.append(
            f"- 默认参数（{p1}={v1[di]}, {p2}={v2[dj]}）：样本内 {M_is[di, dj]:.2f}，样本外 {M_oos[di, dj]:.2f}\n"
            f"- 样本内最优（{p1}={v1[bi]}, {p2}={v2[bj]}）：样本内 {M_is[bi, bj]:.2f}，"
            f"**样本外 {M_oos[bi, bj]:.2f}**\n"
            f"- 全部格子样本内 Sharpe 中位数 {np.nanmedian(M_is):.2f}，"
            f"样本外中位数 {np.nanmedian(M_oos):.2f}\n")
        report.append(f"![{name}](3_robust_{name}.png)\n")


# ------------------------------------------------------------------ 4. 交易成本
def section_costs(P, periods, report):
    a, b = periods["全区间"]
    spy = P["SPY"]
    costs_bp = [0, 5, 10, 20, 50]
    rows = {}
    for name in ["buy_hold", *SINGLE]:
        row = {}
        for bp in costs_bp:
            r = buy_hold(spy, "SPY", bp / 1e4) if name == "buy_hold" else \
                run_single(name, spy, "SPY", cost=bp / 1e4)
            row[f"{bp}bp"] = metrics.sharpe(r.returns.loc[a:b])
        rows[LABELS[name]] = row
    t = pd.DataFrame(rows).T
    t.index.name = "Sharpe / 单边成本"
    report.append("## 4. 交易成本敏感性（SPY 全区间）\n")
    report.append("1bp = 0.01%。SPY 实际点差约 0.5bp，但个股、小盘股、期货换月等成本会高得多。"
                  "交易越频繁的策略，Sharpe 随成本下降得越快。\n")
    report.append(md_table(t) + "\n")


# ------------------------------------------------------------------ 5. Bootstrap
def section_bootstrap(res, periods, report, B=2000, L=20, seed=0):
    oa, ob = periods["样本外"]
    rng = np.random.default_rng(seed)
    bh = res["buy_hold"].returns.loc[oa:ob]
    idx = block_bootstrap_idx(len(bh), B, L, rng)
    ann = np.sqrt(252)

    def boot_sharpe(x):
        s = x[idx]
        with np.errstate(invalid="ignore", divide="ignore"):
            return s.mean(axis=1) / s.std(axis=1) * ann

    rf = CASH["ret"].reindex(bh.index).fillna(0.0).to_numpy() if CASH["ret"] is not None else 0.0
    sh_bh = boot_sharpe(bh.to_numpy() - rf)
    rows = {}
    for k, r in res.items():
        x = r.returns.loc[oa:ob].reindex(bh.index).fillna(0.0).to_numpy() - rf
        sh = boot_sharpe(x)
        diff = sh - sh_bh
        rows[LABELS[k]] = {
            "超额Sharpe": metrics.sharpe(pd.Series(x)),
            "95% 下限": np.nanpercentile(sh, 2.5), "95% 上限": np.nanpercentile(sh, 97.5),
            "比买入持有高的概率%": np.nanmean(diff > 0) * 100 if k != "buy_hold" else np.nan,
        }
    t = pd.DataFrame(rows).T
    t.index.name = "样本外"
    report.append("## 5. Bootstrap：结果是不是运气？（样本外）\n")
    report.append(f"把样本外的日收益按 {L} 天一段随机重抽 {B} 次（块 bootstrap，保留短期相关性），"
                  "每次算一个超额 Sharpe（扣掉短债收益）。每次抽样时策略和买入持有用同一组日期，所以两者可以直接比较。"
                  "置信区间很宽是正常的：十年数据能告诉你的东西比直觉少得多。\n")
    report.append(md_table(t) + "\n")


# ------------------------------------------------------------------ 6. Walk-forward
def section_walkforward(P, periods, report, train_years=5):
    spy = P["SPY"]
    a, b = periods["全区间"]
    years = sorted(set(spy.loc[a:b].index.year))
    report.append("## 6. Walk-forward（SPY）\n")
    report.append(f"每年年初，只用**过去 {train_years} 年**的数据在参数网格里挑 Sharpe 最高的，"
                  "用在接下来这一年；一年一年往前滚。这最接近真实使用：你永远不知道未来。"
                  "如果 walk-forward 比固定默认参数还差，说明\"优化参数\"这件事本身没带来价值。\n")
    rows, chosen_all = {}, {}
    for name, (fn, grid) in SINGLE.items():
        if not grid:
            continue
        combos = [c for c in itertools.product(*grid.values())
                  if not (name == "ma_adx_plus" and c[0] >= c[1])]
        R = pd.DataFrame({c: run_single(name, spy, "SPY", **dict(zip(grid, c))).returns
                          for c in combos})
        pieces, chosen = [], {}
        for y in years[train_years:]:
            train = R.loc[str(y - train_years):str(y - 1)]
            best = train.apply(metrics.sharpe).idxmax()
            chosen[y] = ", ".join(f"{k}={v}" for k, v in zip(grid, best))
            pieces.append(R.loc[str(y), best])
        wf = pd.concat(pieces)
        default = run_single(name, spy, "SPY").returns.loc[wf.index]
        bh = buy_hold(spy, "SPY").returns.loc[wf.index]
        rows[LABELS[name]] = {"WF Sharpe": metrics.sharpe(wf),
                              "默认参数 Sharpe": metrics.sharpe(default),
                              "买入持有 Sharpe": metrics.sharpe(bh),
                              "WF CAGR%": metrics.stats(wf)["CAGR%"],
                              "默认 CAGR%": metrics.stats(default)["CAGR%"]}
        chosen_all[LABELS[name]] = chosen
    t = pd.DataFrame(rows).T
    t.index.name = f"{years[train_years]}–{years[-1]}"
    report.append(md_table(t) + "\n")
    c = pd.DataFrame(chosen_all)
    c.index.name = "每年选中的参数"
    report.append("### 每年选中的参数\n参数每年跳来跳去 = 最优参数主要是噪音。\n")
    report.append(md_table(c) + "\n")


# ------------------------------------------------------------------ 0. 现金收益的影响
def section_cash(P, report):
    """同一批策略，现金计息 vs 不计息，看差多少。"""
    spy = P["SPY"]
    rows = {}
    for name in ["trend_vol", "trend_ens", "donchian_s1", "rsi2"]:
        r = {}
        for tag, cr in [("不计息", None), ("按SHY计息", CASH["ret"])]:
            fn = SINGLE[name][0]
            res = run_weights({"SPY": spy}, fn(spy).rename("SPY").to_frame(), cost=COST,
                              band=BAND, cash_ret=cr)
            st = metrics.stats(res.returns.iloc[260:])
            r[f"CAGR% {tag}"], r[f"Sharpe {tag}"] = st["CAGR%"], st["Sharpe"]
        rows[LABELS[name]] = r
    t = pd.DataFrame(rows).T
    t.index.name = "SPY 全区间"
    report.append("## 0. 空仓现金计息的影响\n")
    report.append("上一版报告里空仓的钱不计利息，低估了经常空仓的策略。"
                  "这里改为空仓部分按 SHY 的日收益计算（SHY 有约 2 年久期，比真正的国库券波动大一点，"
                  "但 2005 年起就有数据）。\n")
    report.append(md_table(t) + "\n")


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2005-01-01")
    ap.add_argument("--is-end", default="2016-12-31", help="样本内截止日期")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--skip", nargs="*", default=[], help="跳过某些检验，如 --skip wf robust")
    ap.add_argument("--no-cash", action="store_true", help="空仓现金不计利息（旧版行为）")
    a = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    P = load_prices(a)
    report = ["# 策略研究报告\n"]
    if not a.no_cash:
        CASH["ret"] = P["SHY"]["Close"].pct_change()
    if a.synthetic:
        report.append("> ⚠️ 这是**随机模拟数据**，只用来检查代码能跑，数字没有任何意义。\n")
    report.append(f"单边成本 {COST * 1e4:.0f}bp；信号收盘产生、次日开盘成交；"
                  + ("空仓现金不计利息。\n" if a.no_cash else
                     "空仓部分按 SHY（1–3 年期美债 ETF）的收益计息。\n"))

    if not a.no_cash:
        section_cash(P, report)
    print("1/6 主结果"); res, periods, _ = section_main(P, a.is_end, report)
    print("2/6 跨品种"); section_cross_asset(P, periods, report)
    if "robust" not in a.skip:
        print("3/6 参数稳健性"); section_robustness(P, periods, report)
    print("4/6 交易成本"); section_costs(P, periods, report)
    print("5/6 Bootstrap"); section_bootstrap(res, periods, report)
    if "wf" not in a.skip:
        print("6/6 Walk-forward"); section_walkforward(P, periods, report)

    (OUT / "report.md").write_text("\n".join(report), encoding="utf-8")
    print(f"\n完成 → {OUT / 'report.md'}")


if __name__ == "__main__":
    main()
