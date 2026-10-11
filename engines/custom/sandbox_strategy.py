"""Write your own strategy here. After saving, enter a stock code and run the backtest;
it runs on real historical data (in-house engine rules: fills at the next open, A-share fees and price-limit rules).

You only need to write one function:

    def strategy(df: pd.DataFrame) -> pd.Series

- df: daily data for this stock in ascending date order, with columns Open / High / Low / Close / Volume (all back-adjusted prices)
- Return value: a Series with the same length as df.index, giving the target position decided after that day's close:
    0   = no position
    1   = fully invested
    0.5 = half position (any value between 0 and 1)
- The rules are the same as for the 7 built-in strategies: you may only use data up to that day's close, never future prices
  (for example df["Close"].shift(-1) peeks into the future; the backtest would be unreliable and live trading cannot do it)
- You do not handle fill prices, fees or price limits; the backtest engine does that for you. You only write the signal

Below is the simplest possible example: fully invested when price is above the 20-day moving average, in cash when it falls below. Replace it with your own strategy.
"""
from __future__ import annotations

import pandas as pd


def strategy(df: pd.DataFrame) -> pd.Series:
    ma20 = df["Close"].rolling(20).mean()
    return (df["Close"] > ma20).astype(float)
