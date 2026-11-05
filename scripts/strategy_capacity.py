"""C07 策略容量：按真实成交额估算七个正式策略能承载多少资金。

问题：回测默认「买多少都按收盘/开盘价成交、只扣固定 8bp」。资金大了以后，一笔调仓可能占到该股当天成交额的很大一块，
  价格会被自己推着走（市场冲击），净收益就比回测低。容量 = 冲击成本还没把收益吃掉之前，最多能放多少钱。
做法（全程复用项目已有函数，不改任何策略规则）：
  1. 行情读进来立刻截到 CUTOFF（2026-03-31），再算信号：registry.target_weights → portfolio.run_weights（次日开盘成交、
     扣 8bp、10% 调仓阈值、空仓现金计息、A 股一字板），口径同 C04/C06。逐笔成交 = run_weights 返回的 weights 的逐日变化（已含一字板没成交的情况）。
  2. 资金怎么摆：总资金 K 平分给 N 只股票，每只股票是一个「仓位袖」，袖里资金 A = K/N 固定不滚雪球（假设利润取走、亏损补回）。
     袖里策略仓位是 0~1 的比例，所以一笔调仓的成交额 = A × |仓位变化|。默认 N=20（和项目 AI 账户「最多 20 只」一致），另给 N=10、50。
  3. 参与率 = 这一笔成交额 / 该股当日成交额。当日成交额用旁表 src/turnover.py 的真实成交额（元）。
     *** 数据局限：旁表只有 2026-09-11 之后的数据，全在研究截止日之后；这里只用它当「每只股票典型日成交额（ADV）」的静态水平，
         不取任何价格/收益，也不影响信号和成交日期；把 2026 年秋天的水平往回套到 2020~2026-03，是近似。给 ADV×0.5、×2 的敏感性。***
  4. 平方根冲击模型：单边冲击成本（占成交额的比例）= c · σ · sqrt(参与率)。σ = 信号日收盘及以前 60 日日收益标准差（不含当日，无未来函数）。
     c 是假设的系数（没有 A 股逐笔委托数据可以校准），取 0.1 / 0.5 / 1.0 三档，覆盖文献常见的 0.1~1 区间。
  5. 因为 c·σ·sqrt(A·|Δw|/ADV)·|Δw| = c·sqrt(A)·[σ·|Δw|^1.5/sqrt(ADV)]，冲击拖累对资金规模恰好是 sqrt(K) 比例，
     所以每个策略只需算一次「单位拖累」D1，任何 K、c、N、ADV 倍数的结果都能闭式得到，不用重跑回测。
  6. 容量：拖累年化 = c·sqrt(K/N)·D1。容量 K* 取下面两条之一刚好成立的资金：
       (a) 年化拖累 = 1 个百分点；(b) 年化拖累 = 毛超额年化收益的一半（毛收益 ≤ 0 时无定义）。
     K* = N·(预算/(c·D1))²。

只读 data/ 下的行情和旁表，不写 data/；不联网。

用法：
  .venv/bin/python -B scripts/strategy_capacity.py --out-dir DIR [--data-dir data] [--n-stocks 1000] [--seed 6] [--workers 4]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import multiprocessing as mp
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from config import MARKETS, TRADE_LIST_MIN_TURNOVER  # noqa: E402
from src import registry, turnover  # noqa: E402
from src.datasource import limit_pct  # noqa: E402
from src.metrics import TRADING_DAYS  # noqa: E402
from src.portfolio import run_weights  # noqa: E402
from deflated_sharpe_table import check_end, find_daily_dir  # noqa: E402  C04：截止日检查、找行情目录
from strategy_overlap import BAND, CUTOFF, ORDER, STOCK_COST, WARM, WIN_START  # noqa: E402  C06：同一套窗口/成本/策略清单

SIGMA_WINDOW = 60               # 波动率回看窗口（交易日），只用信号日收盘及以前
SIGMA_MIN = 20                  # 至少 20 个有效日收益才给 σ
MIN_ADV_DAYS = 7                # 共同交易日（见 load_adv）里至少 7 天有成交额才算有 ADV
COMMON_COVER = 0.9              # 一天的旁表行数 ≥ 当日最大行数的 90%，才算「全市场覆盖」的共同交易日
EPS = 1e-9                      # 仓位变化小于它不算一笔交易
CAPITALS = [1e5, 1e6, 1e7, 1e8, 1e9]     # 任务要求的 10万/100万/1000万/1亿，外加 10亿（看参与率在多大资金时才明显）
NS = [10, 20, 50]                        # 同时持有的股票数（每只股票一个仓位袖）
N_MAIN = 20
ADV_MULTS = [0.5, 1.0, 2.0]              # ADV 敏感性：旁表水平往回套的误差
COEFS = [0.1, 0.5, 1.0]                  # 平方根冲击系数 c（假设，覆盖文献常见区间）
C_MAIN = 0.5
THRESHOLDS = [0.05, 0.10, 1.0]           # 参与率阈值：5%、10%、100%（超过 100% = 当天根本成交不完）
BUDGET_PP = 0.01                         # 容量规则 (a)：年化拖累 1 个百分点
BUCKET_EDGES = [0.0, TRADE_LIST_MIN_TURNOVER, 2e8, 1e9, np.inf]
BUCKET_LABELS = ["<5千万", "5千万~2亿", "2亿~10亿", "≥10亿"]
ALL = "全部"


# ------------------------------------------------------------------ 公式（纯函数，测试里单独验）
def participation(trade_value, adv):
    """参与率 = 成交额 / 该股当日成交额（元）。"""
    return np.asarray(trade_value, float) / np.asarray(adv, float)


def impact_frac(c, sigma, part):
    """平方根冲击：单边冲击成本占成交额的比例 = c · σ · sqrt(参与率)。σ 是日波动率。"""
    return c * np.asarray(sigma, float) * np.sqrt(np.asarray(part, float))


def trailing_sigma(close: pd.Series, window: int = SIGMA_WINDOW, minimum: int = SIGMA_MIN) -> pd.Series:
    """第 t 天的 σ = 到 t−1 日收盘为止的 window 日日收益标准差。shift(1) 保证不含 t 日自己（t 日开盘成交，决策在 t−1 收盘后）。"""
    return close.pct_change().rolling(window, min_periods=minimum).std().shift(1)


def unit_drag(absdw: np.ndarray, sigma: np.ndarray, adv: np.ndarray) -> np.ndarray:
    """单位拖累 x = σ·|Δw|^1.5/sqrt(ADV)（逐 日×股票）。实际冲击成本 = c·sqrt(A)·x。
    推导：冲击率 = c·σ·sqrt(A|Δw|/ADV)，乘以成交额占袖比例 |Δw|，得 c·sqrt(A)·σ·|Δw|^1.5/sqrt(ADV)。σ 缺失的格子按 0 处理（并在 meta 里计数）。"""
    x = np.nan_to_num(sigma, nan=0.0) * np.where(absdw > EPS, absdw, 0.0) ** 1.5 / np.sqrt(adv)[None, :]
    return x


def participation_stats(absdw: np.ndarray, adv: np.ndarray, A: float, thresholds=THRESHOLDS) -> dict:
    """absdw：D×S 每格 |仓位变化|；adv：S 只股票的 ADV。只统计 |Δw|>EPS 的格子（一笔交易）。
    返回交易笔数、各阈值下按笔数/按成交额加权的占比、参与率中位数。"""
    mask = absdw > EPS
    n = int(mask.sum())
    if n == 0:
        return {"n_trades": 0, "median_part": np.nan, **{f"share_gt{int(t * 100)}": np.nan for t in thresholds},
                **{f"value_share_gt{int(t * 100)}": np.nan for t in thresholds}}
    adv_full = np.broadcast_to(adv[None, :], absdw.shape)
    dw = absdw[mask]
    part = participation(A * dw, adv_full[mask])
    out = {"n_trades": n, "median_part": float(np.median(part))}
    for t in thresholds:
        over = part > t
        out[f"share_gt{int(t * 100)}"] = float(over.mean())
        out[f"value_share_gt{int(t * 100)}"] = float(dw[over].sum() / dw.sum())
    return out


def capacity_capital(d1: float, budget: float, c: float, n: int) -> tuple[float, float]:
    """拖累 = c·sqrt(A)·D1 = 预算 ⇒ A* = (预算/(c·D1))²，K* = N·A*。D1 ≤ 0 或预算 ≤ 0 → (inf, inf) / (nan, nan)。"""
    if not np.isfinite(budget) or budget <= 0:
        return float("nan"), float("nan")
    if d1 <= 0:
        return float("inf"), float("inf")
    a = (budget / (c * d1)) ** 2
    return float(n * a), float(a)


def ann_mean(x) -> float:
    """日收益序列 → 年化算术均值（×252）。"""
    x = np.asarray(x, float)
    x = x[np.isfinite(x)]
    return float(TRADING_DAYS * x.mean()) if len(x) else float("nan")


def cagr(x) -> float:
    x = np.asarray(x, float)
    x = x[np.isfinite(x)]
    return float(np.prod(1 + x) ** (TRADING_DAYS / len(x)) - 1) if len(x) else float("nan")


def bucket_of(adv: np.ndarray) -> np.ndarray:
    """ADV（元）→ 流动性档位下标 0~3。"""
    return np.clip(np.searchsorted(BUCKET_EDGES, adv, side="right") - 1, 0, len(BUCKET_LABELS) - 1)


# ------------------------------------------------------------------ 数据：ADV 代理
def load_adv(daily: Path, stat: str = "median", min_days: int = MIN_ADV_DAYS) -> tuple[pd.Series, dict]:
    """从真实成交额旁表取每只股票的 ADV（元）。只留 date/code/turnover_yuan 三列，价格列一读进来就丢掉。
    旁表里约 2400 只股票有 20~29 天（腾讯回补），另外约 2800 只只有最近约 8 天（每日快照）。为了让所有股票口径一致、不偏向大盘股，
    ADV 一律只用「全市场覆盖」的共同交易日（当日行数 ≥ 最大行数的 90%）；某只股票这些共同日里至少有 min_days 天才算有 ADV。
    旁表日期全在 CUTOFF 之后：这是任务指定的数据，只当静态流动性水平用（见模块说明），日期范围写进 meta 供核对。"""
    path = daily / "turnover_cn.parquet"
    t = turnover.load(path)[["date", "code", "turnover_yuan"]]
    t = t[np.isfinite(t["turnover_yuan"]) & (t["turnover_yuan"] > 0)]
    per_day = t.groupby("date").size()
    common = per_day[per_day >= COMMON_COVER * per_day.max()].index
    tc = t[t["date"].isin(common)]
    g = tc.groupby("code")["turnover_yuan"]
    adv = (g.median() if stat == "median" else g.mean())[g.count() >= min_days]
    # 核对：对旁表里天数更长的股票，共同日中位数与全部日中位数差多少（ADV 的噪声有多大）
    full_med = t.groupby("code")["turnover_yuan"].median()
    long_codes = t.groupby("code").size()
    long_codes = long_codes[long_codes >= 20].index.intersection(adv.index)
    ratio = (adv[long_codes] / full_med[long_codes]) if len(long_codes) else pd.Series(dtype=float)
    info = {"stat": stat, "min_days": min_days, "table_first": str(t["date"].min()), "table_last": str(t["date"].max()),
            "table_days": int(t["date"].nunique()), "common_days": [str(d) for d in sorted(common)],
            "codes_in_table": int(t["code"].nunique()), "codes_with_adv": int(len(adv)),
            "common_vs_full_median_ratio": ({"codes": int(len(ratio)), "p10": float(ratio.quantile(0.1)), "p50": float(ratio.median()),
                                             "p90": float(ratio.quantile(0.9))} if len(ratio) else None),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    return adv.rename("adv"), info


# ------------------------------------------------------------------ 单只股票：7 个策略的逐日仓位变化和超额收益
_CASH: pd.Series | None = None


def _init_worker(cash: pd.Series) -> None:
    """spawn 出来的子进程拿不到父进程的全局变量，用 initializer 把现金 ETF 日收益传进去。"""
    global _CASH
    _CASH = cash


def stock_job(args):
    """返回窗口内的 {idx, sigma, 策略: (超额收益, |Δw|)}。df 必须已截到 CUTOFF。"""
    code, name, path = args
    df = pd.read_parquet(path).sort_index().loc[:CUTOFF]
    return stock_panel(df, code, name, _CASH)


def stock_panel(df: pd.DataFrame, code: str, name: str, cash: pd.Series) -> dict:
    check_end(str(df.index[-1].date()))
    prices = {code: df}
    lim = {code: limit_pct(code, name)}
    out, idx = {}, None
    for strat in ORDER:
        W = registry.target_weights(strat, prices, code, registry.clean_params(strat, {}), MARKETS["cn"])
        res = run_weights(prices, W, cost=STOCK_COST, band=BAND, cash_ret=cash, limits=lim)
        held = res.weights.iloc[:, 0]
        absdw = held.diff().fillna(held).abs()           # 开盘成交后仓位的逐日变化 = 当天实际成交的仓位比例
        ret = res.returns - cash.reindex(res.returns.index).fillna(0.0)
        out[strat] = (ret, absdw)
        idx = ret.index
    start = max(pd.Timestamp(WIN_START), idx[min(WARM, len(idx) - 1)])
    sl = slice(start, pd.Timestamp(CUTOFF))
    sigma = trailing_sigma(df["Close"]).reindex(idx).loc[sl]
    return {"code": code, "idx": sigma.index, "sigma": sigma.to_numpy(),
            **{s: (r.loc[sl].to_numpy(), a.reindex(idx).loc[sl].to_numpy()) for s, (r, a) in out.items()}}


def eligible_stocks(daily: Path, seed: int, adv: pd.Series):
    """同 C06 sample_stocks 的筛选顺序（固定种子打乱），额外要求有 ADV。逐个产出 (code, name, path)，由调用方凑够数量。
    合格 = 非 ST/退、窗口开始前至少 WARM+260 根历史、2018 年底前已上市、行情到 CUTOFF 附近。只含现存股票 → 有幸存者偏差。"""
    lst = pd.read_csv(daily / "cn_all_list.csv", dtype=str).fillna("")
    lst = lst[~lst["name"].str.upper().str.contains("ST") & ~lst["name"].str.contains("退")]
    codes = sorted(lst["code"])
    names = dict(zip(lst["code"], lst["name"]))
    for i in np.random.default_rng(seed).permutation(len(codes)):
        code = codes[i]
        p = daily / "cn_all" / f"{code}.parquet"
        if p.exists() and code in adv.index:
            yield code, names[code], p


def collect_stocks(daily: Path, n: int, seed: int, adv: pd.Series, workers: int, log=print) -> tuple[list, int]:
    """取够 n 只通过历史长度检查的股票。先按顺序多取一批候选并行算，再按顺序截断，保证结果与 workers 无关。"""
    global _CASH
    cash_df = pd.read_csv(daily / "cn" / f"{MARKETS['cn']['cash']}.csv", index_col=0, parse_dates=True).sort_index().loc[:CUTOFF]
    _CASH = cash_df["Close"].pct_change()
    cands = []
    for code, name, p in eligible_stocks(daily, seed, adv):
        cands.append((code, name, p))
    results, tried, pos = [], 0, 0
    # 用 spawn 而不是 fork：父进程已经用过 pyarrow 的线程池，fork 多线程进程有死锁风险
    pool = mp.get_context("spawn").Pool(workers, initializer=_init_worker, initargs=(_CASH,)) if workers > 1 else None
    try:
        while len(results) < n and pos < len(cands):
            batch = cands[pos:pos + max(workers * 8, n - len(results))]
            pos += len(batch)
            tried += len(batch)
            ok = [b for b in batch if _history_ok(b[2])]
            res = pool.map(stock_job, ok, chunksize=4) if pool else [stock_job(b) for b in ok]
            results += res
            log(f"  已完成 {min(len(results), n)}/{n} 只（候选已看 {tried}）")
    finally:
        if pool:
            pool.close()
            pool.join()
    return results[:n], tried


def _history_ok(path: Path) -> bool:
    df = pd.read_parquet(path, columns=["Close"]).sort_index().loc[:CUTOFF]
    return not (len(df) < WARM + 260 or df.index[0] > pd.Timestamp("2018-12-31")
                or df.index[-1] < pd.Timestamp(CUTOFF) - pd.Timedelta(days=10))


# ------------------------------------------------------------------ 拼成 日期×股票 面板
def build_panel(results: list, adv: pd.Series) -> dict:
    dates = results[0]["idx"]
    for r in results[1:]:
        dates = dates.union(r["idx"])
    D, S = len(dates), len(results)
    sigma = np.full((D, S), np.nan)
    valid = np.zeros((D, S), bool)
    ret = {s: np.full((D, S), np.nan) for s in ORDER}
    dw = {s: np.zeros((D, S)) for s in ORDER}
    for j, r in enumerate(results):
        pos = dates.get_indexer(r["idx"])
        valid[pos, j] = True
        sigma[pos, j] = r["sigma"]
        for s in ORDER:
            ret[s][pos, j] = r[s][0]
            dw[s][pos, j] = r[s][1]
    codes = [r["code"] for r in results]
    a = adv.reindex(codes).to_numpy(float)
    return {"dates": dates, "codes": codes, "valid": valid, "sigma": sigma, "ret": ret, "dw": dw, "adv": a,
            "bucket": bucket_of(a)}


def daily_mean(M: np.ndarray, cols: np.ndarray | None = None) -> np.ndarray:
    """日期×股票 → 每天对（有效的）股票等权平均；无效格子是 nan，被忽略。"""
    X = M if cols is None else M[:, cols]
    with np.errstate(all="ignore"):
        return np.nanmean(X, axis=1)


def x_matrix(panel: dict, strat: str, mult: float = 1.0) -> np.ndarray:
    """单位拖累 x（D×S），无效格子 nan。ADV 乘 mult → x 除以 sqrt(mult)。"""
    x = unit_drag(panel["dw"][strat], panel["sigma"], panel["adv"] * mult)
    return np.where(panel["valid"], x, np.nan)


# ------------------------------------------------------------------ 统计
def turnover_rows(panel: dict) -> list[dict]:
    years = panel["valid"].sum(axis=0) / TRADING_DAYS                 # 每只股票的窗口年数
    rows = []
    for s in ORDER:
        dw = panel["dw"][s]
        trades = dw > EPS
        n_tr = trades.sum(axis=0)
        rows.append({"strategy": s,
                     "annual_oneway_turnover": float(np.mean(dw.sum(axis=0) / years)),         # 每年单边换手（仓位袖的倍数）
                     "trades_per_stock_year": float(np.mean(n_tr / years)),
                     "mean_abs_dw_per_trade": float(dw[trades].mean()) if trades.any() else float("nan"),
                     "mean_sigma_at_trade": float(np.nanmean(panel["sigma"][trades])) if trades.any() else float("nan"),
                     "missing_sigma_trades": int(np.isnan(panel["sigma"][trades]).sum()),
                     "n_trades": int(trades.sum())})
    return rows


def bucket_rows(panel: dict) -> list[dict]:
    """每个流动性档位：股数、ADV 分位数、交易时 σ（blend2）、七个策略的年单边换手均值。解释「小盘为什么容量小」用。"""
    b = panel["bucket"]
    years = panel["valid"].sum(axis=0) / TRADING_DAYS
    rows = []
    for k, lab in enumerate(BUCKET_LABELS):
        gm = b == k
        if not gm.any():
            continue
        tr = panel["dw"]["blend2"][:, gm] > EPS
        row = {"bucket": lab, "n_stocks": int(gm.sum()), "adv_p25": float(np.percentile(panel["adv"][gm], 25)),
               "adv_median": float(np.median(panel["adv"][gm])), "adv_p75": float(np.percentile(panel["adv"][gm], 75)),
               "sigma_daily_all_days": float(np.nanmean(panel["sigma"][:, gm])),
               "sigma_at_blend2_trade": float(np.nanmean(panel["sigma"][:, gm][tr]))}
        for s in ORDER:
            row[f"turnover_{s}"] = float(np.mean(panel["dw"][s][:, gm].sum(axis=0) / years[gm]))
        rows.append(row)
    return rows


def participation_rows(panel: dict) -> list[dict]:
    rows = []
    b = panel["bucket"]
    groups = [(ALL, np.ones(len(b), bool))] + [(lab, b == k) for k, lab in enumerate(BUCKET_LABELS)]
    for s in ORDER:
        for gname, gm in groups:
            if not gm.any():
                continue
            dw = panel["dw"][s][:, gm]
            for n in NS:
                for m in ADV_MULTS:
                    for K in CAPITALS:
                        A = K / n
                        rows.append({"strategy": s, "bucket": gname, "n_stocks": int(gm.sum()), "N": n, "adv_mult": m, "K": K, "A": A,
                                     **participation_stats(dw, panel["adv"][gm] * m, A)})
    return rows


def drag_tables(panel: dict) -> tuple[list[dict], list[dict], dict]:
    """返回 (拖累行, 容量行, 每个 策略×档位×m 的 D1 和毛收益)。"""
    b = panel["bucket"]
    groups = [(ALL, np.ones(len(b), bool))] + [(lab, b == k) for k, lab in enumerate(BUCKET_LABELS)]
    drag_rows, cap_rows, base = [], [], {}
    for s in ORDER:
        for gname, gm in groups:
            if not gm.any():
                continue
            R = daily_mean(panel["ret"][s], gm)
            for m in ADV_MULTS:
                X = daily_mean(x_matrix(panel, s, m), gm)
                ok = np.isfinite(R) & np.isfinite(X)
                R_, X_ = R[ok], X[ok]
                gross = ann_mean(R_)
                d1 = ann_mean(X_)                                  # 单位拖累：年化后的 x 日均值 ×252
                base[(s, gname, m)] = {"d1": d1, "gross": gross, "gross_cagr": cagr(R_), "days": int(ok.sum())}
                for n in NS:
                    for c in COEFS:
                        for K in CAPITALS:
                            extra = c * np.sqrt(K / n) * X_
                            drag_rows.append({"strategy": s, "bucket": gname, "n_stocks": int(gm.sum()), "N": n, "adv_mult": m,
                                              "c": c, "K": K, "gross_ann_pct": gross * 100, "drag_ann_pp": ann_mean(extra) * 100,
                                              "net_ann_pct": ann_mean(R_ - extra) * 100,
                                              "gross_cagr_pct": cagr(R_) * 100, "net_cagr_pct": cagr(R_ - extra) * 100})
                        for rule, budget in (("drag_1pp", BUDGET_PP), ("half_of_gross", 0.5 * gross if gross > 0 else float("nan"))):
                            K_star, A_star = capacity_capital(d1, budget, c, n)
                            cap_rows.append({"strategy": s, "bucket": gname, "n_stocks": int(gm.sum()), "N": n, "adv_mult": m, "c": c,
                                             "rule": rule, "budget_pp": budget * 100, "K_star": K_star, "A_star": A_star,
                                             "gross_ann_pct": gross * 100, "unit_drag_d1": d1})
    return drag_rows, cap_rows, base


def impact_bp_rows(panel: dict, c: float = C_MAIN) -> list[dict]:
    """每笔交易的平均冲击成本（bp，占成交额）：笔数平均和成交额加权平均。N=20、ADV×1。"""
    rows = []
    for s in ORDER:
        dw = panel["dw"][s]
        mask = (dw > EPS) & panel["valid"] & np.isfinite(panel["sigma"])
        adv = np.broadcast_to(panel["adv"][None, :], dw.shape)[mask]
        for K in CAPITALS:
            part = participation(K / N_MAIN * dw[mask], adv)
            imp = impact_frac(c, panel["sigma"][mask], part) * 1e4
            rows.append({"strategy": s, "K": K, "c": c, "mean_bp": float(imp.mean()), "value_weighted_bp": float((imp * dw[mask]).sum() / dw[mask].sum()),
                         "median_bp": float(np.median(imp))})
    return rows


def selfcheck(panel: dict, strat: str = "blend2", K: float = 1e7, c: float = C_MAIN, n: int = N_MAIN) -> dict:
    """用「逐笔直接算冲击成本再按日平均」核对闭式写法 c·sqrt(K/N)·x。两者应一致到浮点误差。"""
    dw, sig, adv = panel["dw"][strat], panel["sigma"], panel["adv"]
    extra = np.zeros(dw.shape)
    for t, j in zip(*np.nonzero((dw > EPS) & panel["valid"])):
        part = (K / n) * dw[t, j] / adv[j]
        extra[t, j] = impact_frac(c, 0.0 if np.isnan(sig[t, j]) else sig[t, j], part) * dw[t, j]
    brute = daily_mean(np.where(panel["valid"], extra, np.nan))
    fact = daily_mean(c * np.sqrt(K / n) * x_matrix(panel, strat))
    return {"strategy": strat, "K": K, "c": c, "N": n, "max_abs_diff": float(np.nanmax(np.abs(brute - fact))),
            "brute_ann_drag_pp": ann_mean(brute) * 100, "closed_form_ann_drag_pp": ann_mean(fact) * 100}


# ------------------------------------------------------------------ 输出：表
def _f(v, nd=2):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "—"
    if isinstance(v, float) and np.isinf(v):
        return "∞"
    return f"{v:.{nd}f}"


def fmt_money(v) -> str:
    """元 → 人民币简写（万/亿）。"""
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "—"
    if np.isinf(v):
        return "∞"
    for unit, name in ((1e8, "亿"), (1e4, "万")):
        if v >= unit:
            x = v / unit
            return f"{x:,.0f}{name}" if x >= 100 else f"{x:.3g}{name}"      # 避免 .3g 对 ≥1000 给出科学计数法
    return f"{v:.0f}元"


def money_k(K) -> str:
    return fmt_money(float(K))


def markdown(res: dict, meta: dict) -> str:
    part = pd.DataFrame(res["participation"])
    drag = pd.DataFrame(res["drag"])
    cap = pd.DataFrame(res["capacity"])
    turn = pd.DataFrame(res["turnover"])
    L = ["# 七个正式策略的容量（C07）", "",
         f"生成时间 {meta['generated']}；窗口 {WIN_START}~{CUTOFF}；个股 {meta['n_stocks_used']} 只（固定种子 {meta['seed']}，同 C06 的抽样顺序，且要求旁表有 ADV）；"
         f"默认 N={N_MAIN} 只股票各占 K/N；ADV = 旁表（{meta['adv']['table_first']}~{meta['adv']['table_last']}，{meta['adv']['table_days']} 个交易日）共同交易日里每只股票成交额的"
         f"{'中位数' if meta['adv']['stat'] == 'median' else '均值'}；平方根冲击系数 c 是假设值。", "",
         "## 1. 换手与交易频率（每只股票一个仓位袖）", "",
         "| 策略 | 年单边换手(袖倍数) | 每股每年交易笔数 | 每笔平均 abs(Δ仓位) | 交易时 σ(日) | 总笔数 |", "|---|---|---|---|---|---|"]
    for _, r in turn.iterrows():
        L.append(f"| {r.strategy} | {_f(r.annual_oneway_turnover)} | {_f(r.trades_per_stock_year)} | {_f(r.mean_abs_dw_per_trade, 3)} | "
                 f"{_f(r.mean_sigma_at_trade, 4)} | {int(r.n_trades):,} |")
    bk = pd.DataFrame(res["buckets"])
    L += ["", "### 按流动性档位（ADV = 旁表共同交易日中位数）看股票特征", "",
          "| 档位 | 股数 | ADV 25% | ADV 中位 | ADV 75% | σ(日，全部日) | σ(日，blend2 交易日) | " + " | ".join(f"换手 {s}" for s in ORDER) + " |",
          "|---|---|---|---|---|---|---|" + "---|" * len(ORDER)]
    for _, r in bk.iterrows():
        L.append(f"| {r.bucket} | {int(r.n_stocks)} | {fmt_money(r.adv_p25)} | {fmt_money(r.adv_median)} | {fmt_money(r.adv_p75)} | "
                 f"{_f(r.sigma_daily_all_days, 4)} | {_f(r.sigma_at_blend2_trade, 4)} | " + " | ".join(_f(r[f"turnover_{s}"]) for s in ORDER) + " |")
    p = part[(part.bucket == ALL) & (part.N == N_MAIN) & (part.adv_mult == 1.0)]
    L += ["", f"## 2. 参与率超过 5% / 10% 的交易占比（全部样本股，N={N_MAIN}，ADV×1；格子 = 按笔数 / 按成交额加权）", "",
          "| 策略 | " + " | ".join(f"K={money_k(K)}" for K in CAPITALS) + " |", "|---|" + "---|" * len(CAPITALS)]
    for thr in (5, 10):
        L.append(f"| **参与率 >{thr}%** |" + " |" * len(CAPITALS))
        for s in ORDER:
            cells = []
            for K in CAPITALS:
                r = p[(p.strategy == s) & (p.K == K)].iloc[0]
                cells.append(f"{_f(r[f'share_gt{thr}'] * 100, 1)}% / {_f(r[f'value_share_gt{thr}'] * 100, 1)}%")
            L.append(f"| {s} | " + " | ".join(cells) + " |")
    L += ["", "### 参与率超过 10% 的交易占比（按笔数）：按流动性档位拆开（N=20，ADV×1）", "",
          "| 策略 | 档位(股数) | " + " | ".join(f"K={money_k(K)}" for K in CAPITALS) + " |", "|---|---|" + "---|" * len(CAPITALS)]
    for s in ORDER:
        for lab in BUCKET_LABELS:
            q = part[(part.strategy == s) & (part.bucket == lab) & (part.N == N_MAIN) & (part.adv_mult == 1.0)]
            if not len(q):
                continue
            L.append(f"| {s} | {lab}({int(q.n_stocks.iloc[0])}) | " + " | ".join(
                f"{_f(q[q.K == K].share_gt10.iloc[0] * 100, 1)}%" for K in CAPITALS) + " |")
    L += ["", "### 敏感性：参与率 >10% 占比（按笔数，全部样本股）随 N 和 ADV 倍数变化，只列 blend2", "",
          "| N | ADV倍数 | " + " | ".join(f"K={money_k(K)}" for K in CAPITALS) + " |", "|---|---|" + "---|" * len(CAPITALS)]
    for n in NS:
        for m in ADV_MULTS:
            q = part[(part.strategy == "blend2") & (part.bucket == ALL) & (part.N == n) & (part.adv_mult == m)]
            L.append(f"| {n} | ×{m} | " + " | ".join(f"{_f(q[q.K == K].share_gt10.iloc[0] * 100, 1)}%" for K in CAPITALS) + " |")

    d = drag[(drag.bucket == ALL) & (drag.N == N_MAIN) & (drag.adv_mult == 1.0)]
    L += ["", f"## 3. 平方根冲击成本对年化超额收益的拖累（全部样本股，N={N_MAIN}，ADV×1）", "",
          "年化 = 日超额收益（相对现金 ETF）等权组合的日均值 ×252；已含回测原有的 8bp 单边成本；拖累 = 额外冲击成本的年化。", "",
          "| 策略 | 毛年化超额% | c | " + " | ".join(f"K={money_k(K)} 拖累pp / 净%" for K in CAPITALS) + " |", "|---|---|---|" + "---|" * len(CAPITALS)]
    for s in ORDER:
        for c in COEFS:
            q = d[(d.strategy == s) & (d.c == c)]
            L.append(f"| {s} | {_f(q.gross_ann_pct.iloc[0])} | {c} | " + " | ".join(
                f"{_f(q[q.K == K].drag_ann_pp.iloc[0])} / {_f(q[q.K == K].net_ann_pct.iloc[0])}" for K in CAPITALS) + " |")

    cp = cap[(cap.bucket == ALL) & (cap.N == N_MAIN) & (cap.adv_mult == 1.0)]
    L += ["", f"## 4. 容量 K*（N={N_MAIN}，ADV×1，全部样本股）", "",
          "规则 (a)：年化拖累 = 1 个百分点；规则 (b)：年化拖累 = 毛超额年化收益的一半（毛收益 ≤ 0 → 无定义）。K* ∝ N，每只股票占 K*/N。", "",
          "| 策略 | 毛年化超额% | " + " | ".join(f"(a) c={c}" for c in COEFS) + " | " + " | ".join(f"(b) c={c}" for c in COEFS) + " |",
          "|---|---|" + "---|" * (2 * len(COEFS))]
    for s in ORDER:
        q = cp[cp.strategy == s]
        a = [fmt_money(q[(q.rule == "drag_1pp") & (q.c == c)].K_star.iloc[0]) for c in COEFS]
        b = [fmt_money(q[(q.rule == "half_of_gross") & (q.c == c)].K_star.iloc[0]) for c in COEFS]
        L.append(f"| {s} | {_f(q.gross_ann_pct.iloc[0])} | " + " | ".join(a) + " | " + " | ".join(b) + " |")
    L += ["", "### 容量 K* 按流动性档位拆开：规则 (a)，c=0.5，N=20，ADV×1", "",
          "| 策略 | " + " | ".join(BUCKET_LABELS) + " |", "|---|" + "---|" * len(BUCKET_LABELS)]
    for s in ORDER:
        cells = []
        for lab in BUCKET_LABELS:
            q = cap[(cap.strategy == s) & (cap.bucket == lab) & (cap.N == N_MAIN) & (cap.adv_mult == 1.0) & (cap.c == C_MAIN) & (cap.rule == "drag_1pp")]
            cells.append(fmt_money(q.K_star.iloc[0]) if len(q) else "—")
        L.append(f"| {s} | " + " | ".join(cells) + " |")
    L += ["", "### 容量敏感性：规则 (a)，c=0.5，blend2，全部样本股", "",
          "| N | ADV倍数 | K* |", "|---|---|---|"]
    for n in NS:
        for m in ADV_MULTS:
            q = cap[(cap.strategy == "blend2") & (cap.bucket == ALL) & (cap.N == n) & (cap.adv_mult == m) & (cap.c == C_MAIN) & (cap.rule == "drag_1pp")]
            L.append(f"| {n} | ×{m} | {fmt_money(q.K_star.iloc[0])} |")
    L += ["", "## 5. 每笔交易平均冲击成本（bp，占成交额；c=0.5，N=20，ADV×1）", "",
          "| 策略 | " + " | ".join(f"K={money_k(K)} 笔均/加权" for K in CAPITALS) + " |", "|---|" + "---|" * len(CAPITALS)]
    ib = pd.DataFrame(res["impact_bp"])
    for s in ORDER:
        q = ib[ib.strategy == s]
        L.append(f"| {s} | " + " | ".join(f"{_f(q[q.K == K].mean_bp.iloc[0], 1)} / {_f(q[q.K == K].value_weighted_bp.iloc[0], 1)}" for K in CAPITALS) + " |")
    sc = res["selfcheck"]
    L += ["", f"自检：逐笔直接计算与闭式写法（blend2，K={money_k(sc['K'])}，c={sc['c']}，N={sc['N']}）日拖累最大差 {sc['max_abs_diff']:.2e}，"
          f"年化拖累 {sc['brute_ann_drag_pp']:.4f}pp vs {sc['closed_form_ann_drag_pp']:.4f}pp。", ""]
    return "\n".join(L)


# ------------------------------------------------------------------ 输出：图
COLORS = {"blend2": "#0072B2", "trend_ens": "#E69F00", "rsi2": "#009E73", "donchian_s1": "#CC79A7",
          "trend_vol": "#D55E00", "ma_adx_original": "#56B4E9", "ma_adx_plus": "#444444"}


def make_figures(res: dict, panel: dict, base: dict, out: Path) -> list[str]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams["font.sans-serif"] = ["PingFang SC", "Heiti SC", "Arial Unicode MS", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    files = []
    Ks = np.logspace(5, 10, 60)
    part = pd.DataFrame(res["participation"])

    # 1. 参与率 >10% 的占比随资金变化（按笔数）：直接对 K 网格重算
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.4), constrained_layout=True, sharey=True)
    for ax, thr in zip(axes, (0.05, 0.10)):
        for s in ORDER:
            ys = [participation_stats(panel["dw"][s], panel["adv"], K / N_MAIN, (thr,))[f"share_gt{int(thr * 100)}"] * 100 for K in Ks]
            ax.plot(Ks, ys, color=COLORS[s], label=s, lw=1.8)
        for K in CAPITALS:
            ax.axvline(K, color="#bbb", lw=0.8, ls=":")
        ax.set_xscale("log")
        ax.set_xlabel(f"总资金 K（元），N={N_MAIN}")
        ax.set_title(f"参与率 > {int(thr * 100)}% 的交易占比（按笔数）")
        ax.grid(alpha=0.25)
    axes[0].set_ylabel("%")
    axes[0].legend(frameon=False, fontsize=8)
    fig.savefig(out / "fig_participation.png", dpi=140)
    plt.close(fig)
    files.append("fig_participation.png")

    # 2. 年化拖累随资金变化：三个 c 一张小图，7 条线；参考线 1pp
    fig, axes = plt.subplots(1, 3, figsize=(14, 4.4), constrained_layout=True, sharey=True)
    for ax, c in zip(axes, COEFS):
        for s in ORDER:
            d1 = base[(s, ALL, 1.0)]["d1"]
            ax.plot(Ks, 100 * c * np.sqrt(Ks / N_MAIN) * d1, color=COLORS[s], label=s, lw=1.8)
        ax.axhline(100 * BUDGET_PP, color="#555", ls="--", lw=1)
        ax.text(Ks[0], 100 * BUDGET_PP, " 1pp", va="bottom", fontsize=8, color="#555")
        ax.set_xscale("log")
        ax.set_yscale("log")
        ax.set_xlabel(f"总资金 K（元），N={N_MAIN}")
        ax.set_title(f"冲击系数 c = {c}")
        ax.grid(alpha=0.25)
    axes[0].set_ylabel("年化冲击拖累（百分点）")
    axes[0].legend(frameon=False, fontsize=8)
    fig.suptitle("平方根冲击对年化超额收益的拖累（对数坐标；斜率 1/2 = 拖累与 sqrt(K) 成正比）")
    fig.savefig(out / "fig_drag.png", dpi=140)
    plt.close(fig)
    files.append("fig_drag.png")

    # 3. 容量条形图：规则 (a)，三个 c
    cap = pd.DataFrame(res["capacity"])
    cp = cap[(cap.bucket == ALL) & (cap.N == N_MAIN) & (cap.adv_mult == 1.0) & (cap.rule == "drag_1pp")]
    fig, ax = plt.subplots(figsize=(10, 4.6), constrained_layout=True)
    width = 0.26
    shades = {0.1: 0.45, 0.5: 0.75, 1.0: 1.0}
    for i, c in enumerate(COEFS):
        vals = [cp[(cp.strategy == s) & (cp.c == c)].K_star.iloc[0] for s in ORDER]
        ax.bar(np.arange(len(ORDER)) + (i - 1) * width, vals, width, color=[COLORS[s] for s in ORDER], alpha=shades[c], label=f"c = {c}")
    ax.set_yscale("log")
    ax.set_xticks(range(len(ORDER)), ORDER, rotation=30, ha="right")
    ax.set_ylabel("容量 K*（元，年化拖累 = 1pp）")
    ax.set_title(f"各策略容量（N={N_MAIN}，ADV×1；每组 3 柱 = c 由小到大，越淡 c 越小）")
    ax.grid(alpha=0.25, axis="y")
    fig.savefig(out / "fig_capacity.png", dpi=140)
    plt.close(fig)
    files.append("fig_capacity.png")

    # 4. 按流动性档位：K=1000 万时参与率 >10% 的占比
    q = part[(part.N == N_MAIN) & (part.adv_mult == 1.0) & (part.K == 1e9) & (part.bucket != ALL)]
    fig, ax = plt.subplots(figsize=(10, 4.4), constrained_layout=True)
    w = 0.8 / len(ORDER)
    for i, s in enumerate(ORDER):
        ys = [q[(q.strategy == s) & (q.bucket == lab)].share_gt10.iloc[0] * 100 if len(q[(q.strategy == s) & (q.bucket == lab)]) else np.nan
              for lab in BUCKET_LABELS]
        ax.bar(np.arange(len(BUCKET_LABELS)) + (i - 3) * w, ys, w, color=COLORS[s], label=s)
    ax.set_xticks(range(len(BUCKET_LABELS)), BUCKET_LABELS)
    ax.set_xlabel("该股 ADV 档位（旁表中位数成交额）")
    ax.set_ylabel("参与率 > 10% 的交易占比 %")
    ax.set_title(f"K = 10亿（N={N_MAIN}，每只 5000万）：ADV 越小的档位，越多交易超过 10% 参与率")
    ax.legend(frameon=False, fontsize=8, ncol=2)
    ax.grid(alpha=0.25, axis="y")
    fig.savefig(out / "fig_bucket.png", dpi=140)
    plt.close(fig)
    files.append("fig_bucket.png")
    return files


# ------------------------------------------------------------------ 主流程
def run(data_dir: Path, out_dir: Path, n_stocks: int, seed: int, workers: int = 1, log=print) -> dict:
    check_end(CUTOFF)
    t0 = time.time()
    daily = find_daily_dir(data_dir.resolve())
    log(f"行情目录 {daily}")
    adv, adv_info = load_adv(daily)
    log(f"ADV：旁表 {adv_info['table_first']}~{adv_info['table_last']}，{adv_info['codes_with_adv']} 只有 ADV")
    results, tried = collect_stocks(daily, n_stocks, seed, adv, workers, log)
    if not results:
        raise RuntimeError("没有合格的股票")
    log(f"个股回测完成：{len(results)} 只 ({time.time() - t0:.0f}s)")
    panel = build_panel(results, adv)
    check_end(str(panel["dates"][-1].date()))

    res = {"turnover": turnover_rows(panel), "buckets": bucket_rows(panel), "participation": participation_rows(panel)}
    res["drag"], res["capacity"], base = drag_tables(panel)
    res["impact_bp"] = impact_bp_rows(panel)
    res["selfcheck"] = selfcheck(panel)
    log(f"表格完成 ({time.time() - t0:.0f}s)；自检最大差 {res['selfcheck']['max_abs_diff']:.2e}")

    out_dir.mkdir(parents=True, exist_ok=True)
    for key, name in (("turnover", "capacity_turnover.csv"), ("buckets", "capacity_buckets.csv"), ("participation", "capacity_participation.csv"), ("drag", "capacity_drag.csv"),
                      ("capacity", "capacity_capacity.csv"), ("impact_bp", "capacity_impact_bp.csv")):
        pd.DataFrame(res[key]).to_csv(out_dir / name, index=False)
    b = panel["bucket"]
    pd.DataFrame({"code": panel["codes"], "adv_yuan": panel["adv"], "bucket": [BUCKET_LABELS[k] for k in b]}).to_csv(out_dir / "capacity_stock_sample.csv", index=False)
    meta = {"generated": time.strftime("%Y-%m-%d %H:%M:%S"), "cutoff": CUTOFF, "window": [WIN_START, CUTOFF], "backtest_last_date": str(panel["dates"][-1].date()),
            "warm": WARM, "band": BAND, "stock_cost": STOCK_COST, "n_stocks_requested": n_stocks, "n_stocks_used": len(results), "candidates_seen": tried,
            "seed": seed, "sigma_window": SIGMA_WINDOW, "capitals": CAPITALS, "Ns": NS, "N_main": N_MAIN, "adv_mults": ADV_MULTS, "coefs": COEFS,
            "bucket_edges": [None if np.isinf(e) else e for e in BUCKET_EDGES], "bucket_labels": BUCKET_LABELS,
            "bucket_counts": {lab: int((b == k).sum()) for k, lab in enumerate(BUCKET_LABELS)}, "adv": adv_info,
            "adv_note": "旁表日期全在 CUTOFF 之后；只用 turnover_yuan 的各股中位数当静态 ADV，不用其价格列；见脚本说明",
            "selfcheck": res["selfcheck"], "daily_dir": str(daily), "versions": {"pandas": pd.__version__, "numpy": np.__version__}}
    meta["figures"] = make_figures(res, panel, base, out_dir)
    (out_dir / "capacity_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
    (out_dir / "capacity_tables.md").write_text(markdown(res, meta), encoding="utf-8")
    log(f"写出 {out_dir}（{time.time() - t0:.0f}s）")
    return {"res": res, "panel": panel, "meta": meta, "base": base}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out-dir", required=True, type=Path)
    ap.add_argument("--data-dir", type=Path, default=ROOT / "data")
    ap.add_argument("--n-stocks", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=6)
    ap.add_argument("--workers", type=int, default=1)
    a = ap.parse_args(argv)
    a.out_dir.mkdir(parents=True, exist_ok=True)

    def log(msg):                       # 同时打印并追加到输出目录里的 run.log
        print(msg, flush=True)
        with (a.out_dir / "run.log").open("a", encoding="utf-8") as f:
            f.write(msg + "\n")

    run(a.data_dir, a.out_dir, a.n_stocks, a.seed, a.workers, log=log)


if __name__ == "__main__":
    main()
