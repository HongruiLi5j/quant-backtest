"""paper.py 单测(T007 / S5):只用合成数据和临时目录,不读写 data/。
运行:python3 -B tests/test_validation_paper.py
"""
import math
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from src.validation.paper import PaperJournal, PaperError

CAL = ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"]


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "paper_journal.db")
        self.j = PaperJournal(self.path)

    def tearDown(self):
        self.j.close()
        self._tmp.cleanup()

    def hold_scenario(self):
        """第 1 日 10.00 买 1000 股,之后 3 日无成交,marks 为 10.00/10.50/10.20/10.80。"""
        sid = self.j.add_signal("2024-01-02", "600000", "buy", 10.0, "s1")
        self.j.add_fill(sid, 10.0, 1000, "2024-01-02 09:35:00")
        for d, p in zip(CAL, [10.0, 10.5, 10.2, 10.8]):
            self.j.add_mark(d, "600000", p)
        return sid


class TestCrud(Base):
    def test_add_query_delete(self):
        sid = self.j.add_signal("2024-01-02", "600000", "buy", 10.0, "s1")
        self.assertEqual(self.j.get_signal(sid)["theory_price"], 10.0)
        fid = self.j.add_fill(sid, 10.02, 500, "2024-01-03 09:31:00", fee=5.0, note="部分")
        self.assertEqual(self.j.list_fills(sid)[0]["id"], fid)
        s2 = self.j.add_signal("2024-01-03", "000001", "sell", 20.0, "s1")
        mid = self.j.add_miss(s2, "limit")
        self.assertEqual(self.j.list_misses()[0]["id"], mid)
        self.assertEqual(len(self.j.list_signals("2024-01-03", "2024-01-03")), 1)
        self.assertTrue(self.j.delete_miss(mid))
        self.assertFalse(self.j.delete_miss(mid))
        self.assertTrue(self.j.delete_signal(sid))  # 级联删除成交
        self.assertEqual(self.j.list_fills(), [])
        self.assertIsNone(self.j.get_signal(sid))

    def test_persist_across_reopen(self):
        sid = self.j.add_signal("2024-01-02", "600000", "buy", 10.0, "s1")
        self.j.close()
        self.j = PaperJournal(self.path)
        self.assertEqual(self.j.get_signal(sid)["code"], "600000")

    def test_idempotent(self):
        a = self.j.add_signal("2024-01-02", "600000", "buy", 10.0, "s1")
        self.assertEqual(a, self.j.add_signal("2024-01-02", "600000", "buy", 10.0, "s1"))
        with self.assertRaises(PaperError):
            self.j.add_signal("2024-01-02", "600000", "buy", 10.5, "s1")
        f = self.j.add_fill(a, 10.0, 100, "2024-01-02 10:00", fee=1.0)
        self.assertEqual(f, self.j.add_fill(a, 10.0, 100, "2024-01-02 10:00", fee=1.0))
        with self.assertRaises(PaperError):
            self.j.add_fill(a, 10.0, 100, "2024-01-02 10:00", fee=2.0)
        self.assertEqual(len(self.j.list_fills()), 1)
        self.j.add_mark("2024-01-02", "600000", 10.0)
        self.j.add_mark("2024-01-02", "600000", 10.0)
        with self.assertRaises(PaperError):
            self.j.add_mark("2024-01-02", "600000", 10.1)
        self.assertEqual(len(self.j.list_marks()), 1)

    def test_miss_rules(self):
        a = self.j.add_signal("2024-01-02", "600000", "buy", 10.0, "s1")
        m = self.j.add_miss(a, "suspended")
        self.assertEqual(m, self.j.add_miss(a, "suspended"))
        with self.assertRaises(PaperError):
            self.j.add_miss(a, "limit")
        with self.assertRaises(PaperError):  # 有漏单后不能再登记成交
            self.j.add_fill(a, 10.0, 100, "2024-01-02 10:00")
        b = self.j.add_signal("2024-01-02", "600001", "buy", 10.0, "s1")
        self.j.add_fill(b, 10.0, 100, "2024-01-02 10:00")
        with self.assertRaises(PaperError):  # 有成交后不能再登记漏单
            self.j.add_miss(b, "other")


class TestInvalid(Base):
    def test_bad_inputs(self):
        bad = [
            lambda: self.j.add_signal("2024-13-01", "600000", "buy", 10, "s"),
            lambda: self.j.add_signal("2024-01-02", "6000;DROP", "buy", 10, "s"),
            lambda: self.j.add_signal("2024-01-02", "600000", "hold", 10, "s"),
            lambda: self.j.add_signal("2024-01-02", "600000", "buy", -1, "s"),
            lambda: self.j.add_signal("2024-01-02", "600000", "buy", float("nan"), "s"),
            lambda: self.j.add_signal("2024-01-02", "600000", "buy", True, "s"),
            lambda: self.j.add_signal("2024-01-02", "600000", "buy", 10, ""),
            lambda: self.j.add_fill(999, 10, 100, "2024-01-02"),
            lambda: self.j.add_mark("2024-01-02", "600000", 0),
            lambda: self.j.add_miss(999, "limit"),
        ]
        for f in bad:
            with self.assertRaises(PaperError):
                f()
        sid = self.j.add_signal("2024-01-02", "600000", "buy", 10, "s")
        for args in [(10, 0, "2024-01-02"), (10, 1.5, "2024-01-02"), (10, 100, "garbage"),
                     (10, 100, "2024-01-01"), (float("inf"), 100, "2024-01-02")]:
            with self.assertRaises(PaperError):
                self.j.add_fill(sid, *args)
        with self.assertRaises(PaperError):
            self.j.add_fill(sid, 10, 100, "2024-01-02", fee=-1)
        with self.assertRaises(PaperError):
            self.j.add_miss(sid, "bored")
        self.assertEqual(self.j.list_fills(), [])  # 失败不留半写入

    def test_injection_is_inert(self):
        sid = self.j.add_signal("2024-01-02", "600000", "buy", 10, "s'); DROP TABLE signals;--")
        self.assertEqual(self.j.get_signal(sid)["strategy_id"], "s'); DROP TABLE signals;--")
        self.assertEqual(len(self.j.list_signals()), 1)

    def test_path_rules(self):
        with self.assertRaises(PaperError):
            PaperJournal(os.path.join(self._tmp.name, "simtrade.db"))
        with self.assertRaises(PaperError):
            PaperJournal(os.path.join(self._tmp.name, "nodir", "x.db"))
        with self.assertRaises(PaperError):
            PaperJournal(None)

    def test_rollback_on_failure(self):
        sid = self.j.add_signal("2024-01-02", "600000", "buy", 10, "s")
        try:
            with self.j._tx():
                self.j._conn.execute("INSERT INTO marks(date, code, price) VALUES ('2024-01-02','600000',10)")
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        self.assertEqual(self.j.list_marks(), [])
        self.assertIsNotNone(self.j.get_signal(sid))


class TestReport(Base):
    def test_slippage_and_miss_distribution(self):
        # 买入理论 10.00 实际 10.05 → +0.5%(金额 1005);买入理论 20.00 实际 19.80 → -1%(金额 1980)
        j2 = self.j
        a = j2.add_signal("2024-01-02", "A", "buy", 10.0, "s")
        j2.add_fill(a, 10.05, 100, "2024-01-02 10:00")  # +0.5%,金额 1005
        b = j2.add_signal("2024-01-02", "B", "buy", 20.0, "s")
        j2.add_fill(b, 19.80, 100, "2024-01-02 10:00")  # -1%,金额 1980
        for i, r in enumerate(["limit", "limit", "suspended", "too_late"]):
            j2.add_miss(j2.add_signal("2024-01-02", f"M{i}", "buy", 10, "s"), r)
        j2.add_signal("2024-01-02", "P", "buy", 10, "s")
        j2.add_mark("2024-01-02", "A", 10.05)
        j2.add_mark("2024-01-02", "B", 19.80)
        r = j2.report("2024-01-02", "2024-01-02", 1e6, {}, CAL)
        self.assertAlmostEqual(r["avg_slippage"], (0.005 - 0.01) / 2)
        self.assertAlmostEqual(r["weighted_slippage"], (0.005 * 1005 - 0.01 * 1980) / (1005 + 1980))
        self.assertEqual((r["n_filled"], r["n_missed"], r["n_pending"]), (2, 4, 1))
        self.assertAlmostEqual(r["miss_rate"], 4 / 6)
        self.assertEqual(r["miss_reasons"], {"limit": 2, "suspended": 1, "too_late": 1, "other": 0})

    def test_sell_slippage_sign(self):
        a = self.j.add_signal("2024-01-02", "A", "buy", 10.0, "s")
        self.j.add_fill(a, 10.0, 100, "2024-01-02 10:00")
        b = self.j.add_signal("2024-01-03", "A", "sell", 20.0, "s")
        self.j.add_fill(b, 19.90, 100, "2024-01-03 10:00")  # 卖低了 0.5%
        for d in CAL[:2]:
            self.j.add_mark(d, "A", 10.0)
        r = self.j.report("2024-01-02", "2024-01-03", 10000, {}, CAL)
        self.assertAlmostEqual(r["avg_slippage"], 0.0025)  # (0 + 0.005) / 2
        self.assertAlmostEqual(r["attribution"]["2024-01-03"]["slippage_cost"], 0.10 * 100)

    def test_holding_without_fills_marks_to_market(self):
        self.hold_scenario()
        bt = dict(zip(CAL, [0.0, 0.004, -0.003, 0.005]))
        r = self.j.report("2024-01-02", "2024-01-05", 100000, bt, CAL)
        # 现金 90000;净值 = 90000 + 1000 × mark
        exp = [100000.0, 100500.0, 100200.0, 100800.0]
        for d, e in zip(CAL, exp):
            self.assertAlmostEqual(r["equity"][d], e)
        rets = [0.0, 500 / 100000, 100200 / 100500 - 1, 100800 / 100200 - 1]
        for d, e in zip(CAL, rets):
            self.assertAlmostEqual(r["daily_returns"][d], e, places=12)
        self.assertNotEqual(r["daily_returns"]["2024-01-03"], 0)  # 无成交日收益非零
        # 手算跟踪误差:diff = [0, 0.001, -0.0029851+0.003, 0.0059880-0.005]
        d3 = 100200 / 100500 - 1 + 0.003
        d4 = 100800 / 100200 - 1 - 0.005
        diffs = [0.0, 0.001, d3, d4]
        m = sum(diffs) / 4
        sd = math.sqrt(sum((x - m) ** 2 for x in diffs) / 3)
        self.assertAlmostEqual(r["tracking_error"], sd * math.sqrt(252), places=10)
        cum_p = 100800 / 100000 - 1
        cum_b = 1.004 * 0.997 * 1.005 - 1
        self.assertAlmostEqual(r["cum_deviation"], cum_p - cum_b, places=12)
        self.assertEqual(r["n_compared"], 4)
        self.assertEqual(r["stale_mark_days"], [])

    def test_fee_reduces_cash(self):
        sid = self.j.add_signal("2024-01-02", "A", "buy", 10.0, "s")
        self.j.add_fill(sid, 10.0, 1000, "2024-01-02 10:00", fee=5.0)
        self.j.add_mark("2024-01-02", "A", 10.0)
        r = self.j.report("2024-01-02", "2024-01-02", 100000, {}, CAL)
        self.assertAlmostEqual(r["final_equity"], 99995.0)
        self.assertAlmostEqual(r["attribution"]["2024-01-02"]["fees"], 5.0)
        self.assertIsNone(r["tracking_error"])
        self.assertIsNone(r["cum_deviation"])

    def test_missing_mark_error_and_ffill(self):
        sid = self.j.add_signal("2024-01-02", "600000", "buy", 10.0, "s1")
        self.j.add_fill(sid, 10.0, 1000, "2024-01-02 09:35:00")
        self.j.add_mark("2024-01-02", "600000", 10.0)
        self.j.add_mark("2024-01-04", "600000", 10.2)  # 缺 01-03 和 01-05
        with self.assertRaises(PaperError):
            self.j.report("2024-01-02", "2024-01-05", 100000, {}, CAL)
        r = self.j.report("2024-01-02", "2024-01-05", 100000, {}, CAL, missing_mark="ffill")
        self.assertEqual(r["stale_mark_days"], [("2024-01-03", "600000"), ("2024-01-05", "600000")])
        self.assertAlmostEqual(r["equity"]["2024-01-03"], 100000.0)  # 沿用 10.0
        self.assertAlmostEqual(r["equity"]["2024-01-04"], 100200.0)
        self.assertAlmostEqual(r["equity"]["2024-01-05"], 100200.0)  # 沿用 10.2
        self.assertTrue(any("陈旧" in w for w in r["warnings"]))

    def test_ffill_without_any_mark_errors(self):
        sid = self.j.add_signal("2024-01-02", "A", "buy", 10.0, "s")
        self.j.add_fill(sid, 10.0, 100, "2024-01-02 10:00")
        with self.assertRaises(PaperError):
            self.j.report("2024-01-02", "2024-01-02", 10000, {}, CAL, missing_mark="ffill")

    def test_calendar_rules(self):
        self.hold_scenario()
        for bad in (["2024-01-03", "2024-01-02"], ["2024-01-02", "2024-01-02"], [], ["x"]):
            with self.assertRaises(PaperError):
                self.j.report("2024-01-02", "2024-01-05", 100000, {}, bad)
        # 窗口之外无交易日
        with self.assertRaises(PaperError):
            self.j.report("2025-01-01", "2025-01-02", 100000, {}, CAL)
        # 成交日不在日历里
        with self.assertRaises(PaperError):
            self.j.report("2024-01-02", "2024-01-05", 100000, {}, CAL[1:])

    def test_bt_alignment_gaps(self):
        self.hold_scenario()
        bt = {"2024-01-03": 0.004, "2024-01-05": 0.005, "2023-12-29": 0.1}  # 缺 01-02、01-04;多一个窗口外日期
        r = self.j.report("2024-01-02", "2024-01-05", 100000, bt, CAL)
        self.assertEqual(r["bt_missing_days"], ["2024-01-02", "2024-01-04"])
        self.assertEqual(r["n_compared"], 2)
        self.assertEqual(sorted(r["diff"]), ["2024-01-03", "2024-01-05"])

    def test_bad_report_args(self):
        self.hold_scenario()
        for kw in ({"initial_cash": 0}, {"initial_cash": float("nan")}, {"missing_mark": "zero"},
                   {"bt_returns": {"2024-01-03": -1.5}}, {"bt_returns": [1, 2]},
                   {"start": "2024-01-05", "end": "2024-01-02"}):
            args = dict(start="2024-01-02", end="2024-01-05", initial_cash=100000, bt_returns={}, calendar=CAL)
            args.update(kw)
            with self.assertRaises(PaperError):
                self.j.report(**args)

    def test_oversell_rejected(self):
        a = self.j.add_signal("2024-01-02", "A", "sell", 10.0, "s")
        self.j.add_fill(a, 10.0, 100, "2024-01-02 10:00")
        with self.assertRaises(PaperError):
            self.j.report("2024-01-02", "2024-01-02", 10000, {}, CAL)


class TestReviewFixes(Base):
    def test_future_fill_not_counted(self):
        # 1/2 信号,1/4 才成交:截至 1/2 的报告里这笔成交不存在,信号算待定
        a = self.j.add_signal("2024-01-02", "A", "buy", 10.0, "s")
        self.j.add_fill(a, 11.0, 100, "2024-01-04 10:00")
        r = self.j.report("2024-01-02", "2024-01-02", 10000, {}, CAL)
        self.assertEqual((r["n_filled"], r["n_pending"], r["n_fills"]), (0, 1, 0))
        self.assertIsNone(r["avg_slippage"])
        self.assertAlmostEqual(r["final_equity"], 10000.0)
        self.j.add_mark("2024-01-04", "A", 11.0)
        r2 = self.j.report("2024-01-02", "2024-01-04", 10000, {}, CAL[:3], missing_mark="ffill")
        self.assertEqual(r2["n_filled"], 1)
        self.assertAlmostEqual(r2["avg_slippage"], 0.1)

    def test_prior_holdings_rejected(self):
        a = self.j.add_signal("2024-01-02", "A", "buy", 10.0, "s")
        self.j.add_fill(a, 10.0, 100, "2024-01-02 10:00")
        for d, p in zip(CAL, [10, 11, 12, 12]):
            self.j.add_mark(d, "A", p)
        with self.assertRaises(PaperError):
            self.j.report("2024-01-03", "2024-01-05", 10000, {}, CAL)

    def test_timestamp_formats_equivalent(self):
        a = self.j.add_signal("2024-01-02", "A", "buy", 10.0, "s")
        f1 = self.j.add_fill(a, 10.0, 100, "2024-01-02T10:00:00")
        self.assertEqual(f1, self.j.add_fill(a, 10.0, 100, "2024-01-02 10:00:00"))  # 同一笔
        self.assertEqual(self.j.list_fills(a)[0]["ts"], "2024-01-02 10:00:00")
        b = self.j.add_signal("2024-01-02", "A", "sell", 10.0, "s")
        self.j.add_fill(b, 10.0, 100, "2024-01-02 11:00:00")  # 空格写法的卖出晚于 T 写法的买入
        self.j.add_mark("2024-01-02", "A", 10.0)
        r = self.j.report("2024-01-02", "2024-01-02", 10000, {}, CAL)
        self.assertAlmostEqual(r["final_equity"], 10000.0)
        for bad in ("2024-01-02T10:00:00+08:00", "2024-01-02 25:00"):
            with self.assertRaises(PaperError):
                self.j.add_fill(a, 10.0, 200, bad)

    def test_zero_equity_rejected(self):
        a = self.j.add_signal("2024-01-02", "A", "buy", 10.0, "s")
        self.j.add_fill(a, 10.0, 100, "2024-01-02 10:00", fee=10000)
        for d in CAL:
            self.j.add_mark(d, "A", 10.0)
        with self.assertRaises(PaperError):
            self.j.report("2024-01-02", "2024-01-05", 10000, {}, CAL)


class TestRound3(Base):
    def test_protected_names_case_and_symlink(self):
        d = self._tmp.name
        with self.assertRaises(PaperError):
            PaperJournal(os.path.join(d, "SIMTRADE.DB"))
        with self.assertRaises(PaperError):
            PaperJournal(os.path.join(d, "Signal_Ledger.db"))
        self.assertEqual(sorted(os.listdir(d)), ["paper_journal.db"])  # 没有创建任何文件
        real = os.path.join(d, "simtrade.db")
        open(real, "wb").close()
        link = os.path.join(d, "innocent.db")
        os.symlink(real, link)
        with self.assertRaises(PaperError):
            PaperJournal(link)
        self.assertEqual(os.path.getsize(real), 0)  # 受保护文件没被改动

    def test_foreign_db_untouched(self):
        import sqlite3
        foreign = os.path.join(self._tmp.name, "other.db")
        c = sqlite3.connect(foreign)
        c.execute("CREATE TABLE signals (x INTEGER)")  # 同名表但列不同
        c.commit(); c.close()
        with self.assertRaises(PaperError):
            PaperJournal(foreign)
        c = sqlite3.connect(foreign)
        tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master")]
        cols = [r[1] for r in c.execute("PRAGMA table_info(signals)")]
        c.close()
        self.assertEqual(tables, ["signals"])
        self.assertEqual(cols, ["x"])
        # 非 SQLite 文件也拒绝且不改动
        junk = os.path.join(self._tmp.name, "junk.db")
        with open(junk, "wb") as f:
            f.write(b"not a database" * 100)
        with self.assertRaises(PaperError):
            PaperJournal(junk)
        with open(junk, "rb") as f:
            self.assertEqual(f.read(), b"not a database" * 100)

    def test_commit_failure_rolls_back(self):
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            with self.j._tx():
                self.j._conn.execute("PRAGMA defer_foreign_keys = ON")  # 外键检查推迟到提交时
                self.j._conn.execute("INSERT INTO fills(signal_id, price, shares, ts) VALUES (999, 10, 100, '2024-01-02')")
        self.assertFalse(self.j._conn.in_transaction)
        self.assertEqual(self.j.list_fills(), [])
        self.j.add_mark("2024-01-02", "A", 10.0)  # 连接仍可继续写
        self.assertEqual(len(self.j.list_marks()), 1)


class TestImport(unittest.TestCase):
    def test_import_creates_no_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = ("import sys; sys.path.insert(0, %r); import src.validation.paper; "
                    "import os; print(sorted(os.listdir('.')))" % ROOT)
            out = subprocess.run([sys.executable, "-B", "-c", code], cwd=tmp, capture_output=True,
                                 text=True, check=True).stdout.strip()
            self.assertEqual(out, "[]")
            self.assertEqual(os.listdir(tmp), [])
        self.assertFalse(os.path.exists(os.path.join(ROOT, "data", "paper_journal.db")))


if __name__ == "__main__":
    unittest.main()
