"""C04 通缩夏普表脚本的测试：.venv/bin/python tests/test_deflated_sharpe_table.py

全部用临时目录里的合成行情（src.data.synthetic 随机游走），不读写 data/，不联网。
"""
import math
import sys
import tempfile
import unittest
from pathlib import Path
from statistics import NormalDist

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from config import MARKETS  # noqa: E402
from src import data, metrics, registry  # noqa: E402
import deflated_sharpe_table as D  # noqa: E402

ND = NormalDist()
FAKE = {"universe": {"AAA": "测试品种", "CSH": "测试现金"}, "cash": "CSH", "cost_bp": 5, "stock_cost_bp": 8,
        "dual_momentum": {"risky": ("AAA",), "safe": "CSH", "cash": "CSH"}}


def fake_prices(start="2012-01-02", n=3800, seed=0):
    """到 2026 年下半年的合成行情，故意越过研究截止日，用来检查截断。"""
    a = data.synthetic(n=n, seed=seed, start=start)
    c = data.synthetic(n=n, seed=seed + 100, start=start)
    c[["Open", "High", "Low", "Close"]] = np.repeat(100 * np.cumprod(np.full(n, 1.00005))[:, None], 4, axis=1)   # 现金：每天稳定小幅增长
    return {"AAA": a, "CSH": c}


def write_csvs(root: Path, market: str, prices: dict):
    d = root / "daily" / market
    d.mkdir(parents=True, exist_ok=True)
    for s, df in prices.items():
        df.to_csv(d / f"{s}.csv")


class FormulaTests(unittest.TestCase):
    def test_moments_normal(self):
        x = np.random.default_rng(1).standard_normal(200_000)
        mo = D.moments(x)
        self.assertAlmostEqual(mo["skew"], 0.0, delta=0.02)
        self.assertAlmostEqual(mo["kurt"], 3.0, delta=0.05)
        self.assertAlmostEqual(mo["sr"], x.mean() / x.std(ddof=1))

    def test_expected_max_matches_monte_carlo(self):
        self.assertEqual(D.expected_max_sr(1, 1.0), 0.0)
        z = np.random.default_rng(2).standard_normal((4000, 100)).max(axis=1).mean()
        self.assertAlmostEqual(D.expected_max_sr(100, 1.0), z, delta=0.03 * z)
        # 方差放大 4 倍，期望最大值放大 2 倍
        self.assertAlmostEqual(D.expected_max_sr(100, 4.0), 2 * D.expected_max_sr(100, 1.0))

    def test_psr_normal_case(self):
        sr, T = 0.05, 1000
        want = ND.cdf(sr * math.sqrt(T - 1) / math.sqrt(1 + sr ** 2 / 2))
        self.assertAlmostEqual(D.psr(sr, 0.0, T, 0.0, 3.0), want)
        # 负偏 + 肥尾 → 分母变大 → 同样的 Sharpe 可信度更低
        self.assertLess(D.psr(sr, 0.0, T, -1.0, 10.0), want)

    def test_dsr_decreases_with_m(self):
        sr, T, sk, ku, v = 0.08, 1500, -0.5, 8.0, 1 / 1499
        self.assertAlmostEqual(D.dsr(sr, T, sk, ku, 1, v), D.psr(sr, 0.0, T, sk, ku))
        vals = [D.dsr(sr, T, sk, ku, m, v) for m in D.M_GRID]
        self.assertTrue(all(a > b for a, b in zip(vals, vals[1:])))

    def test_breakeven_m(self):
        sr, T, sk, ku, v = 0.08, 1500, -0.5, 8.0, 1 / 1499
        m = D.breakeven_m(sr, T, sk, ku, v)
        self.assertGreaterEqual(m, 1)
        self.assertGreaterEqual(D.dsr(sr, T, sk, ku, m, v), D.PASS)
        self.assertLess(D.dsr(sr, T, sk, ku, m + 1, v), D.PASS)
        self.assertEqual(D.breakeven_m(0.0, T, sk, ku, v), 0)

    def test_m_main(self):
        self.assertEqual(len(D.STRATS), 7)
        self.assertEqual(D.M_MAIN, 7 + 18 + 490)
        self.assertIn(D.M_MAIN, D.M_GRID)

    def test_cutoff(self):
        D.check_end("2026-03-31")
        with self.assertRaises(ValueError):
            D.check_end("2026-04-01")
        for _, b in D.PERIODS.values():
            self.assertLessEqual(b, D.CUTOFF)

    def test_noise_calibration(self):
        rows = D.calibrate(ms=(1, 25), T=500, reps=400, seed=3, log=lambda *_: None)
        self.assertGreater(rows[0]["false_pass_null"], 0.02)     # m=1 就是普通单边 5% 检验
        self.assertLess(rows[0]["false_pass_null"], 0.09)
        self.assertLessEqual(rows[1]["false_pass_null"], 0.05)   # 挑最好的那个也不该更容易过


class PipelineTests(unittest.TestCase):
    def test_same_as_registry_run(self):
        """超额收益的 Sharpe 与网页回测 registry.run 的 ExcessSharpe 完全相同（同一口径）。"""
        pr = {s: df.loc[:D.CUTOFF] for s, df in fake_prices().items()}
        for n in D.STRATS:
            ex = D.strategy_excess(n, pr, "AAA", FAKE, "us")
            want = registry.run(n, pr, "AAA", {}, FAKE, market=None)["stats"]["ExcessSharpe"]
            self.assertAlmostEqual(metrics.sharpe(ex), want, places=10, msg=n)

    def test_rejects_data_after_cutoff(self):
        with self.assertRaises(ValueError):
            D.strategy_excess("rsi2", fake_prices(), "AAA", FAKE, "us")

    def test_truncation_and_no_future_leak(self):
        """截止日之后的行情怎么改，结果都不变。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pr = fake_prices()
            write_csvs(root, "us", pr)
            got, dg = D.load_prices(D.find_daily_dir(root), "us", ["AAA", "CSH", "ZZZ"])
            self.assertEqual(set(got), {"AAA", "CSH"})
            self.assertEqual(len(dg), 2)
            self.assertLessEqual(max(df.index[-1] for df in got.values()), pd.Timestamp(D.CUTOFF))
            base = pd.DataFrame(D.instrument_rows(got, "AAA", FAKE, "us"))
            bad = {s: df.copy() for s, df in pr.items()}
            after = bad["AAA"].index > pd.Timestamp(D.CUTOFF)      # 严格晚于截止日，截止日当天不动
            self.assertGreater(after.sum(), 50)
            bad["AAA"].loc[after, ["Open", "High", "Low", "Close"]] *= 3.0
            write_csvs(root, "us", bad)
            got2, _ = D.load_prices(D.find_daily_dir(root), "us", ["AAA", "CSH"])
            again = pd.DataFrame(D.instrument_rows(got2, "AAA", FAKE, "us"))
            pd.testing.assert_frame_equal(base, again)
        self.assertEqual(len(base), 2 * 8)
        self.assertTrue((base["end"] <= D.CUTOFF).all())
        self.assertEqual(set(base.loc[base.period == "OOS", "start"]), {"2020-01-01"})

    def test_main_end_to_end(self):
        """用合成行情冒充两个市场的 universe，跑完整脚本，检查输出文件。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "data"
            for i, mk in enumerate(("us", "cn")):
                mc = MARKETS[mk]
                pr = {s: data.synthetic(n=3600, seed=10 * i + j, start="2012-06-01")
                      for j, s in enumerate(mc["universe"])}
                write_csvs(root, mk, pr)
            out = Path(tmp) / "out"
            D.main(["--out-dir", str(out), "--data-dir", str(root)])
            for f in ("dsr_all_etf.csv", "dsr_main.csv", "dsr_m_sensitivity.csv", "dsr_meta.json", "dsr_table.md"):
                self.assertTrue((out / f).exists(), f)
            m = pd.read_csv(out / "dsr_main.csv", dtype={"symbol": str})
            self.assertEqual(set(m["symbol"]), {"SPY", "510300"})
            self.assertEqual(len(m), 2 * 8 * 2)
            ok = m["sr_ann"].notna()          # 某段完全空仓时超额收益全为 0，Sharpe 无定义，应保持 NaN 且判不通过
            self.assertTrue((m.loc[ok, "dsr_null"] <= m.loc[ok, "psr0"] + 1e-12).all())
            self.assertTrue(m.loc[~ok, "dsr_null"].isna().all())
            self.assertFalse(m.loc[~ok, "pass"].any())
            self.assertIn("m 敏感性", (out / "dsr_table.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
