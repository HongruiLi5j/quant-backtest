"""策略体检 src/health_check.py 的测试（T022）。全部用合成数据，不读 data/、不联网、不导入 app。

python -m pytest tests/test_health_check.py
"""
import json
import random
import sys
import unittest
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src import health_check as H  # noqa: E402

warnings.simplefilter("ignore")

_REAL_CI = H.tr.sharpe_ci
_CI_CACHE: dict = {}


def _cached_ci(r, **kw):
    """bootstrap 区间对同一序列同一参数是确定性的（固定种子），缓存它只是为了让测试别花几十秒重复算。"""
    key = (tuple(r), tuple(sorted(kw.items())))
    if key not in _CI_CACHE:
        _CI_CACHE[key] = _REAL_CI(r, **kw)
    return _CI_CACHE[key]


def setUpModule():
    H.tr.sharpe_ci = _cached_ci


def tearDownModule():
    H.tr.sharpe_ci = _REAL_CI


def equity(returns):
    e = [1.0]
    for x in returns:
        e.append(e[-1] * (1 + x))
    return e


def synth(n, mu, sigma, seed):
    g = random.Random(seed)
    return [g.gauss(mu, sigma) for _ in range(n)]


GOOD_SPLIT = {"is": {"sharpe": 1.2}, "oos": {"sharpe": 1.0}}
GOOD_COST = [{"label": l, "sharpe": s} for l, s in (("0", 1.5), ("1x", 1.2), ("2x", 0.9), ("4x", 0.3))]
GOOD_MC = {"p_loss": 5.0}
GOOD_PLATEAU = [{"key": "n", "label": "n", "pts": [{"v": i, "sharpe": s} for i, s in enumerate((1.0, 1.1, 1.2, 1.1, 1.0))]}]


def make_res(r, b=None, trades=60, split=GOOD_SPLIT, cost=GOOD_COST, mc=GOOD_MC, plateau=GOOD_PLATEAU, analysis=True):
    res = {"strategy": "x", "symbol": "S", "params": {"n": 2, "m": 2}, "start": "2020-01-01", "end": "2024-01-01",
           "equity": equity(r), "bench": equity(b) if b is not None else None, "stats": {"Trades": trades}}
    if analysis:
        res["analysis"] = {"split": split, "cost_sens": cost, "mc": mc, "param_sens": plateau}
    return res


def status(rep, cid):
    return next(c["status"] for c in rep["checks"] if c["id"] == cid)


def value(rep, cid):
    return next(c["value"] for c in rep["checks"] if c["id"] == cid)


STRONG = [(synth(2000, 0.0012, 0.01, s), synth(2000, 0.0002, 0.01, 100 + s)) for s in (1, 2, 3)]
ZERO = [(synth(2000, 0.0, 0.01, s), synth(2000, 0.0002, 0.01, 100 + s)) for s in (1, 2, 3)]
NEG = [(synth(2000, -0.0005, 0.01, s), synth(2000, 0.0002, 0.01, 100 + s)) for s in (1, 2, 3)]


class ThresholdGuardTests(unittest.TestCase):
    def test_thresholds_are_frozen(self):
        """阈值是事先写死的。如果这个测试变红，说明有人改了阈值——必须由所有者批准并记录原因，而不是为了让某个策略过关。"""
        self.assertEqual(H.THRESHOLDS, {
            "min_days": 504, "min_trades": 30, "dsr_pass": 0.95, "dsr_warn": 0.80,
            "excess_psr_pass": 0.95, "excess_psr_warn": 0.80, "oos_keep_ratio": 0.5,
            "cost_label": "2x", "cost_label_hard": "4x", "plateau_pass": 0.60, "plateau_warn": 0.30, "plateau_keep": 0.50,
            "p_loss_pass": 20.0, "p_loss_warn": 40.0, "ci_block": 10, "ci_n_boot": 1000, "ci_seed": 0})

    def test_report_carries_the_thresholds_it_used(self):
        rep = H.health_check(make_res(*STRONG[0]))
        self.assertEqual(rep["thresholds"], H.THRESHOLDS)


class VerdictBehaviourTests(unittest.TestCase):
    def test_strong_drift_with_few_trials_passes_the_checks(self):
        for r, b in STRONG:
            rep = H.health_check(make_res(r, b), n_trials=1)
            self.assertEqual(rep["verdict"]["code"], "passes_checks", rep["verdict"])
            self.assertEqual(status(rep, "sharpe_ci"), "pass")
            self.assertEqual(status(rep, "dsr"), "pass")

    def test_more_trials_means_a_harsher_dsr_monotonically(self):
        for r, b in STRONG:
            d = [value(H.health_check(make_res(r, b), n_trials=n), "dsr")["dsr"] for n in (1, 10, 1000, 10 ** 6)]
            self.assertTrue(all(x >= y for x, y in zip(d, d[1:])), d)
            self.assertGreater(d[0] - d[-1], 0.1, d)

    def test_an_absurd_trial_count_turns_a_passing_result_into_no_evidence_or_a_warning(self):
        outcomes = {H.health_check(make_res(r, b), n_trials=10 ** 6)["verdict"]["code"] for r, b in STRONG}
        self.assertTrue(outcomes <= {"no_evidence", "passes_checks"})
        self.assertIn("no_evidence", outcomes)

    def test_zero_and_negative_drift_never_look_like_evidence_whatever_the_trial_count(self):
        for r, b in ZERO + NEG:
            for n in (1, 10, 10 ** 6):
                rep = H.health_check(make_res(r, b), n_trials=n)
                self.assertEqual(rep["verdict"]["code"], "no_evidence", (n, rep["verdict"]))

    def test_short_sample_is_insufficient_and_says_so_first(self):
        r = synth(200, 0.0015, 0.01, 1)
        rep = H.health_check(make_res(r, synth(200, 0, 0.01, 2)))
        self.assertEqual(rep["verdict"]["code"], "insufficient_sample")
        self.assertEqual(status(rep, "sample"), "fail")

    def test_too_few_trades_is_insufficient_even_with_a_long_sample(self):
        rep = H.health_check(make_res(*STRONG[0], trades=12))
        self.assertEqual(rep["verdict"]["code"], "insufficient_sample")

    def test_unknown_trade_count_is_na_and_blocks_a_passing_verdict(self):
        res = make_res(*STRONG[0])
        del res["stats"]["Trades"]
        rep = H.health_check(res)
        self.assertEqual(status(rep, "sample"), "na")
        self.assertNotEqual(rep["verdict"]["code"], "passes_checks")
        self.assertIn("sample", rep["verdict"]["missing_core"])

    def test_negative_oos_alone_prevents_a_passing_verdict(self):
        rep = H.health_check(make_res(*STRONG[0], split={"is": {"sharpe": 1.5}, "oos": {"sharpe": -0.2}}))
        self.assertEqual(status(rep, "oos"), "fail")
        self.assertEqual(rep["verdict"]["code"], "no_evidence")

    def test_oos_degradation_only_warns(self):
        rep = H.health_check(make_res(*STRONG[0], split={"is": {"sharpe": 2.0}, "oos": {"sharpe": 0.6}}))
        self.assertEqual(status(rep, "oos"), "warn")

    def test_costs_that_kill_the_edge_at_2x_fail_the_check(self):
        cost = [{"label": l, "sharpe": s} for l, s in (("0", 1.0), ("1x", 0.4), ("2x", -0.1), ("4x", -0.8))]
        rep = H.health_check(make_res(*STRONG[0], cost=cost))
        self.assertEqual(status(rep, "cost"), "fail")
        self.assertEqual(rep["verdict"]["code"], "mixed")

    def test_edge_dying_only_at_4x_costs_warns(self):
        cost = [{"label": l, "sharpe": s} for l, s in (("0", 1.5), ("1x", 1.0), ("2x", 0.4), ("4x", -0.3))]
        self.assertEqual(status(H.health_check(make_res(*STRONG[0], cost=cost)), "cost"), "warn")

    def test_a_parameter_spike_fails_the_plateau_check(self):
        spike = [{"key": "n", "pts": [{"v": i, "sharpe": s} for i, s in enumerate((-0.2, 0.1, 1.5, 0.0, -0.3))]}]
        rep = H.health_check(make_res(*STRONG[0], plateau=spike))
        self.assertEqual(status(rep, "plateau"), "fail")
        self.assertEqual(value(rep, "plateau")["worst_ratio"], 0.0)

    def test_plateau_uses_the_worst_parameter_not_the_average(self):
        good = GOOD_PLATEAU[0]
        bad = {"key": "m", "pts": [{"v": i, "sharpe": s} for i, s in enumerate((-0.2, 0.1, 1.5, 0.0, -0.3))]}
        rep = H.health_check(make_res(*STRONG[0], plateau=[good, bad]))
        self.assertEqual(status(rep, "plateau"), "fail")

    def test_high_monte_carlo_loss_probability(self):
        self.assertEqual(status(H.health_check(make_res(*STRONG[0], mc={"p_loss": 55.0})), "mc"), "fail")
        self.assertEqual(status(H.health_check(make_res(*STRONG[0], mc={"p_loss": 30.0})), "mc"), "warn")
        self.assertEqual(status(H.health_check(make_res(*STRONG[0], mc={"p_loss": 20.0})), "mc"), "pass")

    def test_missing_analysis_gives_na_checks_and_an_incomplete_verdict(self):
        rep = H.health_check(make_res(*STRONG[0], analysis=False))
        for cid in ("oos", "cost", "plateau", "mc"):
            self.assertEqual(status(rep, cid), "na", cid)
        self.assertEqual(rep["verdict"]["code"], "incomplete")

    def test_no_benchmark_makes_the_excess_check_na_not_a_pass(self):
        rep = H.health_check(make_res(STRONG[0][0], None))
        self.assertEqual(status(rep, "excess"), "na")

    def test_underperforming_buy_and_hold_fails_the_excess_check(self):
        r = synth(2000, 0.0004, 0.01, 5)
        b = synth(2000, 0.0012, 0.01, 6)
        self.assertEqual(status(H.health_check(make_res(r, b)), "excess"), "fail")


class InputAndOutputTests(unittest.TestCase):
    def test_n_trials_must_be_a_positive_integer(self):
        res = make_res(*STRONG[0])
        for bad in (0, -1, 1.5, True, "3", None):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    H.health_check(res, n_trials=bad)

    def test_res_must_have_an_equity_series(self):
        for bad in (None, {}, {"equity": []}, {"equity": [1.0, 1.1]}, "x", {"equity": "abc"}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    H.health_check(bad)

    def test_report_is_strict_json_serialisable_even_with_nan_inputs(self):
        flat = [0.0] * 600                                   # 完全没有波动：夏普无定义
        rep = H.health_check(make_res(flat, flat))
        json.dumps(rep, allow_nan=False)
        rep2 = H.health_check(make_res(*STRONG[0], split={"is": {"sharpe": float("nan")}, "oos": {"sharpe": float("inf")}}))
        json.dumps(rep2, allow_nan=False)
        self.assertEqual(status(rep2, "oos"), "na")

    def test_deterministic_for_the_same_input(self):
        res = make_res(*STRONG[1])
        self.assertEqual(json.dumps(H.health_check(res, 7), sort_keys=True), json.dumps(H.health_check(res, 7), sort_keys=True))

    def test_every_check_has_both_languages_and_a_known_status(self):
        rep = H.health_check(make_res(*STRONG[0]))
        self.assertEqual([c["id"] for c in rep["checks"]], ["sample", "sharpe_ci", "dsr", "excess", "oos", "cost", "plateau", "mc"])
        for c in rep["checks"]:
            self.assertIn(c["status"], ("pass", "warn", "fail", "na"))
            self.assertTrue(c["zh"].strip() and c["en"].strip(), c["id"])

    def test_inputs_are_echoed_so_the_report_is_self_describing(self):
        rep = H.health_check(make_res(*STRONG[0]), n_trials=12)
        self.assertEqual(rep["inputs"]["n_trials"], 12)
        self.assertEqual(rep["inputs"]["params"], {"n": 2, "m": 2})
        self.assertEqual(rep["inputs"]["days"], 2000)

    def test_does_not_mutate_its_input(self):
        res = make_res(*STRONG[0])
        before = json.dumps(res, sort_keys=True)
        H.health_check(res, 3)
        self.assertEqual(json.dumps(res, sort_keys=True), before)


def synthetic_prices(n=1600, mu=0.0004, sigma=0.011, seed=1):
    import numpy as np
    import pandas as pd
    idx = pd.bdate_range("2014-01-01", periods=n)

    def ohlc(m, sg, sd):
        g = np.random.default_rng(sd)
        c = 100 * np.cumprod(1 + g.normal(m, sg, n))
        o = np.r_[100, c[:-1]] * (1 + g.normal(0, 0.001, n))
        return pd.DataFrame({"Open": o, "High": np.maximum(o, c) * 1.003, "Low": np.minimum(o, c) * 0.997, "Close": c, "Volume": 1e6}, index=idx)
    return {"AAA": ohlc(mu, sigma, seed), "CASH": ohlc(0.00008, 0.0002, seed + 1)}


MCFG = {"universe": {"AAA": "合成"}, "cash": "CASH", "cost_bp": 5, "stock_cost_bp": 8, "main": "AAA"}


class EndToEndTests(unittest.TestCase):
    """用真实的 registry.run(detail=True) 输出（而不是手造的字典）喂给体检——保证两边的字段结构真的对得上。"""

    @classmethod
    def setUpClass(cls):
        from src import registry
        cls.prices = synthetic_prices()
        cls.res = registry.run("trend_ens", cls.prices, "AAA", {}, MCFG, detail=True)
        cls.res_bh = registry.run("buy_hold", cls.prices, "AAA", {}, MCFG, detail=True)

    def test_real_backtest_output_feeds_the_health_check(self):
        rep = H.health_check(self.res, n_trials=3)
        json.dumps(rep, allow_nan=False)
        self.assertEqual([c["id"] for c in rep["checks"]], ["sample", "sharpe_ci", "dsr", "excess", "oos", "cost", "plateau", "mc"])
        self.assertEqual(rep["inputs"]["days"], len(self.res["equity"]) - 1)
        self.assertEqual((rep["inputs"]["strategy"], rep["inputs"]["symbol"]), ("trend_ens", "AAA"))
        for cid in ("oos", "cost", "plateau", "mc", "excess", "sharpe_ci", "dsr"):
            self.assertNotEqual(status(rep, cid), "na", f"{cid} 在真实输出上不应是 na（字段结构没对上？）")

    def test_the_real_structure_has_the_fields_the_module_reads(self):
        a = self.res["analysis"]
        self.assertIn("Trades", self.res["stats"])
        self.assertIsNotNone(a["split"]["is"].get("sharpe"))
        self.assertIsNotNone(a["split"]["oos"].get("sharpe"))
        self.assertTrue({x["label"] for x in a["cost_sens"]} >= {"1x", "2x", "4x"})
        self.assertIn("p_loss", a["mc"])
        self.assertTrue(a["param_sens"] and all("pts" in row for row in a["param_sens"]))

    def test_a_strategy_without_parameters_gets_na_for_plateau_not_a_crash(self):
        rep = H.health_check(self.res_bh, n_trials=1)
        self.assertEqual(status(rep, "plateau"), "na")
        json.dumps(rep, allow_nan=False)

    def test_trial_count_changes_only_the_dsr_related_outcome_not_the_other_checks(self):
        a, b = H.health_check(self.res, 1), H.health_check(self.res, 10 ** 6)
        for cid in ("sample", "sharpe_ci", "excess", "oos", "cost", "plateau", "mc"):
            self.assertEqual(status(a, cid), status(b, cid), cid)
        self.assertGreaterEqual(value(a, "dsr")["dsr"], value(b, "dsr")["dsr"])

    def test_two_years_of_noise_is_not_called_evidence(self):
        from src import registry
        noise = synthetic_prices(n=1600, mu=0.0, sigma=0.011, seed=9)
        res = registry.run("trend_ens", noise, "AAA", {}, MCFG, detail=True)
        self.assertNotEqual(H.health_check(res, 20)["verdict"]["code"], "passes_checks")


class CodexR1R2R4Tests(unittest.TestCase):
    """T022 Codex 审核 R1/R2/R4 的回归用例：全部合成数据。"""

    def test_missing_core_evidence_never_passes(self):
        # 没有基准 → excess 为 na，其余全好：不能给"通过"
        rep = H.health_check(make_res(*STRONG[0]))
        self.assertEqual(rep["verdict"]["code"], "passes_checks")          # 对照：有基准时可以
        res = make_res(STRONG[0][0], None)
        rep = H.health_check(res)
        self.assertEqual(status(rep, "excess"), "na")
        self.assertEqual(rep["verdict"]["code"], "incomplete")
        self.assertIn("excess", rep["verdict"]["missing_core"])

    def test_missing_oos_blocks_pass(self):
        rep = H.health_check(make_res(*STRONG[0], split=None))
        self.assertEqual(status(rep, "oos"), "na")
        self.assertEqual(rep["verdict"]["code"], "incomplete")

    def test_noncore_na_does_not_block(self):
        rep = H.health_check(make_res(*STRONG[0], mc=None))
        self.assertEqual(status(rep, "mc"), "na")
        self.assertEqual(rep["verdict"]["code"], "passes_checks")

    def test_plateau_centre_comes_from_params_not_list_position(self):
        # 预算截断后只剩 v=10,15,20，真实当前参数是 20（最后一个）；按位置取中点会把 15 当中心而误判通过
        rows = [{"key": "n", "pts": [{"v": 10, "sharpe": 0.1}, {"v": 15, "sharpe": 0.1}, {"v": 20, "sharpe": 10.0}]}]
        chk = H._check_plateau(rows, {"n": 20})
        self.assertEqual((chk["status"], chk["value"]["worst_ratio"]), ("fail", 0.0))

    def test_plateau_missing_centre_is_na(self):
        rows = [{"key": "n", "pts": [{"v": 1, "sharpe": 1.0}, {"v": 2, "sharpe": 1.0}, {"v": 4, "sharpe": 1.0}, {"v": 5, "sharpe": 1.0}]}]
        chk = H._check_plateau(rows, {"n": 3})
        self.assertEqual(chk["status"], "na")
        self.assertEqual(chk["value"]["uncovered"], ["n"])
        self.assertEqual(H._check_plateau(rows, None)["status"], "na")

    def test_plateau_invalid_centre_sharpe_is_uncovered(self):
        rows = [{"key": "n", "pts": [{"v": i, "sharpe": float("nan") if i == 2 else 1.0} for i in range(5)]}]
        self.assertEqual(H._check_plateau(rows, {"n": 2})["status"], "na")

    def test_plateau_invalid_neighbour_counts_as_not_kept(self):
        rows = [{"key": "n", "pts": [{"v": i, "sharpe": (float("nan") if i in (0, 1) else 1.0)} for i in range(5)]}]
        chk = H._check_plateau(rows, {"n": 2})
        self.assertEqual(chk["value"]["worst_ratio"], 0.5)

    def test_plateau_duplicate_boundary_values_count_once(self):
        # 边界截断：v=0,0,0,1,2 → 去重后中心 0 只有 2 个邻点
        pts = [{"v": 0, "sharpe": 1.0}, {"v": 0, "sharpe": 1.0}, {"v": 0, "sharpe": 1.0}, {"v": 1, "sharpe": 1.0}, {"v": 2, "sharpe": 1.0}]
        chk = H._check_plateau([{"key": "n", "pts": pts}], {"n": 0})
        self.assertEqual(chk["value"]["params"][0]["n_neighbours"], 2)
        # 只剩中心 + 1 个不同邻点：不够
        pts2 = [{"v": 0, "sharpe": 1.0}, {"v": 0, "sharpe": 1.0}, {"v": 1, "sharpe": 1.0}]
        self.assertEqual(H._check_plateau([{"key": "n", "pts": pts2}], {"n": 0})["status"], "na")

    def test_whole_row_missing_from_budget_truncation_is_uncovered(self):
        # 预期三个参数，上游预算耗尽只返回第一个：另外两个整行缺失，不能假通过
        rows = [{"key": "target_vol", "pts": [{"v": i, "sharpe": 1.0} for i in range(5)]}]
        chk = H._check_plateau(rows, {"target_vol": 2}, ["target_vol", "vol_len", "buffer"])
        self.assertEqual(chk["status"], "warn")
        self.assertEqual(chk["value"]["uncovered"], ["vol_len", "buffer"])

    def test_no_expected_keys_means_no_row_check(self):
        rows = [{"key": "n", "pts": [{"v": i, "sharpe": 1.0} for i in range(5)]}]
        self.assertEqual(H._check_plateau(rows, {"n": 2}, None)["status"], "pass")

    def test_all_expected_rows_present_passes(self):
        rows = [{"key": k, "pts": [{"v": i, "sharpe": 1.0} for i in range(5)]} for k in ("a", "b")]
        self.assertEqual(H._check_plateau(rows, {"a": 2, "b": 2}, ["a", "b"])["status"], "pass")

    def test_health_check_passes_registry_spec_keys_for_known_strategies(self):
        # 真实登记的策略：用 registry 里的参数清单；未登记的策略名返回 None
        self.assertIsNone(H._expected_param_keys({"strategy": "no_such_strategy"}))
        self.assertTrue(H._expected_param_keys({"strategy": "trend_ens"}))

    def test_known_core_failure_is_not_hidden_by_three_nas(self):
        ids = ["sample", "sharpe_ci", "dsr", "excess", "oos", "cost", "plateau", "mc"]
        sts = {k: "pass" for k in ids}
        sts.update({"dsr": "fail", "cost": "na", "plateau": "na", "mc": "na"})
        v = H._verdict([{"id": k, "status": sts[k]} for k in ids])
        self.assertEqual(v["code"], "no_evidence")
        self.assertEqual(v["fails"], ["dsr"])

    def test_three_nas_without_core_failure_is_still_incomplete(self):
        ids = ["sample", "sharpe_ci", "dsr", "excess", "oos", "cost", "plateau", "mc"]
        sts = {k: "pass" for k in ids}
        sts.update({"cost": "na", "plateau": "na", "mc": "na"})
        self.assertEqual(H._verdict([{"id": k, "status": sts[k]} for k in ids])["code"], "incomplete")

    def test_partial_coverage_caps_at_warn(self):
        good = {"key": "n", "pts": [{"v": i, "sharpe": 1.0} for i in range(5)]}
        lost = {"key": "m", "pts": [{"v": 9, "sharpe": 1.0}]}
        chk = H._check_plateau([good, lost], {"n": 2, "m": 5})
        self.assertEqual(chk["status"], "warn")
        self.assertEqual(chk["value"]["uncovered"], ["m"])

    def test_invalid_probability_is_na(self):
        for bad in (-10, 150, float("nan"), True):
            with self.subTest(p=bad):
                self.assertEqual(H._check_mc({"p_loss": bad})["status"], "na")
        self.assertEqual(H._check_mc({"p_loss": 0})["status"], "pass")
        self.assertEqual(H._check_mc({"p_loss": 100})["status"], "fail")

    def test_num_rejects_bool_and_huge_ints(self):
        self.assertIsNone(H._num(True))
        self.assertIsNone(H._num(10 ** 400))
        self.assertEqual(H._num(3), 3.0)

    def test_nonpositive_or_nonfinite_equity_is_rejected_not_dropped(self):
        for bad in ([1, 2, 0, 3], [1, -2, 3], [1, float("nan"), 3], [1, float("inf"), 3]):
            with self.subTest(e=bad):
                with self.assertRaises(ValueError):
                    H._returns(bad)

    def test_invalid_benchmark_is_treated_as_missing(self):
        res = make_res(*STRONG[0])
        res["bench"][5] = -1.0
        self.assertEqual(status(H.health_check(res), "excess"), "na")

    def test_nested_nan_in_params_still_serialises(self):
        import json
        res = make_res(*STRONG[0])
        res["params"] = {"n": 2, "m": 2, "nested": {"a": float("nan"), "b": [float("inf"), 1]}}
        rep = H.health_check(res)
        json.dumps(rep, allow_nan=False)
        self.assertIsNone(rep["inputs"]["params"]["nested"]["a"])

    def test_mc_text_uses_returned_n_and_block(self):
        c = H._check_mc({"p_loss": 10.0, "n": 500, "block": 5})
        self.assertIn("500", c["zh"])
        self.assertIn("5 天一块", c["zh"])
        self.assertIn("有放回", c["zh"])
        self.assertNotIn("重排", c["zh"])

    def test_mixed_verdict_never_suggests_lowering_trial_count(self):
        self.assertNotIn("更少的试验次数", H.VERDICTS["mixed"][2])

    def test_dsr_caveat_does_not_claim_conservative(self):
        zh = " ".join(c[0] for c in H.CAVEATS)
        self.assertNotIn("所以偏保守", zh)


class CliValidationTests(unittest.TestCase):
    """命令行参数错误：返回 2，且在读行情/回测之前就拒绝（把读数据的函数换成一碰就报错的桩）。"""

    def _run(self, argv):
        import contextlib
        import io
        from unittest import mock
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
        import strategy_health_check as cli
        err = io.StringIO()
        with mock.patch.object(cli, "_prices", side_effect=AssertionError("不该读行情")), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, err.getvalue()

    def test_bad_n_trials_returns_2_before_loading_data(self):
        code, err = self._run(["--market", "cn", "--strategy", "trend_ens", "--n-trials", "0"])
        self.assertEqual(code, 2)
        self.assertIn("n-trials", err)

    def test_bad_cost_returns_2_before_loading_data(self):
        for c in ("-1", "nan", "inf"):
            with self.subTest(cost=c):
                code, _ = self._run(["--market", "cn", "--strategy", "trend_ens", "--n-trials", "3", "--cost", c])
                self.assertEqual(code, 2)


class BoundaryTests(unittest.TestCase):
    """每一条阈值线的两侧都要测：直接调用各检查函数，用受控的输入（必要时 patch 统计函数的返回值），
    这样把"通过线"悄悄放宽/收紧一点点的改动会立刻被发现，而不是被"远高于/远低于"的粗测试放过。"""

    def test_sample_boundaries(self):
        cases = [((503, 100), "fail"), ((504, 29), "fail"), ((504, 30), "pass"), ((504, None), "na"), ((10, 1000), "fail")]
        for (days, trades), want in cases:
            with self.subTest(days=days, trades=trades):
                self.assertEqual(H._check_sample(days, trades)["status"], want)

    def test_sharpe_ci_status_rules(self):
        from unittest.mock import patch
        rows = [({"point": 1.0, "lower": 0.01, "upper": 2.0}, "pass"), ({"point": 1.0, "lower": 0.0, "upper": 2.0}, "warn"),
                ({"point": 1.0, "lower": -0.5, "upper": 2.0}, "warn"), ({"point": -0.2, "lower": -1.0, "upper": 0.5}, "fail"),
                ({"point": 0.0, "lower": -1.0, "upper": 1.0}, "fail"), ({"point": float("nan"), "lower": float("nan"), "upper": float("nan")}, "na")]
        for ci, want in rows:
            with self.subTest(ci=ci), patch.object(H.tr, "sharpe_ci", return_value=ci):
                self.assertEqual(H._check_sharpe_ci([0.01] * 100, ci["point"])["status"], want)

    def test_dsr_threshold_lines(self):
        from unittest.mock import patch
        rows = [(0.96, "pass"), (0.95, "pass"), (0.9499, "warn"), (0.80, "warn"), (0.7999, "fail"), (0.0, "fail"), (float("nan"), "na")]
        for v, want in rows:
            with self.subTest(dsr=v), patch.object(H.tr, "deflated_sharpe", return_value=v):
                self.assertEqual(H._check_dsr(0.08, 2000, 0.0, 3.0, 5)["status"], want)

    def test_excess_threshold_lines(self):
        from unittest.mock import patch
        r, b = synth(300, 0.001, 0.01, 1), synth(300, 0.0, 0.01, 2)
        rows = [(0.96, "pass"), (0.95, "pass"), (0.9499, "warn"), (0.80, "warn"), (0.7999, "fail"), (float("nan"), "na")]
        for v, want in rows:
            with self.subTest(psr=v), patch.object(H.tr, "prob_sharpe_gt", return_value=v):
                self.assertEqual(H._check_excess(r, b)["status"], want)

    def test_oos_threshold_lines(self):
        rows = [((1.0, 0.5), "pass"), ((1.0, 0.49), "warn"), ((1.0, 0.01), "warn"), ((1.0, 0.0), "fail"), ((1.0, -0.1), "fail"),
                ((2.0, 1.0), "pass"), ((2.0, 0.99), "warn"), ((-0.5, 0.3), "pass"), ((None, 1.0), "na"), ((1.0, None), "na")]
        for (i, o), want in rows:
            with self.subTest(is_=i, oos=o):
                self.assertEqual(H._check_oos({"is": {"sharpe": i}, "oos": {"sharpe": o}})["status"], want)

    def test_cost_threshold_lines(self):
        def cs(s2, s4):
            return [{"label": "1x", "sharpe": 1.0}, {"label": "2x", "sharpe": s2}, {"label": "4x", "sharpe": s4}]
        rows = [((0.01, 0.5), "pass"), ((0.01, 0.0), "warn"), ((0.01, -0.1), "warn"), ((0.0, 0.5), "fail"), ((-0.1, -0.5), "fail"),
                ((0.01, None), "pass"), ((None, 0.5), "na")]
        for (a, b), want in rows:
            with self.subTest(s2=a, s4=b):
                self.assertEqual(H._check_cost(cs(a, b))["status"], want)
        self.assertEqual(H._check_cost(None)["status"], "na")

    def test_plateau_threshold_lines_and_keep_rule(self):
        def row(center, neighbours):                                  # 5 个点：两侧各两个邻点，中间是中心点
            vals = [neighbours[0], neighbours[1], center, neighbours[2], neighbours[3]]
            return [{"key": "n", "pts": [{"v": i, "sharpe": v} for i, v in enumerate(vals)]}]
        rows = [((1.0, [1, 1, 1, 1]), 1.0, "pass"), ((1.0, [1, 1, 1, 0]), 0.75, "pass"), ((1.0, [1, 1, 0, 0]), 0.5, "warn"),
                ((1.0, [1, 0, 0, 0]), 0.25, "fail"), ((1.0, [0, 0, 0, 0]), 0.0, "fail")]
        for (c, n), ratio, want in rows:
            with self.subTest(center=c, neigh=n):
                chk = H._check_plateau(row(c, n), {"n": 2})
                self.assertEqual((chk["status"], chk["value"]["worst_ratio"]), (want, ratio))
        # "保留 50%" 的线：邻点 0.5×中心 算保留，0.49×中心不算；中心为负则一个都不保留
        self.assertEqual(H._check_plateau(row(1.0, [0.5, 0.5, 0.5, 0.5]), {"n": 2})["value"]["worst_ratio"], 1.0)
        self.assertEqual(H._check_plateau(row(1.0, [0.49, 0.49, 0.49, 0.49]), {"n": 2})["value"]["worst_ratio"], 0.0)
        self.assertEqual(H._check_plateau(row(-1.0, [1, 1, 1, 1]), {"n": 2})["value"]["worst_ratio"], 0.0)
        self.assertEqual(H._check_plateau(None)["status"], "na")
        self.assertEqual(H._check_plateau([{"key": "n", "pts": [{"v": 1, "sharpe": 1.0}]}], {"n": 1})["status"], "na")      # 点太少

    def test_monte_carlo_threshold_lines(self):
        rows = [(0.0, "pass"), (20.0, "pass"), (20.1, "warn"), (40.0, "warn"), (40.1, "fail"), (None, "na")]
        for p, want in rows:
            with self.subTest(p_loss=p):
                self.assertEqual(H._check_mc({"p_loss": p} if p is not None else None)["status"], want)

    def test_n_trials_validation_is_independent_of_downstream_functions(self):
        """样本太短时不会走到 DSR，所以这里抛错只能来自 health_check 自己的校验（True 不能被当成整数 1）。"""
        short = make_res(synth(20, 0.001, 0.01, 1))
        for bad in (True, False, 0, -3, 1.5, "2", None):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    H.health_check(short, n_trials=bad)
        self.assertEqual(H.health_check(short, n_trials=1)["verdict"]["code"], "insufficient_sample")


class WordingGuardTests(unittest.TestCase):
    FORBIDDEN_ZH = ("推荐", "建议买", "建议卖", "买入信号", "卖出信号", "稳赚", "保证", "必涨", "低风险", "高收益", "跑赢大盘", "有效策略", "值得投资")
    FORBIDDEN_EN = ("recommend", "guarantee", "must buy", "should buy", "sure profit", "risk-free", "will profit", "works!")

    def all_texts(self):
        texts = []
        for r, b in STRONG[:1] + ZERO[:1] + NEG[:1]:
            for n in (1, 50):
                rep = H.health_check(make_res(r, b), n_trials=n)
                texts += [c["zh"] for c in rep["checks"]] + [c["en"] for c in rep["checks"]]
                texts += [rep["verdict"]["zh"], rep["verdict"]["en"], rep["verdict"]["title_zh"], rep["verdict"]["title_en"]]
                texts += [x["zh"] for x in rep["caveats"]] + [x["en"] for x in rep["caveats"]]
        for code, (zh_t, en_t, zh) in H.VERDICTS.items():
            texts += [zh_t, en_t, zh]
        return texts

    def test_no_promotional_or_advisory_wording_anywhere(self):
        for t in self.all_texts():
            for w in self.FORBIDDEN_ZH:
                self.assertNotIn(w, t, w)
            for w in self.FORBIDDEN_EN:
                self.assertNotIn(w, t.lower(), w)

    def test_every_verdict_carries_the_not_advice_disclaimer_and_the_passing_one_says_it_is_not_proof(self):
        for r, b in STRONG[:1] + ZERO[:1]:
            rep = H.health_check(make_res(r, b))
            self.assertIn("不构成投资建议", rep["verdict"]["zh"])
            self.assertIn("not investment advice", rep["verdict"]["en"])
        passing = H.health_check(make_res(*STRONG[0]))["verdict"]
        self.assertEqual(passing["code"], "passes_checks")
        self.assertIn("不是策略有效的证明", passing["zh"])
        self.assertIn("不代表有效", passing["title_zh"])

    def test_caveats_always_mention_the_unverifiable_trial_count(self):
        rep = H.health_check(make_res(*STRONG[0]))
        self.assertTrue(any("n_trials" in c["zh"] for c in rep["caveats"]))


if __name__ == "__main__":
    unittest.main()
