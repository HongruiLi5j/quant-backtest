"""T015 S3 接线测试：wiring 鉴权矩阵、app.py 接入冒烟（AST 提取 Handler）、前端静态检查。
不导入 app、不联网、不读写 data/、不启动真实采集。"""
import ast
from datetime import datetime
import io
import json
from itertools import product
from pathlib import Path
import re
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src import admin, members, personal_mode
from src.us_market_monitor import wiring
from src.us_market_monitor.core import US_EASTERN
from src.us_market_monitor.scheduler import USMonitorScheduler
from src.us_market_monitor.store import Store

GOOD = wiring.Ctx(True, True, True, True)
READS = ('handle_status', 'handle_quotes', 'handle_alerts', 'handle_intraday_events', 'handle_intraday_tracking')
WRITES = ('handle_start', 'handle_stop')


class FakeScheduler:
    """记录调用；wiring 只能通过这几个方法碰调度器。"""
    def __init__(self):
        self.calls, self.interval, self.top_n, self.running = [], 300, 0, False

    def status(self):
        return {'state': 'running' if self.running else 'stopped', 'interval': self.interval, 'top_n': self.top_n,
                'max_watch': 30}

    def quotes(self):
        return [{'code': 'AAPL'}]

    def alerts(self):
        return []

    def intraday_events(self):
        return [{'kind': 'watch', 'code': 'AAPL'}]

    def intraday_tracking(self):
        return {'day': '2026-10-22', 'rows': [], 'summary': {}, 'write_error': False}

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
                got = self.call(name, wiring.Ctx(*flags, origin_ok=False), FakeScheduler())
                if all(flags):
                    self.assertEqual(got[0], 200, name)
                else:
                    self.assertEqual(got, wiring.FORBIDDEN, (name, flags))

    def test_auth_matrix_writes_and_no_side_effect(self):
        for name in WRITES + ('handle_config',):
            for flags in product((True, False), repeat=4):
                sched = FakeScheduler()
                got = self.call(name, wiring.Ctx(*flags), sched, b'{"interval": 120}')
                if all(flags):
                    self.assertEqual(got[0], 200, name)
                else:
                    self.assertEqual(got, wiring.FORBIDDEN, (name, flags))
                    self.assertEqual(sched.calls, [], (name, flags))

    def test_forbidden_is_uniform_and_leaks_nothing(self):
        bodies = {repr(self.call(n, wiring.Ctx(), FakeScheduler(), b'{}')) for n in READS + WRITES + ('handle_config',)}
        self.assertEqual(bodies, {repr(wiring.FORBIDDEN)})
        self.assertEqual(wiring.FORBIDDEN, (403, {'error': 'FORBIDDEN'}))

    def test_truthy_but_not_true_is_rejected(self):
        for bad in (1, 'yes', [1], object()):
            self.assertEqual(wiring.handle_status(wiring.Ctx(bad, True, True, True), FakeScheduler()), wiring.FORBIDDEN)
            self.assertEqual(wiring.handle_quotes(wiring.Ctx(True, bad, True, True), FakeScheduler()), wiring.FORBIDDEN)
            self.assertEqual(wiring.handle_start(wiring.Ctx(True, True, True, bad), FakeScheduler()), wiring.FORBIDDEN)

    def test_a_share_ctx_is_the_same_gate(self):
        from src.market_monitor import wiring as a_share
        self.assertIs(wiring.Ctx, a_share.Ctx)                       # 单一门禁实现，不复制
        self.assertIs(wiring._gate, a_share._gate)

    def test_quotes_include_intraday(self):
        body = wiring.handle_quotes(GOOD, FakeScheduler())[1]
        self.assertEqual(body['intraday']['events'][0]['code'], 'AAPL')
        self.assertEqual(body['intraday']['tracking']['day'], '2026-10-22')
        self.assertEqual(wiring.handle_intraday_events(GOOD, FakeScheduler())[1]['events'][0]['kind'], 'watch')

    def test_config_valid_and_partial(self):
        s = FakeScheduler()
        self.assertEqual(wiring.handle_config(GOOD, s, b'{"interval": 120, "top_n": 10}')[0], 200)
        self.assertEqual((s.interval, s.top_n), (120, 10))
        self.assertEqual(wiring.handle_config(GOOD, s, '{"top_n": 0}')[0], 200)
        self.assertEqual((s.interval, s.top_n), (120, 0))              # 未给的字段保持不变
        self.assertEqual(wiring.handle_config(GOOD, s, b'{"interval": 60, "top_n": 30}')[0], 200)

    def test_config_rejects_bad_input_without_changes(self):
        bad = [b'', b'[]', b'{}', b'null', b'{"interval": 59}', b'{"interval": 3601}', b'{"interval": 60.5}',
               b'{"interval": true}', b'{"interval": "60"}', b'{"top_n": -1}', b'{"top_n": 31}', b'{"top_n": 1.0}',
               b'{"top_n": null}', b'{"interval": NaN}', b'{"interval": Infinity}', b'{"interval": 60, "interval": 61}',
               b'{"interval": 60, "request_budget": 600}', b'{"min_interval": 1}', b'{"max_watch": 999}',
               b'{"multi_code": true}', b'\xff\xfe', b'x' * 5000, b'{"interval":' + b'[' * 5000 + b']' * 5000 + b'}',
               12345, None]
        for body in bad:
            s = FakeScheduler()
            got = wiring.handle_config(GOOD, s, body)
            self.assertEqual(got, wiring.INVALID, body)
            self.assertEqual(s.calls, [], body)

    def test_rate_and_budget_not_reachable_via_config(self):
        s = FakeScheduler()
        for key in ('request_budget', 'min_interval', 'symbol_gap', 'max_watch', 'timeout', 'thresholds'):
            self.assertEqual(wiring.handle_config(GOOD, s, json.dumps({key: 1}).encode()), wiring.INVALID, key)
        self.assertEqual(s.calls, [])

    def test_start_stop_idempotent_with_real_scheduler(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        s = USMonitorScheduler(store=Store(root=Path(tmp.name).resolve() / 's'), request=Mock(side_effect=AssertionError),
                               build_fn=lambda n: {'codes': [], 'sources': {}, 'targets': {}, 'signals': {}, 'truncated': False},
                               now=lambda: datetime(2026, 10, 24, 10, 0, tzinfo=US_EASTERN),
                               wait=lambda ev, sec: __import__('time').sleep(0.001))
        self.addCleanup(s.stop)
        self.assertEqual(wiring.handle_stop(GOOD, s)[1]['status']['state'], 'stopped')   # 未启动时停止也成功
        first, second = wiring.handle_start(GOOD, s), wiring.handle_start(GOOD, s)
        self.assertEqual((first[1]['started'], second[1]['started']), (True, False))
        self.assertEqual(second[1]['status']['state'], 'running')
        thread = s._thread
        self.assertEqual(wiring.handle_stop(GOOD, s)[1]['status']['state'], 'stopped')
        self.assertEqual(wiring.handle_stop(GOOD, s)[1]['status']['state'], 'stopped')
        self.assertFalse(thread.is_alive())

    def test_real_scheduler_config_bounds(self):
        s = USMonitorScheduler()
        self.assertEqual(wiring.handle_config(GOOD, s, b'{"top_n": 31}'), wiring.INVALID)
        self.assertEqual(wiring.handle_config(GOOD, s, b'{"interval": 59}'), wiring.INVALID)
        self.assertEqual(wiring.handle_config(GOOD, s, b'{"interval": 60, "top_n": 30}')[0], 200)
        self.assertEqual((s.interval, s.top_n), (60, 30))


# ---------------- app.py 接入冒烟（不导入 app，只用 AST 取出 Handler 的相关方法）
APP_TREE = ast.parse((ROOT / 'app.py').read_text(encoding='utf-8'))
HANDLER = next(n for n in APP_TREE.body if isinstance(n, ast.ClassDef) and n.name == 'Handler')
METHODS = {n.name: n for n in HANDLER.body if isinstance(n, ast.FunctionDef)}
ASSIGNS = [n for n in HANDLER.body if isinstance(n, ast.Assign)]
HOST, TOKEN = '127.0.0.1:8765', 'synthetic-token'
ROUTES = (('GET', '/api/us_market_monitor/status'), ('GET', '/api/us_market_monitor/quotes'),
          ('GET', '/api/us_market_monitor/alerts'), ('POST', '/api/us_market_monitor/start'),
          ('POST', '/api/us_market_monitor/stop'), ('POST', '/api/us_market_monitor/config'))


def build_class(us_scheduler, a_scheduler):
    names = ('do_GET', 'do_POST', '_personal_blocked', '_market_monitor', '_us_market_monitor', '_authed', '_remote', '_external',
             '_owner_cfg', '_owner', '_local_or_owner', '_origin_ok', '_https')
    cls = ast.ClassDef(name='Handler', bases=[], keywords=[], decorator_list=[],
                       body=ASSIGNS + [METHODS[n] for n in names])
    ns = dict(personal_mode=personal_mode, urlparse=urlparse, parse_qs=parse_qs, json=json, admin=admin,
              members=members, ALLOWED_HOSTS={HOST, 'localhost:8765'}, owner_access=None, ROOT=None, COOKIE='qb', MEMBER_COOKIE='qm',
              TOKEN=TOKEN, hmac=__import__('hmac'), SimpleCookie=__import__('http.cookies', fromlist=['x']).SimpleCookie,
              market_monitor_scheduler=lambda: a_scheduler, us_market_monitor_scheduler=lambda: us_scheduler,
              meta=lambda: {}, _alive={}, _bad_login=[], time=SimpleNamespace(time=lambda: 0))
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
        self.sched, self.a_sched = FakeScheduler(), FakeScheduler()
        self.cls = build_class(self.sched, self.a_sched)

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
        for name in ('_admin_get', '_admin_post', '_ai', '_holdings', '_market_monitor_unused', '_share_login',
                     '_member_get', '_member_post', '_page', '_static', '_post_engine'):
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
        self.assertEqual(self.call('GET', '/api/us_market_monitor/status')[1]['status']['state'], 'stopped')
        self.assertEqual(self.sched.calls, [])                            # 查询状态不启动
        self.assertEqual(self.call('POST', '/api/us_market_monitor/start', {})[1]['started'], True)
        self.assertEqual(self.call('POST', '/api/us_market_monitor/start', {})[1]['started'], False)
        quotes = self.call('GET', '/api/us_market_monitor/quotes')[1]
        self.assertEqual(quotes['quotes'], [{'code': 'AAPL'}])
        self.assertIn('intraday', quotes)
        self.assertEqual(self.call('GET', '/api/us_market_monitor/alerts')[1], {'ok': True, 'alerts': []})
        self.assertEqual(self.call('POST', '/api/us_market_monitor/config', {'interval': 90})[0], 200)
        self.assertEqual(self.sched.interval, 90)
        self.assertEqual(self.call('POST', '/api/us_market_monitor/stop', {})[1]['status']['state'], 'stopped')
        self.assertEqual(self.a_sched.calls, [])                          # 绝不碰 A 股调度器

    def test_a_share_routes_never_reach_us_scheduler(self):
        self.mode(True)
        self.assertEqual(self.call('POST', '/api/market_monitor/start', {})[1]['started'], True)
        self.assertEqual(self.a_sched.calls, ['start'])
        self.assertEqual(self.sched.calls, [])

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
        self.assertEqual(self.call('POST', '/api/us_market_monitor/status', {}), (404, b'not found'))
        self.assertEqual(self.call('GET', '/api/us_market_monitor/start'), (404, b'not found'))
        self.assertEqual(self.call('GET', '/api/us_market_monitor/unknown'), (404, b'not found'))
        self.assertEqual(self.call('GET', '/api/us_market_monitor/../market_monitor/status'), (404, b'not found'))

    def test_oversized_config_body_is_refused_unread(self):
        self.mode(True)
        got = self.call('POST', '/api/us_market_monitor/config', None, headers={'Content-Length': '999999'})
        self.assertEqual(got, (400, {'error': 'INVALID_INPUT'}))
        self.assertEqual(self.sched.calls, [])

    def test_internal_error_returns_fixed_code(self):
        self.mode(True)
        self.sched.status = Mock(side_effect=RuntimeError('PRIVATE'))
        self.assertEqual(self.call('GET', '/api/us_market_monitor/status'), (500, {'error': 'MONITOR_INTERNAL'}))

    def test_scheduler_factory_is_lazy_and_not_started_at_import(self):
        text = (ROOT / 'app.py').read_text(encoding='utf-8')
        self.assertIn('_us_monitor_sched = None', text)
        self.assertIn('from src.us_market_monitor.scheduler import USMonitorScheduler', text)
        self.assertNotRegex(text, r'(?m)^\s*us_market_monitor_scheduler\(\)\.start')
        self.assertNotRegex(text, r'(?m)^[^#\n]*\.start\(\)[^\n]*us_market_monitor')
        # 工厂函数里的导入是延迟导入（函数体内），模块顶层不导入美股包
        top_imports = [n for n in APP_TREE.body if isinstance(n, (ast.Import, ast.ImportFrom))]
        self.assertFalse([n for n in top_imports if 'us_market_monitor' in ast.dump(n)])

    def test_offline_export_and_share_have_no_us_monitor_data(self):
        export = (ROOT / 'export_offline.py').read_text(encoding='utf-8')
        self.assertNotIn('us_market_monitor', export)
        offline_branch = re.search(r'elif u\.path == "/api/offline":(.*?)\n            elif ', (ROOT / 'app.py').read_text(encoding='utf-8'), re.S)
        self.assertIsNotNone(offline_branch)
        self.assertNotIn('us_market_monitor', offline_branch.group(1))
        # 分享访客：_remote() 为真时 trusted_local=False → 统一 403（鉴权矩阵测试已覆盖 Host/隧道头），分享/代理拒绝
        us_src = ast.get_source_segment((ROOT / 'app.py').read_text(encoding='utf-8'), METHODS['_us_market_monitor'])
        self.assertIn('and not self._remote()', us_src)


# ---------------- 前端静态检查
JS = (ROOT / 'dashboard' / 'app.js').read_text(encoding='utf-8')
I18N = (ROOT / 'dashboard' / 'i18n.js').read_text(encoding='utf-8')
HTML = (ROOT / 'dashboard' / 'index.html').read_text(encoding='utf-8')
CSS = (ROOT / 'dashboard' / 'terminal.css').read_text(encoding='utf-8')
BLOCK = JS[JS.index('// ================================================================ 美股盯盘（T015 S3）'):
           JS.index('// ================================================================ 实时盯盘（T006 S3）')]
PAIRS = {k: (z, e) for k, z, e in re.findall(r'"((?:us)?mon\.[\w.]+)": \["([^"]+)", "([^"]+)"\]',
                                             I18N[I18N.index('const monitorTranslations = {'):I18N.index('window.MONITOR_I18N')])}


class FrontendStaticTests(unittest.TestCase):
    def test_no_html_injection_or_storage_or_a_share_endpoints(self):
        for bad in ('innerHTML', 'outerHTML', 'insertAdjacentHTML', 'document.write', 'localStorage', 'sessionStorage',
                    'eval(', 'new Function', 'indexedDB', 'fetch(', '/api/market_monitor/', 'XMLHttpRequest'):
            self.assertNotIn(bad, BLOCK, bad)

    def test_requests_go_through_dashboard_header_helper(self):
        paths = set(re.findall(r'"(/api/[\w/]+)"', BLOCK))
        self.assertEqual(paths, {'/api/us_market_monitor/quotes', '/api/us_market_monitor/alerts',
                                 '/api/us_market_monitor/start', '/api/us_market_monitor/stop',
                                 '/api/us_market_monitor/config'})
        self.assertIn('"X-Requested-With": "dashboard"', JS[JS.index('async function hRequest'):JS.index('function hErrorKey')])

    def test_eighth_nav_item_route_and_page_only_in_personal_mode(self):
        nav = re.search(r'const PERSONAL_NAV = (.*);', JS).group(1)
        names = re.findall(r'\["(\w+)",', nav)
        self.assertEqual(len(names), 8)
        self.assertEqual(names[-1], 'usmonitor')
        self.assertIn('["usmonitor", "美股盯盘"]', JS)
        self.assertRegex(JS, r'name !== "usmonitor" \|\| personalMode\(\)')
        self.assertRegex(JS, r'name !== "monitor"\) \|\| personalMode\(\)')            # A 股盯盘的门禁不变
        self.assertIn('if (!personalMode()) return;\n  usmBuild();', BLOCK)
        self.assertIn('id="p-usmonitor"', HTML)
        self.assertIn('"美股盯盘": "US Monitor"', I18N)
        self.assertIn('usmonitor: loadUsMonitor', JS)
        self.assertRegex(JS, r'const PAGES = \[[^\]]*"usmonitor"')

    def test_polling_timer_cleared_on_leave_and_state_wiped(self):
        self.assertIn('clearInterval(USM.timer)', BLOCK)
        self.assertIn('if (p !== "usmonitor") usmLeave();', JS)
        self.assertIn('USM.quotes = []', BLOCK[BLOCK.index('function usmLeave'):])
        self.assertIn('setInterval(usmRefresh, 10000)', BLOCK)

    def test_i18n_keys_complete_zh_en(self):
        for key, (zh, en) in PAIRS.items():
            self.assertTrue(zh.strip() and en.strip(), key)
        self.assertGreater(len([k for k in PAIRS if k.startswith('usmon.')]), 30)
        used = set(re.findall(r'"((?:us)?mon\.[\w.]+)"', BLOCK))
        prefixes = {'mon.state', 'mon.src', 'mon.alert', 'mon.dir', 'usmon.err', 'usmon.phase', 'usmon.reason', 'usmon.quality'}
        self.assertEqual(sorted(k for k in used - prefixes if k not in PAIRS), [])
        dynamic = ([f'usmon.phase.{x}' for x in ('idle', 'waiting_session', 'polling', 'cooling_down')]
                   + [f'usmon.quality.{x}' for x in ('valid', 'stale', 'invalid', 'off_session', 'after_hours', 'pending')]
                   + [f'usmon.reason.{x}' for x in ('budget_exhausted', 'storage_error', 'service_error', 'quality_error',
                                                    'timeout', 'rate_limited', 'network', 'worker_exit', 'empty',
                                                    'symbol_missing', 'bad_response', 'invalid_record', 'provider_error',
                                                    'yfinance_not_installed')]
                   + [f'mon.state.{x}' for x in ('stopped', 'running', 'paused')]
                   + [f'mon.err.{x}' for x in ('generic', 'FORBIDDEN', 'AUTH', 'UNAVAILABLE', 'NETWORK', 'RATE_LIMITED',
                                               'MONITOR_INTERNAL')]
                   + ['usmon.err.INVALID_INPUT'])
        self.assertEqual([k for k in dynamic if k not in PAIRS], [])

    def test_every_reason_the_scheduler_can_emit_has_text(self):
        from src.us_market_monitor.service import REASONS
        emitted = (REASONS - {'minimum_interval', 'not_started', 'already_started', 'cancelled', 'invalid_symbols',
                              'budget_exceeded'}) | {'budget_exhausted', 'storage_error', 'service_error', 'quality_error'}
        self.assertEqual(sorted(r for r in emitted if f'usmon.reason.{r}' not in PAIRS), [])

    def test_ids_do_not_collide_with_a_share_page(self):
        ids = set(re.findall(r'"(usm[A-Z]\w+)"', BLOCK))
        self.assertTrue(ids)
        a_share = JS[JS.index('// ================================================================ 实时盯盘（T006 S3）'):JS.index('const PAGES = [')]
        for ident in ids:
            self.assertNotIn(f'"{ident}"', a_share)
        self.assertIn('#p-usmonitor', CSS)

    def test_a_share_nav_label_translation_present(self):
        self.assertIn('"盯盘": "Monitor"', I18N)


class BackendFilesUntouchedTests(unittest.TestCase):
    def test_wiring_does_not_import_app_or_network(self):
        text = (ROOT / 'src' / 'us_market_monitor' / 'wiring.py').read_text(encoding='utf-8')
        for bad in ('import socket', 'urllib', 'import requests', 'http.client', 'import app', 'from app'):
            self.assertNotIn(bad, text, bad)


if __name__ == '__main__':
    unittest.main()
