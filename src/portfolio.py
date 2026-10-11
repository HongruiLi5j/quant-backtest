"""按"目标仓位"回测的引擎，单品种和多品种通用。

和 backtest.py（逐笔、盘中止损，用来和 AmiBroker 对照）不同，这里的约定是：

  1. 策略在第 t 天收盘后算出目标仓位 target[t]（0 = 空仓，1 = 满仓，0.5 = 半仓）
  2. 第 t+1 天开盘按目标仓位成交
  3. 止损也按收盘价判断、次日开盘执行（比盘中止损保守，但没有"盘中先碰高还是先碰低"的歧义）

每天的收益拆成三段，复利相乘：
  隔夜段  昨收 → 今开：用昨天收盘时的持仓（已随昨天日内价格漂移）
  开盘调仓：扣手续费 = 费率 × 换手（|今天目标 - 昨天目标| 之和）
  日内段  今开 → 今收：用今天开盘调整后的持仓
空仓现金：传入 cash_ret 时，没用上的仓位（1 - 总仓位）按 cash_ret 计利息；否则为 0。

近似：持仓期间假设每天开盘都把仓位调回目标（手续费只按目标变化计）。
对 0/1 仓位的策略这是精确的；对连续仓位（波动率目标）用 band 参数减少无意义的小额调仓。
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class PortResult:
    returns: pd.Series        # 每日策略收益（已扣手续费）
    equity: pd.Series         # 净值，从 1 开始
    weights: pd.DataFrame     # 每天实际持有的仓位（开盘成交后）
    turnover: pd.Series       # 每天换手
    trades: pd.DataFrame      # 单品种时的逐笔交易；多品种为空


def panel(prices: dict[str, pd.DataFrame], field: str) -> pd.DataFrame:
    """把多个品种的某个字段（Open/Close…）拼成一张表，只保留共同交易日。"""
    return pd.concat({s: df[field] for s, df in prices.items()}, axis=1, join="inner")


def apply_band(target: pd.DataFrame, band: float) -> pd.DataFrame:
    """调仓阈值：目标仓位变化小于 band 就不动。进场/清仓（从 0 变或变成 0）总是执行。"""
    if band <= 0:
        return target
    t = target.to_numpy()
    out = np.zeros_like(t)
    cur = np.zeros(t.shape[1])
    for i in range(len(t)):
        row = t[i]
        change = (np.abs(row - cur) > band) | ((row == 0) != (cur == 0))
        cur = np.where(change, row, cur)
        out[i] = cur
    return pd.DataFrame(out, index=target.index, columns=target.columns)


def run_weights(prices: dict[str, pd.DataFrame], target: pd.DataFrame | pd.Series,
                cost: float = 0.0005, band: float = 0.0,
                cash_ret: pd.Series | None = None,
                limits: dict[str, float] | None = None) -> PortResult:
    """cash_ret：没投出去的那部分钱每天的收益（例如短期国债 ETF 的日收益）。
    None 表示现金不计利息。
    limits：A 股涨跌停幅度 {代码: 0.10}。开盘就一字涨停买不进、一字跌停卖不出，
    这种情况下保持原仓位，第二天再试。"""
    if isinstance(target, pd.Series):
        target = target.to_frame(next(iter(prices)))
    O = panel(prices, "Open")[target.columns]
    C = panel(prices, "Close")[target.columns]
    idx = O.index.intersection(target.index)
    O, C = O.loc[idx], C.loc[idx]
    tgt = target.loc[idx].fillna(0.0).clip(lower=0.0)

    tgt = apply_band(tgt, band)
    held = tgt.shift(1).fillna(0.0)          # 今天开盘成交后的仓位 = 昨天收盘定的目标
    if limits:
        gap_all = (O / C.shift(1) - 1).fillna(0.0)
        for col, lim in limits.items():
            if col not in held:
                continue
            want, gap = held[col].to_numpy(), gap_all[col].to_numpy()
            actual = np.zeros_like(want)
            prev = 0.0
            for i in range(len(want)):
                w = want[i]
                if (w > prev and gap[i] >= lim - 0.002) or (w < prev and gap[i] <= -lim + 0.002):
                    w = prev          # 涨停开盘买不进 / 跌停开盘卖不出
                actual[i] = prev = w
            held[col] = actual
    held_prev = held.shift(1).fillna(0.0)    # 昨天的仓位（承担隔夜段）

    r_gap = (O / C.shift(1) - 1).fillna(0.0)
    r_intra = C / O - 1
    turnover = (held - held_prev).abs().sum(axis=1)
    # 日内：开盘按 held 建仓，收益 i；收盘时仓位随价格漂移成 w_close
    intra = (held * r_intra).sum(axis=1)
    w_close = held * (1 + r_intra)
    w_close = w_close.div(1 + intra, axis=0)
    # 隔夜：用昨天收盘时（漂移后）的仓位
    gap = (w_close.shift(1).fillna(0.0) * r_gap).sum(axis=1)
    # 先隔夜、再开盘调仓扣费、再日内，三段复利
    ret = (1 + gap) * (1 - cost * turnover) * (1 + intra) - 1
    if cash_ret is not None:
        idle = (1 - held.sum(axis=1)).clip(lower=0.0)
        ret = ret + idle * cash_ret.reindex(idx).fillna(0.0)
    equity = (1 + ret).cumprod()

    trades = _split_trades(held.iloc[:, 0], ret) if held.shape[1] == 1 else pd.DataFrame()
    return PortResult(ret.rename("ret"), equity.rename("equity"), held, turnover, trades)


def _split_trades(w: pd.Series, ret: pd.Series) -> pd.DataFrame:
    """把单品种仓位路径拆成一笔笔交易：从仓位 > 0 开始，到仓位回到 0 的那天（含）结束。"""
    on = (w > 0).to_numpy()
    r = ret.to_numpy()
    rows, start = [], None
    for i in range(len(on)):
        if on[i] and start is None:
            start = i
        elif not on[i] and start is not None:
            seg = r[start:i + 1]                 # 包括出场当天的隔夜段
            rows.append((w.index[start], w.index[i], i - start, np.prod(1 + seg) - 1))
            start = None
    if start is not None:
        seg = r[start:]
        rows.append((w.index[start], w.index[-1], len(on) - 1 - start, np.prod(1 + seg) - 1))
    df = pd.DataFrame(rows, columns=["entry_date", "exit_date", "bars", "ret"])
    df["ret_pct"] = df["ret"] * 100
    df["pnl"] = df["ret"]                        # 让 metrics.summarize 能统计胜率/盈亏比
    return df
