"""美股全市场逐只回测：和 A 股 scan.py 同一套策略、同一套统计口径，数据来自 usdata。

输出：data/daily/scan_us.parquet + scan_us_meta.json
排除：流动性差、低价、坏数据、上市不满一年的股票（原因和数量写在 meta 里）。
成本：个股单边 10bp（佣金近乎 0，主要是买卖价差和滑点）。无涨跌停。
注意：股票名单来自今天的上市名单，退市公司不在里面，回测偏乐观（幸存者偏差）。
"""
from __future__ import annotations

import json
import logging
import os
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from . import registry, scan, usdata
from .datasource import CACHE, load_cached
from .portfolio import run_weights

OUT = CACHE / "scan_us.parquet"
META = CACHE / "scan_us_meta.json"
COST = 0.0010
log = logging.getLogger(__name__)
_CASH = _MCFG = _INFO = None


def _init(cash, mcfg, info):
    global _CASH, _MCFG, _INFO
    _CASH, _MCFG, _INFO = cash, mcfg, info


def scan_one(code: str) -> tuple[list[dict], str]:
    try:
        raw = usdata.load_raw(code)
        df = usdata.adjust(raw)
    except Exception as e:
        return [], f"读取失败：{type(e).__name__}"
    why = usdata.screen(raw.dropna(subset=["Close", "Adj Close"]), df)
    if why:
        return [], why
    name, board, mc, rk = _INFO.get(code, (code, "", np.nan, np.nan))
    n, c = len(df), df["Close"]
    prices = {code: df}
    base = {"code": code, "name": name, "delisted": False, "board": board, "industry": "", "mcap": mc, "mcap_rank": rk,
            "last_date": df.index[-1].strftime("%Y-%m-%d"),
            "d1": float((c.iloc[-1] / c.iloc[-2] - 1) * 100), "m1": float((c.iloc[-1] / c.iloc[-22] - 1) * 100) if n > 22 else np.nan}
    cash = _CASH.reindex(df.index).fillna(0.0) if _CASH is not None else pd.Series(0.0, index=df.index)
    evaluate = n >= scan.MIN_EVAL
    t0 = df.index[250] if evaluate else df.index[-1]
    if evaluate:
        rb = run_weights(prices, pd.Series(1.0, index=df.index, name=code).to_frame(), cost=COST).returns.loc[t0:]
        b = scan._stats(rb, cash.loc[t0:])
        bis, boos = scan._split_sharpe(rb, cash)
        base.update({"b_cagr": b["cagr"], "b_sharpe": b["sharpe"], "b_maxdd": b["maxdd"], "b_y1": b["y1"], "b_sharpe_is": bis, "b_sharpe_oos": boos})
    rows = []
    for s in scan.STRATS:
        try:
            W = registry.target_weights(s, prices, code, registry.clean_params(s, {}), _MCFG)
            res = run_weights(prices, W, cost=COST, band=0.10, cash_ret=cash)
        except Exception as e:
            rows.append(base | {"strategy": s, "error": str(e)[:80]})
            continue
        now_w, next_w = float(res.weights.iloc[-1].sum()), float(W.iloc[-1].sum())
        row = base | {"strategy": s, "now": now_w, "next": next_w, "action": scan._action(now_w, next_w)}
        if evaluate:
            r = res.returns.loc[t0:]
            row.update(scan._stats(r, cash.loc[t0:], res.trades, res.weights))
            row["sharpe_is"], row["sharpe_oos"] = scan._split_sharpe(r, cash)
        rows.append(row)
    return rows, ""


def run(processes: int | None = None, limit: int | None = None) -> dict:
    from config import MARKETS
    mcfg = MARKETS["us"]
    t0 = time.time()
    codes = usdata.codes()
    if limit:
        codes = codes[:limit]
    u = usdata.universe().set_index("yahoo") if len(usdata.universe()) else pd.DataFrame()
    cap = usdata.caps()
    why = Counter()
    if len(cap):                      # 有市值名单：只回测名单里的股票（顺便挡掉基金、封闭式基金等），并带上市值
        out = [c for c in codes if c not in cap.index]
        why["不在市值名单里（多为基金或已退市）"] += len(out)
        codes = [c for c in codes if c in cap.index]
    def _nm(c):
        n = u["name"].get(c, "") if len(u) else ""
        return n or (cap["name"].get(c, c) if len(cap) else c)
    def _bd(c):
        e = u["exchange"].get(c, "") if len(u) else ""
        return usdata.EXCHANGE.get(e, e)
    info = {c: (_nm(c), _bd(c), float(cap["mcap"].get(c, np.nan)) if len(cap) else np.nan, float(cap["rank"].get(c, np.nan)) if len(cap) else np.nan) for c in codes}
    cash_df = load_cached("us", mcfg["cash"])
    cash = cash_df["Close"].pct_change() if not cash_df.empty else None
    rows = []
    processes = processes or max(1, (os.cpu_count() or 2) - 1)
    with ProcessPoolExecutor(max_workers=processes, initializer=_init, initargs=(cash, mcfg, info)) as ex:
        for r, w in ex.map(scan_one, codes, chunksize=20):
            rows.extend(r)
            if w:
                why[w] += 1
    df = pd.DataFrame(rows)
    if df.empty:
        raise RuntimeError("没有可以回测的美股：先运行下载脚本")
    tmp = OUT.with_suffix(".tmp")
    df.to_parquet(tmp)
    if len(pd.read_parquet(tmp)) != len(df):
        tmp.unlink(missing_ok=True)
        raise IOError("美股全市场结果校验失败")
    tmp.replace(OUT)
    latest = df["last_date"].max()
    now = datetime.now(ZoneInfo("America/New_York"))
    meta = {"generated": now.strftime("%Y-%m-%d %H:%M"), "generated_at": now.isoformat(timespec="seconds"), "timezone_label": "美东时间",
            "data_date": latest, "stocks": int(df["code"].nunique()), "delisted": 0, "downloaded": len(codes), "listed": len(codes),
            "excluded_st": 0, "excluded": dict(why), "split": scan.SPLIT, "seconds": round(time.time() - t0), "strategies": {}}
    for s, g in df.groupby("strategy"):
        ev = g.dropna(subset=["sharpe", "b_sharpe"]) if "sharpe" in g else g.iloc[:0]
        fresh = g[g["last_date"] == latest]
        meta["strategies"][s] = scan._credibility(g) | {
            "label": registry.STRATEGIES[s]["label"], "evaluated": int(len(ev)),
            "beat_bh_pct": float((ev["sharpe"] > ev["b_sharpe"]).mean() * 100) if len(ev) else None,
            "median_sharpe": float(ev["sharpe"].median()) if len(ev) else None, "median_b_sharpe": float(ev["b_sharpe"].median()) if len(ev) else None,
            "median_maxdd": float(ev["maxdd"].median()) if len(ev) else None, "median_b_maxdd": float(ev["b_maxdd"].median()) if len(ev) else None,
            "buy": int((fresh["action"] == "买入").sum()), "sell": int((fresh["action"] == "卖出").sum()),
            "holding_pct": float((fresh["next"] > 0).mean() * 100) if len(fresh) else None}
    META.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    log.info("美股全市场回测完成：%d 只，用时 %d 秒", meta["stocks"], meta["seconds"])
    return meta


def load_scan():
    if not OUT.exists():
        return None, None
    return pd.read_parquet(OUT), (json.loads(META.read_text()) if META.exists() else None)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    m = run(limit=a.limit)
    print(f"美股扫描完成：{m['stocks']} 只入选，排除原因 {m['excluded']}，用时 {m['seconds']} 秒，数据日期 {m['data_date']}")
