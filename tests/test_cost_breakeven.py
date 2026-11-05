"""C11 交易成本敏感性脚本的测试：.venv/bin/python tests/test_cost_breakeven.py

全部用临时目录里的合成行情（src.data.synthetic 随机游走）和手算的小例子，不读写 data/，不联网。
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
import cost_breakeven as C  # noqa: E402

FAKE = {"universe": {"AAA": "测试品种", "CSH": "测试现金"}, "cash": "CSH", "cost_bp": 5, "stock_cost_bp": 8,
        "dual_momentum": {"risky": ("AAA",), "safe": "CSH", "cash": "CSH"}}


def fake_df(start="2012-01-02", n=3800, seed=0):
    return data.synthetic(n=n, seed=seed, start=start).loc[:C.CUTOFF]


def fake_cash(df):
    return pd.Series(0.00005, index=df.index)


class FormulaTests(unittest.TestCase):
    def test_cagr_hand_computed(self):
        # 两天 +10%、−10%：(1.1×0.9)^(252/2) − 1
        self.assertAlmostEqual(C.cagr([0.10, -0.10]), (1.1 * 0.9) ** 126 - 1)
        self.assertTrue(math.isnan(C.cagr([])))
        # 每天 0.01% 一整年（252 天）→ 年化 = 1.0001^252 − 1
        self.assertAlmostEqual(C.cagr([0.0001] * 252), 1.0001 ** 252 - 1)

    def test_annual_turnover(self):
        self.assertAlmostEqual(C.annual_turnover([0.01] * 100), 2.52)
        self.assertAlmostEqual(C.annual_turnover([0.0, 0.0, 0.5, 0.0]), 252 * 0.125)

    def test_breakeven_interp_hand_computed(self):
        # 8bp 时 +1%，15bp 时 −1%：线性插值 8 + 1/(1−(−1)) × 7 = 11.5
        be, kind = C.breakeven(C.COSTS_BP, [0.02, 0.01, -0.01, -0.03])
        self.assertEqual(kind, "interp")
        self.assertAlmostEqual(be, 11.5)

    def test_breakeven_exact_zero_on_grid_point(self):
        be, kind = C.breakeven(C.COSTS_BP, [0.02, 0.0, -0.01, -0.02])
        self.assertEqual(kind, "interp")
        self.assertAlmostEqual(be, 8.0)

    def test_breakeven_extrapolates_when_still_positive_at_30bp(self):
        # 15→30bp 每 15bp 少 1%，30bp 时剩 1% → 再走 15bp 到 0 → 45bp
        be, kind = C.breakeven(C.COSTS_BP, [0.04, 0.03, 0.02, 0.01])
        self.assertEqual(kind, "extrap")
        self.assertAlmostEqual(be, 45.0)

    def test_breakeven_edge_cases(self):
        self.assertEqual(C.breakeven(C.COSTS_BP, [-0.01, -0.02, -0.03, -0.04])[1], "loss_at_zero")
        self.assertTrue(math.isnan(C.breakeven(C.COSTS_BP, [0.0, -0.02, -0.03, -0.04])[0]))     # 0 成本刚好为 0 也算没有盈利空间
        be, kind = C.breakeven(C.COSTS_BP, [0.01, 0.01, 0.01, 0.02])
        self.assertEqual(kind, "flat")
        self.assertTrue(math.isinf(be))
        self.assertEqual(C.breakeven(C.COSTS_BP, [0.01, float("nan"), 0.0, 0.0])[1], "nodata")

    def test_breakeven_capped(self):
        self.assertEqual(C.breakeven_capped(C.COSTS_BP, [-0.01, -0.02, -0.03, -0.04]), 0.0)
        self.assertEqual(C.breakeven_capped(C.COSTS_BP, [0.01, 0.01, 0.01, 0.02]), C.BE_CAP)
        self.assertEqual(C.breakeven_capped(C.COSTS_BP, [0.04, 0.03, 0.02, 0.015], cap=40.0), 40.0)
        self.assertAlmostEqual(C.breakeven_capped(C.COSTS_BP, [0.02, 0.01, -0.01, -0.03]), 11.5)

    def test_approx_and_drag_ratio(self):
        # 毛超额 5%、年换手 5 倍 → 每 1bp 成本少 5bp 收益 → 近似盈亏平衡 100bp
        self.assertAlmostEqual(C.approx_breakeven_bp(0.05, 5.0), 100.0)
        self.assertTrue(math.isnan(C.approx_breakeven_bp(-0.01, 5.0)))
        self.assertTrue(math.isnan(C.approx_breakeven_bp(0.05, 0.0)))
        # 收益从 8% 掉到 6.5%，30bp × 换手 5 = 1.5% → 比值 1
        self.assertAlmostEqual(C.drag_ratio(0.08, 0.065, 30, 5.0), 1.0)
        self.assertTrue(math.isnan(C.drag_ratio(0.08, 0.065, 30, 0.0)))

    def test_bootstrap_constant_returns_reproduces_point_estimate(self):
        """日收益是常数时，任何重抽样都一样 → 自助法每一次都等于点估计；同种子可复现。"""
        T = 400
        costs = C.COSTS_BP
        daily = np.array([0.0004, 0.00036, 0.00033, 0.00028])        # 每个成本一个常数日收益
        R = np.tile(daily, (T, 1))
        rf = np.full(T, 0.0001)
        ex = [C.cagr(R[:, j]) - C.cagr(rf) for j in range(4)]
        point = C.breakeven_capped(costs, ex)
        bs = C.block_bootstrap_breakeven(R, rf, costs, reps=20, seed=1)
        np.testing.assert_allclose(bs, point)
        rng = np.random.default_rng(0)
        R2 = R + rng.normal(0, 0.01, (T, 1))                          # 加上共同的噪声（所有成本列同一批噪声）
        a = C.block_bootstrap_breakeven(R2, rf, costs, reps=30, seed=5)
        b = C.block_bootstrap_breakeven(R2, rf, costs, reps=30, seed=5)
        np.testing.assert_array_equal(a, b)


class BacktestTests(unittest.TestCase):
    def test_rejects_data_after_cutoff(self):
        df = data.synthetic(n=3900, seed=0, start="2012-01-02")
        self.assertGreater(df.index[-1], pd.Timestamp(C.CUTOFF))
        with self.assertRaises(ValueError):
            C.Inst(df, "AAA", FAKE, fake_cash(df), None)

    def test_strategy_list_is_the_seven_formal(self):
        from src.scan import STRATS
        self.assertEqual(sorted(C.ORDER), sorted(STRATS))
        self.assertEqual(len(C.ORDER), 7)
        self.assertEqual(C.COSTS_BP, [0, 8, 15, 30])
        self.assertEqual(C.WIN_START, "2013-01-01")

    def test_window_start_is_after_warmup_and_win_start(self):
        df = fake_df(start="2012-01-02")
        it = C.Inst(df, "AAA", FAKE, fake_cash(df), None)
        self.assertGreaterEqual(it.start, pd.Timestamp(C.WIN_START))
        self.assertGreaterEqual(it.start, df.index[C.WARM])
        r, t = it.run("blend2", 8)
        self.assertEqual(r.index[0], it.start)
        self.assertLessEqual(r.index[-1], pd.Timestamp(C.CUTOFF))
        # 数据 2015 年才开始 → 起点由预热决定，而不是 2013-01-01
        late = fake_df(start="2015-03-02", n=3000)
        it2 = C.Inst(late, "AAA", FAKE, fake_cash(late), None)
        self.assertEqual(it2.start, late.index[C.WARM])

    def test_cost_monotone_and_turnover_cost_independent(self):
        """成本越高年化越低（不增）；换手和仓位路径与成本无关；买入持有在窗口内基本不付成本。"""
        df = fake_df()
        it = C.Inst(df, "AAA", FAKE, fake_cash(df), None)
        for strat in C.ORDER:
            anns, turns = [], []
            for c in C.COSTS_BP:
                r, t = it.run(strat, c)
                anns.append(C.cagr(r.to_numpy()))
                turns.append(t.to_numpy())
            self.assertTrue(all(a >= b - 1e-12 for a, b in zip(anns, anns[1:])), strat)
            for t in turns[1:]:
                np.testing.assert_array_equal(turns[0], t)
            self.assertGreater(turns[0].sum(), 0, strat)
        r0, _ = it.run(C.BH, 0)
        r30, t30 = it.run(C.BH, 30)
        self.assertAlmostEqual(t30.sum(), 0.0)                     # 买入持有的建仓在预热期里完成
        pd.testing.assert_series_equal(r0, r30)

    def test_matches_registry_run_at_same_cost(self):
        """同样的成本、同样的起点，本脚本的年化收益（CAGR）必须与 registry.run（网页回测用的那条路径）一致到 9 位小数。"""
        df = fake_df()
        it = C.Inst(df, "AAA", FAKE, None, None)          # registry.run 在 prices 里没有现金品种时不计现金利息，这里同样不给
        prices = {"AAA": df}
        for strat in ("blend2", "rsi2", "ma_adx_plus"):
            for c in (0, 8, 30):
                res = registry.run(strat, prices, "AAA", registry.clean_params(strat, {}), FAKE, cost_bp=c, start=C.WIN_START)
                r, _ = it.run(strat, c)
                self.assertEqual(str(r.index[0].date()), res["start"])
                self.assertAlmostEqual(C.cagr(r.to_numpy()) * 100, res["stats"]["CAGR%"], places=9)

    def test_no_future_leak_into_past_returns(self):
        """把 2025-12-31 之后的行情砍掉或把价格乘 3，2025-12-31 及以前的日收益逐位不变。"""
        df = fake_df()
        cut = pd.Timestamp("2025-12-31")
        base = C.Inst(df, "AAA", FAKE, fake_cash(df), None)
        short = df.loc[:cut]
        got = C.Inst(short, "AAA", FAKE, fake_cash(short), None)
        bad = df.copy()
        after = bad.index > cut
        self.assertGreater(after.sum(), 30)
        bad.loc[after, ["Open", "High", "Low", "Close"]] *= 3.0
        mess = C.Inst(bad, "AAA", FAKE, fake_cash(bad), None)
        for strat in [*C.ORDER, C.BH]:
            a = base.run(strat, 15)[0].loc[:cut]
            pd.testing.assert_series_equal(a, got.run(strat, 15)[0].loc[:cut], check_names=False, obj=strat)
            pd.testing.assert_series_equal(a, mess.run(strat, 15)[0].loc[:cut], check_names=False, obj=strat)

    def test_group_table_shapes_and_consistency(self):
        df1, df2 = fake_df(seed=1), fake_df(seed=2)
        cash = fake_cash(df1)
        insts = [C.Inst(df1, "AAA", FAKE, cash, None), C.Inst(df2, "BBB", FAKE, cash, None)]
        rows, be = C.group_table("g", insts, cash, boot=20, seed=3)
        self.assertEqual(len(rows), 7 * 4)
        self.assertEqual(len(be), 7 * 2)
        m = pd.DataFrame(rows)
        b = pd.DataFrame(be)
        self.assertEqual(set(m.cost_bp), set(C.COSTS_BP))
        self.assertLessEqual(m.end.max(), C.CUTOFF)
        # 净超额(对现金) = 年化收益 − 现金年化
        np.testing.assert_allclose(m.excess_cash_pct, m.ann_ret_pct - m.cash_ann_pct)
        # breakeven 表里 0bp 的净超额列与指标表一致
        s = "rsi2"
        self.assertAlmostEqual(b[(b.strategy == s) & (b.bench == "cash")].excess_0bp_pct.iloc[0],
                               m[(m.strategy == s) & (m.cost_bp == 0)].excess_cash_pct.iloc[0])
        # 找到的盈亏平衡点，用真实回测在该成本重算，净超额应接近 0（插值残差）
        c = b[(b.bench == "cash") & (b.kind == "interp")]
        for _, r in c.iterrows():
            self.assertLess(abs(r.check_excess_pct), 0.05, (r.strategy, r.breakeven_bp))
        # 自助法区间包含（或贴近）点估计所在的量级：下界 ≤ 上界
        self.assertTrue((b[b.bench == "cash"].ci_lo <= b[b.bench == "cash"].ci_hi).all())
        # 成本拖累 ≈ cost × 换手 × (1+R)：比值与 1+R 同量级
        for _, r in b[b.bench == "cash"].iterrows():
            r0 = m[(m.strategy == r.strategy) & (m.cost_bp == 0)].ann_ret_pct.iloc[0] / 100
            self.assertAlmostEqual(r.drag_ratio, 1 + r0, delta=0.05, msg=r.strategy)

    def test_turnover_relation_keys(self):
        be = pd.DataFrame({"group": ["a"] * 5, "bench": ["cash"] * 5, "breakeven_bp": [10, 20, 30, 40, 50.0],
                           "turnover_at_0": [5, 4, 3, 2, 1.0], "approx_bp": [10, 20, 30, 40, 50.0]})
        t = C.turnover_relation(be)
        self.assertAlmostEqual(t["pooled_spearman"], -1.0)
        self.assertAlmostEqual(t["within_group_mean_spearman"], -1.0)
        self.assertAlmostEqual(t["approx_ratio_median"], 1.0)

    def test_end_to_end(self):
        """用合成行情冒充两个市场的 universe 和一小批个股，跑完整脚本，检查输出文件、行数、边界，且不写数据目录。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "data"
            for i, mk in enumerate(("us", "cn")):
                mc = MARKETS[mk]
                d = root / "daily" / mk
                d.mkdir(parents=True, exist_ok=True)
                for j, s in enumerate(mc["universe"]):
                    data.synthetic(n=3900, seed=10 * i + j, start="2012-01-02").to_csv(d / f"{s}.csv")   # 故意超过截止日
            codes = ["000001", "600000", "300001"]
            stock_dir = root / "daily" / "cn_all"
            stock_dir.mkdir(parents=True, exist_ok=True)
            pd.DataFrame({"code": codes, "name": ["平安", "浦发", "特锐"]}).to_csv(root / "daily" / "cn_all_list.csv", index=False)
            for k, code in enumerate(codes):
                data.synthetic(n=4300, seed=50 + k, start="2010-01-04").to_parquet(stock_dir / f"{code}.parquet")
            before = sorted(str(p.relative_to(root)) for p in root.rglob("*"))
            out = Path(tmp) / "out"
            C.main(["--out-dir", str(out), "--data-dir", str(root), "--n-stocks", "3", "--boot", "10"])
            self.assertEqual(before, sorted(str(p.relative_to(root)) for p in root.rglob("*")))        # 没写数据目录
            for f in ("cost_metrics.csv", "cost_breakeven.csv", "cost_meta.json", "cost_tables.md", "cost_stock_sample.csv",
                      "fig_excess_vs_cost.png", "fig_breakeven_vs_turnover.png"):
                self.assertTrue((out / f).exists(), f)
            m = pd.read_csv(out / "cost_metrics.csv", dtype={"group": str})
            b = pd.read_csv(out / "cost_breakeven.csv", dtype={"group": str})
            self.assertEqual(set(m["group"]), set(C.GROUPS))
            self.assertEqual(len(m), 5 * 7 * 4)
            self.assertEqual(len(b), 5 * 7 * 2)
            self.assertEqual(set(m["strategy"]), set(C.ORDER))
            self.assertLessEqual(m["end"].max(), C.CUTOFF)
            self.assertGreaterEqual(m["start"].min(), C.WIN_START)
            self.assertEqual(set(b["bench"]), {"cash", "buy_hold"})
            self.assertTrue(set(b["kind"]) <= {"interp", "extrap", "loss_at_zero", "flat", "nodata"})
            md = (out / "cost_tables.md").read_text(encoding="utf-8")
            self.assertIn("盈亏平衡", md)
            self.assertIn("cn_stock", md)


if __name__ == "__main__":
    unittest.main(verbosity=2)
