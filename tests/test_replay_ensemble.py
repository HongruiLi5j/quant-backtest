"""T008 S5:幸存者组合 + 市场状态过滤 + replay_run 端到端测试。只用合成数据与临时目录,不读 data/,不联网。"""
import copy
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from src.replay import ensemble as E, registry, selection as S  # noqa: E402
import replay_run as RR  # noqa: E402

KEYS = ["mom_01|n=5", "mom_01|n=10"]


def bdays(a, b):
    return [str(d.date()) for d in pd.bdate_range(a, b)]


DAYS = bdays("2024-01-02", "2024-06-28")
CODES = [f"C{j:02d}" for j in range(30)]


def make_null(days=DAYS, seed=1, tradable=None):
    rng = np.random.default_rng(seed)
    ret = pd.DataFrame(0.0003 + 0.01 * rng.standard_normal((len(days), len(CODES))), index=days, columns=CODES)
    mask = pd.DataFrame(True, index=days, columns=CODES)
    if tradable is not None:
        for d, cs in tradable.items():
            mask.loc[d, :] = False
            mask.loc[d, cs] = True
    return S.NullPanel(ret, mask)


def mk_rows(key, picks_fn, days=DAYS, ex=0.001, mkt=0.0003):
    """合成回放行:picks_fn(i) -> 当日选的股票列表;持有期 = 当日 → 下一个交易日。"""
    out = []
    for i, d in enumerate(days[:-1]):
        p = picks_fn(i)
        w = {c: 1.0 / len(p) for c in p}
        out.append({"key": key, "date": d, "next_date": days[i + 1], "net": mkt + ex, "gross": mkt + ex + 1e-4,
                    "cost": 1e-4, "mkt": mkt, "excess": ex, "picks": list(p), "weights": w, "shares": {p[0]: 100}})
    return out


class TestMarketState(unittest.TestCase):
    def mret(self, n=200, seed=0, drift=0.0005, std=0.005):
        idx = pd.bdate_range("2023-06-01", periods=n)
        return pd.Series(drift + std * np.random.default_rng(seed).standard_normal(n), index=idx)

    def test_warmup_and_cut_below_ma(self):
        # 先涨后连续下跌:跌破 60 日均线后要降仓;前 59 天是 warmup,按满仓
        r = pd.Series([0.01] * 80 + [-0.02] * 40, index=pd.bdate_range("2023-06-01", periods=120))
        sc = E.market_scale(r, threshold=1.0)               # 阈值给大,排除波动率触发
        days = list(sc)
        self.assertEqual(sc[days[10]]["state"], "warmup")
        self.assertEqual(sc[days[10]]["scale"], 1.0)
        self.assertEqual(sc[days[70]]["state"], "normal")
        self.assertEqual(sc[days[119]]["state"], "cut")
        self.assertTrue(sc[days[119]]["below_ma"])
        self.assertEqual(sc[days[119]]["scale"], E.REDUCE)

    def test_high_vol_cuts_with_frozen_threshold(self):
        r = self.mret(150)
        thr = E.freeze_vol_threshold(r, r.index[99])
        r2 = r.copy()
        r2.iloc[130:] = 0.05 * np.random.default_rng(3).standard_normal(len(r2) - 130)    # 之后波动骤升
        sc = E.market_scale(r2, thr["threshold"])
        self.assertTrue(any(v["high_vol"] for d, v in sc.items() if d >= str(r.index[135].date())))
        self.assertTrue(all(v["scale"] == E.REDUCE for v in sc.values() if v["high_vol"] or v["below_ma"]))

    def test_no_lookahead_truncate_and_modify_future(self):
        r = self.mret(180)
        thr = E.freeze_vol_threshold(r, r.index[99])["threshold"]
        full = E.market_scale(r, thr)
        T = r.index[120]
        cut = E.market_scale(r[r.index <= T], thr)
        for d in cut:
            self.assertEqual(cut[d], full[d])
        r2 = r.copy()
        r2[r2.index > T] = -0.05                               # 改掉 T 之后的所有数据
        mod = E.market_scale(r2, thr)
        for d in cut:
            self.assertEqual(mod[d], full[d])

    def test_threshold_is_train_only(self):
        r = self.mret(200)
        end = r.index[119]
        a = E.freeze_vol_threshold(r, end)
        r2 = r.copy()
        r2[r2.index > end] *= 10
        self.assertEqual(E.freeze_vol_threshold(r2, end)["threshold"], a["threshold"])
        self.assertEqual(a["pct"], 80.0)
        with self.assertRaises(E.EnsembleError):
            E.freeze_vol_threshold(r, r.index[30])           # 训练段太短

    def test_bad_reduce(self):
        with self.assertRaises(E.EnsembleError):
            E.market_scale(self.mret(80), 0.01, reduce=0.0)


class TestWeights(unittest.TestCase):
    def test_decay_halflife(self):
        lam = E.decay_lambda(20)
        self.assertAlmostEqual(lam ** 20, 0.5)
        with self.assertRaises(E.EnsembleError):
            E.decay_lambda(0)

    def test_blend_rules(self):
        w = E.blend_weights({"a": 0.02, "b": 0.0, "c": -0.5}, shrink=0.5)
        self.assertAlmostEqual(sum(w.values()), 1.0)
        self.assertGreater(w["a"], w["b"])
        self.assertAlmostEqual(w["b"], w["c"])               # 负分与零分同权(不做空)
        eq = E.blend_weights({"a": -1.0, "b": -2.0})
        self.assertEqual(eq, {"a": 0.5, "b": 0.5})            # 全负 → 等权
        self.assertEqual(E.blend_weights({"a": None, "b": None}), {"a": 0.5, "b": 0.5})
        self.assertEqual(E.blend_weights({}), {})


class TestBuild(unittest.TestCase):
    def rows(self):
        return (mk_rows(KEYS[0], lambda i: ["C00", "C01", "C02"]) +
                mk_rows(KEYS[1], lambda i: ["C01", "C02", "C03"]))

    def test_vote_tiebreak_and_topn(self):
        out = E.build_ensemble(self.rows(), KEYS, make_null(), "vote", top_n=3, gross_cap=1.0, single_cap=1.0)
        # C01、C02 两票,C00、C03 一票:前 3 = C01、C02 + 票数相同里代码小的 C00
        self.assertEqual(sorted(out[5]["weights"]), ["C00", "C01", "C02"])
        self.assertAlmostEqual(sum(out[5]["weights"].values()), 1.0)

    def test_caps_and_scale(self):
        rows = [r for k in KEYS for r in mk_rows(k, lambda i: [f"C{j:02d}" for j in range(30)])]
        o = E.build_ensemble(rows, KEYS, make_null(), "vote", top_n=30)
        w = o[3]["weights"]
        self.assertAlmostEqual(sum(w.values()), E.MAX_GROSS)                    # 总仓 80%
        self.assertLessEqual(max(w.values()), E.MAX_SINGLE + 1e-12)
        sc = {d: {"scale": 0.5, "state": "cut"} for d in DAYS}
        o2 = E.build_ensemble(rows, KEYS, make_null(), "vote", top_n=30, scale=sc)
        self.assertAlmostEqual(sum(o2[3]["weights"].values()), E.MAX_GROSS * 0.5)
        o3 = E.build_ensemble(self.rows(), KEYS, make_null(), "vote", top_n=3)    # 3 只:单票封顶 10%,总仓 30%
        self.assertAlmostEqual(max(o3[3]["weights"].values()), 0.10)
        self.assertLess(sum(o3[3]["weights"].values()), E.MAX_GROSS)

    def test_untradable_dropped(self):
        d = DAYS[5]
        out = E.build_ensemble(self.rows(), KEYS, make_null(tradable={d: ["C01", "C02"]}), "vote", top_n=3,
                               gross_cap=1.0, single_cap=1.0)
        row = next(r for r in out if r["date"] == d)
        self.assertEqual(sorted(row["weights"]), ["C01", "C02"])               # C00、C03 不可交易,不买

    def test_cost_and_turnover(self):
        out = E.build_ensemble(self.rows(), KEYS, make_null(), "vote", top_n=3, gross_cap=1.0, single_cap=1.0)
        buy, sell = E._rates(None)
        self.assertAlmostEqual(out[0]["cost"], 1.0 * buy)                       # 首日从现金全仓买入
        self.assertAlmostEqual(out[1]["cost"], 0.0)                              # 之后不变,无成本
        out2 = E.build_ensemble(self.rows(), KEYS, make_null(), "vote", top_n=3, gross_cap=1.0, single_cap=1.0,
                                cost_mult=2.0)
        self.assertAlmostEqual(out2[0]["cost"], 2 * out[0]["cost"])
        self.assertAlmostEqual(out[0]["excess"], out[0]["net"] - out[0]["mkt"])
        S._check_rows(out)

    def test_weighted_follows_recent_winner(self):
        a = mk_rows(KEYS[0], lambda i: ["C00"], ex=0.004)       # 持续赢
        b = mk_rows(KEYS[1], lambda i: ["C01"], ex=-0.004)      # 持续输
        out = E.build_ensemble(a + b, KEYS, make_null(), "weighted", top_n=2, gross_cap=1.0, single_cap=1.0, shrink=1.0)
        w = out[40]["weights"]
        self.assertGreater(w["C00"], 0.99)                       # shrink=1:输家权重 0,全部给赢家
        self.assertNotIn("C01", w)
        first = out[0]["weights"]                                # 首日没有任何已实现表现 → 等权
        self.assertAlmostEqual(first["C00"], first["C01"])

    def test_weighted_no_lookahead(self):
        a = mk_rows(KEYS[0], lambda i: ["C00"], ex=0.002)
        b = mk_rows(KEYS[1], lambda i: ["C01"], ex=0.001)
        base = E.build_ensemble(a + b, KEYS, make_null(), "weighted", top_n=2, gross_cap=1.0, single_cap=1.0)
        T = 60
        a2 = copy.deepcopy(a)
        for r in a2[T:]:                                         # 只改 date ≥ DAYS[T] 的行(这些行在 T 日尚未实现)
            r["excess"] = -0.05
            r["net"] = r["mkt"] - 0.05
            r["gross"] = r["net"] + r["cost"]
        mod = E.build_ensemble(a2 + b, KEYS, make_null(), "weighted", top_n=2, gross_cap=1.0, single_cap=1.0)
        for i in range(T + 1):                                   # 第 T 天的权重只用 date < T 行(next_date ≤ T)的表现
            self.assertEqual(base[i]["weights"], mod[i]["weights"])
        self.assertNotEqual(base[T + 5]["weights"], mod[T + 5]["weights"])

    def test_same_day_row_not_absorbed(self):
        # 行 i 的 next_date = 第 i+1 天:在第 i 天不能被吸收;第 i+1 天才可以
        a = mk_rows(KEYS[0], lambda i: ["C00"], ex=0.0)
        a[0]["excess"], a[0]["net"] = 0.5, a[0]["mkt"] + 0.5
        b = mk_rows(KEYS[1], lambda i: ["C01"], ex=0.0)
        o = E.build_ensemble(a + b, KEYS, make_null(), "weighted", top_n=2, gross_cap=1.0, single_cap=1.0, shrink=1.0)
        self.assertAlmostEqual(o[0]["weights"]["C00"], o[0]["weights"]["C01"])      # 第 0 天:没有任何已实现
        self.assertGreater(o[1]["weights"]["C00"], 0.99)                             # 第 1 天:行 0 已实现

    def test_errors(self):
        r = self.rows()
        with self.assertRaises(E.EnsembleError):
            E.build_ensemble(r, [], make_null(), "vote")
        with self.assertRaises(E.EnsembleError):
            E.build_ensemble(r, KEYS, make_null(), "bogus")
        with self.assertRaises(E.EnsembleError):
            E.build_ensemble(r, KEYS + ["nope|"], make_null(), "vote")
        with self.assertRaises(E.EnsembleError):
            E.build_ensemble(r, KEYS, make_null(days=DAYS[:50]), "vote")           # NullPanel 缺日期
        with self.assertRaises(E.EnsembleError):
            E.build_ensemble(r, KEYS, make_null(), "vote", top_n=0)

    def test_holdout_rows_rejected(self):
        days = bdays("2026-03-20", "2026-04-10")
        rows = mk_rows(KEYS[0], lambda i: ["C00"], days=days)
        with self.assertRaises(E.EnsembleError):
            E.build_ensemble(rows, [KEYS[0]], make_null(days=days), "vote")


class TestMetrics(unittest.TestCase):
    def test_drawdown_and_basic(self):
        s = [{"net": x, "excess": x, "cost": 0.0, "weights": {"A": 0.5}} for x in (0.1, -0.2, 0.05)]
        m = E.series_metrics(s)
        self.assertAlmostEqual(m["max_drawdown"], 0.8 - 1)
        self.assertEqual(m["days"], 3)
        self.assertAlmostEqual(m["mean_gross_exposure"], 0.5)
        self.assertEqual(E.series_metrics([]), {"days": 0})

    def test_no_survivors_study(self):
        mret = pd.Series(0.0, index=pd.bdate_range("2023-06-01", periods=10))
        rep = E.ensemble_study([], [], make_null(), mret)
        self.assertEqual(rep["status"], "no_survivors")


def synth_panel(seed=11, start="2023-09-01", end="2026-03-31", n=24):
    """合成面板:每只股票有持续的漂移(强度不同),动量类策略应能选出高漂移股票。"""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(start, end)
    panel = {}
    for j in range(n):
        mu = (j - n / 2) * 0.0004
        r = mu + 0.01 * rng.standard_normal(len(idx))
        close = 20 * np.cumprod(1 + r)
        op = np.concatenate([[close[0]], close[:-1]]) * (1 + 0.002 * rng.standard_normal(len(idx)))
        panel[f"{600000 + j}"] = pd.DataFrame({"open": op, "high": np.maximum(op, close) * 1.003,
                                              "low": np.minimum(op, close) * 0.997, "close": close,
                                              "volume": np.full(len(idx), 1e6) * (1 + 0.1 * j)}, index=idx)
    return panel


class TestPipelineE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        t = Path(cls.tmp.name)
        cls.env = {k: os.environ.get(k) for k in (S.TRIAL_LOG_ENV, S.LEDGER_ENV)}
        os.environ[S.TRIAL_LOG_ENV] = str(t / "trial_log.jsonl")
        os.environ[S.LEDGER_ENV] = str(t / "ledger.jsonl")
        cls.reg = t / "frozen.json"
        cls.out = t / "run"
        cls.panel = synth_panel()
        cls.rep = RR.run_pipeline(cls.panel, "2024-01-01", "2026-03-31", cls.out, 20, ["mom_01"], top_n=5,
                                  null_k=200, seed=0, n_boot=200, registry_path=cls.reg)

    @classmethod
    def tearDownClass(cls):
        for k, v in cls.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        cls.tmp.cleanup()

    def test_files_written_in_out_only(self):
        names = sorted(p.name for p in self.out.iterdir())
        for f in ("replay.sqlite", "report.md", "run_meta.json", "monotonic_report.json", "train_record.json",
                  "valid_report.json", "ensemble_report.json"):
            self.assertIn(f, names)
        for f in ("run_meta.json", "train_record.json", "valid_report.json", "ensemble_report.json"):
            json.loads((self.out / f).read_text(encoding="utf-8"))
        data_dir = (ROOT / "data").resolve()
        self.assertFalse(data_dir in self.out.resolve().parents)

    def test_report_sections(self):
        md = (self.out / "report.md").read_text(encoding="utf-8")
        self.assertTrue(md.startswith("> **存在幸存者偏差/状态未过滤**"))
        for s in ("Holm", "随机对照", "成本翻倍", "月换手", "参数邻域", "过拟合", "多重检验", "样本量", "成本", "幸存者偏差"):
            self.assertIn(s, md)

    def test_survivors_get_ensemble(self):
        tr = self.rep["train"]
        ens = self.rep["ensemble"]
        self.assertEqual(tr["m_trials"], S.m_trials(registry.load_validated()[0]))
        if tr["survivors"]:
            self.assertEqual(ens["status"], "ok")
            self.assertEqual(ens["m_with_ensembles"], tr["m_trials"] + 2)
            for mode in E.MODES:
                o = ens["modes"][mode]
                for k in ("holm_p", "cost_x2_managed_valid", "neighborhood", "metrics", "filter_effect_valid"):
                    self.assertIn(k, o)
                self.assertIn("valid", o["metrics"]["managed"])
        else:
            self.assertEqual(ens["status"], "no_survivors")

    def test_no_holdout_rows(self):
        from src.replay import engine
        rows = engine.load_rows(self.out / "replay.sqlite")
        self.assertTrue(rows)
        ho0 = S.segments(registry.load_validated()[0])["holdout"][0]
        self.assertTrue(all(r["date"] < ho0 for r in rows))

    def test_refuse_overwrite(self):
        with self.assertRaises(FileExistsError):
            RR.run_pipeline(self.panel, "2024-01-01", "2026-03-31", self.out, 20, ["mom_01"], top_n=5,
                            null_k=200, n_boot=200, registry_path=self.reg)

    def test_refuse_data_dir_and_holdout_end(self):
        with self.assertRaises(RR.RunError):
            RR.check_out_dir(ROOT / "data" / "replay_out_x")
        with self.assertRaises(RR.RunError):
            RR.check_dates("2024-01-01", "2026-04-30", registry.load_validated()[0])


class TestCleanNan(unittest.TestCase):
    def test_clean(self):
        x = {"a": float("nan"), "b": [1.0, float("inf"), {"c": -float("inf")}], "d": 2, "e": "s"}
        self.assertEqual(RR.clean_nan(x), {"a": None, "b": [1.0, None, {"c": None}], "d": 2, "e": "s"})


class TestCli(unittest.TestCase):
    def run_main(self, argv, **kw):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = RR.main(argv, **kw)
        return code, buf.getvalue()

    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "o"
            code, txt = self.run_main(["--start", "2024-01-01", "--end", "2026-03-31", "--universe", "500",
                                       "--out", str(out), "--daily-dir", str(Path(d) / "nope")])
            self.assertEqual(code, 0)
            self.assertIn("dry-run", txt)
            self.assertIn('"universe_top_n_by_amount": 500', txt)
            self.assertFalse(out.exists())

    def test_refuse_nonempty_out(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "x.txt").write_text("a")
            code, txt = self.run_main(["--start", "2024-01-01", "--end", "2026-03-31", "--universe", "500", "--out", d])
            self.assertEqual(code, 2)
            self.assertIn("拒绝覆盖", txt)
            self.assertEqual([p.name for p in Path(d).iterdir()], ["x.txt"])

    def test_bad_args(self):
        with tempfile.TemporaryDirectory() as d:
            base = ["--universe", "500", "--out", str(Path(d) / "o")]
            self.assertEqual(self.run_main(["--start", "2024-01-01", "--end", "2026-06-30"] + base)[0], 1)    # 进封存段
            self.assertEqual(self.run_main(["--start", "2025-01-01", "--end", "2024-01-01"] + base)[0], 1)
            self.assertEqual(self.run_main(["--start", "2024-01-01", "--end", "2026-03-31", "--universe", "0",
                                            "--out", str(Path(d) / "o")])[0], 1)
            self.assertEqual(self.run_main(["--start", "2024-01-01", "--end", "2026-03-31", "--strategies", "nope"] + base)[0], 1)

    def test_execute_with_injected_loader(self):
        with tempfile.TemporaryDirectory() as d:
            t = Path(d)
            env = {k: os.environ.get(k) for k in (S.TRIAL_LOG_ENV, S.LEDGER_ENV)}
            os.environ[S.TRIAL_LOG_ENV] = str(t / "trial_log.jsonl")
            os.environ[S.LEDGER_ENV] = str(t / "ledger.jsonl")
            try:
                panel = synth_panel(seed=4, n=12)
                code, txt = self.run_main(["--start", "2024-01-01", "--end", "2026-03-31", "--universe", "10",
                                           "--out", str(t / "out"), "--strategies", "mom_01", "--top-n", "4",
                                           "--null-k", "100", "--n-boot", "200", "--execute",
                                           "--freeze-file", str(t / "frozen.json"),
                                           "--daily-dir", str(t / "nodaily")], panel_loader=lambda *a: panel)
            finally:
                for k, v in env.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v
            self.assertEqual(code, 0, txt)
            self.assertTrue((t / "out" / "report.md").exists())
            self.assertIn("回放结果好不代表策略有效", txt)


if __name__ == "__main__":
    unittest.main()
