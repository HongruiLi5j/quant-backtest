"""B15：POST 请求体的错误处理——Content-Length 不合法、体过大、JSON 坏、JSON 不是对象，都要给干净的 4xx，不能变 500。
不 import app（import 会读 .secret）：模块逻辑直接测 src/http_errors.py；接线处用 ast 读 app.py 源码做守卫，
并把 Handler._body 抽出来在桩对象上跑一遍。"""
import ast
import io
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src import http_errors  # noqa: E402
from src.http_errors import BodyError, content_length, read_json_object  # noqa: E402

APP = (ROOT / "app.py").read_text(encoding="utf-8")
TREE = ast.parse(APP)


def _method(name):
    handler = next(n for n in ast.walk(TREE) if isinstance(n, ast.ClassDef) and n.name == "Handler")
    return next(f for f in handler.body if isinstance(f, ast.FunctionDef) and f.name == name)


def read(raw=b"", cl="auto"):
    headers = {} if cl is None else {"Content-Length": str(len(raw)) if cl == "auto" else cl}
    return read_json_object(headers, io.BytesIO(raw))


class ReadJsonObjectTests(unittest.TestCase):
    def err(self, raw=b"", cl="auto"):
        with self.assertRaises(BodyError) as cm:
            read(raw, cl)
        return cm.exception

    def test_valid_object(self):
        self.assertEqual(read(b'{"a": 1}'), {"a": 1})
        self.assertEqual(read("{\"名字\": \"中文\"}".encode("utf-8")), {"名字": "中文"})

    def test_no_body_gives_empty_dict(self):
        for cl in (None, "", "0"):
            self.assertEqual(read(b"{}", cl), {}, cl)        # 没声明长度就不读体

    def test_content_length_not_integer(self):
        for bad in ("abc", "1.5", "0x10", "-1", "1 2"):
            e = self.err(b"{}", bad)
            self.assertEqual((e.status, e.code), (400, "BAD_CONTENT_LENGTH"), bad)

    def test_too_large_is_413_and_nothing_read(self):
        rfile = io.BytesIO(b"{}")
        with self.assertRaises(BodyError) as cm:
            read_json_object({"Content-Length": str(http_errors.MAX_JSON_BODY_BYTES + 1)}, rfile)
        self.assertEqual((cm.exception.status, cm.exception.code), (413, "BODY_TOO_LARGE"))
        self.assertEqual(rfile.tell(), 0)

    def test_exactly_at_limit_is_accepted(self):
        pad = "x" * (http_errors.MAX_JSON_BODY_BYTES - len('{"k": ""}'))
        raw = json.dumps({"k": pad}).encode()
        self.assertEqual(len(raw), http_errors.MAX_JSON_BODY_BYTES)
        self.assertEqual(read(raw), {"k": pad})

    def test_limit_matches_old_hardcoded_value(self):
        self.assertEqual(http_errors.MAX_JSON_BODY_BYTES, 1_000_000)      # 原 app.py._body 里写死的值

    def test_bad_json_and_bad_utf8(self):
        for raw in (b"{", b"not json", b"\xff\xfe\x00", b"   ", b'{"a": 1,}'):
            e = self.err(raw)
            self.assertEqual((e.status, e.code), (400, "BAD_JSON"), raw)
            self.assertNotIn("Expecting", e.message)         # 不回显解析器的英文报错

    def test_non_object_json(self):
        for raw in (b"[]", b"[1,2]", b"null", b"123", b'"s"', b"true"):
            e = self.err(raw)
            self.assertEqual((e.status, e.code), (400, "BODY_NOT_OBJECT"), raw)

    def test_short_body_does_not_crash(self):
        # 声明 30 字节实际只有 5 字节：读到的是残缺 JSON → BAD_JSON，不是 500
        e = self.err(b'{"a":', "30")
        self.assertEqual(e.code, "BAD_JSON")

    def test_payload_shape(self):
        self.assertEqual(self.err(b"[]").payload,
                         {"ok": False, "error": "BODY_NOT_OBJECT", "message": "请求体必须是 JSON 对象"})
        self.assertIsInstance(self.err(b"[]").payload["error"], str)    # 前端 j.error 按字符串读

    def test_is_value_error(self):
        # 没加专门处理的老调用点（except ValueError / Exception）照旧能接住
        self.assertTrue(issubclass(BodyError, ValueError))


class ContentLengthTests(unittest.TestCase):
    def test_values(self):
        self.assertEqual(content_length({"Content-Length": "12"}), 12)
        self.assertEqual(content_length({}), 0)
        self.assertEqual(content_length({"Content-Length": ""}), 0)
        self.assertEqual(content_length({"Content-Length": "abc"}), -1)
        self.assertEqual(content_length({"Content-Length": "²"}), -1)     # 上标数字：str.isdigit() 为真但 int() 失败，也要得 -1


class HandlerBodyTests(unittest.TestCase):
    """把 app.py 里真实的 Handler._body 抽出来，在桩对象上跑：确认它确实委托给 src.http_errors。"""

    def make(self, raw, cl="auto"):
        cls = ast.ClassDef(name="H", bases=[], keywords=[], decorator_list=[], body=[_method("_body")])
        g = {}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[])), "app.py", "exec"), g)
        h = g["H"]()
        h.rfile = io.BytesIO(raw)
        h.headers = {"Content-Length": str(len(raw)) if cl == "auto" else cl}
        return h

    def test_ok_and_errors(self):
        self.assertEqual(self.make(b'{"a": 1}')._body(), {"a": 1})
        for raw, cl, code in ((b"[", "auto", "BAD_JSON"), (b"[]", "auto", "BODY_NOT_OBJECT"),
                              (b"{}", "abc", "BAD_CONTENT_LENGTH"), (b"{}", "99999999", "BODY_TOO_LARGE")):
            with self.assertRaises(BodyError) as cm:
                self.make(raw, cl)._body()
            self.assertEqual(cm.exception.code, code)


class CallSiteGuards(unittest.TestCase):
    """调用点守卫：这些函数里的 try 必须有排在最前面的 `except BodyError`，否则坏请求体会被吞成 500 或别的格式。"""

    def first_handler_names(self, fname):
        for n in ast.walk(_method(fname)):
            if isinstance(n, ast.Try):
                names = [getattr(h.type, "id", None) for h in n.handlers if not isinstance(h.type, ast.Tuple)]
                if "BodyError" in names:
                    return names
        return None

    def test_sites_catch_bodyerror_first(self):
        for fname in ("_admin_post", "_share_login", "_member_post", "do_POST"):
            names = self.first_handler_names(fname)
            self.assertIsNotNone(names, fname)
            self.assertEqual(names.index("BodyError"), 0, fname)

    def test_sites_import_bodyerror_locally(self):
        # 局部 import：已有测试把这些方法单独抽进桩类，桩的命名空间里没有 app.py 的全局名
        for fname in ("_admin_post", "_share_login", "_member_post", "do_POST", "_body"):
            src = ast.get_source_segment(APP, _method(fname))
            self.assertIn("from src.http_errors import", src, fname)

    def test_no_raw_int_content_length_left_in_wiring(self):
        # 持仓 / 两个盯盘接线不再直接 int(Content-Length)（垃圾头会抛 ValueError → 500）
        for fname in ("_holdings", "_market_monitor", "_us_market_monitor"):
            src = ast.get_source_segment(APP, _method(fname))
            self.assertNotIn('int(self.headers.get("Content-Length")', src, fname)
            self.assertIn("content_length(self.headers)", src, fname)


class EndToEndTests(unittest.TestCase):
    """真实 HTTP：把 app.py 里真实的 _body / _admin_post / _member_post / _share_login 抽进桩类，开 127.0.0.1 临时端口，
    发坏请求体，确认拿到的是统一格式的 4xx。成员库、审计日志都指向临时目录，不碰 data/。"""
    NAMES = ("_send", "_json", "_body", "_admin_post", "_member_post", "_share_login", "_admin_settings_post")

    @classmethod
    def setUpClass(cls):
        import hmac
        import http.client
        import tempfile
        import threading
        import time
        from http.cookies import SimpleCookie
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from unittest import mock
        from urllib.parse import parse_qs, urlparse
        from src import admin, admin_settings, members, sharepass

        cls.http_client = http.client
        cls.tmp = tempfile.TemporaryDirectory()
        old_db = members.DB
        members.set_db(Path(cls.tmp.name) / "m.db")
        cls.addClassCleanup(members.set_db, old_db)
        cls.addClassCleanup(cls.tmp.cleanup)
        p = mock.patch.object(admin, "AUDIT", Path(cls.tmp.name) / "audit.jsonl")
        p.start()
        cls.addClassCleanup(p.stop)

        handler = next(n for n in ast.walk(TREE) if isinstance(n, ast.ClassDef) and n.name == "Handler")
        methods = {f.name: f for f in handler.body if isinstance(f, ast.FunctionDef)}
        klass = ast.ClassDef(name="Handler", bases=[ast.Name(id="BaseHTTPRequestHandler", ctx=ast.Load())],
                             keywords=[], decorator_list=[], body=[methods[n] for n in cls.NAMES])
        ns = dict(BaseHTTPRequestHandler=BaseHTTPRequestHandler, urlparse=urlparse, parse_qs=parse_qs, json=json, time=time,
                  hmac=hmac, SimpleCookie=SimpleCookie, members=members, admin=admin, admin_settings=admin_settings,
                  sharepass=sharepass, MEMBER_COOKIE="qm", ADMIN_COOKIE="qa", ADMIN_TOKEN="t", ADMIN_CTX={}, ALLOWED_HOSTS=set())
        exec(compile(ast.fix_missing_locations(ast.Module(body=[klass], type_ignores=[])), "<handler>", "exec"), ns)

        class Test(ns["Handler"]):
            def log_message(self, *a):
                pass

            def _local_or_owner(self):
                return True

            def _admin_authed(self):
                return True

            def _origin_ok(self):
                return True

            def _external(self):
                return True

            def _client_ip(self):
                return "9.9.9.9"

            def _https(self):
                return False

            def do_POST(self):
                u = urlparse(self.path)
                if u.path == "/__share_login":
                    return self._share_login()
                (self._admin_post if u.path.startswith("/api/admin/") else self._member_post)(u)

        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Test)
        threading.Thread(target=cls.srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        cls.addClassCleanup(cls.srv.server_close)
        cls.addClassCleanup(cls.srv.shutdown)

    def post(self, path, raw=b"", headers=None):
        h = {"X-Requested-With": "dashboard", "Content-Type": "application/json", **(headers or {})}
        c = self.http_client.HTTPConnection("127.0.0.1", self.srv.server_port, timeout=5)
        c.putrequest("POST", path, skip_accept_encoding=True)
        for k, v in h.items():
            c.putheader(k, v)
        if "Content-Length" not in h:
            c.putheader("Content-Length", str(len(raw)))
        c.endheaders(raw)
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, json.loads(body)

    PATHS = ("/api/member/login", "/api/admin/action", "/__share_login")

    def test_bad_json_is_400_everywhere(self):
        for path in self.PATHS:
            st, obj = self.post(path, b"{not json")
            self.assertEqual((st, obj), (400, {"ok": False, "error": "BAD_JSON", "message": "请求体不是合法的 JSON"}), path)

    def test_array_body_is_400_everywhere(self):
        for path in self.PATHS:
            st, obj = self.post(path, b"[1, 2]")
            self.assertEqual((st, obj["error"]), (400, "BODY_NOT_OBJECT"), path)

    def test_garbage_content_length_is_400_everywhere(self):
        for path in self.PATHS:
            st, obj = self.post(path, b"{}", {"Content-Length": "abc"})
            self.assertEqual((st, obj["error"]), (400, "BAD_CONTENT_LENGTH"), path)

    def test_oversized_is_413_everywhere(self):
        for path in self.PATHS:
            st, obj = self.post(path, b"", {"Content-Length": str(http_errors.MAX_JSON_BODY_BYTES + 1)})
            self.assertEqual((st, obj["error"]), (413, "BODY_TOO_LARGE"), path)

    def test_error_field_stays_a_string_for_the_frontend(self):
        st, obj = self.post("/api/admin/action", b"[]")
        self.assertIsInstance(obj["error"], str)
        self.assertIs(obj["ok"], False)

    def test_settings_keeps_its_own_contract(self):
        # /api/admin/settings 有自己的既有约定（体坏 → 400 SETTING_INVALID key=body），本任务不改
        st, obj = self.post("/api/admin/settings", b"{not json")
        self.assertEqual((st, obj), (400, {"error": "SETTING_INVALID", "key": "body"}))


class MemberQtyTests(unittest.TestCase):
    def test_infinite_qty_is_member_error_not_overflow(self):
        from src import members
        for qty in (float("inf"), float("-inf"), float("nan"), "abc", [], {}, None):
            with self.assertRaises(members.MemberError):
                members.order(1, "cn", "600000", "buy", qty, {})     # 数量校验在碰数据库之前


if __name__ == "__main__":
    unittest.main()
