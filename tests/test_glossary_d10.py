"""D10：页面里用到的每个术语（withInfo/info 的第二个参数、TERMS 表里的值）在中英文字典里都有 gl.<术语> 和 gl.<术语>.name，
否则 ⓘ 不显示或英文界面露出键名。另外检查中英文 gl.* 键集合一致。"""
import json
import re
import unittest
from pathlib import Path

UI = Path(__file__).resolve().parents[1] / "dashboard" / "ui"
ZH = json.loads((UI / "i18n" / "zh.json").read_text(encoding="utf-8"))
EN = json.loads((UI / "i18n" / "en.json").read_text(encoding="utf-8"))


def used_terms():
    terms = set()
    for p in (UI / "pages").glob("*.js"):
        src = p.read_text(encoding="utf-8")
        if "glossary.js" not in src:
            continue
        terms |= set(re.findall(r"withInfo\([^;]*?,\s*'([a-z0-9_]+)'\)", src))
        terms |= set(re.findall(r"\binfo\('([a-z0-9_]+)'\)", src))
        for block in re.findall(r"(?:TERMS|terms) = \{([^}]*)\}", src):
            terms |= set(re.findall(r":\s*'([a-z0-9_]+)'", block))
        terms |= set(re.findall(r"\['u1\.[a-z0-9_.]+', \d, '([a-z0-9_]+)'\]", src))
        terms |= set(re.findall(r"null, '([a-z0-9_]+)'\)", src))
    return terms


class GlossaryTests(unittest.TestCase):
    def test_terms_found(self):
        self.assertGreaterEqual(len(used_terms()), 10)

    def test_every_used_term_has_both_languages(self):
        for term in sorted(used_terms()):
            for d, lang in ((ZH, "zh"), (EN, "en")):
                self.assertIn(f"gl.{term}", d, f"{lang} 缺 gl.{term}")
                self.assertIn(f"gl.{term}.name", d, f"{lang} 缺 gl.{term}.name")

    def test_zh_en_same_glossary_keys(self):
        self.assertEqual({k for k in ZH if k.startswith("gl.")}, {k for k in EN if k.startswith("gl.")})

    def test_sharpe_definitions_state_rf(self):
        self.assertIn("511880", ZH["gl.sharpe_cash"])
        self.assertIn("SHY", EN["gl.sharpe_cash"])
        self.assertIn("rf = 0", EN["gl.sharpe_rf0"])


if __name__ == "__main__":
    unittest.main()
