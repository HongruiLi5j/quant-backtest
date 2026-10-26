"""策略开发（T028）：因子 TopkDropout 组合的候选搜索，带多重检验校正。只读本地缓存，不联网，不导入 app。

做法（参考 Qlib 官方基准的 TopkDropout：持有前 K 只，每期最多换 n_drop 只；López de Prado 的 PBO/DSR；
Harvey-Liu-Zhu 多重检验下新因子 t 值要远高于 2，这里最终段用 3.0 作为参照线）：
  1. 候选 = 因子库里的 score 因子 × 调仓间隔 {5, 20 个交易日} × 持有只数 K {30, 60}；n_drop = K 的 10%。
  2. 每个候选用同一个引擎跑完 2016 年到最新数据，逐期记录扣成本后相对基准的超额。
  3. 选择：只看训练段（信号日与持有期都在 2016-01~2021-12）的月度超额夏普，取前 5 个；
     验证段（2022~2023）超额均值>0 且 Newey-West t>2.0 才留下；留下的才允许看最终段（2024-01 起），且只看一次。
  4. 最终段「存活」= 超额均值>0 且 NW t>2.0 且成本加倍后均值>0；t>3.0 另外标出（Harvey-Liu-Zhu 参照线）。
  5. 同时报告：全部候选的数量（多重检验次数）、训练+验证段的 CSCV 回测过拟合概率 PBO、最佳训练候选的 Deflated Sharpe、
     最佳训练候选对随机打分组合的分位。
事先登记：规则在算任何结果之前写入 PREREG_DEV.json（排他创建，已存在则拒绝），之后不得修改。

用法：.venv/bin/python -B scripts/strategy_dev.py --out-dir DIR [--smoke]

局限（登记时就承认）：5223 只名单与 2012 年以来的数据在之前的检验里全部被看过，所以这不是真正的样本外，只是新增一批检验；
名单是当前在市股票（幸存者偏差），ST 用当前名称；后复权价；基准（可交易池等权）不扣成本，只是比较口径，不能说偏保守；
最终段只有约 2.8 年；通过也只说明「未被推翻」。未经 Codex 审时一律标探索性。
"""
import argparse
import itertools
import json
import math
import statistics
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from config import MARKETS  # noqa: E402
from strategy_lab_xs import eligibility, nw_t, rebalance_positions  # noqa: E402

TRAIN = ("2016-01-01", "2021-12-31")
VALID = ("2022-01-01", "2023-12-31")
FINAL = ("2024-01-01", "2099-12-31")
STEPS = (5, 20)
KS = (30, 60)
DROP_FRAC = 0.10
LIMIT_UP = 0.095
TOP_N = 5
T_MIN = 2.0
T_HLZ = 3.0
CSCV_S = 8
N_RANDOM = 200
SEED = 20261026


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


# ---------------------------------------------------------------- 引擎
def run_portfolio(score, ok, O, C, V, Cff, dates, step, K, ndrop, cost_bp):
    """逐期 TopkDropout 回测（numpy）。全部输入是 (T, N) 数组，dates 是长度 T 的 DatetimeIndex。
    信号用 t 日及以前；t+1 开盘成交；持有到下一个调仓日的 t'+1 开盘。成本按实际买入+卖出金额（占净值比例）× 单边 bp。
    买不进（无开盘价/无成交量/开盘涨幅≥9.5%）的候选顺延到下一名；卖不出（无成交/开盘跌幅≥9.5%）的旧仓继续持有；
    停牌期间退出价用最后一个收盘价。基准 = t 日可交易、t+1 能买进、有退出价的股票等权，不扣成本。
    返回 DataFrame：sig_date、exit_date、gross、cost、net、bench、excess、turnover、n_hold。"""
    T, N = O.shape
    pos = rebalance_positions(T, step)
    rows, w_pre = [], np.zeros(N)
    for a, b in zip(pos[:-1], pos[1:]):
        o1, v1, pc = O[a + 1], V[a + 1], C[a]
        tradable = np.isfinite(o1) & (v1 > 0) & np.isfinite(pc)
        with np.errstate(divide="ignore", invalid="ignore"):
            gap = o1 / pc - 1.0
        can_buy = tradable & (gap < LIMIT_UP)
        can_sell = tradable & (gap > -LIMIT_UP)
        sc = score[a]
        valid = ok[a] & np.isfinite(sc)
        held = w_pre > 0
        keep = held.copy()
        keep &= ~(held & ~valid & can_sell)                          # 失去资格且卖得出：强制卖出
        cand = np.where(keep & valid)[0]
        if len(cand) and ndrop > 0:
            worst = cand[np.argsort(sc[cand], kind="stable")[:ndrop]]    # 持仓里得分最低的 n_drop 只
            keep[worst[can_sell[worst]]] = False
        need = K - int(keep.sum())
        if need > 0:
            pool = np.where(valid & can_buy & ~keep)[0]
            keep[pool[np.argsort(-sc[pool], kind="stable")[:need]]] = True
        if not keep.any():
            continue
        fixed = keep & held & ~valid                                  # 失去资格又卖不出的旧仓：权重不动
        free = keep & ~fixed
        w_tgt = np.zeros(N)
        w_tgt[fixed] = w_pre[fixed]
        if free.any():
            w_tgt[free] = max(0.0, 1.0 - w_tgt.sum()) / free.sum()
        entry = np.where(tradable, o1, Cff[a + 1])
        o2 = O[b + 1]
        exit_px = np.where(np.isfinite(o2) & (V[b + 1] > 0), o2, Cff[b + 1])
        with np.errstate(divide="ignore", invalid="ignore"):
            r = exit_px / entry - 1.0
        r_f = np.where(np.isfinite(r), r, 0.0)
        gross = float((w_tgt * r_f).sum())
        buys, sells = np.maximum(w_tgt - w_pre, 0).sum(), np.maximum(w_pre - w_tgt, 0).sum()
        cost = float((buys + sells) * cost_bp / 1e4)
        elig = valid & can_buy & np.isfinite(r)
        bench = float(r[elig].mean()) if elig.any() else float("nan")
        rows.append({"sig_date": dates[a], "exit_date": dates[b + 1], "gross": gross, "cost": cost, "net": gross - cost, "bench": bench,
                     "excess": gross - cost - bench, "turnover": float((buys + sells) / 2), "n_hold": int(keep.sum())})
        v = w_tgt * (1.0 + r_f)
        total = v.sum() + (1.0 - w_tgt.sum())
        w_pre = v / total
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- 分段与统计
def segment_of(sig, exit_):
    """信号日和退出日必须都落在同一段里，否则返回 None（跨界的整期清除，防止标签泄漏）。"""
    for name, (lo, hi) in (("train", TRAIN), ("valid", VALID), ("final", FINAL)):
        if pd.Timestamp(lo) <= sig and exit_ <= pd.Timestamp(hi):
            return name
    return None


def split_segments(df):
    seg = [segment_of(s, e) for s, e in zip(df["sig_date"], df["exit_date"])]
    out = df.assign(seg=seg)
    return {k: out[out["seg"] == k] for k in ("train", "valid", "final")}


def monthly(df, col="excess"):
    """把各期收益按退出月份相加（近似月度序列，用来对齐不同调仓间隔的候选）。"""
    if df is None or len(df) == 0:
        return pd.Series(dtype=float)
    return df.groupby(pd.to_datetime(df["exit_date"]).dt.to_period("M"))[col].sum()


def ann_sharpe_m(x):
    x = pd.Series(x).dropna()
    if len(x) < 3 or x.std(ddof=1) == 0:
        return float("nan")
    return float(x.mean() / x.std(ddof=1) * math.sqrt(12))


def cscv_pbo(M, S=CSCV_S):
    """组合对称交叉验证回测过拟合概率（Bailey et al.）。M 是 (月份 × 候选) 的超额收益矩阵。
    返回 (PBO, 切分数)；训练集最优候选在测试集排名不高于中位数的比例。"""
    M = np.asarray(M, dtype=float)
    T, n = M.shape
    if n < 2 or T < S * 2:
        return float("nan"), 0
    blocks = np.array_split(np.arange(T), S)
    below, total = 0, 0
    for comb in itertools.combinations(range(S), S // 2):
        tr = np.concatenate([blocks[i] for i in comb])
        te = np.concatenate([blocks[i] for i in range(S) if i not in comb])
        def sh(idx):
            x = M[idx]
            sd = x.std(axis=0, ddof=1)
            with np.errstate(divide="ignore", invalid="ignore"):
                return np.where(sd > 0, x.mean(axis=0) / sd, -np.inf)
        best = int(np.argmax(sh(tr)))
        s_te = sh(te)
        rank = (s_te <= s_te[best]).sum() / n            # 最优候选在测试集的相对排名（0~1）
        below += rank <= 0.5
        total += 1
    return float(below / total), total


def deflated_sharpe(sr_monthly, n_trials, var_trial_sr, T, skew, kurt):
    """Deflated Sharpe Ratio（Bailey & López de Prado 2014）：月度夏普 sr_monthly 在 n_trials 次尝试后仍显著的概率。"""
    if n_trials < 2 or T < 3 or not math.isfinite(sr_monthly) or var_trial_sr <= 0:
        return float("nan")
    nd = statistics.NormalDist()
    g = 0.5772156649
    sr0 = math.sqrt(var_trial_sr) * ((1 - g) * nd.inv_cdf(1 - 1 / n_trials) + g * nd.inv_cdf(1 - 1 / (n_trials * math.e)))
    denom = 1 - skew * sr_monthly + (kurt - 1) / 4 * sr_monthly ** 2
    if denom <= 0:
        return float("nan")
    return float(nd.cdf((sr_monthly - sr0) * math.sqrt(T - 1) / math.sqrt(denom)))


def seg_stats(df):
    ex = df["excess"].dropna() if len(df) else pd.Series(dtype=float)
    ex2 = (df["gross"] - 2 * df["cost"] - df["bench"]).dropna() if len(df) else pd.Series(dtype=float)
    return {"n": int(len(ex)), "mean": float(ex.mean()) if len(ex) else float("nan"), "nw_t": nw_t(ex),
            "mean_cost_x2": float(ex2.mean()) if len(ex2) else float("nan"),
            "turnover": float(df["turnover"].mean()) if len(df) else float("nan")}


def select_and_test(cands):
    """事先登记的选择协议。cands: {id: {"train": df, "valid": df, "final": df}}。返回报告字典。
    train 夏普排前 TOP_N → valid 均值>0 且 t>T_MIN 才留下 → 只有留下的才看 final。"""
    train_sh = {k: ann_sharpe_m(monthly(v["train"])) for k, v in cands.items()}
    ranked = sorted((k for k in cands if math.isfinite(train_sh[k])), key=lambda k: -train_sh[k])[:TOP_N]
    out = {"top": [], "n_candidates": len(cands)}
    for k in ranked:
        vs = seg_stats(cands[k]["valid"])
        keep = bool(vs["n"] > 0 and vs["mean"] > 0 and vs["nw_t"] > T_MIN)
        row = {"id": k, "train_sharpe_m": train_sh[k], "train": seg_stats(cands[k]["train"]), "valid": vs, "kept": keep, "final": None}
        if keep:
            fs = seg_stats(cands[k]["final"])
            fs["survive"] = bool(fs["n"] > 0 and fs["mean"] > 0 and fs["nw_t"] > T_MIN and fs["mean_cost_x2"] > 0)
            fs["above_hlz"] = bool(fs["nw_t"] > T_HLZ)
            row["final"] = fs
        out["top"].append(row)
    return out


# ---------------------------------------------------------------- 数据与候选
def build_arrays(codes, fx, allmarket, end=None):
    cols = ["Open", "High", "Low", "Close", "Volume"]
    ser = {c: {} for c in cols}
    for code in codes:
        try:
            df = allmarket.load(code)
            df = df[df.index >= "2014-01-01"]
            if end:
                df = df[df.index <= end]
        except Exception:
            continue
        if len(df) < 400:
            continue
        for c in cols:
            ser[c][code] = df[c].astype(float)
    wide = {c: pd.DataFrame(ser[c]).sort_index() for c in cols}
    lst = pd.read_csv(allmarket.LIST_FILE, dtype=str).set_index("code")
    cl = wide["Close"].columns
    return fx.Panel(wide["Open"], wide["High"], wide["Low"], wide["Close"], wide["Volume"], lst["name"].reindex(cl), lst["industry"].reindex(cl))


def candidate_grid(fx, smoke=False):
    score_ids = sorted(k for k, m in fx.FACTORS.items() if m.kind == "score")
    steps, ks = ((5, 20), (30,)) if smoke else (STEPS, KS)
    if smoke:
        score_ids = score_ids[:3]
    return [(f, s, k) for f in score_ids for s in steps for k in ks]


def run_all(panel, fx, cost_bp, smoke=False, n_random=N_RANDOM):
    ok = eligibility(panel, fx).values
    O, C, V = panel.open.values, panel.close.values, panel.volume.values
    Cff = panel.close.ffill().values
    dates = panel.close.index
    cands, series, scores = {}, {}, {}
    grid = candidate_grid(fx, smoke)
    for fid in sorted({g[0] for g in grid}):
        scores[fid] = (fx.compute_factor(fid, panel) * fx.FACTORS[fid].direction).values
    for i, (fid, step, K) in enumerate(grid):
        df = run_portfolio(scores[fid], ok, O, C, V, Cff, dates, step, K, max(1, int(K * DROP_FRAC)), cost_bp)
        cid = f"{fid}|{step}d|K{K}"
        cands[cid] = split_segments(df) if len(df) else {k: df for k in ("train", "valid", "final")}
        series[cid] = df
        if (i + 1) % 10 == 0:
            log(f"候选 {i + 1}/{len(grid)}")
    rep = select_and_test(cands)
    # PBO 与 DSR 只用训练+验证段（最终段保持干净）
    M = pd.concat({k: monthly(pd.concat([v["train"], v["valid"]])) for k, v in cands.items()}, axis=1).dropna(how="any")
    rep["pbo"], rep["pbo_splits"] = cscv_pbo(M.values) if len(M) else (float("nan"), 0)
    rep["months_pbo"] = int(len(M))
    if rep["top"]:
        best = rep["top"][0]["id"]
        x = monthly(pd.concat([cands[best]["train"], cands[best]["valid"]]))
        srs = [ann_sharpe_m(M[c]) / math.sqrt(12) for c in M.columns] if len(M) else []
        sr_m = float(x.mean() / x.std(ddof=1)) if len(x) > 2 and x.std(ddof=1) > 0 else float("nan")
        rep["dsr_best"] = deflated_sharpe(sr_m, len(cands), float(np.nanvar(srs, ddof=1)) if len(srs) > 1 else 0.0, len(x),
                                          float(pd.Series(x).skew()) if len(x) > 2 else 0.0, float(pd.Series(x).kurt() + 3) if len(x) > 3 else 3.0)
        rep["best_id"] = best
        fid, step, K = best.split("|")[0], int(best.split("|")[1][:-1]), int(best.split("|")[2][1:])
        rng = np.random.default_rng(SEED)
        real = seg_stats(pd.concat([cands[best]["train"], cands[best]["valid"]]))["mean"]
        means = []
        for _ in range(n_random):
            rs = rng.standard_normal(scores[fid].shape)
            d = run_portfolio(rs, ok, O, C, V, Cff, dates, step, K, max(1, int(K * DROP_FRAC)), cost_bp)
            if len(d):
                s = split_segments(d)
                means.append(seg_stats(pd.concat([s["train"], s["valid"]]))["mean"])
        means = np.array([m for m in means if math.isfinite(m)])
        rep["random_percentile_best"] = float((means < real).mean()) if len(means) else float("nan")
        rep["n_random_done"] = int(len(means))
    return rep, cands


def write_prereg(out, grid_n, cost_bp, n_codes, smoke):
    pf = out / "PREREG_DEV.json"
    body = {"started": time.strftime("%Y-%m-%d %H:%M"), "train": TRAIN, "valid": VALID, "final": FINAL, "steps": STEPS, "ks": KS,
            "drop_frac": DROP_FRAC, "top_n": TOP_N, "t_min": T_MIN, "t_hlz": T_HLZ, "cscv_s": CSCV_S, "n_random_planned": N_RANDOM,
            "cost_bp_one_side": cost_bp, "limit_up": LIMIT_UP, "n_candidates": grid_n, "n_codes": n_codes, "smoke": bool(smoke), "seed": SEED,
            "rule": "训练段月度超额夏普前5 → 验证段均值>0且NW t>2.0才留下 → 只有留下的才看最终段一次；最终段存活=均值>0且t>2.0且成本加倍后均值>0",
            "note": "本文件在计算任何结果之前写入，之后不得修改；名单与历史已在之前的检验中全部被看过，这不是真正的样本外；未经 Codex 审时标探索性"}
    with open(pf, "x", encoding="utf-8") as f:                           # 排他创建：已存在就报错，不覆盖
        f.write(json.dumps(body, ensure_ascii=False, indent=1))


def render(rep, n_codes, cost_bp, smoke):
    L = ["# 策略开发报告（探索性，事先登记，未经 Codex 审）", "",
         f"股票 {n_codes} 只{'（smoke 自检，不是正式结果）' if smoke else ''}；候选 {rep['n_candidates']} 个（多重检验次数）；单边成本 {cost_bp}bp；规则见 PREREG_DEV.json。", "",
         f"- 训练+验证段 CSCV 回测过拟合概率 PBO：{rep.get('pbo', float('nan')):.2f}（{rep.get('pbo_splits', 0)} 个切分，{rep.get('months_pbo', 0)} 个月；越接近 0.5 越像在拟合噪音）",
         f"- 最佳训练候选 {rep.get('best_id', '-')} 的 Deflated Sharpe：{rep.get('dsr_best', float('nan')):.2f}（>0.95 才算在 {rep['n_candidates']} 次尝试后仍显著）",
         f"- 最佳训练候选相对随机打分组合的分位（训练+验证段，{rep.get('n_random_done', 0)} 次）：{rep.get('random_percentile_best', float('nan')):.2f}", "",
         "| 候选 | 训练夏普(月) | 训练均值 | 验证均值 | 验证 t | 留下 | 最终均值 | 最终 t | 成本×2 均值 | 平均单边换手 | 最终存活 | t>3.0 |", "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rep["top"]:
        f = r["final"]
        L.append(f"| {r['id']} | {r['train_sharpe_m']:+.2f} | {r['train']['mean']:+.4f} | {r['valid']['mean']:+.4f} | {r['valid']['nw_t']:+.2f} | {'是' if r['kept'] else '否'} | "
                 + (f"{f['mean']:+.4f} | {f['nw_t']:+.2f} | {f['mean_cost_x2']:+.4f} | {f['turnover']:.2f} | {'**存活**' if f['survive'] else '未存活'} | {'是' if f['above_hlz'] else '否'} |" if f
                    else "- | - | - | - | 未进入最终段 | - |"))
    kept = [r for r in rep["top"] if r["kept"]]
    L += ["", f"**结论**：训练段前 {len(rep['top'])} 名里，验证段留下 {len(kept)} 个；最终段存活 {sum(1 for r in kept if r['final'] and r['final']['survive'])} 个。",
          "", "## 局限", "", "- 名单与历史在之前的检验里全部被看过，这不是真正的样本外，只是新增一批检验；候选数量已计入 PBO/DSR。",
          "- 当前在市名单（幸存者偏差）、ST 用当前名称、后复权价；基准不扣成本只是比较口径；最终段约 2.8 年，样本有限；涨停阈值统一 9.5%。",
          "- 即使「存活」，也只说明未被推翻，不是样本外证明，更不构成投资建议；通过的候选只能进入前向纸面记录继续观察。"]
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
    if a.smoke:
        codes = codes[:300]
    out.mkdir(parents=True, exist_ok=True)
    write_prereg(out, len(candidate_grid(fx, a.smoke)), cost_bp, len(codes), a.smoke)
    log(f"构造面板：{len(codes)} 只")
    panel = build_arrays(codes, fx, allmarket)
    log(f"面板 {panel.close.shape}，开始候选搜索")
    rep, _ = run_all(panel, fx, cost_bp, smoke=a.smoke, n_random=10 if a.smoke else N_RANDOM)
    (out / "dev_results.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
    (out / "report.md").write_text(render(rep, len(codes), cost_bp, a.smoke), encoding="utf-8")
    log("完成")


if __name__ == "__main__":
    main()
