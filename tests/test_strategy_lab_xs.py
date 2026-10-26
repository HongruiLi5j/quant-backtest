"""strategy_lab_xs 测试：只用合成数据，不读 data/，不导入 app。"""
import math
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import lab_import_guard as guard  # noqa: E402

(xs,) = guard.import_guarded("strategy_lab_xs")


def make(n=80, codes=("A", "B", "C", "D", "E", "F", "G", "H", "I", "J"), drift=None, seed=0):
    """合成宽表：开盘/收盘/成交量。drift[code] = 每日对数漂移。"""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2021-11-01", periods=n)
    drift = drift or {}
    o = pd.DataFrame({c: 10 * np.exp(np.cumsum(drift.get(c, 0.0) + rng.normal(0, 0.002, n))) for c in codes}, index=idx)
    c = o * 1.0005
    v = pd.DataFrame(1e6, index=idx, columns=codes)
    return o, v, c


class RebalanceTests(unittest.TestCase):
    def test_positions_need_next_day(self):
        self.assertEqual(xs.rebalance_positions(12, 5), [0, 5, 10])
        self.assertEqual(xs.rebalance_positions(11, 5), [0, 5])      # 位置 10 没有 t+1，不能当调仓日
        self.assertEqual(xs.rebalance_positions(10, 5), [0, 5])


class SelectTests(unittest.TestCase):
    def test_top_quantile_and_min_one(self):
        s = pd.Series([5, 4, 3, 2, 1.0], index=list("abcde"))
        self.assertEqual(xs.select_top(s, pd.Series(True, index=s.index), 0.4), ["a", "b"])
        self.assertEqual(xs.select_top(s, pd.Series(True, index=s.index), 0.05), ["a"])

    def test_mask_and_nan(self):
        s = pd.Series([5, np.nan, 3, 2.0], index=list("abcd"))
        ok = pd.Series([False, True, True, True], index=s.index)
        self.assertEqual(xs.select_top(s, ok, 0.5), ["c"])

    def test_ties_by_code_order(self):
        s = pd.Series([1.0, 1.0, 1.0, 1.0], index=list("abcd"))
        self.assertEqual(xs.select_top(s, pd.Series(True, index=s.index), 0.5), ["a", "b"])

    def test_empty(self):
        s = pd.Series([np.nan], index=["a"])
        self.assertEqual(xs.select_top(s, pd.Series(True, index=s.index)), [])


class BuyableTests(unittest.TestCase):
    def test_limit_up_zero_volume_and_nan(self):
        idx = pd.bdate_range("2022-01-03", periods=2)
        o = pd.DataFrame({"u": [10, 11.0], "z": [10, 10.0], "n": [10, np.nan], "ok": [10, 10.2]}, index=idx)
        c = pd.DataFrame({"u": [10, 11.0], "z": [10, 10.0], "n": [10, 10.0], "ok": [10, 10.2]}, index=idx)
        v = pd.DataFrame({"u": [1, 1.0], "z": [1, 0.0], "n": [1, 1.0], "ok": [1, 1.0]}, index=idx)
        b = xs.buyable(o, v, c, 0)
        self.assertEqual(b.to_dict(), {"u": False, "z": False, "n": False, "ok": True})

    def test_just_under_limit_buyable(self):
        idx = pd.bdate_range("2022-01-03", periods=2)
        o = pd.DataFrame({"a": [10, 10.94]}, index=idx)
        c = pd.DataFrame({"a": [10, 10.0]}, index=idx)
        v = pd.DataFrame({"a": [1, 1.0]}, index=idx)
        self.assertTrue(xs.buyable(o, v, c, 0)["a"])


class BacktestTests(unittest.TestCase):
    def setUp(self):
        self.o, self.v, self.c = make(drift={"A": 0.01})
        self.ok = pd.DataFrame(True, index=self.o.index, columns=self.o.columns)
        self.score = pd.DataFrame(0.0, index=self.o.index, columns=self.o.columns)
        self.score["A"] = 1.0                                                   # A 永远排第一

    def bt(self, **kw):
        a = dict(score=self.score, ok=self.ok, open_=self.o, volume=self.v, close=self.c, step=5, cost_bp=0)
        a.update(kw)
        return xs.backtest(**a)

    def test_top_stock_beats_universe(self):
        r = self.bt(q=0.1)
        self.assertGreater(len(r), 5)
        self.assertGreater(r["excess"].mean(), 0)

    def test_open_to_open_timing(self):
        r = self.bt(q=0.1)
        t0 = 0
        expect = self.o["A"].iloc[6] / self.o["A"].iloc[1] - 1          # 信号日 0 → 开盘 1 入场 → 下个调仓日 5 的下一日 6 开盘出场
        self.assertAlmostEqual(r["port_gross"].iloc[0], expect)

    def test_no_lookahead_future_data_irrelevant(self):
        """把信号日之后第 3 天起的数据改掉，不应影响更早的信号选择（选股只看 score 当行，评分已由调用者保证只用 t 及以前）。"""
        r1 = self.bt(q=0.1)
        o2 = self.o.copy()
        o2.iloc[40:] *= 1.5
        r2 = self.bt(q=0.1, open_=o2)
        self.assertEqual(list(r1["port_gross"].iloc[:5]), list(r2["port_gross"].iloc[:5]))

    def test_cost_formula(self):
        r0, r1 = self.bt(q=0.1, cost_bp=0), self.bt(q=0.1, cost_bp=10)
        d = (r0["port"] - r1["port"]).values
        rep = r1["replaced"].values
        np.testing.assert_allclose(d, 2 * rep * 10 / 1e4, atol=1e-12)
        self.assertEqual(rep[0], 1.0)
        self.assertTrue((rep[1:] == 0).all())                             # 持仓不变不再付成本

    def test_cost_x2_lowers_excess(self):
        self.assertLess(self.bt(q=0.1, cost_bp=20)["excess"].mean(), self.bt(q=0.1, cost_bp=10)["excess"].mean())

    def test_unbuyable_held_in_cash(self):
        o, v = self.o.copy(), self.v.copy()
        v.iloc[1, 0] = 0.0                                                 # A 在 t+1 无成交 → 第一期空仓
        r = self.bt(q=0.1, open_=o, volume=v)
        self.assertEqual(r["port_gross"].iloc[0], 0.0)

    def test_signal_date_filter(self):
        r = self.bt(q=0.1, signal_dates=lambda d: d >= self.o.index[20])
        self.assertTrue((r["signal_date"] >= self.o.index[20]).all())

    def test_bench_uses_only_ok_names(self):
        ok = self.ok.copy()
        ok["A"] = False
        r = self.bt(q=0.1, ok=ok)
        r_all = self.bt(q=0.1)
        self.assertNotEqual(r["bench"].iloc[0], r_all["bench"].iloc[0])


class JudgeTests(unittest.TestCase):
    def frame(self, vals, years=None):
        n = len(vals)
        dates = pd.to_datetime(years if years is not None else ["2022-03-01"] * (n // 2) + ["2023-03-01"] * (n - n // 2))
        return pd.DataFrame({"signal_date": dates, "excess": vals, "replaced": 0.2})

    def good(self):
        rng = np.random.default_rng(3)
        return 0.01 + rng.normal(0, 0.002, 40)

    def test_confirmed(self):
        v = xs.judge(self.frame(self.good()), self.frame(self.good() - 0.001))
        self.assertTrue(v["confirmed"])

    def test_needs_positive_mean(self):
        self.assertFalse(xs.judge(self.frame(-self.good()), self.frame(-self.good()))["confirmed"])

    def test_needs_t(self):
        rng = np.random.default_rng(4)
        x = rng.normal(0.0005, 0.02, 40)
        self.assertLess(xs.judge(self.frame(x), self.frame(x))["nw_t"], 2.0)
        self.assertFalse(xs.judge(self.frame(x), self.frame(x))["confirmed"])

    def test_only_t_fails(self):
        # 均值>0、两年均值都>0、成本加倍后均值>0，唯独 t 不够：去掉 t 条件就会误确认
        x = np.array([0.05, -0.04] * 20)
        self.assertGreater(x[:20].mean(), 0)
        v = xs.judge(self.frame(x), self.frame(x))
        self.assertGreater(v["mean_excess"], 0)
        self.assertLess(v["nw_t"], 2.0)
        self.assertFalse(v["confirmed"])

    def test_needs_both_years(self):
        g = self.good()
        x = np.concatenate([g[:20], -g[20:] * 0.1 + 0.0001 * 0])          # 2023 年均值为负
        x = np.concatenate([g[:20], np.full(20, -0.001)])
        self.assertFalse(xs.judge(self.frame(x), self.frame(x))["confirmed"])

    def test_needs_cost_x2(self):
        self.assertFalse(xs.judge(self.frame(self.good()), self.frame(-np.abs(self.good())))["confirmed"])

    def test_empty(self):
        e = pd.DataFrame({"signal_date": pd.to_datetime([]), "excess": [], "replaced": []})
        self.assertFalse(xs.judge(e, e)["confirmed"])


class StatsTests(unittest.TestCase):
    def test_nw_t_small_and_flat(self):
        self.assertTrue(math.isnan(xs.nw_t([1.0, 2.0])))
        self.assertTrue(math.isnan(xs.nw_t([1.0] * 10)))

    def test_nw_t_matches_naive_when_iid(self):
        x = np.random.default_rng(5).normal(0.3, 1, 400)
        naive = x.mean() / (x.std(ddof=0) / math.sqrt(len(x)))
        self.assertAlmostEqual(xs.nw_t(x, lag=0), naive, places=6)


class RandomControlTests(unittest.TestCase):
    def test_deterministic_and_percentile_range(self):
        o, v, c = make(drift={"A": 0.01})
        ok = pd.DataFrame(True, index=o.index, columns=o.columns)
        score = pd.DataFrame(0.0, index=o.index, columns=o.columns)
        score["A"] = 1.0
        real = xs.backtest(score, ok, o, v, c, 5, 0, q=0.1)["excess"].mean()
        p1, m1 = xs.random_percentile(real, score, ok, o, v, c, 5, 0, lambda d: True, n=60, seed=1)
        p2, m2 = xs.random_percentile(real, score, ok, o, v, c, 5, 0, lambda d: True, n=60, seed=1)
        self.assertEqual(p1, p2)
        self.assertGreater(p1, 0.8)                                       # 真实（押对 A）应在随机分布的高位
        self.assertTrue(0 <= p1 <= 1)


class EligibilityTests(unittest.TestCase):
    def test_age_and_st_and_suspension(self):
        from src.market_monitor import factors as fx
        n = 300
        idx = pd.bdate_range("2020-01-01", periods=n)
        cols = ["normal", "st", "new"]
        c = pd.DataFrame(10.0, index=idx, columns=cols)
        c.loc[idx[:200], "new"] = np.nan                                   # 第 201 根才上市
        v = pd.DataFrame(2e7, index=idx, columns=cols)                     # 成交额 2e8，高于 1 亿门槛
        v.loc[idx[:200], "new"] = np.nan
        p = fx.Panel(c, c, c, c, v, pd.Series({"normal": "平安", "st": "*ST 某某", "new": "新股"}))
        ok = xs.eligibility(p, fx)
        self.assertTrue(ok["normal"].iloc[-1])
        self.assertFalse(ok["st"].iloc[-1])
        self.assertFalse(ok["new"].iloc[-1])                               # 上市只有 100 根 < 250
        self.assertFalse(ok["normal"].iloc[100])                           # 前 250 根内不算


class ImportTests(unittest.TestCase):
    def test_import_guard_and_constants(self):
        self.assertNotIn("app", sys.modules)
        self.assertEqual(xs.FACTORS, {"reversal1": 5, "low_vol": 20, "turnover": 20})
        self.assertEqual(xs.TOP_Q, 0.20)
        self.assertEqual(xs.T_MIN, 2.0)


if __name__ == "__main__":
    unittest.main()
