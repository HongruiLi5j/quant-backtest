"""Command-line entry point.

Examples:
  python run_backtest.py SPY                       # backtest with default parameters
  python run_backtest.py QQQ --start 2015-01-01 --fast 10 --slow 50
  python run_backtest.py SPY --scan                # parameter scan (like AmiBroker's Optimize)
  python run_backtest.py --synthetic               # test that the code runs, using random data
"""
from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

from src import backtest, data, metrics
from src.strategy import Params, generate_signals

RESULTS = Path(__file__).resolve().parent / "results"


def plot(res: backtest.Result, symbol: str, path: Path):
    sig, eq, tr = res.signals, res.equity, res.trades
    fig, axes = plt.subplots(3, 1, figsize=(12, 9), sharex=True,
                             gridspec_kw={"height_ratios": [3, 2, 1]})
    ax = axes[0]
    ax.plot(sig.index, sig.Close, lw=0.8, color="#444", label="Close")
    ax.plot(sig.index, sig.ma_fast, lw=0.8, label=f"MA{res.params.fast}")
    ax.plot(sig.index, sig.ma_slow, lw=0.8, label=f"MA{res.params.slow}")
    if len(tr):
        ax.scatter(tr.entry_date, tr.entry_price, marker="^", color="green", s=25, label="Buy", zorder=3)
        ax.scatter(tr.exit_date, tr.exit_price, marker="v", color="red", s=25, label="Sell", zorder=3)
    ax.set_title(f"{symbol} — MA{res.params.fast}/{res.params.slow} + ADX>{res.params.adx_min:g} + {res.params.atr_mult:g}×ATR stop")
    ax.legend(loc="upper left", fontsize=8)

    ax = axes[1]
    bh = sig.Close / sig.Close.iloc[0] * eq.iloc[0]
    ax.plot(eq.index, eq, label="Strategy")
    ax.plot(bh.index, bh, alpha=0.6, label="Buy & Hold")
    ax.set_ylabel("Equity")
    ax.legend(loc="upper left", fontsize=8)

    ax = axes[2]
    dd = (eq / eq.cummax() - 1) * 100
    ax.fill_between(dd.index, dd, 0, color="red", alpha=0.3)
    ax.set_ylabel("Drawdown %")
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)


def scan(df: pd.DataFrame, symbol: str, cfg: backtest.Config):
    """Scan fast/slow moving-average combinations. Note: the best parameters picked this way are almost certainly overfit;
    before relying on them, run an out-of-sample test (walk-forward); see the exercises in the README."""
    rows = []
    for fast in [3, 5, 8, 10, 15, 20]:
        for slow in [20, 30, 50, 100, 150, 200]:
            if fast >= slow:
                continue
            p = Params(fast=fast, slow=slow)
            r = backtest.run(generate_signals(df, p), p, cfg)
            s = metrics.summarize(r.equity, r.trades)
            rows.append({"fast": fast, "slow": slow, "sharpe": s["Sharpe (rf=0)"],
                         "cagr": s["年化收益 CAGR %"], "maxdd": s["最大回撤 %"],
                         "trades": s["交易次数"]})
    table = pd.DataFrame(rows)
    table.to_csv(RESULTS / f"{symbol}_scan.csv", index=False)
    pivot = table.pivot(index="fast", columns="slow", values="sharpe")
    fig, ax = plt.subplots(figsize=(7, 5))
    im = ax.imshow(pivot.values, cmap="RdYlGn", aspect="auto")
    ax.set_xticks(range(len(pivot.columns)), pivot.columns)
    ax.set_yticks(range(len(pivot.index)), pivot.index)
    ax.set_xlabel("slow MA"); ax.set_ylabel("fast MA")
    for (y, x), v in pd.DataFrame(pivot.values).stack().items():
        ax.text(x, y, f"{v:.2f}", ha="center", va="center", fontsize=8)
    fig.colorbar(im, label="Sharpe")
    ax.set_title(f"{symbol} parameter scan (Sharpe)")
    fig.tight_layout()
    fig.savefig(RESULTS / f"{symbol}_scan.png", dpi=120)
    plt.close(fig)
    print(table.sort_values("sharpe", ascending=False).head(10).to_string(index=False))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("symbol", nargs="?", default="SPY")
    ap.add_argument("--start", default="2010-01-01")
    ap.add_argument("--end", default=None)
    ap.add_argument("--refresh", action="store_true", help="re-download the data")
    ap.add_argument("--synthetic", action="store_true", help="use random data (offline test)")
    ap.add_argument("--fast", type=int, default=5)
    ap.add_argument("--slow", type=int, default=20)
    ap.add_argument("--adx-min", type=float, default=20.0)
    ap.add_argument("--atr-mult", type=float, default=2.0)
    ap.add_argument("--commission", type=float, default=0.0005)
    ap.add_argument("--scan", action="store_true")
    a = ap.parse_args()

    RESULTS.mkdir(exist_ok=True)
    if a.synthetic:
        symbol, df = "SYNTH", data.synthetic()
    else:
        symbol, df = a.symbol.upper(), data.load(a.symbol.upper(), a.start, a.end, a.refresh)

    cfg = backtest.Config(commission_pct=a.commission)
    if a.scan:
        scan(df, symbol, cfg)
        return

    p = Params(fast=a.fast, slow=a.slow, adx_min=a.adx_min, atr_mult=a.atr_mult)
    res = backtest.run(generate_signals(df, p), p, cfg)
    stats = metrics.summarize(res.equity, res.trades, df.Close)
    print(f"\n{symbol}  {df.index[0].date()} → {df.index[-1].date()}\n")
    print(metrics.format_summary(stats))
    res.trades.to_csv(RESULTS / f"{symbol}_trades.csv", index=False)
    plot(res, symbol, RESULTS / f"{symbol}_backtest.png")
    print(f"\nTrade list -> results/{symbol}_trades.csv\nChart      -> results/{symbol}_backtest.png")


if __name__ == "__main__":
    main()
