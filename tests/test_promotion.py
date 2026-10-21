"""主策略晋升规则测试：python tests/test_promotion.py（需要 numpy，用 .venv/bin/python 跑）

合成账本：每天 300 只股票，5 日收益 ~ N(0, 5%)。
  good  —— 每天买入当天涨得最多的 10 只（人为的“有效”策略）
  bad   —— 每天买入当天涨得最少的 10 只
  noise —— 每天随机买 10 只
"""
import json, random, sys, tempfile, unittest
from datetime import date, timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from src import signal_ledger as L
from src import promotion as PR
from src import auto_tune as _A

_ISO = Path(tempfile.mkdtemp(prefix="promo_test_"))      # 默认路径全部指到临时目录，绝不写 data/ 下的真实文件
L.DB, L.OUT, L.MAIN_FILE = _ISO / "signal_ledger.db", _ISO / "ledger", _ISO / "main_strategy.json"
_A.PARAMS, _A.LOG = _ISO / "auto_params.json", _ISO / "ledger" / "tune_log.jsonl"
from realdata_guard import ZRealDataUntouchedTests  # noqa: E402,F401  真实数据守卫

COST = L.ROUND_TRIP_BP / 1e4


def build(n_weeks, per_day=10, stocks=300, seed=1, good_edge_days=None, extra=()):
    """返回 (连接, 日历, today)。today = 最后一周之后的下一个周一，所以 n_weeks 周都是“完整周”。"""
    c = L.connect(Path(tempfile.mkdtemp()) / "p.db")
    cal, d = [], date(2026, 1, 5)
    while len(cal) < n_weeks * 5:
        if d.weekday() < 5:
            cal.append(d.isoformat())
        d += timedelta(days=1)
    rng = random.Random(seed)
    for i, day in enumerate(cal):
        c.execute("INSERT INTO days VALUES(?,?,?)", (day, 1, "x"))
        c.execute("INSERT INTO done VALUES(?,?,?)", (day, 5, stocks))
        rets = [rng.gauss(0, 0.05) for _ in range(stocks)]
        c.executemany("INSERT INTO moves VALUES(?,?,?,?,?,?,?)", [(day, f"6{k:05d}", 5, r, 0.0, 0, 0) for k, r in enumerate(rets)])
        order = sorted(range(stocks), key=lambda k: rets[k])
        noise_pick = rng.sample(range(stocks), per_day)
        picks = {"good": order[-per_day:] if (good_edge_days is None or i < good_edge_days) else rng.sample(range(stocks), per_day),
                 "bad": order[:per_day], "noise": noise_pick}
        for k2 in extra:
            picks[k2] = rng.sample(range(stocks), per_day)
        for st, ks in picks.items():
            c.executemany("INSERT INTO signals VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                          [(day, st, f"6{k:05d}", "", "买入", 0, 0.5, 10, "正常", 0, "[]") for k in ks])
    c.commit()
    today = (date.fromisoformat(cal[-1]) + timedelta(days=3)).isoformat()
    return c, cal, today


def prep(c, today):
    PR.register(c)
    PR.ensure_null(c)
    return PR.promotion_status(c, today)


def main_file(main="blend2", promoted_on="2025-12-01", default=True):
    p = Path(tempfile.mkdtemp()) / "m.json"
    p.write_text(json.dumps({"main": main, "promoted_on": promoted_on, "default": default, "history": []}), encoding="utf-8")
    return p


class Ranking(unittest.TestCase):
    def test_insufficient_sample_not_ranked(self):
        c, cal, today = build(2)                                   # 10 个信号日 × 10 条 = 100 条，刚好够
        b = L.strategy_board(c)
        self.assertTrue(b["good"]["h5"]["enough"]); self.assertEqual(b["good"]["h5"]["rank"], 1)
        c, cal, today = build(2)
        c.execute("DELETE FROM signals WHERE day=?", (cal[0],)); c.commit()     # 少一天 → 9 个信号日、90 条
        b = L.strategy_board(c)
        for s in ("good", "bad", "noise"):
            self.assertFalse(b[s]["h5"]["enough"], s); self.assertIsNone(b[s]["h5"]["rank"], s)

    def test_few_signals_not_ranked_even_with_many_days(self):
        c, cal, today = build(3, per_day=3)                        # 15 天但每天 3 条 = 45 条 < 100
        self.assertFalse(L.strategy_board(c)["good"]["h5"]["enough"])

    def test_ranking_order_and_controls_unranked(self):
        c, cal, today = build(4, extra=("ctrl_random_a",))
        b = L.strategy_board(c)
        self.assertEqual(b["good"]["h5"]["rank"], 1); self.assertEqual(b["bad"]["h5"]["rank"], 3)
        self.assertIsNone(b["ctrl_random_a"]["h5"]["rank"])        # 随机对照不参与排名
        self.assertIsNotNone(b["good"]["h5"]["hit_lo"]); self.assertLess(b["good"]["h5"]["hit_lo"], b["good"]["h5"]["hit"])

    def test_page_data_flags_insufficient(self):
        c, cal, today = build(1)
        st = L.strategy_board(c)["noise"]["h5"]
        self.assertFalse(st["enough"]); self.assertIn("hit_lo", st); self.assertGreater(st["n"], 0)


class NullControl(unittest.TestCase):
    def test_null_is_matched_and_stored(self):
        c, cal, today = build(3)
        PR.register(c); written = PR.ensure_null(c)
        self.assertEqual(written, 3 * 15)                           # 3 个策略 × 15 个信号日
        self.assertEqual(PR.ensure_null(c), 0)                      # 重跑不重复
        r = c.execute("SELECT * FROM null_draws WHERE strategy='noise' AND day=?", (cal[0],)).fetchone()
        self.assertEqual((r["n"], r["nb"], r["ns"], r["k"]), (10, 10, 0, 1000))
        self.assertEqual(len(r["sums"]), 8 * 1000)

    def test_own_score_equals_ledger_score(self):
        c, cal, today = build(3)
        PR.register(c); PR.ensure_null(c)
        for s in ("good", "noise"):
            a = [x for _, x, _ in L.scored(c, s, 5)]
            tot = c.execute("SELECT SUM(own_sum) a, SUM(n) n, SUM(own_hits) h FROM null_draws WHERE strategy=?", (s,)).fetchone()
            self.assertEqual(tot["n"], len(a))
            self.assertAlmostEqual(tot["a"], sum(a), places=9)
            self.assertEqual(tot["h"], sum(1 for _, _, k in L.scored(c, s, 5) if k))

    def test_null_mean_is_minus_cost_for_buys_and_hit_is_half(self):
        """纯买入的随机对照：每条信号的超额平均 0，所以得分平均 ≈ −来回费用，命中率 ≈ 50%。"""
        c, cal, today = build(8)
        st = prep(c, today)["strategies"]["noise"]
        self.assertAlmostEqual(st["ctrl_mean"], -COST, delta=0.004)
        self.assertAlmostEqual(st["ctrl_hit"], 0.5, delta=0.04)
        self.assertLess(st["ctrl_lo"], st["ctrl_mean"]); self.assertGreater(st["ctrl_hi"], st["ctrl_mean"])

    def test_sells_are_matched_and_pay_no_cost(self):
        c, cal, today = build(3)
        c.execute("UPDATE signals SET action='卖出' WHERE strategy='noise'"); c.commit()
        PR.register(c); PR.ensure_null(c)
        r = c.execute("SELECT nb, ns FROM null_draws WHERE strategy='noise' AND day=?", (cal[0],)).fetchone()
        self.assertEqual((r["nb"], r["ns"]), (0, 10))
        s = PR.promotion_status(c, today)["strategies"]["noise"]
        self.assertAlmostEqual(s["ctrl_mean"], 0.0, delta=0.006)    # 卖出不扣费用

    def test_limit_up_buys_excluded_from_both_sides(self):
        c, cal, today = build(2)
        c.execute("UPDATE moves SET lim=1 WHERE day=? AND code IN (SELECT code FROM signals WHERE strategy='good' AND day=?)", (cal[0], cal[0]))
        c.commit()
        PR.register(c); PR.ensure_null(c)
        self.assertIsNone(c.execute("SELECT 1 FROM null_draws WHERE strategy='good' AND day=?", (cal[0],)).fetchone())  # 全是买不进的 → 当天不计
        self.assertIsNotNone(c.execute("SELECT 1 FROM null_draws WHERE strategy='good' AND day=?", (cal[1],)).fetchone())

    def test_good_beats_all_controls_noise_and_bad_do_not(self):
        c, cal, today = build(16)
        s = prep(c, today)["strategies"]
        self.assertAlmostEqual(s["good"]["p"], 1 / 1001)             # 1000 个对照里一个都没超过 → 经验 p 的最小值
        self.assertGreater(s["good"]["advantage"], 0.05); self.assertEqual(s["good"]["noise"], "显著高于随机")
        self.assertGreater(s["good"]["hit_diff"], 0.3)
        self.assertGreater(s["noise"]["p"], 0.01); self.assertEqual(s["noise"]["noise"], "在噪声范围内")
        self.assertEqual(s["bad"]["noise"], "显著低于随机"); self.assertGreater(s["bad"]["p"], 0.99)

    def test_empirical_p_formula(self):
        import numpy as np
        nl = np.arange(1000, dtype=float)
        self.assertAlmostEqual(PR._emp_p(5000.0, nl), 1 / 1001)
        self.assertAlmostEqual(PR._emp_p(997.5, nl), 3 / 1001)       # 有 2 个对照（998、999）更高
        self.assertAlmostEqual(PR._emp_p(-1.0, nl), 1.0)


class Gate(unittest.TestCase):
    def test_holm_needs_p_below_0_002_at_m25(self):
        self.assertEqual(PR.holm({"a": 2 / 1001}, 25), {"a"})        # 最多 1 个对照比它高 → 过
        self.assertEqual(PR.holm({"a": 3 / 1001}, 25), set())        # 2 个对照比它高 → 不过
        self.assertEqual(PR.holm({"a": 3 / 1001}, 1), {"a"})         # 同样的 p 如果不做多重检验就“显著”了
        self.assertEqual(PR.holm({"a": 0.001, "b": 0.0021}, 25), {"a"})   # 第二步要求 ≤ 0.05/24 = 0.00208

    def test_m_counts_every_strategy_ever_seen_and_not_controls(self):
        c, cal, today = build(2, extra=("ctrl_random_a",))
        PR.register(c)
        self.assertEqual(c.execute("SELECT COUNT(*) n FROM strategy_registry").fetchone()["n"], 3)
        self.assertEqual(PR.n_hypotheses(c), 25)                     # 下限 25
        for i in range(30):                                          # 曾经试过 30 个别的策略
            c.execute("INSERT INTO strategy_registry VALUES(?,?)", (f"old{i}", "2026-01-01"))
        c.execute("DELETE FROM signals WHERE strategy='bad'"); c.commit()
        PR.register(c)
        self.assertEqual(PR.n_hypotheses(c), 33)                     # 被淘汰/不再出现的也计入，对照不计

    def test_no_promotion_before_12_weeks(self):
        c, cal, today = build(11)
        status = prep(c, today)
        g = status["strategies"]["good"]
        self.assertFalse(g["data_ok"]); self.assertIn("数据不足", g["status"]); self.assertEqual(g["streak"], 0)
        self.assertFalse(status["any_passed"]); self.assertEqual(status["message"], "尚无策略通过晋升检验")
        r = L.choose_main(c, L.strategy_board(c), today, path=main_file(), status=status)
        self.assertEqual(r["main"], "blend2"); self.assertEqual(r["history"], [])

    def test_no_promotion_without_4_consecutive_checkpoints(self):
        c, cal, today = build(13)                                    # 检查点是第 10、11、12、13 周：只有后两周数据够
        status = prep(c, today)
        g = status["strategies"]["good"]
        self.assertTrue(g["holm_pass"]); self.assertEqual(g["streak"], 2)
        self.assertIn("通过 2/4", g["status"]); self.assertEqual(g["weeks_left"], 2)
        self.assertEqual(status["eligible"], [])
        self.assertTrue(status["any_passed"])
        r = L.choose_main(c, L.strategy_board(c), today, path=main_file(), status=status)
        self.assertEqual(r["main"], "blend2")

    def test_promotes_good_only_after_gate_and_sets_promoted_on(self):
        c, cal, today = build(16)
        status = prep(c, today)
        self.assertEqual(status["eligible"], ["good"])               # noise / bad 不过
        p = main_file()
        r = L.choose_main(c, L.strategy_board(c), today, path=p, status=status)
        self.assertEqual(r["main"], "good"); self.assertEqual(r["promoted_on"], today); self.assertFalse(r["default"])
        self.assertEqual(r["history"][-1]["from"], "blend2"); self.assertEqual(r["history"][-1]["promoted_on"], today)
        self.assertEqual(json.loads(p.read_text(encoding="utf-8"))["promoted_on"], today)

    def test_incumbent_tenure_blocks_switch(self):
        c, cal, today = build(16)
        status = prep(c, today)
        recent = (date.fromisoformat(today) - timedelta(days=30)).isoformat()
        r = L.choose_main(c, L.strategy_board(c), today, path=main_file(promoted_on=recent), status=status)
        self.assertEqual(r["main"], "blend2"); self.assertIn("任期", r["reason"])

    def test_must_beat_incumbent_when_incumbent_also_passes(self):
        c, cal, today = build(16)
        c.execute("INSERT INTO signals SELECT day,'good2',code,name,action,now,next,close,liquidity,small,flags FROM signals WHERE strategy='good'")
        c.commit()                                                   # good2 与 good 信号完全相同：同样通过检验，但不比现任强
        status = prep(c, today)
        self.assertEqual(sorted(status["eligible"]), ["good", "good2"])
        r = L.choose_main(c, L.strategy_board(c), today, path=main_file(main="good2", default=False), status=status)
        self.assertEqual(r["main"], "good2"); self.assertIn("优势不够", r["reason"])      # 现任也过关，挑战者没有多 0.3% → 不换
        # 现任 noise 不通过检验 → 只要挑战者自己过关就可以换（不需要再比现任）
        r = L.choose_main(c, L.strategy_board(c), today, path=main_file(main="noise", default=False), status=status)
        self.assertIn(r["main"], ("good", "good2")); self.assertIn("已不通过检验", r["reason"])
        # 现任通过检验、挑战者明显更强（周配对多 ≥0.3% 且 t≥2）→ 换
        c.execute("DELETE FROM signals WHERE strategy='good2'")
        for day in cal:                                              # mid：每天买涨幅排名第 30~39 的股票 → 仍然显著，但比 good（前 10 名）弱得多
            codes = [r["code"] for r in c.execute("SELECT code FROM moves WHERE day=? AND h=5 ORDER BY ret DESC LIMIT 10 OFFSET 30", (day,))]
            c.executemany("INSERT INTO signals VALUES(?,?,?,?,?,?,?,?,?,?,?)", [(day, "mid", k, "", "买入", 0, 0.5, 10, "正常", 0, "[]") for k in codes])
        c.commit()
        PR.register(c); PR.ensure_null(c)
        status = PR.promotion_status(c, today)
        self.assertTrue(status["strategies"]["mid"]["holm_pass"])
        r = L.choose_main(c, L.strategy_board(c), today, path=main_file(main="mid", default=False), status=status)
        self.assertEqual(r["main"], "good"); self.assertIn("每条信号平均多", r["reason"])

    def test_insufficient_incumbent_does_not_hand_title_to_leader(self):
        """旧规则：现任样本不足就直接换成得分最高者。新规则：没通过检验的领先者也不能上。"""
        c, cal, today = build(5)
        status = prep(c, today)
        r = L.choose_main(c, L.strategy_board(c), today, path=main_file(main="blend2"), status=status)
        self.assertEqual(r["main"], "blend2")
        self.assertTrue(L.strategy_board(c)["good"]["h5"]["enough"])  # 榜单上 good 样本够、得分第一，但这不等于晋升
        self.assertEqual(L.strategy_board(c)["good"]["h5"]["rank"], 1)

    def test_shadow_candidates_cannot_become_main(self):
        from src import shadow
        name = next(k for k in shadow.CANDIDATES if not k.startswith("ctrl_"))
        c, cal, today = build(16)
        c.execute("UPDATE signals SET strategy=? WHERE strategy='good'", (name,)); c.commit()
        status = prep(c, today)
        self.assertIn(name, status["eligible"])                      # 检验上它是过的
        r = L.choose_main(c, L.strategy_board(c), today, path=main_file(), status=status)
        self.assertEqual(r["main"], "blend2")                        # 但影子候选没有完整解释，不能当主策略

    def test_promotion_error_means_no_change(self):
        c, cal, today = build(16)
        r = L.choose_main(c, L.strategy_board(c), today, path=main_file(), status={"error": "boom", "strategies": {}, "eligible": [], "any_passed": False})
        self.assertEqual(r["main"], "blend2")


class MDE(unittest.TestCase):
    def test_mde_and_weeks_left_when_short(self):
        c, cal, today = build(8)
        s = prep(c, today)["strategies"]["good"]
        self.assertEqual(s["weeks"], 8)
        self.assertGreater(s["mde"], 0)
        self.assertAlmostEqual(s["mde_12w"], s["mde"] * (8 / 12) ** 0.5)       # 噪声按 1/√周数收窄
        self.assertAlmostEqual(s["mde_26w"], s["mde"] * (8 / 26) ** 0.5)
        self.assertEqual(s["weeks_left"], 12 + 4 - 1 - 8)                       # 首次达标最早第 12 周，再连续 4 个检查点
        self.assertGreater(s["advantage"], s["mde"])                            # 有效策略的优势高于最小可检测效应

    def test_mde_matches_second_largest_control(self):
        c, cal, today = build(8)
        PR.register(c); PR.ensure_null(c)
        wk = PR.load_weekly(c, "noise", PR.complete_weeks(c, today))
        own_c, null_c = PR._cum(wk)
        md = PR.mde_info(own_c, null_c, 25)
        self.assertTrue(md["possible"])
        self.assertAlmostEqual(md["crit"], sorted(null_c[-1])[-2])               # m=25 时最多允许 1 个对照更高 → 要超过第 2 大
        self.assertAlmostEqual(md["mde"], md["crit"] - null_c[-1].mean())
        self.assertFalse(PR.mde_info(own_c, null_c, 60)["possible"])             # m>50 时 1000 个对照也不够

    def test_strategy_without_edge_has_no_catch_up_estimate(self):
        c, cal, today = build(8)
        s = prep(c, today)["strategies"]["bad"]
        self.assertIsNone(s["weeks_left"])                                       # 优势 ≤ 0：现有数据下追不上

    def test_no_data(self):
        c, cal, today = build(1)
        st = PR.promotion_status(c, cal[0])                                      # 第一周还没结束
        self.assertEqual(st["complete_weeks"], 0)
        self.assertEqual(st["message"], "尚无策略通过晋升检验")


class PrePost(unittest.TestCase):
    def test_pre_and_post_are_separate(self):
        c, cal, today = build(16, good_edge_days=40)                 # good 只在前 40 个信号日有“本事”，之后变成随机
        PR.register(c); PR.ensure_null(c)
        promoted_on = cal[39]
        p = main_file(main="good", promoted_on=promoted_on, default=False)
        st = PR.promotion_status(c, today)
        r = L.choose_main(c, L.strategy_board(c), today, path=p, status=st)
        rec = r["record"]
        pre, post = rec["pre"], rec["post"]
        self.assertEqual(rec["promoted_on"], promoted_on)
        self.assertEqual(pre["n"], 400); self.assertEqual(post["n"], 400)         # 晋升日当天算晋升前，不重复、不遗漏
        self.assertEqual(pre["days"], 40); self.assertEqual(post["days"], 40)
        self.assertGreater(pre["hit"], 0.9)                                       # 晋升前（被选中的依据）漂亮
        self.assertAlmostEqual(post["hit"], 0.5, delta=0.1)                       # 晋升后（正式成绩）回到随机水平
        self.assertEqual(pre["vs_control"]["noise"], "显著高于随机")
        self.assertEqual(post["vs_control"]["noise"], "在噪声范围内")
        self.assertIn("正式成绩从晋升日之后", rec["note"])

    def test_default_blend2_marked_and_formal_record_starts_after_deploy(self):
        c, cal, today = build(3)
        p = Path(tempfile.mkdtemp()) / "none.json"                               # 没有文件：第一次运行
        c.execute("INSERT INTO signals SELECT day,'blend2',code,name,action,now,next,close,liquidity,small,flags FROM signals WHERE strategy='noise'")
        c.commit()
        PR.register(c); PR.ensure_null(c)
        r = L.choose_main(c, L.strategy_board(c), today, path=p)
        self.assertEqual(r["promoted_on"], L.DEFAULT_PROMOTED_ON); self.assertTrue(r["default"])
        self.assertEqual(r["default_note"], "默认策略，未经晋升检验")
        self.assertEqual(r["record"]["post"]["n"], 0)                            # 2026-01 的信号都在上线日之前 → 不算正式成绩
        self.assertGreater(r["record"]["pre"]["n"], 0)


class SameParams(unittest.TestCase):
    def test_shadow_signals_use_the_day_params_of_official_list(self):
        from src import shadow
        c = L.connect(Path(tempfile.mkdtemp()) / "s.db")
        L.record_day(c, {"data_date": "2026-03-02", "threshold": 0.20, "min_target": 0.10, "strategies": {}})
        c.execute("INSERT INTO state VALUES(?,?,?,?,?)", ("2026-03-02", "mom_3m", "600001", 0.30, 0.45))     # 加仓 15 个百分点
        c.execute("INSERT INTO state VALUES(?,?,?,?,?)", ("2026-03-03", "mom_3m", "600001", 0.30, 0.45))
        c.commit()
        self.assertEqual(shadow.record_signals(c, "2026-03-02"), 0)              # 当天正式清单阈值 20% → 15 点不算
        self.assertEqual(shadow.record_signals(c, "2026-03-03"), 1)              # 没有记录 → 默认 10%


class Tune(unittest.TestCase):
    def test_frozen_before_20_weeks(self):
        import test_signal_ledger as TS
        from src import auto_tune as A
        self.assertEqual(A.FREEZE_WEEKS, 20)
        _, c = TS.Tune.build(None, 12, True)                         # 这组数据在旧规则下会换参数
        sp = Path(tempfile.mkdtemp()) / "s.json"
        d = A.tune(c, TS.CAL[20], state_path=sp, log_path=Path(tempfile.mkdtemp()) / "l.jsonl")
        self.assertEqual(d["action"], "keep"); self.assertIn("冻结", d["why"])
        self.assertEqual(A.load_state(sp)["active"], A.DEFAULT)

    def test_freeze_resets_hand_edited_params(self):
        import test_signal_ledger as TS
        from src import auto_tune as A
        _, c = TS.Tune.build(None, 12, True)
        sp = Path(tempfile.mkdtemp()) / "s.json"
        A.save_state({"active": {"thr": 0.2, "mt": 0.1}, "history": [], "banned": {}}, sp)
        A.tune(c, TS.CAL[20], state_path=sp, log_path=Path(tempfile.mkdtemp()) / "l.jsonl")
        self.assertEqual(A.load_state(sp)["active"], A.DEFAULT)

    def test_only_execution_layer_params(self):
        from src import auto_tune as A
        self.assertEqual(set(A.BOUNDS), {"thr", "mt"}); self.assertEqual(set(A.DEFAULT), {"thr", "mt"})
        self.assertEqual(set(A.clamp({"thr": 0.15, "mt": 0.1, "sma_len": 7, "formula": "x"})), {"thr", "mt"})   # 任何别的键进不来
        self.assertEqual(A.TARGET, "blend2")

    def test_threshold_grows_stricter_with_fewer_weeks(self):
        from src import auto_tune as A
        a = 0.05 / 11
        self.assertGreater(A.t_crit(a, 3), A.t_crit(a, 19)); self.assertGreater(A.t_crit(a, 19), 2.5)


class EndToEnd(unittest.TestCase):
    def test_run_writes_promotion_block(self):
        import test_signal_ledger as TS
        db, out, mf = Path(tempfile.mkdtemp()) / "t.db", Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp()) / "m.json"
        bars = {f"6{i:05d}": TS.flat() for i in range(150)}
        bars.update({"600901": TS.rising(), "600902": TS.rising()})
        kw = dict(provider=TS.P(bars), cal=TS.CAL, db=db, out=out, main_file=mf, tune_state=Path(tempfile.mkdtemp()) / "s.json",
                  tune_log=Path(tempfile.mkdtemp()) / "l.jsonl")
        L.run({"data_date": TS.D0, "threshold": 0.1, "min_target": 0.05, "strategies": {"blend2": {"rows": [TS.row("600901", "买入"), TS.row("600902", "买入")]}}}, **kw)
        rep = L.run({"data_date": TS.CAL[20], "strategies": {"blend2": {"rows": []}}}, **kw)
        self.assertEqual(rep["promotion"]["message"], "尚无策略通过晋升检验")
        self.assertFalse(rep["main_pick"]["any_passed"]); self.assertTrue(rep["main_pick"]["default"])
        self.assertIn("blend2", rep["promotion"]["strategies"])
        json.dumps(rep)                                                          # 能序列化
        self.assertEqual(json.loads(mf.read_text(encoding="utf-8"))["main"], "blend2")
        self.assertEqual(rep["tune"]["action"], "keep")
        c = L.connect(db)
        self.assertEqual(c.execute("SELECT thr FROM day_params WHERE day=?", (TS.D0,)).fetchone()["thr"], 0.1)


class PageRender(unittest.TestCase):
    """用 Node 把 dashboard/app.js 里的 renderBoard 跑一遍（没有 node 就跳过）：三种状态页面上该出现的字。"""
    JS = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
const a = src.indexOf("function renderBoard(d) {"), b = src.indexOf("function renderTune(t) {");
const isNum = (v) => typeof v === "number" && isFinite(v), esc = (s) => String(s), S = { meta: null };
eval(src.slice(a, b) + "; globalThis.renderBoard = renderBoard;");
const reps = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const out = {};
for (const k of Object.keys(reps)) out[k] = renderBoard(reps[k]).replace(/<[^>]+>/g, " ").replace(/\s+/g, " ");
console.log(JSON.stringify(out));
"""

    def test_render_states(self):
        import shutil, subprocess
        node = shutil.which("node")
        if not node:
            self.skipTest("没有 node")
        reps = {}
        for name, weeks in (("empty", 1), ("short", 8), ("promoted", 16)):
            c, cal, today = build(weeks, extra=("ctrl_random_a",))
            st = prep(c, today)
            mp = L.choose_main(c, L.strategy_board(c), today, path=main_file(), status=st)
            reps[name] = {"board": PR.jsonable(L.strategy_board(c)), "main_pick": mp, "promotion": PR.jsonable(st), "main": mp["main"],
                          "labels": {"good": "好策略"}, "families": {"ctrl_random_a": "对照"}}
        d = Path(tempfile.mkdtemp())
        (d / "r.js").write_text(self.JS, encoding="utf-8"); (d / "reps.json").write_text(json.dumps(reps, ensure_ascii=False), encoding="utf-8")
        r = subprocess.run([node, str(d / "r.js"), str(Path(__file__).resolve().parent.parent / "dashboard" / "app.js"), str(d / "reps.json")],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        for k in out:
            self.assertIn("尚无策略通过晋升检验" if k != "promoted" else "通过晋升检验：good", out[k])
            self.assertIn("晋升后 · 正式成绩", out[k]); self.assertIn("最小可检测效应", out[k]); self.assertIn("距晋升还差", out[k])
        self.assertIn("默认策略，未经晋升检验", out["empty"]); self.assertIn("默认策略，未经晋升检验", out["short"])
        self.assertNotIn("默认策略，未经晋升检验", out["promoted"].split("主策略成绩")[0].split("门槛")[0].replace("主策略晋升检验", ""))
        self.assertIn("数据不足", out["empty"]); self.assertIn("#1", out["short"])          # 样本够的才有名次


if __name__ == "__main__":
    unittest.main(verbosity=1)
