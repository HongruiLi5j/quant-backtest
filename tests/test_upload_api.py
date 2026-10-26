"""upload_api 测试：纯函数，合成数据，不读 data/，不联网，不导入 app。"""
import datetime as dt
import json
import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import upload_api as api  # noqa: E402


def body(n=400, **extra):
    rng = random.Random(1)
    d0 = dt.date(2019, 1, 1)
    o = {"dates": [(d0 + dt.timedelta(days=i)).isoformat() for i in range(n)], "returns": [rng.gauss(0.0005, 0.01) for _ in range(n)], "n_trials": 3}
    o.update(extra)
    return json.dumps(o).encode()


class ApiTests(unittest.TestCase):
    def test_disabled_is_404(self):
        for flag in (False, None, 1, "yes"):
            self.assertEqual(api.handle(body(), enabled=flag)[0], 404)
        self.assertEqual(api.handle(body())[0], 404)                               # 默认关闭

    def test_ok(self):
        code, out = api.handle(body(), enabled=True)
        self.assertEqual(code, 200)
        self.assertIn("verdict", out)
        json.dumps(out, allow_nan=False)

    def test_size_limit(self):
        self.assertEqual(api.handle(b" " * (api.MAX_BODY_BYTES + 1), enabled=True)[0], 413)

    def test_bad_json_and_types(self):
        for raw in (b"", b"not json", b"[]", b"123", b"\xff\xfe", b'{"dates": NaN}', b'{"dates": [1], "returns": [Infinity], "n_trials": 1}'):
            code, out = api.handle(raw, enabled=True)
            self.assertEqual(code, 400, raw)
            self.assertIn("error", out)
        self.assertEqual(api.handle("str-not-bytes", enabled=True)[0], 400)

    def test_unknown_and_missing_keys(self):
        code, out = api.handle(body(extra_secret=1), enabled=True)
        self.assertEqual(code, 400)
        self.assertNotIn("extra_secret", out["error"])                              # 不回显字段名
        o = json.loads(body())
        del o["n_trials"]
        code, out = api.handle(json.dumps(o).encode(), enabled=True)
        self.assertEqual(code, 400)
        self.assertIn("n_trials", out["error"])

    def test_validation_errors_are_400(self):
        o = json.loads(body())
        o["returns"][3] = "=1+1"
        code, out = api.handle(json.dumps(o).encode(), enabled=True)
        self.assertEqual(code, 400)
        self.assertNotIn("=1+1", out["error"])                                      # 不回显用户内容原文

    def test_deep_nesting_does_not_crash(self):
        deep = b"[" * 100000 + b"]" * 100000
        self.assertEqual(api.handle(deep, enabled=True)[0], 400)

    def test_optional_fields_pass_through(self):
        n = 400
        extra = {"benchmark": [0.0002] * n, "n_trades": 50, "turnover": [0.1] * n, "cost_bp": 8, "cost_included": True}
        code, out = api.handle(body(n, **extra), enabled=True)
        self.assertEqual(code, 200)
        self.assertEqual(out["upload"]["missing_inputs"], [])

    def test_no_app_import(self):
        self.assertNotIn("app", sys.modules)


if __name__ == "__main__":
    unittest.main()
