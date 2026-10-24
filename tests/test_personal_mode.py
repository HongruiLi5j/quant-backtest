"""仅执行 AST 提取的 Handler；不导入 app，不接触真实凭据或行情。"""
import ast
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import personal_mode

ROOT = Path(__file__).resolve().parent.parent
SOURCE = (ROOT / "app.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)
HANDLER = next(n for n in TREE.body if isinstance(n, ast.ClassDef) and n.name == "Handler")
METHODS = {n.name: n for n in HANDLER.body if isinstance(n, ast.FunctionDef)}


def extracted(names, **env):
    namespace = dict(personal_mode=personal_mode, urlparse=urlparse, parse_qs=parse_qs,
                     json=json, re=re, hashlib=hashlib, SimpleCookie=SimpleCookie)
    namespace.update(env)
    cls = ast.ClassDef(name="Handler", bases=[], keywords=[],
                       body=[METHODS[n] for n in names], decorator_list=[])
    exec(compile(ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[])),
                 "<isolated-app-handler>", "exec"), namespace)
    return namespace["Handler"]


HIDDEN = (
    "/member", "/member/", "/member.js", "/api/member/rank", "/api/member/login",
    "/api/member/order", "/admin", "/admin.js", "/admin/", "/api/admin/action",
    "/api/sim", "/api/share", "/api/share_visits", "/__share_login",
    "/api/eng/job", "/api/eng/meta", "/api/code/save", "/api/code/list",
    "/api/ai/status", "/api/ai/translate", "/api/offline", "/share_gate.js",
    "/share_gate.html", "/engines.js", "/ai_translator.js",
)


class PersonalModeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = Path(self.tmp.name) / "personal_mode.json"
        self.patcher = patch.object(personal_mode, "CONFIG", self.config)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def enabled(self, value=True):
        self.config.write_text(json.dumps({"enabled": value}), encoding="utf-8")

    def handler(self, method, path, authenticated=True, external=False, guard=True):
        cls = extracted(["do_GET", "do_POST", "_personal_blocked"],
                        meta=lambda: {"markets": ["synthetic"]},
                        MARKETS={"cn": {}}, time=SimpleNamespace(time=lambda: 0),
                        _bad_login=[], _alive={}, refresh_status=lambda: {"ok": True},
                        search=lambda *a: {"search": True}, scan_query=lambda q: {"scan": True},
                        trade_list=SimpleNamespace(dates=lambda: ["synthetic"],
                                                   load=lambda *a: {"signals": []}, PublishError=ValueError),
                        datasource=SimpleNamespace(load_watchlist=lambda m: ["synthetic"]),
                        _pending=set(), start_refresh=lambda m: True)
        h = cls()
        h.command, h.path, h.headers = method, path, {"X-Requested-With": "dashboard"}
        for name in ("_send", "_json", "_admin_get", "_admin_post", "_ai", "_share_login",
                     "_member_get", "_member_post", "_page", "_static", "_post_engine"):
            setattr(h, name, Mock())
        h._guard = Mock(return_value=guard)
        h._authed = Mock(return_value=authenticated)
        h._external = Mock(return_value=external)
        h._remote = Mock(return_value=False)
        h._share_gate = Mock(return_value=False)
        h._backtest = Mock(return_value={"backtest": True})
        h._table = Mock(return_value={"table": True})
        getattr(h, "do_" + method)()
        return h

    def test_config_defaults_strict_types_and_live_reload(self):
        self.assertFalse(personal_mode.is_enabled())
        for raw in ('{', '[]', 'null', 'true', '{"enabled": 1}', '{"enabled": "true"}',
                    '{"enabled": false}', '{}'):
            self.config.write_text(raw)
            self.assertFalse(personal_mode.is_enabled(), raw)
        self.config.write_bytes(b'\xff')
        self.assertFalse(personal_mode.is_enabled())
        with patch.object(Path, "read_text", side_effect=PermissionError):
            self.assertFalse(personal_mode.is_enabled())
        self.enabled()
        self.assertTrue(personal_mode.is_enabled())
        self.enabled(False)
        self.assertFalse(personal_mode.is_enabled())
        self.assertEqual(personal_mode.CONFIG.name, "personal_mode.json")

    def test_hidden_matrix_stops_before_auth_handlers_or_body(self):
        self.enabled()
        for method in ("GET", "POST"):
            for path in HIDDEN:
                for auth in (False, True):
                    for external in (False, True):
                        with self.subTest(method=method, path=path, auth=auth, external=external):
                            h = self.handler(method, path + "?ignored=1", auth, external)
                            h._send.assert_called_once_with(404, b"not found", "text/plain")
                            for name in ("_authed", "_share_gate", "_admin_get", "_admin_post", "_ai",
                                         "_share_login", "_member_get", "_member_post", "_post_engine"):
                                getattr(h, name).assert_not_called()

    def test_off_or_corrupt_continues_original_dispatch(self):
        for raw in (None, '{"enabled": false}', '{'):
            if raw is not None:
                self.config.write_text(raw)
            for method in ("GET", "POST"):
                for path in HIDDEN:
                    h = self.handler(method, path, authenticated=False)
                    self.assertFalse(h._personal_mode)
                    if path in ("/admin", "/admin.js") and method == "GET" or path.startswith("/api/admin/"):
                        getattr(h, "_admin_" + method.lower()).assert_called_once()
                    elif path.startswith("/api/ai/"):
                        h._ai.assert_called_once()
                    elif path == "/__share_login" and method == "POST":
                        h._share_login.assert_called_once()
                    else:
                        self.assertEqual(h._send.call_args.args[0], 401)

    def test_mode_roundtrip_restores_dispatch_without_rewriting_config(self):
        # 同一进程连续开/关/再开；坏配置也恢复原分派，不重写配置。
        for raw, mode in (('{"enabled": true}', True),
                          ('{"enabled": false}', False), ('{', False),
                          ('{"enabled": true}', True)):
            self.config.write_text(raw, encoding="utf-8")
            for method in ("GET", "POST"):
                for path, target in (("/api/ai/status", "_ai"),
                                     ("/api/admin/action", "_admin_" + method.lower()),
                                     ("/api/member/order", "_member_" + method.lower())):
                    with self.subTest(raw=raw, method=method, path=path):
                        h = self.handler(method, path)
                        self.assertIs(h._personal_mode, mode)
                        if mode:
                            h._send.assert_called_once_with(404, b"not found", "text/plain")
                            getattr(h, target).assert_not_called()
                        else:
                            h._send.assert_not_called()
                            getattr(h, target).assert_called_once()
            h = self.handler("GET", "/api/meta")
            self.assertIs(h._json.call_args.args[0]["personal_mode"], mode)
            self.assertEqual(self.config.read_text(encoding="utf-8"), raw)

    def test_retained_endpoints_and_meta(self):
        for enabled in (False, True):
            self.enabled(enabled)
            for path in ("/api/meta", "/api/backtest", "/api/status", "/api/search", "/api/scan",
                         "/api/table", "/api/watchlist", "/api/trade_list", "/api/trade_list_dates"):
                h = self.handler("GET", path)
                h._send.assert_not_called()
                h._json.assert_called_once()
                if path == "/api/meta":
                    self.assertEqual(h._json.call_args.args[0], {"markets": ["synthetic"], "personal_mode": enabled})
            h = self.handler("POST", "/api/refresh")
            h._json.assert_called_once_with({"started": True})

    def test_exact_route_boundaries_and_guard_first(self):
        self.enabled()
        for path in ("/api/backtest", "/api/trade_list_export", "/api/shared", "/api/administer",
                     "/api/codebook", "/membership", "/api/air", "/api/engine"):
            h = self.handler("GET", path, authenticated=False)
            self.assertEqual(h._send.call_args.args[0], 401)
        h = self.handler("GET", "/api/ai/status", guard=False)
        self.assertFalse(hasattr(h, "_personal_mode"))
        h._send.assert_not_called()

    def test_member_readonly_exceptions_keep_auth_and_local_boundary(self):
        self.enabled()
        for path in ("/api/member/me", "/api/member/portfolio"):
            for method in ("GET", "POST"):
                for external in (False, True):
                    h = self.handler(method, path, external=external)
                    if method == "GET" and not external:
                        h._authed.assert_called_once()
                        h._member_get.assert_called_once()
                    else:
                        self.assertEqual(h._send.call_args.args[0], 404)
            h = self.handler("GET", path, authenticated=False)
            self.assertEqual(h._send.call_args.args[0], 401)
            h._member_get.assert_not_called()

    def test_member_session_still_required(self):
        cls = extracted(["_member_get"], members=SimpleNamespace(MemberError=ValueError))
        for path, code in (("/api/member/me", 200), ("/api/member/portfolio", 401)):
            h = cls()
            h._member_user, h._json = Mock(return_value=None), Mock()
            h._member_get(urlparse(path), {})
            self.assertEqual(h._json.call_args.args[1] if len(h._json.call_args.args) > 1 else 200, code)

    def test_real_local_classifier_rejects_proxy_headers(self):
        tree = ast.parse((ROOT / "src/admin.py").read_text())
        selected = [n for n in tree.body if
                    isinstance(n, ast.FunctionDef) and n.name == "is_local_request" or
                    isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "PROXY_HEADERS"
                                                    for t in n.targets)]
        env = {}
        exec(compile(ast.Module(body=selected, type_ignores=[]), "<local-classifier>", "exec"), env)
        admin = SimpleNamespace(is_local_request=env["is_local_request"])
        cls = extracted(["_external", "_remote"], admin=admin, ALLOWED_HOSTS={"localhost:8765"})
        h = cls()
        h.headers = {"Host": "localhost:8765"}
        self.assertFalse(h._external())
        for header in env["PROXY_HEADERS"]:
            h.headers = {"Host": "localhost:8765", header.upper(): "synthetic"}
            self.assertTrue(h._external(), header)
        h.headers = {"Host": "synthetic.trycloudflare.com"}
        self.assertTrue(h._external())

    def test_share_gate_has_no_password_ui_in_personal_mode(self):
        for mode in (False, True):
            for configured in (False, True):
                for valid in (False, True):
                    sp = SimpleNamespace(is_set=lambda: configured, check_cookie=lambda v: valid)
                    h = extracted(["_share_gate"], sharepass=sp)()
                    h._personal_mode, h.headers, h.command = mode, {}, "GET"
                    h._external, h._send = Mock(return_value=True), Mock()
                    # 旧模式配置口令且无会话时会读口令页；此项另用合成文件验证。
                    with tempfile.TemporaryDirectory(dir=self.tmp.name) as directory:
                        dash = Path(directory)
                        (dash / "share_gate.html").write_bytes(b"synthetic gate")
                        h._share_gate.__func__.__globals__["DASH"] = dash
                        blocked = h._share_gate(urlparse("/"))
                    self.assertEqual(blocked, (not valid if configured else mode))
                    if blocked:
                        self.assertEqual(h._send.call_args.args[0], 404 if mode else 401)

    def test_final_html_etag_gzip_and_cross_mode_304(self):
        dash = Path(self.tmp.name)
        raw = ('<script src="/engines.js?v=old"></script>'
               '<script src="/ai_translator.js"></script>'
               '<script src="/app.js?v=old"></script>' + 'x' * 1500)
        (dash / "index.html").write_text(raw)
        cls = extracted(["_page", "_send"], DASH=dash,
                        _asset=lambda n: (b"synthetic", '"asset-hash"'),
                        COMPRESSIBLE={"text/html"}, _gzip_cached=lambda b, e: gzip.compress(b))
        etags = []
        for mode in (False, True, False):
            h = cls()
            h._personal_mode, h.command, h.path = mode, "GET", "/"
            h.headers = {"Accept-Encoding": "gzip"}
            if etags:
                h.headers["If-None-Match"] = etags[-1]
            h._remote = lambda: False
            h.send_response, h.end_headers = Mock(), Mock()
            headers = {}
            h.send_header = lambda k, v: headers.update({k: v})
            h.wfile = io.BytesIO()
            h._page("index.html")
            h.send_response.assert_called_once_with(200)
            body = gzip.decompress(h.wfile.getvalue()).decode()
            self.assertEqual("/ai_translator.js" in body, not mode)
            self.assertEqual("/engines.js" in body, not mode)
            self.assertIn('/app.js?v=asset-hash', body)
            etags.append(headers["ETag"])
            h.headers["If-None-Match"] = headers["ETag"]
            h.send_response.reset_mock()
            h._page("index.html")
            h.send_response.assert_called_once_with(304)
        self.assertEqual(etags[0], etags[2])
        self.assertNotEqual(etags[0], etags[1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
