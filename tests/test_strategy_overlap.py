"""C06 持仓重合度脚本的测试：.venv/bin/python tests/test_strategy_overlap.py

全部用临时目录里的合成行情（src.data.synthetic 随机游走），不读写 data/，不联网。
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
from config import MARKETS  # noqa: E402
from src import data, registry  # noqa: E402
import strategy_overlap as O  # noqa: E402


def fake_market(root: Path, extra_future_scale: float | None = None, cutoff_rows: bool = False):
    """按 config 里的真实代码名，写合成行情：<root>/daily/{cn,us}/*.csv、<root>/daily/cn_all/*.parquet + 名单。
    合成数据越过研究截止日；extra_future_scale 把截止日之后的价格乘以这个倍数（制造「未来」的异常）；
    cutoff_rows=True 则直接丢掉截止日之后的行。"""
    daily = root / "daily"
    seed = 0

    def make(n=3900, start="2012-01-02"):
        nonlocal seed
        seed += 1
        df = data.synthetic(n=n, seed=seed, start=start)
        if extra_future_scale:
            df.loc[df.index > O.CUTOFF, ["Open", "High", "Low", "Close"]] *= extra_future_scale
        if cutoff_rows:
            df = df.loc[:O.CUTOFF]
        return df

    for market in ("cn", "us"):
        (daily / market).mkdir(parents=True, exist_ok=True)
        for sym in MARKETS[market]["universe"]:
            df = make()
            if sym == MARKETS[market]["cash"]:       # 现金：每天稳定小幅增长
                df[["Open", "High", "Low", "Close"]] = np.repeat(100 * np.cumprod(np.full(len(df), 1.00005))[:, None], 4, axis=1)
            df.to_csv(daily / market / f"{sym}.csv")
    (daily / "cn_all").mkdir(parents=True, exist_ok=True)
    rows = [("600001", "正常股甲"), ("600002", "正常股乙"), ("600003", "ST某某"), ("600004", "正常股丙"), ("600005", "新股丁"),
            ("600006", "正常股戊")]
    for code, name in rows:
        df = make(start="2019-06-03" if name == "新股丁" else "2012-01-02", n=2000 if name == "新股丁" else 3900)
        df.to_parquet(daily / "cn_all" / f"{code}.parquet")
    pd.DataFrame(rows, columns=["code", "name"]).to_csv(daily / "cn_all_list.csv", index=False)
    return daily


def snapshot(root: Path) -> dict:
    return {str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns) for p in sorted(root.rglob("*")) if p.is_file()}


class FormulaTests(unittest.TestCase):
    def test_jaccard_pooled(self):
        a = np.array([1, 1, 0, 0, 1], bool)
        b = np.array([1, 0, 0, 1, 1], bool)
        self.assertAlmostEqual(O.jaccard_pooled(a, b), 2 / 4)             # 交 2，并 4
        self.assertAlmostEqual(O.jaccard_pooled(a, a), 1.0)
        self.assertAlmostEqual(O.jaccard_pooled(a, ~a), 0.0)
        self.assertTrue(math.isnan(O.jaccard_pooled(~a & a, ~a & a)))     # 并为空

    def test_jaccard_weighted(self):
        a = np.array([1.0, 0.5, 0.0])
        b = np.array([0.5, 0.5, 1.0])
        self.assertAlmostEqual(O.jaccard_weighted(a, b), (0.5 + 0.5 + 0) / (1 + 0.5 + 1))
        x, y = np.array([1.0, 1, 0, 0]), np.array([1.0, 0, 0, 1])          # 0/1 仓位时等于普通 Jaccard
        self.assertAlmostEqual(O.jaccard_weighted(x, y), O.jaccard_pooled(x > 0, y > 0))

    def test_jaccard_independent_matches_simulation(self):
        rng = np.random.default_rng(0)
        a, b = rng.random(400_000) < 0.3, rng.random(400_000) < 0.6
        self.assertAlmostEqual(O.jaccard_independent(0.3, 0.6), O.jaccard_pooled(a, b), delta=0.005)
        self.assertAlmostEqual(O.jaccard_independent(1.0, 1.0), 1.0)

    def test_effective_n_extremes(self):
        for n in (2, 3, 7):
            e = O.effective_n(np.eye(n))
            self.assertAlmostEqual(e["pr"], n)
            self.assertAlmostEqual(e["entropy"], n)
            self.assertAlmostEqual(O.equal_weight_n(np.eye(n)), n)
            ones = O.effective_n(np.ones((n, n)))
            self.assertAlmostEqual(ones["pr"], 1.0)
            self.assertAlmostEqual(ones["entropy"], 1.0)
            self.assertAlmostEqual(O.equal_weight_n(np.ones((n, n))), 1.0)

    def test_effective_n_two_assets_closed_form(self):
        r = 0.6
        C = np.array([[1, r], [r, 1.0]])
        self.assertAlmostEqual(O.effective_n(C)["pr"], 2 / (1 + r ** 2))      # 特征值 1±r
        self.assertAlmostEqual(O.equal_weight_n(C), 2 / (1 + r))
        self.assertAlmostEqual(O.mean_offdiag(C), r)

    def test_effective_n_monotone_in_correlation(self):
        vals = [O.effective_n(np.full((4, 4), r) + (1 - r) * np.eye(4))["pr"] for r in (0.0, 0.3, 0.6, 0.9)]
        self.assertTrue(all(a > b for a, b in zip(vals, vals[1:])))

    def test_corr_matrix_constant_column_is_nan(self):
        R = np.random.default_rng(1).standard_normal((50, 3))
        R[:, 1] = 0.0
        C = O.corr_matrix(R)
        self.assertTrue(np.isnan(C[1]).all() and np.isnan(C[:, 1]).all())      # 常数列：整行整列没有定义
        want = np.corrcoef(R[:, [0, 2]], rowvar=False)
        np.testing.assert_allclose(C[np.ix_([0, 2], [0, 2])], want)            # 其余照常算
        self.assertTrue(np.isnan(O.corr_matrix(np.zeros((50, 2)))).all())
        self.assertTrue(math.isnan(O.effective_n(C)["pr"]))
        self.assertTrue(np.isfinite(O.effective_n(C[np.ix_([0, 2], [0, 2])])["pr"]))

    def test_effective_n_nan_input(self):
        C = np.array([[1, np.nan], [np.nan, np.nan]])
        self.assertTrue(all(math.isnan(v) for v in O.effective_n(C).values()))
        self.assertTrue(math.isnan(O.equal_weight_n(C)))

    def test_residual_removes_market(self):
        rng = np.random.default_rng(2)
        m = rng.standard_normal(5000)
        R = np.column_stack([2 * m + rng.standard_normal(5000), -m + rng.standard_normal(5000)])
        res = O.residual_on(R, m)
        self.assertLess(abs(np.corrcoef(res[:, 0], m)[0, 1]), 1e-9)
        self.assertLess(abs(np.corrcoef(res[:, 1], m)[0, 1]), 1e-9)
        # 两列都靠大盘驱动时原始相关为负，去大盘后接近 0
        self.assertLess(np.corrcoef(R, rowvar=False)[0, 1], -0.3)
        self.assertLess(abs(np.corrcoef(res, rowvar=False)[0, 1]), 0.05)

    def test_bootstrap_deterministic_and_independent_noise(self):
        R = np.random.default_rng(3).standard_normal((1500, 7))
        a = O.block_bootstrap(R, 30, seed=1)
        b = O.block_bootstrap(R, 30, seed=1)
        pd.testing.assert_frame_equal(a, b)
        self.assertEqual(len(a), 30)
        self.assertGreater(a["neff_pr_3"].mean(), 2.8)           # 独立噪声：3 个成分接近 3 个独立策略
        self.assertLess(abs(a["mean_corr_3"].mean()), 0.1)

    def test_summary_stats_blend_identity(self):
        """blend2 列恰好是 3 个成分的均值 → 与成分均值的相关为 1。"""
        rng = np.random.default_rng(4)
        R = rng.standard_normal((800, 7))
        c3 = [O.ORDER.index(s) for s in O.COMPONENTS]
        R[:, 0] = R[:, c3].mean(axis=1)
        s = O.summary_stats(R)
        self.assertAlmostEqual(s["blend_vs_avg_corr"], 1.0)
        # 独立等波动成分取均值：波动 = 1/√3 倍
        self.assertAlmostEqual(s["vol_ratio"], 1 / math.sqrt(3), delta=0.04)


class DefinitionTests(unittest.TestCase):
    def test_components_are_blend2_definition(self):
        """脚本假设 blend2 = 三个成分的 1/3 平均；这里用 registry 真正的 blend2 验证，策略规则变了会在这里报错。"""
        df = data.synthetic(n=1200, seed=11, start="2018-01-02")
        mcfg = MARKETS["cn"]
        W = {s: registry.target_weights(s, {"X": df}, "X", registry.clean_params(s, {}), mcfg).iloc[:, 0]
             for s in ("blend2", *O.COMPONENTS)}
        want = sum(W[s] for s in O.COMPONENTS) / 3
        np.testing.assert_allclose(W["blend2"].to_numpy(), want.to_numpy(), atol=1e-12)

    def test_order_matches_formal_strategies(self):
        self.assertEqual(sorted(O.ORDER), sorted(O.STRATS))
        self.assertEqual(O.ORDER[0], "blend2")
        self.assertEqual(O.CUTOFF, "2026-03-31")
        with self.assertRaises(ValueError):
            O.check_end("2026-04-01")


class EndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        cls.src = fake_market(base / "src")
        cls.before = snapshot(base / "src")
        cls.out = base / "out"
        cls.res = O.run(base / "src", cls.out, n_stocks=3, seed=6, boot=20, log=lambda *_: None)
        cls.base = base

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_does_not_touch_input_dir(self):
        self.assertEqual(snapshot(self.base / "src"), self.before)

    def test_outputs_exist(self):
        for f in ("overlap_matrices.csv", "overlap_effective_n.csv", "overlap_blend2_vs_components.csv", "overlap_yearly.csv",
                  "overlap_bootstrap.csv", "overlap_meta.json", "overlap_tables.md", "overlap_stock_sample.csv",
                  "fig_jaccard.png", "fig_corr.png", "fig_effective_n.png", "fig_yearly.png"):
            self.assertTrue((self.out / f).exists(), f)
            self.assertGreater((self.out / f).stat().st_size, 0, f)

    def test_window_never_past_cutoff(self):
        for g, r in self.res.items():
            self.assertLessEqual(pd.Timestamp(r["end"]), pd.Timestamp(O.CUTOFF), g)
            self.assertGreaterEqual(pd.Timestamp(r["start"]), pd.Timestamp(O.WIN_START), g)
            self.assertLessEqual(r["corr"]["port_ret"].index.max(), pd.Timestamp(O.CUTOFF))
        self.assertLessEqual(pd.read_csv(self.out / "overlap_yearly.csv")["year"].max(), 2026)

    def test_matrices_are_valid(self):
        for g, r in self.res.items():
            for key, M in r["overlap"].items():
                self.assertTrue(np.allclose(M.to_numpy(), M.to_numpy().T, equal_nan=True), (g, key))
            J = r["overlap"]["jaccard"].to_numpy()
            self.assertTrue(((J >= 0) & (J <= 1)).all())
            C = r["corr"]["portfolio"].to_numpy()
            self.assertTrue(np.allclose(np.diag(C), 1.0))
            self.assertTrue(((C >= -1 - 1e-9) & (C <= 1 + 1e-9)).all())
            for x in r["neff"]:
                self.assertGreaterEqual(x["neff_pr"], 1 - 1e-9)
                self.assertLessEqual(x["neff_pr"], x["N"] + 1e-9)
                self.assertLessEqual(x["neff_equal_weight"], x["N"] + 1e-9)

    def test_blend2_held_is_union_of_components(self):
        """成分仓位非负 → blend2 > 0 当且仅当至少一个成分 > 0，所以 blend2 的持有格子 = 成分持有格子的并集。"""
        per = {}
        for sym in ("510300", "510500"):
            df = pd.read_csv(self.src / "cn" / f"{sym}.csv", index_col=0, parse_dates=True).loc[:O.CUTOFF]
            per[sym] = O.instrument_panel(df, sym, MARKETS["cn"], None, 0.0005, 0.1)
        g = O.assemble(per)
        held = {n: g["wt"][n] > O.HELD_EPS for n in O.ORDER}
        union = held["trend_ens"] | held["rsi2"] | held["donchian_s1"]
        np.testing.assert_array_equal(held["blend2"], union)

    def test_assemble_marks_missing_days_invalid(self):
        """一个品种缺了某天行情（停牌/数据缺口）：那天对它必须是无效格子，不能当成有效，否则逐品种相关会全变 NaN。"""
        a = data.synthetic(n=1500, seed=21, start="2019-01-02")
        b = a.drop(a.index[1000])                                  # b 缺一天
        gap_day = a.index[1000]
        per = {s: O.instrument_panel(df, s, MARKETS["cn"], None, 0.0005, 0.1) for s, df in (("AAA", a), ("BBB", b))}
        g = O.assemble(per)
        row = int(g["dates"].get_loc(gap_day))
        self.assertEqual(g["valid"][row].tolist(), [True, False])
        self.assertTrue(np.isnan(g["ret"]["blend2"][row, 1]))
        self.assertEqual(g["wt"]["blend2"][row, 1], 0.0)
        # 有效格子里不应有 NaN 收益
        for n in O.ORDER:
            self.assertFalse(np.isnan(g["ret"][n][g["valid"]]).any(), n)
        C = O.correlation_tables(g)
        self.assertTrue(np.isfinite(C["inst_mean"].to_numpy()).all())
        self.assertEqual(len(C["inst_neff"].dropna()), 2)

    def test_stock_sample_filters(self):
        s = pd.read_csv(self.out / "overlap_stock_sample.csv", dtype={"code": str})
        self.assertEqual(len(s), 3)
        self.assertNotIn("600003", set(s["code"]))      # ST
        self.assertNotIn("600005", set(s["code"]))      # 2019 年才上市，窗口前历史不足
        again = O.sample_stocks(self.src / "daily" if (self.src / "daily").exists() else self.src, 3, 6, {})[1]
        self.assertEqual(list(again["code"]), list(s["code"]))   # 同一个种子 → 同一批

    def test_no_lookahead(self):
        """把截止日之后的行情整个丢掉、或者改成离谱的价格，结果必须逐字节不变。"""
        outs = []
        for name, kw in (("dropped", {"cutoff_rows": True}), ("scaled", {"extra_future_scale": 5.0}), ("plain", {})):
            root = self.base / f"la_{name}"
            fake_market(root, **kw) if name != "plain" else None
            if name == "plain":
                root = self.base / "src"
            # 三份数据的合成种子相同（seed 计数器每次从 0 开始），截止日之前完全一样
            out = self.base / f"la_out_{name}"
            O.run(root, out, n_stocks=3, seed=6, boot=10, log=lambda *_: None)
            outs.append(out)
        for f in ("overlap_matrices.csv", "overlap_effective_n.csv", "overlap_yearly.csv", "overlap_bootstrap.csv"):
            ref = (outs[0] / f).read_bytes()
            for o in outs[1:]:
                self.assertEqual((o / f).read_bytes(), ref, f)


if __name__ == "__main__":
    unittest.main(verbosity=2)
