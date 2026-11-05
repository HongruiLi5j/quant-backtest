"""报告页（N22）的测试：纯函数用临时目录，不读真实 reports/；接线和前端只做静态检查，不 import app。"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src import reports_view  # noqa: E402

APP = (ROOT / "app.py").read_text(encoding="utf-8")


class ReportsViewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "reports"
        (self.root / "overnight_2026-10-24" / "replay_500").mkdir(parents=True)
        (self.root / "overnight_2026-10-24" / "replay_500" / "report.md").write_text("> 提示\n\n# 回放报告\n正文", encoding="utf-8")
        (self.root / "T008_总结.md").write_text("# 跨市场结论\n", encoding="utf-8")
        (self.root / "notes.md.before-N21").write_text("# 备份\n", encoding="utf-8")
        (self.root / ".hidden.md").write_text("# 隐藏\n", encoding="utf-8")
        (self.root / "data.csv").write_text("a,b\n", encoding="utf-8")
        (self.root / "dir.md").mkdir()                                     # 名字像 .md 的目录不能当文件
        (self.root / ".git").mkdir()
        (self.root / ".git" / "x.md").write_text("# no\n", encoding="utf-8")
        self.secret = Path(self.tmp.name) / "secret.md"
        self.secret.write_text("# 不该被读到\n", encoding="utf-8")
        os.symlink(self.secret, self.root / "link.md")
        reports_view._cache.update(t=0.0, root=None, v=None)

    def tearDown(self):
        reports_view._cache.update(t=0.0, root=None, v=None)
        self.tmp.cleanup()

    def paths(self):
        return {r["path"] for r in reports_view.list_reports(self.root)["reports"]}

    def test_list_only_regular_md(self):
        self.assertEqual(self.paths(), {"overnight_2026-10-24/replay_500/report.md", "T008_总结.md"})

    def test_meta(self):
        rows = {r["path"]: r for r in reports_view.list_reports(self.root)["reports"]}
        r = rows["overnight_2026-10-24/replay_500/report.md"]
        self.assertEqual((r["title"], r["kind"], r["group"], r["date"]), ("回放报告", "replay", "overnight_2026-10-24", "2026-10-24"))
        self.assertEqual((rows["T008_总结.md"]["title"], rows["T008_总结.md"]["group"]), ("跨市场结论", ""))
        json.dumps(rows, allow_nan=False)

    def test_read_ok(self):
        d = reports_view.read_report("T008_总结.md", self.root)
        self.assertEqual(d["text"], "# 跨市场结论\n")
        self.assertFalse(d["truncated"])

    def test_read_rejects(self):
        bad = ["", None, 5, "../secret.md", "/etc/passwd", "link.md", ".hidden.md", "notes.md.before-N21", "dir.md",
               "overnight_2026-10-24/../T008_总结.md", "./T008_总结.md", "x\\y.md", "a\x00.md", "data.csv", "missing.md",
               ".git/x.md", "a" * 400]
        for rel in bad:
            with self.subTest(rel=rel), self.assertRaises(reports_view.ReportError):
                reports_view.read_report(rel, self.root)

    def test_truncate_and_control_chars(self):
        big = self.root / "big.md"
        big.write_bytes(b"# big\n" + b"\x07x\r\n" * (reports_view.MAX_BYTES // 3 + 10))
        reports_view._cache.update(t=0.0, root=None, v=None)
        d = reports_view.read_report("big.md", self.root)
        self.assertTrue(d["truncated"])
        self.assertLessEqual(len(d["text"].encode("utf-8")), reports_view.MAX_BYTES)
        self.assertNotIn("\x07", d["text"])
        self.assertNotIn("\r", d["text"])

    def test_missing_root(self):
        self.assertEqual(reports_view.list_reports(Path(self.tmp.name) / "nope")["total"], 0)


class WiringTests(unittest.TestCase):
    def test_routes_and_guards(self):
        self.assertIn('u.path in ("/api/reports/list", "/api/reports/file")', APP)
        body = APP[APP.index("def _reports(self"):]
        body = body[:body.index("\n    def ", 10)]
        for must in ("_local_or_owner()", "self._remote()", "self._authed()", 'X-Requested-With', "members.hit("):
            self.assertIn(must, body)
        dispatch = APP.index('u.path in ("/api/reports/list", "/api/reports/file")')
        self.assertLess(dispatch, APP.index("if self._share_gate(u):"))    # 和账本接口一样，在分享门之前自己把关

    def test_frontend_no_html_strings(self):
        ui = ROOT / "dashboard" / "ui"
        js = (ui / "pages" / "reports.js").read_text(encoding="utf-8")
        self.assertIn("DOMPurify", js)
        self.assertIn("RETURN_DOM_FRAGMENT", js)                           # 清洗结果直接是 DOM 片段
        self.assertNotIn("innerHTML", js)
        zh = json.loads((ui / "i18n" / "zh.json").read_text(encoding="utf-8"))
        en = json.loads((ui / "i18n" / "en.json").read_text(encoding="utf-8"))
        self.assertEqual({k for k in zh if k.startswith("rep.")}, {k for k in en if k.startswith("rep.")})


if __name__ == "__main__":
    unittest.main()
