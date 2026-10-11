"""技术指标，尽量与 AmiBroker 的定义保持一致。

AmiBroker 的 ATR / ADX 用的都是 Wilder 平滑（Wilders()），
不是普通 EMA。Wilder 平滑 = alpha 为 1/n 的指数平滑，
第一个值用前 n 个值的简单平均作为种子。
"""
import numpy as np
import pandas as pd


def sma(s: pd.Series, n: int) -> pd.Series:
    """简单移动平均，对应 AFL 的 MA(C, n)。"""
    return s.rolling(n, min_periods=n).mean()


def wilder(s: pd.Series, n: int) -> pd.Series:
    """Wilder 平滑，对应 AFL 的 Wilders(x, n)。

    前 n 个有效值取简单平均作为种子，之后：
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
    """对应 AFL 的 ATR(n)。"""
    return wilder(true_range(df), n)


def adx(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """对应 AFL 的 ADX(n)（Wilder 原始定义）。"""
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
    """Wilder 的 RSI，对应 AFL 的 RSI(n)。"""
    d = close.diff()
    gain = wilder(d.clip(lower=0), n)
    loss = wilder((-d).clip(lower=0), n)
    rs = gain / loss
    out = 100 - 100 / (1 + rs)
    return out.where(loss > 0, 100.0).where(gain.notna())


def realized_vol(close: pd.Series, n: int = 20) -> pd.Series:
    """过去 n 天日收益率的标准差，年化。"""
    return close.pct_change().rolling(n, min_periods=n).std() * np.sqrt(252)


def cross(a: pd.Series, b: pd.Series) -> pd.Series:
    """对应 AFL 的 Cross(a, b)：今天 a > b，昨天 a <= b。"""
    return (a > b) & (a.shift(1) <= b.shift(1))
