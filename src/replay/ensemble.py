"""T008 S5:幸存者组合 + 市场状态过滤(设计文档第 9 节)。

输入是 S3 回放引擎写出的逐日行(含 picks / weights / net / mkt / next_date)和 S4 训练段留下的幸存者名单;
另需 selection.NullPanel(个股持有期收益 + 可交易掩码,与回放同口径:T+1 开盘 → T+2 开盘)。
本模块只做离线计算,不联网、不读写任何文件。

一、两种组合方式(都不翻转信号;票数/得分相同按股票代码升序,保证可复现)
  vote(投票):T 日每个幸存者"投"自己当天选的股票,按票数取前 top_n,等权。
  weighted(按近期表现加权,在线更新):每个幸存者维护一个指数加权平均(EWMA)的"净超额"分数 s_k。
    更新规则:一笔回放行的持有期结束(next_date ≤ 当前信号日 T,即 T 日收盘前已经实现)后才被吸收:
        s_k ← λ·s_k + (1-λ)·x ,x = 该行的净超额,首次出现 s_k = x
    衰减:λ = 0.5^(1/halflife),半衰期 halflife 个交易日(默认 20),越久远的表现权重越小,
    所以组合会逐渐"遗忘"一阵子前的好运。T 日当天及之后才实现的行不会被用到(不用未来)。
    权重:raw_k = max(s_k, 0)(近期为负的不给权重,不做空),w_k = (1-shrink)·(1/K) + shrink·raw_k/Σraw
    (shrink 默认 0.5,向等权收缩,防止被一两个"最近运气好"的策略带跑);Σraw=0(全部为负或没有历史)则等权。
    个股得分 = Σ_k w_k × (幸存者 k 当天的目标权重),取前 top_n 后归一化。
    注意:权重只用滚动的已实现表现,没有用验证段统计来选;幸存者名单本身来自训练段(见 selection)。

二、市场状态过滤(降仓,默认减半):
  等权市场指数(close 基的累计净值,T 日值只含 ≤T 日收盘)在 60 日均线下,
  或近 20 日市场收益波动率高于【训练段】80 分位 → 当日目标仓位乘 reduce(默认 0.5)。
  波动率阈值只在训练段确定一次(freeze_vol_threshold),之后冻结传入,验证段不重估。
  均线窗口没攒够 60 个点时不过滤(warmup,按满仓处理)。
  注意:信号日 T 的状态只用 ≤T 日收盘数据,生效于 T+1 开盘成交的仓位,和回放引擎的时序一致。
  不能用回放行里的 mkt 字段做指数:mkt[T] 是 T+1 开盘到 T+2 开盘的收益,在 T 日是未来数据。

三、风控硬约束(沿用 PDF):单票 ≤ 10%,总仓 ≤ 80%(再乘以状态过滤的缩放)。超出部分留作现金。

四、成本:按目标权重变化计,买入 (佣金+过户费+滑点)·买入额,卖出再加印花税;最低佣金不计(对大资金是近似);
  上一日目标权重当作昨日持仓(忽略持仓随价格漂移),可交易掩码为 False 的股票不会被买入,卖不出的情形不模拟,都是近似。

五、评估(ensemble_study):显著性只对"满仓、不过滤、不设上限"的 raw 变体做(跟匹配随机对照同口径比选股质量),
  含 Holm 校正(把组合方式也算作额外试验:m = 清单 m + 组合变体数)、随机对照零分布对比、成本翻倍;
  风控后的 managed 变体看收益、波动、最大回撤、换手、过滤前后对比、参数邻域。
  组合方式、半衰期、top_n 都是在看过幸存者后新增的自由度,应视为额外试验。

局限(报告里也写):回放结果好不代表策略有效;组合建立在训练段幸存者上,训练段内的组合表现是样本内的;
幸存者通常很少,投票/加权的差别很可能小于噪声;验证段只有约 9 个月;成本为占位费率;
managed 变体相对满仓的市场基准会有现金拖累,"超额"为负不代表选股差。
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from . import registry
from . import selection as sel
from .engine import CostParams

MODES = ("vote", "weighted")
TOP_N = 20
HALFLIFE = 20
SHRINK = 0.5
MA_WINDOW = 60
VOL_WINDOW = 20
VOL_PCT = 80.0
REDUCE = 0.5
MAX_SINGLE = 0.10
MAX_GROSS = 0.80
MIN_VOL_OBS = 30                 # 训练段里用来估阈值的有效波动率点数下限
DAYS_PER_YEAR = sel.DAYS_PER_YEAR
HALFLIFE_NEIGHBORS = (10, 40)
TOP_N_NEIGHBORS = (10, 30)
LIMITS = sel.LIMITS + [
    "幸存者很少时,投票与加权几乎相同;组合方式、半衰期、top_n 是看过幸存者之后加的自由度,已按额外试验计入 Holm",
    "训练段内的组合表现是样本内的,只有验证段才是样本外;验证段约 9 个月,置信区间很宽",
    "managed 变体有现金拖累(总仓 ≤80%、降仓),与满仓市场基准比的超额偏低,不代表选股差",
    "成本按目标权重变化估算,忽略最低佣金与持仓漂移;降仓会增加换手与成本",
]


class EnsembleError(ValueError):
    pass


# ---------------------------------------------------------------- 市场状态

def market_index(mret: pd.Series) -> pd.Series:
    """等权市场指数:日收益 mret(T 日收盘较 T-1 收盘)累计净值,缺失按 0。T 日的值只含 ≤T 日。"""
    return (1.0 + mret.astype(float).fillna(0.0)).cumprod()


def _vol(mret: pd.Series, window: int) -> pd.Series:
    return mret.astype(float).rolling(window).std()


def freeze_vol_threshold(mret: pd.Series, train_end, window: int = VOL_WINDOW, pct: float = VOL_PCT) -> dict:
    """在训练段(日期 ≤ train_end)上确定波动率阈值 = 滚动 window 日波动率的 pct 分位,之后冻结。"""
    vol = _vol(mret, window)
    vals = vol[vol.index <= pd.Timestamp(train_end)].dropna()
    if len(vals) < MIN_VOL_OBS:
        raise EnsembleError(f"训练段有效波动率点数 {len(vals)} < {MIN_VOL_OBS},无法确定阈值")
    return {"threshold": float(np.percentile(vals.to_numpy(), pct)), "pct": float(pct), "window": int(window),
            "train_end": str(pd.Timestamp(train_end).date()), "n_obs": int(len(vals))}


def market_scale(mret: pd.Series, threshold: float, ma_window: int = MA_WINDOW, vol_window: int = VOL_WINDOW,
                 reduce: float = REDUCE) -> dict:
    """{信号日(字符串): {"scale","below_ma","high_vol","state"}}。任一条件成立 → scale = reduce(不连乘)。"""
    if not 0 < reduce <= 1:
        raise EnsembleError("reduce 必须在 (0, 1]")
    idx = market_index(mret)
    ma = idx.rolling(ma_window).mean()
    vol = _vol(mret, vol_window)
    out = {}
    for ts in idx.index:
        m, v = ma.loc[ts], vol.loc[ts]
        warm = not np.isfinite(m)
        below = (not warm) and bool(idx.loc[ts] < m)
        high = bool(np.isfinite(v) and v > threshold)
        cut = below or high
        out[str(ts.date())] = {"scale": reduce if cut else 1.0, "below_ma": below, "high_vol": high,
                               "state": "cut" if cut else ("warmup" if warm else "normal")}
    return out


# ---------------------------------------------------------------- 在线加权

def decay_lambda(halflife: float) -> float:
    if not halflife > 0:
        raise EnsembleError("halflife 必须 > 0")
    return 0.5 ** (1.0 / float(halflife))


def blend_weights(scores: dict, shrink: float = SHRINK) -> dict:
    """scores: {策略键: EWMA 分数或 None}。返回归一化权重;规则见模块文档。"""
    keys = sorted(scores)
    if not keys:
        return {}
    eq = 1.0 / len(keys)
    raw = {k: max(scores[k], 0.0) if scores[k] is not None else 0.0 for k in keys}
    tot = sum(raw.values())
    if tot <= 0:
        return {k: eq for k in keys}
    return {k: (1 - shrink) * eq + shrink * raw[k] / tot for k in keys}


# ---------------------------------------------------------------- 组合构建

def _rates(cost_params) -> tuple:
    cp = cost_params if cost_params is not None else CostParams()
    buy = cp.commission_rate + cp.transfer_rate + cp.slippage_bps / 1e4
    return buy, buy + cp.stamp_rate


def _cap(raw: dict, gross: float, single_cap: float) -> dict:
    tot = sum(raw.values())
    if not raw or tot <= 0:
        return {}
    return {c: min(v / tot * gross, single_cap) for c, v in sorted(raw.items()) if v > 0}


def build_ensemble(rows, survivors, panel: "sel.NullPanel", mode: str = "vote", key: str | None = None,
                   scale: dict | None = None, top_n: int = TOP_N, halflife: float = HALFLIFE, shrink: float = SHRINK,
                   gross_cap: float = MAX_GROSS, single_cap: float = MAX_SINGLE, cost_params=None,
                   cost_mult: float = 1.0) -> list:
    """把幸存者的回放行合成一条组合的逐日行(格式同回放行:key/date/next_date/gross/cost/net/mkt/excess/weights)。
    scale=market_scale 的结果(None 表示不过滤)。只用 ≤T 日的信息决定 T 日的目标权重。"""
    if mode not in MODES:
        raise EnsembleError(f"mode 必须是 {MODES}")
    if not isinstance(top_n, int) or isinstance(top_n, bool) or top_n <= 0:
        raise EnsembleError("top_n 必须是正整数")
    surv = sorted(set(survivors))
    if not surv:
        raise EnsembleError("没有幸存者,无法组合")
    lam = decay_lambda(halflife)
    sel._check_rows(rows)
    ho0 = sel.segments(registry.load_validated()[0])["holdout"][0]
    mine = [r for r in rows if r["key"] in set(surv)]
    if any(r["date"] >= ho0 for r in mine):
        raise EnsembleError(f"输入含封存段({ho0} 起)的行:组合不得读封存段")
    by = sel._by_key(mine)
    missing = [k for k in surv if k not in by]
    if missing:
        raise EnsembleError(f"幸存者没有回放行: {missing}")
    today = {(r["key"], r["date"]): r for r in mine}
    dates = sorted({r["date"] for r in mine})
    gone = [d for d in dates if d not in panel.ret.index]
    if gone:
        raise EnsembleError(f"NullPanel 缺少信号日 {gone[0]} 等 {len(gone)} 天")
    buy_r, sell_r = _rates(cost_params)
    ptr = {k: 0 for k in surv}
    score = {k: None for k in surv}
    prev: dict = {}
    out = []
    for d in dates:
        for k in surv:                       # 吸收所有"持有期已结束"的行(next_date ≤ d);d 当天的行 next_date > d,不会被吸收
            lst = by[k]
            while ptr[k] < len(lst) and lst[ptr[k]]["next_date"] <= d:
                x = lst[ptr[k]]["excess"]
                score[k] = x if score[k] is None else lam * score[k] + (1 - lam) * x
                ptr[k] += 1
        rs = {k: today[(k, d)] for k in surv if (k, d) in today}
        ok = panel.tradable_mask.loc[d]
        tradable = {str(c) for c in ok.index[ok.to_numpy(dtype=bool)]}
        if mode == "vote":
            cnt: dict = {}
            for r in rs.values():
                for c in (r.get("picks") or list((r.get("weights") or {}))):
                    if c in tradable:
                        cnt[c] = cnt.get(c, 0) + 1
            raw = {c: 1.0 for c, _ in sorted(cnt.items(), key=lambda kv: (-kv[1], kv[0]))[:top_n]}
        else:
            w = blend_weights({k: score[k] for k in rs}, shrink)
            acc: dict = {}
            for k, r in rs.items():
                for c, x in (r.get("weights") or {}).items():
                    if c in tradable:
                        acc[c] = acc.get(c, 0.0) + w[k] * float(x)
            raw = dict(sorted(acc.items(), key=lambda kv: (-kv[1], kv[0]))[:top_n])
        st = scale.get(d, {"scale": 1.0, "state": "normal"}) if scale is not None else {"scale": 1.0, "state": "off"}
        tgt = _cap(raw, gross_cap * st["scale"], single_cap)
        rets = panel.ret.loc[d]
        gross = float(sum(v * float(np.nan_to_num(rets.get(c, 0.0))) for c, v in tgt.items()))
        buys = sum(max(tgt.get(c, 0.0) - prev.get(c, 0.0), 0.0) for c in set(tgt) | set(prev))
        sells = sum(max(prev.get(c, 0.0) - tgt.get(c, 0.0), 0.0) for c in set(tgt) | set(prev))
        cost = cost_mult * (buys * buy_r + sells * sell_r)
        net = gross - cost
        mkt = float(np.mean([r["mkt"] for r in rs.values()]))
        out.append({"key": key or f"ens_{mode}", "date": d, "next_date": max(r["next_date"] for r in rs.values()),
                    "gross": gross, "cost": cost, "net": net, "mkt": mkt, "excess": net - mkt,
                    "weights": tgt, "scale": st["scale"], "state": st["state"], "n_stocks": len(tgt),
                    "turnover": 0.5 * (buys + sells)})
        prev = tgt
    return out


# ---------------------------------------------------------------- 指标

def series_metrics(series: list) -> dict:
    """一条逐日行序列的汇总指标(净收益口径,年化按 252 日)。"""
    if not series:
        return {"days": 0}
    net = np.array([r["net"] for r in series], dtype="f8")
    nav = np.cumprod(1.0 + net)
    peak = np.maximum.accumulate(np.concatenate([[1.0], nav]))[1:]
    sd = float(net.std(ddof=1)) if len(net) > 1 else float("nan")
    return {"days": len(series), "cum_return": float(nav[-1] - 1.0),
            "ann_return": float(net.mean() * DAYS_PER_YEAR),
            "ann_vol": sd * math.sqrt(DAYS_PER_YEAR) if math.isfinite(sd) else None,
            "sharpe": float(net.mean() / sd * math.sqrt(DAYS_PER_YEAR)) if math.isfinite(sd) and sd > 0 else None,
            "max_drawdown": float((nav / peak - 1.0).min()),
            "mean_net_excess": float(np.mean([r["excess"] for r in series])),
            "monthly_turnover": float(np.mean([r.get("turnover", 0.0) for r in series]) * sel.DAYS_PER_MONTH)
            if "turnover" in series[0] else sel._turnover_monthly(series),
            "mean_gross_exposure": float(np.mean([sum((r.get("weights") or {}).values()) for r in series])),
            "total_cost": float(sum(r["cost"] for r in series)),
            "cut_days_frac": float(np.mean([1.0 if r.get("state") == "cut" else 0.0 for r in series]))}


# ---------------------------------------------------------------- 完整评估

def _slice(series: list, segment: str, cat: dict):
    kept, dropped = sel.rows_for_segment(series, segment, cat)
    return kept, dropped


def ensemble_study(rows, survivors, panel: "sel.NullPanel", mret: pd.Series, survivor_ps: dict | None = None,
                   seed=0, null_k: int = sel.NULL_K, top_n: int = TOP_N, halflife: float = HALFLIFE,
                   shrink: float = SHRINK, cost_params=None, catalog: dict | None = None) -> dict:
    """幸存者组合的完整验证报告。
    rows=回放行(训练+验证,可含随机对照 ctrl_*;不得含封存段);survivors=S4 冻结的幸存者键;
    mret=等权市场日收益(close 基,日期索引);survivor_ps={幸存者键: 验证段经验 p},用于 Holm 联合校正(可空)。
    返回报告字典;不写文件。"""
    cat = catalog or registry.load_validated()[0]
    chash = registry.catalog_hash(cat)
    seg = sel.segments(cat)
    surv = sorted(set(survivors))
    if not surv:
        return {"kind": "T008_S5_ensemble_report", "catalog_hash": chash, "status": "no_survivors",
                "note": "训练段没有幸存者,不组合。这是正常结果:先写死的规则下没有策略稳过随机对照。", "limits": LIMITS}
    sel._check_rows(rows)
    thr = freeze_vol_threshold(mret, seg["train"][1])
    scale = market_scale(mret, thr["threshold"])
    kw = dict(top_n=top_n, halflife=halflife, shrink=shrink, cost_params=cost_params)
    surv_rows = [r for r in rows if r["key"] in set(surv)]

    def build(mode, tag, **o):
        return build_ensemble(surv_rows, surv, panel, mode, key=f"ens_{mode}_{tag}", **{**kw, **o})

    variants = {}
    for mode in MODES:
        variants[mode] = {
            "raw": build(mode, "raw", scale=None, gross_cap=1.0, single_cap=1.0),
            "managed": build(mode, "managed", scale=scale),
            "managed_nofilter": build(mode, "nofilter", scale=None),
            "managed_cost2": build(mode, "cost2", scale=scale, cost_mult=2.0)}
    all_valid, _ = sel.rows_for_segment(rows, "valid", cat)
    by_valid = sel._by_key(all_valid)
    ctrl_cost = sel._control_cost(by_valid)
    ctrl_keys = sorted(k for k in by_valid if sel.is_control_key(k))
    ctrl_weekly = {k: float(np.mean(list(sel._weekly(v).values()))) for k, v in by_valid.items()
                   if sel.is_control_key(k)}
    m_base = sel.m_trials(cat)
    m_total = m_base + len(MODES)
    out_modes, ps = {}, {k: p for k, p in (survivor_ps or {}).items() if p is not None}
    for mode in MODES:
        v = variants[mode]
        raw_valid, _ = _slice(v["raw"], "valid", cat)
        base = sel._score_vs_null(raw_valid, panel, ctrl_cost, seed, null_k)
        dbl = sel._score_vs_null(raw_valid, panel, ctrl_cost, seed, null_k, cost_mult=2.0)
        if base["evaluable"]:
            ps[f"ens_{mode}"] = base["p_value"]
        out_modes[mode] = {"raw_valid_vs_null": base, "raw_valid_vs_null_cost_x2": dbl}
    adj = sel._holm_adj(ps, m_total) if ps else {}
    for mode in MODES:
        v, o = variants[mode], out_modes[mode]
        b = o["raw_valid_vs_null"]
        o["holm_p"] = adj.get(f"ens_{mode}")
        o["holm_significant"] = o["holm_p"] is not None and o["holm_p"] < sel.ALPHA
        o["beats_control"] = bool(b["evaluable"] and b["score"] > b["null_mean"])
        o["cost_x2_positive"] = bool(o["raw_valid_vs_null_cost_x2"].get("evaluable")
                                     and o["raw_valid_vs_null_cost_x2"]["score"] > 0)
        o["metrics"] = {}
        for name, s in v.items():
            o["metrics"][name] = {seg_name: series_metrics(_slice(s, seg_name, cat)[0]) for seg_name in ("train", "valid")}
        vm, nf = o["metrics"]["managed"]["valid"], o["metrics"]["managed_nofilter"]["valid"]
        o["filter_effect_valid"] = {
            k: (None if vm.get(k) is None or nf.get(k) is None else vm[k] - nf[k])
            for k in ("ann_return", "ann_vol", "max_drawdown", "monthly_turnover", "total_cost")}
        c2 = o["metrics"]["managed_cost2"]["valid"]
        o["cost_x2_managed_valid"] = {"ann_return": c2.get("ann_return"), "cum_return": c2.get("cum_return"),
                                      "ann_return_cost_x1": vm.get("ann_return")}
    # 参数邻域(只作诊断,不据此选参):top_n 与 halflife 的相邻档位在验证段是否也有正的平均净超额
    neigh = {}
    for mode in MODES:
        items = []
        grid = [("top_n", t, dict(top_n=t)) for t in TOP_N_NEIGHBORS]
        if mode == "weighted":
            grid += [("halflife", h, dict(halflife=h)) for h in HALFLIFE_NEIGHBORS]
        for name, val, o in grid:
            s = build_ensemble(surv_rows, surv, panel, mode, key=f"ens_{mode}_n", scale=None, gross_cap=1.0,
                               single_cap=1.0, **{**kw, **o})
            sv, _ = _slice(s, "valid", cat)
            mean_ex = float(np.mean([r["excess"] for r in sv])) if sv else None
            items.append({"param": name, "value": val, "mean_daily_net_excess": mean_ex,
                          "profitable": bool(mean_ex is not None and mean_ex > 0)})
        base_v, _ = _slice(variants[mode]["raw"], "valid", cat)
        bm = float(np.mean([r["excess"] for r in base_v])) if base_v else None
        neigh[mode] = {"base": {"top_n": top_n, "halflife": halflife, "mean_daily_net_excess": bm,
                                "profitable": bool(bm is not None and bm > 0)},
                       "neighbors": items, "param_island": any(not i["profitable"] for i in items)}
        out_modes[mode]["neighborhood"] = neigh[mode]
        out_modes[mode]["shadow_pool_eligible"] = bool(
            out_modes[mode]["beats_control"] and not neigh[mode]["param_island"]
            and out_modes[mode]["metrics"]["raw"]["valid"].get("monthly_turnover", 9) <= sel.MAX_MONTHLY_TURNOVER)
    floor_blocks = m_total * (1.0 / (null_k + 1)) > sel.ALPHA
    return {"kind": "T008_S5_ensemble_report", "catalog_hash": chash, "status": "ok", "survivors": surv,
            "n_survivors": len(surv), "segments": {k: list(v) for k, v in seg.items() if k != "holdout"},
            "params": {"top_n": top_n, "halflife": halflife, "shrink": shrink, "lambda": decay_lambda(halflife),
                       "ma_window": MA_WINDOW, "vol_window": VOL_WINDOW, "reduce": REDUCE,
                       "max_single": MAX_SINGLE, "max_gross": MAX_GROSS},
            "vol_threshold": thr, "seed": str(seed), "null_k": int(null_k), "m_base": m_base,
            "m_with_ensembles": m_total, "empirical_p_floor_blocks_holm": floor_blocks,
            "controls": {"n": len(ctrl_keys), "mean_weekly_net_excess": ctrl_weekly},
            "modes": out_modes, "limits": LIMITS}


def build_null_panel(eng, dates, codes=None) -> "sel.NullPanel":
    """由回放引擎 Engine 构造 NullPanel:信号日 T 的持有期收益 = 估值价 vo[T+2]/vo[T+1]-1,可交易 = eng.tradable[T+1]。
    dates=面板全部交易日(DatetimeIndex);最后两天没有 T+2,不产生信号日。"""
    idx = list(dates)
    cols = list(codes) if codes is not None else list(eng.vo.columns)
    n = len(idx)
    if n < 3:
        raise EnsembleError("交易日不足 3 天")
    vo = eng.vo.reindex(columns=cols)
    tr = eng.tradable.reindex(columns=cols)
    ret = (vo.iloc[2:].to_numpy() / vo.iloc[1:-1].to_numpy() - 1.0)
    ret = np.where(np.isfinite(ret), ret, 0.0)
    labels = [str(t.date()) for t in idx[: n - 2]]
    return sel.NullPanel(pd.DataFrame(ret, index=labels, columns=cols),
                         pd.DataFrame(tr.iloc[1:-1].to_numpy(dtype=bool), index=labels, columns=cols))


__all__ = ["build_ensemble", "ensemble_study", "market_scale", "freeze_vol_threshold", "market_index",
           "blend_weights", "decay_lambda", "series_metrics", "build_null_panel", "EnsembleError"]
