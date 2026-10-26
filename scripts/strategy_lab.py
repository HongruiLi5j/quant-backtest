"""策略验证实验室（T023）：用本地已有行情，对"单标的策略 × 品种"做事先登记的批量验证。只读、不联网、不导入 app。

设计原则（为什么这样做）：
  1. 事先登记：格子（策略、品种、随机种子、阈值）在算任何结果之前写进 PREREG.json，之后不改；
     这样"挑好看的组合"这种数据窥探会留下痕迹。
  2. 诚实的 n_trials：每个品种上同时比较了多少个策略/组合，就按多少填进 Deflated Sharpe，不是随便填。
     这仍是下限（没算人类之前手动试过的参数）。
  3. 多重检验：整个格子的"超额夏普 > 0"p 值统一做 Benjamini-Hochberg 校正（FDR 10%）。
  4. 发现集/确认集：A 股个股随机抽两批互不重叠的样本，只有在 A 批校正后显著的结论才去 B 批检验。
  5. 只用 registry.run（和网页同一套回测、含成本与涨跌停），不另写回测。
  已知局限：个股名单是"当前在市"的（幸存者偏差，偏乐观）；夏普的 i.i.d. 近似；滚动窗口彼此重叠，不是独立样本。

用法（项目根目录）：
  .venv/bin/python -B scripts/strategy_lab.py --out-dir DIR [--n-stocks 600] [--seed 20261025] [--max-minutes 270] [--smoke]
"""
import argparse
import itertools
import json
import math
import random
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import MARKETS  # noqa: E402
from src import allmarket, datasource, health_check, registry  # noqa: E402
from src.market_monitor import factors as fx  # noqa: E402
from src.validation import trials as tr  # noqa: E402

TD = 252
STRATS = ["blend2", "trend_ens", "trend_vol", "donchian_s1", "rsi2", "ma_adx_original", "ma_adx_plus", "buy_hold"]
STRATS = [s for s in STRATS if s != "buy_hold"]          # buy_hold 就是基准，超额恒为 0，不进格子
ETF_SYMBOLS = {"us": ["SPY", "QQQ", "IWM", "EFA", "GLD", "TLT", "IEF"], "cn": ["510300", "510500", "159915", "518880", "511010"]}
FDR_Q = 0.10
WIN_YEARS, WIN_STEP = 3, 1                                  # 滚动窗口：3 年，每次挪 1 年
BLEND_PAIRS = list(itertools.combinations(STRATS, 2))


def log(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def git_head():
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        return ""


def rets(equity):
    e = np.asarray(equity, dtype=float)
    return e[1:] / e[:-1] - 1.0


def ann_sharpe(r):
    r = np.asarray(r, dtype=float)
    sd = r.std(ddof=1) if len(r) > 1 else 0.0
    return float(r.mean() / sd * math.sqrt(TD)) if sd > 0 else float("nan")


def excess_p(r, b):
    """H0: 超额收益(策略−基准)的夏普 ≤ 0 的单侧 p 值 = 1 − PSR(0)。样本太短返回 None。"""
    x = np.asarray(r) - np.asarray(b)
    if len(x) < 60:
        return None, float("nan")
    m = tr.sample_moments(x)
    if not m["std"] > 0:
        return None, float("nan")
    sr_d = m["mean"] / m["std"]
    psr = tr.prob_sharpe_gt(sr_d, 0.0, len(x), m["skew"], m["kurt"])
    return (None if psr != psr else 1.0 - float(psr)), sr_d * math.sqrt(TD)


def bh_fdr(pvals, q=FDR_Q):
    """Benjamini-Hochberg：返回和输入等长的布尔列表（None 的 p 值一律不显著）。"""
    idx = [i for i, p in enumerate(pvals) if p is not None]
    for i in idx:
        if not (isinstance(pvals[i], (int, float)) and not isinstance(pvals[i], bool) and 0.0 <= pvals[i] <= 1.0):
            raise ValueError(f"p 值必须在 [0,1] 内且为有限数，收到 {pvals[i]!r}")      # NaN 比较恒为 False，会悄悄破坏排序
    order = sorted(idx, key=lambda i: pvals[i])
    m, kmax = len(order), 0
    for k, i in enumerate(order, 1):
        if pvals[i] <= q * k / m:
            kmax = k
    keep = set(order[:kmax])
    return [i in keep for i in range(len(pvals))]


def windows(n):
    w, s = WIN_YEARS * TD, WIN_STEP * TD
    return [(a, a + w) for a in range(0, n - w + 1, s)] if n >= w else []


def window_stats(r, b):
    out = []
    for a, z in windows(len(r)):
        out.append({"sharpe": ann_sharpe(r[a:z]), "excess_sharpe": ann_sharpe(r[a:z] - b[a:z]),
                    "ret": float(np.prod(1 + r[a:z]) - 1), "bench_ret": float(np.prod(1 + b[a:z]) - 1)})
    ok = [w for w in out if w["excess_sharpe"] == w["excess_sharpe"]]
    return {"n": len(out), "pos_excess": sum(1 for w in ok if w["excess_sharpe"] > 0),
            "pos_sharpe": sum(1 for w in ok if w["sharpe"] > 0),
            "worst_excess_sharpe": min((w["excess_sharpe"] for w in ok), default=None),
            "median_excess_sharpe": float(np.median([w["excess_sharpe"] for w in ok])) if ok else None}


def run_one(strat, sym, prices, cfg, market, health=False, n_trials=1):
    res = registry.run(strat, prices, sym, {}, cfg, market=market, detail=True)
    r, b = rets(res["equity"]), rets(res["bench"])
    p, ex_sr = excess_p(r, b)
    st = res["stats"]
    row = {"market": market, "strategy": strat, "symbol": sym, "days": len(r), "start": res["start"], "end": res["end"],
           "sharpe": st.get("Sharpe"), "bench_sharpe": res["bench_stats"].get("Sharpe"), "excess_sharpe": ex_sr,
           "excess_p": p, "cagr": st.get("CAGR%"), "maxdd": st.get("MaxDD%"), "trades": st.get("Trades"),
           "win_pct": st.get("Win%"), "exposure": st.get("Exposure%"), "windows": window_stats(r, b)}
    if health:
        h = health_check.health_check(res, n_trials=n_trials)
        row["health"] = {"verdict": h["verdict"]["code"], "checks": {c["id"]: c["status"] for c in h["checks"]}, "n_trials": n_trials}
    return row, res


def stage_etf(out, market_filter=None):
    rows, blends = [], []
    for market, syms in ETF_SYMBOLS.items():
        cfg = MARKETS[market]
        prices = datasource.load_market(market, cfg, with_stocks=False)
        for sym in syms:
            if sym not in prices:
                log(f"跳过 {market} {sym}：本地无行情")
                continue
            n_local = len(STRATS) + len(BLEND_PAIRS)       # 同一品种上同时比较的策略 + 两两组合 = 诚实的下限
            series = {}
            for s in STRATS:
                try:
                    row, res = run_one(s, sym, prices, cfg, market, health=True, n_trials=n_local)
                except Exception as e:                       # 单个失败不拖垮整晚
                    log(f"失败 {market} {s} {sym}: {type(e).__name__}: {e}")
                    continue
                rows.append(row)
                series[s] = pd.Series(rets(res["equity"]), index=pd.to_datetime(res["dates"][1:]))
                b = pd.Series(rets(res["bench"]), index=series[s].index)
            for a, c in BLEND_PAIRS:
                if a in series and c in series:
                    df = pd.concat([series[a], series[c], b], axis=1, join="inner").dropna()
                    mix = ((df.iloc[:, 0] + df.iloc[:, 1]) / 2).values
                    p, ex = excess_p(mix, df.iloc[:, 2].values)
                    blends.append({"market": market, "symbol": sym, "pair": [a, c], "sharpe": ann_sharpe(mix),
                                   "sharpe_a": ann_sharpe(df.iloc[:, 0].values), "sharpe_b": ann_sharpe(df.iloc[:, 1].values),
                                   "corr": float(np.corrcoef(df.iloc[:, 0], df.iloc[:, 1])[0, 1]), "excess_sharpe": ex, "excess_p": p,
                                   "windows": window_stats(mix, df.iloc[:, 2].values)})
            log(f"ETF {market} {sym} 完成")
    fam = bh_fdr([r["excess_p"] for r in rows] + [x["excess_p"] for x in blends])
    for r, f in zip(rows + blends, fam):
        r["fdr_significant"] = f
    (out / "etf_results.json").write_text(json.dumps({"singles": rows, "blends": blends}, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return rows, blends


def sample_codes(seed, n, exclude=()):
    lst = pd.read_csv(allmarket.LIST_FILE, dtype=str)
    codes = sorted(c for c in lst["code"] if datasource.is_valid_stock(c) and c not in set(exclude))
    return random.Random(seed).sample(codes, min(n, len(codes)))


def stage_stocks(out, name, codes, deadline):
    cfg = MARKETS["cn"]
    base = datasource.load_market("cn", cfg, with_stocks=False)
    rows, fails = [], 0
    for i, code in enumerate(codes):
        if time.time() > deadline:
            log(f"{name} 到时间上限，已完成 {i}/{len(codes)} 只")
            break
        try:
            df = allmarket.load(code)
            if df is None or len(df) < 3 * TD + 300:        # 需要约 4 年以上，否则滚动窗口没意义
                continue
            prices = dict(base)
            prices[code] = df
        except Exception:
            fails += 1
            continue
        for s in STRATS:
            try:
                row, _ = run_one(s, code, prices, cfg, "cn")
                rows.append(row)
            except Exception:
                fails += 1
        if (i + 1) % 50 == 0:
            log(f"{name} {i + 1}/{len(codes)}，累计 {len(rows)} 行，出错 {fails}")
    fam = bh_fdr([r["excess_p"] for r in rows])
    for r, f in zip(rows, fam):
        r["fdr_significant"] = f
    (out / f"stocks_{name}.json").write_text(json.dumps({"rows": rows, "failures": fails, "n_codes": len(codes)}, ensure_ascii=False, default=str), encoding="utf-8")
    return rows


def summarize_stocks(rows):
    out = {}
    for s in STRATS:
        x = [r for r in rows if r["strategy"] == s and r["excess_sharpe"] == r["excess_sharpe"]]
        if not x:
            continue
        ex = np.array([r["excess_sharpe"] for r in x])
        pos = int((ex > 0).sum())
        # 符号检验：若策略没有超额能力，超额夏普 >0 的个股应约占一半；p 为双侧二项检验（正态近似）
        z = (pos - len(x) / 2) / math.sqrt(len(x) / 4)
        p_sign = math.erfc(abs(z) / math.sqrt(2))
        wn = [r["windows"]["pos_excess"] / r["windows"]["n"] for r in x if r["windows"]["n"] > 0]
        out[s] = {"n": len(x), "median_excess_sharpe": float(np.median(ex)), "share_positive": pos / len(x), "sign_test_p": p_sign,
                  "fdr_significant_n": sum(1 for r in x if r.get("fdr_significant")), "median_share_windows_positive": float(np.median(wn)) if wn else None,
                  "median_sharpe": float(np.median([r["sharpe"] for r in x if r["sharpe"] is not None])),
                  "median_bench_sharpe": float(np.median([r["bench_sharpe"] for r in x if r["bench_sharpe"] is not None]))}
    return out


def stage_signals(out):
    """真实每日信号记录的事后检验：只有约 16 个信号日，样本极小，只能看方向，不能下结论。
    规则（无未来函数）：信号日 t 收盘产生，t+1 才能成交；这里保守地从 t+1 收盘算起，看之后 h 日收益，
    再减去同一天所有被评估股票的平均同期收益（市场调整）。"""
    f = Path("data/daily/signals_history.parquet")
    if not f.exists():
        return {"error": "没有 signals_history.parquet"}
    h = pd.read_parquet(f)
    h = h[h["market"] == "cn"]
    codes = sorted(h["code"].unique())
    close = {}
    for c in codes:
        try:
            d = allmarket.load(c)
            close[c] = d["Close"].astype(float)
        except Exception:
            continue
    px = pd.DataFrame(close).sort_index()
    res = {}
    for hz in (1, 5):
        fwd = px.shift(-(1 + hz)) / px.shift(-1) - 1                 # t+1 收盘 → t+1+h 收盘
        mkt = fwd.mean(axis=1)
        rows = []
        for (strat, act, d), g in h.groupby(["strategy", "action", "signal_date"]):
            dt = pd.Timestamp(d)
            if dt not in fwd.index:
                continue
            v = fwd.loc[dt, [c for c in g["code"] if c in fwd.columns]].dropna()
            if len(v) >= 5 and mkt.loc[dt] == mkt.loc[dt]:
                rows.append({"strategy": strat, "action": act, "date": d, "n": len(v), "adj": float(v.mean() - mkt.loc[dt])})
        df = pd.DataFrame(rows)
        agg = {}
        if len(df):
            for (strat, act), g in df.groupby(["strategy", "action"]):
                a = g["adj"].values
                se = a.std(ddof=1) / math.sqrt(len(a)) if len(a) > 1 else float("nan")
                agg[f"{strat}|{act}"] = {"signal_days": len(a), "mean_adj_ret": float(a.mean()), "t": float(a.mean() / se) if hz == 1 and se and se == se and se > 0 else None}   # 5 日窗口彼此重叠，t 会被夸大，所以只给 1 日的 t
        res[f"h{hz}"] = agg
    res["note"] = "信号日数很少且彼此相关（同一天的信号共享市场环境），t 值只是方向参考；5 日收益的窗口彼此重叠，所以不给 t；真正的结论至少要几十到上百个信号日。"
    (out / "signals_eval.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    return res


def stage_multi(out):
    """多品种策略 dual_momentum（组合层面，不在单品种格子里）：每个市场跑一次，同样做体检。"""
    rows = []
    for market in ("cn", "us"):
        cfg = MARKETS[market]
        prices = datasource.load_market(market, cfg, with_stocks=False)
        row, _ = run_one("dual_momentum", cfg["main"], prices, cfg, market, health=True, n_trials=len(STRATS) + 1)
        rows.append(row)
    (out / "multi_results.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return rows

# ---------------------------------------------------------------- S2 参数优化质量
# 事先登记的参数网格（全部在 registry 允许的范围内；不看结果调整）。默认参数另跑一次作对照。
GRIDS = {
    "trend_ens": {"target_vol": [0.10, 0.15, 0.20], "buffer": [0.0, 0.01, 0.02]},
    "trend_vol": {"sma_len": [100, 150, 200, 250], "buffer": [0.0, 0.01, 0.02]},
    "donchian_s1": {"entry_len": [10, 20, 40, 60], "exit_len": [5, 10, 20]},
    "rsi2": {"trend_len": [100, 200], "entry_th": [5, 10, 15], "exit_ma": [3, 5, 10]},
    "ma_adx_original": {"fast": [5, 10, 20], "slow": [20, 50, 100], "adx_min": [15, 20, 25]},
    "ma_adx_plus": {"fast": [5, 10, 20], "slow": [20, 50, 100], "atr_mult": [2.0, 3.0, 4.0]},
}
TRAIN_D, TEST_D = 4 * TD, 1 * TD          # 滚动前进：4 年训练选参数，接下来 1 年检验，每次挪 1 年


def combos(grid):
    keys = list(grid)
    return [dict(zip(keys, v)) for v in itertools.product(*grid.values())]


def spearman(a, b):
    a, b = pd.Series(a), pd.Series(b)
    if len(a) < 4 or a.nunique() < 2 or b.nunique() < 2:
        return float("nan")
    return float(a.rank().corr(b.rank()))


def walk_forward(R, D):
    """滚动前进的纯函数（便于用合成数据单测）：R 是各参数组合的日收益（列=组合），D 是默认参数的日收益。
    每一折只用训练窗 is_sr 选参数（idxmax），然后在紧随其后、不重叠的检验窗上评估；训练/检验窗长度取模块当前的 TRAIN_D/TEST_D
    （扩展脚本会临时改它们）。返回 (folds, 选出参数的样本外收益列表, 默认参数的样本外收益列表, 检验窗列表)。
    某折的训练或检验夏普全是 NaN 时整折跳过，并记录在 folds 里没有这一折，调用方应披露折数。"""
    folds, sel_oos, def_oos, te_all = [], [], [], []
    for a in range(0, len(R) - TRAIN_D - TEST_D + 1, TEST_D):
        tr_, te = R.iloc[a:a + TRAIN_D], R.iloc[a + TRAIN_D:a + TRAIN_D + TEST_D]
        is_sr = tr_.apply(lambda x: ann_sharpe(x.values))
        oos_sr = te.apply(lambda x: ann_sharpe(x.values))
        if is_sr.isna().all() or oos_sr.isna().all():
            continue
        best = is_sr.idxmax()
        te_all.append(te)
        sel_oos.append(te[best].values)
        def_oos.append(D.iloc[a + TRAIN_D:a + TRAIN_D + TEST_D].values)
        folds.append({"fold_start": a, "best": best, "oos_selected": float(oos_sr[best]), "oos_default": ann_sharpe(D.iloc[a + TRAIN_D:a + TRAIN_D + TEST_D].values),
                      "oos_grid_median": float(oos_sr.median()), "oos_grid_best": float(oos_sr.max()),
                      "rank_corr": spearman(is_sr.values, oos_sr.values),
                      "selected_oos_pct": float((oos_sr < oos_sr[best]).mean())})
    return folds, sel_oos, def_oos, te_all


def opt_quality(strat, sym, prices, cfg, market):
    """参数优化质量：每个参数组合在全历史上只算一次（信号只用过去数据，按日期切片不会引入新的未来信息（未做完整的未来函数测试）），
    然后滚动前进——只用训练窗的夏普选参数，拿到没见过的下一年检验。比较 选出的参数 / 默认参数 / 网格里随便一组。
    注意：没有计入换参数时的调仓成本（也没有计入切换时持仓衔接，优化结果偏乐观，程度未量化）。"""
    def series(params):
        res = registry.run(strat, prices, sym, params, cfg, market=market)
        return pd.Series(rets(res["equity"]), index=pd.to_datetime(res["dates"][1:]))
    default = series({})
    cols = {}
    for i, c in enumerate(combos(GRIDS[strat])):
        if "fast" in c and "slow" in c and c["fast"] >= c["slow"]:
            continue                                              # 快线必须短于慢线
        try:
            cols[i] = series(c)
        except Exception:
            continue
    if len(cols) < 6:
        return None
    R = pd.concat(cols, axis=1, join="inner").dropna()
    D = default.reindex(R.index).fillna(0.0)
    folds, sel_oos, def_oos, te_all = walk_forward(R, D)
    if not folds:
        return None
    f = pd.DataFrame(folds)
    # 主指标用"拼接全部样本外区间后算夏普"：逐折平均夏普会被"几乎不开仓、波动趋近 0"的折放大成虚高数（实测出现过 5~7），所以只作辅助
    T = pd.concat(te_all)
    grid_stitched = T.apply(lambda x: ann_sharpe(x.values))
    st_sel, st_def = ann_sharpe(np.concatenate(sel_oos)), ann_sharpe(np.concatenate(def_oos))
    return {"market": market, "strategy": strat, "symbol": sym, "n_combos": len(cols), "n_folds": len(f),
            "oos_selected": st_sel, "oos_default": st_def,
            "oos_grid_median": float(grid_stitched.median()), "oos_grid_best": float(grid_stitched.max()),
            "foldmean_selected": float(f["oos_selected"].mean()), "foldmean_default": float(f["oos_default"].mean()),
            "stitched_selected": st_sel, "stitched_default": st_def,
            "mean_rank_corr": float(f["rank_corr"].mean()), "pbo": float((f["selected_oos_pct"] < 0.5).mean()),
            "folds_selected_beats_default": float((f["oos_selected"] > f["oos_default"]).mean())}


def stage_opt_etf(out):
    rows = []
    for market, syms in ETF_SYMBOLS.items():
        cfg = MARKETS[market]
        prices = datasource.load_market(market, cfg, with_stocks=False)
        for sym in syms:
            if sym not in prices:
                continue
            for strat in GRIDS:
                try:
                    r = opt_quality(strat, sym, prices, cfg, market)
                except Exception as e:
                    log(f"优化失败 {market} {strat} {sym}: {type(e).__name__}: {e}")
                    continue
                if r:
                    rows.append(r)
            log(f"优化质量 ETF {market} {sym} 完成")
    (out / "opt_etf.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return rows


def stage_opt_stocks(out, name, codes, deadline):
    cfg = MARKETS["cn"]
    base = datasource.load_market("cn", cfg, with_stocks=False)
    rows = []
    for i, code in enumerate(codes):
        if time.time() > deadline:
            log(f"优化质量 {name} 到时间上限，已完成 {i}/{len(codes)} 只")
            break
        try:
            df = allmarket.load(code)
            if df is None or len(df) < TRAIN_D + 3 * TEST_D + 300:
                continue
            prices = dict(base)
            prices[code] = df
        except Exception:
            continue
        for strat in GRIDS:
            try:
                r = opt_quality(strat, code, prices, cfg, "cn")
            except Exception:
                continue
            if r:
                rows.append(r)
        if (i + 1) % 10 == 0:
            log(f"优化质量 {name} {i + 1}/{len(codes)}，累计 {len(rows)} 行")
    (out / f"opt_stocks_{name}.json").write_text(json.dumps(rows, ensure_ascii=False, default=str), encoding="utf-8")
    return rows


def summarize_opt(rows):
    out = {}
    for s in GRIDS:
        x = [r for r in rows if r["strategy"] == s]
        if not x:
            continue
        d = np.array([r["oos_selected"] - r["oos_default"] for r in x])
        z = (int((d > 0).sum()) - len(d) / 2) / math.sqrt(len(d) / 4) if len(d) else float("nan")
        out[s] = {"n": len(x), "oos_selected": float(np.median([r["oos_selected"] for r in x])), "oos_default": float(np.median([r["oos_default"] for r in x])),
                  "oos_grid_median": float(np.median([r["oos_grid_median"] for r in x])), "oos_grid_best": float(np.median([r["oos_grid_best"] for r in x])),
                  "rank_corr": float(np.nanmean([r["mean_rank_corr"] for r in x])), "pbo": float(np.mean([r["pbo"] for r in x])),
                  "share_selected_beats_default": float((d > 0).mean()), "sign_p": math.erfc(abs(z) / math.sqrt(2))}
    return out


# ---------------------------------------------------------------- S6 因子库体检
# 只用 2023-12-31 以前的数据：T008 的训练段从 2024-01-01 开始、封存段从 2026-04-01 起，这里不碰那些日期，也不重复它用过的数据。
FACTOR_END = "2023-12-31"
FACTOR_SPLIT = "2022-01-01"          # 2022 年以前做发现，2022~2023 做确认
FACTOR_START = "2016-01-01"
HORIZONS = {1: 5, 5: 5, 20: 20}      # 持有期 h 天 → 取样间隔（≥h，保证样本彼此不重叠）


FACTOR_BOUNDARY_VERSION = 2          # 版本 2：发现期标签的结束日必须早于分界（旧版 1 没有清除跨界标签，E4 拒绝读取）


def label_end(index, d, h):
    """信号日 d 的收益标签是 t+1 收盘到 t+1+h 收盘；返回按实际交易日索引算出的标签结束日（超出数据范围返回 None）。"""
    j = index.get_loc(d) + 1 + h
    return index[j] if j < len(index) else None


def label_segment(d, end, split=None):
    """把信号日分到 disc/conf/purged：信号日 >= 分界 → conf；信号日 < 分界但标签结束日 >= 分界（跨界）或未知 → purged（清除，不进任何一段）。"""
    split = pd.Timestamp(split or FACTOR_SPLIT)
    if d >= split:
        return "conf"
    if end is None or pd.isna(end) or end >= split:
        return "purged"
    return "disc"


def build_factor_panel(codes):
    cols = ["Open", "High", "Low", "Close", "Volume"]
    ser = {c: {} for c in cols}
    for code in codes:
        try:
            df = allmarket.load(code)
            df = df[(df.index >= "2014-01-01") & (df.index <= FACTOR_END)]
        except Exception:
            continue
        if len(df) < 400:
            continue
        for c in cols:
            ser[c][code] = df[c].astype(float)
    wide = {c: pd.DataFrame(ser[c]).sort_index() for c in cols}
    lst = pd.read_csv(allmarket.LIST_FILE, dtype=str).set_index("code")
    return fx.Panel(wide["Open"], wide["High"], wide["Low"], wide["Close"], wide["Volume"],
                    lst["name"].reindex(wide["Close"].columns), lst["industry"].reindex(wide["Close"].columns))


def seg_stats(vals):
    v = np.array([x for x in vals if x == x], dtype=float)
    if len(v) < 8 or v.std(ddof=1) == 0:
        return {"n": len(v), "mean": float(v.mean()) if len(v) else None, "t": None, "p": None, "hit": None}
    t = v.mean() / (v.std(ddof=1) / math.sqrt(len(v)))
    return {"n": len(v), "mean": float(v.mean()), "t": float(t), "p": math.erfc(abs(t) / math.sqrt(2)), "hit": float((v > 0).mean())}


def stage_factors(out, codes):
    panel = build_factor_panel(codes)
    close = panel.close
    log(f"因子面板：{close.shape[0]} 天 × {close.shape[1]} 只，{close.index[0].date()} ~ {close.index[-1].date()}")
    fwd = {h: close.shift(-(1 + h)) / close.shift(-1) - 1 for h in HORIZONS}      # 信号日 t 收盘后产生，t+1 收盘进，持有 h 天
    ok_idx = [d for d in close.index if d >= pd.Timestamp(FACTOR_START)]
    small = fx.Panel(*(getattr(panel, k).iloc[:, :60] for k in ("open", "high", "low", "close", "volume")), panel.names.iloc[:60], panel.industry.iloc[:60])
    results, excluded = [], []
    for fid, meta in fx.FACTORS.items():
        try:
            fx.check_no_lookahead(fid, small, 400)               # 未来函数断言不过的因子不进统计
        except Exception as e:
            excluded.append({"factor": fid, "reason": f"{type(e).__name__}: {e}"})
            log(f"因子 {fid} 未通过无未来函数检查，已排除")
            continue
        F = fx.compute_factor(fid, panel)
        for h, gap in HORIZONS.items():
            dates = ok_idx[::gap]
            ics = {"disc": [], "conf": []}
            n_purged = 0
            for d in dates:
                seg = label_segment(d, label_end(close.index, d, h))
                if seg == "purged":
                    n_purged += 1
                    continue
                f, r = F.loc[d], fwd[h].loc[d]
                m = r.notna()
                if meta.kind == "score":
                    x = (f * meta.direction).astype(float)
                    m = m & x.notna()
                    if m.sum() < 100:
                        continue
                    v = x[m].rank().corr(r[m].rank())
                else:
                    b = f.fillna(False).astype(bool)
                    if (m & b).sum() < 20 or (m & ~b).sum() < 20:
                        continue
                    v = r[m & b].mean() - r[m & ~b].mean()
                ics[seg].append(v)
            results.append({"factor": fid, "kind": meta.kind, "category": meta.category, "h": h, "n_purged": n_purged,
                            "disc": seg_stats(ics["disc"]), "conf": seg_stats(ics["conf"])})
        log(f"因子 {fid} 完成")
    ps = [r["disc"]["p"] for r in results]
    fam = bh_fdr(ps)
    for r, f in zip(results, fam):
        r["fdr_significant_disc"] = f
        d, c = r["disc"], r["conf"]
        r["confirmed"] = bool(f and d["mean"] is not None and c["p"] is not None and c["p"] < 0.05 and (d["mean"] > 0) == (c["mean"] > 0))
    (out / "factors.json").write_text(json.dumps({"boundary_version": FACTOR_BOUNDARY_VERSION, "results": results, "excluded": excluded,
                                                  "n_stocks": int(close.shape[1]), "n_tests": len(results)}, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return {"results": results, "excluded": excluded, "n_stocks": int(close.shape[1])}


def write_summary(out, meta, etf, stocks_a, stocks_b, sig, opt=None, fac=None):
    L = [f"# 策略验证实验室 · 早上看这里", "", f"开始 {meta['started']}，种子 {meta['seed']}，代码版本 {meta['git']}，"
         f"总用时 {meta.get('minutes', '?')} 分钟。格子在算任何结果之前已写入 PREREG.json。", "",
         "**读法提醒**：下面所有数字都是历史回测统计，不是预测；通过检验不等于策略有效；个股名单是当前在市股票（幸存者偏差，偏乐观）。", ""]
    if etf:
        rows, blends = etf
        L += ["## 1. 品种级体检（ETF，默认参数）", "",
              f"共 {len(rows)} 个策略×品种、{len(blends)} 个两两组合；整个家族做 BH-FDR（q={FDR_Q}）。", "",
              "| 市场 | 品种 | 策略 | 夏普 | 基准夏普 | 超额夏普 | 滚动3年窗口超额>0 | FDR显著 | 体检结论 |", "|---|---|---|---|---|---|---|---|---|"]
        for r in sorted(rows, key=lambda r: -(r["excess_sharpe"] if r["excess_sharpe"] == r["excess_sharpe"] else -9)):
            w = r["windows"]
            L.append(f"| {r['market']} | {r['symbol']} | {r['strategy']} | {r['sharpe']:.2f} | {r['bench_sharpe']:.2f} | {r['excess_sharpe']:.2f} | "
                     f"{w['pos_excess']}/{w['n']} | {'是' if r.get('fdr_significant') else '否'} | {r['health']['verdict']} |")
        n_sig = sum(1 for r in rows + blends if r.get("fdr_significant"))
        L += ["", f"FDR 校正后显著的有 {n_sig} 个（共 {len(rows) + len(blends)} 个）。", ""]
        hel = [b for b in blends if b["sharpe"] == b["sharpe"]]
        if hel:
            gain = [b["sharpe"] - (b["sharpe_a"] + b["sharpe_b"]) / 2 for b in hel]
            L += ["## 2. 策略组合是否有分散化收益", "",
                  f"两两等权组合共 {len(hel)} 个；组合夏普相对两个成分平均夏普的提升：中位数 {np.median(gain):+.2f}，"
                  f"提升>0 的占 {np.mean([g > 0 for g in gain]):.0%}；成分收益的平均相关系数 {np.mean([b['corr'] for b in hel]):.2f}。", ""]
    for name, rows in (("A（发现集）", stocks_a), ("B（确认集）", stocks_b)):
        if rows:
            sm = summarize_stocks(rows)
            L += [f"## 3{'a' if name[0] == 'A' else 'b'}. A 股个股 {name}", "", "| 策略 | 样本数 | 中位数超额夏普 | 超额>0 的占比 | 符号检验 p | FDR显著只数 | 滚动窗口超额>0 中位占比 |", "|---|---|---|---|---|---|---|"]
            for s, v in sm.items():
                L.append(f"| {s} | {v['n']} | {v['median_excess_sharpe']:+.2f} | {v['share_positive']:.0%} | {v['sign_test_p']:.3f} | {v['fdr_significant_n']} | "
                         f"{v['median_share_windows_positive']:.0%} |" if v["median_share_windows_positive"] is not None else f"| {s} | {v['n']} | - | - | - | - | - |")
            L.append("")
    if stocks_a and stocks_b:
        sa, sb = summarize_stocks(stocks_a), summarize_stocks(stocks_b)
        L += ["## 4. 确认检验（事先约定：A 批符号检验 p<0.05 且中位数超额>0 的策略，才看 B 批是否同向且 p<0.05）", "", "| 策略 | A 批 | B 批 | 是否确认 |", "|---|---|---|---|"]
        for s in sa:
            a_ok = sa[s]["sign_test_p"] < 0.05 and sa[s]["median_excess_sharpe"] > 0
            b = sb.get(s)
            ok = bool(a_ok and b and b["sign_test_p"] < 0.05 and b["median_excess_sharpe"] > 0)
            L.append(f"| {s} | {'通过' if a_ok else '未通过'}(p={sa[s]['sign_test_p']:.3f}) | "
                     f"{('p=%.3f 中位%+.2f' % (b['sign_test_p'], b['median_excess_sharpe'])) if b else '-'} | {'**确认**' if ok else '否'} |")
        L.append("")
    if sig and "error" not in sig:
        L += ["## 5. 真实每日信号的事后表现（样本极小）", "", sig.get("note", ""), ""]
        for hz in ("h1", "h5"):
            L += [f"### 信号后 {hz[1:]} 日，减去当天平均收益", "", "| 策略|动作 | 信号日数 | 平均调整后收益 | t |", "|---|---|---|---|"]
            for k, v in sorted(sig.get(hz, {}).items()):
                t = "-" if v["t"] is None else f"{v['t']:.2f}"
                L.append(f"| {k} | {v['signal_days']} | {v['mean_adj_ret']:+.3%} | {t} |")
            L.append("")
    if opt:
        for title, rows in opt:
            if not rows:
                continue
            L += [f"## 6. 参数优化质量：{title}", "",
                  "滚动前进（4 年训练选参数 → 下一年检验）；夏普为把所有样本外年份拼接后计算，表中数字是各标的的中位数。「选出」= 训练期夏普最高的参数；「默认」= 系统默认参数；「网格中位」= 网格里随便挑一组的平均水平；「事后最优」= 看了答案的上限。"
                  "排名相关 = 训练期排名与样本外排名的 Spearman 相关（≈0 说明训练期的好坏几乎预测不了样本外）；PBO = 训练期第一名在样本外掉到中位数以下的比例（越接近 50% 越像在拟合噪音）。", "",
                  "| 策略 | 样本 | 样本外夏普·选出 | 默认 | 网格中位 | 事后最优 | 排名相关 | PBO | 选出>默认的占比 | 符号检验 p |", "|---|---|---|---|---|---|---|---|---|---|"]
            for sname, v in summarize_opt(rows).items():
                L.append(f"| {sname} | {v['n']} | {v['oos_selected']:.2f} | {v['oos_default']:.2f} | {v['oos_grid_median']:.2f} | {v['oos_grid_best']:.2f} | "
                         f"{v['rank_corr']:+.2f} | {v['pbo']:.0%} | {v['share_selected_beats_default']:.0%} | {v['sign_p']:.3f} |")
            L += ["", "没有计入换参数时的调仓成本，所以「选出」一栏偏乐观。", ""]
    if fac:
        res = fac["results"]
        L += ["## 7. 因子库体检（24 个因子，仅用 2023-12-31 以前的数据）", "",
              f"样本 {fac['n_stocks']} 只 A 股随机抽样；发现期 {FACTOR_START[:4]}~2021，确认期 2022~2023；未碰 T008 的训练/验证/封存段。"
              f"共 {len(res)} 个检验（因子 × 持有期），统一 BH-FDR（q={FDR_Q}）；score 因子看 rank IC，flag/filter 因子看触发组减未触发组的平均收益。", "",
              f"**发现期 FDR 显著：{sum(1 for r in res if r['fdr_significant_disc'])} 个；确认期同向且 p<0.05：{sum(1 for r in res if r['confirmed'])} 个。**", ""]
        if fac["excluded"]:
            L += ["未通过无未来函数检查而排除：" + "、".join(e["factor"] for e in fac["excluded"]), ""]
        L += ["| 因子 | 类型 | 持有期 | 发现期均值 | t | 确认期均值 | t | FDR显著 | 确认 |", "|---|---|---|---|---|---|---|---|---|"]
        def fmt(x, nd=4):
            return "-" if x is None else f"{x:+.{nd}f}"
        for r in sorted(res, key=lambda r: -abs(r["disc"]["t"] or 0)):
            L.append(f"| {r['factor']} | {r['kind']} | {r['h']}日 | {fmt(r['disc']['mean'])} | {fmt(r['disc']['t'], 2)} | {fmt(r['conf']['mean'])} | "
                     f"{fmt(r['conf']['t'], 2)} | {'是' if r['fdr_significant_disc'] else '否'} | {'**是**' if r['confirmed'] else '否'} |")
        L.append("")
    L += ["## 局限", "", "- 幸存者偏差；夏普按 i.i.d. 近似；滚动窗口重叠不独立；成本与滑点是估算；n_trials 只算了本实验室内的比较数，是下限。",
          "- 只用默认参数，没有调参；这正是为了不引入新的数据窥探。", "- 这份报告不构成投资建议。"]
    (out / "MORNING.md").write_text("\n".join(L), encoding="utf-8")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--n-stocks", type=int, default=600)
    ap.add_argument("--n-opt-stocks", type=int, default=80, help="参数优化质量每批的个股数")
    ap.add_argument("--n-factor-stocks", type=int, default=800)
    ap.add_argument("--seed", type=int, default=20261025)
    ap.add_argument("--max-minutes", type=int, default=270)
    ap.add_argument("--smoke", action="store_true", help="小规模自检：每批只 6 只股票")
    ap.add_argument("--exclude-prereg", help="另一轮的 PREREG.json：把它用过的 A/B 股票排除掉，保证本轮是全新的独立样本")
    a = ap.parse_args(argv)
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    n = 6 if a.smoke else a.n_stocks
    n_opt = 3 if a.smoke else a.n_opt_stocks
    n_fac = 150 if a.smoke else a.n_factor_stocks
    used = []
    if a.exclude_prereg:
        pj = json.loads(Path(a.exclude_prereg).read_text(encoding="utf-8"))
        used = list(pj["stock_sample_A"]) + list(pj["stock_sample_B"])
    A = sample_codes(a.seed, n, exclude=used)
    B = sample_codes(a.seed + 1, n, exclude=list(A) + used)
    FAC = sample_codes(a.seed + 2, n_fac)
    meta = {"started": time.strftime("%Y-%m-%d %H:%M"), "seed": a.seed, "git": git_head()}
    prereg = {**meta, "strategies": STRATS, "etf_symbols": ETF_SYMBOLS, "params": "S1/S3 全部默认参数，不调参", "windows": f"{WIN_YEARS}年窗口/{WIN_STEP}年步长",
              "fdr_q": FDR_Q, "n_trials_per_etf_symbol": len(STRATS) + len(BLEND_PAIRS), "blend_pairs": len(BLEND_PAIRS),
              "stock_sample_A": A, "stock_sample_B": B, "confirmation_rule": "A 批符号检验 p<0.05 且中位超额>0 才看 B 批是否同向且 p<0.05",
              "opt_grids": GRIDS, "opt_walk_forward": f"训练 {TRAIN_D} 天 / 检验 {TEST_D} 天 / 步长 {TEST_D} 天", "opt_stock_count_per_batch": n_opt,
              "factor_window": f"{FACTOR_START} ~ {FACTOR_END}（发现 < {FACTOR_SPLIT} ≤ 确认）", "factor_horizons": {str(k): v for k, v in HORIZONS.items()},
              "factor_stock_sample": FAC, "factor_tests": len(fx.FACTORS) * len(HORIZONS),
              "excluded_on_purpose": "T008 回放目录的 100 个策略/490 个参数点：已按冻结规则淘汰，按所有者决定不重跑，本实验不使用",
              "note": "本文件在计算任何结果之前写入，之后不得修改"}
    pf = out / "PREREG.json"
    if pf.exists():
        sys.exit("PREREG.json 已存在：拒绝覆盖（事先登记的格子不能改）。换一个输出目录。")
    pf.write_text(json.dumps(prereg, ensure_ascii=False, indent=1), encoding="utf-8")
    deadline_all = t0 + a.max_minutes * 60
    deadline_mid = t0 + a.max_minutes * 60 * 0.75           # 个股两批留出后面优化阶段的时间
    st = {"etf": None, "sig": None, "A": None, "B": None, "multi": None, "oetf": None, "oA": None, "oB": None, "fac": None}
    stages = [("ETF", "etf", lambda: stage_etf(out)), ("信号", "sig", lambda: stage_signals(out)),
              ("多品种·dual_momentum", "multi", lambda: stage_multi(out)), ("优化质量·ETF", "oetf", lambda: stage_opt_etf(out)), ("因子", "fac", lambda: stage_factors(out, FAC)),
              ("A", "A", lambda: stage_stocks(out, "A", A, deadline_mid)), ("B", "B", lambda: stage_stocks(out, "B", B, deadline_mid)),
              ("优化质量·A", "oA", lambda: stage_opt_stocks(out, "A", A[:n_opt], deadline_all)),
              ("优化质量·B", "oB", lambda: stage_opt_stocks(out, "B", B[:n_opt], deadline_all))]
    for name, key, fn in stages:
        if time.time() > deadline_all:
            log(f"跳过 {name}：已到总时间上限")
            continue
        try:
            log(f"开始 {name}")
            st[key] = fn()
        except Exception as e:
            log(f"阶段 {name} 失败：{type(e).__name__}: {e}")
        meta["minutes"] = round((time.time() - t0) / 60)
        write_summary(out, meta, st["etf"], st["A"], st["B"], st["sig"],
                      opt=[("ETF", st["oetf"]), ("A 股个股·A 批", st["oA"]), ("A 股个股·B 批", st["oB"])], fac=st["fac"])
    log("全部结束")


if __name__ == "__main__":
    main()
