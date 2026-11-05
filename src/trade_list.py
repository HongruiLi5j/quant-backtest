"""收盘后的机械信号清单：复用扫描仓位，按需补算解释和样本外表现。

此模块不下载全市场数据、不修改策略、不记录跟踪信号。发布失败时保留上一份清单。
"""
from __future__ import annotations

import argparse
import inspect
import json
import logging
import math
import re
import time as clock_time
from datetime import timedelta
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from config import MARKETS, TRADE_LIST_THRESHOLD, TRADE_LIST_LIMIT_TOLERANCE, TRADE_LIST_MIN_TARGET, TRADE_LIST_MIN_TURNOVER
from . import allmarket, datasource, display_price, market_clock, registry, scan, strategies, turnover
from .indicators import adx, atr, cross, realized_vol, rsi, sma
from .portfolio import run_weights

CACHE = datasource.CACHE
EPS = 1e-9
VERSION = 4
ACTIONS = ("买入", "卖出", "加仓", "减仓")
BINARY = {"rsi2", "ma_adx_original"}
DISCLAIMER = "这是按回测策略机械生成的信号清单，不是投资建议。"
LIMITATIONS = [
    "多重检验：同时查看很多股票和策略，很容易把偶然的好表现当成规律。",
    "幸存者偏差：表里的历史回看只包含今天还在交易的股票（数据里没有退市股），结果偏乐观。",   # D31 口径：退市股 0 只
    "成本和滑点：回测按既有引擎估算费用，实际成交价、冲击成本可能更差。",
    "涨跌停与停牌：信号不保证成交；今日一字板也不能确定明日是否能成交。",
    "2020 年以后的回测我已经看过，所以不算干净的样本外；表里「2020 年起」的数字只是时间切分。",
]
PERFORMANCE_NOTE = (
    "2020 年起（回看，不是样本外）：先用完整历史计算指标与持仓，再统计 2020-01-01 起且完成扫描的 250 个交易日预热后的收益。"
    "策略与买入持有使用相同日期、个股单边 8bp 和既有涨跌停模型。"
    "交易次数仅计统计区间内开仓且已平仓的完整交易，跨期持仓与未平仓不计次数，但收益计入同期收益。"
    "blend2 由三个子策略叠加，一笔交易指总仓位大于 0 的连续时段，不等于单策略的入场次数。"
    "2020 年起 Sharpe（扣现金收益）直接读取扫描结果，其可用起点与这里的预热口径一致。"
)


class PublishError(ValueError):
    """输入未通过发布检查，不覆盖有效清单。"""


def number(value):
    try:
        x = float(value)
        return x if math.isfinite(x) else None
    except (ValueError, TypeError):
        return None


def classify(now, nxt, threshold=None):
    """严格按浮点数比较，与扫描及引擎调仓带一致，不做边界舍入修正。"""
    a, b = number(now), number(nxt)
    threshold = TRADE_LIST_THRESHOLD if threshold is None else threshold
    if a is None or b is None or a < 0 or b < 0:
        return None
    if a < EPS and b >= EPS:
        return "买入"
    if a >= EPS and b < EPS:
        return "卖出"
    if a >= EPS and b >= EPS:
        delta = b - a
        if abs(delta) > threshold:
            return "加仓" if delta > 0 else "减仓"
    return None


def valid_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise PublishError("日期必须为 YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise PublishError("日期无效") from exc


def _market(market):
    if market != "cn":
        raise PublishError("今日买卖目前只支持 A 股")


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _clock(now=None):
    if now is not None and (now.tzinfo is None or now.utcoffset() is None):
        raise PublishError("检查时间必须带时区")
    return (now or datetime.now(ZoneInfo("Asia/Shanghai"))).astimezone(ZoneInfo("Asia/Shanghai"))


def check_release(progress, meta, release_day, now=None):
    day, current = valid_date(release_day), _clock(now)
    if progress.get("complete") is not True:
        raise PublishError("全市场更新未明确完成，拒绝发布")
    if progress.get("trade_day") != release_day or meta.get("data_date") != release_day:
        raise PublishError("进度日期、扫描元数据日期、待发布日期不一致，拒绝发布")
    if not market_clock.bar_usable(day, current):
        raise PublishError("待发布日期尚未到北京时间 15:30，拒绝使用当天 K 线")


def board_limit(code):
    """板块涨跌停幅度（最新规则），统一委托 src/limits.py（D17）。清单已排除 ST 和上市不满一年的新股，所以只按板块。
    北交所仅预留识别，清单范围仍由原沪深扫描决定。"""
    from .limits import limit_pct
    return limit_pct(code, "")


def _ledger_close_rate():
    """D30：从信号核对账本 latest.json 读「收盘涨停的买入信号次日开盘仍涨停」的历史比例。读不到返回 None。"""
    from . import signal_ledger
    p = signal_ledger.OUT / "latest.json"
    try:
        st = p.stat()
        if _CLOSE_RATE_CACHE.get("key") != (st.st_mtime_ns, st.st_size):
            _CLOSE_RATE_CACHE.update(key=(st.st_mtime_ns, st.st_size), val=json.loads(p.read_text(encoding="utf-8")).get("limit_close"))
        return _CLOSE_RATE_CACHE.get("val")
    except (OSError, ValueError):
        return None


_CLOSE_RATE_CACHE: dict = {}


def close_limit_text(stats=None):
    """收盘涨停的提示文字；比例来自账本（不写死），样本不足时只说「可能」。"""
    s = (stats or {}).get("close_limit") if isinstance(stats, dict) else None
    if stats and stats.get("enough") and s and s.get("rate") is not None:
        return f"今日收盘涨停：历史上约 {s['rate']:.0%} 次日开盘仍涨停买不进（账本 {s['n']} 条）"
    return "今日收盘涨停：明天开盘可能仍涨停买不进（历史比例样本不足）"


def limit_flags(code, df, close_rate=None):
    """一字板：沿用原提示。D30：不是一字板、但收盘在涨停价 → 也提示，比例从账本读（close_rate 可注入，测试用）。"""
    if len(df) < 2:
        return []
    bar = df.iloc[-1]
    values = [number(bar[k]) for k in ("Open", "High", "Low", "Close")]
    previous = number(df.Close.iloc[-2])
    if any(x is None for x in values) or previous is None or previous <= 0:
        return []
    change, limit = values[-1] / previous - 1, board_limit(code)
    if not all(math.isclose(x, values[0], rel_tol=1e-10, abs_tol=1e-10) for x in values[1:]):
        if change >= limit - TRADE_LIST_LIMIT_TOLERANCE:
            return [close_limit_text(close_rate if close_rate is not None else _ledger_close_rate())]
        return []
    if change >= limit - TRADE_LIST_LIMIT_TOLERANCE:
        return ["今日一字涨停，明天可能仍买不进"]
    if change <= -limit + TRADE_LIST_LIMIT_TOLERANCE:
        return ["今日一字跌停，明天可能仍卖不出"]
    return []


def replay(code, name, strategy, df, cash=None, cfg=None):
    """与 scan_one 完全相同的策略、成本、调仓带和现金收益，历史不从 2020 截断。"""
    cfg = cfg or MARKETS["cn"]
    weights = registry.target_weights(strategy, {code: df}, code, registry.clean_params(strategy, {}), cfg)
    rf = cash.reindex(df.index).fillna(0.0) if cash is not None else pd.Series(0.0, index=df.index)
    limits = {code: datasource.limit_pct(code, name)}
    result = run_weights({code: df}, weights, cost=scan.COST, band=0.10, cash_ret=rf, limits=limits)
    bench = run_weights({code: df}, pd.DataFrame({code: 1.0}, index=df.index), cost=scan.COST, limits=limits)
    return weights, result, bench


def performance(result, bench, sharpe_oos, history_rows):
    out = {"scope": "样本外", "start": None, "end": None, "maxdd": None, "trades": None,
           "total": None, "b_total": None, "beat_bh": None, "sharpe": number(sharpe_oos),
           "small_sample": None}
    if history_rows < scan.MIN_EVAL:
        return out
    start = max(pd.Timestamp(scan.SPLIT), result.returns.index[250])
    r = result.returns.loc[start:]
    b = bench.returns.reindex(r.index)
    if len(r) < 2 or not np.isfinite(r.to_numpy()).all() or not np.isfinite(b.to_numpy()).all():
        return out
    eq = (1 + r).cumprod().to_numpy()
    # 把区间起点净值 1 纳入峰值，避免漏掉样本外第一天的下跌。
    peaks = np.maximum.accumulate(np.r_[1.0, eq])[1:]
    total, bh = float((eq[-1] - 1) * 100), float(((1 + b).prod() - 1) * 100)
    t = result.trades
    completed = t[(t.entry_date >= r.index[0]) & (t.exit_date <= r.index[-1])].copy()
    if len(t) and result.weights.iloc[-1].sum() > EPS:
        completed = completed[completed.entry_date != t.iloc[-1].entry_date]
    count = len(completed)
    out.update(start=str(r.index[0].date()), end=str(r.index[-1].date()),
               maxdd=float((eq / peaks - 1).min() * 100), trades=count, total=total,
               b_total=bh, beat_bh=total > bh, small_sample=count < 10)
    return out


def _f(value):
    x = number(value)
    return "数据不足" if x is None else f"{x:.2f}"


def _segment_start(state, at):
    """从现有策略返回的状态定位连续持仓起点，不另写入场/退出规则。"""
    off = np.flatnonzero(state.iloc[:at + 1].to_numpy() <= EPS)
    return int(off[-1] + 1) if len(off) else 0


STRATEGY_FUNCTIONS = {
    "trend_ens": strategies.trend_ensemble, "trend_vol": strategies.trend_vol_target,
    "donchian_s1": strategies.donchian_s1, "rsi2": strategies.rsi2_reversion,
    "ma_adx_original": strategies.ma_adx_original, "ma_adx_plus": strategies.ma_adx_plus,
}


def _defaults(fn):
    return {key: p.default for key, p in inspect.signature(fn).parameters.items()
            if p.default is not inspect.Parameter.empty}


def _parameters(strategy, raw=None):
    defaults = _defaults(STRATEGY_FUNCTIONS[strategy]) if strategy in STRATEGY_FUNCTIONS else {}
    params = {**defaults, **registry.clean_params(strategy, raw or {})}
    # 未登记在网页中的指标周期取策略函数默认值；解释测试也允许显式覆盖这些参数。
    params.update({key: value for key, value in (raw or {}).items() if key in defaults})
    return params


def _t(lang, zh, en):
    """D89：同一句话的中文 / 英文；逻辑和数字完全相同，只换措辞。"""
    return en if lang == "en" else zh


def explain(strategy, df, k=1.0, params=None, lang="zh"):
    """文字只解释既有策略；参数来自登记表及策略函数签名，绝不用于重新选股。lang="en" 返回英文（D89），数字和判断完全一样。"""
    T = lambda zh, en: _t(lang, zh, en)
    sep, semi = T("、", ", "), T("；", "; ")
    c, p = df.Close, _parameters(strategy, params)
    close = float(c.iloc[-1])
    price = lambda x: _f(x * k)
    vol_len = p.get("vol_len", _defaults(strategies.vol_scale)["vol_len"])
    target = p.get("target_vol", _defaults(strategies.trend_ensemble)["target_vol"])
    vol = realized_vol(c, vol_len).iloc[-1]
    vscale = strategies.vol_scale(c, target or None, vol_len, p.get("max_lev", 1.0)).iloc[-1]
    vol_text = (T(f"{vol_len} 日年化波动率 {_f(vol * 100)}%，仓位系数 "
                  f"min({_f(p.get('max_lev', 1.0))},{_f(target * 100)}%/波动率)={_f(vscale)}",
                  f"{vol_len}-day annualized volatility {_f(vol * 100)}%, position scale "
                  f"min({_f(p.get('max_lev', 1.0))},{_f(target * 100)}%/volatility)={_f(vscale)}")
                if target else T("不按波动率缩放仓位，仓位系数 1", "no volatility scaling, position scale 1"))
    if strategy in ("trend_ens", "trend_vol"):
        lens = p["lens"] if strategy == "trend_ens" else (p["sma_len"],)
        parts = []
        for n in lens:
            mean = sma(c, n).iloc[-1]
            state = strategies.trend_vol_target(df, sma_len=n, buffer=p["buffer"], target_vol=None)
            parts.append(T(f"MA{n}={price(mean)}，入场需收盘>{price(mean * (1 + p['buffer']))}，"
                           f"退出需收盘<{price(mean * (1 - p['buffer']))}，迟滞状态{'开' if state.iloc[-1] else '关'}",
                           f"MA{n}={price(mean)}, entry needs close>{price(mean * (1 + p['buffer']))}, "
                           f"exit needs close<{price(mean * (1 - p['buffer']))}, buffer state: {'on' if state.iloc[-1] else 'off'}"))
        return (T(f"收盘 {price(close)}；", f"Close {price(close)}; ") + semi.join(parts) +
                T(f"；{p['buffer']:.0%} 迟滞区间内延续原状态；{vol_text}；状态均值乘系数得到目标仓位。",
                  f"; inside the {p['buffer']:.0%} buffer band the previous state is kept; {vol_text}; "
                  f"the average state times the scale gives the target position."))
    if strategy == "donchian_s1":
        atr_len = _defaults(strategies.donchian_breakout)["atr_len"]
        # 系统 1 的包装函数显式使用 ATR(20)，从它调用的既有实现读取该固定参数。
        # 用函数签名核对 ATR 的现有默认周期；系统 1 当前也使用相同周期。
        hi = df.High.rolling(p["entry_len"]).max().shift(1).iloc[-1]
        lo = df.Low.rolling(p["exit_len"]).min().shift(1).iloc[-1]
        a = atr(df, atr_len).iloc[-1]
        state = strategies.donchian_s1(df, **{**p, "target_vol": None})
        was_on = len(state) > 1 and state.iloc[-2] > EPS
        stop = c.iloc[_segment_start(state, len(state) - 2):].max() - p["atr_mult"] * a if was_on else None
        exits = []
        if was_on and close < lo:
            exits.append(T(f"收盘低于前 {p['exit_len']} 日最低", f"close below the prior {p['exit_len']}-day low"))
        if was_on and stop is not None and close < stop:
            exits.append(T("收盘低于 ATR 跟踪止损线", "close below the ATR trailing stop"))
        status = T("实际退出：", "Actual exit: ") + sep.join(exits) if exits else (
            T(f"实际入场：收盘突破前 {p['entry_len']} 日最高", f"Actual entry: close broke the prior {p['entry_len']}-day high")
            if not was_on and state.iloc[-1] > EPS else T("今日未触发新的入场/退出，沿用策略状态", "no new entry/exit today; strategy state unchanged"))
        return T(f"收盘 {price(close)}；不含当天的前 {p['entry_len']} 日最高 {price(hi)}、前 {p['exit_len']} 日最低 {price(lo)}；"
                 f"ATR({atr_len})={price(a)}；跟踪止损线=持仓以来最高收盘−{p['atr_mult']:g}×ATR："
                 f"{price(stop) if stop is not None else '未持仓，无有效止损线'}；{status}；{vol_text}。",
                 f"Close {price(close)}; prior {p['entry_len']}-day high (excluding today) {price(hi)}, prior {p['exit_len']}-day low {price(lo)}; "
                 f"ATR({atr_len})={price(a)}; trailing stop = highest close since entry − {p['atr_mult']:g}×ATR: "
                 f"{price(stop) if stop is not None else 'not holding, no active stop'}; {status}; {vol_text}.")
    if strategy == "rsi2":
        r = rsi(c, p["rsi_len"]).iloc[-1]
        mean, ex = sma(c, p["trend_len"]).iloc[-1], sma(c, p["exit_ma"]).iloc[-1]
        state = strategies.rsi2_reversion(df, **p)
        was_on = len(state) > 1 and state.iloc[-2] > EPS
        held = len(df) - 1 - _segment_start(state, len(state) - 2) if was_on else 0
        exits = []
        if was_on and close > ex:
            exits.append(T(f"收盘高于 MA{p['exit_ma']}", f"close above MA{p['exit_ma']}"))
        if was_on and held >= p["max_hold"]:
            exits.append(T(f"持有达到 {p['max_hold']} 天", f"held {p['max_hold']} days"))
        return T(f"RSI({p['rsi_len']})={_f(r)}，入场需 RSI<{p['entry_th']:g} 且收盘>MA{p['trend_len']}；收盘 {price(close)}，"
                 f"MA{p['trend_len']}={price(mean)}；退出需收盘>MA{p['exit_ma']}（{price(ex)}）或持有达到 {p['max_hold']} 天；"
                 f"策略持有计数 {held} 天；实际退出条件：{'、'.join(exits) if exits else '未触发'}；"
                 f"今日策略状态：{'满仓' if state.iloc[-1] else '空仓'}。",
                 f"RSI({p['rsi_len']})={_f(r)}, entry needs RSI<{p['entry_th']:g} and close>MA{p['trend_len']}; close {price(close)}, "
                 f"MA{p['trend_len']}={price(mean)}; exit needs close>MA{p['exit_ma']} ({price(ex)}) or holding {p['max_hold']} days; "
                 f"days held {held}; exit conditions met: {', '.join(exits) if exits else 'none'}; "
                 f"strategy state today: {'fully invested' if state.iloc[-1] else 'in cash'}.")
    if strategy in ("ma_adx_original", "ma_adx_plus"):
        fast, slow = sma(c, p["fast"]), sma(c, p["slow"])
        strength = adx(df, p["adx_len"]).iloc[-1]
        up, down = bool(cross(fast, slow).iloc[-1]), bool(cross(slow, fast).iloc[-1])
        text = T(f"MA{p['fast']} 昨日/今日={price(fast.iloc[-2])}/{price(fast.iloc[-1])}，"
                 f"MA{p['slow']} 昨日/今日={price(slow.iloc[-2])}/{price(slow.iloc[-1])}；"
                 f"今日金叉={'是' if up else '否'}，死叉={'是' if down else '否'}；ADX({p['adx_len']})={_f(strength)}，入场需金叉且 ADX>{p['adx_min']:g}",
                 f"MA{p['fast']} yesterday/today={price(fast.iloc[-2])}/{price(fast.iloc[-1])}, "
                 f"MA{p['slow']} yesterday/today={price(slow.iloc[-2])}/{price(slow.iloc[-1])}; "
                 f"golden cross today={'yes' if up else 'no'}, death cross={'yes' if down else 'no'}; ADX({p['adx_len']})={_f(strength)}, entry needs a golden cross and ADX>{p['adx_min']:g}")
        if strategy == "ma_adx_original":
            state = strategies.ma_adx_original(df, **p)
            actual = bool(state.iloc[-2] > EPS and down)
            return text + T(f"；退出条件为死叉，今日实际退出={'是' if actual else '否'}。",
                            f"; exit on a death cross, actual exit today={'yes' if actual else 'no'}.")
        state = strategies.ma_adx_plus(df, **{**p, "target_vol": None})
        mean, a = sma(c, p["regime_len"]).iloc[-1], atr(df, p["atr_len"]).iloc[-1]
        was_on = state.iloc[-2] > EPS
        stop = c.iloc[_segment_start(state, len(state) - 2):].max() - p["atr_mult"] * a if was_on else None
        exits = []
        if was_on and down:
            exits.append(T("死叉", "death cross"))
        if was_on and close < mean:
            exits.append(T(f"收盘低于 MA{p['regime_len']}", f"close below MA{p['regime_len']}"))
        if was_on and stop is not None and close < stop:
            exits.append(T("收盘低于 ATR 跟踪止损线", "close below the ATR trailing stop"))
        return text + T(f" 且收盘>MA{p['regime_len']}（{price(mean)}）；收盘 {price(close)}，ATR({p['atr_len']})={price(a)}；"
                        f"跟踪止损线=持仓以来最高收盘−{p['atr_mult']:g}×ATR：{price(stop) if stop is not None else '未持仓，无有效止损线'}；"
                        f"退出条件：死叉、收盘低于止损线或 MA{p['regime_len']}，任一满足即退出；"
                        f"今日实际退出：{'、'.join(exits) if exits else '未触发'}；{vol_text}。",
                        f" and close>MA{p['regime_len']} ({price(mean)}); close {price(close)}, ATR({p['atr_len']})={price(a)}; "
                        f"trailing stop = highest close since entry − {p['atr_mult']:g}×ATR: {price(stop) if stop is not None else 'not holding, no active stop'}; "
                        f"exit on a death cross, a close below the stop or below MA{p['regime_len']}, whichever comes first; "
                        f"actual exit today: {', '.join(exits) if exits else 'none'}; {vol_text}.")
    if strategy == "blend2":
        keys = ("trend_ens", "rsi2", "donchian_s1")
        components = [STRATEGY_FUNCTIONS[key](df).iloc[-1] for key in keys]
        total = p["w_trend"] + p["w_rsi"] + p["w_donchian"]
        mix = tuple(p[x] / total for x in ("w_trend", "w_rsi", "w_donchian")) if total > 0 else (1 / 3,) * 3
        target = sum(w * x for w, x in zip(mix, components))
        labels = ("趋势集成", "RSI2", "海龟") if lang != "en" else ("Trend ensemble", "RSI2", "Turtle")
        return (T("子策略明日目标：", "Sub-strategy targets for tomorrow: ") + T("，", ", ").join(f"{label} {_f(x * 100)}% × {_f(w * 100)}%"
                for label, x, w in zip(labels, components, mix)) +
                T(f"；加权结果 {_f(target * 100)}%。", f"; weighted result {_f(target * 100)}%. ") + " ".join(
                    explain(key, df, k, params=_defaults(STRATEGY_FUNCTIONS[key]), lang=lang) for key in keys))
    return T("无可用策略解释。", "No explanation available for this strategy.")


def _trigger(strategy, df, k, p, lang="zh"):
    """D12：子策略今天真正触发的条件（一句话）；没有新的入场 / 退出返回 None。
    只比较既有策略函数返回的昨天 / 今天状态，条件文字和 explain() 用同样的指标，不另写入场 / 退出规则。lang="en"：D89 英文。"""
    T = lambda zh, en: _t(lang, zh, en)
    sep = T("、", ", ")
    c = df.Close
    close = float(c.iloc[-1])
    price = lambda x: _f(x * k)
    on = lambda st: bool(st.iloc[-1] > EPS)
    was = lambda st: len(st) > 1 and bool(st.iloc[-2] > EPS)
    if strategy in ("trend_ens", "trend_vol"):
        lens = p["lens"] if strategy == "trend_ens" else (p["sma_len"],)
        ups, downs, n_was, n_now = [], [], 0, 0
        for n in lens:
            st = strategies.trend_vol_target(df, sma_len=n, buffer=p["buffer"], target_vol=None)
            mean = sma(c, n).iloc[-1]
            n_was, n_now = n_was + was(st), n_now + on(st)
            if on(st) and not was(st):
                ups.append(T(f"站上 MA{n} 上沿 {price(mean * (1 + p['buffer']))}", f"crossed above the MA{n} upper band {price(mean * (1 + p['buffer']))}"))
            elif was(st) and not on(st):
                downs.append(T(f"跌破 MA{n} 下沿 {price(mean * (1 - p['buffer']))}", f"fell below the MA{n} lower band {price(mean * (1 - p['buffer']))}"))
        if not ups and not downs:
            return None
        return T(f"收盘 {price(close)} ", f"Close {price(close)} ") + sep.join(ups + downs) + \
            T(f"（站上均线 {n_was}→{n_now} 条）", f" (averages above: {n_was}→{n_now})")
    if strategy == "donchian_s1":
        st = strategies.donchian_s1(df, **{**p, "target_vol": None})
        if on(st) and not was(st):
            hi = df.High.rolling(p["entry_len"]).max().shift(1).iloc[-1]
            return T(f"入场：收盘 {price(close)} 突破前 {p['entry_len']} 日最高 {price(hi)}",
                     f"Entry: close {price(close)} broke the prior {p['entry_len']}-day high {price(hi)}")
        if was(st) and not on(st):
            lo = df.Low.rolling(p["exit_len"]).min().shift(1).iloc[-1]
            a = atr(df, _defaults(strategies.donchian_breakout)["atr_len"]).iloc[-1]
            stop = c.iloc[_segment_start(st, len(st) - 2):].max() - p["atr_mult"] * a
            why = [T(f"收盘 {price(close)} 低于前 {p['exit_len']} 日最低 {price(lo)}",
                     f"close {price(close)} below the prior {p['exit_len']}-day low {price(lo)}")] if close < lo else []
            if close < stop:
                why.append(T(f"收盘 {price(close)} 低于跟踪止损线 {price(stop)}", f"close {price(close)} below the trailing stop {price(stop)}"))
            return T("退出：", "Exit: ") + (sep.join(why) if why else T("按策略状态退出", "per strategy state"))
        return None
    if strategy == "rsi2":
        st = strategies.rsi2_reversion(df, **p)
        if on(st) and not was(st):
            r = rsi(c, p["rsi_len"]).iloc[-1]
            return T(f"入场：RSI({p['rsi_len']})={_f(r)} 低于 {p['entry_th']:g}，"
                     f"收盘 {price(close)} 高于 MA{p['trend_len']} {price(sma(c, p['trend_len']).iloc[-1])}",
                     f"Entry: RSI({p['rsi_len']})={_f(r)} below {p['entry_th']:g}, "
                     f"close {price(close)} above MA{p['trend_len']} {price(sma(c, p['trend_len']).iloc[-1])}")
        if was(st) and not on(st):
            ex = sma(c, p["exit_ma"]).iloc[-1]
            held = len(df) - 1 - _segment_start(st, len(st) - 2)
            why = [T(f"收盘 {price(close)} 高于 MA{p['exit_ma']} {price(ex)}", f"close {price(close)} above MA{p['exit_ma']} {price(ex)}")] if close > ex else []
            if held >= p["max_hold"]:
                why.append(T(f"持有满 {p['max_hold']} 天", f"held {p['max_hold']} days"))
            return T("退出：", "Exit: ") + (sep.join(why) if why else T("按策略状态退出", "per strategy state"))
        return None
    if strategy in ("ma_adx_original", "ma_adx_plus"):
        fn = strategies.ma_adx_original if strategy == "ma_adx_original" else strategies.ma_adx_plus
        st = fn(df, **p) if strategy == "ma_adx_original" else fn(df, **{**p, "target_vol": None})
        fast, slow = sma(c, p["fast"]), sma(c, p["slow"])
        if on(st) and not was(st):
            return T(f"入场：MA{p['fast']} 上穿 MA{p['slow']}，ADX({p['adx_len']})={_f(adx(df, p['adx_len']).iloc[-1])}",
                     f"Entry: MA{p['fast']} crossed above MA{p['slow']}, ADX({p['adx_len']})={_f(adx(df, p['adx_len']).iloc[-1])}")
        if was(st) and not on(st):
            why = [T("死叉", "death cross")] if bool(cross(slow, fast).iloc[-1]) else []
            if strategy == "ma_adx_plus" and close < sma(c, p["regime_len"]).iloc[-1]:
                why.append(T(f"收盘低于 MA{p['regime_len']}", f"close below MA{p['regime_len']}"))
            return T("退出：", "Exit: ") + (sep.join(why) if why else T("收盘低于 ATR 跟踪止损线或按策略状态退出",
                                                                          "close below the ATR trailing stop or per strategy state"))
        return None
    return None


def headline(strategy, df, k=1.0, params=None, lang="zh"):
    """D12（CP6）：信号表「触发原因」的第一行，只写今天真正变化的部分；完整推导仍在 explain()。
    组合策略：列出目标变了的子策略（昨天→今天）和它触发的条件；都没变时说明动作来自实际仓位与目标的差额。lang="en"：D89 英文。"""
    T = lambda zh, en: _t(lang, zh, en)
    p = _parameters(strategy, params)
    if strategy == "blend2":
        keys = ("trend_ens", "rsi2", "donchian_s1")
        labels = ("趋势集成", "RSI2", "海龟") if lang != "en" else ("Trend ensemble", "RSI2", "Turtle")
        total = p["w_trend"] + p["w_rsi"] + p["w_donchian"]
        mix = tuple(p[x] / total for x in ("w_trend", "w_rsi", "w_donchian")) if total > 0 else (1 / 3,) * 3
        parts, target = [], 0.0
        for key, label, w in zip(keys, labels, mix):
            comp = STRATEGY_FUNCTIONS[key](df)
            prev, now = (float(comp.iloc[-2]) if len(comp) > 1 else 0.0), float(comp.iloc[-1])
            target += w * now
            if abs(now - prev) > 0.005:
                trig = _trigger(key, df, k, _defaults(STRATEGY_FUNCTIONS[key]), lang=lang)
                parts.append(f"{label} {_f(prev * 100)}%→{_f(now * 100)}%" + ((T("：", ": ") + trig) if trig else T("（波动率系数变化）", " (volatility scale changed)")))
        if parts:
            return T("；", "; ").join(parts) + T(f"。加权目标 {_f(target * 100)}%。", f". Weighted target {_f(target * 100)}%.")
        return T(f"子策略目标与昨日相同（加权 {_f(target * 100)}%），今天没有新触发的条件；清单动作来自今日实际仓位与目标的差额。",
                 f"Sub-strategy targets unchanged from yesterday (weighted {_f(target * 100)}%); nothing new triggered today; "
                 f"the list action comes from the gap between today's actual position and the target.")
    if strategy not in STRATEGY_FUNCTIONS:
        return None
    return _trigger(strategy, df, k, p, lang=lang) or T("今天没有新触发的入场 / 退出条件，沿用策略状态。",
                                                        "No new entry / exit triggered today; strategy state unchanged.")


def _fill_display(row, strategy, df, factor, note):
    calibrated = note == "前复权" and number(factor) is not None and factor > 0
    row.update(close=float(df.Close.iloc[-1]) * factor if calibrated else None,
               price_note="前复权" if calibrated else "数据不足（价格暂不可用）",
               reason=explain(strategy, df, factor if calibrated else float("nan")))
    try:   # D89：英文版，同样的数字和判断；出错只缺英文，不影响清单
        row["reason_en"] = explain(strategy, df, factor if calibrated else float("nan"), lang="en")
    except Exception:
        logging.exception("英文解释生成失败：%s", row.get("code"))
        row["reason_en"] = None
    try:   # D12：一句话触发原因；出错不影响清单（前端没有 trigger 时照旧显示 reason）
        row["trigger"] = headline(strategy, df, factor if calibrated else float("nan"))
        row["trigger_en"] = headline(strategy, df, factor if calibrated else float("nan"), lang="en")
    except Exception:
        logging.exception("触发原因生成失败：%s", row.get("code"))
        row["trigger"] = row.get("trigger")
        row["trigger_en"] = None
    row["lot_cost"] = turnover.lot_cost(row["close"], calibrated)
    row["flags"] = [flag for flag in row["flags"] if flag != "价格暂不可用"]
    if not calibrated:
        row["flags"].append("价格暂不可用")
    return calibrated


def _wait_price_cooldown():
    remaining = max(0, display_price._fail_until - clock_time.time())
    if remaining:
        logging.info("价格接口冷却中，等待约 %.0f 秒后仅补取缺价股票", remaining)
    while remaining > 0:
        clock_time.sleep(min(30, remaining))
        remaining = max(0, display_price._fail_until - clock_time.time())


def _signature(paths):
    return [(p.stat().st_mtime_ns, p.stat().st_size) for p in paths]


def _atomic_json(path, payload):
    """临时文件校验后替换；失败留下临时文件，不删除文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=1), encoding="utf-8")
    if _read(tmp) != payload:
        raise PublishError("清单临时文件校验失败")
    tmp.replace(path)


def generate(market="cn", release_day=None, *, cache=None, now=None, loader=None, scale=None):
    """只读取明确的扫描文件及有信号个股，market 参数为后续市场扩展预留。"""
    _market(market)
    base = Path(cache) if cache is not None else CACHE
    from . import trade_signals
    trade_signals.initialize(cache=base, now=now)
    paths = [base / name for name in ("cn_all_progress.json", "scan_cn_meta.json", "scan_cn.parquet", "cn_all_suspended.json")]
    source = _signature(paths)
    progress, meta = _read(paths[0]), _read(paths[1])
    day = release_day or progress.get("trade_day")
    check_release(progress, meta, day, now)
    suspended_data = _read(paths[3])
    if day not in suspended_data or not isinstance(suspended_data[day], list):
        raise PublishError("缺少发布日的停牌记录，拒绝发布")
    suspended = set(suspended_data[day])
    # 旁表/日历缺失或损坏只影响流动性展示，不影响信号发布与重试条件。
    try:
        turnover_table = turnover.load(base / "turnover_cn.parquet")
        calendar_path = base / "cn_trade_calendar.csv"
        turnover_calendar = pd.to_datetime(pd.read_csv(calendar_path)["trade_date"]).tolist() if calendar_path.exists() else []
        turnover_calendar = sorted(d for d in turnover_calendar if d.date() <= valid_date(day))[-20:]
    except Exception as exc:
        logging.warning("成交额数据暂不可用，清单按流动性未知发布：%s", exc)
        turnover_table, turnover_calendar = pd.DataFrame(columns=turnover.COLUMNS), []
    liquidity_cache = {}
    table = pd.read_parquet(paths[2])
    required = {"code", "name", "strategy", "last_date", "now", "next", "delisted"}
    if not required.issubset(table.columns):
        raise PublishError("扫描结果缺少必要字段")
    table = table[~table.delisted.fillna(False)].copy()
    table = table[table.code.astype(str).str.fullmatch(r"(?:60|68|00|30)\d{4}")]
    table = table[~table.name.str.upper().str.contains("ST|退", regex=True, na=False)]
    if table.empty or not (table.last_date == day).any():
        raise PublishError("整批扫描日线日期与待发布日期不一致，拒绝发布")
    if table.duplicated(["code", "strategy"]).any():
        raise PublishError("扫描存在重复股票/策略，拒绝发布")
    if set(scan.STRATS) - set(table.strategy):
        raise PublishError("扫描策略不完整，拒绝发布")
    latest = base / "trade_list_cn.json"
    if latest.exists() and _read(latest).get("data_date", "") > day:
        raise PublishError("不允许用较早数据覆盖最新清单")
    # 测试通过注入数据源隔离真实数据；生产环境只按代码读取，不遍历行情目录。
    loader = loader or (lambda code: allmarket._drop_unfinished(allmarket.load(code)))
    scale = scale or display_price.scale_for
    cash_df = datasource.load_cached(market, MARKETS[market]["cash"]) if cache is None else pd.DataFrame()
    cash = cash_df.Close.pct_change() if not cash_df.empty else None
    result = {"version": VERSION, "market": market, "data_date": day,
              "generated_at": _clock(now).isoformat(timespec="seconds"), "timezone_label": "北京时间",
              "threshold": TRADE_LIST_THRESHOLD, "min_target": TRADE_LIST_MIN_TARGET, "min_turnover": TRADE_LIST_MIN_TURNOVER, "default_strategy": "blend2", "disclaimer": DISCLAIMER,
              "limitations": LIMITATIONS, "performance_note": PERFORMANCE_NOTE,
              "scan_generated_at": meta.get("generated_at"), "price_unavailable": 0, "strategies": {}}
    price_stats0 = dict(display_price.stats)           # D62：本次清单里真实价各来源的数量（快照 / 腾讯补取 / 取不到）
    history, price_cache, pending_prices = {}, {}, {}
    valid_signals = 0
    requested_signals = 0
    for strategy, spec in registry.STRATEGIES.items():
        if spec["kind"] != "single" or strategy == "buy_hold":
            continue
        group = table[table.strategy == strategy].sort_values("code")
        pack = {"label": spec["label"], "binary": strategy in BINARY,
                "counts": {action: 0 for action in ACTIONS},
                "counts_main": {action: 0 for action in ACTIONS},
                "counts_small": {action: 0 for action in ACTIONS}, "rows": [], "exceptions": []}
        result["strategies"][strategy] = pack
        for raw in group.to_dict("records"):
            code = str(raw["code"])
            action = classify(raw.get("now"), raw.get("next"))
            flags = []
            if code in suspended:
                flags.append("发布日停牌")
            if raw["last_date"] != day:
                flags.append(f"数据日期异常：{raw['last_date']}，应为 {day}")
            if any(number(raw.get(key)) is None or number(raw.get(key)) < 0 for key in ("now", "next")):
                flags.append("扫描仓位数据不足")
            if not action and not flags:
                continue
            row = {"code": code, "name": str(raw["name"]), "last_date": str(raw["last_date"]),
                   "now": number(raw.get("now")), "next": number(raw.get("next")), "action": action,
                   "close": None, "price_note": "数据不足", "d1": number(raw.get("d1")), "reason": "数据异常，未解释今日策略条件", "reason_en": "Data anomaly: today's strategy conditions are not explained",
                   "performance": {"scope": "样本外", "sharpe": number(raw.get("sharpe_oos"))}, "flags": flags}
            if code not in liquidity_cache:
                liquidity_cache[code] = turnover.average_20d(code, day, turnover_calendar, turnover_table, suspended_data, TRADE_LIST_MIN_TURNOVER)
            row.update(liquidity_cache[code], lot_cost=None)
            if action in ("买入", "加仓"):
                row["small_target"] = row["next"] is not None and row["next"] < TRADE_LIST_MIN_TARGET
            if flags:
                pack["exceptions"].append(row)
                continue
            requested_signals += 1
            try:
                if code not in history:
                    history[code] = loader(code)
                df = history[code]
                if len(df) < scan.MIN_ROWS or not df.index.is_monotonic_increasing or df.index.has_duplicates:
                    raise ValueError("日线长度不足或日期顺序异常")
                if df.index[-1].strftime("%Y-%m-%d") != day:
                    raise ValueError("个股日线日期与待发布日期不一致")
                bars = df[["Open", "High", "Low", "Close"]].to_numpy(float)
                if not np.isfinite(bars).all() or (bars <= 0).any():
                    raise ValueError("日线价格缺失、非有限值或非正数")
                weights, res, bench = replay(code, row["name"], strategy, df, cash)
                if not (math.isclose(float(res.weights.iloc[-1].sum()), row["now"], abs_tol=1e-8) and
                        math.isclose(float(weights.iloc[-1].sum()), row["next"], abs_tol=1e-8)):
                    raise ValueError("补算仓位与扫描结果不一致，请先重新完成扫描")
                row["performance"] = performance(res, bench, raw.get("sharpe_oos"), len(df))
                if code not in price_cache:
                    price_cache[code] = scale(market, code, df)
                factor, note = price_cache[code]
                row.update(d1=float((df.Close.iloc[-1] / df.Close.iloc[-2] - 1) * 100), flags=limit_flags(code, df))
                if not _fill_display(row, strategy, df, factor, note):
                    pending_prices.setdefault(code, []).append((row, strategy))
                pack["rows"].append(row)
                pack["counts"][action] += 1
                pack["counts_small" if row.get("small_target", False) else "counts_main"][action] += 1
                valid_signals += 1
            except Exception as exc:
                row["flags"].append(f"数据异常：{type(exc).__name__}：{str(exc)[:160]}")
                pack["exceptions"].append(row)
        logging.info("今日买卖 %s：正常信号 %d，只读隔离记录 %d", strategy, len(pack["rows"]), len(pack["exceptions"]))
    # 首轮失败只补取这些股票，最多两轮；不重新回测、不清空显示层的冷却状态。
    for retry_round in range(2):
        if not pending_prices:
            break
        _wait_price_cooldown()
        remaining = {}
        for code, entries in pending_prices.items():
            factor, note = scale(market, code, history[code])
            failed = [(row, strategy) for row, strategy in entries
                      if not _fill_display(row, strategy, history[code], factor, note)]
            if failed:
                remaining[code] = failed
        pending_prices = remaining
        logging.info("价格单独补取第 %d 轮，仍缺 %d 只", retry_round + 1, len(pending_prices))
    result["price_unavailable"] = sum(len(entries) for entries in pending_prices.values())
    used = {k: display_price.stats[k] - price_stats0.get(k, 0) for k in display_price.stats}
    logging.info("今日买卖显示价格来源：当天快照 %d 只，腾讯补取 %d 只，取不到 %d 次", used["snapshot"], used["tencent"], used["failed"])
    if requested_signals and not valid_signals:
        raise PublishError("整批待发布信号均未通过个股数据校验，拒绝发布")
    if source != _signature(paths):
        raise PublishError("生成期间扫描/进度/停牌文件发生变化，等待下次重试")
    # 写入前再次检查时钟，避免测试或外部调用绕过盘中保护。
    check_release(progress, meta, day, now)
    archive = base / "trade_list" / f"{day}.json"
    # 历史快照首次发布后不重写；最新清单允许用同日新取得的公开数据刷新。
    if not archive.exists():
        _atomic_json(archive, result)
    _atomic_json(latest, result)
    try:
        recorded = trade_signals.record_publication(result, cache=base, now=now)
        logging.info("独立清单跟踪新增 %d 条；旧日期重算不补记", recorded)
    except Exception:
        logging.exception("清单已发布，但独立信号记录失败")
    return result


def main_strategy(path=None):
    """信号核对账本按准确率选出的主策略（data/main_strategy.json）；读不到或不是有效策略就用 blend2。"""
    try:
        m = _read(Path(path) if path else Path(__file__).resolve().parent.parent / "data" / "main_strategy.json").get("main")
        if m in registry.STRATEGIES and registry.STRATEGIES[m]["kind"] == "single" and m != "buy_hold":
            return m
    except (OSError, ValueError, AttributeError):
        pass
    return "blend2"


def load(market="cn", day=None, strategy="blend2", *, cache=None):
    """只读读取，日期严格校验防止路径穿越；不在接口请求中生成清单。"""
    _market(market)
    if strategy not in registry.STRATEGIES or registry.STRATEGIES[strategy]["kind"] != "single":
        raise PublishError("未知单品种策略")
    base = Path(cache) if cache is not None else CACHE
    if day:
        valid_date(day)
    path = base / "trade_list" / f"{day}.json" if day else base / "trade_list_cn.json"
    if not path.exists():
        return {"empty": True, "market": market, "strategy": strategy, "message": "尚无有效清单，等待收盘后数据更新与扫描完成。"}
    payload = _read(path)
    pack = payload.get("strategies", {}).get(strategy)
    if pack is None:
        return {"empty": True, "market": market, "strategy": strategy, "message": "该策略尚无已发布清单。"}
    return {key: value for key, value in payload.items() if key != "strategies"} | pack | {
        "empty": False, "strategy": strategy,
        "available_strategies": [{"key": key, "label": item["label"]} for key, item in payload["strategies"].items()]}


def dates(market="cn", *, cache=None):
    """只枚举清单归档目录；不扫描行情目录。"""
    _market(market)
    base = Path(cache) if cache is not None else CACHE
    folder = base / "trade_list"
    available = []
    if folder.exists():
        for path in folder.glob("????-??-??.json"):
            try:
                valid_date(path.stem)
                available.append(path.stem)
            except PublishError:
                continue
    available.sort(reverse=True)
    try:
        latest = _read(base / "trade_list_cn.json").get("data_date")
    except (OSError, ValueError):
        latest = None
    return {"dates": available, "latest": latest if latest in available else (available[0] if available else None)}


def _listing_outdated(day, *, cache=None):
    base = Path(cache) if cache is not None else CACHE
    try:
        meta = _read(base / "scan_cn_meta.json")
        progress = _read(base / "cn_all_progress.json")
        latest = _read(base / "trade_list_cn.json")
        archive = _read(base / "trade_list" / f"{valid_date(day).isoformat()}.json")
        return not (progress.get("complete") is True and progress.get("trade_day") == day
                    and meta.get("data_date") == day and latest.get("data_date") == day
                    and latest.get("version") == VERSION and latest.get("threshold") == TRADE_LIST_THRESHOLD
                    and latest.get("min_target") == TRADE_LIST_MIN_TARGET
                    and latest.get("min_turnover") == TRADE_LIST_MIN_TURNOVER
                    and latest.get("scan_generated_at") == meta.get("generated_at")
                    and archive.get("data_date") == day and isinstance(archive.get("strategies"), dict))
    except (OSError, ValueError, TypeError, AttributeError):
        return True


def attempt_status(day, *, cache=None, now=None):
    """只约束自动尝试，手动命令可明确绕过次数与间隔限制。"""
    valid_date(day)
    base = Path(cache) if cache is not None else CACHE
    try:
        attempt = _read(base / "trade_list_attempts.json")
        if attempt.get("day") != day:
            return True, "新的发布日，自动次数重新计算"
        count = int(attempt.get("count", 0))
        why = f"（上次失败：{re.sub(r'[\x00-\x1f\x7f-\x9f]', ' ', str(attempt.get('error')))[:200]}）" if attempt.get("error") else ""     # 把真实失败原因带进日志，不再只说「已尝试 3 次」
        if count >= 3:
            return False, "同一发布日已自动尝试 3 次" + why
        if count:
            last = datetime.fromisoformat(attempt["last_attempt"])
            if _clock(now) - last < timedelta(minutes=30):
                return False, "距上次自动尝试不足 30 分钟" + why
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        pass
    return True, "可以自动尝试"


def needs_generation(day, *, cache=None, now=None):
    if not _listing_outdated(day, cache=cache):
        return False
    allowed, reason = attempt_status(day, cache=cache, now=now)
    if not allowed:
        logging.info("今日买卖暂不自动重试：%s；可手动运行 python -m src.trade_list 重试", reason)
    return allowed


def ready_day(now=None):
    """最近已收盘交易日；与现有每日检查共用 A 股交易日历。"""
    current = _clock(now)
    day = market_clock.last_usable_bar_date(current)
    try:
        return max(d for d in allmarket.trade_calendar() if d <= day).isoformat()
    except Exception:
        while day.weekday() >= 5:
            day -= timedelta(days=1)
        return day.isoformat()


def pending_day(market="cn", *, now=None):
    """扫描最新且只缺清单时返回日期，使每日任务在下载/扫描之前进入轻量路径。"""
    if market != "cn":
        return None
    day = ready_day(now)
    try:
        progress, meta = _read(CACHE / "cn_all_progress.json"), _read(CACHE / "scan_cn_meta.json")
        if (progress.get("complete") is True and progress.get("trade_day") == day
                and meta.get("data_date") == day and _listing_outdated(day)):
            return day
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return None


def current_day(market="cn", *, now=None, cache=None):
    """全市场进度、扫描和清单都已是最近收盘交易日的结果时返回该日期；只读，不记自动尝试、不打日志。
    每日任务用它跳过重复的全市场快照和回测：重跑扫描会刷新扫描时间戳，让已发布清单被判为过期。"""
    if market != "cn":
        return None
    day = ready_day(now)
    return None if _listing_outdated(day, cache=cache) else day


def try_generate(market, day, *, automatic=False, cache=None, now=None, **kwargs):
    """在每日任务入口记录自动尝试；失败只更新摘要并保留有效清单。"""
    base = Path(cache) if cache is not None else CACHE
    if automatic:
        allowed, reason = attempt_status(day, cache=cache, now=now)
        if not allowed:
            logging.info("今日买卖暂不自动重试：%s；可手动运行 python -m src.trade_list 重试", reason)
            return None
        try:
            old = _read(base / "trade_list_attempts.json")
        except (OSError, ValueError):
            old = {}
        count = int(old.get("count", 0)) if old.get("day") == day else 0
        attempt = {"day": day, "count": count + 1, "last_attempt": _clock(now).isoformat(timespec="seconds"), "error": ""}
        _atomic_json(base / "trade_list_attempts.json", attempt)
    try:
        result = generate(market, day, cache=cache, now=now, **kwargs)
        logging.info("今日买卖清单已发布，数据日期 %s", result["data_date"])
        return result
    except Exception as exc:
        if automatic:
            attempt["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
            _atomic_json(base / "trade_list_attempts.json", attempt)
        logging.exception("今日买卖生成失败，保留有效清单；可手动运行 python -m src.trade_list 重试")
        return None


def main():
    parser = argparse.ArgumentParser(description="手动生成今日买卖，不下载行情、不重跑全市场扫描；忽略自动重试次数限制")
    parser.add_argument("--market", choices=["cn"], default="cn")
    parser.add_argument("--date", help="待发布日期 YYYY-MM-DD，默认使用进度日期")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    result = generate(args.market, args.date)
    print(f"已发布 {result['data_date']} 今日买卖，价格暂不可用 {result['price_unavailable']} 条")


if __name__ == "__main__":
    main()
