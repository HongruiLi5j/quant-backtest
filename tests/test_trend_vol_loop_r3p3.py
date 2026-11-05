"""R3-perf-1 P3：trend_vol_target / donchian_breakout / rsi2_reversion 逐日循环改写后与原实现逐位一致。python tests/test_trend_vol_loop_r3p3.py"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from src import strategies as S  # noqa: E402
from src.indicators import atr, rsi, sma  # noqa: E402


def reference(df, sma_len=200, buffer=0.01, target_vol=0.15, vol_len=20, max_lev=1.0):
    """改动前的原实现，留作对照。"""
    c = df["Close"].to_numpy()
    m = sma(df["Close"], sma_len).to_numpy()
    state = np.zeros(len(c))
    on = False
    for t in range(len(c)):
        if np.isnan(m[t]):
            continue
        if not on and c[t] > m[t] * (1 + buffer):
            on = True
        elif on and c[t] < m[t] * (1 - buffer):
            on = False
        state[t] = on
    w = pd.Series(state, index=df.index) * S.vol_scale(df["Close"], target_vol, vol_len, max_lev)
    return w.fillna(0.0).rename("trend_vol")


def ref_donchian(df, entry_len=55, exit_len=20, atr_len=20, atr_mult=3.0, target_vol=0.15, vol_len=20):
    """改动前的原实现。"""
    c = df["Close"].to_numpy()
    hi = df["High"].rolling(entry_len).max().shift(1).to_numpy()
    lo = df["Low"].rolling(exit_len).min().shift(1).to_numpy()
    a = atr(df, atr_len).to_numpy()
    state = np.zeros(len(c))
    on, peak = False, 0.0
    for t in range(len(c)):
        if np.isnan(hi[t]) or np.isnan(a[t]):
            continue
        if not on:
            if c[t] > hi[t]:
                on, peak = True, c[t]
        else:
            peak = max(peak, c[t])
            if c[t] < lo[t] or c[t] < peak - atr_mult * a[t]:
                on = False
        state[t] = on
    w = pd.Series(state, index=df.index) * S.vol_scale(df["Close"], target_vol, vol_len)
    return w.fillna(0.0).rename("donchian")


def ref_rsi2(df, trend_len=200, rsi_len=2, entry_th=10.0, exit_ma=5, max_hold=10):
    """改动前的原实现（只到 state；之后的返回与新实现共用同一段代码）。"""
    c = df["Close"].to_numpy()
    trend = sma(df["Close"], trend_len).to_numpy()
    r = rsi(df["Close"], rsi_len).to_numpy()
    ex = sma(df["Close"], exit_ma).to_numpy()
    state = np.zeros(len(c))
    on, held = False, 0
    for t in range(len(c)):
        if np.isnan(trend[t]) or np.isnan(r[t]):
            continue
        if not on:
            if c[t] > trend[t] and r[t] < entry_th:
                on, held = True, 0
        else:
            held += 1
            if c[t] > ex[t] or held >= max_hold:
                on = False
        state[t] = on
    return pd.Series(state, index=df.index, name="rsi2")


def frames():
    rng = np.random.default_rng(33)
    for n in (5, 199, 200, 201, 3500):
        idx = pd.bdate_range("2012-01-02", periods=n)
        c = 10 * np.exp(np.cumsum(rng.normal(0, 0.02, n)))
        hi, lo = c * (1 + rng.uniform(0, 0.03, n)), c * (1 - rng.uniform(0, 0.03, n))
        yield pd.DataFrame({"Open": c, "High": hi, "Low": lo, "Close": c, "Volume": 1.0}, index=idx)
    idx = pd.bdate_range("2012-01-02", periods=600)
    flat = np.full(600, 10.0)
    flat[300:] = 10.1                                          # 正好压在 1% 缓冲边界附近
    yield pd.DataFrame({"Open": flat, "High": flat, "Low": flat, "Close": flat, "Volume": 1.0}, index=idx)
    gap = 10 * np.exp(np.cumsum(rng.normal(0, 0.02, 600)))
    gap[250:260] = np.nan                                      # 收盘价有缺失
    yield pd.DataFrame({"Open": gap, "High": gap, "Low": gap, "Close": gap, "Volume": 1.0}, index=idx)


def test_identical():
    for df in frames():
        for kw in ({}, {"sma_len": 50, "buffer": 0.0}, {"buffer": 0.05, "target_vol": None}, {"sma_len": 100, "max_lev": 2.0}):
            a, b = reference(df, **kw), S.trend_vol_target(df, **kw)
            assert a.index.equals(b.index) and a.name == b.name and a.dtype == b.dtype
            assert np.array_equal(a.to_numpy(), b.to_numpy(), equal_nan=True), (len(df), kw)


def test_identical_donchian_and_rsi2():
    for df in frames():
        for kw in ({}, {"entry_len": 20, "exit_len": 10, "atr_mult": 2.0}, {"target_vol": None}):
            a, b = ref_donchian(df, **kw), S.donchian_breakout(df, **kw)
            assert a.index.equals(b.index) and a.name == b.name and a.dtype == b.dtype
            assert np.array_equal(a.to_numpy(), b.to_numpy(), equal_nan=True), ("donchian", len(df), kw)
        for kw in ({}, {"entry_th": 30.0, "max_hold": 3}, {"trend_len": 50, "exit_ma": 3}):
            a, b = ref_rsi2(df, **kw), S.rsi2_reversion(df, **kw)
            assert a.name == b.name and a.dtype == b.dtype
            assert np.array_equal(a.to_numpy(), b.to_numpy(), equal_nan=True), ("rsi2", len(df), kw)


def test_faster():
    df = next(f for f in frames() if len(f) == 3500)
    t0 = time.perf_counter(); [reference(df) for _ in range(20)]
    t1 = time.perf_counter(); [S.trend_vol_target(df) for _ in range(20)]
    t2 = time.perf_counter()
    print(f"3500 天：原 {(t1 - t0) / 20 * 1000:.2f}ms，新 {(t2 - t1) / 20 * 1000:.2f}ms")
    assert t2 - t1 < t1 - t0


if __name__ == "__main__":
    test_identical()
    test_identical_donchian_and_rsi2()
    test_faster()
    print("ok: 3 项通过")
