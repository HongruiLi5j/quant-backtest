"""信号核对账本单元测试（只用标准库）：python3 tests/test_signal_ledger.py"""
import json, sys, tempfile, unittest
from datetime import date, timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import signal_ledger as L
from src import auto_tune as _A

# 隔离：L.run 的默认输出（账本库、报告、主策略文件、调参参数和日志）全部指到临时目录，绝不写 data/ 下的真实文件。
# （以前这里只传了 db/out，主策略文件、auto_params.json、tune_log.jsonl 会被合成数据覆盖。）
_ISO = Path(tempfile.mkdtemp(prefix="ledger_test_"))
L.DB, L.OUT, L.MAIN_FILE = _ISO / "signal_ledger.db", _ISO / "ledger", _ISO / "main_strategy.json"
_A.PARAMS, _A.LOG = _ISO / "auto_params.json", _ISO / "ledger" / "tune_log.jsonl"
sys.path.insert(0, str(Path(__file__).resolve().parent))
from realdata_guard import ZRealDataUntouchedTests  # noqa: E402,F401  真实数据守卫

CAL = []
d = date(2026, 10, 1)
while len(CAL) < 40:
    if d.weekday() < 5:
        CAL.append(d.isoformat())
    d += timedelta(days=1)
D0 = CAL[5]


class P:
    def __init__(self, bars): self.b = bars
    def codes(self): return list(self.b)
    def bars(self, code): return self.b[code]


def series(start_close, path, open_ratio=1.0):
    """path[i] = 第 i 个交易日的收盘价倍数（相对 start_close）；开盘 = 前一日收盘 × open_ratio。"""
    out, prev = {}, start_close
    for i, day in enumerate(CAL):
        close = start_close * (path[i] if i < len(path) else path[-1])
        o = prev * open_ratio
        out[day] = (o, max(o, close), min(o, close), close)
        prev = close
    return out


def flat(): return series(10, [1.0])
def rising(): return series(10, [1.0] * 6 + [1.02, 1.04, 1.06, 1.08, 1.10, 1.12] + [1.12] * 40)       # D0 之后 5 日 +10%
def falling(): return series(10, [1.0] * 6 + [0.98, 0.96, 0.94, 0.92, 0.90, 0.9] + [0.9] * 40)


def row(code, action, nxt=0.5, now=0.0, liq="正常", small=False, name="测"):
    return {"code": code, "name": name, "action": action, "now": now, "next": nxt, "close": 10.0, "liquidity": liq, "small_target": small, "flags": []}


def payload(day, rows): return {"data_date": day, "strategies": {"blend2": {"rows": rows}}}


class T(unittest.TestCase):
    def setUp(self):
        self.db = Path(tempfile.mkdtemp()) / "t.db"
        self.out = Path(tempfile.mkdtemp())
        self.bars = {f"6{i:05d}": flat() for i in range(150)}                # 150 只不动的陪衬股票
        self.bars.update({"600901": rising(), "600902": rising(), "600903": rising(), "600904": falling(), "600905": rising()})

    def go(self, today, rows=None, state=None):
        rows = rows if rows is not None else [row("600901", "买入"), row("600904", "卖出", 0.0), row("600902", "买入", liq="流动性差")]
        return L.run(payload(D0, rows), provider=P(self.bars), cal=CAL, state_rows=state, db=self.db, out=self.out)

    def test_nth_day(self):
        self.assertEqual(L.nth_day(CAL, CAL[5], 1), CAL[6])
        self.assertIsNone(L.nth_day(CAL, CAL[-1], 1))

    def test_not_matured_gives_no_results(self):
        rep = L.run(payload(CAL[5], [row("600901", "买入")]), provider=P(self.bars), cal=CAL, db=self.db, out=self.out)
        self.assertEqual(rep["evaluated_days"], [])        # 数据日期=信号日，下一个交易日还没到

    def test_precision_and_gate(self):
        rep = L.run(payload(D0, [row("600901", "买入"), row("600904", "卖出", 0.0)]), provider=P(self.bars), cal=CAL, db=self.db, out=self.out)
        # 把“今天”推到走满 5 日之后
        later = payload(CAL[12], [])
        rep = L.run(later, provider=P(self.bars), cal=CAL, db=self.db, out=self.out)
        prec = {(p["action"], p["h"]): p for p in rep["precision"]["blend2"]}
        self.assertEqual(prec[("买入", 5)]["hit_raw"], 1.0)
        self.assertEqual(prec[("卖出", 5)]["hit_raw"], 1.0)                 # 下跌 = 卖出信号正确
        self.assertGreater(prec[("买入", 5)]["mean_excess"], 0)            # 相对全市场平均也是正的
        self.assertFalse(prec[("买入", 5)]["enough"])                        # 只有 1 条：样本不足，不能据此改策略
        self.assertTrue(any("样本不足" in n for n in rep["notes"]))

    def test_gate_needs_many_days(self):
        # 一天里 40 条信号：条数够了，但只有 1 个信号日，仍然是样本不足
        rows = [row(f"6{i:05d}", "买入") for i in range(40)]
        L.run(payload(D0, rows), provider=P(self.bars), cal=CAL, db=self.db, out=self.out)
        rep = L.run(payload(CAL[12], []), provider=P(self.bars), cal=CAL, db=self.db, out=self.out)
        p = [x for x in rep["precision"]["blend2"] if x["action"] == "买入" and x["h"] == 1][0]
        self.assertEqual((p["n"], p["days"], p["enough"]), (40, 1, False))

    def test_recall_categories(self):
        rows = [row("600901", "买入"), row("600902", "买入", liq="流动性差"), row("600903", "持有")]
        rows[2]["action"] = None
        state = [("blend2", "600903", 0.4, 0.4)]                             # 策略已持有 600903，但清单里没有动作
        L.run(payload(D0, rows), provider=P(self.bars), cal=CAL, state_rows=state, db=self.db, out=self.out)
        rep = L.run(payload(CAL[12], []), provider=P(self.bars), cal=CAL, db=self.db, out=self.out)
        r = rep["recall"][0]
        self.assertEqual(r["categories"], {"已推荐": 1, "已持有": 1, "被过滤": 1, "没有信号": 1})   # 600905 没有任何信号
        self.assertEqual({m["code"] for m in r["missed"]}, {"600902", "600905"})

    def test_no_state_means_no_recall(self):
        self.go(CAL[5])
        rep = L.run(payload(CAL[12], []), provider=P(self.bars), cal=CAL, db=self.db, out=self.out)
        self.assertEqual(rep["recall"], [])
        self.assertTrue(any("持仓状态" in n for n in rep["notes"]))

    def test_limit_up_open_excluded(self):
        self.bars["600906"] = series(10, [1.0] * 6 + [1.1, 1.21] + [1.21] * 40, open_ratio=1.0)
        self.bars["600906"] = {**self.bars["600906"], CAL[6]: (11.0, 11.0, 11.0, 11.0)}   # 次日开盘涨停 10%
        L.run(payload(D0, [row("600906", "买入")]), provider=P(self.bars), cal=CAL, db=self.db, out=self.out)
        rep = L.run(payload(CAL[12], []), provider=P(self.bars), cal=CAL, db=self.db, out=self.out)
        p = [x for x in rep["precision"]["blend2"] if x["action"] == "买入" and x["h"] == 1][0]
        self.assertEqual((p["n"], p["untradable"]), (0, 1))

    def test_suspended_has_no_move(self):
        b = dict(self.bars["600901"]); b.pop(CAL[6])
        self.assertEqual(L.moves_for("600901", b, D0, CAL), [])

    def test_idempotent(self):
        for _ in range(3):
            L.run(payload(D0, [row("600901", "买入")]), provider=P(self.bars), cal=CAL, db=self.db, out=self.out)
            L.run(payload(CAL[12], []), provider=P(self.bars), cal=CAL, db=self.db, out=self.out)
        c = L.connect(self.db)
        self.assertEqual(c.execute("SELECT COUNT(*) n FROM signals").fetchone()["n"], 1)
        self.assertEqual(c.execute("SELECT COUNT(*) n FROM moves WHERE h=5").fetchone()["n"], len(self.bars))

    def test_report_files(self):
        self.go(CAL[5])
        self.assertTrue((self.out / "latest.json").exists())


class Stats(unittest.TestCase):
    def test_wilson_wide_when_small(self):
        lo, hi = L._wilson(3, 5)
        self.assertLess(lo, 0.3); self.assertGreater(hi, 0.85)
        lo2, hi2 = L._wilson(300, 500)
        self.assertLess(hi2 - lo2, 0.1)

    def test_day_t_clusters_by_day(self):
        self.assertIsNone(L._day_t({"a": [0.01] * 100}))               # 只有一天：不能检验
        t = L._day_t({"a": [0.02, 0.02], "b": [0.03], "c": [0.025]})
        self.assertGreater(t, 2)

    def test_verdict(self):
        base = {"action": "买入", "enough": True, "t_day": 3.0}
        self.assertEqual(L.verdict(base), "有效")
        self.assertEqual(L.verdict({**base, "t_day": -3.0}), "反向")
        self.assertEqual(L.verdict({**base, "action": "卖出", "t_day": -3.0}), "有效")
        self.assertEqual(L.verdict({**base, "t_day": 0.5}), "看不出差别")
        self.assertEqual(L.verdict({**base, "enough": False}), "样本不足")

    def test_reflect_never_claims_with_few_days(self):
        c = L.connect(Path(tempfile.mkdtemp()) / "r.db")
        out = L.reflect(c, "blend2", {"blend2": []}, [], "2026-10-19")
        self.assertTrue(any("不到" in x["text"] for x in out))
        self.assertTrue(any("不会自动修改" in x["text"] for x in out))


class Tune(unittest.TestCase):
    """自动调参：合成数据，确认 采用 / 退回 / 样本不足 / 硬边界 都对。
    这里测的是“采用/退回”机制本身，所以把 20 周冻结期关掉；冻结期本身在 tests/test_promotion.py 里测。"""
    def setUp(self):
        from src import auto_tune as A
        self._fw, A.FREEZE_WEEKS = A.FREEZE_WEEKS, 0

    def tearDown(self):
        from src import auto_tune as A
        A.FREEZE_WEEKS = self._fw

    def build(self, n_days, good_edge):
        from src import auto_tune as A
        c = L.connect(Path(tempfile.mkdtemp()) / "t.db")
        for i in range(n_days):
            day = CAL[i]
            c.execute("INSERT INTO days VALUES(?,?,?)", (day, 1, "x"))
            c.execute("INSERT INTO done VALUES(?,?,?)", (day, 5, 400))
            for k in range(400):
                code = f"6{k:05d}"
                # 小幅加仓（变化 0.12，会被 thr=0.15 以上挡掉）：good_edge 为 True 时这类信号表现很差
                if k < 100:
                    now, nxt, ret = 0.30, 0.42, (-0.03 if good_edge else 0.02)
                elif k < 200:
                    now, nxt, ret = 0.0, 0.5, 0.02          # 买入：表现好
                else:
                    now, nxt, ret = 0.0, 0.0, 0.0
                c.execute("INSERT INTO state VALUES(?,?,?,?,?)", (day, "blend2", code, now, nxt))
                c.execute("INSERT INTO moves VALUES(?,?,?,?,?,?,?)", (day, code, 5, ret + (0.001 * (i % 3)), 0.0, 0, 0))
        c.commit()
        return A, c

    def test_clamp_rejects_out_of_bounds(self):
        from src import auto_tune as A
        self.assertEqual(A.clamp({"thr": 0.01, "mt": 0.5}), A.DEFAULT)
        self.assertEqual(A.clamp("garbage"), A.DEFAULT)

    def test_too_few_days_keeps(self):
        A, c = self.build(4, True)
        d = A.tune(c, CAL[10], state_path=Path(tempfile.mkdtemp()) / "s.json", log_path=Path(tempfile.mkdtemp()) / "l.jsonl")
        self.assertEqual(d["action"], "keep"); self.assertIn("不动参数", d["why"])

    def test_adopts_when_bad_small_adds_filtered(self):
        A, c = self.build(12, True)
        sp = Path(tempfile.mkdtemp()) / "s.json"
        d = A.tune(c, CAL[20], state_path=sp, log_path=Path(tempfile.mkdtemp()) / "l.jsonl")
        self.assertEqual(d["action"], "adopt")
        self.assertGreater(d["to"]["thr"], 0.12)                           # 把 0.12 的小幅加仓挡掉
        self.assertEqual(A.load_state(sp)["active"], d["to"])

    def test_keeps_when_no_edge(self):
        A, c = self.build(12, False)
        d = A.tune(c, CAL[20], state_path=Path(tempfile.mkdtemp()) / "s.json", log_path=Path(tempfile.mkdtemp()) / "l.jsonl")
        self.assertEqual(d["action"], "keep")

    def test_switches_to_best_when_new_days_get_worse(self):
        A, c = self.build(12, True)
        sp, lp = Path(tempfile.mkdtemp()) / "s.json", Path(tempfile.mkdtemp()) / "l.jsonl"
        d = A.tune(c, CAL[20], state_path=sp, log_path=lp)
        self.assertEqual(d["action"], "adopt")
        adopt_day = CAL[11]
        # 换参数之后新增 6 个信号日，小幅加仓反而表现更好 → 新参数更差
        for i in range(12, 18):
            day = CAL[i]
            c.execute("INSERT INTO days VALUES(?,?,?)", (day, 1, "x")); c.execute("INSERT INTO done VALUES(?,?,?)", (day, 5, 400))
            for k in range(400):
                now, nxt, ret = (0.30, 0.42, 0.05) if k < 100 else ((0.0, 0.5, 0.0) if k < 200 else (0.0, 0.0, 0.0))
                c.execute("INSERT INTO state VALUES(?,?,?,?,?)", (day, "blend2", f"6{k:05d}", now, nxt))
                c.execute("INSERT INTO moves VALUES(?,?,?,?,?,?,?)", (day, f"6{k:05d}", 5, ret, 0.0, 0, 0))
        c.commit()
        st = A.load_state(sp); st["history"][-1]["day"] = adopt_day; A.save_state(st, sp)
        d2 = A.tune(c, CAL[25], state_path=sp, log_path=lp)
        self.assertEqual(d2["action"], "switch")
        st = A.load_state(sp)
        self.assertNotEqual(A.key(st["active"]), A.key(d["to"]))         # 失败的那组被淘汰，换成别的
        self.assertIn(A.key(d["to"]), st["banned"])


class Board(unittest.TestCase):
    def mk(self, n_days):
        c = L.connect(Path(tempfile.mkdtemp()) / "b.db")
        for i in range(n_days):
            day = CAL[i]
            c.execute("INSERT INTO days VALUES(?,?,?)", (day, 1, "x")); c.execute("INSERT INTO done VALUES(?,?,?)", (day, 5, 100)); c.execute("INSERT INTO done VALUES(?,?,?)", (day, 1, 100))
            for k in range(100):
                code = f"6{k:05d}"
                ret = 0.02 if k < 50 else -0.02 + 0.0005 * (i % 3)        # 前 50 只涨、后 50 只跌
                for h in (1, 5):
                    c.execute("INSERT INTO moves VALUES(?,?,?,?,?,?,?)", (day, code, h, ret, 0.0, 0, 0))
                if k < 25:   c.execute("INSERT INTO signals VALUES(?,?,?,?,?,?,?,?,?,?,?)", (day, "good", code, "", "买入", 0, .5, 10, "正常", 0, "[]"))
                if k >= 75:  c.execute("INSERT INTO signals VALUES(?,?,?,?,?,?,?,?,?,?,?)", (day, "bad", code, "", "买入", 0, .5, 10, "正常", 0, "[]"))
        c.commit()
        return c

    def test_board_and_delta(self):
        c = self.mk(12)
        b = L.strategy_board(c)
        self.assertGreater(b["good"]["h5"]["hit"], b["bad"]["h5"]["hit"])
        d = b["good"]["h1"]["daily"]
        self.assertIsNone(d[0]["d_hit"]); self.assertIsNotNone(d[1]["d_hit"])

    def test_main_does_not_switch_on_short_history(self):
        """旧规则下 12 个信号日 good 就会被选成主策略；新规则要 ≥12 个完整周 + 多重检验，所以一律沿用 blend2。"""
        p = Path(tempfile.mkdtemp()) / "m.json"
        for n in (3, 12):
            c = self.mk(n)
            r = L.choose_main(c, L.strategy_board(c), CAL[20], path=p)
            self.assertEqual(r["main"], "blend2"); self.assertIn("尚无策略通过晋升检验", r["reason"])
            self.assertTrue(r["default"]); self.assertEqual(r["default_note"], "默认策略，未经晋升检验")
        import src.trade_list as TL
        self.assertEqual(TL.main_strategy(p), "blend2")


class Backup(unittest.TestCase):
    """写账本之前的整体快照：库 + 报告 + 主策略 + 调参 + 日志。一致、每天一份、保留最近 7 份、失败不拖垮每日任务。全部在临时目录里，不碰 data/。"""
    FILES = ["signal_ledger.db", "main_strategy.json", "auto_params.json", "ledger/latest.json", "ledger/2026-10-19.json",
             "ledger/2026-10-20.json", "ledger/tune_log.jsonl", "ledger/reflection.jsonl", "MANIFEST.json"]

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="ledger_bk_"))
        self.db = self.dir / "src.db"
        c = L.connect(self.db)
        c.execute("INSERT INTO days VALUES('2026-10-19',1,'x')"); c.commit(); c.close()
        self.out = self.dir / "ledger"; self.out.mkdir()
        for n in ("latest.json", "2026-10-19.json", "2026-10-20.json"):
            (self.out / n).write_text('{"name": "%s"}' % n, encoding="utf-8")
        (self.out / "tune_log.jsonl").write_text('{"day": "2026-10-20"}\n', encoding="utf-8")
        (self.out / "reflection.jsonl").write_text('{"day": "2026-10-20"}\n', encoding="utf-8")
        (self.out / "notes.txt").write_text("not part of the snapshot")
        self.main_file, self.params = self.dir / "main_strategy.json", self.dir / "auto_params.json"
        self.main_file.write_text('{"main": "blend2"}', encoding="utf-8"); self.params.write_text('{"active": {}}', encoding="utf-8")
        self.bk = self.dir / "backups"

    def at(self, day):
        from datetime import datetime
        return datetime.fromisoformat(day + "T16:00:00")

    def snap(self, day="2026-10-20", **kw):
        kw.setdefault("db", self.db); kw.setdefault("folder", self.bk)
        return L.backup_snapshot(now=self.at(day), out=self.out, main_file=self.main_file, params=self.params, **kw)

    def test_contains_every_file_and_manifest_matches(self):
        import hashlib
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        p = self.snap()
        self.assertEqual(p.name, "20261020")
        found = sorted(str(f.relative_to(p)) for f in p.rglob("*") if f.is_file())
        self.assertEqual(found, sorted(self.FILES))                                             # 不多不少（notes.txt 不在内）
        man = json.loads((p / "MANIFEST.json").read_text(encoding="utf-8"))
        self.assertEqual(man["failed"], {})
        self.assertEqual(sorted(man["files"]), sorted(f for f in self.FILES if f != "MANIFEST.json"))
        for rel, info in man["files"].items():
            self.assertEqual(hashlib.sha256((p / rel).read_bytes()).hexdigest(), info["sha256"], rel)
        self.assertEqual((p / "ledger/latest.json").read_text(encoding="utf-8"), (self.out / "latest.json").read_text(encoding="utf-8"))
        c = L.connect(p / "signal_ledger.db")
        self.assertEqual(c.execute("SELECT day FROM days").fetchone()["day"], "2026-10-19")
        self.assertEqual(c.execute("PRAGMA integrity_check").fetchone()[0], "ok"); c.close()
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), before)              # 备份不改源库
        self.assertEqual(list(self.bk.glob("*.part")), [])

    def test_snapshot_is_pre_write_state_and_not_overwritten_same_day(self):
        self.snap()
        c = L.connect(self.db); c.execute("INSERT INTO days VALUES('2026-10-20',1,'x')"); c.commit(); c.close()    # 之后的写入
        (self.out / "latest.json").write_text('{"name": "overwritten"}', encoding="utf-8")
        self.main_file.write_text('{"main": "polluted"}', encoding="utf-8")
        self.assertIsNone(self.snap())                                                          # 当天第二次：不覆盖
        d = self.bk / "20261020"
        self.assertEqual(L.connect(d / "signal_ledger.db").execute("SELECT COUNT(*) n FROM days").fetchone()["n"], 1)
        self.assertEqual(json.loads((d / "ledger/latest.json").read_text(encoding="utf-8"))["name"], "latest.json")     # 还是覆盖之前的内容
        self.assertEqual(json.loads((d / "main_strategy.json").read_text(encoding="utf-8"))["main"], "blend2")        # 事故里被污染的文件，快照里是干净的

    def test_keeps_only_latest_7_snapshots(self):
        from datetime import date, timedelta
        self.bk.mkdir(); (self.bk / "other").mkdir(); (self.bk / "other" / "x").write_text("keep me"); (self.bk / "notes.txt").write_text("keep")
        for i in range(10):
            self.snap((date(2026, 10, 1) + timedelta(days=i)).isoformat())
        names = sorted(d.name for d in self.bk.iterdir() if d.name.isdigit())
        self.assertEqual(names, [f"202610{d:02d}" for d in range(4, 11)])                       # 7 份，最新的 7 天
        self.assertTrue((self.bk / "other" / "x").exists() and (self.bk / "notes.txt").exists())      # 不认识的东西不删

    def test_missing_files_are_skipped_and_dated_reports_follow_the_folder(self):
        (self.out / "reflection.jsonl").unlink(); self.params.unlink()
        (self.out / "2026-10-21.json").write_text("{}")
        p = self.snap()
        found = {str(f.relative_to(p)) for f in p.rglob("*") if f.is_file()}
        self.assertNotIn("ledger/reflection.jsonl", found); self.assertNotIn("auto_params.json", found)
        self.assertIn("ledger/2026-10-21.json", found); self.assertIn("ledger/latest.json", found)

    def test_small_file_failure_is_recorded_and_others_still_backed_up(self):
        import shutil
        from unittest.mock import patch
        real = shutil.copy2
        def flaky(src, dst, *a, **k):
            if Path(src).name == "latest.json":
                raise OSError("disk full")
            return real(src, dst, *a, **k)
        with patch("shutil.copy2", side_effect=flaky):
            p = self.snap()
        self.assertIsNotNone(p)                                                                 # 快照仍然成立
        man = json.loads((p / "MANIFEST.json").read_text(encoding="utf-8"))
        self.assertIn("ledger/latest.json", man["failed"]); self.assertIn("disk full", man["failed"]["ledger/latest.json"])
        self.assertFalse((p / "ledger/latest.json").exists())
        for rel in ("signal_ledger.db", "main_strategy.json", "ledger/tune_log.jsonl", "ledger/reflection.jsonl", "ledger/2026-10-20.json"):
            self.assertTrue((p / rel).exists(), rel)

    def test_db_failure_voids_snapshot_and_never_raises(self):
        self.assertIsNone(L.backup_snapshot(db=self.dir / "nope.db", folder=self.bk, now=self.at("2026-10-20"), out=self.out,
                                            main_file=self.main_file, params=self.params))     # 库不存在
        self.assertFalse(self.bk.exists())
        blocker = self.dir / "file_not_dir"; blocker.write_text("x")
        self.assertIsNone(self.snap(folder=blocker))                                            # 目标目录是个文件
        bad = self.dir / "bad.db"; bad.write_bytes(b"not a sqlite file" * 100)
        self.assertIsNone(self.snap("2026-10-21", db=bad))                                      # 库损坏
        self.assertEqual([d.name for d in self.bk.iterdir()], [])                               # 不留 .part，也不留不完整的快照目录
        self.assertIsNotNone(self.snap("2026-10-22"))                                           # 之后正常的备份不受影响

    def test_main_snapshots_before_anything_is_written(self):
        import inspect
        src = inspect.getsource(L.main)
        self.assertIn("backup_snapshot()", src)
        self.assertLess(src.index("backup_snapshot()"), src.index("run(payload"))               # 先快照，后写库
        self.assertEqual(L.BACKUP_KEEP, 7)
        self.assertTrue(str(L.BACKUP_DIR).endswith("data/backups/signal_ledger"))

    def test_defaults_use_the_real_run_paths(self):
        """不传路径时，快照的就是 run() 默认写入的那些文件（OUT / MAIN_FILE / auto_tune.PARAMS），不会漏。"""
        from src import auto_tune
        names = [rel for _, rel in L._snapshot_files(L.DB, L.OUT, L.MAIN_FILE, auto_tune.PARAMS)]
        for must in ("signal_ledger.db", "main_strategy.json", "auto_params.json", "ledger/latest.json", "ledger/tune_log.jsonl", "ledger/reflection.jsonl"):
            self.assertIn(must, names)
        self.assertEqual(names[0], "signal_ledger.db")                                          # 库必须第一个（失败则整份作废）


if __name__ == "__main__":
    unittest.main(verbosity=1)
