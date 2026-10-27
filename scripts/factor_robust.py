"""因子稳健性体检（T041）：回答「因子库里看似有效的因子，是不是只是规模/流动性/行业暴露、只在个别年份有效、扣成本后就没了」。

和 strategy_lab 的因子库体检、strategy_dev 的组合搜索是**不同的问题**，不是换种子重复：
  Q1 年度稳定性：2016~2023 每个日历年的 rank IC 符号是否一致（不能只靠合并后的一个 t 值）；
  Q2 中性化：把因子对「行业 + 20 日成交额(规模/流动性代理)」回归取残差后，IC 还剩多少（FWL 定理，逐日截面）；
  Q3 扣成本后的头部组合：方向调整后排名前 20% 的等权组合，相对可交易池等权的超额，扣换手成本后是否还为正；
  Q4 冗余：因子之间 IC 序列的相关，贪心聚类后「有效独立因子数」是多少（24 个因子不是 24 个独立证据）；
  Q5 规模分层：IC 在成交额最小/中间/最大三分之一的股票里分别是多少（有效性是不是只活在最难交易的小票里）；
  Q6 市场环境：IC 在「过去 60 日大盘上涨/下跌」「波动高于/低于历史中位数(只用过去数据的扩展中位数)」下分别是多少；
  Q7 随机对照：用随机打分跑同一套统计规则（多组、每组与真实检验家族一样大），看规则自己会不会误报。
统计口径：持有期 h∈{5,20} 个交易日，信号日收盘、次日开盘买、h 日后开盘卖，每隔 h 日取一个不重叠观测；
多重检验：全部 score 因子×2 个持有期共同做 BH-FDR（q=0.1），用中性化后 IC 的 Newey-West t（正态近似 p）。
**数据只用到 2023-12-31**（不碰最终段 2024 起，也不涉及任何封存数据）。事先登记：PREREG_ROBUST.json 在算任何结果前写入，排他创建。
「稳健候选」= 中性化 IC 的 FDR 显著且方向为正、≥75% 的年份同号、扣成本后头部组合超额均值>0；这仍是探索性结果，
不是策略有效的证据：当前在市股票的幸存者偏差、后复权价、因子之间高度相关、2012 年以来这批数据已被多次检验。

用法：.venv/bin/python -B scripts/factor_robust.py --out-dir DIR [--smoke]
"""
import argparse
import hashlib
import json
import math
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from config import MARKETS  # noqa: E402
from strategy_dev import build_arrays  # noqa: E402
from strategy_lab_xs import eligibility, nw_t  # noqa: E402

START, END = "2016-01-01", "2023-12-31"
HORIZONS = (5, 20)
TOP_Q = 0.20
LIMIT_UP = 0.095
MIN_CROSS = 200
FDR_Q = 0.10
YEAR_SHARE = 0.75
YEARS = tuple(range(2016, 2024))          # 年度稳定性的固定分母：缺失的年份算不同号，不能因为缺数据变容易通过
MIN_YEAR_OBS = 5
IC_EPS = 0.003                            # 原始 IC 绝对值低于它时，「保留比例」没有解释意义
DEGENERATE_RHO = 0.98                     # 因子与中性化变量的秩相关均值超过它，视为中性化后无剩余信号的退化情形
PLACEBO_FAMILIES = 5


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def rank_pct(a):
    """一维数组的百分位秩（NaN 保持 NaN），并列取平均秩。"""
    return pd.Series(a).rank(pct=True).values


def spearman(x, y):
    """先去掉任一侧为 NaN 的配对，再各自取秩求相关；有效配对不足 3 个、任一侧是常数时返回 nan（常数在取秩前识别，避免浮点误差）。"""
    x, y = np.asarray(x, float), np.asarray(y, float)
    m = np.isfinite(x) & np.isfinite(y)
    if m.sum() < 3:
        return float("nan")
    x, y = x[m], y[m]
    if np.ptp(x) == 0 or np.ptp(y) == 0:
        return float("nan")
    a, b = rank_pct(x), rank_pct(y)
    a, b = a - a.mean(), b - b.mean()
    d = math.sqrt(float(a @ a) * float(b @ b))
    return float(a @ b) / d if d > 1e-12 else float("nan")


def residualize(y, x, groups):
    """FWL：y 与 x 分别减去行业组内均值后，再把 y 对 x 回归取残差。groups 为整数编码。"""
    y, x = np.asarray(y, float), np.asarray(x, float)
    g = np.asarray(groups)
    n = g.max() + 1
    cnt = np.bincount(g, minlength=n).astype(float)

    def demean(v):
        return v - (np.bincount(g, weights=v, minlength=n) / np.maximum(cnt, 1))[g]
    yd, xd = demean(y), demean(x)
    den = float(xd @ xd)
    return yd - (float(xd @ yd) / den if den > 0 else 0.0) * xd


def bh_fdr(pvals, q=FDR_Q, m=None):
    """Benjamini-Hochberg：返回与输入同序的布尔列表。检验家族大小 m 固定（默认=输入长度，NaN 也算一个检验但永不拒绝）。"""
    p = np.asarray(pvals, float)
    m = len(p) if m is None else m
    idx = np.where(np.isfinite(p))[0]
    order = idx[np.argsort(p[idx])]
    thr = 0
    for rank, i in enumerate(order, 1):
        if p[i] <= q * rank / m:
            thr = rank
    keep = np.zeros(len(p), bool)
    for rank, i in enumerate(order, 1):
        if rank <= thr:
            keep[i] = True
    return keep.tolist()


def norm_p(t):
    return float("nan") if not math.isfinite(t) else math.erfc(abs(t) / math.sqrt(2))


def cluster_count(corr, thr=0.7):
    """贪心聚类：按序取一个未分配的因子，把与它 |相关|≥thr 的都并入同一簇；返回簇列表。"""
    ids, seen, out = list(corr.index), set(), []
    for a in ids:
        if a in seen:
            continue
        grp = [b for b in ids if b not in seen and (b == a or abs(corr.loc[a, b]) >= thr)]
        seen.update(grp)
        out.append(grp)
    return out


def sample_positions(dates, h):
    """不重叠观测：落在 [START, END] 内，每隔 h 个交易日取一个。"""
    pos = np.where((dates >= pd.Timestamp(START)) & (dates <= pd.Timestamp(END)))[0]
    return list(pos[::h])


def rebalance_cost(prev_codes, prev_ret, codes, cost_bp):
    """等权头部组合的调仓成本，与 strategy_dev.run_portfolio 同口径：成本 = (买入权重+卖出权重) × 单边成本。
    prev 持仓经过一个持有期后权重漂移成 (1+r_i)/Σ(1+r_j)，再调回等权；首期全部买入（权重和 1）。"""
    if not len(codes):
        return float("nan")
    if prev_codes is None:
        return cost_bp / 1e4
    r = np.nan_to_num(np.asarray(prev_ret, float), nan=0.0)
    wd = dict(zip(prev_codes, (1 + r) / (1 + r).sum()))
    wn = {c: 1.0 / len(codes) for c in codes}
    delta = sum(abs(wn.get(c, 0.0) - wd.get(c, 0.0)) for c in set(wd) | set(wn))
    return delta * cost_bp / 1e4


class Ctx:
    """评估用的预计算数组（只依赖面板，不依赖具体因子）。"""

    def __init__(self, panel, fx):
        self.ok = eligibility(panel, fx).values
        self.O, self.C = panel.open.values, panel.close.values
        self.dates = panel.close.index
        self.n = len(self.dates)
        self.amt = np.log(panel.amount.rolling(20, min_periods=15).mean().replace(0, np.nan)).values
        ind = panel.industry.reindex(panel.close.columns).fillna("未知").astype(str)
        self.grp = pd.factorize(ind)[0]
        mret = panel.mkt_ret().astype(float)
        cum = np.log1p(mret.fillna(0)).rolling(60).sum()
        vol = mret.rolling(60).std()
        self.up = (cum > 0).values                         # 只用 t 及以前的大盘数据
        self.hivol = (vol > vol.expanding().median()).values   # 扩展中位数：只用到 t 为止


def eval_score(sc, ctx, h, cost_bp):
    """对一个打分宽表（已按方向调整，越大越好）做 h 日持有期评估，返回逐观测 DataFrame。
    入选集合只依赖 t 时刻（及 t+1 开盘）的信息；未来收益缺失的股票留在入选集合里，只是不参与收益平均，避免用未来存在性做筛选。"""
    O, C, n = ctx.O, ctx.C, ctx.n
    rows, prev_codes, prev_ret = [], None, None
    for t in sample_positions(ctx.dates, h):
        if t + 1 + h >= n:
            continue
        with np.errstate(all="ignore"):
            ent, ext = O[t + 1], O[t + 1 + h]
            buyable = np.isfinite(ent) & (ent / C[t] - 1 < LIMIT_UP)
            fwd = ext / ent - 1
        m0 = ctx.ok[t] & buyable & np.isfinite(sc[t]) & np.isfinite(ctx.amt[t])
        if m0.sum() < MIN_CROSS:
            continue
        idx0 = np.where(m0)[0]
        s, a, g, f = sc[t][idx0], ctx.amt[t][idx0], ctx.grp[idx0], fwd[idx0]
        fin = np.isfinite(f)
        if fin.sum() < MIN_CROSS:
            continue
        top = s >= np.nanquantile(s, 1 - TOP_Q)
        if not (top & fin).any():
            continue
        ra = rank_pct(a)
        r = residualize(rank_pct(s), ra, g)
        codes = idx0[top]
        gross = float(f[top & fin].mean() - f[fin].mean())
        cost = rebalance_cost(prev_codes, prev_ret, codes, cost_bp)
        prev_codes, prev_ret = codes, f[top]
        ic_b = [spearman(s[fin & ((ra > lo) & (ra <= hi))], f[fin & ((ra > lo) & (ra <= hi))]) if (fin & ((ra > lo) & (ra <= hi))).sum() >= 50 else float("nan")
                for lo, hi in ((0, 1 / 3), (1 / 3, 2 / 3), (2 / 3, 1.0))]
        rows.append((ctx.dates[t], spearman(s[fin], f[fin]), spearman(r[fin], f[fin]), gross, cost, abs(spearman(s, a)),
                     ic_b[0], ic_b[1], ic_b[2], bool(ctx.up[t]), bool(ctx.hivol[t])))
    return pd.DataFrame(rows, columns=["date", "ic", "ic_n", "gross", "cost", "rho_amt", "ic_small", "ic_mid", "ic_large", "up", "hivol"]).set_index("date")


def evaluate(panel, fx, cost_bp, factor_ids, horizons=HORIZONS, ctx=None):
    """返回 {(factor,h): 汇总} 以及每个 (factor,h) 的逐观测 DataFrame。"""
    ctx = ctx or Ctx(panel, fx)
    res, dfs = {}, {}
    for fid in factor_ids:
        sc = (fx.compute_factor(fid, panel) * fx.FACTORS[fid].direction).values
        for h in horizons:
            dfs[(fid, h)] = eval_score(sc, ctx, h, cost_bp)
            res[(fid, h)] = summarize(dfs[(fid, h)])
    return res, dfs


def _mean(x):
    x = pd.Series(x).dropna()
    return float(x.mean()) if len(x) else float("nan")


def summarize(df):
    if len(df) < 20:
        return {"n": len(df), "status": "样本不足"}
    mean_ic, mean_icn = float(df["ic"].mean()), float(df["ic_n"].mean())
    if float(df["rho_amt"].mean()) > DEGENERATE_RHO:
        return {"n": len(df), "status": "退化", "rho_amt": float(df["rho_amt"].mean()), "ic": mean_ic, "t_raw": nw_t(df["ic"])}
    cnt = df.groupby(df.index.year)["ic_n"].count()
    ic_ny = df.groupby(df.index.year)["ic_n"].mean()
    valid = [y for y in YEARS if cnt.get(y, 0) >= MIN_YEAR_OBS]
    sign = np.sign(mean_icn) or 1.0
    net = (df["gross"] - df["cost"]).dropna()
    t_neu = nw_t(df["ic_n"])
    return {"n": len(df), "status": "ok",
            "ic": mean_ic, "t_raw": nw_t(df["ic"]), "ic_neutral": mean_icn, "t_neutral": t_neu, "p_neutral": norm_p(t_neu),
            "retained": mean_icn / mean_ic if abs(mean_ic) >= IC_EPS else float("nan"),
            "years": len(YEARS), "years_valid": len(valid), "years_same_sign": int(sum(1 for y in valid if np.sign(ic_ny[y]) == sign)),
            "top_gross": float(df["gross"].mean()), "top_net": float(net.mean()) if len(net) else float("nan"), "top_net_t": nw_t(net),
            "turnover_cost": float(df["cost"].mean()),
            "ic_small": _mean(df["ic_small"]), "ic_mid": _mean(df["ic_mid"]), "ic_large": _mean(df["ic_large"]),
            "ic_up": _mean(df.loc[df["up"], "ic"]), "ic_down": _mean(df.loc[~df["up"], "ic"]),
            "ic_hivol": _mean(df.loc[df["hivol"], "ic"]), "ic_lovol": _mean(df.loc[~df["hivol"], "ic"])}


def verdicts(res, with_cost=True):
    """统一 BH-FDR：家族大小 = 全部登记的检验数（样本不足/退化的算一个检验但永不拒绝）。with_cost=False 只用于随机对照的统计部分。"""
    keys = list(res)
    p = [res[k]["p_neutral"] if res[k].get("status") == "ok" else float("nan") for k in keys]
    sig = dict(zip(keys, bh_fdr(p, FDR_Q, m=len(keys))))
    for k, v in res.items():
        if v.get("status") != "ok":
            v["robust"], v["fdr"] = False, False
            continue
        v["fdr"] = bool(sig[k])
        v["stat_pass"] = bool(sig[k] and v["ic_neutral"] > 0 and v["years_same_sign"] >= YEAR_SHARE * v["years"])
        v["robust"] = bool(v["stat_pass"] and v["top_net"] > 0)
    return res


def placebo_families(ctx, fx, panel, cost_bp, n_per_family, n_families, seed):
    """随机对照：每个家族 n_per_family 个随机打分×全部持有期（与真实家族同样大），用同一套规则，返回每个家族的统计通过数/完全通过数/最大 |t|。"""
    out = []
    for fam in range(n_families):
        rng = np.random.default_rng(seed + fam)
        res = {}
        for i in range(n_per_family):
            sc = rng.normal(size=(ctx.n, len(ctx.grp)))
            for h in HORIZONS:
                res[(f"placebo{i}", h)] = summarize(eval_score(sc, ctx, h, cost_bp))
        res = verdicts(res)
        ts = [abs(v["t_neutral"]) for v in res.values() if v.get("status") == "ok" and math.isfinite(v["t_neutral"])]
        out.append({"family": fam, "tests": len(res), "stat_pass": sum(1 for v in res.values() if v.get("stat_pass")),
                    "robust": sum(1 for v in res.values() if v.get("robust")), "max_abs_t": max(ts) if ts else float("nan")})
        log(f"随机对照家族 {fam + 1}/{n_families}：统计通过 {out[-1]['stat_pass']}，完全通过 {out[-1]['robust']}")
    return out


def render(res, clusters, n_codes, smoke, placebo=None):
    L = ["# 因子稳健性体检（T041）" + ("【冒烟，不作结论】" if smoke else ""), "",
         f"股票 {n_codes} 只；数据截止 {END}；检验 {len(res)} 个（因子×持有期，统一 BH-FDR q={FDR_Q}，家族大小固定）。探索性结果，未经 Codex 审。", "",
         "| 因子 | 持有期 | IC | t(原始) | IC(中性化) | t(中性化) | 保留比例 | 年份同号(有效年) | 头部组合超额(毛) | 扣成本后 | FDR | 稳健候选 |", "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    f = lambda x, p=4: "—" if x is None or not math.isfinite(x) else f"{x:+.{p}f}"
    order = sorted(res, key=lambda k: (not res[k].get("robust", False), -(res[k].get("t_neutral") or 0) if res[k].get("status") == "ok" else 0))
    for k in order:
        v = res[k]
        if v.get("status") != "ok":
            L.append(f"| {k[0]} | {k[1]}日 | {v['status']} | {f(v.get('t_raw'), 2)} | | | | | | | | |")
            continue
        L.append(f"| {k[0]} | {k[1]}日 | {f(v['ic'])} | {f(v['t_raw'], 2)} | {f(v['ic_neutral'])} | {f(v['t_neutral'], 2)} | {f(v['retained'], 2)} | "
                 f"{v['years_same_sign']}/{v['years']}（有效{v['years_valid']}） | {f(v['top_gross'])} | {f(v['top_net'])} | {'是' if v['fdr'] else '否'} | {'**是**' if v['robust'] else '否'} |")
    nr = sum(1 for v in res.values() if v.get("robust"))
    L += ["", f"**稳健候选：{nr}/{len(res)}。**（\"退化\"= 因子与中性化变量几乎同一个排序，如 avg_amount，中性化后无信息，不参与判定。）", "",
          "## 分层 IC（原始 IC，按持有期 20 日；只描述，不参与判定）", "",
          "| 因子 | 小盘三分之一 | 中间 | 大盘三分之一 | 大盘上涨期 | 下跌期 | 高波动 | 低波动 |", "|---|---|---|---|---|---|---|---|"]
    for k in sorted(res):
        v = res[k]
        if k[1] == 20 and v.get("status") == "ok":
            L.append(f"| {k[0]} | {f(v['ic_small'])} | {f(v['ic_mid'])} | {f(v['ic_large'])} | {f(v['ic_up'])} | {f(v['ic_down'])} | {f(v['ic_hivol'])} | {f(v['ic_lovol'])} |")
    L += ["", "「小盘/大盘」按近 20 日成交额三分位（代理规模与流动性）。若 IC 几乎全在小盘一栏，说明有效性可能活在最难交易的股票里。", ""]
    if placebo:
        L += ["## 随机对照（规则自己会不会误报）", "", "每个家族用随机打分、与真实家族同样大的检验数，套同一套规则：", "",
              "| 家族 | 检验数 | 统计通过 | 完全通过(含成本) | 最大|t| |", "|---|---|---|---|---|"]
        for r in placebo:
            L.append(f"| {r['family'] + 1} | {r['tests']} | {r['stat_pass']} | {r['robust']} | {f(r['max_abs_t'], 2)} |")
        bad = sum(1 for r in placebo if r["stat_pass"] > 0)
        L += ["", f"{bad}/{len(placebo)} 个随机家族出现了「统计通过」。FDR 水平 q={FDR_Q} 下，纯噪声家族里出现至少一个通过的概率应不高于约 10%；明显更高说明规则偏松或 t 值被低估。", ""]
    L += ["## 冗余：IC 序列相关 ≥0.7 的因子并成一簇", "", f"共 {len(clusters)} 簇（从 {sum(len(c) for c in clusters)} 个因子）。", ""]
    for c in clusters:
        L.append(f"- {', '.join(c)}")
    L += ["", "## 读法与局限", "",
          "- 「保留比例」= 中性化后 IC ÷ 原始 IC（原始 IC 绝对值<0.003 时不计算）：接近 0 说明因子主要是行业/规模/流动性暴露；",
          "- 入选集合只用 t 时刻信息（含 t+1 开盘价判断涨停开盘不可买）；未来收益缺失（退市/长期停牌）的股票不参与收益平均，这会略微高估，因为最差的结局被丢掉；",
          "- 头部组合 = 方向调整后排名前 20% 等权，减可交易池等权；成本按买卖权重变化 × 单边成本，首期计全部买入；未模拟涨跌停无法卖出；",
          "- 成交额代理的是规模/流动性，不是市值（本地无股本数据）；行业取当前分类；",
          "- 年度稳定性分母固定为 2016~2023 共 8 年，缺数据的年份不算同号；",
          "- 当前在市股票（幸存者偏差）；后复权价；数据只到 2023-12-31；这批数据已被多次检验，新增检验仍会抬高假阳性；",
          "- 通过这些检查只说明「没被这几个角度推翻」，不说明可交易；不构成投资建议。"]
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--smoke", action="store_true")
    a = ap.parse_args(argv)
    out = Path(a.out_dir)
    from src import allmarket, datasource
    from src.market_monitor import factors as fx
    cost_bp = MARKETS["cn"]["stock_cost_bp"]
    lst = pd.read_csv(allmarket.LIST_FILE, dtype=str)
    codes = sorted(c for c in lst["code"] if datasource.is_valid_stock(c))
    fids = sorted(k for k, m in fx.FACTORS.items() if m.kind == "score")
    n_fam = PLACEBO_FAMILIES
    if a.smoke:
        codes, fids, n_fam = codes[:1200], fids[:3], 2
    out.mkdir(parents=True, exist_ok=False)
    with open(out / "PREREG_ROBUST.json", "x", encoding="utf-8") as pf:
        json.dump({"started": time.strftime("%Y-%m-%d %H:%M"), "window": [START, END], "horizons": list(HORIZONS), "top_q": TOP_Q,
                   "factors": fids, "directions": {k: fx.FACTORS[k].direction for k in fids}, "n_tests": len(fids) * len(HORIZONS),
                   "fdr_q": FDR_Q, "fdr_family": "全部登记的检验，家族大小固定", "year_share": YEAR_SHARE, "years": list(YEARS), "min_year_obs": MIN_YEAR_OBS,
                   "nw_lag": 4, "min_cross": MIN_CROSS, "limit_up": LIMIT_UP, "ic_eps": IC_EPS, "degenerate_rho": DEGENERATE_RHO,
                   "placebo": {"families": n_fam, "per_family": len(fids), "seed": 20270128},
                   "cost_bp_one_side": cost_bp, "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                   "robust_rule": "中性化IC FDR显著且为正 & 年份同号≥75%(固定8年分母) & 扣成本头部组合超额>0",
                   "note": "规则在算任何结果之前写入，之后不得修改；数据只到 2023-12-31"},
                  pf, ensure_ascii=False, indent=1)
    log(f"构造面板：{len(codes)} 只，截至 {END}")
    panel = build_arrays(codes, fx, allmarket, end=END)
    log(f"面板 {panel.close.shape}")
    ctx = Ctx(panel, fx)
    res, dfs = evaluate(panel, fx, cost_bp, fids, ctx=ctx)
    res = verdicts(res)
    log("真实因子评估完成，开始随机对照")
    placebo = placebo_families(ctx, fx, panel, cost_bp, len(fids), n_fam, 20270128)
    h0 = HORIZONS[0]
    icm = pd.DataFrame({fid: dfs[(fid, h0)]["ic"] for fid in fids}).dropna(how="all")
    clusters = cluster_count(icm.corr().fillna(0)) if len(icm) > 20 and len(fids) > 1 else [[f] for f in fids]
    (out / "robust_results.json").write_text(json.dumps({"results": {f"{k[0]}|{k[1]}": v for k, v in res.items()}, "placebo": placebo}, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
    (out / "report.md").write_text(render(res, clusters, len(panel.close.columns), a.smoke, placebo), encoding="utf-8")
    log("完成")


if __name__ == "__main__":
    main()
