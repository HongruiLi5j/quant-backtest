"""策略库。每个策略只做一件事：根据截至第 t 天收盘的数据，给出第 t 天收盘后的目标仓位。

执行（次日开盘成交、手续费）统一交给 portfolio.run_weights，所以策略里不用关心成交细节，
只要保证**不用到 t 之后的数据**（tests/test_strategies.py 会检查这一点）。

默认参数都来自公开文献或业界常用值，**没有在任何数据上优化过**，这点很重要：
这样样本外结果才有意义。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .indicators import adx, atr, cross, realized_vol, rsi, sma


# ---------------------------------------------------------------- 工具函数
def vol_scale(close: pd.Series, target_vol: float | None, vol_len: int = 20,
              max_lev: float = 1.0) -> pd.Series:
    """波动率目标仓位：市场越动荡仓位越小。target_vol=None 表示固定满仓。
    仓位 = min(max_lev, 目标年化波动 / 过去 vol_len 天的实际年化波动)"""
    if target_vol is None:
        return pd.Series(1.0, index=close.index)
    return (target_vol / realized_vol(close, vol_len)).clip(upper=max_lev)


# ---------------------------------------------------------------- 1. 趋势 + 波动率目标
def trend_vol_target(df: pd.DataFrame, sma_len: int = 200, buffer: float = 0.01,
                     target_vol: float | None = 0.15, vol_len: int = 20,
                     max_lev: float = 1.0) -> pd.Series:
    """长期趋势过滤 + 波动率目标仓位。

    思路：价格在 200 日均线之上时持有，之下时空仓（Faber 2007 "A Quantitative Approach to
    Tactical Asset Allocation"）。再按波动率调仓位：大跌往往伴随波动放大，
    降低仓位能削减回撤（Moreira & Muir 2017 "Volatility-Managed Portfolios"）。

    buffer：迟滞区间，价格要高于均线 1% 才进、低于均线 1% 才出，减少在均线附近来回打脸。
    """
    c = df["Close"].to_numpy()
    m = sma(df["Close"], sma_len).to_numpy()
    state = np.zeros(len(c))
    on = False
    for t in range(len(c)):
        if np.isnan(m[t]):
            continue
        if not on and c[t] > m[t] * (1 + buffer):
            on = True
        elif on and c[t] < m[t] * (1 - buffer):
            on = False
        state[t] = on
    w = pd.Series(state, index=df.index) * vol_scale(df["Close"], target_vol, vol_len, max_lev)
    return w.fillna(0.0).rename("trend_vol")


def trend_ensemble(df: pd.DataFrame, lens: tuple = (100, 150, 200, 250), buffer: float = 0.01,
                   target_vol: float | None = 0.15, vol_len: int = 20,
                   max_lev: float = 1.0) -> pd.Series:
    """趋势信号的"集成"版：同时看 100/150/200/250 日四条均线，
    仓位 = 在均线上方的比例（0、25%、50%、75%、100%）× 波动率仓位。

    为什么：只用 200 日均线，结果会依赖"200"这一个数字（参数风险）。
    几条均线取平均，进出场是渐进的，不会因为某个参数恰好好/坏而大起大落。
    这是 CTA 行业里的标准做法。"""
    states = [trend_vol_target(df, sma_len=n, buffer=buffer, target_vol=None) for n in lens]
    avg = sum(states) / len(states)
    w = avg * vol_scale(df["Close"], target_vol, vol_len, max_lev)
    return w.fillna(0.0).rename("trend_ens")


# ---------------------------------------------------------------- 2. 唐奇安通道突破（海龟）
def donchian_breakout(df: pd.DataFrame, entry_len: int = 55, exit_len: int = 20,
                      atr_len: int = 20, atr_mult: float = 3.0,
                      target_vol: float | None = 0.15, vol_len: int = 20) -> pd.Series:
    """收盘价创 55 日新高进场；跌破 20 日低点、或从持仓期最高收盘回撤超过 3×ATR 出场。

    经典海龟交易法则的简化版（Faith, "Way of the Turtle"）。
    吊灯止损（Chandelier exit）会随价格上涨而上移，锁住利润。
    """
    c = df["Close"].to_numpy()
    hi = df["High"].rolling(entry_len).max().shift(1).to_numpy()   # 不含今天
    lo = df["Low"].rolling(exit_len).min().shift(1).to_numpy()
    a = atr(df, atr_len).to_numpy()
    state = np.zeros(len(c))
    on, peak = False, 0.0
    for t in range(len(c)):
        if np.isnan(hi[t]) or np.isnan(a[t]):
            continue
        if not on:
            if c[t] > hi[t]:
                on, peak = True, c[t]
        else:
            peak = max(peak, c[t])
            if c[t] < lo[t] or c[t] < peak - atr_mult * a[t]:
                on = False
        state[t] = on
    w = pd.Series(state, index=df.index) * vol_scale(df["Close"], target_vol, vol_len)
    return w.fillna(0.0).rename("donchian")


def donchian_s1(df: pd.DataFrame, entry_len: int = 20, exit_len: int = 10,
                atr_mult: float = 2.0, target_vol: float | None = 0.15) -> pd.Series:
    """海龟"系统 1"：20 日突破进场、10 日低点出场、2×ATR 止损（Faith, "Way of the Turtle"）。
    上一版报告里 walk-forward 反复选中 20 日突破，这里用海龟原书的系统 1 参数来验证，
    而不是直接用 walk-forward 选出来的数字（那样就是拿测试结果调参了）。"""
    return donchian_breakout(df, entry_len=entry_len, exit_len=exit_len, atr_len=20,
                             atr_mult=atr_mult, target_vol=target_vol).rename("donchian_s1")


# ---------------------------------------------------------------- 3. RSI(2) 短线均值回归
def rsi2_reversion(df: pd.DataFrame, trend_len: int = 200, rsi_len: int = 2,
                   entry_th: float = 10.0, exit_ma: int = 5, max_hold: int = 10) -> pd.Series:
    """长期上升趋势中，买短期超跌，反弹就走。

    进场：收盘 > 200 日均线 且 RSI(2) < 10
    出场：收盘 > 5 日均线，或持有满 10 天（时间止损）
    来源：Connors & Alvarez, "Short Term Trading Strategies That Work" (2008)。
    特点：胜率高、持仓短、大部分时间空仓；和趋势策略的相关性很低，适合做组合。
    风险：没有价格止损，遇到连续暴跌（如 2020 年 3 月）会吃大亏 → 持仓上限 max_hold 控制。
    """
    c = df["Close"].to_numpy()
    trend = sma(df["Close"], trend_len).to_numpy()
    r = rsi(df["Close"], rsi_len).to_numpy()
    ex = sma(df["Close"], exit_ma).to_numpy()
    state = np.zeros(len(c))
    on, held = False, 0
    for t in range(len(c)):
        if np.isnan(trend[t]) or np.isnan(r[t]):
            continue
        if not on:
            if c[t] > trend[t] and r[t] < entry_th:
                on, held = True, 0
        else:
            held += 1
            if c[t] > ex[t] or held >= max_hold:
                on = False
        state[t] = on
    return pd.Series(state, index=df.index, name="rsi2")


# ---------------------------------------------------------------- 4. 改进版 MA 交叉 + ADX
def ma_adx_plus(df: pd.DataFrame, fast: int = 5, slow: int = 20, adx_len: int = 14,
                adx_min: float = 20.0, regime_len: int = 200, atr_len: int = 14,
                atr_mult: float = 3.0, target_vol: float | None = 0.15,
                vol_len: int = 20) -> pd.Series:
    """你原来那个策略的升级版，改了三处：
      1. 加长期趋势过滤：只在收盘 > 200 日均线时做多（原版在熊市里反复被打）
      2. 固定止损 → 吊灯跟踪止损：持仓期最高收盘 - 3×ATR，赚到的利润能锁住
      3. 满仓 → 波动率目标仓位
    进场：快线上穿慢线 且 ADX > 20 且 在 200 日均线之上
    出场：快线下穿慢线 / 跟踪止损 / 跌破 200 日均线，任一触发
    """
    c = df["Close"].to_numpy()
    mf, ms = sma(df["Close"], fast), sma(df["Close"], slow)
    up = (cross(mf, ms) & (adx(df, adx_len) > adx_min)).to_numpy()
    down = cross(ms, mf).to_numpy()
    reg = sma(df["Close"], regime_len).to_numpy()
    a = atr(df, atr_len).to_numpy()
    state = np.zeros(len(c))
    on, peak = False, 0.0
    for t in range(len(c)):
        if np.isnan(reg[t]) or np.isnan(a[t]):
            continue
        if not on:
            if up[t] and c[t] > reg[t]:
                on, peak = True, c[t]
        else:
            peak = max(peak, c[t])
            if down[t] or c[t] < peak - atr_mult * a[t] or c[t] < reg[t]:
                on = False
        state[t] = on
    w = pd.Series(state, index=df.index) * vol_scale(df["Close"], target_vol, vol_len)
    return w.fillna(0.0).rename("ma_adx_plus")


def ma_adx_original(df: pd.DataFrame, fast: int = 5, slow: int = 20, adx_len: int = 14,
                    adx_min: float = 20.0) -> pd.Series:
    """原版（不含 ATR 止损），作为对照基准。完整带止损的原版见 backtest.py。"""
    mf, ms = sma(df["Close"], fast), sma(df["Close"], slow)
    up = (cross(mf, ms) & (adx(df, adx_len) > adx_min)).to_numpy()
    down = cross(ms, mf).to_numpy()
    state = np.zeros(len(df))
    on = False
    for t in range(len(df)):
        if not on and up[t]:
            on = True
        elif on and down[t]:
            on = False
        state[t] = on
    return pd.Series(state, index=df.index, name="ma_adx_original")


# ---------------------------------------------------------------- 5. 双动量轮动（多品种）
def dual_momentum(prices: dict[str, pd.DataFrame], risky: tuple = ("SPY", "EFA"),
                  safe: str = "IEF", cash: str = "SHY", lookback: int = 252,
                  skip: int = 0) -> pd.DataFrame:
    """Antonacci, "Dual Momentum Investing" (2014)。每月末调仓一次：
      相对动量：在风险资产（美股 SPY、海外股 EFA）里选过去 12 个月涨得多的那个
      绝对动量：如果它连短期国债（SHY，代替现金）都跑不赢 → 说明股市整体走弱，换成中期国债 IEF
    skip：跳过最近 skip 天（学术上常跳过最近 1 个月以避开短期反转）。
    """
    C = pd.concat({s: prices[s]["Close"] for s in set(risky) | {safe, cash}},
                  axis=1, join="inner")
    mom = C.shift(skip) / C.shift(lookback) - 1
    # 月末 = 下一个交易日换月。数据的最后一天只有在它确实是当月最后一个工作日时才算月末，
    # 否则月中运行时会把"今天"误当成月末提前调仓
    d = C.index.to_series()
    month_end = (d.shift(-1).dt.month != d.dt.month).to_numpy().copy()
    last = C.index[-1]
    month_end[-1] = last == last + pd.offsets.BMonthEnd(0)
    cols = list(dict.fromkeys([*risky, safe]))
    W = pd.DataFrame(np.nan, index=C.index, columns=cols)
    for t in C.index[month_end]:
        m = mom.loc[t]
        if m[list(risky)].isna().any() or np.isnan(m[cash]):
            continue
        best = m[list(risky)].idxmax()
        row = pd.Series(0.0, index=cols)
        row[best if m[best] > m[cash] else safe] = 1.0
        W.loc[t] = row
    return W.ffill().fillna(0.0)


# ---------------------------------------------------------------- 6. 组合
def blend(*weights: pd.Series, mix: tuple[float, ...] | None = None) -> pd.Series:
    """把几个单品种策略按比例合成一个仓位，例如 50% 趋势 + 50% 均值回归。
    两个策略赚钱的时段不同（相关性低），合在一起回撤通常比任何一个单独都小。"""
    mix = mix or tuple(1 / len(weights) for _ in weights)
    return sum(m * w for m, w in zip(mix, weights)).rename("blend")
