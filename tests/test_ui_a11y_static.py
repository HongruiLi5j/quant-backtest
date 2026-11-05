"""A11：按钮 / 图标 / 可点击元素的无障碍标签与键盘可达检查。

只读 dashboard/ui 下的源文件，不启动服务、不读 data/；node 运行时检查直接读 ui 目录（只读，不写）。
1. A11 两份字典：合法 JSON、键完全一致、占位符一致、英文里没有中文、不和基础字典或其他 parts 撞键；代码用到的 a11y.* 键都有、词条都被用到
2. 只有图标（或只有 × 之类符号）没有文字的 <button>（html 和 js 里 el('button', …)）必须有 aria-label；aria-label 不能写死中文，要走 t()
3. 可点击的非按钮元素：带 tabindex 的表格行必须用 activateOnKey（Enter 和空格都响应），不能只写 Enter；不许 role="button" 的 div / span；html 里不许行内 onclick
4. 弹窗：el('dialog') 必须有 aria-label / aria-labelledby、用 showModal()（浏览器自带 Esc 关闭和焦点陷阱）、关闭后还焦点（restoreFocusOnClose）；页内确认区用 closeOnEscape
5. （有 node 时）假 DOM 里真的跑 shared/a11y.js 和持仓页的确认区 / 撤销确认：焦点落点、Esc、朗读标签；词条没合并前不露键名
"""
import json
import sys
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "dashboard" / "ui"
sys.path.insert(0, str(Path(__file__).resolve().parent))
import i18n_parts_compat as parts_compat  # noqa: E402  部件（parts/）已退役，键名单来自快照、值来自基础字典
ZH = parts_compat.part("A11", "zh")
EN = parts_compat.part("A11", "en")
CJK = re.compile(r"[㐀-鿿　-〿＀-￯]")
PLACEHOLDER = re.compile(r"\{(\w+)\}")

JS_FILES = sorted(list((UI / "pages").glob("*.js")) + list((UI / "shared").glob("*.js")) + [UI / "nav.js"])
HTML_FILES = sorted((UI / "w").glob("*.html"))
# el('button', …) 刚创建时没有任何子节点、文字是后面 append 进去的：这里只放已人工确认过的
EMPTY_BUTTON_OK = {"quota-bar.js"}


def rel(p):
    return str(p.relative_to(UI))


def read(p):
    return p.read_text(encoding="utf-8")


# ---------- 极简 js 解析：找 el('tag', attrs, …children) 调用，拆出顶层参数 ----------
def _skip_string(src, i):
    """src[i] 是引号：返回字符串结束后的下标（支持转义；模板字符串里的 ${…} 当普通文字跳过）"""
    q = src[i]
    j = i + 1
    while j < len(src):
        if src[j] == "\\":
            j += 2
            continue
        if src[j] == q:
            return j + 1
        j += 1
    return len(src)


def split_call(src, open_idx):
    """open_idx 指向 '('：返回 (顶层参数列表, 结束下标)"""
    args, depth, start, i = [], 0, open_idx + 1, open_idx
    while i < len(src):
        c = src[i]
        if c in "'\"`":
            i = _skip_string(src, i)
            continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if depth == 0:
                tail = src[start:i].strip()
                if tail:
                    args.append(tail)
                return args, i + 1
        elif c == "," and depth == 1:
            args.append(src[start:i].strip())
            start = i + 1
        i += 1
    return args, len(src)


def strip_js_comments(src):
    """去掉 // 和 /* */ 注释（字符串里的 // 不动），保持行数不变"""
    out, i, n = [], 0, len(src)
    while i < n:
        c = src[i]
        if c in "'\"`":
            j = _skip_string(src, i)
            out.append(src[i:j])
            i = j
        elif src.startswith("//", i) and (i == 0 or src[i - 1] != ":"):
            while i < n and src[i] != "\n":
                i += 1
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append("\n" * src.count("\n", i, j))
            i = j
        else:
            out.append(c)
            i += 1
    return "".join(out)


def el_calls(src, tag_pattern):
    """返回 [(行号, tag, attrs 文本, [children 文本])]"""
    res = []
    for m in re.finditer(r"\bel\(\s*'(%s)'" % tag_pattern, src):
        open_idx = src.index("(", m.start())
        args, _ = split_call(src, open_idx)
        attrs = args[1] if len(args) > 1 and args[1].startswith("{") else ""
        kids = args[2:] if attrs else args[1:]
        res.append((src.count("\n", 0, m.start()) + 1, m.group(1), attrs, kids))
    return res


def is_icon_only(kids):
    """子节点全是图标（icon(…) / svg(…) / el('svg'…)）或全是没有字母数字汉字的符号字符串（×、✕）"""
    if not kids:
        return False
    for k in kids:
        if re.match(r"(icon|svg)\(|el\(\s*'svg'", k):
            continue
        lit = re.fullmatch(r"'([^']*)'|\"([^\"]*)\"", k)
        if lit and not re.search(r"\w", lit.group(1) or lit.group(2) or ""):
            continue
        return False
    return True


def has_label(attrs):
    return "'aria-label'" in attrs or "aria-labelledby" in attrs


class A11DictTests(unittest.TestCase):
    def test_keys_identical_and_prefixed(self):
        self.assertEqual(set(ZH), set(EN))
        self.assertTrue(ZH)
        for k in ZH:
            self.assertTrue(k.startswith("a11y."), k)

    def test_placeholders_match(self):
        for k in ZH:
            self.assertEqual(sorted(PLACEHOLDER.findall(ZH[k])), sorted(PLACEHOLDER.findall(EN[k])), k)

    def test_english_has_no_chinese_and_chinese_not_empty(self):
        for k, v in EN.items():
            self.assertIsNone(CJK.search(v), f"{k}: {v}")
        for k, v in ZH.items():
            self.assertTrue(v.strip(), k)

    def test_all_keys_in_base_dictionary(self):
        """部件已合并进基础字典并退役（parts/ 移到 _to_delete/）：本任务的每个键都必须在基础字典里。
        原来的「不和基础字典撞键」检查针对合并前的状态，合并后必然撞键，已不适用。"""
        for lang in ("zh", "en"):
            self.assertEqual(parts_compat.missing_in_base("A11", lang), [], lang)

    def test_used_keys_exist_and_all_keys_used(self):
        used = set()
        for p in JS_FILES:
            used |= set(re.findall(r"'(a11y\.[\w.]+)'", strip_js_comments(read(p))))
        self.assertEqual(used - set(ZH), set(), "代码里用到但字典里没有")
        self.assertEqual(set(ZH) - used, set(), "字典里有但没人用")


class IconOnlyButtonTests(unittest.TestCase):
    def test_html_buttons_have_text_or_label(self):
        n = 0
        for p in HTML_FILES:
            for m in re.finditer(r"<button\b([^>]*)>(.*?)</button>", read(p), re.S):
                n += 1
                inner = re.sub(r"<svg.*?</svg>", "", m.group(2), flags=re.S)
                text = re.sub(r"<[^>]+>", "", inner).strip()
                attrs = m.group(1)
                ok = text or "aria-label=" in attrs or re.search(r"data-i18n-attr=\"[^\"]*aria-label:", attrs)
                self.assertTrue(ok, f"{rel(p)}: 没有文字也没有 aria-label 的按钮 {m.group(0)[:80]}")
        self.assertGreater(n, 0)

    def test_html_has_no_inline_handlers_or_div_buttons(self):
        for p in HTML_FILES:
            src = read(p)
            self.assertIsNone(re.search(r"\son(click|keydown|keyup)=", src), rel(p))
            self.assertIsNone(re.search(r'<(div|span|li)\b[^>]*role="button"', src), rel(p))

    def test_js_icon_only_buttons_have_aria_label(self):
        n = 0
        for p in JS_FILES:
            for line, _, attrs, kids in el_calls(strip_js_comments(read(p)), "button"):
                n += 1
                if is_icon_only(kids):
                    self.assertTrue(has_label(attrs), f"{rel(p)}:{line} 只有图标的按钮没有 aria-label")
                if not kids and p.name not in EMPTY_BUTTON_OK:
                    self.assertTrue(has_label(attrs), f"{rel(p)}:{line} 没有子节点的按钮没有 aria-label（文字后面才 append 的请加进 EMPTY_BUTTON_OK）")
        self.assertGreater(n, 20)

    def test_aria_label_never_hardcoded_chinese(self):
        for p in JS_FILES:
            src = strip_js_comments(read(p))
            for m in re.finditer(r"'aria-label'\s*:\s*(['\"`])([^'\"`]*)\1", src):
                self.assertIsNone(CJK.search(m.group(2)), f"{rel(p)}: aria-label 写死了中文，要走 t()")
        for p in HTML_FILES:
            # html 里允许「中文默认值 + data-i18n-attr」的老写法，但必须有 data-i18n-attr 才能翻成英文
            for m in re.finditer(r"<[^>]*\saria-label=\"([^\"]*)\"[^>]*>", read(p)):
                if CJK.search(m.group(1)):
                    self.assertIn("data-i18n-attr", m.group(0), f"{rel(p)}: 中文 aria-label 没有 data-i18n-attr")


class ClickableNonButtonTests(unittest.TestCase):
    def test_no_div_span_acting_as_button(self):
        for p in JS_FILES:
            for line, tag, attrs, _ in el_calls(strip_js_comments(read(p)), "div|span|li|td|p|i|img"):
                self.assertNotIn("role: 'button'", attrs, f"{rel(p)}:{line} <{tag}> 当按钮用：请改成 <button>")
                self.assertNotRegex(attrs, r"\bonclick\b", f"{rel(p)}:{line}")

    def test_focusable_rows_use_activate_on_key(self):
        rows = 0
        for p in JS_FILES:
            src = strip_js_comments(read(p))
            focusable = [c for c in el_calls(src, "tr") if re.search(r"tabindex", c[2], re.I)]
            focusable += [(0, "tr", "", []) for _ in re.finditer(r"\btr\.tabIndex\s*=", src)]
            if not focusable:
                continue
            rows += len(focusable)
            self.assertIn("activateOnKey(", src, f"{rel(p)}: 带 tabindex 的表格行没有用 activateOnKey（Enter 和空格都要响应）")
            self.assertIsNone(re.search(r"tr\.addEventListener\('keydown'", src), f"{rel(p)}: 表格行还在手写只认 Enter 的 keydown")
        # D49-2（revtech A11-03）：market.js 汇总表和 stock.js 策略对比表不再整行可聚焦（读屏念成没名字的「行」），
        # 改成首格放 <button aria-pressed>策略名</button>；这里守住新写法，并且行上不能再出现 aria-selected
        for name, cls in (("market.js", "u4-rowbtn"), ("stock.js", "u2-rowbtn")):
            src = strip_js_comments(read(UI / "pages" / name))
            self.assertIn(f"class: '{cls}', type: 'button', 'aria-pressed'", src, f"{name}: 可点的策略行首格应是带 aria-pressed 的按钮")
            self.assertNotRegex(src, r"el\('tr', \{[^}]*tabindex", f"{name}: 表格行不应再可聚焦")
            self.assertNotIn("'aria-selected'", src, f"{name}: 普通表格行不支持 aria-selected")

    def test_activate_helper_handles_enter_and_space(self):
        src = read(UI / "shared" / "a11y.js")
        self.assertIn("e.key === 'Enter'", src)
        self.assertIn("e.key === ' '", src)
        self.assertIn("preventDefault", src)


class DialogTests(unittest.TestCase):
    def test_dialogs_are_labelled_native_and_restore_focus(self):
        n = 0
        for p in JS_FILES:
            src = strip_js_comments(read(p))
            calls = el_calls(src, "dialog")
            if not calls:
                continue
            for line, _, attrs, _ in calls:
                n += 1
                self.assertTrue(has_label(attrs), f"{rel(p)}:{line} <dialog> 没有 aria-label")
            self.assertIn(".showModal()", src, f"{rel(p)}: 弹窗要用 showModal()（自带 Esc 关闭和焦点陷阱）")
            self.assertIsNone(re.search(r"\.show\(\)", src.replace("msg.show()", "")), f"{rel(p)}: 非模态 dialog.show() 没有 Esc 和焦点陷阱")
            self.assertIn("restoreFocusOnClose(", src, f"{rel(p)}: 弹窗关闭后没有还焦点")
        self.assertGreaterEqual(n, 3, "快捷键表、全局搜索、后台确认框三处")

    def test_html_dialogs_labelled(self):
        for p in HTML_FILES:
            for m in re.finditer(r"<dialog\b[^>]*>", read(p)):
                self.assertRegex(m.group(0), r"aria-label(ledby)?=", rel(p))

    def test_keys_dialog_can_take_focus(self):
        src = strip_js_comments(read(UI / "shared" / "page-boot.js"))
        self.assertIn("dlg.tabIndex = -1", src)
        self.assertRegex(src, r"showModal\(\);\s*dlg\.focus\(\)")

    def test_inline_confirmations_close_on_escape(self):
        form = strip_js_comments(read(UI / "pages" / "holdings-form.js"))
        self.assertGreaterEqual(form.count("closeOnEscape("), 2, "录入确认区和撤销确认都要能按 Esc 退出")
        self.assertIn("cancel.focus()", form)       # 撤销确认默认焦点在更安全的「取消」
        self.assertIn("back.focus()", form)         # 录入确认默认焦点在「返回修改」
        self.assertIn("closeOnEscape(", strip_js_comments(read(UI / "pages" / "holdings.js")))


NODE_PROBE = ROOT / "tests" / "a11_probe.mjs"


def run_node(merge):
    r = subprocess.run(["node", "--experimental-default-type=module", str(NODE_PROBE), str(UI), merge],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise AssertionError(r.stderr)
    return json.loads(r.stdout.strip().splitlines()[-1])


@unittest.skipUnless(shutil.which("node"), "需要 node")
class NodeRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.merged = run_node("1")
        cls.unmerged = run_node("0")

    def test_labels_follow_language_after_merge(self):
        self.assertEqual(self.merged["zh.label"], ZH["a11y.watch_add"].replace("{n}", "豫光金铅"))
        self.assertEqual(self.merged["en.label"], EN["a11y.watch_add"].replace("{n}", "豫光金铅"))
        self.assertEqual(self.merged["zh.void_label"], ZH["a11y.void_trade"].replace("{date}", "2026-03-02").replace("{code}", "600531"))
        self.assertEqual(self.merged["en.void_label"], EN["a11y.void_trade"].replace("{date}", "2026-03-02").replace("{code}", "600531"))

    def test_before_merge_no_label_instead_of_key_name(self):
        for lang in ("zh", "en"):
            self.assertIsNone(self.unmerged[f"{lang}.label"])
            self.assertIsNone(self.unmerged[f"{lang}.void_label"])
            self.assertIsNone(self.unmerged[f"{lang}.tMaybe_missing"])

    def test_activate_on_key(self):
        for out in (self.merged, self.unmerged):
            for lang in ("zh", "en"):
                a = out[f"{lang}.activate"]
                self.assertEqual(a["hits"], 2)             # Enter 和空格各一次；其他键 / 带修饰键不触发
                self.assertEqual(a["hitsAfterInner"], 2)   # 焦点在行内链接上冒泡上来的按键不触发
                self.assertTrue(a["enterPrevented"] and a["spacePrevented"])   # 空格不能让页面滚动

    def test_focus_restore_and_escape_helpers(self):
        for lang in ("zh", "en"):
            self.assertEqual(self.merged[f"{lang}.restore"], "button:")
            self.assertIsNone(self.merged[f"{lang}.restore_orphan"])   # 原按钮已被重绘掉：不乱抢焦点
            self.assertEqual(self.merged[f"{lang}.escape"], {"esc": 1, "prevented": True})

    def test_holdings_confirm_focus_flow(self):
        for out in (self.merged, self.unmerged):
            for lang, back in (("zh", "返回修改"), ("en", "Back to edit")):
                o = out[f"{lang}.confirm_open"]
                self.assertFalse(o["hidden"])
                self.assertEqual((o["role"], o["labelledby"], o["titleExists"]), ("group", "holdConfirmTitle", True))
                self.assertEqual(o["focus"], f"button:{back}")                        # 焦点进入确认区，且落在不会入账的按钮上
                self.assertEqual(out[f"{lang}.confirm_esc"], {"hidden": True, "kids": 0, "focusIsSubmit": True})
                self.assertEqual(out[f"{lang}.confirm_back"], {"hidden": True, "focusIsSubmit": True})

    def test_holdings_void_confirm_focus_flow(self):
        for out in (self.merged, self.unmerged):
            for lang, cancel in (("zh", "取消"), ("en", "Cancel")):
                o = out[f"{lang}.void_open"]
                self.assertEqual((o["kids"], o["focus"], o["describedbyMatches"]), (3, f"button:{cancel}", True))
                self.assertEqual(out[f"{lang}.void_esc"], {"kids": 1, "restored": True, "focusIsOriginal": True})
                self.assertEqual(out[f"{lang}.void_cancel"], {"restored": True, "focusIsOriginal": True})
                self.assertTrue(out[f"{lang}.void_esc_again_ok"])


if __name__ == "__main__":
    unittest.main()
