"""我的登录列表 + 退出单台（N14d）的测试：python -m pytest tests/test_owner_sessions_n14d.py
- 单元：src/admin_settings.py 的 register_session / session_allowed / list_sessions / revoke_session（数据库指到临时目录）。
- 接口：用 AST 从 app.py 摘出 Handler 的真实方法（含真实的 _admin_authed），起临时本机服务器；不导入 app，不联网。
- 页面：只读 admin.js 做静态检查。"""
import ast
import hmac
import http.client
import json
import sqlite3
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
NAMES = ("_send", "_json", "_body", "_admin_get", "_admin_post", "_admin_authed", "_admin_settings_post", "_admin_cookie_value",
         "_owner_sessions_list", "_owner_session_revoke")
H = {"X-Requested-With": "dashboard", "Content-Type": "application/json"}
UA_IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
UA_MAC = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


@pytest.fixture()
def db(tmp_path, monkeypatch):
    old = members.DB
    members.set_db(tmp_path / "m.db")
    monkeypatch.setattr(admin, "AUDIT", tmp_path / "audit.jsonl")
    monkeypatch.setattr(admin_settings, "_last_good", None)
    members._hits.clear()
    yield tmp_path
    members.set_db(old)
    admin.clear_actor()


def make_server(mode):
    """mode: local / owner / remote。只伪造 _owner / _local_or_owner；真实的 _admin_get/_admin_post/_admin_authed 原样使用。"""
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


def call(srv, method, path, cookie=None, ua=UA_MAC, extra=None):
    c = http.client.HTTPConnection("127.0.0.1", srv.server_port, timeout=5)
    hd = {**H, "User-Agent": ua, **(extra or {})}
    if cookie:
        hd["Cookie"] = "qa=" + cookie
    c.request(method, path, body="{}" if method == "POST" else None, headers=hd)
    r = c.getresponse()
    raw = r.read()
    c.close()
    try:
        return r.status, json.loads(raw)
    except ValueError:
        return r.status, raw


def cookie_at(sec):
    return admin.make_session("t", sec)


def hdrs(ua=UA_MAC, ip="", cc=""):
    d = {"User-Agent": ua}
    if ip:
        d["CF-Connecting-IP"] = ip
    if cc:
        d["CF-IPCountry"] = cc
    return d


# ---------- 单元
def test_device_summary_short_text():
    assert admin_settings.device_summary(UA_IPHONE) == "iPhone · Safari"
    assert admin_settings.device_summary(UA_MAC) == "Mac · Chrome"
    assert admin_settings.device_summary("") == "未知系统 · 未知浏览器"
    assert "Mozilla" not in admin_settings.device_summary(UA_MAC)      # 不存原始头


def test_migration_idempotent_and_adds_only(db):
    for _ in range(3):
        admin_settings.list_sessions()
    c = sqlite3.connect(str(db / "m.db"))
    cols = [r[1] for r in c.execute("PRAGMA table_info(owner_sessions)")]
    assert cols == ["id", "th", "device", "ip", "country", "created_at", "last_active", "exp", "revoked"]
    assert c.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0       # 旧表照常在
    c.close()


def test_register_list_and_no_cookie_stored(db):
    now = int(time.time())
    ck = cookie_at(now)
    admin_settings.register_session(ck, hdrs(UA_IPHONE, "1.2.3.4", "us"), "127.0.0.1")
    admin_settings.register_session(ck, hdrs(UA_IPHONE, "1.2.3.4", "us"), "127.0.0.1")       # 重复登记不产生第二行
    rows = admin_settings.list_sessions(ck)
    assert len(rows) == 1
    r = rows[0]
    assert (r["device"], r["ip"], r["country"], r["current"]) == ("iPhone · Safari", "1.2.3.4", "US", True)
    assert set(r) == {"id", "device", "ip", "country", "last_active", "current"}
    raw = (db / "m.db").read_bytes()
    assert ck.encode() not in raw                                            # 库里只有哈希


def test_expired_not_listed_and_purged(db):
    old = cookie_at(int(time.time()) - 9 * 3600)
    admin_settings.register_session(old, hdrs(), "127.0.0.1")
    assert admin_settings.list_sessions() == []


def test_revoke_other_ok_current_refused(db):
    now = int(time.time())
    a, b = cookie_at(now), cookie_at(now - 5)
    admin_settings.register_session(a, hdrs(), "127.0.0.1")
    admin_settings.register_session(b, hdrs(UA_IPHONE), "127.0.0.1")
    rows = admin_settings.list_sessions(a)
    cur = next(r for r in rows if r["current"])
    oth = next(r for r in rows if not r["current"])
    with pytest.raises(admin_settings.SessionError) as e:
        admin_settings.revoke_session(cur["id"], a)
    assert e.value.code == "CANNOT_REVOKE_CURRENT"
    assert admin_settings.session_allowed(a, hdrs()) is True
    admin_settings.revoke_session(oth["id"], a)
    assert admin_settings.session_allowed(b, hdrs()) is False
    assert [r["id"] for r in admin_settings.list_sessions(a)] == [cur["id"]]
    with pytest.raises(admin_settings.SessionError) as e:
        admin_settings.revoke_session(oth["id"], a)                          # 已退出的再退
    assert e.value.code == "NOT_FOUND"


@pytest.mark.parametrize("bad", ["", "zz", "x' OR '1'='1", "../../a", "a" * 40, "ABCDEF"])
def test_bad_ids_rejected_before_sql(db, bad):
    with pytest.raises(admin_settings.SessionError) as e:
        admin_settings.revoke_session(bad, "")
    assert e.value.code == "BAD_ID"


def test_legacy_cookie_registered_lazily(db):
    ck = cookie_at(int(time.time()))
    assert admin_settings.list_sessions() == []
    assert admin_settings.session_allowed(ck, hdrs(), "127.0.0.1") is True
    assert len(admin_settings.list_sessions(ck)) == 1


def test_session_allowed_fails_open_on_db_error(db, monkeypatch):
    def boom():
        raise sqlite3.OperationalError("locked")
    monkeypatch.setattr(members, "_conn", boom)
    assert admin_settings.session_allowed(cookie_at(1), hdrs()) is True      # 库坏了不把所有者锁在外面


# ---------- 接口
def test_api_list_and_revoke_flow(db):
    srv = make_server("local")
    try:
        now = int(time.time())
        a, b = cookie_at(now), cookie_at(now - 5)
        st, rows = call(srv, "GET", "/api/admin/owner/sessions", a)
        assert st == 200 and len(rows) == 1 and rows[0]["current"] is True
        st, _ = call(srv, "GET", "/api/admin/owner/sessions", b, ua=UA_IPHONE)
        assert st == 200
        st, rows = call(srv, "GET", "/api/admin/owner/sessions", a)
        assert len(rows) == 2
        cur = next(r for r in rows if r["current"])
        oth = next(r for r in rows if not r["current"])
        assert oth["device"] == "iPhone · Safari"
        st, j = call(srv, "POST", f"/api/admin/owner/sessions/{cur['id']}/revoke", a)
        assert (st, j["error"]) == (400, "CANNOT_REVOKE_CURRENT")
        st, j = call(srv, "POST", f"/api/admin/owner/sessions/{oth['id']}/revoke", a)
        assert st == 200 and j["ok"] is True
        assert call(srv, "GET", "/api/admin/owner/sessions", b)[0] == 401      # 被退出的那台：真的进不来了
        assert call(srv, "GET", "/api/admin/owner/sessions", a)[0] == 200      # 当前这台不受影响
        st, j = call(srv, "POST", f"/api/admin/owner/sessions/{oth['id']}/revoke", a)
        assert (st, j["error"]) == (404, "NOT_FOUND")
        st, j = call(srv, "POST", "/api/admin/owner/sessions/zz/revoke", a)
        assert (st, j["error"]) == (400, "BAD_ID")
        acts = [json.loads(x) for x in (db / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
        assert [x["ok"] for x in acts if x["action"] == "session_revoke"] == [False, True, False, False]
    finally:
        srv.shutdown()


def test_api_requires_login_and_csrf_header(db):
    srv = make_server("local")
    try:
        assert call(srv, "GET", "/api/admin/owner/sessions")[0] == 401         # 没 cookie
        assert call(srv, "GET", "/api/admin/owner/sessions", "1.bad")[0] == 401
        ck = cookie_at(int(time.time()))
        st, _ = call(srv, "POST", "/api/admin/owner/sessions/abcd/revoke", ck, extra={"X-Requested-With": "x"})
        assert st == 401
    finally:
        srv.shutdown()


def test_remote_without_owner_still_404(db):
    srv = make_server("remote")
    try:
        ck = cookie_at(int(time.time()))
        assert call(srv, "GET", "/api/admin/owner/sessions", ck)[0] == 404
        assert call(srv, "POST", "/api/admin/owner/sessions/abcd/revoke", ck)[0] == 404
        assert call(srv, "GET", "/api/admin/settings", ck)[0] == 404
    finally:
        srv.shutdown()


def test_owner_channel_without_cookie_can_list_and_revoke_any(db):
    srv = make_server("owner")
    try:
        ck = cookie_at(int(time.time()))
        admin_settings.register_session(ck, hdrs(), "127.0.0.1")
        st, rows = call(srv, "GET", "/api/admin/owner/sessions")
        assert st == 200 and len(rows) == 1 and rows[0]["current"] is False
        assert call(srv, "POST", f"/api/admin/owner/sessions/{rows[0]['id']}/revoke")[0] == 200
    finally:
        srv.shutdown()


def test_app_source_uses_parameterized_sql_only():
    src = (ROOT / "src" / "admin_settings.py").read_text(encoding="utf-8")
    seg = src[src.index("def _ensure_sessions"):]
    assert "f\"SELECT" not in seg and "f\"UPDATE" not in seg and "f\"DELETE" not in seg and "f\"INSERT" not in seg
    assert ".format(" not in seg and "% (" not in seg


# ---------- 页面（只读 admin.js）
def test_admin_js_wires_sessions_row():
    js = (ROOT / "dashboard" / "admin.js").read_text(encoding="utf-8")
    assert "/api/admin/owner/sessions" in js and "/revoke" in js
    assert ".innerHTML" not in js and "insertAdjacentHTML" not in js and "document.write" not in js
    assert "offRow('退出其他登录设备'" not in js                                # 不再是灰色占位
    assert "revoke:" in js and "saveDraft" in js
