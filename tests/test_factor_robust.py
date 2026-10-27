"""因子稳健性体检(T041)的测试:纯函数 + 合成面板上的 eval_score。不读 data/,不联网,不 import app。"""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import factor_robust as R  # noqa: E402


def make_ctx(n_days=400, n_stocks=300, seed=0):
    """合成面板:开盘价随机游走;打分 sc[t, i] = 股票序号(稳定),未来收益与打分正相关。"""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2016-01-04", periods=n_days)
    drift = np.linspace(-0.002, 0.002, n_stocks)                       # 序号越大收益越高
    ret = drift + rng.normal(0, 0.01, (n_days, n_stocks))
    O = 10 * np.cumprod(1 + ret, axis=0)
    C = O.copy()
    ctx = SimpleNamespace(ok=np.ones((n_days, n_stocks), bool), O=O, C=C, dates=dates, n=n_days,
                          amt=rng.normal(size=(n_days, n_stocks)), grp=rng.integers(0, 5, n_stocks),
                          up=np.ones(n_days, bool), hivol=np.zeros(n_days, bool))
    sc = np.tile(np.arange(n_stocks, dtype=float), (n_days, 1))
    return ctx, sc


class PureTests(unittest.TestCase):
    def test_residualize_matches_explicit_regression(self):
        rng = np.random.default_rng(0)
        g = rng.integers(0, 5, 400)
        x = rng.normal(size=400) + g
        y = 3 * x + 2 * g + rng.normal(size=400) * 0.1 + rng.normal(size=400)
        r = R.residualize(y, x, g)
        X = np.column_stack([np.eye(5)[g], x])                           # 行业哑变量 + 控制变量
        beta = np.linalg.lstsq(X, y, rcond=None)[0]
        self.assertTrue(np.allclose(r, y - X @ beta, atol=1e-8))         # 与显式回归残差一致
        self.assertGreater(float(np.std(r)), 0.5)                        # 不是恒零

    def test_residualize_degenerate_x(self):
        r = R.residualize(np.arange(10.0), np.ones(10), np.zeros(10, int))
        self.assertTrue(np.isfinite(r).all())

    def test_spearman(self):
        a = np.arange(50.0)
        self.assertAlmostEqual(R.spearman(a, a ** 3), 1.0)
        self.assertAlmostEqual(R.spearman(a, -a), -1.0)
        b = a.copy(); b[3] = np.nan
        self.assertAlmostEqual(R.spearman(a, b), 1.0)
        self.assertTrue(np.isnan(R.spearman(np.ones(400), np.ones(400))))      # 常数:nan,不是 1.0
        self.assertTrue(np.isnan(R.spearman(np.ones(5), np.arange(5.0))))

    def test_bh_fixed_family(self):
        self.assertEqual(R.bh_fdr([0.001, 0.009, 0.04, 0.2, float("nan"), 0.8], 0.1), [True, True, True, False, False, False])
        self.assertEqual(R.bh_fdr([0.06, float("nan")], 0.1), [False, False])         # NaN 占一个检验名额
        self.assertEqual(R.bh_fdr([0.06], 0.1), [True])
        self.assertEqual(R.bh_fdr([0.06], 0.1, m=2), [False])

    def test_cluster(self):
        df = pd.DataFrame({"a": [1, 2, 3, 4.0], "b": [2, 4, 6, 8.0], "c": [4, 1, 3, 2.0]})
        self.assertEqual(sorted(map(sorted, R.cluster_count(df.corr()))), [["a", "b"], ["c"]])

    def test_rebalance_cost(self):
        bp = 8.0
        self.assertAlmostEqual(R.rebalance_cost(None, None, [1, 2], bp), bp / 1e4)          # 首期全部买入
        self.assertAlmostEqual(R.rebalance_cost([1, 2], [0.0, 0.0], [1, 2], bp), 0.0)      # 无漂移无换手
        self.assertAlmostEqual(R.rebalance_cost([1, 2], [1.0, 0.0], [1, 2], bp), (1 / 3) * bp / 1e4)   # 权重漂移成 2/3、1/3 再调回等权
        self.assertAlmostEqual(R.rebalance_cost([1, 2], [0.0, 0.0], [3, 4], bp), 2.0 * bp / 1e4)      # 全换:卖 1 买 1
        self.assertTrue(np.isnan(R.rebalance_cost([1], [0.0], [], bp)))

    def test_summarize_years_rule_and_degenerate(self):
        rng = np.random.default_rng(1)
        idx23 = pd.date_range("2023-01-02", periods=60, freq="5D")
        df = pd.DataFrame({"ic": 0.05 + rng.normal(0, 0.01, 60), "ic_n": 0.05 + rng.normal(0, 0.01, 60), "gross": 0.01, "cost": 0.0,
                           "rho_amt": 0.1, "ic_small": 0.0, "ic_mid": 0.0, "ic_large": 0.0, "up": True, "hivol": False}, index=idx23)
        s = R.summarize(df)
        self.assertEqual((s["years_valid"], s["years"]), (1, 8))              # 只有 2023 年:分母仍是 8
        res = R.verdicts({("f", 5): s})
        self.assertTrue(res[("f", 5)]["fdr"])
        self.assertFalse(res[("f", 5)]["robust"])                              # 年份不足不能通过
        d = df.copy(); d["rho_amt"] = 0.99
        self.assertEqual(R.summarize(d)["status"], "退化")
        tiny = df.copy(); tiny["ic"] = 1e-12
        self.assertTrue(np.isnan(R.summarize(tiny)["retained"]))

    def test_summarize_full_years_passes(self):
        rng = np.random.default_rng(2)
        idx = pd.date_range("2016-01-04", "2023-12-29", freq="7D")
        n = len(idx)
        df = pd.DataFrame({"ic": 0.05 + rng.normal(0, 0.02, n), "ic_n": 0.04 + rng.normal(0, 0.02, n), "gross": 0.004 + rng.normal(0, 0.01, n),
                           "cost": 0.0005, "rho_amt": 0.1, "ic_small": 0.0, "ic_mid": 0.0, "ic_large": 0.0, "up": True, "hivol": False}, index=idx)
        res = R.verdicts({("g", 5): R.summarize(df)})
        self.assertTrue(res[("g", 5)]["robust"])
        df2 = df.copy(); df2["cost"] = 0.01                                       # 成本吃光超额
        self.assertFalse(R.verdicts({("g", 5): R.summarize(df2)})[("g", 5)]["robust"])


class EvalScoreTests(unittest.TestCase):
    def test_signal_detected_and_dates_in_window(self):
        ctx, sc = make_ctx()
        df = R.eval_score(sc, ctx, 5, 8.0)
        self.assertGreater(len(df), 20)
        self.assertGreater(df["ic"].mean(), 0.05)                               # 打分与未来收益正相关
        self.assertTrue(df.index.min() >= pd.Timestamp(R.START) and df.index.max() <= pd.Timestamp(R.END))
        self.assertEqual(df["cost"].iloc[0], 8.0 / 1e4)                         # 首期计全部买入
        self.assertTrue(df["cost"].notna().all())
        self.assertLess(df["rho_amt"].mean(), 0.2)

    def test_no_future_selection(self):
        """未来退出价缺失的股票不能改变入选集合:把打分最低的 10% 股票的所有价格后半段设为 NaN 之外,
        另造一份「只有退出日 NaN」的版本——入选(打分最高 20%)的平均收益必须与手算一致,且与全有限版本在同一批股票上相同。"""
        ctx, sc = make_ctx(seed=3)
        base = R.eval_score(sc, ctx, 5, 8.0)
        t = R.sample_positions(ctx.dates, 5)[0]
        low = np.arange(30)                                                     # 打分最低的 30 只
        ctx2, _ = make_ctx(seed=3)
        ctx2.O = ctx2.O.copy()
        ctx2.O[t + 1 + 5, low] = np.nan                                         # 只让它们在第一个样本日的退出日缺失
        got = R.eval_score(sc, ctx2, 5, 8.0)
        n = sc.shape[1]
        top = np.arange(n)[sc[t] >= np.quantile(sc[t], 0.8)]                   # 入选集合只由打分决定
        ent, ext = ctx2.O[t + 1], ctx2.O[t + 6]
        f = ext / ent - 1
        fin = np.isfinite(f)
        expected = f[top].mean() - f[fin].mean()
        self.assertAlmostEqual(float(got["gross"].iloc[0]), float(expected), places=12)
        self.assertEqual(got["cost"].iloc[0], base["cost"].iloc[0])             # 首期成本与是否缺失无关

    def test_limit_up_open_excluded_from_top_group(self):
        """次日涨停开盘的高分股不可买：入选集合只剩可买股票，头部组合超额必须等于手算值(Codex N1027_5)。"""
        ctx, sc = make_ctx(seed=4)
        t = R.sample_positions(ctx.dates, 5)[0]
        ctx.O = ctx.O.copy()
        ctx.O[t + 1, 250:] = ctx.C[t, 250:] * 1.10                              # 打分最高的 50 只次日涨停开盘
        got = R.eval_score(sc, ctx, 5, 8.0)
        r = ctx.O[t + 6, :250] / ctx.O[t + 1, :250] - 1                         # 可买的是 0..249，其中最高分 20% 为 200..249
        self.assertAlmostEqual(float(got.loc[ctx.dates[t], "gross"]), float(r[200:].mean() - r.mean()), places=12)
        self.assertFalse(hasattr(ctx, "V"))                                     # 可买性不依赖次日成交量

    def test_sample_window_boundaries_and_bh_equality(self):
        dates = pd.to_datetime(["2015-12-31", "2016-01-04", "2023-12-29", "2024-01-02"])
        self.assertEqual(R.sample_positions(dates, 1), [1, 2])                   # 2024 年及以后、2016 年以前都不取
        self.assertEqual(R.bh_fdr([0.05], q=0.05), [True])                       # p == q*rank/m 时拒绝(BH 是 ≤)
        self.assertEqual(R.bh_fdr([0.0500001], q=0.05), [False])

    def test_placebo_family_shape(self):
        ctx, _ = make_ctx(n_stocks=300)
        out = R.placebo_families(ctx, None, None, 8.0, 2, 1, 1)
        self.assertEqual(out[0]["tests"], 2 * len(R.HORIZONS))


if __name__ == "__main__":
    unittest.main()
