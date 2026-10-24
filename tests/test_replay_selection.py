"""T008 S4:训练段淘汰 / 验证段只看 / 封存段守卫测试。只用合成回放行、合成个股面板与临时目录,不读 data/,不联网。"""
import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.replay import registry, selection as S  # noqa: E402
from src.replay.engine import strategy_key  # noqa: E402
from src.validation.splits import LockedTestError, LockedTestGuard, Split, locked_fingerprint  # noqa: E402
from src.validation.trials import TrialLog  # noqa: E402

CTRLS = [f"ctrl_rand_0{i}|" for i in range(1, 6)]
STRAT = {"id": "mom_01", "params": {"n": 20}}
KEY = strategy_key("mom_01", {"n": 20})
NK = 300                              # 测试里零分布的大小(够用且快)


def bdays(start, end):
    return [str(d.date()) for d in pd.bdate_range(start, end)]


def build(key, days, mean=0.0, std=0.002, cost=0.0002, seed=0, weights="stable", held=1.0, k=2):
    """合成某个键的逐日行:excess ≈ mean + 噪声;gross = net + cost;mkt 固定。weights: stable 不变 / flip 每天换。
    k = 稳定持仓的股票条数(权重条数就是当日选股数)。"""
    rng = np.random.default_rng(seed)
    stable = {f"S{j:02d}": 1.0 / k for j in range(k)}
    out = []
    for i, d in enumerate(days[:-1]):
        ex = mean + std * rng.standard_normal()
        w = stable if weights == "stable" else ({"A": 1.0} if i % 2 == 0 else {"B": 1.0})
        has = (i % 100) < held * 100
        out.append({"key": key, "date": d, "next_date": days[i + 1], "net": ex + 0.0003, "gross": ex + 0.0003 + cost,
                    "cost": cost, "mkt": 0.0003, "excess": ex, "weights": w if has else {},
                    "shares": {"A": 100} if has else {}})
    return out


def world(days, strat_rows, ctrl_std=0.002, seed=100):
    rows = list(strat_rows)
    for j, k in enumerate(CTRLS):
        rows += build(k, days, 0.0, ctrl_std, seed=seed + j)
    return rows


TRAIN = bdays("2024-01-02", "2025-06-30")
VALID = bdays("2025-07-01", "2026-03-31")
HOLD = bdays("2026-04-01", "2026-06-30")


def make_panel(days, ncodes=60, seed=5, mean=0.0003, std=0.01, tradable=None):
    """合成 NullPanel:个股收益 ~ N(mean, std),均值约等于行里的 mkt=0.0003;tradable=可交易代码个数(其余不可交易)。"""
    rng = np.random.default_rng(seed)
    codes = [f"C{j:03d}" for j in range(ncodes)]
    ret = pd.DataFrame(mean + std * rng.standard_normal((len(days), ncodes)), index=days, columns=codes)
    mask = pd.DataFrame(True, index=days, columns=codes)
    if tradable is not None:
        mask.iloc[:, tradable:] = False
    return S.NullPanel(ret, mask)


PANEL = make_panel(TRAIN + VALID + HOLD)


def mono_for(strats, passed=True):
    """合成训练段单调性报告:strats 里每个组合都通过(或都不通过)。"""
    res = {}
    for s in strats:
        res.setdefault(s["id"], []).append({"params": s["params"], "passed": passed})
    return {"catalog_hash": registry.load_validated()[1], "segment": "train",
            "results": [{"id": i, "params": p} for i, p in res.items()]}


class Base(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self._env = {k: os.environ.get(k) for k in (S.TRIAL_LOG_ENV, S.LEDGER_ENV)}
        os.environ[S.LEDGER_ENV] = str(self.tmp / "locked_segment_ledger.jsonl")
        self.log = self.use_log(TrialLog(self.tmp / "trial_log.jsonl"))
        self._n = 0
        self.regs = {}

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._td.cleanup()

    def log_cur(self):
        return TrialLog(os.environ[S.TRIAL_LOG_ENV])

    def use_log(self, log):
        """把固定 TrialLog 路径(锚点读的就是它)重定向到这个临时账本。"""
        os.environ[S.TRIAL_LOG_ENV] = str(log.path)
        return log

    def new_log(self, name=None):
        self._n += 1
        return self.use_log(TrialLog(self.tmp / (name or f"trial_log_{self._n}.jsonl")))

    def reg_path(self):
        self._n += 1
        return self.tmp / f"frozen_{self._n}.json"

    def train(self, rows, strats=None, log=None, **kw):
        """直接调 train_eliminate(rows 已含对照)。默认:单调性报告全通过、每次用新的冻结文件、零分布 NK 个。"""
        st = strats or [STRAT]
        kw.setdefault("mono_report", mono_for(st))
        if "registry_path" not in kw:       # 全新冻结文件 = 全新账本(否则固定账本里的旧记录会触发篡改锚点)
            kw["registry_path"] = self.reg_path()
            if log is None:
                self.log = self.new_log()
        log = self.use_log(log or self.log)
        kw.setdefault("null_k", NK)
        kw.setdefault("panel", PANEL)
        rec = S.train_eliminate(rows, st, log, **kw)
        self.regs[rec["record_sha256"]] = kw["registry_path"]
        self.rec_reg = kw["registry_path"]               # 最近一次训练用的冻结文件(篡改过的记录回退到它)
        return rec

    def regof(self, rec):
        return self.regs.get(rec["record_sha256"], self.rec_reg)

    def run_train(self, strat_rows, strategies=None, **kw):
        return self.train(world(TRAIN, strat_rows), strategies, **kw)

    def dec(self, rec, key=KEY):
        return next(d for d in rec["decisions"] if d["key"] == key)


class TestRules(Base):
    def test_survivor_when_no_rule_triggers(self):
        rec = self.run_train(build(KEY, TRAIN, mean=0.001))
        d = self.dec(rec)
        self.assertEqual(d["decision"], "survivor", d["reasons"])
        self.assertEqual(rec["survivors"], [KEY])

    def test_rule1_significantly_below_controls(self):
        rec = self.run_train(build(KEY, TRAIN, mean=-0.004, cost=0.0))
        d = self.dec(rec)
        self.assertEqual(d["decision"], "eliminated")
        self.assertTrue(d["rule1"]["triggered"])
        self.assertFalse(d["rule2"]["triggered"])
        self.assertFalse(d["rule3"]["triggered"])

    def test_rule1_not_triggered_when_slightly_positive(self):
        d = self.dec(self.run_train(build(KEY, TRAIN, mean=0.0005)))
        self.assertFalse(d["rule1"]["triggered"])
        self.assertTrue(d["rule1"]["evaluable"])

    def test_rule1_p_is_empirical(self):
        # p = (null>=观测 的个数 + 1)/(n+1):用同样的输入重算零分布,必须逐位一致(不是正态近似)
        rows = world(TRAIN, build(KEY, TRAIN, mean=0.001))
        d = self.dec(self.train(rows))
        series = sorted([r for r in rows if r["key"] == KEY], key=lambda r: r["date"])
        cc = S._control_cost(S._by_key(rows))
        null = S._null_dist(series, PANEL, cc, 0, NK)
        self.assertEqual(len(null), NK)
        self.assertAlmostEqual(d["rule1"]["p_value"], (int((null >= d["rule1"]["score"] - S.EPS).sum()) + 1) / (NK + 1))
        self.assertEqual(d["null_dist_fingerprint"], S._null_fp(null))

    def test_insufficient_null_no_judgement(self):
        # 零分布 < 20:不做显著性判定,规则 1 不触发,结果是 undetermined 而不是 survivor/eliminated
        rec = self.run_train(build(KEY, TRAIN, mean=-0.004, cost=0.0), null_k=10)
        d = self.dec(rec)
        self.assertEqual(d["rule1"]["status"], "insufficient_null")
        self.assertFalse(d["rule1"]["evaluable"])
        self.assertEqual(d["decision"], "undetermined")

    def test_rule2_turnover_over_100pct_monthly(self):
        d = self.dec(self.run_train(build(KEY, TRAIN, mean=0.002, weights="flip")))
        self.assertTrue(d["rule2"]["turnover_triggered"])
        self.assertGreater(d["rule2"]["monthly_turnover"], 1.0)
        self.assertEqual(d["decision"], "eliminated")

    def test_rule2_stable_weights_low_turnover(self):
        d = self.dec(self.run_train(build(KEY, TRAIN, mean=0.001)))
        self.assertLess(d["rule2"]["monthly_turnover"], 1.0)
        self.assertFalse(d["rule2"]["turnover_triggered"])

    def test_rule2_cost_eats_all_gross_alpha(self):
        # 成本前年化超额 > 0,成本后 ≤ 0:gross 超额 +0.0005,成本 0.002 → 净 -0.0015
        rows = build(KEY, TRAIN, mean=-0.0015, std=0.0, cost=0.002)
        d = self.dec(self.run_train(rows))
        self.assertGreater(d["rule2"]["gross_annual_excess"], 0)
        self.assertLessEqual(d["rule2"]["net_annual_excess"], 0)
        self.assertTrue(d["rule2"]["cost_eats_triggered"])
        self.assertEqual(d["decision"], "eliminated")

    def test_rule2_not_triggered_when_gross_also_negative(self):
        rows = build(KEY, TRAIN, mean=-0.001, std=0.0, cost=0.0)
        d = self.dec(self.run_train(rows))
        self.assertFalse(d["rule2"]["cost_eats_triggered"])

    def test_rule3_mostly_flat(self):
        d = self.dec(self.run_train(build(KEY, TRAIN, mean=0.001, held=0.4)))
        self.assertTrue(d["rule3"]["triggered"])
        self.assertLess(d["rule3"]["held_fraction"], 0.5)
        self.assertEqual(d["decision"], "eliminated")

    def test_rule3_boundary_exactly_half_not_triggered(self):
        d = self.dec(self.run_train(build(KEY, TRAIN, mean=0.001, held=0.5)))
        self.assertFalse(d["rule3"]["triggered"])

    def test_rule4_monotonic_fail_and_missing_and_pass(self):
        ok = mono_for([STRAT])
        bad = mono_for([STRAT], passed=False)
        none = {**ok, "results": []}
        rows = build(KEY, TRAIN, mean=0.001)
        d_ok = self.dec(self.run_train(rows, mono_report=ok))
        self.assertEqual(d_ok["decision"], "survivor")
        self.assertTrue(d_ok["rule4"]["applied"])
        for rep in (bad, none):
            d = self.dec(self.run_train(rows, mono_report=rep))
            self.assertEqual(d["decision"], "eliminated")
            self.assertTrue(d["rule4"]["triggered"])

    def test_rule4_report_must_match_catalog_and_train(self):
        rows = build(KEY, TRAIN, mean=0.001)
        h = registry.load_validated()[1]
        with self.assertRaises(S.SelectionError):
            self.run_train(rows, mono_report={"catalog_hash": "x", "segment": "train", "results": []})
        with self.assertRaises(S.SelectionError):
            self.run_train(rows, mono_report={"catalog_hash": h, "segment": "valid", "results": []})

    def test_no_signal_flip_for_negative_strategy(self):
        # 负超额策略只会被淘汰,记录里没有"反向"选项,也不出现在幸存者里
        rec = self.run_train(build(KEY, TRAIN, mean=-0.004, cost=0.0))
        self.assertIn(KEY, rec["eliminated"])
        self.assertNotIn(KEY, rec["survivors"])
        self.assertFalse(rec["rules"]["flip_signals"])

    def test_insufficient_weeks_undetermined(self):
        short = TRAIN[:30]          # 约 6 周 < 12
        rec = self.train(world(short, build(KEY, short, mean=0.001)))
        d = self.dec(rec)
        self.assertEqual(d["decision"], "undetermined")
        self.assertNotIn(KEY, rec["survivors"])
        self.assertNotIn(KEY, rec["eliminated"])


class TestFixes(Base):
    """针对 Codex attempt 1 六条审阅意见的独立验收测试(Fix 1/2/3/5/6;第 4 条经验 p 见 TestRules/TestValidation)。"""

    def test_selection_requires_monotonic(self):
        # Fix 1:不传单调性报告 -> ineligible,不是 survivor;不评估、不记试验、不写冻结文件
        reg = self.reg_path()
        rec = S.train_eliminate(world(TRAIN, build(KEY, TRAIN, mean=0.001)), [STRAT], self.log, null_k=NK,
                                panel=PANEL, registry_path=reg)
        self.assertEqual(rec["status"], "ineligible")
        self.assertEqual(rec["reason"], "monotonicity_report_missing")
        self.assertEqual(rec["survivors"], [])
        self.assertEqual(self.dec(rec)["decision"], "ineligible")
        self.assertEqual(self.dec(rec)["reasons"], ["monotonicity_report_missing"])
        self.assertEqual(self.log.count(), 0)
        self.assertFalse(reg.exists())
        S.verify_record(rec)

    def test_eliminate_frozen(self):
        # Fix 2:第一次 eliminated;换成明显优秀的输入再跑,仍必须读冻结得到 eliminated
        reg = self.reg_path()
        first = self.run_train(build(KEY, TRAIN, mean=-0.004, cost=0.0), registry_path=reg)
        self.assertEqual(self.dec(first)["decision"], "eliminated")
        self.assertFalse(self.dec(first)["frozen"])
        entry = json.loads(reg.read_text(encoding="utf-8"))["entries"][KEY]
        for f in ("strategy_id", "verdict", "timestamp", "inputs_fingerprint"):
            self.assertTrue(entry[f])
        n_trials = self.log.count()
        second = self.run_train(build(KEY, TRAIN, mean=0.01, seed=9), registry_path=reg)
        d = self.dec(second)
        self.assertEqual(d["decision"], "eliminated")
        self.assertNotIn(KEY, second["survivors"])
        self.assertTrue(d["frozen"])
        self.assertTrue(d["inputs_changed"])                    # 输入变了会被标出来,但 verdict 不变
        self.assertEqual(second["frozen"], [KEY])
        self.assertEqual(self.log.count(), n_trials)            # 冻结读取不重复记试验
        # 同样输入再跑:inputs_changed 为 False
        third = self.run_train(build(KEY, TRAIN, mean=-0.004, cost=0.0), registry_path=reg)
        self.assertFalse(self.dec(third)["inputs_changed"])

    def test_frozen_file_tamper_rejected(self):
        reg = self.reg_path()
        self.run_train(build(KEY, TRAIN, mean=-0.004, cost=0.0), registry_path=reg)
        obj = json.loads(reg.read_text(encoding="utf-8"))
        obj["entries"][KEY]["verdict"] = "survivor"             # 篡改 verdict(哈希对不上)
        reg.write_text(json.dumps(obj), encoding="utf-8")
        with self.assertRaises(S.SelectionError):
            self.run_train(build(KEY, TRAIN, mean=0.01), registry_path=reg)

    def test_undetermined_not_frozen(self):
        reg = self.reg_path()
        self.run_train(build(KEY, TRAIN, mean=0.001), registry_path=reg, null_k=10)      # undetermined
        self.assertFalse(reg.exists() and KEY in json.loads(reg.read_text(encoding="utf-8"))["entries"])
        d = self.dec(self.run_train(build(KEY, TRAIN, mean=0.001), registry_path=reg))
        self.assertEqual(d["decision"], "survivor")

    def _hold_setup(self):
        nb = [strategy_key("mom_01", {"n": n}) for n in (10, 20, 60)]
        strats = [{"id": "mom_01", "params": {"n": n}} for n in (10, 20, 60)]
        tr = [r for i, k in enumerate(nb) for r in build(k, TRAIN, mean=0.001, seed=10 + i)]
        rec = self.train(world(TRAIN, tr), strats)
        dates = TRAIN + VALID + HOLD
        t0 = dates.index("2026-04-01")
        hold_rows = world(HOLD, [r for i, k in enumerate(nb) for r in build(k, HOLD, mean=0.002, seed=70 + i)])
        n = len(dates)
        split = Split(train=(0, len(TRAIN)), valid=(len(TRAIN), t0), test=(t0, n))
        fp = locked_fingerprint(dates[t0:], [0] * (n - t0))
        guard = LockedTestGuard(split, self.tmp / "locked_access_log.jsonl", fp)
        return rec, dates, hold_rows, guard

    def test_locked_segment_label_bypass(self):
        # Fix 3:同一封存段 + 同一策略,换 label 第二次必须抛 LockedSegmentReuseError
        rec, dates, hold_rows, g = self._hold_setup()
        S.holdout_report(hold_rows, dates, g, "label_a", rec, panel=PANEL, null_k=NK, registry_path=self.rec_reg)
        with self.assertRaises(S.LockedSegmentReuseError):
            S.holdout_report(hold_rows, dates, g, "label_b", rec, panel=PANEL, null_k=NK, registry_path=self.rec_reg)
        self.assertEqual(g.open_count(), 1)                    # 第二次没有消耗守卫授权
        # 新建守卫实例(同一日志/同目录)也一样:配对账本是持久的
        g2 = LockedTestGuard(g.split, g.log_path, locked_fingerprint(dates[g.split.test[0]:], [0] * (len(dates) - g.split.test[0])))
        with self.assertRaises(S.LockedSegmentReuseError):
            S.holdout_report(hold_rows, dates, g2, "label_c", rec, panel=PANEL, null_k=NK, registry_path=self.rec_reg)

    def test_segment_hash_definition(self):
        import hashlib
        # 唯一键 = (策略, 封存起点, 股票池集合哈希),不含终点;股票池顺序与重复不影响
        h = S.segment_hash("s1|", "2026-04-01", ["b", "a", "a"])
        ch = hashlib.sha1("a\nb".encode()).hexdigest()
        self.assertEqual(h, hashlib.sha1(f"s1||2026-04-01|{ch}".encode()).hexdigest())
        self.assertEqual(h, S.segment_hash("s1|", "2026-04-01", ["a", "b"]))
        self.assertNotEqual(h, S.segment_hash("s2|", "2026-04-01", ["a", "b"]))
        self.assertNotEqual(h, S.segment_hash("s1|", "2026-04-02", ["a", "b"]))
        self.assertNotEqual(h, S.segment_hash("s1|", "2026-04-01", ["a", "c"]))

    def test_null_dist_matched_sampling(self):
        # Fix 5:k=27,100 只可交易(总共 150 只列,其余不可交易)-> 每次抽样恰好 27 只且全来自可交易池
        panel = make_panel(TRAIN + VALID + HOLD, ncodes=150, tradable=100)
        tradable_pos = set(range(100))
        rng = np.random.default_rng(1)
        draws = S._draw_matched(rng, np.arange(100), 27, 500)
        self.assertEqual(draws.shape, (500, 27))
        for row in draws:
            self.assertEqual(len(set(row.tolist())), 27)            # 无放回,恰好 27 只
            self.assertTrue(set(row.tolist()) <= tradable_pos)
        # 端到端:包装 _draw_matched 记录 train_eliminate 每次实际抽样的形状与来源
        calls = []
        orig = S._draw_matched

        def spy(rng_, pool, k, n):
            out = orig(rng_, pool, k, n)
            calls.append((k, n, out.shape, set(out.ravel().tolist()) <= tradable_pos, set(pool.tolist()) == tradable_pos))
            return out

        S._draw_matched = spy
        try:
            self.run_train(build(KEY, TRAIN, mean=0.001, k=27), panel=panel, null_k=50)
        finally:
            S._draw_matched = orig
        self.assertGreater(len(calls), 100)
        for k, n, shape, in_pool, pool_ok in calls:
            self.assertEqual((k, n, shape), (27, 50, (50, 27)))
            self.assertTrue(in_pool and pool_ok)

    def test_null_dist_not_from_control_returns(self):
        # Fix 5:零分布只看个股面板,与对照策略的收益无关(改对照的超额,零分布不变;改面板收益,零分布变)
        series = build(KEY, TRAIN, mean=0.001)
        c1 = S._control_cost(S._by_key(world(TRAIN, series)))
        c2 = S._control_cost(S._by_key(world(TRAIN, series, ctrl_std=0.05, seed=777)))
        a = S._null_dist(series, PANEL, c1, 0, 100)
        b = S._null_dist(series, PANEL, c2, 0, 100)
        self.assertTrue(np.array_equal(a, b))
        other = make_panel(TRAIN + VALID + HOLD, seed=99)
        self.assertFalse(np.array_equal(a, S._null_dist(series, other, c1, 0, 100)))

    def test_null_pool_smaller_than_k_rejected(self):
        panel = make_panel(TRAIN + VALID + HOLD, ncodes=60, tradable=10)
        with self.assertRaises(S.SelectionError):
            self.run_train(build(KEY, TRAIN, mean=0.001, k=27), panel=panel)

    def test_decision_record_complete(self):
        # Fix 6:k=27 -> k_real==27,null_k_real==实际零分布大小,所有指纹非空
        rec = self.run_train(build(KEY, TRAIN, mean=0.001, k=27), null_k=NK)
        d = self.dec(rec)
        self.assertEqual(d["k_real"], 27)
        self.assertEqual(d["null_k_real"], NK)
        self.assertEqual(rec["null_k"], NK)
        for f in ("strategy_id", "ohlc_panel_fingerprint", "monotonic_report_fingerprint", "null_dist_fingerprint",
                  "verdict", "timestamp", "inputs_fingerprint"):
            self.assertTrue(d[f], f)
        self.assertEqual(d["ohlc_panel_fingerprint"], PANEL.fingerprint())
        self.assertEqual(d["monotonic_report_fingerprint"], S._sha1(mono_for([STRAT])))
        self.assertEqual(d["verdict"], d["decision"])
        self.assertNotIn("null_k", rec["rules"])                # 零分布大小不再硬编码在规则里
        rec2 = self.run_train(build(KEY, TRAIN, mean=0.001, k=27), null_k=77)
        self.assertEqual(self.dec(rec2)["null_k_real"], 77)

    def test_fingerprints_track_inputs(self):
        # 自身哈希只管内容;输入变了,指纹字段要变
        rows = build(KEY, TRAIN, mean=0.001)
        a = self.dec(self.run_train(rows))
        b = self.dec(self.run_train(rows, panel=make_panel(TRAIN + VALID + HOLD, seed=42)))
        self.assertNotEqual(a["ohlc_panel_fingerprint"], b["ohlc_panel_fingerprint"])
        self.assertNotEqual(a["null_dist_fingerprint"], b["null_dist_fingerprint"])
        bad = mono_for([STRAT])
        bad["results"].append({"id": "mom_02", "params": [{"params": {"n": 5}, "passed": True}]})
        c = self.dec(self.run_train(rows, mono_report=bad))
        self.assertNotEqual(a["monotonic_report_fingerprint"], c["monotonic_report_fingerprint"])


class TestInputsAndRecord(Base):
    def test_requires_controls_trial_log_and_panel(self):
        with self.assertRaises(S.SelectionError):
            self.train(build(KEY, TRAIN))
        with self.assertRaises(S.SelectionError):
            S.train_eliminate(world(TRAIN, build(KEY, TRAIN)), [STRAT], None, mono_report=mono_for([STRAT]),
                              panel=PANEL, registry_path=self.reg_path())
        with self.assertRaises(S.SelectionError):
            S.train_eliminate(world(TRAIN, build(KEY, TRAIN)), [STRAT], self.log, mono_report=mono_for([STRAT]),
                              registry_path=self.reg_path())

    def test_missing_strategy_rows_error(self):
        with self.assertRaises(S.SelectionError):
            self.train(world(TRAIN, []))

    def test_bad_rows_rejected(self):
        rows = world(TRAIN, build(KEY, TRAIN))
        bad = [dict(rows[0], net=float("nan"))] + rows[1:]
        with self.assertRaises(S.SelectionError):
            self.train(bad)
        miss = [{k: v for k, v in rows[0].items() if k != "gross"}] + rows[1:]
        with self.assertRaises(S.SelectionError):
            self.train(miss)

    def test_valid_rows_do_not_influence_elimination(self):
        tr = world(TRAIN, build(KEY, TRAIN, mean=0.001))
        a = self.train(tr, log=TrialLog(self.tmp / "a_log.jsonl"), timestamp="t0")
        v_rows = world(VALID, build(KEY, VALID, mean=-0.5))      # 验证段极端差
        b = self.train(tr + v_rows, log=TrialLog(self.tmp / "b_log.jsonl"), timestamp="t0")
        strip = lambda r: {k: v for k, v in r.items() if k not in ("decisions", "record_sha256")}   # noqa: E731
        self.assertEqual(strip(a), strip(b))
        da, db = self.dec(a), self.dec(b)
        for f in ("decision", "rule1", "rule2", "rule3"):
            self.assertEqual(da[f], db[f])

    def test_holdout_rows_rejected_without_guard(self):
        rows = world(TRAIN, build(KEY, TRAIN)) + build(KEY, ["2026-04-01", "2026-04-02"])
        with self.assertRaises(S.SelectionError):
            self.train(rows)
        with self.assertRaises(S.SelectionError):
            rec = self.run_train(build(KEY, TRAIN, mean=0.001))
            S.validate_survivors(rows, rec, panel=PANEL, registry_path=self.regof(rec))

    def test_embargo_drops_rows_crossing_segment_end(self):
        rec = self.run_train(build(KEY, TRAIN + ["2025-07-01"], mean=0.001))
        self.assertGreaterEqual(rec["embargo_dropped_rows"], 1)

    def test_deterministic_and_tamper_evident(self):
        rows = world(TRAIN, build(KEY, TRAIN, mean=0.001))
        a = self.train(rows, log=TrialLog(self.tmp / "x1.jsonl"), seed=7, timestamp="2026-01-01T00:00:00+00:00")
        b = self.train(rows, log=TrialLog(self.tmp / "x2.jsonl"), seed=7, timestamp="2026-01-01T00:00:00+00:00")
        # trial_id 因账本相同位置一致,整条记录逐位相同(时间戳固定)
        self.assertEqual(a, b)
        S.verify_record(a)
        t = copy.deepcopy(a)
        t["survivors"] = ["x|"]
        with self.assertRaises(S.SelectionError):
            S.verify_record(t)

    def test_write_once_and_not_in_data_dir(self):
        rec = self.run_train(build(KEY, TRAIN, mean=0.001))
        p = S.write_json_once(rec, self.tmp / "sub" / "train_selection.json")
        self.assertEqual(json.loads(p.read_text(encoding="utf-8"))["survivors"], [KEY])
        with self.assertRaises(S.SelectionError):
            S.write_json_once(rec, p)
        with self.assertRaises(S.SelectionError):
            S.write_json_once(rec, ROOT / "data" / "_should_not_exist_t008.json")
        self.assertFalse((ROOT / "data" / "_should_not_exist_t008.json").exists())
        with self.assertRaises(S.SelectionError):               # 冻结文件同样不许写进仓库 data/
            self.run_train(build(KEY, TRAIN, mean=0.001), registry_path=ROOT / "data" / "_should_not_exist_t008.json")
        self.assertFalse((ROOT / "data" / "_should_not_exist_t008.json").exists())


class TestTrials(Base):
    def test_eliminated_still_counted_and_m_not_reduced(self):
        good = {"id": "mom_01", "params": {"n": 20}}
        bad = {"id": "mom_01", "params": {"n": 5}}
        kb = strategy_key("mom_01", {"n": 5})
        rows = build(KEY, TRAIN, mean=0.001, seed=1) + build(kb, TRAIN, mean=-0.004, cost=0.0, seed=2)
        rec = self.run_train(rows, [good, bad])
        self.assertEqual(rec["eliminated"], [kb])
        self.assertEqual(self.log.count(), 2)                       # 淘汰者也记
        recs = self.log.load()
        self.assertTrue(any("eliminated" in r["tag"] for r in recs))
        self.assertTrue(all(registry.load_validated()[1][:12] in r["tag"] for r in recs))
        self.assertEqual(rec["m_trials"], max(registry.total_trials(registry.load_validated()[0]), S.M_FLOOR))
        self.assertEqual(rec["m_trials"], 490)
        self.assertEqual({d["trial_id"] for d in rec["decisions"]}, {r["id"] for r in recs})

    def test_controls_not_counted_as_trials(self):
        self.run_train(build(KEY, TRAIN, mean=0.001),
                       [STRAT, {"id": "ctrl_rand_01", "params": {}}])
        self.assertEqual(self.log.count(), 1)

    def test_same_freeze_file_does_not_recount_new_file_is_blocked(self):
        rows = build(KEY, TRAIN, mean=0.001)
        reg = self.reg_path()
        self.run_train(rows, registry_path=reg)
        self.run_train(rows, registry_path=reg)                      # 同一冻结文件:直接读冻结,不重复记
        self.assertEqual(self.log.count(), 1)
        with self.assertRaises(S.ElimRegistryTamperedError):         # 换新路径重来:被固定账本的锚点拦下
            self.run_train(rows, registry_path=self.reg_path())
        self.assertEqual(self.log.count(), 1)


class TestValidation(Base):
    def setUp(self):
        super().setUp()
        self.nb = [strategy_key("mom_01", {"n": n}) for n in (10, 20, 60)]
        tr = []
        for i, k in enumerate(self.nb):
            tr += build(k, TRAIN, mean=0.001, seed=10 + i)
        self.strats = [{"id": "mom_01", "params": {"n": n}} for n in (10, 20, 60)]
        self.rec = self.train(world(TRAIN, tr), self.strats)
        self.assertEqual(len(self.rec["survivors"]), 3)

    def vrows(self, means, **kw):
        rows = []
        for i, (k, mu) in enumerate(zip(self.nb, means)):
            rows += build(k, VALID, mean=mu, seed=50 + i, **kw)
        return world(VALID, rows, seed=900)

    def validate(self, rows, rec=None, **kw):
        kw.setdefault("null_k", NK)
        r = rec or self.rec
        kw.setdefault("registry_path", self.regof(r))
        return S.validate_survivors(rows, r, panel=PANEL, **kw)

    def test_report_fields_and_record_untouched(self):
        before = copy.deepcopy(self.rec)
        rep = self.validate(self.vrows([0.002, 0.002, 0.002]))
        self.assertEqual(self.rec, before)
        self.assertFalse(rep["params_changed"])
        self.assertEqual(rep["eliminated_by_valid"], [])
        self.assertEqual(rep["m_trials"], 490)
        self.assertEqual(rep["null_k"], NK)
        it = next(x for x in rep["items"] if x["key"] == self.nb[1])
        for f in ("holm_p", "holm_significant", "cost_x2_weekly", "monthly_turnover", "neighbors",
                  "param_island", "beats_control", "shadow_pool_eligible", "net_weekly", "k_real", "null_k_real",
                  "null_dist_fingerprint", "ohlc_panel_fingerprint", "monotonic_report_fingerprint"):
            self.assertIn(f, it)
        self.assertEqual(it["k_real"], 2)
        self.assertEqual(it["null_k_real"], NK)
        self.assertEqual(len(it["neighbors"]), 2)                  # n=20 的邻居是 10 与 60
        self.assertTrue(it["beats_control"])
        self.assertTrue(it["shadow_pool_eligible"])
        # 经验 p 下限 1/(NK+1),×490 > 0.05:Holm 不可能显著,报告里要明说,且不能因此假装显著
        self.assertTrue(rep["empirical_p_floor_blocks_holm"])
        self.assertFalse(it["holm_significant"])

    def test_holm_input_is_empirical_p_with_full_m(self):
        rep = self.validate(self.vrows([0.002, 0.002, 0.002]))
        ps = [x["net_weekly"]["p_value"] for x in rep["items"]]
        for p in ps:                                               # 经验 p 的形态:整数/(NK+1),下限 1/(NK+1)
            self.assertAlmostEqual(p * (NK + 1), round(p * (NK + 1)), places=6)
            self.assertGreaterEqual(p, 1.0 / (NK + 1) - 1e-12)
        self.assertNotIn("p_normal", rep["items"][0]["net_weekly"])
        adj_min = min(x["holm_p"] for x in rep["items"])
        self.assertAlmostEqual(adj_min, min(1.0, 490 * min(ps)))   # m=490 而不是只数实际检验的 3 个
        self.assertTrue(all(not x["holm_p"] < 0.05 for x in rep["items"]))

    def test_holm_adj_math(self):
        out = S._holm_adj({"a": 0.00001, "b": 0.02, "c": 0.5}, 490)
        self.assertAlmostEqual(out["a"], 490 * 0.00001)
        self.assertAlmostEqual(out["b"], 489 * 0.02 if 489 * 0.02 < 1 else 1.0)
        self.assertEqual(out["c"], 1.0)
        # m 足够小时经验 p 能够显著
        self.assertLess(S._holm_adj({"a": 1 / 1001}, 20)["a"], 0.05)

    def test_insufficient_null_in_validation(self):
        # 验证段的 null_k 必须等于记录里的值(见 test_valid_seed_from_record),这里直接测 _evaluate 的小零分布分支
        rep = S._evaluate(S.rows_for_segment(self.vrows([0.002, 0.002, 0.002]), "valid")[0], self.rec,
                          registry.load_validated()[0], PANEL, "0", 10)
        for it in rep["items"]:
            self.assertEqual(it["significance_status"], "insufficient_null")
            self.assertIsNone(it["holm_p"])
            self.assertFalse(it["holm_significant"])
            self.assertFalse(it["shadow_pool_eligible"])

    def test_cost_doubling_lowers_excess(self):
        rep = self.validate(self.vrows([0.001, 0.001, 0.001], cost=0.0008))
        for it in rep["items"]:
            self.assertLess(it["cost_x2_weekly"]["score"], it["net_weekly"]["score"])

    def test_param_island_blocks_shadow_pool(self):
        rep = self.validate(self.vrows([-0.0005, 0.003, -0.0005]))
        mid = next(x for x in rep["items"] if x["key"] == self.nb[1])
        self.assertTrue(mid["param_island"])
        self.assertFalse(mid["shadow_pool_eligible"])

    def test_missing_neighbor_data_is_island(self):
        rows = [r for r in self.vrows([0.002, 0.002, 0.002]) if r["key"] != self.nb[0]]
        rep = self.validate(rows)
        mid = next(x for x in rep["items"] if x["key"] == self.nb[1])
        self.assertIn("no_data", [n["status"] for n in mid["neighbors"]])
        self.assertTrue(mid["param_island"])

    def test_high_turnover_blocks_shadow_pool(self):
        rep = self.validate(self.vrows([0.002, 0.002, 0.002], weights="flip"))
        self.assertTrue(all(not it["shadow_pool_eligible"] for it in rep["items"]))

    def test_bad_validation_never_eliminates_or_changes_names(self):
        rep = self.validate(self.vrows([-0.01, -0.01, -0.01]))
        self.assertEqual([x["key"] for x in rep["items"]], self.rec["survivors"])
        self.assertEqual(rep["eliminated_by_valid"], [])
        self.assertTrue(all(not it["shadow_pool_eligible"] for it in rep["items"]))

    def test_only_survivors_reported(self):
        k5 = strategy_key("mom_01", {"n": 5})
        st = [STRAT, {"id": "mom_01", "params": {"n": 5}}]
        tr = build(KEY, TRAIN, mean=0.001) + build(k5, TRAIN, mean=-0.004, cost=0.0, seed=3)
        rec = self.train(world(TRAIN, tr), st)
        rows = world(VALID, build(KEY, VALID, mean=0.002) + build(k5, VALID, mean=0.1))
        rep = self.validate(rows, rec)
        self.assertEqual([x["key"] for x in rep["items"]], [KEY])

    def test_ineligible_record_has_nothing_to_validate(self):
        reg = self.reg_path()
        self.new_log()                                    # 空的固定账本:没有任何冻结判定的锚点记录
        rec = S.train_eliminate(world(TRAIN, build(KEY, TRAIN, mean=0.001)), [STRAT], self.log_cur(), null_k=NK,
                                panel=PANEL, registry_path=reg)
        rep = self.validate(world(VALID, build(KEY, VALID, mean=0.002)), rec, registry_path=reg)
        self.assertEqual(rep["items"], [])

    def test_tampered_record_or_changed_catalog_rejected(self):
        t = copy.deepcopy(self.rec)
        t["survivors"].append("evil|")
        with self.assertRaises(S.SelectionError):
            self.validate(self.vrows([0.002] * 3), t)
        t2 = copy.deepcopy(self.rec)
        t2["catalog_hash"] = "0" * 64
        with self.assertRaises(S.SelectionError):
            self.validate(self.vrows([0.002] * 3), S._seal(t2))

    def test_train_rows_in_validation_input_ignored(self):
        tr = world(TRAIN, [r for k in self.nb for r in build(k, TRAIN, mean=-0.5)])
        a = self.validate(self.vrows([0.002] * 3))
        b = self.validate(tr + self.vrows([0.002] * 3))
        self.assertEqual(a, b)

    def test_panel_required(self):
        with self.assertRaises(S.SelectionError):
            S.validate_survivors(self.vrows([0.002] * 3), self.rec, registry_path=self.rec_reg)


class TestHoldoutGuard(Base):
    def setUp(self):
        super().setUp()
        self.nb = [strategy_key("mom_01", {"n": n}) for n in (10, 20, 60)]
        tr = [r for i, k in enumerate(self.nb) for r in build(k, TRAIN, mean=0.001, seed=10 + i)]
        self.strats = [{"id": "mom_01", "params": {"n": n}} for n in (10, 20, 60)]
        self.rec = self.train(world(TRAIN, tr), self.strats)
        self.dates = TRAIN + VALID + HOLD
        self.t0 = self.dates.index("2026-04-01")
        self.hold_rows = world(HOLD, [r for i, k in enumerate(self.nb) for r in build(k, HOLD, mean=0.002, seed=70 + i)])

    def guard(self, test_start=None):
        a = self.t0 if test_start is None else test_start
        n = len(self.dates)
        split = Split(train=(0, len(TRAIN)), valid=(len(TRAIN), a), test=(a, n))
        fp = locked_fingerprint(self.dates[a:], [0] * (n - a))
        return LockedTestGuard(split, self.tmp / "locked_access_log.jsonl", fp)

    def hold(self, g, label, rows=None, rec=None, dates=None):
        r = rec or self.rec
        return S.holdout_report(self.hold_rows if rows is None else rows, self.dates if dates is None else dates, g,
                                label, r, panel=PANEL, null_k=NK, registry_path=self.regof(r))

    def test_non_guard_path_refuses_holdout(self):
        with self.assertRaises(S.SelectionError):
            S.validate_survivors(self.hold_rows, self.rec, panel=PANEL, registry_path=self.rec_reg)
        with self.assertRaises(S.SelectionError):
            S.rows_for_segment(self.hold_rows, "holdout")
        with self.assertRaises(S.SelectionError):
            S.holdout_report(self.hold_rows, self.dates, object(), "final", self.rec, panel=PANEL)

    def test_guard_opens_once_and_marks_contaminated(self):
        g = self.guard()
        rep = self.hold(g, "final_v1")
        self.assertEqual(g.open_count(), 1)
        self.assertTrue(rep["contaminated"])
        self.assertFalse(rep["params_changed"])
        self.assertEqual(len(rep["items"]), 3)
        self.assertEqual(rep["segment_hashes"], {k: S.segment_hash(k, "2026-04-01", PANEL.codes) for k in self.nb})
        with self.assertRaises(S.SelectionError):               # 同 label:先被配对账本拦下(LockedSegmentReuseError)
            self.hold(g, "final_v1")
        self.assertEqual(g.open_count(), 1)

    def test_guard_itself_still_rejects_same_label(self):
        g = self.guard()
        g.open("x")
        with self.assertRaises(LockedTestError):
            g.open("x")

    def test_bad_input_does_not_burn_the_one_shot(self):
        g = self.guard()
        with self.assertRaises(S.SelectionError):                 # 行落在封存区间之外
            self.hold(g, "x", rows=self.hold_rows + build(KEY, VALID[-3:]))
        t = copy.deepcopy(self.rec)
        t["survivors"] = []
        with self.assertRaises(S.SelectionError):                 # 记录被改
            self.hold(g, "x", rec=t)
        with self.assertRaises(S.SelectionError):                 # 缺面板
            S.holdout_report(self.hold_rows, self.dates, g, "x", self.rec, registry_path=self.rec_reg)
        self.assertEqual(g.open_count(), 0)
        self.assertFalse(S.ledger_path().exists())                 # 校验失败不写账本

    def test_guard_range_must_match_catalog_holdout(self):
        g = self.guard(test_start=self.t0 - 10)                    # 守卫封存区间起点早于 2026-04-01
        with self.assertRaises(S.SelectionError):
            self.hold(g, "x")
        self.assertEqual(g.open_count(), 0)

    def test_tampered_guard_log_blocks_access(self):
        g = self.guard()
        Path(g.log_path).write_text("not json\n", encoding="utf-8")
        with self.assertRaises(LockedTestError):
            self.hold(g, "x")


class TestFrozenRoster(Base):
    """返工审阅第1、2、4条:验证段/封存段核对冻结名单;输入指纹覆盖 weights/shares/next_date。"""

    def setUp(self):
        super().setUp()
        self.nb = [strategy_key("mom_01", {"n": n}) for n in (10, 20, 60)]
        self.strats = [{"id": "mom_01", "params": {"n": n}} for n in (10, 20, 60)]
        self.tr = [r for i, k in enumerate(self.nb) for r in build(k, TRAIN, mean=0.001, seed=10 + i)]
        self.reg = self.reg_path()
        self.rec = self.train(world(TRAIN, self.tr), self.strats, registry_path=self.reg)
        self.vrows = world(VALID, [r for i, k in enumerate(self.nb) for r in build(k, VALID, mean=0.002, seed=50 + i)])

    def forged(self, extra_key):
        # 手工往名单里塞一个策略并重新封装(哈希自洽,但冻结文件里没有)
        t = copy.deepcopy(self.rec)
        d = copy.deepcopy(t["decisions"][0])
        d["key"] = d["strategy_id"] = extra_key
        t["decisions"].append(d)
        t["survivors"].append(extra_key)
        return S._seal({k: v for k, v in t.items() if k != "record_sha256"})

    def test_forged_rewrapped_roster_rejected_by_validate_and_holdout(self):
        f = self.forged("evil|")
        S.verify_record(f)                                         # 自身哈希是自洽的
        with self.assertRaises(S.SelectionError):
            S.validate_survivors(self.vrows, f, panel=PANEL, null_k=NK, registry_path=self.reg)
        dates = TRAIN + VALID + HOLD
        t0 = dates.index("2026-04-01")
        split = Split(train=(0, len(TRAIN)), valid=(len(TRAIN), t0), test=(t0, len(dates)))
        g = LockedTestGuard(split, self.tmp / "locked_access_log.jsonl",
                            locked_fingerprint(dates[t0:], [0] * (len(dates) - t0)))
        with self.assertRaises(S.SelectionError):
            S.holdout_report(world(HOLD, build(KEY, HOLD)), dates, g, "x", f, panel=PANEL, null_k=NK,
                             registry_path=self.reg)
        self.assertEqual(g.open_count(), 0)                        # 没有消耗一次性授权

    def test_rerun_with_fresh_freeze_file_cannot_enter_validation(self):
        # 换一个冻结文件重跑训练,得到的新记录(试验编号不同)拿到原冻结名单面前过不了关
        # r4:固定账本里已有冻结判定,新路径的冻结文件是空的 -> 训练阶段就被锚点拦下
        with self.assertRaises(S.ElimRegistryTamperedError):
            self.train(world(TRAIN, self.tr), self.strats, registry_path=self.reg_path())
        S.validate_survivors(self.vrows, self.rec, panel=PANEL, null_k=NK, registry_path=self.reg)   # 原记录通过

    def test_missing_freeze_file_rejected(self):
        with self.assertRaises(S.SelectionError):
            S.validate_survivors(self.vrows, self.rec, panel=PANEL, null_k=NK, registry_path=self.tmp / "nope.json")

    def _changed(self, mutate):
        rows = copy.deepcopy(world(TRAIN, self.tr))      # world 不拷贝行,深拷贝避免改动串到后面的用例
        for r in rows:
            if r["key"] == self.nb[0]:
                mutate(r)
        rec = self.train(rows, self.strats, registry_path=self.reg)
        return next(d for d in rec["decisions"] if d["key"] == self.nb[0])

    def test_fingerprint_covers_weights_content_shares_next_date(self):
        # 条数不变、只改内容:指纹必须变(inputs_changed=True);结论仍读冻结
        d = self._changed(lambda r: r.update(weights={"Z": 1.0}) if r["weights"] else None)
        self.assertTrue(d["frozen"] and d["inputs_changed"])
        d = self._changed(lambda r: r.update(shares={"Z": 7}) if r["shares"] else None)
        self.assertTrue(d["inputs_changed"])
        d = self._changed(lambda r: r.update(next_date=r["next_date"][:-1] + "0"))
        self.assertTrue(d["inputs_changed"])
        d = self._changed(lambda r: None)                           # 不改:指纹不变
        self.assertFalse(d["inputs_changed"])

    def test_rows_fingerprint_and_call_params_recorded(self):
        self.assertTrue(self.rec["rows_fingerprint"])
        for d in self.rec["decisions"]:
            self.assertEqual(d["rows_fingerprint"], self.rec["rows_fingerprint"])
            self.assertEqual(d["null_k"], NK)
            self.assertEqual(d["seed"], "0")


class TestR4Fixes(Base):
    """第 3 次返工(审阅 r4)6 条意见的独立验收测试。"""

    def setUp(self):
        super().setUp()
        self.nb = [strategy_key("mom_01", {"n": n}) for n in (10, 20, 60)]
        self.strats = [{"id": "mom_01", "params": {"n": n}} for n in (10, 20, 60)]
        self.tr = [r for i, k in enumerate(self.nb) for r in build(k, TRAIN, mean=0.001, seed=10 + i)]
        self.reg = self.reg_path()
        self.rec = self.train(world(TRAIN, self.tr), self.strats, registry_path=self.reg, seed=42)
        self.vrows = world(VALID, [r for i, k in enumerate(self.nb) for r in build(k, VALID, mean=0.002, seed=50 + i)])
        self.dates = TRAIN + VALID + HOLD
        self.hold_rows = world(HOLD, [r for i, k in enumerate(self.nb) for r in build(k, HOLD, mean=0.002, seed=70 + i)])

    def guard(self, name="locked_access_log.jsonl", end=None):
        n = len(self.dates) if end is None else end
        t0 = self.dates.index("2026-04-01")
        split = Split(train=(0, len(TRAIN)), valid=(len(TRAIN), t0), test=(t0, n))
        return LockedTestGuard(split, self.tmp / name, locked_fingerprint(self.dates[t0:n], [0] * (n - t0)))

    # r4-Fix 1
    def test_valid_m_not_from_record(self):
        forged = copy.deepcopy(self.rec)
        forged["m_trials"] = 25                                  # 真实应是 490
        forged = S._seal({k: v for k, v in forged.items() if k != "record_sha256"})
        S.verify_record(forged)                                  # 重新封装后自身哈希自洽
        with self.assertRaises(S.RecordTamperedError):
            S.validate_survivors(self.vrows, forged, panel=PANEL, registry_path=self.reg)
        g = self.guard()
        with self.assertRaises(S.RecordTamperedError):           # 封存段同样被拦,且不消耗授权
            S.holdout_report(self.hold_rows, self.dates, g, "x", forged, panel=PANEL, registry_path=self.reg)
        self.assertEqual(g.open_count(), 0)
        self.assertEqual(S.validate_survivors(self.vrows, self.rec, panel=PANEL, registry_path=self.reg)["m_trials"], 490)

    # r4-Fix 2
    def test_rows_fingerprint_covers_excess(self):
        def changed(delta):
            rows = copy.deepcopy(world(TRAIN, self.tr))
            for r in rows:
                if r["key"] == self.nb[0]:
                    r["excess"] += delta
            return rows
        with self.assertRaises(S.ExcessInconsistentError):       # 只改 excess、net/gross/cost/mkt 不变
            self.train(changed(0.001), self.strats, registry_path=self.reg, seed=42)
        # 差在容差之内也不能悄悄过去:指纹含 excess,inputs_changed 必须为 True
        rec = self.train(changed(5e-10), self.strats, registry_path=self.reg, seed=42)
        self.assertTrue(next(d for d in rec["decisions"] if d["key"] == self.nb[0])["inputs_changed"])
        rec = self.train(changed(0.0), self.strats, registry_path=self.reg, seed=42)
        self.assertFalse(next(d for d in rec["decisions"] if d["key"] == self.nb[0])["inputs_changed"])

    # r4-Fix 3
    def test_frozen_registry_cannot_be_bypassed_by_new_path(self):
        log = self.new_log("anchor.jsonl")
        path_a, path_b = self.reg_path(), self.reg_path()
        rows = world(TRAIN, build(KEY, TRAIN, mean=-0.004, cost=0.0))
        first = self.train(rows, registry_path=path_a, log=log)
        self.assertEqual(first["eliminated"], [KEY])
        self.assertEqual(log.count(), 1)
        with self.assertRaises(S.ElimRegistryTamperedError):     # 全新路径:冻结为空,但固定账本里已有淘汰记录
            self.train(world(TRAIN, build(KEY, TRAIN, mean=0.01)), registry_path=path_b, log=log)
        self.assertFalse(path_b.exists())
        self.assertEqual(log.count(), 1)
        path_a.rename(self.tmp / "moved.json")                   # 原路径文件被挪走/删除,同样被拦
        with self.assertRaises(S.ElimRegistryTamperedError):
            self.train(rows, registry_path=path_a, log=log)

    def test_anchor_ignores_undetermined_and_other_catalog(self):
        log = self.new_log("anchor2.jsonl")
        self.train(world(TRAIN, build(KEY, TRAIN, mean=0.001)), registry_path=self.reg_path(), log=log, null_k=10)
        self.assertEqual(log.count(), 1)                         # undetermined 记了试验但不冻结,不应触发锚点
        self.train(world(TRAIN, build(KEY, TRAIN, mean=0.001)), registry_path=self.reg_path(), log=log)
        # 别的清单版本的冻结记录不属于当前清单,不触发锚点
        log.record("mom_01", {"n": 5}, "m", 0.0, 1, tag="T008-S4|train|aaaaaaaaaaaa|eliminated")
        self.train(world(TRAIN, build(KEY, TRAIN, mean=0.001)), registry_path=self.reg, log=log)
        self.assertEqual(log.count(), 3)

    def test_trial_log_must_be_the_fixed_path(self):
        other = TrialLog(self.tmp / "elsewhere.jsonl")           # 固定路径指向别处,传入的账本不是它
        with self.assertRaises(S.SelectionError):
            S.train_eliminate(world(TRAIN, build(KEY, TRAIN)), [STRAT], other, mono_report=mono_for([STRAT]),
                              panel=PANEL, registry_path=self.reg_path(), null_k=NK)

    # r4-Fix 4
    def test_locked_segment_endpoint_bypass(self):
        g1 = self.guard("log1.jsonl")
        S.holdout_report(self.hold_rows, self.dates, g1, "a", self.rec, panel=PANEL, registry_path=self.reg)
        end = len(self.dates) - 5                                # 同一封存起点,终点往前挪 5 天
        dates2 = self.dates[:end]
        rows2 = [r for r in self.hold_rows if r["date"] <= dates2[-1]]
        g2 = self.guard("log2.jsonl", end=end)
        with self.assertRaises(S.LockedSegmentReuseError):
            S.holdout_report(rows2, dates2, g2, "b", self.rec, panel=PANEL, registry_path=self.reg)
        self.assertEqual(g2.open_count(), 0)                     # 先写账本后 open:被拦时没有消耗授权

    def test_locked_segment_start_shift_bypass(self):
        # r5-Fix 1:守卫区间起点往后挪一天(换日志、换 label)也是同一个封存段,唯一键用清单写死的封存起点
        g1 = self.guard("log1.jsonl")
        S.holdout_report(self.hold_rows, self.dates, g1, "a", self.rec, panel=PANEL, registry_path=self.reg)
        t1 = self.dates.index("2026-04-01") + 1
        n = len(self.dates)
        split = Split(train=(0, len(TRAIN)), valid=(len(TRAIN), t1), test=(t1, n))
        g2 = LockedTestGuard(split, self.tmp / "log2.jsonl", locked_fingerprint(self.dates[t1:], [0] * (n - t1)))
        rows2 = [r for r in self.hold_rows if r["date"] >= self.dates[t1]]
        with self.assertRaises(S.LockedSegmentReuseError):
            S.holdout_report(rows2, self.dates, g2, "b", self.rec, panel=PANEL, registry_path=self.reg)
        self.assertEqual(g2.open_count(), 0)

    def test_frozen_rerun_with_new_seed_cannot_enter_validation(self):
        # r5-Fix 2:同一冻结文件换 seed 重跑训练(全走冻结、不留试验记录),新记录顶层 seed 与冻结条目不符
        rec2 = self.train(world(TRAIN, self.tr), self.strats, registry_path=self.reg, seed=99)
        self.assertEqual(rec2["trials_recorded"], 0)
        self.assertEqual(rec2["seed"], "99")
        with self.assertRaises(S.SeedMismatchError):
            S.validate_survivors(self.vrows, rec2, panel=PANEL, registry_path=self.reg)
        g = self.guard("log9.jsonl")
        with self.assertRaises(S.SeedMismatchError):
            S.holdout_report(self.hold_rows, self.dates, g, "x", rec2, panel=PANEL, registry_path=self.reg)
        self.assertEqual(g.open_count(), 0)
        S.validate_survivors(self.vrows, self.rec, panel=PANEL, registry_path=self.reg)      # 原记录仍可用

    def test_locked_segment_ledger_fixed_path(self):
        custom = self.tmp / "custom" / "custom.jsonl"
        custom.parent.mkdir()
        g = self.guard("custom/custom.jsonl")
        with self.assertRaises(TypeError):                       # 调用方不能传账本路径
            S.holdout_report(self.hold_rows, self.dates, g, "a", self.rec, panel=PANEL, registry_path=self.reg,
                             log_path="/tmp/custom.jsonl")
        S.holdout_report(self.hold_rows, self.dates, g, "a", self.rec, panel=PANEL, registry_path=self.reg)
        self.assertEqual(S.ledger_path(), self.tmp / "locked_segment_ledger.jsonl")     # 来自环境变量,与守卫日志无关
        self.assertTrue(S.ledger_path().exists())
        self.assertEqual([p.name for p in custom.parent.iterdir()], ["custom.jsonl"])    # 守卫日志旁边没有账本
        self.assertEqual(S.LEDGER_DEFAULT.parts[-4:], ("handoff", "data", "replay", "locked_segment_ledger.jsonl"))

    def test_ledger_pending_then_committed_or_failed(self):
        g = self.guard()
        S.holdout_report(self.hold_rows, self.dates, g, "a", self.rec, panel=PANEL, registry_path=self.reg)
        evs = [json.loads(x) for x in S.ledger_path().read_text(encoding="utf-8").splitlines()]
        self.assertEqual([e["event"] for e in evs], ["pending"] * 3 + ["committed"] * 3)
        for e in evs:
            self.assertTrue(e["strategy_id"] and e["segment_start"] == "2026-04-01" and e["panel_codes_hash"]
                            and e["timestamp"])
        S.ledger_path().unlink()
        g2 = self.guard("log3.jsonl")
        Path(g2.log_path).write_text("not json\n", encoding="utf-8")        # 守卫日志被破坏:open 失败
        with self.assertRaises(LockedTestError):
            S.holdout_report(self.hold_rows, self.dates, g2, "a", self.rec, panel=PANEL, registry_path=self.reg)
        evs = [json.loads(x)["event"] for x in S.ledger_path().read_text(encoding="utf-8").splitlines()]
        self.assertEqual(evs, ["pending"] * 3 + ["failed"] * 3)

    def test_unparseable_ledger_blocks(self):
        S.ledger_path().write_text("garbage\n", encoding="utf-8")
        g = self.guard()
        with self.assertRaises(S.SelectionError):
            S.holdout_report(self.hold_rows, self.dates, g, "a", self.rec, panel=PANEL, registry_path=self.reg)
        self.assertEqual(g.open_count(), 0)

    # r4-Fix 5
    def test_valid_seed_from_record(self):
        self.assertEqual(self.rec["seed"], "42")
        with self.assertRaises(S.SeedMismatchError):
            S.validate_survivors(self.vrows, self.rec, panel=PANEL, seed=99, registry_path=self.reg)
        with self.assertRaises(S.SeedMismatchError):
            S.validate_survivors(self.vrows, self.rec, panel=PANEL, null_k=NK + 1, registry_path=self.reg)
        g = self.guard()
        with self.assertRaises(S.SeedMismatchError):
            S.holdout_report(self.hold_rows, self.dates, g, "x", self.rec, panel=PANEL, seed=99, registry_path=self.reg)
        self.assertEqual(g.open_count(), 0)
        a = S.validate_survivors(self.vrows, self.rec, panel=PANEL, registry_path=self.reg)         # 默认取记录里的值
        b = S.validate_survivors(self.vrows, self.rec, panel=PANEL, seed=42, null_k=NK, registry_path=self.reg)
        self.assertEqual(a, b)
        self.assertEqual(a["null_k"], NK)


if __name__ == "__main__":
    unittest.main(verbosity=1)
