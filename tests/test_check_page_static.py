"""上传体检页的静态检查:词条齐全、不用 innerHTML、默认关闭的提示存在。不启动服务,不读 data/。"""
import json
import re
import shutil
import subprocess
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
        # 进页面读 /api/meta 的 features.upload_check 判断开关（不再用空 POST 探测）；提交只打 /api/check/upload
        self.assertEqual(set(re.findall(r"/api/[a-z_/]+", JS)), {"/api/check/upload", "/api/meta"})
        self.assertIn("features?.upload_check", JS)
        self.assertIn("404", JS)                                                    # 提交时仍按 404 显示「功能未开启」（开关在两次之间被关掉）

    def test_admin_toggle_present(self):
        av = (UI / "pages" / "admin-views.js").read_text(encoding="utf-8")
        self.assertIn("live('upload_check_on'", av)
        self.assertIn("adm.row.upload_check_on", ZH)
        self.assertIn("adm.row.upload_check_on.hint", EN)


@unittest.skipUnless(shutil.which("node"), "需要 node")
class ParseCsvTests(unittest.TestCase):
    def run_js(self, text):
        code = ("const fs=require('fs');const s=fs.readFileSync(process.argv[1],'utf8');const a=s.indexOf('export function parseCsv');"
                "const b=s.indexOf('const field =');const MAX_ROWS=10000;const t=(k)=>k;"
                "const f=eval('('+s.slice(a,b).replace('export function parseCsv','function')+')');"
                "try{console.log(JSON.stringify(f(process.argv[2])))}catch(e){console.log('ERR:'+e.message)}")
        r = subprocess.run(["node", "-e", code, str(UI / "pages" / "check.js"), text], capture_output=True, text=True, timeout=20)
        return r.stdout.strip()

    def test_valid(self):
        self.assertEqual(json.loads(self.run_js("date,ret\n2020-01-02,0.01\n2020-01-03,0.02".replace("\\n", "\n")))["returns"], [0.01, 0.02])
        full = json.loads(self.run_js("2020-01-02,0.01,0.005,0.1\n2020-01-03,-0.02,0.001,0.2".replace("\\n", "\n")))
        self.assertEqual(full["turnover"], [0.1, 0.2])

    def test_header_variants_accepted(self):
        for h in ("date,ret", "Date , Ret", "日期,收益", "\ufeffdate,ret", "datetime,return"):
            r = json.loads(self.run_js((h + "\n2020-01-02,0.01\n2020-01-03,0.02").replace("\\n", "\n")))
            self.assertEqual(r["returns"], [0.01, 0.02], h)

    def test_strict_rejections(self):
        for bad in ("", "junk\ndate,ret\n2020-01-02,0.01", "2020-01-02BAD,0.01", "2020-01-02,0.1\n2020-01-03,0.1,0.2",
                    "2020-01-02,0.1,0.2,0.3,0.4", "2020-01-02,0.01\n2020-01-03,", "2020-01-02,abc",
                    "2020-01-02BAD,0.5\n2020-01-03,0.02", "date\n2020-01-02,0.01", "date,ret,bench\n2020-01-02,0.01", "2020-1-2,0.5\n2020-01-03,0.02",
                    "２０２０-０１-０２,０.５\n2020-01-03,0.02", "bad-date,NaN\n2020-01-03,0.02", "bad-date,Infinity\n2020-01-03,0.02", "foo,bar\n2020-01-03,0.02"):
            self.assertTrue(self.run_js(bad.replace("\\n", "\n")).startswith("ERR:"), bad)


if __name__ == "__main__":
    unittest.main()
