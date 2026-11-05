"""维护模式（N14b，BACKEND_SPEC §2.10 第 1、2、3 条）的测试：python -m pytest tests/test_maintenance_n14b.py
- 单元：src/admin_settings.py 的 maintenance_on / maintenance_until、Retry-After 计算、读失败回退（members 库用 tmp_path）。
- 接口：用 AST 从 app.py 摘出 Handler._maintenance_blocked，起临时本机服务器；不导入 app，不联网。
- 页面：只读 admin.js 做静态检查。"""
import ast
import re
import http.client
import sys
import threading
import time
from html import escape as html_escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src import admin_settings, members  # noqa: E402

TREE = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
HANDLER = next(n for n in TREE.body if isinstance(n, ast.ClassDef) and n.name == "Handler")
METHODS = {n.name: n for n in HANDLER.body if isinstance(n, ast.FunctionDef)}


@pytest.fixture()
def db(tmp_path, monkeypatch):
    old = members.DB
    members.set_db(tmp_path / "m.db")
    monkeypatch.setattr(admin_settings, "_last_good", None)
    yield tmp_path
    members.set_db(old)


@pytest.fixture()
def server(db):
    names = ("_maintenance_blocked", "_send")
    cls = ast.ClassDef(name="Handler", bases=[ast.Name(id="BaseHTTPRequestHandler", ctx=ast.Load())],
                       keywords=[], decorator_list=[], body=[METHODS[n] for n in names])
    ns = dict(BaseHTTPRequestHandler=BaseHTTPRequestHandler, urlparse=urlparse, admin_settings=admin_settings,
              html_escape=html_escape, COMPRESSIBLE=set(), time=time)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[])), "<handler>", "exec"), ns)

    class Test(ns["Handler"]):
        local = False

        def log_message(self, *a):
            pass

        def _local_or_owner(self):
            return Test.local

        def _https(self):
            return False

        def do_GET(self):
            if self._maintenance_blocked():
                return
            self._send(200, b"real page", "text/plain")

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Test)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()   # 默认 0.5 秒会让 shutdown() 白等
    yield srv, Test
    srv.shutdown()
    srv.server_close()


def get(srv, path):
    c = http.client.HTTPConnection("127.0.0.1", srv.server_port, timeout=5)
    c.request("GET", path)
    r = c.getresponse()
    out = (r.status, dict(r.getheaders()), r.read())
    c.close()
    return out


# ---------------- 单元
def test_defaults_maintenance_off(db):
    s = admin_settings.get_all()
    assert s["maintenance_on"] is False and s["maintenance_until"] == ""
    assert admin_settings.maintenance() == (False, "")


def test_update_and_read_back(db):
    res = admin_settings.update({"maintenance_on": True, "maintenance_until": "  2026-11-01 18:00 "})
    assert set(res["changed"]) == {"maintenance_on", "maintenance_until"}
    assert admin_settings.maintenance() == (True, "2026-11-01 18:00")
    admin_settings.update({"maintenance_on": False})
    assert admin_settings.maintenance()[0] is False


@pytest.mark.parametrize("patch,key", [
    ({"maintenance_on": "true"}, "maintenance_on"),
    ({"maintenance_on": 1}, "maintenance_on"),
    ({"maintenance_until": 5}, "maintenance_until"),
    ({"maintenance_until": "x" * 41}, "maintenance_until"),
    ({"maintenance_until": "a\nb"}, "maintenance_until"),
])
def test_invalid_rejected_and_nothing_written(db, patch, key):
    with pytest.raises(admin_settings.SettingError) as e:
        admin_settings.update({"registration_open": True, **patch})
    assert e.value.key == key
    assert admin_settings.get_all()["registration_open"] is False       # 整批不写


def test_migration_idempotent(db):
    for _ in range(3):
        admin_settings.get_all()
    admin_settings.update({"maintenance_on": True})
    admin_settings.get_all()
    with members._conn() as c:
        assert c.execute("SELECT COUNT(*) FROM settings WHERE key='maintenance_on'").fetchone()[0] == 1


def test_dirty_db_values_mean_off(db):
    admin_settings.get_all()
    with members._conn() as c:
        c.execute("INSERT INTO settings(key,value,updated_at) VALUES('maintenance_on','\"yes\"','x')")
        c.execute("INSERT INTO settings(key,value,updated_at) VALUES('maintenance_until','123','x')")
    assert admin_settings.maintenance() == (False, "")


def test_read_failure_uses_last_good_then_default(db, monkeypatch):
    admin_settings.update({"maintenance_on": True, "maintenance_until": "晚上"})
    assert admin_settings.maintenance() == (True, "晚上")           # 这次成功，记下

    def boom():
        raise RuntimeError("db locked")
    monkeypatch.setattr(members, "_conn", boom)
    assert admin_settings.maintenance() == (True, "晚上")            # 读失败：沿用上次的值
    monkeypatch.setattr(admin_settings, "_last_good", None)
    assert admin_settings.maintenance() == (False, "")              # 从没读到过：安全默认（网站开放）


def test_retry_after_seconds():
    from datetime import datetime, timedelta, timezone
    now = datetime(2026, 11, 1, 10, 0, tzinfo=timezone(timedelta(hours=8))).timestamp()
    assert admin_settings.retry_after_seconds("2026-11-01 11:00", now) == 3600
    assert admin_settings.retry_after_seconds("2026-11-01 12:30", now) == 9000
    assert admin_settings.retry_after_seconds("", now) == 3600                       # 没填
    assert admin_settings.retry_after_seconds("今晚", now) == 3600                   # 解析不了
    assert admin_settings.retry_after_seconds("2026-10-01 11:00", now) == 3600       # 已过期
    assert admin_settings.retry_after_seconds("2026-11-01 10:00:30", now) == 3600    # 格式不对
    assert admin_settings.retry_after_seconds("2026-11-01 10:00", now) == 3600       # 差值 0 → 默认
    assert admin_settings.retry_after_seconds("2026-11-01 10:05", now) == 300
    assert admin_settings.retry_after_seconds("2030-01-01", now) == 7 * 86400       # 上限 7 天


# ---------------- 接口
def test_off_does_not_block(server, db):
    srv, T = server
    T.local = False
    assert get(srv, "/")[0] == 200


def test_on_blocks_outsiders_with_503_and_retry_after(server, db):
    srv, T = server
    admin_settings.update({"maintenance_on": True})
    T.local = False
    code, hdr, body = get(srv, "/")
    assert code == 503 and hdr["Retry-After"] == "3600"
    assert "维护" in body.decode("utf-8")
    code, hdr, _ = get(srv, "/api/meta")
    assert code == 503                                              # 不是 404 / 403


def test_retry_after_from_until(server, db):
    srv, T = server
    from datetime import datetime, timedelta, timezone
    t = (datetime.now(timezone(timedelta(hours=8))) + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M")
    admin_settings.update({"maintenance_on": True, "maintenance_until": t})
    code, hdr, body = get(srv, "/")
    assert code == 503 and 60 <= int(hdr["Retry-After"]) <= 7500
    assert t in body.decode("utf-8")


def test_until_is_html_escaped(server, db):
    srv, T = server
    admin_settings.update({"maintenance_on": True, "maintenance_until": "<script>1</script>"})
    _, _, body = get(srv, "/")
    assert b"<script>" not in body and b"&lt;script&gt;" in body


def test_owner_or_local_bypasses(server, db):
    srv, T = server
    admin_settings.update({"maintenance_on": True})
    T.local = True
    assert get(srv, "/") == (200, get(srv, "/")[1], b"real page")


def test_robots_always_200(server, db):
    srv, T = server
    admin_settings.update({"maintenance_on": True})
    T.local = False
    code, hdr, body = get(srv, "/robots.txt")
    assert code == 200 and b"User-agent" in body


def test_admin_paths_not_turned_into_503(server, db):
    """后台路径不归维护页管：外网无主人身份时由后台自己返回 404（不会变成 503，也不会泄漏后台存在）。"""
    srv, T = server
    admin_settings.update({"maintenance_on": True})
    T.local = False
    for p in ("/admin", "/admin.js", "/api/admin/settings"):
        assert get(srv, p)[0] == 200                                # 测试用 do_GET 放行了 → 说明维护门没拦


def test_gate_exception_means_open(server, db, monkeypatch):
    srv, T = server

    def boom():
        raise RuntimeError("x")
    monkeypatch.setattr(admin_settings, "maintenance", boom)
    T.local = False
    assert get(srv, "/")[0] == 200


# ---------------- 源码约束
def test_app_gate_wired_in_get_and_post_and_admin_404_kept():
    src = (ROOT / "app.py").read_text(encoding="utf-8")
    for name in ("do_GET", "do_POST"):
        body = ast.get_source_segment(src, METHODS[name])
        assert "self._maintenance_blocked()" in body
    gate = ast.get_source_segment(src, METHODS["_maintenance_blocked"])
    assert "/api/admin/" in gate
    assert "_send(404" not in gate and "_send(403" not in gate and "_send(410" not in gate
    for n in ("_admin_get", "_admin_post"):                          # 外网无主人身份仍然 404
        assert "if not self._local_or_owner()" in ast.get_source_segment(src, METHODS[n])


def test_admin_js_maintenance_switch_wired():
    js = (ROOT / "dashboard" / "admin.js").read_text(encoding="utf-8")
    assert "swLive('maintenance_on'" in js and "state.draft.maintenance_on === true" in js
    assert "maintenance_until" in js
    assert "offRow('维护模式'" not in js
    assert not re.search(r"\.innerHTML\s*=|insertAdjacentHTML|document\.write", js)
