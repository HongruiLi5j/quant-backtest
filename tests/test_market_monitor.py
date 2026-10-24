"""T006 S1：仅假响应/假时钟/临时目录，禁止访问网络和真实数据目录。"""
import ast
from contextlib import redirect_stdout, redirect_stderr
from dataclasses import replace
from datetime import datetime, timedelta
import importlib.util
import http.client
import io
import json
from pathlib import Path
import signal
import socket
import stat
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.market_monitor.core import (BEIJING, Quote, validate_quote, continuous_session,
                                     Condition, AlertState, alert_step)
from src.market_monitor.providers import (TencentProvider, FeedError, parse_response, deadline,
                                          NoRedirect, TimeoutSafeHTTPSConnection,
                                          TimeoutSafeHTTPSHandler)
from scripts import validate_tencent_feed as script

AT = datetime(2026, 10, 22, 10, 0, tzinfo=BEIJING)
# 保留标准库实现；测试仅在假 DNS/假套接字下运行它，不打开真实连接。
STDLIB_CREATE_CONNECTION = socket.create_connection


def body(symbol='sh600000', code='600000', at=AT, price='9.64', vol='405836'):
    fields = [''] * 31
    fields[0:7] = ['1', '浦发银行', code, price, '9.66', '9.66', vol]
    fields[30] = at.strftime('%Y%m%d%H%M%S')
    return ('v_' + symbol + '="' + '~'.join(fields) + '";').encode('gbk')


def quote(price=9., seconds=0, **changes):
    at = AT + timedelta(seconds=seconds)
    q = Quote('sh600000', '浦发银行', price, 9.66, 9.66, '100', at.strftime('%Y%m%d%H%M%S'),
              None, at + timedelta(seconds=1), .1)
    return validate_quote(replace(q, **changes), 'sh600000')


class OfflineTests(unittest.TestCase):
    def setUp(self):
        for name in ('socket', 'create_connection'):
            guard = patch.object(socket, name, side_effect=AssertionError('测试禁止联网'))
            guard.start()
            self.addCleanup(guard.stop)

    def assert_error(self, category, fn):
        with self.assertRaises(FeedError) as caught:
            fn()
        self.assertEqual(caught.exception.category, category)
        return caught.exception

    def test_gbk_and_whitelist(self):
        q = parse_response(body(), ('sh600000',), AT, .2)[0]
        self.assertEqual(q.name, '浦发银行')
        self.assertEqual(q.price, 9.64)
        self.assertEqual(q.volume_raw, '405836')
        self.assertEqual(q.fields_raw, ('9.64', '9.66', '9.66', '405836', '20261022100000'))
        self.assertEqual(validate_quote(q, q.symbol).source_time, AT)
        self.assertEqual(q.quality, 'unchecked')

    def test_parse_error_categories(self):
        for raw, category in [(b'\xff', 'gbk_decode_error'),
                              (b'v_sh600000="1~name~600000";', 'insufficient_fields'),
                              (body(code='600001'), 'code_mismatch'),
                              (body(vol='1'*65), 'whitelist_field_too_long'),
                              (body(symbol='sz000001', code='000001'), 'code_mismatch'),
                              (b'upstream private text', 'response_format_error'),
                              (body() + b'\n' + body(), 'response_count_mismatch')]:
            with self.subTest(category=category):
                error = self.assert_error(category, lambda: parse_response(raw, ('sh600000',), AT, 0))
                self.assertNotIn('private', str(error))
        for value in ('nan', 'inf', '-1', '0', 'garbage'):
            q = parse_response(body(price=value), ('sh600000',), AT, 0)[0]
            self.assertIn('invalid_price', validate_quote(q, q.symbol).reasons)

    def test_multi_code_offline_only_unverified(self):
        data = body(symbol='sz000001', code='000001') + b'\n' + body()
        rows = parse_response(data, ('sh600000', 'sz000001'), AT, .1)
        self.assertEqual([q.symbol for q in rows], ['sh600000', 'sz000001'])
        self.assert_error('code_mismatch', lambda: parse_response(body() + b'\n' + body(), ('sh600000', 'sz000001'), AT, 0))

    def test_quality_values(self):
        for field in ('price', 'previous_close', 'opening'):
            for value in (None, float('nan'), float('inf'), -1, 0, True):
                self.assertIn('invalid_price', quote(**{field: value}).reasons)
        for value in ('NaN', 'Infinity', '-1', 'bad'):
            self.assertIn('invalid_volume', quote(volume_raw=value).reasons)
        self.assertIn('invalid_elapsed', quote(elapsed_seconds=-1).reasons)
        self.assertIn('code_mismatch', validate_quote(quote(), 'sz000001').reasons)
        self.assertIn('invalid_received_time', quote(received_at=AT.replace(tzinfo=None)).reasons)
        for raw in ('', '20260230093000', '202610220930', 'garbage'):
            self.assertIn('invalid_source_time', quote(source_time_raw=raw).reasons)

    def test_age_and_timezones(self):
        self.assertEqual(quote(received_at=AT + timedelta(seconds=120)).quality, 'valid')
        self.assertEqual(quote(received_at=AT + timedelta(seconds=121)).quality, 'stale')
        self.assertEqual(validate_quote(quote(received_at=AT + timedelta(seconds=121)), 'sh600000', stale_seconds=122).quality, 'valid')
        self.assertEqual(quote(received_at=AT - timedelta(seconds=5)).quality, 'valid')
        self.assertIn('future_time', quote(received_at=AT - timedelta(seconds=6)).reasons)
        la = datetime.fromisoformat('2026-10-21T19:00:01-07:00')
        self.assertEqual(quote(received_at=la).quality, 'valid')
        self.assertEqual(quote(received_at=la).received_at.utcoffset(), timedelta(hours=-7))
        off = quote(received_at=AT.replace(hour=12))
        self.assertEqual(off.quality, 'off_session')
        self.assertEqual(off.session, '非交易时段')
        self.assertNotIn('stale', off.reasons)

    def test_sequence_and_volume(self):
        first = quote()
        self.assertIn('duplicate_timestamp', validate_quote(first, first.symbol, first).reasons)
        self.assertIn('timestamp_regression', validate_quote(quote(seconds=-1), first.symbol, first).reasons)
        self.assertIn('volume_regression', validate_quote(quote(seconds=1, volume_raw='99'), first.symbol, first).reasons)
        next_day = quote(seconds=86400, volume_raw='1')
        self.assertNotIn('volume_regression', validate_quote(next_day, first.symbol, first).reasons)

    def test_session_boundaries_and_no_holiday_claim(self):
        for hour, minute, expected in [(9,29,False),(9,30,True),(11,30,True),(11,31,False),
                                       (12,59,False),(13,0,True),(15,0,True),(15,1,False)]:
            self.assertEqual(continuous_session(AT.replace(hour=hour, minute=minute)), expected)
        self.assertFalse(continuous_session(AT + timedelta(days=2)))
        # 10 月 1 日也只给钟表提示，绝不以此声称节假日休市。
        self.assertTrue(continuous_session(AT.replace(day=1)))
        with self.assertRaises(ValueError):
            continuous_session(AT.replace(tzinfo=None))

    def test_crossing_dedup_both_directions(self):
        for direction, before, after in [('up', 9, 11), ('down', 11, 9)]:
            c = Condition('sh600000', direction, 10, cooldown_seconds=0)
            state, alert = alert_step(AlertState(), c, quote(before))
            self.assertIsNone(alert)
            state, alert = alert_step(state, c, quote(after, 1))
            self.assertIsNotNone(alert)
            self.assertEqual(alert.previous_price, before)
            state, alert = alert_step(state, c, quote(after, 2))
            self.assertIsNone(alert)
            state, alert = alert_step(state, c, quote(after, 2))
            self.assertIsNone(alert)
            self.assertIsNone(state.baseline)
        state, event = alert_step(AlertState(), c, quote(after))
        self.assertIsNone(event)

    def test_alert_cooldown_and_daily_limit(self):
        c = Condition('sh600000', 'up', 10, cooldown_seconds=5, daily_limit=2)
        state = AlertState()
        events = []
        for second, price in [(0,9),(1,10),(2,9),(3,11),(6,9),(7,11),(8,9),(15,11)]:
            state, event = alert_step(state, c, quote(price, second))
            if event:
                events.append(second)
        self.assertEqual(events, [1, 7])
        self.assertEqual(state.count, 2)
        state, event = alert_step(state, c, quote(9, 86400))
        self.assertIsNone(event)
        state, event = alert_step(state, c, quote(10, 86401))
        self.assertIsNotNone(event)
        self.assertEqual(state.count, 1)

    def test_alert_reset_on_quality_disconnect_config_and_gap(self):
        c = Condition('sh600000', 'up', 10, cooldown_seconds=0)
        for bad in (None, quote(11, 1, volume_raw='bad'), quote(11, 1, source_time_raw='bad'),
                    quote(11, 1, received_at=AT + timedelta(seconds=200)),
                    quote(11, -1), quote(11, 0)):
            state, _ = alert_step(AlertState(), c, quote(9))
            state, event = alert_step(state, c, bad)
            self.assertIsNone(event)
            self.assertIsNone(state.baseline)
            state, event = alert_step(state, c, quote(11, 2))
            self.assertIsNone(event)
        state, _ = alert_step(AlertState(), c, quote(9))
        state, event = alert_step(state, c, quote(11, 1), connected=False)
        self.assertIsNone(state.baseline)
        state, event = alert_step(state, c, quote(11, 2))
        self.assertIsNone(event)
        state, event = alert_step(state, replace(c, threshold=12), quote(13, 3))
        self.assertIsNone(event)
        state, _ = alert_step(AlertState(), c, quote(9))
        state, event = alert_step(state, c, quote(11, 121))
        self.assertIsNone(event)
        self.assertIsNotNone(state.baseline)
        state, event = alert_step(state, c, quote(9, 122))
        state, event = alert_step(state, c, quote(11, 123))
        self.assertIsNotNone(event)

    def fake_opener(self, payload=None, status=200, error=None):
        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.closed = True
            def read(self, size):
                return body() if payload is None else payload
        response = Response()
        response.status = status
        from unittest.mock import Mock
        opener = Mock()
        opener.open.side_effect = error
        opener.open.return_value = response
        return opener

    def test_provider_default_closed_limits_and_restart(self):
        tick = [0.]
        p = TencentProvider(monotonic=lambda: tick[0], now=lambda: AT, request_budget=2)
        self.assert_error('not_started', p.fetch)
        p.start()
        self.assert_error('already_started', p.start)
        opener = self.fake_opener()
        with patch('urllib.request.build_opener', return_value=opener) as build:
            self.assertEqual(p.fetch()[0].price, 9.64)
            request = opener.open.call_args.args[0]
            self.assertEqual(request.full_url, 'https://qt.gtimg.cn/q=sh600000')
            self.assertEqual(request.header_items(), [])
            self.assertEqual(build.call_args.args[0].proxies, {})
            self.assertIsInstance(build.call_args.args[1], NoRedirect)
            self.assertIsInstance(build.call_args.args[2], TimeoutSafeHTTPSHandler)
            self.assert_error('minimum_interval', p.fetch)
            p.stop()
            p.start()
            self.assert_error('minimum_interval', p.fetch)
            tick[0] = 60
            p.fetch()
            tick[0] = 120
            p.fetch()
            self.assert_error('budget_exceeded', p.fetch)
            self.assertEqual(p.requests_used, 2)
            self.assertEqual(opener.open.call_count, 3)
        for symbols in ((), ('bad',), ('sh600000',)*2, ('sh600000',)*6):
            self.assert_error('invalid_symbols', lambda: p.fetch(symbols))
        for kw in ({'min_interval':59}, {'min_interval':float('nan')}, {'timeout':11},
                   {'timeout':0}, {'request_budget':6}, {'request_budget':True}):
            with self.assertRaises(ValueError):
                TencentProvider(**kw)

    def test_provider_limits_are_read_only(self):
        p = TencentProvider()
        for field in ('min_interval', 'request_budget', 'timeout'):
            with self.assertRaises(AttributeError):
                setattr(p, field, 0)
        self.assertEqual((p.min_interval, p.request_budget, p.timeout), (60, 3, 10))

    def test_http_protocol_errors_are_sanitized(self):
        for phase in ('open', 'read'):
            for error in (http.client.IncompleteRead(b'sensitive upstream', 10),
                          http.client.LineTooLong('sensitive upstream'),
                          http.client.BadStatusLine('sensitive upstream')):
                with self.subTest(phase=phase, error=type(error).__name__):
                    p = TencentProvider()
                    p.start()
                    opener = self.fake_opener()
                    response = opener.open.return_value
                    if phase == 'open':
                        opener.open.side_effect = error
                    else:
                        from unittest.mock import Mock
                        response.read = Mock(side_effect=error)
                    with patch('urllib.request.build_opener', return_value=opener):
                        caught = self.assert_error('network_error', p.fetch)
                    self.assertEqual(str(caught), 'network_error')
                    self.assertTrue(caught.__suppress_context__)
                    self.assertEqual(p.requests_used, 1)
                    self.assertEqual(opener.open.call_count, 1)
                    if phase == 'read':
                        self.assertTrue(response.closed)

    def test_errors_no_retry_or_sensitive_text(self):
        cases = [(urllib.error.URLError('sensitive upstream'), 'network_error', None),
                 (TimeoutError('sensitive upstream'), 'timeout', None),
                 (urllib.error.URLError(TimeoutError()), 'timeout', None),
                 (urllib.error.HTTPError('url',302,'sensitive',{},None), 'redirect_refused',302),
                 (urllib.error.HTTPError('url',429,'sensitive',{},None), 'http_status',429)]
        for error, category, status in cases:
            p = TencentProvider()
            p.start()
            opener = self.fake_opener(error=error)
            with patch('urllib.request.build_opener', return_value=opener):
                caught = self.assert_error(category, p.fetch)
            self.assertEqual(caught.http_status, status)
            self.assertNotIn('sensitive', str(caught))
            self.assertEqual(opener.open.call_count, 1)
            self.assertEqual(p.requests_used, 1)
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, '', {}, 'https://example.com'))
        p = TencentProvider()
        p.start()
        with patch('urllib.request.build_opener', return_value=self.fake_opener(status=503)):
            self.assertEqual(self.assert_error('http_status', p.fetch).http_status, 503)
        for payload, expected in [(b'x'*65537, 'response_too_large'), (b'\xff', 'gbk_decode_error')]:
            p = TencentProvider()
            p.start()
            with patch('urllib.request.build_opener', return_value=self.fake_opener(payload=payload)):
                self.assertEqual(self.assert_error(expected, p.fetch).http_status, 200)

    def test_signal_timeout_and_restore(self):
        original = signal.getsignal(signal.SIGALRM)
        p = TencentProvider(timeout=.01)
        p.start()
        opener = self.fake_opener()
        completed = []
        def slow(*args, **kwargs):
            time.sleep(.1)
            completed.append(True)
        opener.open.side_effect = slow
        with patch('urllib.request.build_opener', return_value=opener):
            self.assert_error('timeout', p.fetch)
        # 验证闹钟中断了调用，不用严格墙钟耗时断言，避免繁忙机器偶发失败。
        self.assertEqual(completed, [])
        self.assertEqual(signal.getsignal(signal.SIGALRM), original)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0., 0.))
        with patch('signal.getitimer', return_value=(1., 0.)):
            with self.assertRaises(FeedError):
                with deadline(1):
                    self.fail('必须保留已有闹钟')
        with patch('threading.current_thread', return_value=object()):
            with self.assertRaises(FeedError):
                with deadline(1):
                    self.fail('必须拒绝不支持的线程')

    def test_signal_timeout_closes_socket_without_address_retry(self):
        # 实际 HTTPS 连接类，假 DNS/套接字；只尝试首地址并在信号异常时关闭。
        attempts, sent, closed = [], [], []
        addresses = [('192.0.2.1', 443), ('192.0.2.2', 443)]
        class FakeSocket:
            def __init__(self, *args):
                pass
            def settimeout(self, seconds):
                pass
            def connect(self, address):
                attempts.append(address)
                if address == addresses[0]:
                    time.sleep(.2)
            def close(self):
                closed.append(True)
        def connect_and_send(*args, **kwargs):
            connection = TimeoutSafeHTTPSConnection('offline.invalid', timeout=10)
            connection.connect()
            sent.append(True)
            connection.close()
            return self.fake_opener().open.return_value
        original = signal.getsignal(signal.SIGALRM)
        p = TencentProvider(timeout=.02)
        p.start()
        opener = self.fake_opener()
        opener.open.side_effect = connect_and_send
        resolved = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', address) for address in addresses]
        with patch.object(socket, 'getaddrinfo', return_value=resolved), \
                patch.object(socket, 'socket', FakeSocket), \
                patch('urllib.request.build_opener', return_value=opener):
            self.assert_error('timeout', p.fetch)
        self.assertEqual(attempts, addresses[:1])
        self.assertEqual(sent, [])
        self.assertEqual(closed, [True])
        self.assertEqual(p.requests_used, 1)
        self.assertEqual(opener.open.call_count, 1)
        self.assertEqual(signal.getsignal(signal.SIGALRM), original)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0., 0.))

    def test_signal_timeout_during_interruptible_fake_dns_and_read(self):
        # sleep 可被 Python 信号中断；不能证明真实 getaddrinfo 的 C 阻塞有硬上限。
        for phase in ('dns', 'read'):
            completed = []
            def slow(*args, **kwargs):
                time.sleep(.2)
                completed.append(True)
                return []
            opener = self.fake_opener()
            p = TencentProvider(timeout=.02)
            p.start()
            with patch('urllib.request.build_opener', return_value=opener), \
                    patch.object(socket, 'getaddrinfo', side_effect=slow):
                if phase == 'dns':
                    opener.open.side_effect = lambda *a, **k: STDLIB_CREATE_CONNECTION(('offline.invalid', 443), timeout=10)
                else:
                    opener.open.return_value.read = slow
                self.assert_error('timeout', p.fetch)
            self.assertEqual(completed, [])
            self.assertEqual(p.requests_used, 1)
            self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0., 0.))

    def test_https_handler_and_tls_failure_cleanup(self):
        handler = TimeoutSafeHTTPSHandler()
        request = urllib.request.Request('https://qt.gtimg.cn/q=sh600000')
        with patch.object(handler, 'do_open', return_value='offline') as opened:
            self.assertEqual(handler.https_open(request), 'offline')
        self.assertEqual(opened.call_args.args, (TimeoutSafeHTTPSConnection, request))
        self.assertIs(opened.call_args.kwargs['context'], handler._context)
        from unittest.mock import Mock
        sock = Mock()
        connection = TimeoutSafeHTTPSConnection('offline.invalid', timeout=10)
        def tls_failure():
            connection.sock = sock
            raise FeedError('timeout')
        with patch.object(http.client.HTTPSConnection, 'connect', side_effect=tls_failure):
            self.assert_error('timeout', connection.connect)
        sock.close.assert_called_once_with()
        self.assertIsNone(connection.sock)

    def test_static_headers_and_import_no_network(self):
        # 真实 opener 的请求预处理会补默认 UA；这里只运行预处理，不调用 open。
        import urllib.request
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        request = urllib.request.Request('https://qt.gtimg.cn/q=sh600000')
        self.assertEqual(request.header_items(), [])
        handler = next(h for h in opener.handlers if isinstance(h, urllib.request.HTTPHandler))
        processed = handler.http_request(request)
        self.assertEqual(processed.get_header('User-agent'), dict(opener.addheaders)['User-agent'])
        source = (ROOT/'src/market_monitor/providers.py').read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                self.assertNotIn(getattr(node.func, 'attr', ''), ('add_header', 'add_unredirected_header', 'install_opener'))
                self.assertFalse(any(k.arg == 'headers' for k in node.keywords))
        spec = importlib.util.spec_from_file_location('offline_import_probe', ROOT/'src/market_monitor/__init__.py', submodule_search_locations=[])
        with patch('urllib.request.build_opener', side_effect=AssertionError('导入禁止联网')):
            spec.loader.exec_module(importlib.util.module_from_spec(spec))
        self.assertNotIn('app', sys.modules)

    def test_output_permissions_exclusive_and_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()/'monitor'/'validation'
            with patch.object(script.uuid, 'uuid4') as unique, patch.object(script, 'datetime') as clock:
                unique.return_value.hex = 'fixed'
                clock.now.return_value.strftime.return_value = 'fixed'
                stream, path = script.secure_output(root)
                stream.close()
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(root.parent.stat().st_mode), 0o700)
                with self.assertRaises(FileExistsError):
                    script.secure_output(root)
            link = Path(tmp).resolve()/'link'
            link.symlink_to(root, target_is_directory=True)
            for target in (link, link/'nested'):
                with self.assertRaises(OSError):
                    script.secure_output(target)
            root.chmod(0o755)
            with self.assertRaises(ValueError):
                script.secure_output(root)

    def test_script_bounds_and_default_no_io(self):
        for argv in (['--count','6'], ['--count','0'], ['--code','600000,000001'], ['--code','000001'],
                     ['--interval','59'], ['--interval','nan'], ['--timeout','11'], ['--timeout','nan'],
                     ['--stale-seconds','0'], ['--run','--self-test']):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                script.arguments(argv)
        with patch.object(script, 'secure_output', side_effect=AssertionError('默认禁止写入')), redirect_stdout(io.StringIO()):
            self.assertEqual(script.main([]), 0)

    def test_script_sampling_margin_with_real_provider(self):
        ticks, calls, waits, instances = [0.], [], [], []
        def advance(seconds):
            waits.append(seconds)
            ticks[0] += seconds
        class DelayedFirstFetch(TencentProvider):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                instances.append(self)
            def fetch(self, symbols=('sh600000',)):
                if not calls:
                    ticks[0] += .1
                calls.append(ticks[0])
                return super().fetch(symbols)
        opener = self.fake_opener()
        def response(*args, **kwargs):
            return self.fake_opener(payload=body(at=AT + timedelta(seconds=int(ticks[0])))).open.return_value
        opener.open.side_effect = response
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()), \
                patch('urllib.request.build_opener', return_value=opener):
            result = script.probe(script.arguments(['--run']), Path(tmp).resolve()/'monitor'/'validation',
                                  provider_factory=DelayedFirstFetch, sleep=advance,
                                  monotonic=lambda: ticks[0], now=lambda: AT + timedelta(seconds=ticks[0]))
        self.assertEqual(result, 0)
        self.assertEqual(instances[0].requests_used, 3)
        self.assertEqual(opener.open.call_count, 3)
        self.assertEqual(len(waits), 2)
        self.assertTrue(all(b-a >= 60 for a, b in zip(calls, calls[1:])))
        self.assertAlmostEqual(waits[0], 60.4)

    def test_script_fake_run_and_failure_stops(self):
        for mode in ('success', 'network', 'duplicate', 'regression', 'stale'):
            ticks, calls = [0.], []
            def advance(seconds):
                ticks[0] += seconds
            class Fake(TencentProvider):
                def fetch(self, symbols=('sh600000',)):
                    calls.append(ticks[0])
                    if mode == 'network':
                        raise FeedError('network_error')
                    seconds = int(ticks[0])
                    if mode == 'duplicate':
                        seconds = 0
                    elif mode == 'regression' and len(calls) > 1:
                        seconds = -1
                    elif mode == 'stale':
                        seconds -= 121
                    return parse_response(body(at=AT + timedelta(seconds=seconds)), symbols,
                                          AT + timedelta(seconds=ticks[0]), .1)
            with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()):
                root = Path(tmp).resolve()/'monitor'/'validation'
                args = script.arguments(['--run','--count','3'])
                result = script.probe(args, root, provider_factory=Fake, sleep=advance,
                                      monotonic=lambda:ticks[0], now=lambda:AT + timedelta(seconds=ticks[0]))
                rows = [json.loads(line) for line in next(root.glob('*.jsonl')).read_text().splitlines()]
                self.assertEqual(result, int(mode != 'success'))
                expected_count = 3 if mode == 'success' else (2 if mode in ('duplicate','regression') else 1)
                self.assertEqual(len(calls), expected_count)
                self.assertEqual(len(rows), expected_count)
                self.assertTrue(all(b-a >= 60 for a,b in zip(calls,calls[1:])))
                self.assertTrue(all(row['conclusion'] == script.CONCLUSION for row in rows))
                if mode == 'network':
                    self.assertEqual(rows[-1]['error_category'], 'network_error')
                else:
                    self.assertEqual(rows[0]['http_status'], 200)
                    self.assertEqual(len(rows[0]['fields_4_5_6_7_31']), 5)


if __name__ == '__main__':
    unittest.main(verbosity=2)
