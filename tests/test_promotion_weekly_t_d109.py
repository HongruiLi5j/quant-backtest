"""D109：晋升的按周 t 检验第二道门。合成数据，不读写 data/。运行：.venv/bin/python tests/test_promotion_weekly_t_d109.py"""
import sys
import unittest
from pathlib import Path

import numpy as np
from scipy import stats

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import promotion as PR  # noqa: E402


class WeeklyT(unittest.TestCase):
    def test_matches_scipy(self):
        rng = np.random.default_rng(1)
        own = rng.normal(0.004, 0.01, 12); null = rng.normal(0, 0.003, (12, 50))
        d = own - null.mean(1)
        self.assertAlmostEqual(PR.weekly_t_p(own, null), stats.ttest_1samp(d, 0, alternative="greater").pvalue, places=12)

    def test_degenerate(self):
        self.assertEqual(PR.weekly_t_p([0.1], np.zeros((1, 5))), 1.0)                 # 周数 < 2
        self.assertEqual(PR.weekly_t_p(np.zeros(4), np.zeros((4, 3))), 1.0)           # 零波动、均值 0
        self.assertEqual(PR.weekly_t_p(np.full(4, 0.1), np.zeros((4, 3))), 0.0)       # 零波动、均值 > 0

    def test_gate_is_max(self):
        old = PR.WEEKLY_T_GATE
        try:
            PR.WEEKLY_T_GATE = True
            self.assertEqual(PR.gate_p(0.001, 0.2), 0.2)
            self.assertEqual(PR.gate_p(0.3, 0.001), 0.3)
            PR.WEEKLY_T_GATE = False                      # 默认关闭：只用对照 p
            self.assertEqual(PR.gate_p(0.001, 0.2), 0.001)
        finally:
            PR.WEEKLY_T_GATE = old

    def test_common_shock_is_caught(self):
        # 自己 − 对照 在各周大幅摇摆（共同冲击）：均值为正但 t 检验不显著
        own = np.array([0.03, -0.02, 0.04, -0.03, 0.02, -0.01]); null = np.zeros((6, 20))
        self.assertGreater(PR.weekly_t_p(own, null), 0.2)


if __name__ == "__main__":
    unittest.main()
