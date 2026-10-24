"""T006 S3c：市场状态与残差 Z-score 测试（合成数据，不读写文件）。"""
from pathlib import Path
import sys
import unittest

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.market_monitor import regime as rg


def levels(returns, start=100.0):
    idx = pd.bdate_range('2025-01-01', periods=len(returns) + 1)
    return pd.Series(start * np.concatenate([[1.0], np.cumprod(1 + np.asarray(returns))]), index=idx)


def flat_then_ramp(total_60d):
    """前 80 天不动，最后 60 天按恒定日收益走到 60 日累计 total_60d（恒定收益 -> 波动率近 0，不触发 high_vol）。"""
    g = (1 + total_60d) ** (1 / 60) - 1
    return levels([0.0] * 80 + [g] * 60)


class RegimeTests(unittest.TestCase):
    def test_scaling_table(self):
        self.assertEqual({k: rg.get_scaling(k) for k in ('range', 'trend_up', 'trend_down', 'high_vol', 'unknown')},
                         {'range': 1.0, 'trend_up': 0.6, 'trend_down': 0.6, 'high_vol': 0.25, 'unknown': 0.0})
        self.assertEqual(rg.get_scaling('不存在'), 0.0)

    def test_range(self):
        s = levels([0.005, -0.005] * 70)                         # 来回抖动，60 日累计≈0，波动率恒定
        self.assertEqual(rg.identify_regime(s, s.index[-1]), 'range')

    def test_trend_up_boundary(self):
        s = flat_then_ramp(0.05)                                   # 刚好 +5%
        self.assertEqual(rg.identify_regime(s, s.index[-1]), 'trend_up')
        s = flat_then_ramp(0.0499)
        self.assertEqual(rg.identify_regime(s, s.index[-1]), 'range')

    def test_trend_down_boundary(self):
        s = flat_then_ramp(-0.05)
        self.assertEqual(rg.identify_regime(s, s.index[-1]), 'trend_down')
        s = flat_then_ramp(-0.0499)
        self.assertEqual(rg.identify_regime(s, s.index[-1]), 'range')

    def test_high_vol_has_priority_over_trend(self):
        s = levels([0.0] * 100 + [0.06, -0.02] * 10)               # 最近 20 天剧烈波动，且 60 日累计 >> +5%
        self.assertGreater(s.iloc[-1] / s.iloc[-61] - 1, 0.05)
        self.assertEqual(rg.identify_regime(s, s.index[-1]), 'high_vol')

    def test_unknown_when_insufficient_or_bad_input(self):
        s = levels([0.001] * 60)
        self.assertEqual(rg.identify_regime(s, s.index[-1]), 'unknown')
        self.assertEqual(rg.identify_regime([1, 2, 3], '2025-06-01'), 'unknown')
        bad = flat_then_ramp(0.0)
        bad.iloc[50] = -1.0
        self.assertEqual(rg.identify_regime(bad, bad.index[-1]), 'unknown')

    def test_as_of_ignores_future_data(self):
        g = 1.10 ** (1 / 60) - 1
        s = levels([0.0] * 140 + [g] * 60)
        mid = s.index[130]                                         # 截到第 130 天：还在平台期
        self.assertEqual(rg.identify_regime(s, mid), 'range')
        self.assertEqual(rg.identify_regime(s, s.index[-1]), 'trend_up')

    def test_high_vol_quantile_boundary(self):
        hist = [1, 2, 3, 4, 5]                                     # 75 分位 = 4
        self.assertFalse(rg._is_high_vol(4.0, hist))               # 恰好等于不算
        self.assertTrue(rg._is_high_vol(4.0000001, hist))
        self.assertFalse(rg._is_high_vol(float('nan'), hist))
        self.assertFalse(rg._is_high_vol(1.0, []))


class ResidualZTests(unittest.TestCase):
    def setUp(self):
        rng = np.random.default_rng(7)
        idx = pd.bdate_range('2025-01-01', periods=200)
        self.m = pd.Series(rng.normal(0, 0.01, 200), index=idx)
        self.s = 1.5 * self.m + pd.Series(rng.normal(0, 0.005, 200), index=idx)

    def test_shock_gives_large_z_and_nan_warmup(self):
        s = self.s.copy()
        s.iloc[-1] += 0.05                                          # 当天比"按大盘应有涨幅"多涨 5%，噪声 sd=0.5%
        z = rg.get_residual_zscore(s, self.m, 60)
        self.assertTrue(z.iloc[:119].isna().all())                  # 先要 60 个 β，再要 60 个历史残差
        self.assertGreater(z.iloc[-1], 6)
        self.assertLess(abs(z.iloc[-2]), 4)

    def test_hand_calc_last_point(self):
        z = rg.get_residual_zscore(self.s, self.m, 60)
        s, m = self.s.values, self.m.values
        t = len(s) - 1
        betas = {}
        for k in range(t - 60, t + 1):
            w = slice(k - 60, k)
            betas[k] = np.cov(s[w], m[w], ddof=1)[0, 1] / np.var(m[w], ddof=1)
        resid = {k: s[k] - betas[k] * m[k] for k in betas}
        past = np.array([resid[k] for k in range(t - 60, t)])
        self.assertAlmostEqual(z.iloc[-1], (resid[t] - past.mean()) / past.std(ddof=1), places=9)

    def test_no_lookahead(self):
        full = rg.get_residual_zscore(self.s, self.m, 60)
        part = rg.get_residual_zscore(self.s.iloc[:150], self.m.iloc[:150], 60)
        self.assertAlmostEqual(full.iloc[149], part.iloc[149], places=12)

    def test_dataframe_columns_match_series(self):
        df = pd.DataFrame({'A': self.s, 'B': -self.s})
        z = rg.get_residual_zscore(df, self.m, 60)
        single = rg.get_residual_zscore(self.s, self.m, 60)
        self.assertAlmostEqual(z['A'].iloc[-1], single.iloc[-1], places=12)

    def test_bad_window(self):
        with self.assertRaises(ValueError):
            rg.get_residual_zscore(self.s, self.m, 2)


if __name__ == '__main__':
    unittest.main()
