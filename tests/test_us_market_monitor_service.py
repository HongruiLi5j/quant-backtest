"""T015 S2 离线契约测试：只用临时目录、合成数据、假 provider/时钟，不联网，不读写真实 data/。"""
from dataclasses import replace
from datetime import datetime, timedelta
import itertools
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.market_monitor.providers import FeedError
from src.market_monitor.wiring import Ctx
from src.us_market_monitor import api, signals_readonly, store as store_module
from src.us_market_monitor.core import US_EASTERN, Condition, Quote
from src.us_market_monitor.service import (Config, MonitorService, process_request, process_us_request,
                                            symbol_for)
from src.us_market_monitor.store import Store, StorageError, empty_state

# 2026-10-22 周四，美东 10:00（夏令时，EDT）
AT = datetime(2026, 10, 22, 10, 0, tzinfo=US_EASTERN)
OWNER = Ctx(True, True, True, True)


def quote(symbol='AAPL', at=AT, price=9):
    return Quote(symbol, price, 9, 9, 9, 9, 100, at, at + timedelta(seconds=1), .01)


class Clock:
    def __init__(self):
        self.tick = 0

    def now(self):
        return AT + timedelta(seconds=self.tick)

    def wait(self, event, seconds):
        if not event.is_set():
            self.tick += seconds
        return event.is_set()


class ChildFake:
    """真实 spawn 子进程使用的假 provider，不碰网络。"""
    def __init__(self, **kwargs):
        # 子进程里必须关掉 provider 自带重试，预算只够这一批
        assert kwargs['max_retries'] == 0
        assert kwargs['request_budget'] >= 1
        assert 0 < kwargs['timeout'] <= 10

    def start(self):
        pass

    def stop(self):
        pass

    def fetch(self, symbols):
        return [quote(s) for s in symbols]


class ChildHang(ChildFake):
    def fetch(self, symbols):
        time.sleep(60)


class ChildExit(ChildFake):
    def fetch(self, symbols):
        os._exit(2)


class ChildRate(ChildFake):
    def fetch(self, symbols):
        raise FeedError('rate_limited')


class ChildWeird(ChildFake):
    def fetch(self, symbols):
        raise FeedError('PRIVATE upstream text')


class ChildNoYF(ChildFake):
    def fetch(self, symbols):
        raise ImportError('yfinance_not_installed')


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.clock = Clock()
        self.calls = []
        self.services = []
        for target in ('socket.socket', 'socket.create_connection', 'socket.getaddrinfo',
                       'urllib.request.OpenerDirector.open'):
            guard = patch(target, side_effect=AssertionError('测试禁止联网'))
            guard.start()
            self.addCleanup(guard.stop)
        self.addCleanup(self.cleanup_services)

    def cleanup_services(self):
        for service in self.services:
            service.stop()

    def service(self, **kw):
        def request(symbols, event, timeout):
            self.calls.append((self.clock.tick, symbols))
            return [quote(s, self.clock.now()) for s in symbols]
        options = dict(store=Store(root=self.root / 'monitor'), monotonic=lambda: self.clock.tick,
                       now=self.clock.now, wait=self.clock.wait, request=request)
        options.update(kw)
        service = MonitorService(**options)
        self.services.append(service)
        return service

    def call(self, service, action, body=b'{}', ctx=OWNER):
        return api.handle(ctx, service, action, body)

    def add(self, service, code='AAPL'):
        self.assertEqual(self.call(service, 'watch_add', {'code': code})[0], 200)

    def run_session(self, service):
        self.assertTrue(service.start())
        service._thread.join(3)
        self.assertFalse(service._thread.is_alive())
        return service.status()

    # ---------- 生命周期 ----------
    def test_default_closed_restart_and_cached_status(self):
        service = self.service()
        self.assertFalse((self.root / 'monitor').exists())
        for _ in range(3):
            self.assertEqual(self.call(service, 'status')[1]['data']['state'], 'stopped')
        self.assertIsNone(service._thread)
        self.assertEqual(self.calls, [])
        self.add(service)
        self.run_session(service)
        reloaded = self.service()
        self.assertEqual(reloaded.status()['watch'], ['AAPL'])
        self.assertEqual(reloaded.status()['state'], 'stopped')
        self.assertIsNone(reloaded._thread)
        self.assertEqual(len(self.calls), 5)

    def test_start_stop_idempotent(self):
        entered, release = threading.Event(), threading.Event()

        def slow(symbols, cancel, timeout):
            entered.set()
            release.wait(2)
            return [quote(s, self.clock.now()) for s in symbols]
        service = self.service(request=slow)
        service.stop()
        service.stop()
        self.assertEqual(service.status()['state'], 'stopped')
        self.add(service)
        self.assertTrue(service.start())
        self.assertTrue(entered.wait(1))
        thread = service._thread
        for _ in range(3):
            self.assertFalse(service.start())
        self.assertIs(service._thread, thread)
        self.assertEqual(self.call(service, 'start')[1]['data']['started'], False)
        release.set()
        service.stop()
        service.stop()
        self.assertIsNone(service._thread)
        self.assertFalse(thread.is_alive())
        self.assertEqual(service.status()['state'], 'stopped')
        self.assertTrue(service.start())
        service.stop()

    def test_budget_interval_and_explicit_config(self):
        service = self.service(config=Config(request_budget=7, poll_seconds=65))
        self.add(service)
        status = self.run_session(service)
        self.assertEqual((status['requests_used'], status['reason']), (7, 'budget_exhausted'))
        self.assertTrue(all(b[0] - a[0] >= 65 for a, b in zip(self.calls, self.calls[1:])))
        # 暂停后可重新 start，预算重置
        self.assertTrue(service.start())
        service._thread.join(3)
        self.assertEqual(len(self.calls), 14)

    def test_config_validation(self):
        for kw in ({'poll_seconds': 59}, {'poll_seconds': float('nan')}, {'request_budget': 0},
                   {'request_budget': 5001}, {'request_budget': True}, {'timeout_seconds': 11},
                   {'timeout_seconds': 0}):
            with self.assertRaises(ValueError):
                Config(**kw)
        Config(request_budget=5000)

    def test_symbol_for(self):
        for good in ('AAPL', 'BRK-B', 'F', 'GOOGL'):
            self.assertEqual(symbol_for(good), good)
        for bad in ('aapl', 'BF.B', '^GSPC', 'ES=F', 'TOOLONG', '', ' AAPL', 'AAPL\n', 'ＡＡＰＬ', 600000, None):
            with self.assertRaises(ValueError):
                symbol_for(bad)

    def test_three_failures_pause_and_sanitization(self):
        def failed(*args):
            self.calls.append(True)
            raise FeedError('PRIVATE upstream text')
        service = self.service(request=failed, config=Config(request_budget=20))
        self.add(service)
        state = self.run_session(service)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(state['reason'], 'provider_error')
        self.assertNotIn('PRIVATE', json.dumps(service.export_quality()))
        self.assertNotIn('PRIVATE', json.dumps(state, ensure_ascii=False))

        def boom(*args):
            raise RuntimeError('SECRET-KEY')
        service = self.service(request=boom, store=Store(root=self.root / 'boom'), config=Config(request_budget=20))
        self.add(service)
        state = self.run_session(service)
        self.assertEqual(state['reason'], 'provider_error')
        self.assertNotIn('SECRET', json.dumps(state, ensure_ascii=False))

    def test_known_errors_pass_through_and_missing_dependency_pauses_at_once(self):
        def limited(*args):
            self.calls.append(True)
            raise FeedError('rate_limited')
        service = self.service(request=limited, config=Config(request_budget=20))
        self.add(service)
        state = self.run_session(service)
        self.assertEqual((len(self.calls), state['reason']), (3, 'rate_limited'))
        self.calls.clear()

        def missing(*args):
            self.calls.append(True)
            raise ImportError('yfinance_not_installed')
        service = self.service(request=missing, store=Store(root=self.root / 'noyf'), config=Config(request_budget=20))
        self.add(service)
        state = self.run_session(service)
        self.assertEqual((len(self.calls), state['reason']), (1, 'yfinance_not_installed'))

    def test_failure_backs_off_then_recovers(self):
        outcomes = iter([FeedError('network'), None, None, None])

        def flaky(symbols, event, timeout):
            self.calls.append((self.clock.tick, symbols))
            outcome = next(outcomes)
            if outcome is not None:
                raise outcome
            return [quote(s, self.clock.now()) for s in symbols]
        service = self.service(request=flaky, config=Config(request_budget=4))
        self.add(service)
        self.run_session(service)
        ticks = [t for t, _ in self.calls]
        # 失败后下一轮至少隔 2 倍轮询；成功后恢复 1 倍
        self.assertGreaterEqual(ticks[1] - ticks[0], 120)
        self.assertEqual(ticks[2] - ticks[1], 60)
        self.assertEqual(service.status()['quality']['reasons']['network'], 1)

    def test_quality_failure_and_gap_no_alert(self):
        counter = [0]

        def stale(symbols, event, timeout):
            counter[0] += 1
            return [replace(quote(s, self.clock.now() - timedelta(seconds=1201), 11), received_at=self.clock.now())
                    for s in symbols]
        service = self.service(request=stale)
        self.add(service)
        service.configure('rule_set', 'AAPL', Condition('AAPL', 'up', 10, 0))
        state = self.run_session(service)
        self.assertEqual((counter[0], state['reason']), (3, 'quality_error'))
        self.assertEqual(service.alerts(), [])
        self.assertEqual(state['quality']['reasons']['stale'], 3)

    def test_after_hours_quote_never_alerts(self):
        service = self.service()
        self.add(service)
        service.configure('rule_set', 'AAPL', Condition('AAPL', 'up', 10, 0))
        late = AT.replace(hour=16, minute=30)
        for i, price in enumerate((9, 11)):
            q = quote(at=late + timedelta(seconds=60 * i), price=price)
            q = replace(q, received_at=AT + timedelta(seconds=60 * i))
            service._consume(('AAPL',), [q], None)
        self.assertEqual(service.alerts(), [])
        self.assertEqual(service.status()['quality']['reasons']['after_hours'], 2)

    def test_stop_join_discard_old_session_and_config_result(self):
        entered, release = threading.Event(), threading.Event()

        def delayed(symbols, cancel, timeout):
            entered.set()
            release.wait(2)
            return [quote(symbols[0], self.clock.now(), 11)]
        service = self.service(request=delayed)
        self.add(service)
        service.start()
        self.assertTrue(entered.wait(1))
        self.assertFalse(service.start())
        self.add(service, 'MSFT')
        stopper = threading.Thread(target=service.stop)
        stopper.start()
        deadline = time.monotonic() + 1
        while service.status()['state'] != 'stopped' and time.monotonic() < deadline:
            time.sleep(.001)
        release.set()
        stopper.join(2)
        self.assertFalse(stopper.is_alive())
        self.assertIsNone(service._thread)
        self.assertEqual(service.export_quality()['quality']['requests'], 0)
        self.assertEqual(service.trend('AAPL')['points'], [])
        service.request = lambda symbols, cancel, timeout: [quote(s, self.clock.now()) for s in symbols]
        self.run_session(service)
        self.assertGreater(service.export_quality()['quality']['requests'], 0)

    def test_config_change_discard_inflight_without_stop(self):
        entered, release = threading.Event(), threading.Event()

        def delayed(symbols, cancel, timeout):
            entered.set()
            release.wait(2)
            return [quote(s, self.clock.now()) for s in symbols]
        service = self.service(request=delayed, config=Config(request_budget=1))
        self.add(service)
        service.start()
        self.assertTrue(entered.wait(1))
        service.configure('watch_remove', 'AAPL')
        release.set()
        service._thread.join(2)
        self.assertEqual(service.trend('AAPL')['points'], [])

    def test_off_session_and_calendar_unavailable(self):
        for mode in ('premarket', 'afterhours', 'weekend', 'holiday'):
            options = {'wait': lambda event, seconds: event.set()}
            if mode == 'premarket':
                options['now'] = lambda: AT.replace(hour=9, minute=29, second=59)
            elif mode == 'afterhours':
                options['now'] = lambda: AT.replace(hour=16, second=1)
            elif mode == 'weekend':
                options['now'] = lambda: AT + timedelta(days=2)
            else:
                options['calendar'] = lambda day: False
            service = self.service(store=Store(root=self.root / mode), **options)
            self.add(service)
            self.run_session(service)
        self.assertEqual(self.calls, [])

        def unavailable(day):
            raise OSError()
        service = self.service(calendar=unavailable, store=Store(root=self.root / 'cal'))
        self.add(service)
        self.run_session(service)
        self.assertEqual(len(self.calls), 5)

    def test_session_boundaries_and_dst(self):
        # 09:30:00 与 16:00:00 两端含；冬令时（11-02）同样按美东 09:30 判断
        for at, expected in ((datetime(2026, 10, 22, 9, 30, tzinfo=US_EASTERN), True),
                             (datetime(2026, 10, 22, 16, 0, tzinfo=US_EASTERN), True),
                             (datetime(2026, 11, 2, 9, 30, tzinfo=US_EASTERN), True),
                             (datetime(2026, 11, 2, 9, 29, tzinfo=US_EASTERN), False)):
            service = self.service(now=lambda at=at: at, store=Store(root=self.root / str(at)))
            self.assertEqual(service._eligible(), expected, at)

    # ---------- 存储 ----------
    def test_default_store_root_is_us_only_and_not_created(self):
        self.assertEqual(store_module.STORE_ROOT.parts[-2:], ('data', 'us_market_monitor'))
        self.assertEqual(Store().root, store_module.STORE_ROOT)
        from src.market_monitor import store as cn_store
        self.assertNotEqual(store_module.STORE_ROOT, cn_store.STORE_ROOT)
        # 构造 Store/MonitorService 都不创建目录、不读真实数据（根目录换成临时目录）
        self.assertEqual(Store(root=self.root / 'x').load(), empty_state())
        self.assertFalse((self.root / 'x').exists())

    def test_storage_permissions_symlinks_exclusive_and_reload(self):
        store = Store(root=self.root / 'private' / 'monitor')
        store.save(empty_state())
        file = next(store.root.glob('snapshot_*.json'))
        self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)
        for path in (store.root, store.root.parent):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
        with patch.object(store_module.uuid, 'uuid4') as unique:
            unique.return_value.hex = 'collision'
            store._revision = 9
            store.save(empty_state())
            store._revision = 9
            with self.assertRaises(StorageError):
                store.save(empty_state())
        link = self.root / 'link'
        link.symlink_to(store.root, target_is_directory=True)
        for target in (link, link / 'child'):
            with self.assertRaises(StorageError):
                Store(root=target).save(empty_state())
            with self.assertRaises(StorageError):
                Store(root=target).load()
        store.root.chmod(0o755)
        with self.assertRaises(StorageError):
            store.load()

    def test_storage_symlink_file_and_corrupt_fail_closed(self):
        for case in ('link', 'corrupt', 'mode', 'hardlink', 'format'):
            root = self.root / case
            store = Store(root=root)
            store.save(empty_state())
            original = next(root.glob('*.json'))
            nextfile = root / 'snapshot_999999999999_bad.json'
            if case == 'link':
                nextfile.symlink_to(original)
            elif case == 'corrupt':
                nextfile.write_text('{')
                nextfile.chmod(0o600)
            elif case == 'mode':
                original.chmod(0o644)
            elif case == 'hardlink':
                os.link(original, nextfile)
            else:
                nextfile.write_text('{"version": 1}')
                nextfile.chmod(0o600)
            service = self.service(store=store)
            self.assertEqual(service.status()['reason'], 'storage_error')
            with self.assertRaises(StorageError):
                service.start()
            with self.assertRaises(StorageError):
                service.configure('watch_add', 'AAPL')
            self.assertEqual(self.call(service, 'watch_add', {'code': 'AAPL'}), (500, {'error': 'STORAGE_ERROR'}))

    def test_snapshot_with_invalid_business_content_fails_closed(self):
        bad_states = []
        for watch in (['aapl'], ['AAPL', 'AAPL'], ['A', 'B', 'C', 'D', 'E', 'F'], ['600000']):
            state = empty_state()
            state['watch'] = watch
            bad_states.append(state)
        state = empty_state()
        state['watch'] = ['AAPL']
        state['rules'] = {'AAPL:up': {'symbol': 'MSFT', 'direction': 'up', 'threshold': 1}}
        bad_states.append(state)
        for i, state in enumerate(bad_states):
            store = Store(root=self.root / f'biz{i}')
            store.save(state)
            service = self.service(store=store)
            self.assertEqual(service.status()['reason'], 'storage_error', state)

    def test_storage_failure_pauses_before_exposing_alert(self):
        service = self.service()
        self.add(service)
        service.configure('rule_set', 'AAPL', Condition('AAPL', 'up', 10, 0))
        with service._lock:
            service._consume(('AAPL',), [quote(price=9)], None)
            with patch.object(service.store, 'save', side_effect=StorageError('disk_full')):
                with self.assertRaises(StorageError):
                    service._consume(('AAPL',), [quote(at=AT + timedelta(seconds=60), price=11)], None)
        self.assertEqual(service.alerts(), [])
        self.assertEqual(service.status()['reason'], 'storage_error')
        self.assertTrue(service.status()['alerts_paused'])
        service.stop()
        with self.assertRaises(StorageError):
            service.start()

    def test_storage_failure_during_running_session_stops_cleanly(self):
        service = self.service(config=Config(request_budget=5))
        self.add(service)
        with patch.object(service.store, 'save', side_effect=StorageError('disk_full')):
            self.assertTrue(service.start())
            service._thread.join(3)
            self.assertFalse(service._thread.is_alive())
        self.assertEqual(service.status()['reason'], 'storage_error')
        self.assertEqual(service.alerts(), [])

    # ---------- 业务行为 ----------
    def test_ring_trend_window_changes_and_insufficient(self):
        service = self.service()
        self.add(service)
        for i in range(100):
            self.clock.tick = i * 60
            with service._lock:
                service._consume(('AAPL',), [quote(at=self.clock.now(), price=10 + i)], None)
        self.assertEqual(len(service._data['samples']['AAPL']), 90)
        trend = service.trend('AAPL')
        self.assertEqual(trend['label'], '采样价格')
        self.assertEqual(len(trend['points']), 31)
        self.assertEqual(trend['span_seconds'], 1800)
        self.assertEqual(trend['changes']['5']['span_seconds'], 300)
        self.assertAlmostEqual(trend['changes']['15']['percent'], (109 / 94 - 1) * 100)
        self.assertEqual(service.trend('MSFT')['changes']['5']['status'], '数据不足')
        self.clock.tick += 3600
        self.assertEqual(service.trend('AAPL')['points'], [])

    def test_crossing_dedup_cooldown_daily_limit_survives_restart(self):
        service = self.service()
        self.add(service)
        service.configure('rule_set', 'AAPL', Condition('AAPL', 'up', 10, 180, 2))
        for i, price in enumerate((9, 11, 9, 11, 9, 11, 9, 11)):
            self.clock.tick = i * 60
            service._consume(('AAPL',), [quote(at=self.clock.now(), price=price)], None)
        self.assertEqual(len(service.alerts()), 2)
        for field in ('code', 'direction', 'threshold', 'previous_price', 'price', 'source_time',
                      'received_at', 'triggered_at'):
            self.assertIn(field, service.alerts()[0])
        reloaded = self.service()
        for i, price in enumerate((9, 11)):
            self.clock.tick = 600 + i * 60
            reloaded._consume(('AAPL',), [quote(at=self.clock.now(), price=price)], None)
        self.assertEqual(len(reloaded.alerts()), 2)

    def test_failure_resets_baseline_and_next_day_resets_limit(self):
        service = self.service()
        self.add(service)
        service.configure('rule_set', 'AAPL', Condition('AAPL', 'up', 10, 0, 1))
        for seconds, price, error in ((0, 9, None), (60, 11, 'network'),
                                      (120, 11, None), (180, 9, None), (240, 11, None),
                                      (300, 9, None), (360, 11, None), (86400, 9, None), (86460, 11, None)):
            self.clock.tick = seconds
            service._consume(('AAPL',), [quote(at=self.clock.now(), price=price)] if error is None else [], error)
        self.assertEqual(len(service.alerts()), 2)
        self.assertEqual(service.alerts()[0]['source_time'], (AT + timedelta(seconds=240)).isoformat())
        self.assertEqual(service.alerts()[1]['source_time'], (AT + timedelta(seconds=86460)).isoformat())

    def test_cache_ages_no_query_requests_and_quality_summary(self):
        service = self.service(config=Config(request_budget=1))
        self.add(service)
        self.assertEqual(self.call(service, 'start')[0], 200)
        service._thread.join(2)
        calls = len(self.calls)
        self.clock.tick += 1201
        status = self.call(service, 'status')[1]['data']
        self.assertTrue(status['latest']['AAPL']['stale'])
        self.assertEqual(len(self.calls), calls)
        summary = service.export_quality()['quality']
        self.assertEqual(summary['age_seconds']['count'], 1)
        self.assertEqual(summary['age_seconds']['max'], 1)
        self.assertEqual(summary['elapsed_seconds_sum'], .01)

    def test_trend_irregular_actual_span_and_api_storage_error(self):
        service = self.service()
        self.add(service)
        for seconds in (0, 200, 400, 601):
            self.clock.tick = seconds
            service._consume(('AAPL',), [quote(at=self.clock.now())], None)
        trend = self.call(service, 'trend', {'code': 'AAPL'})[1]['data']
        self.assertEqual(trend['changes']['5']['span_seconds'], 401)
        self.assertEqual(trend['changes']['15']['status'], '数据不足')
        with patch.object(service.store, 'save', side_effect=StorageError()):
            result = self.call(service, 'watch_remove', {'code': 'AAPL'})
        self.assertEqual(result, (500, {'error': 'STORAGE_ERROR'}))
        self.assertTrue(service.status()['alerts_paused'])

    # ---------- API 鉴权与校验 ----------
    def test_api_auth_matrix(self):
        service = self.service()
        reads = [a for a in api.SCHEMA if a not in api.WRITE_ACTIONS]
        body = {'code': 'AAPL'}
        for action in api.SCHEMA:
            write = action in api.WRITE_ACTIONS
            for flags in itertools.product((False, True), repeat=4):
                ctx = Ctx(*flags)
                needed = flags[:3] == (True, True, True) and (flags[3] or not write)
                payload = {'code': 'AAPL', 'direction': 'up', 'threshold': 10} if action == 'rule_set' else body
                if action in ('start', 'stop', 'status', 'alerts', 'export_quality'):
                    payload = {}
                if action == 'rule_delete':
                    payload = {'code': 'AAPL', 'direction': 'up'}
                status = api.handle(ctx, service, action, payload)
                if needed:
                    self.assertNotEqual(status[0], 403, (action, flags))
                else:
                    self.assertEqual(status, (403, {'error': 'FORBIDDEN'}), (action, flags))
        self.assertEqual(self.calls, [])
        self.assertEqual(reads, ['status', 'alerts', 'trend', 'export_quality'])

    def test_api_truthy_non_bool_and_foreign_ctx_rejected(self):
        service = self.service()
        for flag in (1, 'yes', [1], object()):
            for fields in ((flag, True, True, True), (True, flag, True, True),
                           (True, True, flag, True), (True, True, True, flag)):
                self.assertEqual(api.handle(Ctx(*fields), service, 'start')[0], 403)
        for ctx in (None, True, {'trusted_local': True}, 'owner', type('X', (), {
                'trusted_local': True, 'authed': True, 'dashboard_header': True, 'origin_ok': True})()):
            self.assertEqual(api.handle(ctx, service, 'status'), (403, {'error': 'FORBIDDEN'}))
        self.assertEqual(service.status()['state'], 'stopped')

    def test_api_unauthorized_cannot_probe_actions_and_no_side_effects(self):
        service = self.service()
        denied = Ctx(True, False, True, True)
        for action in ('start', 'nonexistent', '', None, 5):
            self.assertEqual(api.handle(denied, service, action), (403, {'error': 'FORBIDDEN'}))
        self.assertEqual(api.handle(OWNER, service, 'nonexistent'), (404, {'error': 'UNKNOWN_ACTION'}))
        self.assertEqual(api.handle(OWNER, service, None), (404, {'error': 'UNKNOWN_ACTION'}))
        self.assertEqual(api.handle(denied, service, 'watch_add', {'code': 'AAPL'})[0], 403)
        self.assertEqual(service.status()['watch'], [])
        self.assertFalse((self.root / 'monitor').exists())

    def test_api_validation_and_operations(self):
        service = self.service()
        bad = [('watch_add', {'code': '../foo'}), ('watch_add', {'code': 'aapl'}),
               ('watch_add', {'code': '600000'}), ('watch_add', {'code': 'BF.B'}),
               ('watch_add', {'code': '^GSPC'}), ('watch_add', {'code': 5}),
               ('watch_add', {'code': 'AAPL', 'path': '/tmp'}), ('watch_add', {}),
               ('start', {'request_budget': 600}), ('status', {'path': 'private'}),
               ('rule_set', {'code': 'AAPL', 'direction': 'x', 'threshold': 1}),
               ('rule_set', {'code': 'AAPL', 'direction': 'up', 'threshold': float('nan')}),
               ('rule_set', {'code': 'AAPL', 'direction': 'up', 'threshold': True}),
               ('rule_set', {'code': 'AAPL', 'direction': 'up', 'threshold': 0}),
               ('rule_set', {'code': 'AAPL', 'direction': 'up', 'threshold': 1, 'cooldown_seconds': False}),
               ('rule_set', {'code': 'AAPL', 'direction': 'up', 'threshold': 1, 'daily_limit': True}),
               ('status', b'{"x":1,"x":2}'), ('status', b'NaN'), ('status', b'[]'), ('status', b'\xff'),
               ('status', 5), ('status', b'{')]
        for action, body in bad:
            with self.subTest(action=action, body=body):
                self.assertEqual(api.handle(OWNER, service, action, body), (400, {'error': 'INVALID_INPUT'}))
        self.assertEqual(api.handle(OWNER, service, 'status', ' ' * 8193), (413, {'error': 'BODY_TOO_LARGE'}))
        for code in ('AAPL', 'MSFT', 'NVDA', 'BRK-B', 'F'):
            self.add(service, code)
        self.assertEqual(self.call(service, 'watch_add', {'code': 'TSLA'})[0], 400)
        # 重复添加幂等
        self.assertEqual(self.call(service, 'watch_add', {'code': 'AAPL'})[0], 200)
        self.assertEqual(len(service.status()['watch']), 5)
        self.assertEqual(self.call(service, 'rule_set', {'code': 'TSLA', 'direction': 'up', 'threshold': 1})[0], 400)
        self.assertEqual(self.call(service, 'rule_set', {'code': 'AAPL', 'direction': 'up', 'threshold': 10})[0], 200)
        self.assertEqual(self.call(service, 'rule_set', {'code': 'BRK-B', 'direction': 'down', 'threshold': 400})[0], 200)
        self.assertEqual(self.call(service, 'rule_delete', {'code': 'AAPL', 'direction': 'up'})[0], 200)
        self.assertEqual(list(service.status()['rules']), ['BRK-B:down'])
        self.assertEqual(self.call(service, 'watch_remove', {'code': 'BRK-B'})[0], 200)
        self.assertEqual(service.status()['rules'], {})
        self.assertEqual(self.call(service, 'trend', {'code': 'AAPL'})[0], 200)
        for action in ('alerts', 'export_quality', 'stop', 'status'):
            self.assertEqual(self.call(service, action)[0], 200)
        self.assertEqual(self.calls, [])

    # ---------- 信号适配器 ----------
    def test_signals_are_empty_and_do_no_io(self):
        service = self.service()
        self.add(service)
        with patch('os.open', side_effect=AssertionError('不得做文件 I/O')), \
                patch('builtins.open', side_effect=AssertionError('不得做文件 I/O')):
            for codes in ((), ['AAPL'], ('AAPL', 'MSFT')):
                self.assertEqual(signals_readonly.read_signals(codes, now=AT), {'status': '无信号', 'signals': []})
            data = self.call(service, 'status')[1]['data']
        self.assertEqual(data['closing_signals'], {'status': '无信号', 'signals': []})
        source = Path(signals_readonly.__file__).read_text(encoding='utf-8')
        self.assertNotIn('trade_list_cn', source.replace('data/daily/trade_list_cn.json（那是 A 股的', ''))

    def test_import_no_threads_network_files_or_app(self):
        code = '''
from unittest.mock import patch
with patch('threading.Thread.start',side_effect=AssertionError('thread')), patch('socket.socket',side_effect=AssertionError('network')), patch('pathlib.Path.read_text',side_effect=AssertionError('read')), patch('os.open',side_effect=AssertionError('storage')):
 import src.us_market_monitor
 import src.us_market_monitor.api
 import src.us_market_monitor.service
 import src.us_market_monitor.store
 import src.us_market_monitor.signals_readonly
 import sys
 assert 'app' not in sys.modules
 assert 'yfinance' not in sys.modules
print('import safe')
'''
        result = subprocess.run([sys.executable, '-B', '-c', code], cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'import safe')

    # ---------- 真实子进程（假 provider，不联网） ----------
    def test_real_process_fake_provider_success_timeout_cancel_and_exit(self):
        pids = []
        from multiprocessing.process import BaseProcess
        original_start = BaseProcess.start

        def tracked_start(process):
            original_start(process)
            pids.append(process.pid)
        with patch.object(BaseProcess, 'start', tracked_start):
            self.assertEqual(process_request(('AAPL',), threading.Event(), 1, provider_factory=ChildFake)[0].price, 9)
            for factory, expected in ((ChildHang, 'timeout'), (ChildExit, 'worker_exit'),
                                      (ChildRate, 'rate_limited'), (ChildWeird, 'provider_error'),
                                      (ChildNoYF, 'yfinance_not_installed')):
                with self.assertRaises(FeedError) as caught:
                    process_request(('AAPL',), threading.Event(), .05 if factory is ChildHang else 2,
                                    provider_factory=factory)
                self.assertEqual(caught.exception.category, expected)
            cancel = threading.Event()
            timer = threading.Timer(.15, cancel.set)
            timer.start()
            try:
                with self.assertRaises(FeedError) as caught:
                    process_request(('AAPL',), cancel, 1, provider_factory=ChildHang)
                self.assertEqual(caught.exception.category, 'cancelled')
            finally:
                timer.join()
            already = threading.Event()
            already.set()
            with self.assertRaises(FeedError):
                process_request(('AAPL',), already, 1, provider_factory=ChildFake)
            # 服务 stop 必须等到请求进程回收，丢弃未完成结果。
            started = threading.Event()

            def run_child(symbols, event, timeout):
                started.set()
                return process_request(symbols, event, timeout, provider_factory=ChildHang)
            service = self.service(request=run_child)
            self.add(service)
            service.start()
            self.assertTrue(started.wait(1))
            service.stop()
            self.assertIsNone(service._thread)
            self.assertEqual(service.export_quality()['quality']['requests'], 0)
        self.assertGreaterEqual(len(pids), 8)
        for pid in pids:
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    def test_default_request_function_has_spawn_grace(self):
        seen = {}

        def fake(symbols, cancelled, timeout, **kw):
            seen.update(kw, timeout=timeout)
            return 'ok'
        with patch('src.us_market_monitor.service.process_request', fake):
            self.assertEqual(process_us_request(('AAPL',), threading.Event(), 10), 'ok')
        self.assertGreater(seen['_extra'], 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
