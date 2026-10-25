"""持仓台账核心测试(S1):只用合成数据与临时目录,不读写真实 data/。"""
import ast
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from decimal import Decimal as D
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.holdings import HoldingsError, HoldingsStore, calc, store as store_mod

TODAY = date(2026, 10, 22)


def no_fee(code, side, amount):
    return D(0)


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.path = self.dir / "sub" / "holdings.db"
        self.s = HoldingsStore(self.path)

    def add(self, d, code, side, qty, price, fee=None, **kw):
        return self.s.add_trade(trade_date=d, code=code, side=side, qty=qty, price=price,
                                fee=fee, today=TODAY, **kw)["trade"]

    def code_of(self, fn):
        with self.assertRaises(HoldingsError) as cm:
            fn()
        return cm.exception.code

    def pos(self, price=None, capital=None):
        fn = (lambda c: (D(price), "2026-10-21")) if price is not None else (lambda c: None)
        return calc.positions(self.s.load_trades(), fn, TODAY, capital)


class TestCalcExample(Base):
    """设计文档 2.4 算例,全部可手算。"""

    def build(self):
        self.add("2026-10-01", "600000", "buy", 1000, "10.000", "5.00")
        self.add("2026-10-05", "600000", "buy", 500, "11.000", "5.00")
        self.add("2026-10-10", "600000", "sell", 600, "12.000", "6.50")

    def test_weighted_average_cost(self):
        self.add("2026-10-01", "600000", "buy", 1000, "10.000", "5.00")
        self.assertEqual(self.pos()["positions"][0]["avg_cost"], D("10.005"))
        self.add("2026-10-05", "600000", "buy", 500, "11.000", "5.00")
        p = self.pos()["positions"][0]
        self.assertEqual((p["shares"], p["cost"], p["avg_cost"]), (1500, D("15510.00"), D("10.340")))

    def test_sell_keeps_avg_and_realizes(self):
        self.build()
        r = self.pos(price="11.50", capital="100000")
        p = r["positions"][0]
        self.assertEqual(p["shares"], 900)
        self.assertEqual(p["cost"], D("9306.00"))
        self.assertEqual(p["avg_cost"], D("10.340"))
        self.assertEqual(p["realized"], D("989.50"))
        self.assertEqual(p["market_value"], D("10350.00"))
        self.assertEqual(p["floating_pnl"], D("1044.00"))
        self.assertEqual(p["floating_pct"], D("0.1122"))
        self.assertEqual(p["weight"], D("0.1035"))
        self.assertEqual(p["first_buy"], "2026-10-01")
        self.assertEqual(p["held_days"], 21)
        self.assertEqual(r["summary"]["realized"], D("989.50"))
        self.assertEqual(r["summary"]["invested_ratio"], D("0.1035"))

    def test_bonus_dilutes_avg(self):
        self.build()
        self.add("2026-10-12", "600000", "bonus", 450, None)
        p = self.pos()["positions"][0]
        self.assertEqual((p["shares"], p["cost"], p["avg_cost"]), (1350, D("9306.00"), D("6.893")))

    def test_dividend_not_in_cost(self):
        self.build()
        self.add("2026-10-12", "600000", "dividend", 900, "0.200", "10.00")
        r = self.pos()
        self.assertEqual(r["positions"][0]["cost"], D("9306.00"))
        self.assertEqual(r["summary"]["dividends"], D("170.00"))

    def test_missing_price_is_none_not_zero(self):
        self.build()
        r = self.pos(capital="100000")
        p = r["positions"][0]
        for k in ("price", "market_value", "floating_pnl", "floating_pct", "weight"):
            self.assertIsNone(p[k])
        self.assertEqual(r["summary"]["missing_price_count"], 1)

    def test_bad_price_fn_treated_as_missing(self):
        self.build()
        for fn in (lambda c: 0, lambda c: -1, lambda c: float("nan"), lambda c: "abc",
                   lambda c: (_ for _ in ()).throw(ValueError("x"))):
            r = calc.positions(self.s.load_trades(), fn, TODAY, 100000)
            self.assertIsNone(r["positions"][0]["market_value"])

    def test_no_capital_gives_no_weight(self):
        self.build()
        self.assertIsNone(self.pos(price="11")["positions"][0]["weight"])

    def test_decimal_no_float_drift(self):
        self.add("2026-10-01", "600000", "buy", 100, "0.100", "0.10")
        self.add("2026-10-02", "600000", "buy", 200, "0.200", "0.20")
        p = self.pos()["positions"][0]
        # 成本 = 10.10 + 40.20 = 50.30,精确
        self.assertEqual(p["cost"], D("50.30"))
        self.assertEqual(self.s.load_trades()[0]["price_milli"], 100)

    def test_default_fee_estimate_injectable(self):
        t = self.s.add_trade(trade_date="2026-10-01", code="600000", side="buy", qty=100, price="10",
                             today=TODAY, fee_fn=lambda c, s, a: D("1.23"))["trade"]
        self.assertEqual((t["fee"], t["fee_estimated"]), (D("1.23"), True))
        t = self.s.add_trade(trade_date="2026-10-02", code="600000", side="buy", qty=100, price="10",
                             today=TODAY)["trade"]
        self.assertTrue(t["fee_estimated"])
        self.assertGreaterEqual(t["fee"], D("5.00"))  # 默认估算:佣金最低 5 元
        t = self.add("2026-10-03", "600000", "buy", 100, "10", "0")
        self.assertFalse(t["fee_estimated"])

    def test_blank_fee_means_estimate(self):
        """T021:页面提示"留空将按费率估算"，所以空串/纯空格要和不传一样走估算，而不是报"费用无效"。"""
        for i, blank in enumerate(("", " ", "   ", "\t", "　")):
            t = self.add(f"2026-10-{i + 1:02d}", "600000", "buy", 100, "10", blank, fee_fn=lambda c, s, a: D("1.23"))
            self.assertEqual((t["fee"], t["fee_estimated"]), (D("1.23"), True), repr(blank))

    def test_blank_fee_estimate_uses_sell_stamp_tax(self):
        self.add("2026-10-01", "600000", "buy", 1000, "10", "0")
        buy = self.add("2026-10-02", "600000", "buy", 1000, "10", "")
        sell = self.add("2026-10-03", "600000", "sell", 1000, "10", "  ")
        self.assertTrue(buy["fee_estimated"] and sell["fee_estimated"])
        self.assertGreater(sell["fee"], buy["fee"])      # 卖出多一笔印花税，买入没有

    def test_bad_fee_still_rejected_after_blank_fix(self):
        for bad in ("abc", "-1", "1e3", "5,0", "0.001"):
            self.assertEqual(self.code_of(lambda b=bad: self.add("2026-10-01", "600000", "buy", 100, "10", b)),
                             "HOLDINGS_BAD_FEE", bad)

    def test_blank_fee_bonus_is_zero(self):
        self.add("2026-10-01", "600000", "buy", 100, "10", "0")
        t = self.s.add_trade(trade_date="2026-10-02", code="600000", side="bonus", qty=10, price=None, fee="",
                             today=TODAY)["trade"]
        self.assertEqual(t["fee"], D("0"))

    def test_600531_pnl_uses_real_price_not_hfq(self):
        """T021 回归：600531 买入 1200 股、成本 15.465，现价必须是真实收盘价 10.1，而不是后复权的 75.694。"""
        self.add("2026-10-20", "600531", "buy", 1200, "15.465", "0")
        real = self.pos(price="10.1")["positions"][0]
        self.assertEqual(real["price"], D("10.100"))
        self.assertEqual(real["floating_pnl"], D("1200") * (D("10.1") - D("15.465")))
        self.assertLess(real["floating_pnl"], 0)
        hfq = self.pos(price="75.694")["positions"][0]             # 用后复权价算出来的是虚假的 +389%
        self.assertGreater(hfq["floating_pct"], D("3.8"))
        self.assertLess(real["floating_pct"], 0)


class TestRounds(Base):
    def test_sell_all_moves_to_closed_and_rebuy_resets_first_buy(self):
        self.add("2026-09-01", "600000", "buy", 100, "10", "0")
        self.add("2026-09-10", "600000", "sell", 100, "12", "0")
        r = self.pos(price="11")
        self.assertEqual(r["positions"], [])
        self.assertEqual(len(r["closed"]), 1)
        c = r["closed"][0]
        self.assertEqual((c["first_buy"], c["last_sell"], c["held_days"], c["pnl"]),
                         ("2026-09-01", "2026-09-10", 9, D("200.00")))
        self.add("2026-10-15", "600000", "buy", 200, "9", "0")
        r = self.pos(price="11")
        p = r["positions"][0]
        self.assertEqual((p["first_buy"], p["held_days"], p["avg_cost"]), ("2026-10-15", 7, D("9.000")))
        self.assertEqual(p["realized"], D("200.00"))  # 历史各轮已实现保留
        self.assertEqual(len(r["closed"]), 1)

    def test_odd_lot_sell(self):
        self.add("2026-10-01", "600000", "buy", 300, "10", "0")
        self.add("2026-10-02", "600000", "sell", 37, "11", "0")
        p = self.pos()["positions"][0]
        self.assertEqual(p["shares"], 263)
        self.add("2026-10-03", "600000", "sell", 263, "11", "0")
        self.assertEqual(self.pos()["positions"], [])

    def test_positions_sorted_by_weight_desc(self):
        self.add("2026-10-01", "600000", "buy", 100, "10", "0")
        self.add("2026-10-01", "000001", "buy", 1000, "10", "0")
        r = self.pos(price="10", capital="100000")
        self.assertEqual([p["code"] for p in r["positions"]], ["000001", "600000"])


class TestReplayChecks(Base):
    def test_oversell_rejected_and_nothing_written(self):
        self.add("2026-10-01", "600000", "buy", 100, "10", "0")
        self.assertEqual(self.code_of(lambda: self.add("2026-10-02", "600000", "sell", 101, "10", "0")),
                         "HOLDINGS_OVERSELL")
        self.assertEqual(len(self.s.load_trades()), 1)

    def test_sell_without_position_rejected(self):
        self.assertEqual(self.code_of(lambda: self.add("2026-10-02", "600000", "sell", 100, "10", "0")),
                         "HOLDINGS_OVERSELL")

    def test_backfill_earlier_sell_that_breaks_later_is_rejected(self):
        self.add("2026-10-01", "600000", "buy", 100, "10", "0")
        self.add("2026-10-05", "600000", "sell", 100, "10", "0")
        # 在 10-05 之前再补一笔卖出 → 10-05 的卖出会超卖
        self.assertEqual(self.code_of(lambda: self.add("2026-10-03", "600000", "sell", 50, "10", "0")),
                         "HOLDINGS_OVERSELL")

    def test_backfill_earlier_buy_changes_first_buy(self):
        self.add("2026-10-05", "600000", "buy", 100, "10", "0")
        self.add("2026-10-01", "600000", "buy", 100, "10", "0")
        self.assertEqual(self.pos()["positions"][0]["first_buy"], "2026-10-01")

    def test_bonus_dividend_need_position(self):
        self.assertEqual(self.code_of(lambda: self.add("2026-10-01", "600000", "bonus", 10, None)),
                         "HOLDINGS_NO_POSITION")
        self.add("2026-10-01", "600000", "buy", 100, "10", "0")
        self.assertEqual(self.code_of(lambda: self.add("2026-10-02", "600000", "dividend", 101, "0.1")),
                         "HOLDINGS_NO_POSITION")

    def test_void_recompute(self):
        a = self.add("2026-10-01", "600000", "buy", 100, "10", "0")
        b = self.add("2026-10-03", "600000", "buy", 100, "20", "0")
        self.assertEqual(self.pos()["positions"][0]["avg_cost"], D("15.000"))
        self.s.void_trade(b["id"])
        p = self.pos()["positions"][0]
        self.assertEqual((p["shares"], p["avg_cost"]), (100, D("10.000")))
        # 软删除:记录仍在,标记为已撤销
        self.assertEqual(len(self.s.list_trades()), 1)
        allr = self.s.list_trades(include_voided=True)
        self.assertEqual(len(allr), 2)
        self.assertTrue(next(r for r in allr if r["id"] == b["id"])["voided"])
        # 撤销第一笔后首次买入日重算
        self.s.void_trade(a["id"])
        self.assertEqual(self.pos()["positions"], [])

    def test_void_breaking_history_rejected(self):
        a = self.add("2026-10-01", "600000", "buy", 100, "10", "0")
        self.add("2026-10-02", "600000", "sell", 100, "10", "0")
        self.assertEqual(self.code_of(lambda: self.s.void_trade(a["id"])), "HOLDINGS_VOID_BREAKS_HISTORY")
        self.assertFalse(self.s.list_trades(include_voided=True)[-1]["voided"])

    def test_void_errors(self):
        a = self.add("2026-10-01", "600000", "buy", 100, "10", "0")
        self.s.void_trade(a["id"])
        self.assertEqual(self.code_of(lambda: self.s.void_trade(a["id"])), "HOLDINGS_ALREADY_VOIDED")
        self.assertEqual(self.code_of(lambda: self.s.void_trade(999)), "HOLDINGS_TRADE_NOT_FOUND")
        for bad in (0, -1, "1", 1.5, True, None):
            self.assertEqual(self.code_of(lambda: self.s.void_trade(bad)), "HOLDINGS_BAD_ID")

    def test_voided_buy_does_not_count_for_oversell(self):
        a = self.add("2026-10-01", "600000", "buy", 100, "10", "0")
        self.s.void_trade(a["id"])
        self.assertEqual(self.code_of(lambda: self.add("2026-10-02", "600000", "sell", 100, "10", "0")),
                         "HOLDINGS_OVERSELL")


class TestValidation(Base):
    def err(self, **over):
        kw = dict(trade_date="2026-10-01", code="600000", side="buy", qty=100, price="10", fee="0")
        kw.update(over)
        return self.code_of(lambda: self.s.add_trade(today=TODAY, **kw))

    def test_matrix(self):
        cases = [
            (dict(code="60000"), "HOLDINGS_BAD_CODE"),
            (dict(code="6000000"), "HOLDINGS_BAD_CODE"),
            (dict(code="abcdef"), "HOLDINGS_BAD_CODE"),
            (dict(code=600000), "HOLDINGS_BAD_CODE"),
            (dict(code="830000"), "HOLDINGS_BAD_CODE"),   # 北交所
            (dict(code="920000"), "HOLDINGS_BAD_CODE"),
            (dict(code="400000"), "HOLDINGS_BAD_CODE"),
            (dict(side="short"), "HOLDINGS_BAD_SIDE"),
            (dict(side=None), "HOLDINGS_BAD_SIDE"),
            (dict(qty=0), "HOLDINGS_BAD_QTY"),
            (dict(qty=-100), "HOLDINGS_BAD_QTY"),
            (dict(qty=100.5), "HOLDINGS_BAD_QTY"),
            (dict(qty=True), "HOLDINGS_BAD_QTY"),
            (dict(qty="1e2"), "HOLDINGS_BAD_QTY"),
            (dict(qty=100_000_100), "HOLDINGS_BAD_QTY"),
            (dict(qty=150), "HOLDINGS_BAD_LOT"),
            (dict(code="688001", qty=100), "HOLDINGS_BAD_LOT"),   # 科创板至少 200
            (dict(price="0"), "HOLDINGS_BAD_PRICE"),
            (dict(price="-1"), "HOLDINGS_BAD_PRICE"),
            (dict(price="1e1"), "HOLDINGS_BAD_PRICE"),
            (dict(price="NaN"), "HOLDINGS_BAD_PRICE"),
            (dict(price=float("inf")), "HOLDINGS_BAD_PRICE"),
            (dict(price="10.0001"), "HOLDINGS_BAD_PRICE"),
            (dict(price="100001"), "HOLDINGS_BAD_PRICE"),
            (dict(price=None), "HOLDINGS_BAD_PRICE"),
            (dict(price=True), "HOLDINGS_BAD_PRICE"),
            (dict(fee="-1"), "HOLDINGS_BAD_FEE"),
            (dict(fee="0.001"), "HOLDINGS_BAD_FEE"),
            (dict(fee="abc"), "HOLDINGS_BAD_FEE"),
            (dict(fee="1001"), "HOLDINGS_BAD_FEE"),  # 大于成交金额 1000
            (dict(trade_date="2026-13-01"), "HOLDINGS_BAD_DATE"),
            (dict(trade_date="2026-02-30"), "HOLDINGS_BAD_DATE"),
            (dict(trade_date="20261001"), "HOLDINGS_BAD_DATE"),
            (dict(trade_date="1990-12-18"), "HOLDINGS_BAD_DATE"),
            (dict(trade_date=None), "HOLDINGS_BAD_DATE"),
            (dict(trade_date="2026-10-23"), "HOLDINGS_FUTURE_DATE"),
            (dict(note="x" * 201), "HOLDINGS_BAD_NOTE"),
            (dict(note=5), "HOLDINGS_BAD_NOTE"),
            (dict(signal_ref="bad"), "HOLDINGS_BAD_SIGNAL_REF"),
            (dict(signal_ref="2026-10-01|ma_cross|000001"), "HOLDINGS_BAD_SIGNAL_REF"),  # 代码不一致
            (dict(client_id="short"), "HOLDINGS_BAD_CLIENT_ID"),
            (dict(client_id="a b c d e f g h"), "HOLDINGS_BAD_CLIENT_ID"),
        ]
        for over, code in cases:
            with self.subTest(over=over):
                self.assertEqual(self.err(**over), code)
        # 校验失败不得创建任何文件
        self.assertFalse(self.path.exists())

    def test_today_is_inclusive_and_valid_inputs(self):
        self.add("2026-10-22", "600000", "buy", 100, "10", "0")
        self.add("2026-10-22", "688001", "buy", 201, 10.5, 0)      # 科创板 200 之后任意整数
        self.add("2026-10-22", "159915", "buy", 100, "1.234", "0")
        self.add("2026-10-22", "300750", "buy", "300", "10", "1.5")
        self.add("2026-10-22", "510300", "buy", 100, 4, 0, signal_ref="2026-10-21|ma_cross|510300",
                 note="a\x00b\nc")
        self.assertEqual(self.s.list_trades()[0]["note"], "abc")

    def test_bonus_price_must_be_zero(self):
        self.add("2026-10-01", "600000", "buy", 100, "10", "0")
        self.assertEqual(self.code_of(lambda: self.add("2026-10-02", "600000", "bonus", 10, "1")),
                         "HOLDINGS_BAD_PRICE")
        self.assertEqual(self.code_of(lambda: self.add("2026-10-02", "600000", "bonus", 10, None, fee="1")),
                         "HOLDINGS_BAD_FEE")
        self.add("2026-10-02", "600000", "bonus", 10, "0")

    def test_capital_validation(self):
        for bad in (0, -5, "abc", "1e5", 1_000_000_001, "100.001", None, True):
            self.assertEqual(self.code_of(lambda: self.s.set_capital(bad)), "HOLDINGS_BAD_CAPITAL")
        self.assertFalse(self.path.exists())
        self.assertIsNone(self.s.get_capital())
        self.s.set_capital("100000.50")
        self.assertEqual(self.s.get_capital(), D("100000.50"))
        self.s.set_capital(200000)
        self.assertEqual(self.s.get_capital(), D("200000.00"))

    def test_sql_injection_is_inert(self):
        self.add("2026-10-01", "600000", "buy", 100, "10", "0", note="'); DROP TABLE trades;--")
        self.assertEqual(len(self.s.list_trades()), 1)
        self.assertEqual(self.s.list_trades()[0]["note"], "'); DROP TABLE trades;--")


class TestIdempotency(Base):
    def test_duplicate_client_id_not_double_booked(self):
        r1 = self.s.add_trade(trade_date="2026-10-01", code="600000", side="buy", qty=100, price="10",
                              fee="0", client_id="req_12345678", today=TODAY)
        r2 = self.s.add_trade(trade_date="2026-10-01", code="600000", side="buy", qty=100, price="10",
                              fee="0", client_id="req_12345678", today=TODAY)
        self.assertFalse(r1["duplicate"])
        self.assertTrue(r2["duplicate"])
        self.assertEqual(r1["trade"]["id"], r2["trade"]["id"])
        self.assertEqual(len(self.s.load_trades()), 1)
        self.assertEqual(self.pos()["positions"][0]["shares"], 100)

    def test_duplicate_without_explicit_fee_still_idempotent(self):
        kw = dict(trade_date="2026-10-01", code="600000", side="buy", qty=100, price="10",
                  client_id="req_abcdefgh", today=TODAY)
        self.s.add_trade(**kw)
        self.assertTrue(self.s.add_trade(**kw)["duplicate"])
        self.assertEqual(len(self.s.load_trades()), 1)

    def test_same_client_id_different_content_conflicts(self):
        kw = dict(trade_date="2026-10-01", code="600000", side="buy", price="10", fee="0",
                  client_id="req_12345678", today=TODAY)
        self.s.add_trade(qty=100, **kw)
        self.assertEqual(self.code_of(lambda: self.s.add_trade(qty=200, **kw)), "HOLDINGS_CLIENT_ID_CONFLICT")
        self.assertEqual(len(self.s.load_trades()), 1)

    def test_no_client_id_means_no_dedup(self):
        self.add("2026-10-01", "600000", "buy", 100, "10", "0")
        self.add("2026-10-01", "600000", "buy", 100, "10", "0")
        self.assertEqual(len(self.s.load_trades()), 2)


class TestFileSafety(Base):
    def test_import_and_construct_create_no_file(self):
        before = set(os.listdir(self.dir))
        HoldingsStore(self.dir / "x" / "holdings.db")
        self.assertEqual(set(os.listdir(self.dir)), before)
        # 在全新进程、空目录里导入包:目录必须仍为空
        with tempfile.TemporaryDirectory() as empty:
            code = ("import sys; sys.path.insert(0, %r); import src.holdings, src.holdings.store, "
                    "src.holdings.calc" % str(ROOT))
            subprocess.run([sys.executable, "-c", code], cwd=empty, check=True,
                           env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
            self.assertEqual(os.listdir(empty), [])

    def test_default_path_is_data_holdings_db(self):
        self.assertEqual(store_mod.DEFAULT_PATH, ROOT / "data" / "holdings.db")
        self.assertEqual(HoldingsStore().path, ROOT / "data" / "holdings.db")

    def test_reads_on_missing_db_create_nothing(self):
        self.assertEqual(self.s.load_trades(), [])
        self.assertEqual(self.s.list_trades(), [])
        self.assertIsNone(self.s.get_capital())
        self.assertFalse(self.path.exists())
        self.assertFalse(self.path.parent.exists())
        r = calc.positions(self.s.load_trades(), None, TODAY)
        self.assertEqual((r["positions"], r["closed"]), ([], []))

    def test_first_write_creates_0600(self):
        self.add("2026-10-01", "600000", "buy", 100, "10", "0")
        self.assertTrue(self.path.exists())
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        # journal_mode=DELETE:不留 -wal/-shm
        self.assertEqual(sorted(os.listdir(self.path.parent)), ["holdings.db"])

    def test_existing_wide_permission_tightened(self):
        self.path.parent.mkdir()
        sqlite3.connect(self.path).close()
        os.chmod(self.path, 0o644)
        self.add("2026-10-01", "600000", "buy", 100, "10", "0")
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_symlink_rejected_for_read_and_write(self):
        real = self.dir / "real.db"
        sqlite3.connect(real).close()
        link = self.dir / "link.db"
        os.symlink(real, link)
        s = HoldingsStore(link)
        self.assertEqual(self.code_of(lambda: s.add_trade(trade_date="2026-10-01", code="600000",
                                                          side="buy", qty=100, price="10", fee="0",
                                                          today=TODAY)), "HOLDINGS_STORE_UNSAFE")
        self.assertEqual(self.code_of(s.load_trades), "HOLDINGS_STORE_UNSAFE")
        self.assertEqual(self.code_of(s.get_capital), "HOLDINGS_STORE_UNSAFE")
        self.assertEqual(self.code_of(lambda: s.set_capital(1000)), "HOLDINGS_STORE_UNSAFE")
        # 目标文件没被改动(仍无任何表)
        con = sqlite3.connect(real)
        self.assertEqual(con.execute("SELECT count(*) FROM sqlite_master").fetchone()[0], 0)
        con.close()

    def test_dangling_symlink_rejected_and_target_not_created(self):
        target = self.dir / "nowhere.db"
        link = self.dir / "dangling.db"
        os.symlink(target, link)
        s = HoldingsStore(link)
        self.assertEqual(self.code_of(lambda: s.set_capital(1000)), "HOLDINGS_STORE_UNSAFE")
        self.assertFalse(target.exists())

    def test_directory_rejected(self):
        d = self.dir / "adir.db"
        d.mkdir()
        s = HoldingsStore(d)
        self.assertEqual(self.code_of(lambda: s.set_capital(1000)), "HOLDINGS_STORE_UNSAFE")
        self.assertEqual(self.code_of(s.load_trades), "HOLDINGS_STORE_UNSAFE")


class TestAuditAndPrivacy(Base):
    def test_audit_has_only_actions_and_ids(self):
        t = self.add("2026-10-01", "600000", "buy", 1200, "56.789", "0", note="私人备注")
        self.s.set_capital("987654.32")
        self.s.void_trade(t["id"])
        con = sqlite3.connect(self.path)
        cols = [r[1] for r in con.execute("PRAGMA table_info(audit)")]
        self.assertEqual(cols, ["id", "ts", "action", "trade_id"])
        rows = con.execute("SELECT action, trade_id FROM audit ORDER BY id").fetchall()
        self.assertEqual(rows, [("init_db", None), ("add_trade", t["id"]), ("set_capital", None),
                                ("void_trade", t["id"])])
        dump = repr(con.execute("SELECT * FROM audit").fetchall())
        for secret in ("600000", "1200", "56.789", "987654", "私人备注"):
            self.assertNotIn(secret, dump)
        con.close()

    def test_error_messages_are_fixed(self):
        with self.assertRaises(HoldingsError) as cm:
            self.add("2026-10-01", "600000", "buy", 150, "88.888", "0")
        e = cm.exception
        for s in (str(e), e.message, repr(e)):
            self.assertNotIn("150", s)
            self.assertNotIn("88.888", s)
            self.assertNotIn("600000", s)

    def test_static_scan_no_print_logging_network(self):
        banned_mods = {"logging", "urllib", "requests", "http", "socket", "ssl", "smtplib", "ftplib",
                       "xmlrpc", "websocket", "aiohttp", "httpx", "subprocess", "src"}
        for f in ("__init__.py", "store.py", "calc.py"):
            tree = ast.parse((ROOT / "src" / "holdings" / f).read_text(encoding="utf-8"))
            for n in ast.walk(tree):
                if isinstance(n, ast.Import):
                    mods = [a.name.split(".")[0] for a in n.names]
                elif isinstance(n, ast.ImportFrom) and n.level == 0:
                    mods = [(n.module or "").split(".")[0]]
                else:
                    mods = []
                for m in mods:
                    self.assertNotIn(m, banned_mods, f)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
                    self.assertNotIn(n.func.id, {"print", "open", "eval", "exec"}, f)


class TestS6SelfAudit(Base):
    """S6 全量自查:无密钥、无明文金额日志、出口不含持仓、默认关闭行为不变。"""

    def test_api_module_has_no_print_logging_network(self):
        banned = {"logging", "urllib", "requests", "http", "socket", "ssl", "smtplib", "ftplib",
                  "httpx", "aiohttp", "subprocess"}
        tree = ast.parse((ROOT / "src" / "holdings" / "api.py").read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                mods = [a.name.split(".")[0] for a in n.names]
            elif isinstance(n, ast.ImportFrom) and n.level == 0:
                mods = [(n.module or "").split(".")[0]]
            else:
                mods = []
            for m in mods:
                self.assertNotIn(m, banned)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
                self.assertNotIn(n.func.id, {"print", "open", "eval", "exec"})

    def test_no_secret_like_strings_in_new_files(self):
        import re
        pat = re.compile(r"(api[_-]?key|secret|passw(or)?d|bearer\s|sk-[A-Za-z0-9]{16,}|BEGIN [A-Z ]*PRIVATE KEY)", re.I)
        files = [ROOT / "src" / "holdings" / f for f in ("__init__.py", "store.py", "calc.py", "api.py")]
        files += [ROOT / "docs" / "holdings_howto.md", ROOT / "docs" / "holdings_design.md"]
        for f in files:
            self.assertIsNone(pat.search(f.read_text(encoding="utf-8")), f.name)

    def test_outputs_do_not_reference_holdings(self):
        # 离线导出、日报、通知、会员私人清单、管理后台(含备份白名单)、会员模块都不得碰持仓
        for rel in ("export_offline.py", "daily.py", "src/trade_notifications.py", "src/trade_private.py",
                    "src/admin.py", "src/members.py"):
            text = (ROOT / rel).read_text(encoding="utf-8").lower()
            self.assertNotIn("holdings", text, rel)

    def test_dispatch_only_in_personal_mode(self):
        text = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn('getattr(self, "_personal_mode", False) and u.path == "/api/holdings"', text)
        self.assertIn('getattr(self, "_personal_mode", False) and urlparse(self.path).path in (', text)

    def test_howto_covers_required_topics(self):
        doc = (ROOT / "docs" / "holdings_howto.md").read_text(encoding="utf-8")
        for key in ("个人模式", "录入规则", "撤销规则", "送转", "现金分红", "隐私边界", "已知局限",
                    "备份建议", "data/holdings.db"):
            self.assertIn(key, doc)

    def test_store_use_prints_nothing_and_leaks_no_amount(self):
        code = (
            "import sys, tempfile, pathlib\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            "from datetime import date\n"
            "from src.holdings import HoldingsStore\n"
            "d = pathlib.Path(tempfile.mkdtemp()) / 'h.db'\n"
            "s = HoldingsStore(d)\n"
            "r = s.add_trade(trade_date='2026-10-01', code='600000', side='buy', qty=1234500,"
            " price='77.777', fee='8.88', today=date(2026, 10, 22))\n"
            "s.void_trade(r['trade']['id'])\n"
            "s.set_capital('987654.32')\n"
        )
        r = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout + r.stderr, "")


if __name__ == "__main__":
    unittest.main()
