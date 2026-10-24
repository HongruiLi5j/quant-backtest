"""T006 S3d：全市场历史因子扫描测试。合成 parquet + 临时目录；不联网，不读写 data/。"""
from datetime import datetime
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np
import pandas as pd
from scipy import stats

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.market_monitor.core import BEIJING
from src.market_monitor.factors import FACTORS
sys.path.insert(0, str(ROOT / 'scripts'))
import run_factor_scan as rfs

NOW = datetime(2026, 10, 23, 10, 0, tzinfo=BEIJING)
N_DAYS, N_CODES = 420, 40


def make_daily(root: Path, end='2026-03-31', n_days=N_DAYS, n_codes=N_CODES, poison_after=None, drop_after=None):
    """写合成日线到 <root>/cn_all。poison_after 给定时，该日期之后的价格全部改成极端值（用来证明扫描不读封存段）；drop_after 给定时，该日期之后的行直接丢掉。"""
    daily = root / 'cn_all'
    daily.mkdir(parents=True)
    rng = np.random.default_rng(5)
    idx = pd.bdate_range(end=end, periods=n_days)
    mkt = rng.normal(0, 0.008, n_days)
    for i in range(n_codes):
        ret = mkt + rng.normal(0, 0.012, n_days)
        close = 10 * np.cumprod(1 + ret)
        open_ = np.r_[close[0], close[:-1]] * (1 + rng.normal(0, 0.003, n_days))
        df = pd.DataFrame({'Open': open_, 'High': np.maximum(open_, close) * 1.01, 'Low': np.minimum(open_, close) * 0.99,
                           'Close': close, 'Volume': rng.uniform(1e6, 5e6, n_days)}, index=idx)
        if poison_after:
            df.loc[df.index > pd.Timestamp(poison_after), ['Open', 'High', 'Low', 'Close']] *= 50
        if drop_after:
            df = df[df.index <= pd.Timestamp(drop_after)]
        df.to_parquet(daily / f'{600000 + i}.parquet')
    return daily


class MetricsTest(unittest.TestCase):
    def test_ic_perfect_and_reversed(self):
        rng = np.random.default_rng(1)
        v = pd.DataFrame(rng.normal(size=(3, 60)))
        fwd = v * 0.01 + 0.002                      # 单调递增变换：秩完全一致
        m = rfs.day_metrics(v, fwd)
        self.assertTrue(np.allclose(m['ic'], 1.0))
        m2 = rfs.day_metrics(v, -fwd)
        self.assertTrue(np.allclose(m2['ic'], -1.0))

    def test_ic_matches_scipy_with_nans(self):
        rng = np.random.default_rng(2)
        v = pd.DataFrame(rng.normal(size=(5, 80)))
        fwd = pd.DataFrame(0.5 * v.values + rng.normal(size=(5, 80)))
        v.iloc[0, :10] = np.nan
        fwd.iloc[1, 5:15] = np.nan
        m = rfs.day_metrics(v, fwd)
        for i in range(5):
            ok = v.iloc[i].notna() & fwd.iloc[i].notna()
            expect = stats.spearmanr(v.iloc[i][ok], fwd.iloc[i][ok])[0]
            self.assertAlmostEqual(m['ic'].iloc[i], expect, places=10)
            self.assertEqual(m['n'].iloc[i], ok.sum())

    def test_too_few_stocks_gives_nan(self):
        v = pd.DataFrame(np.arange(10.0).reshape(1, 10))
        m = rfs.day_metrics(v, v * 0.01)
        self.assertTrue(np.isnan(m['ic'].iloc[0]))

    def test_deciles_monotonic_and_tmb(self):
        v = pd.DataFrame([np.arange(100.0)] * 4)
        fwd = v * 0.001
        m = rfs.day_metrics(v, fwd)
        g = m[rfs.GROUP_COLS].iloc[0].values
        self.assertTrue((np.diff(g) > 0).all())
        # 每组 10 只：第 1 组 0..9 均值 0.0045，第 10 组 90..99 均值 0.0945
        self.assertAlmostEqual(g[0], 0.0045)
        self.assertAlmostEqual(g[-1], 0.0945)
        self.assertTrue(np.allclose(m['tmb'], 0.09))

    def test_summary_monotonicity_and_direction(self):
        rng = np.random.default_rng(3)
        days = pd.date_range('2025-01-01', periods=60)
        v = pd.DataFrame(rng.normal(size=(60, 100)), index=days)
        fwd_up = v * 0.01 + rng.normal(0, 0.0005, v.shape)
        m = rfs.day_metrics(v, fwd_up)
        m.insert(0, 'factor', 'reversal1')                 # reversal1 方向 +1
        m['day'] = m.index.strftime('%Y-%m-%d')
        s = rfs.summarize(m.reset_index(drop=True), {'reversal1': FACTORS['reversal1']}).iloc[0]
        self.assertGreater(s['mono_rho'], 0.95)
        self.assertGreater(s['ic_mean'], 0.9)
        self.assertTrue(bool(s['mono_ok']))
        # 方向 -1 的因子：同样数据，调整后应为负
        m['factor'] = 'gap_open'
        s2 = rfs.summarize(m.reset_index(drop=True), {'gap_open': FACTORS['gap_open']}).iloc[0]
        self.assertLess(s2['ic_mean'], -0.9)
        self.assertLess(s2['mono_rho'], -0.95)
        self.assertFalse(bool(s2['mono_ok']))

    def test_nw_t_larger_se_for_autocorrelated(self):
        rng = np.random.default_rng(4)
        e = rng.normal(size=400)
        x = np.convolve(e, np.ones(20) / 20, mode='valid') + 0.05      # 20 日滑动平均：强自相关
        self.assertLess(rfs.nw_t(x), rfs.naive_t(x))
        self.assertTrue(np.isnan(rfs.nw_t([1.0, 2.0])))

    def test_flag_factor_metrics(self):
        v = pd.DataFrame([[True] * 20 + [False] * 40] * 3)
        fwd = pd.DataFrame([[0.05] * 20 + [0.01] * 40] * 3)
        m = rfs.day_metrics(v, fwd, kind='filter')
        self.assertTrue(np.allclose(m['tmb'], 0.04))
        self.assertTrue(np.allclose(m['n_trigger'], 20))
        self.assertTrue(m[rfs.GROUP_COLS].isna().all().all())


class ScanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.daily = make_daily(self.root)
        self.logs = []

    def tearDown(self):
        self.tmp.cleanup()

    def run_scan(self, out, **kw):
        kw.setdefault('chunk_days', 30)
        return rfs.run_factor_scan(self.daily, '2025-12-01', '2026-03-31', self.root / out, now=NOW, progress=self.logs.append, **kw)

    def test_end_to_end_outputs(self):
        res = self.run_scan('o1')
        out = self.root / 'o1'
        for name in (rfs.SUMMARY_FILE, rfs.DAILY_FILE, rfs.REPORT_FILE):
            self.assertTrue((out / name).is_file())
        s = pd.read_csv(out / rfs.SUMMARY_FILE)
        self.assertEqual(set(s['factor']), set(FACTORS))
        d = pd.read_parquet(out / rfs.DAILY_FILE)
        self.assertEqual(d['factor'].nunique(), 24)
        self.assertEqual(len(d), 24 * res['meta']['n_days'])
        # 最后 20 个交易日没有完整未来收益 -> IC 为空
        last = d[(d['factor'] == 'mom20') & (d['day'] == d['day'].max())]
        self.assertTrue(last['ic'].isna().all())
        self.assertTrue(any('chunk' in line for line in self.logs))

    def test_no_overwrite_final_and_foreign_dir(self):
        self.run_scan('o1')
        with self.assertRaises(FileExistsError):
            self.run_scan('o1')
        foreign = self.root / 'foreign'
        foreign.mkdir()
        (foreign / 'x.txt').write_text('keep')
        with self.assertRaises(FileExistsError):
            self.run_scan('foreign')
        self.assertEqual((foreign / 'x.txt').read_text(), 'keep')
        self.assertEqual(rfs.main(['--start', '2025-12-01', '--end', '2026-03-31', '--output', str(self.root / 'o1'),
                                   '--daily-dir', str(self.daily)], now=NOW), 2)

    def test_resume_matches_full_run(self):
        self.run_scan('full')
        with self.assertRaises(rfs.ScanInterrupted):
            self.run_scan('part', stop_after_chunks=1)
        part = self.root / 'part'
        self.assertEqual(len(list((part / rfs.CKPT_DIR).glob('chunk_*.parquet'))), 1)
        self.assertFalse((part / rfs.SUMMARY_FILE).exists())
        self.logs.clear()
        self.run_scan('part')
        self.assertTrue(any('断点' in line for line in self.logs))
        a = pd.read_parquet(self.root / 'full' / rfs.DAILY_FILE)
        b = pd.read_parquet(part / rfs.DAILY_FILE)
        pd.testing.assert_frame_equal(a, b)
        pd.testing.assert_frame_equal(pd.read_csv(self.root / 'full' / rfs.SUMMARY_FILE), pd.read_csv(part / rfs.SUMMARY_FILE))

    def test_resume_with_different_params_refused(self):
        with self.assertRaises(rfs.ScanInterrupted):
            self.run_scan('part', stop_after_chunks=1)
        with self.assertRaises(rfs.ScanError) as cm:
            self.run_scan('part', chunk_days=20)
        self.assertEqual(str(cm.exception), 'checkpoint_mismatch')

    def test_corrupt_checkpoint_recomputed(self):
        with self.assertRaises(rfs.ScanInterrupted):
            self.run_scan('part', stop_after_chunks=1)
        ck = next((self.root / 'part' / rfs.CKPT_DIR).glob('chunk_00000.parquet'))
        ck.write_bytes(b'not a parquet')
        self.run_scan('part')
        self.run_scan('full')
        pd.testing.assert_frame_equal(pd.read_parquet(self.root / 'part' / rfs.DAILY_FILE), pd.read_parquet(self.root / 'full' / rfs.DAILY_FILE))

    def test_empty_input_raises(self):
        empty = self.root / 'empty'
        empty.mkdir()
        with self.assertRaises(rfs.ScanError) as cm:
            rfs.run_factor_scan(empty, '2025-12-01', '2026-03-31', self.root / 'o', now=NOW, progress=lambda *_: None)
        self.assertEqual(str(cm.exception), 'no_daily_data')
        with self.assertRaises(rfs.ScanError):
            rfs.run_factor_scan(self.root / 'missing', '2025-12-01', '2026-03-31', self.root / 'o', now=NOW, progress=lambda *_: None)
        self.assertFalse((self.root / 'o').exists())
        self.assertEqual(rfs.main(['--start', '2025-12-01', '--end', '2026-03-31', '--output', str(self.root / 'o'),
                                   '--daily-dir', str(empty)], now=NOW), 1)

    def test_bad_arguments(self):
        for start, end, code in (('2026-03-01', '2025-12-01', 'bad_range'), ('xx', '2026-03-31', 'invalid_date'),
                                 ('2026-04-02', '2026-10-22', 'start_in_locked'), ('2026-01-03', '2026-01-04', 'no_days_in_range')):
            with self.assertRaises(rfs.ScanError) as cm:
                rfs.run_factor_scan(self.daily, start, end, self.root / 'ob', now=NOW, progress=lambda *_: None)
            self.assertEqual(str(cm.exception), code)

    def test_locked_segment_clamped_and_never_read(self):
        clean = make_daily(self.root / 'a', end='2026-05-26', n_days=N_DAYS + 40, drop_after='2026-03-31')
        # 同样的历史，再加上封存段之后的 40 个交易日（极端价格）：结果必须与截断数据完全一致
        full = make_daily(self.root / 'b', end='2026-05-26', n_days=N_DAYS + 40, poison_after='2026-03-31')
        r1 = rfs.run_factor_scan(clean, '2025-12-01', '2026-10-22', self.root / 'oa', now=NOW, chunk_days=30, progress=lambda *_: None)
        r2 = rfs.run_factor_scan(full, '2025-12-01', '2026-10-22', self.root / 'ob2', now=NOW, chunk_days=30, progress=lambda *_: None)
        self.assertTrue(r1['warnings'] and r2['warnings'])
        d = pd.read_parquet(self.root / 'ob2' / rfs.DAILY_FILE)
        self.assertLess(d['day'].max(), '2026-04-01')
        self.assertEqual(r2['meta']['end_effective'], '2026-03-31')
        pd.testing.assert_frame_equal(pd.read_parquet(self.root / 'oa' / rfs.DAILY_FILE), d)

    def test_today_half_bar_dropped(self):
        make = make_daily(self.root / 'c', end='2026-03-31')
        now_early = datetime(2026, 3, 31, 10, 0, tzinfo=BEIJING)         # 15:30 前：当天 K 线不能用
        panel = rfs.load_history(make, pd.Timestamp('2026-03-31'), now_early)
        self.assertEqual(panel.close.index.max(), pd.Timestamp('2026-03-30'))
        panel2 = rfs.load_history(make, pd.Timestamp('2026-03-31'), datetime(2026, 3, 31, 16, 0, tzinfo=BEIJING))
        self.assertEqual(panel2.close.index.max(), pd.Timestamp('2026-03-31'))


if __name__ == '__main__':
    unittest.main()
