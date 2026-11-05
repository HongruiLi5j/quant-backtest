"""D09：同一天标题和正文都相同的通知只保留一条、不重复弹窗；数据日期（标题）变了或换了一天照常通知。
不会真的弹 Mac 通知：osascript 调用被替换成记录器。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src import notify  # noqa: E402


class NotifyDedupeTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.log = Path(self.dir.name) / "notifications.json"
        self.patches = [mock.patch.object(notify, "LOG", self.log),
                        mock.patch.object(notify.platform, "system", return_value="Darwin"),
                        mock.patch.object(notify.subprocess, "run")]
        for p in self.patches:
            p.start()
        self.popups = notify.subprocess.run

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.dir.cleanup()

    def items(self):
        return json.loads(self.log.read_text(encoding="utf-8"))

    def test_same_day_repeat_is_folded_and_silent(self):
        for _ in range(6):
            notify.send("量化日报 · A股 2026-11-03", "没有新信号")
        items = self.items()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["count"], 6)
        self.assertIn("first_time", items[0])
        self.assertEqual(self.popups.call_count, 1)   # 只在第一次弹窗

    def test_new_data_date_still_notifies(self):
        notify.send("量化日报 · A股 2026-11-02", "没有新信号")
        notify.send("量化日报 · A股 2026-11-03", "没有新信号")
        self.assertEqual(len(self.items()), 2)
        self.assertEqual(self.popups.call_count, 2)

    def test_different_body_still_notifies(self):
        notify.send("量化日报 · A股 2026-11-03", "买入 143 只")
        notify.send("量化日报 · A股 2026-11-03", "买入 150 只")
        self.assertEqual(len(self.items()), 2)

    def test_other_market_in_between_does_not_break_fold(self):
        notify.send("量化日报 · A股 2026-11-03", "x")
        notify.send("量化日报 · 美股 2026-11-03", "y", timezone="America/New_York", timezone_label="美东时间")
        notify.send("量化日报 · A股 2026-11-03", "x")
        items = self.items()
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["title"], "量化日报 · A股 2026-11-03")
        self.assertEqual(items[0]["count"], 2)

    def test_previous_day_same_text_is_new(self):
        self.log.write_text(json.dumps([{"time": "2000-01-01 10:00", "title": "T", "body": "B"}]), encoding="utf-8")
        notify.send("T", "B")
        self.assertEqual(len(self.items()), 2)
        self.assertEqual(self.popups.call_count, 1)


if __name__ == "__main__":
    unittest.main()
