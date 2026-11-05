"""B11：scripts/ledger_restore_drill.py（账本快照恢复演练）。

快照用 signal_ledger.backup_snapshot() 在临时目录里用合成小库现做，所以同时验证"生产快照的格式"和"演练脚本"对得上。
全部在临时目录里读写，不读写 data/ 下的真实文件。
运行：.venv/bin/python tests/test_ledger_restore_drill_b11.py
"""
import contextlib
import hashlib
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
from src import signal_ledger as SL  # noqa: E402
import ledger_restore_drill as D  # noqa: E402
from realdata_guard import ZRealDataUntouchedTests  # noqa: E402,F401  真实数据守卫（必须在所有测试类之后运行）


def _tree_hash(folder: Path) -> dict:
    """目录下所有文件的 相对路径 → sha256，用来证明"演练前后一个字节都没变"。"""
    return {str(p.relative_to(folder)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(folder.rglob("*")) if p.is_file()}


class DrillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name) / "data"
        self.data.mkdir()
        self.db = self.data / "signal_ledger.db"
        self.folder = self.data / "backups" / "signal_ledger"
        self.out = self.data / "daily" / "ledger"
        self.out.mkdir(parents=True)
        (self.out / "latest.json").write_text('{"x": 1}', encoding="utf-8")
        (self.out / "2026-10-20.json").write_text('{"day": "2026-10-20"}', encoding="utf-8")
        (self.data / "main_strategy.json").write_text('{"main": "demo"}', encoding="utf-8")
        (self.data / "auto_params.json").write_text('{"thr": 0.5}', encoding="utf-8")
        c = SL.connect(self.db)
        for day in ("2026-10-19", "2026-10-20"):
            SL.record_day(c, self._payload(day), state_rows=[("demo", "600000", 0.0, 1.0)])
        c.close()
        self.work = Path(self.tmp.name) / "work"
        self.work.mkdir()

    @staticmethod
    def _payload(day):
        rows = [{"code": "600000", "name": "浦发银行", "action": "买入", "now": 0.0, "next": 1.0, "close": 10.0},
                {"code": "000001", "name": "平安银行", "action": "卖出", "now": 1.0, "next": 0.0, "close": 11.0}]
        return {"data_date": day, "threshold": 0.5, "min_target": 0.1, "strategies": {"demo": {"rows": rows}}}

    def snapshot(self, day="20261021"):
        now = datetime.strptime(day, "%Y%m%d")
        dest = SL.backup_snapshot(db=self.db, folder=self.folder, now=now, out=self.out,
                                  main_file=self.data / "main_strategy.json", params=self.data / "auto_params.json")
        self.assertIsNotNone(dest)
        return Path(dest)

    def drill(self, snap, **kw):
        return D.drill_one(snap, self.db, work_base=self.work, **kw)

    # ---- 正常路径
    def test_好快照通过且行数一致(self):
        snap = self.snapshot()
        res = self.drill(snap)
        self.assertEqual(res["status"], D.PASS, res)
        self.assertEqual(res["integrity"], "ok")
        self.assertEqual(res["problems"], [])
        by = {r["table"]: r for r in res["counts"]}
        self.assertEqual(by["days"]["snapshot"], 2)
        self.assertEqual(by["signals"]["snapshot"], 4)       # 2 天 × 2 条信号
        self.assertEqual(by["state"]["snapshot"], 2)
        self.assertTrue(all(r["diff"] == 0 and r["flag"] == "" for r in res["counts"]))

    def test_快照之后账本又增长_仍通过且差值为正(self):
        snap = self.snapshot()
        c = SL.connect(self.db)
        SL.record_day(c, self._payload("2026-10-21"))
        c.close()
        res = self.drill(snap)
        self.assertEqual(res["status"], D.PASS)
        by = {r["table"]: r for r in res["counts"]}
        self.assertEqual(by["days"]["diff"], 1)
        self.assertEqual(by["signals"]["diff"], 2)

    def test_演练后临时目录被清理且不留文件(self):
        snap = self.snapshot()
        res = self.drill(snap)
        self.assertIsNone(res["workdir"])
        self.assertEqual(list(self.work.iterdir()), [])

    def test_keep时保留恢复目录且内容和快照一致(self):
        snap = self.snapshot()
        res = self.drill(snap, keep=True)
        restored = Path(res["workdir"]) / "restored"
        self.assertEqual(_tree_hash(restored), _tree_hash(snap))

    def test_只读_快照和当前库演练前后字节不变(self):
        snap = self.snapshot()
        before_snap, before_data = _tree_hash(self.folder), _tree_hash(self.data)
        self.drill(snap)
        self.assertEqual(_tree_hash(self.folder), before_snap)
        self.assertEqual(_tree_hash(self.data), before_data)       # 含当前库、快照目录、小文件

    # ---- 坏快照
    def test_库文件被截断_失败(self):
        snap = self.snapshot()
        db = snap / "signal_ledger.db"
        db.write_bytes(db.read_bytes()[:len(db.read_bytes()) // 2])
        res = self.drill(snap)
        self.assertEqual(res["status"], D.FAIL)
        self.assertTrue(any("signal_ledger.db" in p for p in res["problems"]))

    def test_库文件内容损坏_完整性或哈希检查能发现(self):
        snap = self.snapshot()
        db = snap / "signal_ledger.db"
        raw = bytearray(db.read_bytes())
        raw[:16] = b"not a sqlite db!"                               # 破坏文件头
        db.write_bytes(bytes(raw))
        res = self.drill(snap)
        self.assertEqual(res["status"], D.FAIL)
        self.assertNotEqual(res["integrity"], "ok")

    def test_小文件被改动_清单哈希不一致_失败(self):
        snap = self.snapshot()
        (snap / "main_strategy.json").write_text('{"main": "dome"}', encoding="utf-8")      # 和原文 '{"main": "demo"}' 等长，只能靠哈希发现
        res = self.drill(snap)
        self.assertEqual(res["status"], D.FAIL)
        self.assertTrue(any("main_strategy.json" in p and "sha256" in p for p in res["problems"]))

    def test_清单里的文件缺失_失败(self):
        snap = self.snapshot()
        (snap / "ledger" / "latest.json").unlink()
        res = self.drill(snap)
        self.assertEqual(res["status"], D.FAIL)
        self.assertTrue(any("ledger/latest.json 缺失" in p for p in res["problems"]))

    def test_快照里没有库文件_失败(self):
        snap = self.snapshot()
        (snap / "signal_ledger.db").unlink()
        res = self.drill(snap)
        self.assertEqual(res["status"], D.FAIL)

    def test_没有清单_给提示但库完好仍通过(self):
        snap = self.snapshot()
        (snap / "MANIFEST.json").unlink()
        res = self.drill(snap)
        self.assertEqual(res["status"], D.PASS)
        self.assertTrue(any("MANIFEST" in n for n in res["notes"]))

    # ---- 和当前库的对比
    def test_当前库行数比快照少_警告(self):
        snap = self.snapshot()
        con = sqlite3.connect(self.db)
        con.execute("DELETE FROM signals WHERE day='2026-10-20'")
        con.commit()
        con.close()
        res = self.drill(snap)
        self.assertEqual(res["status"], D.WARN)
        by = {r["table"]: r for r in res["counts"]}
        self.assertEqual(by["signals"]["flag"], "当前库行数比快照少")
        self.assertEqual(by["signals"]["diff"], -2)
        self.assertEqual(by["days"]["flag"], "")

    def test_当前库缺表_警告(self):
        snap = self.snapshot()
        con = sqlite3.connect(self.db)
        con.execute("DROP TABLE null_draws")
        con.commit()
        con.close()
        res = self.drill(snap)
        self.assertEqual(res["status"], D.WARN)
        self.assertIn("null_draws:当前库里没有这张表", D.summary_line(res))

    def test_当前库不存在_警告而不是失败(self):
        snap = self.snapshot()
        res = D.drill_one(snap, Path(self.tmp.name) / "nope.db", work_base=self.work)
        self.assertEqual(res["status"], D.WARN)
        self.assertEqual(res["integrity"], "ok")

    def test_失败的优先级高于警告(self):
        snap = self.snapshot()
        (snap / "auto_params.json").write_text("{}", encoding="utf-8")
        con = sqlite3.connect(self.db)
        con.execute("DELETE FROM signals")
        con.commit()
        con.close()
        self.assertEqual(self.drill(snap)["status"], D.FAIL)

    # ---- 选快照 + 命令行
    def test_list_snapshots_按日期排序且忽略半成品和杂物(self):
        self.snapshot("20261021")
        self.snapshot("20261023")
        (self.folder / "20261024.part").mkdir()
        (self.folder / "notes.txt").write_text("x")
        (self.folder / "2026102").mkdir()
        self.assertEqual([d.name for d in D.list_snapshots(self.folder)], ["20261021", "20261023"])
        self.assertEqual(D.list_snapshots(self.folder / "不存在"), [])

    def _main(self, *args):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = D.main(["--data-dir", str(self.data), "--work-dir", str(self.work), *args])
        return code, buf.getvalue()

    def test_命令行默认演练最新一份且只输出结论行(self):
        self.snapshot("20261021")
        self.snapshot("20261023")
        code, text = self._main()
        self.assertEqual(code, 0)
        lines = text.strip().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("20261023", lines[0])
        self.assertIn("通过", lines[0])
        self.assertIn("integrity_check=ok", lines[0])
        self.assertNotIn("20261021", text)

    def test_命令行_all演练全部_snapshot指定一份(self):
        self.snapshot("20261021")
        self.snapshot("20261023")
        code, text = self._main("--all")
        self.assertEqual(code, 0)
        self.assertEqual(len(text.strip().splitlines()), 2)
        code, text = self._main("--snapshot", "20261021")
        self.assertIn("20261021", text)
        self.assertNotIn("20261023", text)

    def test_命令行退出码_失败1_警告2_无快照1(self):
        code, text = self._main()                                  # 一份快照都没有
        self.assertEqual(code, 1)
        self.assertIn("失败", text)
        snap = self.snapshot()
        (snap / "signal_ledger.db").write_bytes(b"garbage")
        self.assertEqual(self._main()[0], 1)
        (snap / "signal_ledger.db").unlink()
        self.assertEqual(self._main()[0], 1)

    def test_命令行警告退出码是2(self):
        self.snapshot()
        self.db.unlink()
        self.assertEqual(self._main()[0], 2)

    def test_verbose打印逐表对比(self):
        self.snapshot()
        code, text = self._main("--verbose")
        self.assertIn("signals", text)
        self.assertGreater(len(text.strip().splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
