"""公开站导出检查页的静态检查:词条、不用 innerHTML、只调用一个后台动作。不启动服务,不读 data/。"""
import json
import re
import unittest
from pathlib import Path

UI = Path(__file__).resolve().parent.parent / "dashboard" / "ui"
JS = (UI / "pages" / "publish.js").read_text(encoding="utf-8")
HTML = (UI / "w" / "publish.html").read_text(encoding="utf-8")
ZH = json.loads((UI / "i18n" / "zh.json").read_text(encoding="utf-8"))
EN = json.loads((UI / "i18n" / "en.json").read_text(encoding="utf-8"))


class PublishPageTests(unittest.TestCase):
    def test_keys(self):
        keys = set(re.findall(r"t\('([a-z_.0-9]+)'", JS)) | set(re.findall(r'data-i18n="([a-z_.0-9]+)"', HTML))
        keys = {k for k in keys if k.startswith("pub.")} | {"adm.row.publish_check_on", "adm.row.publish_check_on.hint"}
        self.assertGreaterEqual(len(keys), 7)
        for k in sorted(keys):
            self.assertIn(k, ZH, k)
            self.assertIn(k, EN, k)

    def test_safe_and_single_action(self):
        self.assertNotIn("innerHTML", JS)
        self.assertEqual(set(re.findall(r"/api/[a-z_/]+", JS)), {"/api/admin/action"})
        self.assertIn("'publish_check'", JS)
        self.assertIn('data-page="publish"', HTML)

    def test_admin_toggle(self):
        av = (UI / "pages" / "admin-views.js").read_text(encoding="utf-8")
        self.assertIn("live('publish_check_on'", av)

    def test_not_in_public_allowlist(self):
        al = (UI.parent.parent / "scripts" / "public_allowlist.json").read_text(encoding="utf-8")
        self.assertNotIn("publish.html", al)
        self.assertNotIn("publish.js", al)


if __name__ == "__main__":
    unittest.main()
