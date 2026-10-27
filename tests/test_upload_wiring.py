"""上传体检接入 app.py 的测试(T031/T032 接线):只用 AST 提取的 _upload_check 和替身,不导入 app,不读写真实 data/,不联网。"""
import ast
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import admin_settings, members, upload_api  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TREE = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
HANDLER = next(n for n in TREE.body if isinstance(n, ast.ClassDef) and n.name == "Handler")
METHODS = {n.name: n for n in HANDLER.body if isinstance(n, ast.FunctionDef)}


def build():
    cls = ast.ClassDef(name="H", bases=[], keywords=[], decorator_list=[], body=[METHODS["_upload_check"]])
    ns = dict(members=members)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[])), "<h>", "exec"), ns)
    return ns["H"]


H = build()


class Fake(H):
    def __init__(self, body=b"", local=True, authed=True, header="dashboard", origin=True, length=None, origin_header="http://127.0.0.1:8765"):
        self.headers = {"X-Requested-With": header, "Content-Length": str(len(body) if length is None else length)}
        if origin_header:
            self.headers["Origin"] = origin_header
        self.rfile = io.BytesIO(body)
        self._local, self._auth, self._origin = local, authed, origin
        self.out = None
        self.close_connection = False

    def _local_or_owner(self): return self._local
    def _remote(self): return False
    def _authed(self): return self._auth
    def _origin_ok(self): return self._origin
    def _send(self, code, body, ctype="text/plain"): self.out = (code, body)
    def _json(self, obj, code=200): self.out = (code, obj)


def good_body(n=300):
    import datetime as dt
    import random
    rng = random.Random(1)
    d0 = dt.date(2020, 1, 1)
    return json.dumps({"dates": [(d0 + dt.timedelta(days=i)).isoformat() for i in range(n)],
                       "returns": [rng.gauss(0.0005, 0.01) for _ in range(n)], "n_trials": 2}).encode()


class WiringTests(unittest.TestCase):
    def setUp(self):
        members._hits.clear()

    def run_it(self, on=True, **kw):
        h = Fake(**kw)
        with patch.object(admin_settings, "get_all", return_value={"upload_check_on": on}):
            h._upload_check()
        return h.out

    def test_default_off_is_404(self):
        self.assertEqual(self.run_it(on=False, body=good_body())[0], 404)
        self.assertFalse(admin_settings.DEFAULTS["upload_check_on"])               # 出厂默认关闭
        with patch.object(admin_settings, "get_all", return_value={}):
            self.assertFalse(admin_settings.upload_check_on())                     # 读不到设置也按关闭

    def test_not_local_is_404_even_when_on(self):
        self.assertEqual(self.run_it(body=good_body(), local=False)[0], 404)

    def test_auth_and_headers(self):
        self.assertEqual(self.run_it(body=good_body(), authed=False)[0], 401)
        self.assertEqual(self.run_it(body=good_body(), header="")[0], 403)
        self.assertEqual(self.run_it(body=good_body(), origin=False)[0], 403)
        self.assertEqual(self.run_it(body=good_body(), origin_header="")[0], 403)           # 缺 Origin 头：即使 _origin_ok 放行也要拒绝

    def test_ok(self):
        code, obj = self.run_it(body=good_body())
        self.assertEqual(code, 200)
        self.assertIn("verdict", obj)

    def test_rate_limit(self):
        codes = [self.run_it(body=good_body())[0] for _ in range(8)]
        self.assertEqual(codes[:6], [200] * 6)
        self.assertEqual(codes[6:], [429, 429])

    def test_too_large_and_bad_length(self):
        h = Fake(body=b"x", length=upload_api.MAX_BODY_BYTES + 1)
        with patch.object(admin_settings, "get_all", return_value={"upload_check_on": True}):
            h._upload_check()
        self.assertEqual(h.out[0], 413)
        self.assertTrue(h.close_connection)
        h2 = Fake(body=b"x")
        h2.headers["Content-Length"] = "abc"
        with patch.object(admin_settings, "get_all", return_value={"upload_check_on": True}):
            h2._upload_check()
        self.assertEqual(h2.out[0], 400)                                           # 非法长度：客户端错误并关闭连接，不回显
        self.assertTrue(h2.close_connection)
        self.assertNotIn("abc", json.dumps(h2.out[1]))

    def test_route_registered_in_do_post(self):
        src = ast.get_source_segment((ROOT / "app.py").read_text(encoding="utf-8"), METHODS["do_POST"])
        self.assertIn('"/api/check/upload"', src)
        self.assertIn("self._upload_check()", src)

    def test_settings_validate_accepts_bool_only(self):
        with patch.object(admin_settings, "get_all", return_value=dict(admin_settings.DEFAULTS)):
            self.assertEqual(admin_settings.validate({"upload_check_on": True}), {"upload_check_on": True})
            with self.assertRaises(admin_settings.SettingError):
                admin_settings.validate({"upload_check_on": "yes"})


if __name__ == "__main__":
    unittest.main()
