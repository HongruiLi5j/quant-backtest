"""D17：src/limits.py 涨跌停规则表单测。纯计算，不读写 data/（上市天数用传入的小清单和日历）。
运行：.venv/bin/python tests/test_limits_table.py"""
import sys
import unittest
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import limits as LM  # noqa: E402
from src import datasource, simtrade, signal_ledger  # noqa: E402
from src.dataquality import consistency as cons  # noqa: E402
from src.market_monitor import intraday  # noqa: E402


class Rules(unittest.TestCase):
    def test_table_rows(self):
        cases = [
            ("600001", "*ST甲", "2026-07-03", 0.05), ("600001", "*ST甲", "2026-07-06", 0.10),   # 主板 ST 改 10% 的边界
            ("000001", "ST乙", "2026-07-03", 0.05), ("000001", "ST乙", None, 0.10),
            ("000001", "平安银行", "2015-01-05", 0.10), ("000001", "平安银行", None, 0.10),
            ("300001", "ST乙", "2020-08-21", 0.05), ("300001", "ST乙", "2020-08-24", 0.20),     # 创业板 ST：改革前按主板 5%
            ("300750", "宁德时代", "2020-08-21", 0.10), ("300750", "宁德时代", "2020-08-24", 0.20),
            ("302132", "X", None, 0.20),                                                          # 30 开头都是创业板
            ("688001", "ST丙", "2024-01-02", 0.20), ("689009", "X", None, 0.20),                  # 科创板 ST 按 20%
            ("830001", "X", None, 0.30), ("430047", "ST丁", None, 0.30), ("920001", "X", None, 0.30),
            ("200002", "X", None, 0.10), ("900901", "X", None, 0.10),                             # B 股按主板
            ("600002", "st小写", "2026-07-03", 0.05),
            ("600003", "某某退", None, 0.10),                                                     # 退市整理期名称不含 ST：按板块
        ]
        for code, name, day, want in cases:
            self.assertEqual(LM.limit_pct(code, name, day), want, (code, name, day))

    def test_date_input_types(self):
        for d in ("2020-08-24", date(2020, 8, 24), pd.Timestamp("2020-08-24 15:00"), pd.Timestamp("2020-08-24").to_pydatetime()):
            self.assertEqual(LM.limit_pct("300750", "", d), 0.20)

    def test_series(self):
        idx = pd.bdate_range("2020-08-20", periods=5)
        self.assertEqual(list(LM.limit_series("300001", "ST乙", idx)), [0.05, 0.05, 0.20, 0.20, 0.20])

    def test_pure_stdlib_import(self):
        head = Path(LM.__file__).read_text(encoding="utf-8").split("def limit_series")[0]
        self.assertNotIn("import pandas", head)


class NewListings(unittest.TestCase):
    CAL = tuple(d.strftime("%Y-%m-%d") for d in pd.bdate_range("2026-10-01", "2026-11-30"))
    IPO = {"301999": "2026-10-28", "603999": "2026-10-28", "920999": "2026-10-28", "600998": "2020-01-02"}

    def ld(self, code, day):
        return LM.listed_days(code, day, self.IPO, self.CAL)

    def test_listed_days_count(self):
        self.assertEqual(self.ld("301999", "2026-10-28"), 1)
        self.assertEqual(self.ld("301999", "2026-10-30"), 3)
        self.assertIsNone(self.ld("301999", "2026-10-27"))
        self.assertIsNone(self.ld("000000", "2026-10-30"))

    def test_no_limit_windows(self):
        self.assertIsNone(LM.limit_pct("301999", "X", "2026-10-30", self.ld("301999", "2026-10-30")))   # 创业板第 3 天：不设涨跌停
        self.assertEqual(LM.limit_pct("301999", "X", "2026-11-04", self.ld("301999", "2026-11-04")), 0.20)  # 第 6 天恢复
        self.assertIsNone(LM.limit_pct("603999", "X", "2026-11-03", 5))                                  # 主板注册制前 5 天
        self.assertEqual(LM.limit_pct("603999", "X", "2026-11-04", 6), 0.10)
        self.assertIsNone(LM.limit_pct("920999", "X", "2026-10-28", 1))                                  # 北交所只有首日
        self.assertEqual(LM.limit_pct("920999", "X", "2026-10-29", 2), 0.30)
        self.assertIsNone(LM.limit_pct("600998", "X", "2020-01-02", 1))                                  # 注册制前主板：首日不对称，返回 None
        self.assertEqual(LM.limit_pct("600998", "X", "2020-01-03", 2), 0.10)
        self.assertEqual(LM.limit_pct("600998", "X", "2020-01-03", None), 0.10)                          # 不知道上市天数：正常幅度


class Callers(unittest.TestCase):
    GRID = [(c, n) for c in ("600000", "000001", "300750", "301000", "688981", "830799", "920001")
            for n in ("", "平安银行", "ST某某", "*ST某某")]

    def test_all_callers_agree(self):
        for c, n in self.GRID:
            want = LM.limit_pct(c, n)
            self.assertEqual(datasource.limit_pct(c, n), want, (c, n))
            self.assertEqual(simtrade.limit_pct(c, n), want, (c, n))
            if not LM.is_st(n):
                self.assertEqual(signal_ledger.limit_pct(c), want, c)

    def test_intraday_same_as_table_for_old_stocks(self):
        for c, n in self.GRID:
            got = intraday.limit_pct(c, n, "2026-11-04")
            self.assertIn(got, (LM.limit_pct(c, n, "2026-11-04"), None), (c, n))     # None 只会出现在新股窗口

    def test_intraday_limit_state_st_now_10pct(self):
        # 主板 ST 昨收 10.00、现价 10.50：旧规则会判涨停，2026-07-06 起不是
        self.assertIsNone(intraday.limit_state("600000", "*ST某", 10.50, 10.00))
        self.assertEqual(intraday.limit_state("600000", "*ST某", 11.00, 10.00), "up")

    def test_trade_list_and_ledger_use_table(self):
        from src import trade_list
        for c in ("600000", "000001", "300750", "301000", "688981", "830799", "920001"):
            self.assertEqual(trade_list.board_limit(c), LM.limit_pct(c, ""), c)
            self.assertEqual(signal_ledger.limit_pct(c), LM.limit_pct(c, ""), c)

    def test_ledger_moves_ipo_window_not_untradable(self):
        """账本：新股上市第 2 天开盘 +30%，不能标成"开盘涨停买不进"（P4）。用补丁替换上市天数，不读真实清单。"""
        cal = ["2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30", "2026-11-02", "2026-11-03"]
        bars = {d: (10.0, 10.0, 10.0, 10.0) for d in cal}
        bars["2026-10-27"] = (13.0, 13.0, 13.0, 13.0)
        from unittest import mock
        with mock.patch.object(LM, "listed_days", lambda code, day=None, *a, **k: 2):
            mv = signal_ledger.moves_for("301999", bars, "2026-10-26", cal, (1,))
        self.assertEqual(mv[0]["lim"], 0)
        with mock.patch.object(LM, "listed_days", lambda code, day=None, *a, **k: 30):
            mv = signal_ledger.moves_for("301999", bars, "2026-10-26", cal, (1,))
        self.assertEqual(mv[0]["lim"], 1)                       # 老股票同样的跳空 = 开盘涨停

    def test_consistency_shares_table(self):
        self.assertIs(cons.DEFAULT_LIMIT_RULES, LM.LIMIT_RULES)
        th = cons.Thresholds()
        for d, want in ((date(2026, 7, 3), 0.05), (date(2026, 7, 6), 0.10)):
            self.assertEqual(cons._limit_for("主板", d, True, th), want)
        self.assertEqual(cons._limit_for("创业板", date(2020, 8, 21), True, th), 0.05)
        self.assertEqual(cons._limit_for("创业板", date(2020, 8, 24), True, th), 0.20)
        self.assertEqual(cons._limit_for("科创板", date(2024, 1, 2), True, th), 0.20)
        self.assertEqual(cons._limit_for("主板", date(2024, 1, 2), False, th), 0.10)


if __name__ == "__main__":
    unittest.main(verbosity=1)
