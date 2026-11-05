"""操作记录增加 actor / ip / country（N14c）的测试：python -m pytest tests/test_audit_actor_n14c.py
- 单元：src/admin.py 的 set_actor_from_request / audit / audit_tail（audit 文件指到临时目录，不碰真实 data/）。
- 接口：用 AST 从 app.py 摘出 Handler 的相关方法，起临时本机服务器；不导入 app，不联网。
- 页面：只读 admin.js 做静态检查。"""
import ast
import base64
import hmac
import http.client
import json
import sys
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src import admin, admin_settings, members  # noqa: E402

TREE = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
HANDLER = next(n for n in TREE.body if isinstance(n, ast.ClassDef) and n.name == "Handler")
METHODS = {n.name: n for n in HANDLER.body if isinstance(n, ast.FunctionDef)}
NAMES = ("_send", "_json", "_body", "_admin_get", "_admin_post", "_admin_settings_post", "_member_post")
H = {"X-Requested-With": "dashboard", "Content-Type": "application/json"}


def jwt_with(email):
    seg = base64.urlsafe_b64encode(json.dumps({"email": email}).encode()).decode().rstrip("=")
    return f"aaa.{seg}.ccc"


@pytest.fixture()
def db(tmp_path, monkeypatch):
    old = members.DB
    members.set_db(tmp_path / "m.db")
    monkeypatch.setattr(admin, "AUDIT", tmp_path / "audit.jsonl")
    monkeypatch.setattr(admin_settings, "_last_good", None)
    monkeypatch.setattr(admin, "_last_blocked", 0.0)      # blocked_remote 是 60 秒全局限流；别的测试刚记过一条会让本测试的那条被吞掉
    members._hits.clear()
    yield tmp_path
    members.set_db(old)
    admin.clear_actor()


def make_server(mode):
    """mode: local / owner / remote。_owner、_local_or_owner 按 mode 伪造；真实的 _admin_get/_admin_post 原样使用。"""
    cls = ast.ClassDef(name="Handler", bases=[ast.Name(id="BaseHTTPRequestHandler", ctx=ast.Load())],
                       keywords=[], decorator_list=[], body=[METHODS[n] for n in NAMES])
    ns = dict(BaseHTTPRequestHandler=BaseHTTPRequestHandler, urlparse=urlparse, parse_qs=parse_qs, json=json, time=time,
              hmac=hmac, SimpleCookie=SimpleCookie, members=members, admin=admin, admin_settings=admin_settings,
              MEMBER_COOKIE="qm", ADMIN_COOKIE="qa", ADMIN_TOKEN="t", ADMIN_CTX={}, ALLOWED_HOSTS=set(), DASH=ROOT / "dashboard")
    exec(compile(ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[])), "<handler>", "exec"), ns)

    class Test(ns["Handler"]):
        def log_message(self, *a):
            pass

        def _owner(self):
            return mode == "owner"

        def _local_or_owner(self):
            return mode in ("local", "owner")

        def _admin_authed(self):
            return True

        def _origin_ok(self):
            return True

        def _https(self):
            return False

        def _client_ip(self):
            return "9.9.9.9"

        def _mlog(self, *a, **k):
            pass

        def do_GET(self):
            u = urlparse(self.path)
            self._admin_get(u, {k: v[0] for k, v in parse_qs(u.query).items()})

        def do_POST(self):
            self._admin_post(urlparse(self.path))

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Test)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()   # 默认 0.5 秒会让 shutdown() 白等
    return srv


def call(srv, method, path, body=None, extra=None):
    c = http.client.HTTPConnection("127.0.0.1", srv.server_port, timeout=5)
    c.request(method, path, body=json.dumps(body) if body is not None else None, headers={**H, **(extra or {})})
    r = c.getresponse()
    raw = r.read()
    c.close()
    try:
        return r.status, json.loads(raw)
    except ValueError:
        return r.status, raw


def audit_lines(tmp):
    p = tmp / "audit.jsonl"
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


# ---------------- 单元
def test_audit_has_three_fields_even_without_context(db):
    admin.clear_actor()
    admin.audit("x")
    rec = audit_lines(db)[0]
    assert rec["actor"] == "" and rec["ip"] == "" and rec["country"] == ""


def test_local_request_actor(db):
    admin.set_actor_from_request({"Host": "127.0.0.1:8765"}, "127.0.0.1", None)
    admin.audit("x")
    rec = audit_lines(db)[0]
    assert (rec["actor"], rec["ip"], rec["country"]) == ("local", "127.0.0.1", "")


def test_owner_actor_from_jwt_and_cf_headers(db):
    h = {"Cf-Access-Jwt-Assertion": jwt_with("me@example.com"), "CF-Connecting-IP": "1.2.3.4, 5.6.7.8", "CF-IPCountry": "us"}
    admin.set_actor_from_request(h, "127.0.0.1", lambda: True)
    admin.audit("x")
    rec = audit_lines(db)[0]
    assert (rec["actor"], rec["ip"], rec["country"]) == ("me@example.com", "1.2.3.4", "US")


def test_owner_with_broken_jwt_falls_back_to_owner(db):
    admin.set_actor_from_request({"Cf-Access-Jwt-Assertion": "garbage"}, "", lambda: True)
    admin.audit("x")
    assert audit_lines(db)[0]["actor"] == "owner"


def test_non_owner_proxy_request_is_anonymous_not_owner(db):
    h = {"CF-Connecting-IP": "8.8.8.8", "CF-IPCountry": "DE", "Cf-Access-Jwt-Assertion": jwt_with("me@example.com")}
    admin.set_actor_from_request(h, "127.0.0.1", lambda: False)      # 签名没过：邮箱不能被采信
    admin.audit("x")
    rec = audit_lines(db)[0]
    assert rec["actor"] == "anonymous" and rec["ip"] == "8.8.8.8"


def test_fields_sanitized_and_truncated(db):
    admin.set_actor("a\nb\x00" + "x" * 500, "1.1.1.1\r\n" + "9" * 100, "us\n" * 10)
    admin.audit("x")
    rec = audit_lines(db)[0]
    assert "\n" not in rec["actor"] and len(rec["actor"]) <= admin.ACTOR_MAX
    assert "\r" not in rec["ip"] and len(rec["ip"]) <= admin.IP_MAX
    assert len(rec["country"]) <= admin.COUNTRY_MAX


def test_old_records_get_empty_fields(db):
    (db / "audit.jsonl").write_text(json.dumps({"t": "2026-01-01 00:00:00", "action": "login", "ok": True}) + "\n", encoding="utf-8")
    items = admin.audit_tail(10)
    assert items[0]["actor"] == "" and items[0]["ip"] == "" and items[0]["country"] == ""


def test_actor_is_per_thread(db):
    admin.set_actor("main", "1.1.1.1", "US")
    seen = {}

    def other():
        admin.audit("t")
        seen["rec"] = audit_lines(db)[-1]

    t = threading.Thread(target=other)
    t.start()
    t.join()
    assert seen["rec"]["actor"] == ""


# ---------------- 接口
def test_settings_change_is_audited_with_actor_local(db):
    srv = make_server("local")
    try:
        st, _ = call(srv, "POST", "/api/admin/settings", {"registration_open": True, "maintenance_on": True, "sections": {"today": {"on": False}}})
        assert st == 200
    finally:
        srv.shutdown()
        srv.server_close()
    recs = [r for r in audit_lines(db) if r["action"] == "setting"]
    assert len(recs) == 3                                            # 每个真正变化的开关一条
    assert all(r["actor"] == "local" and r["ip"] == "127.0.0.1" and r["ok"] for r in recs)


def test_settings_change_audited_with_owner_identity(db):
    srv = make_server("owner")
    hdr = {"Cf-Access-Jwt-Assertion": jwt_with("boss@example.com"), "CF-Connecting-IP": "2.2.2.2", "CF-IPCountry": "JP"}
    try:
        st, _ = call(srv, "POST", "/api/admin/settings", {"membership_on": False}, hdr)
        assert st == 200
        st, j = call(srv, "GET", "/api/admin/audit", extra=hdr)
    finally:
        srv.shutdown()
        srv.server_close()
    rec = next(r for r in j["items"] if r["action"] == "setting")
    assert (rec["actor"], rec["ip"], rec["country"]) == ("boss@example.com", "2.2.2.2", "JP")


def test_invalid_setting_audited_as_failure_without_changing(db):
    srv = make_server("local")
    try:
        st, j = call(srv, "POST", "/api/admin/settings", {"nope": 1})
        assert st == 400 and j["error"] == "SETTING_INVALID"
    finally:
        srv.shutdown()
        srv.server_close()
    rec = [r for r in audit_lines(db) if r["action"] == "setting"][0]
    assert rec["ok"] is False and rec["actor"] == "local"
    assert admin_settings.get_all()["registration_open"] is False


def test_remote_without_owner_still_404_and_logged_anonymous(db):
    srv = make_server("remote")
    try:
        for m, p in (("GET", "/api/admin/audit"), ("GET", "/api/admin/settings"), ("POST", "/api/admin/settings"), ("POST", "/api/admin/action")):
            st, _ = call(srv, m, p, {"registration_open": True} if m == "POST" else None, {"CF-Connecting-IP": "6.6.6.6", "CF-IPCountry": "RU"})
            assert st == 404
    finally:
        srv.shutdown()
        srv.server_close()
    assert admin_settings.get_all()["registration_open"] is False
    blocked = [r for r in audit_lines(db) if r["action"] == "blocked_remote"]
    assert blocked and blocked[0]["actor"] == "anonymous" and blocked[0]["ip"] == "6.6.6.6" and blocked[0]["country"] == "RU"


# ---------------- 页面
def test_admin_js_shows_new_columns_with_textcontent_only():
    js = (ROOT / "dashboard" / "admin.js").read_text(encoding="utf-8")
    seg = js[js.index("function viewAudit"):js.index("// ---------- 会员")]
    assert "操作人" in seg and "i.actor" in seg and "i.ip" in seg and "i.country" in seg and "colSpan: 7" in seg
    assert "innerHTML" not in seg and "insertAdjacentHTML" not in seg
