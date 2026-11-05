"""C06 七个正式策略的信号/持仓重合度：blend2 的分散化是不是真的。

问题：blend2 = 趋势集成 + RSI2 + 海龟 各 1/3 仓位。如果这三个成分的信号几乎同进同出、日收益高度相关，
  那「三个策略互补」的分散化主要是心理安慰——名义上 3 个策略，实际上只有 1 个多一点的独立赌注。
做法（全程复用项目已有函数，不改任何策略规则）：
  1. 行情读进来立刻截到 CUTOFF（2026-03-31），之后才算信号：registry.target_weights（每日目标仓位，收盘后定、次日成交）
     → portfolio.run_weights（次日开盘成交、扣费、10% 调仓阈值、空仓现金计息、A 股一字板），口径同 registry.run / scan.py。
  2. 研究窗口 2020-01-01~2026-03-31，每个品种前 260 根作预热不进窗口（与 registry.run 相同）。
     日收益用「超额收益」= 策略 − 现金 ETF（美股 SHY、A 股 511880），和 C04 一致。
  3. 三组品种：cn_etf（5 个 A 股 ETF）、us_etf（7 个美股 ETF）、cn_stock（固定种子随机抽的 A 股个股）。
  4. 重合度：
       持仓 Jaccard = |两个策略都持有的 (品种,日) 格子| / |至少一个持有的格子|，「持有」= 目标仓位 > 0；
         另给加权 Jaccard（Σmin(仓位)/Σmax(仓位)，连续仓位下更准）、逐品种平均、逐日截面平均、
         同时持有比例在「各自独立」假设下的期望（独立基线）以及两者之比（lift）。
       日收益相关：逐品种相关矩阵取平均（headline）+ 组合层（各品种等权平均后再算）+ 去掉大盘（买入持有）后的残差相关。
  5. 有效独立策略数（effective N，输入是相关矩阵 C 的特征值 λ，Σλ = N）：
       PR   = (Σλ)² / Σλ²            （参与比，Participation Ratio；全不相关 = N，全相关 = 1）
       熵   = exp(−Σ p ln p)，p = λ/Σλ （Meucci 2009 的有效赌注数）
       等权 = 1 / (w'Cw)，w = 1/N     （等权组合的方差相当于几个独立策略平分风险）
     对象：blend2 的 3 个成分、正式 7 个策略、blend2+3 成分（4×4，blend2 是成分的线性组合所以天然冗余）。
  6. 不确定性：组合层日收益做循环区块自助法（block=20）给 95% 区间；按年份看稳定性；逐品种的分布。

只读 data/ 下的行情，不写 data/；不联网。

用法：
  .venv/bin/python -B scripts/strategy_overlap.py --out-dir DIR [--data-dir data] [--n-stocks 1000] [--seed 6] [--boot 1000]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
from itertools import combinations
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from config import MARKETS  # noqa: E402
from src import registry  # noqa: E402
from src.datasource import limit_pct  # noqa: E402
from src.metrics import TRADING_DAYS  # noqa: E402
from src.portfolio import run_weights  # noqa: E402
from src.scan import COST, STRATS  # noqa: E402  7 个正式策略，和全市场扫描、信号账本同一份清单
from deflated_sharpe_table import check_end, find_daily_dir, load_prices  # noqa: E402  C04 里的读行情/截止日检查

CUTOFF = "2026-03-31"                       # 队列红线：任何研究的结束日期不得晚于此
WIN_START = "2020-01-01"
WARM = 260                                  # 预热期根数，与 registry.run 相同
BAND = 0.10                                 # 调仓阈值，与 registry.run / scan.py 相同
STOCK_COST = COST                           # 个股单边 8bp，直接用 scan.COST（数值在 src/costs.py，单一来源）
ORDER = ["blend2", "trend_ens", "rsi2", "donchian_s1", "trend_vol", "ma_adx_original", "ma_adx_plus"]
COMPONENTS = ("trend_ens", "rsi2", "donchian_s1")   # blend2 的三个成分（registry.target_weights 里 blend2 的定义）
BH = "buy_hold"                             # 去大盘用的参照，不进 7 个正式策略
HELD_EPS = 1e-9                             # 持有 = 目标仓位 > 0
HELD_TH_SENS = 0.10                         # 敏感性：只把目标仓位 > 10% 算持有（交易清单的 thr）
GROUPS = ["cn_etf", "us_etf", "cn_stock"]
BLOCK = 20                                  # 自助法区块长度（约一个交易月）
assert sorted(ORDER) == sorted(STRATS)


# ------------------------------------------------------------------ 公式（纯函数，测试里单独验）
def jaccard_pooled(a: np.ndarray, b: np.ndarray) -> float:
    """布尔数组 a、b 的 Jaccard：交 / 并；并为空返回 nan。"""
    uni = (a | b).sum()
    return float((a & b).sum() / uni) if uni else float("nan")


def jaccard_weighted(x: np.ndarray, y: np.ndarray) -> float:
    """加权 Jaccard（Ruzicka）：Σmin / Σmax。0/1 仓位时等于普通 Jaccard。"""
    den = np.maximum(x, y).sum()
    return float(np.minimum(x, y).sum() / den) if den > 0 else float("nan")


def jaccard_independent(pa: float, pb: float) -> float:
    """两个持有概率分别为 pa、pb 且互相独立时，Jaccard 的期望（pa·pb / (pa+pb−pa·pb)）。"""
    both = pa * pb
    den = pa + pb - both
    return both / den if den > 0 else float("nan")


def effective_n(corr: np.ndarray) -> dict:
    """相关矩阵的特征值 → 有效独立策略数。corr 是 N×N 相关矩阵（对角线 1）。"""
    corr = np.asarray(corr, float)
    if np.isnan(corr).any():          # 某策略整段没持仓 → 相关没有定义，不硬算
        return {"pr": float("nan"), "entropy": float("nan"), "top_share": float("nan")}
    lam = np.clip(np.linalg.eigvalsh(corr), 0.0, None)
    s = lam.sum()
    p = lam / s
    p = p[p > 0]
    return {"pr": float(s ** 2 / (lam ** 2).sum()), "entropy": float(math.exp(-(p * np.log(p)).sum())),
            "top_share": float(lam.max() / s)}


def equal_weight_n(corr: np.ndarray) -> float:
    """等权组合相当于几个独立策略：1 / (w'Cw)，w = 1/N。全不相关 = N，全相关 = 1。"""
    n = len(corr)
    w = np.full(n, 1.0 / n)
    return float(1.0 / (w @ np.asarray(corr, float) @ w))


def mean_offdiag(corr: np.ndarray) -> float:
    n = len(corr)
    return float((corr.sum() - np.trace(corr)) / (n * (n - 1)))


def corr_matrix(R: np.ndarray) -> np.ndarray:
    """T×K 日收益 → K×K 相关矩阵；方差为 0 的那几列（整段没持仓）对应的行和列是 nan（相关没有定义），其余照常算。"""
    K = R.shape[1]
    out = np.full((K, K), np.nan)
    ok = (R.std(axis=0) > 0) if len(R) >= 3 else np.zeros(K, bool)
    if ok.any():
        out[np.ix_(ok, ok)] = np.corrcoef(R[:, ok], rowvar=False).reshape(ok.sum(), ok.sum())
    return out


def residual_on(R: np.ndarray, m: np.ndarray) -> np.ndarray:
    """每一列对 m 做带截距的 OLS，返回残差（去掉「跟着大盘走」的那部分）。"""
    X = np.column_stack([np.ones(len(m)), m])
    beta, *_ = np.linalg.lstsq(X, R, rcond=None)
    return R - X @ beta


def summary_stats(R: np.ndarray) -> dict:
    """R：T×7，列顺序 = ORDER。返回 headline 统计量（自助法里对每个重抽样本重复算）。"""
    C = corr_matrix(R)
    c3 = [ORDER.index(s) for s in COMPONENTS]
    C3 = C[np.ix_(c3, c3)]
    sd = R.std(axis=0, ddof=1)
    blend = R[:, 0]
    out = {"mean_corr_7": mean_offdiag(C), "neff_pr_7": effective_n(C)["pr"],
           "neff_ent_7": effective_n(C)["entropy"], "neff_eq_7": equal_weight_n(C),
           "mean_corr_3": mean_offdiag(C3), "neff_pr_3": effective_n(C3)["pr"],
           "neff_ent_3": effective_n(C3)["entropy"], "neff_eq_3": equal_weight_n(C3),
           "neff_pr_4": effective_n(C[np.ix_([0, *c3], [0, *c3])])["pr"],
           "vol_ratio": float(sd[0] / sd[c3].mean())}
    out["blend_vs_avg_corr"] = float(np.corrcoef(blend, R[:, c3].mean(axis=1))[0, 1])
    return out


def block_bootstrap(R: np.ndarray, reps: int, seed: int, block: int = BLOCK) -> pd.DataFrame:
    """循环区块自助法：保留日收益的短期序列相关，重抽整块。返回每次抽样的 summary_stats。"""
    rng = np.random.default_rng(seed)
    T = len(R)
    nb = int(np.ceil(T / block))
    rows = []
    for _ in range(reps):
        starts = rng.integers(0, T, size=nb)
        idx = ((starts[:, None] + np.arange(block)[None, :]) % T).reshape(-1)[:T]
        rows.append(summary_stats(R[idx]))
    return pd.DataFrame(rows)


# ------------------------------------------------------------------ 单品种：目标仓位 + 日超额收益
def instrument_panel(df: pd.DataFrame, symbol: str, mcfg: dict, cash: pd.Series | None,
                     cost: float, limit: float | None) -> dict:
    """一个品种：7 个正式策略 + 买入持有参照的 (目标仓位, 日超额收益)，截到研究窗口。
    df 必须已经截到 CUTOFF；窗口起点 = max(WIN_START, 第 WARM 根)。"""
    check_end(str(df.index[-1].date()))
    prices = {symbol: df}
    lim = {symbol: limit} if limit else None
    w, r = {}, {}
    for name in [*ORDER, BH]:
        if name == BH:
            W = pd.Series(1.0, index=df.index, name=symbol).to_frame()
        else:
            W = registry.target_weights(name, prices, symbol, registry.clean_params(name, {}), mcfg)
        res = run_weights(prices, W, cost=cost, band=0.0 if name == BH else BAND, cash_ret=cash, limits=lim)
        ret = res.returns
        rf = cash.reindex(ret.index).fillna(0.0) if cash is not None else 0.0
        w[name], r[name] = W.iloc[:, 0], ret - rf
    ret_idx = r[ORDER[0]].index
    start = max(pd.Timestamp(WIN_START), ret_idx[min(WARM, len(ret_idx) - 1)])
    sl = slice(start, pd.Timestamp(CUTOFF))
    return {"w": {k: v.reindex(ret_idx).loc[sl].fillna(0.0) for k, v in w.items()},
            "ret": {k: v.loc[sl] for k, v in r.items()}}


def assemble(per: dict) -> dict:
    """多个品种 → 日期×品种 的数组。valid = 该品种那天在窗口内有行情；无效格子仓位记 0、收益记 nan。"""
    syms = list(per)
    dates = per[syms[0]]["ret"][ORDER[0]].index
    for s in syms[1:]:
        dates = dates.union(per[s]["ret"][ORDER[0]].index)
    names = [*ORDER, BH]
    # 每个品种先各自 reindex 到总日历再拼（先拼再 reindex 的话，品种缺的日子是 NaN，to_numpy(bool) 会把 NaN 变成 True）
    valid = pd.DataFrame({s: pd.Series(True, index=per[s]["ret"][ORDER[0]].index).reindex(dates, fill_value=False)
                          for s in syms}).to_numpy(bool)
    wt = {n: pd.DataFrame({s: per[s]["w"][n] for s in syms}).reindex(dates).fillna(0.0).to_numpy() for n in names}
    rt = {n: pd.DataFrame({s: per[s]["ret"][n] for s in syms}).reindex(dates).to_numpy() for n in names}
    return {"syms": syms, "dates": dates, "valid": valid, "wt": wt, "ret": rt}


# ------------------------------------------------------------------ 一组品种的全部统计
def pair_matrix(fn) -> pd.DataFrame:
    M = np.eye(len(ORDER))
    for i, j in combinations(range(len(ORDER)), 2):
        M[i, j] = M[j, i] = fn(ORDER[i], ORDER[j])
    return pd.DataFrame(M, index=ORDER, columns=ORDER)


def overlap_tables(g: dict) -> dict[str, pd.DataFrame]:
    """各种重合度矩阵。held 只在 valid 格子里统计（wt 在无效格子本来就是 0）。"""
    wt, valid = g["wt"], g["valid"]
    held = {n: wt[n] > HELD_EPS for n in ORDER}
    held10 = {n: wt[n] > HELD_TH_SENS for n in ORDER}
    nv = valid.sum()
    p = {n: held[n].sum() / nv for n in ORDER}

    def per_inst(a, b):          # 逐品种 Jaccard 取平均（并为空的品种不计）
        uni = (a | b).sum(axis=0)
        ok = uni > 0
        return float(((a & b).sum(axis=0)[ok] / uni[ok]).mean())

    def per_day(a, b):           # 逐日截面 Jaccard 取平均（并为空的日子不计）
        uni = (a | b).sum(axis=1)
        ok = uni > 0
        return float(((a & b).sum(axis=1)[ok] / uni[ok]).mean())

    out = {
        "jaccard": pair_matrix(lambda x, y: jaccard_pooled(held[x], held[y])),
        "jaccard_th10": pair_matrix(lambda x, y: jaccard_pooled(held10[x], held10[y])),
        "jaccard_weighted": pair_matrix(lambda x, y: jaccard_weighted(wt[x], wt[y])),
        "jaccard_per_instrument": pair_matrix(lambda x, y: per_inst(held[x], held[y])),
        "jaccard_per_day": pair_matrix(lambda x, y: per_day(held[x], held[y])),
        "jaccard_independent": pair_matrix(lambda x, y: jaccard_independent(p[x], p[y])),
        "agree": pair_matrix(lambda x, y: float((((held[x] == held[y]) & valid).sum()) / nv)),
    }
    out["jaccard_lift"] = out["jaccard"] / out["jaccard_independent"]
    return out


def correlation_tables(g: dict) -> dict:
    """三种相关矩阵 + 逐品种的 effective N 分布。"""
    rt, valid = g["ret"], g["valid"]
    K = len(g["syms"])
    stack = np.stack([rt[n] for n in ORDER], axis=-1)           # 日期×品种×7
    # 逐品种相关
    per = []
    for k in range(K):
        X = stack[valid[:, k], k, :]
        per.append(corr_matrix(X))
    per = np.array(per)
    with np.errstate(all="ignore"):
        inst_mean = np.nanmean(per, axis=0)
    c3 = [ORDER.index(s) for s in COMPONENTS]
    inst_neff = []
    for C in per:
        C3 = C[np.ix_(c3, c3)]      # 3 成分有定义不要求另外 4 个策略也有仓位，所以 3 和 7 各自判断
        inst_neff.append({"neff_pr_7": effective_n(C)["pr"], "neff_pr_3": effective_n(C3)["pr"],
                          "mean_corr_3": mean_offdiag(C3), "mean_corr_7": mean_offdiag(C)})
    # 组合层：每天对有效品种等权平均
    with np.errstate(all="ignore"):
        port = np.nanmean(stack, axis=1)                         # 日期×7
        mkt = np.nanmean(rt[BH], axis=1)                         # 日期
    ok = np.isfinite(port).all(axis=1) & np.isfinite(mkt)
    port, mkt = port[ok], mkt[ok]
    dates = g["dates"][ok]
    resid = residual_on(port, mkt)
    return {"inst_mean": pd.DataFrame(inst_mean, index=ORDER, columns=ORDER),
            "portfolio": pd.DataFrame(corr_matrix(port), index=ORDER, columns=ORDER),
            "residual": pd.DataFrame(corr_matrix(resid), index=ORDER, columns=ORDER),
            "inst_neff": pd.DataFrame(inst_neff),
            "port_ret": pd.DataFrame(port, index=dates, columns=ORDER), "mkt": pd.Series(mkt, index=dates)}


def neff_rows(group: str, corr: dict) -> list[dict]:
    """各相关矩阵 × 各对象（3 成分 / 7 策略 / blend2+3 成分）的 effective N。"""
    c3 = [ORDER.index(s) for s in COMPONENTS]
    c4 = [0, *c3]
    rows = []
    for kind in ("inst_mean", "portfolio", "residual"):
        C = corr[kind].to_numpy()
        for obj, ix in (("components3", c3), ("blend2+3", c4), ("all7", list(range(len(ORDER))))):
            sub = C[np.ix_(ix, ix)]
            e = effective_n(sub)
            rows.append({"group": group, "corr_kind": kind, "object": obj, "N": len(ix),
                         "mean_pairwise_corr": mean_offdiag(sub), "neff_pr": e["pr"], "neff_entropy": e["entropy"],
                         "neff_equal_weight": equal_weight_n(sub), "top_eig_share": e["top_share"]})
    return rows


def blend_rows(group: str, g: dict, ov: dict, corr: dict) -> list[dict]:
    """blend2 对每个成分：Jaccard / 加权 Jaccard / 相关；再加成分一致性和波动比。"""
    rows = []
    for s in COMPONENTS:
        rows.append({"group": group, "component": s,
                     "jaccard": ov["jaccard"].loc["blend2", s], "jaccard_weighted": ov["jaccard_weighted"].loc["blend2", s],
                     "corr_inst_mean": corr["inst_mean"].loc["blend2", s], "corr_portfolio": corr["portfolio"].loc["blend2", s],
                     "corr_residual": corr["residual"].loc["blend2", s]})
    return rows


def blend_agreement(g: dict) -> dict:
    """blend2 有仓位的格子里，3 个成分同时持有几个？与「各自独立」时的期望比较。"""
    held = {n: g["wt"][n] > HELD_EPS for n in COMPONENTS}
    k = sum(h.astype(int) for h in held.values())
    inb = k > 0                                                  # blend2 > 0 ⇔ 至少一个成分 > 0（权重非负）
    obs = {j: float((k[inb] == j).mean()) for j in (1, 2, 3)}
    nv = g["valid"].sum()
    ps = [held[n].sum() / nv for n in COMPONENTS]
    # 独立假设：P(恰好 j 个持有)，再按「至少 1 个」归一化
    exp = {1: 0.0, 2: 0.0, 3: 0.0}
    for mask in range(1, 8):
        pr = np.prod([ps[i] if mask >> i & 1 else 1 - ps[i] for i in range(3)])
        exp[bin(mask).count("1")] += pr
    tot = sum(exp.values())
    exp = {j: v / tot for j, v in exp.items()}
    w = g["wt"]["blend2"][g["wt"]["blend2"] > HELD_EPS]
    return {"k_obs": obs, "k_indep": exp, "hold_rate": {n: float(p) for n, p in zip(COMPONENTS, ps)},
            "blend2_hold_rate": float(inb.sum() / nv), "blend2_mean_weight_when_held": float(w.mean()),
            "blend2_full_weight_share": float((w > 1 - 1e-6).mean())}


def yearly_rows(group: str, port: pd.DataFrame) -> list[dict]:
    rows = []
    for y, X in port.groupby(port.index.year):
        if len(X) < 40:
            continue
        s = summary_stats(X.to_numpy())
        rows.append({"group": group, "year": int(y), "days": len(X), **{k: s[k] for k in
                     ("mean_corr_3", "neff_pr_3", "neff_eq_3", "mean_corr_7", "neff_pr_7", "vol_ratio")}})
    return rows


def vol_rows(group: str, g: dict, port: pd.DataFrame) -> list[dict]:
    wt, valid = g["wt"], g["valid"]
    return [{"group": group, "strategy": n, "hold_rate": float((wt[n] > HELD_EPS).sum() / valid.sum()),
             "mean_weight": float(wt[n].sum() / valid.sum()),
             "port_ann_vol_pct": float(port[n].std(ddof=1) * math.sqrt(TRADING_DAYS) * 100)} for n in ORDER]


def analyze_group(group: str, per: dict, boot: int, seed: int, log=print) -> dict:
    g = assemble(per)
    ov = overlap_tables(g)
    corr = correlation_tables(g)
    port = corr["port_ret"]
    res = {"group": group, "n_inst": len(g["syms"]), "n_days": int(len(port)),
           "start": str(port.index[0].date()), "end": str(port.index[-1].date()),
           "valid_cells": int(g["valid"].sum()), "overlap": ov, "corr": corr,
           "neff": neff_rows(group, corr), "blend": blend_rows(group, g, ov, corr),
           "agree": blend_agreement(g), "yearly": yearly_rows(group, port), "vol": vol_rows(group, g, port),
           "point": summary_stats(port.to_numpy())}
    bs = block_bootstrap(port.to_numpy(), boot, seed)
    res["boot"] = {k: [float(bs[k].quantile(0.025)), float(bs[k].quantile(0.975))] for k in bs.columns}
    log(f"  {group}: {res['n_inst']} 品种, {res['n_days']} 日, 3 成分 N_eff(PR)={res['point']['neff_pr_3']:.2f} "
        f"95%CI [{res['boot']['neff_pr_3'][0]:.2f}, {res['boot']['neff_pr_3'][1]:.2f}]")
    return res


# ------------------------------------------------------------------ 数据
def etf_group(daily: Path, market: str, digests: dict) -> dict:
    mcfg = MARKETS[market]
    prices, dg = load_prices(daily, market, mcfg["universe"])
    digests |= dg
    cash = prices[mcfg["cash"]]["Close"].pct_change() if mcfg["cash"] in prices else None
    cost = mcfg["cost_bp"] / 1e4
    per = {}
    for sym in mcfg["universe"]:
        if sym == mcfg["cash"] or sym not in prices:
            continue
        per[sym] = instrument_panel(prices[sym], sym, mcfg, cash, cost, limit_pct(sym) if market == "cn" else None)
    return per


def sample_stocks(daily: Path, n: int, seed: int, digests: dict) -> tuple[dict, pd.DataFrame]:
    """从 cn_all 里按固定种子打乱后依次取，直到凑够 n 只合格的。合格 = 非 ST/退、窗口开始前至少有 WARM+260 根历史、
    2018 年底前已上市、行情到 CUTOFF 附近（排除长期停牌/已退市）。只含现存股票 → 有幸存者偏差（报告里写明）。"""
    lst = pd.read_csv(daily / "cn_all_list.csv", dtype=str).fillna("")
    lst = lst[~lst["name"].str.upper().str.contains("ST") & ~lst["name"].str.contains("退")]
    codes = sorted(lst["code"])
    order = np.random.default_rng(seed).permutation(len(codes))
    names = dict(zip(lst["code"], lst["name"]))
    cash_df = pd.read_csv(daily / "cn" / f"{MARKETS['cn']['cash']}.csv", index_col=0, parse_dates=True).sort_index().loc[:CUTOFF]
    cash = cash_df["Close"].pct_change()
    per, rows, tried = {}, [], 0
    for i in order:
        if len(per) >= n:
            break
        code = codes[i]
        p = daily / "cn_all" / f"{code}.parquet"
        if not p.exists():
            continue
        tried += 1
        df = pd.read_parquet(p).sort_index().loc[:CUTOFF]
        if len(df) < WARM + 260 or df.index[0] > pd.Timestamp("2018-12-31") or df.index[-1] < pd.Timestamp(CUTOFF) - pd.Timedelta(days=10):
            continue
        digests[f"cn_all/{code}.parquet"] = hashlib.sha256(p.read_bytes()).hexdigest()
        per[code] = instrument_panel(df, code, MARKETS["cn"], cash, STOCK_COST, limit_pct(code, names[code]))
        rows.append({"code": code, "name": names[code], "first": str(df.index[0].date()), "last": str(df.index[-1].date())})
    return per, pd.DataFrame(rows).assign(tried=tried)


# ------------------------------------------------------------------ 输出：表
def _f(v, nd=2):
    return "—" if v is None or (isinstance(v, float) and not np.isfinite(v)) else f"{v:.{nd}f}"


def md_matrix(M: pd.DataFrame, nd=2) -> list[str]:
    L = ["| | " + " | ".join(M.columns) + " |", "|---" * (len(M.columns) + 1) + "|"]
    for r in M.index:
        L.append(f"| {r} | " + " | ".join(_f(float(v), nd) for v in M.loc[r]) + " |")
    return L


def markdown(results: dict, meta: dict) -> str:
    L = ["# 七个正式策略的信号/持仓重合度（C06）", "",
         f"生成时间 {meta['generated']}；窗口 {WIN_START}~{CUTOFF}（各品种另扣前 {WARM} 根预热）；"
         f"「持有」= 目标仓位 > 0；收益 = 相对现金 ETF 的日超额收益。", ""]
    for gname, r in results.items():
        L += [f"## {gname}（{r['n_inst']} 个品种，{r['n_days']} 个交易日，{r['start']}~{r['end']}，有效 (品种,日) 格子 {r['valid_cells']:,}）", ""]
        for title, key, nd in (("持仓 Jaccard（合并所有格子）", "jaccard", 2), ("持仓 Jaccard 的独立基线", "jaccard_independent", 2),
                               ("lift = Jaccard / 独立基线", "jaccard_lift", 2), ("加权 Jaccard（按仓位大小）", "jaccard_weighted", 2),
                               ("持仓 Jaccard（只把目标仓位>10% 算持有）", "jaccard_th10", 2),
                               ("持仓 Jaccard（逐品种平均）", "jaccard_per_instrument", 2), ("持仓 Jaccard（逐日截面平均）", "jaccard_per_day", 2),
                               ("持有/不持有 一致率（含同时空仓）", "agree", 2)):
            L += [f"### {title}", ""] + md_matrix(r["overlap"][key], nd) + [""]
        for title, key in (("日超额收益相关：逐品种相关取平均（headline）", "inst_mean"), ("日超额收益相关：组合层（品种等权平均后）", "portfolio"),
                           ("日超额收益相关：组合层、去掉大盘（买入持有）后的残差", "residual")):
            L += [f"### {title}", ""] + md_matrix(r["corr"][key]) + [""]
        L += ["### 有效独立策略数 effective N", "",
              "| 相关矩阵 | 对象 | N | 平均两两相关 | PR | 熵 | 等权 1/(w'Cw) | 第一特征值占比 |", "|---|---|---|---|---|---|---|---|"]
        for x in r["neff"]:
            L.append(f"| {x['corr_kind']} | {x['object']} | {x['N']} | {_f(x['mean_pairwise_corr'])} | {_f(x['neff_pr'])} | "
                     f"{_f(x['neff_entropy'])} | {_f(x['neff_equal_weight'])} | {_f(x['top_eig_share'])} |")
        L += ["", f"组合层自助法 95% 区间（block={BLOCK}，重抽 {meta['boot']} 次）：", "",
              "| 统计量 | 点估计 | 2.5% | 97.5% |", "|---|---|---|---|"]
        for k, v in r["point"].items():
            L.append(f"| {k} | {_f(v, 3)} | {_f(r['boot'][k][0], 3)} | {_f(r['boot'][k][1], 3)} |")
        ine = r["corr"]["inst_neff"]
        if len(ine):
            L += ["", f"逐品种 effective N 的分布（共 {len(ine)} 个品种；某策略在窗口内全程没持仓时，含它的相关矩阵没有定义，该品种不计入对应统计量）：", "",
                  "| 统计量 | 有定义的品种数 | 均值 | 中位数 | 25% | 75% |", "|---|---|---|---|---|---|"]
            for c in ine.columns:
                x = ine[c].dropna()
                q = x.quantile([0.25, 0.5, 0.75])
                L.append(f"| {c} | {len(x)} | {_f(x.mean(), 3)} | {_f(q[0.5], 3)} | {_f(q[0.25], 3)} | {_f(q[0.75], 3)} |")
        a = r["agree"]
        L += ["", "### blend2 有仓位时，3 个成分同时持有几个", "",
              f"blend2 持有率 {_f(a['blend2_hold_rate'], 3)}；有仓位时平均目标仓位 {_f(a['blend2_mean_weight_when_held'], 3)}；"
              f"目标仓位≈100% 的占比 {_f(a['blend2_full_weight_share'], 3)}。成分各自持有率："
              + "、".join(f"{k} {_f(v, 3)}" for k, v in a["hold_rate"].items()), "",
              "| 同时持有的成分数 | 实际占比 | 各自独立时的期望占比 |", "|---|---|---|"]
        for j in (1, 2, 3):
            L.append(f"| {j} | {_f(a['k_obs'][j], 3)} | {_f(a['k_indep'][j], 3)} |")
        L += ["", "### blend2 对各成分", "", "| 成分 | Jaccard | 加权 Jaccard | 相关(逐品种均值) | 相关(组合层) | 相关(去大盘残差) |", "|---|---|---|---|---|---|"]
        for x in r["blend"]:
            L.append(f"| {x['component']} | {_f(x['jaccard'])} | {_f(x['jaccard_weighted'])} | {_f(x['corr_inst_mean'])} | "
                     f"{_f(x['corr_portfolio'])} | {_f(x['corr_residual'])} |")
        L += ["", "### 各策略持有率与组合层年化波动", "", "| 策略 | 持有率 | 平均仓位 | 组合层年化波动% |", "|---|---|---|---|"]
        for x in r["vol"]:
            L.append(f"| {x['strategy']} | {_f(x['hold_rate'], 3)} | {_f(x['mean_weight'], 3)} | {_f(x['port_ann_vol_pct'], 2)} |")
        L += ["", "### 分年份（组合层）", "", "| 年 | 天数 | 3 成分平均相关 | 3 成分 N_eff(PR) | 3 成分等权 N | 7 策略 N_eff(PR) | blend2 波动/成分平均波动 |", "|---|---|---|---|---|---|---|"]
        for x in r["yearly"]:
            L.append(f"| {x['year']} | {x['days']} | {_f(x['mean_corr_3'])} | {_f(x['neff_pr_3'])} | {_f(x['neff_eq_3'])} | "
                     f"{_f(x['neff_pr_7'])} | {_f(x['vol_ratio'], 3)} |")
        L.append("")
    return "\n".join(L)


# ------------------------------------------------------------------ 输出：图
def make_figures(results: dict, out: Path) -> list[str]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams["font.sans-serif"] = ["PingFang SC", "Heiti SC", "Arial Unicode MS", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    files = []
    short = {"blend2": "blend2", "trend_ens": "trend_ens", "rsi2": "rsi2", "donchian_s1": "donchian",
             "trend_vol": "trend_vol", "ma_adx_original": "ma_adx", "ma_adx_plus": "ma_adx+"}
    labels = [short[s] for s in ORDER]

    def heat(key_path, title, fname, vmin, vmax, cmap="Blues"):
        fig, axes = plt.subplots(1, len(results), figsize=(5.6 * len(results), 5.6), constrained_layout=True)
        axes = np.atleast_1d(axes)
        for ax, (gname, r) in zip(axes, results.items()):
            M = key_path(r).to_numpy()
            im = ax.imshow(M, vmin=vmin, vmax=vmax, cmap=cmap)
            ax.set_xticks(range(7), labels, rotation=45, ha="right")
            ax.set_yticks(range(7), labels)
            ax.set_title(f"{gname}（{r['n_inst']} 品种）")
            for i in range(7):
                for j in range(7):
                    if np.isfinite(M[i, j]):
                        ax.text(j, i, f"{M[i, j]:.2f}", ha="center", va="center", fontsize=8,
                                color="white" if (M[i, j] - vmin) / (vmax - vmin) > 0.6 else "#222")
            # 画出 blend2 与三个成分所在的 4×4 块
            ax.add_patch(plt.Rectangle((-0.5, -0.5), 4, 4, fill=False, ec="#d95f02", lw=1.6))
        fig.colorbar(im, ax=axes, shrink=0.8, label=title)
        fig.suptitle(title + "（橙框 = blend2 与它的 3 个成分）")
        fig.savefig(out / fname, dpi=140)
        plt.close(fig)
        files.append(fname)

    heat(lambda r: r["overlap"]["jaccard"], "持仓 Jaccard 重合度（合并所有品种×日）", "fig_jaccard.png", 0, 1)
    heat(lambda r: r["overlap"]["jaccard_weighted"], "加权 Jaccard 重合度（按目标仓位大小）", "fig_jaccard_weighted.png", 0, 1)
    heat(lambda r: r["corr"]["inst_mean"], "日超额收益相关系数（逐品种平均）", "fig_corr.png", 0, 1)
    heat(lambda r: r["corr"]["residual"], "去掉大盘后的残差相关（组合层）", "fig_corr_residual.png", -0.6, 1, cmap="RdBu_r")

    # 有效独立策略数：条形 + 95% 区间 + 参照线
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.6), constrained_layout=True)
    colors = {"cn_etf": "#1b6ca8", "us_etf": "#e08a1e", "cn_stock": "#2a9d6f"}
    for ax, (key, n, ttl) in zip(axes, (("neff_pr_3", 3, "blend2 的 3 个成分"), ("neff_pr_7", 7, "7 个正式策略"))):
        names = list(results)
        vals = [results[g]["point"][key] for g in names]
        lo = [results[g]["point"][key] - results[g]["boot"][key][0] for g in names]
        hi = [results[g]["boot"][key][1] - results[g]["point"][key] for g in names]
        ax.bar(names, vals, yerr=[lo, hi], color=[colors.get(g, "#555") for g in names], capsize=4, width=0.55)
        ax.axhline(n, color="#555", ls="--", lw=1)
        ax.axhline(1, color="#555", ls=":", lw=1)
        ax.set_xlim(-0.5, len(names) + 0.4)           # 右边留白放参照线说明，不压在柱子上
        ax.text(len(names) + 0.35, n, f"完全独立 = {n}", ha="right", va="bottom", fontsize=8, color="#555")
        ax.text(len(names) + 0.35, 1, "完全同步 = 1", ha="right", va="top", fontsize=8, color="#555")
        for i, v in enumerate(vals):
            ax.text(i, v / 2, f"{v:.2f}", ha="center", color="white", fontsize=10)
        ax.set_ylim(0, n + 0.5)
        ax.set_title(f"{ttl}的有效独立策略数（PR，组合层，95% 自助区间）")
        ax.title.set_fontsize(10)
    fig.savefig(out / "fig_effective_n.png", dpi=140)
    plt.close(fig)
    files.append("fig_effective_n.png")

    # 分年份
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.2), constrained_layout=True, sharex=True)
    for ax, (key, ttl) in zip(axes, (("neff_pr_3", "3 个成分 N_eff（PR）"), ("mean_corr_3", "3 个成分平均两两相关"))):
        for g, r in results.items():
            ys = pd.DataFrame(r["yearly"])
            ax.plot(ys["year"], ys[key], marker="o", color=colors.get(g, "#555"), label=g, lw=1.8)
        ax.set_title(ttl + "（组合层，按自然年；2026 仅一季度）")
        ax.title.set_fontsize(10)
        ax.grid(alpha=0.25)
    axes[0].set_ylim(1, 3)
    axes[0].legend(frameon=False)
    fig.savefig(out / "fig_yearly.png", dpi=140)
    plt.close(fig)
    files.append("fig_yearly.png")
    return files


# ------------------------------------------------------------------ 主流程
def run(data_dir: Path, out_dir: Path, n_stocks: int, seed: int, boot: int, log=print) -> dict:
    check_end(CUTOFF)
    t0 = time.time()
    daily = find_daily_dir(data_dir.resolve())
    log(f"行情目录 {daily}")
    digests: dict = {}
    per_group = {"cn_etf": etf_group(daily, "cn", digests), "us_etf": etf_group(daily, "us", digests)}
    log(f"ETF 完成 ({time.time() - t0:.0f}s)")
    stocks, sample = {}, pd.DataFrame()
    if n_stocks > 0:
        stocks, sample = sample_stocks(daily, n_stocks, seed, digests)
        per_group["cn_stock"] = stocks
        log(f"个股抽样完成：{len(stocks)} 只 ({time.time() - t0:.0f}s)")
    results = {g: analyze_group(g, per, boot, seed, log) for g, per in per_group.items()}

    out_dir.mkdir(parents=True, exist_ok=True)
    long = []
    for g, r in results.items():
        for key, M in r["overlap"].items():
            long += [{"group": g, "metric": key, "row": a, "col": b, "value": float(M.loc[a, b])} for a in ORDER for b in ORDER]
        for key in ("inst_mean", "portfolio", "residual"):
            M = r["corr"][key]
            long += [{"group": g, "metric": f"corr_{key}", "row": a, "col": b, "value": float(M.loc[a, b])} for a in ORDER for b in ORDER]
    pd.DataFrame(long).to_csv(out_dir / "overlap_matrices.csv", index=False)
    pd.DataFrame([x for r in results.values() for x in r["neff"]]).to_csv(out_dir / "overlap_effective_n.csv", index=False)
    pd.DataFrame([x for r in results.values() for x in r["blend"]]).to_csv(out_dir / "overlap_blend2_vs_components.csv", index=False)
    pd.DataFrame([x for r in results.values() for x in r["yearly"]]).to_csv(out_dir / "overlap_yearly.csv", index=False)
    pd.DataFrame([x for r in results.values() for x in r["vol"]]).to_csv(out_dir / "overlap_strategy_summary.csv", index=False)
    pd.DataFrame([{"group": g, "stat": k, "point": v, "ci_lo": r["boot"][k][0], "ci_hi": r["boot"][k][1]}
                  for g, r in results.items() for k, v in r["point"].items()]).to_csv(out_dir / "overlap_bootstrap.csv", index=False)
    pd.DataFrame([{"group": g, "components_held": j, "observed": r["agree"]["k_obs"][j], "if_independent": r["agree"]["k_indep"][j]}
                  for g, r in results.items() for j in (1, 2, 3)]).to_csv(out_dir / "overlap_blend2_component_count.csv", index=False)
    if len(sample):
        sample.to_csv(out_dir / "overlap_stock_sample.csv", index=False)
    meta = {"generated": time.strftime("%Y-%m-%d %H:%M:%S"), "cutoff": CUTOFF, "window": [WIN_START, CUTOFF], "warm": WARM,
            "band": BAND, "n_stocks_requested": n_stocks, "n_stocks_used": len(stocks), "seed": seed, "boot": boot, "block": BLOCK,
            "groups": {g: {"n_inst": r["n_inst"], "n_days": r["n_days"], "instruments": per_group[g].__len__()} for g, r in results.items()},
            "daily_dir": str(daily), "inputs_digest": hashlib.sha256(json.dumps(digests, sort_keys=True).encode()).hexdigest(),
            "n_input_files": len(digests), "versions": {"pandas": pd.__version__, "numpy": np.__version__}}
    figs = make_figures(results, out_dir)
    meta["figures"] = figs
    (out_dir / "overlap_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / "overlap_tables.md").write_text(markdown(results, meta), encoding="utf-8")
    log(f"写出 {out_dir}（{time.time() - t0:.0f}s）")
    return results


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out-dir", required=True, type=Path)
    ap.add_argument("--data-dir", type=Path, default=ROOT / "data")
    ap.add_argument("--n-stocks", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=6)
    ap.add_argument("--boot", type=int, default=1000)
    a = ap.parse_args(argv)
    run(a.data_dir, a.out_dir, a.n_stocks, a.seed, a.boot)


if __name__ == "__main__":
    main()
