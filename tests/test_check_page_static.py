"""上传体检页的静态检查:词条齐全、不用 innerHTML、默认关闭的提示存在。不启动服务,不读 data/。"""
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "dashboard" / "ui"
JS = (UI / "pages" / "check.js").read_text(encoding="utf-8")
HTML = (UI / "w" / "check.html").read_text(encoding="utf-8")
ZH = json.loads((UI / "i18n" / "zh.json").read_text(encoding="utf-8"))
EN = json.loads((UI / "i18n" / "en.json").read_text(encoding="utf-8"))


class CheckPageTests(unittest.TestCase):
    def test_keys_exist_in_both_languages(self):
        keys = set(re.findall(r"t\('([a-z_.0-9]+)'", JS)) | set(re.findall(r'data-i18n="([a-z_.0-9]+)"', HTML))
        keys |= {f"chk.status.{s}" for s in ("pass", "warn", "fail", "na")}
        keys = {k for k in keys if k.startswith(("chk.", "adm.row.upload"))}
        self.assertGreater(len(keys), 20)
        for k in sorted(keys):
            self.assertIn(k, ZH, k)
            self.assertIn(k, EN, k)

    def test_no_innerhtml_and_page_id(self):
        self.assertNotIn("innerHTML", JS)
        self.assertNotIn("insertAdjacentHTML", JS)
        self.assertIn('data-page="check"', HTML)

    def test_calls_only_the_upload_endpoint(self):
        self.assertEqual(set(re.findall(r"/api/[a-z_/]+", JS)), {"/api/check/upload"})
        self.assertIn("404", JS)                                                    # 功能未开启时的提示分支

    def test_admin_toggle_present(self):
        av = (UI / "pages" / "admin-views.js").read_text(encoding="utf-8")
        self.assertIn("live('upload_check_on'", av)
        self.assertIn("adm.row.upload_check_on", ZH)
        self.assertIn("adm.row.upload_check_on.hint", EN)


if __name__ == "__main__":
    unittest.main()
