"""stats.py 单测(T007 / S4):只用合成数据,不读写任何文件。
运行:python3 -B tests/test_validation_stats.py
"""
import math
import os
import statistics
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.validation.stats import (StatsError, perf_stats, max_drawdown_info, decay_ratio,
                                  compare_segments)

# 手算例子:净值 1.1, 0.99, 1.0395, 0.8316, 0.91476
R = [0.1, -0.1, 0.05, -0.2, 0.1]
D = ["d0", "d1", "d2", "d3", "d4"]


class TestDailyStats(unittest.TestCase):
    def test_hand_example(self):
        s = perf_stats(R, dates=D)
        self.assertEqual(s["n_obs"], 5)
        self.assertAlmostEqual(s["cum_return"], 1.1 * 0.9 * 1.05 * 0.8 * 1.1 - 1, places=12)
        self.assertAlmostEqual(s["cum_return"], -0.08524, places=12)
        # 年化 = (1+cum)^(252/5) - 1
        self.assertAlmostEqual(s["ann_return"], 0.91476 ** (252 / 5) - 1, places=12)
        sd = statistics.stdev(R)
        self.assertAlmostEqual(s["ann_vol"], sd * math.sqrt(252), places=12)
        self.assertAlmostEqual(s["sharpe"], statistics.mean(R) / sd * math.sqrt(252), places=12)
        self.assertAlmostEqual(s["day_expectancy"], -0.01, places=12)
        # 3 个盈利日 2 个亏损日:胜率 0.6;盈亏比 0.25/0.3;平均盈亏比 (0.25/3)/(0.3/2)
        self.assertAlmostEqual(s["day_win_rate"], 0.6)
        self.assertAlmostEqual(s["day_profit_factor"], 0.25 / 0.3, places=12)
        self.assertAlmostEqual(s["day_payoff_ratio"], (0.25 / 3) / 0.15, places=12)
        self.assertEqual(s["max_losing_days"], 1)
        self.assertEqual(s["n_zero_days"], 0)

    def test_drawdown_interval_unrecovered(self):
        s = perf_stats(R, dates=D)
        self.assertAlmostEqual(s["max_drawdown"], 0.8316 / 1.1 - 1, places=12)
        self.assertEqual((s["max_dd_peak_date"], s["max_dd_trough_date"]), ("d0", "d3"))
        self.assertIsNone(s["max_dd_recovery_date"])
        self.assertAlmostEqual(s["calmar"], s["ann_return"] / -s["max_drawdown"], places=12)

    def test_drawdown_recovered_and_second_dip(self):
        # 净值 1.1, 0.99, 1.1, 1.21(新高), 0.968(回撤 -20%), 0.9922
        r = [0.1, -0.1, 1.1 / 0.99 - 1, 0.1, -0.2, 0.025]
        info = max_drawdown_info(r, list("abcdef"))
        self.assertAlmostEqual(info["max_drawdown"], -0.2, places=12)
        self.assertEqual((info["peak_idx"], info["trough_idx"]), (3, 4))
        self.assertEqual((info["peak_date"], info["trough_date"]), ("d", "e"))
        self.assertIsNone(info["recovery_idx"])
        # 第一次回撤 -10% 在 idx2 恢复:只看前 4 个点
        info2 = max_drawdown_info(r[:4], list("abcd"))
        self.assertAlmostEqual(info2["max_drawdown"], 0.99 / 1.1 - 1, places=12)
        self.assertEqual((info2["peak_idx"], info2["trough_idx"], info2["recovery_idx"]), (0, 1, 2))
        self.assertEqual(info2["recovery_date"], "c")

    def test_drawdown_from_start(self):
        # 第 0 日就亏:峰值是起点(idx -1),日期 None
        info = max_drawdown_info([-0.1, 0.05], ["a", "b"])
        self.assertAlmostEqual(info["max_drawdown"], -0.1)
        self.assertEqual(info["peak_idx"], -1)
        self.assertIsNone(info["peak_date"])
        self.assertEqual(info["trough_date"], "a")

    def test_no_drawdown(self):
        s = perf_stats([0.01, 0.02, 0.0, 0.01], dates=list("abcd"))
        self.assertEqual(s["max_drawdown"], 0.0)
        self.assertIsNone(s["max_dd_peak_date"])
        self.assertIsNone(s["calmar"])
        self.assertEqual(s["n_zero_days"], 1)
        self.assertEqual(s["day_win_rate"], 1.0)      # 零收益日不进分母
        self.assertIsNone(s["day_profit_factor"])     # 没有亏损日 → 无定义
        self.assertTrue(any("没有亏损日" in w for w in s["warnings"]))

    def test_consecutive_losing_days(self):
        # 零收益日打断连续亏损
        r = [-0.01, -0.01, -0.01, 0.0, -0.01, -0.01, 0.02]
        self.assertEqual(perf_stats(r)["max_losing_days"], 3)

    def test_rf_effect(self):
        r = [0.01, 0.03] * 10
        s0 = perf_stats(r, rf=0.0)
        s1 = perf_stats(r, rf=0.0252)       # 日 rf = 0.0001
        sd = statistics.stdev(r)
        self.assertAlmostEqual(s1["sharpe"], (statistics.mean(r) - 0.0001) / sd * math.sqrt(252), places=12)
        self.assertLess(s1["sharpe"], s0["sharpe"])

    def test_pandas_series_index_as_dates(self):
        try:
            import pandas as pd
        except ImportError:
            self.skipTest("无 pandas")
        s = pd.Series(R, index=pd.date_range("2024-01-02", periods=5, freq="B"))
        out = perf_stats(s)
        self.assertEqual(out["max_dd_peak_date"][:10], "2024-01-02")
        self.assertEqual(out["max_dd_trough_date"][:10], "2024-01-05")

    def test_cross_check_with_metrics(self):
        try:
            import numpy as np
            import pandas as pd
            from src import metrics
        except Exception:
            self.skipTest("无法导入 pandas / src.metrics")
        rng = np.random.default_rng(7)
        r = pd.Series(rng.normal(0.0005, 0.01, 300))
        r.iloc[0] = abs(r.iloc[0])      # 首日非亏损,两种回撤起点口径一致
        mine = perf_stats(r)
        ref = metrics.stats(r)
        self.assertAlmostEqual(mine["ann_return"] * 100, ref["CAGR%"], places=6)
        self.assertAlmostEqual(mine["ann_vol"] * 100, ref["Vol%"], places=6)
        self.assertAlmostEqual(mine["sharpe"], ref["Sharpe"], places=6)
        self.assertAlmostEqual(mine["max_drawdown"] * 100, ref["MaxDD%"], places=6)
        self.assertAlmostEqual(mine["calmar"], ref["Calmar"], places=6)


class TestRobustness(unittest.TestCase):
    def test_empty(self):
        for bad in ([], ()):
            with self.assertRaises(StatsError):
                perf_stats(bad)

    def test_not_iterable_or_wrong_type(self):
        for bad in (None, 5, "abc"):
            with self.assertRaises(StatsError):
                perf_stats(bad)
        with self.assertRaises(StatsError):
            perf_stats([0.01, "x"])
        with self.assertRaises(StatsError):
            perf_stats([0.01, True])

    def test_nan_error_and_drop(self):
        with self.assertRaises(StatsError):
            perf_stats([0.01, float("nan"), 0.02])
        with self.assertRaises(StatsError):
            perf_stats([0.01, None, 0.02])
        s = perf_stats([0.01, float("nan"), 0.02], nan_policy="drop", dates=["a", "b", "c"])
        self.assertEqual((s["n_obs"], s["n_dropped_nan"]), (2, 1))
        self.assertAlmostEqual(s["cum_return"], 1.01 * 1.02 - 1, places=12)
        self.assertEqual(s["end_date"], "c")
        with self.assertRaises(StatsError):
            perf_stats([float("nan")], nan_policy="drop")      # 剔除后为空
        with self.assertRaises(StatsError):
            perf_stats([0.01], nan_policy="bogus")

    def test_inf_and_percent_mistake(self):
        with self.assertRaises(StatsError):
            perf_stats([0.01, float("inf")])
        with self.assertRaises(StatsError):
            perf_stats([0.01, -1.0])
        with self.assertRaises(StatsError):
            perf_stats([0.01, -5.0])      # 误传百分比

    def test_bad_params(self):
        with self.assertRaises(StatsError):
            perf_stats([0.01, 0.02], dates=["a"])
        with self.assertRaises(StatsError):
            perf_stats([0.01, 0.02], periods=0)
        with self.assertRaises(StatsError):
            perf_stats([0.01, 0.02], rf=float("nan"))

    def test_single_point(self):
        s = perf_stats([0.05], dates=["a"])
        self.assertAlmostEqual(s["cum_return"], 0.05)
        for k in ("ann_return", "ann_vol", "sharpe", "calmar", "day_profit_factor"):
            self.assertIsNone(s[k], k)
        self.assertEqual(s["max_drawdown"], 0.0)
        self.assertTrue(any("1 个点" in w for w in s["warnings"]))

    def test_all_zero(self):
        s = perf_stats([0.0] * 40)
        self.assertEqual(s["cum_return"], 0.0)
        self.assertEqual(s["ann_vol"], 0.0)
        self.assertIsNone(s["sharpe"])
        self.assertIsNone(s["day_win_rate"])
        self.assertEqual(s["max_losing_days"], 0)
        self.assertEqual(s["n_zero_days"], 40)
        self.assertTrue(any("标准差为 0" in w for w in s["warnings"]))

    def test_constant_positive_has_no_sharpe(self):
        s = perf_stats([0.001] * 40)
        self.assertIsNone(s["sharpe"])      # 标准差为 0(浮点误差内)不给无穷大

    def test_short_sample_warning(self):
        self.assertTrue(any("样本仅" in w for w in perf_stats(R)["warnings"]))
        self.assertFalse(any("样本仅" in w for w in perf_stats([0.01, -0.01] * 20)["warnings"]))

    def test_all_losses_and_no_wins(self):
        s = perf_stats([-0.01, -0.02, -0.01])
        self.assertEqual(s["day_win_rate"], 0.0)
        self.assertEqual(s["day_profit_factor"], 0.0)      # 盈利之和 0 / 亏损之和 > 0
        self.assertIsNone(s["day_payoff_ratio"])           # 没有盈利日,平均盈利无定义
        self.assertTrue(any("没有盈利日" in w for w in s["warnings"]))
        self.assertEqual(s["max_losing_days"], 3)
        # 与交易级口径一致
        t = perf_stats([-0.01, -0.02], trades=[{"ret": -0.01}, {"ret": -0.02}])
        self.assertEqual(t["trade_profit_factor"], 0.0)
        self.assertEqual(t["day_profit_factor"], 0.0)

    def test_drawdown_peak_retouched_after_recovery(self):
        # 净值 1.25, 1.0, 1.25(回到峰值), 0.875:最大回撤 -30% 应从 idx2 起算,
        # 而不是跨越已恢复的前一次回撤(idx0)
        info = max_drawdown_info([0.25, -0.2, 0.25, -0.3], list("abcd"))
        self.assertAlmostEqual(info["max_drawdown"], -0.3, places=12)
        self.assertEqual((info["peak_idx"], info["trough_idx"]), (2, 3))
        self.assertEqual((info["peak_date"], info["trough_date"]), ("c", "d"))
        self.assertIsNone(info["recovery_idx"])
        # 前一次回撤更深时,区间仍落在第一次:1.25, 0.875(-30%), 1.25(恢复), 1.0(-20%)
        info2 = max_drawdown_info([0.25, -0.3, 1.25 / 0.875 - 1, -0.2], list("abcd"))
        self.assertAlmostEqual(info2["max_drawdown"], -0.3, places=12)
        self.assertEqual((info2["peak_idx"], info2["trough_idx"]), (0, 1))


class TestDatesAndOverflow(unittest.TestCase):
    def test_unordered_or_duplicate_dates_raise(self):
        with self.assertRaises(StatsError):
            perf_stats([0.1, -0.2], dates=["2024-01-03", "2024-01-02"])
        with self.assertRaises(StatsError):
            perf_stats([0.1, -0.2], dates=["a", "a"])

    def test_nan_or_mixed_type_dates_raise(self):
        with self.assertRaises(StatsError):
            perf_stats([0.1, -0.2], dates=["a", float("nan")])
        with self.assertRaises(StatsError):
            perf_stats([0.1, -0.2], dates=["a", None])
        from datetime import date
        with self.assertRaises(StatsError):
            perf_stats([0.1, -0.2], dates=["2024-01-01", date(2024, 1, 2)])

    def test_dates_checked_after_nan_drop(self):
        s = perf_stats([0.1, float("nan"), -0.2], dates=["a", "z", "b"], nan_policy="drop")
        self.assertEqual(s["end_date"], "b")      # 被剔除的 NaN 行对应的日期一并剔除

    def test_exit_date_nan_raises(self):
        t = [{"ret": -0.1, "exit_date": float("nan")}, {"ret": 0.1, "exit_date": 2}]
        with self.assertRaises(StatsError):
            perf_stats(R, trades=t)

    def test_equity_overflow_does_not_hide_drawdown(self):
        # 1100 个 +100% 之后净值 2^1100 超出 float 范围;末日 -50% 的回撤必须仍被算出
        s = perf_stats([1.0] * 1100 + [-0.5])
        self.assertAlmostEqual(s["max_drawdown"], -0.5, places=9)
        self.assertEqual((s["max_dd_peak_idx"], s["max_dd_trough_idx"]), (1099, 1100))
        self.assertIsNone(s["cum_return"])      # 累计收益溢出,降级为 None 并有提示
        self.assertTrue(any("溢出" in w for w in s["warnings"]))


class TestTradeLevel(unittest.TestCase):
    # 日级与交易级口径不同:同一份收益,日胜率 0.6,笔胜率另算
    TR = [{"ret": 0.10, "exit_date": "d1"}, {"ret": -0.04, "exit_date": "d2"},
          {"ret": -0.02, "exit_date": "d3"}, {"ret": 0.06, "exit_date": "d4"}]

    def test_trades_none_placeholder(self):
        s = perf_stats(R)
        self.assertEqual(s["trade_status"], "unavailable")
        self.assertEqual(s["trade_reason"], "未提供交易列表")
        for k in ("trade_count", "trade_win_rate", "trade_profit_factor", "trade_expectancy",
                  "max_losing_trades"):
            self.assertIsNone(s[k], k)
        self.assertIsNotNone(s["day_win_rate"])      # 日级照常

    def test_trade_vs_day_differ_and_hand_check(self):
        s = perf_stats(R, trades=self.TR)
        self.assertEqual(s["trade_status"], "ok")
        self.assertEqual(s["trade_count"], 4)
        self.assertAlmostEqual(s["trade_win_rate"], 0.5)
        self.assertAlmostEqual(s["trade_profit_factor"], 0.16 / 0.06, places=12)
        self.assertAlmostEqual(s["trade_expectancy"], 0.1 / 4, places=12)     # 总和 0.10,共 4 笔
        self.assertEqual(s["max_losing_trades"], 2)
        self.assertNotAlmostEqual(s["trade_win_rate"], s["day_win_rate"])     # 0.5 vs 0.6
        self.assertEqual(s["max_losing_days"], 1)                             # 与笔级的 2 不同

    def test_trades_order_by_exit_date(self):
        # 传入乱序:按 exit_date 排序后连续亏损是 3;按传入顺序只有 1 和 2
        t = [{"ret": -0.01, "exit_date": "d3"}, {"ret": 0.02, "exit_date": "d4"},
             {"ret": -0.01, "exit_date": "d1"}, {"ret": -0.01, "exit_date": "d2"}]
        self.assertEqual(perf_stats(R, trades=t)["max_losing_trades"], 3)

    def test_trades_objects_and_empty(self):
        class T:
            def __init__(self, ret): self.ret = ret
        s = perf_stats(R, trades=[T(0.1), T(-0.1)])
        self.assertAlmostEqual(s["trade_win_rate"], 0.5)
        e = perf_stats(R, trades=[])
        self.assertEqual((e["trade_status"], e["trade_count"]), ("ok", 0))
        self.assertIsNone(e["trade_win_rate"])

    def test_trades_all_wins_pf_none(self):
        s = perf_stats(R, trades=[{"ret": 0.1}, {"ret": 0.2}])
        self.assertIsNone(s["trade_profit_factor"])
        self.assertEqual(s["max_losing_trades"], 0)

    def test_trades_unsortable_or_partial_dates_raise(self):
        from datetime import date
        mixed = [{"ret": -0.1, "exit_date": "2024-01-01"}, {"ret": 0.1, "exit_date": date(2024, 1, 4)},
                 {"ret": -0.1, "exit_date": "2024-01-02"}]
        with self.assertRaises(StatsError):
            perf_stats(R, trades=mixed)          # 字符串与 date 不可比较,不能静默用传入顺序
        partial = [{"ret": -0.1, "exit_date": "d1"}, {"ret": 0.1}]
        with self.assertRaises(StatsError):
            perf_stats(R, trades=partial)        # 只有部分有 exit_date

    def test_trade_order_reported(self):
        self.assertEqual(perf_stats(R, trades=self.TR)["trade_order"], "exit_date")
        self.assertEqual(perf_stats(R, trades=[{"ret": 0.1}])["trade_order"], "input_order")

    def test_trades_invalid(self):
        for bad in ([{"ret": float("nan")}], [{"x": 1}], [{"ret": "a"}], 5, "abc"):
            with self.assertRaises(StatsError):
                perf_stats(R, trades=bad)


class TestCompareSegments(unittest.TestCase):
    TRAIN = [0.01, 0.03] * 20      # 均值 0.02
    VALID = [0.00, 0.02] * 20      # 同样的标准差,均值 0.01 → 夏普恰好减半
    TEST = [-0.01, 0.01] * 20      # 均值 0 → 夏普 0

    def test_decay_ratio_function(self):
        self.assertAlmostEqual(decay_ratio(2.0, 1.0), 0.5)
        self.assertAlmostEqual(decay_ratio(2.0, 3.0), -0.5)       # 变好为负
        self.assertAlmostEqual(decay_ratio(2.0, -1.0), 1.5)       # 变成负数,衰减 > 100%
        self.assertIsNone(decay_ratio(0.0, 1.0))
        self.assertIsNone(decay_ratio(-1.0, 1.0))
        self.assertIsNone(decay_ratio(None, 1.0))
        self.assertIsNone(decay_ratio(1.0, float("nan")))

    def test_decay_values(self):
        c = compare_segments(self.TRAIN, self.VALID, self.TEST)
        tr, va, te = c["train"], c["validate"], c["test"]
        self.assertAlmostEqual(va["sharpe"] / tr["sharpe"], 0.5, places=9)
        self.assertAlmostEqual(c["decay"]["validate"]["sharpe"], 0.5, places=9)
        self.assertAlmostEqual(c["decay"]["test"]["sharpe"], 1.0, places=9)
        self.assertAlmostEqual(c["decay"]["test"]["ann_return"],
                               (tr["ann_return"] - te["ann_return"]) / tr["ann_return"], places=12)
        self.assertEqual(set(c["decay"]["validate"]), {"sharpe", "ann_return", "calmar"})

    def test_hints(self):
        c = compare_segments(self.TRAIN, self.VALID, self.TEST, decay_warn=0.4)
        text = "\n".join(c["hints"])
        self.assertIn("验证段夏普相对训练段下降 50.0%", text)
        self.assertIn("测试段夏普相对训练段下降 100.0%", text)
        self.assertNotIn("有效", text.replace("仅为提示", ""))     # 不下结论
        # 默认阈值 0.5:验证段 50.0% 不超过阈值(浮点边界用 0.6 避开),测试段 100% 超过
        c2 = compare_segments(self.TRAIN, self.VALID, self.TEST, decay_warn=0.6)
        t2 = "\n".join(c2["hints"])
        self.assertNotIn("验证段夏普", t2)
        self.assertIn("测试段夏普", t2)

    def test_sign_flip_hint(self):
        neg = [-0.01, -0.005] * 20
        c = compare_segments(self.TRAIN, self.VALID, neg)
        self.assertTrue(any("符号翻转" in h for h in c["hints"]))

    def test_no_hint_when_stable(self):
        c = compare_segments(self.TRAIN, self.TRAIN, self.TRAIN)
        self.assertEqual(c["hints"], [])
        self.assertAlmostEqual(c["decay"]["validate"]["sharpe"], 0.0, places=12)

    def test_nonpositive_train_gives_none(self):
        c = compare_segments(self.TEST, self.TRAIN, self.VALID)
        self.assertIsNone(c["decay"]["validate"]["sharpe"])
        self.assertTrue(any("训练段夏普缺失或 ≤ 0" in h for h in c["hints"]))

    def test_short_segment_hint(self):
        c = compare_segments(self.TRAIN, [0.01, 0.02, 0.03], self.VALID)
        self.assertTrue(any("验证段仅 3 个交易日" in h for h in c["hints"]))

    def test_missing_segments(self):
        c = compare_segments(self.TRAIN, None, None)
        self.assertEqual(c["validate"]["status"], "unavailable")
        self.assertEqual(c["test"]["status"], "unavailable")
        self.assertIsNone(c["decay"]["test"]["sharpe"])

    def test_invalid_inputs(self):
        with self.assertRaises(StatsError):
            compare_segments(None, self.VALID, self.TEST)
        with self.assertRaises(StatsError):
            compare_segments([], self.VALID, self.TEST)
        with self.assertRaises(StatsError):
            compare_segments(self.TRAIN, [], self.TEST)
        with self.assertRaises(StatsError):
            compare_segments(self.TRAIN, self.VALID, self.TEST, decay_warn=-1)
        with self.assertRaises(StatsError):
            compare_segments(self.TRAIN, [0.01, float("nan")], self.TEST)

    def test_nan_drop_propagates(self):
        c = compare_segments(self.TRAIN, self.VALID + [float("nan")], self.TEST, nan_policy="drop")
        self.assertEqual(c["validate"]["n_dropped_nan"], 1)


if __name__ == "__main__":
    unittest.main()
