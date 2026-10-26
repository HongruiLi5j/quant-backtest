"""strategy_dev 测试：只用合成数据，不读 data/，不导入 app。"""
import math
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import lab_import_guard as guard  # noqa: E402

(sd,) = guard.import_guarded("strategy_dev")


def arrays(T=60, N=6, o=10.0, seed=0):
    dates = pd.bdate_range("2020-01-01", periods=T)
    O = np.full((T, N), o, dtype=float)
    C = O.copy()
    V = np.full((T, N), 1e6)
    return dates, O, C, V


def run(score, ok, O, C, V, dates, step=5, K=2, ndrop=1, cost=10):
    Cff = pd.DataFrame(C).ffill().values
    return sd.run_portfolio(score, ok, O, C, V, Cff, dates, step, K, ndrop, cost)


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.dates, self.O, self.C, self.V = arrays()
        T, N = self.O.shape
        self.ok = np.ones((T, N), dtype=bool)
        self.score = np.tile(np.arange(N, 0, -1, dtype=float), (T, 1))          # 0 号最高分

    def test_first_period_cost_is_one_side(self):
        r = run(self.score, self.ok, self.O, self.C, self.V, self.dates)
        self.assertAlmostEqual(r["cost"].iloc[0], 10 / 1e4)                       # 全仓买入一次，单边 10bp
        self.assertTrue((r["cost"].iloc[1:] == 0).all())                          # 价格不变、持仓不变：不再付成本

    def test_top_scores_held(self):
        self.O[:, 0] *= np.linspace(1, 1.5, len(self.dates))                       # 0 号一路涨
        self.C[:, 0] = self.O[:, 0]
        r = run(self.score, self.ok, self.O, self.C, self.V, self.dates)
        self.assertGreater(r["gross"].iloc[0], 0)
        self.assertEqual(r["n_hold"].iloc[0], 2)

    def test_limit_up_candidate_skipped(self):
        # 第 1 期：0 号在 t+1 开盘涨停(+10%)，买不进，顺延买 2 号
        self.C[:, :] = 10.0
        self.O[1, 0] = 11.0
        r = run(self.score, self.ok, self.O, self.C, self.V, self.dates, K=2)
        self.assertEqual(r["n_hold"].iloc[0], 2)
        # 0 号没买到：第一期组合 = 1 号 + 2 号，而不是 0 号 + 1 号；用价格变化验证
        self.O[6:, 2] = 12.0                                                      # 2 号在第一期退出日(t'=5 → 6)涨 20%
        self.C[6:, 2] = 12.0
        r2 = run(self.score, self.ok, self.O, self.C, self.V, self.dates, K=2)
        self.assertAlmostEqual(r2["gross"].iloc[0], 0.5 * 0.2, places=6)

    def test_zero_volume_not_buyable(self):
        self.V[1, 0] = 0.0
        self.V[1, 1] = 0.0
        r = run(self.score, self.ok, self.O, self.C, self.V, self.dates, K=2)
        self.assertEqual(r["n_hold"].iloc[0], 2)                                  # 顺延到 2、3 号

    def test_unsellable_held_stays(self):
        # 持有 0、1；第 2 期 0 号失去资格且开盘跌停(-10%)：卖不出，继续持有
        ok = self.ok.copy()
        ok[5:, 0] = False
        self.O[6, 0] = 9.0
        r = run(self.score, ok, self.O, self.C, self.V, self.dates, K=2, ndrop=0)
        self.assertEqual(r["n_hold"].iloc[1], 2)
        self.assertEqual(r["cost"].iloc[1], 0.0)                                  # 没有任何成交

    def test_suspended_exit_uses_last_close(self):
        # 0 号在第一期退出日停牌(无开盘价、无成交量)：用最后一个收盘价结算
        self.C[:6, 0] = 10.0
        self.C[5, 0] = 12.0
        self.O[6, 0] = np.nan
        self.V[6, 0] = 0.0
        self.C[6:, 0] = np.nan
        r = run(self.score, self.ok, self.O, self.C, self.V, self.dates, K=1, ndrop=0)
        self.assertAlmostEqual(r["gross"].iloc[0], 0.2, places=6)

    def test_dropout_limits_turnover(self):
        T, N = self.O.shape
        rng = np.random.default_rng(1)
        sc = rng.standard_normal((T, N))
        r = run(sc, self.ok, self.O, self.C, self.V, self.dates, step=5, K=4, ndrop=1)
        # 每期最多换 1 只：单边换手 ≤ 1/4 左右（价格不变，换 1 只 = 买 0.25 + 卖 0.25 → turnover 0.25）
        self.assertTrue((r["turnover"].iloc[1:] <= 0.25 + 1e-9).all())

    def test_cost_is_buys_plus_sells(self):
        rng = np.random.default_rng(1)
        sc = rng.standard_normal(self.O.shape)
        r = run(sc, self.ok, self.O, self.C, self.V, self.dates, step=5, K=4, ndrop=1, cost=10)
        np.testing.assert_allclose(r["cost"], 2 * r["turnover"] * 10 / 1e4)       # 买入+卖出两侧都计
        self.assertGreater(r["cost"].iloc[1:].max(), 0)

    def test_ineligible_sellable_is_forced_out(self):
        ok = self.ok.copy()
        ok[5:, 0] = False                                                          # 第 2 期起 0 号失去资格，且卖得出
        r = run(self.score, ok, self.O, self.C, self.V, self.dates, K=2, ndrop=0)
        self.assertGreater(r["cost"].iloc[1], 0)                                   # 必须卖出并换成别的
        self.assertEqual(r["n_hold"].iloc[1], 2)

    def test_dropout_drops_lowest_score_held(self):
        T, N = self.O.shape
        sc = np.tile(np.array([5.0, 0.0, 2.0, 4.0, 1.0, 1.0]), (T, 1))
        sc[0] = [5, 4, 0, 0, 0, 0]                                                 # 第 1 期买 0、1
        self.O[11:, 3] = 15.0                                                      # 3 号在第 2 期退出日 +50%
        self.C[11:, 3] = 15.0
        r = run(sc, self.ok, self.O, self.C, self.V, self.dates, step=5, K=2, ndrop=1)
        # 第 2 期：持仓 {0,1}，1 号得分最低被换出，买得分最高的非持仓 3 号 → 0.5×0 + 0.5×0.5
        self.assertAlmostEqual(r["gross"].iloc[1], 0.25, places=6)

    def test_only_rebalance_rows_of_score_matter(self):
        rng = np.random.default_rng(2)
        sc = rng.standard_normal(self.O.shape)
        O = 10 * np.exp(np.cumsum(rng.normal(0, 0.02, self.O.shape), axis=0))
        C = O.copy()
        base = run(sc, self.ok, O, C, self.V, self.dates, step=5, K=3, ndrop=1)
        sc2 = sc.copy()
        for t in range(len(sc2)):
            if t % 5 != 0:
                sc2[t] = rng.standard_normal(sc2.shape[1]) * 100                    # 非调仓日的得分乱改
        alt = run(sc2, self.ok, O, C, self.V, self.dates, step=5, K=3, ndrop=1)
        self.assertEqual(list(base["gross"]), list(alt["gross"]))
        self.assertGreater(base["gross"].abs().max(), 0)                           # 价格有波动，结果不是恒为 0                  # 只看调仓日(t)的得分，不看 t+1

    def test_bench_excludes_unbuyable(self):
        self.O[1, 3] = 11.0                                                        # 3 号第一期 t+1 开盘涨停
        self.O[6:, 3] = 30.0                                                       # 且之后暴涨(买不进的人吃不到)
        self.C[6:, 3] = 30.0
        r = run(self.score, self.ok, self.O, self.C, self.V, self.dates)
        self.assertAlmostEqual(r["bench"].iloc[0], 0.0, places=9)                  # 基准里不含 3 号

    def test_no_lookahead_future_prices_do_not_change_earlier_rows(self):
        base = run(self.score, self.ok, self.O, self.C, self.V, self.dates)
        O2, C2 = self.O.copy(), self.C.copy()
        O2[20:] *= 3
        C2[20:] *= 3
        alt = run(self.score, self.ok, O2, C2, self.V, self.dates)
        n = 2                                                                      # 退出日 <20 的前两期不应受影响
        self.assertEqual(list(base["gross"].iloc[:n]), list(alt["gross"].iloc[:n]))

    def test_bench_is_equal_weight_of_buyable(self):
        self.O[6:, 3] = 20.0                                                       # 3 号第一期涨 100%
        self.C[6:, 3] = 20.0
        r = run(self.score, self.ok, self.O, self.C, self.V, self.dates)
        self.assertAlmostEqual(r["bench"].iloc[0], 1.0 / 6)

    def test_excess_is_net_minus_bench(self):
        self.O[6:, 0] = 11.0
        self.C[6:, 0] = 11.0
        r = run(self.score, self.ok, self.O, self.C, self.V, self.dates)
        np.testing.assert_allclose(r["excess"], r["gross"] - r["cost"] - r["bench"])

    def test_ineligible_never_bought(self):
        ok = self.ok.copy()
        ok[:, 0] = False
        r = run(self.score, ok, self.O, self.C, self.V, self.dates, K=1)
        self.O[6:, 1] = 12.0
        self.C[6:, 1] = 12.0
        r2 = run(self.score, ok, self.O, self.C, self.V, self.dates, K=1)
        self.assertAlmostEqual(r2["gross"].iloc[0], 0.2, places=6)               # 买的是 1 号，不是被排除的 0 号


class SegmentTests(unittest.TestCase):
    def test_boundaries(self):
        T = pd.Timestamp
        self.assertEqual(sd.segment_of(T("2021-06-01"), T("2021-07-01")), "train")
        self.assertIsNone(sd.segment_of(T("2021-12-20"), T("2022-01-10")))        # 跨界清除
        self.assertEqual(sd.segment_of(T("2022-02-01"), T("2022-03-01")), "valid")
        self.assertEqual(sd.segment_of(T("2024-01-02"), T("2024-02-01")), "final")
        self.assertIsNone(sd.segment_of(T("2015-12-01"), T("2016-01-05")))        # 训练段之前

    def test_monthly_sum(self):
        df = pd.DataFrame({"exit_date": pd.to_datetime(["2022-01-05", "2022-01-20", "2022-02-03"]), "excess": [0.01, 0.02, 0.03]})
        m = sd.monthly(df)
        self.assertAlmostEqual(m.iloc[0], 0.03)
        self.assertAlmostEqual(m.iloc[1], 0.03)


class PboDsrTests(unittest.TestCase):
    def test_noise_pbo_near_half(self):
        M = np.random.default_rng(0).standard_normal((96, 40))
        p, n = sd.cscv_pbo(M)
        self.assertEqual(n, 70)
        self.assertTrue(0.25 < p < 0.75, p)

    def test_dominant_candidate_low_pbo(self):
        rng = np.random.default_rng(1)
        M = rng.standard_normal((96, 20)) * 0.5
        M[:, 0] += 1.0
        p, _ = sd.cscv_pbo(M)
        self.assertLess(p, 0.1)

    def test_too_small_nan(self):
        self.assertTrue(math.isnan(sd.cscv_pbo(np.zeros((5, 1)))[0]))

    def test_dsr_more_trials_lower(self):
        a = sd.deflated_sharpe(0.3, 10, 0.01, 60, 0.0, 3.0)
        b = sd.deflated_sharpe(0.3, 1000, 0.01, 60, 0.0, 3.0)
        self.assertGreater(a, b)
        self.assertTrue(math.isnan(sd.deflated_sharpe(0.3, 1, 0.01, 60, 0.0, 3.0)))
        self.assertTrue(math.isnan(sd.deflated_sharpe(float("nan"), 10, 0.01, 60, 0.0, 3.0)))


def seg_df(mean, n=24, start="2016-03-01", gross_extra=0.0, cost=0.0005, noise=0.003, seed=0):
    rng = np.random.default_rng(seed)
    d = pd.date_range(start, periods=n, freq="MS")
    ex = mean + rng.normal(0, noise, n)
    return pd.DataFrame({"sig_date": d, "exit_date": d + pd.Timedelta(days=25), "excess": ex, "gross": ex + cost + 0.0, "cost": cost,
                         "bench": 0.0, "turnover": 0.2})


class SelectionTests(unittest.TestCase):
    def cands(self):
        good = {"train": seg_df(0.01, seed=1), "valid": seg_df(0.01, start="2022-01-01", seed=2), "final": seg_df(0.01, start="2024-01-01", seed=3)}
        bad_valid = {"train": seg_df(0.012, seed=4), "valid": seg_df(-0.01, start="2022-01-01", seed=5), "final": seg_df(0.5, start="2024-01-01", seed=6)}
        weak_train = {"train": seg_df(0.0001, seed=7, noise=0.01), "valid": seg_df(0.01, start="2022-01-01", seed=8), "final": seg_df(0.01, start="2024-01-01", seed=9)}
        return {"good": good, "bad_valid": bad_valid, "weak": weak_train}

    def test_protocol(self):
        rep = sd.select_and_test(self.cands())
        by = {r["id"]: r for r in rep["top"]}
        self.assertTrue(by["good"]["kept"])
        self.assertTrue(by["good"]["final"]["survive"])
        self.assertFalse(by["bad_valid"]["kept"])
        self.assertIsNone(by["bad_valid"]["final"])                                # 验证没过：最终段不看（哪怕最终段很漂亮）
        self.assertEqual(rep["n_candidates"], 3)

    def test_valid_needs_t(self):
        # 验证段均值>0 但很吵(t<2)：不能留下
        noisy = {"train": seg_df(0.012, seed=1), "valid": seg_df(0.002, n=24, start="2022-01-01", seed=2, noise=0.05), "final": seg_df(0.01, start="2024-01-01", seed=3)}
        v = sd.seg_stats(noisy["valid"])
        self.assertGreater(v["mean"], 0)
        self.assertLess(v["nw_t"], 2.0)
        rep = sd.select_and_test({"n": noisy})
        self.assertFalse(rep["top"][0]["kept"])

    def test_top_n_limit(self):
        many = {f"c{i}": {"train": seg_df(0.01 + i * 0.001, seed=i), "valid": seg_df(0.01, start="2022-01-01", seed=100 + i),
                          "final": seg_df(0.01, start="2024-01-01", seed=200 + i)} for i in range(9)}
        self.assertEqual(len(sd.select_and_test(many)["top"]), sd.TOP_N)

    def test_final_needs_cost_x2(self):
        c = self.cands()
        c["good"]["final"] = seg_df(0.0004, n=24, start="2024-01-01", seed=3, cost=0.01, noise=0.0001)    # 加倍成本后为负
        rep = sd.select_and_test({"good": c["good"]})
        self.assertFalse(rep["top"][0]["final"]["survive"])

    def test_hlz_flag(self):
        rep = sd.select_and_test({"good": self.cands()["good"]})
        self.assertIn("above_hlz", rep["top"][0]["final"])


class PreregTests(unittest.TestCase):
    def test_exclusive_create(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            sd.write_prereg(out, 10, 8, 100, True)
            with self.assertRaises(FileExistsError):
                sd.write_prereg(out, 10, 8, 100, True)


class IntegrationTests(unittest.TestCase):
    def test_run_all_on_synthetic_panel(self):
        from src.market_monitor import factors as fx
        dates = pd.bdate_range("2014-06-02", "2025-06-30")
        rng = np.random.default_rng(0)
        N = 45
        cols = [f"{600000 + i}" for i in range(N)]
        close = pd.DataFrame(10 * np.exp(np.cumsum(rng.normal(0.0002, 0.015, (len(dates), N)), axis=0)), index=dates, columns=cols)
        open_ = close.shift(1).fillna(close.iloc[0]) * (1 + rng.normal(0, 0.002, close.shape))
        vol = pd.DataFrame(rng.uniform(5e6, 2e7, close.shape), index=dates, columns=cols)
        names = pd.Series([f"股票{i}" for i in range(N)], index=cols)
        panel = fx.Panel(open_, close * 1.01, close * 0.99, close, vol, names, pd.Series("x", index=cols))
        rep, cands = sd.run_all(panel, fx, 8, smoke=True, n_random=3)
        self.assertEqual(rep["n_candidates"], 6)
        self.assertEqual(len(cands), 6)
        for k in ("pbo", "dsr_best", "random_percentile_best", "top"):
            self.assertIn(k, rep)
        txt = sd.render(rep, N, 8, True)
        self.assertIn("探索性", txt)
        self.assertIn("smoke", txt)


class ImportTests(unittest.TestCase):
    def test_import_guard(self):
        self.assertNotIn("app", sys.modules)
        self.assertEqual((sd.TOP_N, sd.T_MIN, sd.T_HLZ), (5, 2.0, 3.0))


if __name__ == "__main__":
    unittest.main()
