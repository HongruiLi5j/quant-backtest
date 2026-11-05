"""F03：公开站子页（策略、回测、回放、引擎、全市场、信号、方法论）视觉和内容统一的检查。
只读 dashboard/ 和 scripts/ 下的源文件，不读写 data/，不联网，不起浏览器。

1. F03 部件 parts/F03.*.json 两边键一致、占位符一致、和公开站字典逐条相同；公开站字典 zh/en 键一致
2. 代码里用到的 f03.* 键字典里都有；部件里的 f03.* 键代码里都用到了（不留死词条）
3. F03 文案里没有导出脚本的红线词、内部接口路径、合规词
4. 浅色主题不漏色：tokens.css 浅色主题改写的每个颜色 / 阴影 token，public-story.css 的 .ps-page 作用域都重写了
5. 图表颜色从容器读（chartColors(container)），权益 / 回撤图价格轴带 %
6. 引擎名统一成 RQAlpha（公开子页用到的词条里不再出现「米筐 / RiceQuant」）
7. 「合成数据」脚注：回放、引擎的游客锁定页也有；改过的渲染器只走 textContent 路径
8. HTML 静态首帧里的 f03.* 文字和中文字典一致

运行：.venv/bin/python tests/test_f03_public_subpages.py
"""
import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MOCK = ROOT / "dashboard" / "mockups"
sys.path.insert(0, str(Path(__file__).resolve().parent))
import i18n_parts_compat as parts_compat  # noqa: E402  部件（parts/）已退役，键名单来自快照、值来自基础字典
sys.path.insert(0, str(ROOT / "scripts"))
import build_public_site as B  # noqa: E402  只用它的红线词、合规词常量，不构建

# F03 改过的渲染器 / 页面
JS_FILES = ["shared/page-strategies.js", "shared/page-engines.js", "shared/page-market.js", "shared/page-signals.js",
            "shared/page-replay.js", "shared/page-method.js", "shared/chart-helpers.js", "quant-ai-dark/public.js"]
HTML_FILES = ["quant-ai-dark/backtest.html"]
# 改的是已有键的值（引擎名），不是新键
RENAMED = {"pub.engine.rq", "engines.lock.rq_title", "lock.engines.title", "t5.pub.engines"}


def load(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


def read(rel: str) -> str:
    return (MOCK / rel).read_text(encoding="utf-8")


def css_block(text: str, selector_start: str) -> str:
    """取以 selector_start 开头的第一个规则块的花括号内文字"""
    i = text.index(selector_start)
    j = text.index("{", i)
    depth, k = 0, j
    while True:
        if text[k] == "{":
            depth += 1
        elif text[k] == "}":
            depth -= 1
            if depth == 0:
                return text[j + 1:k]
        k += 1


class F03SubpagesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.zh = load(MOCK / "shared" / "i18n-zh.json")
        cls.en = load(MOCK / "shared" / "i18n-en.json")
        # F03 部件原是公开站字典的镜像，已退役：键名单来自快照，值直接从公开站字典取（缺键会报 KeyError）
        cls.pzh = parts_compat.part("F03", "zh", source=cls.zh)
        cls.pen = parts_compat.part("F03", "en", source=cls.en)
        cls.code = "\n".join(read(f) for f in JS_FILES + HTML_FILES)

    def test_dict_and_parts_keys_match(self):
        self.assertEqual(set(self.zh), set(self.en))
        self.assertEqual(set(self.pzh), set(self.pen))
        self.assertTrue(self.pzh)

    def test_parts_mirror_public_dict(self):
        for k in self.pzh:
            self.assertEqual(self.pzh[k], self.zh.get(k), k)
            self.assertEqual(self.pen[k], self.en.get(k), k)
            self.assertTrue(self.pzh[k].strip() and self.pen[k].strip(), f"{k} 不能为空")
            self.assertEqual(set(re.findall(r"\{\w+\}", self.pzh[k])), set(re.findall(r"\{\w+\}", self.pen[k])), f"{k} 占位符不一致")

    def test_used_keys_exist_and_no_dead_keys(self):
        used = set(re.findall(r"""['"`](f03\.[\w.]+?)['"`]""", self.code))
        used |= set(re.findall(r'data-i18n="(f03\.[\w.]+)"', self.code))
        # public.js 用模板拼的 `${words}.more` / `.less`
        if "'f03.cards'" in self.code:
            used |= {"f03.cards.more", "f03.cards.less"}
        used.discard("f03.cards")
        for k in used:
            self.assertIn(k, self.zh, f"代码用到的 {k} 字典里没有")
        for k in set(self.pzh) - RENAMED:
            self.assertIn(k, used, f"部件里的 {k} 代码里没用到")

    def test_no_redline_or_compliance_words(self):
        for part in (self.pzh, self.pen):
            for k, v in part.items():
                self.assertIsNone(B.REDLINE.search(v), f"{k} 含红线词")
                self.assertIsNone(B.INTERNAL_API.search(v), f"{k} 含内部接口路径")
                for w in B.COMPLIANCE_WORDS:
                    self.assertNotIn(w, v, f"{k} 含合规词 {w}")

    def test_light_theme_does_not_leak_into_public_pages(self):
        tokens = read("quant-ai-dark/tokens.css")
        light = set(re.findall(r"(--[\w-]+)\s*:", css_block(tokens, ':root[data-theme="light"]')))
        scope = set(re.findall(r"(--[\w-]+)\s*:", css_block(read("shared/public-story.css"), ".ps-page,")))
        self.assertTrue(light)
        self.assertEqual(light - scope, set(), "浅色主题改写了、但 .ps-page 作用域没重写的 token 会漏进公开子页")

    def test_chart_colors_read_from_container(self):
        js = read("shared/chart-helpers.js")
        self.assertNotIn("chartColors()", js, "图表颜色要从容器读，才能拿到 .ps-page 作用域里的 token")
        self.assertGreaterEqual(js.count("chartColors(container)"), 4)
        self.assertIn("fmt(v, 1, unit)", js)
        self.assertEqual(js.count("}, '%');"), 2, "权益图和回撤图的价格轴要带 %")

    def test_engine_naming_unified(self):
        for k in ("pub.engine.rq", "engines.lock.rq_title", "lock.engines.title", "t5.pub.engines", "land.s3.col.rq"):
            for d in (self.zh, self.en):
                self.assertIn("RQAlpha", d[k], k)
                self.assertNotIn("米筐", d[k], k)
                self.assertNotIn("RiceQuant", d[k], k)

    def test_synthetic_footnotes_and_safe_dom(self):
        for rel in ("shared/page-replay.js", "shared/page-engines.js"):
            self.assertGreaterEqual(read(rel).count("t('pub.synthetic')"), 2, f"{rel} 游客锁定页也要有合成数据脚注")
        for rel in ("shared/page-strategies.js", "shared/page-market.js", "shared/page-signals.js", "shared/page-method.js"):
            self.assertIn("t('pub.synthetic')", read(rel), rel)
        for rel in JS_FILES:
            js = read(rel)
            for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
                self.assertNotIn(bad, js, f"{rel} 出现 {bad}")
            self.assertNotRegex(js, r"https?://", f"{rel} 不能加外网地址")

    def test_market_units(self):
        js = read("shared/page-market.js")
        self.assertIn("t('today.above_ma_foot'), '', '%')", js, "站上 20 日线的占比要带 %")
        self.assertIn("f03.market.heat_note", js)

    def test_static_first_frame_matches_dict(self):
        for rel in HTML_FILES:
            html = read(rel)
            found = re.findall(r'data-i18n="(f03\.[\w.]+)">([^<]+)<', html)
            self.assertTrue(found, rel)
            for k, text in found:
                self.assertEqual(text, self.zh[k], f"{rel} 的 {k} 静态文字和字典不一致")


if __name__ == "__main__":
    unittest.main(verbosity=2)
