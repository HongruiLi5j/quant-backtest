"""T006 S3b：实时信号跟踪（A 执行提醒 / B 信号追踪 / C 新信号探测）测试。
纯函数 + 临时目录；不联网、不读写 data/、不启动线程。"""
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.market_monitor import intraday as im, wiring
from src.market_monitor.core import BEIJING
from src.market_monitor.scheduler import MonitorScheduler
from src.market_monitor.store import Store

DAY = '2026-10-22'          # 周四
OPEN = datetime(2026, 10, 22, 9, 30, tzinfo=BEIJING)


def at(minutes):
    return OPEN + timedelta(minutes=minutes)


def sample(minutes, price, prev=10.0, open_=10.0, volume=0, name='测试'):
    return {'t': at(minutes), 'price': price, 'prev_close': prev, 'open': open_, 'volume': volume, 'name': name}


def kinds(events):
    return [(e['code'], e['kind']) for e in events]


class LimitRuleTests(unittest.TestCase):
    def test_board_percentages(self):
        self.assertEqual(im.limit_pct('600000'), 0.10)
        self.assertEqual(im.limit_pct('000001'), 0.10)
        self.assertEqual(im.limit_pct('300750'), 0.20)
        self.assertEqual(im.limit_pct('688001'), 0.20)
        self.assertEqual(im.limit_pct('600000', '*ST某某'), 0.05)
        self.assertEqual(im.limit_pct('300001', 'ST某某'), 0.05)     # ST 优先于创业板

    def test_limit_state_boundaries(self):
        self.assertEqual(im.limit_state('600000', '', 11.00, 10.0), 'up')       # 刚好涨停
        self.assertIsNone(im.limit_state('600000', '', 10.99, 10.0))
        self.assertEqual(im.limit_state('600000', '', 9.00, 10.0), 'down')      # 刚好跌停
        self.assertIsNone(im.limit_state('600000', '', 9.01, 10.0))
        self.assertEqual(im.limit_state('300750', '', 12.00, 10.0), 'up')
        self.assertIsNone(im.limit_state('300750', '', 11.00, 10.0))            # 创业板 +10% 不是涨停
        self.assertEqual(im.limit_state('600000', 'ST某', 10.50, 10.0), 'up')
        self.assertEqual(im.limit_state('600000', 'ST某', 9.50, 10.0), 'down')
        self.assertEqual(im.limit_state('600000', '', 10.99, 9.99), 'up')       # 9.99*1.1=10.989 -> 10.99
        for bad in (None, 0, -1, float('nan'), True):
            self.assertIsNone(im.limit_state('600000', '', bad, 10.0))
            self.assertIsNone(im.limit_state('600000', '', 10.0, bad))

    def test_distance_to_limits(self):
        up, dn = im.distance_to_limits('600000', '', 10.0, 10.0)
        self.assertAlmostEqual(up, 10.0)
        self.assertAlmostEqual(dn, -10.0)
        self.assertEqual(im.distance_to_limits('600000', '', None, 10.0), (None, None))


class ExecutionHintTests(unittest.TestCase):
    BUY = [{'code': '600000', 'action': '买入'}]

    def test_buy_window_opens_within_five_minutes(self):
        ev = im.execution_hints(at(3), self.BUY, [], {'600000': sample(3, 10.0)})
        self.assertEqual(kinds(ev), [('600000', 'buy_window')])
        self.assertEqual(ev[0]['severity'], 'ok')
        self.assertIn('买入窗口打开', ev[0]['message'])
        self.assertEqual(ev[0]['timestamp'], at(3).isoformat())

    def test_buy_window_boundaries(self):
        snap = {'600000': sample(0, 10.0)}
        self.assertEqual(kinds(im.execution_hints(at(0), self.BUY, [], snap)), [('600000', 'buy_window')])     # 9:30 整
        self.assertEqual(kinds(im.execution_hints(at(4.99), self.BUY, [], snap)), [('600000', 'buy_window')])
        self.assertEqual(im.execution_hints(at(5), self.BUY, [], snap), [])                                    # 9:35 起关闭
        self.assertEqual(im.execution_hints(at(-1), self.BUY, [], snap), [])

    def test_buy_window_skips_held_and_non_buy(self):
        snap = {'600000': sample(1, 10.0)}
        self.assertEqual(im.execution_hints(at(1), self.BUY, ['600000'], snap), [])        # 已持有 = 已执行
        self.assertEqual(im.execution_hints(at(1), [{'code': '600000', 'action': '卖出'}], [], snap), [])

    def test_buy_window_not_on_weekend(self):
        sat = datetime(2026, 10, 24, 9, 31, tzinfo=BEIJING)
        self.assertEqual(im.execution_hints(sat, self.BUY, [], {}), [])

    def test_limit_up_blocks_buy(self):
        ev = im.execution_hints(at(60), self.BUY, [], {'600000': sample(60, 11.00)})
        self.assertEqual(kinds(ev), [('600000', 'limit_up_no_buy')])
        self.assertEqual(ev[0]['severity'], 'danger')
        self.assertEqual(im.execution_hints(at(60), self.BUY, [], {'600000': sample(60, 10.99)}), [])   # 差一分不算
        # 涨停但清单里没有买入信号：不提醒
        self.assertEqual(im.execution_hints(at(60), [], [], {'600000': sample(60, 11.00)}), [])

    def test_limit_down_blocks_sell_by_signal_or_holding(self):
        snap = {'600000': sample(60, 9.00)}
        sell = [{'code': '600000', 'action': '卖出'}]
        self.assertEqual(kinds(im.execution_hints(at(60), sell, [], snap)), [('600000', 'limit_down_no_sell')])
        ev = im.execution_hints(at(60), [], ['600000'], snap)
        self.assertIn(('600000', 'limit_down_no_sell'), kinds(ev))
        self.assertEqual(im.execution_hints(at(60), [], [], snap), [])         # 既不持有也无卖出信号

    def test_board_specific_limits(self):
        sig = [{'code': '300750', 'action': '买入'}]
        self.assertEqual(im.execution_hints(at(60), sig, [], {'300750': sample(60, 11.0)}), [])             # 创业板 +10% 不是涨停
        self.assertEqual(kinds(im.execution_hints(at(60), sig, [], {'300750': sample(60, 12.0)})), [('300750', 'limit_up_no_buy')])
        st = [{'code': '600001', 'action': '买入'}]
        self.assertEqual(kinds(im.execution_hints(at(60), st, [], {'600001': sample(60, 10.5, name='ST某')})), [('600001', 'limit_up_no_buy')])

    def test_holding_drop_default_5pct_and_custom(self):
        snap = {'600000': sample(60, 9.4)}                                     # -6%
        self.assertEqual(kinds(im.execution_hints(at(60), [], ['600000'], snap)), [('600000', 'holding_drop')])
        self.assertEqual(im.execution_hints(at(60), [], ['600000'], {'600000': sample(60, 9.5)}), [])        # 恰好 -5% 不触发（严格大于）
        self.assertEqual(im.execution_hints(at(60), [], [], snap), [])                                        # 没持仓不提醒
        self.assertEqual(im.execution_hints(at(60), [], ['600000'], snap, thresholds={'drop_pct': 8}), [])

    def test_empty_and_garbage_inputs(self):
        self.assertEqual(im.execution_hints(at(1), [], [], {}), [])
        self.assertEqual(im.execution_hints(at(1), None, None, None), [])
        self.assertEqual(im.execution_hints(at(1), [None, {'code': 1}, 'x'], {'600000': {}}, {'600000': 'bad'}), [])
        with self.assertRaises(ValueError):
            im.execution_hints(datetime(2026, 10, 22, 9, 31), [], [], {})                                    # 无时区
        with self.assertRaises(ValueError):
            im.execution_hints(at(1), [], [], {}, thresholds={'drop_pct': -1})


class TrackSignalTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name).resolve() / 'intraday'
        self.signals = [{'code': '600000', 'action': '买入', 'name': '浦发', 'close': 10.0},
                        {'code': '600001', 'action': '卖出', 'close': 20.0}]
        self.history = {'600000': [sample(1, 10.1, open_=10.2), sample(30, 10.71, open_=10.2)],
                        '600001': [sample(1, 19.0, prev=20.0, open_=19.5)]}

    def test_track_fields_and_returns(self):
        rec = im.track_signals(DAY, self.signals, self.history, now=at(31), directory_path=self.dir)
        buy, sell = rec
        self.assertEqual(buy['price'], 10.71)
        self.assertEqual(buy['open'], 10.2)
        self.assertEqual(buy['open_source'], 'exchange')
        self.assertAlmostEqual(buy['change_vs_open_pct'], 5.0)
        self.assertAlmostEqual(buy['return_since_open'], 5.0)
        self.assertAlmostEqual(buy['return_since_signal'], 7.1)          # 相对信号日收盘 10.0
        self.assertAlmostEqual(buy['to_limit_up_pct'], (11.0 / 10.71 - 1) * 100)
        self.assertAlmostEqual(buy['to_limit_down_pct'], (9.0 / 10.71 - 1) * 100)
        self.assertIsNone(sell['return_since_open'])                    # 卖出信号不计正确率
        self.assertIsNone(sell['return_since_signal'])

    def test_open_falls_back_to_first_quote(self):
        hist = {'600000': [dict(sample(1, 10.3), open=None), dict(sample(2, 10.6), open=None)]}
        rec = im.track_signals(DAY, self.signals[:1], hist, now=at(3), write=False)[0]
        self.assertEqual(rec['open'], 10.3)
        self.assertEqual(rec['open_source'], 'first_quote')

    def test_jsonl_append_only_and_permissions(self):
        im.track_signals(DAY, self.signals, self.history, now=at(31), directory_path=self.dir)
        im.track_signals(DAY, self.signals, self.history, now=at(32), directory_path=self.dir)
        path = self.dir / f'{DAY}.jsonl'
        lines = path.read_text(encoding='utf-8').splitlines()
        self.assertEqual(len(lines), 4)                                  # 追加而非覆盖
        self.assertEqual([json.loads(x)['ts'] for x in lines][:2], [at(31).isoformat()] * 2)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.dir.stat().st_mode), 0o700)

    def test_write_refuses_symlink_file(self):
        self.dir.mkdir(mode=0o700, parents=True)
        target = self.dir / 'elsewhere'
        target.write_text('x')
        (self.dir / f'{DAY}.jsonl').symlink_to(target)
        with self.assertRaises(OSError):
            im.append_jsonl(DAY, [{'a': 1}], self.dir)
        self.assertEqual(target.read_text(), 'x')

    def test_summary(self):
        hist = dict(self.history)
        hist['600002'] = [sample(1, 9.8, open_=10.0)]
        hist['600003'] = [sample(1, 10.0, open_=10.0)]
        sigs = self.signals + [{'code': '600002', 'action': '买入'}, {'code': '600003', 'action': '加仓'}]
        im.track_signals(DAY, sigs, hist, now=at(31), directory_path=self.dir)
        s = im.track_signals_summary(DAY, self.dir)
        self.assertEqual((s['total'], s['winners'], s['losers']), (3, 1, 1))            # 平盘既非赢也非输
        self.assertAlmostEqual(s['mean_return'], (5.0 - 2.0 + 0.0) / 3)
        self.assertAlmostEqual(s['median_return'], 0.0)

    def test_summary_uses_latest_record_per_code_and_skips_bad_lines(self):
        im.track_signals(DAY, self.signals[:1], {'600000': [sample(1, 10.3, open_=10.0)]}, now=at(2), directory_path=self.dir)
        im.track_signals(DAY, self.signals[:1], {'600000': [sample(9, 9.8, open_=10.0)]}, now=at(10), directory_path=self.dir)
        with open(self.dir / f'{DAY}.jsonl', 'a', encoding='utf-8') as f:
            f.write('not json\n[1,2]\n')
        s = im.track_signals_summary(DAY, self.dir)
        self.assertEqual((s['total'], s['winners'], s['losers']), (1, 0, 1))

    def test_empty_inputs(self):
        self.assertEqual(im.track_signals(DAY, [], {}, directory_path=self.dir), [])
        self.assertEqual(im.track_signals(DAY, self.signals, {}, directory_path=self.dir), [])
        self.assertFalse(self.dir.exists())                               # 没有记录就不建目录
        s = im.track_signals_summary(DAY, self.dir)
        self.assertEqual((s['total'], s['winners'], s['losers'], s['mean_return'], s['median_return']), (0, 0, 0, None, None))

    def test_other_day_samples_ignored_and_bad_day_rejected(self):
        old = {'600000': [dict(sample(1, 10.5), t=at(1) - timedelta(days=1))]}
        self.assertEqual(im.track_signals(DAY, self.signals, old, write=False), [])
        for bad in ('../x', '2026-1-2', None, '2026-10-22/../..'):
            with self.assertRaises(ValueError):
                im.track_signals(bad, self.signals, self.history, write=False)


class DetectNewSignalTests(unittest.TestCase):
    def hist(self, code, prices, open_=10.0, volume=0, start=0):
        return {code: [sample(start + i * 2, p, open_=open_, volume=volume) for i, p in enumerate(prices)]}

    def test_momentum_flags_unheld_as_watch(self):
        ev = im.detect_new_signals(DAY, self.hist('600000', [10.0, 10.1, 10.4], start=4), [])
        self.assertEqual(kinds(ev), [('600000', 'watch')])
        self.assertIn('momentum', ev[0]['params']['triggers'])
        self.assertIn(im.REFERENCE_NOTE, ev[0]['message'])
        self.assertTrue(ev[0]['reference_only'])
        self.assertEqual(ev[0]['severity'], 'watch')

    def test_momentum_threshold_boundary_and_wait_minutes(self):
        # 恰好 +3% 不触发（严格大于）
        self.assertEqual(im.detect_new_signals(DAY, {'600000': [sample(10, 10.3)]}, []), [])
        self.assertEqual(kinds(im.detect_new_signals(DAY, {'600000': [sample(10, 10.31)]}, [])), [('600000', 'watch')])
        # 开盘后不满 5 分钟不判断动量
        self.assertEqual(im.detect_new_signals(DAY, {'600000': [sample(4, 10.5)]}, []), [])

    def test_volume_ratio(self):
        hist = {'600000': [sample(10, 10.0, volume=300)]}                  # 300 手 = 30000 股
        self.assertEqual(im.detect_new_signals(DAY, hist, [], avg_volumes={'600000': 15000}), [])             # 恰好 2 倍不触发
        ev = im.detect_new_signals(DAY, hist, [], avg_volumes={'600000': 14000})
        self.assertEqual(kinds(ev), [('600000', 'watch')])
        self.assertIn('volume', ev[0]['params']['triggers'])
        self.assertEqual(im.detect_new_signals(DAY, hist, []), [])                                          # 缺均量：跳过该项
        self.assertEqual(im.detect_new_signals(DAY, hist, [], avg_volumes={'600000': 0}), [])

    def test_breakout_uses_prior_high_not_self(self):
        flat = self.hist('600000', [10.0, 10.05, 10.08])
        self.assertEqual(im.detect_new_signals(DAY, flat, []), [])
        ev = im.detect_new_signals(DAY, self.hist('600000', [10.0, 10.05, 10.16]), [])      # 10.16/10.05-1 > 1%
        self.assertEqual(ev[0]['params']['triggers'], ['breakout'])
        self.assertEqual(im.detect_new_signals(DAY, self.hist('600000', [10.0, 10.0, 10.1]), []), [])   # 恰好 1% 不触发

    def test_held_negative_gives_consider_sell(self):
        ev = im.detect_new_signals(DAY, self.hist('600000', [10.0, 9.9, 9.6], start=4), ['600000'])
        self.assertEqual(kinds(ev), [('600000', 'consider_sell')])
        self.assertEqual(ev[0]['severity'], 'danger')
        self.assertIn('考虑卖出', ev[0]['message'])
        self.assertIn('momentum', ev[0]['params']['triggers'])

    def test_held_volume_spike_with_falling_price_is_negative(self):
        hist = {'600000': [sample(10, 9.9, volume=300)]}
        ev = im.detect_new_signals(DAY, hist, ['600000'], avg_volumes={'600000': 10000})
        self.assertEqual(kinds(ev), [('600000', 'consider_sell')])
        self.assertEqual(ev[0]['params']['triggers'], ['volume'])

    def test_held_breakdown_negative(self):
        ev = im.detect_new_signals(DAY, self.hist('600000', [10.0, 9.95, 9.8], start=1), ['600000'])
        self.assertEqual(ev[0]['params']['triggers'], ['breakout'])

    def test_polarity_isolation(self):
        up = self.hist('600000', [10.0, 10.1, 10.5], start=4)
        down = self.hist('600001', [10.0, 9.9, 9.5], start=4)
        # 持仓股的正向 = 无事件；未持仓股的负向 = 无事件
        self.assertEqual(im.detect_new_signals(DAY, up, ['600000']), [])
        self.assertEqual(im.detect_new_signals(DAY, down, []), [])

    def test_universe_limit(self):
        hist = {}
        for i in range(70):
            hist.update(self.hist('600%03d' % i, [10.0, 10.1, 10.6], start=4))
        self.assertEqual(len(im.detect_new_signals(DAY, hist, [])), 60)                  # 默认 60
        self.assertEqual(len(im.detect_new_signals(DAY, hist, [], universe_limit=5)), 5)
        held = im.detect_new_signals(DAY, hist, ['600069'], universe_limit=1)           # 持仓股优先进入名单
        self.assertEqual([e['code'] for e in held], [])                                  # 持仓正向不报，但名额已被它占用
        for bad in (0, 61, True, 1.5, None):
            with self.assertRaises(ValueError):
                im.detect_new_signals(DAY, hist, [], universe_limit=bad)

    def test_empty_inputs_and_thresholds(self):
        self.assertEqual(im.detect_new_signals(DAY, {}, []), [])
        self.assertEqual(im.detect_new_signals(DAY, None, None), [])
        self.assertEqual(im.detect_new_signals(DAY, {'600000': []}, []), [])
        hist = {'600000': [sample(10, 10.2)]}
        self.assertEqual(im.detect_new_signals(DAY, hist, []), [])
        self.assertEqual(kinds(im.detect_new_signals(DAY, hist, [], thresholds={'momentum': 0.01})), [('600000', 'watch')])
        with self.assertRaises(ValueError):
            im.detect_new_signals(DAY, hist, [], thresholds={'nope': 1})

    def test_trigger_tracking(self):
        hist = self.hist('600000', [10.0, 10.1, 10.5], start=4)
        ev = im.detect_new_signals(DAY, hist, [])
        trig = im.register_triggers({}, ev)
        self.assertEqual(list(trig), ['600000:watch'])
        hist['600000'].append(sample(12, 10.7))
        again = im.register_triggers(trig, im.detect_new_signals(DAY, hist, []))
        self.assertEqual(again['600000:watch']['trigger_price'], 10.5)                 # 首次触发价不被覆盖
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name).resolve() / 'x'
        rec = im.track_triggers(DAY, trig, hist, now=at(13), directory_path=d)
        self.assertAlmostEqual(rec[0]['return_since_trigger'], (10.7 / 10.5 - 1) * 100)
        self.assertEqual(rec[0]['type'], 'trigger')
        self.assertEqual(len(im.read_jsonl(DAY, d)), 1)
        # 触发记录不进入正确率汇总
        self.assertEqual(im.track_signals_summary(DAY, d)['total'], 0)


class SchedulerIntegrationTests(unittest.TestCase):
    """不启动线程：直接塞内存数据，调用一轮后的分析步骤。"""

    def make(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        self.now = at(3)
        s = MonitorScheduler(store=Store(root=root / 'store'), now=lambda: self.now,
                             monotonic=lambda: self.mono, avg_volume_fn=lambda codes: {})
        self.mono = 1000.0
        s._plan = {'codes': ['600000', '600001'], 'sources': {'600000': ['signal'], '600001': ['holding']},
                   'targets': {}, 'signals': {'600000': {'action': '买入', 'close': 10.0}}, 'truncated': False}
        s._history = {'600000': [sample(1, 10.1)], '600001': [sample(1, 9.4)]}
        self.root = root
        return s

    def test_default_dir_follows_store_and_default_avg_is_off_for_test_store(self):
        s = self.make()
        self.assertEqual(s.intraday_dir, self.root / 'market_monitor_intraday')
        self.assertEqual(s.avg_volume_fn(['600000']), {})
        self.assertEqual(MonitorScheduler().intraday_dir, im.INTRADAY_DIR)          # 默认存储对应 data/market_monitor_intraday（只是路径，不创建）

    def test_step_produces_events_tracking_and_jsonl_once_per_minute(self):
        s = self.make()
        s._intraday_step()
        ev = s.intraday_events()
        self.assertEqual(sorted(kinds(ev)), [('600000', 'buy_window'), ('600001', 'holding_drop')])
        tr = s.intraday_tracking()
        self.assertEqual(tr['summary']['total'], 1)
        self.assertEqual(tr['rows'][0]['code'], '600000')
        path = self.root / 'market_monitor_intraday' / f'{DAY}.jsonl'
        self.assertEqual(len(path.read_text().splitlines()), 1)
        s._intraday_step()                                                          # 同一分钟内：事件去重、不重复写盘
        self.assertEqual(len(s.intraday_events()), 2)
        self.assertEqual(len(path.read_text().splitlines()), 1)
        self.mono += 61
        s._intraday_step()
        self.assertEqual(len(path.read_text().splitlines()), 2)

    def test_views_are_copies_and_stop_clears_memory(self):
        s = self.make()
        s._intraday_step()
        s.intraday_events().clear()
        self.assertEqual(len(s.intraday_events()), 2)
        s.stop()
        self.assertEqual(s.intraday_events(), [])
        self.assertEqual(s.intraday_tracking()['rows'], [])

    def test_write_failure_does_not_raise(self):
        s = self.make()
        blocker = self.root / 'market_monitor_intraday'
        blocker.parent.mkdir(parents=True, exist_ok=True)
        blocker.write_text('a file, not a directory')
        s._intraday_step()
        self.assertTrue(s.intraday_tracking()['write_error'])
        self.assertEqual(len(s.intraday_events()), 2)


class WiringTests(unittest.TestCase):
    class Fake:
        def intraday_events(self):
            return [{'code': '600000'}]

        def intraday_tracking(self):
            return {'rows': [], 'summary': {}}

        def quotes(self):
            return []

        def status(self):
            return {}

    GOOD = wiring.Ctx(True, True, True, True)

    def test_handlers_read_only_and_gated(self):
        f = self.Fake()
        self.assertEqual(wiring.handle_intraday_events(self.GOOD, f), (200, {'ok': True, 'events': [{'code': '600000'}]}))
        self.assertEqual(wiring.handle_intraday_tracking(self.GOOD, f)[1]['tracking'], {'rows': [], 'summary': {}})
        for ctx in (wiring.Ctx(), wiring.Ctx(True, True, False, True), wiring.Ctx(True, False, True, True), wiring.Ctx(1, 1, 1, 1)):
            self.assertEqual(wiring.handle_intraday_events(ctx, f), wiring.FORBIDDEN)
            self.assertEqual(wiring.handle_intraday_tracking(ctx, f), wiring.FORBIDDEN)
            self.assertEqual(wiring.handle_quotes(ctx, f), wiring.FORBIDDEN)

    def test_quotes_carries_intraday_when_available(self):
        self.assertIn('intraday', wiring.handle_quotes(self.GOOD, self.Fake())[1])

        class Old:
            def quotes(self): return []
            def status(self): return {}
        self.assertNotIn('intraday', wiring.handle_quotes(self.GOOD, Old())[1])


if __name__ == '__main__':
    unittest.main()
