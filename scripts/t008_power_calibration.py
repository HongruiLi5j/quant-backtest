"""T008 回放规则 1(随机对照)与规则 4(单调性)的检验功效校准(纯合成数据,任务 C01)。

目的:项目交接里说"规则 4 的纯噪声通过率没校准过,不能据 6.2% 判断有没有选股信息"。这里用纯合成数据回答:
  1. 因子是纯噪声时,规则 1 / 规则 4 的通过率(= 第一类错误率或其补数)是多少,95% 置信区间多宽;
  2. 因子带已知强度的真信号(截面秩相关 IC = 0.02/0.05,规则 1 另加负 IC)时,规则能抓住它的概率(检验功效)。

做法:
  - 每次重复生成一个"合成市场":N 只股票、T 个交易日的独立随机收益(单因子市场模型:r = β·市场 + 个股噪声);
    不读任何真实行情,所以不构成对训练段/验证段的数据窥探。
  - 判定一律导入 src/replay 里的真实代码,不改它、不重写它:
      规则 4:monotonic.forward_returns(20 日标签)+ monotonic.check_factor(分组、Spearman、多空差、块 bootstrap);
      规则 1:selection._check_rows(行校验)+ selection._score_vs_null(匹配抽样零分布、周聚合、第 5 百分位);
             触发条件 score < null_p5 - EPS 是 selection.train_eliminate 里的一行比较(第 542 行),这里照抄同一个式子,
             因为 train_eliminate 会写 TrialLog 和冻结文件,不能为模拟去调它。
  - 植入信号的方式(只在合成世界里用"未来收益"造因子,这是已知答案的实验设计,不是被检验代码的未来函数):
      因子 = a·标准化(该规则自己的标签) + √(1-a²)·噪声,a = 2·sin(π·IC/6),
      使正态假设下因子与标签的 Spearman 秩相关等于目标 IC;实际实现的 IC 也从模拟里读出来报告。
      规则 1 的标签 = T+1 开盘 → T+2 开盘的 1 日收益;规则 4 的标签 = close[T+20]/open[T+1]-1。

安全:
  - 不调用 train_eliminate / validate_survivors / holdout_report,不写任何账本;
  - 仍按任务要求把环境变量 T008_TRIAL_LOG_PATH 指向输出目录下的临时文件(双保险);
  - 运行前后记录 handoff/data/replay 下三个固定账本/冻结文件的存在性与 SHA-256,写进 ledger_proof.json;
  - 输出目录不允许落在仓库的 data/ 或 handoff/ 里;不联网。

用法(示例):
  .venv/bin/python scripts/t008_power_calibration.py --reps 200 --workers 7 --out <目录>/batch1
  .venv/bin/python scripts/t008_power_calibration.py --reps 300 --rep-start 200 --ics 0 --out <目录>/batch2
  .venv/bin/python scripts/t008_power_calibration.py --merge-from <目录>/batch1,<目录>/batch2 --out <目录>
  .venv/bin/python scripts/t008_power_calibration.py --quick --out /tmp/c01_quick
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import time
import warnings
from multiprocessing import get_context
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = Path("/Users/lihongrui/Documents/AQ-o44/run/outputs/C01")
TRIAL_LOG_ENV = "T008_TRIAL_LOG_PATH"

# 必须在导入 src.replay 之前设好:即使哪天有代码误写账本,也只会写到临时文件
if not os.environ.get(TRIAL_LOG_ENV):
    os.environ[TRIAL_LOG_ENV] = str(DEFAULT_OUT / "tmp_trial_log.jsonl")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np            # noqa: E402
import pandas as pd           # noqa: E402

from src.replay import monotonic, selection   # noqa: E402

# ---------------------------------------------------------------- 合成市场的默认参数(贴近正式回放的配置)
N_STOCKS = 500        # 正式回放 --universe 500
N_DAYS = 363          # 训练段 2024-01-01~2025-06-30 约 363 个 A 股交易日
TOP_N = 20            # 正式回放 --top-n 20(规则 1 的候选每天选 k=20 只)
NULL_K = 1000         # 正式回放 --null-k 1000
N_BOOT = 300          # 正式回放 --n-boot 300(scripts/night_runner.py)
SIGMA_IDIO = 0.02     # 个股日特质波动 2%
SIGMA_MKT = 0.012     # 市场日波动 1.2%
BETA_SD = 0.3         # β ~ N(1, 0.3²):让组间多空差也带一点市场暴露的噪声
LIMIT = 0.10          # 日收益截在 ±10%(近似涨跌停,也保证价格为正)
AR_PHI = 0.98         # "持续型"噪声因子的日自相关(半衰期约 34 天,接近动量/估值类因子的慢变)
IC_VOL = 0.10         # "时变 IC"变体:每日 IC 围绕均值的标准差
IC_VOL_PHI = 0.95     # 时变 IC 的日自相关
SEG_START = "2024-01-02"
ALPHA_CI = 0.05


def ic_to_loading(ic: float) -> float:
    """目标 Spearman 秩相关 -> 正态模型里的 Pearson 载荷 a:ρ_s = (6/π)·arcsin(a/2) 的反函数。"""
    return 2.0 * math.sin(math.pi * ic / 6.0)


def wilson_ci(k: int, n: int, alpha: float = ALPHA_CI) -> tuple:
    """二项比例的 Wilson 置信区间(比"p±1.96·√(p(1-p)/n)"在 p 接近 0 或 1 时更可靠)。"""
    if n <= 0:
        return (float("nan"), float("nan"))
    z = _norm_ppf(1 - alpha / 2)
    p = k / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (max(0.0, c - h), min(1.0, c + h))


def _norm_ppf(q: float) -> float:
    """标准正态分位数(二分法求解 Φ(x)=q,不依赖 scipy)。"""
    lo, hi = -10.0, 10.0
    for _ in range(100):
        mid = (lo + hi) / 2
        if 0.5 * (1 + math.erf(mid / math.sqrt(2))) < q:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def binom_sf(k: int, n: int, p: float) -> float:
    """P(X ≥ k),X ~ Binomial(n, p)。只用于"假如 485 个组合互相独立"的示意。"""
    return float(sum(math.comb(n, j) * p ** j * (1 - p) ** (n - j) for j in range(k, n + 1)))


# ---------------------------------------------------------------- 合成世界

def simulate_returns(rng, n_stocks: int, n_days: int) -> np.ndarray:
    """r[t, i] = 第 t 天收盘相对前一天收盘的收益;各股之间只通过市场因子相关,时间上独立。"""
    beta = 1.0 + BETA_SD * rng.standard_normal(n_stocks)
    mkt = SIGMA_MKT * rng.standard_normal(n_days)
    r = mkt[:, None] * beta[None, :] + SIGMA_IDIO * rng.standard_normal((n_days, n_stocks))
    return np.clip(r, -LIMIT, LIMIT)


def build_panel(r: np.ndarray, dates: pd.DatetimeIndex, codes: list) -> dict:
    """收益 -> 面板 {代码: OHLCV};open[t] = close[t-1](无隔夜跳空),于是
    规则 1 的持有期收益 open[t+2]/open[t+1]-1 = r[t+1],规则 4 的标签 close[t+20]/open[t+1]-1 = close[t+20]/close[t]-1。"""
    close = 10.0 * np.cumprod(1.0 + r, axis=0)
    open_ = np.vstack([np.full((1, r.shape[1]), 10.0), close[:-1]])
    out = {}
    for j, c in enumerate(codes):
        hi = np.maximum(open_[:, j], close[:, j])
        lo = np.minimum(open_[:, j], close[:, j])
        out[c] = pd.DataFrame({"open": open_[:, j], "high": hi, "low": lo, "close": close[:, j],
                               "volume": 1e6}, index=dates)
    return out


def noise_matrix(rng, n_days: int, n_stocks: int, persist: float) -> np.ndarray:
    """因子的噪声部分:persist=0 为每日独立;否则为平稳 AR(1),边际方差 1。"""
    e = rng.standard_normal((n_days, n_stocks))
    if persist <= 0:
        return e
    out = np.empty_like(e)
    out[0] = e[0]
    s = math.sqrt(1 - persist * persist)
    for t in range(1, n_days):
        out[t] = persist * out[t - 1] + s * e[t]
    return out


def ic_path(rng, n_days: int, ic: float, ic_vol: float) -> np.ndarray:
    """每日目标 IC:ic_vol=0 时恒为 ic;否则 ic + ic_vol·u_t,u 为日自相关 IC_VOL_PHI 的 AR(1)。"""
    if ic_vol <= 0:
        return np.full(n_days, ic)
    u = noise_matrix(rng, n_days, 1, IC_VOL_PHI)[:, 0]
    return np.clip(ic + ic_vol * u, -0.9, 0.9)


def _zrow(x: np.ndarray) -> np.ndarray:
    """逐行截面标准化;整行缺失的返回全 0(这些日子不植入信号)。"""
    with warnings.catch_warnings(), np.errstate(invalid="ignore"):
        warnings.simplefilter("ignore", RuntimeWarning)      # 整行 NaN 时 nanmean 的"空切片"提示
        mu = np.nanmean(x, axis=1, keepdims=True)
        sd = np.nanstd(x, axis=1, keepdims=True)
    z = (x - mu) / np.where(sd > 0, sd, 1.0)
    return np.nan_to_num(z, nan=0.0)


def plant_factor(label: np.ndarray, noise: np.ndarray, ics: np.ndarray) -> np.ndarray:
    """因子 = a_t·z(标签_t) + √(1-a_t²)·噪声_t。标签缺失的行(段尾)a 取 0。"""
    a = np.array([ic_to_loading(v) for v in ics])
    has = np.isfinite(label).any(axis=1)
    a = np.where(has, a, 0.0)[:, None]
    return a * _zrow(label) + np.sqrt(1 - a * a) * noise


def _rank_ic(f: np.ndarray, y: np.ndarray) -> float:
    """逐日截面 Spearman 的均值(只用于报告"实际实现的 IC")。"""
    fr = pd.DataFrame(f).rank(axis=1).to_numpy()
    yr = pd.DataFrame(y).rank(axis=1).to_numpy()
    fr = fr - fr.mean(axis=1, keepdims=True)
    yr = yr - yr.mean(axis=1, keepdims=True)
    num = (fr * yr).sum(axis=1)
    den = np.sqrt((fr * fr).sum(axis=1) * (yr * yr).sum(axis=1))
    return float(np.mean(num / den))


# ---------------------------------------------------------------- 两条规则(调用真实判定代码)

def rule1_eval(factor: np.ndarray, r: np.ndarray, dates: pd.DatetimeIndex, codes: list, top_n: int,
               null_k: int, seed: str) -> dict:
    """规则 1:候选每天选因子最高的 top_n 只等权,持有 T+1 开盘 → T+2 开盘;成本两边都取 0(匹配成本时相互抵消)。
    返回是否触发淘汰(score < 零分布第 5 百分位)及统计量。"""
    n_days, n = r.shape
    n_sig = n_days - 2                           # next_date = T+2 必须在段内(否则 rows_for_segment 会禁运丢弃)
    ds = [d.strftime("%Y-%m-%d") for d in dates]
    hold = r[1:n_sig + 1]                        # hold[t] = r[t+1] = 信号日 t 的持有期收益
    rows = []
    for t in range(n_sig):
        top = np.argpartition(-factor[t], top_n - 1)[:top_n]
        gross = float(hold[t, top].mean())
        mkt = float(hold[t].mean())
        rows.append({"key": "c01_sim", "date": ds[t], "next_date": ds[t + 2], "net": gross, "gross": gross,
                     "cost": 0.0, "mkt": mkt, "excess": gross - mkt,
                     "weights": {codes[i]: 1.0 / top_n for i in top}, "shares": {codes[i]: 1 for i in top}})
    selection._check_rows(rows)                  # 与正式流程同一套行校验(含 excess = net - mkt)
    panel = selection.NullPanel(ret=pd.DataFrame(hold, index=ds[:n_sig], columns=codes),
                                tradable_mask=pd.DataFrame(True, index=ds[:n_sig], columns=codes))
    ctrl_cost = {d: 0.0 for d in ds[:n_sig]}
    r1 = selection._score_vs_null(rows, panel, ctrl_cost, seed, null_k)
    trig = bool(r1["evaluable"] and r1["score"] < r1["null_p5"] - selection.EPS)   # 同 train_eliminate 第 542 行
    return {"r1_evaluable": bool(r1["evaluable"]), "r1_triggered": trig, "r1_pass": not trig,
            "r1_score": r1.get("score"), "r1_null_p5": r1.get("null_p5"), "r1_p_value": r1.get("p_value"),
            "r1_weeks": r1.get("weeks"), "r1_ic_real": _rank_ic(factor[:n_sig], hold)}


def rule4_eval(score: pd.DataFrame, fwd: pd.DataFrame, n_boot: int) -> dict:
    """规则 4:直接调用 monotonic.check_factor(5 组、20 日标签、禁运、块长 20 的 bootstrap)。"""
    start, end = score.index[0], score.index[-1]
    res = monotonic.check_factor(score, fwd, start, end, monotonic.N_GROUPS, n_boot=n_boot, seed=0)
    rho_ok = bool(res["rho"] >= monotonic.MONO_RHO_MIN - monotonic.RHO_EPS)
    spread_ok = bool(res["spread_mean"] > 0)
    return {"r4_pass": bool(res["passed"]), "r4_rho_ok": rho_ok, "r4_spread_ok": spread_ok,
            "r4_rho_and_spread_ok": rho_ok and spread_ok, "r4_rho": res["rho"], "r4_spread_mean": res["spread_mean"],
            "r4_ci_lower": res["spread_ci_lower"], "r4_n_days": res["n_days"], "r4_ic_real": res["ic_mean"]}


# ---------------------------------------------------------------- 场景与单次重复

def scenarios(quick: bool = False) -> list:
    """场景表。rule: r1 / r4;persist: 噪声因子日自相关;ic_vol: 时变 IC 的标准差;joint: IC=0 时同一因子也跑规则 1。
    上一次全量 21 组 × 1000 次跑满 70 分钟超时,这次只保留任务要求的 IC(0、0.02、0.05)和规则 1 的对称负 IC。"""
    if quick:
        return [{"name": "r1_iid", "rule": "r1", "persist": 0.0, "ic_vol": 0.0, "ics": [0.0, -0.3]},
                {"name": "r4_iid", "rule": "r4", "persist": 0.0, "ic_vol": 0.0, "ics": [0.0, 0.3], "joint": True}]
    return [
        {"name": "r1_iid", "rule": "r1", "persist": 0.0, "ic_vol": 0.0, "ics": [-0.05, -0.02, 0.0, 0.02, 0.05]},
        {"name": "r4_iid", "rule": "r4", "persist": 0.0, "ic_vol": 0.0, "ics": [0.0, 0.02, 0.05], "joint": True},
        {"name": "r4_ar98", "rule": "r4", "persist": AR_PHI, "ic_vol": 0.0, "ics": [0.0, 0.02, 0.05], "joint": True},
        {"name": "r4_ar98_icvol", "rule": "r4", "persist": AR_PHI, "ic_vol": IC_VOL, "ics": [0.0, 0.02, 0.05],
         "optional": True},     # 只在 --only 点名时跑
    ]


def run_one(task: dict) -> dict:
    """一次重复:新的合成市场 + 新的因子,跑对应规则。种子由 (场景序号, IC 序号, 重复号) 决定,可复现。"""
    t0 = time.time()
    ss = np.random.SeedSequence(entropy=task["base_seed"], spawn_key=(task["s_idx"], task["ic_idx"], task["rep"]))
    rng = np.random.default_rng(ss)
    n, T = task["n_stocks"], task["n_days"]
    dates = pd.bdate_range(SEG_START, periods=T)
    codes = [f"S{i:04d}" for i in range(n)]
    r = simulate_returns(rng, n, T)
    noise = noise_matrix(rng, T, n, task["persist"])
    ics = ic_path(rng, T, task["ic"], task["ic_vol"])
    out = {k: task[k] for k in ("scenario", "rule", "ic", "persist", "ic_vol", "rep")}
    seed = f"C01|{task['scenario']}|{task['ic']}|{task['rep']}"
    if task["rule"] == "r1":
        label = np.full_like(r, np.nan)
        label[:-1] = r[1:]                       # 规则 1 的标签:信号日 t 的 1 日持有期收益 r[t+1]
        f = plant_factor(label, noise, ics)
        out.update(rule1_eval(f, r, dates, codes, task["top_n"], task["null_k"], seed))
    else:
        panel = build_panel(r, dates, codes)
        fwd = monotonic.forward_returns(panel)   # 真实代码算 20 日标签
        f = plant_factor(fwd.to_numpy(float), noise, ics)
        score = pd.DataFrame(f, index=fwd.index, columns=fwd.columns)
        out.update(rule4_eval(score, fwd, task["n_boot"]))
        if task.get("joint") and task["ic"] == 0.0:   # 纯噪声时同一个因子再过规则 1,得到"两条都过"的联合通过率
            out.update(rule1_eval(f, r, dates, codes, task["top_n"], task["null_k"], seed))
    out["seconds"] = round(time.time() - t0, 3)
    return out


# ---------------------------------------------------------------- 账本证明与汇总

def ledger_state() -> dict:
    """三个固定路径(淘汰锚点 TrialLog、冻结文件、封存段配对账本)的存在性与 SHA-256;只读。"""
    out = {}
    for p in (selection.TRIAL_LOG_DEFAULT, selection.FREEZE_DEFAULT, selection.LEDGER_DEFAULT):
        p = Path(p)
        if p.exists():
            out[str(p)] = {"exists": True, "size": p.stat().st_size, "mtime": p.stat().st_mtime,
                           "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
        else:
            out[str(p)] = {"exists": False}
    return out


def _check_out(out: Path) -> None:
    """输出目录不得落在仓库的 data/ 或 handoff/ 里。"""
    o = out.resolve()
    for bad in (ROOT / "data", ROOT / "handoff"):
        b = bad.resolve()
        if o == b or b in o.parents:
            raise SystemExit(f"输出目录不能放在 {b} 里: {o}")


def summarize(df: pd.DataFrame) -> pd.DataFrame:
    """按 (场景, IC) 汇总各指标的比例与 Wilson 95% 置信区间。"""
    rows = []
    metrics = ["r1_pass", "r1_triggered", "r4_pass", "r4_rho_ok", "r4_spread_ok", "r4_rho_and_spread_ok"]
    for (sc, ic), g in df.groupby(["scenario", "ic"], sort=False):
        base = {"scenario": sc, "rule": g["rule"].iloc[0], "ic_target": ic, "persist": g["persist"].iloc[0],
                "ic_vol": g["ic_vol"].iloc[0], "reps": len(g)}
        for m in metrics:
            if m in g and g[m].notna().any():
                v = g[m].dropna().astype(bool)
                lo, hi = wilson_ci(int(v.sum()), len(v))
                base.update({f"{m}_count": int(v.sum()), f"{m}_n": len(v), f"{m}_rate": v.mean(),
                             f"{m}_ci_lo": lo, f"{m}_ci_hi": hi})
        j = g.dropna(subset=["r1_pass", "r4_pass"]) if {"r1_pass", "r4_pass"} <= set(g) else g.iloc[:0]
        if len(j):                               # 只有同一因子两条规则都跑过的行才算联合通过率
            v = j["r1_pass"].astype(bool) & j["r4_pass"].astype(bool)
            lo, hi = wilson_ci(int(v.sum()), len(v))
            base.update({"joint_pass_count": int(v.sum()), "joint_pass_rate": v.mean(),
                         "joint_pass_ci_lo": lo, "joint_pass_ci_hi": hi})
        for c in ("r1_ic_real", "r4_ic_real"):
            if c in g and g[c].notna().any():
                base[f"{c}_mean"] = g[c].mean()
        if "r1_p_value" in g and g["r1_p_value"].notna().any():
            base["r1_p_le_0.05_rate"] = float((g["r1_p_value"].dropna() <= 0.05).mean())
        rows.append(base)
    return pd.DataFrame(rows)


def plot(summary: pd.DataFrame, path: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams["font.sans-serif"] = ["Arial Unicode MS", "PingFang SC", "Heiti SC", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.6))
    s1 = summary[summary["rule"] == "r1"]
    ax = axes[0]
    if len(s1):
        x, y = s1["ic_target"], s1["r1_triggered_rate"]
        ax.errorbar(x, y, yerr=[y - s1["r1_triggered_ci_lo"], s1["r1_triggered_ci_hi"] - y], marker="o", capsize=3,
                    color="#3b6fb6", label="规则 1 触发淘汰的比例")
        ax.axhline(0.05, color="#999", ls="--", lw=1, label="名义 5%")
    ax.set_title("规则 1(随机对照,低尾):淘汰概率 vs IC")
    ax.set_xlabel("植入的日截面 IC(1 日持有期)")
    ax.set_ylabel("比例")
    ax.set_ylim(0, 1.02)
    ax.legend(fontsize=8)
    ax = axes[1]
    colors = {"r4_iid": "#3b6fb6", "r4_ar98": "#d9822b", "r4_ar98_icvol": "#5a9e57"}
    labels = {"r4_iid": "噪声每日独立", "r4_ar98": "噪声持续(AR φ=0.98)", "r4_ar98_icvol": "持续噪声 + IC 时变(sd 0.10)"}
    for sc, g in summary[summary["rule"] == "r4"].groupby("scenario", sort=False):
        x, y = g["ic_target"], g["r4_pass_rate"]
        ax.errorbar(x, y, yerr=[y - g["r4_pass_ci_lo"], g["r4_pass_ci_hi"] - y], marker="o", capsize=3,
                    color=colors.get(sc), label=labels.get(sc, sc))
    ax.axhline(0.062, color="#c0392b", ls=":", lw=1, label="正式回放 30/485=6.2%")
    ax.set_title("规则 4(单调性):通过概率 vs IC")
    ax.set_xlabel("植入的日截面 IC(20 日标签)")
    ax.set_ylabel("通过比例")
    ax.set_ylim(0, 1.02)
    ax.legend(fontsize=8)
    fig.text(0.5, 0.005, "纯合成数据;误差线为 Wilson 95% 置信区间", ha="center", fontsize=8, color="#666")
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    fig.savefig(path, dpi=130)
    plt.close(fig)


def finalize(df: pd.DataFrame, out: Path, names: list, meta: dict) -> pd.DataFrame:
    """写 raw_results.csv / summary.csv / run_meta.json / power_curves.png;返回汇总表。"""
    df = df.sort_values(["scenario", "ic", "rep"], kind="stable").reset_index(drop=True)
    df.to_csv(out / "raw_results.csv", index=False)
    order = [n for n in names if n in set(df["scenario"])]
    summary = summarize(df.set_index("scenario", drop=False).loc[order].reset_index(drop=True))
    summary.to_csv(out / "summary.csv", index=False)
    noise4 = summary[(summary["rule"] == "r4") & (summary["ic_target"] == 0.0)]
    meta["constants"] = {"N_STOCKS": N_STOCKS, "N_DAYS": N_DAYS, "TOP_N": TOP_N, "NULL_K": NULL_K, "N_BOOT": N_BOOT,
                         "SIGMA_IDIO": SIGMA_IDIO, "SIGMA_MKT": SIGMA_MKT, "BETA_SD": BETA_SD, "LIMIT": LIMIT,
                         "AR_PHI": AR_PHI, "IC_VOL": IC_VOL, "IC_VOL_PHI": IC_VOL_PHI, "SEG_START": SEG_START,
                         "monotonic.N_GROUPS": monotonic.N_GROUPS, "monotonic.HORIZON": monotonic.HORIZON,
                         "monotonic.BLOCK": monotonic.BLOCK, "monotonic.MONO_RHO_MIN": monotonic.MONO_RHO_MIN,
                         "selection.NULL_PCT": selection.NULL_PCT, "selection.MIN_WEEKS": selection.MIN_WEEKS}
    meta["real_run_comparison"] = {
        "note": "正式回放数字来自 项目交接.md(规则 4 通过 30/485,规则 1 触发 13/485);二项尾概率假设 485 个组合相互独立,"
                "而网格相邻参数高度相关,所以只是示意,不是有效检验",
        "rule4_real_pass": [30, 485], "rule1_real_trigger": [13, 485],
        "binom_sf_rule4_ge30_given_noise_rate": {r.scenario: binom_sf(30, 485, float(r.r4_pass_rate))
                                                 for r in noise4.itertuples()}}
    (out / "run_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    try:
        plot(summary, out / "power_curves.png")
    except Exception as e:      # 画图失败不影响数字结果
        print(f"画图失败: {e}", flush=True)
    return summary


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="T008 规则 1/4 纯噪声通过率与检验功效(合成数据)")
    ap.add_argument("--reps", type=int, default=200)
    ap.add_argument("--rep-start", type=int, default=0, help="重复号起点;分批续跑时用,种子按重复号生成,不会与前一批重复")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--n-stocks", type=int, default=N_STOCKS)
    ap.add_argument("--n-days", type=int, default=N_DAYS)
    ap.add_argument("--top-n", type=int, default=TOP_N)
    ap.add_argument("--null-k", type=int, default=NULL_K)
    ap.add_argument("--n-boot", type=int, default=N_BOOT)
    ap.add_argument("--seed", type=int, default=20261029)
    ap.add_argument("--only", default="", help="只跑这些场景(逗号分隔)")
    ap.add_argument("--ics", default="", help="只跑这些 IC(逗号分隔,如 0 或 0,0.02)")
    ap.add_argument("--merge-from", default="", help="不模拟,只合并这些目录下的 raw_results.csv 再汇总(逗号分隔)")
    ap.add_argument("--quick", action="store_true", help="小规模自检(测试用)")
    a = ap.parse_args(argv)
    _check_out(a.out)
    a.out.mkdir(parents=True, exist_ok=True)
    before = ledger_state()
    all_scs = scenarios(a.quick)
    names = [s["name"] for s in all_scs]
    meta = {"args": {k: str(v) for k, v in vars(a).items()}}
    t0 = time.time()
    if a.merge_from:
        parts = [pd.read_csv(Path(p) / "raw_results.csv") for p in a.merge_from.split(",") if p]
        df = pd.concat(parts, ignore_index=True)
        dup = int(df.duplicated(subset=["scenario", "ic", "rep"]).sum())
        if dup:
            raise SystemExit(f"合并的批次里有 {dup} 行 (场景, IC, 重复号) 重复,种子会相同,拒绝合并")
        meta["merged_from"] = a.merge_from.split(",")
    else:
        if a.only:
            scs = [s for s in all_scs if s["name"] in a.only.split(",")]
        else:
            scs = [s for s in all_scs if not s.get("optional")]
        keep_ics = {float(x) for x in a.ics.split(",")} if a.ics else None
        tasks = []
        for s in scs:
            si = names.index(s["name"])                # 场景序号固定取全表位置,分批/筛选不改变种子
            for ii, ic in enumerate(s["ics"]):
                if keep_ics is not None and float(ic) not in keep_ics:
                    continue
                for rep in range(a.rep_start, a.rep_start + a.reps):
                    tasks.append({"scenario": s["name"], "rule": s["rule"], "persist": s["persist"],
                                  "ic_vol": s["ic_vol"], "joint": s.get("joint", False), "ic": ic, "rep": rep,
                                  "s_idx": si, "ic_idx": ii, "base_seed": a.seed, "n_stocks": a.n_stocks,
                                  "n_days": a.n_days, "top_n": a.top_n, "null_k": a.null_k, "n_boot": a.n_boot})
        print(f"任务数 {len(tasks)},进程 {a.workers},输出 {a.out}", flush=True)
        results = []
        if a.workers <= 1:
            results = [run_one(t) for t in tasks]
        else:
            with get_context("spawn").Pool(a.workers) as pool:
                for i, res in enumerate(pool.imap_unordered(run_one, tasks, chunksize=2), 1):
                    results.append(res)
                    if i % 100 == 0 or i == len(tasks):
                        print(f"  {i}/{len(tasks)} 完成,用时 {time.time() - t0:.0f}s", flush=True)
        df = pd.DataFrame(results)
        meta.update({"scenarios": scs, "n_tasks": len(tasks)})
    meta["elapsed_sec"] = round(time.time() - t0, 1)
    summary = finalize(df, a.out, names, meta)
    after = ledger_state()
    tmp_log = Path(os.environ[TRIAL_LOG_ENV])
    proof = {"trial_log_env": os.environ[TRIAL_LOG_ENV], "tmp_trial_log_exists_after": tmp_log.exists(),
             "before": before, "after": after, "unchanged": before == after}
    (a.out / "ledger_proof.json").write_text(json.dumps(proof, ensure_ascii=False, indent=1), encoding="utf-8")
    print(summary.to_string(), flush=True)
    print(f"账本前后一致: {proof['unchanged']};总用时 {time.time() - t0:.0f}s", flush=True)
    return 0 if proof["unchanged"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
