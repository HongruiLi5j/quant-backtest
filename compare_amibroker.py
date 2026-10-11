"""对比 AmiBroker 和 Python 的回测结果。

步骤见 README「和 AmiBroker 对比」。用法：
  python compare_amibroker.py SPY amibroker/SPY_ab_trades.csv
  python compare_amibroker.py SPY amibroker/SPY_ab_trades.csv --explore amibroker/SPY_ab_explore.csv

第一个文件：AmiBroker 回测结果窗口里 Trade list → File → Export 出来的 CSV
第二个文件（可选）：跑 Exploration 后导出的 CSV，用来逐日对比指标
"""
from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

from src import backtest, data
from src.strategy import Params, generate_signals


def pick(df: pd.DataFrame, *names: str) -> str:
    """AmiBroker 不同版本导出的列名略有不同，这里按候选名找。"""
    norm = {c.strip().lower().replace(".", "").replace(" ", ""): c for c in df.columns}
    for n in names:
        key = n.lower().replace(".", "").replace(" ", "")
        if key in norm:
            return norm[key]
    raise KeyError(f"找不到列 {names}，实际列名：{list(df.columns)}")


def load_ab_trades(path: str) -> pd.DataFrame:
    ab = pd.read_csv(path)
    out = pd.DataFrame({
        "entry_date": pd.to_datetime(ab[pick(ab, "Date", "Entry date")]).dt.normalize(),
        "entry_price": ab[pick(ab, "Price", "Entry price")].astype(float),
        "exit_date": pd.to_datetime(ab[pick(ab, "Ex. date", "Exit date")]).dt.normalize(),
        "exit_price": ab[pick(ab, "Ex. Price", "Exit price")].astype(float),
    })
    return out.sort_values("entry_date").reset_index(drop=True)


def compare_trades(py: pd.DataFrame, ab: pd.DataFrame, tol: float = 0.01):
    py = py[py.exit_reason != "end_of_data"]  # AmiBroker 不会强制平掉最后一笔
    m = py.merge(ab, on="entry_date", how="outer", suffixes=("_py", "_ab"), indicator=True)
    only_py = m[m._merge == "left_only"]
    only_ab = m[m._merge == "right_only"]
    both = m[m._merge == "both"].copy()
    both["exit_date_ok"] = both.exit_date_py == both.exit_date_ab
    both["entry_px_diff"] = (both.entry_price_py - both.entry_price_ab).abs()
    both["exit_px_diff"] = (both.exit_price_py - both.exit_price_ab).abs()
    bad = both[(~both.exit_date_ok) | (both.entry_px_diff > tol) | (both.exit_px_diff > tol)]

    print(f"Python 交易 {len(py)} 笔，AmiBroker 交易 {len(ab)} 笔，入场日期一致 {len(both)} 笔")
    print(f"  只有 Python 有：{len(only_py)}   只有 AmiBroker 有：{len(only_ab)}   出场/价格不一致：{len(bad)}")
    cols = ["entry_date", "entry_price_py", "entry_price_ab", "exit_date_py",
            "exit_date_ab", "exit_price_py", "exit_price_ab", "exit_reason"]
    for title, part in [("只有 Python 有", only_py), ("只有 AmiBroker 有", only_ab), ("不一致", bad)]:
        if len(part):
            print(f"\n--- {title} ---")
            print(part[[c for c in cols if c in part]].head(20).to_string(index=False))
    if not len(only_py) and not len(only_ab) and not len(bad):
        print("\n✅ 两边逐笔完全一致")


def compare_indicators(sig: pd.DataFrame, path: str, tol: float = 1e-3):
    ex = pd.read_csv(path)
    ex.index = pd.to_datetime(ex[pick(ex, "Date/Time", "Date")]).dt.normalize()
    pairs = [("ma_fast", "MAfast"), ("ma_slow", "MAslow"), ("adx", "ADX"), ("atr", "ATR")]
    print("\n指标逐日对比（最大绝对误差，前 100 根 K 线是预热期，已跳过）：")
    for py_col, ab_col in pairs:
        joined = pd.concat([sig[py_col], ex[pick(ex, ab_col)].rename("ab")], axis=1, join="inner").iloc[100:]
        diff = (joined[py_col] - joined["ab"]).abs()
        flag = "✅" if diff.max() < tol else "⚠️"
        print(f"  {flag} {ab_col:<7} max diff = {diff.max():.6f}   (第一次超过 {tol} 的日期: "
              f"{diff[diff > tol].index.min().date() if (diff > tol).any() else '-'})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("symbol")
    ap.add_argument("ab_trades")
    ap.add_argument("--explore", default=None)
    ap.add_argument("--fast", type=int, default=5)
    ap.add_argument("--slow", type=int, default=20)
    ap.add_argument("--adx-min", type=float, default=20.0)
    ap.add_argument("--atr-mult", type=float, default=2.0)
    a = ap.parse_args()

    df = data.load(a.symbol.upper())
    p = Params(fast=a.fast, slow=a.slow, adx_min=a.adx_min, atr_mult=a.atr_mult)
    sig = generate_signals(df, p)
    res = backtest.run(sig, p)
    compare_trades(res.trades, load_ab_trades(a.ab_trades))
    if a.explore:
        compare_indicators(sig, a.explore)


if __name__ == "__main__":
    main()
