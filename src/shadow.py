"""影子候选策略：每天并行算一大批备选策略的「今日仓位 → 明日仓位」，进信号核对账本和主策略竞争，不出现在今日买卖清单里。

为什么单独一条轻量通道：正式的 7 个策略要跑完整回测、生成解释和历史表现，很慢；候选只需要“今天该持多少”，
所以只取每只股票最近约 400 个交易日，算目标仓位的最后两天（前一天 = 今日仓位，今天 = 明日仓位），不跑回测、不套调仓带。
所有候选都只用「当天收盘及以前」的数据（复用 strategies.py / indicators.py，同样受 test_strategies 的无未来函数检查约束）。

候选按“原理”分家族，避免一堆几乎一样的策略互相冒充多样性：趋势 / 突破 / 均值回归 / 动量 / 形态 / 组合。
另有 3 个“随机对照”：每天按固定哈希随机决定持不持有（可重复），没有任何信息含量。
它们的得分应该在 0 附近（买入类还会被费用拖成负）；哪个候选连对照都跑不赢，或者对照也经常排前面，就说明排名主要是运气。
"""
from __future__ import annotations

import zlib
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import pandas as pd

from . import strategies as S
from .indicators import atr, realized_vol, rsi, sma

TAIL = 420
MIN_ROWS = 250


# ---------------------------------------------------------------- 候选的构件
def _trend(df, n, buffer=0.01, tv=None):
    return S.trend_vol_target(df, sma_len=n, buffer=buffer, target_vol=tv)


def _mom(df, look, skip=0):
    c = df["Close"]
    return ((c.shift(skip) / c.shift(look) - 1) > 0).astype(float)


def _high52(df):
    c = df["Close"]
    return ((c >= 0.9 * c.rolling(252).max()) & (c > sma(c, 200))).astype(float)


def _stack(df):
    c = df["Close"]
    return ((c > sma(c, 20)) & (sma(c, 20) > sma(c, 50)) & (sma(c, 50) > sma(c, 100))).astype(float)


def _low_vol_trend(df):
    c = df["Close"]
    return ((c > sma(c, 100) * 1.01) & (realized_vol(c, 60) < 0.35)).astype(float)


def _pullback(df):
    c = df["Close"]
    return ((c > sma(c, 200)) & (c > sma(c, 50)) & (c < sma(c, 10) * 0.97)).astype(float)


def _boll_rev(df, n=20, k=2.0, trend=200, max_hold=10):
    c = df["Close"]
    m, sd, tr = sma(c, n), c.rolling(n).std(), sma(c, trend)
    lo = (m - k * sd).to_numpy()
    mid, cc, trv = m.to_numpy(), c.to_numpy(), tr.to_numpy()
    st = np.zeros(len(cc))
    on, held = False, 0
    for t in range(len(cc)):
        if np.isnan(lo[t]) or np.isnan(trv[t]):
            continue
        if not on:
            if cc[t] < lo[t] and cc[t] > trv[t]:
                on, held = True, 0
        else:
            held += 1
            if cc[t] > mid[t] or held >= max_hold:
                on = False
        st[t] = on
    return pd.Series(st, index=df.index)


def _random(df, name, p, code):
    """没有信息的对照：每天用 crc32(名称|代码|日期) 决定持不持有，同一天重复运行结果一样。"""
    u = np.array([zlib.crc32(f"{name}|{code}|{d.date()}".encode()) / 2 ** 32 for d in df.index])
    return pd.Series((u < p).astype(float), index=df.index)


# 名称: (中文名, 家族, 函数(df, code) -> 目标仓位 Series)
CANDIDATES = {
    "trend_sma50":   ("趋势 50 日均线", "趋势", lambda d, c: _trend(d, 50)),
    "trend_sma100":  ("趋势 100 日均线", "趋势", lambda d, c: _trend(d, 100)),
    "trend_short":   ("趋势集成（20/50/100 日）", "趋势", lambda d, c: S.trend_ensemble(d, lens=(20, 50, 100), target_vol=None)),
    "trend_ens_raw": ("趋势集成（不按波动率调仓）", "趋势", lambda d, c: S.trend_ensemble(d, target_vol=None)),
    "stack_ma":      ("均线多头排列（20>50>100）", "趋势", lambda d, c: _stack(d)),
    "donchian_55":   ("海龟系统 2（55 日突破）", "突破", lambda d, c: S.donchian_breakout(d)),
    "donchian_10":   ("短期突破（10 日）", "突破", lambda d, c: S.donchian_breakout(d, entry_len=10, exit_len=5, atr_mult=2.0)),
    "high52":        ("接近 52 周新高", "突破", lambda d, c: _high52(d)),
    "rsi2_loose":    ("RSI2 宽松版", "均值回归", lambda d, c: S.rsi2_reversion(d, trend_len=100, entry_th=20.0)),
    "rsi5_dip":      ("RSI5 超跌反弹", "均值回归", lambda d, c: S.rsi2_reversion(d, rsi_len=5, entry_th=25.0, exit_ma=10, max_hold=15)),
    "boll_rev":      ("布林带下轨反弹", "均值回归", lambda d, c: _boll_rev(d)),
    "pullback":      ("上升趋势中的回调", "均值回归", lambda d, c: _pullback(d)),
    "mom_12_1":      ("12 个月动量（跳过最近 1 个月）", "动量", lambda d, c: _mom(d, 252, 21)),
    "mom_6m":        ("6 个月动量", "动量", lambda d, c: _mom(d, 126)),
    "mom_3m":        ("3 个月动量", "动量", lambda d, c: _mom(d, 63)),
    "low_vol_trend": ("低波动 + 趋势", "形态", lambda d, c: _low_vol_trend(d)),
    "blend_tm":      ("组合：趋势集成 + 12 月动量", "组合", lambda d, c: S.blend(S.trend_ensemble(d), _mom(d, 252, 21))),
    "blend_br":      ("组合：海龟 + RSI2", "组合", lambda d, c: S.blend(S.donchian_s1(d), S.rsi2_reversion(d))),
    "ctrl_random_a": ("随机对照 A（30% 概率持有）", "对照", lambda d, c: _random(d, "a", 0.30, c)),
    "ctrl_random_b": ("随机对照 B（15% 概率持有）", "对照", lambda d, c: _random(d, "b", 0.15, c)),
    "ctrl_random_c": ("随机对照 C（50% 概率持有）", "对照", lambda d, c: _random(d, "c", 0.50, c)),
}
LABELS = {k: v[0] for k, v in CANDIDATES.items()}
FAMILY = {k: v[1] for k, v in CANDIDATES.items()}
CONTROLS = [k for k, v in CANDIDATES.items() if v[1] == "对照"]


def compute_code(args):
    """一只股票 → [(策略, 今日仓位, 明日仓位)]；数据不够或最后一天不是目标日就返回空。"""
    code, day = args
    from . import allmarket
    try:
        full = allmarket._drop_unfinished(allmarket.load(code))
    except Exception:
        return []
    if full is None or len(full) < MIN_ROWS or full.index[-1].strftime("%Y-%m-%d") != day:
        return []
    df = full.tail(TAIL)
    out = []
    for name, (_, _, fn) in CANDIDATES.items():
        try:
            w = fn(df, code).astype(float).fillna(0.0).clip(0.0, 1.0)
            out.append((name, code, float(w.iloc[-2]), float(w.iloc[-1])))
        except Exception:
            continue
    return out


def compute_all(codes: list[str], day: str, processes: int | None = None) -> list[tuple]:
    """[(策略, 代码, 今日仓位, 明日仓位)]，格式和账本的 state 表一致。"""
    import os
    processes = processes or max(1, (os.cpu_count() or 2) - 1)
    rows = []
    with ProcessPoolExecutor(max_workers=processes) as ex:
        for r in ex.map(compute_code, [(c, day) for c in codes], chunksize=40):
            rows.extend(r)
    return rows


def record_signals(c, day: str, thr: float | None = None, mt: float | None = None) -> int:
    """把某天的候选持仓状态转成信号，写进账本的 signals 表（重复调用不会重复写）。
    阈值默认取「当天正式清单用的那一组」（账本 day_params 表），这样候选、随机对照和正式策略同口径；没有记录才用默认值。"""
    from .auto_tune import _classify, BUYS, DEFAULT
    if thr is None or mt is None:
        row = c.execute("SELECT thr, mt FROM day_params WHERE day=?", (day,)).fetchone()
        thr = thr if thr is not None else (row["thr"] if row else DEFAULT["thr"])
        mt = mt if mt is not None else (row["mt"] if row else DEFAULT["mt"])
    names = list(CANDIDATES)
    ph = ",".join("?" * len(names))
    n = 0
    for r in c.execute(f"SELECT strategy, code, now, next FROM state WHERE day=? AND strategy IN ({ph})", (day, *names)).fetchall():
        a = _classify(r["now"], r["next"], thr)
        if not a:
            continue
        cur = c.execute("INSERT OR IGNORE INTO signals VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (day, r["strategy"], r["code"], None, a, r["now"], r["next"], None, "未知",
                         1 if (a in BUYS and r["next"] < mt) else 0, "[]"))
        n += cur.rowcount
    c.commit()
    return n
