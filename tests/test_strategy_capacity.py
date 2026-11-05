"""C07 策略容量脚本的测试：.venv/bin/python tests/test_strategy_capacity.py

全部用临时目录里的合成行情和合成成交额旁表，不读写 data/，不联网。
"""
import math
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from config import MARKETS, TRADE_LIST_MIN_TURNOVER  # noqa: E402
from src import registry, turnover  # noqa: E402
from src.datasource import limit_pct  # noqa: E402
from src.portfolio import run_weights  # noqa: E402
import strategy_capacity as C  # noqa: E402
from test_strategy_overlap import fake_market, snapshot  # noqa: E402  合成行情生成器和目录快照


def write_turnover(daily: Path, codes_days: dict) -> Path:
    """codes_days：{代码: {日期: 成交额}} → 写成旁表 parquet（列同 turnover.COLUMNS）。"""
    rows = [{"date": d, "code": c, "turnover_yuan": v, "volume_shares": 1e6, "close_raw": 10.0, "low_raw": 9.0,
             "high_raw": 11.0, "source": "测试"} for c, days in codes_days.items() for d, v in days.items()]
    path = daily / "turnover_cn.parquet"
    pd.DataFrame(rows, columns=turnover.COLUMNS).sort_values(["date", "code"]).to_parquet(path, index=False)
    return path


class FormulaTests(unittest.TestCase):
    def test_participation_and_sqrt_impact(self):
        self.assertAlmostEqual(float(C.participation(5e5, 1e7)), 0.05)
        base = C.impact_frac(0.5, 0.02, 0.01)
        self.assertAlmostEqual(float(base), 0.5 * 0.02 * 0.1)             # sqrt(0.01) = 0.1
        self.assertAlmostEqual(float(C.impact_frac(0.5, 0.02, 0.04) / base), 2.0)   # 参与率 ×4 → 成本 ×2
        self.assertEqual(float(C.impact_frac(0.5, 0.02, 0.0)), 0.0)

    def test_trailing_sigma_has_no_lookahead(self):
        close = pd.Series(100 * np.cumprod(1 + np.random.default_rng(0).normal(0, 0.02, 200)),
                          index=pd.bdate_range("2020-01-01", periods=200))
        s1 = C.trailing_sigma(close)
        self.assertTrue(s1.iloc[:C.SIGMA_MIN].isna().all())              # 样本不足不给 σ
        t = 120
        bumped = close.copy()
        bumped.iloc[t] *= 1.5                                            # t 日收盘大涨
        s2 = C.trailing_sigma(bumped)
        pd.testing.assert_series_equal(s1.iloc[:t + 1], s2.iloc[:t + 1])  # t 日及以前的 σ 不受 t 日收盘影响（t 日开盘成交，σ 只用到 t−1 收盘）
        self.assertNotEqual(s1.iloc[t + 1], s2.iloc[t + 1])               # 第二天才用到

    def test_unit_drag_factorization_matches_direct(self):
        rng = np.random.default_rng(1)
        dw = rng.uniform(0, 1, (30, 5)) * (rng.random((30, 5)) < 0.3)
        sig = rng.uniform(0.01, 0.04, (30, 5))
        adv = rng.uniform(1e7, 1e9, 5)
        A, c = 3e6, 0.5
        direct = np.where(dw > C.EPS, C.impact_frac(c, sig, C.participation(A * dw, adv[None, :])) * dw, 0.0)
        np.testing.assert_allclose(c * math.sqrt(A) * C.unit_drag(dw, sig, adv), direct, rtol=1e-12)

    def test_unit_drag_nan_sigma_counts_zero(self):
        dw = np.array([[0.5, 0.5]])
        sig = np.array([[np.nan, 0.02]])
        x = C.unit_drag(dw, sig, np.array([1e8, 1e8]))
        self.assertEqual(x[0, 0], 0.0)
        self.assertGreater(x[0, 1], 0.0)

    def test_participation_stats_by_hand(self):
        dw = np.array([[1.0, 0.0], [0.5, 0.2]])                          # 3 笔交易（0 不算）
        adv = np.array([1e7, 1e7])
        st = C.participation_stats(dw, adv, A=1e6)                       # 参与率 10%、5%、2%
        self.assertEqual(st["n_trades"], 3)
        self.assertAlmostEqual(st["share_gt5"], 1 / 3)                   # 严格大于 5%：只有 10%（5% 不算）
        self.assertAlmostEqual(st["share_gt10"], 0.0)                    # 10% 不严格大于 10%
        self.assertAlmostEqual(st["value_share_gt5"], 1.0 / 1.7)
        self.assertAlmostEqual(st["median_part"], 0.05)
        empty = C.participation_stats(np.zeros((2, 2)), adv, A=1e6)
        self.assertEqual(empty["n_trades"], 0)
        self.assertTrue(math.isnan(empty["share_gt5"]))

    def test_capacity_closed_form(self):
        d1 = 0.8
        for c in C.COEFS:
            K, A = C.capacity_capital(d1, 0.01, c, 20)
            self.assertAlmostEqual(c * math.sqrt(A) * d1, 0.01)           # 在 K* 处拖累刚好等于预算
            self.assertAlmostEqual(K, 20 * A)
        K1, _ = C.capacity_capital(d1, 0.01, 0.5, 20)
        K2, _ = C.capacity_capital(d1, 0.01, 1.0, 20)
        self.assertAlmostEqual(K1 / K2, 4.0)                             # c 翻倍，容量 ÷4（sqrt 的平方）
        K3, _ = C.capacity_capital(d1, 0.01, 0.5, 40)
        self.assertAlmostEqual(K3 / K1, 2.0)                             # K* 正比于 N
        self.assertTrue(math.isnan(C.capacity_capital(d1, float("nan"), 0.5, 20)[0]))
        self.assertTrue(math.isnan(C.capacity_capital(d1, -0.01, 0.5, 20)[0]))
        self.assertTrue(math.isinf(C.capacity_capital(0.0, 0.01, 0.5, 20)[0]))

    def test_bucket_edges(self):
        adv = np.array([1.0, 4.99e7, 5e7, 1.99e8, 2e8, 9.9e8, 1e9, 1e12])
        np.testing.assert_array_equal(C.bucket_of(adv), [0, 0, 1, 1, 2, 2, 3, 3])
        self.assertEqual(C.BUCKET_EDGES[1], TRADE_LIST_MIN_TURNOVER)      # 第一个界 = 项目的「流动性差」阈值

    def test_annualizers(self):
        x = np.full(252, 0.001)
        self.assertAlmostEqual(C.ann_mean(x), 0.252)
        self.assertAlmostEqual(C.cagr(x), 1.001 ** 252 - 1)
        self.assertAlmostEqual(C.ann_mean(np.array([0.01, np.nan])), 252 * 0.01)   # nan 被忽略
        self.assertTrue(math.isnan(C.ann_mean(np.array([np.nan]))))

    def test_fmt_money(self):
        self.assertEqual(C.fmt_money(1e5), "10万")
        self.assertEqual(C.fmt_money(1e7), "1,000万")
        self.assertEqual(C.fmt_money(1.3e8), "1.3亿")
        self.assertEqual(C.fmt_money(float("inf")), "∞")
        self.assertEqual(C.fmt_money(float("nan")), "—")
        self.assertNotIn("e+", C.fmt_money(2.5e10))


class LoadAdvTests(unittest.TestCase):
    def test_adv_uses_only_common_days_and_min_days(self):
        days = [f"2026-10-{d:02d}" for d in range(12, 24)]               # 12 天
        common = days[-8:]                                               # 后 8 天全市场覆盖，前 4 天只有 "L" 一只
        data = {"F": {d: 1e8 for d in common},                          # 只在共同日有数据
                "L": {**{d: 5e7 for d in days}, days[0]: 9e12},         # 长历史；非共同日（第 1 天）有极端值，不应影响
                "FEW": {d: 3e8 for d in common[:3]}}                    # 共同日里只有 3 天 < min_days
        # 再加几个只在共同日出现的代码，让共同日的行数占比达标（≥90% 最大行数）
        for k in range(8):
            data[f"X{k}"] = {d: 1e7 for d in common}
        with tempfile.TemporaryDirectory() as tmp:
            daily = Path(tmp)
            write_turnover(daily, data)
            adv, info = C.load_adv(daily)
        self.assertEqual(info["common_days"], common)
        self.assertAlmostEqual(adv["F"], 1e8)
        self.assertAlmostEqual(adv["L"], 5e7)                           # 极端值落在非共同日，被排除
        self.assertNotIn("FEW", adv.index)
        self.assertEqual(info["codes_with_adv"], len(adv))
        self.assertEqual(info["table_first"], days[0])
        self.assertEqual(info["table_last"], days[-1])
        self.assertIn("sha256", info)
        self.assertEqual(adv.name, "adv")

    def test_load_adv_does_not_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            daily = Path(tmp)
            write_turnover(daily, {"A": {f"2026-10-{d:02d}": 1e8 for d in range(12, 22)}})
            before = snapshot(daily)
            C.load_adv(daily)
            self.assertEqual(snapshot(daily), before)


class StockPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.daily = fake_market(Path(cls.tmp.name))
        cash = pd.read_csv(cls.daily / "cn" / f"{MARKETS['cn']['cash']}.csv", index_col=0, parse_dates=True).sort_index().loc[:C.CUTOFF]
        cls.cash = cash["Close"].pct_change()
        cls.df = pd.read_parquet(cls.daily / "cn_all" / "600001.parquet").sort_index().loc[:C.CUTOFF]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_panel_window_and_dw_matches_run_weights(self):
        p = C.stock_panel(self.df, "600001", "正常股甲", self.cash)
        self.assertGreaterEqual(p["idx"][0], pd.Timestamp(C.WIN_START))
        self.assertLessEqual(p["idx"][-1], pd.Timestamp(C.CUTOFF))
        # 逐日 |Δw| 之和 = run_weights 自己算的换手
        W = registry.target_weights("blend2", {"600001": self.df}, "600001", registry.clean_params("blend2", {}), MARKETS["cn"])
        res = run_weights({"600001": self.df}, W, cost=C.STOCK_COST, band=C.BAND, cash_ret=self.cash, limits={"600001": limit_pct("600001", "正常股甲")})
        np.testing.assert_allclose(p["blend2"][1], res.turnover.reindex(p["idx"]).to_numpy(), atol=1e-12)
        self.assertEqual(len(p["sigma"]), len(p["idx"]))
        for s in C.ORDER:
            self.assertEqual(len(p[s][0]), len(p["idx"]))
            self.assertTrue((p[s][1] >= 0).all())

    def test_truncating_future_does_not_change_past(self):
        full = C.stock_panel(self.df, "600001", "正常股甲", self.cash)
        cut = pd.Timestamp("2024-06-28")
        part = C.stock_panel(self.df.loc[:cut], "600001", "正常股甲", self.cash)
        n = len(part["idx"])
        self.assertGreater(n, 100)
        np.testing.assert_allclose(part["sigma"], full["sigma"][:n], atol=1e-12, equal_nan=True)
        for s in C.ORDER:
            np.testing.assert_allclose(part[s][1], full[s][1][:n], atol=1e-12)     # 成交量（仓位变化）只依赖当时及以前的数据
            np.testing.assert_allclose(part[s][0], full[s][0][:n], atol=1e-12)

    def test_rejects_data_after_cutoff(self):
        raw = pd.read_parquet(self.daily / "cn_all" / "600001.parquet").sort_index()
        self.assertGreater(raw.index[-1], pd.Timestamp(C.CUTOFF))        # 合成数据确实越过截止日
        with self.assertRaises(ValueError):
            C.stock_panel(raw, "600001", "正常股甲", self.cash)
        with self.assertRaises(ValueError):
            C.check_end("2026-04-01")


class EndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name) / "data"
        cls.root.mkdir()
        cls.daily = fake_market(cls.root)
        days = [f"2026-10-{d:02d}" for d in range(12, 24)]
        rng = np.random.default_rng(3)
        codes = ["600001", "600002", "600003", "600004", "600005", "600006"]
        adv_level = {c: float(rng.uniform(2e7, 5e8)) for c in codes}
        cls.adv_level = adv_level
        # 600006 故意不进旁表 → 不应被选中；600003 是 ST、600005 历史太短，也不应被选中
        write_turnover(cls.daily, {c: {d: adv_level[c] for d in days} for c in codes if c != "600006"})
        cls.before = snapshot(cls.root)
        cls.out = Path(cls.tmp.name) / "out"
        cls.R = C.run(cls.root, cls.out, n_stocks=3, seed=1, workers=1, log=lambda *_: None)
        cls.drag = pd.read_csv(cls.out / "capacity_drag.csv")
        cls.cap = pd.read_csv(cls.out / "capacity_capacity.csv")
        cls.part = pd.read_csv(cls.out / "capacity_participation.csv")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_outputs_exist_and_inputs_untouched(self):
        for name in ("capacity_turnover.csv", "capacity_buckets.csv", "capacity_participation.csv", "capacity_drag.csv", "capacity_capacity.csv",
                     "capacity_impact_bp.csv", "capacity_stock_sample.csv", "capacity_meta.json", "capacity_tables.md",
                     "fig_participation.png", "fig_drag.png", "fig_capacity.png", "fig_bucket.png"):
            self.assertTrue((self.out / name).exists(), name)
        self.assertEqual(snapshot(self.root), self.before)               # 只读：输入目录一个字节都没变

    def test_sample_respects_eligibility(self):
        codes = self.R["panel"]["codes"]
        self.assertEqual(len(codes), 3)
        self.assertEqual(sorted(codes), ["600001", "600002", "600004"])  # 排除 ST(600003)、短历史(600005)、无 ADV(600006)
        self.assertTrue(self.R["meta"]["backtest_last_date"] <= C.CUTOFF)
        np.testing.assert_allclose(self.R["panel"]["adv"], [self.adv_level[c] for c in codes])

    def test_selfcheck_and_sqrt_scaling(self):
        self.assertLess(self.R["res"]["selfcheck"]["max_abs_diff"], 1e-12)
        d = self.drag[(self.drag.bucket == C.ALL) & (self.drag.N == 20) & (self.drag.adv_mult == 1.0) & (self.drag.c == 0.5)]
        for s in C.ORDER:
            q = d[d.strategy == s].set_index("K").drag_ann_pp
            self.assertTrue((q >= 0).all())
            self.assertAlmostEqual(q[1e8] / q[1e6], 10.0, places=9)       # 资金 ×100 → 拖累 ×10
            self.assertAlmostEqual(q[1e7] / q[1e5], 10.0, places=9)
        # c 翻倍 → 拖累翻倍；ADV 翻倍 → 拖累 ÷sqrt(2)
        a = self.drag[(self.drag.bucket == C.ALL) & (self.drag.N == 20) & (self.drag.K == 1e7) & (self.drag.strategy == "blend2")]
        x = a.set_index(["adv_mult", "c"]).drag_ann_pp
        self.assertAlmostEqual(x[(1.0, 1.0)] / x[(1.0, 0.5)], 2.0, places=9)
        self.assertAlmostEqual(x[(1.0, 0.5)] / x[(2.0, 0.5)], math.sqrt(2), places=9)
        # 同样总资金 K，N 翻倍 → 每只股票的资金减半 → 拖累 ÷sqrt(2)
        def at(n):
            return float(self.drag[(self.drag.bucket == C.ALL) & (self.drag.N == n) & (self.drag.K == 1e8) & (self.drag.strategy == "blend2")
                                   & (self.drag.adv_mult == 1.0) & (self.drag.c == 0.5)].drag_ann_pp.iloc[0])
        self.assertAlmostEqual(at(10) / at(20), math.sqrt(2), places=9)

    def test_net_equals_gross_minus_drag(self):
        d = self.drag
        np.testing.assert_allclose(d.gross_ann_pct - d.drag_ann_pp, d.net_ann_pct, atol=1e-9)

    def test_capacity_rows_consistent(self):
        a = self.cap[(self.cap.rule == "drag_1pp") & (self.cap.bucket == C.ALL) & np.isfinite(self.cap.K_star)]
        self.assertGreater(len(a), 0)
        for _, r in a.iterrows():
            self.assertAlmostEqual(100 * r.c * math.sqrt(r.K_star / r.N) * r.unit_drag_d1, 1.0, places=9)
        b = self.cap[(self.cap.rule == "half_of_gross") & (self.cap.gross_ann_pct <= 0)]
        self.assertTrue(b.K_star.isna().all())                           # 毛收益 ≤ 0 → 容量无定义
        ok = self.cap[(self.cap.rule == "half_of_gross") & (self.cap.gross_ann_pct > 0) & np.isfinite(self.cap.K_star)]
        for _, r in ok.iterrows():
            self.assertAlmostEqual(100 * r.c * math.sqrt(r.K_star / r.N) * r.unit_drag_d1, r.gross_ann_pct / 2, places=9)

    def test_participation_monotone_in_capital(self):
        p = self.part[(self.part.bucket == C.ALL) & (self.part.N == 20) & (self.part.adv_mult == 1.0)]
        for s in C.ORDER:
            q = p[p.strategy == s].sort_values("K")
            self.assertTrue(q.share_gt5.dropna().is_monotonic_increasing)
            self.assertTrue((q.share_gt10.dropna() <= q.share_gt5.dropna() + 1e-12).all())    # >10% 的交易一定也 >5%
            self.assertTrue((q.share_gt100.dropna() <= q.share_gt10.dropna() + 1e-12).all())
        self.assertEqual(set(self.part.K), set(C.CAPITALS))
        for K in (1e5, 1e6, 1e7, 1e8):                                    # 任务要求的四档都在
            self.assertIn(K, set(self.part.K))

    def test_workers_do_not_change_results(self):
        out2 = Path(self.tmp.name) / "out2"
        C.run(self.root, out2, n_stocks=3, seed=1, workers=2, log=lambda *_: None)
        a = pd.read_csv(self.out / "capacity_drag.csv")
        b = pd.read_csv(out2 / "capacity_drag.csv")
        pd.testing.assert_frame_equal(a, b)


if __name__ == "__main__":
    unittest.main(verbosity=1)
