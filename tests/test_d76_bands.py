"""D76：研究页信号跟踪改用全市场等权基准（卖出翻转、附随机基线）；回测 Sharpe 误差范围、胜率 Wilson 区间。
合成数据 / 替换函数，不读写 data/。运行：.venv/bin/python tests/test_d76_bands.py"""
import sys
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import MARKETS  # noqa: E402
from src import trade_signals, tracking  # noqa: E402

DAYS = pd.bdate_range("2026-10-01", periods=40)


def frame(rets):
    c = 10 * np.cumprod(1 + np.asarray(rets))
    return pd.DataFrame({"Open": c, "High": c, "Low": c, "Close": c, "Volume": 1e6}, index=DAYS)


class TrackingEqualWeight(unittest.TestCase):
    def test_ew_fields_and_sell_flip(self):
        up = frame([0.01] * 40)            # 每天 +1%：5 日约 +4%
        dn = frame([-0.01] * 40)
        prices = {"AAA": up, "BBB": dn, "510300": frame([0.0] * 40)}
        day = DAYS[5].strftime("%Y-%m-%d")
        hist = pd.DataFrame([{"market": "cn", "signal_date": day, "code": "AAA", "name": "a", "strategy": "blend2", "action": "买入"},
                             {"market": "cn", "signal_date": day, "code": "BBB", "name": "b", "strategy": "blend2", "action": "卖出"}])
        ew = {(day, 5): 0.0, (day, 20): 0.0}            # 当天全市场平均 0
        rnd = {(day, 5): (0.45, 0.55), (day, 20): (0.45, 0.55)}
        with mock.patch.object(tracking, "_prices", lambda m, code, cfg: prices.get(code)), \
             mock.patch.object(trade_signals, "_ledger_baseline", lambda db=None: (ew, rnd)):
            ev = tracking.evaluate("cn", MARKETS["cn"], hist=hist)
        rows = {r["action"]: r for r in ev["summary"]}
        self.assertEqual(rows["买入"]["ew_n5"], 1)
        self.assertEqual(rows["买入"]["ew_dir5"], 100.0)          # 涨的股票，买入 = 方向正确
        self.assertAlmostEqual(rows["买入"]["ew_random5"], 45.0)
        self.assertEqual(rows["卖出"]["ew_dir5"], 100.0)          # 跌的股票，卖出 = 方向正确（翻转）
        self.assertAlmostEqual(rows["卖出"]["ew_random5"], 55.0)  # 卖出的随机基线 = 跑输平均的占比
        self.assertAlmostEqual(rows["卖出"]["ew_edge5"], 45.0)
        self.assertEqual(rows["买入"]["ew_n60"], 0)               # 账本没有 60 日基准：不编
        self.assertIsNone(rows["买入"]["ew_dir60"])
        self.assertIn("beat5", rows["买入"])                       # 旧字段保留

    def test_no_ledger_no_crash(self):
        day = DAYS[5].strftime("%Y-%m-%d")
        hist = pd.DataFrame([{"market": "cn", "signal_date": day, "code": "AAA", "name": "a", "strategy": "blend2", "action": "买入"}])
        with mock.patch.object(tracking, "_prices", lambda m, code, cfg: frame([0.01] * 40)), \
             mock.patch.object(trade_signals, "_ledger_baseline", lambda db=None: ({}, {})):
            ev = tracking.evaluate("cn", MARKETS["cn"], hist=hist)
        self.assertEqual(ev["summary"][0]["ew_n5"], 0)


class BacktestBands(unittest.TestCase):
    def test_sharpe_se_formula_and_basic(self):
        from src import analytics as A
        self.assertAlmostEqual(A.sharpe_se(0.0, 4.0), 0.5)
        self.assertAlmostEqual(A.sharpe_se(1.0, 6.0), np.sqrt(1.5 / 6.0))
        self.assertIsNone(A.sharpe_se(float("nan"), 5.0))
        r = pd.Series(np.random.default_rng(1).normal(0.0004, 0.01, 2520), index=pd.bdate_range("2015-01-01", periods=2520))
        b = A.basic(r)
        self.assertAlmostEqual(b["sharpe_se"], A.sharpe_se(b["sharpe"], 2520 / 252), places=3)
        self.assertIn("独立同分布", b["sharpe_se_note"])

    def test_win_rate_wilson(self):
        from src import analytics as A
        t = A.trade_stats([0.1] * 9 + [-0.01] * 15, [10] * 24)
        self.assertEqual(t["win"], 37.5)
        self.assertEqual((t["win_lo"], t["win_hi"]), (21.2, 57.3))           # 24 笔：区间很宽
        self.assertEqual(A.wilson(0, 0), (None, None))


if __name__ == "__main__":
    unittest.main()
