"""发布页（N23）只读接口的测试：用临时 build/ 目录，不读真实 build/，不构建、不上传。"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src import publish_view  # noqa: E402

SHA = "a" * 64


def report(**kw):
    r = {"ok": True, "release_ready": True, "errors": [], "blockers": [], "warnings": ["w"], "stats": {"files": 9},
         "build_id": "x", "manifest_sha256": SHA, "diff_vs_published": {"baseline": "20261001T000000Z-00000000", "added": ["a.js"], "changed": [], "removed": ["b.js", "c.js"]}}
    r.update(kw)
    return r


class PublishViewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.b = Path(self.tmp.name) / "build"
        self.b.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, bid, rep):
        (self.b / bid).mkdir()
        (self.b / bid / "report.json").write_text(json.dumps(rep), encoding="utf-8")

    def test_none(self):
        self.assertEqual(publish_view.latest(self.b)["status"], "none")

    def test_latest_ready_has_all_commands(self):
        self.put("20261027T002633Z-d745df8b", report(release_ready=False))
        self.put("20261027T012755Z-1b5f4456", report())
        (self.b / "20261028T000000Z-deadbeef.FAILED").mkdir()             # 失败的目录名不合格式，不算
        (self.b / "junk").mkdir()
        d = publish_view.latest(self.b)
        self.assertEqual(d["build_id"], "20261027T012755Z-1b5f4456")
        self.assertTrue(d["release_ready"])
        self.assertEqual((d["diff"]["n_added"], d["diff"]["n_removed"]), (1, 2))
        self.assertEqual(set(d["commands"]), {"preview", "preview_stop", "deploy", "mark"})
        self.assertIn("--verify 20261027T012755Z-1b5f4456 --release --expect-manifest-sha256 " + SHA, d["commands"]["deploy"])
        self.assertIn("--project-name accessiblequant --branch main", d["commands"]["deploy"])
        self.assertIn("127.0.0.1", d["commands"]["preview"])
        json.dumps(d, allow_nan=False)

    def test_blocked_has_no_deploy(self):
        self.put("20261027T012755Z-1b5f4456", report(blockers=["邮箱占位"], release_ready=False))
        d = publish_view.latest(self.b)
        self.assertFalse(d["release_ready"])
        self.assertNotIn("deploy", d["commands"])
        self.assertNotIn("mark", d["commands"])
        self.assertIn("preview", d["commands"])

    def test_release_ready_flag_alone_is_not_enough(self):
        self.put("20261027T012755Z-1b5f4456", report(ok=False))
        self.assertFalse(publish_view.latest(self.b)["release_ready"])

    def test_bad_sha_no_commands(self):
        self.put("20261027T012755Z-1b5f4456", report(manifest_sha256="abc; rm -rf ~"))
        d = publish_view.latest(self.b)
        self.assertEqual(d["commands"], {})
        self.assertIsNone(d["manifest_sha256"])

    def test_symlink_and_garbage(self):
        target = Path(self.tmp.name) / "evil.json"
        target.write_text(json.dumps(report()), encoding="utf-8")
        (self.b / "20261027T012755Z-1b5f4456").mkdir()
        os.symlink(target, self.b / "20261027T012755Z-1b5f4456" / "report.json")
        self.assertEqual(publish_view.latest(self.b)["status"], "unreadable")
        (self.b / "published.json").write_text("{bad", encoding="utf-8")
        self.assertIsNone(publish_view.latest(self.b)["published"])

    def test_published_flag(self):
        self.put("20261027T012755Z-1b5f4456", report())
        (self.b / "published.json").write_text(json.dumps({"build_id": "20261027T012755Z-1b5f4456"}), encoding="utf-8")
        self.assertTrue(publish_view.latest(self.b)["is_published"])

    def test_commands_reject_bad_ids(self):
        self.assertEqual(publish_view.commands("../x", SHA), {})
        self.assertEqual(publish_view.commands("20261027T012755Z-1b5f4456", "x"), {})


class WiringTests(unittest.TestCase):
    def test_route_guarded_before_share_gate(self):
        app = (ROOT / "app.py").read_text(encoding="utf-8")
        i = app.index('u.path == "/api/publish/latest"')
        self.assertLess(i, app.index("if self._share_gate(u):"))
        body = app[app.index("def _publish_latest(self"):]
        body = body[:body.index("\n    def ", 10)]
        for must in ("_local_or_owner()", "self._remote()", "self._authed()", "X-Requested-With", "members.hit("):
            self.assertIn(must, body)


if __name__ == "__main__":
    unittest.main()
