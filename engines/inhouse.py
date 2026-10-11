"""自研引擎（src/portfolio.py）按和另外两个引擎一样的接口包一层，方便三方对比。

自研引擎的做法和两个外部引擎的区别：
  - 按"目标仓位（百分比）"回测，不按股数：没有 100 股整手、没有每笔最低 5 元佣金
  - 手续费是一个固定的单边费率（默认 8bp = 万 8，里面含佣金、平摊的印花税和滑点估计）
  - 这里为了和外部引擎口径一致，空仓的钱不计利息（「回测实验室」里默认按货币基金计息）
"""
from __future__ import annotations

import time

import numpy as np
import pandas as pd

from engines import common


def run(code: str, name: str, params: dict | None = None, start: str | None = None,
        cash: float = common.DEFAULT_CASH, df: pd.DataFrame | None = None, info: dict | None = None,
        light: bool = False, cost_bp: float | None = None) -> dict:
    from src import registry
    from src.portfolio import run_weights

    t0 = time.time()
    params = common.parse_params(name, params)
    if df is None:
        raw, info = common.load_stock(code)
        df, k, note = common.to_forward_adjusted(code, raw)
    else:
        k, note = 1.0, ""
    info = info or {"name": code}
    cost_bp = common.CN.get("stock_cost_bp", 8) if cost_bp is None else cost_bp
    cost = cost_bp / 1e4
    W = registry.target_weights(name, {code: df}, code, params, common.CN)
    lim = common.limit_pct(code, info.get("name", ""))
    res = run_weights({code: df}, W, cost=cost, band=0.10, cash_ret=None, limits={code: lim})
    value = res.equity * cash
    held = res.weights.iloc[:, 0]
    # 收盘时的股票占比（开盘建仓后随当天涨跌漂移）
    r_intra = df["Close"] / df["Open"] - 1
    expo = held * (1 + r_intra.reindex(held.index)) / (1 + held * r_intra.reindex(held.index))
    if light:
        return {"value": value, "index": df.index}

    # 仓位变化 → "成交记录"（股数可以是小数，自研引擎不按整手）
    fills = []
    prev, shares = 0.0, 0.0
    eq_prev = value.shift(1).fillna(cash)
    for d, w in held.items():
        if abs(w - prev) > 1e-12:
            o = float(df.at[d, "Open"])
            new_shares = w * float(eq_prev.at[d]) / o          # 目标仓位对应的（小数）股数
            fills.append({"date": d.strftime("%Y-%m-%d"), "qty": new_shares - shares, "price": o,
                          "fee": abs(w - prev) * float(eq_prev.at[d]) * cost})
            prev, shares = w, new_shares
    extra = {
        "price_scale": k, "price_note": note, "events": [], "cost_bp": cost_bp,
        "engine_notes": [
            "撮合：收盘出信号，次日开盘按目标仓位成交（src/portfolio.py）",
            f"费用：固定单边 {cost_bp:g}bp（含佣金、平摊的印花税和滑点估计），没有最低 5 元",
            "按百分比持仓，不取整到 100 股",
            "涨跌停：开盘涨幅 ≥ 涨停幅度 - 0.2% 不买、跌幅 ≥ 跌停幅度 - 0.2% 不卖",
            "空仓资金不计利息（和两个外部引擎一致）",
        ],
    }
    out = common.pack_result("inhouse", name, code, info, params, df, value, expo, fills, start, cash, t0, extra)
    for t in out["fills"]:
        t["qty"] = round(t["qty"], 2)
    return out
