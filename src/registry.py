"""策略登记表：网页上能选的策略、参数范围，以及统一的"跑一次回测"入口。

加新策略：在 strategies.py 写函数，再在 STRATEGIES 里加一项即可，网页会自动出现。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import metrics
from . import strategies as S
from .portfolio import run_weights


def P(key, label, default, lo, hi, step, help=""):
    return {"key": key, "label": label, "default": default, "min": lo, "max": hi,
            "step": step, "help": help}


TV = P("target_vol", "目标年化波动", 0.15, 0, 0.4, 0.01, "0 = 不按波动率调仓，信号出现就满仓")

STRATEGIES = {
    "blend2": {
        "label": "组合：趋势集成 + RSI2 + 海龟", "kind": "single",
        "desc": "三个策略各 1/3 仓位。赚钱时段不同，合起来回撤最小。",
        "params": [P("w_trend", "趋势集成权重", 1 / 3, 0, 1, 0.05),
                   P("w_rsi", "RSI2 权重", 1 / 3, 0, 1, 0.05),
                   P("w_donchian", "海龟权重", 1 / 3, 0, 1, 0.05)],
    },
    "trend_ens": {
        "label": "趋势集成（4 条均线）", "kind": "single",
        "desc": "看 100/150/200/250 日均线，站上几条持几成仓，再乘以波动率仓位。",
        "params": [TV, P("vol_len", "波动率回看天数", 20, 5, 120, 1),
                   P("buffer", "均线迟滞区间", 0.01, 0, 0.05, 0.005, "价格要超过均线这么多才算站上")],
    },
    "trend_vol": {
        "label": "趋势 + 波动率目标（单均线）", "kind": "single",
        "desc": "收盘在均线上方持有，下方空仓；仓位按波动率调整。",
        "params": [P("sma_len", "均线周期", 200, 20, 400, 10),
                   P("buffer", "均线迟滞区间", 0.01, 0, 0.05, 0.005), TV,
                   P("vol_len", "波动率回看天数", 20, 5, 120, 1)],
    },
    "donchian_s1": {
        "label": "海龟系统 1（通道突破）", "kind": "single",
        "desc": "突破 N 日最高价进场；跌破 M 日最低价或回撤 k×ATR 出场。",
        "params": [P("entry_len", "突破周期 N", 20, 5, 120, 1),
                   P("exit_len", "出场周期 M", 10, 3, 60, 1),
                   P("atr_mult", "跟踪止损 k×ATR", 2.0, 0.5, 6, 0.5), TV],
    },
    "rsi2": {
        "label": "RSI(2) 均值回归", "kind": "single",
        "desc": "长期上升趋势中买短期超跌，反弹就卖。胜率高、持仓短，怕交易成本。",
        "params": [P("trend_len", "趋势均线", 200, 50, 300, 10),
                   P("rsi_len", "RSI 周期", 2, 2, 14, 1),
                   P("entry_th", "RSI 低于多少买入", 10, 1, 40, 1),
                   P("exit_ma", "收盘上穿几日均线卖出", 5, 2, 20, 1),
                   P("max_hold", "最多持有天数", 10, 2, 30, 1)],
    },
    "ma_adx_original": {
        "label": "MA + ADX（你的原版）", "kind": "single",
        "desc": "快线上穿慢线且 ADX 高于阈值买入，下穿卖出。",
        "params": [P("fast", "快线", 5, 2, 50, 1), P("slow", "慢线", 20, 5, 200, 1),
                   P("adx_min", "ADX 阈值", 20, 0, 50, 1)],
    },
    "ma_adx_plus": {
        "label": "MA + ADX 改进版", "kind": "single",
        "desc": "原版 + 200 日均线过滤 + 跟踪止损 + 波动率仓位。",
        "params": [P("fast", "快线", 5, 2, 50, 1), P("slow", "慢线", 20, 5, 200, 1),
                   P("adx_min", "ADX 阈值", 20, 0, 50, 1),
                   P("atr_mult", "跟踪止损 k×ATR", 3.0, 0.5, 6, 0.5), TV],
    },
    "dual_momentum": {
        "label": "双动量轮动（多品种）", "kind": "multi",
        "desc": "每月末在两个股票类资产里选涨得多的；跑不赢现金就换债券。",
        "params": [P("lookback", "动量回看天数", 252, 21, 504, 21),
                   P("skip", "跳过最近几天", 0, 0, 63, 1)],
    },
    "buy_hold": {
        "label": "买入持有（基准）", "kind": "single",
        "desc": "一直满仓，用来做比较。", "params": [],
    },
}


def clean_params(name: str, raw: dict | None) -> dict:
    """只接受登记过的参数，并夹在允许范围内（网页传来的数据一律不信任）。"""
    raw = raw or {}
    out = {}
    for p in STRATEGIES[name]["params"]:
        try:
            v = float(raw.get(p["key"], p["default"]))
        except (TypeError, ValueError):
            v = p["default"]
        if not np.isfinite(v):
            v = p["default"]
        v = min(max(v, p["min"]), p["max"])
        out[p["key"]] = int(round(v)) if float(p["step"]).is_integer() else v
    return out


def target_weights(name: str, prices: dict, symbol: str, params: dict, mcfg: dict) -> pd.DataFrame:
    df = prices[symbol]
    tv = params.get("target_vol")
    p = dict(params)
    if "target_vol" in p:
        p["target_vol"] = None if not tv else tv
    if name == "buy_hold":
        w = pd.Series(1.0, index=df.index)
    elif name == "trend_ens":
        w = S.trend_ensemble(df, **p)
    elif name == "trend_vol":
        w = S.trend_vol_target(df, **p)
    elif name == "donchian_s1":
        w = S.donchian_s1(df, **p)
    elif name == "rsi2":
        w = S.rsi2_reversion(df, **p)
    elif name == "ma_adx_original":
        w = S.ma_adx_original(df, **p)
    elif name == "ma_adx_plus":
        w = S.ma_adx_plus(df, **p)
    elif name == "blend2":
        tot = p["w_trend"] + p["w_rsi"] + p["w_donchian"]
        mix = (1 / 3, 1 / 3, 1 / 3) if tot <= 0 else (p["w_trend"] / tot, p["w_rsi"] / tot,
                                                       p["w_donchian"] / tot)
        w = S.blend(S.trend_ensemble(df), S.rsi2_reversion(df), S.donchian_s1(df), mix=mix)
    elif name == "dual_momentum":
        dm = mcfg["dual_momentum"]
        return S.dual_momentum(prices, risky=dm["risky"], safe=dm["safe"], cash=dm["cash"], **p)
    else:
        raise KeyError(name)
    return w.rename(symbol).to_frame()


def run(name: str, prices: dict, symbol: str, params: dict, mcfg: dict,
        cost_bp: float | None = None, start: str | None = None, market: str | None = None,
        detail: bool = False) -> dict:
    """跑一次回测，返回网页需要的全部结果（可直接转成 JSON）。"""
    params = clean_params(name, params)
    is_stock = symbol not in mcfg["universe"]
    if cost_bp is None:
        cost_bp = mcfg.get("stock_cost_bp", mcfg["cost_bp"]) if is_stock else mcfg["cost_bp"]
    cost = cost_bp / 1e4
    cash = prices[mcfg["cash"]]["Close"].pct_change() if mcfg["cash"] in prices else None
    W = target_weights(name, prices, symbol, params, mcfg)
    used = {s: prices[s] for s in W.columns}
    limits = None
    if market == "cn":   # A 股涨跌停：一字涨停买不进、一字跌停卖不出
        from .datasource import limit_pct, load_watchlist
        wl = load_watchlist("cn")
        limits = {s: limit_pct(s, wl.get(s, "")) for s in set(W.columns) | {symbol}}
    band = 0.10 if name not in ("buy_hold", "dual_momentum") else 0.0
    res = run_weights(used, W, cost=cost, band=band, cash_ret=cash, limits=limits)
    bench = run_weights({symbol: prices[symbol]},
                        pd.Series(1.0, index=prices[symbol].index, name=symbol).to_frame(), cost=cost,
                        limits=limits)

    # 预热期（最长指标 252 天）之后才开始统计
    warm = res.returns.index[min(260, len(res.returns) - 1)]
    t0 = max(warm, pd.Timestamp(start)) if start else warm
    r = res.returns.loc[t0:]
    b = bench.returns.reindex(r.index).fillna(0.0)
    rf = cash.reindex(r.index).fillna(0.0) if cash is not None else None

    def pack(x: pd.Series) -> dict:
        s = metrics.stats(x, res.trades, res.weights)
        s["ExcessSharpe"] = metrics.sharpe(x - rf) if rf is not None else s["Sharpe"]
        s["TotalRet%"] = ((1 + x).prod() - 1) * 100
        return {k: (None if (isinstance(v, float) and not np.isfinite(v)) else float(v))
                for k, v in s.items()}

    eq, beq = (1 + r).cumprod(), (1 + b).cumprod()
    dd = eq / eq.cummax() - 1
    w_now = res.weights.iloc[-1]
    tgt_today = W.reindex(res.weights.index).fillna(0.0).iloc[-1]   # 今天收盘后定的，明天开盘执行
    trades = res.trades.tail(30).iloc[::-1] if len(res.trades) else pd.DataFrame()
    ana = None
    if detail:
        ana = _detail(res, r, b, W, used, name, cost, band, cash, limits, prices, symbol, mcfg, market, is_stock, t0, params)
    try:
        from . import report as _rp
        rpt = _rp.report(r, b, rf, res.trades, res.weights)
    except Exception:
        rpt = None
    return {
        "analysis": ana, "report": rpt,
        "strategy": name, "symbol": symbol, "params": params, "cost_bp": cost * 1e4, "is_stock": is_stock,
        "start": str(r.index[0].date()), "end": str(r.index[-1].date()),
        "dates": [d.strftime("%Y-%m-%d") for d in eq.index],
        "equity": np.round(eq.to_numpy(), 5).tolist(),
        "bench": np.round(beq.to_numpy(), 5).tolist(),
        "drawdown": np.round(dd.to_numpy() * 100, 3).tolist(),
        "stats": pack(r), "bench_stats": pack(b),
        "recent": {k: _period_ret(r, n) for k, n in [("1D", 1), ("1W", 5), ("1M", 21), ("3M", 63), ("1Y", 252)]}
        | {"YTD": float(((1 + r.loc[str(r.index[-1].year)]).prod() - 1) * 100)},
        "position_now": {k: float(v) for k, v in w_now.items() if v > 1e-9},
        "position_next": {k: float(v) for k, v in tgt_today.items() if v > 1e-9},
        "trades": [
            {"entry": str(t.entry_date.date()), "exit": str(t.exit_date.date()),
             "days": int(t.bars), "ret": round(float(t.ret) * 100, 2),
             "open": bool(i == 0 and w_now.sum() > 0)}
            for i, t in enumerate(trades.itertuples())
        ],
    }


def _period_ret(r: pd.Series, n: int) -> float:
    return float(((1 + r.iloc[-n:]).prod() - 1) * 100)


def _detail(res, r, b, W, used, name, cost, band, cash, limits, prices, symbol, mcfg, market, is_stock, t0, params=None):
    """网页「分析」区需要的扩展数据：月度收益、样本内外、交易分布、成本敏感性、多基准。只在单次回测时算。"""
    from . import analytics
    t = res.trades
    if len(t):
        t = t[t.entry_date >= r.index[0]]
    tr = (t.ret * 100).tolist() if len(t) else []
    dy = t.bars.tolist() if len(t) else []
    expo = res.weights.sum(axis=1).reindex(r.index).fillna(0.0).to_numpy()
    mult = [("0", 0.0), ("1x", 1.0), ("2x", 2.0), ("4x", 4.0)]
    sens = []
    for lab, k in mult:
        try:
            x = run_weights(used, W, cost=cost * k, band=band, cash_ret=cash, limits=limits).returns.reindex(r.index).fillna(0.0)
            bs = analytics.basic(x)
            sens.append({"label": lab, "cost_bp": round(cost * k * 1e4, 1), "cagr": bs.get("cagr"),
                         "sharpe": bs.get("sharpe"), "maxdd": bs.get("maxdd")})
        except Exception:
            pass
    bm = {}
    cand = ["510300", "510500"] if market == "cn" else ["SPY", "QQQ"]
    for c in cand:
        if c in prices and c != symbol:
            cl = prices[c]["Close"].reindex(r.index).ffill().dropna()
            if len(cl) > 20:
                bm[mcfg["universe"].get(c, c)] = np.round((cl / cl.iloc[0]).reindex(r.index).ffill().to_numpy(), 5).tolist()
    a = analytics.analyze(r, b, tr, dy, expo, extra_bench=bm or None)
    a["cost_sens"] = sens
    try:
        a["mc"] = _monte_carlo(r)
    except Exception:
        a["mc"] = None
    try:
        a["param_sens"] = _param_sens(name, prices, symbol, mcfg, cost, band, cash, limits, market, r, params)
    except Exception:
        a["param_sens"] = None
    a["assumptions"] = analytics.assumptions(market, "lab", cost * 1e4, is_stock)
    return a


def _monte_carlo(r: pd.Series, n: int = 1000, block: int = 10, seed: int = 7) -> dict | None:
    """块自助法（block bootstrap）：把策略的日收益按 10 天一块随机重排 1000 次，
    看年化收益、最大回撤的分布。回答「如果历史换个顺序发生，结果会差多远」。"""
    x = r.to_numpy(float)
    L = len(x)
    if L < 120:
        return None
    rng = np.random.default_rng(seed)
    nb = int(np.ceil(L / block))
    starts = rng.integers(0, L - block + 1, size=(n, nb))
    idx = (starts[:, :, None] + np.arange(block)[None, None, :]).reshape(n, -1)[:, :L]
    P = x[idx]
    eq = np.cumprod(1 + P, axis=1)
    yrs = L / 252
    cagr = (eq[:, -1] ** (1 / yrs) - 1) * 100
    dd = (eq / np.maximum.accumulate(eq, axis=1) - 1).min(axis=1) * 100
    q = lambda a, ps: [round(float(np.percentile(a, p)), 2) for p in ps]
    ps = [5, 25, 50, 75, 95]
    hist, edges = np.histogram(cagr, bins=20)
    return {"n": n, "block": block, "pct": ps, "cagr": q(cagr, ps), "maxdd": q(dd, ps),
            "p_loss": round(float((eq[:, -1] < 1).mean() * 100), 1),
            "hist": hist.tolist(), "edges": [round(float(e), 2) for e in edges]}


def _param_sens(name, prices, symbol, mcfg, cost, band, cash, limits, market, r, params=None, budget_s: float = 8.0):
    """参数敏感性：每个参数单独向两边各挪 1、2 步（按范围的 10%/20%），看 Sharpe 变化。
    曲线越平越稳健；只有一个尖峰说明是过拟合。"""
    import time
    from . import analytics
    spec = STRATEGIES[name]["params"]
    if not spec:
        return None
    base = {p["key"]: (params or {}).get(p["key"], p["default"]) for p in spec}
    out, t0 = [], time.time()
    # 用当前请求的参数作基准：registry.run 已清洗过，这里从 clean_params 的结果重建
    for p in spec:
        row = {"key": p["key"], "label": p["label"], "pts": []}
        span = (p["max"] - p["min"]) * 0.10
        for k in (-2, -1, 0, 1, 2):
            if time.time() - t0 > budget_s:
                break
            v = min(max(base[p["key"]] + k * span, p["min"]), p["max"])
            v = int(round(v)) if float(p["step"]).is_integer() else round(v, 4)
            prm = clean_params(name, {**base, p["key"]: v})
            try:
                W2 = target_weights(name, prices, symbol, prm, mcfg)
                used2 = {c: prices[c] for c in W2.columns}
                x = run_weights(used2, W2, cost=cost, band=band, cash_ret=cash, limits=limits).returns.reindex(r.index).fillna(0.0)
                bs = analytics.basic(x)
                row["pts"].append({"v": v, "cagr": bs.get("cagr"), "sharpe": bs.get("sharpe"), "maxdd": bs.get("maxdd")})
            except Exception:
                pass
        if row["pts"]:
            out.append(row)
    return out or None
