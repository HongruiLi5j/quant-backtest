"""月度价值排名测试(T014 S3):全部合成数据,临时目录,不联网、不读 data/。

运行:.venv/bin/python tests/test_fundamental_ranker.py
"""
import importlib.util
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fundamental import factors as F  # noqa: E402
from src.fundamental.ranker import RankConfig, build_ranking  # noqa: E402

_spec = importlib.util.spec_from_file_location("run_fundamental_rank", ROOT / "scripts" / "run_fundamental_rank.py")
CLI = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(CLI)

ASOF = "2024-06-28"
BJT = timezone(timedelta(hours=8))


def make_market(n_per=10, seed=1):
    """两个行业各 n_per 只。行业 A 整体"便宜"(ep 大),行业 B 整体"贵",但行业内各有好坏。
    quality 随 i 递增:i 越大利润越高 => 行业内排名应由 i 决定。"""
    rows = []
    for ind, base_cap in (("A行业", 500.0), ("B行业", 5000.0)):
        for i in range(n_per):
            profit = 50 + 10 * i
            code = ("%s%02d" % ("00" if ind == "A行业" else "60", i)).rjust(6, "0")
            rows.append(dict(
                code=code, name="n" + code, industry=ind, market_cap=base_cap, close_raw=10.0,
                net_profit_ttm=profit, net_profit_ttm_prev=profit * 0.9, revenue_ttm=1000 + 50 * i,
                revenue_ttm_prev=900 + 50 * i, operating_cost_ttm=600 + 10 * i, operating_profit_ttm=100 + 10 * i,
                equity_parent=800 + 20 * i, total_assets=2000, total_liabilities=1200 - 20 * i,
                cfo_ttm=profit * 1.2, dps_ttm=0.1 + 0.05 * i, available_date="2024-04-30"))
    return pd.DataFrame(rows)


def make_prices(codes, date=ASOF, extra_future=False):
    rows = [dict(code=c, date=date, close=10.0) for c in codes]
    if extra_future:
        rows += [dict(code=c, date="2024-07-05", close=1.0) for c in codes]
    return pd.DataFrame(rows)


class RankerTests(unittest.TestCase):
    def setUp(self):
        self.fdf = make_market()
        self.px = make_prices(self.fdf["code"])

    def test_industry_neutral(self):
        # 行业 A 市值只有 B 的 1/10,若不做行业中性,A 的 ep/bp/sp 全排前。
        # 行业内 Z-score 后,两个行业应对称:最强的几只各占一半,且行业内顺序由 i 决定
        res = build_ranking(self.fdf, self.px, ASOF, top_n=4)
        t = res.table
        self.assertEqual(len(t), 20)
        inds = set(res.longs["industry"])
        self.assertEqual(inds, {"A行业", "B行业"})
        for ind in ("A行业", "B行业"):
            sub = t[t["industry"] == ind].sort_values("rank")
            self.assertEqual(list(sub["code"]), sorted(sub["code"], reverse=True))   # i 越大越靠前
        # 行业间对称:两个行业同位次的综合分一致(A、B 的比率因子除了市值尺度外结构相同,
        # 且 ep/bp/sp 在行业内 Z-score 与尺度无关)
        a = t[t["industry"] == "A行业"].sort_values("rank")["score"].to_numpy()
        b = t[t["industry"] == "B行业"].sort_values("rank")["score"].to_numpy()
        np.testing.assert_allclose(a, b, atol=1e-9)

    def test_industry_zscore_hand_value(self):
        # 单因子手算:只给 fv_roe 权重,行业 A 的 roe = profit/equity
        cfg = RankConfig(weights={"fv_roe": 1.0}, min_factors=1)
        res = build_ranking(self.fdf, self.px, ASOF, top_n=3, config=cfg)
        sub = self.fdf[self.fdf["industry"] == "A行业"]
        roe = sub["net_profit_ttm"] / sub["equity_parent"]
        z = (roe - roe.mean()) / roe.std(ddof=0)
        got = res.table.set_index("code").loc[sub["code"], "score"]
        np.testing.assert_allclose(got.to_numpy(), z.to_numpy(), atol=1e-9)

    def test_small_industry_falls_back_to_market(self):
        df = self.fdf.copy()
        df.loc[df.index[:3], "industry"] = "小行业"            # 3 只 < 8
        res = build_ranking(df, self.px, ASOF, top_n=3)
        small = res.table[res.table["industry"] == "小行业"]
        self.assertEqual(len(small), 3)
        self.assertTrue((small["n_fallback"] == len(F.FACTOR_IDS)).all())
        self.assertTrue(any("industry_fallback" in n for n in res.notes))

    def test_zero_variance_industry_gives_zero(self):
        df = self.fdf.copy()
        for c in ["net_profit_ttm"]:
            df.loc[df["industry"] == "A行业", c] = 100.0
        cfg = RankConfig(weights={"fv_roa": 1.0}, min_factors=1)
        res = build_ranking(df, self.px, ASOF, top_n=3, config=cfg)
        a = res.table[res.table["industry"] == "A行业"]
        self.assertTrue(np.allclose(a["score"], 0.0))

    def test_missing_impute_by_industry_median(self):
        df = self.fdf.copy()
        victim = df["code"].iloc[3]
        df.loc[df["code"] == victim, "dps_ttm"] = np.nan        # 只缺股息率
        res = build_ranking(df, self.px, ASOF, top_n=3)
        row = res.table.set_index("code").loc[victim]
        self.assertEqual(row["n_imputed"], 1)
        self.assertEqual(row["n_factors"], 11)
        zs = res.table[(res.table["industry"] == "A行业") & (res.table["code"] != victim)]["z_fv_dy"]
        self.assertAlmostEqual(row["z_fv_dy"], float(zs.median()), places=9)
        self.assertEqual(len(res.excluded), 0)

    def test_missing_drop_mode(self):
        df = self.fdf.copy()
        victim = df["code"].iloc[3]
        df.loc[df["code"] == victim, "dps_ttm"] = np.nan
        res = build_ranking(df, self.px, ASOF, top_n=3, config=RankConfig(missing_mode="drop"))
        self.assertNotIn(victim, set(res.table["code"]))
        self.assertEqual(dict(zip(res.excluded["code"], res.excluded["reason"]))[victim], "missing_factors")
        self.assertEqual(len(res.table), 19)

    def test_too_few_factors_and_all_nan_stock_excluded(self):
        df = self.fdf.copy()
        cols = [c for c in df.columns if c not in ("code", "name", "industry", "available_date")]
        df.loc[0, cols] = np.nan                                  # 全 NaN
        df.loc[1, ["dps_ttm", "close_raw", "revenue_ttm", "revenue_ttm_prev", "operating_cost_ttm",
                   "operating_profit_ttm", "cfo_ttm", "net_profit_ttm_prev", "equity_parent"]] = np.nan
        res = build_ranking(df, self.px, ASOF, top_n=3)
        reasons = dict(zip(res.excluded["code"], res.excluded["reason"]))
        self.assertEqual(reasons[df["code"].iloc[0]], "no_factor_data")
        self.assertEqual(reasons[df["code"].iloc[1]], "too_few_factors")

    def test_all_nan_stock_does_not_change_others(self):
        base = build_ranking(self.fdf, self.px, ASOF, top_n=5)
        extra = self.fdf.iloc[[0]].copy()
        extra["code"] = "999999"
        for c in extra.columns:
            if c not in ("code", "name", "industry", "available_date"):
                extra[c] = np.nan
        df2 = pd.concat([self.fdf, extra], ignore_index=True)
        px2 = make_prices(df2["code"])
        r2 = build_ranking(df2, px2, ASOF, top_n=5)
        pd.testing.assert_frame_equal(base.table, r2.table)

    def test_deterministic_repeat(self):
        shuffled = self.fdf.sample(frac=1.0, random_state=7)
        r1 = build_ranking(self.fdf, self.px, ASOF, top_n=5)
        r2 = build_ranking(self.fdf, self.px, ASOF, top_n=5)
        r3 = build_ranking(shuffled, self.px, ASOF, top_n=5)
        pd.testing.assert_frame_equal(r1.table, r2.table)
        pd.testing.assert_frame_equal(r1.table, r3.table)            # 输入顺序无关
        self.assertEqual(r1.cfg_hash, r2.cfg_hash)

    def test_ties_broken_by_code(self):
        df = self.fdf.copy()
        # 同行业两只完全相同 -> 分数相同,代码小的在前
        df.loc[1, df.columns.difference(["code", "name"])] = df.loc[0, df.columns.difference(["code", "name"])]
        res = build_ranking(df, self.px, ASOF, top_n=3)
        t = res.table.set_index("code")
        c0, c1 = df["code"].iloc[0], df["code"].iloc[1]
        self.assertAlmostEqual(t.loc[c0, "score"], t.loc[c1, "score"], places=12)
        self.assertLess(t.loc[min(c0, c1), "rank"], t.loc[max(c0, c1), "rank"])

    def test_no_lookahead_future_announcement(self):
        df = self.fdf.copy()
        victim = df["code"].iloc[5]
        base = build_ranking(df, self.px, ASOF, top_n=3)
        # 把一只股票的数据改成"排名日之后才公告"且数值极端:它应被当作缺失,极端值不泄漏给他人
        df2 = df.copy()
        m = df2["code"] == victim
        df2.loc[m, "available_date"] = "2024-07-30"
        df2.loc[m, "net_profit_ttm"] = 1e12
        r = build_ranking(df2, self.px, ASOF, top_n=3)
        self.assertIn(victim, set(r.excluded["code"]))
        others = base.table[base.table["code"] != victim]
        self.assertEqual(len(r.table), len(df) - 1)
        # 其他股票的名次顺序不应受极端未来值影响(相对顺序在重算后仍存在;分数因剔除一只会微变,但极端值不可泄漏)
        self.assertLess(r.table["score"].abs().max(), 3.0 + 1e-9)
        self.assertEqual(len(others) - 0, len(r.table))

    def test_future_available_date_extreme_does_not_leak(self):
        df = self.fdf.copy()
        df2 = df.copy()
        extra = df.iloc[[0]].copy()
        extra["code"] = "888888"
        extra["available_date"] = "2025-01-01"
        extra["net_profit_ttm"] = 1e15
        df2 = pd.concat([df2, extra], ignore_index=True)
        px2 = make_prices(df2["code"])
        a = build_ranking(df, self.px, ASOF, top_n=3)
        b = build_ranking(df2, px2, ASOF, top_n=3)
        pd.testing.assert_frame_equal(a.table, b.table)           # 未来公告的股票整只不影响他人

    def test_future_price_rows_ignored(self):
        a = build_ranking(self.fdf, self.px, ASOF, top_n=3)
        px_f = make_prices(self.fdf["code"], extra_future=True)
        b = build_ranking(self.fdf, px_f, ASOF, top_n=3)
        pd.testing.assert_frame_equal(a.table, b.table)
        # 价格表只有未来日期 -> 排名日无行情 -> 全部剔除而不是用未来数据
        px_only_future = make_prices(self.fdf["code"], date="2024-07-05")
        c = build_ranking(self.fdf, px_only_future, ASOF, top_n=3)
        self.assertEqual(len(c.table), 0)
        self.assertEqual(set(c.excluded["reason"]), {"no_price_on_date"})

    def test_suspended_stock_excluded(self):
        px = self.px[self.px["code"] != self.fdf["code"].iloc[2]]
        res = build_ranking(self.fdf, px, ASOF, top_n=3)
        self.assertEqual(dict(zip(res.excluded["code"], res.excluded["reason"]))[self.fdf["code"].iloc[2]],
                         "no_price_on_date")

    def test_longs_avoids_no_overlap_and_order(self):
        res = build_ranking(self.fdf, self.px, ASOF, top_n=5)
        self.assertEqual(len(res.longs), 5)
        self.assertEqual(len(res.avoids), 5)
        self.assertFalse(set(res.longs["code"]) & set(res.avoids["code"]))
        self.assertEqual(list(res.longs["rank"]), [1, 2, 3, 4, 5])
        self.assertEqual(list(res.avoids["rank"]), [20, 19, 18, 17, 16])
        short = build_ranking(self.fdf, self.px, ASOF, top_n=15)
        self.assertEqual(len(short.longs), 15)
        self.assertEqual(len(short.avoids), 5)
        self.assertFalse(set(short.longs["code"]) & set(short.avoids["code"]))

    def test_monotone_transform_of_market_cap_keeps_order(self):
        a = build_ranking(self.fdf, self.px, ASOF, top_n=3, config=RankConfig(weights={"fv_ep": 1.0}, min_factors=1))
        df = self.fdf.copy()
        df["market_cap"] = df["market_cap"] * 7.5
        b = build_ranking(df, self.px, ASOF, top_n=3, config=RankConfig(weights={"fv_ep": 1.0}, min_factors=1))
        self.assertEqual(list(a.table["code"]), list(b.table["code"]))

    def test_weights_change_result_and_validation(self):
        a = build_ranking(self.fdf, self.px, ASOF, top_n=3)
        b = build_ranking(self.fdf, self.px, ASOF, top_n=3, config=RankConfig(weights={"fv_ep": 1.0}))
        self.assertNotEqual(a.cfg_hash, b.cfg_hash)
        for bad in ({"nope": 1.0}, {"fv_ep": -1.0}, {"fv_ep": 0.0}, {"fv_ep": float("nan")}):
            with self.assertRaises(ValueError):
                build_ranking(self.fdf, self.px, ASOF, top_n=3, config=RankConfig(weights=bad))

    def test_empty_and_bad_inputs(self):
        for empty in (pd.DataFrame(), None, pd.DataFrame(columns=["code", "market_cap"])):
            r = build_ranking(empty, self.px, ASOF, top_n=3)
            self.assertEqual(len(r.table), 0)
            self.assertEqual(len(r.longs), 0)
            self.assertEqual(len(r.avoids), 0)
        # 缺字段:全部因子 NaN -> 全部剔除而不是崩
        r = build_ranking(self.fdf[["code", "industry"]], self.px, ASOF, top_n=3)
        self.assertEqual(len(r.table), 0)
        self.assertEqual(len(r.excluded), 20)
        with self.assertRaises(ValueError):
            build_ranking(self.fdf, self.px, "not-a-date", top_n=3)
        for bad in (0, -1, 1.5, "3", True):
            with self.assertRaises(ValueError):
                build_ranking(self.fdf, self.px, ASOF, top_n=bad)
        dup = pd.concat([self.fdf, self.fdf.iloc[[0]]])
        with self.assertRaises(ValueError):
            build_ranking(dup, self.px, ASOF, top_n=3)
        # 价格表为 None / 空:不做行情检查
        r = build_ranking(self.fdf, None, ASOF, top_n=3)
        self.assertEqual(len(r.table), 20)
        self.assertTrue(any("no_price_table" in n for n in r.notes))

    def test_input_not_modified(self):
        f0, p0 = self.fdf.copy(), self.px.copy()
        build_ranking(self.fdf, self.px, ASOF, top_n=3)
        pd.testing.assert_frame_equal(f0, self.fdf)
        pd.testing.assert_frame_equal(p0, self.px)

    def test_industry_and_names_injection(self):
        df = self.fdf.drop(columns=["industry", "name"])
        ind = pd.Series(self.fdf["industry"].values, index=self.fdf["code"].values)
        nm = pd.Series(self.fdf["name"].values, index=self.fdf["code"].values)
        a = build_ranking(self.fdf, self.px, ASOF, top_n=3)
        b = build_ranking(df, self.px, ASOF, top_n=3, industry=ind, names=nm)
        pd.testing.assert_frame_equal(a.table, b.table)
        c = build_ranking(df, self.px, ASOF, top_n=3)               # 无行业:退化全市场
        self.assertTrue(any("no_industry_info" in n for n in c.notes))

    def test_outlier_clipped(self):
        df = self.fdf.copy()
        df.loc[0, "net_profit_ttm"] = 1e9                           # 极端 ROE
        res = build_ranking(df, self.px, ASOF, top_n=3)
        zcols = [c for c in res.table.columns if c.startswith("z_")]
        self.assertLessEqual(res.table[zcols].abs().max().max(), 3.0 + 1e-9)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.fdf = make_market()
        self.px = make_prices(self.fdf["code"])
        self.loader = lambda args: (self.fdf, self.px)
        self.now = datetime(2026, 10, 22, 16, 0, tzinfo=BJT)

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, argv, loader="default", now=None):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = CLI.main(argv, loader=self.loader if loader == "default" else loader, now=now or self.now)
        return code, out.getvalue(), err.getvalue()

    def test_writes_report_with_disclaimer(self):
        out = self.dir / "REPORT.md"
        code, so, se = self.run_cli(["--date", ASOF, "--top", "5", "--out", str(out)])
        self.assertEqual(code, 0, se)
        text = out.read_text(encoding="utf-8")
        self.assertIn("免责声明", text)
        self.assertIn("做多候选", text)
        self.assertIn("避开候选", text)
        self.assertIn("风险提示", text)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["REPORT.md"])   # 无临时文件残留

    def test_refuses_overwrite_unless_force(self):
        out = self.dir / "REPORT.md"
        out.write_text("old", encoding="utf-8")
        code, _, se = self.run_cli(["--date", ASOF, "--out", str(out)])
        self.assertEqual(code, 2)
        self.assertEqual(out.read_text(encoding="utf-8"), "old")
        code, _, _ = self.run_cli(["--date", ASOF, "--out", str(out), "--force"])
        self.assertEqual(code, 0)
        self.assertIn("免责声明", out.read_text(encoding="utf-8"))

    def test_report_reproducible(self):
        a, b = self.dir / "a.md", self.dir / "b.md"
        self.run_cli(["--date", ASOF, "--top", "5", "--out", str(a)])
        self.run_cli(["--date", ASOF, "--top", "5", "--out", str(b)])
        self.assertEqual(a.read_text(encoding="utf-8"), b.read_text(encoding="utf-8"))

    def test_no_data_given_refuses_without_network(self):
        out = self.dir / "R.md"
        code, _, se = self.run_cli(["--date", ASOF, "--out", str(out)], loader=None)
        self.assertEqual(code, 2)
        self.assertIn("未提供数据", se)
        self.assertFalse(out.exists())

    def test_intraday_today_refused(self):
        early = datetime(2026, 10, 22, 14, 0, tzinfo=BJT)
        code, _, se = self.run_cli(["--date", "2026-10-22", "--out", str(self.dir / "R.md")], now=early)
        self.assertEqual(code, 2)
        self.assertIn("15:30", se)
        late = datetime(2026, 10, 22, 15, 30, tzinfo=BJT)
        code, _, _ = self.run_cli(["--date", "2026-10-22", "--out", str(self.dir / "R.md")], now=late)
        self.assertEqual(code, 0)
        code, _, se = self.run_cli(["--date", "2026-10-23", "--out", str(self.dir / "R2.md")], now=late)
        self.assertEqual(code, 2)

    def test_bad_args(self):
        code, _, _ = self.run_cli(["--date", "garbage", "--out", str(self.dir / "R.md")])
        self.assertEqual(code, 2)
        code, _, _ = self.run_cli(["--date", ASOF, "--top", "0", "--out", str(self.dir / "R.md")])
        self.assertEqual(code, 2)

    def test_protected_dir_refused(self):
        code, _, se = self.run_cli(["--date", ASOF, "--out", str(ROOT / "data" / "daily" / "x.md")])
        self.assertEqual(code, 2)
        self.assertFalse((ROOT / "data" / "daily" / "x.md").exists())

    def test_empty_input_report(self):
        out = self.dir / "E.md"
        code, _, se = self.run_cli(["--date", ASOF, "--out", str(out)], loader=lambda a: (pd.DataFrame(), pd.DataFrame()))
        self.assertEqual(code, 0, se)
        self.assertIn("免责声明", out.read_text(encoding="utf-8"))

    def test_csv_loader_roundtrip(self):
        f, p = self.dir / "f.csv", self.dir / "p.csv"
        self.fdf.to_csv(f, index=False)
        self.px.to_csv(p, index=False)
        out = self.dir / "C.md"
        code, _, se = self.run_cli(["--date", ASOF, "--top", "3", "--fundamental", str(f), "--prices", str(p),
                                    "--out", str(out)], loader=CLI.default_loader)
        self.assertEqual(code, 0, se)
        self.assertIn("免责声明", out.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main(verbosity=1)
