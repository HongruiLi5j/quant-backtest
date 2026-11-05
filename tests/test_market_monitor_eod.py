"""T006 S3c：收盘后分析端到端测试。合成 parquet + 临时目录；不联网，不读写 data/。"""
from datetime import date, datetime
import csv
import json
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.market_monitor import eod_analysis as eod
from src.market_monitor.core import BEIJING
from src.market_monitor.factors import FACTORS
sys.path.insert(0, str(ROOT / 'scripts'))
import run_eod_analysis as script

DAY = '2026-03-31'                                          # 封存段起点 2026-04-01 的前一个交易日（分析日必须早于封存段）
NOW = datetime(2026, 4, 1, 10, 0, tzinfo=BEIJING)           # 次日早晨，当天 K 线已完整
N_CODES, N_DAYS = 30, 300


def make_daily(root: Path, shock_code='600003', end=DAY, n_codes=N_CODES):
    """写 N_CODES 只合成股票到 <root>/cn_all，另写 cn_all_list.csv。shock_code 最后一天暴涨，应出现在残差排名顶部。"""
    daily = root / 'cn_all'
    daily.mkdir(parents=True)
    rng = np.random.default_rng(11)
    idx = pd.bdate_range(end=end, periods=N_DAYS)
    mkt = rng.normal(0, 0.008, N_DAYS)
    rows = []
    for i in range(n_codes):
        code = f'{600000 + i}'
        ret = 1.2 * mkt + rng.normal(0, 0.01, N_DAYS)
        if code == shock_code:
            ret[-1] += 0.08
        close = 10 * np.cumprod(1 + ret)
        open_ = np.r_[close[0], close[:-1]] * (1 + rng.normal(0, 0.003, N_DAYS))
        df = pd.DataFrame({'Open': open_, 'High': np.maximum(open_, close) * 1.004, 'Low': np.minimum(open_, close) * 0.996,
                           'Close': close, 'Volume': rng.uniform(5e6, 2e7, N_DAYS)}, index=idx)
        df.to_parquet(daily / f'{code}.parquet')
        rows.append({'code': code, 'name': 'ST测试' if i == 1 else f'股票{i}', 'ipo': '2000-01-01', 'industry': 'A行业' if i % 2 else 'B行业', 'board': '沪市主板'})
    pd.DataFrame(rows).to_csv(root / 'cn_all_list.csv', index=False)
    return daily


def write_jsonl(path: Path, codes):
    path.write_text(''.join(json.dumps({'type': 'track', 'day': DAY, 'code': c, 'price': 10.0}) + '\n' for c in codes) + 'not json\n', encoding='utf-8')


class EodEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.daily = make_daily(self.root)
        self.jsonl = self.root / f'{DAY}.jsonl'
        write_jsonl(self.jsonl, ['600003', '600005', '699999'])      # 699999 不在日线里
        self.out = self.root / 'out' / f'{DAY}_eod'

    def tearDown(self):
        self.tmp.cleanup()

    def run_it(self, **kw):
        return eod.run_eod_analysis(DAY, str(self.jsonl), str(self.daily), str(self.out), now=NOW, **kw)

    def test_end_to_end_outputs(self):
        res = self.run_it()
        self.assertEqual(set(Path(p).name for p in res['files'].values()), {'factor_summary.csv', 'residual_zscore_rank.csv', 'eod_report.md'})
        with open(res['files']['factors'], encoding='utf-8-sig') as f:
            rows = list(csv.DictReader(f))
        self.assertEqual({r['id'] for r in rows}, set(FACTORS))
        self.assertEqual(len(rows), 24)
        self.assertIn(res['regime'], ('range', 'trend_up', 'trend_down', 'high_vol'))
        self.assertEqual(res['scaling'], {'range': 1.0, 'trend_up': 0.6, 'trend_down': 0.6, 'high_vol': 0.25}[res['regime']])
        by = {r['id']: r for r in rows}
        self.assertEqual(int(by['st_filter']['n_trigger']), N_CODES - 1)          # 恰有一只 ST 被剔除
        self.assertEqual(res['monitored'], ['600003', '600005', '699999'])
        self.assertEqual(int(by['susp_filter']['monitor_overlap']), 2)             # 699999 没有日线，不计重合
        # 暴涨股应排在残差 Z-score 第一
        self.assertEqual(list(res['top'].index)[0], '600003')
        self.assertNotIn('600001', list(res['top'].index) + list(res['bottom'].index))      # ST 股被过滤
        report = Path(res['files']['report']).read_text(encoding='utf-8')
        for text in ('市场状态', '残差 Z-score', '成本会计恒等式', '不是买卖建议', '跳过'):
            self.assertIn(text, report)
        with open(res['files']['residual'], encoding='utf-8-sig') as f:
            sides = [r['side'] for r in csv.DictReader(f)]
        self.assertEqual(sides.count('watch'), 14)            # 29 只合格 → k=min(20, 29//2)=14，两边各 14 且互斥
        self.assertEqual(sides.count('avoid'), 14)
        self.assertFalse(set(res['top'].index) & set(res['bottom'].index))

    def test_baseline_columns_and_rates_in_range(self):
        res = self.run_it()
        for r in res['factor_rows']:
            for key in ('hit_random60', 'hit_amount60', 'hit_gain60', 'hit_monitor'):
                self.assertTrue(r[key] is None or 0.0 <= r[key] <= 1.0, (r['id'], key))
        # 同一 seed 重跑结果一致（随机基准可复现）
        out2 = self.root / 'second'
        res2 = eod.run_eod_analysis(DAY, str(self.jsonl), str(self.daily), str(out2), now=NOW)
        self.assertEqual([r['hit_random60'] for r in res['factor_rows']], [r['hit_random60'] for r in res2['factor_rows']])

    def test_refuses_to_overwrite(self):
        self.run_it()
        report = self.out / 'eod_report.md'
        before = report.read_bytes()
        with self.assertRaises(FileExistsError):
            self.run_it()
        self.assertEqual(report.read_bytes(), before)

    def test_empty_directory_ok_but_nonempty_refused(self):
        self.out.mkdir(parents=True)
        self.run_it()                                                # 已存在的空目录可以用
        other = self.root / 'other'
        other.mkdir()
        (other / 'x.txt').write_text('keep', encoding='utf-8')
        with self.assertRaises(FileExistsError):
            eod.run_eod_analysis(DAY, str(self.jsonl), str(other), str(other), now=NOW)
        self.assertEqual((other / 'x.txt').read_text(encoding='utf-8'), 'keep')

    def test_symlink_output_refused(self):
        target = self.root / 'real'
        target.mkdir()
        link = self.root / 'link'
        link.symlink_to(target)
        with self.assertRaises(FileExistsError):
            eod.run_eod_analysis(DAY, str(self.jsonl), str(self.daily), str(link), now=NOW)

    def test_empty_inputs(self):
        empty = self.root / 'empty_daily'
        empty.mkdir()
        out = self.root / 'never'
        with self.assertRaises(eod.EodError) as cm:
            eod.run_eod_analysis(DAY, str(self.jsonl), str(empty), str(out), now=NOW)
        self.assertEqual(cm.exception.args[0], 'no_daily_data')
        self.assertFalse(out.exists())                                # 失败时不留下输出目录
        with self.assertRaises(eod.EodError):
            eod.run_eod_analysis(DAY, str(self.jsonl), str(self.root / 'missing'), str(out), now=NOW)
        # jsonl 缺失/为空：照常运行，只加警告
        self.jsonl.write_text('', encoding='utf-8')
        res = self.run_it()
        self.assertEqual(res['monitored'], [])
        self.assertTrue(any('jsonl' in w for w in res['warnings']))

    def test_day_validation_and_partial_bar_rule(self):
        with self.assertRaises(eod.EodError):
            eod.run_eod_analysis('2026/10/23', str(self.jsonl), str(self.daily), str(self.out), now=NOW)
        before = datetime(2026, 3, 31, 15, 29, tzinfo=BEIJING)
        with self.assertRaises(eod.EodError) as cm:                  # 15:30 前当天 K 线是盘中半天数据，不能用
            eod.run_eod_analysis(DAY, str(self.jsonl), str(self.daily), str(self.out), now=before)
        self.assertEqual(cm.exception.args[0], 'no_bar_for_day')
        after = datetime(2026, 3, 31, 15, 30, tzinfo=BEIJING)
        self.assertEqual(eod.run_eod_analysis(DAY, str(self.jsonl), str(self.daily), str(self.out), now=after)['day'], DAY)
        with self.assertRaises(eod.EodError) as cm:
            eod.run_eod_analysis(DAY, str(self.jsonl), str(self.daily), str(self.root / 'f'), now=datetime(2026, 3, 30, 20, 0, tzinfo=BEIJING))
        self.assertEqual(cm.exception.args[0], 'future_day')

    def test_future_bars_are_not_used(self):
        """day 之后的 K 线必须被截掉：把 DAY 前一天当分析日，结果不应依赖 DAY 当天的数据。"""
        prev = (pd.Timestamp(DAY) - pd.tseries.offsets.BDay(1)).strftime('%Y-%m-%d')
        a = eod.run_eod_analysis(prev, str(self.jsonl), str(self.daily), str(self.root / 'a'), now=NOW)
        for f in self.daily.glob('*.parquet'):                        # 改写 DAY 当天的数据
            df = pd.read_parquet(f)
            df.iloc[-1] = df.iloc[-1] * 1.5
            df.to_parquet(f)
        b = eod.run_eod_analysis(prev, str(self.jsonl), str(self.daily), str(self.root / 'b'), now=NOW)
        self.assertEqual(list(a['top'].index), list(b['top'].index))
        self.assertEqual(a['regime'], b['regime'])

    def test_cli_script(self):
        out = self.root / 'cli_out'
        argv = ['--day', DAY, '--output', str(out), '--jsonl', str(self.jsonl), '--daily-dir', str(self.daily)]
        self.assertEqual(script.main(argv, now=NOW), 0)
        self.assertTrue((out / 'eod_report.md').exists())
        self.assertEqual(script.main(argv, now=NOW), 2)               # 第二次拒绝覆盖
        self.assertEqual(script.main(['--day', 'bad', '--output', str(self.root / 'x')], now=NOW), 1)


class LockedBoundary(unittest.TestCase):
    """封存段（2026-04-01 起）：分析日落在其中一律拒绝；分析日之前的分析不得读到封存段的行。"""

    def test_post_lock_days_allowed(self):
        """当日复盘不受封存段限制（eod_analysis.LOCKED_SEGMENT_START 的注释）：封存段只约束 run_factor_scan 等历史回测/因子筛选。
        分析日在 2026-04-01 之后可以正常出报告；面板只含分析日及以前的行（不含未来）。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            daily = make_daily(root, end='2026-04-10')
            jsonl = root / 'j.jsonl'
            write_jsonl(jsonl, ['600003'])
            now = datetime(2026, 12, 1, 10, 0, tzinfo=BEIJING)
            for day in ('2026-04-01', '2026-04-10'):
                out = root / ('o_' + day)
                res = eod.run_eod_analysis(day, str(jsonl), str(daily), str(out), now=now)
                self.assertEqual(res['day'], day)
                self.assertTrue(out.exists())
            panel = eod.load_panel(str(daily), '2026-04-01', now=now)
            self.assertEqual(panel.close.index.max(), pd.Timestamp('2026-04-01'))      # 不含 04-02 及以后的行

    def test_factor_scan_still_guards_locked_segment(self):
        """封存段守卫仍在因子筛选脚本里：它引用 LOCKED_SEGMENT_START（2026-04-01）。"""
        self.assertEqual(eod.LOCKED_SEGMENT_START, date(2026, 4, 1))
        self.assertIn("LOCKED_SEGMENT_START", Path(script.__file__).parent.joinpath("run_factor_scan.py").read_text(encoding="utf-8"))

    def test_locked_boundary(self):
        """分析日 2026-03-31：parquet 里即使有 04-01 及之后的行，面板也不含它们。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            daily = make_daily(root, end='2026-04-10')
            now = datetime(2026, 12, 1, 10, 0, tzinfo=BEIJING)
            panel = eod.load_panel(str(daily), '2026-03-31', now=now)
            self.assertEqual(panel.close.index.max(), pd.Timestamp('2026-03-31'))
            self.assertTrue((panel.close.index < pd.Timestamp('2026-04-01')).all())
            res = eod.run_eod_analysis('2026-03-31', str(root / 'none.jsonl'), str(daily), str(root / 'ok'), now=now)
            self.assertEqual(res['day'], '2026-03-31')


class WatchAvoidDisjoint(unittest.TestCase):
    def ranked(self, n):
        return pd.Series(np.linspace(3, -3, n), index=[f'{600000 + i}' for i in range(n)])

    def test_watch_avoid_disjoint(self):
        for n, expect in ((200, 20), (40, 20), (29, 14), (5, 2), (2, 1), (1, 0), (0, 0)):
            top, bottom = eod.split_watch_avoid(self.ranked(n))
            self.assertEqual((len(top), len(bottom)), (expect, expect), n)
            self.assertFalse(set(top.index) & set(bottom.index), n)
            if expect:
                self.assertGreater(top.min(), bottom.max())            # 关注的 Z 都高于避开的


class AccountingIdentity(unittest.TestCase):
    BASE = dict(start_nav=1_000_000.0, price_pnl=12_000.0, dividend_pnl=500.0, trading_fee=300.0, borrow_fee=0.0, financing_fee=0.0)

    def test_identity_holds(self):
        led = dict(self.BASE, end_nav=1_000_000 + 12_000 + 500 - 300)
        r = eod.accounting_identity(led)
        self.assertTrue(r['ok'])
        self.assertAlmostEqual(r['deviation'], 0.0)
        self.assertAlmostEqual(r['lhs'], 12_200.0)

    def test_short_side_terms_reduce_nav(self):
        led = dict(self.BASE, borrow_fee=100.0, financing_fee=50.0, end_nav=1_000_000 + 12_000 + 500 - 300 - 150)
        self.assertTrue(eod.accounting_identity(led)['ok'])

    def test_deviation_boundary_at_0_1_pct(self):
        good = 1_000_000 + 12_000 + 500 - 300
        ok = eod.accounting_identity(dict(self.BASE, end_nav=good + 1000.0))          # 偏差恰好 0.1%
        self.assertTrue(ok['ok'])
        bad = eod.accounting_identity(dict(self.BASE, end_nav=good + 1000.01))
        self.assertFalse(bad['ok'])
        self.assertAlmostEqual(bad['deviation_pct'], 0.00100001, places=8)

    def test_invalid_ledgers(self):
        for led in (None, {}, dict(self.BASE, end_nav=float('nan')), dict(self.BASE, end_nav=True), dict(self.BASE, start_nav=0.0, end_nav=1.0)):
            with self.assertRaises(eod.EodError):
                eod.accounting_identity(led)

    def test_dividend_required(self):
        """股息盈亏必填：缺字段不能悄悄当 0 而显示"通过"；借券费/融资费可省略（A 股为 0）。"""
        led = dict(self.BASE, end_nav=1_000_000 + 12_000 + 500 - 300)
        no_div = {k: v for k, v in led.items() if k != 'dividend_pnl'}
        with self.assertRaises(eod.EodError) as cm:
            eod.accounting_identity(no_div)
        self.assertEqual(cm.exception.args[0], 'missing_dividend')
        no_fees = {k: v for k, v in led.items() if k not in ('borrow_fee', 'financing_fee')}
        self.assertTrue(eod.accounting_identity(no_fees)['ok'])

    def test_warning_goes_into_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            daily = make_daily(root)
            jsonl = root / 'j.jsonl'
            write_jsonl(jsonl, ['600003'])
            led = dict(self.BASE, end_nav=1_000_000 + 12_000 + 500 - 300 + 5_000)         # 差 0.5%
            res = eod.run_eod_analysis(DAY, str(jsonl), str(daily), str(root / 'o1'), now=NOW, ledger=led)
            self.assertTrue(any('成本会计恒等式' in w for w in res['warnings']))
            self.assertIn('警告：超过 0.1%', Path(res['files']['report']).read_text(encoding='utf-8'))
            good = dict(self.BASE, end_nav=1_000_000 + 12_000 + 500 - 300)
            res = eod.run_eod_analysis(DAY, str(jsonl), str(daily), str(root / 'o2'), now=NOW, ledger=good)
            self.assertFalse(any('成本会计恒等式' in w for w in res['warnings']))
            self.assertIn('通过', Path(res['files']['report']).read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
