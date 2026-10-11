"""个股详情：K 线、策略相关的指标线、买卖点、持仓区间，以及这只股票在所有策略下的对比。"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import allmarket, datasource, metrics, registry
from .indicators import adx, atr, rsi, sma
from .portfolio import run_weights

SINGLE = ["blend2", "trend_ens", "trend_vol", "donchian_s1", "rsi2", "ma_adx_original", "ma_adx_plus", "buy_hold"]
PERIODS = {"6m": 126, "1y": 252, "3y": 756, "5y": 1260, "all": None}


def lookup(market: str, code: str, mcfg: dict) -> tuple[pd.DataFrame | None, dict]:
    """找任意品种的数据：ETF/自选股缓存 → 全市场缓存。返回 (数据, 基本信息)。"""
    info = {"code": code, "name": code, "kind": "", "board": "", "industry": ""}
    if code in mcfg["universe"]:
        info.update(name=mcfg["universe"][code], kind="ETF")
        df = datasource.load_cached(market, code)
        return (df if not df.empty else None), info
    if market == "us":
        wl = datasource.load_watchlist(market)
        if code not in wl:
            from . import usdata
            if usdata.has(code):
                u = usdata.universe().set_index("yahoo")
                info.update(name=(u["name"].get(code) or code), kind="个股", board=usdata.EXCHANGE.get(u["exchange"].get(code, ""), ""))
                return usdata.load(code), info
            return None, info
        info.update(name=wl[code] or code, kind="个股", watch=True)
        df = datasource.load_cached(market, code)
        return (df if not df.empty else None), info
    if market != "cn" or not datasource.is_valid_stock(code):
        return None, info
    info.update(kind="个股", board=allmarket.board_of(code))
    try:
        lst = allmarket.stock_list()
        row = lst[lst["code"] == code]
        if len(row):
            info.update(name=row["name"].iloc[0], industry=row["industry"].iloc[0])
    except Exception:
        pass
    wl = datasource.load_watchlist(market)
    if code in wl and wl[code]:
        info["name"] = wl[code]
    info["watch"] = code in wl
    df = allmarket.load(code) if allmarket.path(code).exists() else datasource.load_cached(market, code)
    df = allmarket._drop_unfinished(df) if not df.empty else df
    return (df if not df.empty else None), info


def _overlays(name: str, df: pd.DataFrame, held: pd.Series) -> tuple[list, dict | None]:
    c = df["Close"]
    lines, sub = [], None
    if name in ("trend_ens", "blend2"):
        for n in (100, 150, 200, 250):
            lines.append({"name": f"MA{n}", "values": sma(c, n)})
    elif name == "trend_vol":
        lines.append({"name": "MA200", "values": sma(c, 200)})
    elif name == "donchian_s1":
        lines.append({"name": "20日最高", "values": df["High"].rolling(20).max().shift(1)})
        lines.append({"name": "10日最低", "values": df["Low"].rolling(10).min().shift(1)})
        # 跟踪止损线：持仓期间 最高收盘 - 2×ATR
        a = atr(df, 20)
        on = (held > 0).to_numpy()
        seg = np.cumsum(np.r_[0, np.diff(on.astype(int)) == 1])
        peak = c.groupby(seg).cummax()
        stop = (peak - 2 * a).where(on)
        lines.append({"name": "止损线", "values": stop, "dash": True})
    elif name == "rsi2":
        lines.append({"name": "MA200", "values": sma(c, 200)})
        lines.append({"name": "MA5", "values": sma(c, 5)})
        sub = {"name": "RSI(2)", "values": rsi(c, 2), "levels": [10, 70], "range": [0, 100]}
    elif name in ("ma_adx_original", "ma_adx_plus"):
        lines.append({"name": "MA5", "values": sma(c, 5)})
        lines.append({"name": "MA20", "values": sma(c, 20)})
        if name == "ma_adx_plus":
            lines.append({"name": "MA200", "values": sma(c, 200)})
        sub = {"name": "ADX(14)", "values": adx(df, 14), "levels": [20], "range": [0, 60]}
    return lines, sub


def _action(a: float, b: float) -> str:
    if a < 1e-9 and b < 1e-9:
        return "空仓"
    if a < 1e-9:
        return "买入"
    if b < 1e-9:
        return "卖出"
    if b - a > 0.1:
        return "加仓"
    if a - b > 0.1:
        return "减仓"
    return "持有"


def _run(name, prices, code, mcfg, cost, cash, limits):
    W = registry.target_weights(name, prices, code, registry.clean_params(name, {}), mcfg)
    band = 0.0 if name == "buy_hold" else 0.10
    res = run_weights(prices, W, cost=cost, band=band, cash_ret=cash, limits=limits)
    return W, res


def build(market: str, code: str, mcfg: dict, strategy: str = "blend2", period: str = "1y") -> dict:
    df, info = lookup(market, code, mcfg)
    if df is None or len(df) < 60:
        return {"error": f"{code} 没有数据（还没下载，或者上市时间太短）"}
    from src import display_price
    df, _k, _note = display_price.apply(market, code, df)
    info = dict(info, price_basis=_note)
    if strategy not in SINGLE:
        strategy = "blend2"
    prices = {code: df}
    cash_df = datasource.load_cached(market, mcfg["cash"])
    cash = cash_df["Close"].pct_change() if not cash_df.empty else None
    is_stock = info["kind"] == "个股"
    cost = (mcfg.get("stock_cost_bp", mcfg["cost_bp"]) if is_stock else mcfg["cost_bp"]) / 1e4
    limits = {code: datasource.limit_pct(code, info["name"])} if market == "cn" else None
    t0 = df.index[min(250, len(df) - 1)]
    rf = cash.reindex(df.index).fillna(0.0) if cash is not None else pd.Series(0.0, index=df.index)

    # ---- 所有策略对比
    compare, chosen = [], None
    for name in SINGLE:
        try:
            W, res = _run(name, prices, code, mcfg, cost, cash, limits)
        except Exception as e:
            compare.append({"key": name, "label": registry.STRATEGIES[name]["label"], "error": str(e)[:80]})
            continue
        r = res.returns.loc[t0:]
        st = metrics.stats(r, res.trades, res.weights, rf=rf)
        now_w, next_w = float(res.weights.iloc[-1].sum()), float(W.iloc[-1].sum())
        compare.append({
            "key": name, "label": registry.STRATEGIES[name]["label"],
            "cagr": st["CAGR%"], "sharpe": st.get("ExSharpe"), "maxdd": st["MaxDD%"],
            "vol": st["Vol%"], "win": st.get("Win%"), "trades": st.get("Trades"),
            "exposure": st.get("Exposure%"), "y1": float(((1 + r.iloc[-252:]).prod() - 1) * 100),
            "next": next_w, "action": _action(now_w, next_w),
        })
        if name == strategy:
            chosen = (W, res)

    W, res = chosen
    held = res.weights.iloc[:, 0].reindex(df.index).fillna(0.0)
    lines, sub = _overlays(strategy, df, held)

    n = PERIODS.get(period, 252)
    win = df.iloc[-n:] if n else df
    idx = win.index

    def ser(s):
        v = s.reindex(idx).to_numpy(dtype=float)
        return [None if not np.isfinite(x) else round(float(x), 4) for x in v]

    # 买卖点：次日开盘成交，所以价格是成交当天的开盘价
    trades = []
    for i, t in enumerate(res.trades.itertuples()):
        still = bool(i == len(res.trades) - 1 and held.iloc[-1] > 0)
        trades.append({
            "entry": t.entry_date.strftime("%Y-%m-%d"), "exit": None if still else t.exit_date.strftime("%Y-%m-%d"),
            "entry_px": float(df.loc[t.entry_date, "Open"]),
            "exit_px": None if still else float(df.loc[t.exit_date, "Open"]),
            "days": int(t.bars), "ret": round(float(t.ret) * 100, 2), "open": still,
            # 价格涨跌（不含仓位大小）；ret 是账户收益（按波动率调过仓位，所以通常比价格涨跌小）
            "px_ret": None if still else round((float(df.loc[t.exit_date, "Open"]) /
                                                float(df.loc[t.entry_date, "Open"]) - 1) * 100, 2),
        })
    # 常用技术指标：用整段历史计算（EMA 等需要预热），只取窗口这一段返回
    cc, hh, ll = df["Close"], df["High"], df["Low"]
    ma = cc.rolling(20).mean(); sd = cc.rolling(20).std(ddof=0)
    e12, e26 = cc.ewm(span=12, adjust=False).mean(), cc.ewm(span=26, adjust=False).mean()
    dif = e12 - e26; dea = dif.ewm(span=9, adjust=False).mean()
    dl = cc.diff(); g = dl.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean(); lo_ = (-dl.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rsi = 100 - 100 / (1 + g / lo_.replace(0, float("nan")))
    rsv = (cc - ll.rolling(9).min()) / (hh.rolling(9).max() - ll.rolling(9).min()).replace(0, float("nan")) * 100
    kk = rsv.ewm(alpha=1 / 3, adjust=False).mean(); dd = kk.ewm(alpha=1 / 3, adjust=False).mean()
    ind = {"boll": {"up": ser((ma + 2 * sd).reindex(idx)), "mid": ser(ma.reindex(idx)), "low": ser((ma - 2 * sd).reindex(idx))},
           "macd": {"dif": ser(dif.reindex(idx)), "dea": ser(dea.reindex(idx)), "hist": ser((dif - dea).reindex(idx))},
           "rsi": ser(rsi.reindex(idx)), "kdj": {"k": ser(kk.reindex(idx)), "d": ser(dd.reindex(idx)), "j": ser((3 * kk - 2 * dd).reindex(idx))}}
    k0 = len(df) - len(win)
    eq = (1 + res.returns.reindex(idx).fillna(0.0)).cumprod()
    bh = df["Close"].reindex(idx) / df["Close"].reindex(idx).iloc[0]
    c = df["Close"]
    return {
        "info": info | {"last_date": df.index[-1].strftime("%Y-%m-%d"),
                        "d1": float((c.iloc[-1] / c.iloc[-2] - 1) * 100),
                        "m1": float((c.iloc[-1] / c.iloc[-22] - 1) * 100) if len(c) > 22 else None,
                        "y1": float((c.iloc[-1] / c.iloc[-252] - 1) * 100) if len(c) > 252 else None,
                        "rows": len(df), "first_date": df.index[0].strftime("%Y-%m-%d")},
        "strategy": strategy, "period": period, "cost_bp": cost * 1e4,
        "dates": [d.strftime("%Y-%m-%d") for d in idx],
        "open": ser(win["Open"]), "high": ser(win["High"]), "low": ser(win["Low"]),
        "close": ser(win["Close"]), "volume": ser(win["Volume"]),
        "lines": [{"name": l["name"], "values": ser(l["values"]), "dash": l.get("dash", False)} for l in lines],
        "sub": ({"name": sub["name"], "values": ser(sub["values"]), "levels": sub["levels"],
                 "range": sub["range"]} if sub else None),
        "ind": ind,
        "position": ser(held),
        "equity": ser(eq), "bench": ser(bh),
        "trades": trades[::-1],
        "compare": compare,
        "next": float(W.iloc[-1].sum()),
        "action": _action(float(held.iloc[-1]), float(W.iloc[-1].sum())),
    }
