"""Technical indicators, kept as close as possible to AmiBroker's definitions.

AmiBroker's ATR and ADX both use Wilder smoothing (Wilders()),
not a plain EMA. Wilder smoothing = exponential smoothing with alpha = 1/n,
where the first value is seeded with the simple average of the first n values.
"""
import numpy as np
import pandas as pd


def sma(s: pd.Series, n: int) -> pd.Series:
    """Simple moving average, matches AFL's MA(C, n)."""
    return s.rolling(n, min_periods=n).mean()


def wilder(s: pd.Series, n: int) -> pd.Series:
    """Wilder smoothing, matches AFL's Wilders(x, n).

    The first n valid values are averaged to seed the series, then:
        w[t] = w[t-1] + (x[t] - w[t-1]) / n
    """
    x = s.to_numpy(dtype=float)
    out = np.full_like(x, np.nan)
    valid = np.flatnonzero(~np.isnan(x))
    if len(valid) < n:
        return pd.Series(out, index=s.index)
    start = valid[0]
    seed_end = start + n - 1
    out[seed_end] = np.nanmean(x[start:seed_end + 1])
    for t in range(seed_end + 1, len(x)):
        out[t] = out[t - 1] + (x[t] - out[t - 1]) / n
    return pd.Series(out, index=s.index)


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["Close"].shift(1)
    tr = pd.concat(
        [df["High"] - df["Low"],
         (df["High"] - prev_close).abs(),
         (df["Low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    tr.iloc[0] = df["High"].iloc[0] - df["Low"].iloc[0]
    return tr


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """Matches AFL's ATR(n)."""
    return wilder(true_range(df), n)


def adx(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """Matches AFL's ADX(n) (Wilder's original definition)."""
    up = df["High"].diff()
    down = -df["Low"].diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)
    plus_dm.iloc[0] = np.nan
    minus_dm.iloc[0] = np.nan

    tr = true_range(df)
    tr.iloc[0] = np.nan
    tr_s = wilder(tr, n)
    pdi = 100 * wilder(plus_dm, n) / tr_s
    mdi = 100 * wilder(minus_dm, n) / tr_s
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi)
    return wilder(dx, n)


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    """Wilder's RSI, matches AFL's RSI(n)."""
    d = close.diff()
    gain = wilder(d.clip(lower=0), n)
    loss = wilder((-d).clip(lower=0), n)
    rs = gain / loss
    out = 100 - 100 / (1 + rs)
    return out.where(loss > 0, 100.0).where(gain.notna())


def realized_vol(close: pd.Series, n: int = 20) -> pd.Series:
    """Standard deviation of daily returns over the past n days, annualized."""
    return close.pct_change().rolling(n, min_periods=n).std() * np.sqrt(252)


def cross(a: pd.Series, b: pd.Series) -> pd.Series:
    """Matches AFL's Cross(a, b): a > b today, a <= b yesterday."""
    return (a > b) & (a.shift(1) <= b.shift(1))
