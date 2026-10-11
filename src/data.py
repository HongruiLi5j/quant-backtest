"""行情数据：下载、缓存、导出成 AmiBroker 能导入的 CSV。"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def download(symbol: str, start: str = "2010-01-01", end: str | None = None) -> pd.DataFrame:
    """用 yfinance 下载日线（已做拆股/分红复权），并缓存到 data/<symbol>.csv。"""
    import yfinance as yf

    raw = yf.download(symbol, start=start, end=end, auto_adjust=True, progress=False)
    if raw.empty:
        raise RuntimeError(f"没有下载到 {symbol} 的数据，检查代码或网络")
    if isinstance(raw.columns, pd.MultiIndex):  # 新版 yfinance 返回多级列名
        raw.columns = raw.columns.get_level_values(0)
    df = raw[["Open", "High", "Low", "Close", "Volume"]].dropna()
    df.index = pd.to_datetime(df.index).tz_localize(None)
    df.index.name = "Date"
    DATA_DIR.mkdir(exist_ok=True)
    df.to_csv(DATA_DIR / f"{symbol}.csv")
    export_for_amibroker(df, symbol)
    return df


def load(symbol: str, start: str = "2010-01-01", end: str | None = None,
         refresh: bool = False) -> pd.DataFrame:
    """优先读本地缓存，没有就下载。"""
    path = DATA_DIR / f"{symbol}.csv"
    if refresh or not path.exists():
        return download(symbol, start, end)
    df = pd.read_csv(path, index_col="Date", parse_dates=True)
    return df.loc[start:end] if end else df.loc[start:]


def export_for_amibroker(df: pd.DataFrame, symbol: str) -> Path:
    """导出成 AmiBroker ASCII Import 能直接读的格式，
    保证两边用的是同一份数据，结果才有可比性。"""
    out = DATA_DIR / f"{symbol}_amibroker.csv"
    tmp = df.copy()
    tmp.insert(0, "Ticker", symbol)
    tmp.index = tmp.index.strftime("%Y-%m-%d")
    tmp.to_csv(out, index_label="Date", float_format="%.4f",
               columns=["Ticker", "Open", "High", "Low", "Close", "Volume"])
    return out


def synthetic(n: int = 2500, seed: int = 0, start: str = "2015-01-01") -> pd.DataFrame:
    """随机游走生成的假数据，只用来测试代码能不能跑，不代表任何真实市场。"""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(start, periods=n, name="Date")
    ret = rng.normal(0.0003, 0.012, n)
    close = 100 * np.exp(np.cumsum(ret))
    open_ = close * np.exp(rng.normal(0, 0.004, n))
    high = np.maximum(open_, close) * np.exp(np.abs(rng.normal(0, 0.006, n)))
    low = np.minimum(open_, close) * np.exp(-np.abs(rng.normal(0, 0.006, n)))
    vol = rng.integers(1_000_000, 5_000_000, n)
    return pd.DataFrame({"Open": open_, "High": high, "Low": low,
                         "Close": close, "Volume": vol}, index=idx)
