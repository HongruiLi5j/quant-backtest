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


def load_raw(code: str) -> pd.DataFrame:
    return pd.read_parquet(root() / "data" / f"{code}.parquet")


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
        return "价格异常（单日涨跌超过 300%，疑似坏数据）"
    tail = raw.iloc[-60:]
    dv = (tail["Close"] * tail["Volume"].fillna(0)).median()
    if raw["Close"].iloc[-1] < 1.0:
        return "股价低于 1 美元"
    if dv < 500_000:
        return "流动性太差（近 60 日成交额中位数低于 50 万美元）"
    if (raw["Volume"].iloc[-252:].fillna(0) <= 0).mean() > 0.25:
        return "停牌或无成交的日子太多"
    return ""


def caps() -> pd.DataFrame:
    """市值名单（爸爸提供，按市值从大到小）：索引 = Yahoo 代码，列 mcap、rank、name。没有这个文件就返回空表。"""
    r = root()
    p = r / "美股市值名单.csv" if r else None
    if not p or not p.exists():
        return pd.DataFrame(columns=["mcap", "rank", "name"])
    c = pd.read_csv(p, dtype={"yahoo": str}).dropna(subset=["yahoo"])
    c["mcap"] = pd.to_numeric(c["mcap"], errors="coerce")
    return c.drop_duplicates("yahoo").set_index("yahoo")[["mcap", "rank", "name"]]
