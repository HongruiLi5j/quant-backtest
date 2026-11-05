"""个股详情：K 线、策略相关的指标线、买卖点、持仓区间，以及这只股票在所有策略下的对比。"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import allmarket, datasource, metrics, registry, resultcache
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
    df = allmarket.load(code, keep_flagged=True) if allmarket.path(code).exists() else datasource.load_cached(market, code)
    df = allmarket._drop_unfinished(df) if not df.empty else df
    return (df if not df.empty else None), info


def input_stamps(market: str, code: str, mcfg: dict) -> tuple:
    """build() 会读到的所有磁盘文件的指纹 + 北京时间 15:30 状态，给 /api/stock 的结果缓存当键（B08）。

    对应关系（lookup / build 里读了什么）：
      行情：ETF、自选股、美股自选 → datasource 的 csv；A 股全市场个股 → allmarket 的 parquet（没有就退回 csv）；
            美股全市场个股 → usdata 的 parquet；美股名称/交易所 → usdata 的 universe.csv
      名称/行业/自选标记：全市场股票清单 cn_all_list.csv、自选股名单 watchlist_<市场>.json
      现金收益（策略对比的无风险利率）：mcfg["cash"] 的 csv
      A 股显示价格的复权系数：display_price 的 engine_price_scale.json
      时间：allmarket._drop_unfinished 会按北京时间 15:30 丢掉当天半根 K 线
    不管 code 实际走哪条分支，所有候选文件都盖章：只会多失效，不会漏失效。新增读文件的代码时要同步这里。"""
    from . import display_price, usdata
    files = [datasource.cache_path(market, code), datasource.cache_path(market, mcfg["cash"]),
             datasource._watch_path(market), allmarket.LIST_FILE, allmarket.path(code), display_price._SCALE_FILE]
    root = usdata.root()
    if root is not None:
        files += [root / "data" / f"{code}.parquet", root / "universe.csv"]
    return (tuple(resultcache.file_stamp(f) for f in files), resultcache.bj_clock_state(allmarket._now_bj()))


def cacheable(result: dict) -> bool:
    """哪些 build() 结果可以缓存：出错的不缓存；A 股没取到真实价格（临时网络问题，显示的是后复权价）也不缓存，
    否则网络恢复后还会一直显示降级结果。"""
    if not isinstance(result, dict) or result.get("error"):
        return False
    return not str(result.get("info", {}).get("price_basis", "")).startswith("未取到")


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


def _suspended_days(market: str, mcfg: dict, df) -> int | None:
    """D86：这只股票在自己的数据区间里缺了多少个交易日（多半是停牌）。A 股用交易所交易日历 data/daily/cn_trade_calendar.csv
    （revquant 建议，和他的核对口径一致）；读不到或其他市场时退回本市场基准 ETF（mcfg['main']）的日期。只数两者重叠的区间。
    回测年化按「有 K 线的天数」算、停牌期间的现金利息也没计入（revquant_ref_engine.md），页面在这个数超过 20 时提示。
    只读，不改任何回测数字；拿不到日历时返回 None。"""
    cal = None
    if market == "cn":
        try:
            cal = pd.DatetimeIndex(pd.to_datetime(pd.read_csv(datasource.CACHE / "cn_trade_calendar.csv")["trade_date"]))
        except Exception:
            cal = None
    if cal is None:
        try:
            cal = datasource.load_cached(market, mcfg["main"]).index
        except Exception:
            return None
    if len(cal) == 0 or len(df) == 0:
        return None
    lo, hi = max(cal[0], df.index[0]), min(cal[-1], df.index[-1])
    want = cal[(cal >= lo) & (cal <= hi)]
    have = df.index[(df.index >= lo) & (df.index <= hi)]
    return int(len(want.difference(have)))


_COMPARE_MEMO: "OrderedDict" = None     # D94：(输入指纹) -> (对比表, {策略: (W, res)})；最多 32 只，最久没用的先丢
_COMPARE_MAX = 32


def _frame_digest(obj) -> str:
    """表格内容指纹（含索引、列名和每个值）：内容一变指纹就变。"""
    import hashlib
    if obj is None:
        return "-"
    h = hashlib.sha1(pd.util.hash_pandas_object(obj, index=True).to_numpy().tobytes())
    h.update(repr(list(getattr(obj, "columns", [getattr(obj, "name", None)]))).encode())
    return h.hexdigest()


def _compare_all(market, code, prices, mcfg, cost, cash, limits, t0, rf):
    """8 个单品种策略在整段历史上的回测 + 对比表行。和区间（period）、页面上选的策略都无关，
    原来离线版每只股票按 3 个区间各算一遍、网页上切换区间也重算 8 个回测。
    D94：按「价格表内容 + 现金收益 + 费用 + 涨跌停幅度 + 市场配置」做指纹缓存；结果只读，对比表每次返回深拷贝。"""
    import copy
    import json
    from collections import OrderedDict
    global _COMPARE_MEMO
    if _COMPARE_MEMO is None:
        _COMPARE_MEMO = OrderedDict()
    key = (market, code, float(cost), tuple(sorted((limits or {}).items())), str(t0),
           _frame_digest(prices[code]), _frame_digest(cash), json.dumps(mcfg, sort_keys=True, default=str))
    hit = _COMPARE_MEMO.get(key)
    if hit is not None:
        _COMPARE_MEMO.move_to_end(key)
        return copy.deepcopy(hit[0]), hit[1]
    compare, runs = [], {}
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
        runs[name] = (W, res)
    _COMPARE_MEMO[key] = (copy.deepcopy(compare), runs)
    while len(_COMPARE_MEMO) > _COMPARE_MAX:
        _COMPARE_MEMO.popitem(last=False)
    return compare, runs


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
    t0 = registry.stats_start(df.index)          # D18：和 registry.run 同一个起点，各页数字一致
    rf = cash.reindex(df.index).fillna(0.0) if cash is not None else pd.Series(0.0, index=df.index)

    # ---- 所有策略对比（D94：同一份数据只算一次，换区间 / 换显示策略时直接复用，见 _compare_all）
    compare, runs = _compare_all(market, code, prices, mcfg, cost, cash, limits, t0, rf)
    chosen = runs.get(strategy)

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
                        "rows": len(df), "first_date": df.index[0].strftime("%Y-%m-%d"),
                        "suspended_days": _suspended_days(market, mcfg, df),
                        # D110：数据质量标记（只 A 股全市场文件有）；mask = 全市场统计里已屏蔽，flag = 只提示
                        "quality": [{k: g[k] for k in ("from", "to", "n", "kind")} for g in allmarket.quality_flags(info["code"])] if market == "cn" else []},
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
        "compare_start": t0.strftime("%Y-%m-%d"),      # 对比表的统计起点（全部历史，扣除预热期）
        "next": float(W.iloc[-1].sum()),
        "action": _action(float(held.iloc[-1]), float(W.iloc[-1].sum())),
    }
