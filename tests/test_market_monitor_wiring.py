"""T006 S3 接线测试：wiring 纯函数、app.py 接入冒烟（AST 提取 Handler）、前端静态检查。
不导入 app、不联网、不读写 data/、不启动真实采集。"""
import ast
import io
import json
import re
import sys
import tempfile
import unittest
from itertools import product
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src import admin, members, personal_mode
from src.market_monitor import wiring
from src.market_monitor.scheduler import MonitorScheduler
from src.market_monitor.store import Store

GOOD = wiring.Ctx(True, True, True, True)
READS = ('handle_status', 'handle_quotes', 'handle_alerts')
WRITES = ('handle_start', 'handle_stop')


class FakeScheduler:
    """记录调用；wiring 只能通过这几个方法碰调度器。"""
    def __init__(self):
        self.calls, self.interval, self.top_n, self.running = [], 60, 20, False

    def status(self):
        return {'state': 'running' if self.running else 'stopped', 'interval': self.interval, 'top_n': self.top_n}

    def quotes(self):
        return [{'code': '600000'}]

    def alerts(self):
        return []

    def start(self):
        self.calls.append('start')
        started, self.running = not self.running, True
        return started

    def stop(self):
        self.calls.append('stop')
        self.running = False

    def set_config(self, interval, top_n):
        self.calls.append(('config', interval, top_n))
        self.interval, self.top_n = interval, top_n


class WiringTests(unittest.TestCase):
    def call(self, name, ctx, sched, body=None):
        fn = getattr(wiring, name)
        return fn(ctx, sched, body) if name == 'handle_config' else fn(ctx, sched)

    def test_auth_matrix_reads(self):
        for name in READS:
            for flags in product((True, False), repeat=3):
                sched = FakeScheduler()
                got = self.call(name, wiring.Ctx(*flags, origin_ok=False), sched)
                if all(flags):
                    self.assertEqual(got[0], 200, name)
                else:
                    self.assertEqual(got, wiring.FORBIDDEN, (name, flags))

    def test_auth_matrix_writes_and_no_side_effect(self):
        for name in WRITES + ('handle_config',):
            for flags in product((True, False), repeat=4):
                sched = FakeScheduler()
                got = self.call(name, wiring.Ctx(*flags), sched, b'{"interval": 60}')
                if all(flags):
                    self.assertEqual(got[0], 200, name)
                else:
                    self.assertEqual(got, wiring.FORBIDDEN, (name, flags))
                    self.assertEqual(sched.calls, [], (name, flags))

    def test_forbidden_is_uniform_and_leaks_nothing(self):
        bodies = {self.call(n, wiring.Ctx(), FakeScheduler(), b'{}')[1].__repr__() for n in READS + WRITES + ('handle_config',)}
        self.assertEqual(bodies, {repr({'error': 'FORBIDDEN'})})

    def test_truthy_but_not_true_is_rejected(self):
        for bad in (1, 'yes', [1], object()):
            self.assertEqual(wiring.handle_status(wiring.Ctx(bad, True, True, True), FakeScheduler()), wiring.FORBIDDEN)
            self.assertEqual(wiring.handle_start(wiring.Ctx(True, True, True, bad), FakeScheduler()), wiring.FORBIDDEN)

    def test_config_valid_and_partial(self):
        s = FakeScheduler()
        self.assertEqual(wiring.handle_config(GOOD, s, b'{"interval": 45, "top_n": 10}')[0], 200)
        self.assertEqual((s.interval, s.top_n), (45, 10))
        self.assertEqual(wiring.handle_config(GOOD, s, '{"top_n": 0}')[0], 200)
        self.assertEqual((s.interval, s.top_n), (45, 0))                  # 未给的字段保持不变

    def test_config_top_n_upper_bound_is_max_watch(self):
        """T016 S2 D10：top_n 上界随调度器 max_watch（默认 500）；61..500 接受，501 拒绝；max_watch 本身仍不能经 HTTP 改。"""
        for ok in (61, 100, 500):
            s = FakeScheduler()
            self.assertEqual(wiring.handle_config(GOOD, s, b'{"top_n": %d}' % ok)[0], 200, ok)
            self.assertEqual(s.top_n, ok)
        for bad in (501, 5500):
            s = FakeScheduler()
            self.assertEqual(wiring.handle_config(GOOD, s, b'{"top_n": %d}' % bad), wiring.INVALID, bad)
            self.assertEqual(s.calls, [])
        small = FakeScheduler()      # 实例的 max_watch 更小时，上界跟着变小
        small.status = lambda: {'state': 'stopped', 'interval': 60, 'top_n': 20, 'max_watch': 60}
        self.assertEqual(wiring.handle_config(GOOD, small, b'{"top_n": 61}'), wiring.INVALID)
        self.assertEqual(wiring.handle_config(GOOD, small, b'{"top_n": 60}')[0], 200)
        self.assertEqual(wiring.handle_config(GOOD, small, b'{"max_watch": 999}'), wiring.INVALID)

    def test_config_interval_floor_follows_batch_mode(self):
        batch = FakeScheduler()
        batch.status = lambda: {'state': 'stopped', 'interval': 60, 'top_n': 20, 'batch_mode': True}
        self.assertEqual(wiring.handle_config(GOOD, batch, b'{"interval": 10}')[0], 200)
        self.assertEqual(wiring.handle_config(GOOD, batch, b'{"interval": 9}'), wiring.INVALID)
        self.assertEqual(wiring.handle_config(GOOD, FakeScheduler(), b'{"interval": 10}'), wiring.INVALID)

    def test_config_rejects_bad_input_without_changes(self):
        bad = [b'', b'[]', b'{}', b'null', b'{"interval": 29}', b'{"interval": 3601}', b'{"interval": 60.5}',
               b'{"interval": true}', b'{"interval": "60"}', b'{"top_n": -1}', b'{"top_n": 501}', b'{"top_n": 1.0}',
               b'{"top_n": null}', b'{"interval": NaN}', b'{"interval": Infinity}', b'{"interval": 60, "interval": 61}',
               b'{"interval": 60, "request_budget": 600}', b'{"min_interval": 1}', b'{"max_watch": 999}',
               b'{"multi_code": true}', b'\xff\xfe', b'{' + b'"a":1,' * 10 + b'}', b'x' * 5000,
               b'{"interval":' + b'[' * 5000 + b']' * 5000 + b'}', 12345, None]
        for body in bad:
            s = FakeScheduler()
            got = wiring.handle_config(GOOD, s, body)
            self.assertEqual(got, wiring.INVALID, body)
            self.assertEqual(s.calls, [], body)
        self.assertNotIn('NaN', repr(wiring.INVALID))

    def test_start_stop_idempotent_with_real_scheduler(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        waits = []
        s = MonitorScheduler(store=Store(root=Path(tmp.name).resolve() / 's'), request=Mock(side_effect=AssertionError),
                             build_fn=lambda n: {'codes': [], 'sources': {}, 'targets': {}, 'truncated': False},
                             now=lambda: __import__('datetime').datetime(2026, 10, 24, 10, 0, tzinfo=__import__('zoneinfo').ZoneInfo('Asia/Shanghai')),
                             wait=lambda ev, sec: (waits.append(sec), __import__('time').sleep(0.001)))
        self.addCleanup(s.stop)
        self.assertEqual(wiring.handle_stop(GOOD, s)[1]['status']['state'], 'stopped')   # 未启动时停止也成功
        first = wiring.handle_start(GOOD, s)
        second = wiring.handle_start(GOOD, s)
        self.assertEqual((first[1]['started'], second[1]['started']), (True, False))
        self.assertEqual(second[1]['status']['state'], 'running')
        thread = s._thread
        self.assertEqual(wiring.handle_stop(GOOD, s)[1]['status']['state'], 'stopped')
        self.assertEqual(wiring.handle_stop(GOOD, s)[1]['status']['state'], 'stopped')
        self.assertFalse(thread.is_alive())

    def test_rate_and_budget_not_reachable_via_config(self):
        schema_keys = set()
        s = FakeScheduler()
        for key in ('request_budget', 'min_interval', 'symbol_gap', 'max_watch', 'multi_code', 'timeout'):
            self.assertEqual(wiring.handle_config(GOOD, s, json.dumps({key: 1}).encode()), wiring.INVALID, key)
        self.assertEqual(schema_keys, set())


# ---------------- app.py 接入冒烟（不导入 app，只用 AST 取出 Handler 的相关方法）
APP_TREE = ast.parse((ROOT / 'app.py').read_text(encoding='utf-8'))
HANDLER = next(n for n in APP_TREE.body if isinstance(n, ast.ClassDef) and n.name == 'Handler')
METHODS = {n.name: n for n in HANDLER.body if isinstance(n, ast.FunctionDef)}
ASSIGNS = [n for n in HANDLER.body if isinstance(n, ast.Assign)]
HOST, TOKEN = '127.0.0.1:8765', 'synthetic-token'
ROUTES = (('GET', '/api/market_monitor/status'), ('GET', '/api/market_monitor/quotes'),
          ('GET', '/api/market_monitor/alerts'), ('POST', '/api/market_monitor/start'),
          ('POST', '/api/market_monitor/stop'), ('POST', '/api/market_monitor/config'))


def build_class(scheduler):
    names = ('do_GET', 'do_POST', '_personal_blocked', '_market_monitor', '_authed', '_remote', '_external')
    cls = ast.ClassDef(name='Handler', bases=[], keywords=[], decorator_list=[],
                       body=ASSIGNS + [METHODS[n] for n in names])
    ns = dict(personal_mode=personal_mode, urlparse=urlparse, parse_qs=parse_qs, json=json, admin=admin,
              members=members, ALLOWED_HOSTS={HOST, 'localhost:8765'}, COOKIE='qb', MEMBER_COOKIE='qm',
              TOKEN=TOKEN, hmac=__import__('hmac'), SimpleCookie=__import__('http.cookies', fromlist=['x']).SimpleCookie,
              market_monitor_scheduler=lambda: scheduler, meta=lambda: {}, _alive={}, _bad_login=[],
              time=SimpleNamespace(time=lambda: 0))
    exec(compile(ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[])), '<handler>', 'exec'), ns)
    return ns['Handler']


class AppSmokeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = Path(self.tmp.name) / 'personal_mode.json'
        for p in (patch.object(personal_mode, 'CONFIG', self.config), patch.object(members, 'hit', lambda *a: False)):
            p.start()
            self.addCleanup(p.stop)
        self.sched = FakeScheduler()
        self.cls = build_class(self.sched)

    def mode(self, on):
        self.config.write_text(json.dumps({'enabled': on}), encoding='utf-8')

    def call(self, method, path, body=None, headers=None, cookie=True, xrw=True):
        h = self.cls()
        raw = json.dumps(body).encode() if body is not None else b''
        hdr = {'Host': HOST, 'Content-Length': str(len(raw))}
        if cookie:
            hdr['Cookie'] = f'qb={TOKEN}'
        if xrw:
            hdr['X-Requested-With'] = 'dashboard'
        hdr.update(headers or {})
        h.command, h.path, h.headers, h.rfile = method, path, hdr, io.BytesIO(raw)
        h._guard = Mock(return_value=True)
        h.sent = []
        h._send = lambda code, data, ctype, *a, **k: h.sent.append((code, data))
        h._json = lambda obj, code=200: h.sent.append((code, obj))
        for name in ('_admin_get', '_admin_post', '_ai', '_holdings', '_share_login', '_member_get', '_member_post',
                     '_page', '_static', '_post_engine'):
            setattr(h, name, Mock())
        h._share_gate = Mock(return_value=False)
        getattr(h, 'do_' + method)()
        return h.sent[-1] if h.sent else None

    def test_mode_off_or_missing_is_404_and_untouched(self):
        for state in (None, False):
            if state is not None:
                self.mode(state)
            for method, path in ROUTES:
                for cookie in (True, False):
                    self.assertEqual(self.call(method, path, {} if method == 'POST' else None, cookie=cookie),
                                     (404, b'not found'), (state, path))
        self.assertEqual(self.sched.calls, [])

    def test_mode_on_calls_handlers(self):
        self.mode(True)
        self.assertEqual(self.call('GET', '/api/market_monitor/status')[1]['status']['state'], 'stopped')
        self.assertEqual(self.sched.calls, [])                            # 查询状态不启动
        self.assertEqual(self.call('POST', '/api/market_monitor/start', {})[1]['started'], True)
        self.assertEqual(self.call('POST', '/api/market_monitor/start', {})[1]['started'], False)
        self.assertEqual(self.call('GET', '/api/market_monitor/quotes')[1]['quotes'], [{'code': '600000'}])
        self.assertEqual(self.call('GET', '/api/market_monitor/alerts')[1], {'ok': True, 'alerts': []})
        self.assertEqual(self.call('POST', '/api/market_monitor/config', {'interval': 90})[0], 200)
        self.assertEqual(self.sched.interval, 90)
        self.assertEqual(self.call('POST', '/api/market_monitor/stop', {})[1]['status']['state'], 'stopped')

    def test_mode_on_auth_failures_are_403_with_no_side_effects(self):
        self.mode(True)
        for method, path in ROUTES:
            body = {'interval': 60} if method == 'POST' else None
            cases = [dict(xrw=False), dict(cookie=False), dict(headers={'Host': 'x.trycloudflare.com'}),
                     dict(headers={'CF-Connecting-IP': '1.2.3.4'}), dict(headers={'X-Forwarded-For': '1.2.3.4'}),
                     dict(headers={'Host': 'evil.example'})]
            if method == 'POST':
                cases.append(dict(headers={'Origin': 'http://evil.example'}))
            for kw in cases:
                self.assertEqual(self.call(method, path, body, **kw), (403, {'error': 'FORBIDDEN'}), (path, kw))
        self.assertEqual(self.sched.calls, [])

    def test_wrong_method_or_unknown_path_is_404(self):
        self.mode(True)
        self.assertEqual(self.call('POST', '/api/market_monitor/status', {}), (404, b'not found'))
        self.assertEqual(self.call('GET', '/api/market_monitor/start'), (404, b'not found'))
        self.assertEqual(self.call('GET', '/api/market_monitor/unknown'), (404, b'not found'))

    def test_oversized_config_body_is_refused_unread(self):
        self.mode(True)
        got = self.call('POST', '/api/market_monitor/config', None, headers={'Content-Length': '999999'})
        self.assertEqual(got, (400, {'error': 'INVALID_INPUT'}))
        self.assertEqual(self.sched.calls, [])

    def test_scheduler_factory_is_lazy_and_not_started_at_import(self):
        text = (ROOT / 'app.py').read_text(encoding='utf-8')
        self.assertIn('_monitor_sched = None', text)
        self.assertNotRegex(text, r'(?m)^\s*market_monitor_scheduler\(\)\.start')
        self.assertNotRegex(text, r'(?m)^[^#\n]*\.start\(\)[^\n]*market_monitor')

    def test_offline_export_has_no_monitor_data(self):
        export = (ROOT / 'export_offline.py').read_text(encoding='utf-8')
        self.assertNotIn('market_monitor', export)
        self.assertNotIn('monitor', export.lower().replace('monitoring', ''))
        self.assertIn('/api/offline', personal_mode.hidden_routes())      # 个人模式下离线下载本身也被隐藏
        offline_branch = re.search(r'elif u\.path == "/api/offline":(.*?)\n            elif ', (ROOT / 'app.py').read_text(encoding='utf-8'), re.S)
        self.assertIsNotNone(offline_branch)
        self.assertNotIn('monitor', offline_branch.group(1))


# ---------------- 前端静态检查
JS = (ROOT / 'dashboard' / 'app.js').read_text(encoding='utf-8')
I18N = (ROOT / 'dashboard' / 'i18n.js').read_text(encoding='utf-8')
BLOCK = JS[JS.index('// ================================================================ 实时盯盘'):JS.index('const PAGES = [')]


class FrontendStaticTests(unittest.TestCase):
    def test_no_html_injection_or_local_storage(self):
        for bad in ('innerHTML', 'outerHTML', 'insertAdjacentHTML', 'document.write', 'localStorage', 'sessionStorage',
                    'eval(', 'new Function', 'indexedDB'):
            self.assertNotIn(bad, BLOCK, bad)

    def test_requests_go_through_dashboard_header_helper(self):
        self.assertNotIn('fetch(', BLOCK)
        self.assertRegex(BLOCK, r'hRequest\("/api/market_monitor/quotes"\)')
        self.assertIn('"X-Requested-With": "dashboard"', JS[JS.index('async function hRequest'):JS.index('function hErrorKey')])

    def test_nav_route_and_page_only_in_personal_mode(self):
        self.assertIn('["monitor", "盯盘"]', JS)
        self.assertRegex(JS, r'name !== "monitor"\) \|\| personalMode\(\)')
        self.assertIn('if (!personalMode()) return;\n  monitorBuild();', BLOCK)
        self.assertIn('id="p-monitor"', (ROOT / 'dashboard' / 'index.html').read_text(encoding='utf-8'))
        self.assertIn('"盯盘": "Monitor"', I18N)

    def test_polling_timer_cleared_on_leave(self):
        self.assertIn('clearInterval(MON.timer)', BLOCK)
        self.assertIn('if (p !== "monitor") monitorLeave();', JS)

    def test_i18n_keys_complete_zh_en(self):
        part = I18N[I18N.index('const monitorTranslations = {'):I18N.index('window.MONITOR_I18N')]
        pairs = {k: (z, e) for k, z, e in re.findall(r'"(mon\.[\w.]+)": \["([^"]+)", "([^"]+)"\]', part)}
        self.assertGreater(len(pairs), 60)
        for key, (zh, en) in pairs.items():
            self.assertTrue(zh.strip() and en.strip(), key)
        used = set(re.findall(r'"(mon\.[\w.]+)"', BLOCK))
        used.discard('mon.state'); used.discard('mon.phase'); used.discard('mon.reason'); used.discard('mon.src')
        used -= {'mon.quality', 'mon.alert', 'mon.dir', 'mon.err'}
        self.assertEqual(sorted(k for k in used if k not in pairs), [])
        dynamic = ([f'mon.state.{x}' for x in ('stopped', 'running', 'paused')]
                   + [f'mon.phase.{x}' for x in ('idle', 'waiting_session', 'polling')]
                   + [f'mon.quality.{x}' for x in ('valid', 'stale', 'invalid', 'off_session', 'pending')]
                   + [f'mon.alert.{x}' for x in ('none', 'armed', 'triggered')]
                   + [f'mon.src.{x}' for x in ('holding', 'watchlist', 'signal')]
                   + [f'mon.dir.{x}' for x in ('up', 'down')]
                   + [f'mon.reason.{x}' for x in ('budget_exhausted', 'storage_error', 'service_error', 'quality_error',
                                                  'timeout', 'network_error', 'worker_exit', 'http_status',
                                                  'redirect_refused', 'invalid_record', 'provider_error')]
                   + [f'mon.err.{x}' for x in ('generic', 'FORBIDDEN', 'AUTH', 'UNAVAILABLE', 'NETWORK',
                                               'INVALID_INPUT', 'RATE_LIMITED', 'MONITOR_INTERNAL')])
        self.assertEqual([k for k in dynamic if k not in pairs], [])


class BackendFilesUntouchedTests(unittest.TestCase):
    def test_s3_modules_do_not_import_app_or_network(self):
        for name in ('scheduler', 'watchlist', 'wiring'):
            text = (ROOT / 'src' / 'market_monitor' / f'{name}.py').read_text(encoding='utf-8')
            for bad in ('import socket', 'urllib', 'import requests', 'http.client', 'import app', 'from app'):
                self.assertNotIn(bad, text, (name, bad))


if __name__ == '__main__':
    unittest.main()
