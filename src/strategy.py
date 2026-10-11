"""MA5/MA20 均线交叉 + ADX 趋势过滤 + ATR 止损。

规则（和 amibroker/ma_cross_adx_atr.afl 一一对应）：
  买入信号：快线上穿慢线，且 ADX > adx_min
  卖出信号：快线下穿慢线
  止损：入场价 - atr_mult * ATR（用信号当天的 ATR），入场后固定不动
  执行：信号在收盘时产生，下一根 K 线开盘价成交（避免未来函数）
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .indicators import adx, atr, cross, sma


@dataclass
class Params:
    fast: int = 5
    slow: int = 20
    adx_len: int = 14
    adx_min: float = 20.0
    atr_len: int = 14
    atr_mult: float = 2.0


def generate_signals(df: pd.DataFrame, p: Params) -> pd.DataFrame:
    out = df.copy()
    out["ma_fast"] = sma(df["Close"], p.fast)
    out["ma_slow"] = sma(df["Close"], p.slow)
    out["adx"] = adx(df, p.adx_len)
    out["atr"] = atr(df, p.atr_len)
    out["buy_sig"] = cross(out["ma_fast"], out["ma_slow"]) & (out["adx"] > p.adx_min)
    out["sell_sig"] = cross(out["ma_slow"], out["ma_fast"])
    out[["buy_sig", "sell_sig"]] = out[["buy_sig", "sell_sig"]].fillna(False)
    return out
