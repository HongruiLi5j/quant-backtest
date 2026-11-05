"""美股全市场数据读取：从「文档/美股数据」文件夹（download_us.py 下载的 Parquet）读取，并整理成回测用的格式。

价格处理：Yahoo 的 Adj Close 已经含拆股和分红。把开高低收都乘以 (Adj Close / Close)，
这样收益率连续可比，和 A 股那边用前复权价的口径一致。成交量保持原样。
"""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pandas as pd

COLS = ["Open", "High", "Low", "Close", "Volume"]
EXCHANGE = {"N": "NYSE", "Q": "NASDAQ", "A": "NYSE American", "P": "NYSE Arca", "Z": "Cboe BZX", "V": "IEX"}


def root() -> Path | None:
    cands = []
    if os.environ.get("US_DATA_DIR"):
        cands.append(Path(os.environ["US_DATA_DIR"]))
    home = Path.home()
    cands += [home / "Documents" / "美股数据", home / "mnt" / "Documents" / "美股数据"]
    for c in cands:
        if (c / "data").is_dir():
            return c
    return None


def codes() -> list[str]:
    r = root()
    return sorted(p.stem for p in (r / "data").glob("*.parquet")) if r else []


def has(code: str) -> bool:
    r = root()
    return bool(r) and (r / "data" / f"{code}.parquet").exists()


def universe() -> pd.DataFrame:
    r = root()
    p = r / "universe.csv" if r else None
    if not p or not p.exists():
        return pd.DataFrame(columns=["symbol", "name", "exchange", "yahoo"])
    df = pd.read_csv(p, dtype=str).fillna("")
    df["name"] = df["name"].str.replace(r"\s+(Common Stock|Class [A-Z] Common Stock|Ordinary Shares).*$", "", regex=True).str.strip().str.rstrip(" -").str.strip()
    return df


US_CLOSE_READY = (16, 30)     # 美东 16:30 以后保存的文件，当天 K 线才算收盘完成


def drop_partial(df: pd.DataFrame, saved_ts: float) -> pd.DataFrame:
    """D87（revquant 美股审计 U3）：外部下载目录里，如果文件是在某天美东 16:30 之前保存的，那天（及以后）的 K 线是盘中数据，去掉。
    只在内存里去掉，不改外部文件（和 A 股 allmarket.strip_intraday 同一个判断，后者会改写自己的缓存）。"""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    if df.empty:
        return df
    saved = datetime.fromtimestamp(saved_ts, ZoneInfo("America/New_York"))
    ready = saved.date() if (saved.hour, saved.minute) >= US_CLOSE_READY else saved.date() - timedelta(days=1)
    idx = pd.DatetimeIndex(df.index).tz_localize(None) if getattr(df.index, "tz", None) is not None else pd.DatetimeIndex(df.index)
    return df[idx.normalize() <= pd.Timestamp(ready)]


def load_raw(code: str) -> pd.DataFrame:
    p = root() / "data" / f"{code}.parquet"
    return drop_partial(pd.read_parquet(p), p.stat().st_mtime)


def adjust(raw: pd.DataFrame) -> pd.DataFrame:
    raw = raw.dropna(subset=["Close", "Adj Close"])
    raw = raw[(raw["Close"] > 0) & (raw["Adj Close"] > 0)]
    f = raw["Adj Close"] / raw["Close"]
    out = pd.DataFrame({"Open": raw["Open"] * f, "High": raw["High"] * f, "Low": raw["Low"] * f,
                        "Close": raw["Adj Close"], "Volume": raw["Volume"].fillna(0.0)}, index=raw.index)
    out = out.dropna(subset=["Open", "High", "Low", "Close"])
    return out[~out.index.duplicated(keep="last")].sort_index()


def load(code: str) -> pd.DataFrame:
    """回测用的复权日线（列 Open High Low Close Volume）。"""
    return adjust(load_raw(code))


def screen(raw: pd.DataFrame, df: pd.DataFrame) -> str:
    """数据/流动性筛选。返回空字符串 = 可以回测；否则返回排除原因。"""
    if len(df) < 250:
        return "上市不满一年"
    if (raw["Close"] <= 0).any() or (raw["Adj Close"] <= 0).any():
        return "价格异常（出现 0 或负数）"
    if (df["Close"].pct_change().abs() > 3.0).any():
        return "价格异常（单日涨跌超过 300%：可能是坏数据，也可能是真实暴跌或反向拆股；整只排除）"     # D87：规则不变（U2 待所有者决定），只改说明
    tail = raw.iloc[-60:]
    dv = (tail["Close"] * tail["Volume"].fillna(0)).median()
    if raw["Close"].iloc[-1] < 1.0:
        return "股价低于 1 美元"
    if dv < 500_000:
        return "流动性太差（近 60 日成交额中位数低于 50 万美元）"
    if (raw["Volume"].iloc[-252:].fillna(0) <= 0).mean() > 0.25:
        return "停牌或无成交的日子太多"
    return ""


MIN_DOLLAR_VOL = 500_000      # 与 screen 同一组门槛（D103：改成按历史时点判断；股价门槛不用，见 eligible_pit）
MAX_ZERO_SHARE = 0.25
MIN_AGE = 250


def hard_screen(raw: pd.DataFrame, df: pd.DataFrame) -> str:
    """D103：整只排除只剩“数据本身不能用”的情况（上市不满一年、出现 0/负价、单日 ±300%）。
    股价、成交额、停牌这些“当时能不能买”的条件改由 eligible_pit 按历史时点逐月判断，不再用今天的值整只排除。"""
    if len(df) < MIN_AGE:
        return "上市不满一年"
    if (raw["Close"] <= 0).any() or (raw["Adj Close"] <= 0).any():
        return "价格异常（出现 0 或负数）"
    if (df["Close"].pct_change().abs() > 3.0).any():
        return "价格异常（单日涨跌超过 300%：可能是坏数据，也可能是真实暴跌或反向拆股；整只排除）"
    return ""


def eligible_pit(raw: pd.DataFrame, index) -> pd.Series:
    """D103：按历史时点的可交易样本。每月第一个交易日调仓：用“上一个交易日收盘为止”的数据判断
    近 60 日成交额中位数 ≥ 50 万美元、近 252 日无成交天数 ≤ 25%、已上市 ≥ 250 个交易日，
    全部满足这个月才算样本内（可以持有），否则整月空仓。门槛和 screen 相同，只是不再用今天的值。
    没有“股价 ≥ 1 美元”这一条（revquant 复核）：Yahoo 的 Close 已按之后的拆股调整过，用它判断当时的股价就是用了未来信息
    （NVDA 2012 年 Close 只有 0.35，当时真实价约 14 美元；反向拆股则相反）。成交额 = Close × Volume，拆股对两者的调整互相抵消，不受影响。
    不用未来数据：第 d 天的结果只取决于 d 之前的数据。返回 index 上的 bool Series。"""
    r = raw.dropna(subset=["Close"])
    r = r[~r.index.duplicated(keep="last")].sort_index()
    px = r["Close"]
    vol = r["Volume"].fillna(0.0)
    dv = (px * vol).rolling(60, min_periods=60).median()
    zero = (vol <= 0).astype(float).rolling(252, min_periods=1).mean()
    age = pd.Series(np.arange(1, len(r) + 1), index=r.index)
    ok = (dv >= MIN_DOLLAR_VOL) & (zero <= MAX_ZERO_SHARE) & (age >= MIN_AGE)
    ok = ok.reindex(index).fillna(False).astype(bool)
    prev = ok.shift(1, fill_value=False)                       # 上一个交易日收盘为止
    idx = pd.DatetimeIndex(index)
    month = idx.to_period("M")
    first = pd.Series(month, index=idx) != pd.Series(month, index=idx).shift(1)
    return prev.where(first.to_numpy()).ffill().fillna(False).astype(bool)


def caps() -> pd.DataFrame:
    """市值名单（爸爸提供，按市值从大到小）：索引 = Yahoo 代码，列 mcap、rank、name。没有这个文件就返回空表。"""
    r = root()
    p = r / "美股市值名单.csv" if r else None
    if not p or not p.exists():
        return pd.DataFrame(columns=["mcap", "rank", "name"])
    c = pd.read_csv(p, dtype={"yahoo": str}).dropna(subset=["yahoo"])
    c["mcap"] = pd.to_numeric(c["mcap"], errors="coerce")
    return c.drop_duplicates("yahoo").set_index("yahoo")[["mcap", "rank", "name"]]
