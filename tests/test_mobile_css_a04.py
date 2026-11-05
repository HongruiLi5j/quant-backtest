"""A04：手机宽度（375px）的 CSS 修复是否还在（静态检查，只读 dashboard/ui 下的源文件，不启动服务、不读 data/）。

真正的 375px 实测（Chrome 无头、逐页遍历元素的 getBoundingClientRect）在 outputs/A04/audit_mobile.py，
那个脚本要启动浏览器，不适合放进单元测试；这里只守住「修复用的那几条规则不被改掉」：
1. signals 的折叠块：.u1-fold__body 是 grid，必须显式 minmax(0, 1fr) 列，否则宽表格会把整列撑到 1338px，被 html 的 overflow-x: clip 裁掉
2. 代码里用到的 class="tablewrap"（check.js / ledger.js）必须有对应的 CSS 规则，且是横向滚动容器
3. check 页的浮动标签：.field__label 在基础样式里是绝对定位，check 页必须把它改回普通流
4. holdings 的 KPI：480px 以下字号变小 + overflow-wrap，数字不压过卡片边框
5. 顶栏：480px 以下品牌文字用「视觉隐藏」（clip），不能用 display:none（会丢掉链接的可访问名称）
6. 站点的 html/body 仍是 overflow-x: clip——这是上面这些规则存在的前提（越界内容会被直接裁掉而不是出现滚动条）
7. 单个 css 文件 ≤ 500 行（dashboard/ui/README.md 的约定）
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "dashboard" / "ui"


def css(rel):
    return (UI / rel).read_text(encoding="utf-8")


def rule_body(text, selector):
    """返回第一条以 selector 开头的规则的花括号内容（不处理嵌套，只用于顶层规则）"""
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", text)
    return m.group(1) if m else None


def media_block(text, query):
    """返回 @media (query) { ... } 里的内容（按花括号配对，取第一个匹配）"""
    i = text.find(f"@media {query}")
    if i < 0:
        return None
    start = text.index("{", i)
    depth = 0
    for j in range(start, len(text)):
        depth += {"{": 1, "}": -1}.get(text[j], 0)
        if depth == 0:
            return text[start + 1:j]
    return None


class MobileCssTests(unittest.TestCase):
    def test_signals_fold_body_has_bounded_column(self):
        body = rule_body(css("pages/U1.css"), ".u1-fold__body")
        self.assertIsNotNone(body)
        self.assertIn("display: grid", body)
        self.assertIn("grid-template-columns: minmax(0, 1fr)", body)

    def test_every_tablewrap_class_in_js_has_a_scroll_rule(self):
        used = [p for p in (UI / "pages").glob("*.js") if re.search(r"class:\s*'tablewrap'", p.read_text(encoding="utf-8"))]
        self.assertTrue(used, "check.js / ledger.js 应该在用 tablewrap")
        body = rule_body(css("pages/U4.css"), ".tablewrap")
        self.assertIsNotNone(body, "tablewrap 没有 CSS 规则，表格会被 overflow-x: clip 裁掉")
        self.assertIn("overflow-x: auto", body)
        # 用到 tablewrap 的页面必须引了 U4.css
        for js in used:
            page = (UI / "w" / f"{js.stem}.html").read_text(encoding="utf-8")
            self.assertIn("../pages/U4.css", page, js.name)

    def test_check_page_floating_label_is_reset(self):
        html = (UI / "w" / "check.html").read_text(encoding="utf-8")
        self.assertIn('data-page="check"', html)
        u4 = css("pages/U4.css")
        label = rule_body(u4, '[data-page="check"] .field__label')
        self.assertIsNotNone(label)
        self.assertIn("position: static", label)
        self.assertIn("transform: none", label)
        # 基础样式确实是浮动标签（绝对定位）——如果哪天基础样式改了，这条提醒回头看看 check 页的覆盖是否还需要
        self.assertIn("position: absolute", rule_body(css("shared/components-core.css"), ".field__label"))
        # 输入控件占满宽度，按钮行允许换行
        self.assertIn("width: 100%", rule_body(u4, '[data-page="check"] .input'))
        self.assertIn("flex-wrap: wrap", rule_body(u4, '[data-page="check"] .row'))

    def test_holdings_kpi_fits_two_column_cards_on_phone(self):
        block = media_block(css("pages/U3.css"), "(max-width: 480px)")
        self.assertIsNotNone(block)
        value = rule_body(block, ".ws-grid--kpi6 .metric__value")
        self.assertIsNotNone(value)
        # D81 起字号可能写成 token（var(--fs-md) 等），按 fonts.css 里 token 的 rem 值换算成 px 再比
        m = re.search(r"font-size:\s*(\d+)px", value)
        if m:
            size = int(m.group(1))
        else:
            tok = re.search(r"font-size:\s*var\((--fs-[\w-]+)\)", value).group(1)
            rem = re.search(re.escape(tok) + r":\s*([\d.]+)rem", css("shared/fonts.css")).group(1)
            size = round(float(rem) * 16)
        self.assertLessEqual(size, 18)          # 22px 的 “500,000.00” 要 128px，装不进约 100px 的卡片内容区
        self.assertIn("overflow-wrap: anywhere", value)
        self.assertIn("min-width: 0", rule_body(block, ".u3-capital"))

    def test_topnav_brand_text_is_visually_hidden_not_removed(self):
        block = media_block(css("shared/topnav.css"), "(max-width: 480px)")
        self.assertIsNotNone(block)
        body = rule_body(block, ".topnav__brand > span:last-child")
        self.assertIsNotNone(body)
        self.assertIn("clip-path", body)
        self.assertNotIn("display: none", body)
        # nav.js 里品牌文字确实是链接里最后一个 span（选择器依赖这个结构）
        nav = (UI / "nav.js").read_text(encoding="utf-8")
        self.assertRegex(nav, r"el\('span', \{ class: 'brand__mark'[^\n]*el\('span', \{\}, 'Quant Backtest'\)")

    def test_page_root_clips_horizontal_overflow(self):
        reset = css("shared/reset.css")
        self.assertRegex(reset, r"overflow-x:\s*clip")

    def test_css_files_stay_under_500_lines(self):
        for rel in ("pages/U1.css", "pages/U3.css", "pages/U4.css", "shared/topnav.css"):
            self.assertLessEqual(len(css(rel).splitlines()), 500, rel)


if __name__ == "__main__":
    unittest.main()
