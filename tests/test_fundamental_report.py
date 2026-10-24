"""月度价值排名报告测试(T014 S4):合成数据 + 临时目录,不联网。

运行:.venv/bin/python tests/test_fundamental_report.py
"""
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fundamental.ranker import build_ranking  # noqa: E402
from src.fundamental.report import build_monthly_report, write_report  # noqa: E402

ASOF = "2024-06-28"


def make_df(n_per=10, shift=0):
    rows = []
    for ind, cap, pre in (("A行业", 500.0, "00"), ("B行业", 5000.0, "60")):
        for i in range(n_per):
            code = ("%s%02d" % (pre, i)).rjust(6, "0")
            k = (i + shift) % n_per
            rows.append(dict(
                code=code, name="n" + code, industry=ind, market_cap=cap, close_raw=10.0,
                net_profit_ttm=50 + 10 * k, net_profit_ttm_prev=40.0, revenue_ttm=1000.0 + 20 * k,
                revenue_ttm_prev=900.0, operating_cost_ttm=600.0, operating_profit_ttm=80 + 5 * k,
                equity_parent=400.0, total_assets=1000.0, total_liabilities=500.0 - 10 * k,
                cfo_ttm=60 + 8 * k, dps_ttm=0.1 * (k + 1), available_date="2024-04-30"))
    return pd.DataFrame(rows)


def rank(df, top=5):
    prices = pd.DataFrame({"code": df["code"], "date": pd.Timestamp(ASOF), "close": 10.0})
    return build_ranking(df, prices, ASOF, top_n=top)


class ReportTest(unittest.TestCase):
    def setUp(self):
        self.res = rank(make_df())
        self.prev = rank(make_df(shift=3))

    def test_sections_and_disclaimer(self):
        t = build_monthly_report(self.res, {"top_n": 5, "generated_at": "2024-06-28 16:00"})
        for s in ("免责声明", "前 5 名", "后 5 名", "与上月对比", "各因子贡献拆解", "所属行业分布", "已知局限", "2024-06-28"):
            self.assertIn(s, t)
        self.assertIn("无上月排名", t)

    def test_contains_codes_and_factors(self):
        t = build_monthly_report(self.res, {"top_n": 5})
        self.assertIn(self.res.longs.iloc[0]["code"], t)
        self.assertIn("fv_roe", t)

    def test_with_prev_ranking(self):
        t = build_monthly_report(self.res, {"top_n": 5}, prev_ranking=self.prev)
        self.assertNotIn("无上月排名", t)
        self.assertIn("新进入", t)
        t2 = build_monthly_report(self.res, {"top_n": 5}, prev_ranking=self.prev.table[["code", "rank"]])
        self.assertEqual(t, t2)

    def test_same_prev_no_change(self):
        t = build_monthly_report(self.res, {"top_n": 5}, prev_ranking=self.res)
        self.assertIn("新进入 0 只,退出 0 只", t)

    def test_empty_prev_degrades(self):
        empty = build_ranking(pd.DataFrame(), None, ASOF)
        self.assertIn("无上月排名", build_monthly_report(self.res, {"top_n": 5}, prev_ranking=empty))

    def test_contribution_math(self):
        t = build_monthly_report(self.res, {"top_n": 5})
        exp = (self.res.longs["z_fv_roe"] / 12.0).mean() - (self.res.avoids["z_fv_roe"] / 12.0).mean()
        line = [x for x in t.splitlines() if x.startswith("| fv_roe ")][0]
        self.assertTrue(line.strip().endswith("%.4f |" % exp), line)

    def test_industry_distribution(self):
        t = build_monthly_report(self.res, {"top_n": 5})
        self.assertIn("| A行业 |", t.split("所属行业分布")[1])

    def test_empty_ranking(self):
        empty = build_ranking(pd.DataFrame(), None, ASOF)
        t = build_monthly_report(empty, {})
        self.assertIn("无排名结果", t)
        self.assertIn("免责声明", t)
        self.assertNotIn("各因子贡献拆解", t)
        self.assertIn("无排名结果", build_monthly_report(None))

    def test_deterministic_and_input_unchanged(self):
        before = self.res.table.copy()
        a = build_monthly_report(self.res, {"top_n": 5}, self.prev)
        b = build_monthly_report(self.res, {"top_n": 5}, self.prev)
        self.assertEqual(a, b)
        pd.testing.assert_frame_equal(before, self.res.table)

    def test_pipe_in_name_escaped(self):
        df = make_df()
        df.loc[0, "name"] = "a|b"
        t = build_monthly_report(rank(df, top=20), {"top_n": 20})
        self.assertNotIn("a|b", t)

    def test_write_no_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "sub" / "r.md"
            write_report(p, "one")
            with self.assertRaises(FileExistsError):
                write_report(p, "two")
            self.assertEqual(p.read_text(encoding="utf-8"), "one")
            write_report(p, "three", overwrite=True)
            self.assertEqual(p.read_text(encoding="utf-8"), "three")
            self.assertEqual([x.name for x in p.parent.iterdir()], ["r.md"])
            with self.assertRaises(IsADirectoryError):
                write_report(p.parent, "x")


if __name__ == "__main__":
    unittest.main()
