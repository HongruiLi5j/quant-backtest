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

    def test_range_error_does_not_echo_number(self):
        o = json.loads(body())
        o["returns"][3] = 123456.789
        code, out = api.handle(json.dumps(o).encode(), enabled=True)
        self.assertEqual(code, 400)
        self.assertNotIn("123456", out["error"])

    def test_huge_ints_are_400_not_500(self):
        for field, val in (("returns", None), ("cost_bp", 10 ** 400), ("n_trades", 10 ** 400), ("n_trials", 10 ** 400)):
            o = json.loads(body(400, turnover=[0.1] * 400, cost_bp=8, cost_included=True))
            if field == "returns":
                o["returns"][5] = 10 ** 400
            else:
                o[field] = val
            code, out = api.handle(json.dumps(o).encode(), enabled=True)
            self.assertEqual(code, 400, field)

    def test_internal_errors_are_fixed_500(self):
        real = api.uc.upload_check
        api.uc.upload_check = lambda *a, **k: (_ for _ in ()).throw(ValueError("/Users/secret/path boom"))
        try:
            code, out = api.handle(body(), enabled=True)
        finally:
            api.uc.upload_check = real
        self.assertEqual(code, 500)
        self.assertNotIn("secret", out["error"])
        api.uc.upload_check = lambda *a, **k: {"x": float("nan")}                    # 序列化失败
        try:
            code, out = api.handle(body(), enabled=True)
        finally:
            api.uc.upload_check = real
        self.assertEqual(code, 500)
        self.assertNotIn("nan", out["error"].lower())

    def test_duplicate_keys_rejected(self):
        ok = body()                                                                   # 本身是合法请求
        self.assertEqual(api.handle(ok, enabled=True)[0], 200)
        dup = ok[:-1] + b', "n_trials": 100}'                                         # 末尾追加一个重复的 n_trials
        self.assertEqual(api.handle(dup, enabled=True)[0], 400)

    def test_row_cap_and_busy(self):
        n = api.API_MAX_ROWS + 1
        raw = json.dumps({"dates": ["2019-01-01"] * n, "returns": [0.0] * n, "n_trials": 1}).encode()
        self.assertEqual(api.handle(raw, enabled=True)[0], 413)
        self.assertTrue(api._BUSY.acquire(blocking=False))
        try:
            self.assertEqual(api.handle(body(), enabled=True)[0], 429)
        finally:
            api._BUSY.release()
        self.assertEqual(api.handle(body(), enabled=True)[0], 200)                    # 释放后恢复

    def test_semaphore_released_after_error(self):
        o = json.loads(body())
        o["n_trials"] = 0
        api.handle(json.dumps(o).encode(), enabled=True)
        self.assertEqual(api.handle(body(), enabled=True)[0], 200)

    def _factors(self, n=400):
        import datetime as dt
        import random
        rng = random.Random(7)
        d0 = dt.date(2019, 1, 1)
        return {(d0 + dt.timedelta(days=i)).isoformat(): {"Mkt-RF": rng.gauss(0, 0.01), "SMB": rng.gauss(0, 0.005), "HML": rng.gauss(0, 0.005),
                                                          "Mom": rng.gauss(0, 0.005), "RF": 0.0001} for i in range(n)}

    def test_factor_model_adds_attribution(self):
        code, out = api.handle(body(400, factor_model="ff3"), enabled=True, factor_loader=lambda: self._factors())
        self.assertEqual(code, 200)
        self.assertEqual(out["factor_attribution"]["status"], "ok")
        self.assertEqual(out["factor_attribution"]["model"], "ff3")
        json.dumps(out, allow_nan=False)
        code, out = api.handle(body(), enabled=True)
        self.assertNotIn("factor_attribution", out)                                   # 不要求时不出现

    def test_attribution_uses_net_returns_like_main_report(self):
        """毛收益 + 换手 + 费率 与「等价的净收益」，主报告相同，因子归因也必须相同(Codex T037：曾差 25 个百分点)。"""
        o = json.loads(body(400))
        turn = [1.0] * 400
        gross = dict(o, turnover=turn, cost_bp=50, cost_included=False, factor_model="ff3")
        net = dict(o, returns=[r - 1.0 * 50 / 1e4 for r in o["returns"]], turnover=turn, cost_bp=50, cost_included=True, factor_model="ff3")
        _, a = api.handle(json.dumps(gross).encode(), enabled=True, factor_loader=lambda: self._factors())
        _, b = api.handle(json.dumps(net).encode(), enabled=True, factor_loader=lambda: self._factors())
        self.assertAlmostEqual(a["factor_attribution"]["alpha_annual"], b["factor_attribution"]["alpha_annual"], places=9)
        _, c = api.handle(json.dumps(dict(gross, cost_bp=0)).encode(), enabled=True, factor_loader=lambda: self._factors())
        self.assertGreater(abs(a["factor_attribution"]["alpha_annual"] - c["factor_attribution"]["alpha_annual"]), 0.05)   # 成本确实改变了归因

    def test_factor_model_errors(self):
        for bad in ("capm", 3, ["ff3"], {"a": 1}, ""):
            self.assertEqual(api.handle(body(400, factor_model=bad), enabled=True, factor_loader=lambda: self._factors())[0], 400, bad)
        code, out = api.handle(body(400, factor_model="ff3"), enabled=True, factor_loader=lambda: None)
        self.assertEqual(code, 400)
        self.assertIn("extdata", out["error"])                                        # 没有缓存：提示怎么下载，不联网
        called = []
        api.handle(body(400, factor_model="ff3"), enabled=True, factor_loader=lambda: called.append(1) or None)
        api.handle(body(400), enabled=True, factor_loader=lambda: called.append(2))
        self.assertEqual(called, [1])                                                 # 没要求因子归因就不读因子数据

    def test_few_overlap_days_is_na(self):
        code, out = api.handle(body(400, factor_model="ff3"), enabled=True, factor_loader=lambda: self._factors(100))
        self.assertEqual(code, 200)
        self.assertEqual(out["factor_attribution"]["status"], "na")

    def test_no_app_import(self):
        self.assertNotIn("app", sys.modules)


if __name__ == "__main__":
    unittest.main()
