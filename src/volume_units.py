"""A 股日线 Volume 单位统一（股 / 手）：只在读取时换算，不改磁盘文件。

背景：data/daily/cn_all/<代码>.parquet 的 Volume 来自不同数据源，单位不一致——
  新浪历史 / 新浪快照 / BaoStock 是"股"，腾讯历史（科创板除外）和东方财富快照是"手"（1 手 = 100 股）。
落盘时没有记录来源，所以同一个文件里会混着两种单位，相差正好 100 倍。

做法（两层证据）：
  1. 旁表核实：src/turnover.py 的真实成交额旁表里有 成交额(元) 和 未复权收盘价，
     成交额 / 收盘价 ≈ 当天成交股数。文件里的 Volume 与它的比值 ≈1 是"股"，≈0.01 是"手"。
  2. 没有旁表的日期（历史大部分时间）用动态规划推断：给每一天一个状态（股 / 手），
     把"手"的那天在 log10 上加 2（即 ×100），选一条让换算后的成交量曲线最平滑的状态序列；
     每切换一次单位要付固定罚分，旁表核实过的日期状态固定不能改。
     一字板 / 涨跌停收盘这类"真实缩量"的日子两侧切换单位要多付罚分，避免把真实缩量当成单位错误。

注意：
  - 推断用到了整段序列（包括 t 之后的日子）来判断 t 当天的单位，属于事后数据清洗，
    不是交易信号；但它可能把"真实的极端缩量日"误判成"手"而放大 100 倍，接入策略前必须站主决定。
  - 本模块不被任何策略调用，接入会改变用到成交量的因子 / 研究脚本的结果。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

LOT = 100                  # 1 手 = 100 股
LOG_LOT = 2.0              # log10(100)
TOL = 0.3                  # 旁表核实容差（log10）：比值在 10^±0.3 ≈ 0.5～2 倍之内才算匹配
SWITCH_PENALTY = 1.0       # 每切换一次单位的罚分（log10 单位）；单根跳变 >1+罚分/2≈1.5（约 32 倍）才值得切换
LIMIT_MOVE = 0.045         # 收在最高/最低且涨跌幅 ≥4.5% 视为涨跌停收盘（ST 涨跌停是 5%）
# 一字板/涨跌停收盘两侧切换单位的额外罚分。理由（事先推出，不是用数据调的）：
# 把某天 ×100 最多让相邻差 |Δlog10| 缩小 2（三角不等式），所以切换总代价 1+1.5=2.5>2 时，
# 单靠"让曲线更平滑"永远不值得在这类日子切换，真实缩量不会被放大；但旁表锚点要求切换时仍然切得过去。
# （第一版用"禁止切换"，会把数据切成没有锚点的孤岛、整段乱判，留出验证里看到了，所以改成有限罚分。）
LOCK_PENALTY = 1.5
_FORCED = 1e9              # 违反旁表核实的代价

BASIS_VERIFIED = "旁表核实"
BASIS_INFERRED = "推断"
BASIS_NO_ANCHOR = "无锚点"   # 这只股票没有任何旁表核实的日期：无法确定绝对单位，保持原值


def classify_ratio(volume, implied_shares, tol: float = TOL):
    """文件 Volume / 旁表推算股数 → 0=股，1=手，NaN=对不上（不判断）。支持标量或数组。"""
    v = np.asarray(volume, dtype=float)
    s = np.asarray(implied_shares, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        lr = np.log10(v / s)
    out = np.full(lr.shape, np.nan)
    ok = np.isfinite(lr) & (v > 0) & (s > 0)
    out[ok & (np.abs(lr) <= tol)] = 0
    out[ok & (np.abs(lr + LOG_LOT) <= tol)] = 1
    return out if out.ndim else float(out)


def verified_lot_flags(daily: pd.DataFrame, turnover_rows: pd.DataFrame, tol: float = TOL) -> pd.Series:
    """用成交额旁表核实每天的单位。

    daily：单只股票日线（日期索引，至少有 Volume 列）；
    turnover_rows：同一只股票的旁表行（列 date / turnover_yuan / close_raw，见 src/turnover.py）。
    返回与 daily 同索引的 Series：0=股，1=手，NaN=当天没有旁表或对不上。
    """
    out = pd.Series(np.nan, index=daily.index, dtype=float)
    if turnover_rows is None or len(turnover_rows) == 0 or daily.empty:
        return out
    t = turnover_rows.copy()
    t["date"] = pd.to_datetime(t["date"])
    t = t.drop_duplicates("date", keep="last").set_index("date")
    implied = (t["turnover_yuan"].astype(float) / t["close_raw"].astype(float)).reindex(
        pd.DatetimeIndex(daily.index).normalize())
    out[:] = classify_ratio(daily["Volume"].to_numpy(dtype=float), implied.to_numpy(dtype=float), tol)
    return out


def _locked_days(daily: pd.DataFrame) -> np.ndarray:
    """真实缩量的高发日：一字板（最高=最低）、收在最高/最低且涨跌幅 ≥4.5%（涨跌停收盘）、序列第一行。"""
    n = len(daily)
    if not {"High", "Low", "Close"}.issubset(daily.columns):
        return np.zeros(n, dtype=bool)
    h = daily["High"].to_numpy(dtype=float)
    lo = daily["Low"].to_numpy(dtype=float)
    c = daily["Close"].to_numpy(dtype=float)
    one_price = np.isclose(h, lo, rtol=1e-6, atol=0)
    prev = np.concatenate([[np.nan], c[:-1]])
    with np.errstate(divide="ignore", invalid="ignore"):
        move = np.abs(c / prev - 1)
    at_edge = np.isclose(c, h, rtol=1e-6, atol=0) | np.isclose(c, lo, rtol=1e-6, atol=0)
    limit_close = at_edge & (move >= LIMIT_MOVE)
    locked = one_price | np.nan_to_num(limit_close, nan=0).astype(bool)
    if n:
        locked[0] = True   # 第一行（通常是上市首日）没有昨收，判断不了涨跌停，按真实缩量高发日处理
    return locked


def infer_lot_flags(daily: pd.DataFrame, verified: pd.Series | None = None,
                    switch_penalty: float = SWITCH_PENALTY) -> pd.DataFrame:
    """推断每天 Volume 的单位。返回 DataFrame(index 同 daily)：is_lot(bool，True=手)、basis(判断依据)。

    动态规划（两状态 Viterbi）：
      状态 a_t ∈ {0=股, 1=手}，换算后 y_t = log10(Volume_t) + 2·a_t；
      相邻两个有成交日之间的代价 = |y_t - y_(t-1)| + 罚分·[单位切换]；
      切换的两侧任一天是一字板/涨跌停收盘 → 额外加 LOCK_PENALTY；旁表核实过的日子状态固定。
    Volume ≤0 或缺失的日子不参与计算（0 换算后还是 0），沿用前一天的状态。
    """
    n = len(daily)
    result = pd.DataFrame({"is_lot": np.zeros(n, dtype=bool), "basis": BASIS_NO_ANCHOR}, index=daily.index)
    if n == 0:
        return result
    ver = (verified.reindex(daily.index) if verified is not None
           else pd.Series(np.nan, index=daily.index)).to_numpy(dtype=float)
    if not np.isfinite(ver).any():
        return result                      # 没有锚点：全体×1 和全体×100 一样平滑，无法确定绝对单位
    vol = daily["Volume"].to_numpy(dtype=float)
    valid = np.isfinite(vol) & (vol > 0)
    idx = np.flatnonzero(valid)
    locked = _locked_days(daily)
    if len(idx):
        x = np.log10(vol[idx]).tolist()
        vv = ver[idx].tolist()
        lk = locked[idx].tolist()
        m = len(idx)
        back = [[0, 0] for _ in range(m)]   # back[i][s]：第 i 天处于状态 s 时，前一天的最优状态

        def unary(i, s):
            v = vv[i]
            return _FORCED if (v == v and int(v) != s) else 0.0   # v == v 用来排除 NaN

        cost = [unary(0, 0), unary(0, 1)]
        for i in range(1, m):
            switch = switch_penalty + (LOCK_PENALTY if (lk[i] or lk[i - 1]) else 0.0)
            new = [0.0, 0.0]
            for s in (0, 1):
                y = x[i] + LOG_LOT * s
                c_stay = cost[s] + abs(y - x[i - 1] - LOG_LOT * s)
                c_flip = cost[1 - s] + abs(y - x[i - 1] - LOG_LOT * (1 - s)) + switch
                if c_stay <= c_flip:
                    new[s], back[i][s] = c_stay, s
                else:
                    new[s], back[i][s] = c_flip, 1 - s
                new[s] += unary(i, s)
            cost = new
        states = [0] * m
        states[-1] = 0 if cost[0] <= cost[1] else 1
        for i in range(m - 1, 0, -1):
            states[i - 1] = back[i][states[i]]
        flags = np.zeros(n, dtype=float)
        flags[:] = np.nan
        flags[idx] = states
        # 没成交的日子沿用前一个状态（换算结果不受影响，只是让 is_lot 连续好读）
        flags = pd.Series(flags).ffill().bfill().to_numpy()
        result["is_lot"] = flags.astype(bool)
    result["basis"] = np.where(np.isfinite(ver), BASIS_VERIFIED, BASIS_INFERRED)
    return result


def to_shares(daily: pd.DataFrame, turnover_rows: pd.DataFrame | None = None,
              switch_penalty: float = SWITCH_PENALTY) -> tuple[pd.DataFrame, pd.DataFrame]:
    """读取层统一入口：返回 (换算成"股"的日线副本, 每天的判断明细)。不修改传入的 DataFrame，不写盘。

    没有任何旁表锚点的股票原样返回（basis=无锚点），调用方应把它当作"单位未知"。
    """
    verified = verified_lot_flags(daily, turnover_rows) if turnover_rows is not None else None
    info = infer_lot_flags(daily, verified, switch_penalty)
    out = daily.copy()
    if "Volume" in out.columns and len(out):
        factor = np.where(info["is_lot"].to_numpy(), LOT, 1)
        out["Volume"] = out["Volume"].astype(float) * factor
    return out, info


def summarize(info: pd.DataFrame) -> dict:
    """单只股票的换算摘要（给检测脚本和页面用）。"""
    lot = info["is_lot"].to_numpy(dtype=bool)
    switches = int(np.count_nonzero(lot[1:] != lot[:-1])) if len(lot) > 1 else 0
    first = info.index[lot][0] if lot.any() else None
    last = info.index[lot][-1] if lot.any() else None
    return {"rows": int(len(info)), "lot_rows": int(lot.sum()), "switches": switches,
            "first_lot": None if first is None else pd.Timestamp(first).strftime("%Y-%m-%d"),
            "last_lot": None if last is None else pd.Timestamp(last).strftime("%Y-%m-%d"),
            "basis": {k: int(v) for k, v in info["basis"].value_counts().items()},
            "lot_inferred": int((lot & (info["basis"] == BASIS_INFERRED).to_numpy()).sum())}
