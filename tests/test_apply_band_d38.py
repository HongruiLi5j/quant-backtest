"""D38：apply_band 新写法与原写法逐位一致。python tests/test_apply_band_d38.py"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.portfolio import apply_band  # noqa: E402


def apply_band_reference(target, band):
    """改动前的原实现（逐行 numpy），留作对照。"""
    if band <= 0:
        return target
    t = target.to_numpy()
    out = np.zeros_like(t)
    cur = np.zeros(t.shape[1])
    for i in range(len(t)):
        row = t[i]
        change = (np.abs(row - cur) > band) | ((row == 0) != (cur == 0))
        cur = np.where(change, row, cur)
        out[i] = cur
    return pd.DataFrame(out, index=target.index, columns=target.columns)


def _same(a, b):
    assert a.index.equals(b.index) and a.columns.equals(b.columns)
    assert a.to_numpy().dtype == b.to_numpy().dtype
    assert np.array_equal(a.to_numpy(), b.to_numpy(), equal_nan=True)


def test_random_targets_identical():
    rng = np.random.default_rng(38)
    idx = pd.bdate_range("2012-01-02", periods=3500)
    for cols in (1, 2, 7):
        for band in (0.05, 0.10, 0.25):
            a = rng.random((3500, cols))
            a[rng.random(a.shape) < 0.3] = 0.0                  # 空仓
            q = np.round(a * 4) / 4                              # 1/4 档仓位，带等号边界
            for arr in (a, q):
                df = pd.DataFrame(arr, index=idx, columns=[f"s{i}" for i in range(cols)])
                _same(apply_band(df, band), apply_band_reference(df, band))


def test_edge_cases_identical():
    idx = pd.RangeIndex(8)
    cases = [
        [0.0, 0.1, 0.2, 0.2, 0.0, 0.0, 0.05, 1.0],               # 正好等于 band 不调
        [np.nan, 0.5, np.nan, 0.55, 0.0, np.nan, 0.3, 0.3],      # NaN 行
        [1.0] * 8, [0.0] * 8, [-0.2, 0.0, 0.3, -0.1, 0.0, 0.0, 0.2, 0.2],
    ]
    for c in cases:
        df = pd.DataFrame({"x": c}, index=idx)
        for band in (0.1, 0.0, -1.0):
            _same(apply_band(df, band), apply_band_reference(df, band))
    ints = pd.DataFrame({"x": [0, 1, 1, 0, 1]})                 # 整数输入：dtype 也一致
    _same(apply_band(ints, 0.5), apply_band_reference(ints, 0.5))
    empty = pd.DataFrame({"x": []}, dtype=float)
    _same(apply_band(empty, 0.1), apply_band_reference(empty, 0.1))


if __name__ == "__main__":
    test_random_targets_identical()
    test_edge_cases_identical()
    print("ok: 2 项通过")
