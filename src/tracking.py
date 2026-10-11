"""信号记录与跟踪：每天把策略发出的买入/卖出信号存下来，之后统计"当时说买的，后来涨没涨"。

这是检验策略最诚实的办法：历史回测可以被反复调参"调好"，但记录下来的信号是在当天
就定死的，之后的涨跌是真正的样本外结果（前提是不回头改记录）。

规则：
  信号日 d 收盘后发出 → 第 d+1 个交易日开盘买入 → 持有 N 个交易日后按收盘价计算收益
  同期基准：A 股用沪深300ETF，美股用 SPY，同样的开盘买、N 天后收盘卖
"""
from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from . import allmarket, datasource
from .datasource import CACHE

HIST = CACHE / "signals_history.parquet"
HORIZONS = (5, 20, 60)
ACTIONS = ("买入", "卖出", "加仓", "减仓", "换仓")


def record(market: str, signal_date: str, rows: list[dict]):
    """rows: [{code, name, strategy, action, next}]。同一天同一品种同一策略只保留一条（重跑不会重复）。"""
    if not rows:
        return 0
    new = pd.DataFrame(rows)
    new["market"] = market
    new["signal_date"] = signal_date
    from config import MARKETS
    new["recorded_at"] = datetime.now(ZoneInfo(MARKETS[market]["tz"])).isoformat(timespec="seconds")
    cols = ["market", "signal_date", "code", "name", "strategy", "action", "next", "recorded_at"]
    new = new[[c for c in cols if c in new.columns]]
    if HIST.exists():
        old = pd.read_parquet(HIST)
        key = ["market", "signal_date", "code", "strategy"]
        old = old.merge(new[key], on=key, how="left", indicator=True)
        old = old[old["_merge"] == "left_only"].drop(columns="_merge")
        new = pd.concat([old, new], ignore_index=True)
    tmp = HIST.with_suffix(".tmp")
    new.to_parquet(tmp)
    check = pd.read_parquet(tmp)
    if len(check) != len(new) or not set(cols).issubset(check.columns):
        tmp.unlink(missing_ok=True)
        raise IOError("信号历史临时文件校验失败")
    tmp.replace(HIST)
    return len(rows)


def _prices(market: str, code: str, mcfg: dict) -> pd.DataFrame | None:
    if code in mcfg["universe"]:
        df = datasource.load_cached(market, code)
    elif market == "cn" and allmarket.path(code).exists():
        df = allmarket._drop_unfinished(allmarket.load(code))
    else:
        df = datasource.load_cached(market, code)
    return None if df.empty else df


def _fwd(df: pd.DataFrame, signal_date: str, h: int):
    """信号日之后第一个交易日开盘买，持有 h 天后收盘卖。返回 (收益, 是否已满 h 天, 入场日)。"""
    idx = df.index
    pos = idx.searchsorted(pd.Timestamp(signal_date), side="right")
    if pos >= len(idx):
        return None, False, None
    entry = df["Open"].iloc[pos]
    end = min(pos + h - 1, len(idx) - 1)
    ret = df["Close"].iloc[end] / entry - 1
    return float(ret), (pos + h - 1) < len(idx), idx[pos]


def evaluate(market: str, mcfg: dict, hist: "pd.DataFrame | None" = None) -> dict:
    """hist 不为空时评估这份历史（历史回放），否则评估实盘记录文件。"""
    if hist is None:
        if not HIST.exists():
            return {"empty": True}
        h = pd.read_parquet(HIST)
    else:
        h = hist
    h = h[h["market"] == market]
    if h.empty:
        return {"empty": True}
    bench = _prices(market, mcfg["main"], mcfg)
    cache: dict = {}
    rows = []
    for r in h.itertuples():
        if r.code not in cache:
            cache[r.code] = _prices(market, r.code, mcfg)
        df = cache[r.code]
        if df is None:
            continue
        row = {"signal_date": r.signal_date, "code": r.code, "name": r.name, "strategy": r.strategy,
               "action": r.action}
        for n in HORIZONS:
            ret, done, _ = _fwd(df, r.signal_date, n)
            b, _, _ = _fwd(bench, r.signal_date, n) if bench is not None else (None, False, None)
            row[f"r{n}"] = None if ret is None else ret * 100
            row[f"x{n}"] = None if ret is None or b is None else (ret - b) * 100
            row[f"done{n}"] = done
        # 到目前为止（最新收盘）的收益
        ret, _, entry_day = _fwd(df, r.signal_date, 10 ** 6)
        b, _, _ = _fwd(bench, r.signal_date, 10 ** 6) if bench is not None else (None, False, None)
        row["r_now"] = None if ret is None else ret * 100
        row["x_now"] = None if ret is None or b is None else (ret - b) * 100
        row["entry_day"] = None if entry_day is None else entry_day.strftime("%Y-%m-%d")
        rows.append(row)
    d = pd.DataFrame(rows)
    if d.empty:
        return {"empty": True}
    # 尚无完成 5 个持有交易日的信号时，用最近行情日期加 5 个工作日做提示。
    # 这是估算日期，交易所节假日可能让首批统计延后。
    latest_price = bench.index[-1].date() if bench is not None and not bench.empty else pd.Timestamp(d["signal_date"].max()).date()
    expected_first_5 = pd.bdate_range(start=pd.Timestamp(latest_price) + pd.Timedelta(days=1), periods=5)[-1].date().isoformat()

    summary = []
    for (strat, act), g in d.groupby(["strategy", "action"]):
        item = {"strategy": strat, "action": act, "signals": int(len(g)),
                "first": g["signal_date"].min(), "last": g["signal_date"].max()}
        for n in HORIZONS:
            done = g[g[f"done{n}"] & g[f"r{n}"].notna()]
            item[f"n{n}"] = int(len(done))
            item[f"avg{n}"] = float(done[f"r{n}"].mean()) if len(done) else None
            item[f"ex{n}"] = float(done[f"x{n}"].mean()) if len(done) else None
            item[f"win{n}"] = float((done[f"r{n}"] > 0).mean() * 100) if len(done) else None
            item[f"beat{n}"] = float((done[f"x{n}"] > 0).mean() * 100) if len(done) else None
        summary.append(item)

    recent = d.sort_values("signal_date", ascending=False).head(200)
    recent = recent.replace({np.nan: None})
    now_market = datetime.now(ZoneInfo(mcfg["tz"]))
    return {
        "empty": False,
        "generated": now_market.strftime("%Y-%m-%d %H:%M"),
        "generated_at": now_market.isoformat(timespec="seconds"),
        "timezone_label": "北京时间" if market == "cn" else "美东时间",
        "total": int(len(d)), "since": d["signal_date"].min(), "latest": d["signal_date"].max(),
        "latest_price_date": latest_price.isoformat(), "expected_first_5": expected_first_5,
        "summary": summary,
        "recent": recent[["signal_date", "entry_day", "code", "name", "strategy", "action",
                          "r5", "r20", "r60", "r_now", "x_now"]].to_dict("records"),
    }


REPLAY_STRATS = ["blend2", "trend_ens", "donchian_s1", "rsi2", "trend_vol", "ma_adx_original", "ma_adx_plus"]


def build_replay(market: str, mcfg: dict, prices: dict, years: int = 3) -> "pd.DataFrame":
    """历史回放：用现在的规则把过去 years 年每天会发出的信号重算一遍。

    每个信号只用当天收盘及以前的数据（策略权重本来就是逐日因果计算的），没有未来函数。
    但它不是"当时记录下来的"信号：规则和参数是事后确定的，结果会偏乐观，
    只能当参考，真正的样本外要看实盘记录（signals_history.parquet）。
    动作的判断和 daily_report._action 一致：0 到正是买入，正到 0 是卖出，变化超过 10% 是加仓或减仓。
    """
    from . import registry
    names = {**mcfg["universe"], **datasource.load_watchlist(market)}
    main = mcfg["main"]
    syms = [main] + [s for s in names if s != main and s != mcfg.get("cash")]
    rows = []
    for sym in syms:
        df = prices.get(sym)
        if df is None or len(df) < 260:
            continue
        cutoff = df.index[-1] - pd.DateOffset(years=years)
        for strat in REPLAY_STRATS:
            try:
                p = registry.clean_params(strat, {})
                w = registry.target_weights(strat, prices, sym, p, mcfg)[sym].astype(float).fillna(0.0)
            except Exception:
                continue
            prev = w.shift(1).fillna(0.0)
            for d in w.index[w.index >= cutoff]:
                a, b = float(prev.loc[d]), float(w.loc[d])
                if a < 1e-9 and b < 1e-9:
                    continue
                if a < 1e-9:
                    act = "买入"
                elif b < 1e-9:
                    act = "卖出"
                elif b - a > 0.1:
                    act = "加仓"
                elif a - b > 0.1:
                    act = "减仓"
                else:
                    continue
                rows.append({"market": market, "signal_date": d.strftime("%Y-%m-%d"), "code": sym,
                             "name": names.get(sym, sym), "strategy": strat, "action": act, "next": b})
    return pd.DataFrame(rows, columns=["market", "signal_date", "code", "name", "strategy", "action", "next"])


def save_eval(market: str, ev: dict, replay: bool = False):
    p = CACHE / f"tracking_{market}{'_replay' if replay else ''}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(ev, ensure_ascii=False, default=float), encoding="utf-8")
    check = json.loads(tmp.read_text(encoding="utf-8"))
    if check.get("empty") != ev.get("empty") or check.get("total", 0) != ev.get("total", 0):
        tmp.unlink(missing_ok=True)
        raise IOError(f"跟踪汇总临时文件校验失败：{market}")
    tmp.replace(p)


def load_eval(market: str, replay: bool = False) -> dict | None:
    p = CACHE / f"tracking_{market}{'_replay' if replay else ''}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
