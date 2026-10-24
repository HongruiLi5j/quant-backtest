"""T006 S3c：24 因子库测试。只用合成数据；每类因子至少一个手算核对 + 全部因子的未来函数断言。"""
from pathlib import Path
import sys
import unittest

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.market_monitor import factors as fx


def frame(values, code='A', n=None):
    """一列序列 -> 宽表。"""
    idx = pd.bdate_range('2025-01-01', periods=len(values))
    return pd.DataFrame({code: values}, index=idx, dtype=float)


def panel_from(close, open_=None, high=None, low=None, volume=None, **kw):
    close = frame(close)
    like = lambda x, default: frame(x) if x is not None else default
    return fx.Panel(like(open_, close.copy()), like(high, close.copy()), like(low, close.copy()), close,
                    like(volume, close * 0 + 1000.0), **kw)


def random_panel(n=300, k=8, seed=1):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range('2024-01-01', periods=n)
    codes = [f'{600000 + i}' for i in range(k)]
    ret = rng.normal(0.0004, 0.02, (n, k))
    close = pd.DataFrame(10 * np.cumprod(1 + ret, axis=0), index=idx, columns=codes)
    open_ = close.shift(1).fillna(close.iloc[0]) * (1 + rng.normal(0, 0.005, (n, k)))
    high = np.maximum(close, open_) * (1 + rng.uniform(0, 0.01, (n, k)))
    low = np.minimum(close, open_) * (1 - rng.uniform(0, 0.01, (n, k)))
    volume = pd.DataFrame(rng.uniform(1e5, 1e6, (n, k)), index=idx, columns=codes)
    names = pd.Series(['平安', '*ST某某', '茅台', '万科', '宁德', 'ST甲', '招行', '中免'], index=codes)
    industry = pd.Series(['银行', '地产', '白酒', '地产', '电池', '地产', '银行', '零售'], index=codes)
    return fx.Panel(open_, high, low, close, volume, names, industry)


class LibraryTests(unittest.TestCase):
    def test_source_citations(self):
        """来源"精确可查"：24 个 source 非空；只有真正的 WQ101 原式（wq12、wq54）才写 Alpha#NN，且编号在 1..101。"""
        import re
        wq_ids = set()
        for fid, meta in fx.FACTORS.items():
            self.assertTrue(meta.source.strip(), fid)
            if 'WorldQuant' in meta.source and 'Alpha#' in meta.source:
                nums = [int(x) for x in re.findall(r'Alpha#(\d+)', meta.source)]
                self.assertTrue(nums and all(1 <= k <= 101 for k in nums), fid)
                wq_ids.add(fid)
            if 'GTJA' in meta.source or '国泰君安' in meta.source or 'MSCI' in meta.source:
                self.assertIn('自行实现', meta.source, fid)        # 没核对过原编号的只能写"自行实现，参考…"
        self.assertEqual(wq_ids, {'wq12', 'wq54'})
        self.assertNotIn('Alpha#', fx.FACTORS['close_pos'].source)
        self.assertNotIn('Alpha#', fx.FACTORS['pv_corr5'].source.replace('Alpha#6', ''))

    def test_24_factors_with_meta(self):
        self.assertEqual(len(fx.FACTORS), 24)
        cats = {}
        for meta in fx.FACTORS.values():
            cats[meta.category] = cats.get(meta.category, 0) + 1
            self.assertTrue(meta.formula and meta.source and meta.direction in (1, -1))
            self.assertIn(meta.kind, ('score', 'flag', 'filter'))
        self.assertEqual(cats, {'liquidity': 4, 'price_volume': 8, 'momentum': 4, 'volatility': 2, 'trend': 3, 'risk': 3})


class LiquidityTests(unittest.TestCase):
    def test_avg_amount_and_turnover_and_big_order(self):
        p = panel_from([10.0] * 70, volume=[1000.0] * 70)
        self.assertAlmostEqual(fx.compute_factor('avg_amount', p).iloc[-1, 0], 10000.0)
        self.assertAlmostEqual(fx.compute_factor('turnover', p).iloc[-1, 0], 1.0)
        c = [10.0] * 30
        p2 = panel_from(c, high=c, low=[9.0] * 30, volume=[100.0] * 30)        # 收在最高价 -> 乘数恒为 1
        self.assertAlmostEqual(fx.compute_factor('big_order', p2).iloc[-1, 0], 1.0)
        p3 = panel_from(c, high=[11.0] * 30, low=c, volume=[100.0] * 30)       # 收在最低价 -> -1
        self.assertAlmostEqual(fx.compute_factor('big_order', p3).iloc[-1, 0], -1.0)

    def test_amihud_hand_calc(self):
        close = [10 * 1.01 ** i for i in range(40)]
        p = panel_from(close, volume=[1e6] * 40)
        c = np.array(close)
        expected = np.mean([0.01 / (c[i] * 1e6) for i in range(20, 40)]) * 1e8      # 最近 20 个收益
        self.assertAlmostEqual(fx.compute_factor('amihud', p).iloc[-1, 0], expected, places=9)


class PriceVolumeTests(unittest.TestCase):
    def test_wq12(self):
        p = panel_from([10, 11], volume=[100, 200])
        self.assertAlmostEqual(fx.compute_factor('wq12', p).iloc[-1, 0], -1.0)       # 放量上涨 -> sign=+1, -delta=-1
        p = panel_from([10, 11], volume=[200, 100])
        self.assertAlmostEqual(fx.compute_factor('wq12', p).iloc[-1, 0], 1.0)

    def test_wq54(self):
        p = panel_from([10.0], open_=[10.0], high=[11.0], low=[9.0])
        self.assertAlmostEqual(fx.compute_factor('wq54', p).iloc[-1, 0], -0.5)       # -(9-10)/(9-11)*(10/10)^5
        p = panel_from([10.0], open_=[10.0], high=[10.0], low=[10.0])
        self.assertTrue(np.isnan(fx.compute_factor('wq54', p).iloc[-1, 0]))           # 一字板除零 -> NaN

    def test_pv_corr_gap_closepos_reversal_range_skew(self):
        up = [1, 2, 3, 4, 5, 6]
        self.assertAlmostEqual(fx.compute_factor('pv_corr5', panel_from(up, volume=up)).iloc[-1, 0], 1.0)
        self.assertAlmostEqual(fx.compute_factor('pv_corr5', panel_from(up, volume=up[::-1])).iloc[-1, 0], -1.0)
        p = panel_from([10, 10], open_=[10, 10.5])
        self.assertAlmostEqual(fx.compute_factor('gap_open', p).iloc[-1, 0], 0.05)
        p = panel_from([10, 12], high=[10, 14], low=[10, 10])
        self.assertAlmostEqual(fx.compute_factor('close_pos', p).iloc[-1, 0], 0.5)
        self.assertAlmostEqual(fx.compute_factor('reversal1', p).iloc[-1, 0], -0.2)
        c = [10.0] * 25
        p = panel_from(c, high=[12.0] * 25, low=[8.0] * 25)
        self.assertAlmostEqual(fx.compute_factor('hl_range', p).iloc[-1, 0], 0.5)
        x = np.array([0.0] * 19 + [0.2])                          # 近 20 日 close/open-1：19 个 0 和 1 个 0.2
        n = 20
        m2 = np.mean((x - x.mean()) ** 2)
        g1 = np.mean((x - x.mean()) ** 3) / m2 ** 1.5
        expected = g1 * np.sqrt(n * (n - 1)) / (n - 2)           # pandas 的样本偏度
        close = [10.0] * 19 + [12.0]
        p = panel_from(close, open_=[10.0] * 20)
        self.assertAlmostEqual(fx.compute_factor('intraday_skew', p).iloc[-1, 0], expected, places=9)


class MomentumTests(unittest.TestCase):
    def test_mom20_and_mom252(self):
        c = [10 + 0.1 * i for i in range(300)]
        p = panel_from(c)
        self.assertAlmostEqual(fx.compute_factor('mom20', p).iloc[-1, 0], c[-1] / c[-21] - 1)
        self.assertAlmostEqual(fx.compute_factor('mom252', p).iloc[-1, 0], c[-21] / c[-253] - 1)
        self.assertTrue(np.isnan(fx.compute_factor('mom252', p).iloc[251, 0]))        # 数据不足为 NaN

    def test_resid_mom_subtracts_industry_mean(self):
        idx = pd.bdate_range('2025-01-01', periods=30)
        close = pd.DataFrame({'A': np.linspace(10, 12, 30), 'B': np.linspace(10, 14, 30), 'C': np.linspace(10, 11, 30)}, index=idx)
        p = fx.Panel(close, close, close, close, close * 0 + 1, industry=pd.Series({'A': 'x', 'B': 'x', 'C': 'y'}))
        r = close.iloc[-1] / close.iloc[-21] - 1
        out = fx.compute_factor('resid_mom', p).iloc[-1]
        self.assertAlmostEqual(out['A'], r['A'] - (r['A'] + r['B']) / 2)
        self.assertAlmostEqual(out['C'], 0.0)                                           # 行业里只有自己

    def test_rel_strength_subtracts_market(self):
        idx = pd.bdate_range('2025-01-01', periods=30)
        close = pd.DataFrame({'A': np.linspace(10, 12, 30)}, index=idx)
        mkt = pd.Series(0.001, index=idx)
        p = fx.Panel(close, close, close, close, close * 0 + 1, market_ret=mkt)
        expected = close['A'].iloc[-1] / close['A'].iloc[-21] - 1 - (1.001 ** 20 - 1)
        self.assertAlmostEqual(fx.compute_factor('rel_strength', p).iloc[-1, 0], expected)


class VolatilityTests(unittest.TestCase):
    def test_low_vol_hand_calc(self):
        rng = np.random.default_rng(3)
        r = rng.normal(0, 0.01, 80)
        close = 10 * np.cumprod(1 + r)
        p = panel_from(close)
        expected = np.std(pd.Series(close).pct_change().iloc[-60:], ddof=1)
        self.assertAlmostEqual(fx.compute_factor('low_vol', p).iloc[-1, 0], expected, places=12)

    def test_idio_vol_zero_when_stock_is_pure_beta(self):
        rng = np.random.default_rng(4)
        m = pd.Series(rng.normal(0, 0.01, 100), index=pd.bdate_range('2025-01-01', periods=100))
        close = (1 + 2 * m).cumprod() * 10
        p = fx.Panel(close.to_frame('A'), close.to_frame('A'), close.to_frame('A'), close.to_frame('A'),
                     close.to_frame('A') * 0 + 1, market_ret=m)
        self.assertLess(fx.compute_factor('idio_vol', p).iloc[-1, 0], 1e-6)
        noisy = close * (1 + rng.normal(0, 0.01, 100))
        p2 = fx.Panel(noisy.to_frame('A'), noisy.to_frame('A'), noisy.to_frame('A'), noisy.to_frame('A'),
                      noisy.to_frame('A') * 0 + 1, market_ret=m)
        self.assertGreater(fx.compute_factor('idio_vol', p2).iloc[-1, 0], 0.005)


class TrendTests(unittest.TestCase):
    def test_ma_bull(self):
        up = panel_from([10 + i for i in range(70)])
        self.assertTrue(fx.compute_factor('ma_bull', up).iloc[-1, 0])
        down = panel_from([100 - i for i in range(70)])
        self.assertFalse(fx.compute_factor('ma_bull', down).iloc[-1, 0])
        self.assertFalse(fx.compute_factor('ma_bull', up).iloc[10, 0])                  # 不足 60 根为 False

    def test_breakout_boundaries(self):
        base = [10.0] * 20
        self.assertTrue(fx.compute_factor('breakout20', panel_from(base + [10.01])).iloc[-1, 0])
        self.assertFalse(fx.compute_factor('breakout20', panel_from(base + [10.0])).iloc[-1, 0])       # 等于前高不算
        v = [100.0] * 20
        self.assertTrue(fx.compute_factor('vol_breakout', panel_from(base + [10.5], volume=v + [151.0])).iloc[-1, 0])
        self.assertFalse(fx.compute_factor('vol_breakout', panel_from(base + [10.5], volume=v + [150.0])).iloc[-1, 0])
        self.assertFalse(fx.compute_factor('vol_breakout', panel_from(base + [10.0], volume=v + [900.0])).iloc[-1, 0])


class RiskFilterTests(unittest.TestCase):
    def test_liquidity_st_suspension(self):
        p = panel_from([10.0] * 30, volume=[2e7] * 30)          # 日均 2 亿
        self.assertTrue(fx.compute_factor('liq_filter', p).iloc[-1, 0])
        p = panel_from([10.0] * 30, volume=[1e7] * 30)          # 恰好 1 亿，不是 ">"
        self.assertFalse(fx.compute_factor('liq_filter', p).iloc[-1, 0])
        p = panel_from([10.0] * 3, names=pd.Series({'A': '*ST某某'}))
        self.assertFalse(fx.compute_factor('st_filter', p).iloc[-1, 0])
        p = panel_from([10.0] * 3, names=pd.Series({'A': '平安银行'}))
        self.assertTrue(fx.compute_factor('st_filter', p).iloc[-1, 0])
        p = panel_from([10.0, 10.0, np.nan], volume=[100, 100, np.nan])
        self.assertFalse(fx.compute_factor('susp_filter', p).iloc[-1, 0])
        p = panel_from([10.0, 10.0], volume=[100, 0])
        self.assertFalse(fx.compute_factor('susp_filter', p).iloc[-1, 0])


class TriggerTests(unittest.TestCase):
    def test_score_trigger_uses_direction_and_quantile(self):
        idx = pd.bdate_range('2025-01-01', periods=1)
        vals = pd.DataFrame([list(range(1, 11))], index=idx, columns=list('ABCDEFGHIJ'), dtype=float)
        hi = fx.trigger_mask('avg_amount', vals).iloc[-1]       # direction=+1：最大的 10%
        self.assertEqual(list(hi[hi].index), ['J'])
        lo = fx.trigger_mask('low_vol', vals).iloc[-1]          # direction=-1：最小的 10%
        self.assertEqual(list(lo[lo].index), ['A'])


class NoLookaheadTests(unittest.TestCase):
    def test_all_factors_no_lookahead(self):
        p = random_panel()
        for fid in fx.FACTORS:
            for cut in (80, 270, 300):
                with self.subTest(factor=fid, cut=cut):
                    self.assertTrue(fx.check_no_lookahead(fid, p, cut))

    def test_assertion_catches_a_leaky_function(self):
        p = random_panel(n=120)
        original = fx.FUNCS['mom20']
        try:
            fx.FUNCS['mom20'] = lambda panel: panel.close.shift(-1) / panel.close - 1      # 故意偷看明天
            with self.assertRaises(AssertionError):
                fx.check_no_lookahead('mom20', p, 100)
        finally:
            fx.FUNCS['mom20'] = original


if __name__ == '__main__':
    unittest.main()
