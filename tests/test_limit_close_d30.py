"""D30：收盘涨停的买入信号提示 + 可成交率。纯计算 / 临时账本，不读写 data/。
运行：.venv/bin/python tests/test_limit_close_d30.py"""
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import signal_ledger as L  # noqa: E402
from src import trade_list as T  # noqa: E402

CAL = ["2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23", "2026-10-26", "2026-10-27"]
ENOUGH = {"enough": True, "close_limit": {"rate": 0.2, "n": 50}}


class Flags(unittest.TestCase):
    def frame(self, o, h, l, c, prev=10.0):
        return pd.DataFrame({"Open": [prev, o], "High": [prev, h], "Low": [prev, l], "Close": [prev, c]})

    def test_close_at_limit_not_one_word(self):
        f = T.limit_flags("600001", self.frame(10.5, 11.0, 10.3, 11.0), close_rate=ENOUGH)      # 盘中开过板，收盘涨停
        self.assertEqual(f, ["今日收盘涨停：历史上约 20% 次日开盘仍涨停买不进（账本 50 条）"])

    def test_one_word_keeps_original(self):
        self.assertEqual(T.limit_flags("600001", self.frame(11.0, 11.0, 11.0, 11.0), close_rate=ENOUGH), ["今日一字涨停，明天可能仍买不进"])
        self.assertEqual(T.limit_flags("600001", self.frame(9.0, 9.0, 9.0, 9.0), close_rate=ENOUGH), ["今日一字跌停，明天可能仍卖不出"])

    def test_not_at_limit_or_small_sample(self):
        self.assertEqual(T.limit_flags("600001", self.frame(10.5, 10.9, 10.3, 10.9), close_rate=ENOUGH), [])
        self.assertEqual(T.limit_flags("300001", self.frame(10.5, 11.0, 10.3, 11.0), close_rate=ENOUGH), [])       # 创业板 +10% 不是涨停
        self.assertEqual(T.limit_flags("600001", self.frame(10.5, 11.0, 10.3, 11.0), close_rate={"enough": False}),
                         ["今日收盘涨停：明天开盘可能仍涨停买不进（历史比例样本不足）"])                      # 比例不写死


class FakeProvider:
    def __init__(self, bars):
        self._b = bars

    def bars(self, code):
        return self._b.get(code, {})


class Ledger(unittest.TestCase):
    def test_ensure_and_stats(self):
        c = L.connect(Path(tempfile.mkdtemp()) / "t.db")
        d0, d1 = CAL[1], CAL[2]
        c.execute("INSERT INTO days VALUES(?,?,?)", (d0, 0, "x"))
        bars = {
            "600001": {CAL[0]: (10, 10, 10, 10.0), d0: (10.5, 11, 10.3, 11.0), d1: (12.1, 12.1, 12.1, 12.1)},   # 收盘涨停 → 次日一字涨停
            "600002": {CAL[0]: (10, 10, 10, 10.0), d0: (10.5, 11, 10.3, 11.0), d1: (11.5, 11.8, 11.2, 11.6)},   # 收盘涨停 → 次日能买
            "600003": {CAL[0]: (10, 10, 10, 10.0), d0: (10.2, 10.4, 10.1, 10.3), d1: (10.3, 10.5, 10.2, 10.4)},  # 普通
            "600004": {CAL[0]: (10, 10, 10, 10.0), d0: (11.0, 11.0, 11.0, 11.0), d1: (12.1, 12.1, 12.1, 12.1)},  # 一字板 → 次日一字
        }
        for code in bars:
            c.execute("INSERT INTO signals VALUES(?,?,?,?,?,?,?,?,?,?,?)", (d0, "blend2", code, "X", "买入", 0, 1, 10.0, None, 0, "[]"))
            c.execute("INSERT INTO signals VALUES(?,?,?,?,?,?,?,?,?,?,?)", (d0, "ctrl_random_a", code, "X", "买入", 0, 1, 10.0, None, 0, "[]"))
            for m in L.moves_for(code, bars[code], d0, CAL, (1,)):
                c.execute("INSERT INTO moves VALUES(?,?,?,?,?,?,?)", (d0, code, 1, m["ret"], m["gap"], m["lim"], m["pump"]))
        c.commit()
        self.assertEqual(L.ensure_limit_close(c, FakeProvider(bars)), 4)            # 对照策略不重复记
        self.assertEqual(L.ensure_limit_close(c, FakeProvider(bars)), 0)            # 已记过的不重算
        s = L.limit_close_stats(c)
        self.assertEqual((s["close_limit"]["n"], s["close_limit"]["blocked"]), (2, 1))
        self.assertAlmostEqual(s["close_limit"]["rate"], 0.5)
        self.assertEqual((s["other"]["n"], s["other"]["blocked"]), (1, 0))
        self.assertEqual((s["yizi"]["n"], s["yizi"]["blocked"]), (1, 1))            # 一字板单独一组，不混进「收盘涨停」的比例
        self.assertFalse(s["enough"])                                                 # 2 条、1 天：样本不足
        p = [r for r in L.precision(c, "blend2", CAL[-1]) if r["action"] == "买入" and r["h"] == 1][0]
        self.assertEqual(p["untradable"], 2)
        self.assertAlmostEqual(p["tradable_rate"], 2 / 4)                             # 可成交率 = 能成交 ÷ 全部


if __name__ == "__main__":
    unittest.main()
