"""MA5/MA20 moving-average crossover + ADX trend filter + ATR stop.

Rules (one-to-one with amibroker/ma_cross_adx_atr.afl):
  Buy signal: fast MA crosses above slow MA, and ADX > adx_min
  Sell signal: fast MA crosses below slow MA
  Stop: entry price - atr_mult * ATR (using the ATR on the signal day), fixed after entry
  Execution: the signal is generated at the close and filled at the next bar's open (avoids look-ahead bias)
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
