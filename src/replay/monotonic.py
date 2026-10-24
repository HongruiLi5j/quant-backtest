"""T008 S2:单因子分组单调性检验(设计文档第 6 节)。

做法:对策略网格里的每个参数组合,每个交易日把截面股票按因子得分分成 n_groups 组(默认 5),
算各组"未来 20 日收益"(T 日收盘出信号,T+1 开盘买,T+20 收盘卖:close[t+20]/open[t+1]-1,
与 scripts/run_factor_scan.py、信号账本口径一致),再对日期取平均,得到"组号 -> 组均收益"。

判定(写死,S4 起不得放宽):同时满足才通过,否则该参数组合淘汰、不进组合:
  1. Spearman(组号, 组均收益) ≥ MONO_RHO_MIN(0.8);
  2. 多空差(最高组 - 最低组)的均值 > 0;
  3. 多空差日序列的块 bootstrap 置信区间下界 > 0(块长 ≥ 20,因为 20 日标签相邻日重叠 19 天,
     按日当独立样本会高估显著性)。
方向:清单 direction 一律 higher_better(得分越高越靠前),不允许事后翻转;对照(random)不做检验。
策略层面:网格里没有任何一个组合通过 -> 整个策略淘汰;部分通过 -> 只有通过的组合有资格进入回放。

防未来函数:
  - 信号日限定在检验段内,且丢弃段内最后 horizon 个交易日(它们的 20 日标签会越过段尾),即禁运;
  - 面板先截到段尾再算因子,因此段后的数据(含封存段)既不进因子也不进标签;
  - 封存段(holdout)拒绝检验,它只能走 T007 LockedTestGuard。

统计提醒(报告里也会写):通过单调性只是进入回放的入场券,不是策略有效的证据;
网格有约 490 个组合,每个都是一次试验,通过者里会混入纯运气,多重检验在 S4 之后处理;
没有扣成本,没有处理涨跌停/停牌,股票池若来自现存股票则有幸存者偏差。
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from . import factors, registry
from ..validation import trials

HORIZON = 20             # 前瞻标签天数
N_GROUPS = 5             # 默认分组数
MIN_GROUPS, MAX_GROUPS = 5, 10   # 设计文档第 6 节:5~10 组;组数太少(如 2 组)时 Spearman 恒为 ±1,会绕过单调性要求
MONO_RHO_MIN = 0.8       # Spearman(组号, 组均收益)下限
MIN_STOCKS_PER_GROUP = 4 # 某日截面有效股票数 < n_groups * 该值 则这一天不参与
MIN_DAYS = trials.MIN_N  # 有效交易日少于 30 天时置信区间不可靠,直接判不通过
BLOCK = 20               # bootstrap 块长(≥ horizon,覆盖标签重叠)
N_BOOT = 1000
MIN_N_BOOT = 200         # 重采样次数硬下限:次数太少(如 2 次)区间会异常窄,可靠"凑"出下界>0 而绕过显著性门槛
RHO_EPS = 1e-9           # 浮点容差:理论上恰好等于 0.8 的秩相关(如 0.7999999999999999)不能被误淘汰
ALPHA = 0.05
SEG_CHECKABLE = ("train", "valid")   # holdout 禁止在这里检验


# ---------------------------------------------------------------- 标签与分组

def forward_returns(panel: dict, horizon: int = HORIZON) -> pd.DataFrame:
    """close[t+h] / open[t+1] - 1(日期 × 代码);价格非正视为缺失;最后 h 行自然为 NaN。"""
    o = pd.concat({k: factors._norm(v)["open"].astype(float) for k, v in panel.items()}, axis=1).sort_index()
    c = pd.concat({k: factors._norm(v)["close"].astype(float) for k, v in panel.items()}, axis=1).sort_index()
    o = o.where(o > 0).shift(-1)
    c = c.where(c > 0).shift(-horizon)
    return (c / o - 1).replace([np.inf, -np.inf], np.nan)


def _rank(a: np.ndarray) -> np.ndarray:
    """平均秩(并列取均值),不依赖 scipy。"""
    return pd.Series(a).rank(method="average").to_numpy()


def spearman(x, y) -> float:
    """Spearman 秩相关;任一方常数或样本 < 2 返回 NaN。"""
    x = np.asarray(x, float)
    y = np.asarray(y, float)
    if len(x) != len(y) or len(x) < 2:
        return float("nan")
    rx, ry = _rank(x), _rank(y)
    if rx.std() == 0 or ry.std() == 0:
        return float("nan")
    return float(np.corrcoef(rx, ry)[0, 1])


def assign_groups(score: np.ndarray, n_groups: int) -> np.ndarray:
    """按得分升序分成 n_groups 组(0=最低分,n-1=最高分),各组股票数尽量相等。
    得分并列时按位置先后切开(结果可复现);调用方要保证得分不全相同。"""
    m = len(score)
    order = pd.Series(score).rank(method="first").to_numpy().astype(int) - 1     # 0..m-1
    return (order * n_groups // m).astype(int)


def _check_groups(n_groups) -> None:
    if isinstance(n_groups, bool) or not isinstance(n_groups, int) or not (MIN_GROUPS <= n_groups <= MAX_GROUPS):
        raise ValueError(f"分组数必须是 {MIN_GROUPS}..{MAX_GROUPS} 的整数,收到 {n_groups!r}")


def _check_boot(n_boot) -> None:
    if isinstance(n_boot, bool) or not isinstance(n_boot, int) or n_boot < MIN_N_BOOT:
        raise ValueError(f"bootstrap 次数必须是 ≥{MIN_N_BOOT} 的整数,收到 {n_boot!r}")


def daily_group_returns(score: pd.DataFrame, fwd: pd.DataFrame, dates, n_groups: int = N_GROUPS):
    """返回 (组均收益 DataFrame[日期 × 组], 每日截面 Spearman IC Series)。
    当天有效股票数不足或得分不足 n_groups 个不同取值的日子被跳过。"""
    _check_groups(n_groups)
    rows, ics, kept = [], [], []
    cols = score.columns.intersection(fwd.columns)
    for d in dates:
        if d not in score.index or d not in fwd.index:
            continue
        s = score.loc[d, cols].to_numpy(float)
        f = fwd.loc[d, cols].to_numpy(float)
        ok = np.isfinite(s) & np.isfinite(f)
        if ok.sum() < n_groups * MIN_STOCKS_PER_GROUP:
            continue
        s, f = s[ok], f[ok]
        if len(np.unique(s)) < n_groups:
            continue
        g = assign_groups(s, n_groups)
        rows.append([f[g == k].mean() for k in range(n_groups)])
        ics.append(spearman(s, f))
        kept.append(d)
    return (pd.DataFrame(rows, index=kept, columns=range(n_groups), dtype=float),
            pd.Series(ics, index=kept, dtype=float))


def segment_signal_dates(index: pd.DatetimeIndex, start, end, horizon: int = HORIZON):
    """检验段内可用的信号日:丢掉段内最后 horizon 个交易日(禁运,标签会越过段尾)。"""
    inside = index[(index >= pd.Timestamp(start)) & (index <= pd.Timestamp(end))]
    return inside[:-horizon] if len(inside) > horizon else inside[:0]


# ---------------------------------------------------------------- 判定

def judge(group_means: pd.DataFrame, spread: pd.Series, n_boot: int = N_BOOT, block: int = BLOCK,
          seed: int = 0, alpha: float = ALPHA) -> dict:
    """由"日 × 组"收益表给出单调性统计与淘汰结论。不单调/多空差不显著 -> passed=False。"""
    _check_boot(n_boot)
    n_days = len(group_means)
    k = group_means.shape[1] if n_days else 0
    out = {"n_days": n_days, "n_groups": k, "group_mean": [], "rho": float("nan"),
           "spread_mean": float("nan"), "spread_ci_lower": float("nan"), "spread_ci_upper": float("nan"),
           "passed": False, "reasons": []}
    if n_days < MIN_DAYS:
        out["reasons"].append(f"有效交易日 {n_days} < {MIN_DAYS},样本不足")
        return out
    gm = group_means.mean(axis=0).to_numpy(float)
    out["group_mean"] = [float(v) for v in gm]
    out["rho"] = spearman(np.arange(k), gm)
    out["spread_mean"] = float(spread.mean())
    if not (out["rho"] >= MONO_RHO_MIN - RHO_EPS):
        out["reasons"].append(f"不单调:Spearman={out['rho']:.3f} < {MONO_RHO_MIN}")
    if not (out["spread_mean"] > 0):
        out["reasons"].append(f"多空差均值 {out['spread_mean']:.5f} ≤ 0")
    if out["reasons"]:
        return out      # 已经淘汰,省掉昂贵的 bootstrap
    ci = trials.mean_ci(spread.to_list(), block=block, n_boot=n_boot, seed=seed, alpha=alpha)
    out["spread_ci_lower"], out["spread_ci_upper"] = ci["lower"], ci["upper"]
    if not (ci["lower"] > 0):
        out["reasons"].append(f"多空差 bootstrap 置信区间下界 {ci['lower']:.5f} ≤ 0(块长 {block})")
    out["passed"] = not out["reasons"]
    return out


def check_factor(score: pd.DataFrame, fwd: pd.DataFrame, start, end, n_groups: int = N_GROUPS,
                 horizon: int = HORIZON, n_boot: int = N_BOOT, seed: int = 0) -> dict:
    """对一张得分表(日期 × 代码)做单调性检验。fwd 必须是 forward_returns(同一面板)。"""
    dates = segment_signal_dates(score.index, start, end, horizon)
    gm, ic = daily_group_returns(score, fwd, dates, n_groups)
    spread = (gm[n_groups - 1] - gm[0]) if len(gm) else pd.Series(dtype=float)
    res = judge(gm, spread, n_boot=n_boot, seed=seed)
    res["ic_mean"] = float(ic.mean()) if len(ic) else float("nan")
    return res


def check_strategy(strategy_id: str, panel: dict, segment: str = "train", n_groups: int = N_GROUPS,
                   industry: dict | None = None, n_boot: int = N_BOOT, seed: int = 0,
                   catalog: dict | None = None, fwd: pd.DataFrame | None = None) -> dict:
    """检验一个策略的全部网格组合。返回
    {id, direction, status(checked/skipped_control), n_params, n_passed, eliminated, params:[...]}。
    eliminated = 没有任何组合通过;params 里每个组合带 passed/reasons,未通过的不进组合。"""
    if segment not in SEG_CHECKABLE:
        raise ValueError(f"段 {segment!r} 不能在这里检验(封存段只能走 LockedTestGuard);可选 {SEG_CHECKABLE}")
    _check_groups(n_groups)
    _check_boot(n_boot)
    cat = catalog if catalog is not None else registry.load_validated()[0]
    spec = next((s for s in cat["strategies"] if s["id"] == strategy_id), None)
    if spec is None:
        raise KeyError(f"清单里没有策略 {strategy_id}")
    direction = spec["direction"]
    if direction == "random":
        return {"id": strategy_id, "direction": direction, "status": "skipped_control",
                "n_params": 0, "n_passed": 0, "eliminated": False, "params": []}
    if direction != "higher_better":
        raise ValueError(f"{strategy_id} 的 direction={direction!r},不允许事后翻转方向")
    start, end = cat["segments"][segment]
    # 面板先截到段尾:段后数据(含封存段)不进因子也不进标签
    cut = {k: factors._norm(v).loc[lambda d: d.index <= pd.Timestamp(end)] for k, v in panel.items()}
    f = fwd if fwd is not None else forward_returns(cut)
    rows = []
    for p in factors.iter_param_grid(strategy_id):
        sc = factors.factor_frame(strategy_id, cut, p, industry)
        r = check_factor(sc, f, start, end, n_groups, n_boot=n_boot, seed=seed)
        r["params"] = p
        rows.append(r)
    n_pass = sum(r["passed"] for r in rows)
    return {"id": strategy_id, "direction": direction, "status": "checked", "n_params": len(rows),
            "n_passed": n_pass, "eliminated": n_pass == 0, "params": rows}


def check_all(panel: dict, segment: str = "train", n_groups: int = N_GROUPS, industry: dict | None = None,
              n_boot: int = N_BOOT, seed: int = 0, only: list | None = None) -> dict:
    """检验清单里所有(或 only 指定的)策略,汇总成报告字典。"""
    _check_groups(n_groups)
    _check_boot(n_boot)
    cat, h = registry.load_validated()
    start, end = cat["segments"][segment]
    cut = {k: factors._norm(v).loc[lambda d: d.index <= pd.Timestamp(end)] for k, v in panel.items()}
    f = forward_returns(cut)
    ids = only if only else registry.strategy_ids(cat)
    results = []
    for sid in ids:
        try:
            results.append(check_strategy(sid, cut, segment, n_groups, industry, n_boot, seed, cat, f))
        except ValueError as e:      # 例如行业类因子缺 industry 映射:如实记为未检验,不当作通过
            results.append({"id": sid, "status": "error", "error": str(e), "eliminated": None,
                            "n_params": 0, "n_passed": 0, "params": []})
    return {"catalog_hash": h, "segment": segment, "segment_range": [start, end], "n_groups": n_groups,
            "horizon": HORIZON, "seed": seed, "rule": {"rho_min": MONO_RHO_MIN, "spread_gt": 0, "ci_lower_gt": 0,
                                         "block": BLOCK, "n_boot": n_boot, "alpha": ALPHA},
            "total_grid_trials": registry.total_trials(cat), "results": results}
