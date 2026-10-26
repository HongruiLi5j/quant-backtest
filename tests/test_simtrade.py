"""模拟盘单元测试（只用标准库）：python3 tests/test_simtrade.py"""
import sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import simtrade as s
sys.path.insert(0, str(Path(__file__).resolve().parent))
from realdata_guard import ZRealDataUntouchedTests  # noqa: E402,F401  真实数据守卫


def bar(o, c=None, prev=None, vol=1000):
    return {"open": o, "high": max(o, c or o), "low": min(o, c or o), "close": c or o, "prev_close": prev or o, "volume": vol}


def row(code, action, nxt, close=10.0, name="测试", **kw):
    d = {"code": code, "name": name, "action": action, "now": 0.0, "next": nxt, "close": close, "liquidity": "正常",
         "avg_turnover_20d": 1e8, "flags": []}
    d.update(kw)
    return d


def payload(day, rows):
    return {"data_date": day, "strategies": {"blend2": {"rows": rows}}}


class T(unittest.TestCase):
    def setUp(self):
        self._old = dict(s.ACCOUNTS)       # 测试用固定参数的账户，不受线上参数调整影响
        s.ACCOUNTS.clear()
        s.ACCOUNTS["ai_1m"] = {"label": "测试 100 万", "initial": 1_000_000.0, "max_weight": 0.10, "max_positions": 20, "min_trade": 5000.0}
        self.addCleanup(lambda: (s.ACCOUNTS.clear(), s.ACCOUNTS.update(self._old)))
        self.db = Path(tempfile.mkdtemp()) / "t.db"
        self.bars = {}
        self.fn = lambda code, day: self.bars.get((code, day))

    def run_day(self, day, rows):
        return s.run_day(payload(day, rows), bars_fn=self.fn, db=self.db)

    def q(self, sql, *a):
        c = s.connect(self.db)
        try:
            return [dict(r) for r in c.execute(sql, a)]
        finally:
            c.close()

    def test_fees(self):
        f = s.fees("600000", "buy", 10000)
        self.assertEqual((f["commission"], f["transfer"], f["stamp"]), (5.0, 0.1, 0.0))   # 万2.5=2.5 → 最低5元
        f = s.fees("600000", "sell", 100000)
        self.assertEqual((f["commission"], f["transfer"], f["stamp"]), (25.0, 1.0, 50.0))
        f = s.fees("510300", "sell", 100000)
        self.assertEqual((f["transfer"], f["stamp"]), (0.0, 0.0))                       # ETF 无印花税、过户费

    def test_limits(self):
        self.assertEqual(s.limit_prices("600000", "浦发", 10.0), (11.0, 9.0))
        self.assertEqual(s.limit_prices("300750", "宁德", 10.0), (12.0, 8.0))
        self.assertEqual(s.limit_prices("600001", "*ST甲", 10.0), (10.5, 9.5))

    def test_next_day(self):
        self.assertEqual(s.next_day("2026-10-16"), "2026-10-19")        # 周五 → 周一
        import datetime as d
        self.assertEqual(s.next_day("2026-10-16", [d.date(2026, 10, 20)]), "2026-10-20")

    def test_buy_flow_t1_and_lots(self):
        self.bars[("600001", "2026-10-16")] = bar(10, 10, 10)
        r = self.run_day("2026-10-16", [row("600001", "买入", 0.5)])
        self.assertEqual(r["ai_1m"]["new_orders"], 1)
        o = self.q("SELECT * FROM orders WHERE acct='ai_1m'")[0]
        self.assertEqual(o["qty"] % 100, 0)
        self.assertEqual(o["qty"], 5000)                   # 100万×10%×0.5/10元 = 5000 股
        self.bars[("600001", "2026-10-19")] = bar(10.2, 10.5, 10.0)
        r = self.run_day("2026-10-19", [row("600001", "卖出", 0.0, 10.5)])
        t = self.q("SELECT * FROM trades WHERE acct='ai_1m'")
        self.assertEqual((len(t), t[0]["side"], t[0]["price"]), (1, "buy", 10.2))
        self.assertGreater(t[0]["fee"], 0)
        # 卖单在 10-19 挂出，10-20 撮合（买入的 10-19 当天不能卖；到 10-20 才可卖）
        self.bars[("600001", "2026-10-20")] = bar(10.4, 10.3, 10.5)
        self.run_day("2026-10-20", [])
        t = self.q("SELECT * FROM trades WHERE acct='ai_1m' ORDER BY id")
        self.assertEqual([x["side"] for x in t], ["buy", "sell"])
        self.assertEqual(self.q("SELECT COUNT(*) n FROM positions WHERE acct='ai_1m'")[0]["n"], 0)
        self.assertGreater(t[1]["stamp"], 0)

    def test_limit_up_buy_rejected_and_explained(self):
        self.bars[("600002", "2026-10-16")] = bar(10, 10, 10)
        self.run_day("2026-10-16", [row("600002", "买入", 0.5)])
        self.bars[("600002", "2026-10-19")] = bar(11.0, 11.0, 10.0)
        r = self.run_day("2026-10-19", [])
        o = self.q("SELECT * FROM orders WHERE acct='ai_1m'")[0]
        self.assertEqual(o["status"], "rejected")
        self.assertIn("涨停", o["reason"])
        self.assertTrue(any("未成交" in x and "涨停" in x for x in r["ai_1m"]["summary"]))

    def test_suspended_waits_then_rejects(self):
        self.run_day("2026-10-16", [row("600003", "买入", 0.5)])
        self.run_day("2026-10-19", [])                                    # 当天行情还没有：继续挂单
        self.assertEqual(self.q("SELECT status FROM orders WHERE acct='ai_1m'")[0]["status"], "pending")
        self.run_day("2026-10-20", [])                                    # 撮合日已过：放弃
        self.assertEqual(self.q("SELECT status FROM orders WHERE acct='ai_1m'")[0]["status"], "rejected")

    def test_late_settle_is_cancelled_not_filled(self):
        # 日任务漏跑：成交日(10-19)有行情，但到 10-20 才来撮合 → 作废，不用事后价格补成交（T026）
        self.bars[("600001", "2026-10-16")] = bar(10, 10, 10)
        self.run_day("2026-10-16", [row("600001", "买入", 0.5)])
        self.bars[("600001", "2026-10-19")] = bar(10.2, 10.5, 10.0)
        r = self.run_day("2026-10-20", [])
        o = self.q("SELECT * FROM orders WHERE acct='ai_1m'")[0]
        self.assertEqual(o["status"], "cancelled")
        self.assertIn("过期", o["reason"])
        self.assertEqual(self.q("SELECT COUNT(*) n FROM trades")[0]["n"], 0)
        self.assertEqual(self.q("SELECT cash FROM accounts WHERE id='ai_1m'")[0]["cash"], 1_000_000.0)

    def test_on_time_settle_still_fills(self):
        self.bars[("600001", "2026-10-16")] = bar(10, 10, 10)
        self.run_day("2026-10-16", [row("600001", "买入", 0.5)])
        self.bars[("600001", "2026-10-19")] = bar(10.2, 10.5, 10.0)
        self.run_day("2026-10-19", [])
        self.assertEqual(self.q("SELECT status FROM orders WHERE acct='ai_1m'")[0]["status"], "filled")

    def test_stale_list_refused(self):
        # 旧清单(10-20)在最近已收盘日是 10-23 时运行：拒绝，不撮合、不挂单（T026 r3）
        self.bars[("600001", "2026-10-16")] = bar(10, 10, 10)
        self.run_day("2026-10-16", [row("600001", "买入", 0.5)])
        self.bars[("600001", "2026-10-19")] = bar(5, 5.2, 4.9)
        with self.assertRaises(s.StaleListError):
            s.run_day(payload("2026-10-19", []), bars_fn=self.fn, db=self.db, expect_day="2026-10-23")
        self.assertEqual(self.q("SELECT status FROM orders WHERE acct='ai_1m'")[0]["status"], "pending")
        self.assertEqual(self.q("SELECT COUNT(*) n FROM trades")[0]["n"], 0)

    def test_expected_day_matches_runs(self):
        self.bars[("600001", "2026-10-16")] = bar(10, 10, 10)
        r = s.run_day(payload("2026-10-16", [row("600001", "买入", 0.5)]), bars_fn=self.fn, db=self.db, expect_day="2026-10-16")
        self.assertEqual(r["ai_1m"]["new_orders"], 1)

    def test_expected_day_calendar_coverage(self):
        import datetime as d
        cal = [d.date(2026, 10, 19), d.date(2026, 10, 20), d.date(2026, 10, 21), d.date(2026, 10, 22), d.date(2026, 10, 23), d.date(2026, 10, 26)]
        bj = lambda *a: d.datetime(*a)
        self.assertEqual(s.expected_day(cal, bj(2026, 10, 23, 16, 0)), "2026-10-23")      # 收盘后
        self.assertEqual(s.expected_day(cal, bj(2026, 10, 23, 15, 0)), "2026-10-22")      # 15:30 前算上一日
        self.assertEqual(s.expected_day(cal, bj(2026, 10, 25, 12, 0)), "2026-10-23")      # 周日
        with self.assertRaises(s.StaleListError):                                           # 日历只到 10-20，却是 10-23 收盘后
            s.expected_day(cal[:2], bj(2026, 10, 23, 16, 0))
        with self.assertRaises(s.StaleListError):
            s.expected_day([], bj(2026, 10, 23, 16, 0))

    def test_small_account_skips_and_explains(self):
        s.ACCOUNTS["ai_10k"] = {"label": "测试小账户", "initial": 10_000.0, "max_weight": 0.20, "max_positions": 5, "min_trade": 500.0}
        self.addCleanup(s.ACCOUNTS.pop, "ai_10k", None)
        r = self.run_day("2026-10-16", [row("600004", "买入", 0.5, close=80.0)])
        o = self.q("SELECT * FROM orders WHERE acct='ai_10k'")[0]
        self.assertEqual(o["status"], "skipped")
        self.assertIn("一手约", o["reason"])
        self.assertEqual(self.q("SELECT status FROM orders WHERE acct='ai_1m'")[0]["status"], "pending")

    def test_filters(self):
        rows = [row("600005", "买入", 0.5, liquidity="流动性差"), row("600006", "买入", 0.5, flags=["今日一字涨停，明天可能仍买不进"]),
                row("600007", "买入", 0.03), row("600008", "加仓", 0.5)]
        self.run_day("2026-10-16", rows)
        got = {o["code"]: o["status"] for o in self.q("SELECT * FROM orders WHERE acct='ai_1m'")}
        self.assertEqual(got, {"600005": "skipped", "600006": "skipped"})     # 低于 5% 与没有持仓的加仓不产生订单

    def test_min_target_is_configurable(self):
        s.ACCOUNTS["ai_1m"]["min_target"] = 0.0
        self.run_day("2026-10-16", [row("600007", "买入", 0.03)])
        self.assertEqual(self.q("SELECT status FROM orders WHERE acct='ai_1m'")[0]["status"], "pending")

    def test_idempotent(self):
        self.run_day("2026-10-16", [row("600001", "买入", 0.5)])
        self.run_day("2026-10-16", [row("600001", "买入", 0.5)])
        self.assertEqual(self.q("SELECT COUNT(*) n FROM orders WHERE acct='ai_1m'")[0]["n"], 1)

    def test_rerun_keeps_first_summary(self):
        self.bars[("600001", "2026-10-16")] = bar(10, 10, 10)
        self.run_day("2026-10-16", [row("600001", "买入", 0.5)])
        self.bars[("600001", "2026-10-19")] = bar(10.2, 10.5, 10.0)
        first = self.run_day("2026-10-19", [row("600001", "持有", 0.5, 10.5)])
        self.assertTrue(first["ai_1m"]["summary"])
        again = self.run_day("2026-10-19", [row("600001", "持有", 0.5, 10.5)])    # 每日任务重复触发
        self.assertEqual(again["ai_1m"]["summary"], first["ai_1m"]["summary"])

    def test_stop_loss(self):
        self.bars[("600001", "2026-10-16")] = bar(10, 10, 10)
        self.run_day("2026-10-16", [row("600001", "买入", 0.5)])
        self.bars[("600001", "2026-10-19")] = bar(10, 10, 10)
        self.run_day("2026-10-19", [])
        self.bars[("600001", "2026-10-20")] = bar(8.0, 8.4, 10)             # 收盘比成本低 16%
        self.run_day("2026-10-20", [])
        self.assertTrue(any(o["side"] == "sell" and "硬止损" in o["reason"] for o in self.q("SELECT * FROM orders WHERE acct='ai_1m'")))

    def test_weekly_report_has_no_side_effects(self):
        self.run_day("2026-10-16", [row("600001", "买入", 0.5)])          # 周五会带周报
        c = s.connect(self.db)
        try:
            w = s.weekly_report(c, "ai_1m", "2026-10-16")
        finally:
            c.close()
        self.assertIn("不会自动修改", w["note"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
