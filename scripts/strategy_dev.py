"""策略开发（T028）：因子 TopkDropout 组合的候选搜索，带多重检验校正。只读本地缓存，不联网，不导入 app。

做法（参考 Qlib 官方基准的 TopkDropout：持有前 K 只，每期最多换 n_drop 只；López de Prado 的 PBO/DSR；
Harvey-Liu-Zhu 多重检验下新因子 t 值要远高于 2，这里最终段用 3.0 作为参照线）：
  1. 候选 = 因子库里的 score 因子 × 调仓间隔 {5, 20 个交易日} × 持有只数 K {30, 60}；n_drop = K 的 10%。
  2. 每个候选用同一个引擎跑完 2016 年到最新数据，逐期记录扣成本后相对基准的超额。
  3. 选择：只看训练段（信号日与持有期都在 2016-01~2021-12）的月度超额夏普，取前 5 个；
     验证段（2022~2023）超额均值>0 且 Newey-West t>2.0 才留下；留下的才允许看最终段（2024-01 起），且只看一次：
     选择阶段的面板截到 2023-12-31，看最终段前要先在固定路径 handoff/data/dev/FINAL_LOOKED.json 排他创建全局锁。
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
import dataclasses
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
def run_portfolio(score, ok, O, C, V, Cff, dates, step, K, ndrop, cost_bp, bench_ok=None):
    """逐期 TopkDropout 回测（numpy）。全部输入是 (T, N) 数组，dates 是长度 T 的 DatetimeIndex。
    信号用 t 日及以前；t+1 开盘成交；持有到下一个调仓日的 t'+1 开盘。成本按实际买入+卖出金额（占净值比例）× 单边 bp。
    买不进（无开盘价/开盘涨幅≥9.5%）的候选顺延到下一名；卖不出（无开盘价/开盘跌幅≥9.5%）的旧仓继续持有；
    停牌期间退出价用决策日以前的最后一个收盘价；能否成交只看次日是否有开盘价(本地数据停牌日没有行)，不用次日成交量。基准 = t 日可交易(bench_ok，缺省用 ok)、t+1 能买进、有退出价的股票等权，不扣成本；基准池与因子得分是否缺失无关，所有候选用同一个基准。
    返回 DataFrame：sig_date、exit_date、gross、cost、net、bench、excess、turnover、n_hold。"""
    T, N = O.shape
    pos = rebalance_positions(T, step)
    rows, w_pre = [], np.zeros(N)
    for a, b in zip(pos[:-1], pos[1:]):
        o1, pc = O[a + 1], C[a]
        # 次日开盘能否成交：只看次日是否有开盘价(本地数据里停牌日没有行，已核对 40 万行样本无零成交行)，不用次日成交量；涨跌停按开盘价相对前收判断
        tradable = np.isfinite(o1) & (o1 > 0) & np.isfinite(pc)
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
        if not keep.any() and not held.any():
            continue                                                  # 既没有持仓也选不出股票：这一期不存在
        # 卖不出的旧仓(停牌/开盘跌停)无论有没有资格，实际持股固定不动；只在可成交的名字之间分配剩下的资金
        fixed = keep & held & ~can_sell
        free = keep & ~fixed
        w_tgt = np.zeros(N)
        w_tgt[fixed] = w_pre[fixed]
        if free.any():
            w_tgt[free] = max(0.0, 1.0 - w_tgt.sum()) / free.sum()
            cap = free & ~can_buy & (w_tgt > w_pre)                   # 买不进的(开盘涨停)不能加仓，多出的部分留作现金
            w_tgt[cap] = w_pre[cap]
        # 入场价：当天能成交用开盘价，否则沿用决策日以前的最后一个收盘价；退出价同理(不用 t+1 当天收盘价)
        entry = np.where(tradable, o1, Cff[a])
        o2 = O[b + 1]
        exit_px = np.where(np.isfinite(o2) & (o2 > 0), o2, Cff[b])
        with np.errstate(divide="ignore", invalid="ignore"):
            r = exit_px / entry - 1.0
        r_f = np.where(np.isfinite(r), r, 0.0)
        gross = float((w_tgt * r_f).sum())
        buys, sells = np.maximum(w_tgt - w_pre, 0).sum(), np.maximum(w_pre - w_tgt, 0).sum()
        cost = float((buys + sells) * cost_bp / 1e4)
        elig = (ok[a] if bench_ok is None else bench_ok[a]) & can_buy & np.isfinite(r)     # 基准池不看因子得分：不同因子的缺失值不能改变比较基准
        bench = float(r[elig].mean()) if elig.any() else 0.0           # 没有可买股票(如清仓期)：基准按空仓 0 计，保证清仓成本进入超额统计
        rows.append({"sig_date": dates[a], "exit_date": dates[b + 1], "gross": gross, "cost": cost, "net": gross - cost, "bench": bench,
                     "excess": gross - cost - bench, "turnover": float((buys + sells) / 2), "n_hold": int(keep.sum())})
        # 权重都是占「当期净值」的比例：费用按比例从各持仓和现金里扣，所以不改变持仓之间的相对权重，只体现在 net 里
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
        less, eq = int((s_te < s_te[best]).sum()), int((s_te == s_te[best]).sum())
        rank = (less + (eq + 1) / 2) / (n + 1)         # 并列取中间名次；全部并列时 rank=0.5，计为「不高于中位数」（区分不了就不能说没过拟合）
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


def select_and_test(cands, final_provider=None):
    """事先登记的选择协议。cands: {id: {"train": df, "valid": df, ...}}。返回报告字典。
    train 夏普排前 TOP_N → valid 均值>0 且 t>T_MIN 才留下 → 只有留下的才看 final。
    final_provider(留下的 id 列表) 返回 {id: 最终段 df}，或返回 None 表示最终段已被看过、拒绝再看；缺省取 cands[k]['final']（仅测试用）。"""
    if final_provider is None:
        final_provider = lambda ids: {k: cands[k]["final"] for k in ids}
    train_sh = {k: ann_sharpe_m(monthly(v["train"])) for k, v in cands.items()}
    ranked = sorted((k for k in cands if math.isfinite(train_sh[k])), key=lambda k: -train_sh[k])[:TOP_N]
    out = {"top": [], "n_candidates": len(cands), "final_blocked": False}
    rows = []
    for k in ranked:
        vs = seg_stats(cands[k]["valid"])
        keep = bool(vs["n"] > 0 and vs["mean"] > 0 and vs["nw_t"] > T_MIN)
        rows.append({"id": k, "train_sharpe_m": train_sh[k], "train": seg_stats(cands[k]["train"]), "valid": vs, "kept": keep, "final": None})
    kept_ids = [r["id"] for r in rows if r["kept"]]
    finals = final_provider(kept_ids) if kept_ids else {}
    if finals is None:
        out["final_blocked"] = True                                   # 最终段只能看一次：锁已存在，拒绝
        finals = {}
    for r in rows:
        if r["kept"] and r["id"] in finals:
            fs = seg_stats(finals[r["id"]])
            fs["survive"] = bool(fs["n"] > 0 and fs["mean"] > 0 and fs["nw_t"] > T_MIN and fs["mean_cost_x2"] > 0)
            fs["above_hlz"] = bool(fs["nw_t"] > T_HLZ)
            fs["survive_hlz"] = bool(fs["survive"] and fs["above_hlz"])
            r["final"] = fs
    out["top"] = rows
    return out


def acquire_final_lock(path, ids):
    """最终段「只看一次」的全局锁：固定路径、排他创建；已存在就抛 FileExistsError。换输出目录也绕不过去。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "x", encoding="utf-8") as f:
        f.write(json.dumps({"time": time.strftime("%Y-%m-%d %H:%M"), "ids": list(ids)}, ensure_ascii=False, indent=1))


def truncate_panel(panel, end):
    """只保留 end（含）以前的数据：选择阶段拿到的面板里根本没有最终段。"""
    kw = {f: getattr(panel, f).loc[:end] for f in ("open", "high", "low", "close", "volume")}
    if panel.market_ret is not None:
        kw["market_ret"] = panel.market_ret.loc[:end]
    return dataclasses.replace(panel, **kw)


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


def _prep(panel, fx):
    return (eligibility(panel, fx).values, panel.open.values, panel.close.values, panel.volume.values,
            panel.close.ffill().values, panel.close.index)


def restrict_columns(panel, cols):
    """只保留指定股票列，并去掉这些股票全都没有行情的日期：最终段用的股票池和交易日历在拿锁之前就由选择面板定死，
    被排除股票的日期不能改变调仓日(调仓按行号每隔固定步长)。"""
    idx = panel.close[cols].dropna(how="all").index
    kw = {f: getattr(panel, f).loc[idx, cols] for f in ("open", "high", "low", "close", "volume")}
    for f in ("names", "industry"):
        v = getattr(panel, f)
        if v is not None:
            kw[f] = v.reindex(cols)
    if panel.market_ret is not None:
        kw["market_ret"] = panel.market_ret.reindex(idx)
    return dataclasses.replace(panel, **kw)


def check_same_history(sel, full, end):
    """完整面板截到 end 的日期与行情必须和选择面板完全一致，否则拒绝(防止选择阶段与最终段用了不同的交易日历或数据)。"""
    a, b = sel.close, full.close.loc[:end]
    if not a.index.equals(b.index) or not list(a.columns) == list(b.columns):
        raise ValueError("完整面板与选择面板的日期或股票列不一致")
    for f in ("open", "high", "low", "close", "volume"):          # high/low 也参与因子计算，必须一并核对
        x, y = getattr(sel, f), getattr(full, f).loc[:end]
        if not x.equals(y):
            raise ValueError(f"完整面板与选择面板的 {f} 历史数据不一致")
    for f in ("names", "industry"):
        x, y = getattr(sel, f), getattr(full, f)
        if (x is None) != (y is None) or (x is not None and not x.equals(y)):
            raise ValueError(f"完整面板与选择面板的 {f} 不一致")
    x, y = sel.market_ret, full.market_ret
    if (x is None) != (y is None) or (x is not None and not x.equals(y.loc[:end])):
        raise ValueError("完整面板与选择面板的 market_ret 不一致")


def run_all(panel, fx, cost_bp, smoke=False, n_random=N_RANDOM, lock_path=None, full_panel_fn=None, allow_final=True):
    """选择阶段只用截到验证段末尾的面板；留下的候选要看最终段时，先用 full_panel_fn(验证段末尾) 预检历史一致(失败不消耗锁)，再拿全局锁，再调用 full_panel_fn(None) 取完整面板，
    只跑这几个候选，且股票池限定为选择面板的股票。full_panel_fn 缺省时用 panel 自己(仅测试)。
    lock_path 为 None 且有候选需要看最终段时直接报错(不允许没有锁地看最终段)。"""
    sel = truncate_panel(panel, VALID[1])
    ok, O, C, V, Cff, dates = _prep(sel, fx)
    cands, series, scores = {}, {}, {}
    grid = candidate_grid(fx, smoke)
    for fid in sorted({g[0] for g in grid}):
        scores[fid] = (fx.compute_factor(fid, sel) * fx.FACTORS[fid].direction).values
    for i, (fid, step, K) in enumerate(grid):
        df = run_portfolio(scores[fid], ok, O, C, V, Cff, dates, step, K, max(1, int(K * DROP_FRAC)), cost_bp)
        cid = f"{fid}|{step}d|K{K}"
        cands[cid] = split_segments(df) if len(df) else {k: df for k in ("train", "valid", "final")}
        series[cid] = df
        if (i + 1) % 10 == 0:
            log(f"候选 {i + 1}/{len(grid)}")

    def final_provider(ids):
        if not allow_final:                                             # 本次运行不看最终段(不拿锁、不载入完整面板)
            return None
        if lock_path is None:
            raise RuntimeError("需要看最终段但没有提供全局锁路径")
        if full_panel_fn:                                               # 锁前预检：只载入到验证段末尾，校验失败不消耗全局锁
            pre = restrict_columns(full_panel_fn(VALID[1]), list(sel.close.columns))
            check_same_history(sel, pre, VALID[1])
        try:
            acquire_final_lock(lock_path, ids)
        except FileExistsError:
            return None
        full = restrict_columns(full_panel_fn(None) if full_panel_fn else panel, list(sel.close.columns))
        check_same_history(sel, full, VALID[1])
        fok, fO, fC, fV, fCff, fdates = _prep(full, fx)
        res = {}
        for cid in ids:
            fid, step, K = cid.split("|")[0], int(cid.split("|")[1][:-1]), int(cid.split("|")[2][1:])
            sc = (fx.compute_factor(fid, full) * fx.FACTORS[fid].direction).values
            d = run_portfolio(sc, fok, fO, fC, fV, fCff, fdates, step, K, max(1, int(K * DROP_FRAC)), cost_bp)
            res[cid] = split_segments(d)["final"] if len(d) else d
        return res

    rep = select_and_test(cands, final_provider)
    if not allow_final:
        rep["final_blocked"], rep["final_skipped"] = False, True
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
        ok_r = ok & np.isfinite(scores[fid])                              # 随机对照沿用真实候选的有效股票池，只随机打分，不改池子
        means = []
        for _ in range(n_random):
            rs = rng.standard_normal(scores[fid].shape)
            d = run_portfolio(rs, ok_r, O, C, V, Cff, dates, step, K, max(1, int(K * DROP_FRAC)), cost_bp, bench_ok=ok)
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
    blocked = "（最终段已被看过，全局锁存在，本次拒绝再看）" if rep.get("final_blocked") else "（本次按设定不看最终段：未拿全局锁，留下的候选等所有者决定）" if rep.get("final_skipped") else ""
    L += ["", f"**结论**：训练段前 {len(rep['top'])} 名里，验证段留下 {len(kept)} 个；最终段存活 {sum(1 for r in kept if r['final'] and r['final']['survive'])} 个，"
          f"其中 t>3.0 的 {sum(1 for r in kept if r['final'] and r['final'].get('survive_hlz'))} 个{blocked}。「存活」用的是 t>2.0，只有 t>3.0 才接近多重检验下的门槛。",
          "", "## 局限", "", "- 名单与历史在之前的检验里全部被看过，这不是真正的样本外，只是新增一批检验；候选数量已计入 PBO/DSR。",
          "- 当前在市名单（幸存者偏差）、ST 用当前名称、后复权价；基准不扣成本只是比较口径；最终段约 2.8 年，样本有限；涨停阈值统一 9.5%。",
          "- 即使「存活」，也只说明未被推翻，不是样本外证明，更不构成投资建议；通过的候选只能进入前向纸面记录继续观察。"]
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--no-final", action="store_true", help="只做到验证段：不拿全局锁、不载入最终段，留下的候选等所有者决定")
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
    panel = build_arrays(codes, fx, allmarket, end=VALID[1])             # 选择阶段只载入到验证段末尾：连「数据够不够 400 行」的筛选都看不到最终段
    log(f"面板 {panel.close.shape}，开始候选搜索")
    lock = out / "FINAL_LOCK_SMOKE.json" if a.smoke else ROOT / "handoff" / "data" / "dev" / "FINAL_LOOKED.json"     # 正式锁在固定全局路径，换输出目录也绕不过
    rep, _ = run_all(panel, fx, cost_bp, smoke=a.smoke, n_random=10 if a.smoke else N_RANDOM, lock_path=lock, allow_final=not (a.no_final or a.smoke),
                    full_panel_fn=None if a.smoke else (lambda end=None: build_arrays(codes, fx, allmarket, end=end)))     # smoke 永远不载入完整面板
    (out / "dev_results.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
    (out / "report.md").write_text(render(rep, len(codes), cost_bp, a.smoke), encoding="utf-8")
    log("完成")


if __name__ == "__main__":
    main()
