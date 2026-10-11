"""Bar-by-bar backtest engine (long only, single symbol, fully invested).

Deliberately written as a loop instead of vectorized: the logic matches the for loop in the AFL code exactly,
which makes trade-by-trade comparison easy and makes each step easier to follow.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .strategy import Params


@dataclass
class Config:
    initial_equity: float = 100_000.0
    commission_pct: float = 0.0005     # one-way commission 0.05%
    stop_on_entry_bar: bool = False    # whether to check the stop on the entry bar (AmiBroker does not by default)


@dataclass
class Result:
    equity: pd.Series
    trades: pd.DataFrame
    signals: pd.DataFrame
    params: Params
    config: Config = field(default_factory=Config)


def run(sig: pd.DataFrame, p: Params, cfg: Config = Config()) -> Result:
    o, h, l, c = (sig[k].to_numpy() for k in ("Open", "High", "Low", "Close"))
    atr_ = sig["atr"].to_numpy()
    buy_sig = sig["buy_sig"].to_numpy()
    sell_sig = sig["sell_sig"].to_numpy()
    dates = sig.index
    n = len(sig)

    cash = cfg.initial_equity
    shares = 0
    entry_i = -1
    entry_px = stop = 0.0
    equity = np.empty(n)
    trades = []

    def close_position(i: int, px: float, reason: str):
        nonlocal cash, shares
        proceeds = shares * px
        fee = proceeds * cfg.commission_pct
        cash += proceeds - fee
        cost = shares * entry_px * (1 + cfg.commission_pct)
        trades.append({
            "entry_date": dates[entry_i], "entry_price": entry_px,
            "exit_date": dates[i], "exit_price": px, "shares": shares,
            "pnl": proceeds - fee - cost,
            "ret_pct": ((proceeds - fee) / cost - 1) * 100,
            "bars": i - entry_i, "exit_reason": reason, "stop": stop,
        })
        shares = 0

    for i in range(n):
        if i > 0:
            if shares > 0:
                # 1) sell signal at yesterday's close -> sell at today's open
                if sell_sig[i - 1]:
                    close_position(i, o[i], "signal")
                # 2) stop loss: if price gaps down at the open, fill at the open price; otherwise fill at the stop price when it is touched intraday
                elif i > entry_i or cfg.stop_on_entry_bar:
                    if o[i] <= stop:
                        close_position(i, o[i], "stop_gap")
                    elif l[i] <= stop:
                        close_position(i, stop, "stop")
            elif buy_sig[i - 1] and not np.isnan(atr_[i - 1]):
                # buy signal at yesterday's close -> buy at today's open, whole shares
                px = o[i]
                qty = math.floor(cash / (px * (1 + cfg.commission_pct)))
                if qty > 0:
                    cash -= qty * px * (1 + cfg.commission_pct)
                    shares, entry_i, entry_px = qty, i, px
                    stop = px - p.atr_mult * atr_[i - 1]
                    if cfg.stop_on_entry_bar and l[i] <= stop:
                        close_position(i, stop, "stop")
        equity[i] = cash + shares * c[i]

    # still holding at the end of the backtest: close at the last close price for easier statistics
    if shares > 0:
        close_position(n - 1, c[-1], "end_of_data")
        equity[-1] = cash

    return Result(
        equity=pd.Series(equity, index=dates, name="equity"),
        trades=pd.DataFrame(trades),
        signals=sig, params=p, config=cfg,
    )
