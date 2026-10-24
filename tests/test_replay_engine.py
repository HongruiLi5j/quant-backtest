"""T008 S3:回放引擎测试。只用合成面板与临时目录,不读 data/,不联网。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.replay import engine as E  # noqa: E402
from src.replay import factors  # noqa: E402
from src.validation.costs import trade_fees  # noqa: E402
from unittest.mock import patch  # noqa: E402

N_STOCKS, N_DAYS = 24, 130
MOM_P = next(factors.iter_param_grid("mom_01"))
STRATS = [{"id": "mom_01", "params": MOM_P},
          {"id": "rev_01", "params": next(factors.iter_param_grid("rev_01"))},
          {"id": "ctrl_rand_01", "params": {}}]


def make_panel(seed=3, n_days=N_DAYS, n_stocks=N_STOCKS):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2023-10-02", periods=n_days)
    panel = {}
    for k in range(n_stocks):
        r = rng.normal(0.0005, 0.02, n_days)
        c = 10 * (1 + k % 5) * np.exp(np.cumsum(r))
        o = np.r_[c[0], c[:-1]] * (1 + rng.normal(0, 0.004, n_days))
        panel[f"S{k:02d}"] = pd.DataFrame({
            "open": o, "high": np.maximum(o, c) * 1.01, "low": np.minimum(o, c) * 0.99, "close": c,
            "volume": rng.uniform(1e5, 1e6, n_days) * (k + 1)}, index=idx)
    return panel


def run(panel, out, start="2024-01-02", end="2024-03-29", **kw):
    kw.setdefault("strategies", STRATS)
    kw.setdefault("top_n", 5)
    return E.run_replay(panel, start, end, out, **kw)


class TestEngine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.panel = make_panel()

    def tearDown(self):
        self.tmp.cleanup()

    def test_deterministic_and_both_backends_agree(self):
        a, b, c = self.d / "a.sqlite", self.d / "b.sqlite", self.d / "c.jsonl"
        run(self.panel, a)
        run(self.panel, b)
        run(self.panel, c)
        ra, rb, rc = E.load_rows(a), E.load_rows(b), E.load_rows(c)
        self.assertTrue(ra)
        self.assertEqual(ra, rb)
        self.assertEqual(ra, rc)

    def test_hand_computed_equal_weight_first_day(self):
        out = self.d / "h.jsonl"
        run(self.panel, out, end="2024-01-02", strategies=[STRATS[0]])
        row = E.load_rows(out)[0]
        T = pd.Timestamp(row["date"])
        i = self.panel["S00"].index.get_loc(T)
        sc = factors.score("mom_01", self.panel, T, MOM_P)
        top = [k for k, _ in sorted(sc.items(), key=lambda kv: (-kv[1], kv[0]))[:5]]
        self.assertEqual(row["picks"], top)
        rets = {k: df["open"].iloc[i + 2] / df["open"].iloc[i + 1] - 1 for k, df in self.panel.items()}
        mkt = float(np.mean(list(rets.values())))
        lots = {k: (int(0.2 * 1_000_000 / float(self.panel[k]["open"].iloc[i + 1])) // 100) * 100 for k in top}
        w = {k: lots[k] * float(self.panel[k]["open"].iloc[i + 1]) / 1_000_000 for k in top}   # 整手后的真实权重
        gross = sum(w[k] * rets[k] for k in top)
        self.assertAlmostEqual(row["gross"], gross, places=12)
        for k in top:
            self.assertAlmostEqual(row["weights_after"][k], w[k], places=12)
        self.assertAlmostEqual(row["mkt"], mkt, places=12)
        cp = E.CostParams()
        yuan = 0.0                                                # 首日全部买入:每只 20% × 本金,整手,T007 费用+滑点
        for k in top:
            px = float(self.panel[k]["open"].iloc[i + 1])
            sh = (int(0.2 * 1_000_000 / px) // 100) * 100
            yuan += trade_fees(cp, "buy", px * sh)["total"] + px * sh * cp.slippage_bps / 1e4   # 成交价=开盘价,滑点折成费用
        self.assertAlmostEqual(row["cost"], yuan / 1_000_000, places=12)
        self.assertAlmostEqual(row["net"], gross - yuan / 1_000_000, places=12)
        self.assertEqual(row["exec_date"], str(self.panel["S00"].index[i + 1].date()))
        self.assertEqual(row["days"], 1)

    def test_resume_matches_single_run(self):
        for ext in ("sqlite", "jsonl"):
            full, part = self.d / f"full.{ext}", self.d / f"part.{ext}"
            run(self.panel, full)
            r1 = run(self.panel, part, end="2024-02-15")
            r2 = run(self.panel, part)
            r3 = run(self.panel, part)                      # 已跑完,再跑不新增
            self.assertGreater(r1["processed"], 0)
            self.assertGreater(r2["processed"], 0)
            self.assertEqual(r3["processed"], 0)
            self.assertEqual(E.load_rows(full), E.load_rows(part))

    def test_jsonl_partial_day_and_torn_line_ignored(self):
        full, part = self.d / "f.jsonl", self.d / "p.jsonl"
        run(self.panel, full)
        run(self.panel, part, end="2024-02-15")
        with open(part, "a", encoding="utf-8") as f:       # 模拟崩溃:多出半天的行 + 半截最后一行
            f.write(json.dumps({"type": "row", "key": "mom_01|n=5", "date": "2099-01-01"}) + "\n")
            f.write('{"type": "row", "key": "x')
        run(self.panel, part)
        self.assertEqual(E.load_rows(full), E.load_rows(part))

    def test_config_mismatch_rejected(self):
        out = self.d / "m.sqlite"
        run(self.panel, out, end="2024-02-01")
        with self.assertRaises(E.ReplayError):
            run(self.panel, out, top_n=6)
        with self.assertRaises(E.ReplayError):
            run(self.panel, out, weighting="risk_parity")

    def test_no_future_leak(self):
        a, b = self.d / "a.sqlite", self.d / "b.sqlite"
        run(self.panel, a, end="2024-02-15")
        i = self.panel["S00"].index.get_loc(pd.Timestamp("2024-02-15"))
        bad = {}
        for k, df in self.panel.items():
            d = df.copy()
            d.iloc[i + 3:] = d.iloc[i + 3:] * 7.0 + 100     # T+3 起的数据改得面目全非
            bad[k] = d
        run(bad, b, end="2024-02-15")
        self.assertEqual(E.load_rows(a), E.load_rows(b))

    def test_strict_equals_vectorized(self):
        a, b = self.d / "a.sqlite", self.d / "b.sqlite"
        run(self.panel, a, end="2024-01-31")
        run(self.panel, b, end="2024-01-31", strict=True)
        self.assertEqual(E.load_rows(a), E.load_rows(b))

    def test_controls_matched_count_and_fixed_seed(self):
        out = self.d / "c.sqlite"
        run(self.panel, out, strategies=[STRATS[2]], seed=1)
        rows = E.load_rows(out)
        self.assertTrue(all(len(r["picks"]) == 5 for r in rows))
        out2 = self.d / "c2.sqlite"
        run(self.panel, out2, strategies=[STRATS[2]], seed=2)
        self.assertNotEqual([r["picks"] for r in rows], [r["picks"] for r in E.load_rows(out2)])

    def test_universe_top_amount_uses_only_pre_start_data(self):
        u = E.select_universe(self.panel, "2024-01-02", 6)
        self.assertEqual(len(u), 6)
        bad = {k: df.copy() for k, df in self.panel.items()}
        for df in bad.values():
            df.loc[df.index >= "2024-01-02", "volume"] = 1.0     # 改 start 及以后的数据,股票池不得变
        self.assertEqual(u, E.select_universe(bad, "2024-01-02", 6))
        out = self.d / "u.sqlite"
        res = run(self.panel, out, universe_n=6)
        self.assertEqual(res["universe"], 6)
        for r in E.load_rows(out):
            self.assertTrue(set(r["picks"]) <= set(u))

    def test_universe_benchmark_is_full_market(self):
        out, out_full = self.d / "ub.sqlite", self.d / "ub_full.sqlite"
        run(self.panel, out, end="2024-01-12", strategies=[STRATS[0]], universe_n=6)
        run(self.panel, out_full, end="2024-01-12", strategies=[STRATS[0]])
        a, b = E.load_rows(out), E.load_rows(out_full)
        self.assertEqual([r["mkt"] for r in a], [r["mkt"] for r in b])      # 基准与股票池大小无关
        T = pd.Timestamp(a[0]["date"])
        i = self.panel["S00"].index.get_loc(T)
        full = np.mean([df["open"].iloc[i + 2] / df["open"].iloc[i + 1] - 1 for df in self.panel.values()])
        self.assertAlmostEqual(a[0]["mkt"], full, places=12)

    def test_universe_benchmark_respects_external_mask(self):
        # 回归:设了 universe_n 后,全市场基准也必须用外部掩码估值,结果与不设 universe_n 完全一致
        idx = pd.bdate_range("2024-01-01", periods=5)
        ops = {"A": [10.0, 10.0, 99.0, 12.0, 12.0], "B": [10.0] * 5, "C": [10.0] * 5}
        p = {k: pd.DataFrame({"open": o, "high": o, "low": o, "close": o, "volume": 1000.0}, index=idx)
             for k, o in ops.items()}
        mask = pd.DataFrame({"A": [True, True, False, True, True], "B": [True] * 5, "C": [True] * 5}, index=idx)
        frames = pd.DataFrame({"A": [1.0] * 5, "B": [0.0] * 5, "C": [0.0] * 5}, index=idx)
        res = []
        for n in (None, 2):
            out = self.d / f"bm{n}.jsonl"
            with patch.object(E.factors, "factor_frame", return_value=frames), \
                    patch.object(E, "select_universe", return_value=["A", "B"]):
                E.run_replay(p, idx[1], idx[1], out, [{"id": "mom_01", "params": {"n": 5}}], top_n=1,
                             tradable_mask=mask, universe_n=n, capital=100000.0)
            res.append(E.load_rows(out)[0]["mkt"])
        self.assertAlmostEqual(res[0], res[1], places=12)
        self.assertAlmostEqual(res[0], 0.2 / 3, places=12)     # A 在 T+1 掩码 False 沿用 10,T+2 为 12(+20%),B/C 不动;不被 99 污染

    def test_risk_parity_weights_sum_to_one_and_favor_low_vol(self):
        w = E._weights(["A", "B"], pd.Series({"A": 0.01, "B": 0.04}), "risk_parity")
        self.assertAlmostEqual(sum(w.values()), 1.0)
        self.assertAlmostEqual(w["A"] / w["B"], 4.0)
        out = self.d / "rp.sqlite"
        run(self.panel, out, weighting="risk_parity", strategies=[STRATS[0]])
        for r in E.load_rows(out):
            self.assertAlmostEqual(sum(r["weights"].values()), 1.0)

    def test_blocked_buy_goes_to_cash(self):
        panel = {k: df.copy() for k, df in self.panel.items()}
        out0 = self.d / "o.jsonl"
        run(panel, out0, end="2024-01-02", strategies=[STRATS[0]])
        row0 = E.load_rows(out0)[0]
        i = panel["S00"].index.get_loc(pd.Timestamp(row0["date"]))
        victim = row0["picks"][0]
        panel[victim].iloc[i + 1, panel[victim].columns.get_loc("volume")] = 0.0     # T+1 停牌
        out1 = self.d / "o1.jsonl"
        run(panel, out1, end="2024-01-02", strategies=[STRATS[0]])
        row1 = E.load_rows(out1)[0]
        self.assertEqual(row1["blocked_buys"], 1)
        self.assertNotIn(victim, row1["weights_after"])
        self.assertLessEqual(sum(row1["weights_after"].values()), 0.8)
        self.assertAlmostEqual(sum(row1["weights_after"].values()), 0.8, delta=0.03)   # 整手取整略低于 0.8

    def _tiny(self, a_open):
        idx = pd.bdate_range("2024-01-02", periods=4)
        p = {}
        for k in ("A", "B"):
            o = a_open if k == "A" else [10.0] * 4
            p[k] = pd.DataFrame({"open": o, "close": 10.0, "high": 11.0, "low": 9.0, "volume": 1000.0}, index=idx)
        return idx, p

    def _fixed_scores(self, idx, a, b):
        return patch.object(E.factors, "factor_frame",
                            return_value=pd.DataFrame({"A": a, "B": b}, index=idx))

    def test_min_commission_and_lot_affect_cost(self):
        o1, o2 = self.d / "lo.sqlite", self.d / "hi.sqlite"
        run(self.panel, o1, end="2024-01-02", strategies=[STRATS[0]], cost_params=E.CostParams(min_commission=5.0))
        run(self.panel, o2, end="2024-01-02", strategies=[STRATS[0]],
            cost_params=E.CostParams(min_commission=1000.0), capital=1_000_000.0)
        self.assertGreater(E.load_rows(o2)[0]["cost"], E.load_rows(o1)[0]["cost"] + 1e-4)

    def test_blocked_sell_cannot_create_leverage(self):
        idx, p = self._tiny([10.0, 10.0, 9.0, 9.0])               # A 在 idx[2] 一字跌停(开=高=低),第二天卖不出
        p["A"].loc[idx[2], ["high", "low"]] = 9.0
        with self._fixed_scores(idx, [2.0, 0, 0, 0], [0, 2.0, 2.0, 2.0]):
            E.run_replay(p, idx[0], idx[1], self.d / "bs.jsonl", [{"id": "mom_01", "params": {"n": 5}}], top_n=1)
        rows = E.load_rows(self.d / "bs.jsonl")
        self.assertEqual(rows[1]["blocked_sells"], 1)
        self.assertLessEqual(sum(rows[1]["weights_after"].values()), 1 + 1e-9)
        self.assertNotIn("B", rows[1]["weights_after"])

    def test_weights_drift_so_rebalance_costs(self):
        idx, p = self._tiny([10.0, 10.0, 10.5, 11.0])             # A 涨、B 不动:同样选股也要再平衡
        with self._fixed_scores(idx, [2.0] * 4, [1.0] * 4):
            E.run_replay(p, idx[0], idx[1], self.d / "dr.jsonl", [{"id": "mom_01", "params": {"n": 5}}], top_n=2)
        rows = E.load_rows(self.d / "dr.jsonl")
        self.assertGreater(rows[1]["cost"], 0)
        self.assertAlmostEqual(sum(rows[1]["weights_after"].values()), 1.0, delta=0.01)

    def test_insufficient_capital_cannot_hold_or_earn(self):
        idx, p = self._tiny([10.0, 10.0, 11.0, 12.0])             # 本金 100 元,一手要 1000 元:买不起
        with self._fixed_scores(idx, [2.0] * 4, [1.0] * 4):
            E.run_replay(p, idx[0], idx[0], self.d / "lot.jsonl", [{"id": "mom_01", "params": {"n": 5}}],
                         top_n=1, capital=100.0)
        row = E.load_rows(self.d / "lot.jsonl")[0]
        self.assertEqual(row["weights_after"], {})
        self.assertEqual(row["cost"], 0.0)
        self.assertEqual(row["gross"], 0.0)

    def test_resume_rejects_changed_history(self):
        out = self.d / "fp.sqlite"
        run(self.panel, out, end="2024-02-15")
        bad = {k: df.copy() for k, df in self.panel.items()}
        bad["S03"].loc["2024-01-10", "close"] *= 1.5               # 改已处理区间里的一格
        with self.assertRaises(E.ReplayError):
            run(bad, out)
        ext = {k: df.copy() for k, df in self.panel.items()}       # 只改 T+2 之后的数据是允许的
        for df in ext.values():
            df.loc[df.index > "2024-02-20", "close"] *= 1.1
        run(ext, out)

    def test_matched_control_follows_its_strategy_count(self):
        ctrl = [{"id": "ctrl_rand_01", "params": {}, "match": E.strategy_key("rev_01", STRATS[1]["params"])}]
        slow = {"id": "mom_01", "params": list(factors.iter_param_grid("mom_01"))[-1]}   # 窗口最长,预热期更久
        out = self.d / "mt.sqlite"
        run(self.panel, out, start="2023-10-02", end="2024-01-31", strategies=[slow, STRATS[1]] + ctrl)
        by_day, differ = {}, False
        for r in E.load_rows(out):
            by_day.setdefault(r["date"], {})[r["key"].split("|")[0]] = len(r["picks"])
        for d in by_day.values():
            self.assertEqual(d["ctrl_rand_01"], d["rev_01"])
            differ = differ or d["rev_01"] != d["mom_01"]
        self.assertTrue(differ)                                     # 预热期两策略条数确实不同
        with self.assertRaises(E.ReplayError):
            run(self.panel, self.d / "bad.sqlite", strategies=[dict(ctrl[0], match="nope|n=1")])

    def test_industry_change_blocks_resume(self):
        sid = [{"id": "ind_01", "params": next(factors.iter_param_grid("ind_01"))}]
        ind1 = {k: "x" for k in self.panel}
        ind2 = {k: k for k in self.panel}
        out = self.d / "ind.sqlite"
        run(self.panel, out, end="2024-02-15", strategies=sid, industry=ind1)
        with self.assertRaises(E.ReplayError):
            run(self.panel, out, strategies=sid, industry=ind2)

    def test_control_count_matches_real_strategies(self):
        out = self.d / "mc.sqlite"
        run(self.panel, out, start="2023-10-02", end="2023-10-20", strategies=STRATS)
        by_day = {}
        for r in E.load_rows(out):
            by_day.setdefault(r["date"], {})[r["key"].split("|")[0]] = len(r["picks"])
        self.assertEqual(by_day["2023-10-02"]["ctrl_rand_01"], 0)     # 预热期真实策略选 0 只,对照也 0 只
        for d in by_day.values():
            self.assertEqual(d["ctrl_rand_01"], max(d["mom_01"], d["rev_01"]))

    def test_cumulative_stats_consistent(self):
        out = self.d / "s.sqlite"
        run(self.panel, out, strategies=[STRATS[0]])
        rows = E.load_rows(out)
        comp = 1.0
        for r in rows:
            comp *= 1 + r["net"]
        self.assertAlmostEqual(rows[-1]["cum_net"], comp - 1, places=10)
        self.assertAlmostEqual(rows[-1]["cum_excess"], sum(r["excess"] for r in rows), places=10)
        self.assertEqual(rows[-1]["hit_days"], sum(1 for r in rows if r["excess"] > 0))
        s = E.summarize(out)[E.strategy_key("mom_01", MOM_P)]
        self.assertEqual(s["days"], len(rows))
        self.assertAlmostEqual(s["acc_day"], rows[-1]["hit_days"] / len(rows))

    def test_last_two_days_not_processed_and_validation(self):
        out = self.d / "e.sqlite"
        res = run(self.panel, out, start="2023-10-02", end="2030-01-01")
        self.assertEqual(res["last_date"], str(self.panel["S00"].index[-3].date()))
        with self.assertRaises(E.ReplayError):
            run(self.panel, self.d / "x.txt")
        with self.assertRaises(E.ReplayError):
            run(self.panel, self.d / "y.sqlite", top_n=0)
        with self.assertRaises(E.ReplayError):
            run(self.panel, self.d / "y.sqlite", weighting="bogus")
        bad = ROOT / "data" / "should_not_exist.sqlite"
        with self.assertRaises(E.ReplayError):
            run(self.panel, bad)
        self.assertFalse(bad.exists())


    def _two(self, opens, vols=None, cols=("A",)):
        idx = pd.bdate_range("2024-01-02", periods=len(next(iter(opens.values()))))
        p = {}
        for k in cols:
            p[k] = pd.DataFrame({"open": opens[k], "close": opens[k], "high": np.array(opens[k]) * 1.01,
                                 "low": np.array(opens[k]) * 0.99,
                                 "volume": (vols or {}).get(k, [1000.0] * len(idx))}, index=idx)
        return idx, p

    def test_rebalance_cash_integrity(self):
        # 本金 20000,A/B 各半仓;日 2 A 涨 10%、B 不动。再平衡后现金不为负、总权重不超 1、净资产守恒
        idx, p = self._two({"A": [90.0, 90.0, 99.0, 99.0], "B": [1.0] * 4}, cols=("A", "B"))
        with self._fixed_scores(idx, [2.0] * 4, [2.0] * 4):
            E.run_replay(p, idx[0], idx[1], self.d / "ci.jsonl", [{"id": "mom_01", "params": {"n": 5}}],
                         top_n=2, capital=20000.0)
        r1, r2 = E.load_rows(self.d / "ci.jsonl")
        for r in (r1, r2):
            self.assertGreaterEqual(r["cash"], 0.0)
            self.assertLessEqual(sum(r["weights_after"].values()), 1.0 + 1e-9)
        o2 = {"A": 99.0, "B": 1.0}
        stock2 = sum(sh * o2[k] for k, sh in r2["shares"].items())
        self.assertAlmostEqual(r2["nav_end"], stock2 + r2["cash"], places=6)          # 账本自洽
        self.assertAlmostEqual(r2["nav_end"], r2["nav_start"] * (1 + r2["net"]), places=6)
        gain_a = r1["shares"].get("A", 0) * (99.0 - 90.0)                              # 唯一的收益来源是 A 的涨幅
        self.assertLessEqual(r2["nav_end"], 20000.0 + gain_a + 1e-6)
        self.assertLessEqual(r2["nav_end"], 20000.0 * 1.05)
        self.assertLessEqual(sum(r2["weights_after"].values()), 1.01)

    def test_buy_budget_reserves_fee_and_cash_never_negative(self):
        # 本金 1000、一手 800 元、最低佣金 5:买一手共 805 元,放得下;股价 10 的一手 1000+5 > 1000,必须不买
        for px, expect in ((8.0, 100), (10.0, 0)):
            idx, p = self._two({"A": [px] * 4})
            with self._fixed_scores(idx, [2.0] * 4, [0.0] * 4):
                E.run_replay(p, idx[0], idx[0], self.d / f"bf{px}.jsonl", [{"id": "mom_01", "params": {"n": 5}}],
                             top_n=1, capital=1000.0, cost_params=E.CostParams(commission_rate=0.0003, min_commission=5.0))
            row = E.load_rows(self.d / f"bf{px}.jsonl")[0]
            self.assertEqual(row["shares"].get("A", 0), expect)
            self.assertGreaterEqual(row["cash"], 0.0)

    def test_overnight_assets_conservation(self):
        idx, p = self._two({"A": [8.0] * 5})
        with self._fixed_scores(idx, [2.0] * 5, [0.0] * 5):
            E.run_replay(p, idx[0], idx[1], self.d / "on.jsonl", [{"id": "mom_01", "params": {"n": 5}}],
                         top_n=1, capital=1000.0, cost_params=E.CostParams(commission_rate=0.0003, min_commission=5.0))
        r1, r2 = E.load_rows(self.d / "on.jsonl")
        self.assertEqual(r1["shares"], {"A": 100})
        self.assertEqual(r2["cost"], 0.0)                                      # 价格不变、无交易
        self.assertEqual(r2["shares"], r1["shares"])
        self.assertAlmostEqual(r2["nav_start"], r1["nav_end"], delta=0.01)     # 次日开盘净资产 == 前日收盘净资产(没丢佣金)
        self.assertAlmostEqual(r2["nav_end"], r1["nav_end"], delta=0.01)
        self.assertLess(r1["nav_end"], 1000.0)                                 # 佣金确实扣掉了

    def test_suspension_holding_pnl(self):
        # 第 1 天买入 100 股(价 10);第 2 天停牌(量 0);第 3 天复牌价 12:持有收益 +200 不得丢失
        idx, p = self._two({"A": [10.0, 10.0, 10.0, 12.0, 12.0]},
                           vols={"A": [1000.0, 1000.0, 0.0, 1000.0, 1000.0]})
        with self._fixed_scores(idx, [2.0] * 5, [0.0] * 5):
            E.run_replay(p, idx[0], idx[2], self.d / "sp.jsonl", [{"id": "mom_01", "params": {"n": 5}}],
                         top_n=1, capital=2000.0)
        rows = E.load_rows(self.d / "sp.jsonl")
        self.assertEqual(rows[0]["shares"], {"A": 100})
        self.assertEqual(rows[1]["blocked_buys"], 1)                           # 停牌日不能交易
        self.assertEqual(rows[1]["shares"], {"A": 100})
        self.assertAlmostEqual(rows[1]["gross"] * rows[1]["nav_start"], 200.0, places=6)   # 停牌区间收益不清零
        fees = sum(r["cost"] * r["nav_start"] for r in rows)
        self.assertGreaterEqual(rows[-1]["nav_end"], 2000.0 + 200.0 - fees - 1e-6)

    def test_suspension_market_benchmark_consistent(self):
        # 开盘价 10,10,0(停牌),12,12:基准必须和持仓同口径(前向填充价),不能在停牌日变成 -100%
        idx, p = self._two({"A": [10.0, 10.0, 0.0, 12.0, 12.0]},
                           vols={"A": [1000.0, 1000.0, 0.0, 1000.0, 1000.0]})
        with self._fixed_scores(idx, [2.0] * 5, [0.0] * 5):
            E.run_replay(p, idx[0], idx[2], self.d / "mk.jsonl", [{"id": "mom_01", "params": {"n": 5}}],
                         top_n=1, capital=2000.0)
        rows = E.load_rows(self.d / "mk.jsonl")
        self.assertAlmostEqual(rows[0]["mkt"], 0.0, places=12)          # 第 1 天:10→(停牌沿用 10)
        self.assertAlmostEqual(rows[1]["mkt"], 0.2, places=12)          # 第 2 天:10→12,复牌涨幅补回
        self.assertAlmostEqual(rows[0]["excess"], rows[0]["net"], places=12)   # 没有虚假超额
        self.assertLess(abs(rows[0]["excess"]), 0.01)

    def _bar_panel(self, o1, h1, l1, v1=1000.0):
        # 第 0 天收盘 10;第 1 天(T+1)开盘 o1、最高 h1、最低 l1、成交量 v1;其余日价格平
        idx = pd.bdate_range("2024-01-02", periods=4)
        df = pd.DataFrame({"open": [10.0, o1, 11.0, 11.0], "high": [10.0, h1, 11.0, 11.0],
                           "low": [10.0, l1, 11.0, 11.0], "close": [10.0, 11.0, 11.0, 11.0],
                           "volume": [1000.0, v1, 1000.0, 1000.0]}, index=idx)
        return idx, {"A": df}

    def _first_day(self, name, panel, idx, **kw):
        with self._fixed_scores(idx, [2.0] * 4, [0.0] * 4):
            E.run_replay(panel, idx[0], idx[0], self.d / name, [{"id": "mom_01", "params": {"n": 5}}],
                         top_n=1, capital=100000.0, **kw)
        return E.load_rows(self.d / name)[0]

    def test_approx_limit_uses_only_open_vs_prev_close(self):
        # 没有外部掩码时的近似:T+1 开盘较 T 收盘超出 ±9.9% 不可交易;不看 T+1 的最高/最低,一字与否结果相同
        for h, l in ((11.0, 10.5), (10.96, 10.96)):
            idx, p = self._bar_panel(10.96, h, l)                  # +9.6%:可买
            row = self._first_day(f"nb{h}.jsonl", p, idx)
            self.assertEqual(row["blocked_buys"], 0)
            self.assertGreater(row["shares"].get("A", 0), 0)
        idx, p = self._bar_panel(11.0, 11.5, 10.5)                 # +10%:买不进,钱留作现金(最高/最低不同也一样)
        row = self._first_day("ob.jsonl", p, idx)
        self.assertEqual(row["blocked_buys"], 1)
        self.assertEqual(row["shares"], {})

    def test_cost_params_enforce_flags_respected(self):
        idx, p = self._bar_panel(11.0, 11.0, 11.0)                 # 一字涨停
        row = self._first_day("el.jsonl", p, idx, cost_params=E.CostParams(enforce_limits=False))
        self.assertEqual(row["blocked_buys"], 0)
        self.assertGreater(row["shares"].get("A", 0), 0)
        idx, p = self._bar_panel(10.0, 10.2, 9.8, v1=0.0)          # 开盘有价但成交量 0
        row = self._first_day("sd.jsonl", p, idx)
        self.assertEqual(row["blocked_buys"], 1)                   # 默认:按停牌处理
        row = self._first_day("se.jsonl", p, idx, cost_params=E.CostParams(enforce_suspension=False))
        self.assertEqual(row["blocked_buys"], 1)                   # 关闭开关,成交量为 0 仍不交易
        self.assertEqual(row["shares"], {})

    # ------------------------------------------------------------ 七条合约的独立验收测试(内存,Engine 直接驱动)

    def _eng(self, opens, vols=None, mask=None, cp=None, closes=None):
        """合成 Engine:opens/vols/closes 为 {代码: 列表};mask 为 {代码: bool 列表} 或 None(用近似)。"""
        n = len(next(iter(opens.values())))
        idx = pd.bdate_range("2024-01-02", periods=n)
        o = pd.DataFrame(opens, index=idx, dtype=float)
        v = pd.DataFrame(vols or {k: [1000.0] * n for k in opens}, index=idx, dtype=float)
        c = pd.DataFrame(closes or opens, index=idx, dtype=float)
        m = None if mask is None else pd.DataFrame(mask, index=idx, dtype=bool)
        return E.Engine(o, v, tradable_mask=m, cost_params=cp, close=c)

    NOFEE = dict(commission_rate=0.0, min_commission=0.0, transfer_rate=0.0, stamp_rate=0.0, slippage_bps=0.0)

    def test_no_future_data_strict(self):
        # 合约 1:T+1 开盘固定 10,把 T+1 的 high 11→100、low 10.5→1、close 10.8→50,股数与现金必须逐位一致
        res = []
        for hi, lo, cl in ((11.0, 10.5, 10.8), (100.0, 1.0, 50.0)):
            idx = pd.bdate_range("2024-01-02", periods=4)
            df = pd.DataFrame({"open": [10.0, 10.0, 10.0, 10.0], "high": [10.0, hi, 10.0, 10.0],
                               "low": [10.0, lo, 10.0, 10.0], "close": [10.0, cl, 10.0, 10.0],
                               "volume": 1000.0}, index=idx)
            # (a) Engine 层:只喂开盘/成交量/收盘(引擎本来就没有 high/low 入口)
            eng = E.Engine(df[["open"]].rename(columns={"open": "A"}), df[["volume"]].rename(columns={"volume": "A"}),
                           close=df[["close"]].rename(columns={"close": "A"}))
            pf, info = eng.rebalance(E.Portfolio({}, 100000.0), 1, {"A": 1.0})
            # (b) 回放层:第一个信号日的整行结果
            with self._fixed_scores(idx, [2.0] * 4, [0.0] * 4):
                E.run_replay({"A": df}, idx[0], idx[0], self.d / f"nf{hi}.jsonl",
                             [{"id": "mom_01", "params": {"n": 5}}], top_n=1, capital=100000.0)
            row = E.load_rows(self.d / f"nf{hi}.jsonl")[0]
            res.append((pf.shares, pf.cash, row["shares"], row["cash"], row["gross"], row["cost"], row["nav_end"]))
        self.assertEqual(res[0], res[1])
        self.assertGreater(res[0][0].get("A", 0), 0)               # 确实成交了,不是两边都空

    def test_trades_at_open_price_exactly(self):
        eng = self._eng({"A": [10.0, 12.5, 12.5]}, cp=E.CostParams(**self.NOFEE), mask={"A": [True] * 3})
        pf, info = eng.rebalance(E.Portfolio({}, 100000.0), 1, {"A": 1.0})
        self.assertEqual(pf.shares, {"A": 8000})
        self.assertAlmostEqual(pf.cash, 100000.0 - 8000 * 12.5, places=9)
        self.assertEqual(info["trades"][0][:4], ("A", "buy", 8000, 12.5))

    def test_tradable_mask_input(self):
        # 合约 2:掩码 False → 无论价格怎样都是空交易;改成 True 其他不变 → 成交
        for px in (10.0, 7.0, 13.0):
            eng = self._eng({"A": [10.0, px, px]}, mask={"A": [True, False, True]})
            pf, info = eng.rebalance(E.Portfolio({}, 100000.0), 1, {"A": 1.0})
            self.assertEqual((pf.shares, pf.cash, info["trades"], info["blocked_buys"]), ({}, 100000.0, [], 1))
        eng = self._eng({"A": [10.0, 10.0, 10.0]}, mask={"A": [True, True, True]})
        pf, info = eng.rebalance(E.Portfolio({}, 100000.0), 1, {"A": 1.0})
        self.assertGreater(pf.shares["A"], 0)
        # 已有持仓:掩码 False 卖不出(整仓被拦),换成 True 就卖
        held = E.Portfolio({"A": 1000}, 0.0)
        eng = self._eng({"A": [10.0, 10.0, 10.0]}, mask={"A": [True, False, True]})
        pf, info = eng.rebalance(held, 1, {})
        self.assertEqual((pf.shares, info["blocked_sells"]), ({"A": 1000}, 1))
        eng = self._eng({"A": [10.0, 10.0, 10.0]}, mask={"A": [True, True, True]})
        pf, info = eng.rebalance(held, 1, {})
        self.assertEqual(pf.shares, {})
        # 掩码缺失的格子按不可交易;非 bool 掩码被拒绝
        n = 3
        idx = pd.bdate_range("2024-01-02", periods=n)
        o = pd.DataFrame({"A": [10.0] * n}, index=idx)
        with self.assertRaises(E.ReplayError):
            E.Engine(o, o * 100, tradable_mask=pd.DataFrame({"A": [1, 1, 1]}, index=idx))
        eng = E.Engine(o, o * 100, tradable_mask=pd.DataFrame({"B": [True] * n}, index=idx))
        self.assertEqual(eng.rebalance(E.Portfolio({}, 1e5), 1, {"A": 1.0})[0].shares, {})
        # 回放层接受外部掩码:A 在 T+1 为 False → 买不进
        idx, p = self._bar_panel(10.0, 10.2, 9.8)
        mk = pd.DataFrame({"A": [True, False, True, True]}, index=idx)
        row = self._first_day("mk.jsonl", p, idx, tradable_mask=mk)
        self.assertEqual((row["blocked_buys"], row["shares"]), (1, {}))

    def test_no_hardcoded_limit_when_mask_given(self):
        # 给了外部掩码就不再看涨跌幅:开盘 +30% 但掩码 True 照样成交
        eng = self._eng({"A": [10.0, 13.0, 13.0]}, mask={"A": [True] * 3})
        self.assertGreater(eng.rebalance(E.Portfolio({}, 1e5), 1, {"A": 1.0})[0].shares["A"], 0)
        eng = self._eng({"A": [10.0, 13.0, 13.0]})                  # 没给掩码:近似 ±9.9% 拦下
        self.assertEqual(eng.rebalance(E.Portfolio({}, 1e5), 1, {"A": 1.0})[0].shares, {})

    def test_enforce_flags(self):
        n = 4
        both = {"A": [True] * n}
        # 涨跌停信号:掩码 False 但有量有价
        lim_mask = {"A": [True, True, False, True]}
        opens = {"A": [10.0, 10.0, 12.0, 12.0]}
        held = E.Portfolio({"A": 100}, 0.0)
        e_on = self._eng(opens, mask=lim_mask, cp=E.CostParams(enforce_limits=True))
        e_off = self._eng(opens, mask=lim_mask, cp=E.CostParams(enforce_limits=False))
        # 成交:开关打开时卖不出、关闭时卖得出
        self.assertEqual(e_on.rebalance(held, 2, {})[0].shares, {"A": 100})
        self.assertEqual(e_off.rebalance(held, 2, {})[0].shares, {})
        # 估值:打开时涨跌停日沿用前一有效价(10),关闭时用当日开盘价(12)
        self.assertAlmostEqual(e_on.value(held, 2), 1000.0)
        self.assertAlmostEqual(e_off.value(held, 2), 1200.0)
        # 停牌:成交量 0(开盘价有效)
        vols = {"A": [1000.0, 1000.0, 0.0, 1000.0]}
        s_on = self._eng(opens, vols=vols, mask=both, cp=E.CostParams(enforce_suspension=True))
        s_off = self._eng(opens, vols=vols, mask=both, cp=E.CostParams(enforce_suspension=False))
        self.assertEqual(s_on.rebalance(held, 2, {})[0].shares, {"A": 100})
        self.assertEqual(s_off.rebalance(held, 2, {})[0].shares, {"A": 100})     # 关闭开关也不在停牌日交易
        self.assertEqual(s_off.rebalance(E.Portfolio({}, 1e5), 2, {"A": 1.0})[0].shares, {})
        self.assertAlmostEqual(s_on.value(held, 2), 1000.0)           # 停牌日按前一有效价,不用停牌日价格
        self.assertAlmostEqual(s_off.value(held, 2), 1000.0)          # 关闭开关时同样按前一有效价估值
        self.assertAlmostEqual(s_off.value(held, 3), 1200.0)
        # 开盘价缺失:无论开关,都不能成交,持仓仍按前向填充价估值(不清零)
        nan_open = {"A": [10.0, 10.0, float("nan"), 12.0]}
        for cp in (E.CostParams(enforce_suspension=False, enforce_limits=False), E.CostParams()):
            e = self._eng(nan_open, mask=both, cp=cp)
            self.assertEqual(e.rebalance(held, 2, {})[0].shares, {"A": 100})
            self.assertEqual(e.rebalance(E.Portfolio({}, 1e5), 2, {"A": 1.0})[0].shares, {})
            self.assertAlmostEqual(e.value(held, 2), 1000.0)
            self.assertAlmostEqual(e.value(held, 3), 1200.0)

    def test_rebalance_cash_integrity_contract4(self):
        # 本金 20000,A/B 各半仓;日 2 A 价 110(一手 11000 超过半仓预算)、B 价 1
        eng = self._eng({"A": [100.0, 110.0, 110.0], "B": [1.0, 1.0, 1.0]}, mask={"A": [True] * 3, "B": [True] * 3})
        pf0 = E.Portfolio({}, 20000.0)
        pf, info = eng.rebalance(pf0, 1, {"A": 0.5, "B": 0.5})
        total = eng.value(pf, 1)
        self.assertLessEqual(total, 20000 * 1.005)
        self.assertGreaterEqual(pf.cash, 0.0)
        self.assertEqual(pf.shares.get("A", 0), 0)                    # 买不起一手 A,不成交也不透支
        self.assertGreater(pf.shares["B"], 0)
        self.assertAlmostEqual(total, 20000.0 - info["fees"], places=6)      # 资产 = 本金 − 费用,没凭空多出
        # 价格漂移后的再平衡:先卖后买,整手、现金非负、总资产只减少费用
        eng2 = self._eng({"A": [10.0, 10.0, 12.0], "B": [10.0, 10.0, 10.0]}, mask={"A": [True] * 3, "B": [True] * 3})
        p1, _ = eng2.rebalance(E.Portfolio({}, 100000.0), 1, {"A": 0.5, "B": 0.5})
        before = eng2.value(p1, 2)
        p2, inf2 = eng2.rebalance(p1, 2, {"A": 0.5, "B": 0.5})
        self.assertGreaterEqual(p2.cash, 0.0)
        self.assertTrue(all(n % 100 == 0 for n in p2.shares.values()))
        self.assertAlmostEqual(eng2.value(p2, 2), before - inf2["fees"], places=6)
        self.assertGreater(inf2["fees"], 0)
        self.assertTrue(any(t[1] == "sell" for t in inf2["trades"]))   # 确实卖了 A 的一部分

    def test_sell_proceeds_net_of_fees_and_min_commission(self):
        cp = E.CostParams(commission_rate=0.001, min_commission=5.0, transfer_rate=0.0, stamp_rate=0.001, slippage_bps=0.0)
        eng = self._eng({"A": [10.0, 10.0, 10.0]}, mask={"A": [True] * 3}, cp=cp)
        pf, info = eng.rebalance(E.Portfolio({"A": 300}, 0.0), 1, {})
        amount = 300 * 10.0
        self.assertAlmostEqual(pf.cash, amount - max(amount * 0.001, 5.0) - amount * 0.001, places=9)   # 3000 − 5 − 3
        self.assertEqual(pf.shares, {})

    def test_portfolio_state_shares_only(self):
        # 合约 5:状态只有 shares(100 的倍数)与 cash(非负),没有 weights
        import dataclasses
        eng = self._eng({"A": [10.0, 10.0, 11.0], "B": [7.0, 7.3, 7.0]}, mask={"A": [True] * 3, "B": [True] * 3})
        pf, _ = eng.rebalance(E.Portfolio({}, 54321.0), 1, {"A": 0.6, "B": 0.4})
        pf, _ = eng.rebalance(pf, 2, {"A": 0.3, "B": 0.7})
        self.assertEqual([f.name for f in dataclasses.fields(pf)], ["shares", "cash"])
        self.assertFalse(hasattr(pf, "weights"))
        self.assertFalse(any("weight" in k for k in vars(pf)))
        self.assertIsInstance(pf.shares, dict)
        self.assertTrue(pf.shares)
        for k, n in pf.shares.items():
            self.assertIsInstance(n, int)
            self.assertEqual(n % 100, 0)
        self.assertGreaterEqual(pf.cash, 0.0)
        self.assertAlmostEqual(eng.value(pf, 2), pf.cash + sum(n * {"A": 11.0, "B": 7.0}[k] for k, n in pf.shares.items()))
        # 回放层落盘的状态也是整股股数 + 现金
        idx, p = self._bar_panel(10.0, 10.2, 9.8)
        row = self._first_day("ps.jsonl", p, idx)
        self.assertTrue(all(isinstance(n, int) and n % 100 == 0 for n in row["shares"].values()))
        self.assertGreaterEqual(row["cash"], 0.0)

    def test_fees_no_overdraw(self):
        # 合约 6:本金 1000、buy_fee_rate=0.0003、min_commission=5
        cp = E.CostParams(commission_rate=0.0003, min_commission=5.0, transfer_rate=0.0, stamp_rate=0.0, slippage_bps=0.0)
        for px, expect in ((10.0, 0), (9.0, 100), (9.5, 100)):         # 股价 10:100 股要 1005 > 1000,买不起;9 / 9.5 买得起
            eng = self._eng({"A": [px] * 4}, mask={"A": [True] * 4}, cp=cp)
            self.assertAlmostEqual(eng.buy_fee_rate, 0.0003)
            pf, info = eng.rebalance(E.Portfolio({}, 1000.0), 1, {"A": 1.0})
            self.assertEqual(pf.shares.get("A", 0), expect)
            self.assertGreaterEqual(pf.cash, 0.0)
            self.assertAlmostEqual(pf.cash, 1000.0 - expect * px - (5.0 if expect else 0.0), places=9)
            v1 = eng.value(pf, 1)
            pf2, info2 = eng.rebalance(pf, 2, {"A": 1.0})               # 次日价格不变:无交易,资产不变
            self.assertEqual(info2["trades"], [])
            self.assertAlmostEqual(eng.value(pf2, 2), v1, delta=0.01)
        # 预算小于最低佣金:一手也不买,现金原样
        eng = self._eng({"A": [1.0] * 3}, mask={"A": [True] * 3}, cp=cp)
        pf, _ = eng.rebalance(E.Portfolio({}, 4.0), 1, {"A": 1.0})
        self.assertEqual((pf.shares, pf.cash), ({}, 4.0))
        # 多只同买:现金恒非负
        eng = self._eng({"A": [9.0] * 3, "B": [9.0] * 3, "C": [9.0] * 3}, mask={k: [True] * 3 for k in "ABC"}, cp=cp)
        for cash in (900.0, 1000.0, 1800.0, 2010.0, 2700.0, 2715.0):
            pf, _ = eng.rebalance(E.Portfolio({}, cash), 1, {"A": 1 / 3, "B": 1 / 3, "C": 1 / 3})
            self.assertGreaterEqual(pf.cash, 0.0)

    def test_suspension_holding_pnl_contract7(self):
        # 合约 7:持仓 100 股,T+1/T+2/T+3 开盘 10,10,12,T+2 成交量 0,T+3 复牌:最终市值 ≈ 1200 而不是 1000
        pf = E.Portfolio({"A": 100}, 0.0)
        vols = {"A": [1000.0, 1000.0, 0.0, 1000.0]}
        cases = [self._eng({"A": [10.0, 10.0, 10.0, 12.0]}, vols=vols),                      # 无掩码(近似)
                 self._eng({"A": [10.0, 10.0, 10.0, 12.0]}, vols=vols, mask={"A": [True] * 4}),     # 外部掩码全 True
                 self._eng({"A": [10.0, 10.0, 10.0, 12.0]}, vols=vols,
                           mask={"A": [True, True, False, True]})]                           # 掩码也标 False
        for e in cases:
            self.assertAlmostEqual(e.value(pf, 1), 1000.0)
            self.assertAlmostEqual(e.value(pf, 2), 1000.0)         # 停牌日:前向填充价,不清零
            self.assertAlmostEqual(e.value(pf, 3), 1200.0)         # 复牌当日用复牌价
            new, info = e.rebalance(pf, 2, {"A": 1.0})
            self.assertEqual((new.shares, info["trades"]), ({"A": 100}, []))
            final = e.value(new, 3)
            self.assertGreaterEqual(final, 100 * 12 - info["fees"] - 1e-9)
        # 停牌日的价格数据完全不用:T+2 的开盘价改成 99 / 0 / NaN,估值不变
        for junk in (99.0, 0.0, float("nan")):
            e = self._eng({"A": [10.0, 10.0, junk, 12.0]}, vols=vols, mask={"A": [True, True, False, True]})
            self.assertAlmostEqual(e.value(pf, 2), 1000.0)
            self.assertAlmostEqual(e.value(pf, 3), 1200.0)
        # 回放层:停牌区间收益不丢(同 test_suspension_holding_pnl)

    def test_valuation_ffill_uses_only_past(self):
        # 估值价只往后填充:第一天就停牌、之前没有价格的股票估值为 0(不会借用后来的价格)
        e = self._eng({"A": [float("nan"), 10.0, 10.0]}, mask={"A": [True] * 3})
        self.assertEqual(e.value(E.Portfolio({"A": 100}, 0.0), 0), 0.0)


if __name__ == "__main__":
    n = unittest.defaultTestLoader.loadTestsFromTestCase(TestEngine).countTestCases()
    res = unittest.TextTestRunner(verbosity=1).run(unittest.defaultTestLoader.loadTestsFromTestCase(TestEngine))
    print(f"通过 {res.testsRun - len(res.failures) - len(res.errors)}/{n} 个测试")
    sys.exit(0 if res.wasSuccessful() else 1)
