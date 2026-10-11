"""逐根 K 线的回测引擎（只做多、单一品种、满仓进出）。

故意写成循环而不是向量化：逻辑和 AFL 里的 for 循环一模一样，
方便两边逐笔对照，也更容易看懂每一步发生了什么。
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
    commission_pct: float = 0.0005     # 单边手续费 0.05%
    stop_on_entry_bar: bool = False    # 入场当天是否检查止损（AmiBroker 默认不检查）


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
                # 1) 昨天收盘出现卖出信号 → 今天开盘卖
                if sell_sig[i - 1]:
                    close_position(i, o[i], "signal")
                # 2) 止损：跳空低开直接按开盘价，否则盘中触及按止损价
                elif i > entry_i or cfg.stop_on_entry_bar:
                    if o[i] <= stop:
                        close_position(i, o[i], "stop_gap")
                    elif l[i] <= stop:
                        close_position(i, stop, "stop")
            elif buy_sig[i - 1] and not np.isnan(atr_[i - 1]):
                # 昨天收盘出现买入信号 → 今天开盘买，整数股
                px = o[i]
                qty = math.floor(cash / (px * (1 + cfg.commission_pct)))
                if qty > 0:
                    cash -= qty * px * (1 + cfg.commission_pct)
                    shares, entry_i, entry_px = qty, i, px
                    stop = px - p.atr_mult * atr_[i - 1]
                    if cfg.stop_on_entry_bar and l[i] <= stop:
                        close_position(i, stop, "stop")
        equity[i] = cash + shares * c[i]

    # 回测结束时还持仓：按最后收盘价平掉，方便统计
    if shares > 0:
        close_position(n - 1, c[-1], "end_of_data")
        equity[-1] = cash

    return Result(
        equity=pd.Series(equity, index=dates, name="equity"),
        trades=pd.DataFrame(trades),
        signals=sig, params=p, config=cfg,
    )
