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
        for j in (0, 1):                                                          # 停牌日在本地数据里没有行：开盘价缺失
            self.V[1, j] = 0.0
            self.O[1, j] = np.nan
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


class EngineFixTests(unittest.TestCase):
    """Codex T028 审核指出的问题的回归测试。"""
    def setUp(self):
        self.dates, self.O, self.C, self.V = arrays()
        T, N = self.O.shape
        self.ok = np.ones((T, N), dtype=bool)
        self.score = np.tile(np.arange(N, 0, -1, dtype=float), (T, 1))

    def test_unsellable_but_eligible_held_keeps_weight(self):
        # 0 号仍有资格，但第 2 期 t+1 开盘跌停(前收 12 → 开盘 10.8)卖不出：权重固定在漂移后的 w0，不能被重新等权
        self.C[:6, 0] = 12.0
        self.O[6:11, 0] = 10.8
        self.C[6:11, 0] = 10.8
        self.O[11:, 0] = 20.0                                                      # 第 2 期退出日 0 号大涨
        self.C[11:, 0] = 20.0
        r = run(self.score, self.ok, self.O, self.C, self.V, self.dates, K=2, ndrop=0)
        w0 = 1.08 / 2.08                                                           # 第 1 期末 0 号的漂移权重（10 → 10.8）
        self.assertAlmostEqual(r["gross"].iloc[1], w0 * (20.0 / 10.8 - 1.0), places=6)
        self.assertGreater(abs(w0 - 0.5), 0.01)

    def test_unbuyable_kept_name_is_not_topped_up(self):
        # 0 号第 1 期跌到 8.8 后，第 2 期 t+1 开盘涨停(前收 8 → 8.8)：买不进，不能加仓到 0.5，多出的部分留作现金
        self.O[1:6, 0] = 10.0
        self.C[5, 0] = 8.0                                                         # 第 2 期决策日收盘 8
        self.O[6:11, 0] = 8.8
        self.C[6:11, 0] = 8.8
        self.O[11:, 0] = 17.6
        self.C[11:, 0] = 17.6
        r = run(self.score, self.ok, self.O, self.C, self.V, self.dates, K=2, ndrop=0)
        w0 = 0.44 / 0.94                                                           # 第 1 期 0 号 10 → 8.8，权重漂移到 w0
        self.assertAlmostEqual(r["gross"].iloc[1], w0 * (17.6 / 8.8 - 1.0), places=6)

    def test_suspended_held_not_rebalanced(self):
        self.O[6, 0] = np.nan
        self.V[6, 0] = 0.0
        self.C[6:, 0] = np.nan
        r = run(self.score, self.ok, self.O, self.C, self.V, self.dates, K=2, ndrop=0)
        self.assertEqual(r["cost"].iloc[1], 0.0)                                   # 停牌的旧仓不动，另一只也没有因此被动调整

    def test_suspended_valuation_uses_last_known_close(self):
        # 0 号在第 1 期退出日(t=6)停牌，且停牌日出现离谱收盘价 99：入场/退出估值只能用决策日以前的最后收盘价 12
        self.C[:6, 0] = 12.0
        self.O[6, 0] = np.nan
        self.V[6, 0] = 0.0
        self.C[6, 0] = 99.0
        self.O[7:, 0] = 12.0
        self.C[7:, 0] = 12.0
        r = run(self.score, self.ok, self.O, self.C, self.V, self.dates, K=1, ndrop=0)
        self.assertAlmostEqual(r["gross"].iloc[0], 0.2, places=6)                  # 退出估值 12，不是 99
        self.assertAlmostEqual(r["gross"].iloc[1], 0.0, places=6)                  # 第 2 期入场估值 12，不是 99

    def test_next_day_volume_not_used(self):
        # 次日成交量不能影响能否成交：把 t+1 的成交量改成 0 但开盘价正常，结果不变
        self.O[6:, 0] = 12.0                                                       # 0 号第 1 期涨 20%，成交量被改后若误判不可买，结果会不同
        self.C[6:, 0] = 12.0
        base = run(self.score, self.ok, self.O, self.C, self.V, self.dates, K=2)
        V2 = self.V.copy()
        V2[1, 0] = 0.0
        V2[6, 1] = 0.0
        alt = run(self.score, self.ok, self.O, self.C, V2, self.dates, K=2)
        self.assertEqual(list(base["gross"]), list(alt["gross"]))

    def test_liquidation_cost_enters_excess(self):
        # 清仓期没有任何可买股票：基准按空仓 0 计，清仓成本必须出现在超额统计里(不是 NaN)
        ok = self.ok.copy()
        ok[5:, :] = False
        r = run(self.score, ok, self.O, self.C, self.V, self.dates, K=2, ndrop=0)
        self.assertTrue(np.isfinite(r["excess"].iloc[1]))
        self.assertAlmostEqual(r["excess"].iloc[1], -r["cost"].iloc[1])

    def test_liquidation_period_not_skipped(self):
        # 第 2 期起没有任何股票有资格：必须清仓并付成本，且后面不再带着旧仓位
        ok = self.ok.copy()
        ok[5:, :] = False
        r = run(self.score, ok, self.O, self.C, self.V, self.dates, K=2, ndrop=0)
        self.assertGreater(r["cost"].iloc[1], 0)                                   # 清仓的卖出成本
        self.assertEqual(r["n_hold"].iloc[1], 0)
        self.assertEqual(r["gross"].iloc[1], 0.0)

    def test_next_day_close_not_used_for_entry_fallback(self):
        # 0 号 t+1 停牌(无成交量)：入场估值用决策日收盘，不用 t+1 当天的收盘价；停牌股本来就买不进，改看已持有的
        r0 = run(self.score, self.ok, self.O, self.C, self.V, self.dates, K=2, ndrop=0)
        O2, C2, V2 = self.O.copy(), self.C.copy(), self.V.copy()
        V2[6, 0] = 0.0
        O2[6, 0] = np.nan
        C2[6, 0] = 99.0                                                            # 停牌日出现一个离谱收盘价，不应被使用
        r1 = run(self.score, self.ok, O2, C2, V2, self.dates, K=2, ndrop=0)
        self.assertTrue(np.isfinite(r1["gross"]).all())
        self.assertLess(abs(r1["gross"].iloc[1]), 0.5)


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

    def test_identical_candidates_not_zero_pbo(self):
        col = np.random.default_rng(3).standard_normal((96, 1))
        M = np.tile(col, (1, 6))                                                   # 6 个完全相同的候选：区分不了
        p, _ = sd.cscv_pbo(M)
        self.assertEqual(p, 1.0)

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


class FullPanelTests(unittest.TestCase):
    def test_restrict_columns_and_lazy_full_panel(self):
        from src.market_monitor import factors as fx
        dates = pd.bdate_range("2022-06-01", "2024-06-30")
        df = pd.DataFrame(10.0, index=dates, columns=["600000", "600001", "600002"])
        names = pd.Series(["a", "b", "c"], index=df.columns)
        panel = fx.Panel(df, df, df, df, df, names, pd.Series("x", index=df.columns))
        r = sd.restrict_columns(panel, ["600000", "600002"])
        self.assertEqual(list(r.close.columns), ["600000", "600002"])
        self.assertEqual(list(r.names.index), ["600000", "600002"])


class FinalUniverseTests(unittest.TestCase):
    def test_final_run_restricted_to_selection_universe(self):
        from src.market_monitor import factors as fx
        dates = pd.bdate_range("2014-06-02", "2025-06-30")
        rng = np.random.default_rng(0)
        N = 45
        cols = [f"{600000 + i}" for i in range(N)]
        close = pd.DataFrame(10 * np.exp(np.cumsum(rng.normal(0.0002, 0.015, (len(dates), N)), axis=0)), index=dates, columns=cols)
        vol = pd.DataFrame(rng.uniform(5e6, 2e7, close.shape), index=dates, columns=cols)
        panel = fx.Panel(close.shift(1).fillna(close.iloc[0]), close * 1.01, close * 0.99, close, vol,
                         pd.Series([f"股票{i}" for i in range(N)], index=cols), pd.Series("x", index=cols))
        extra = panel.close["600000"].rename("688999")
        big = fx.Panel(*(pd.concat([getattr(panel, f), extra], axis=1) for f in ("open", "high", "low", "close", "volume")),
                       pd.concat([panel.names, pd.Series(["新股"], index=["688999"])]), pd.concat([panel.industry, pd.Series(["x"], index=["688999"])]))
        shapes, real, real_sel = [], sd.run_portfolio, sd.select_and_test

        def spy(score, ok, O, *a, **k):
            shapes.append(O.shape[1])
            return real(score, ok, O, *a, **k)

        def force(cands, provider=None):                                           # 强制走最终段路径
            provider([next(iter(cands))])
            return real_sel(cands, lambda ids: {})
        sd.run_portfolio, sd.select_and_test = spy, force
        try:
            with tempfile.TemporaryDirectory() as d:
                sd.run_all(panel, fx, 8, smoke=True, n_random=1, lock_path=Path(d) / "L.json", full_panel_fn=lambda end=None: big)
        finally:
            sd.run_portfolio, sd.select_and_test = real, real_sel
        self.assertIn(N, shapes)
        self.assertNotIn(N + 1, shapes)                                            # 最终段没有把选择面板之外的新股票带进池子

        # 完整面板的历史与选择面板不一致：run_all 在最终段路径上必须拒绝
        bad = fx.Panel(*(getattr(panel, f).copy() for f in ("open", "high", "low", "close", "volume")), panel.names, panel.industry)
        bad.close.iloc[100, 3] *= 1.5
        bad_hl = fx.Panel(*(getattr(panel, f).copy() for f in ("open", "high", "low", "close", "volume")), panel.names, panel.industry)
        bad_hl.high.iloc[100, 3] *= 1.5                                           # 只改 high：也必须被发现
        sd.select_and_test = force
        try:
            for badp, nm in ((bad, "L2"), (bad_hl, "L3")):
                with tempfile.TemporaryDirectory() as d:
                    lock = Path(d) / f"{nm}.json"
                    with self.assertRaises(ValueError):
                        sd.run_all(panel, fx, 8, smoke=True, n_random=1, lock_path=lock, full_panel_fn=lambda end=None, b=badp: b)
                    self.assertFalse(lock.exists())                                # 预检失败：全局锁没有被消耗
        finally:
            sd.select_and_test = real_sel


class FinalCalendarTests(unittest.TestCase):
    def test_excluded_stock_dates_do_not_shift_calendar(self):
        from src.market_monitor import factors as fx
        dates = pd.bdate_range("2022-01-03", "2024-06-28")
        a = pd.DataFrame({"A": np.linspace(10, 12, len(dates))}, index=dates).iloc[1:]       # A 从第 2 个日期开始有行情
        b = pd.DataFrame({"B": np.linspace(10, 12, len(dates))}, index=dates)                # B 多出第 1 个日期
        wide = pd.concat([a, b], axis=1)
        names = pd.Series(["a", "b"], index=["A", "B"])
        full = fx.Panel(wide, wide, wide, wide, wide, names, pd.Series("x", index=["A", "B"]))
        r = sd.restrict_columns(full, ["A"])
        self.assertEqual(list(r.close.columns), ["A"])
        self.assertEqual(r.close.index[0], a.index[0])                              # B 独有的日期被去掉，调仓日历不变

    def test_check_same_history(self):
        from src.market_monitor import factors as fx
        dates = pd.bdate_range("2022-01-03", "2024-06-28")
        df = pd.DataFrame({"A": np.linspace(10, 12, len(dates))}, index=dates)
        mk = lambda d: fx.Panel(d, d, d, d, d, pd.Series(["a"], index=["A"]), pd.Series("x", index=["A"]))
        sel = sd.truncate_panel(mk(df), sd.VALID[1])
        sd.check_same_history(sel, mk(df), sd.VALID[1])                              # 一致：不报错
        bad = df.copy()
        bad.iloc[10, 0] = 99.0
        with self.assertRaises(ValueError):
            sd.check_same_history(sel, mk(bad), sd.VALID[1])                         # 历史数据被改动：拒绝
        with self.assertRaises(ValueError):
            sd.check_same_history(sel, mk(df.iloc[1:]), sd.VALID[1])                 # 日期不一致：拒绝


class FinalLockTests(unittest.TestCase):
    def test_lock_is_exclusive(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "sub" / "LOCK.json"
            sd.acquire_final_lock(p, ["a"])
            with self.assertRaises(FileExistsError):
                sd.acquire_final_lock(p, ["b"])

    def test_blocked_when_lock_exists(self):
        good = {"train": seg_df(0.01, seed=1), "valid": seg_df(0.01, start="2022-01-01", seed=2), "final": seg_df(0.01, start="2024-01-01", seed=3)}
        rep = sd.select_and_test({"g": good}, lambda ids: None)
        self.assertTrue(rep["final_blocked"])
        self.assertIsNone(rep["top"][0]["final"])
        self.assertIn("拒绝再看", sd.render({**rep, "pbo": 0, "dsr_best": 0, "n_candidates": 1}, 1, 8, True))

    def test_provider_not_called_when_nothing_kept(self):
        bad = {"train": seg_df(0.012, seed=4), "valid": seg_df(-0.01, start="2022-01-01", seed=5), "final": seg_df(0.5, start="2024-01-01", seed=6)}
        calls = []
        sd.select_and_test({"b": bad}, lambda ids: calls.append(ids) or {})
        self.assertEqual(calls, [])                                                # 没有候选留下：最终段根本不取

    def test_truncate_panel_drops_future(self):
        from src.market_monitor import factors as fx
        dates = pd.bdate_range("2022-06-01", "2024-06-30")
        df = pd.DataFrame(10.0, index=dates, columns=["600000", "600001"])
        panel = fx.Panel(df, df, df, df, df, pd.Series(["a", "b"], index=df.columns), pd.Series("x", index=df.columns))
        t = sd.truncate_panel(panel, sd.VALID[1])
        self.assertLessEqual(t.close.index.max(), pd.Timestamp("2023-12-31"))
        self.assertLessEqual(t.volume.index.max(), pd.Timestamp("2023-12-31"))


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
        with tempfile.TemporaryDirectory() as d:
            lock = Path(d) / "L.json"
            seen = []
            rep, cands = sd.run_all(panel, fx, 8, smoke=True, n_random=3, lock_path=lock,
                                    full_panel_fn=lambda end=None: seen.append((end is None, lock.exists())) or panel)
            kept = [r for r in rep["top"] if r["kept"]]
            self.assertEqual(seen, [(False, False), (True, True)] if kept else [])      # 先锁前预检(只到验证段末尾、无锁)，再锁后取完整面板
            if kept:
                self.assertTrue(lock.exists())
                rep2, _ = sd.run_all(panel, fx, 8, smoke=True, n_random=3, lock_path=lock)
                self.assertTrue(rep2["final_blocked"])                             # 换同一个锁路径再跑：最终段被拒
                self.assertTrue(all(r["final"] is None for r in rep2["top"]))
            else:
                self.assertFalse(lock.exists())                                    # 没有候选留下：锁不被消耗
        self.assertEqual(rep["n_candidates"], 6)
        self.assertEqual(len(cands), 6)
        for v in cands.values():
            self.assertEqual(len(v["final"]), 0)                                   # 选择阶段的面板里没有最终段数据
        for k in ("pbo", "dsr_best", "random_percentile_best", "top"):
            self.assertIn(k, rep)
        txt = sd.render(rep, N, 8, True)
        self.assertIn("探索性", txt)
        self.assertIn("smoke", txt)


class RandomControlPoolTests(unittest.TestCase):
    def test_random_runs_use_candidate_valid_pool(self):
        from src.market_monitor import factors as fx
        dates = pd.bdate_range("2014-06-02", "2025-06-30")
        rng = np.random.default_rng(5)
        N = 45
        cols = [f"{600000 + i}" for i in range(N)]
        close = pd.DataFrame(10 * np.exp(np.cumsum(rng.normal(0.0002, 0.015, (len(dates), N)), axis=0)), index=dates, columns=cols)
        vol = pd.DataFrame(rng.uniform(5e6, 2e7, close.shape), index=dates, columns=cols)
        panel = fx.Panel(close.shift(1).fillna(close.iloc[0]), close * 1.01, close * 0.99, close, vol,
                         pd.Series([f"股票{i}" for i in range(N)], index=cols), pd.Series("x", index=cols))
        seen, real, real_cf = [], sd.run_portfolio, fx.compute_factor

        def holey(fid, pnl):                                                       # 0 号股票在 2020 年后因子缺失，但仍有资格：有效池 = 资格 & 因子有限
            out = real_cf(fid, pnl).copy()
            out.iloc[1500:, 0] = np.nan
            return out

        def spy(score, ok, *a, **k):
            seen.append((ok.copy(), score.copy()))
            return real(score, ok, *a, **k)
        sd.run_portfolio, fx.compute_factor = spy, holey
        try:
            with tempfile.TemporaryDirectory() as d:
                sd.run_all(panel, fx, 8, smoke=True, n_random=3, lock_path=Path(d) / "L.json")
        finally:
            sd.run_portfolio, fx.compute_factor = real, real_cf
        random_calls = [(ok, sc) for ok, sc in seen if np.isfinite(sc).all()]      # 随机打分的分数处处有限
        real_calls = [(ok, sc) for ok, sc in seen if not np.isfinite(sc).all()]
        self.assertGreaterEqual(len(random_calls), 3)
        want = [ok & np.isfinite(sc) for ok, sc in real_calls]
        for ok_r, _ in random_calls[:3]:
            self.assertTrue(any(np.array_equal(ok_r, w) for w in want))             # 随机对照用的就是某个真实候选的有效池
            self.assertTrue(not ok_r[1500:, 0].any())                                # 因子缺失的 0 号股票没有被随机对照放进池子


class BenchmarkAndNoFinalTests(unittest.TestCase):
    """Codex 夜间审计(N1027_3)：基准不能随因子得分缺失而变；allow_final=False 不拿锁、不载入完整面板。"""

    def test_benchmark_independent_of_score_nan(self):
        dates, O, C, V = arrays()
        T, N = O.shape
        rng = np.random.default_rng(0)
        O = O * (1 + rng.normal(0, 0.01, (T, N)).cumsum(axis=0) * 0.1)
        C = O.copy()
        ok = np.ones((T, N), bool)
        score = np.tile(np.arange(N, 0, -1, dtype=float), (T, 1))
        a = run(score, ok, O, C, V, dates)
        s2 = score.copy()
        s2[:, N - 1] = np.nan                                                      # 一只没持有的低分股票得分缺失
        b = run(s2, ok, O, C, V, dates)
        self.assertTrue(np.allclose(a["gross"], b["gross"]))                       # 持仓不变
        self.assertTrue(np.allclose(a["bench"], b["bench"]))                       # 基准也不变

    def test_allow_final_false_never_touches_lock_or_full_panel(self):
        from src.market_monitor import factors as fx
        dates = pd.bdate_range("2016-01-04", "2024-06-28")
        T, N = len(dates), 30
        rng = np.random.default_rng(1)
        px = pd.DataFrame(10 * np.cumprod(1 + rng.normal(0.0004, 0.01, (T, N)), axis=0), index=dates, columns=[f"{i:06d}" for i in range(N)])
        panel = fx.Panel(px, px * 1.01, px * 0.99, px, pd.DataFrame(1e6, index=dates, columns=px.columns),
                         pd.Series(["a"] * N, index=px.columns), pd.Series(["x"] * N, index=px.columns))
        calls = []
        with tempfile.TemporaryDirectory() as d:
            lock = Path(d) / "L.json"
            rep, _ = sd.run_all(panel, fx, 8, smoke=True, n_random=1, lock_path=lock, allow_final=False,
                                full_panel_fn=lambda end=None: calls.append(end) or panel)
            self.assertEqual(calls, [])                                            # 完整面板回调一次都没被调用
            self.assertFalse(lock.exists())                                        # 没有拿锁
        self.assertTrue(rep.get("final_skipped"))
        self.assertFalse(rep["final_blocked"])


class ImportTests(unittest.TestCase):
    def test_import_guard(self):
        self.assertNotIn("app", sys.modules)
        self.assertEqual((sd.TOP_N, sd.T_MIN, sd.T_HLZ), (5, 2.0, 3.0))


if __name__ == "__main__":
    unittest.main()
