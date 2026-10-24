"""持仓台账接口处理函数测试(S2):只用合成数据与临时目录,不读写真实 data/。"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.holdings import api
from src.holdings.api import Ctx, handle_add, handle_capital, handle_get, handle_void

TODAY = date(2026, 10, 22)
PRICES = {"600000": ("12.500", "2026-10-21"), "000001": 9.8}

GET_KEYS = {"ok", "as_of", "capital", "summary", "positions", "closed", "trades"}
SUMMARY_KEYS = {"market_value", "total_cost", "floating_pnl", "realized", "dividends",
                "invested_ratio", "missing_price_count"}
POSITION_KEYS = {"code", "name", "shares", "avg_cost", "first_buy", "held_days", "price", "price_date",
                 "market_value", "floating_pnl", "floating_pct", "weight", "realized", "dividends"}
CLOSED_KEYS = {"code", "name", "first_buy", "last_sell", "held_days", "pnl"}
TRADE_KEYS = {"id", "trade_date", "code", "name", "side", "qty", "price", "fee", "fee_estimated",
              "signal_ref", "note", "voided", "created_at"}


def price_fn(code):
    return PRICES.get(code)


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "d" / "holdings.db"
        self.logged = []

    def ctx(self, **kw):
        d = dict(db_path=self.path, trusted_local=True, dashboard_header=True, authed=True,
                 origin_ok=True, last_price_fn=price_fn, today=TODAY, log_fn=self.logged.append)
        d.update(kw)
        return Ctx(**d)

    def add(self, **body):
        base = {"trade_date": "2026-10-10", "code": "600000", "side": "buy", "qty": 1000, "price": "10", "fee": "5"}
        base.update(body)
        return handle_add(self.ctx(), base)

    def err(self, resp):
        status, body = resp
        self.assertIsInstance(body, dict)
        self.assertIn("error", body)
        return status, body["error"]


class AuthMatrix(Base):
    CALLS = (
        lambda c: handle_get(c),
        lambda c: handle_add(c, {"trade_date": "2026-10-10", "code": "600000", "side": "buy",
                                 "qty": 100, "price": "10"}),
        lambda c: handle_void(c, {"id": 1}),
        lambda c: handle_capital(c, {"capital": "100000"}),
    )

    def test_matrix(self):
        self.assertEqual(self.add()[0], 200)  # 库里先有数据,证明拒绝响应与数据无关
        cases = [
            (dict(trusted_local=False), (404, "not found")),
            (dict(trusted_local=False, authed=False, dashboard_header=False), (404, "not found")),
            (dict(authed=False), (401, "unauthorized")),
            (dict(authed=False, dashboard_header=False), (401, "unauthorized")),
        ]
        for kw, want in cases:
            for call in self.CALLS:
                self.assertEqual(call(self.ctx(**kw)), want, kw)
        for call in self.CALLS:  # 缺头:固定 403 与固定错误体
            self.assertEqual(call(self.ctx(dashboard_header=False)),
                             (403, {"error": "HOLDINGS_FORBIDDEN", "message": "请求被拒绝"}))
        for call in self.CALLS[1:]:  # Origin 不对只影响写操作
            self.assertEqual(call(self.ctx(origin_ok=False))[0], 403)
        self.assertEqual(handle_get(self.ctx(origin_ok=False))[0], 200)

    def test_denied_has_no_side_effects_and_is_identical_with_or_without_data(self):
        empty = [call(self.ctx(trusted_local=False)) for call in self.CALLS]
        self.assertFalse(self.path.exists())
        self.add()
        full = [call(self.ctx(trusted_local=False)) for call in self.CALLS]
        self.assertEqual(empty, full)
        with sqlite3.connect(self.path) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM trades").fetchone()[0], 1)

    def test_missing_origin_ok_defaults_to_deny_writes(self):
        c = Ctx(db_path=self.path, trusted_local=True, authed=True, dashboard_header=True, today=TODAY)
        self.assertFalse(c.origin_ok)
        for call in self.CALLS[1:]:
            self.assertEqual(call(c)[0], 403)
        self.assertFalse(self.path.exists())

    def test_denied_never_creates_db(self):
        for kw in (dict(authed=False), dict(dashboard_header=False), dict(origin_ok=False)):
            for call in self.CALLS:
                call(self.ctx(**kw))
            self.assertFalse(self.path.exists(), kw)
            self.assertFalse(self.path.parent.exists(), kw)

    def test_rate_limited_code_is_known(self):
        status, body = api._err("HOLDINGS_RATE_LIMITED")
        self.assertEqual((status, body["error"]), (429, "HOLDINGS_RATE_LIMITED"))

    def test_default_ctx_denies(self):
        for call in self.CALLS:
            self.assertEqual(call(Ctx(db_path=self.path)), (404, "not found"))
        self.assertFalse(self.path.exists())


class EmptyAndGet(Base):
    def test_empty_db_no_file_created(self):
        status, body = handle_get(self.ctx())
        self.assertEqual(status, 200)
        self.assertEqual(set(body), GET_KEYS)
        self.assertEqual((body["positions"], body["closed"], body["trades"]), ([], [], []))
        self.assertIsNone(body["capital"])
        self.assertIsNone(body["summary"]["invested_ratio"])
        self.assertEqual(set(body["summary"]), SUMMARY_KEYS)
        self.assertEqual(body["as_of"], "2026-10-22")
        self.assertFalse(self.path.exists())
        self.assertFalse(self.path.parent.exists())

    def test_get_fields_whitelist_and_values(self):
        handle_capital(self.ctx(), {"capital": "100000"})
        self.assertEqual(self.add(client_id="abcdefgh1")[0], 200)
        self.add(code="000001", price="9", fee="0")
        status, body = handle_get(self.ctx(name_fn=lambda c: "名" + c))
        self.assertEqual(status, 200)
        self.assertEqual(set(body), GET_KEYS)
        self.assertEqual(body["capital"], "100000.00")
        p = {x["code"]: x for x in body["positions"]}
        self.assertEqual(set(p["600000"]), POSITION_KEYS)
        self.assertEqual(p["600000"]["name"], "名600000")
        self.assertEqual(p["600000"]["shares"], 1000)
        self.assertEqual(p["600000"]["avg_cost"], "10.005")
        self.assertEqual(p["600000"]["price"], "12.500")
        self.assertEqual(p["600000"]["price_date"], "2026-10-21")
        self.assertEqual(p["600000"]["held_days"], 12)
        self.assertEqual(p["600000"]["floating_pnl"], "2495.00")
        self.assertEqual(p["000001"]["price_date"], None)
        for t in body["trades"]:
            self.assertEqual(set(t), TRADE_KEYS)
        json.dumps(body)  # 必须可直接序列化(无 Decimal)

    def test_missing_price_is_null_not_zero(self):
        self.add(code="300001", price="20", qty=100)
        _, body = handle_get(self.ctx(last_price_fn=lambda c: None))
        pos = body["positions"][0]
        for k in ("price", "market_value", "floating_pnl", "floating_pct", "weight"):
            self.assertIsNone(pos[k])
        self.assertEqual(body["summary"]["missing_price_count"], 1)
        _, body = handle_get(self.ctx(last_price_fn=lambda c: 1 / 0))  # 价格函数异常也只当缺失
        self.assertIsNone(body["positions"][0]["price"])
        _, body = handle_get(self.ctx(last_price_fn=None))
        self.assertIsNone(body["positions"][0]["price"])

    def test_closed_and_voided_listing(self):
        self.add(code="600000", qty=100)
        self.add(trade_date="2026-10-12", side="sell", qty=100, price="11", fee="5")
        _, body = handle_get(self.ctx())
        self.assertEqual(body["positions"], [])
        self.assertEqual(set(body["closed"][0]), CLOSED_KEYS)
        self.assertEqual(body["closed"][0]["pnl"], "90.00")
        tid = body["trades"][0]["id"]
        handle_void(self.ctx(), {"id": tid})
        _, body = handle_get(self.ctx())
        self.assertEqual(len(body["trades"]), 1)
        _, body = handle_get(self.ctx(), {"include_voided": "1"})
        self.assertEqual(len(body["trades"]), 2)
        _, body = handle_get(self.ctx(), {"include_voided": "1", "trades_limit": "1"})
        self.assertEqual(len(body["trades"]), 1)
        _, body = handle_get(self.ctx(), {"trades_limit": "abc", "include_voided": "x"})  # 非法查询串退回默认
        self.assertEqual(len(body["trades"]), 1)

    def test_name_fn_failure_falls_back_to_code(self):
        self.add()
        _, body = handle_get(self.ctx(name_fn=lambda c: 1 / 0))
        self.assertEqual(body["positions"][0]["name"], "600000")


class AddValidation(Base):
    def test_success_response_whitelist_no_echo(self):
        status, body = self.add(note="备注SECRETNOTE", signal_ref="2026-10-09|ma_cross|600000", client_id="cid-12345")
        self.assertEqual(status, 200)
        self.assertEqual(set(body), {"ok", "duplicate", "trade"})
        self.assertEqual(set(body["trade"]), TRADE_KEYS)
        self.assertFalse(body["duplicate"])
        self.assertFalse(body["trade"]["fee_estimated"])
        self.assertEqual(self.logged, ["holdings_add"])  # 日志只有动作名
        self.assertNotIn("1000", "".join(self.logged))

    def test_estimated_fee_marked(self):
        body = {"trade_date": "2026-10-10", "code": "600000", "side": "buy", "qty": 1000, "price": "10"}
        status, resp = handle_add(self.ctx(), body)
        self.assertEqual(status, 200)
        self.assertTrue(resp["trade"]["fee_estimated"])
        status, resp = handle_add(self.ctx(fee_fn=lambda c, s, a: 7), dict(body, trade_date="2026-10-11"))
        self.assertEqual(resp["trade"]["fee"], "7.00")

    def test_bad_body_structure(self):
        good = {"trade_date": "2026-10-10", "code": "600000", "side": "buy", "qty": 100, "price": "10"}
        cases = [
            ([1, 2], "HOLDINGS_BAD_JSON"), ("not json", "HOLDINGS_BAD_JSON"), (None, "HOLDINGS_BAD_JSON"),
            ("[]", "HOLDINGS_BAD_JSON"), (b"\xff\xfe", "HOLDINGS_BAD_JSON"),
            (dict(good, extra=1), "HOLDINGS_UNKNOWN_FIELD"),
            (dict(good, voided=1), "HOLDINGS_UNKNOWN_FIELD"),
            ({k: v for k, v in good.items() if k != "qty"}, "HOLDINGS_MISSING_FIELD"),
            ({}, "HOLDINGS_MISSING_FIELD"),
            (dict(good, note=["x"]), "HOLDINGS_BAD_NOTE"),
            (dict(good, price={"a": 1}), "HOLDINGS_BAD_PRICE"),
            (dict(good, note="x" * 1001), "HOLDINGS_BAD_NOTE"),
            (dict(good, note="x" * 201), "HOLDINGS_BAD_NOTE"),
            (dict(good, code="6" * 65), "HOLDINGS_BAD_CODE"),
            (dict(good, client_id="a" * 65), "HOLDINGS_BAD_CLIENT_ID"),
            (json.dumps(good) + " " * 17000, "HOLDINGS_TOO_LARGE"),
        ]
        for body, want in cases:
            status, code = self.err(handle_add(self.ctx(), body))
            self.assertEqual(code, want, body if len(str(body)) < 200 else "big")
            self.assertEqual(status, 413 if want == "HOLDINGS_TOO_LARGE" else 400)
        self.assertFalse(self.path.exists())  # 校验失败不建库

    def test_raw_json_string_accepted(self):
        status, _ = handle_add(self.ctx(), json.dumps(
            {"trade_date": "2026-10-10", "code": "600000", "side": "buy", "qty": 100, "price": "10"}))
        self.assertEqual(status, 200)

    def test_field_validation_matrix(self):
        cases = [
            (dict(trade_date="2026/10/10"), "HOLDINGS_BAD_DATE"),
            (dict(trade_date="2026-13-01"), "HOLDINGS_BAD_DATE"),
            (dict(trade_date="1990-01-01"), "HOLDINGS_BAD_DATE"),
            (dict(trade_date=20261010), "HOLDINGS_BAD_DATE"),
            (dict(trade_date="2026-10-23"), "HOLDINGS_FUTURE_DATE"),
            (dict(code="830001"), "HOLDINGS_BAD_CODE"),
            (dict(code="60000"), "HOLDINGS_BAD_CODE"),
            (dict(code=600000), "HOLDINGS_BAD_CODE"),
            (dict(side="BUY"), "HOLDINGS_BAD_SIDE"),
            (dict(side="short"), "HOLDINGS_BAD_SIDE"),
            (dict(qty=0), "HOLDINGS_BAD_QTY"),
            (dict(qty=-100), "HOLDINGS_BAD_QTY"),
            (dict(qty=True), "HOLDINGS_BAD_QTY"),
            (dict(qty=100.5), "HOLDINGS_BAD_QTY"),
            (dict(qty=10 ** 9), "HOLDINGS_BAD_QTY"),
            (dict(qty=150), "HOLDINGS_BAD_LOT"),
            (dict(code="688001", qty=100), "HOLDINGS_BAD_LOT"),
            (dict(price=0), "HOLDINGS_BAD_PRICE"),
            (dict(price="-1"), "HOLDINGS_BAD_PRICE"),
            (dict(price="1e3"), "HOLDINGS_BAD_PRICE"),
            (dict(price="NaN"), "HOLDINGS_BAD_PRICE"),
            (dict(price=float("inf")), "HOLDINGS_BAD_PRICE"),
            (dict(price="10.0001"), "HOLDINGS_BAD_PRICE"),
            (dict(price=None), "HOLDINGS_BAD_PRICE"),
            (dict(price=True), "HOLDINGS_BAD_PRICE"),
            (dict(fee="-1"), "HOLDINGS_BAD_FEE"),
            (dict(fee="1.234"), "HOLDINGS_BAD_FEE"),
            (dict(fee="999999"), "HOLDINGS_BAD_FEE"),
            (dict(note=5), "HOLDINGS_BAD_NOTE"),
            (dict(signal_ref="2026-10-09|ma|000001"), "HOLDINGS_BAD_SIGNAL_REF"),
            (dict(signal_ref="bad"), "HOLDINGS_BAD_SIGNAL_REF"),
            (dict(client_id="short"), "HOLDINGS_BAD_CLIENT_ID"),
        ]
        for patch, want in cases:
            status, code = self.err(self.add(**patch))
            self.assertEqual((status, code), (400, want), patch)
        self.assertFalse(self.path.exists())

    def test_error_body_is_fixed_and_leaks_nothing(self):
        status, body = self.add(qty=987654321, price="123.456", code="600519", note="私密备注")
        text = json.dumps(body, ensure_ascii=False)
        for secret in ("987654321", "123.456", "600519", "私密备注"):
            self.assertNotIn(secret, text)
        self.assertLessEqual(set(body), {"error", "message", "field"})
        self.assertIn(body["field"], api._FIELD_ERRORS)

    def test_bonus_dividend(self):
        self.add()
        self.assertEqual(self.add(trade_date="2026-10-11", side="bonus", qty=500, price=None, fee=None)[0], 200)
        self.assertEqual(self.err(self.add(trade_date="2026-10-11", side="bonus", qty=500, price="1"))[1],
                         "HOLDINGS_BAD_PRICE")
        self.assertEqual(self.add(trade_date="2026-10-12", side="dividend", qty=1500, price="0.05", fee="0")[0], 200)
        self.assertEqual(self.err(self.add(trade_date="2026-10-12", side="dividend", qty=9000, price="0.05"))[1],
                         "HOLDINGS_NO_POSITION")
        self.assertEqual(self.err(self.add(code="000001", side="bonus", qty=10, price=None, fee=None))[1],
                         "HOLDINGS_NO_POSITION")


class OversellIdempotencyVoid(Base):
    def test_oversell(self):
        self.add(qty=1000)
        self.assertEqual(self.err(self.add(trade_date="2026-10-11", side="sell", qty=1001))[1], "HOLDINGS_OVERSELL")
        self.assertEqual(self.add(trade_date="2026-10-11", side="sell", qty=999)[0], 200)
        self.assertEqual(self.err(self.add(trade_date="2026-10-12", side="sell", qty=2))[1], "HOLDINGS_OVERSELL")
        # 补录更早日期的卖出会让历史上某一刻超卖
        self.assertEqual(self.err(self.add(trade_date="2026-10-09", side="sell", qty=1))[1], "HOLDINGS_OVERSELL")
        self.assertEqual(self.err(self.add(trade_date="2026-10-01", code="000001", side="sell", qty=1))[1],
                         "HOLDINGS_OVERSELL")
        _, body = handle_get(self.ctx())
        self.assertEqual(body["positions"][0]["shares"], 1)
        self.assertEqual(len(body["trades"]), 2)  # 被拒绝的没有入账

    def test_idempotent_client_id(self):
        s1, b1 = self.add(client_id="req-0001-abc")
        s2, b2 = self.add(client_id="req-0001-abc")
        self.assertEqual((s1, s2), (200, 200))
        self.assertFalse(b1["duplicate"])
        self.assertTrue(b2["duplicate"])
        self.assertEqual(b1["trade"]["id"], b2["trade"]["id"])
        _, body = handle_get(self.ctx())
        self.assertEqual(len(body["trades"]), 1)
        self.assertEqual(body["positions"][0]["shares"], 1000)
        status, code = self.err(self.add(client_id="req-0001-abc", qty=2000))
        self.assertEqual((status, code), (409, "HOLDINGS_CLIENT_ID_CONFLICT"))

    def test_void_flow(self):
        _, b = self.add()
        tid = b["trade"]["id"]
        self.assertEqual(handle_void(self.ctx(), {"id": tid}), (200, {"ok": True}))
        self.assertEqual(self.logged[-1], "holdings_void")
        self.assertEqual(self.err(handle_void(self.ctx(), {"id": tid})), (400, "HOLDINGS_ALREADY_VOIDED"))
        self.assertEqual(self.err(handle_void(self.ctx(), {"id": 999})), (404, "HOLDINGS_TRADE_NOT_FOUND"))
        _, body = handle_get(self.ctx())
        self.assertEqual(body["positions"], [])

    def test_void_breaks_history(self):
        _, buy = self.add()
        self.add(trade_date="2026-10-11", side="sell", qty=500)
        self.assertEqual(self.err(handle_void(self.ctx(), {"id": buy["trade"]["id"]})),
                         (400, "HOLDINGS_VOID_BREAKS_HISTORY"))

    def test_void_validation(self):
        for body, want in (({}, "HOLDINGS_MISSING_FIELD"), ({"id": 1, "x": 1}, "HOLDINGS_UNKNOWN_FIELD"),
                           ({"id": "1"}, "HOLDINGS_BAD_ID"), ({"id": True}, "HOLDINGS_BAD_ID"),
                           ({"id": 0}, "HOLDINGS_BAD_ID"), ({"id": -3}, "HOLDINGS_BAD_ID"),
                           ({"id": 1.0}, "HOLDINGS_BAD_ID"), ({"id": [1]}, "HOLDINGS_BAD_ID"),
                           ([1], "HOLDINGS_BAD_JSON")):
            self.assertEqual(self.err(handle_void(self.ctx(), body))[1], want, body)
        self.assertFalse(self.path.exists())  # 空库撤销不建库(校验阶段拦下)

    def test_void_on_empty_db_not_found_no_file(self):
        self.assertEqual(self.err(handle_void(self.ctx(), {"id": 1})), (404, "HOLDINGS_TRADE_NOT_FOUND"))
        self.assertFalse(self.path.exists())
        self.assertFalse(self.path.parent.exists())


class Capital(Base):
    def test_set_and_get(self):
        self.assertEqual(handle_capital(self.ctx(), {"capital": "123456.5"}), (200, {"ok": True, "capital": "123456.50"}))
        self.assertEqual(self.logged, ["holdings_capital"])
        self.assertEqual(handle_capital(self.ctx(), {"capital": 200000})[1]["capital"], "200000.00")
        self.add()
        _, body = handle_get(self.ctx())
        self.assertEqual(body["capital"], "200000.00")
        self.assertEqual(body["positions"][0]["weight"], "0.0625")

    def test_validation(self):
        for body, want in (({"capital": 0}, "HOLDINGS_BAD_CAPITAL"), ({"capital": "-5"}, "HOLDINGS_BAD_CAPITAL"),
                           ({"capital": "1.234"}, "HOLDINGS_BAD_CAPITAL"), ({"capital": "2e9"}, "HOLDINGS_BAD_CAPITAL"),
                           ({"capital": 1_000_000_001}, "HOLDINGS_BAD_CAPITAL"),
                           ({"capital": True}, "HOLDINGS_BAD_CAPITAL"), ({"capital": None}, "HOLDINGS_BAD_CAPITAL"),
                           ({"capital": "x" * 65}, "HOLDINGS_BAD_CAPITAL"),
                           ({}, "HOLDINGS_MISSING_FIELD"), ({"capital": 1, "a": 2}, "HOLDINGS_UNKNOWN_FIELD")):
            status, code = self.err(handle_capital(self.ctx(), body))
            self.assertEqual((status, code), (400, want), body)
        self.assertFalse(self.path.exists())


class Robustness(Base):
    def test_unsafe_store_symlink(self):
        real = Path(self._tmp.name) / "real.db"
        real.write_bytes(b"")
        self.path.parent.mkdir(parents=True)
        os.symlink(real, self.path)
        self.assertEqual(self.err(handle_get(self.ctx())), (500, "HOLDINGS_STORE_UNSAFE"))
        self.assertEqual(self.err(self.add()), (500, "HOLDINGS_STORE_UNSAFE"))

    def test_unexpected_exception_returns_fixed_error(self):
        secret = "SECRET-12345.678"

        def boom(code, side, amount):
            raise RuntimeError(secret)

        status, body = handle_add(self.ctx(fee_fn=boom), {
            "trade_date": "2026-10-10", "code": "600000", "side": "buy", "qty": 100, "price": "10"})
        self.assertEqual((status, body["error"]), (500, "HOLDINGS_INTERNAL"))
        self.assertNotIn(secret, json.dumps(body))
        status, body = handle_get(self.ctx(today="garbage"))
        self.assertEqual(status, 500)
        self.assertEqual(body, {"error": "HOLDINGS_INTERNAL", "message": "内部错误"})

    def test_log_fn_failure_ignored_and_only_action_names(self):
        def bad_log(_):
            raise RuntimeError("x")
        status, _ = handle_get(self.ctx(log_fn=bad_log))
        self.assertEqual(status, 200)
        self.add()
        handle_get(self.ctx())
        handle_capital(self.ctx(), {"capital": "100000"})
        self.assertTrue(set(self.logged) <= {"holdings_add", "holdings_get", "holdings_capital", "holdings_void"})

    def test_audit_table_has_no_amounts(self):
        self.add(qty=1234500 // 100 * 100, price="98.765")
        with sqlite3.connect(self.path) as c:
            cols = [r[1] for r in c.execute("PRAGMA table_info(audit)")]
            rows = c.execute("SELECT * FROM audit").fetchall()
        self.assertEqual(set(cols), {"id", "ts", "action", "trade_id"})
        self.assertNotIn("98.765", json.dumps(rows))

    def test_import_creates_no_file_and_module_hygiene(self):
        import ast
        tree = ast.parse((ROOT / "src" / "holdings" / "api.py").read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                mods = [a.name.split(".")[0] for a in n.names]
            elif isinstance(n, ast.ImportFrom) and n.level == 0:
                mods = [(n.module or "").split(".")[0]]
            else:
                mods = []
            for m in mods:
                self.assertNotIn(m, {"logging", "urllib", "requests", "socket", "http", "subprocess", "os"})
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
                self.assertNotIn(n.func.id, {"print", "open", "eval", "exec"})
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
