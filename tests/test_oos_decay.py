"""C08 样本外逐年衰减脚本的测试：.venv/bin/python tests/test_oos_decay.py

全部用临时目录里的合成行情（src.data.synthetic 随机游走）和手算的小例子，不读写 data/，不联网。
"""
import math
import sys
import tempfile
import unittest
from itertools import combinations
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats as sps

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from config import MARKETS  # noqa: E402
from src import data, metrics  # noqa: E402
import oos_decay as O  # noqa: E402

FAKE = {"universe": {"AAA": "测试品种", "CSH": "测试现金"}, "cash": "CSH", "cost_bp": 5, "stock_cost_bp": 8,
        "dual_momentum": {"risky": ("AAA",), "safe": "CSH", "cash": "CSH"}}


def series(vals, start="2020-01-02"):
    return pd.Series(vals, index=pd.bdate_range(start, periods=len(vals)), dtype=float)


def zeros_like(s):
    return pd.Series(0.0, index=s.index)


def fake_df(start="2012-01-02", n=3800, seed=0):
    return data.synthetic(n=n, seed=seed, start=start)


class YearStatsTests(unittest.TestCase):
    def test_mdd_hand_computed(self):
        # 净值 1.1 → 0.88 → 0.924，峰值 1.1，最大回撤 0.88/1.1 − 1 = −20%
        r = series([0.10, -0.20, 0.05])
        s = O.year_stats(r, zeros_like(r), zeros_like(r))
        self.assertAlmostEqual(s["mdd"], -0.20)

    def test_mdd_counts_first_day_loss(self):
        # 年初净值 1 也是峰值：第一天就亏 10%，回撤是 −10%（不能把第一天的净值当峰值而漏掉）
        r = series([-0.10, 0.05])
        self.assertAlmostEqual(O.year_stats(r, zeros_like(r), zeros_like(r))["mdd"], -0.10)

    def test_ann_ret_full_year_equals_total_return(self):
        r = series([0.0004] * 252)
        s = O.year_stats(r, zeros_like(r), zeros_like(r))
        self.assertAlmostEqual(s["ann_ret"], 1.0004 ** 252 - 1)

    def test_ann_ret_annualises_partial_year(self):
        r = series([0.001] * 126)               # 半年，年化 = (总收益)^(252/126) − 1
        s = O.year_stats(r, zeros_like(r), zeros_like(r))
        self.assertAlmostEqual(s["ann_ret"], (1.001 ** 126) ** 2 - 1)

    def test_sharpe_matches_metrics(self):
        rng = np.random.default_rng(3)
        r, rf = series(rng.normal(0.0005, 0.01, 252)), series(np.full(252, 0.0001))
        s = O.year_stats(r, rf, zeros_like(r))
        self.assertAlmostEqual(s["sharpe"], metrics.sharpe(r - rf), places=12)

    def test_flat_year_sharpe_zero_and_flagged(self):
        rf = series(np.full(252, 0.0001))
        s = O.year_stats(rf.copy(), rf, zeros_like(rf))     # 整年空仓：收益恰好等于现金收益
        self.assertTrue(s["flat"])
        self.assertEqual(s["sharpe"], 0.0)
        r = series(np.random.default_rng(1).normal(0, 0.01, 252))
        self.assertFalse(O.year_stats(r, zeros_like(r), zeros_like(r))["flat"])

    def test_excess_is_difference_of_annual_returns(self):
        rng = np.random.default_rng(4)
        r, b = series(rng.normal(0.0004, 0.01, 252)), series(rng.normal(0.0006, 0.012, 252))
        s = O.year_stats(r, zeros_like(r), b)
        self.assertAlmostEqual(s["excess"], (1 + r).prod() - (1 + b).prod(), places=12)
        self.assertAlmostEqual(s["bench_ret"], (1 + b).prod() - 1, places=12)

    def test_yearly_table_splits_calendar_years(self):
        r = pd.Series(0.0002, index=pd.bdate_range("2012-01-02", "2026-03-31"))
        t = O.yearly_table(r, zeros_like(r), zeros_like(r))
        self.assertEqual(list(t["year"]), list(range(2013, 2027)))
        self.assertEqual(set(t.loc[t["year"] <= 2019, "period"]), {"IS"})
        self.assertEqual(set(t.loc[(t["year"] >= 2020) & (t["year"] <= 2025), "period"]), {"OOS"})
        last = t.iloc[-1]
        self.assertEqual((last["year"], last["period"], last["complete"]), (2026, "ref", False))   # 2026 只作参考，不进检验
        self.assertTrue(t.loc[t["year"] <= 2025, "complete"].all())

    def test_short_year_not_complete(self):
        r = pd.Series(0.0002, index=pd.bdate_range("2013-07-01", "2026-03-31"))       # 2013 只有半年
        t = O.yearly_table(r, zeros_like(r), zeros_like(r)).set_index("year")
        self.assertFalse(t.loc[2013, "complete"])
        self.assertTrue(t.loc[2014, "complete"])


class TestStatTests(unittest.TestCase):
    def setUp(self):
        rng = np.random.default_rng(7)
        self.a, self.b = rng.normal(1.0, 0.8, 7), rng.normal(0.5, 1.5, 6)     # 方差故意不等

    def test_welch_matches_scipy(self):
        w = O.welch(self.a, self.b)
        ref = sps.ttest_ind(self.a, self.b, equal_var=False)
        self.assertAlmostEqual(w["t"], ref.statistic, places=10)
        self.assertAlmostEqual(w["p_two"], ref.pvalue, places=10)
        self.assertAlmostEqual(w["df"], ref.df, places=8)
        ref1 = sps.ttest_ind(self.a, self.b, equal_var=False, alternative="greater")
        self.assertAlmostEqual(w["p_decay"], ref1.pvalue, places=10)
        self.assertAlmostEqual(w["diff"], self.a.mean() - self.b.mean())

    def test_welch_degenerate(self):
        w = O.welch(np.array([1.0]), self.b)
        self.assertTrue(np.isnan(w["t"]) and np.isnan(w["p_two"]))
        w = O.welch(np.ones(5), np.ones(5))              # 两段方差都是 0
        self.assertTrue(np.isnan(w["t"]))

    def test_perm_complete_separation(self):
        a, b = np.arange(10.0, 17.0), np.arange(0.0, 6.0)      # 样本内全部大于样本外
        p2, pd_ = O.perm_pvalues(a, b)
        self.assertAlmostEqual(pd_, 1 / math.comb(13, 7))       # 1716 种分法里只有真实这一种这么极端
        self.assertGreaterEqual(p2, pd_)

    def test_perm_uniform_under_null(self):
        """各年份同分布时，置换 p 值 ≤ 0.05 的比例应接近 5%（精确检验，保守但不该偏离太多）。"""
        rng = np.random.default_rng(11)
        ps = [O.perm_pvalues(rng.normal(size=6), rng.normal(size=5))[0] for _ in range(300)]
        self.assertTrue(0.02 <= np.mean(np.array(ps) <= 0.05) <= 0.09)

    def test_perm_close_to_welch_on_normal_data(self):
        p2, _ = O.perm_pvalues(self.a, self.b)
        self.assertAlmostEqual(p2, O.welch(self.a, self.b)["p_two"], delta=0.12)

    def test_holm_known_values(self):
        got = O.holm([0.01, 0.04, 0.03])
        for g, w in zip(got, [0.03, 0.06, 0.06]):
            self.assertAlmostEqual(g, w)
        got = O.holm([0.5, float("nan"), 0.01])
        self.assertAlmostEqual(got[0], 0.5)
        self.assertAlmostEqual(got[2], 0.02)                # nan 不占名额：m = 2
        self.assertTrue(np.isnan(got[1]))
        self.assertTrue(all(g <= 1 for g in O.holm([0.9, 0.8, 0.95])))

    def test_power_basics(self):
        self.assertAlmostEqual(O.power_two_sample(0.0, 7, 6), O.ALPHA, places=6)       # 没有差异时「检出」的概率 = 显著性水平
        self.assertLess(O.power_two_sample(1.0, 7, 6), O.power_two_sample(1.0, 14, 12))
        self.assertLess(O.power_two_sample(0.5, 7, 6), O.power_two_sample(1.5, 7, 6))

    def test_power_matches_simulation(self):
        rng = np.random.default_rng(5)
        hits = sum(sps.ttest_ind(rng.normal(1.0, 1, 7), rng.normal(0, 1, 6)).pvalue < 0.05 for _ in range(6000))
        self.assertAlmostEqual(O.power_two_sample(1.0, 7, 6), hits / 6000, delta=0.025)

    def test_mde_and_n_needed(self):
        d = O.min_detectable_d(7, 6)
        self.assertAlmostEqual(O.power_two_sample(d, 7, 6), O.POWER, places=4)
        self.assertGreater(d, 1.5)                          # 7 年 vs 6 年只能检出 1.5 个标准差以上的差异
        self.assertEqual(O.n_needed(1.0), 17)               # 教科书值：d=1、功效 80%、双边 5%，每组 17 个
        self.assertEqual(O.n_needed(0.5), 64)

    def test_test_rows_use_complete_years_only(self):
        """不完整年份、2026 年都不能进检验。"""
        rng = np.random.default_rng(2)
        r = series(rng.normal(0.0003, 0.01, 3500), start="2012-06-01")
        r = r.loc[:O.CUTOFF]
        t = O.yearly_table(r, zeros_like(r), zeros_like(r))
        rows = {x["metric"]: x for x in O.test_rows("G", "s", t)}
        self.assertEqual((rows["sharpe"]["n_is"], rows["sharpe"]["n_oos"]), (7, 6))
        t2 = t[t["year"] != 2013]
        rows2 = {x["metric"]: x for x in O.test_rows("G", "s", t2)}
        self.assertEqual(rows2["sharpe"]["n_is"], 6)

    def test_buy_hold_has_no_excess_test(self):
        r = series(np.random.default_rng(2).normal(0.0003, 0.01, 3500), start="2012-06-01").loc[:O.CUTOFF]
        t = O.yearly_table(r, zeros_like(r), r)
        self.assertEqual({x["metric"] for x in O.test_rows("G", O.BH, t)}, {"ann_ret", "sharpe"})

    def test_add_holm_only_over_seven_formal(self):
        rows = []
        for i, s in enumerate([*O.ORDER, "avg7", O.BH]):
            rows.append({"group": "G", "strategy": s, "metric": "sharpe", "p_decay": 0.01 * (i + 1)})
        out = O.add_holm(pd.DataFrame(rows))
        self.assertTrue(out.loc[out.strategy.isin(O.ORDER), "p_decay_holm"].notna().all())
        self.assertTrue(out.loc[~out.strategy.isin(O.ORDER), "p_decay_holm"].isna().all())
        self.assertAlmostEqual(out.loc[out.strategy == O.ORDER[0], "p_decay_holm"].iloc[0], 0.07)   # 0.01 × 7


class BacktestTests(unittest.TestCase):
    def cash(self, df):
        return pd.Series(0.00005, index=df.index)

    def test_rejects_data_after_cutoff(self):
        df = fake_df()
        self.assertGreater(df.index[-1], pd.Timestamp(O.CUTOFF))
        with self.assertRaises(ValueError):
            O.instrument_returns(df, "AAA", FAKE, self.cash(df), 0.0005, None)
        with self.assertRaises(ValueError):
            O.check_end("2026-04-01")

    def test_no_future_leak_into_past_years(self):
        """把 2025 年底之后（含 2026 年一季度）的行情全部砍掉或乱改，2013~2025 的年度指标必须逐位不变。"""
        df = fake_df().loc[:O.CUTOFF]
        cut = pd.Timestamp("2025-12-31")
        base = O.instrument_returns(df, "AAA", FAKE, self.cash(df), 0.0005, None)
        short = df.loc[:cut]
        got = O.instrument_returns(short, "AAA", FAKE, self.cash(short), 0.0005, None)
        bad = df.copy()
        after = bad.index > cut
        self.assertGreater(after.sum(), 30)
        bad.loc[after, ["Open", "High", "Low", "Close"]] *= 3.0
        mess = O.instrument_returns(bad, "AAA", FAKE, self.cash(bad), 0.0005, None)
        for name in [*O.ORDER, O.BH]:
            a = base[name].loc[:cut]
            pd.testing.assert_series_equal(a, got[name].loc[:cut], check_names=False, obj=name)
            # 乱改的是 2026 年的价格：2025-12-31 及以前的日收益不受影响
            pd.testing.assert_series_equal(a, mess[name].loc[:cut], check_names=False, obj=name)

    def test_warmup_dropped(self):
        df = fake_df(start="2012-01-02").loc[:O.CUTOFF]
        out = O.instrument_returns(df, "AAA", FAKE, self.cash(df), 0.0005, None)
        self.assertEqual(out["blend2"].index[0], df.index[O.WARM])

    def test_strategy_list_is_the_seven_formal(self):
        self.assertEqual(sorted(O.ORDER), sorted(O.STRATS))
        self.assertEqual(len(O.ORDER), 7)

    def test_end_to_end(self):
        """用合成行情冒充两个市场的 universe，跑完整脚本，检查输出文件、行数、边界。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "data"
            for i, mk in enumerate(("us", "cn")):
                mc = MARKETS[mk]
                d = root / "daily" / mk
                d.mkdir(parents=True, exist_ok=True)
                for j, s in enumerate(mc["universe"]):
                    data.synthetic(n=3700, seed=10 * i + j, start="2012-06-01").to_csv(d / f"{s}.csv")
            out = Path(tmp) / "out"
            O.main(["--out-dir", str(out), "--data-dir", str(root)])
            for f in ("oos_decay_yearly.csv", "oos_decay_tests.csv", "oos_decay_power.csv", "oos_decay_meta.json",
                      "oos_decay_tables.md", "fig_yearly_sharpe_main.png", "fig_dumbbell_excess.png"):
                self.assertTrue((out / f).exists(), f)
            y = pd.read_csv(out / "oos_decay_yearly.csv", dtype={"group": str})
            self.assertEqual(set(y["group"]), {"SPY", "510300", "us_ew", "cn_ew"})
            self.assertEqual(set(y["strategy"]), {*O.ORDER, "avg7", O.BH})
            self.assertLessEqual(y["year"].max(), 2026)
            self.assertEqual(y.loc[y["year"] == 2026, "complete"].sum(), 0)           # 2026 不进检验
            t = pd.read_csv(out / "oos_decay_tests.csv", dtype={"group": str})
            self.assertTrue((t.loc[t.strategy.isin(O.ORDER), "p_decay_holm"] >= t.loc[t.strategy.isin(O.ORDER), "p_decay"] - 1e-12).all())
            self.assertTrue(((t["p_two"].dropna() >= 0) & (t["p_two"].dropna() <= 1)).all())
            self.assertTrue(set(t["metric"]) == {"ann_ret", "sharpe", "excess"})
            # 年份只有 2013~2025，每组每策略 13 个完整年（合成数据 2012-06 起，预热后 2013 年也完整）或少 1 个
            sub = t[(t.group == "SPY") & (t.strategy == "blend2") & (t.metric == "sharpe")].iloc[0]
            self.assertGreaterEqual(sub["n_is"] + sub["n_oos"], 12)
            md = (out / "oos_decay_tables.md").read_text(encoding="utf-8")
            self.assertIn("Welch", md)


if __name__ == "__main__":
    unittest.main(verbosity=2)
