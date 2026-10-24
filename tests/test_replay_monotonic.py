"""T008 S2:单因子单调性检验测试。只用合成数据与临时目录,不读 data/,不联网。"""
import copy
import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.replay import monotonic as M  # noqa: E402
from src.replay import registry  # noqa: E402

_spec = importlib.util.spec_from_file_location("replay_factor_check", ROOT / "scripts" / "replay_factor_check.py")
CLI = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(CLI)

N_STOCKS = 60


def make_panel(kind, seed=1, start="2023-06-01", end="2025-12-31"):
    """kind: mono=每只股票有持久漂移(过去涨得多的未来也涨,动量单调);
    rev=对数价格是周期 40 日的正弦波(20 日收益与之后 20 日收益负相关,动量因子方向反);rw=纯随机游走。"""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(start, end)
    x = np.linspace(-1, 1, N_STOCKS)
    if kind == "rev":
        t = np.arange(len(idx))[:, None]
        phase = rng.uniform(0, 2 * np.pi, N_STOCKS)[None, :]
        logp = 0.15 * np.sin(2 * np.pi * t / 40 + phase) + np.cumsum(rng.normal(0, 0.01, (len(idx), N_STOCKS)), axis=0)
        close = 10 * np.exp(logp)
    else:
        drift = {"mono": 0.002 * x, "rw": 0 * x}[kind]
        ret = drift + rng.normal(0, 0.01, (len(idx), N_STOCKS))
        close = 10 * np.cumprod(1 + ret, axis=0)
    panel = {}
    for j in range(N_STOCKS):
        c = pd.Series(close[:, j], index=idx)
        o = c.shift(1).fillna(c.iloc[0])
        panel[f"S{j:03d}"] = pd.DataFrame({"open": o, "high": np.maximum(o, c) * 1.002,
                                           "low": np.minimum(o, c) * 0.998, "close": c, "volume": 1e6})
    return panel


class Basics(unittest.TestCase):
    def test_spearman(self):
        self.assertAlmostEqual(M.spearman([1, 2, 3, 4], [10, 20, 30, 40]), 1.0)
        self.assertAlmostEqual(M.spearman([1, 2, 3, 4], [4, 3, 2, 1]), -1.0)
        self.assertTrue(np.isnan(M.spearman([1, 1, 1], [1, 2, 3])))
        self.assertTrue(np.isnan(M.spearman([1], [1])))

    def test_assign_groups_balanced_and_ordered(self):
        g = M.assign_groups(np.array([5.0, 1, 4, 2, 3, 9, 7, 8, 6, 10]), 5)
        self.assertEqual(sorted(np.bincount(g)), [2] * 5)
        self.assertEqual(g[1], 0)     # 最小分在 0 组
        self.assertEqual(g[9], 4)     # 最大分在最高组

    def test_forward_returns_hand_calc(self):
        idx = pd.bdate_range("2024-01-01", periods=6)
        df = pd.DataFrame({"open": [1, 2, 3, 4, 5, 6.0], "high": 9, "low": 0.5,
                           "close": [10, 20, 30, 40, 50, 60.0], "volume": 1.0}, index=idx)
        f = M.forward_returns({"A": df}, horizon=2)
        # t=0: close[2]/open[1]-1 = 30/2-1
        self.assertAlmostEqual(f["A"].iloc[0], 14.0)
        self.assertAlmostEqual(f["A"].iloc[3], 60 / 5 - 1)
        self.assertTrue(np.isnan(f["A"].iloc[4]) and np.isnan(f["A"].iloc[5]))

    def test_embargo_drops_last_horizon_days(self):
        idx = pd.bdate_range("2024-01-01", "2024-12-31")
        d = M.segment_signal_dates(idx, "2024-03-01", "2024-06-28", 20)
        inside = idx[(idx >= "2024-03-01") & (idx <= "2024-06-28")]
        self.assertEqual(len(d), len(inside) - 20)
        self.assertEqual(d[-1], inside[-21])
        self.assertEqual(len(M.segment_signal_dates(idx, "2024-03-01", "2024-03-10", 20)), 0)

    def test_judge_too_few_days_fails(self):
        gm = pd.DataFrame(np.tile(np.arange(5.0), (10, 1)))
        r = M.judge(gm, gm[4] - gm[0])
        self.assertFalse(r["passed"])
        self.assertTrue(any("样本不足" in s for s in r["reasons"]))

    def test_judge_monotone_vs_not(self):
        rng = np.random.default_rng(0)
        base = np.tile(np.arange(5.0) * 0.01, (200, 1)) + rng.normal(0, 0.002, (200, 5))
        ok = M.judge(pd.DataFrame(base), pd.Series(base[:, 4] - base[:, 0]), n_boot=200)
        self.assertTrue(ok["passed"], ok["reasons"])
        self.assertGreaterEqual(ok["rho"], 0.8)
        self.assertGreater(ok["spread_ci_lower"], 0)
        u = np.tile([0.04, 0.01, 0.0, 0.01, 0.04], (200, 1)) + rng.normal(0, 0.002, (200, 5))
        bad = M.judge(pd.DataFrame(u), pd.Series(u[:, 4] - u[:, 0]), n_boot=200)
        self.assertFalse(bad["passed"])
        self.assertTrue(any("不单调" in s for s in bad["reasons"]))

    def test_judge_spread_positive_but_ci_includes_zero(self):
        # 组均收益单调,但每日噪声远大于均值差 -> 多空差置信区间下界 ≤ 0,不通过
        rng = np.random.default_rng(3)
        gm = np.tile(np.arange(5.0) * 1e-3, (60, 1))
        for c in (0, 4):
            z = rng.normal(0, 0.05, 60)
            gm[:, c] += z - z.mean()      # 每日噪声均值为 0:组均值恰好 0..0.004 升序(rho=1),多空差均值 0.004
        r = M.judge(pd.DataFrame(gm), pd.Series(gm[:, 4] - gm[:, 0]), n_boot=300)
        self.assertGreaterEqual(r["rho"], 0.8)
        self.assertFalse(r["passed"])
        self.assertTrue(any("置信区间" in s for s in r["reasons"]))

    def test_group_count_limited_5_to_10(self):
        idx = pd.bdate_range("2024-01-01", periods=60)
        df = pd.DataFrame(np.random.default_rng(0).normal(size=(60, 40)), index=idx)
        for bad in (1, 2, 4, 11, True, 5.0):
            with self.assertRaises(ValueError):
                M.check_factor(df, df, idx[0], idx[-1], n_groups=bad, horizon=5)
        M.check_factor(df, df, idx[0], idx[-1], n_groups=10, horizon=5, n_boot=200)

    def test_n_boot_floor_cannot_be_bypassed(self):
        # 回归:n_boot=2 曾能让"区间含零"的多空差凑出下界>0 而通过
        rng = np.random.default_rng(3)
        gm = np.tile(np.arange(5.0) * 1e-3, (60, 1))
        for c in (0, 4):
            z = rng.normal(0, 0.05, 60)
            gm[:, c] += z - z.mean()
        for bad in (1, 2, 199, True):
            with self.assertRaises(ValueError):
                M.judge(pd.DataFrame(gm), pd.Series(gm[:, 4] - gm[:, 0]), n_boot=bad)
        idx = pd.bdate_range("2024-01-01", periods=60)
        df = pd.DataFrame(np.random.default_rng(0).normal(size=(60, 40)), index=idx)
        with self.assertRaises(ValueError):
            M.check_factor(df, df, idx[0], idx[-1], horizon=5, n_boot=2)
        with self.assertRaises(ValueError):
            M.check_all({}, "train", n_boot=2)

    def test_rho_exactly_at_threshold_not_eliminated(self):
        # 组均收益秩为 [0,2,1,4,3],理论 rho=0.8,浮点算出 0.7999999999999999,必须按等于 0.8 处理
        gm = pd.DataFrame(np.tile([0.0, 2.0, 1.0, 4.0, 3.0], (60, 1)))
        r = M.judge(gm, gm[4] - gm[0], n_boot=200)
        self.assertLess(r["rho"], 0.8)          # 确认浮点误差真实存在
        self.assertTrue(r["passed"], r["reasons"])
        gm2 = pd.DataFrame(np.tile([0.0, 3.0, 1.0, 4.0, 2.0], (60, 1)))   # rho=0.6,仍应淘汰
        self.assertFalse(M.judge(gm2, gm2[4] - gm2[0], n_boot=200)["passed"])

    def test_block_length_at_least_horizon(self):
        self.assertGreaterEqual(M.BLOCK, M.HORIZON)


class Strategy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.panels = {k: make_panel(k) for k in ("mono", "rev", "rw")}

    def run_mom(self, kind, panel=None):
        return M.check_strategy("mom_01", panel or self.panels[kind], "train", n_boot=200)

    def by_n(self, res):
        return {r["params"]["n"]: r for r in res["params"]}

    def test_monotone_data_passes(self):
        res = self.run_mom("mono")
        self.assertEqual(res["status"], "checked")
        self.assertEqual(res["n_params"], 5)
        self.assertFalse(res["eliminated"])
        r = self.by_n(res)[120]
        self.assertTrue(r["passed"], r["reasons"])
        self.assertGreaterEqual(r["rho"], 0.8)
        self.assertEqual(len(r["group_mean"]), 5)
        self.assertEqual(r["group_mean"], sorted(r["group_mean"]))

    def test_reversal_data_fails_not_flipped(self):
        res = self.run_mom("rev")
        r = self.by_n(res)[20]
        self.assertLess(r["spread_mean"], 0)       # 动量因子在反转数据里多空差为负
        self.assertFalse(r["passed"])              # 不允许"反着买":直接淘汰
        self.assertTrue(r["reasons"])

    def test_u_shaped_relation_fails_check_factor(self):
        rng = np.random.default_rng(5)
        idx = pd.bdate_range("2024-01-01", periods=120)
        score = pd.DataFrame(rng.normal(size=(120, 100)), index=idx)
        fwd = (score ** 2) * 0.01 + pd.DataFrame(rng.normal(0, 0.005, (120, 100)), index=idx)   # U 形关系
        r = M.check_factor(score, fwd, idx[0], idx[-1], horizon=5, n_boot=200)
        self.assertFalse(r["passed"])
        self.assertTrue(any("不单调" in s for s in r["reasons"]), r["reasons"])

    def test_random_walk_n120_fails(self):
        r = self.by_n(self.run_mom("rw"))[120]
        self.assertFalse(r["passed"])

    def test_data_after_segment_end_ignored(self):
        base = self.run_mom("mono")
        p2 = {k: v.copy() for k, v in self.panels["mono"].items()}
        for df in p2.values():
            m = df.index > "2025-06-30"
            for c in ("open", "high", "low", "close"):
                df.loc[m, c] = 1e9 * (1 + np.arange(m.sum()))      # 段后数据改得面目全非
        again = self.run_mom("mono", p2)
        for a, b in zip(base["params"], again["params"]):
            self.assertEqual(a["rho"], b["rho"])
            self.assertEqual(a["spread_mean"], b["spread_mean"])
            self.assertEqual(a["n_days"], b["n_days"])

    def test_labels_never_cross_segment_end(self):
        res = self.run_mom("mono")
        n_seg = len(pd.bdate_range("2024-01-01", "2025-06-30"))
        self.assertLessEqual(self.by_n(res)[120]["n_days"], n_seg - M.HORIZON)

    def test_holdout_refused(self):
        with self.assertRaises(ValueError):
            M.check_strategy("mom_01", self.panels["mono"], "holdout")

    def test_control_skipped(self):
        ctrl = next(s for s in registry.load_catalog()["strategies"] if s["id"].startswith("ctrl_"))
        res = M.check_strategy(ctrl["id"], self.panels["mono"], "train")
        self.assertEqual(res["status"], "skipped_control")
        self.assertFalse(res["eliminated"])

    def test_non_higher_better_direction_rejected(self):
        cat = copy.deepcopy(registry.load_catalog())
        for s in cat["strategies"]:
            if s["id"] == "mom_01":
                s["direction"] = "lower_better"
        with self.assertRaises(ValueError):
            M.check_strategy("mom_01", self.panels["mono"], "train", catalog=cat)

    def test_check_all_only_and_error_recorded(self):
        rep = M.check_all(self.panels["mono"], "train", only=["mom_01", "ind_01"], n_boot=200)
        by = {r["id"]: r for r in rep["results"]}
        self.assertEqual(by["mom_01"]["status"], "checked")
        self.assertEqual(by["ind_01"]["status"], "error")     # 缺 industry:如实记为未检验,不算通过
        self.assertIsNone(by["ind_01"]["eliminated"])
        self.assertEqual(rep["total_grid_trials"], registry.total_trials(registry.load_catalog()))
        self.assertEqual(len(rep["catalog_hash"]), 64)


class Cli(unittest.TestCase):
    def make_dir(self, tmp):
        d = Path(tmp) / "daily"
        d.mkdir()
        for code, df in make_panel("mono", start="2023-06-01", end="2025-12-31").items():
            df.columns = [c.capitalize() for c in df.columns]
            df.to_parquet(d / f"{code}.parquet")
        return d

    def test_end_to_end_and_safety(self):
        late = datetime.fromisoformat("2026-10-22T20:00:00+08:00")
        with tempfile.TemporaryDirectory() as tmp:
            d = self.make_dir(tmp)
            before = sorted((p.name, p.stat().st_mtime_ns) for p in d.iterdir())
            out = Path(tmp) / "out"
            rc = CLI.main(["--daily-dir", str(d), "--output", str(out), "--strategies", "mom_01",
                           "--n-boot", "200"], now=late)
            self.assertEqual(rc, 0)
            rep = json.loads((out / "monotonic_report.json").read_text(encoding="utf-8"))
            self.assertEqual(rep["segment"], "train")
            self.assertTrue(rep["caveats"])
            self.assertEqual(rep["results"][0]["id"], "mom_01")
            self.assertTrue((out / "monotonic_summary.csv").read_text(encoding="utf-8").startswith("strategy,"))
            after = sorted((p.name, p.stat().st_mtime_ns) for p in d.iterdir())
            self.assertEqual(before, after)                       # 数据目录只读
            self.assertEqual(CLI.main(["--daily-dir", str(d), "--output", str(out)], now=late), 2)   # 拒绝覆盖
            self.assertEqual(CLI.main(["--daily-dir", str(d), "--output", str(d / "sub")], now=late), 1)   # 不能在数据目录里

    def test_bad_groups_rejected_and_seed_recorded(self):
        late = datetime.fromisoformat("2026-10-22T20:00:00+08:00")
        with tempfile.TemporaryDirectory() as tmp:
            d = self.make_dir(tmp)
            self.assertEqual(CLI.main(["--daily-dir", str(d), "--output", str(Path(tmp) / "o1"), "--groups", "2"], now=late), 1)
            outs = []
            for i in (2, 3):
                o = Path(tmp) / f"o{i}"
                self.assertEqual(CLI.main(["--daily-dir", str(d), "--output", str(o), "--strategies", "mom_01",
                                           "--n-boot", "200","--seed", "7"], now=late), 0)
                outs.append(json.loads((o / "monotonic_report.json").read_text(encoding="utf-8")))
            self.assertEqual(outs[0]["seed"], 7)
            self.assertEqual(outs[0], outs[1])

    def test_holdout_not_selectable(self):
        with self.assertRaises(SystemExit):
            CLI.main(["--daily-dir", "x", "--output", "y", "--segment", "holdout"])

    def test_intraday_bar_dropped_before_1530(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "daily"
            d.mkdir()
            idx = pd.to_datetime(["2024-01-02", "2024-01-03"])
            pd.DataFrame({"Open": 1.0, "High": 1.0, "Low": 1.0, "Close": 1.0, "Volume": 1.0},
                         index=idx).to_parquet(d / "A.parquet")
            end = pd.Timestamp("2024-12-31")
            early = datetime.fromisoformat("2024-01-03T10:00:00+08:00")
            late = datetime.fromisoformat("2024-01-03T16:00:00+08:00")
            self.assertEqual(len(CLI.load_panel(d, end, early)["A"]), 1)
            self.assertEqual(len(CLI.load_panel(d, end, late)["A"]), 2)


if __name__ == "__main__":
    unittest.main()
