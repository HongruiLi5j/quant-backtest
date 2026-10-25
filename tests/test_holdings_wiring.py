"""持仓接口接入 app.py 的测试(T013 · S3):只用 AST 提取的 Handler、合成数据和临时目录。
不导入 app,不读写真实 data/,不联网。"""
import ast
import hashlib
import io
import json
import re
import sys
import tempfile
import unittest
from http.cookies import SimpleCookie
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import admin, members, personal_mode
from src.holdings import api as hapi
from src.holdings.store import HoldingsStore

ROOT = Path(__file__).resolve().parent.parent
TREE = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
HANDLER = next(n for n in TREE.body if isinstance(n, ast.ClassDef) and n.name == "Handler")
METHODS = {n.name: n for n in HANDLER.body if isinstance(n, ast.FunctionDef)}
ASSIGNS = [n for n in HANDLER.body if isinstance(n, ast.Assign)]

HOST = "127.0.0.1:8765"
ALLOWED = {HOST, "localhost:8765"}
TOKEN = "synthetic-token"
ROUTES = (("GET", "/api/holdings"), ("POST", "/api/holdings/add"),
          ("POST", "/api/holdings/void"), ("POST", "/api/holdings/capital"))
GOOD = {"trade_date": "2026-10-20", "code": "600000", "side": "buy", "qty": 100, "price": "10.00", "fee": "5"}


def build_class():
    cls = ast.ClassDef(name="Handler", bases=[], keywords=[], decorator_list=[],
                       body=ASSIGNS + [METHODS[n] for n in
                                       ("do_GET", "do_POST", "_personal_blocked", "_maintenance_blocked", "_holdings",
                                        "_holdings_price", "_authed", "_remote", "_external",
                                        "_owner_cfg", "_owner", "_local_or_owner", "_origin_ok", "_https")])
    ns = dict(personal_mode=personal_mode, urlparse=urlparse, parse_qs=parse_qs, json=json, re=re,
              hashlib=hashlib, SimpleCookie=SimpleCookie, admin=admin, members=members,
              ALLOWED_HOSTS=ALLOWED, owner_access=None, ROOT=None, COOKIE="qb", MEMBER_COOKIE="qm", TOKEN=TOKEN, hmac=__import__("hmac"),
              MARKETS={"cn": {"tz": "Asia/Shanghai", "close_ready": "15:30"}},
              display_price=SimpleNamespace(cached_scale=lambda m, c, df: (1.0, "无缓存")),      # 只读缓存、不联网的替身
              datasource=SimpleNamespace(stock_name=lambda c: "合成", load_cached=lambda m, c: None,
                                         drop_incomplete_bar=lambda df, tz, cr: df),
              meta=lambda: {}, _alive={}, _bad_login=[], time=SimpleNamespace(time=lambda: 0))
    exec(compile(ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[])), "<handler>", "exec"), ns)
    return ns["Handler"]


class WiringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.config = self.dir / "personal_mode.json"
        self.db = self.dir / "holdings.db"
        for p in (patch.object(personal_mode, "CONFIG", self.config),
                  patch.object(hapi, "HoldingsStore", lambda *a: HoldingsStore(self.db)),
                  patch.object(members, "hit", lambda *a: False)):
            p.start()
            self.addCleanup(p.stop)
        self.cls = build_class()

    def mode(self, on):
        self.config.write_text(json.dumps({"enabled": on}), encoding="utf-8")

    def call(self, method, path, body=None, headers=None, cookie=True, xrw=True, share=False, size=None):
        h = self.cls()
        raw = json.dumps(body).encode() if body is not None else b""
        hdr = {"Host": HOST, "Content-Length": str(len(raw) if size is None else size)}
        if cookie:
            hdr["Cookie"] = f"qb={TOKEN}"
        if xrw:
            hdr["X-Requested-With"] = "dashboard"
        hdr.update(headers or {})
        h.command, h.path, h.headers, h.rfile = method, path, hdr, io.BytesIO(raw)
        h._guard = Mock(return_value=True)
        h.sent = []
        h._send = lambda code, data, ctype, *a, **k: h.sent.append((code, data))
        h._json = lambda obj, code=200: h.sent.append((code, obj))
        for name in ("_admin_get", "_admin_post", "_ai", "_share_login", "_member_get", "_member_post",
                     "_page", "_static", "_post_engine"):
            setattr(h, name, Mock())
        h._share_gate = Mock(return_value=False)
        getattr(h, "do_" + method)()
        return h

    def status(self, *a, **k):
        h = self.call(*a, **k)
        return h.sent[-1] if h.sent else None

    # ---------- 个人模式关闭
    def test_off_routes_match_unknown_path(self):
        for state in (None, False):
            if state is not None:
                self.mode(state)
            for method, path in ROUTES:
                got = self.status(method, path, GOOD if method == "POST" else None)
                ref = self.status(method, path + "_unknown_x", GOOD if method == "POST" else None)
                self.assertEqual(got, ref, (state, method, path))
        self.assertFalse(self.db.exists())

    # ---------- 个人模式开启
    def test_on_full_roundtrip_and_file_perms(self):
        self.mode(True)
        self.assertFalse(self.db.exists())  # 构造/读之前不建库
        code, obj = self.status("GET", "/api/holdings")
        self.assertEqual(code, 200)
        self.assertTrue(obj["ok"])
        self.assertFalse(self.db.exists())  # 读空库不建文件
        code, obj = self.status("POST", "/api/holdings/add", GOOD)
        self.assertEqual(code, 200, obj)
        self.assertEqual(oct(self.db.stat().st_mode & 0o777), "0o600")
        code, obj = self.status("GET", "/api/holdings")
        self.assertEqual(obj["positions"][0]["shares"], 100)
        self.assertEqual(self.status("POST", "/api/holdings/capital", {"capital": 100000})[0], 200)
        tid = obj["trades"][0]["id"]
        self.assertEqual(self.status("POST", "/api/holdings/void", {"id": tid})[0], 200)
        self.assertEqual(self.status("GET", "/api/holdings")[1]["positions"], [])

    def test_header_cookie_origin_matrix(self):
        self.mode(True)
        self.assertEqual(self.status("GET", "/api/holdings", xrw=False)[0], 403)
        self.assertEqual(self.status("POST", "/api/holdings/add", GOOD, xrw=False)[0], 403)
        self.assertEqual(self.status("GET", "/api/holdings", cookie=False)[0], 401)
        self.assertEqual(self.status("POST", "/api/holdings/add", GOOD, cookie=False)[0], 401)
        self.assertEqual(self.status("POST", "/api/holdings/add", GOOD,
                                     headers={"Origin": "http://evil.example"})[0], 403)
        self.assertFalse(self.db.exists())

    def test_external_share_and_proxy_rejected(self):
        self.mode(True)
        cases = [{"Host": "x.trycloudflare.com"}, {"CF-Connecting-IP": "1.2.3.4"},
                 {"X-Forwarded-For": "1.2.3.4"}, {"Host": "evil.example"}]
        for hdr in cases:
            for method, path in ROUTES:
                code, obj = self.status(method, path, GOOD if method == "POST" else None, headers=hdr)
                self.assertEqual((code, obj), (404, b"not found"), (hdr, path))
        # 分享口令 cookie 不能替代本机身份
        code, obj = self.status("GET", "/api/holdings", headers={"Host": "x.trycloudflare.com",
                                                                  "Cookie": f"qb={TOKEN}; qb_share=ok"})
        self.assertEqual(code, 404)
        self.assertFalse(self.db.exists())

    def test_wrong_method_and_other_routes_untouched(self):
        self.mode(True)
        h = self.call("POST", "/api/holdings", GOOD)
        self.assertEqual(h.sent[-1][0], 404)
        h = self.call("GET", "/api/holdings/add")
        self.assertEqual(h.sent[-1][0], 404)
        self.assertFalse(self.db.exists())

    def test_errors_fixed_and_body_limit(self):
        self.mode(True)
        code, obj = self.status("POST", "/api/holdings/add", {**GOOD, "qty": "x9999"})
        self.assertEqual(code, 400)
        self.assertNotIn("9999", json.dumps(obj))
        code, obj = self.status("POST", "/api/holdings/add", GOOD, size=10 ** 6)
        self.assertEqual(code, 413)
        with patch.object(hapi, "handle_get", side_effect=RuntimeError("secret-qty-12345")):
            code, obj = self.status("GET", "/api/holdings")
        self.assertEqual(code, 500)
        self.assertNotIn("12345", json.dumps(obj))

    def test_rate_limit_only_after_auth(self):
        self.mode(True)
        with patch.object(members, "hit", lambda *a: True):
            self.assertEqual(self.status("GET", "/api/holdings")[0], 429)
            self.assertEqual(self.status("GET", "/api/holdings", cookie=False)[0], 401)
            self.assertEqual(self.status("GET", "/api/holdings", headers={"Host": "x.trycloudflare.com"})[0], 404)

    # ---------- 持仓不外泄的静态检查
    def test_no_leak_to_offline_backup_or_logs(self):
        self.assertNotIn("holdings", (ROOT / "export_offline.py").read_text(encoding="utf-8").lower())
        names = " ".join(map(str, list(admin.BACKUP_FILES) + list(admin.BACKUP_DIRS)))
        self.assertNotIn("holdings", names)
        self.assertIn("/api/offline", personal_mode.hidden_routes())
        for name in ("_holdings", "_holdings_price"):
            src = ast.get_source_segment((ROOT / "app.py").read_text(encoding="utf-8"), METHODS[name])
            for bad in ("print(", "logging", "admin.audit", "_log_visit", "notify"):
                self.assertNotIn(bad, src, (name, bad))
            self.assertNotIn("{e}", src)
            self.assertNotIn("str(e)", src)

    def test_personal_mode_still_hides_offline(self):
        self.mode(True)
        self.assertEqual(self.status("GET", "/api/offline")[0], 404)


if __name__ == "__main__":
    unittest.main()
