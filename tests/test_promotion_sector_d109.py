"""D109 方案 B：行业匹配区块对照。合成数据。运行：.venv/bin/python tests/test_promotion_sector_d109.py"""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import promotion as PR  # noqa: E402


def setup(T=8, U=400, seed=0):
    rng = np.random.default_rng(seed)
    ex = [rng.normal(0, 0.03, U) for _ in range(T)]
    ok = [np.ones(U, bool) for _ in range(T)]
    ind = np.arange(U) % 8
    sig = [np.sort(rng.choice(U, 20, replace=False)) for _ in range(T)]
    return ex, ok, sig, ind


class Sector(unittest.TestCase):
    def test_none_is_bit_identical(self):
        ex, ok, sig, _ = setup()
        L = np.array([1, 2, 3, 5])
        a = PR.block_side(ex, ok, sig, L, 50, np.random.default_rng(7))
        b = PR.block_side_sector(ex, ok, sig, L, 50, np.random.default_rng(7), industry=None)
        for x, y in zip(a, b):
            np.testing.assert_array_equal(x, y)

    def test_twins_come_from_same_industry(self):
        ex, ok, sig, ind = setup()
        # 用「超额 = 行业编号」让抽到的行业可读：每天每个槽位的新块行业应与当天信号行业的多重集一致
        ex = [ind.astype(float) for _ in sig]
        L = np.array([1])                              # 块长 1：每天全是新块
        out, _, _ = PR.block_side_sector(ex, ok, sig, L, 200, np.random.default_rng(3), industry=ind)
        for t in range(len(sig)):
            want = ind[sig[t]].sum()                   # 行业编号之和：每个对照都应恰好等于它
            np.testing.assert_allclose(out[t], want)

    def test_small_industry_falls_back(self):
        ex, ok, sig, ind = setup()
        ind = ind.copy(); ind[:5] = 99                 # 行业 99 只有 5 只 < 10
        sig = [np.array([0, 1, 100, 101]) for _ in sig]
        PR.SECTOR_FALLBACKS[0] = 0
        PR.block_side_sector(ex, ok, sig, np.array([1]), 20, np.random.default_rng(1), industry=ind)
        self.assertEqual(PR.SECTOR_FALLBACKS[0], 2 * 20 * len(sig))      # 每天 2 个信号 × k 个对照

    def test_unknown_industry_uses_market_pool(self):
        ex, ok, sig, ind = setup()
        ind = np.full_like(ind, -1)
        out, _, _ = PR.block_side_sector(ex, ok, sig, np.array([2]), 30, np.random.default_rng(2), industry=ind)
        self.assertEqual(out.shape, (len(sig), 30))
        self.assertTrue(np.isfinite(out).all())


if __name__ == "__main__":
    unittest.main()
