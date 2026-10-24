"""trials.py 单测(T007 / S3):只用合成数据与临时目录,不读写项目数据目录。
运行:python3 -B tests/test_validation_trials.py
"""
import json
import math
import os
import random
import sys
import tempfile
import unittest
import warnings
from statistics import NormalDist

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.validation.trials import (TrialError, TrialLog, params_hash, bonferroni, sidak, holm, bh_adjusted,
                                   bh_fdr, bonferroni_threshold, sample_moments, sharpe, sharpe_se,
                                   prob_sharpe_gt, expected_max_sharpe, deflated_sharpe, block_bootstrap,
                                   sharpe_ci, mean_ci, min_trades_hint)

ND = NormalDist()


def synth(n=250, mu=0.001, sd=0.01, seed=1):
    rng = random.Random(seed)
    return [rng.gauss(mu, sd) for _ in range(n)]


class TestTrialLog(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "trial_log.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_file_on_init_and_empty(self):
        log = TrialLog(self.path)
        self.assertFalse(os.path.exists(self.path))
        self.assertEqual(log.count(), 0)
        self.assertEqual(log.summary()["total"], 0)

    def test_reserved_name_rejected(self):
        with self.assertRaises(TrialError):
            TrialLog(os.path.join(self.tmp.name, "trials" + ".jsonl"))

    def test_reserved_name_substring_rejected(self):
        with self.assertRaises(TrialError):
            TrialLog(os.path.join(self.tmp.name, "backup_" + "trials" + ".jsonl"))

    def test_partial_tail_does_not_swallow_new_record(self):
        log = TrialLog(self.path)
        log.record("ma", {"n": 5}, "sharpe", 0.5, 100)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write('{"id": "interrupted"')  # 无换行的残缺末行
        tid = log.record("ma", {"n": 6}, "sharpe", 0.6, 100)
        self.assertEqual(log.count(), 2)
        self.assertEqual(log.load()[-1]["id"], tid)
        self.assertEqual(len(log.summary()["bad_lines"]), 1)

    def test_record_missing_time_is_bad_line_not_crash(self):
        log = TrialLog(self.path)
        log.record("ma", {"n": 5}, "sharpe", 0.5, 100)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"id": "x", "strategy": "s", "params_hash": "h", "metric": "m",
                                "value": 1.0}) + "\n")
        s = log.summary()
        self.assertEqual(s["total"], 1)
        self.assertEqual(len(s["bad_lines"]), 1)

    def test_append_not_overwrite(self):
        a = TrialLog(self.path)
        a.record("ma", {"n": 5}, "sharpe", 0.5, 100)
        with open(self.path, "rb") as f:
            first = f.read()
        b = TrialLog(self.path)  # 新实例继续追加
        b.record("ma", {"n": 10}, "sharpe", 0.7, 100)
        with open(self.path, "rb") as f:
            second = f.read()
        self.assertTrue(second.startswith(first))
        self.assertEqual(len(second.splitlines()), 2)
        self.assertEqual(a.count(), 2)

    def test_ids_and_fields(self):
        log = TrialLog(self.path)
        i1 = log.record("ma", {"n": 5}, "sharpe", 0.5, 100, tag="x", now="2026-01-01T00:00:00+00:00")
        i2 = log.record("ma", {"n": 6}, "sharpe", 0.6, 100)
        self.assertEqual((i1, i2), ("t000001", "t000002"))
        r = log.load()[0]
        self.assertEqual(r["time"], "2026-01-01T00:00:00+00:00")
        self.assertEqual(r["params_hash"], params_hash({"n": 5}))
        self.assertEqual((r["metric"], r["value"], r["n_obs"], r["tag"]), ("sharpe", 0.5, 100, "x"))

    def test_duplicate_explicit_id_rejected(self):
        log = TrialLog(self.path)
        log.record("ma", {"n": 5}, "sharpe", 0.5, 100, trial_id="abc")
        with self.assertRaises(TrialError):
            log.record("ma", {"n": 6}, "sharpe", 0.5, 100, trial_id="abc")
        self.assertEqual(log.count(), 1)

    def test_auto_id_skips_taken(self):
        log = TrialLog(self.path)
        log.record("ma", {"n": 5}, "sharpe", 0.5, 100, trial_id="t000002")
        log.record("ma", {"n": 6}, "sharpe", 0.5, 100)  # 数量=1 → 候选 t000002 已占用 → 顺延
        ids = [r["id"] for r in log.load()]
        self.assertEqual(len(set(ids)), 2)

    def test_same_params_still_counted_and_flagged(self):
        log = TrialLog(self.path)
        log.record("ma", {"n": 5, "m": 1}, "sharpe", 0.5, 100)
        log.record("ma", {"m": 1, "n": 5}, "sharpe", 0.5, 100)  # 键顺序不同,同一组参数
        log.record("rsi", {"n": 5, "m": 1}, "sharpe", 0.5, 100)  # 策略不同,不算重复
        s = log.summary()
        self.assertEqual((s["total"], s["unique_params"], s["duplicates"]), (3, 2, 1))
        self.assertEqual(log.count("ma"), 2)
        self.assertEqual(s["by_strategy"], {"ma": 2, "rsi": 1})
        self.assertTrue(log.load()[1]["duplicate"])

    def test_summary_metric_stats(self):
        log = TrialLog(self.path)
        for v in (1.0, 2.0, 3.0):
            log.record("s", {"v": v}, "sharpe", v, 50)
        m = log.summary()["metrics"]["sharpe"]
        self.assertEqual((m["n"], m["min"], m["max"], m["mean"], m["var"]), (3, 1.0, 3.0, 2.0, 1.0))

    def test_corrupt_lines_tolerated_and_reported(self):
        log = TrialLog(self.path)
        log.record("ma", {"n": 5}, "sharpe", 0.5, 100)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write("not json\n")
            f.write('{"id": "x"}\n')
        log.record("ma", {"n": 6}, "sharpe", 0.5, 100)
        self.assertEqual(log.count(), 2)
        self.assertEqual(len(log.summary()["bad_lines"]), 2)

    def test_invalid_inputs(self):
        log = TrialLog(self.path)
        for bad in ({"strategy": ""}, {"value": float("nan")}, {"value": True}, {"n_obs": -1}, {"n_obs": 1.5},
                    {"metric_name": ""}, {"params": {"f": object()}}, {"params": {"x": float("nan")}}):
            kw = dict(strategy="s", params={"a": 1}, metric_name="m", value=1.0, n_obs=10)
            kw.update(bad)
            with self.assertRaises(TrialError, msg=str(bad)):
                log.record(**kw)
        self.assertEqual(log.count(), 0)
        self.assertFalse(os.path.exists(self.path) and os.path.getsize(self.path) > 0)

    def test_hash_stable(self):
        h = params_hash({"b": 2, "a": [1, 2], "c": "中"})
        self.assertEqual(h, params_hash({"c": "中", "a": [1, 2], "b": 2}))
        self.assertEqual(len(h), 64)
        self.assertNotEqual(h, params_hash({"b": 2, "a": [1, 3], "c": "中"}))
        # 固定值:跨平台/跨版本回归(sha256 of '{"a":1}')
        self.assertEqual(params_hash({"a": 1}),
                         "015abd7f5cc57a2dd94b7590f04ad8084273905ee33ec5cebeae62276a97f862")

    def test_json_lines_valid(self):
        log = TrialLog(self.path)
        log.record("ma", {"n": 5}, "sharpe", 0.5, 100)
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                json.loads(line)


class TestCorrections(unittest.TestCase):
    def test_bonferroni_sidak_hand(self):
        self.assertAlmostEqual(bonferroni(0.01, 5), 0.05, places=12)
        self.assertEqual(bonferroni(0.5, 5), 1.0)
        self.assertAlmostEqual(sidak(0.01, 5), 1 - 0.99 ** 5, places=12)
        self.assertAlmostEqual(sidak(0.01, 5), 0.04900995, places=8)
        self.assertLessEqual(sidak(0.01, 5), bonferroni(0.01, 5))

    def test_holm_hand(self):
        # 升序 .005,.01,.03,.04 → 4×.005=.02, 3×.01=.03, 2×.03=.06, 1×.04=.04→取累计最大 .06
        got = holm([0.01, 0.04, 0.03, 0.005])
        for g, e in zip(got, [0.03, 0.06, 0.06, 0.02]):
            self.assertAlmostEqual(g, e, places=12)
        for p, a in zip([0.01, 0.04, 0.03, 0.005], got):
            self.assertLessEqual(a, bonferroni(p, 4) + 1e-15)

    def test_bh_hand(self):
        # 排名 1..4:.02,.02,.04,.04;自后向前取最小
        got = bh_adjusted([0.01, 0.04, 0.03, 0.005])
        for g, e in zip(got, [0.02, 0.04, 0.04, 0.02]):
            self.assertAlmostEqual(g, e, places=12)
        self.assertEqual(bh_fdr([0.01, 0.04, 0.03, 0.005], q=0.03), [True, False, False, True])

    def test_threshold_monotone_in_trials(self):
        ths = [bonferroni_threshold(0.05, m) for m in (1, 2, 5, 10, 100)]
        self.assertEqual(ths, sorted(ths, reverse=True))
        self.assertEqual(len(set(ths)), 5)
        self.assertEqual(ths[0], 0.05)
        adj = [bonferroni(0.01, m) for m in (1, 2, 5, 10)]
        self.assertEqual(adj, sorted(adj))
        adj = [sidak(0.01, m) for m in (1, 2, 5, 10)]
        self.assertEqual(adj, sorted(adj))
        # Holm:加入更多检验(多一个 p)不会让原有项的调整值变小
        base = holm([0.01, 0.02])
        more = holm([0.01, 0.02, 0.5])
        self.assertTrue(all(b <= m + 1e-15 for b, m in zip(base, more)))

    def test_invalid(self):
        for f in (lambda: bonferroni(1.5, 2), lambda: bonferroni(0.1, 0), lambda: holm([0.1, -0.1]),
                  lambda: sidak(0.1, 1.5)):
            with self.assertRaises(TrialError):
                f()


class TestSharpe(unittest.TestCase):
    def test_moments_hand(self):
        m = sample_moments([1, 2, 3, 4])
        self.assertAlmostEqual(m["mean"], 2.5, places=12)
        self.assertAlmostEqual(m["std"], math.sqrt(5 / 3), places=12)
        self.assertAlmostEqual(m["skew"], 0.0, places=12)
        self.assertAlmostEqual(m["kurt"], 2.5625 / 1.5625, places=12)  # 1.64
        m = sample_moments([0, 0, 0, 4])
        self.assertAlmostEqual(m["skew"], 6 / 3 ** 1.5, places=12)
        self.assertAlmostEqual(sharpe([1, 2, 3, 4]), 2.5 / math.sqrt(5 / 3), places=12)

    def test_se_hand(self):
        self.assertAlmostEqual(sharpe_se(0.1, 101), math.sqrt(1.005 / 100), places=12)
        # 偏度 -0.5、峰度 5、SR=0.2:1 + 0.1 + 1×0.04 = 1.14
        self.assertAlmostEqual(sharpe_se(0.2, 101, skew=-0.5, kurt=5.0), math.sqrt(1.14 / 100), places=12)

    def test_psr_hand(self):
        e = ND.cdf(0.2 / math.sqrt(1.02 / 100))
        self.assertAlmostEqual(prob_sharpe_gt(0.2, 0.0, 101), e, places=12)
        self.assertAlmostEqual(prob_sharpe_gt(0.0, 0.0, 101), 0.5, places=12)

    def test_expected_max_known(self):
        self.assertEqual(expected_max_sharpe(1, 0.5), 0.0)
        g = 0.5772156649015329
        e = (1 - g) * ND.inv_cdf(0.9) + g * ND.inv_cdf(1 - 1 / (10 * math.e))
        self.assertAlmostEqual(expected_max_sharpe(10, 1.0), e, places=12)
        # 手算:(1−γ)×1.2816 + γ×Φ⁻¹(0.96321) ≈ 0.5419 + 1.0327 ≈ 1.5746
        self.assertAlmostEqual(expected_max_sharpe(10, 1.0), 1.5746, places=3)
        self.assertAlmostEqual(expected_max_sharpe(10, 4.0), 2 * e, places=12)

    def test_dsr_monotone_in_trials(self):
        vals = [deflated_sharpe(0.15, 500, 0.0, 3.0, n, 0.0004) for n in (1, 2, 5, 20, 100, 1000)]
        for a, b in zip(vals, vals[1:]):
            self.assertLess(b, a)
        self.assertAlmostEqual(vals[0], prob_sharpe_gt(0.15, 0.0, 500), places=12)  # N=1 即 PSR

    def test_dsr_hand(self):
        bench = expected_max_sharpe(10, 0.0004)
        se = math.sqrt((1 - 0.15 * 0.1 + 0.5 * 0.0225) / 499)
        e = ND.cdf((0.15 - bench) / se)
        self.assertAlmostEqual(deflated_sharpe(0.15, 500, 0.1, 3.0, 10, 0.0004), e, places=12)

    def test_short_sample_nan_with_warning(self):
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            self.assertTrue(math.isnan(sharpe_se(0.1, 29)))
            self.assertTrue(math.isnan(deflated_sharpe(0.1, 10, 0, 3, 5, 0.01)))
        self.assertEqual(len(w), 2)

    def test_tiny_sample_nan_not_raise(self):
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            for n in (0, 1):
                self.assertTrue(math.isnan(sharpe_se(0.1, n)))
                self.assertTrue(math.isnan(prob_sharpe_gt(0.1, 0.0, n)))
                self.assertTrue(math.isnan(deflated_sharpe(0.1, n, 0, 3, 5, 0.01)))
            self.assertTrue(math.isnan(sharpe_se(0.1, 1, min_n=1)))  # min_n 再小也不除零
        self.assertEqual(len(w), 7)


class TestBootstrap(unittest.TestCase):
    def test_reproducible_and_seed_matters(self):
        r = synth()
        a = sharpe_ci(r, block=5, n_boot=200, seed=7)
        b = sharpe_ci(r, block=5, n_boot=200, seed=7)
        c = sharpe_ci(r, block=5, n_boot=200, seed=8)
        self.assertEqual(a, b)
        self.assertNotEqual((a["lower"], a["upper"]), (c["lower"], c["upper"]))
        self.assertEqual(list(block_bootstrap(r[:40], 3, 3, 1)), list(block_bootstrap(r[:40], 3, 3, 1)))

    def test_global_random_state_untouched(self):
        random.seed(123)
        expect = random.random()
        random.seed(123)
        sharpe_ci(synth(), n_boot=50, seed=1)
        self.assertEqual(random.random(), expect)

    def test_resample_shape_and_block_structure(self):
        x = list(range(40))
        for s in block_bootstrap(x, 4, 5, 3):
            self.assertEqual(len(s), 40)
            self.assertTrue(set(s) <= set(x))
            # 每 4 个一块,块内相邻元素差 1(或循环回绕)
            for k in range(0, 40, 4):
                for j in range(3):
                    self.assertIn(s[k + j + 1] - s[k + j], (1, -39))

    def test_ci_contains_point_and_sane(self):
        r = synth(500, mu=0.002, sd=0.01)
        ci = sharpe_ci(r, block=1, n_boot=400, seed=0)
        self.assertLess(ci["lower"], ci["point"])
        self.assertLess(ci["point"], ci["upper"])
        self.assertAlmostEqual(ci["point"], sharpe(r), places=12)
        ann = sharpe_ci(r, block=1, n_boot=400, seed=0, periods_per_year=252)
        self.assertAlmostEqual(ann["lower"], ci["lower"] * math.sqrt(252), places=9)
        mci = mean_ci(r, n_boot=400, seed=0)
        self.assertAlmostEqual(mci["point"], sum(r) / len(r), places=12)
        self.assertLess(mci["lower"], mci["upper"])

    def test_constant_series_mean_ci_degenerate(self):
        ci = mean_ci([0.01] * 40, n_boot=50, seed=1)
        self.assertAlmostEqual(ci["lower"], 0.01, places=12)
        self.assertAlmostEqual(ci["upper"], 0.01, places=12)

    def test_short_series_nan_with_warning(self):
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            ci = sharpe_ci(synth(20))
        self.assertTrue(math.isnan(ci["lower"]) and math.isnan(ci["upper"]))
        self.assertTrue(ci["warning"])
        self.assertEqual(len(w), 1)

    def test_invalid(self):
        for f in (lambda: list(block_bootstrap([1.0, 2.0], 3, 1, 0)), lambda: list(block_bootstrap([], 1, 1, 0)),
                  lambda: list(block_bootstrap([1.0], 1, 0, 0)), lambda: sharpe_ci(synth(), alpha=1.5)):
            with self.assertRaises(TrialError):
                f()


class TestMinTrades(unittest.TestCase):
    def test_hand(self):
        h = min_trades_hint(0.01, 0.05)  # (1.96×5)² = 96.04 → 97
        self.assertEqual((h["formula_n"], h["n"]), (97, 97))
        self.assertIn("不是统计保证", h["note"])

    def test_floor_and_nonpositive(self):
        self.assertEqual(min_trades_hint(0.1, 0.05)["n"], 30)  # 公式给 4,取下限 30
        self.assertIsNone(min_trades_hint(-0.01, 0.05)["n"])
        with self.assertRaises(TrialError):
            min_trades_hint(0.01, 0.0)

    def test_monotone_in_noise(self):
        ns = [min_trades_hint(0.01, s)["n"] for s in (0.02, 0.05, 0.1)]
        self.assertEqual(ns, sorted(ns))


if __name__ == "__main__":
    unittest.main()
