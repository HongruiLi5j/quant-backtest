"""后台设置表 + 注册开关（N14a）的测试：python -m pytest tests/test_admin_settings_n14a.py
- 单元：src/admin_settings.py 的校验、读写、读失败回退（members 库用 tempfile，不碰真实 data/）。
- 接口：用 AST 从 app.py 摘出 Handler 的相关方法，起临时本机服务器；不导入 app，不联网。
- 页面：只读 admin.js / admin.html 做静态检查。"""
import ast
import hmac
import http.client
import json
import sys
import tempfile
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


@pytest.fixture()
def db(tmp_path, monkeypatch):
    old = members.DB
    members.set_db(tmp_path / "m.db")
    monkeypatch.setattr(admin, "AUDIT", tmp_path / "audit.jsonl")
    monkeypatch.setattr(admin_settings, "_last_good", None)
    members._hits.clear()
    yield tmp_path
    members.set_db(old)


@pytest.fixture()
def server(db):
    cls = ast.ClassDef(name="Handler", bases=[ast.Name(id="BaseHTTPRequestHandler", ctx=ast.Load())],
                       keywords=[], decorator_list=[], body=[METHODS[n] for n in NAMES])
    ns = dict(BaseHTTPRequestHandler=BaseHTTPRequestHandler, urlparse=urlparse, parse_qs=parse_qs, json=json, time=time,
              hmac=hmac, SimpleCookie=SimpleCookie, members=members, admin=admin, admin_settings=admin_settings,
              MEMBER_COOKIE="qm", ADMIN_COOKIE="qa", ADMIN_TOKEN="t", ADMIN_CTX={}, ALLOWED_HOSTS=set(), DASH=ROOT / "dashboard")
    exec(compile(ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[])), "<handler>", "exec"), ns)

    class Test(ns["Handler"]):
        local = True

        def log_message(self, *a):
            pass

        def _local_or_owner(self):
            return Test.local

        def _admin_authed(self):
            return True

        def _origin_ok(self):
            return True

        def _client_ip(self):
            return "9.9.9.9"

        def _https(self):
            return False

        def _mlog(self, *a, **k):
            pass

        def do_GET(self):
            u = urlparse(self.path)
            self._admin_get(u, {k: v[0] for k, v in parse_qs(u.query).items()})

        def do_POST(self):
            u = urlparse(self.path)
            (self._admin_post if u.path.startswith("/api/admin/") else self._member_post)(u)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Test)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    Test.local = True
    yield srv, Test
    srv.shutdown()
    srv.server_close()


def call(srv, method, path, body=None):
    c = http.client.HTTPConnection("127.0.0.1", srv.server_port, timeout=5)
    c.request(method, path, body=json.dumps(body) if body is not None else None, headers=H)
    r = c.getresponse()
    raw = r.read()
    c.close()
    try:
        return r.status, json.loads(raw)
    except ValueError:
        return r.status, raw


def user_count():
    with members._conn() as c:
        return c.execute("SELECT COUNT(*) FROM users").fetchone()[0]


# ---------------- 单元：src/admin_settings.py
def test_defaults_registration_closed(db):
    s = admin_settings.get_all()
    assert s["registration_open"] is False and s["default_tier"] == "free"
    assert admin_settings.registration_open() is False


def test_migration_repeatable(db):
    for _ in range(3):
        admin_settings.get_all()
    with members._conn() as c:
        cols = [r[1] for r in c.execute("PRAGMA table_info(settings)")]
    assert cols == ["key", "value", "updated_at"]


def test_update_and_persist(db):
    r = admin_settings.update({"registration_open": True, "quota": {"free_backtest": 7}})
    assert sorted(r["changed"]) == ["quota", "registration_open"]
    s = admin_settings.get_all()
    assert s["registration_open"] is True and s["quota"]["free_backtest"] == 7 and s["quota"]["pro_backtest"] == 30


def test_unchanged_not_audited(db):
    seen = []
    admin_settings.update({"registration_open": False}, lambda *a: seen.append(a))
    assert seen == []
    admin_settings.update({"registration_open": True}, lambda *a: seen.append(a))
    assert seen == [("registration_open", False, True)]


@pytest.mark.parametrize("patch,key", [
    ({"nope": 1}, "nope"),
    ({"registration_open": "yes"}, "registration_open"),
    ({"registration_open": 1}, "registration_open"),
    ({"default_tier": "gold"}, "default_tier"),
    ({"quota": {"free_backtest": 1001}}, "quota.free_backtest"),
    ({"quota": {"free_backtest": -1}}, "quota.free_backtest"),
    ({"quota": {"free_backtest": True}}, "quota.free_backtest"),
    ({"quota": {"free_backtest": 1.5}}, "quota.free_backtest"),
    ({"quota": {"zzz": 1}}, "quota.zzz"),
    ({"features": {"engines": "x"}}, "features.engines"),
    ({"features": []}, "features"),
])
def test_invalid_rejected_and_nothing_written(db, patch, key):
    with pytest.raises(admin_settings.SettingError) as e:
        admin_settings.update({"membership_on": False, **patch})
    assert e.value.key == key
    assert admin_settings.get_all()["membership_on"] is True      # 同一次请求里的合法键也不能写进去


def test_sql_injection_key_is_just_unknown(db):
    with pytest.raises(admin_settings.SettingError):
        admin_settings.update({"x'; DROP TABLE users;--": True})
    assert user_count() == 0


def test_read_failure_falls_back(db, monkeypatch):
    admin_settings.update({"registration_open": True})
    admin_settings.get_all()                                        # 记下上次成功的值
    monkeypatch.setattr(members, "_conn", lambda: (_ for _ in ()).throw(RuntimeError("坏了")))
    assert admin_settings.registration_open() is True               # 用上次成功的值
    monkeypatch.setattr(admin_settings, "_last_good", None)
    assert admin_settings.registration_open() is False             # 没有上次的值：安全默认=关


def test_garbage_value_treated_as_closed(db):
    admin_settings.get_all()
    with members._conn() as c:
        c.execute("INSERT INTO settings(key,value,updated_at) VALUES(?,?,?)", ("registration_open", '"true"', "x"))
    assert admin_settings.registration_open() is False


# ---------------- 接口
def test_get_post_settings_and_audit(server, db):
    srv, _ = server
    st, j = call(srv, "GET", "/api/admin/settings")
    assert st == 200 and j["registration_open"] is False
    st, j = call(srv, "POST", "/api/admin/settings", {"registration_open": True})
    assert st == 200 and j["ok"] and j["changed"] == ["registration_open"] and j["settings"]["registration_open"] is True
    lines = [json.loads(x) for x in (db / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    assert lines[-1]["action"] == "setting" and lines[-1]["detail"]["change"] == "registration_open false->true"


def test_post_invalid_400(server):
    srv, _ = server
    st, j = call(srv, "POST", "/api/admin/settings", {"bogus": 1})
    assert st == 400 and j == {"error": "SETTING_INVALID", "key": "bogus"}
    st, j = call(srv, "POST", "/api/admin/settings", {"quota": {"pro_backtest": 5000}})
    assert st == 400 and j["key"] == "quota.pro_backtest"
    st, j = call(srv, "POST", "/api/admin/settings", {})
    assert st == 400 and j["error"] == "SETTING_INVALID"


def test_external_without_owner_is_404(server):
    srv, Test = server
    Test.local = False
    assert call(srv, "GET", "/api/admin/settings")[0] == 404
    assert call(srv, "POST", "/api/admin/settings", {"registration_open": True})[0] == 404
    Test.local = True
    assert admin_settings.registration_open() is False              # 外网那次没有改成


def test_register_closed_by_default(server):
    srv, _ = server
    for _ in range(8):                                              # 超过每小时 5 次的限频，也应一直是 403 而不是 429
        st, j = call(srv, "POST", "/api/member/register", {"name": "newuser", "password": "password123"})
        assert st == 403 and j["error"] == "REGISTRATION_CLOSED"
    assert user_count() == 0
    assert members._hits == {}                                       # 关着时不计限频


def test_register_open_close_cycle(server):
    srv, _ = server
    call(srv, "POST", "/api/admin/settings", {"registration_open": True})
    st, j = call(srv, "POST", "/api/member/register", {"name": "newuser", "password": "password123"})
    assert st == 200 and j["user"]["name"] == "newuser" and user_count() == 1
    call(srv, "POST", "/api/admin/settings", {"registration_open": False})
    st, j = call(srv, "POST", "/api/member/register", {"name": "second1", "password": "password123"})
    assert st == 403 and user_count() == 1
    who, tok = members.login("newuser", "password123")               # 已有会员照常登录
    assert members.user_for(tok)["name"] == "newuser"


def test_register_rate_limit_only_when_open(server):
    srv, _ = server
    call(srv, "POST", "/api/admin/settings", {"registration_open": True})
    codes = [call(srv, "POST", "/api/member/register", {"name": f"user{i}abc", "password": "password123"})[0] for i in range(7)]
    assert codes[:5] == [200] * 5 and codes[5:] == [429, 429]


# ---------------- 页面（静态）
JS = (ROOT / "dashboard" / "admin.js").read_text(encoding="utf-8")
HTML = (ROOT / "dashboard" / "admin.html").read_text(encoding="utf-8")


def test_page_registration_switch_wired():
    assert "'/api/admin/settings'" in JS and "registration_open" in JS
    assert "还没保存" in JS and "beforeunload" in JS and "开放注册？" in JS      # 草稿、离开提示、集中确认
    assert "offRow('开放注册'" not in JS                                      # 不再是灰色占位
    for bad in (".innerHTML", "insertAdjacentHTML(", "document.write(", "eval("):
        assert bad not in JS and bad not in HTML, bad
