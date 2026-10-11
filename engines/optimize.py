"""参数优化：网格搜索 + 样本内/样本外检验。

做法：
  1. 把统计区间切成两段：样本内（用来挑参数）和样本外（挑完之后才看，检验挑出来的参数是不是真的好）
  2. 对每一组参数跑一次完整回测，分别算两段的 Sharpe / 年化 / 最大回撤
  3. 按样本内的目标指标选出"最佳参数"，再看它在样本外排第几

为什么要这样：参数试得越多，总能碰到一组在历史上特别好的，但那多半是运气（过拟合）。
如果样本内排名和样本外排名几乎没关系（秩相关接近 0），说明优化挑出来的只是噪声。
"""
from __future__ import annotations

import itertools
import math
import os
from concurrent.futures import ProcessPoolExecutor, as_completed

import numpy as np
import pandas as pd

from engines import common

METRICS = {"sharpe": "Sharpe", "cagr": "年化收益", "calmar": "Calmar（年化/回撤）", "maxdd": "最大回撤（越小越好）"}
MAX_COMBOS = {"backtrader": 150, "inhouse": 400, "rqalpha": 60}


def _seg_stats(r: pd.Series) -> dict:
    if len(r) < 20:
        return {"sharpe": None, "cagr": None, "maxdd": None, "calmar": None}
    s = common.metrics.stats(r)
    f = lambda v: None if v is None or not np.isfinite(v) else round(float(v), 4)
    return {"sharpe": f(s["Sharpe"]), "cagr": f(s["CAGR%"]), "maxdd": f(s["MaxDD%"]), "calmar": f(s["Calmar"])}


def _values(spec: dict) -> list:
    lo, hi, step = float(spec["from"]), float(spec["to"]), float(spec["step"])
    if step <= 0 or hi < lo:
        raise ValueError("参数范围不对：需要 起点 ≤ 终点、步长 > 0")
    n = int(math.floor((hi - lo) / step + 1e-9)) + 1
    vals = [round(lo + i * step, 6) for i in range(n)]
    return vals


# ---------------------------------------------------------------- 子进程里跑（每个进程只读一次数据）
_W: dict = {}


def _init_worker(engine, code, df, info, start, cash):
    import warnings
    warnings.filterwarnings("ignore")
    _W.update(engine=engine, code=code, df=df, info=info, start=start, cash=cash)


def _one(name: str, params: dict, split: str) -> dict:
    from engines.runner import ENGINES
    fn = ENGINES[_W["engine"]]
    try:
        out = fn(_W["code"], name, params, _W["start"], _W["cash"], df=_W["df"], info=_W["info"], light=True)
    except Exception as e:
        return {"params": params, "error": f"{type(e).__name__}: {e}"}
    df = _W["df"]
    s0 = common.eval_start(df.index, _W["start"])
    v = out["value"].reindex(df.index).ffill().fillna(_W["cash"])
    r = v.loc[s0:].pct_change().fillna(0.0)
    sp = pd.Timestamp(split)
    return {"params": params, "is": _seg_stats(r.loc[:sp - pd.Timedelta(days=1)]),
            "oos": _seg_stats(r.loc[sp:]), "all": _seg_stats(r)}


def run(job: dict, progress) -> dict:
    engine, code, name = job["engine"], job["code"], job["strategy"]
    opt = job.get("opt") or {}
    base = common.parse_params(name, job.get("params"))
    defs = {p["key"]: p for p in common.STRATEGIES[name]["params"]}
    axes = [a for a in (opt.get("x"), opt.get("y")) if a and a.get("key")]
    if not axes:
        raise ValueError("请至少选一个要优化的参数")
    for a in axes:
        if a["key"] not in defs:
            raise ValueError(f"参数 {a['key']} 不属于这个策略")
    if len(axes) == 2 and axes[0]["key"] == axes[1]["key"]:
        axes = axes[:1]
    grids = [_values(a) for a in axes]
    combos = list(itertools.product(*grids))
    limit = MAX_COMBOS.get(engine, 100)
    if len(combos) > limit:
        raise ValueError(f"组合太多（{len(combos)} 组），{engine} 最多 {limit} 组：请加大步长或缩小范围")
    metric = opt.get("metric", "sharpe")
    if metric not in METRICS:
        metric = "sharpe"

    raw, info = common.load_stock(code)
    df, k, note = common.to_forward_adjusted(code, raw)
    s0 = common.eval_start(df.index, job.get("start"))
    idx = df.index[df.index >= s0]
    split = opt.get("split")
    try:
        split = pd.Timestamp(split) if split else None
    except Exception:
        split = None
    if split is None or not (idx[20] < split < idx[-20]):
        split = idx[int(len(idx) * 0.7)]          # 默认前 70% 样本内、后 30% 样本外
    split_s = split.strftime("%Y-%m-%d")

    param_sets = []
    for c in combos:
        p = dict(base)
        for a, v in zip(axes, c):
            p[a["key"]] = v
        param_sets.append(common.parse_params(name, p))     # 夹在允许范围内、整数参数取整

    rows = []
    workers = max(1, min(int(os.cpu_count() or 2) - 1, 6, len(param_sets)))
    cash = float(job.get("cash") or common.DEFAULT_CASH)
    progress(0, len(param_sets), f"开始：{len(param_sets)} 组参数，{workers} 个进程并行")
    with ProcessPoolExecutor(max_workers=workers, initializer=_init_worker,
                             initargs=(engine, code, df, info, job.get("start"), cash)) as ex:
        futs = [ex.submit(_one, name, p, split_s) for p in param_sets]
        for i, f in enumerate(as_completed(futs), 1):
            rows.append(f.result())
            progress(i, len(param_sets), f"已完成 {i}/{len(param_sets)}")

    ok = [r for r in rows if "error" not in r]
    if not ok:
        raise RuntimeError("全部参数组合都失败了：" + rows[0].get("error", ""))
    sign = -1 if metric == "maxdd" else 1

    def key(r, seg):
        v = r[seg][metric] if metric != "maxdd" else (None if r[seg]["maxdd"] is None else -r[seg]["maxdd"])
        return -1e9 if v is None else v

    best = max(ok, key=lambda r: key(r, "is"))
    is_vals = np.array([key(r, "is") for r in ok])
    oos_vals = np.array([key(r, "oos") for r in ok])
    rank_corr = None
    if len(ok) >= 5:
        rank_corr = float(pd.Series(is_vals).rank().corr(pd.Series(oos_vals).rank()))
    oos_rank = int((oos_vals > key(best, "oos")).sum()) + 1
    default_row = next((r for r in ok if all(abs(r["params"][a["key"]] - base[a["key"]]) < 1e-9
                                              for a in axes)), None)
    # 参数平原：最佳参数周围（每个轴 ±1 格）的平均表现，和最佳值差得越少越稳健
    neigh = []
    for r in ok:
        near = all(abs(grids[j].index(r["params"][a["key"]]) - grids[j].index(best["params"][a["key"]])) <= 1
                   if r["params"][a["key"]] in grids[j] else False for j, a in enumerate(axes))
        if near and r is not best:
            neigh.append(key(r, "is"))
    return {
        "kind": "optimize", "engine": engine, "code": code, "name": info.get("name", code), "strategy": name,
        "label": common.STRATEGIES[name]["label"], "metric": metric, "metric_label": METRICS[metric],
        "axes": [{"key": a["key"], "label": defs[a["key"]]["label"], "values": g} for a, g in zip(axes, grids)],
        "base": base, "start": s0.strftime("%Y-%m-%d"), "split": split_s, "end": df.index[-1].strftime("%Y-%m-%d"),
        "rows": sorted(rows, key=lambda r: [r["params"].get(a["key"], 0) for a in axes]),
        "best": best, "default": default_row, "rank_corr": rank_corr, "best_oos_rank": oos_rank, "n": len(ok),
        "neighbor_avg": float(np.mean(neigh)) if neigh else None, "sign": sign,
        "errors": [r for r in rows if "error" in r][:5],
    }
