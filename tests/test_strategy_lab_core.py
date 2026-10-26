"""strategy_lab / strategy_lab_ext 核心纯函数测试（T023 复审后补）：只用合成数据和临时目录，不读 data/，不导入 app。

覆盖：夏普之差与信息比率的区分、BH 手算与异常 p 值、因子标签结束日清除、滚动前进的训练/检验隔离、
扩展脚本的全局配置恢复与旧版 factors.json 拒绝。
"""
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import lab_import_guard as guard  # noqa: E402

L, E = guard.import_guarded("strategy_lab", "strategy_lab_ext")
fx = L.fx


class ImportIsolationTests(unittest.TestCase):
    def test_importing_the_lab_scripts_never_read_real_data(self):
        # config 导入时会尝试读 data/auto_params.json，被拦截层拦下（读不到就用默认值）；除它之外不应有别的读取尝试
        self.assertLessEqual(set(guard.BLOCKED), {"data/auto_params.json"})
        self.assertFalse(any("app" == m for m in sys.modules))        # 没有导入 app


class MetricTests(unittest.TestCase):
    def setUp(self):
        rng = np.random.default_rng(3)
        self.b = rng.normal(0.0008, 0.01, 400)

    def test_half_position_has_zero_sharpe_gap_but_negative_information_ratio(self):
        r = 0.5 * self.b
        self.assertAlmostEqual(L.ann_sharpe(r) - L.ann_sharpe(self.b), 0.0)
        p, ir = L.excess_p(r, self.b)
        self.assertLess(ir, 0)                      # 主动收益序列的夏普为负
        self.assertGreater(p, 0.5)                  # 单侧 p：没有证据说超额>0

    def test_excess_p_returns_the_annualised_active_sharpe_not_a_difference(self):
        # r 与 b 不成比例（含独立噪音），否则 r-b 的夏普对缩放不变，改坏了也看不出来
        r = 0.7 * self.b + np.random.default_rng(11).normal(0.0003, 0.006, len(self.b))
        _, ir = L.excess_p(r, self.b)
        x = r - self.b
        self.assertAlmostEqual(ir, x.mean() / x.std(ddof=1) * math.sqrt(252))
        self.assertNotAlmostEqual(ir, L.ann_sharpe(r) - L.ann_sharpe(self.b), places=2)

    def test_short_or_flat_inputs_give_no_result(self):
        self.assertEqual(L.excess_p(self.b[:30], self.b[:30])[0], None)
        flat = np.zeros(200)
        p, ir = L.excess_p(flat, flat)
        self.assertIsNone(p)
        self.assertTrue(math.isnan(ir))
        self.assertTrue(math.isnan(L.ann_sharpe(flat)))

    def test_rets_drops_first_day(self):
        self.assertEqual(len(L.rets([1.0, 1.1, 1.21])), 2)


class BHTests(unittest.TestCase):
    def test_hand_computed_example(self):
        # q=0.05, m=3：阈值 0.0167 / 0.0333 / 0.05 → 前两个通过
        self.assertEqual(L.bh_fdr([0.001, 0.02, 0.8], q=0.05), [True, True, False])

    def test_step_up_marks_all_before_the_largest_passing_rank(self):
        # 0.04 本身 > 0.05*1/3，但排第 2 且 0.04 <= 0.05*2/3=0.0333? 不成立；用能体现 step-up 的组合：
        # p=[0.02, 0.0301, 0.0302], q=0.05, m=3：阈值 .0167,.0333,.05 → 第 3 个通过，因此前面全部通过
        self.assertEqual(L.bh_fdr([0.02, 0.0301, 0.0302], q=0.05), [True, True, True])

    def test_threshold_lines_use_k_over_m_exactly(self):
        # m=3, q=0.05：第 1 名的线是 0.05/3=0.016667；0.0166 过、0.0168 不过（分母错一位就会翻转）
        self.assertEqual(L.bh_fdr([0.0166, 0.9, 0.9], q=0.05), [True, False, False])
        self.assertEqual(L.bh_fdr([0.0168, 0.9, 0.9], q=0.05), [False, False, False])
        # 第 2 名的线是 0.05*2/3=0.033333
        self.assertEqual(L.bh_fdr([0.001, 0.0333, 0.9], q=0.05), [True, True, False])
        self.assertEqual(L.bh_fdr([0.9, 0.0334, 0.9], q=0.05), [False, False, False])

    def test_equality_on_the_threshold_counts_as_significant(self):
        # BH 是 p <= q*k/m：恰好等于阈值要通过（把 <= 改成 < 会翻转）
        self.assertEqual(L.bh_fdr([0.1], q=0.1), [True])
        self.assertEqual(L.bh_fdr([0.05, 0.1], q=0.1), [True, True])
        self.assertEqual(L.bh_fdr([0.5, 0.1], q=0.1), [False, False])      # 第 2 名 0.5 不过；第 1 名 0.1 > 0.1*1/2

    def test_default_q_is_the_registered_ten_percent(self):
        self.assertEqual(L.FDR_Q, 0.10)

    def test_none_is_excluded_from_family_and_never_significant(self):
        out = L.bh_fdr([0.001, None, 0.02, 0.8], q=0.05)
        self.assertEqual(out, [True, False, True, False])      # 分母 m=3（None 不计）

    def test_order_independent(self):
        self.assertEqual(L.bh_fdr([0.8, 0.02, 0.001], q=0.05), [False, True, True])

    def test_invalid_p_values_are_rejected(self):
        for bad in (float("nan"), -0.1, 1.5, float("inf"), True, "0.01"):
            with self.subTest(p=bad):
                with self.assertRaises(ValueError):
                    L.bh_fdr([0.01, bad])

    def test_all_none_or_empty(self):
        self.assertEqual(L.bh_fdr([]), [])
        self.assertEqual(L.bh_fdr([None, None]), [False, False])


class FactorLabelBoundaryTests(unittest.TestCase):
    """标签 = t+1 收盘到 t+1+h 收盘；用实际交易日索引算结束日，发现期结束日必须严格早于分界 2022-01-01。"""

    def setUp(self):
        self.idx = pd.bdate_range("2021-12-20", "2022-01-20")      # 2021-12-31 是周五，下一个交易日是 2022-01-03

    def test_label_end_uses_trading_day_positions_not_calendar_days(self):
        d = pd.Timestamp("2021-12-30")
        self.assertEqual(L.label_end(self.idx, d, 1), pd.Timestamp("2022-01-03"))      # +2 个交易日，跨过周末
        self.assertEqual(L.label_end(self.idx, pd.Timestamp("2021-12-29"), 1), pd.Timestamp("2021-12-31"))

    def test_label_end_none_past_the_data(self):
        self.assertIsNone(L.label_end(self.idx, self.idx[-1], 5))

    def test_discovery_label_that_ends_in_2022_is_purged(self):
        d = pd.Timestamp("2021-12-30")
        end = L.label_end(self.idx, d, 1)
        self.assertEqual(L.label_segment(d, end), "purged")

    def test_discovery_label_ending_before_the_split_is_kept(self):
        d = pd.Timestamp("2021-12-29")
        self.assertEqual(L.label_segment(d, L.label_end(self.idx, d, 1)), "disc")

    def test_longer_horizon_purges_more_days(self):
        d = pd.Timestamp("2021-12-23")
        self.assertEqual(L.label_segment(d, L.label_end(self.idx, d, 1)), "disc")
        self.assertEqual(L.label_segment(d, L.label_end(self.idx, d, 20)), "purged")

    def test_signal_on_or_after_split_is_confirmation(self):
        for s in ("2022-01-03", "2022-01-10"):
            d = pd.Timestamp(s)
            self.assertEqual(L.label_segment(d, L.label_end(self.idx, d, 5)), "conf")

    def test_unknown_end_before_split_is_purged_not_kept(self):
        self.assertEqual(L.label_segment(pd.Timestamp("2021-06-01"), None), "purged")
        self.assertEqual(L.label_segment(pd.Timestamp("2021-06-01"), pd.NaT), "purged")

    def test_split_is_the_prereg_constant(self):
        self.assertEqual(L.FACTOR_SPLIT, "2022-01-01")
        self.assertGreaterEqual(L.FACTOR_BOUNDARY_VERSION, 2)


def _synth_panel(n_stocks=130, start="2021-09-01", end="2022-03-31", seed=0):
    idx = pd.bdate_range(start, end)
    rng = np.random.default_rng(seed)
    close = pd.DataFrame(100 * np.cumprod(1 + rng.normal(0, 0.01, (len(idx), n_stocks)), axis=0), index=idx,
                         columns=[f"{i:06d}" for i in range(n_stocks)])
    vol = pd.DataFrame(1e6, index=idx, columns=close.columns)
    names = pd.Series("x", index=close.columns)
    return fx.Panel(close, close * 1.01, close * 0.99, close, vol, names, names)


def _expected_counts(idx, h, gap, split="2022-01-01"):
    """独立于被测代码，按交易日位置算每个检验应有的发现/确认/清除样本数。"""
    sp = pd.Timestamp(split)
    disc = conf = purged = 0
    for d in idx[::gap]:
        j = idx.get_loc(d) + 1 + h
        if d >= sp:
            conf += j < len(idx)
        elif j >= len(idx) or idx[j] >= sp:
            purged += 1
        else:
            disc += 1
    return disc, conf, purged


class FactorWiringTests(unittest.TestCase):
    """用合成面板跑真实的 stage_factors / e4_composite，确认边界清除真的接到了主流程（只改辅助函数、不接线的变异会被抓住）。"""

    def _metas(self, *ids):
        from types import SimpleNamespace
        return {i: SimpleNamespace(kind="score", category="t", direction=1) for i in ids}

    def test_main_factor_stage_purges_crossing_labels_and_records_version(self):
        panel = _synth_panel()
        F = pd.DataFrame(np.random.default_rng(1).normal(size=panel.close.shape), index=panel.close.index, columns=panel.close.columns)
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(L, "build_factor_panel", return_value=panel), \
                mock.patch.object(fx, "FACTORS", self._metas("tf")), \
                mock.patch.object(fx, "check_no_lookahead"), \
                mock.patch.object(fx, "compute_factor", return_value=F):
            out = Path(td)
            res = L.stage_factors(out, ["x"])
            saved = json.loads((out / "factors.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["boundary_version"], L.FACTOR_BOUNDARY_VERSION)
        idx = panel.close.index
        self.assertEqual(len(res["results"]), len(L.HORIZONS))
        for r in res["results"]:
            disc, conf, purged = _expected_counts(idx, r["h"], L.HORIZONS[r["h"]])
            self.assertEqual((r["disc"]["n"], r["conf"]["n"], r["n_purged"]), (disc, conf, purged), f"h={r['h']}")
        self.assertTrue(any(r["n_purged"] > 0 for r in res["results"]))          # 合成数据里确实有跨界样本

    def test_e4_composite_uses_the_same_boundary_rule(self):
        panel = _synth_panel(seed=2)
        rng = np.random.default_rng(3)
        mk = lambda: pd.DataFrame(rng.normal(size=panel.close.shape), index=panel.close.index, columns=panel.close.columns)
        frames = {"f1": mk(), "f2": mk()}
        fres = [{"factor": f, "kind": "score", "h": 5, "fdr_significant_disc": True, "disc": {"mean": 0.05}, "conf": {"mean": 0.04}} for f in frames]
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(L, "build_factor_panel", return_value=panel), \
                mock.patch.object(fx, "FACTORS", self._metas("f1", "f2")), \
                mock.patch.object(fx, "compute_factor", side_effect=lambda fid, p: frames[fid]):
            main, out = Path(td) / "main", Path(td) / "out"
            main.mkdir()
            out.mkdir()
            (main / "factors.json").write_text(json.dumps({"boundary_version": L.FACTOR_BOUNDARY_VERSION, "results": fres}), encoding="utf-8")
            res = E.e4_composite(out, main, ["x"])
        idx = panel.close.index
        for h, gap in ((5, 5), (20, 20)):
            disc, conf, _ = _expected_counts(idx, h, gap)
            got = res["composite"][f"h{h}"]
            self.assertEqual((got["disc"]["n"], got["conf"]["n"]), (disc, conf), f"h={h}")


class WalkForwardTests(unittest.TestCase):
    TRAIN, TEST = 60, 20

    def _frame(self, n, seed=0):
        rng = np.random.default_rng(seed)
        idx = pd.bdate_range("2020-01-01", periods=n)
        return pd.DataFrame(rng.normal(0, 0.01, (n, 3)), index=idx, columns=["A", "B", "C"])

    def _run(self, R, D=None):
        D = D if D is not None else pd.Series(0.0, index=R.index)
        with mock.patch.object(L, "TRAIN_D", self.TRAIN), mock.patch.object(L, "TEST_D", self.TEST):
            return L.walk_forward(R, D)

    def test_fold_count_and_windows_do_not_overlap(self):
        n = self.TRAIN + 3 * self.TEST
        folds, sel, dflt, te_all = self._run(self._frame(n))
        self.assertEqual(len(folds), 3)
        self.assertEqual([f["fold_start"] for f in folds], [0, self.TEST, 2 * self.TEST])
        for a, b in zip(te_all, te_all[1:]):
            self.assertLess(a.index[-1], b.index[0])               # 检验窗彼此不重叠
        self.assertEqual(sum(len(t) for t in te_all), 3 * self.TEST)

    def test_selection_uses_only_the_training_window(self):
        # A 在训练窗好、检验窗差；B 相反。训练窗选出的必须是 A，哪怕检验窗里 B 好得多
        n = self.TRAIN + self.TEST
        R = self._frame(n)
        R.iloc[:self.TRAIN, 0] = 0.004 + R.iloc[:self.TRAIN, 0] * 0.1
        R.iloc[:self.TRAIN, 1] = -0.004 + R.iloc[:self.TRAIN, 1] * 0.1
        R.iloc[self.TRAIN:, 0] = -0.01
        R.iloc[self.TRAIN:, 1] = 0.01 + R.iloc[self.TRAIN:, 1] * 0.1
        folds, sel, _, _ = self._run(R)
        self.assertEqual(folds[0]["best"], "A")
        np.testing.assert_allclose(sel[0], R.iloc[self.TRAIN:, 0].values)
        self.assertLess(folds[0]["oos_selected"], folds[0]["oos_grid_best"])

    def test_changing_test_returns_never_changes_the_choice(self):
        n = self.TRAIN + 2 * self.TEST
        R = self._frame(n, seed=5)
        base, _, _, _ = self._run(R)
        R2 = R.copy()
        R2.iloc[self.TRAIN:, :] = np.random.default_rng(99).normal(0.05, 0.02, (2 * self.TEST, 3))     # 把检验窗换成别的
        changed, _, _, _ = self._run(R2)
        self.assertEqual(base[0]["best"], changed[0]["best"])                 # 第一折选参只依赖训练窗

    def test_poison_in_test_window_cannot_leak_into_selection(self):
        n = self.TRAIN + self.TEST
        R = self._frame(n, seed=8)
        R.iloc[self.TRAIN:, 2] = 0.5                                            # C 在检验窗里“爆炸”
        folds, _, _, _ = self._run(R)
        self.assertEqual(folds[0]["best"], self._run(self._frame(n, seed=8))[0][0]["best"])

    def test_fold_with_all_nan_training_sharpe_is_skipped(self):
        n = self.TRAIN + self.TEST
        R = pd.DataFrame(0.0, index=pd.bdate_range("2020-01-01", periods=n), columns=["A", "B", "C"])    # 零波动 → 夏普 NaN
        folds, sel, _, te_all = self._run(R)
        self.assertEqual((len(folds), len(sel), len(te_all)), (0, 0, 0))

    def test_too_short_gives_no_folds(self):
        folds, *_ = self._run(self._frame(self.TRAIN + self.TEST - 1))
        self.assertEqual(folds, [])


class ExtTests(unittest.TestCase):
    def test_with_patch_restores_globals_even_on_error(self):
        old = (L.TRAIN_D, L.TEST_D, L.GRIDS)

        def boom():
            self.assertEqual((L.TRAIN_D, L.TEST_D), (11, 7))
            raise RuntimeError("x")
        with self.assertRaises(RuntimeError):
            E.with_patch(11, 7, {"rsi2": {}}, boom)
        self.assertEqual((L.TRAIN_D, L.TEST_D, L.GRIDS), old)

    def test_with_patch_keeps_grids_when_none(self):
        old = L.GRIDS
        E.with_patch(11, 7, None, lambda: self.assertIs(L.GRIDS, old))
        self.assertIs(L.GRIDS, old)

    def test_e4_refuses_an_old_factors_json(self):
        with tempfile.TemporaryDirectory() as td:
            main, out = Path(td) / "main", Path(td) / "out"
            main.mkdir()
            out.mkdir()
            (main / "factors.json").write_text(json.dumps({"results": []}), encoding="utf-8")        # 没有 boundary_version = 旧版
            res = E.e4_composite(out, main, [])
            self.assertIn("旧版", res["error"])
            self.assertFalse((out / "e4_composite.json").exists())

    def test_e4_new_version_but_too_few_factors_stops_before_loading_data(self):
        with tempfile.TemporaryDirectory() as td:
            main, out = Path(td) / "main", Path(td) / "out"
            main.mkdir()
            out.mkdir()
            (main / "factors.json").write_text(json.dumps({"boundary_version": L.FACTOR_BOUNDARY_VERSION, "results": []}), encoding="utf-8")
            res = E.e4_composite(out, main, [])
            self.assertIn("不足 2 个", res["error"])


if __name__ == "__main__":
    unittest.main()
