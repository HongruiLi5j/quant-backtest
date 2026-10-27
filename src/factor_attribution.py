"""因子归因（T037）：把一份**美股**日收益序列对 Fama-French 因子做回归，看收益有多少能被市场/规模/价值/动量解释。

只做纯计算：输入日期、收益、已加载的因子字典（src/extdata.load_factors 的返回值）；**不联网、不读文件、不预测、不构成投资建议**。
- 模型：r - RF = α + β1·(Mkt-RF) + β2·SMB + β3·HML [+ β4·Mom] + ε；系数用 OLS，标准误用 Newey-West(Bartlett 核，缺省滞后 5)。
- α 年化 = 日 α × 252；它只是「这三/四个因子没解释的部分」，**不等于选股能力**，也可能来自未控制的风险、数据问题或运气。
- 只适用于美股收益；A 股收益不能用这组因子（Ken French 没有中国数据）。
- 对齐规则：只用日期同时出现在收益和因子里、且所需因子不缺失的那些天；重叠不足 min_obs 天时返回 status="na"，不硬算。
- 因子数据是按月更新的，最后几周可能还没有；重叠天数会如实写出来。
"""
from __future__ import annotations

import math

import numpy as np

TD = 252
MODELS = {"ff3": ["Mkt-RF", "SMB", "HML"], "ff4": ["Mkt-RF", "SMB", "HML", "Mom"]}


class AttributionError(ValueError):
    """输入有问题；消息是固定文案，可以安全显示。"""


def _normal_p(t: float) -> float:
    return math.erfc(abs(t) / math.sqrt(2.0))                      # 双侧 p（正态近似）


def newey_west_cov(X: np.ndarray, resid: np.ndarray, lags: int) -> np.ndarray:
    n, k = X.shape
    xtx_inv = np.linalg.inv(X.T @ X)
    Xe = X * resid[:, None]
    S = Xe.T @ Xe
    for l in range(1, lags + 1):
        w = 1.0 - l / (lags + 1.0)
        G = Xe[l:].T @ Xe[:-l]
        S += w * (G + G.T)
    return xtx_inv @ S @ xtx_inv


def attribute(dates, returns, factors: dict | None, model: str = "ff3", *, min_obs: int = 250, lags: int = 5) -> dict:
    if model not in MODELS:
        raise AttributionError("model 只能是 ff3 或 ff4")
    if not isinstance(factors, dict) or not factors:
        raise AttributionError("没有因子数据（先运行 .venv/bin/python -m src.extdata 下载）")
    if len(dates) != len(returns):
        raise AttributionError("日期和收益的行数不一致")
    cols = MODELS[model]
    ys, xs, ds = [], [], []
    for d, r in zip(dates, returns):
        f = factors.get(d if isinstance(d, str) else str(d))
        if not f or f.get("RF") is None or any(f.get(c) is None for c in cols):
            continue
        ys.append(float(r) - f["RF"])
        xs.append([f[c] for c in cols])
        ds.append(d)
    n = len(ys)
    out = {"model": model, "factors": cols, "n_obs": n, "n_input": len(returns), "lags": lags, "status": "na"}
    if n < min_obs:
        out["note"] = f"与因子数据重叠的交易日只有 {n} 天（至少需要 {min_obs} 天）：不做回归。因子数据只覆盖美股，且按月更新。"
        return out
    y = np.asarray(ys)
    X = np.column_stack([np.ones(n), np.asarray(xs)])
    if np.linalg.matrix_rank(X) < X.shape[1]:
        out["note"] = "因子列线性相关或没有变化，无法回归。"
        return out
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ beta
    cov = newey_west_cov(X, resid, lags)
    se = np.sqrt(np.clip(np.diag(cov), 0.0, None))
    t = np.where(se > 0, beta / np.where(se > 0, se, 1.0), np.nan)
    ss_tot = float(((y - y.mean()) ** 2).sum())
    r2 = 1.0 - float((resid ** 2).sum()) / ss_tot if ss_tot > 0 else float("nan")
    names = ["alpha", *cols]
    coef = {nm: {"coef": float(b), "t": (None if not math.isfinite(tt) else float(tt)), "p": (None if not math.isfinite(tt) else _normal_p(float(tt)))}
            for nm, b, tt in zip(names, beta, t)}
    out.update(status="ok", first=ds[0], last=ds[-1], r2=r2 if math.isfinite(r2) else None, coef=coef,
               alpha_annual=float(beta[0]) * TD,
               note="α 是这几个因子没解释的部分，不等于选股能力；t 值用 Newey-West 标准误，p 值是正态近似。只适用于美股收益。")
    return out
