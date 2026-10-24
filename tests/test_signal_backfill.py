"""T006 S3d：信号对账回填测试。合成账本 + 合成日线 + 临时目录；不联网，不读写 data/。"""
from datetime import datetime
import hashlib
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.market_monitor.core import BEIJING
sys.path.insert(0, str(ROOT / 'scripts'))
import signal_backfill as sb

NOW = datetime(2026, 3, 1, 20, 0, tzinfo=BEIJING)
IDX = pd.bdate_range('2026-01-05', periods=40)            # 2026-01-05 起 40 个交易日
SCHEMA = """CREATE TABLE signals(day TEXT, strategy TEXT, code TEXT, name TEXT, action TEXT, now REAL, next REAL, close REAL,
    liquidity TEXT, small INTEGER, flags TEXT, PRIMARY KEY(day, strategy, code));"""


def write_stock(daily: Path, code, open_, close):
    pd.DataFrame({'Open': open_, 'Close': close}, index=IDX).to_parquet(daily / f'{code}.parquet')


def make_world(root: Path):
    """5 只股票。000001：次日开盘 10，之后每天收盘 +1%；000002：次日开盘 10，之后每天 -1%；其余三只走平，用来做全市场均值。"""
    daily = root / 'cn_all'
    daily.mkdir(parents=True)
    n = len(IDX)
    up = 10 * 1.01 ** np.arange(n)
    down = 10 * 0.99 ** np.arange(n)
    flat = np.full(n, 10.0)
    for code, close in (('000001', up), ('000002', down), ('000003', flat), ('000004', flat), ('000005', flat)):
        # 开盘价取"前一日收盘"，这样 open[D+1] = close[D]，收益好手算
        open_ = np.r_[close[0], close[:-1]]
        write_stock(daily, code, open_, close)
    return daily


def make_ledger(path: Path, rows):
    c = sqlite3.connect(path)
    c.executescript(SCHEMA)
    c.executemany('INSERT INTO signals(day, strategy, code, name, action) VALUES (?,?,?,?,?)', rows)
    c.commit()
    c.close()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class BackfillTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.daily = make_world(self.root)
        self.day = str(IDX[2].date())                      # 信号日 2026-01-07；入场 = 下一交易日开盘 = 当日收盘
        self.late = str(IDX[-3].date())                    # 很晚的信号日：h=5、20 还没走完
        self.ledger = self.root / 'ledger.db'
        make_ledger(self.ledger, [
            (self.day, 'blend2', '000001', '甲', '买入'),
            (self.day, 'blend2', '000002', '乙', '卖出'),
            (self.day, 'blend2', '000003', '丙', '加仓'),
            (self.day, 'ma_cross', '000002', '乙', '买入'),
            (self.late, 'blend2', '000001', '甲', '买入'),
            (self.day, 'blend2', '000004', '丁', '观望'),          # 不是四个动作之一：跳过
        ])
        self.asof = str(IDX[-1].date())

    def tearDown(self):
        self.tmp.cleanup()

    def run_bf(self, name='bf.csv', **kw):
        kw.setdefault('asof', self.asof)
        kw.setdefault('days', 365)
        return sb.run_backfill(self.ledger, self.daily, self.root / name, now=NOW, **kw)

    def test_returns_and_horizon_cut(self):
        res = self.run_bf()
        d = res['detail']
        self.assertEqual(len(d), 5)                              # '观望' 被跳过
        up = d[(d['day'] == self.day) & (d['code'] == '000001') & (d['strategy'] == 'blend2')].iloc[0]
        # 入场 open[D+1] = close[D] = 10*1.01^2；出场 close[D+h] = 10*1.01^(2+h) -> 收益 1.01^h - 1
        for h in sb.HORIZONS:
            self.assertAlmostEqual(up[f'ret_{h}'], 1.01 ** h - 1, places=9)
        late = d[(d['day'] == self.late)].iloc[0]
        self.assertAlmostEqual(late['ret_1'], 0.01, places=9)    # 倒数第 3 个交易日 + 1 = 倒数第 2，存在
        self.assertTrue(np.isnan(late['ret_5']))                 # 持有期没走完 -> 留空
        self.assertTrue(np.isnan(late['ret_20']))

    def test_group_stats_direction_and_baseline(self):
        res = self.run_bf()
        s = res['summary']

        def row(strategy, action, h):
            return s[(s['strategy'] == strategy) & (s['action'] == action) & (s['h'] == h)].iloc[0]

        sell = row('blend2', '卖出', 5)
        self.assertEqual(sell['n'], 1)
        self.assertAlmostEqual(sell['mean_ret'], 0.99 ** 5 - 1, places=9)
        self.assertAlmostEqual(sell['mean_adj'], -(0.99 ** 5 - 1), places=9)     # 卖出：跌了才算对
        self.assertEqual(sell['win_rate'], 1.0)
        buy = row('blend2', '买入', 1)
        self.assertEqual(buy['n'], 2)                                              # 信号日 + 晚信号日各一条
        self.assertAlmostEqual(buy['mean_net'], 0.01 - 0.003, places=9)           # 扣 30bp
        self.assertEqual(buy['win_rate'], 1.0)
        # 全市场均值：当天 5 只的 1 日收益均值 = (0.01 - 0.01 + 0 + 0 + 0)/5 = 0
        up_day = row('blend2', '买入', 1)
        self.assertLess(abs(up_day['mean_mkt']), 1e-9 + 1e-3)
        # blend2 整体 h=5 方向调整后：三条信号里 000003 加仓 = 0；000001 买入 = 1.01^5-1；000002 卖出 = 1-0.99^5（晚信号 h=5 为空）
        overall = row('blend2', '全部', 5)
        self.assertEqual(overall['n'], 3)
        self.assertAlmostEqual(overall['mean_adj'], ((1.01 ** 5 - 1) + 0 + (1 - 0.99 ** 5)) / 3, places=9)
        self.assertAlmostEqual(row('blend2', '全部', 5)['vs_blend2_adj'], 0.0, places=12)
        ma = row('ma_cross', '买入', 5)
        self.assertAlmostEqual(ma['vs_blend2_adj'], ma['mean_adj'] - overall['mean_adj'], places=12)   # 000002 买入亏
        self.assertLess(ma['mean_adj'], 0)
        self.assertEqual(ma['win_rate'], 0.0)
        self.assertTrue(bool(ma['small_sample']))

    def test_window_filters_by_days(self):
        res = self.run_bf(days=5)                     # 只回看 asof 前 5 个日历日 -> 早的信号日被排除，只剩 late
        self.assertEqual(set(res['detail']['day']), {self.late})

    def test_empty_ledger_and_missing_table_no_error(self):
        empty = self.root / 'empty.db'
        make_ledger(empty, [])
        res = sb.run_backfill(empty, self.daily, self.root / 'e.csv', days=90, asof=self.asof, now=NOW)
        self.assertEqual(res['n_signals'], 0)
        self.assertTrue(Path(res['files']['report']).read_text(encoding='utf-8').find('没有可核对的信号') >= 0)
        self.assertEqual(len(pd.read_csv(res['files']['detail'])), 0)
        notable = self.root / 'notable.db'
        sqlite3.connect(notable).close()
        res2 = sb.run_backfill(notable, self.daily, self.root / 'e2.csv', days=90, asof=self.asof, now=NOW)
        self.assertEqual(res2['n_signals'], 0)

    def test_ledger_untouched(self):
        before = sha(self.ledger)
        self.run_bf()
        self.assertEqual(sha(self.ledger), before)
        self.assertFalse((self.root / 'ledger.db-journal').exists())

    def test_no_overwrite(self):
        self.run_bf('x.csv')
        before = sha(self.root / 'x.csv')
        with self.assertRaises(FileExistsError):
            self.run_bf('x.csv')
        self.assertEqual(sha(self.root / 'x.csv'), before)
        # 只要有一个派生文件存在也拒绝
        (self.root / 'y_report.md').write_text('keep')
        with self.assertRaises(FileExistsError):
            self.run_bf('y.csv')
        self.assertFalse((self.root / 'y.csv').exists())
        self.assertEqual(sb.main(['--ledger', str(self.ledger), '--daily-dir', str(self.daily), '--output', str(self.root / 'x.csv'),
                                  '--asof', self.asof], now=NOW), 2)

    def test_errors(self):
        self.assertEqual(sb.main(['--ledger', str(self.root / 'nope.db'), '--daily-dir', str(self.daily), '--output', str(self.root / 'z.csv'),
                                  '--asof', self.asof], now=NOW), 1)
        empty = self.root / 'emptydir'
        empty.mkdir()
        self.assertEqual(sb.main(['--ledger', str(self.ledger), '--daily-dir', str(empty), '--output', str(self.root / 'z2.csv'),
                                  '--asof', self.asof], now=NOW), 1)
        self.assertEqual(sb.main(['--ledger', str(self.ledger), '--daily-dir', str(self.daily), '--output', str(self.root / 'z3.csv'),
                                  '--asof', 'bad'], now=NOW), 1)
        self.assertEqual(sb.main(['--days', '0', '--ledger', str(self.ledger), '--daily-dir', str(self.daily),
                                  '--output', str(self.root / 'z4.csv')], now=NOW), 1)
        self.assertEqual(sb.main(['--ledger', str(self.ledger), '--daily-dir', str(self.daily), '--output', str(self.root / 'ok.csv'),
                                  '--asof', self.asof], now=NOW), 0)

    def test_signal_on_non_trading_day_left_blank(self):
        make_ledger(self.root / 'w.db', [('2026-01-10', 'blend2', '000001', '甲', '买入')])      # 周六
        res = sb.run_backfill(self.root / 'w.db', self.daily, self.root / 'w.csv', days=365, asof=self.asof, now=NOW)
        self.assertEqual(res['n_signals'], 1)
        self.assertTrue(res['detail'][['ret_1', 'ret_20']].isna().all().all())


if __name__ == '__main__':
    unittest.main()
