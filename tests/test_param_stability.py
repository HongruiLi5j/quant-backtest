"""C14 参数稳定性热力图脚本的测试：.venv/bin/python tests/test_param_stability.py

全部用临时目录里的合成行情（src.data.synthetic 随机游走）和手算的小例子，不读写 data/，不联网。
"""
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
import param_stability as P  # noqa: E402

FAKE = {"universe": {"AAA": "测试品种", "CSH": "测试现金"}, "cash": "CSH", "cost_bp": 5, "stock_cost_bp": 8,
        "dual_momentum": {"risky": ("AAA",), "safe": "CSH", "cash": "CSH"}}


def fake_df(start="2012-01-02", n=3800, seed=0):
    return data.synthetic(n=n, seed=seed, start=start)


def inst_from(df, cash_ret=0.00005):
    cash = pd.Series(cash_ret, index=df.index)
    return P.Inst(df, "AAA", FAKE, cash, 0.0005, None)


class GridTests(unittest.TestCase):
    def test_center_is_exact_default(self):
        for name in P.ORDER:
            k1, v1, k2, v2 = P.grid_axes(name)
            self.assertEqual(v1[P.MID], P.param_spec(name, k1)["default"], name)
            self.assertEqual(v2[P.MID], P.param_spec(name, k2)["default"], name)

    def test_five_distinct_increasing_values(self):
        for name in P.ORDER:
            _, v1, _, v2 = P.grid_axes(name)
            for v in (v1, v2):
                self.assertEqual(len(v), 5, name)
                self.assertEqual(sorted(set(v)), list(v), name)       # 5 档互不相同且递增

    def test_integer_params_rounded_half_up(self):
        # fast=5：×0.5=2.5→3（银行家舍入会得到 2），×1.5=7.5→8
        self.assertEqual(P.grid_values("ma_adx_original", "fast"), [3, 4, 5, 6, 8])
        self.assertEqual(P.grid_values("ma_adx_original", "slow"), [10, 15, 20, 25, 30])
        self.assertEqual(P.grid_values("rsi2", "entry_th"), [5, 8, 10, 13, 15])
        self.assertTrue(all(isinstance(v, int) for v in P.grid_values("rsi2", "trend_len")))

    def test_float_params_keep_decimals(self):
        self.assertEqual(P.grid_values("trend_vol", "buffer"), [0.005, 0.0075, 0.01, 0.0125, 0.015])
        self.assertEqual(P.grid_values("ma_adx_plus", "atr_mult"), [1.5, 2.25, 3.0, 3.75, 4.5])

    def test_grid_is_plus_minus_50_percent(self):
        for name in P.ORDER:
            for k in P.SPECS[name]:
                d = P.param_spec(name, k)["default"]
                v = P.grid_values(name, k)
                self.assertAlmostEqual(v[0], d * 0.5, delta=0.5 + 1e-9)       # 整数取整最多差 0.5
                self.assertAlmostEqual(v[-1], d * 1.5, delta=0.5 + 1e-9)

    def test_values_survive_clean_params(self):
        """网格取值都在登记表允许范围内：clean_params 不会把它们悄悄夹回去或取整，否则格子的横纵坐标就是假的。"""
        for name in P.ORDER:
            for k in P.SPECS[name]:
                for v in P.grid_values(name, k):
                    got = registry.clean_params(name, {k: v})[k]
                    self.assertAlmostEqual(got, v, places=9, msg=f"{name}.{k}={v}")

    def test_specs_cover_the_seven_formal(self):
        self.assertEqual(sorted(P.SPECS), sorted(P.STRATS))
        self.assertEqual(len(P.ORDER), 7)
        for name, (a, b) in P.SPECS.items():
            self.assertNotEqual(a, b)
            keys = {p["key"] for p in registry.STRATEGIES[name]["params"]}
            self.assertTrue({a, b} <= keys, name)

    def test_neighbour_counts(self):
        self.assertEqual(len(P.neighbours(0, 0)), 3)        # 角
        self.assertEqual(len(P.neighbours(0, 2)), 5)        # 边
        self.assertEqual(len(P.neighbours(2, 2)), 8)        # 内部
        self.assertNotIn((2, 2), P.neighbours(2, 2))
        self.assertEqual(len(P.adjacent_pairs()), 40)       # 5×4 + 4×5


class SurfaceTests(unittest.TestCase):
    def test_roughness_hand_computed(self):
        self.assertEqual(P.roughness(np.ones((5, 5))), 0.0)
        G = np.add.outer(np.arange(5.0), np.zeros(5))      # G[i,j] = i：纵向相邻差 1（20 对），横向差 0（20 对）
        self.assertAlmostEqual(P.roughness(G), 0.5)

    def test_smooth_p_small_for_ramp_and_uniform_for_noise(self):
        ramp = np.add.outer(np.arange(5.0), np.arange(5.0)) / 10
        self.assertLess(P.smooth_pvalue(ramp, 2000, 1), 0.01)
        ps = [P.smooth_pvalue(np.random.default_rng(s).normal(size=(5, 5)), 300, s) for s in range(60)]
        self.assertTrue(0.3 < float(np.mean(ps)) < 0.7, np.mean(ps))     # 纯噪声网格：p 大致均匀，均值约 0.5

    def test_smooth_p_nan_grid(self):
        G = np.ones((5, 5))
        G[0, 0] = np.nan
        self.assertTrue(np.isnan(P.smooth_pvalue(G, 100, 1)))

    def test_axis_rho(self):
        G = np.add.outer(np.arange(5.0), np.zeros(5))
        r1, r2 = P.axis_rho(G)
        self.assertAlmostEqual(r1, 1.0)
        self.assertEqual(r2, 0.0)                              # 沿 j 方向恒定
        self.assertEqual(P.axis_rho(np.ones((5, 5))), (0.0, 0.0))
        r1, _ = P.axis_rho(-G)
        self.assertAlmostEqual(r1, -1.0)

    def test_plateau(self):
        G = np.full((5, 5), 0.80)
        G[1, 1] = 0.84                                       # 最优比邻居只高一点
        m = P.surface_metrics(G, reps=100)
        self.assertEqual(m["cls"], "平台")
        self.assertAlmostEqual(m["best"], 0.84)
        self.assertAlmostEqual(m["neighbor_mean"], 0.80)
        self.assertAlmostEqual(m["drop_ratio"], 0.04 / 0.84)
        self.assertEqual(m["plateau_share"], 1.0)
        self.assertFalse(m["edge"])

    def test_isolated_spike(self):
        G = np.full((5, 5), 0.10)
        G[1, 3] = 1.00                                        # 内部的孤立一格
        m = P.surface_metrics(G, reps=100)
        self.assertEqual(m["cls"], "尖峰")
        self.assertAlmostEqual(m["drop"], 0.90)
        self.assertAlmostEqual(m["drop_ratio"], 0.90)
        self.assertAlmostEqual(m["plateau_share"], 1 / 25)    # 只有它自己 ≥ 0.8×最优
        self.assertEqual((m["best_i"], m["best_j"]), (1, 3))
        self.assertEqual(m["default_rank"], 2)                # 并列的不算「比它高」：只有最优那 1 格严格更高 → 名次 2

    def test_ramp_is_slope_not_spike(self):
        G = np.add.outer(np.arange(5.0), np.zeros(5)) / 4     # 夏普随第一个参数线性增大，最优在最后一行（边缘）
        m = P.surface_metrics(G, reps=100)
        self.assertTrue(m["edge"])
        self.assertEqual(m["best_i"], 4)
        self.assertEqual(m["cls"], "斜坡")
        self.assertAlmostEqual(m["rho_1"], 1.0)

    def test_edge_spike_without_trend_stays_spike(self):
        G = np.full((5, 5), 0.10)
        G[0, 2] = 1.00                                        # 边缘上孤立一格，没有任何单调趋势
        m = P.surface_metrics(G, reps=100)
        self.assertTrue(m["edge"])
        self.assertEqual(m["cls"], "尖峰")

    def test_all_negative_has_no_ratio(self):
        G = -np.abs(np.random.default_rng(0).normal(size=(5, 5))) - 0.1
        m = P.surface_metrics(G, reps=50)
        self.assertEqual(m["cls"], "无正夏普")
        self.assertTrue(np.isnan(m["drop_ratio"]))
        self.assertEqual(m["frac_positive"], 0.0)

    def test_opt_gain_and_default(self):
        G = np.full((5, 5), 0.5)
        G[2, 2] = 0.4
        G[0, 4] = 0.9
        m = P.surface_metrics(G, reps=50)
        self.assertAlmostEqual(m["default"], 0.4)
        self.assertAlmostEqual(m["opt_gain"], 0.5)
        self.assertEqual(m["default_rank"], 25)               # 默认格是全网格最差的

    def test_nan_cells_do_not_crash(self):
        G = np.full((5, 5), 0.5)
        G[0, 0] = np.nan
        G[3, 3] = 0.9
        m = P.surface_metrics(G, reps=50)
        self.assertAlmostEqual(m["best"], 0.9)
        self.assertEqual(P.surface_metrics(np.full((5, 5), np.nan))["cls"], "无数据")


class BacktestTests(unittest.TestCase):
    def test_truncated_at_is_end(self):
        df = fake_df()
        self.assertGreater(df.index[-1], pd.Timestamp(P.IS_END))
        i = inst_from(df)
        self.assertLessEqual(i.df.index[-1], pd.Timestamp(P.IS_END))
        r = i.cell_returns("trend_vol", {})
        self.assertLessEqual(r.index[-1], pd.Timestamp(P.IS_END))

    def test_out_of_sample_data_never_matters(self):
        """把 2019-12-31 以后的行情乱改 ×3，样本内的格子日收益必须逐位不变（样本外数据没进计算）。"""
        df = fake_df()
        bad = df.copy()
        after = bad.index > pd.Timestamp(P.IS_END)
        self.assertGreater(after.sum(), 100)
        bad.loc[after, ["Open", "High", "Low", "Close"]] *= 3.0
        a, b = inst_from(df), inst_from(bad)
        for name in P.ORDER:
            k1, v1, k2, v2 = P.grid_axes(name)
            prm = {k1: v1[0], k2: v2[4]}
            pd.testing.assert_series_equal(a.cell_returns(name, prm), b.cell_returns(name, prm), obj=name)

    def test_no_future_leak_within_sample(self):
        """把样本内最后 200 天的价格乱改，更早的日收益不变（信号只用当天及以前，成交在下一根）。"""
        df = fake_df().loc[:P.IS_END]
        bad = df.copy()
        bad.iloc[-200:, :4] *= 2.0
        cut = df.index[-201]
        for name in ("ma_adx_plus", "donchian_s1", "rsi2"):
            k1, v1, k2, v2 = P.grid_axes(name)
            prm = {k1: v1[3], k2: v2[1]}
            ra = inst_from(df).cell_returns(name, prm).loc[:cut]
            rb = inst_from(bad).cell_returns(name, prm).loc[:cut]
            pd.testing.assert_series_equal(ra, rb, obj=name)

    def test_center_cell_equals_default_backtest(self):
        i = inst_from(fake_df())
        for name in P.ORDER:
            k1, v1, k2, v2 = P.grid_axes(name)
            pd.testing.assert_series_equal(i.cell_returns(name, {k1: v1[2], k2: v2[2]}), i.cell_returns(name, {}), obj=name)

    def test_different_cells_differ(self):
        i = inst_from(fake_df())
        a = i.cell_returns("ma_adx_original", {"fast": 3, "slow": 10})
        b = i.cell_returns("ma_adx_original", {"fast": 8, "slow": 30})
        self.assertFalse(np.allclose(a.to_numpy(), b.to_numpy()))

    def test_window_start(self):
        df = fake_df(start="2012-01-02")
        i = inst_from(df)
        self.assertEqual(i.start, max(pd.Timestamp(P.IS_START), df.index[P.WARM]))
        self.assertGreaterEqual(i.cell_returns("blend2", {}).index[0], pd.Timestamp(P.IS_START))
        late = fake_df(start="2014-03-03", n=2000)               # 上市晚：起点由预热期决定
        j = inst_from(late)
        self.assertEqual(j.start, late.index[P.WARM])

    def test_buy_hold_reference(self):
        """买入持有对照：同一窗口、也只用到 IS_END；夏普 = metrics.sharpe(日收益 − 现金)。"""
        from src.metrics import sharpe
        i = inst_from(fake_df())
        r = i.bh_returns()
        self.assertEqual(r.index[0], i.cell_returns("trend_vol", {}).index[0])
        self.assertLessEqual(r.index[-1], pd.Timestamp(P.IS_END))
        self.assertAlmostEqual(P.group_bh_sharpe([i], i.cash), sharpe(r - i.cash.reindex(r.index).fillna(0.0)), places=12)

    def test_group_grid_sharpe_matches_manual(self):
        """单品种组：网格里每格夏普 = metrics.sharpe(日收益 − 现金)，年化收益 = 复利年化。"""
        from src.metrics import sharpe
        i = inst_from(fake_df())
        cells = P.inst_cells(i)
        self.assertEqual(len(cells), 7 * 25)
        grids = P.group_grids([cells], i.cash)
        r = cells[("rsi2", 1, 3)]
        self.assertAlmostEqual(grids["rsi2"][0][1, 3], sharpe(r - i.cash.reindex(r.index).fillna(0.0)), places=12)
        self.assertAlmostEqual(grids["rsi2"][1][1, 3], float(np.prod(1 + r.to_numpy()) ** (252 / len(r)) - 1), places=12)


class EndToEndTests(unittest.TestCase):
    def test_end_to_end(self):
        """用合成行情冒充两个市场的 universe 和 3 只个股，跑完整脚本，检查输出文件、行数、边界。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "data"
            for i, mk in enumerate(("us", "cn")):
                mc = MARKETS[mk]
                d = root / "daily" / mk
                d.mkdir(parents=True, exist_ok=True)
                for j, s in enumerate(mc["universe"]):
                    data.synthetic(n=3000, seed=10 * i + j, start="2012-06-01").to_csv(d / f"{s}.csv")
            (root / "daily" / "cn_all").mkdir()
            pd.DataFrame({"code": ["600001", "600002", "600003"], "name": ["甲", "乙", "丙"]}).to_csv(root / "daily" / "cn_all_list.csv", index=False)
            for k, c in enumerate(["600001", "600002", "600003"]):
                data.synthetic(n=3000, seed=100 + k, start="2011-03-01").to_parquet(root / "daily" / "cn_all" / f"{c}.parquet")
            out = Path(tmp) / "out"
            P.main(["--out-dir", str(out), "--data-dir", str(root), "--n-stocks", "2", "--perm", "50"])
            for f in ("grid_long.csv", "surface_metrics.csv", "param_meta.json", "param_stability.md", "stock_sample.csv",
                      "heatmap_SPY.png", "heatmap_510300.png", "heatmap_us_ew.png", "heatmap_cn_ew.png", "heatmap_cn_stock.png",
                      "fig_summary_drop_ratio.png"):
                self.assertTrue((out / f).exists(), f)
            L = pd.read_csv(out / "grid_long.csv", dtype={"group": str})
            self.assertEqual(set(L["group"]), {"SPY", "510300", "us_ew", "cn_ew", "cn_stock"})
            self.assertEqual(len(L), 5 * 7 * 25)
            self.assertEqual(L["is_default"].sum(), 5 * 7)
            self.assertTrue(((L["i"] == 2) & (L["j"] == 2) == L["is_default"]).all())
            S = pd.read_csv(out / "surface_metrics.csv", dtype={"group": str})
            self.assertEqual(len(S), 5 * 7)
            self.assertTrue(set(S["cls"]) <= {"平台", "斜坡", "尖峰", "过渡", "无正夏普", "无数据"})
            sp = S["smooth_p"].dropna()                      # 合成数据里个别格子一直空仓 → 夏普 NaN → 没有 p 值
            self.assertGreater(len(sp), 20)
            self.assertTrue(((sp > 0) & (sp <= 1)).all())
            self.assertIn("bh_sharpe", S.columns)
            self.assertEqual(S.groupby("group")["bh_sharpe"].nunique().max(), 1)       # 买入持有夏普每组一个值
            ok = S.dropna(subset=["default"])                  # 默认格夏普是 NaN（一直空仓）的行，名次也是 NaN
            self.assertGreater(len(ok), 20)
            self.assertTrue(((ok["default_rank"] >= 1) & (ok["default_rank"] <= 25)).all())
            self.assertTrue((ok["best"] >= ok["default"] - 1e-12).all())
            md = (out / "param_stability.md").read_text(encoding="utf-8")
            self.assertIn("斜坡", md)
            import json
            meta = json.loads((out / "param_meta.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["is_end"], "2019-12-31")
            self.assertEqual(meta["n_stocks_used"], 2)
            self.assertTrue(all(pd.Timestamp(v) <= pd.Timestamp(P.IS_END) for v in meta["window_start_per_instrument"].values()))


if __name__ == "__main__":
    unittest.main()
