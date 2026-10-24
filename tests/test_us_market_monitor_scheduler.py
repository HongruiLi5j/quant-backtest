"""T015 S3 调度器/名单/验证脚本测试：假请求、假时钟、临时目录；不联网、不读写真实 data/、不开子进程。"""
from datetime import datetime, timedelta
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.market_monitor.providers import FeedError
from src.us_market_monitor import scheduler as sched_mod
from src.us_market_monitor import watchlist
from src.us_market_monitor.core import US_EASTERN, Quote
from src.us_market_monitor.scheduler import USMonitorScheduler
from src.us_market_monitor.store import Store

THURSDAY_OPEN = datetime(2026, 10, 22, 9, 30, tzinfo=US_EASTERN)
SATURDAY = datetime(2026, 10, 24, 10, 0, tzinfo=US_EASTERN)
PRE_MARKET = datetime(2026, 10, 22, 8, 0, tzinfo=US_EASTERN)
AFTER_CLOSE = datetime(2026, 10, 22, 16, 30, tzinfo=US_EASTERN)

spec = importlib.util.spec_from_file_location('validate_us_feed', ROOT / 'scripts' / 'validate_us_feed.py')
feed = importlib.util.module_from_spec(spec)
spec.loader.exec_module(feed)


class FakeTime:
    """同一个假时钟同时驱动 now() 和 monotonic()；wait 只推进假时间，真实只睡 0.5ms 让出 CPU。"""
    def __init__(self, start):
        self.start, self.t = start, 0.0
        self.waits = []

    def now(self):
        return self.start + timedelta(seconds=self.t)

    def monotonic(self):
        return self.t

    def wait(self, event, seconds):
        self.waits.append(seconds)
        if event.is_set():
            return
        self.t += max(0.0, seconds)
        time.sleep(0.0005)


def make_quote(symbol, price, at, volume, opening=None, previous_close=None, age=0):
    stamp = at - timedelta(seconds=age)
    return Quote(symbol, price, previous_close or price, opening or price, price, price, volume, stamp, at, 0.1)


def until(predicate, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.002)
    return predicate()


class Harness:
    def __init__(self, test, start=THURSDAY_OPEN, codes=('AAPL',), targets=None, prices=None, openings=None,
                 age=0, avg=None, **kwargs):
        self.clock = FakeTime(start)
        self.calls = []                  # (假单调时间, 代码元组)
        self.errors = []                 # 预置的错误序列，先进先出
        self.prices, self.openings, self.age = prices or {}, openings or {}, age
        self.notified = []
        tmp = tempfile.TemporaryDirectory()
        test.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()     # macOS 的 /var 是符号链接，Store 逐层拒绝符号链接
        self.root = self.tmp / 'store'
        self.plan = {'codes': list(codes), 'sources': {c: ['watchlist'] for c in codes},
                     'targets': targets or {}, 'signals': {}, 'truncated': False}
        self.volume = 0
        self.waits_at_first = None

        def request(symbols, cancel, timeout):
            if not self.calls:
                self.waits_at_first = list(self.clock.waits)
            self.calls.append((self.clock.t, symbols))
            if self.errors:
                err = self.errors.pop(0)
                if err is not None:
                    raise err
            out = []
            for s in symbols:
                self.volume += 100
                seq = self.prices.get(s, [100.0])
                n = sum(1 for _, sym in self.calls if s in sym)
                out.append(make_quote(s, seq[min(n - 1, len(seq) - 1)], self.clock.now(), self.volume,
                                      opening=self.openings.get(s), age=self.age))
            return out

        kwargs.setdefault('avg_volume_fn', lambda codes: dict(avg or {}))
        self.s = USMonitorScheduler(store=Store(root=self.root), request=request, build_fn=lambda n: self.plan,
                                    notify_fn=lambda t, b: self.notified.append((t, b)),
                                    now=self.clock.now, monotonic=self.clock.monotonic, wait=self.clock.wait,
                                    **kwargs)
        test.addCleanup(self.s.stop)


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        for target in ('socket.socket', 'socket.create_connection', 'socket.getaddrinfo',
                       'urllib.request.OpenerDirector.open'):
            guard = patch(target, side_effect=AssertionError('测试禁止联网'))
            guard.start()
            self.addCleanup(guard.stop)

    def test_default_construction_starts_nothing(self):
        before = threading.active_count()
        s = USMonitorScheduler()
        self.assertEqual(s.status()['state'], 'stopped')
        self.assertEqual(threading.active_count(), before)
        self.assertEqual(s.store.root, sched_mod.WATCH_STORE_ROOT)        # 只是路径常量，构造不创建目录
        self.assertEqual(s.store.root.name, 'us_market_monitor_watch')
        self.assertEqual(s.intraday_dir.name, 'us_market_monitor_intraday')
        self.assertEqual((s.interval, s.top_n, s.max_watch, s.budget), (300, 0, 30, 2500))

    def test_config_validation(self):
        for bad in (dict(interval=59), dict(interval=3601), dict(interval=True), dict(interval=60.0),
                    dict(top_n=31), dict(top_n=-1), dict(top_n=False), dict(request_budget=2501),
                    dict(request_budget=0), dict(max_watch=31), dict(max_watch=0), dict(timeout=11),
                    dict(timeout=True), dict(thresholds={'momentum': -1}), dict(thresholds={'nope': 1})):
            with self.assertRaises(ValueError, msg=bad):
                USMonitorScheduler(**bad)
        s = USMonitorScheduler()
        for args in ((59, 0), (60, 31), (True, 0), (60, True)):
            with self.assertRaises(ValueError):
                s.set_config(*args)
        s.set_config(60, 30)
        self.assertEqual((s.interval, s.top_n), (60, 30))

    def test_polls_inside_session_one_symbol_per_request(self):
        h = Harness(self, codes=('AAPL', 'MSFT'))
        self.assertTrue(h.s.start())
        self.assertTrue(until(lambda: len(h.calls) >= 4))
        h.s.stop()
        self.assertEqual({sym for _, syms in h.calls for sym in syms}, {'AAPL', 'MSFT'})
        self.assertTrue(all(len(syms) == 1 for _, syms in h.calls))
        self.assertEqual(h.s.status()['state'], 'stopped')

    def test_quotes_visible_and_are_copies(self):
        h = Harness(self, prices={'AAPL': [101.0]}, openings={'AAPL': 100.0})
        h.s.start()
        self.assertTrue(until(lambda: h.s.quotes() and h.s.quotes()[0]['price'] == 101.0))
        q = h.s.quotes()[0]
        self.assertEqual((q['code'], q['quality']), ('AAPL', 'valid'))
        self.assertIn('-04:00', q['received_at'])                          # 美东夏令时
        q['price'] = 999
        self.assertNotEqual(h.s.quotes()[0]['price'], 999)

    def test_no_polling_outside_session(self):
        for start in (SATURDAY, AFTER_CLOSE):
            h = Harness(self, start=start)
            h.s.start()
            self.assertTrue(until(lambda: len(h.clock.waits) >= 20))
            h.s.stop()
            self.assertEqual(h.calls, [], start)

    def test_idle_wait_never_exceeds_a_minute_so_open_is_not_missed(self):
        h = Harness(self, start=PRE_MARKET, interval=3600)
        h.s.start()
        self.assertTrue(until(lambda: len(h.calls) >= 1))                  # 08:00 起每分钟看一次，09:30 自动开始
        self.assertGreaterEqual(h.calls[0][0], 90 * 60)
        self.assertLess(h.calls[0][0], 91 * 60 + 1)
        h.s.stop()
        self.assertLessEqual(max(h.waits_at_first), 60)                    # 开盘前每次最多等 60 秒

    def test_rate_limits_per_symbol_and_between_requests(self):
        h = Harness(self, codes=('AAPL', 'MSFT'))
        h.s.start()
        self.assertTrue(until(lambda: len(h.calls) >= 8))
        h.s.stop()
        times = [t for t, _ in h.calls]
        self.assertTrue(all(b - a >= 10 for a, b in zip(times, times[1:])), times)
        for sym in ('AAPL', 'MSFT'):
            own = [t for t, syms in h.calls if sym in syms]
            self.assertTrue(all(b - a >= 300 for a, b in zip(own, own[1:])), (sym, own))

    def test_small_interval_does_not_break_symbol_gap(self):
        h = Harness(self, interval=60)
        h.s.start()
        self.assertTrue(until(lambda: len(h.calls) >= 5))
        h.s.stop()
        times = [t for t, _ in h.calls]
        self.assertTrue(all(b - a >= 300 for a, b in zip(times, times[1:])), times)

    def test_restart_keeps_symbol_gap(self):
        h = Harness(self)
        h.s.start()
        self.assertTrue(until(lambda: len(h.calls) >= 1))
        h.s.stop()
        first = len(h.calls)
        h.s.start()
        self.assertTrue(until(lambda: len(h.calls) > first))
        h.s.stop()
        times = [t for t, _ in h.calls]
        self.assertGreaterEqual(times[first] - times[first - 1], 300)      # 停止再开始也绕不过每只 300 秒

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
        h.s.stop()                                                         # 未启动时停止也安全
        self.assertTrue(h.s.start())
        self.assertFalse(h.s.start())
        thread = h.s._thread
        h.s.stop()
        self.assertFalse(thread.is_alive())
        h.s.stop()
        self.assertEqual(h.s.status()['state'], 'stopped')
        self.assertTrue(all(q['quality'] == 'pending' for q in h.s.quotes()))   # 停止后报价清空
        self.assertTrue(h.s.start())
        h.s.stop()

    def test_upstream_errors_do_not_crash_or_leak_text(self):
        h = Harness(self)
        h.errors = [FeedError('timeout'), RuntimeError('boom 不应出现在任何输出里'), None]
        h.s.start()
        self.assertTrue(until(lambda: len(h.calls) >= 5))
        st = h.s.status()
        self.assertEqual(st['state'], 'running')                          # 失败不连续三次就不暂停
        self.assertNotIn('boom', repr(st) + repr(h.s.quotes()) + repr(h.s.store.load()))
        h.s.stop()

    def test_three_consecutive_failures_pause_with_reason(self):
        h = Harness(self)
        h.errors = [FeedError('network')] * 3
        h.s.start()
        self.assertTrue(until(lambda: h.s.status()['state'] == 'paused'))
        self.assertEqual(h.s.status()['reason'], 'network')
        self.assertEqual(len(h.calls), 3)
        self.assertTrue(until(lambda: not h.s._thread.is_alive()))

    def test_unknown_error_category_is_whitelisted(self):
        h = Harness(self)
        h.errors = [FeedError('<script>x</script>')] * 3
        h.s.start()
        self.assertTrue(until(lambda: h.s.status()['state'] == 'paused'))
        self.assertEqual(h.s.status()['reason'], 'provider_error')
        self.assertNotIn('script', repr(h.s.status()) + repr(h.s.store.load()))

    def test_failure_backs_off_and_success_recovers(self):
        h = Harness(self)
        h.errors = [FeedError('timeout'), None]
        h.s.start()
        self.assertTrue(until(lambda: len(h.calls) >= 3))
        h.s.stop()
        times = [t for t, _ in h.calls]
        self.assertGreaterEqual(times[1] - times[0], 300)                  # 每只最小间隔照旧
        self.assertEqual(h.s.status()['state'], 'stopped')

    def test_rate_limited_cools_down_then_resumes(self):
        h = Harness(self)
        h.errors = [FeedError('rate_limited'), None]
        h.s.start()
        self.assertTrue(until(lambda: len(h.calls) >= 2))
        h.s.stop()
        self.assertGreaterEqual(h.calls[1][0] - h.calls[0][0], sched_mod.RATE_LIMIT_COOLDOWN)

    def test_rate_limited_three_times_pauses(self):
        h = Harness(self)
        h.errors = [FeedError('rate_limited')] * 3
        h.s.start()
        self.assertTrue(until(lambda: h.s.status()['state'] == 'paused'))
        self.assertEqual(h.s.status()['reason'], 'rate_limited')

    def test_missing_yfinance_pauses_immediately(self):
        h = Harness(self)
        h.errors = [ImportError('yfinance_not_installed')]
        h.s.start()
        self.assertTrue(until(lambda: h.s.status()['state'] == 'paused'))
        self.assertEqual(h.s.status()['reason'], 'yfinance_not_installed')
        self.assertEqual(len(h.calls), 1)

    def test_stale_quotes_like_a_holiday_pause_as_quality_error(self):
        h = Harness(self, age=86400)                                       # 行情时间停在昨天
        h.s.start()
        self.assertTrue(until(lambda: h.s.status()['state'] == 'paused'))
        self.assertEqual(h.s.status()['reason'], 'quality_error')
        self.assertEqual(h.s.intraday_events(), [])

    def test_wrong_symbol_or_empty_result_is_invalid_record(self):
        h = Harness(self)
        wrong = lambda symbols, cancel, timeout: [make_quote('MSFT', 100.0, h.clock.now(), 1)]
        h.s.request = wrong
        h.s.start()
        self.assertTrue(until(lambda: h.s.status()['state'] == 'paused'))
        self.assertEqual(h.s.status()['reason'], 'invalid_record')

    def test_price_target_alert_notifies_and_persists(self):
        h = Harness(self, targets={'AAPL': {'sell': 105.0}}, prices={'AAPL': [100.0, 106.0, 107.0]})
        h.s.start()
        self.assertTrue(until(lambda: h.s.alerts()))
        alert = h.s.alerts()[0]
        self.assertEqual((alert['code'], alert['direction'], alert['threshold']), ('AAPL', 'up', 105.0))
        self.assertIn('-04:00', alert['triggered_at'])
        self.assertTrue(h.notified)
        self.assertTrue(until(lambda: h.s.store.load()['alerts']))
        self.assertTrue(until(lambda: h.s.quotes()[0]['alert'] == 'triggered'))
        h.s.stop()
        self.assertIn('AAPL:up', h.s.store.load()['limits'])               # 冷却/次数落盘，跨重启保留

    def test_storage_permissions_after_run(self):
        h = Harness(self)
        h.s.start()
        self.assertTrue(until(lambda: h.root.exists() and any(h.root.iterdir())))
        h.s.stop()
        self.assertEqual(stat.S_IMODE(h.root.stat().st_mode), 0o700)
        for item in h.root.iterdir():
            self.assertEqual(stat.S_IMODE(item.stat().st_mode), 0o600)

    def test_symlinked_store_is_refused_and_start_pauses(self):
        h = Harness(self)
        real = h.tmp / 'real'
        real.mkdir(mode=0o700)
        os.symlink(real, h.root)
        self.assertFalse(h.s.start())
        st = h.s.status()
        self.assertEqual((st['state'], st['reason']), ('paused', 'storage_error'))
        self.assertEqual(h.calls, [])
        self.assertEqual(list(real.iterdir()), [])

    def test_intraday_events_and_tracking_file(self):
        h = Harness(self, prices={'AAPL': [100.0, 104.0, 104.0]}, openings={'AAPL': 100.0}, avg={'AAPL': 1e9},
                    intraday_dir=None)
        h.plan['signals'] = {'AAPL': {'action': '买入', 'close': 99.0}}
        h.s.start()
        self.assertTrue(until(lambda: any(e['kind'] == 'watch' for e in h.s.intraday_events())))
        events = [e for e in h.s.intraday_events() if e['kind'] == 'watch']
        self.assertIn('breakout', events[0]['params']['triggers'])
        self.assertFalse(events[0].get('display_only'))
        self.assertIn('-04:00', events[0]['timestamp'])
        self.assertTrue(until(lambda: len(h.s.intraday_events()) >= 1 and len(h.clock.waits) > 8))
        keys = [(e['kind'], e['code'], tuple(e['params']['triggers'])) for e in h.s.intraday_events() if e['kind'] == 'watch']
        self.assertEqual(len(keys), len(set(keys)))                          # 同一(种类,代码,触发项)只提示一次
        self.assertTrue(until(lambda: h.s.intraday_tracking()['rows']))
        tracking = h.s.intraday_tracking()
        self.assertEqual(tracking['day'], '2026-10-22')
        self.assertTrue(any(r['type'] == 'trigger' for r in tracking['rows']))
        track = next(r for r in tracking['rows'] if r['type'] == 'track')
        self.assertIn('to_day_high_pct', track)
        self.assertNotIn('to_limit_up_pct', track)
        h.s.stop()
        folder = h.tmp / 'us_market_monitor_intraday'
        self.assertTrue(until(lambda: (folder / '2026-10-22.jsonl').exists()))
        self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((folder / '2026-10-22.jsonl').stat().st_mode), 0o600)

    def test_low_price_signal_is_display_only_and_not_tracked(self):
        h = Harness(self, codes=('PENN',), prices={'PENN': [2.0, 2.3, 2.3]}, openings={'PENN': 2.0}, avg={'PENN': 1e9})
        h.s.start()
        self.assertTrue(until(lambda: any(e['kind'] == 'watch' for e in h.s.intraday_events())))
        event = next(e for e in h.s.intraday_events() if e['kind'] == 'watch')
        self.assertTrue(event['display_only'])
        self.assertTrue(until(lambda: h.s.status()['rounds'] >= 3))
        h.s.stop()
        self.assertEqual([r for r in h.s.intraday_tracking()['rows'] if r['type'] == 'trigger'], [])

    def test_intraday_failure_never_stops_polling(self):
        h = Harness(self)
        h.s.avg_volume_fn = lambda codes: 1 / 0
        h.s.start()
        self.assertTrue(until(lambda: h.s.status()['rounds'] >= 3))
        self.assertEqual(h.s.status()['state'], 'running')

    def test_watchlist_builder_failure_degrades_to_empty(self):
        h = Harness(self)
        h.s.build_fn = lambda n: 1 / 0
        h.s.start()
        self.assertTrue(until(lambda: h.s.status()['rounds'] >= 2))
        self.assertEqual((h.calls, h.s.status()['state'], h.s.status()['watch_count']), ([], 'running', 0))

    def test_empty_watchlist_sends_no_requests_and_is_not_a_fault(self):
        h = Harness(self, codes=())
        h.s.start()
        self.assertTrue(until(lambda: h.s.status()['rounds'] >= 3))
        self.assertEqual(h.calls, [])
        self.assertEqual(h.s.quotes(), [])
        self.assertEqual(h.s.status()['state'], 'running')

    def test_new_session_discards_in_flight_result(self):
        h = Harness(self)
        release = threading.Event()
        inner = h.s.request

        def slow(symbols, cancel, timeout):
            out = inner(symbols, cancel, timeout)
            release.wait(2)
            return out
        h.s.request = slow
        h.s.start()
        self.assertTrue(until(lambda: h.calls))
        h.s.stop()
        release.set()
        self.assertEqual(h.s.quotes()[0]['quality'], 'pending')
        self.assertEqual(h.s.store.load()['quality']['requests'], 0)


class WatchlistTests(unittest.TestCase):
    def build(self, top_n=0, holdings=(), watch=(), signals=(), **kw):
        return watchlist.build_us_watchlist(top_n, holdings_fn=lambda: holdings, watch_fn=lambda: watch,
                                            signals_fn=lambda: signals, **kw)

    def test_default_sources_are_safe_and_holdings_signals_empty(self):
        self.assertEqual(watchlist.default_holdings(), [])
        self.assertEqual(watchlist.default_signals(), [])
        with patch('src.datasource.load_watchlist', return_value={'AAPL': 'Apple', '^GSPC': '指数', 'msft': 'x'}):
            self.assertEqual(watchlist.build_us_watchlist(0)['codes'], ['AAPL'])

    def test_priority_dedup_and_sources_kept(self):
        plan = self.build(5, holdings=['AAPL'], watch=['MSFT', 'AAPL'],
                          signals=[{'code': 'MSFT', 'action': '买入', 'name': 'x', 'close': 1.5, 'buy_price': 9.0},
                                   {'code': 'NVDA', 'action': '卖出'}])
        self.assertEqual(plan['codes'], ['AAPL', 'MSFT', 'NVDA'])
        self.assertEqual(plan['sources'], {'AAPL': ['holding', 'watchlist'], 'MSFT': ['watchlist', 'signal'], 'NVDA': ['signal']})
        self.assertEqual(plan['targets'], {'MSFT': {'buy': 9.0}})
        self.assertEqual(plan['signals']['MSFT'], {'action': '买入', 'name': 'x', 'close': 1.5})

    def test_top_n_limits_signals_only(self):
        plan = self.build(1, watch=['AAPL'], signals=[{'code': 'MSFT', 'action': '买入'}, {'code': 'NVDA', 'action': '买入'}])
        self.assertEqual(plan['codes'], ['AAPL', 'MSFT'])

    def test_truncation_drops_lowest_priority_and_flags(self):
        names = [chr(65 + i // 26) + chr(65 + i % 26) for i in range(31)]
        plan = self.build(0, watch=names)
        self.assertEqual(len(plan['codes']), 30)
        self.assertTrue(plan['truncated'])
        self.assertEqual(plan['codes'], names[:30])
        held = self.build(1, holdings=['ZZ'], watch=names[:30], signals=[{'code': 'QQ', 'action': '买入'}])
        self.assertEqual(held['codes'][0], 'ZZ')
        self.assertNotIn('QQ', held['codes'])
        self.assertNotIn('QQ', held['signals'])

    def test_invalid_codes_are_dropped(self):
        bad = ['^GSPC', 'ES=F', 'aapl', 'AAPL ', 'BF.B', '', None, 123, 'TOOLONG', 'ＡＡＰＬ', "A'; DROP", 'BRK-B']
        self.assertEqual(self.build(watch=bad)['codes'], ['BRK-B'])

    def test_source_failure_degrades_only_that_source(self):
        def boom():
            raise RuntimeError('x')
        plan = watchlist.build_us_watchlist(1, holdings_fn=boom, watch_fn=lambda: ['AAPL'], signals_fn=boom)
        self.assertEqual(plan['codes'], ['AAPL'])

    def test_bad_arguments(self):
        for top_n in (True, -1, 31, 1.0, '1', None):
            with self.assertRaises(ValueError, msg=top_n):
                self.build(top_n)
        for max_watch in (0, 31, True, 5.0):
            with self.assertRaises(ValueError, msg=max_watch):
                self.build(0, max_watch=max_watch)
        with self.assertRaises(ValueError):
            self.build(6, max_watch=5)

    def test_bad_target_prices_ignored(self):
        plan = self.build(3, signals=[{'code': 'AAPL', 'action': '买入', 'buy_price': float('nan'), 'sell_price': True},
                                      {'code': 'MSFT', 'action': '买入', 'buy_price': -1, 'sell_price': 10}])
        self.assertEqual(plan['targets'], {'MSFT': {'sell': 10.0}})


class ValidateScriptTests(unittest.TestCase):
    """validate_us_feed.py 的离线测试：假请求函数 + 假时钟 + 临时目录，不联网、不开子进程。"""
    def run_probe(self, request, **overrides):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        out = Path(tmp.name).resolve() / 'data' / 'us_market_monitor' / 'validation'
        args = feed.arguments(['--run', '--symbols', 'AAPL,MSFT', '--count', '2', '--interval', '60'])
        clock = {'t': 0.0}
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            clock['t'] += seconds
        starts = []

        def wrapped(symbols, cancel, timeout):
            starts.append((clock['t'], symbols, timeout))
            return request(symbols, cancel, timeout)
        kw = dict(request=wrapped, sleep=sleep, monotonic=lambda: clock['t'], now=lambda: THURSDAY_OPEN)
        kw.update(overrides)
        with patch('builtins.print'):
            code = feed.probe(args, out, **kw)
        files = sorted(out.glob('*.jsonl'))
        rows = [json.loads(line) for line in files[0].read_text(encoding='utf-8').splitlines()] if files else []
        return code, rows, starts, out, files

    def ok_request(self, symbols, cancel, timeout):
        self.n = getattr(self, 'n', 0) + 1                                 # 每次推进时间戳和成交量，避免被判重复
        return [make_quote(symbols[0], 100.0, SATURDAY + timedelta(seconds=70 * self.n), 1000 + self.n)]   # 周六：off_session

    def test_argument_limits(self):
        good = ['--run', '--symbols', 'AAPL,MSFT', '--count', '3', '--interval', '60', '--timeout', '10']
        self.assertEqual(feed.arguments(good).symbols, ('AAPL', 'MSFT'))
        bad_sets = [['--symbols', 'aapl'], ['--symbols', '^GSPC'], ['--symbols', 'AAPL,AAPL'], ['--symbols', ''],
                    ['--symbols', 'A,B,C,D,E,F'], ['--count', '0'], ['--count', '6'], ['--interval', '59'],
                    ['--interval', 'nan'], ['--interval', 'inf'], ['--timeout', '11'], ['--timeout', '0'],
                    ['--stale-seconds', '0'], ['--symbols', 'AAPL,MSFT,NVDA,AMD,INTC', '--count', '3']]
        for extra in bad_sets:
            with self.assertRaises(SystemExit, msg=extra), patch('sys.stderr'):
                feed.arguments(['--run'] + extra)

    def test_default_is_off(self):
        with patch('builtins.print') as shown:
            self.assertEqual(feed.main([]), 0)
        self.assertIn('默认关闭', shown.call_args[0][0])

    def test_probe_enforces_spacing_and_writes_private_record(self):
        code, rows, starts, out, files = self.run_probe(self.ok_request)
        self.assertEqual(code, 0)
        self.assertEqual([r['symbol'] for r in rows], ['AAPL', 'MSFT', 'AAPL', 'MSFT'])
        times = [t for t, _, _ in starts]
        self.assertTrue(all(b - a >= 10 for a, b in zip(times, times[1:])), times)
        self.assertGreaterEqual(times[2] - times[0], 60)
        self.assertGreaterEqual(times[3] - times[1], 60)
        self.assertTrue(all(r['quality'] == 'off_session' and r['error_category'] is None for r in rows))
        self.assertTrue(all(r['volume_unit'] == '股（不换算）' for r in rows))
        self.assertEqual(stat.S_IMODE(files[0].stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o700)
        self.assertEqual({t for _, _, t in starts}, {10.0})

    def test_first_failure_stops_without_retry(self):
        def fail(symbols, cancel, timeout):
            raise FeedError('rate_limited')
        code, rows, starts, _, _ = self.run_probe(fail)
        self.assertEqual(code, 1)
        self.assertEqual(len(starts), 1)
        self.assertEqual(rows[0]['error_category'], 'rate_limited')

    def test_error_text_is_not_recorded(self):
        for exc, expected in ((FeedError('PRIVATE secret text'), 'provider_error'), (ImportError('x'), 'yfinance_not_installed'),
                              (RuntimeError('PRIVATE boom'), 'validation_internal_error')):
            def fail(symbols, cancel, timeout, exc=exc):
                raise exc
            code, rows, _, _, files = self.run_probe(fail)
            self.assertEqual((code, rows[0]['error_category']), (1, expected))
            self.assertNotIn('PRIVATE', files[0].read_text(encoding='utf-8'))

    def test_invalid_quote_stops(self):
        def broken(symbols, cancel, timeout):
            return [make_quote(symbols[0], -5.0, SATURDAY, 1)]
        code, rows, starts, _, _ = self.run_probe(broken)
        self.assertEqual((code, len(starts)), (1, 1))
        self.assertEqual(rows[0]['quality'], 'invalid')

    def test_probe_rechecks_namespace_limits(self):
        args = feed.arguments(['--run'])
        args.count = 99
        with self.assertRaises(SystemExit), patch('sys.stderr'):
            feed.probe(args, Path(tempfile.gettempdir()) / 'never_created_us_feed')

    def test_secure_output_refuses_symlink_and_wrong_mode(self):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw).resolve()
            real = base / 'real'
            real.mkdir(mode=0o700)
            os.symlink(real, base / 'link')
            with self.assertRaises(OSError):
                feed.secure_output(base / 'link' / 'a' / 'validation')
            loose = base / 'x' / 'validation'
            loose.mkdir(parents=True)
            os.chmod(loose, 0o755)
            os.chmod(loose.parent, 0o700)
            with self.assertRaises(ValueError):
                feed.secure_output(loose)

    def test_script_never_touches_real_data_dir_on_import(self):
        self.assertEqual(feed.OUTPUT.parts[-3:], ('data', 'us_market_monitor', 'validation')[-3:])


class ModuleHygieneTests(unittest.TestCase):
    def test_modules_have_no_network_or_app_imports(self):
        for name in ('scheduler', 'watchlist', 'wiring', 'intraday'):
            text = (ROOT / 'src' / 'us_market_monitor' / f'{name}.py').read_text(encoding='utf-8')
            for bad in ('import socket', 'urllib', 'import requests', 'http.client', 'import app', 'from app', 'import yfinance'):
                self.assertNotIn(bad, text, (name, bad))

    def test_a_share_package_untouched_by_name(self):
        text = (ROOT / 'src' / 'us_market_monitor' / 'scheduler.py').read_text(encoding='utf-8')
        self.assertNotIn("data/market_monitor'", text)
        self.assertEqual(sched_mod.WATCH_STORE_ROOT.parent.name, 'data')


if __name__ == '__main__':
    unittest.main()
