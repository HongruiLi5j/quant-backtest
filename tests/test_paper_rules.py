"""paper_review/rules.py 单测(T012 / S1):只用合成数据,不读写任何文件。
运行:python3 -B tests/test_paper_rules.py
"""
import copy
import os
import py_compile
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from src.paper_review import rules
from src.paper_review.rules import daily_loss_halt, position_limits, stop_rule


def M(*triples):
    return list(triples)


class TestStopRule(unittest.TestCase):
    def test_defaults_match_pdf(self):
        self.assertEqual((rules.STOP_MONTHS, rules.STOP_UNDERPERF), (3, 0.03))
        self.assertEqual((rules.SINGLE_MAX, rules.TOTAL_MAX, rules.DAILY_LOSS), (0.10, 0.80, 0.02))

    def test_exactly_3pct_triggers(self):
        r = stop_rule(M(("2026-01", 0.01, 0.04), ("2026-02", -0.02, 0.01), ("2026-03", 0.00, 0.03)))
        self.assertTrue(r["triggered"])
        self.assertEqual(r["status"], "触发")
        self.assertEqual(r["months"], ["2026-01", "2026-02", "2026-03"])
        self.assertEqual(len(r["excess"]), 3)

    def test_just_under_3pct_does_not_trigger(self):
        r = stop_rule(M(("2026-01", 0.0101, 0.04), ("2026-02", -0.02, 0.01), ("2026-03", 0.0, 0.03)))
        self.assertFalse(r["triggered"])
        self.assertEqual(r["status"], "未触发")
        self.assertEqual(r["streak"], 2)  # 3 月刚好 -3% 算,1 月 -2.99% 不算 -> 尾部连续 2

    def test_second_month_breaks_chain(self):
        r = stop_rule(M(("2026-01", -0.05, 0.0), ("2026-02", -0.0, 0.029), ("2026-03", -0.05, 0.0)))
        self.assertFalse(r["triggered"])
        self.assertEqual(r["status"], "未触发")
        self.assertEqual(r["streak"], 1)

    def test_last_three_of_four(self):
        data = M(("2026-01", 0.05, 0.0), ("2026-02", -0.05, 0.0), ("2026-03", -0.05, 0.0), ("2026-04", -0.05, 0.0))
        r = stop_rule(data)
        self.assertTrue(r["triggered"])
        self.assertEqual(r["months"], ["2026-02", "2026-03", "2026-04"])

    def test_old_streak_recovered_not_triggered(self):
        data = M(("2026-01", -0.05, 0.0), ("2026-02", -0.05, 0.0), ("2026-03", -0.05, 0.0), ("2026-04", 0.02, 0.0))
        self.assertEqual(stop_rule(data)["status"], "未触发")

    def test_calendar_gap_breaks_chain(self):
        data = M(("2026-01", -0.05, 0.0), ("2026-03", -0.05, 0.0), ("2026-04", -0.05, 0.0))
        r = stop_rule(data)
        self.assertFalse(r["triggered"])
        self.assertEqual(r["status"], "数据不足")

    def test_year_boundary_is_contiguous(self):
        data = M(("2025-11", -0.05, 0.0), ("2025-12", -0.05, 0.0), ("2026-01", -0.05, 0.0))
        self.assertTrue(stop_rule(data)["triggered"])

    def test_empty_and_short_input_is_insufficient(self):
        r = stop_rule([])
        self.assertEqual((r["status"], r["triggered"]), ("数据不足", False))
        r = stop_rule(M(("2026-01", -0.1, 0.0), ("2026-02", -0.1, 0.0)))
        self.assertEqual((r["status"], r["triggered"]), ("数据不足", False))

    def test_unsorted_input_is_sorted(self):
        data = M(("2026-03", -0.05, 0.0), ("2026-01", -0.05, 0.0), ("2026-02", -0.05, 0.0))
        self.assertTrue(stop_rule(data)["triggered"])

    def test_cumulative_mode(self):
        # 逐月只差 1.5%,不触发 each;累计差 -4.5% 左右,触发 cumulative
        data = M(("2026-01", 0.0, 0.015), ("2026-02", 0.0, 0.015), ("2026-03", 0.0, 0.015))
        self.assertFalse(stop_rule(data, mode="each")["triggered"])
        r = stop_rule(data, mode="cumulative")
        self.assertTrue(r["triggered"])
        self.assertEqual(r["months"], ["2026-01", "2026-02", "2026-03"])
        # 累计只差 -2%:不触发
        data2 = M(("2026-01", 0.0, 0.01), ("2026-02", 0.0, 0.005), ("2026-03", 0.0, 0.005))
        self.assertFalse(stop_rule(data2, mode="cumulative")["triggered"])

    def test_cumulative_uses_geometric_compounding(self):
        # 策略 +10%,+10%,+10% = 33.1%;大盘 +14%×3 = 48.15%;累计超额 -15% -> 触发
        data = M(("2026-01", 0.10, 0.14), ("2026-02", 0.10, 0.14), ("2026-03", 0.10, 0.14))
        r = stop_rule(data, mode="cumulative")
        self.assertTrue(r["triggered"])
        self.assertIn("-15.", r["reason"])

    def test_cumulative_insufficient(self):
        self.assertEqual(stop_rule(M(("2026-01", -0.5, 0.0)), mode="cumulative")["status"], "数据不足")

    def test_parameters_overridable(self):
        data = M(("2026-01", -0.02, 0.0), ("2026-02", -0.02, 0.0))
        self.assertFalse(stop_rule(data)["triggered"])
        self.assertTrue(stop_rule(data, months=2, underperf=0.02)["triggered"])

    def test_bad_input_raises(self):
        with self.assertRaises(ValueError):
            stop_rule(M(("2026-13", 0.0, 0.0)))
        with self.assertRaises(ValueError):
            stop_rule(M(("2026-1", 0.0, 0.0)))
        with self.assertRaises(ValueError):
            stop_rule(M(("2026-01", float("nan"), 0.0)))
        with self.assertRaises(ValueError):
            stop_rule(M(("2026-01", 0.0, 0.0), ("2026-01", 0.0, 0.0)))
        with self.assertRaises(ValueError):
            stop_rule(M(("2026-01", -1.0, 0.0)))
        with self.assertRaises(ValueError):
            stop_rule([], mode="x")
        with self.assertRaises(ValueError):
            stop_rule([], months=0)

    def test_input_not_mutated(self):
        data = M(("2026-03", -0.05, 0.0), ("2026-01", -0.05, 0.0), ("2026-02", -0.05, 0.0))
        snap = copy.deepcopy(data)
        stop_rule(data)
        self.assertEqual(data, snap)


class TestPositionLimits(unittest.TestCase):
    def test_empty_positions(self):
        self.assertEqual(position_limits([], 1000.0), [])

    def test_single_exactly_10_ok_and_10_01_violates(self):
        self.assertEqual(position_limits([{"code": "A", "qty": 100, "price": 10.0}], 10000.0), [])
        v = position_limits([{"code": "A", "value": 1001.0}], 10000.0)
        self.assertEqual(len(v), 1)
        self.assertEqual((v[0]["kind"], v[0]["code"]), ("single", "A"))
        self.assertAlmostEqual(v[0]["weight"], 0.1001)
        self.assertEqual(v[0]["limit"], 0.10)
        self.assertAlmostEqual(v[0]["excess"], 0.0001)

    def test_total_exactly_80_ok_and_over_violates(self):
        eight = [{"code": c, "value": 1000.0} for c in "ABCDEFGH"]
        self.assertEqual(position_limits(eight, 10000.0), [])
        nine = eight + [{"code": "I", "value": 1000.0}]
        v = position_limits(nine, 10000.0)
        self.assertEqual([x["kind"] for x in v], ["total"])
        self.assertIsNone(v[0]["code"])
        self.assertAlmostEqual(v[0]["weight"], 0.9)

    def test_single_and_total_both(self):
        pos = [{"code": "A", "value": 5000.0}, {"code": "B", "value": 4000.0}]
        kinds = sorted(x["kind"] for x in position_limits(pos, 10000.0))
        self.assertEqual(kinds, ["single", "single", "total"])

    def test_duplicate_codes_merge(self):
        pos = [{"code": "A", "value": 600.0}, {"code": "A", "value": 600.0}]
        v = position_limits(pos, 10000.0)
        self.assertEqual([x["code"] for x in v if x["kind"] == "single"], ["A"])

    def test_custom_limits(self):
        v = position_limits([{"code": "A", "value": 2000.0}], 10000.0, single_max=0.2, total_max=0.2)
        self.assertEqual(v, [])
        v = position_limits([{"code": "A", "value": 2001.0}], 10000.0, single_max=0.2, total_max=0.5)
        self.assertEqual([x["kind"] for x in v], ["single"])

    def test_invalid_inputs_raise(self):
        ok = [{"code": "A", "qty": 1, "price": 1.0}]
        for eq in (0, -5.0, float("nan")):
            with self.assertRaises(ValueError):
                position_limits(ok, eq)
        bad = [
            [{"code": "A", "qty": 100}],
            [{"code": "A", "price": 10.0}],
            [{"code": "A", "qty": -1, "price": 10.0}],
            [{"code": "A", "qty": 1, "price": 0}],
            [{"code": "A", "qty": 1, "price": None}],
            [{"code": "A", "value": -1.0}],
            [{"qty": 1, "price": 1.0}],
            [{"code": "A"}],
        ]
        for pos in bad:
            with self.assertRaises(ValueError, msg=str(pos)):
                position_limits(pos, 1000.0)
        with self.assertRaises(ValueError):
            position_limits(ok, 1000.0, single_max=0)
        with self.assertRaises(ValueError):
            position_limits(ok, 1000.0, total_max=1.5)

    def test_input_not_mutated(self):
        pos = [{"code": "A", "qty": 100, "price": 50.0}]
        snap = copy.deepcopy(pos)
        position_limits(pos, 10000.0)
        self.assertEqual(pos, snap)


class TestDailyLossHalt(unittest.TestCase):
    def test_boundaries(self):
        self.assertTrue(daily_loss_halt(-2000.0, 100000.0))       # 刚好 -2%
        self.assertTrue(daily_loss_halt(-2000.01, 100000.0))
        self.assertFalse(daily_loss_halt(-1990.0, 100000.0))      # -1.99%
        self.assertFalse(daily_loss_halt(500.0, 100000.0))        # 盈利
        self.assertFalse(daily_loss_halt(0.0, 100000.0))

    def test_float_boundary(self):
        self.assertTrue(daily_loss_halt(-0.02, 1.0))
        self.assertTrue(daily_loss_halt(-0.06, 3.0))

    def test_custom_limit(self):
        self.assertTrue(daily_loss_halt(-1000.0, 100000.0, limit=0.01))
        self.assertFalse(daily_loss_halt(-1000.0, 100000.0, limit=0.05))

    def test_invalid_raises(self):
        for eq in (0, -1.0, float("nan")):
            with self.assertRaises(ValueError):
                daily_loss_halt(-1.0, eq)
        with self.assertRaises(ValueError):
            daily_loss_halt(float("nan"), 100.0)
        with self.assertRaises(ValueError):
            daily_loss_halt(-1.0, 100.0, limit=0)

    def test_returns_bool(self):
        self.assertIs(daily_loss_halt(-5000.0, 100000.0), True)
        self.assertIs(daily_loss_halt(5000.0, 100000.0), False)


class TestModule(unittest.TestCase):
    def test_stdlib_only_and_compiles(self):
        with tempfile.TemporaryDirectory() as d:
            py_compile.compile(os.path.join(ROOT, "src", "paper_review", "rules.py"),
                               cfile=os.path.join(d, "r.pyc"), doraise=True)
        with open(os.path.join(ROOT, "src", "paper_review", "rules.py"), encoding="utf-8") as f:
            src = f.read()
        for banned in ("numpy", "pandas", "sqlite3", "open(", "requests", "notify", "datasource"):
            self.assertNotIn(banned, src)

    def test_all_exports(self):
        import src.paper_review as pkg
        for name in pkg.__all__:
            self.assertTrue(hasattr(pkg, name), name)


if __name__ == "__main__":
    unittest.main()
