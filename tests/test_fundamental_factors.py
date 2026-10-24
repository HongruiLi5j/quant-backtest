"""价值+基本面因子库测试(T014 S2):全部合成数据手算核对,不联网、不读 data/。

运行:.venv/bin/python tests/test_fundamental_factors.py
"""
import logging
import math
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fundamental import factors as F  # noqa: E402

ASOF = "2024-06-28"


def make_df():
    """4 只合成股票:正常 / 高 ROE 高杠杆 / 亏损且净资产为负 / 上年亏损基数。"""
    rows = {
        "000001": dict(market_cap=1000, close_raw=10, net_profit_ttm=100, net_profit_ttm_prev=80,
                       revenue_ttm=500, revenue_ttm_prev=400, operating_cost_ttm=300, operating_profit_ttm=120,
                       equity_parent=800, total_assets=2000, total_liabilities=1000, cfo_ttm=150, dps_ttm=0.5),
        "000002": dict(market_cap=2000, close_raw=20, net_profit_ttm=100, net_profit_ttm_prev=100,
                       revenue_ttm=1000, revenue_ttm_prev=800, operating_cost_ttm=900, operating_profit_ttm=50,
                       equity_parent=500, total_assets=1000, total_liabilities=800, cfo_ttm=50, dps_ttm=0.2),
        "000003": dict(market_cap=500, close_raw=5, net_profit_ttm=-50, net_profit_ttm_prev=20,
                       revenue_ttm=200, revenue_ttm_prev=300, operating_cost_ttm=150, operating_profit_ttm=-40,
                       equity_parent=-100, total_assets=400, total_liabilities=500, cfo_ttm=-20, dps_ttm=0.0),
        "000004": dict(market_cap=800, close_raw=8, net_profit_ttm=40, net_profit_ttm_prev=-10,
                       revenue_ttm=400, revenue_ttm_prev=400, operating_cost_ttm=300, operating_profit_ttm=60,
                       equity_parent=400, total_assets=800, total_liabilities=200, cfo_ttm=80, dps_ttm=0.1),
    }
    df = pd.DataFrame.from_dict(rows, orient="index").astype(float)
    df["available_date"] = pd.Timestamp("2024-04-30")
    return df


NAN = math.nan
# 手算期望值(行顺序 000001..000004)
EXPECTED = {
    "fv_ep": [0.1, 0.05, -0.1, 0.05],                    # 100/1000, 100/2000, -50/500, 40/800
    "fv_bp": [0.8, 0.25, NAN, 0.5],                      # 800/1000, 500/2000, 净资产<0 缺失, 400/800
    "fv_sp": [0.5, 0.5, 0.4, 0.5],                       # 500/1000, 1000/2000, 200/500, 400/800
    "fv_dy": [0.05, 0.01, 0.0, 0.0125],                  # 0.5/10, 0.2/20, 0/5, 0.1/8
    "fv_roe": [0.125, 0.2, NAN, 0.1],                    # 100/800, 100/500, 缺失, 40/400
    "fv_roa": [0.05, 0.1, -0.125, 0.05],                 # 100/2000, 100/1000, -50/400, 40/800
    "fv_gross_margin": [0.4, 0.1, 0.25, 0.25],           # 200/500, 100/1000, 50/200, 100/400
    "fv_op_margin": [0.24, 0.05, -0.2, 0.15],            # 120/500, 50/1000, -40/200, 60/400
    "fv_ni_yoy": [0.25, 0.0, -3.5, NAN],                 # 20/80, 0/100, -70/20, 基数-10 缺失
    "fv_rev_yoy": [0.25, 0.25, -1 / 3, 0.0],             # 100/400, 200/800, -100/300, 0/400
    "fv_debt_ratio": [0.5, 0.8, 1.25, 0.25],             # 1000/2000, 800/1000, 500/400, 200/800
    "fv_cfo_ni": [1.5, 0.5, NAN, 2.0],                   # 150/100, 50/100, 利润<0 缺失, 80/40
}


class RegistryTests(unittest.TestCase):
    def test_twelve_factors_and_groups(self):
        self.assertEqual(len(F.REGISTRY), 12)
        self.assertEqual(set(F.REGISTRY), set(EXPECTED))
        count = {}
        for m in F.REGISTRY.values():
            count[m.group] = count.get(m.group, 0) + 1
        self.assertEqual(count, {"value": 4, "quality": 4, "growth": 2, "risk": 2})

    def test_meta_complete(self):
        for m in F.REGISTRY.values():
            self.assertTrue(m.formula and m.source and m.name and m.update_freq, m.id)
            self.assertIn(m.direction, (1, -1))
            self.assertTrue(0 < m.quantile < 1)
            self.assertTrue(len(m.needs) >= 2)
        self.assertEqual([m.id for m in F.REGISTRY.values() if m.direction == -1], ["fv_debt_ratio"])

    def test_named_functions_exist(self):
        df = make_df()
        for fid in F.FACTOR_IDS:
            got = getattr(F, fid)(df, ASOF)
            pd.testing.assert_series_equal(got, F.score(fid, df, ASOF))

    def test_independent_and_offline(self):
        src = (ROOT / "src" / "fundamental" / "factors.py").read_text(encoding="utf-8")
        self.assertNotIn("import market_monitor", src)
        self.assertNotIn("from src.market_monitor", src)
        for bad in ("requests", "urllib", "socket", "akshare", "baostock", "open("):
            self.assertNotIn(bad, src)


class HandComputedTests(unittest.TestCase):
    def test_each_factor_matches_hand_values(self):
        df = make_df()
        for fid, exp in EXPECTED.items():
            got = F.score(fid, df, ASOF)
            self.assertEqual(list(got.index), list(df.index))
            self.assertEqual(got.name, fid)
            for code, e, g in zip(df.index, exp, got.values):
                if math.isnan(e):
                    self.assertTrue(math.isnan(g), "%s %s expected NaN got %r" % (fid, code, g))
                else:
                    self.assertAlmostEqual(g, e, places=9, msg="%s %s" % (fid, code))

    def test_special_cases(self):
        df = make_df()
        self.assertLess(F.score("fv_ep", df, ASOF)["000003"], 0)           # 亏损:EP 为负而非缺失
        self.assertTrue(math.isnan(F.score("fv_bp", df, ASOF)["000003"]))   # 负净资产
        self.assertTrue(math.isnan(F.score("fv_roe", df, ASOF)["000003"]))
        self.assertTrue(math.isnan(F.score("fv_ni_yoy", df, ASOF)["000004"]))  # 上年亏损基数
        self.assertAlmostEqual(F.score("fv_rev_yoy", df, ASOF)["000004"], 0.0)
        self.assertTrue(math.isnan(F.score("fv_cfo_ni", df, ASOF)["000003"]))  # 利润<=0
        self.assertEqual(F.score("fv_dy", df, ASOF)["000003"], 0.0)         # 零分红是 0 不是缺失

    def test_zero_and_inf_denominators(self):
        df = make_df()
        df.loc["000001", "market_cap"] = 0
        df.loc["000002", "total_assets"] = 0
        df.loc["000004", "revenue_ttm"] = np.inf
        self.assertTrue(math.isnan(F.score("fv_ep", df, ASOF)["000001"]))
        self.assertTrue(math.isnan(F.score("fv_roa", df, ASOF)["000002"]))
        self.assertTrue(math.isnan(F.score("fv_debt_ratio", df, ASOF)["000002"]))
        self.assertTrue(math.isnan(F.score("fv_sp", df, ASOF)["000004"]))
        self.assertTrue(np.isfinite(F.score_all(df, ASOF).fillna(0).values).all())

    def test_inconsistent_rows_are_nan_and_logged(self):
        df = make_df()
        df.loc["000001", "operating_cost_ttm"] = -5      # 负成本:数据矛盾
        df.loc["000002", "total_liabilities"] = -1
        df.loc["000004", "dps_ttm"] = -0.1               # 负分红
        issues = []
        self.assertTrue(math.isnan(F.score("fv_gross_margin", df, ASOF, issues)["000001"]))
        self.assertTrue(math.isnan(F.score("fv_debt_ratio", df, ASOF, issues)["000002"]))
        self.assertTrue(math.isnan(F.score("fv_dy", df, ASOF, issues)["000004"]))
        self.assertIn(("fv_gross_margin", "inconsistent_rows:1"), issues)
        self.assertIn(("fv_debt_ratio", "inconsistent_rows:1"), issues)

    def test_string_numbers_and_garbage(self):
        df = make_df().astype({"net_profit_ttm": object})
        df.loc["000001", "net_profit_ttm"] = "abc"
        df.loc["000002", "net_profit_ttm"] = "100"
        out = F.score("fv_ep", df, ASOF)
        self.assertTrue(math.isnan(out["000001"]))
        self.assertAlmostEqual(out["000002"], 0.05)


class RankTests(unittest.TestCase):
    def test_rank_positive_direction_with_ties(self):
        # EP: 0.1 / 0.05 / -0.1 / 0.05 -> 名次 4, 2.5, 1, 2.5 -> (r-1)/3
        r = F.rank_all(make_df(), ASOF)["fv_ep"]
        self.assertAlmostEqual(r["000001"], 1.0)
        self.assertAlmostEqual(r["000002"], 0.5)
        self.assertAlmostEqual(r["000004"], 0.5)
        self.assertAlmostEqual(r["000003"], 0.0)

    def test_rank_negative_direction(self):
        # 资产负债率 .5/.8/1.25/.25,越小越好:000004=1, 000001=2/3, 000002=1/3, 000003=0
        r = F.rank_all(make_df(), ASOF)["fv_debt_ratio"]
        self.assertAlmostEqual(r["000004"], 1.0)
        self.assertAlmostEqual(r["000001"], 2 / 3)
        self.assertAlmostEqual(r["000002"], 1 / 3)
        self.assertAlmostEqual(r["000003"], 0.0)

    def test_rank_ignores_nan_and_single_value(self):
        # BP: .8/.25/NaN/.5 -> 3 个有效值:1, 0, 0.5;NaN 保持 NaN
        r = F.rank_all(make_df(), ASOF)["fv_bp"]
        self.assertAlmostEqual(r["000001"], 1.0)
        self.assertAlmostEqual(r["000004"], 0.5)
        self.assertAlmostEqual(r["000002"], 0.0)
        self.assertTrue(math.isnan(r["000003"]))
        one = F.rank_score(pd.Series([3.0, np.nan]))
        self.assertEqual(one.iloc[0], 0.5)
        self.assertTrue(math.isnan(one.iloc[1]))
        self.assertTrue(F.rank_score(pd.Series([np.nan, np.nan])).isna().all())
        with self.assertRaises(ValueError):
            F.rank_score(pd.Series([1.0, 2.0]), 0)

    def test_scale_invariance(self):
        df = make_df()
        base = F.rank_all(df, ASOF)
        scaled = df.copy()
        scaled["market_cap"] = scaled["market_cap"] * 7.5          # 市值×k:EP/BP/SP 都 ÷k,排序不变
        cols = ["fv_ep", "fv_bp", "fv_sp"]
        pd.testing.assert_frame_equal(F.rank_all(scaled, ASOF)[cols], base[cols])
        np.testing.assert_allclose(F.score("fv_ep", scaled, ASOF).values, F.score("fv_ep", df, ASOF).values / 7.5)


class RobustnessTests(unittest.TestCase):
    def test_empty_and_none(self):
        issues = []
        s = F.score("fv_ep", pd.DataFrame(), ASOF, issues)
        self.assertEqual(len(s), 0)
        self.assertIn(("fv_ep", "empty_data"), issues)
        self.assertEqual(len(F.score("fv_roe", None, ASOF, issues)), 0)
        allf = F.score_all(pd.DataFrame(), ASOF)
        self.assertEqual(list(allf.columns), list(F.FACTOR_IDS))
        self.assertEqual(len(allf), 0)

    def test_missing_columns_give_nan(self):
        df = make_df().drop(columns=["equity_parent"])
        issues = []
        with self.assertLogs(F.log, level=logging.WARNING):
            roe = F.score("fv_roe", df, ASOF, issues)
        self.assertTrue(roe.isna().all())
        self.assertEqual(list(roe.index), list(df.index))
        self.assertIn(("fv_roe", "missing_columns:equity_parent"), issues)
        self.assertAlmostEqual(F.score("fv_ep", df, ASOF)["000001"], 0.1)   # 不依赖该列的因子不受影响
        all_df = F.score_all(df, ASOF)
        self.assertTrue(all_df["fv_bp"].isna().all())
        self.assertTrue(all_df["fv_roe"].isna().all())
        self.assertFalse(all_df["fv_ep"].isna().any())

    def test_bad_as_of_and_unknown_factor(self):
        issues = []
        self.assertTrue(F.score("fv_ep", make_df(), "not-a-date", issues).isna().all())
        self.assertIn(("fv_ep", "bad_as_of_date"), issues)
        self.assertTrue(F.score("fv_ep", make_df(), None, issues).isna().all())
        with self.assertRaises(KeyError):
            F.score("fv_nope", make_df(), ASOF)

    def test_input_not_mutated(self):
        df = make_df()
        before = df.copy(deep=True)
        F.score_all(df, ASOF)
        F.rank_all(df, ASOF)
        pd.testing.assert_frame_equal(df, before)


class LookaheadTests(unittest.TestCase):
    def test_future_announced_rows_are_hidden(self):
        df = make_df()
        df.loc["000002", "available_date"] = pd.Timestamp("2024-07-15")   # 排名日之后才可用
        allf = F.score_all(df, ASOF)
        self.assertTrue(allf.loc["000002"].isna().all())
        self.assertFalse(allf.loc["000001"].isna().all())
        self.assertFalse(F.score_all(df, "2024-07-15").loc["000002"].isna().any())   # 当日可用
        self.assertTrue(F.score_all(df, "2024-07-14").loc["000002"].isna().all())

    def test_unknown_available_date_is_not_guessed(self):
        df = make_df()
        df.loc["000003", "available_date"] = pd.NaT
        self.assertTrue(F.score_all(df, ASOF).loc["000003"].isna().all())

    def test_future_value_changes_do_not_leak(self):
        df = make_df()
        df.loc["000002", "available_date"] = pd.Timestamp("2025-01-01")
        base = F.rank_all(df, ASOF)
        poked = df.copy()
        poked.loc["000002", ["net_profit_ttm", "market_cap", "equity_parent", "total_liabilities"]] = [9e9, 1, 9e9, 1]
        pd.testing.assert_frame_equal(F.rank_all(poked, ASOF), base)
        # 反例:没有 available_date 时同样的改动会泄漏到别人的排名,证明上面的断言有检出力
        a = F.rank_all(poked.drop(columns=["available_date"]), ASOF)
        b = F.rank_all(df.drop(columns=["available_date"]), ASOF)
        self.assertFalse(a.equals(b))

    def test_row_truncation_consistency(self):
        df = make_df()
        full = F.score_all(df, ASOF)
        part = F.score_all(df.iloc[:2], ASOF)
        pd.testing.assert_frame_equal(full.iloc[:2], part)


if __name__ == "__main__":
    unittest.main(verbosity=1)
