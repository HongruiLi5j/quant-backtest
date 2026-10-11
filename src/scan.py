"""全市场逐只回测：每只股票跑全部单品种策略（默认参数），多进程并行。

输出：
  data/daily/scan_cn.parquet   每行 = 一只股票 × 一个策略
  data/daily/scan_cn_meta.json 汇总：每个策略在多少比例的股票上有效、今天多少只出信号

排除：名称带 ST/退 的股票、上市不满一年的新股。
注意：在 5000 只股票里挑"回测最好的"几乎一定是运气（多重检验），
所以网页上重点展示"今天的信号"和"策略整体在多少股票上有效"。
"""
from __future__ import annotations

import json
import logging
import os
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import date, datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from . import allmarket, metrics, registry
from .datasource import CACHE, limit_pct, load_cached
from .portfolio import run_weights

STRATS = ["blend2", "trend_ens", "trend_vol", "donchian_s1", "rsi2", "ma_adx_original", "ma_adx_plus"]
OUT = CACHE / "scan_cn.parquet"
META = CACHE / "scan_cn_meta.json"
MIN_ROWS = 250          # 上市满一年（约 250 个交易日）
MIN_EVAL = 400          # 至少这么多天才计算回测统计（前 250 天是指标预热期）
SPLIT = "2020-01-01"    # 样本内：2013–2019；样本外：2020 至今
MIN_PERIOD = 250        # 每段至少一年数据才算这一段的 Sharpe
COST = 0.0008           # 个股单边 8bp
log = logging.getLogger(__name__)

_CASH = None
_MCFG = None


def _init(cash, mcfg):
    global _CASH, _MCFG
    _CASH, _MCFG = cash, mcfg


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


def _stats(r: pd.Series, rf: pd.Series, trades=None, weights=None) -> dict:
    s = metrics.stats(r, trades, weights)
    y1 = float(((1 + r.iloc[-252:]).prod() - 1) * 100)
    return {"cagr": s["CAGR%"], "sharpe": metrics.sharpe(r - rf), "maxdd": s["MaxDD%"],
            "win": s.get("Win%"), "trades": s.get("Trades"), "exposure": s.get("Exposure%"), "y1": y1}


def _split_sharpe(r: pd.Series, rf: pd.Series) -> tuple[float, float]:
    """样本内 / 样本外各自的超额 Sharpe；某段不足一年就返回 NaN。"""
    out = []
    for part in (r.loc[:SPLIT], r.loc[SPLIT:]):
        out.append(metrics.sharpe(part - rf.reindex(part.index).fillna(0.0)) if len(part) >= MIN_PERIOD else np.nan)
    return out[0], out[1]


def scan_one(item: tuple) -> list[dict]:
    code, name = item[0], item[1]
    delisted = len(item) > 2 and item[2]
    try:
        df = allmarket._drop_unfinished(allmarket.load(code, delisted))   # 不用盘中的半天 K 线
    except Exception:
        return []
    n = len(df)
    if n < MIN_ROWS:
        return []
    c = df["Close"]
    lim = {code: limit_pct(code, name)}
    prices = {code: df}
    base = {"code": code, "name": name, "delisted": bool(delisted), "last_date": df.index[-1].strftime("%Y-%m-%d"),
            "d1": float((c.iloc[-1] / c.iloc[-2] - 1) * 100),
            "m1": float((c.iloc[-1] / c.iloc[-22] - 1) * 100) if n > 22 else np.nan}
    cash = _CASH.reindex(df.index).fillna(0.0) if _CASH is not None else pd.Series(0.0, index=df.index)
    evaluate = n >= MIN_EVAL
    t0 = df.index[250] if evaluate else df.index[-1]

    bench = run_weights(prices, pd.Series(1.0, index=df.index, name=code).to_frame(), cost=COST, limits=lim)
    if evaluate:
        rb = bench.returns.loc[t0:]
        b = _stats(rb, cash.loc[t0:])
        bis, boos = _split_sharpe(rb, cash)
        base.update({"b_cagr": b["cagr"], "b_sharpe": b["sharpe"], "b_maxdd": b["maxdd"], "b_y1": b["y1"],
                     "b_sharpe_is": bis, "b_sharpe_oos": boos})
    rows = []
    for name_s in STRATS:
        try:
            W = registry.target_weights(name_s, prices, code, registry.clean_params(name_s, {}), _MCFG)
            res = run_weights(prices, W, cost=COST, band=0.10, cash_ret=cash, limits=lim)
        except Exception as e:  # 个别股票数据异常，跳过这一个策略
            rows.append(base | {"strategy": name_s, "error": str(e)[:80]})
            continue
        now_w = float(res.weights.iloc[-1].sum())
        next_w = float(W.iloc[-1].sum())
        row = base | {"strategy": name_s, "now": now_w, "next": next_w, "action": _action(now_w, next_w)}
        if evaluate:
            r = res.returns.loc[t0:]
            row.update(_stats(r, cash.loc[t0:], res.trades, res.weights))
            row["sharpe_is"], row["sharpe_oos"] = _split_sharpe(r, cash)
        if delisted:
            row.update(action="", next=0.0, now=0.0)
        rows.append(row)
    return rows


def run(processes: int | None = None, mcfg: dict | None = None) -> dict:
    """扫描全部已下载的股票。"""
    from config import MARKETS
    mcfg = mcfg or MARKETS["cn"]
    t0 = time.time()
    fixed = allmarket.strip_intraday()
    if fixed:
        log.info("删除了 %d 只股票里盘中保存的半天 K 线", fixed)
    lst = allmarket.stock_list()
    names = dict(zip(lst["code"], lst["name"]))
    codes = [c for c in allmarket.cached_codes() if c in names]
    excluded_st = [c for c in codes if "ST" in names[c].upper() or "退" in names[c]]
    codes = [c for c in codes if c not in set(excluded_st)]
    cash_df = load_cached("cn", mcfg["cash"])
    cash = cash_df["Close"].pct_change() if not cash_df.empty else None

    items = [(c, names[c]) for c in codes]
    # 退市股票（如果下载过）：只进统计，修正幸存者偏差
    dl_codes = allmarket.cached_codes(delisted=True)
    dl_total = 0
    if dl_codes:
        try:
            dl = allmarket.delisted_list()
            dl_names = dict(zip(dl["code"], dl["name"]))
            dl_total = len(dl)
        except Exception:
            dl_names = {}
        items += [(c, dl_names.get(c, c), True) for c in dl_codes]
    processes = processes or max(1, (os.cpu_count() or 2) - 1)
    rows = []
    with ProcessPoolExecutor(max_workers=processes, initializer=_init, initargs=(cash, mcfg)) as ex:
        for r in ex.map(scan_one, items, chunksize=20):
            rows.extend(r)
    df = pd.DataFrame(rows)
    if df.empty:
        raise RuntimeError("没有可以回测的股票：先运行 python backfill.py 下载历史数据")
    info = lst.set_index("code")
    df["board"] = df["code"].map(allmarket.board_of)
    df["industry"] = df["code"].map(info["industry"]).fillna("")
    temp = OUT.with_suffix(".tmp")
    df.to_parquet(temp)
    check = pd.read_parquet(temp)
    if len(check) != len(df) or not set(df.columns).issubset(check.columns):
        temp.unlink(missing_ok=True)
        raise IOError("全市场回测结果临时文件校验失败")
    temp.replace(OUT)

    latest = df.loc[~df["delisted"], "last_date"].max()
    live = df[~df["delisted"]]
    now_market = datetime.now(ZoneInfo("Asia/Shanghai"))
    meta = {"generated": now_market.strftime("%Y-%m-%d %H:%M"),
            "generated_at": now_market.isoformat(timespec="seconds"),
            "timezone_label": "北京时间", "data_date": latest,
            "stocks": int(live["code"].nunique()), "delisted": int(df.loc[df["delisted"], "code"].nunique()),
            "delisted_listed": dl_total,
            "split": SPLIT, "excluded_st": len(excluded_st),
            "listed": int(len(lst)), "downloaded": len(allmarket.cached_codes()),
            "seconds": round(time.time() - t0), "strategies": {}}
    for s, gall in df.groupby("strategy"):
        g = gall[~gall["delisted"]]
        ev = g.dropna(subset=["sharpe", "b_sharpe"]) if "sharpe" in g else g.iloc[:0]
        fresh = g[g["last_date"] == latest]
        meta["strategies"][s] = _credibility(gall) | {
            "label": registry.STRATEGIES[s]["label"],
            "evaluated": int(len(ev)),
            "beat_bh_pct": float((ev["sharpe"] > ev["b_sharpe"]).mean() * 100) if len(ev) else None,
            "median_sharpe": float(ev["sharpe"].median()) if len(ev) else None,
            "median_b_sharpe": float(ev["b_sharpe"].median()) if len(ev) else None,
            "median_maxdd": float(ev["maxdd"].median()) if len(ev) else None,
            "median_b_maxdd": float(ev["b_maxdd"].median()) if len(ev) else None,
            "buy": int((fresh["action"] == "买入").sum()), "sell": int((fresh["action"] == "卖出").sum()),
            "holding_pct": float((fresh["next"] > 0).mean() * 100) if len(fresh) else None,
        }
    temp = META.with_suffix(".tmp")
    temp.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    if json.loads(temp.read_text(encoding="utf-8")).get("data_date") != meta["data_date"]:
        temp.unlink(missing_ok=True)
        raise IOError("全市场回测元数据临时文件校验失败")
    temp.replace(META)
    log.info("全市场回测完成：%d 只 × %d 个策略，用时 %d 秒", meta["stocks"], len(STRATS), meta["seconds"])
    return meta


def _credibility(g: pd.DataFrame) -> dict:
    """可信度检验：
      beat_is / beat_oos：样本内 / 样本外，策略超额 Sharpe 高于买入持有的股票比例
      persistence：每只股票"策略比持有好多少"在样本内和样本外的秩相关（Spearman）。
                   接近 0 = 样本内哪些股票好，和样本外哪些股票好没有关系 → 挑股票是运气
      top_pick_oos：样本内最好的 10% 股票，到样本外"策略比持有好多少"的中位数，对比全部股票的中位数
      with_delisted：把退市股票也算进去后，策略比持有好的比例（幸存者偏差修正）"""
    out = {}
    if "sharpe_is" not in g:
        return out
    e = g.dropna(subset=["sharpe_is", "sharpe_oos", "b_sharpe_is", "b_sharpe_oos"])
    live = g[~g["delisted"]]
    is_ = live.dropna(subset=["sharpe_is", "b_sharpe_is"])
    oos = live.dropna(subset=["sharpe_oos", "b_sharpe_oos"])
    out["beat_is"] = float((is_["sharpe_is"] > is_["b_sharpe_is"]).mean() * 100) if len(is_) else None
    out["beat_oos"] = float((oos["sharpe_oos"] > oos["b_sharpe_oos"]).mean() * 100) if len(oos) else None
    ee = e[~e["delisted"]]
    if len(ee) >= 30:
        d_is = ee["sharpe_is"] - ee["b_sharpe_is"]
        d_oos = ee["sharpe_oos"] - ee["b_sharpe_oos"]
        out["persistence"] = float(d_is.rank().corr(d_oos.rank()))
        top = d_is >= d_is.quantile(0.9)
        out["top_pick_oos"] = float(d_oos[top].median())
        out["all_oos"] = float(d_oos.median())
        out["n_split"] = int(len(ee))
    ev_all = g.dropna(subset=["sharpe", "b_sharpe"])
    if g["delisted"].any() and len(ev_all):
        out["with_delisted"] = float((ev_all["sharpe"] > ev_all["b_sharpe"]).mean() * 100)
        dl = ev_all[ev_all["delisted"]]
        out["delisted_bh_median_sharpe"] = float(dl["b_sharpe"].median()) if len(dl) else None
    return out


def load_scan() -> tuple[pd.DataFrame | None, dict | None]:
    if not OUT.exists():
        return None, None
    return pd.read_parquet(OUT), json.loads(META.read_text()) if META.exists() else None
