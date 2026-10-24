"""T015 S3 美股实时分析三块测试：纯函数 + 临时目录；合成数据，不联网、不读写真实 data/。"""
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.us_market_monitor import intraday
from src.us_market_monitor.core import US_EASTERN

DAY = '2026-10-22'
OPEN = datetime(2026, 10, 22, 9, 30, tzinfo=US_EASTERN)


def sample(minutes, price, volume=1000, opening=100.0, prev_close=100.0, high=None, low=None, day=OPEN):
    return {'t': day + timedelta(minutes=minutes), 'price': price, 'prev_close': prev_close, 'open': opening,
            'high': high, 'low': low, 'volume': volume, 'name': ''}


def kinds(events):
    return sorted((e['code'], e['kind']) for e in events)


class ExecutionHintTests(unittest.TestCase):
    def window(self, now, signals=({'code': 'AAPL', 'action': '买入'},), held=()):
        return intraday.execution_hints(now, signals, held, {})

    def test_window_endpoints_inclusive_in_eastern_time(self):
        cases = [(OPEN - timedelta(seconds=1), False), (OPEN, True), (OPEN + timedelta(minutes=5), True),
                 (OPEN + timedelta(minutes=5, seconds=1), False)]
        for now, expected in cases:
            self.assertEqual(bool(self.window(now)), expected, now)

    def test_window_follows_daylight_saving_via_utc(self):
        # 2026-03-08 美国夏令时开始：同一个 UTC 钟点在前后两天对应不同的美东钟点
        winter = datetime(2026, 3, 6, 14, 30, tzinfo=timezone.utc)     # EST: 09:30
        summer_same_utc = datetime(2026, 3, 9, 14, 30, tzinfo=timezone.utc)   # EDT: 10:30
        summer_open = datetime(2026, 3, 9, 13, 30, tzinfo=timezone.utc)       # EDT: 09:30
        self.assertTrue(self.window(winter))
        self.assertFalse(self.window(summer_same_utc))
        self.assertTrue(self.window(summer_open))
        # 北京时间构造：夏令时 21:30 开盘，冬令时 22:30 开盘
        beijing = timezone(timedelta(hours=8))
        self.assertTrue(self.window(datetime(2026, 10, 22, 21, 30, tzinfo=beijing)))
        self.assertFalse(self.window(datetime(2026, 10, 22, 22, 30, tzinfo=beijing)))
        self.assertTrue(self.window(datetime(2026, 11, 2, 22, 30, tzinfo=beijing)))

    def test_weekend_has_no_window(self):
        saturday = datetime(2026, 10, 24, 9, 30, tzinfo=US_EASTERN)
        self.assertEqual(self.window(saturday), [])

    def test_only_unheld_buy_signals_get_window(self):
        self.assertEqual(self.window(OPEN, signals=({'code': 'AAPL', 'action': '卖出'},)), [])
        self.assertEqual(self.window(OPEN, held=['AAPL']), [])
        events = self.window(OPEN, signals=({'code': 'AAPL', 'action': '买入'}, {'code': 'MSFT', 'action': '买入'}))
        self.assertEqual(kinds(events), [('AAPL', 'buy_window'), ('MSFT', 'buy_window')])
        self.assertIn('-04:00', events[0]['timestamp'])

    def test_holding_drop_threshold_is_strict_and_configurable(self):
        snap = lambda price: {'AAPL': sample(10, price)}
        run = lambda price, th=None: intraday.execution_hints(OPEN, [], ['AAPL'], snap(price), thresholds=th)
        self.assertEqual(run(93.0), [])                                  # 恰好 -7% 不触发（严格大于）
        self.assertEqual(kinds(run(92.9)), [('AAPL', 'holding_drop')])
        self.assertEqual(kinds(run(94.0, {'drop_pct': 5.0})), [('AAPL', 'holding_drop')])
        self.assertEqual(run(92.9, {'drop_pct': 10.0}), [])

    def test_unheld_stock_never_gets_drop_and_no_limit_events_exist(self):
        crash = {'AAPL': sample(10, 50.0), 'MSFT': sample(10, 110.0)}
        events = intraday.execution_hints(OPEN + timedelta(hours=1),
                                          [{'code': 'AAPL', 'action': '卖出'}, {'code': 'MSFT', 'action': '买入'}], [], crash)
        self.assertEqual(events, [])                                     # 美股没有涨跌停提示，也没有"持仓"以外的跌幅提示
        text = (ROOT / 'src' / 'us_market_monitor' / 'intraday.py').read_text(encoding='utf-8')
        self.assertNotIn("'limit_up_no_buy'", text)
        self.assertNotIn("'limit_down_no_sell'", text)

    def test_bad_inputs(self):
        with self.assertRaises(ValueError):
            intraday.execution_hints(datetime(2026, 10, 22, 9, 30), [], [], {})            # 不带时区
        for bad in ({'drop_pct': True}, {'drop_pct': -1}, {'drop_pct': math.nan}, {'unknown': 1}, {'drop_pct': 0}):
            with self.assertRaises(ValueError, msg=bad):
                intraday.execution_hints(OPEN, [], [], {}, thresholds=bad)
        self.assertEqual(intraday.execution_hints(OPEN, [{'code': 1, 'action': None}, 'x'], None, None), [])


class TrackingTests(unittest.TestCase):
    def history(self):
        return {'AAPL': [sample(0, 100.0, high=100.0, low=100.0), sample(5, 103.0, high=104.0, low=99.0),
                         sample(10, 102.0, high=104.0, low=99.0)]}

    def test_buy_signal_record_has_us_columns_and_returns(self):
        rows = intraday.track_signals(DAY, [{'code': 'AAPL', 'action': '买入', 'close': 98.0}], self.history(), now=OPEN, write=False)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r['open'], r['open_source'], r['price']), (100.0, 'exchange', 102.0))
        self.assertAlmostEqual(r['return_since_open'], 2.0)
        self.assertAlmostEqual(r['return_since_signal'], (102 / 98 - 1) * 100)
        self.assertAlmostEqual(r['to_day_high_pct'], (104 / 102 - 1) * 100)
        self.assertAlmostEqual(r['to_day_low_pct'], (99 / 102 - 1) * 100)
        self.assertGreaterEqual(r['to_day_high_pct'], 0)
        self.assertLessEqual(r['to_day_low_pct'], 0)
        for gone in ('to_limit_up_pct', 'to_limit_down_pct'):
            self.assertNotIn(gone, r)

    def test_open_falls_back_to_first_quote_and_sell_has_no_return(self):
        hist = {'AAPL': [dict(s, open=None) for s in self.history()['AAPL']]}
        buy = intraday.track_signals(DAY, [{'code': 'AAPL', 'action': '加仓'}], hist, now=OPEN, write=False)[0]
        self.assertEqual((buy['open'], buy['open_source']), (100.0, 'first_quote'))
        sell = intraday.track_signals(DAY, [{'code': 'AAPL', 'action': '卖出'}], hist, now=OPEN, write=False)[0]
        self.assertIsNone(sell['return_since_open'])
        self.assertIsNone(sell['return_since_signal'])

    def test_day_is_eastern_date_not_utc_or_beijing(self):
        late = datetime(2026, 10, 22, 23, 30, tzinfo=US_EASTERN)         # 北京已是 10-23 11:30
        hist = {'AAPL': [{'t': late, 'price': 100.0, 'open': 100.0, 'prev_close': 100.0, 'volume': 1}]}
        self.assertEqual(len(intraday.track_signals(DAY, [{'code': 'AAPL', 'action': '买入'}], hist, now=OPEN, write=False)), 1)
        self.assertEqual(intraday.track_signals('2026-10-23', [{'code': 'AAPL', 'action': '买入'}], hist, now=OPEN, write=False), [])

    def test_missing_or_bad_data_gives_no_rows(self):
        self.assertEqual(intraday.track_signals(DAY, [{'code': 'AAPL', 'action': '买入'}], {}, now=OPEN, write=False), [])
        self.assertEqual(intraday.track_signals(DAY, [{'code': 'AAPL', 'action': '买入'}],
                                                {'AAPL': [{'t': OPEN, 'price': -1.0}]}, now=OPEN, write=False), [])
        for bad_day in ('2026-1-1', '../x', 5, None):
            with self.assertRaises(ValueError):
                intraday.track_signals(bad_day, [], {}, now=OPEN, write=False)

    def test_summary_counts_buy_signals_only(self):
        rows = intraday.track_signals(DAY, [{'code': 'AAPL', 'action': '买入'}, {'code': 'AAPL', 'action': '卖出'}],
                                      self.history(), now=OPEN, write=False)
        summary = intraday.summarize(rows)
        self.assertEqual((summary['total'], summary['winners'], summary['losers']), (1, 1, 0))

    def test_jsonl_roundtrip_in_temp_dir_with_private_modes(self):
        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw).resolve() / 'us_intraday'
            rows = intraday.track_signals(DAY, [{'code': 'AAPL', 'action': '买入'}], self.history(), now=OPEN,
                                          directory_path=folder)
            self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
            file = folder / f'{DAY}.jsonl'
            self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)
            file.write_text(file.read_text(encoding='utf-8') + 'not json\n', encoding='utf-8')
            self.assertEqual(intraday.read_jsonl(DAY, folder)[0]['code'], rows[0]['code'])      # 坏行被跳过
            self.assertEqual(intraday.track_signals_summary(DAY, folder)['total'], 1)
            self.assertEqual(intraday.read_jsonl('2026-10-21', folder), [])
            import os
            os.symlink(folder, Path(raw) / 'link')
            self.assertEqual(intraday.read_jsonl(DAY, Path(raw) / 'link'), [])                   # 符号链接按"没有数据"
            with self.assertRaises(Exception):
                intraday.append_jsonl(DAY, rows, Path(raw) / 'link')

    def test_default_dir_is_us_only(self):
        self.assertEqual(intraday.INTRADAY_DIR.name, 'us_market_monitor_intraday')
        self.assertNotEqual(intraday.INTRADAY_DIR.name, 'market_monitor_intraday')


class NewSignalTests(unittest.TestCase):
    AVG = {'AAPL': 1_000_000.0}

    def detect(self, samples, avg=None, held=(), thresholds=None, code='AAPL', **kw):
        return intraday.detect_new_signals(DAY, {code: samples}, held, avg_volumes=avg if avg is not None else self.AVG,
                                           now=OPEN + timedelta(hours=1), thresholds=thresholds, **kw)

    def test_momentum_is_strictly_greater_than_threshold(self):
        self.assertEqual(self.detect([sample(5, 103.0)]), [])                            # 恰好 3%
        events = self.detect([sample(5, 103.01)])
        self.assertEqual(kinds(events), [('AAPL', 'watch')])
        self.assertEqual(events[0]['params']['triggers'], ['momentum'])
        self.assertTrue(events[0]['reference_only'])
        self.assertAlmostEqual(events[0]['params']['momentum'], 3.01, places=3)

    def test_momentum_waits_until_minutes_after_open(self):
        self.assertEqual(self.detect([sample(4, 110.0)]), [])
        self.assertEqual(kinds(self.detect([sample(5, 110.0)])), [('AAPL', 'watch')])
        self.assertEqual(self.detect([sample(5, 103.0)], thresholds={'momentum': 0.02})[0]['params']['triggers'], ['momentum'])

    def test_volume_in_shares_not_multiplied(self):
        scaled = self.detect([sample(1, 100.0, volume=20_000)])
        self.assertEqual(scaled, [])                                                      # 若误乘 100 就会误报放量
        self.assertEqual(self.detect([sample(1, 100.0, volume=2_000_000)]), [])           # 恰好 2 倍不触发
        events = self.detect([sample(1, 100.0, volume=2_000_001)])
        self.assertEqual(events[0]['params']['triggers'], ['volume'])
        self.assertAlmostEqual(events[0]['params']['volume_ratio'], 2.000001, places=5)

    def test_volume_surge_below_open_is_negative_for_holdings(self):
        events = self.detect([sample(1, 99.0, volume=3_000_000)], held=['AAPL'])
        self.assertEqual(kinds(events), [('AAPL', 'consider_sell')])
        self.assertEqual(events[0]['severity'], 'danger')

    def test_missing_average_volume_marks_insufficient_and_skips_volume(self):
        events = self.detect([sample(5, 104.0, volume=10 ** 12)], avg={})
        self.assertEqual(events[0]['params']['triggers'], ['momentum'])
        self.assertEqual(events[0]['params']['volume_data'], 'insufficient')
        self.assertNotIn('volume_ratio', events[0]['params'])
        self.assertFalse(events[0].get('display_only'))                                   # 缺均量不等于流动性不足

    def test_breakout_uses_prior_prices_only(self):
        flat = [sample(1, 100.0), sample(2, 100.5), sample(3, 100.2)]
        self.assertEqual(self.detect(flat), [])
        up = self.detect(flat + [sample(4, 101.6)])                                      # 高点 100.5 → +1.09%
        self.assertEqual(up[0]['params']['triggers'], ['breakout'])
        self.assertEqual(self.detect(flat + [sample(4, 100.5 * 1.01)]), [])              # 恰好 1% 不触发
        down = self.detect(flat + [sample(4, 98.9)], held=['AAPL'])
        self.assertEqual(kinds(down), [('AAPL', 'consider_sell')])
        self.assertEqual(self.detect([sample(1, 150.0)]), [])                            # 只有一笔：无"此前"价格，不判突破

    def test_thresholds_are_configurable_and_validated(self):
        self.assertEqual(self.detect([sample(1, 100.0), sample(2, 100.6)], thresholds={'breakout': 0.005})[0]['params']['triggers'], ['breakout'])
        for bad in ({'breakout': 0}, {'momentum': True}, {'volume_ratio': math.inf}, {'zzz': 1}):
            with self.assertRaises(ValueError, msg=bad):
                self.detect([sample(1, 100.0)], thresholds=bad)

    def test_illiquid_stocks_are_display_only(self):
        cheap = self.detect([sample(5, 2.3, opening=2.0, prev_close=2.0)], avg={'AAPL': 1e9})
        self.assertTrue(cheap[0]['display_only'])                                         # 价格低于 5 美元
        thin = self.detect([sample(5, 104.0)], avg={'AAPL': 50_000.0})                    # 均成交额约 520 万 < 1000 万
        self.assertTrue(thin[0]['display_only'])
        fine = self.detect([sample(5, 104.0)], avg={'AAPL': 200_000.0})                   # 约 2080 万
        self.assertNotIn('display_only', fine[0])
        relaxed = self.detect([sample(5, 104.0)], avg={'AAPL': 50_000.0}, thresholds={'min_dollar_volume': 1_000_000})
        self.assertNotIn('display_only', relaxed[0])

    def test_universe_limit_and_held_first(self):
        history = {f'S{chr(65 + i)}': [sample(5, 110.0)] for i in range(5)}
        events = intraday.detect_new_signals(DAY, history, ['SE'], universe_limit=2, avg_volumes={}, now=OPEN)
        self.assertEqual([e['code'] for e in events], ['SA'])            # SE 持仓但没有负向信号；上限 2 只 → SE、SA 被扫描
        for bad in (0, 31, True, 2.0):
            with self.assertRaises(ValueError):
                intraday.detect_new_signals(DAY, {}, [], bad)

    def test_register_and_track_triggers(self):
        events = self.detect([sample(5, 104.0)])
        triggers = intraday.register_triggers({}, events)
        again = intraday.register_triggers(triggers, events)
        self.assertEqual(len(again), 1)
        rows = intraday.track_triggers(DAY, triggers, {'AAPL': [sample(5, 104.0), sample(15, 105.04)]}, now=OPEN, write=False)
        self.assertAlmostEqual(rows[0]['return_since_trigger'], 1.0, places=2)
        self.assertEqual(rows[0]['type'], 'trigger')

    def test_default_avg_volumes_reads_local_cache_only(self):
        import pandas as pd
        frame = pd.DataFrame({'Volume': [1000.0] * 20})
        with patch('src.datasource.load_cached', return_value=frame) as loaded:
            self.assertEqual(intraday.default_avg_volumes(['AAPL']), {'AAPL': 1000.0})
        loaded.assert_called_once_with('us', 'AAPL')
        with patch('src.datasource.load_cached', side_effect=OSError):
            self.assertEqual(intraday.default_avg_volumes(['AAPL']), {})
        with patch('src.datasource.load_cached', return_value=pd.DataFrame({'Volume': [1.0, 2.0]})):
            self.assertEqual(intraday.default_avg_volumes(['AAPL']), {})                 # 样本太少不返回

    def test_events_are_json_serialisable(self):
        events = self.detect([sample(5, 104.0)]) + intraday.execution_hints(OPEN, [{'code': 'AAPL', 'action': '买入'}], [], {})
        json.dumps(events, allow_nan=False)


if __name__ == '__main__':
    unittest.main()
