"""paper_review/weekly.py 与 scripts/weekly_summary.py 单测(T012 / S2):只用合成数据与临时目录。
运行:python3 -B tests/test_paper_weekly.py   或   .venv/bin/python -B tests/test_paper_weekly.py
"""
import contextlib
import hashlib
import importlib
import io
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, timedelta
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
from src import promotion as PR
from src import signal_ledger as L
from src.paper_review import weekly as W
from src.validation.paper import PaperJournal
import weekly_summary

WEEK = "2026-W44"                                   # 2026-10-26 ~ 2026-11-01
DAYS = ["2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30"]
N_UNIVERSE = 120


def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def code_of(i):
    return f"{600000 + i}"


def build_ledger(path):
    """合成账本:5 个信号日 × 120 只全市场股票;blend2 每天 8 条买入(共 40),另有 ctrl_random 对照。"""
    c = L.connect(path)
    for di, day in enumerate(DAYS):
        sig = []
        for j in range(8):
            i = di * 8 + j
            sig.append({"code": code_of(i), "name": f"股{i}", "action": "买入", "now": 0, "next": 0.1, "close": 10.0,
                        "liquidity": "正常", "small_target": False})
        ctrl = [{"code": code_of(60 + di * 8 + j), "name": "", "action": "买入", "now": 0, "next": 0.1, "close": 10.0,
                 "liquidity": "正常", "small_target": False} for j in range(8)]
        state = [("blend2", code_of(i), 1.0 if di * 8 <= i < di * 8 + 8 else 0.0, 0.0) for i in range(N_UNIVERSE)]
        L.record_day(c, {"data_date": day, "strategies": {"blend2": {"rows": sig}, "ctrl_random": {"rows": ctrl}}}, state)
        moves = []
        for i in range(N_UNIVERSE):
            for h in (1, 5, 20):
                ret = (((i + di) * 7) % 23 - 11) / 100
                moves.append((day, code_of(i), h, ret, 0.001, 0, 0))
        c.executemany("INSERT INTO moves VALUES(?,?,?,?,?,?,?)", moves)
        for h in (1, 5, 20):
            c.execute("INSERT INTO done VALUES(?,?,?)", (day, h, N_UNIVERSE))
    c.commit()
    PR.register(c)
    PR.ensure_null(c)
    c.close()


def open_ro(path):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def weekdays(start, end):
    d, out = date.fromisoformat(start), []
    while d <= date.fromisoformat(end):
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="weekly_test_")
        cls.db = os.path.join(cls.tmp, "ledger_test.db")
        build_ledger(cls.db)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)


class TestNormalWeek(Base):
    def test_normal_week_and_readonly(self):
        before = sha(self.db)
        conn = open_ro(self.db)
        try:
            rep = W.build(conn, WEEK)
        finally:
            conn.close()
        self.assertEqual(before, sha(self.db), "build 不能改动账本")
        self.assertEqual(rep["signal_days"], DAYS)
        p = rep["precision"]
        self.assertEqual(p["status"], "ok")
        buy5 = [r for r in p["rows"] if r["action"] == "买入" and r["h"] == 5][0]
        self.assertEqual(buy5["n"], 40)
        self.assertTrue(buy5["enough"])
        self.assertLessEqual(buy5["hit_excess_lo"], buy5["hit_excess"])
        self.assertLessEqual(buy5["hit_excess"], buy5["hit_excess_hi"])
        self.assertEqual(len(p["week_curve"]), 5)
        self.assertEqual(rep["recall"]["status"], "ok")
        self.assertEqual(rep["promotion"]["status"], "ok")
        self.assertGreaterEqual(rep["promotion"]["m"], PR.M_FLOOR)
        self.assertIn("ctrl_random", rep["controls"]["controls"])
        for k in ("performance", "turnover", "execution"):
            self.assertEqual(rep[k]["status"], W.INSUFFICIENT)
        md = W.render_markdown(rep)
        self.assertIn("大概率是运气", md)
        self.assertIn("不构成任何投资建议", md)
        self.assertNotIn("需要立即处理", md)

    def test_no_forbidden_modules(self):
        # 环境隔离：全套 pytest 时别的测试文件会先导入 src.notify 等，sys.modules 里本来就有。
        # 临时去掉项目自己的模块，重新导入本文件用到的模块，再做原来的检查；退出 with 后 sys.modules 原样恢复。
        with mock.patch.dict(sys.modules):
            for name in [n for n in sys.modules if n.split(".")[0] in ("src", "notify", "datasource", "weekly_summary")]:
                del sys.modules[name]
            for name in ("src.promotion", "src.signal_ledger", "src.paper_review.weekly", "src.validation.paper", "weekly_summary"):
                importlib.import_module(name)
            for name in ("src.notify", "src.datasource", "notify", "datasource"):
                self.assertNotIn(name, sys.modules)
        with open(os.path.join(ROOT, "src", "paper_review", "weekly.py"), encoding="utf-8") as f:
            src = f.read()
        for banned in ("import notify", "datasource", "requests", "urllib", "socket", "INSERT", "UPDATE ", "DELETE", ".commit("):
            self.assertNotIn(banned, src)


class TestInsufficientWeek(Base):
    def test_week_without_signals(self):
        conn = open_ro(self.db)
        try:
            rep = W.build(conn, "2026-W30")
        finally:
            conn.close()
        for k in ("precision", "recall", "performance", "turnover", "execution"):
            self.assertEqual(rep[k]["status"], W.INSUFFICIENT, k)
            self.assertNotIn("rows", rep[k])
        self.assertEqual(rep["rules"]["stop"]["status"], W.INSUFFICIENT)
        md = W.render_markdown(rep)
        self.assertIn("数据不足", md)
        self.assertNotIn("| 买入 |", md)

    def test_bad_week_format(self):
        conn = open_ro(self.db)
        try:
            with self.assertRaises(ValueError):
                W.build(conn, "2026-44")
        finally:
            conn.close()


class TestStopRule(Base):
    def _build(self, bench_daily):
        days = weekdays("2026-01-01", "2026-04-03")
        strat = {d: 0.0 for d in days}
        bench = {d: bench_daily for d in days}
        conn = open_ro(self.db)
        try:
            return W.build(conn, "2026-W14", strategy_daily=strat, benchmark_daily=bench)
        finally:
            conn.close()

    def test_triggered(self):
        rep = self._build(0.002)
        st = rep["rules"]["stop"]
        self.assertTrue(st["triggered"])
        self.assertEqual(st["months"], ["2026-01", "2026-02", "2026-03"])
        self.assertTrue(any(m["month"] == "2026-04" for m in st["partial"]))
        self.assertEqual([a["code"] for a in rep["alerts"]], ["stop_rule"])
        md = W.render_markdown(rep)
        self.assertIn("⚠ 暂停实盘", md)
        self.assertLess(md.index("⚠ 暂停实盘"), md.index("## 1 "))

    def test_not_triggered(self):
        rep = self._build(0.0005)                   # 每月只差约 1%
        self.assertFalse(rep["rules"]["stop"]["triggered"])
        self.assertEqual(rep["alerts"], [])
        self.assertNotIn("暂停实盘", W.render_markdown(rep).split("## 1 ")[0])

    def test_missing_benchmark_not_guessed(self):
        conn = open_ro(self.db)
        try:
            rep = W.build(conn, "2026-W14", strategy_daily={"2026-01-05": 0.0})
        finally:
            conn.close()
        self.assertEqual(rep["rules"]["stop"]["status"], W.INSUFFICIENT)
        self.assertIn("缺基准", rep["rules"]["stop"]["reason"])
        self.assertEqual(rep["alerts"], [])

    def test_monthly_excess_skips_short_month(self):
        s = {d: 0.0 for d in weekdays("2026-01-01", "2026-01-07")}
        full, partial = W.monthly_excess(s, s, "2026-03-01")
        self.assertEqual(full, [])
        self.assertEqual(partial[0]["month"], "2026-01")


class TestJournal(Base):
    def setUp(self):
        self.jpath = os.path.join(self.tmp, f"journal_{self.id().split('.')[-1]}.db")
        self.cal = weekdays("2026-10-26", "2026-10-30")

    def tearDown(self):
        for suffix in ("", "-journal"):
            if os.path.exists(self.jpath + suffix):
                os.remove(self.jpath + suffix)

    def _marks(self, j, code, prices):
        for d, p in zip(self.cal[1:], prices):
            j.add_mark(d, code, p)

    def _build(self, j):
        conn = open_ro(self.db)
        try:
            return W.build(conn, WEEK, journal=j, calendar=self.cal, initial_cash=1_000_000.0)
        finally:
            conn.close()

    def test_execution_split_buy_and_sell(self):
        conn = sqlite3.connect(self.db)             # 临时库里设定已知 gap(测试库,非真实数据)
        conn.execute("UPDATE moves SET gap=0.005 WHERE day='2026-10-26' AND code='600000' AND h=1")
        conn.execute("INSERT INTO signals VALUES('2026-10-29','blend2','600000','股0','卖出',1,0,11.0,'正常',0,'[]')")
        conn.execute("UPDATE moves SET gap=-0.01 WHERE day='2026-10-29' AND code='600000' AND h=1")
        conn.commit()
        conn.close()
        try:
            with PaperJournal(self.jpath) as j:
                b = j.add_signal("2026-10-26", "600000", "buy", 10.00, "blend2")
                j.add_fill(b, 10.10, 1000, "2026-10-27 10:00", 5.0)
                s = j.add_signal("2026-10-29", "600000", "sell", 11.00, "blend2")
                j.add_fill(s, 10.94, 500, "2026-10-30 10:00", 5.0)
                n = j.add_signal("2026-10-28", "688888", "buy", 20.00, "blend2")
                j.add_fill(n, 20.20, 100, "2026-10-29 10:00", 5.0)
                self._marks(j, "600000", [10.2, 10.3, 11.0, 10.9])
                self._marks(j, "688888", [20.2, 20.2, 20.2, 20.2][:4])
                rep = self._build(j)
        finally:
            conn = sqlite3.connect(self.db)
            conn.execute("DELETE FROM signals WHERE day='2026-10-29' AND code='600000'")
            conn.execute("UPDATE moves SET gap=0.001 WHERE h=1 AND code='600000' AND day IN ('2026-10-26','2026-10-29')")
            conn.commit()
            conn.close()
        ex = rep["execution"]
        self.assertEqual(ex["status"], "ok")
        by = {(i["side"], i["code"]): i for i in ex["top5"]}
        buy, sell, nogap = by[("buy", "600000")], by[("sell", "600000")], by[("buy", "688888")]
        self.assertAlmostEqual(buy["total"], 0.01)
        self.assertAlmostEqual(buy["gap_part"], 0.005)
        self.assertAlmostEqual(buy["exec"], buy["total"] - 0.005)
        self.assertAlmostEqual(sell["total"], (11.0 - 10.94) / 11.0)
        self.assertAlmostEqual(sell["gap_part"], 0.01)               # 低开对卖方不利
        self.assertAlmostEqual(sell["exec"], sell["total"] - 0.01)
        for i in (buy, sell):
            self.assertAlmostEqual(i["total"], i["gap_part"] + i["exec"])
        self.assertIsNone(nogap["exec"])
        self.assertIsNone(nogap["gap_part"])
        self.assertEqual(ex["n_unsplittable"], 1)
        self.assertEqual(ex["n_fills"], 3)
        self.assertTrue(ex["unregistered"], "账本里其余主策略信号没登记,应被列出")
        self.assertNotIn({"date": "2026-10-26", "code": "600000", "side": "buy"}, ex["unregistered"])
        self.assertTrue(any(a["code"] == "unregistered" for a in rep["alerts"]))
        t = rep["turnover"]
        self.assertEqual(t["status"], "ok")
        self.assertAlmostEqual(t["buy"], 10.10 * 1000 + 20.20 * 100)
        self.assertGreater(t["turnover"], 0)
        perf = rep["performance"]
        self.assertEqual(perf["status"], "ok")
        self.assertEqual(perf["perf"]["source"], "validation.stats")
        md = W.render_markdown(rep)
        self.assertIn("无法拆分", md)
        self.assertIn("隔夜跳空", md)

    def test_perf_fallback_labelled(self):
        with PaperJournal(self.jpath) as j:
            b = j.add_signal("2026-10-26", "600000", "buy", 10.00, "blend2")
            j.add_fill(b, 10.0, 1000, "2026-10-27 10:00", 5.0)
            self._marks(j, "600000", [10.0, 9.0, 9.5, 10.0])
            saved, W._STATS = W._STATS, None
            try:
                rep = self._build(j)
            finally:
                W._STATS = saved
        pf = rep["performance"]["perf"]
        self.assertIn("简版", pf["source"])
        self.assertLess(pf["max_drawdown"], 0)

    def test_risk_rules_alert(self):
        with PaperJournal(self.jpath) as j:
            b = j.add_signal("2026-10-26", "600000", "buy", 10.00, "blend2")
            j.add_fill(b, 10.0, 50_000, "2026-10-27 10:00", 5.0)     # 50 万 = 50% 单票
            self._marks(j, "600000", [10.0, 9.5, 9.5, 9.5])             # 10-28 浮亏 2.5 万 = -2.5%
            rep = self._build(j)
        pos = rep["rules"]["positions"]
        self.assertEqual(pos["status"], "ok")
        self.assertEqual([v["kind"] for v in pos["violations"]], ["single"])
        self.assertEqual(rep["rules"]["daily_loss"]["halt_days"], ["2026-10-28"])
        codes = {a["code"] for a in rep["alerts"]}
        self.assertIn("position_limits", codes)
        self.assertIn("daily_loss", codes)
        md = W.render_markdown(rep)
        self.assertIn("需要立即处理", md)
        self.assertLess(md.index("需要立即处理"), md.index("## 1 "))

    def test_journal_without_fills_is_insufficient(self):
        with PaperJournal(self.jpath) as j:
            rep = self._build(j)
        self.assertEqual(rep["performance"]["status"], W.INSUFFICIENT)
        self.assertIn("没有任何成交", rep["performance"]["reason"])


class TestWriteAndScript(Base):
    def test_write_report_refuses_overwrite(self):
        out = os.path.join(self.tmp, "w1")
        p = W.write_report("x", out, WEEK)
        self.assertTrue(os.path.isfile(p))
        with self.assertRaises(FileExistsError):
            W.write_report("y", out, WEEK)
        with open(p, encoding="utf-8") as f:
            self.assertEqual(f.read(), "x")

    def test_script_runs_readonly_and_refuses_overwrite(self):
        out = os.path.join(self.tmp, "w2")
        before = sha(self.db)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = weekly_summary.main(["--week", WEEK, "--ledger", self.db, "--out-dir", out])
        self.assertEqual(rc, 0)
        self.assertEqual(before, sha(self.db))
        path = os.path.join(out, f"{WEEK}.md")
        self.assertTrue(os.path.isfile(path))
        with open(path, encoding="utf-8") as f:
            first = f.read()
        self.assertIn("纸面实盘周报", first)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc2 = weekly_summary.main(["--week", WEEK, "--ledger", self.db, "--out-dir", out])
        self.assertEqual(rc2, 2)
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), first)

    def test_script_missing_ledger_not_created(self):
        missing = os.path.join(self.tmp, "nope.db")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = weekly_summary.main(["--week", WEEK, "--ledger", missing, "--out-dir", os.path.join(self.tmp, "w3")])
        self.assertEqual(rc, 1)
        self.assertFalse(os.path.exists(missing))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "w3")))

    def test_script_default_out_dir(self):
        self.assertTrue(str(weekly_summary.DEFAULT_OUT).endswith(os.path.join("handoff", "reports", "weekly")))

    def test_script_with_benchmark_csv_and_journal_optional(self):
        bench = os.path.join(self.tmp, "bench.csv")
        with open(bench, "w", encoding="utf-8") as f:
            f.write("date,ret\n2026-10-26,0.001\n")
        out = os.path.join(self.tmp, "w4")
        with contextlib.redirect_stdout(io.StringIO()):
            rc = weekly_summary.main(["--week", WEEK, "--ledger", self.db, "--out-dir", out, "--benchmark-csv", bench])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
