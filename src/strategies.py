"""Strategy library. Each strategy does one thing: given data up to the close of day t, it returns the target position after the close of day t.

Execution (fills at the next open, trading costs) is handled in one place by portfolio.run_weights, so a strategy does not deal with fill details and
only has to guarantee that it **never uses data after day t** (tests/test_strategies.py checks this).

Default parameters come from published research or common industry values and **were not optimized on any data**. This matters because
it keeps the out-of-sample results meaningful.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .indicators import adx, atr, cross, realized_vol, rsi, sma


# ---------------------------------------------------------------- Helper functions
def vol_scale(close: pd.Series, target_vol: float | None, vol_len: int = 20,
              max_lev: float = 1.0) -> pd.Series:
    """Volatility-targeted position size: the more turbulent the market, the smaller the position. target_vol=None means a fixed full position.
    position = min(max_lev, target annualized vol / realized annualized vol over the past vol_len days)"""
    if target_vol is None:
        return pd.Series(1.0, index=close.index)
    return (target_vol / realized_vol(close, vol_len)).clip(upper=max_lev)


# ---------------------------------------------------------------- 1. Trend + volatility target
def trend_vol_target(df: pd.DataFrame, sma_len: int = 200, buffer: float = 0.01,
                     target_vol: float | None = 0.15, vol_len: int = 20,
                     max_lev: float = 1.0) -> pd.Series:
    """Long-term trend filter + volatility-targeted position.

    Idea: hold when price is above the 200-day moving average and stay in cash when it is below (Faber 2007, "A Quantitative Approach to
    Tactical Asset Allocation"). Then scale the position by volatility: large drops usually come with rising volatility,
    so cutting the position reduces drawdowns (Moreira & Muir 2017, "Volatility-Managed Portfolios").

    buffer: a hysteresis band. Price must be 1% above the average to enter and 1% below to exit, which reduces whipsaws around the average.
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
    """Ensemble version of the trend signal: looks at four moving averages (100/150/200/250 days) at once,
    position = share of the averages that price is above (0, 25%, 50%, 75%, 100%) × volatility-targeted position.

    Why: with only the 200-day average, results depend on that single number (parameter risk).
    Averaging several moving averages makes entries and exits gradual, so one lucky or unlucky parameter cannot cause big swings.
    This is standard practice in the CTA industry."""
    states = [trend_vol_target(df, sma_len=n, buffer=buffer, target_vol=None) for n in lens]
    avg = sum(states) / len(states)
    w = avg * vol_scale(df["Close"], target_vol, vol_len, max_lev)
    return w.fillna(0.0).rename("trend_ens")


# ---------------------------------------------------------------- 2. Donchian channel breakout (Turtle)
def donchian_breakout(df: pd.DataFrame, entry_len: int = 55, exit_len: int = 20,
                      atr_len: int = 20, atr_mult: float = 3.0,
                      target_vol: float | None = 0.15, vol_len: int = 20) -> pd.Series:
    """Enter when the close makes a 55-day high; exit when price falls below the 20-day low, or drops more than 3×ATR from the highest close since entry.

    A simplified version of the classic Turtle trading rules (Faith, "Way of the Turtle").
    The chandelier exit moves up as price rises, which locks in profits.
    """
    c = df["Close"].to_numpy()
    hi = df["High"].rolling(entry_len).max().shift(1).to_numpy()   # excludes today
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
    """Turtle "System 1": enter on a 20-day breakout, exit at the 10-day low, 2×ATR stop (Faith, "Way of the Turtle").
    In the previous report, walk-forward kept selecting the 20-day breakout, so here I test it with the System 1 parameters from the original Turtle book,
    rather than using the numbers walk-forward picked (that would be tuning parameters on the test results)."""
    return donchian_breakout(df, entry_len=entry_len, exit_len=exit_len, atr_len=20,
                             atr_mult=atr_mult, target_vol=target_vol).rename("donchian_s1")


# ---------------------------------------------------------------- 3. RSI(2) short-term mean reversion
def rsi2_reversion(df: pd.DataFrame, trend_len: int = 200, rsi_len: int = 2,
                   entry_th: float = 10.0, exit_ma: int = 5, max_hold: int = 10) -> pd.Series:
    """In a long-term uptrend, buy short-term oversold dips and sell on the rebound.

    Entry: close > 200-day moving average and RSI(2) < 10
    Exit: close > 5-day moving average, or after holding 10 days (time stop)
    Source: Connors & Alvarez, "Short Term Trading Strategies That Work" (2008).
    Traits: high win rate, short holding periods, in cash most of the time; very low correlation with trend strategies, so it works well in a blend.
    Risk: there is no price stop, so a sustained crash (e.g. March 2020) can cause large losses -> the holding cap max_hold limits this.
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


# ---------------------------------------------------------------- 4. Improved MA crossover + ADX
def ma_adx_plus(df: pd.DataFrame, fast: int = 5, slow: int = 20, adx_len: int = 14,
                adx_min: float = 20.0, regime_len: int = 200, atr_len: int = 14,
                atr_mult: float = 3.0, target_vol: float | None = 0.15,
                vol_len: int = 20) -> pd.Series:
    """An upgraded version of the original strategy, with three changes:
      1. Add a long-term trend filter: only go long when close > 200-day moving average (the original was repeatedly whipsawed in bear markets)
      2. Fixed stop -> chandelier trailing stop: highest close since entry - 3×ATR, which locks in gains
      3. Full position -> volatility-targeted position
    Entry: fast MA crosses above slow MA, ADX > 20, and price above the 200-day moving average
    Exit: fast MA crosses below slow MA, trailing stop hit, or price falls below the 200-day moving average (any one triggers)
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
    """Original version (without the ATR stop), used as a baseline. See backtest.py for the full original with the stop."""
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


# ---------------------------------------------------------------- 5. Dual momentum rotation (multi-asset)
def dual_momentum(prices: dict[str, pd.DataFrame], risky: tuple = ("SPY", "EFA"),
                  safe: str = "IEF", cash: str = "SHY", lookback: int = 252,
                  skip: int = 0) -> pd.DataFrame:
    """Antonacci, "Dual Momentum Investing" (2014). Rebalances once at each month end:
      Relative momentum: among the risk assets (US stocks SPY, foreign stocks EFA), pick the one that gained more over the past 12 months
      Absolute momentum: if it cannot even beat short-term Treasuries (SHY, standing in for cash), the stock market is weak overall, so switch to intermediate Treasuries (IEF)
    skip: ignore the most recent skip days (academic studies often skip the last month to avoid short-term reversal).
    """
    C = pd.concat({s: prices[s]["Close"] for s in set(risky) | {safe, cash}},
                  axis=1, join="inner")
    mom = C.shift(skip) / C.shift(lookback) - 1
    # Month end = the month changes on the next trading day. The last day of the data only counts as a month end if it really is the last business day of the month,
    # otherwise running mid-month would mistake "today" for a month end and rebalance early
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


# ---------------------------------------------------------------- 6. Blend
def blend(*weights: pd.Series, mix: tuple[float, ...] | None = None) -> pd.Series:
    """Combine several single-asset strategies into one position by weights, e.g. 50% trend + 50% mean reversion.
    The two strategies make money at different times (low correlation), so the combined drawdown is usually smaller than either one alone."""
    mix = mix or tuple(1 / len(weights) for _ in weights)
    return sum(m * w for m, w in zip(mix, weights)).rename("blend")
