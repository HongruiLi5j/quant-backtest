"""D108：持仓的自动止损线（只给“我的持仓”，不给自选和信号股；不设上涨目标价）。

每只持仓最多两条线，都是“现有规则里真的会用来卖出”的价位，不是新编的目标价：
  1. 固定止损：持仓成本 × (1 − 15%)。和模拟盘 simtrade.STOP_LOSS 同一个数（直接引用，不另写一份）。
     模拟盘的规则：收盘价 ≤ 这条线，下一个交易日卖出。
  2. 海龟系统 1（donchian_s1）的出场线：只有该策略此刻按规则“持有”这只股票时才给。
     规则（src/strategies.donchian_breakout，参数取注册表默认 20 / 10 / 2×ATR20）：收盘价低于
       a) 前 10 个交易日（截至今天）的最低价，或
       b) 进场以来最高收盘价 − 2 × ATR(20)
     任一条件就出场（下一个交易日开盘卖）。所以明天的出场线 = max(a, b)。
     b 里的 ATR 用今天的值；明天的 ATR 会随明天的波动略变，这是近似（tests 里量过误差）。
     blend2 里海龟只占 1/3：跌破这条线，blend2 只减掉这 1/3。
  ma_adx_plus 也有 3×ATR 跟踪止损，但所有者 D108 只要海龟这一条，这里不加。
单位：全市场文件是后复权价，持仓成本和盘中行情是真实价。策略线用 display_price.scale_for 的系数换成真实价；
  换不到（系数取不到真实价）就不给策略线，绝不把后复权价当真实价显示。
触发口径：规则都按收盘价判断。盘中价格跌破只是“预警”，以收盘为准——字段 basis="close" 写明。
只读：不写任何文件（scale_for 自己的缓存除外，与今日买卖清单共用）。
"""
from __future__ import annotations

import logging
import math

LOG = logging.getLogger('market_monitor')

DONCHIAN = {"entry_len": 20, "exit_len": 10, "atr_len": 20, "atr_mult": 2.0}     # 与 strategies.donchian_s1 / 注册表默认一致（测试核对）


def fixed_stop_pct() -> float:
    from src import simtrade
    return float(simtrade.STOP_LOSS)


def donchian_state(df, entry_len=20, exit_len=10, atr_len=20, atr_mult=2.0):
    """逐日复现 strategies.donchian_breakout 的进出场状态。返回 (on, peak, lo, atr) 四个 list：
    on[t] = 第 t 天收盘后是否持有；peak[t] = 持有时进场以来最高收盘；lo[t] = 前 exit_len 天最低价（不含第 t 天）。"""
    from src.indicators import atr
    c = df["Close"].to_numpy(dtype=float).tolist()
    hi = df["High"].rolling(entry_len).max().shift(1).to_numpy(dtype=float).tolist()
    lo = df["Low"].rolling(exit_len).min().shift(1).to_numpy(dtype=float).tolist()
    a = atr(df, atr_len).to_numpy(dtype=float).tolist()
    on_l, peak_l = [False] * len(c), [math.nan] * len(c)
    on, peak = False, 0.0
    for t in range(len(c)):
        ct, ht, at = c[t], hi[t], a[t]
        if ht != ht or at != at:
            continue
        if not on:
            if ct > ht:
                on, peak = True, ct
        else:
            peak = max(peak, ct)
            if ct < lo[t] or ct < peak - atr_mult * at:
                on = False
        on_l[t] = on
        peak_l[t] = peak if on else math.nan
    return on_l, peak_l, lo, a


def donchian_line(df, entry_len=20, exit_len=10, atr_len=20, atr_mult=2.0) -> dict | None:
    """策略此刻持有时，明天收盘的出场线（后复权价）；不持有 / 数据不够返回 None。"""
    if df is None or len(df) < max(entry_len, exit_len, atr_len) + 2:
        return None
    on, peak, _, a = donchian_state(df, entry_len, exit_len, atr_len, atr_mult)
    if not on[-1]:
        return None
    low_next = float(df["Low"].iloc[-exit_len:].min())             # 明天的 lo = 截至今天的前 exit_len 天最低价
    trail = float(peak[-1] - atr_mult * a[-1])
    if not (math.isfinite(low_next) and math.isfinite(trail)):
        return None
    return {"channel_low": low_next, "atr_trail": trail, "line": max(low_next, trail), "peak": float(peak[-1]), "atr": float(a[-1])}


def default_holdings_cost():
    """{code: 每股成本（真实价）}：T013 持仓台账里仍持有的股票。台账缺失 / 出错返回 {}。"""
    try:
        from src.holdings.calc import replay
        from src.holdings.store import HoldingsStore
        states, _ = replay(HoldingsStore().load_trades())
        return {code: float(st["cost"]) / st["shares"] for code, st in states.items() if st["shares"] > 0 and st["cost"] > 0}
    except Exception:
        LOG.info('stops: holdings source unavailable, degraded to empty')
        return {}


def default_prices(code):
    from src import allmarket
    return allmarket._drop_unfinished(allmarket.load(code))


def default_scale(code, df):
    from src import display_price
    k, note = display_price.scale_for("cn", code, df)
    return k if note == "前复权" and k and math.isfinite(k) and k > 0 else None


def _r2(x):
    return round(float(x), 2)


def stop_lines(costs_fn=default_holdings_cost, prices_fn=default_prices, scale_fn=default_scale) -> dict:
    """{code: {"cost", "lines": [...], "nearest"}}。每条线：kind / price（真实价，2 位小数）/ basis / rule / rule_en / action / action_en。
    nearest = 最高的那条线（价格从上往下最先碰到的）。任何一只出错只丢掉那一只的策略线，不影响别的。"""
    out = {}
    pct = fixed_stop_pct()
    try:
        costs = dict(costs_fn())
    except Exception:
        return out
    for code, cost in costs.items():
        if not (isinstance(cost, (int, float)) and math.isfinite(cost) and cost > 0):
            continue
        lines = [{"kind": "fixed", "price": _r2(cost * (1 - pct)), "basis": "close",
                  "rule": f"成本 {cost:.2f} × (1 − {pct:.0%})：收盘不高于这条线，模拟盘下一个交易日卖出",
                  "rule_en": f"cost {cost:.2f} x (1 - {pct:.0%}): a close at or below this line means the paper account sells the next trading day",
                  "action": "全部卖出（固定止损）", "action_en": "sell all (fixed stop)"}]
        as_of = None
        try:
            df = prices_fn(code)
            d = donchian_line(df, **DONCHIAN)
            k = scale_fn(code, df) if d else None
            if d and k:
                as_of = df.index[-1].strftime("%Y-%m-%d")
                lines.append({"kind": "donchian_s1", "price": _r2(d["line"] * k), "basis": "close", "as_of": as_of,
                              "channel_low": _r2(d["channel_low"] * k), "atr_trail": _r2(d["atr_trail"] * k),
                              "rule": "海龟系统 1：收盘低于前 10 日最低价或进场后最高收盘 − 2×ATR(20)，下一个交易日出场"
                                      "（ATR 用今天的值，明天会略变）",
                              "rule_en": "Turtle system 1: a close below the prior 10-day low or the highest close since entry minus 2xATR(20) "
                                         "exits the next trading day (ATR is today's value and will move slightly tomorrow)",
                              "action": "donchian_s1 全部卖出；blend2 只减掉海龟那 1/3",
                              "action_en": "donchian_s1 sells all; blend2 only drops its 1/3 Turtle part"})
        except Exception:
            LOG.info('stops: strategy line unavailable for %s', code)
        top = max(lines, key=lambda x: x["price"])
        out[code] = {"cost": _r2(cost), "lines": lines, "nearest": {"kind": top["kind"], "price": top["price"]}, "as_of": as_of}
    return out
