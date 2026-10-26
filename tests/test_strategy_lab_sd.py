"""strategy_lab_sd 纯函数测试：只用合成数组，不读 data/，不导入 app。"""
import math
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import lab_import_guard as guard  # noqa: E402

(sd,) = guard.import_guarded("strategy_lab_sd")


class SharpeDiffTests(unittest.TestCase):
    def test_same_series_zero(self):
        r = np.array([0.01, -0.005, 0.002, 0.004] * 30)
        self.assertAlmostEqual(sd.sharpe_diff(r, r), 0.0)

    def test_nan_when_flat(self):
        r = np.array([0.01, -0.005] * 30)
        self.assertTrue(math.isnan(sd.sharpe_diff(np.zeros(60), r)))

    def test_not_information_ratio(self):
        # 策略=基准一半仓位：夏普相同，夏普之差应为 0，而主动收益(信息比率)为负
        rng = np.random.default_rng(1)
        b = rng.normal(0.001, 0.01, 500)
        r = 0.5 * b
        self.assertAlmostEqual(sd.sharpe_diff(r, b), 0.0)
        self.assertLess(sd.ann_sharpe(r - b), 0)


class SignTestTests(unittest.TestCase):
    def test_all_positive_small_p(self):
        n, k, p = sd.sign_test([0.1] * 100)
        self.assertEqual((n, k), (100, 100))
        self.assertLess(p, 1e-6)

    def test_balanced_p_one(self):
        n, k, p = sd.sign_test([1, -1] * 50)
        self.assertAlmostEqual(p, 1.0)

    def test_ignores_nan_and_zero(self):
        n, k, p = sd.sign_test([float("nan"), 0.0, 1.0])
        self.assertEqual((n, k), (1, 1))

    def test_empty(self):
        n, k, p = sd.sign_test([])
        self.assertEqual(n, 0)
        self.assertTrue(math.isnan(p))


class JudgeTests(unittest.TestCase):
    def test_confirmed(self):
        v = sd.judge([0.1] * 100, [0.1] * 100, [0.1] * 100)
        self.assertTrue(v["confirmed"])

    def test_needs_both_halves(self):
        v = sd.judge([0.1] * 100, [0.1] * 100, [-0.1] * 100)
        self.assertFalse(v["confirmed"])

    def test_needs_early_half_too(self):
        # 早期为负、晚期为正：删掉"早期中位数>0"这个条件就会误确认
        v = sd.judge([0.1] * 100, [-0.1] * 100, [0.1] * 100)
        self.assertFalse(v["confirmed"])

    def test_early_exactly_zero_is_not_enough(self):
        self.assertFalse(sd.judge([0.1] * 100, [0.0] * 100, [0.1] * 100)["confirmed"])

    def test_needs_significance(self):
        v = sd.judge([0.1, -0.1] * 50, [0.1] * 100, [0.1] * 100)
        self.assertFalse(v["confirmed"])

    def test_needs_positive_median(self):
        v = sd.judge([-0.1] * 100, [0.1] * 100, [0.1] * 100)
        self.assertFalse(v["confirmed"])

    def test_infinite_not_confirmed(self):
        inf = float("inf")
        v = sd.judge([inf] * 100, [inf] * 100, [inf] * 100)
        self.assertFalse(v["confirmed"])
        self.assertEqual(v["n"], 0)

    def test_inf_mixed_ignored(self):
        v = sd.judge([0.1] * 100 + [float("inf"), float("-inf"), float("nan")], [0.1] * 100, [0.1] * 100)
        self.assertEqual(v["n"], 100)
        self.assertEqual(v["n_early"], 100)

    def test_counts_reported(self):
        v = sd.judge([0.1] * 10, [0.1] * 4 + [float("nan")] * 6, [0.1] * 10)
        self.assertEqual((v["n_early"], v["n_late"]), (4, 10))

    def test_exact_vs_normal_small_n(self):
        # n=6 全正：正态近似 0.0143，精确 0.03125（可能跨 0.025 阈值）
        self.assertAlmostEqual(sd.sign_test_exact([0.1] * 6), 0.03125)
        self.assertLess(sd.sign_test([0.1] * 6)[2], 0.025)

    def test_exact_ignores_nonfinite(self):
        self.assertAlmostEqual(sd.sign_test_exact([0.1] * 6 + [float("inf"), float("nan"), 0.0]), 0.03125)

    def test_alpha_is_bonferroni(self):
        self.assertAlmostEqual(sd.ALPHA, 0.025)


if __name__ == "__main__":
    unittest.main()
