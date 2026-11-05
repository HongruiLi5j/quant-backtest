"""F01 落地页版式层的静态检查（只读仓库里的源码和白名单，不联网、不读 data/、不写任何文件）。

F01 的版式层是 landing-s39.css 末尾的一段（从「F01 · 研究作品集版式层」注释到文件末尾）。检查的是「不能悄悄坏掉」的约定：
  - landing-s39.css 在 landing-story / landing-nebula / landing-s2 之后加载（版式层靠"后加载 + 同等特异性"来覆盖，顺序错了等于没改）
  - 版式层不引任何外网资源（地址、@import、url()、@font-face 都不许有）
  - 落地页永远深色：浅色主题的涨跌色等被固定回深色值
  - 公开站白名单的引用闭包与白名单一致
  - 第 5 屏（写着收盘日期的市场面板）带「合成数据」标签；渲染代码不用 innerHTML 一类写法

.venv/bin/python tests/test_f01_landing_layout.py
"""
import json
import re
import sys
import unittest
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MOCK = ROOT / "dashboard" / "mockups"
LANDING = MOCK / "quant-ai-dark" / "landing.html"
S39_CSS = MOCK / "quant-ai-dark" / "landing-s39.css"
SECTIONS_JS = MOCK / "shared" / "landing-sections.js"
MARKER = "F01 · 研究作品集版式层"
sys.path.insert(0, str(ROOT / "scripts"))
import build_public_site as B  # noqa: E402


class _Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.sheets = []

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag == "link" and (d.get("rel") or "").lower() == "stylesheet":
            self.sheets.append(d.get("href", ""))


def css_without_comments(text: str) -> str:
    return re.sub(r"/\*.*?\*/", "", text, flags=re.S)


def f01_block() -> str:
    text = S39_CSS.read_text(encoding="utf-8")
    i = text.index(MARKER)
    start = text.rindex("/*", 0, i)
    return text[start:]


class TestF01Landing(unittest.TestCase):
    def test_s39_loads_after_the_rules_it_overrides(self):
        p = _Links()
        p.feed(LANDING.read_text(encoding="utf-8"))
        idx = {h: n for n, h in enumerate(p.sheets)}
        for earlier in ("landing-story.css", "landing-nebula.css", "landing-s2.css", "../shared/member-ui.css"):
            self.assertLess(idx[earlier], idx["landing-s39.css"], f"{earlier} 必须在 landing-s39.css 之前加载")

    def test_block_is_at_end_and_marked_once(self):
        text = S39_CSS.read_text(encoding="utf-8")
        self.assertEqual(text.count(MARKER), 1)
        self.assertTrue(text.rstrip().endswith("}"))

    def test_block_has_no_external_resources(self):
        css = css_without_comments(f01_block())
        self.assertNotRegex(css, r"https?:|//[a-z0-9]", "版式层不能引外网地址")
        self.assertNotIn("@import", css)
        self.assertNotIn("url(", css, "版式层不引图片/字体文件")
        self.assertNotIn("@font-face", css)
        self.assertNotIn("!important", css)

    def test_block_keeps_landing_dark_tokens(self):
        """ADR-017：落地页永远深色。浅色主题的涨跌色（#a11a12 / #0e5c2c）在深底上对比度 < 3:1，必须被固定回深色值。"""
        css = css_without_comments(f01_block())
        block = re.search(r"^\.story-page \{(.*?)^\}", css, flags=re.S | re.M)
        self.assertIsNotNone(block)
        body = block.group(1)
        for token, value in (("--red", "#ff8a8a"), ("--green", "#4ade80"), ("--text-secondary", "rgba(255, 255, 255, 0.72)")):
            self.assertRegex(body, rf"{re.escape(token)}:\s*{re.escape(value)};")
        self.assertIn("color-scheme: dark", body)

    def test_block_braces_balanced_and_scoped(self):
        css = css_without_comments(f01_block())
        self.assertEqual(css.count("{"), css.count("}"))
        # 每条规则都只作用于落地页（.story-page 或 html:has(.story-page) 或 .js .story），不会漏到别的页面
        for sel in re.findall(r"([^{}]+)\{", css):
            sel = sel.strip()
            if sel.startswith("@media"):
                continue
            for part in sel.split(","):
                self.assertRegex(part.strip(), r"\.story-page|\.js \.story", f"选择器没有限定在落地页：{part.strip()}")

    def test_allowlist_closure_unchanged(self):
        rep = B.discover(MOCK, B.load_allowlist(B.ALLOWLIST_DEFAULT))
        self.assertEqual(rep["in_closure_not_in_allowlist"], [])
        # 白名单里有、但静态分析看不到引用的文件只能是这两类（D24/D25 自带的第三方文件）：
        #   - 许可证原文（随文件一起发布，本来就不会被页面引用）；
        #   - 只通过 importmap / 动态 import(变量) 加载的库：three.js 及其 addons（landing.html 的 importmap）、
        #     qrcode-generator（share-modal.js 的 import(QR_LIB)）。
        dynamic = {"shared/vendor/qrcode-generator/qrcode-generator.esm.js", "shared/vendor/three/three.module.min.js"}
        ok = lambda rel: (rel.startswith("shared/vendor/") and rel.rsplit("/", 1)[-1] in ("LICENSE.txt", "FFL.txt")) \
            or rel in dynamic or rel.startswith("shared/vendor/three/addons/")
        self.assertEqual([r for r in rep["in_allowlist_not_in_closure"] if not ok(r)], [])
        self.assertEqual(rep["unresolved_hard_refs"], {})

    def test_sections_js_safe_dom_and_synthetic_label(self):
        js = SECTIONS_JS.read_text(encoding="utf-8")
        for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
            self.assertNotIn(bad, js)
        s5 = js[js.index("function s5()"):js.index("function wave()")]
        self.assertIn("t('land.synthetic')", s5, "第 5 屏面板要带「合成数据」标签")
        self.assertIn("class: 'lx__scroll'", js)

    def test_synthetic_label_in_both_dicts(self):
        for lang in ("zh", "en"):
            d = json.loads((MOCK / "shared" / f"i18n-{lang}.json").read_text(encoding="utf-8"))
            self.assertTrue(d.get("land.synthetic"), f"{lang} 字典缺 land.synthetic")
            self.assertTrue(d.get("story.hero.sources"), f"{lang} 字典缺首屏合成数据标注")


if __name__ == "__main__":
    unittest.main(verbosity=2)
