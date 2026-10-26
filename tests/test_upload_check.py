"""upload_check 测试：只用合成数据，不读 data/，不联网，不导入 app。"""
import datetime as dt
import json
import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import upload_check as uc  # noqa: E402


def make(n=1000, mu=0.0008, sd=0.01, seed=0):
    rng = random.Random(seed)
    d0 = dt.date(2018, 1, 1)
    dates = [(d0 + dt.timedelta(days=i)).isoformat() for i in range(n)]
    rets = [rng.gauss(mu, sd) for _ in range(n)]
    bench = [rng.gauss(0.0003, sd) for _ in range(n)]
    turn = [0.2] * n
    return dates, rets, bench, turn


def status(out, cid):
    return next(c["status"] for c in out["checks"] if c["id"] == cid)


class HappyPathTests(unittest.TestCase):
    def test_full_inputs_give_conclusion_and_valid_json(self):
        d, r, b, t = make()
        out = uc.upload_check(d, r, 5, benchmark=b, n_trades=120, turnover=t, cost_bp=8, cost_included=True)
        json.dumps(out, ensure_ascii=False, allow_nan=False)
        self.assertNotEqual(out["verdict"]["code"], "incomplete")
        self.assertEqual(out["upload"]["missing_inputs"], [])
        for cid in ("sample", "sharpe_ci", "dsr", "excess", "oos", "cost"):
            self.assertNotEqual(status(out, cid), "na", cid)
        self.assertEqual(status(out, "plateau"), "na")                             # 上传序列没有可重跑策略：永远 na
        self.assertEqual(status(out, "mc"), "na")

    def test_missing_inputs_reported_and_never_pass(self):
        d, r, _, _ = make(mu=0.003)                                                # 很强的漂移
        out = uc.upload_check(d, r, 1)
        self.assertEqual(out["verdict"]["code"], "incomplete")
        self.assertEqual(set(out["upload"]["missing_inputs"]), {"benchmark", "n_trades", "turnover+cost_bp"})
        self.assertNotEqual(out["verdict"]["code"], "passes_checks")

    def test_more_trials_lower_dsr(self):
        d, r, b, t = make(mu=0.001)
        dsr = lambda k: next(c for c in uc.upload_check(d, r, k, benchmark=b, n_trades=100)["checks"] if c["id"] == "dsr")["value"]["dsr"]
        self.assertGreater(dsr(1), dsr(1000))

    def test_short_sample_is_insufficient(self):
        d, r, b, t = make(n=100)
        out = uc.upload_check(d, r, 1, benchmark=b, n_trades=5)
        self.assertEqual(out["verdict"]["code"], "insufficient_sample")

    def test_inputs_not_mutated(self):
        d, r, b, t = make(n=300)
        snap = (list(d), list(r), list(b), list(t))
        uc.upload_check(d, r, 2, benchmark=b, turnover=t, cost_bp=8, cost_included=False, n_trades=40)
        self.assertEqual(snap, (d, r, b, t))


class CostTests(unittest.TestCase):
    def test_gross_returns_are_netted_first(self):
        d, r, b, t = make(n=600)
        net = uc.upload_check(d, r, 1, benchmark=b, n_trades=60, turnover=t, cost_bp=8, cost_included=True)
        gross = uc.upload_check(d, r, 1, benchmark=b, n_trades=60, turnover=t, cost_bp=8, cost_included=False)
        sh = lambda o: next(c for c in o["checks"] if c["id"] == "sharpe_ci")["value"]["sharpe"]
        self.assertLess(sh(gross), sh(net))                                        # 毛收益被先扣一倍成本

    def test_cost_multiples_decrease(self):
        d, r, b, t = make(n=600)
        out = uc.upload_check(d, r, 1, benchmark=b, n_trades=60, turnover=[0.5] * 600, cost_bp=20, cost_included=True)
        v = next(c for c in out["checks"] if c["id"] == "cost")["value"]
        self.assertGreater(v["sharpe_1x"], v["sharpe_2x"])
        self.assertGreater(v["sharpe_2x"], v["sharpe_4x"])

    def test_unknown_cost_flag_warns(self):
        d, r, b, t = make(n=300)
        w = uc.upload_check(d, r, 1)["upload"]["warnings"]
        self.assertTrue(any("没有说明收益是否已扣成本" in x for x in w))
        w2 = uc.upload_check(d, r, 1, cost_included=False)["upload"]["warnings"]
        self.assertTrue(any("未扣成本" in x for x in w2))

    def test_cost_flag_required_with_turnover(self):
        d, r, b, t = make(n=300)
        with self.assertRaises(ValueError):
            uc.upload_check(d, r, 1, turnover=t[:300], cost_bp=8)
        with self.assertRaises(ValueError):
            uc.upload_check(d, r, 1, turnover=t[:300])                              # 只给换手不给费率


class FixTests(unittest.TestCase):
    def test_cost_ruin_is_failure_not_missing(self):
        d, r, b, _ = make(n=600)
        r[100] = -0.85
        t = [0.0] * 600
        t[100] = 20.0                                                              # 2x 压力：-0.85 - 20×1% = -1.05，单日亏光
        out = uc.upload_check(d, r, 1, benchmark=b, n_trades=60, turnover=t, cost_bp=100, cost_included=True)
        c = next(c for c in out["checks"] if c["id"] == "cost")
        self.assertEqual(c["status"], "fail")
        self.assertIn("2x", c["value"]["ruined"])
        self.assertIn("亏光", c["zh"])
        self.assertIn("cost", out["verdict"]["fails"])

    def test_oos_text_follows_actual_split(self):
        d, r, b, t = make(n=600)
        out = uc.upload_check(d, r, 1, benchmark=b, n_trades=60, split_date=d[300])
        c = next(c for c in out["checks"] if c["id"] == "oos")
        self.assertIn(d[300], c["zh"])
        self.assertNotIn("前 70%", c["zh"])
        self.assertTrue(any("你指定的切分日" in x["zh"] for x in out["caveats"]))

    def test_cost_ruin_only_at_4x_is_warn(self):
        d, r, b, _ = make(n=600, mu=0.004, sd=0.005)             # 漂移很强，2x 夏普仍为正
        r[100] = -0.5
        t = [0.0] * 600
        t[100] = 20.0                                                              # 2x: -0.7 仍活着；4x: -1.1 亏光
        out = uc.upload_check(d, r, 1, benchmark=b, n_trades=60, turnover=t, cost_bp=100, cost_included=True)
        c = next(c for c in out["checks"] if c["id"] == "cost")
        self.assertEqual(c["value"]["ruined"], ["4x"])
        self.assertNotEqual(c["status"], "pass")
        self.assertIn("亏光", c["zh"])
        self.assertNotIn("没有成本敏感性数据", c["zh"])
        self.assertIn("按警告处理", c["zh"])

    def test_existing_fail_not_downgraded_by_4x_ruin(self):
        # 2x 夏普本来就 ≤ 0(原判 fail)，同时 4x 亏光：必须保持 fail，不能降成 warn
        d, r, b, _ = make(n=600, mu=0.0002)
        r[100] = -0.5
        t = [0.2] * 600
        t[100] = 20.0
        out = uc.upload_check(d, r, 1, benchmark=b, n_trades=60, turnover=t, cost_bp=100, cost_included=True)
        c = next(c for c in out["checks"] if c["id"] == "cost")
        self.assertIn("4x", c["value"]["ruined"])
        self.assertEqual(c["status"], "fail")
        self.assertIn("cost", out["verdict"]["fails"])
        json.dumps(out, ensure_ascii=False, allow_nan=False)

    def test_default_split_text_mentions_posthoc(self):
        d, r, b, t = make(n=600)
        c = next(c for c in uc.upload_check(d, r, 1, benchmark=b, n_trades=60)["checks"] if c["id"] == "oos")
        self.assertIn("事后", c["zh"])
        self.assertIn("不是干净的样本外", c["zh"])


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.d, self.r, self.b, self.t = make(n=300)

    def bad(self, **kw):
        a = dict(dates=self.d, returns=self.r, n_trials=1)
        a.update(kw)
        d, r, n = a.pop("dates"), a.pop("returns"), a.pop("n_trials")
        with self.assertRaises(ValueError):
            uc.upload_check(d, r, n, **a)

    def test_rejects(self):
        self.bad(n_trials=0)
        self.bad(n_trials=True)
        self.bad(n_trials=1.5)
        self.bad(returns=self.r[:-1])
        self.bad(returns=self.r[:2], dates=self.d[:2])
        self.bad(returns=[float("nan")] + self.r[1:])
        self.bad(returns=[float("inf")] + self.r[1:])
        self.bad(returns=[-1.0] + self.r[1:])
        self.bad(returns=[11.0] + self.r[1:])
        self.bad(returns=["0.01"] + self.r[1:])
        self.bad(returns=[True] + self.r[1:])
        self.bad(dates=self.d[:5] + ["2018-13-40"] + self.d[6:])
        self.bad(dates=list(reversed(self.d)))
        self.bad(dates=[self.d[0]] + self.d[:-1])                                  # 重复日期
        self.bad(benchmark=self.b[:-1])
        self.bad(benchmark=[-1.0] + self.b[1:])
        self.bad(n_trades=-1)
        self.bad(n_trades=True)
        self.bad(cost_included="yes")
        self.bad(split_date=self.d[5])                                             # 样本内太短
        self.bad(split_date=self.d[-5])                                            # 样本外太短
        self.bad(split_date="not-a-date")

    def test_errors_do_not_echo_user_values(self):
        for kw in ({"returns": ["SECRET-X"] + self.r[1:]}, {"dates": ["SECRET-Y"] + self.d[1:]}):
            a = dict(dates=self.d, returns=self.r)
            a.update(kw)
            with self.assertRaises(ValueError) as cm:
                uc.upload_check(a["dates"], a["returns"], 1)
            self.assertNotIn("SECRET", str(cm.exception))

    def test_row_limit(self):
        n = uc.MAX_ROWS + 1
        d0 = dt.date(1900, 1, 1)
        dates = [(d0 + dt.timedelta(days=i)).isoformat() for i in range(n)]
        with self.assertRaises(ValueError):
            uc.upload_check(dates, [0.001] * n, 1)

    def test_formula_prefix_strings_rejected(self):
        self.bad(returns=["=1+1"] + self.r[1:])
        self.bad(dates=["=cmd"] + self.d[1:])

    def test_user_split_date(self):
        out = uc.upload_check(self.d, self.r, 1, split_date=self.d[150])
        self.assertEqual(out["upload"]["split_date"], self.d[150])
        self.assertIn("用户指定", out["upload"]["split_note"])


class ImportTests(unittest.TestCase):
    def test_no_app_import(self):
        self.assertNotIn("app", sys.modules)


if __name__ == "__main__":
    unittest.main()
