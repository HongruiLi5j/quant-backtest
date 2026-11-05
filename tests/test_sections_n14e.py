"""分区开关（N14e，BACKEND_SPEC §1 “分区”一行）的测试：python -m pytest tests/test_sections_n14e.py
- 单元：src/admin_settings.py 的 sections 校验、读写、脏数据、读失败回退（members 库用 tmp_path）。
- 接口：用 AST 从 app.py 摘出 Handler._section_blocked，起临时本机服务器；不导入 app，不联网。
- 页面：只读 admin.js 做静态检查。"""
import ast
import http.client
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

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
    names = ("_section_blocked", "_send")
    cls = ast.ClassDef(name="Handler", bases=[ast.Name(id="BaseHTTPRequestHandler", ctx=ast.Load())],
                       keywords=[], decorator_list=[], body=[METHODS[n] for n in names])
    ns = dict(BaseHTTPRequestHandler=BaseHTTPRequestHandler, admin_settings=admin_settings, re=re, COMPRESSIBLE=set(), time=time)
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
            if self._section_blocked(self.path.split("?")[0]):
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
def test_defaults_all_open(db):
    s = admin_settings.get_all()["sections"]
    assert set(s) == set(admin_settings.SECTION_IDS) and all(v == {"on": True} for v in s.values())
    assert all(admin_settings.section_open(i) for i in admin_settings.SECTION_IDS)


def test_close_and_reopen_one_page(db):
    res = admin_settings.update({"sections": {"market": {"on": False}}})
    assert res["changed"] == ["sections"]
    assert admin_settings.section_open("market") is False and admin_settings.section_open("today") is True
    admin_settings.update({"sections": {"market": {"on": True}}})
    assert admin_settings.section_open("market") is True


def test_unchanged_not_reported(db):
    assert admin_settings.update({"sections": {"today": {"on": True}}})["changed"] == []


@pytest.mark.parametrize("patch,key", [
    ({"sections": {"bogus": {"on": False}}}, "sections.bogus"),
    ({"sections": {"today": {"on": "no"}}}, "sections.today"),
    ({"sections": {"today": False}}, "sections.today"),
    ({"sections": {"today": {"on": False, "min": "pro"}}}, "sections.today"),
    ({"sections": {}}, "sections"),
    ({"sections": []}, "sections"),
])
def test_invalid_rejected_and_nothing_written(db, patch, key):
    with pytest.raises(admin_settings.SettingError) as e:
        admin_settings.update({"registration_open": True, **patch})
    assert e.value.key == key
    assert admin_settings.get_all()["registration_open"] is False


def test_migration_idempotent_and_single_row(db):
    for _ in range(3):
        admin_settings.get_all()
    admin_settings.update({"sections": {"watch": {"on": False}}})
    admin_settings.update({"sections": {"signals": {"on": False}}})
    with members._conn() as c:
        assert c.execute("SELECT COUNT(*) FROM settings WHERE key='sections'").fetchone()[0] == 1
    assert [i for i in admin_settings.SECTION_IDS if not admin_settings.section_open(i)] == ["signals", "watch"]


def test_dirty_db_values_stay_open(db):
    admin_settings.get_all()
    dirty = '{"today": {"on": "no"}, "x": {"on": false}, "market": 3}'
    with members._conn() as c:
        c.execute("INSERT INTO settings(key,value,updated_at) VALUES('sections',?,'x')", (dirty,))
    s = admin_settings.get_all()["sections"]
    assert s["today"] == {"on": True} and s["market"] == {"on": True} and "x" not in s
    with members._conn() as c:
        c.execute("UPDATE settings SET value='not json' WHERE key='sections'")
    assert admin_settings.section_open("today") is True            # 坏 JSON：回退，不抛错


def test_read_failure_uses_last_good_then_open(db, monkeypatch):
    admin_settings.update({"sections": {"holdings": {"on": False}}})
    assert admin_settings.section_open("holdings") is False

    def boom():
        raise RuntimeError("db locked")
    monkeypatch.setattr(members, "_conn", boom)
    assert admin_settings.section_open("holdings") is False        # 沿用上次
    monkeypatch.setattr(admin_settings, "_last_good", None)
    assert admin_settings.section_open("holdings") is True         # 从没读到：默认开放


def test_unknown_id_is_open(db):
    assert admin_settings.section_open("nope") is True


# ---------------- 接口
def test_open_pages_pass(server, db):
    srv, T = server
    assert get(srv, "/ui/w/market.html")[0] == 200


def test_closed_page_gives_coming_soon_not_404(server, db):
    srv, T = server
    admin_settings.update({"sections": {"market": {"on": False}}})
    code, hdr, body = get(srv, "/ui/w/market.html?mock=1")
    assert code == 200 and "暂未开放" in body.decode("utf-8") and b"real page" not in body
    assert "text/html" in hdr["Content-Type"]
    assert get(srv, "/ui/w/today.html")[2] == b"real page"         # 其他页不受影响


def test_owner_or_local_bypasses(server, db):
    srv, T = server
    admin_settings.update({"sections": {"market": {"on": False}}})
    T.local = True
    assert get(srv, "/ui/w/market.html")[2] == b"real page"


def test_only_known_pages_gated(server, db):
    srv, T = server
    admin_settings.update({"sections": {"market": {"on": False}}})
    for p in ("/ui/w/stock.html", "/ui/w/market.js", "/ui/w/../w/market.html", "/ui/shared/api.js", "/"):
        assert get(srv, p)[2] == b"real page", p


# ---------------- 页面
def test_admin_js_wired():
    js = (ROOT / "dashboard" / "admin.js").read_text(encoding="utf-8")
    assert "swLive('sections:'" in js and "body.sections = secs" in js
    assert "现在全部灰色" not in js
    for bad in (".innerHTML", "insertAdjacentHTML", "document.write("):
        assert bad not in js
    sec = js.split("function viewSections")[1].split("function viewSystem")[0]
    assert "offRow" not in sec                                       # 不再是灰色开关
