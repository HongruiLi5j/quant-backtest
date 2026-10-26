"""T006 S3 调度器/名单测试：假请求、假时钟、临时目录；不联网、不读写 data/、不开子进程。"""
from datetime import datetime, timedelta
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.market_monitor import scheduler as sched_mod
from src.market_monitor import watchlist
from src.market_monitor.core import BEIJING, Quote
from src.market_monitor.providers import FeedError
from src.market_monitor.scheduler import MonitorScheduler
from src.market_monitor.store import Store

THURSDAY_OPEN = datetime(2026, 10, 22, 9, 30, tzinfo=BEIJING)
SATURDAY = datetime(2026, 10, 24, 10, 0, tzinfo=BEIJING)
LUNCH = datetime(2026, 10, 22, 12, 0, tzinfo=BEIJING)


class FakeTime:
    """同一个假时钟同时驱动 now() 和 monotonic()；wait 只推进假时间，真实只睡 0.5ms 让出 CPU。"""
    def __init__(self, start):
        self.start, self.t = start, 0.0
        self.waits = 0

    def now(self):
        return self.start + timedelta(seconds=self.t)

    def monotonic(self):
        return self.t

    def wait(self, event, seconds):
        self.waits += 1
        if event.is_set():
            return
        self.t += max(0.0, seconds)
        time.sleep(0.0005)


def make_quote(symbol, price, at, volume):
    stamp = at.strftime('%Y%m%d%H%M%S')
    return Quote(symbol, '测试' + symbol[2:], price, 10.0, 10.0, str(volume), stamp, None, at, 0.1)


def until(predicate, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.002)
    return predicate()


class Harness:
    def __init__(self, test, start=THURSDAY_OPEN, codes=('600000',), targets=None, prices=None, **kwargs):
        self.clock = FakeTime(start)
        self.calls = []                  # (假单调时间, 代码元组)
        self.errors = []                 # 预置的错误序列，先进先出
        self.prices = prices or {}
        self.notified = []
        tmp = tempfile.TemporaryDirectory()
        test.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve() / 'store'      # macOS 的 /var 是符号链接，Store 逐层拒绝符号链接
        self.plan = {'codes': list(codes), 'sources': {c: ['watchlist'] for c in codes},
                     'targets': targets or {}, 'truncated': False}
        self.volume = 0
        kwargs.setdefault('batch_mode', False)    # 这些是 S3 单只模式的老测试；T016 起默认 batch_mode=True，批量见 test_scheduler_batch.py

        def request(symbols, cancel, timeout):
            self.calls.append((self.clock.t, symbols))
            if self.errors:
                err = self.errors.pop(0)
                if err is not None:
                    raise err
            out = []
            for s in symbols:
                self.volume += 100
                seq = self.prices.get(s[2:], [10.0])
                n = sum(1 for _, sym in self.calls if s in sym)
                out.append(make_quote(s, seq[min(n - 1, len(seq) - 1)], self.clock.now(), self.volume))
            return out

        self.s = MonitorScheduler(store=Store(root=self.root), request=request, build_fn=lambda n: self.plan,
                                  notify_fn=lambda t, b: self.notified.append((t, b)),
                                  now=self.clock.now, monotonic=self.clock.monotonic, wait=self.clock.wait, **kwargs)
        test.addCleanup(self.s.stop)


class SchedulerTests(unittest.TestCase):
    def test_default_construction_starts_nothing(self):
        before = threading.active_count()
        s = MonitorScheduler()
        self.assertEqual(s.status()['state'], 'stopped')
        self.assertEqual(threading.active_count(), before)
        self.assertEqual(s.store.root, sched_mod.WATCH_STORE_ROOT)        # 只是路径常量，构造不创建目录

    def test_polls_inside_session(self):
        h = Harness(self, codes=('600000', '000001'))
        self.assertTrue(h.s.start())
        self.assertTrue(until(lambda: len(h.calls) >= 4))
        h.s.stop()
        self.assertEqual({sym for _, syms in h.calls for sym in syms}, {'sh600000', 'sz000001'})
        self.assertTrue(all(len(syms) == 1 for _, syms in h.calls))     # 默认逐只，不合并多代码
        self.assertEqual(h.s.status()['state'], 'stopped')

    def test_quotes_visible_while_running(self):
        h = Harness(self, codes=('600000',))
        h.s.start()
        self.assertTrue(until(lambda: h.s.quotes() and h.s.quotes()[0]['price'] == 10.0))
        q = h.s.quotes()[0]
        self.assertEqual(q['code'], '600000')
        self.assertEqual(q['quality'], 'valid')
        self.assertEqual(q['pct'], 0.0)
        q['price'] = 999                                                  # 返回的是拷贝
        self.assertNotEqual(h.s.quotes()[0]['price'], 999)

    def test_no_polling_outside_session(self):
        for start in (SATURDAY, LUNCH):
            h = Harness(self, start=start)
            h.s.start()
            self.assertTrue(until(lambda: h.clock.waits >= 20))
            h.s.stop()
            self.assertEqual(h.calls, [], start)

    def test_rate_limits_per_symbol_and_between_requests(self):
        h = Harness(self, codes=('600000', '000001'))
        h.s.start()
        self.assertTrue(until(lambda: len(h.calls) >= 8))
        h.s.stop()
        times = [t for t, _ in h.calls]
        self.assertTrue(all(b - a >= 10 for a, b in zip(times, times[1:])), times)
        for sym in ('sh600000', 'sz000001'):
            own = [t for t, syms in h.calls if sym in syms]
            self.assertTrue(all(b - a >= 60 for a, b in zip(own, own[1:])), (sym, own))

    def test_interval_below_symbol_gap_does_not_break_limit(self):
        h = Harness(self, interval=30)
        h.s.start()
        self.assertTrue(until(lambda: len(h.calls) >= 5))
        h.s.stop()
        times = [t for t, _ in h.calls]
        self.assertTrue(all(b - a >= 60 for a, b in zip(times, times[1:])), times)

    def test_multi_code_batches_at_most_five(self):
        codes = tuple('60000%d' % i for i in range(7))
        h = Harness(self, codes=codes, multi_code=True)
        h.s.start()
        self.assertTrue(until(lambda: len(h.calls) >= 2))
        h.s.stop()
        self.assertEqual([len(syms) for _, syms in h.calls[:2]], [5, 2])

    def test_budget_exhausted_pauses_and_thread_exits(self):
        h = Harness(self, request_budget=3)
        h.s.start()
        self.assertTrue(until(lambda: h.s.status()['state'] == 'paused'))
        self.assertEqual(h.s.status()['reason'], 'budget_exhausted')
        self.assertEqual(len(h.calls), 3)
        self.assertTrue(until(lambda: not h.s._thread.is_alive()))
        self.assertTrue(h.s.start())                                      # 明确再 start 才有新预算
        self.assertTrue(until(lambda: len(h.calls) >= 4))

    def test_clean_stop_and_idempotence(self):
        h = Harness(self)
        self.assertTrue(h.s.start())
        self.assertFalse(h.s.start())
        thread = h.s._thread
        h.s.stop()
        self.assertFalse(thread.is_alive())
        h.s.stop()
        self.assertEqual(h.s.status()['state'], 'stopped')
        self.assertEqual(h.s.quotes()[0]['price'] if h.s.quotes() else None, None)  # 停止后报价清空
        self.assertTrue(h.s.start())
        h.s.stop()

    def test_upstream_errors_do_not_crash(self):
        h = Harness(self)
        h.errors = [FeedError('timeout'), RuntimeError('boom 不应出现在任何输出里'), None]
        h.s.start()
        self.assertTrue(until(lambda: len(h.calls) >= 5))
        st = h.s.status()
        self.assertEqual(st['state'], 'running')                          # 失败不连续三次就不暂停
        self.assertNotIn('boom', repr(st) + repr(h.s.quotes()))
        h.s.stop()

    def test_three_consecutive_failures_pause(self):
        h = Harness(self)
        h.errors = [FeedError('network_error')] * 3
        h.s.start()
        self.assertTrue(until(lambda: h.s.status()['state'] == 'paused'))
        self.assertEqual(h.s.status()['reason'], 'network_error')

    def test_unknown_error_category_is_whitelisted(self):
        h = Harness(self)
        h.errors = [FeedError('<script>x</script>')] * 3
        h.s.start()
        self.assertTrue(until(lambda: h.s.status()['state'] == 'paused'))
        self.assertEqual(h.s.status()['reason'], 'provider_error')

    def test_watchlist_builder_failure_is_survived(self):
        h = Harness(self)
        h.s.build_fn = lambda n: (_ for _ in ()).throw(RuntimeError('x'))
        h.s.start()
        self.assertTrue(until(lambda: h.s.status()['rounds'] >= 2))
        self.assertEqual(h.calls, [])
        self.assertEqual(h.s.status()['state'], 'running')

    def test_price_alert_crossing_cooldown_notify_and_persist(self):
        h = Harness(self, targets={'600000': {'buy': 10.0, 'sell': 12.0}},
                    prices={'600000': [10.5, 10.4, 9.9, 9.8, 9.7, 12.5]})
        h.s.start()
        self.assertTrue(until(lambda: len(h.calls) >= 7))
        h.s.stop()
        alerts = h.s.alerts()
        self.assertEqual([(a['direction'], a['threshold']) for a in alerts], [('down', 10.0), ('up', 12.0)])
        self.assertEqual(alerts[0]['previous_price'], 10.4)
        self.assertEqual(len(h.notified), 2)                              # 持续低于阈值不重复提醒
        saved = Store(root=h.root).load()
        self.assertEqual(len(saved['alerts']), 2)
        self.assertEqual(saved['watch'], [])                              # 名单与报价不落盘
        self.assertEqual(saved['samples'], {})
        self.assertIn('600000:down', saved['limits'])

    def test_no_targets_means_no_alerts(self):
        h = Harness(self, prices={'600000': [10.5, 9.0, 12.0]})
        h.s.start()
        self.assertTrue(until(lambda: len(h.calls) >= 3))
        h.s.stop()
        self.assertEqual(h.s.alerts(), [])
        self.assertEqual(h.notified, [])

    def test_notify_failure_is_survived(self):
        h = Harness(self, targets={'600000': {'buy': 10.0}}, prices={'600000': [10.5, 9.9]})
        h.s.notify_fn = lambda t, b: (_ for _ in ()).throw(OSError('no notifier'))
        h.s.start()
        self.assertTrue(until(lambda: len(h.s.alerts()) == 1))
        self.assertEqual(h.s.status()['state'], 'running')

    def test_set_runtime(self):
        root = Path(tempfile.gettempdir()) / 'mm_unused_never_created'
        s = MonitorScheduler(store=Store(root=root), batch_mode=False, easy_available=lambda: True)
        self.assertEqual((s.provider, s.max_watch, s.batch_mode), ('tencent', 500, False))
        # 非法输入：全部拒绝且状态不变
        bad_cases = ({'max_watch': 0}, {'max_watch': 5501}, {'max_watch': True}, {'max_watch': 100.0}, {'max_watch': '100'},
                     {'max_watch': 19}, {'batch_mode': 1}, {'batch_mode': 'true'}, {'provider': 'sina'}, {'provider': 1},
                     {'provider': True}, {'provider': 'easy', 'batch_mode': False}, {'provider': 'tencent', 'batch_mode': True},
                     {'max_watch': 100, 'batch_mode': 'x'})
        s.set_config(60, 20)
        for kwargs in bad_cases:
            with self.assertRaises(ValueError, msg=kwargs):
                s.set_runtime(**kwargs)
            self.assertEqual((s.provider, s.max_watch, s.batch_mode), ('tencent', 500, False), kwargs)
        # 当前 top_n 超过新 max_watch：拒绝
        s.set_config(60, 300)
        with self.assertRaises(ValueError):
            s.set_runtime(max_watch=100)
        self.assertEqual(s.max_watch, 500)
        # 合法：None 表示不改
        s.set_runtime()
        self.assertEqual((s.provider, s.max_watch, s.batch_mode), ('tencent', 500, False))
        s.set_runtime(max_watch=5500)
        self.assertEqual(s.max_watch, 5500)
        s.set_runtime(provider='easy')
        self.assertEqual((s.provider, s.batch_mode, s.status()['batch_mode']), ('easy', True, True))
        s.set_runtime(batch_mode=False, max_watch=1000)
        self.assertEqual((s.provider, s.max_watch, s.batch_mode), ('tencent', 1000, False))
        s.set_runtime(provider='easy', batch_mode=True)
        self.assertEqual(s.provider, 'easy')
        # 切回单只后间隔下限恢复 30：批量下的小间隔会让切换被拒绝
        s.set_config(10, 20)
        with self.assertRaises(ValueError):
            s.set_runtime(provider='tencent')
        self.assertEqual(s.provider, 'easy')
        # set_config 与 set_runtime 互不干扰
        s.set_config(30, 5)
        s.set_runtime(provider='tencent')
        self.assertEqual((s.status()['interval'], s.status()['top_n'], s.provider), (30, 5, 'tencent'))

    def test_set_runtime_easy_unavailable(self):
        root = Path(tempfile.gettempdir()) / 'mm_unused_never_created'
        s = MonitorScheduler(store=Store(root=root), batch_mode=False, easy_available=lambda: False)
        with self.assertRaises(ValueError):
            s.set_runtime(provider='easy')
        self.assertEqual((s.provider, s.batch_mode), ('tencent', False))

    def test_storage_error_blocks_start(self):
        h = Harness(self)
        h.root.mkdir(parents=True)
        h.root.chmod(0o755)                                               # 权限不对 -> storage_permissions
        self.assertFalse(h.s.start())
        self.assertEqual((h.s.status()['state'], h.s.status()['reason']), ('paused', 'storage_error'))
        self.assertEqual(h.calls, [])

    def test_config_validation(self):
        s = MonitorScheduler(store=Store(root=Path(tempfile.gettempdir()) / 'mm_unused_never_created'),
                             batch_mode=False)
        # D9：top_n 上界随 max_watch，默认 500，所以 61 合法、501 非法
        for bad in ((29, 20), (3601, 20), (60, -1), (60, 501), (60.0, 20), (60, 20.0), (True, 20), (60, True), ('60', 20)):
            with self.assertRaises(ValueError, msg=bad):
                s.set_config(*bad)
        s.set_config(30, 0)
        self.assertEqual((s.status()['interval'], s.status()['top_n']), (30, 0))
        s.set_config(30, 500)
        self.assertEqual(s.status()['top_n'], 500)
        for kwargs in ({'request_budget': 1201}, {'request_budget': 0}, {'multi_code': 1}, {'interval': 10, 'batch_mode': False},
                       {'interval': 4}):
            with self.assertRaises(ValueError, msg=kwargs):
                MonitorScheduler(**kwargs)

    def test_status_has_no_quote_details(self):
        h = Harness(self)
        h.s.start()
        self.assertTrue(until(lambda: len(h.calls) >= 1))
        h.s.stop()
        text = repr(h.s.status())
        for secret in ('600000', '测试', '10.0'):
            self.assertNotIn(secret, text)


class WatchlistTests(unittest.TestCase):
    def test_merge_dedupe_sources_and_priority(self):
        got = watchlist.build_watchlist(
            2, holdings_fn=lambda: ['600000', 'bad', '12345', '300001'],
            watch_fn=lambda: ['600000', '000001'],
            signals_fn=lambda: [{'code': '000001'}, {'code': '601398', 'buy_price': 5.5, 'sell_price': 'x'},
                                {'code': '600036'}])
        self.assertEqual(got['codes'], ['600000', '300001', '000001', '601398'])
        self.assertEqual(got['sources']['600000'], ['holding', 'watchlist'])
        self.assertEqual(got['sources']['000001'], ['watchlist', 'signal'])
        self.assertEqual(got['targets'], {'601398': {'buy': 5.5}})
        self.assertFalse(got['truncated'])

    def test_hard_cap_truncates_signals_first(self):
        """D9：原来写死 60；现在上限是 max_watch（默认 500）。显式 max_watch=60 时行为与旧测试一致。"""
        holds = ['600%03d' % i for i in range(50)]
        sigs = [{'code': '000%03d' % i} for i in range(40)]
        got = watchlist.build_watchlist(40, max_watch=60, holdings_fn=lambda: holds, watch_fn=lambda: [],
                                        signals_fn=lambda: sigs)
        self.assertEqual(len(got['codes']), 60)
        self.assertTrue(got['truncated'])
        self.assertEqual(got['codes'][:50], holds)
        with self.assertRaises(ValueError):
            watchlist.build_watchlist(61, max_watch=60)
        # 默认上限 500：同样的输入不再截断
        got = watchlist.build_watchlist(40, holdings_fn=lambda: holds, watch_fn=lambda: [], signals_fn=lambda: sigs)
        self.assertEqual(len(got['codes']), 90)
        self.assertFalse(got['truncated'])
        with self.assertRaises(ValueError):
            watchlist.build_watchlist(501)

    def test_source_failures_degrade_to_empty(self):
        def boom():
            raise RuntimeError('x')
        got = watchlist.build_watchlist(20, holdings_fn=boom, watch_fn=lambda: ['600000'], signals_fn=boom)
        self.assertEqual(got['codes'], ['600000'])
        self.assertEqual(watchlist.build_watchlist(20, holdings_fn=boom, watch_fn=boom, signals_fn=boom)['codes'], [])

    def test_row_targets_rejects_bad_values(self):
        self.assertEqual(watchlist.row_targets({'buy_price': True, 'sell_price': float('nan')}), {})
        self.assertEqual(watchlist.row_targets({'target_buy': 3, 'target_sell': -1}), {'buy': 3.0})


if __name__ == '__main__':
    unittest.main()
