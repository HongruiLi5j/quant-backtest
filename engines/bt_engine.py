"""用 backtrader 跑一次 A 股回测。

backtrader（https://www.backtrader.com）是国外最常用的开源 Python 回测框架之一，
事件驱动：一根 K 线一根 K 线往前推，策略在每根 K 线上做决定，经纪商（broker）模拟撮合。
"""
from __future__ import annotations

import time

import pandas as pd

from engines import common


def run(code: str, name: str, params: dict | None = None, start: str | None = None,
        cash: float = common.DEFAULT_CASH, df: pd.DataFrame | None = None, info: dict | None = None,
        light: bool = False) -> dict:
    """light=True 时只返回净值（参数优化时用，快一些）。"""
    import backtrader as bt
    from engines import bt_base, bt_strategies

    t0 = time.time()
    params = common.parse_params(name, params)
    if df is None:
        raw, info = common.load_stock(code)
        df, k, note = common.to_forward_adjusted(code, raw)
    else:
        k, note = 1.0, ""
    info = info or {"name": code}
    cls = bt_strategies.STRATEGY_CLASSES[name]

    cerebro = bt.Cerebro(stdstats=False, cheat_on_open=True)
    feed = bt.feeds.PandasData(dataname=df, open="Open", high="High", low="Low", close="Close",
                               volume="Volume", openinterest=None)
    cerebro.adddata(feed, name=code)
    cerebro.broker.setcash(cash)
    cerebro.broker.addcommissioninfo(bt_base.AShareCommission())
    cerebro.broker.set_coc(False)
    kw = {k2: v for k2, v in params.items() if k2 in cls.params._getkeys()}
    kw["limit"] = common.limit_pct(code, info.get("name", ""))
    cerebro.addstrategy(cls, **kw)
    strat = cerebro.run()[0]

    daily = pd.DataFrame(strat.daily, columns=["date", "value", "exposure"])
    daily.index = pd.to_datetime(daily["date"])
    value, exposure = daily["value"], daily["exposure"]
    if light:
        return {"value": value, "index": df.index}
    extra = {
        "price_scale": k, "price_note": note,
        "events": [{"date": d, "msg": m} for d, m in strat.events[-100:]],
        "engine_notes": [
            "撮合：收盘出信号，次日开盘价成交（cheat-on-open，相当于集合竞价下单）",
            "费用：佣金+过户费 万2.6（每笔最低 5 元），卖出印花税 2023-08-28 前 0.1%、之后 0.05%；无滑点",
            "整手：100 股一手，向下取整；资金不够时少买",
            f"涨跌停：开盘涨幅 ≥ {kw['limit'] * 100 - 0.2:.1f}% 不买、跌幅 ≥ {kw['limit'] * 100 - 0.2:.1f}% 不卖，第二天再试",
            "T+1：日线下买入最早下一个交易日才能卖，天然满足",
        ],
    }
    return common.pack_result("backtrader", name, code, info, params, df, value, exposure,
                              strat.fills, start, cash, t0, extra)
