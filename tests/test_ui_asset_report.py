"""静态资源体检脚本(scripts/ui_asset_report.py)的测试。造一个临时的小 ui 目录来验证;不读 data/。
只有最后一个冒烟测试会只读地扫描真实的 dashboard/ui(不写任何东西)。"""
import hashlib
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import ui_asset_report as R  # noqa: E402


def write(base: Path, rel: str, text: str):
    p = base / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def tree_digest(base: Path) -> dict:
    return {str(p.relative_to(base)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(base.rglob("*")) if p.is_file()}


PAGE_HTML = """<!DOCTYPE html><html><head>
<!-- <link rel="stylesheet" href="../shared/ghost.css"> -->
<link rel="stylesheet" href="../shared/base.css">
<link rel="stylesheet" href="../pages/a.css">
<script src="../shared/boot.js"></script>
<script type="module" src="../shared/page-boot.js"></script>
</head><body class="ws" data-page="a"><main class="card"><svg><use href="../shared/icons.svg#i-x"/></svg></main></body></html>"""


def make_ui(base: Path):
    """a.html 加载 page-boot → dom;page-boot 按 data-page 动态加载 pages/a.js;a.js 还字面量动态加载 dbg.js。"""
    write(base, "w/a.html", PAGE_HTML)
    write(base, "w/b.html", PAGE_HTML.replace('data-page="a"', 'data-page="b"').replace("pages/a.css", "pages/b.css"))
    write(base, "shared/boot.js", "(function(){ document.documentElement.classList.add('js'); })();\n")
    write(base, "shared/icons.svg", "<svg xmlns='http://www.w3.org/2000/svg'></svg>")
    write(base, "shared/page-boot.js",
          "// import { ghost } from './ghost.js';\n"
          "import { el } from './dom.js';\n"
          "let mod;\n"
          "export async function go(page) { mod = await import(`../pages/${page}.js`); return el; }\n")
    write(base, "shared/dom.js", "export const el = (t) => document.createElement(t);\n")
    write(base, "shared/dbg.js", "export const dbg = 1;\n")
    write(base, "pages/a.js",
          "import { el } from '../shared/dom.js';\n"
          "export async function render() {\n"
          "  if (location.search.includes('debug')) await import('../shared/dbg.js');\n"
          "  const row = el('div'); row.className = 'card tone-' + 'up'; row.classList.add('used-a');\n"
          "}\n")
    write(base, "pages/b.js", "import { el } from '../shared/dom.js';\nexport function render() { return el('p'); }\n")
    write(base, "shared/base.css",
          "@import './reset.css';\n"
          ".card { color: red; }\n"
          ".never-used { color: blue; }\n"
          ".tone-up { color: green; }\n"
          ".btn:not(.ghost-class) { margin: 0; }\n"
          "@media (min-width: 600px) { .never-used-media { padding: 1px; } }\n"
          "@keyframes spin { from { opacity: 0; } to { opacity: 1; } }\n"
          ".icon { background: url(\"data:image/svg+xml;base64,AAAA;BBB\"); }\n")
    write(base, "shared/reset.css", "* { box-sizing: border-box; }\n")
    write(base, "pages/a.css", ".used-a { x: 1; }\n[data-page=\"a\"] .card { y: 1; }\n[data-page=\"b\"] .card { z: 1; }\n")
    write(base, "pages/b.css", "[data-page=\"b\"] .card { y: 1; }\n")
    write(base, "pages/orphan.js", "import { el } from '../shared/dom.js';\nexport const o = el;\n")
    write(base, "pages/orphan.css", ".o { x: 1; }\n")
    write(base, "shared/dup1.js", "export const dbg = 1;\n")           # 和 dbg.js 内容完全一样
    write(base, "i18n/zh.json.before-N1", "{}")


class LexerTests(unittest.TestCase):
    def test_comments_and_strings(self):
        code, strings, ok = R.lex_js("// import x from './a.js'\nconst u = 'http://x'; /* import y from './b.js' */ const q = \"a // b\";\n")
        self.assertTrue(ok)
        self.assertNotIn("a.js", code)
        self.assertNotIn("b.js", code)
        self.assertEqual([s[1] for s in strings], ["http://x", "a // b"])

    def test_regex_literal_with_quote_does_not_open_a_string(self):
        code, strings, ok = R.lex_js("const r = /['\"]/g; const s = 'ok';\n")
        self.assertTrue(ok)
        self.assertEqual([s[1] for s in strings], ["ok"])

    def test_template_with_nested_expression(self):
        _code, strings, ok = R.lex_js("const x = `a ${ cond ? 'b-c' : `d-${e}` } tone-${k}`;\n")
        self.assertTrue(ok)
        texts = [(s[1], s[2]) for s in strings]
        self.assertIn(("b-c", False), texts)
        self.assertIn(("d-", True), texts)
        self.assertIn((" tone-", True), texts)

    def test_unterminated_string_reported(self):
        self.assertFalse(R.lex_js("const x = 'abc\n")[2])

    def test_import_forms(self):
        m = R.Module(Path("x.js"), "import a, { b } from './one.js';\nimport {\n  c,\n  d\n} from \"./two.js\";\nimport './three.js';\n"
                                   "export * from './four.js';\nexport { e } from './five.js';\nexport const k = 'from \\'z\\'';\n"
                                   "const p = import('./lazy.js'); const q = import(`../pages/${page}.js`);\n"
                                   "const u = new URL(`../i18n/${lang}.json`, import.meta.url);\n")
        self.assertEqual(m.static, ["./one.js", "./two.js", "./three.js", "./four.js", "./five.js"])
        self.assertEqual(m.lazy, ["./lazy.js"])
        self.assertEqual(m.template, ["../pages/${page}.js"])
        self.assertEqual(m.urls, ["../i18n/${lang}.json"])


class CssTests(unittest.TestCase):
    def test_rules_media_keyframes_and_data_uri(self):
        text = ("@import url('x.css');\n/* c */\n.a, .b > .c { x: 1; }\n@media (min-width: 1px) { .d { y: 2; } }\n"
                "@keyframes k { from { a: 1; } to { a: 2; } }\n.e { background: url(data:image/png;base64,AA;BB); }\n")
        rules, imports = R.parse_css(text)
        self.assertEqual(imports, ["x.css"])
        self.assertEqual([r.selectors for r in rules], [[".a", ".b > .c"], [".d"], [".e"]])
        self.assertEqual(rules[0].line, 3)
        self.assertTrue(all(r.bytes > 0 for r in rules))

    def test_selector_helpers(self):
        self.assertEqual(R.selector_classes('.a.b > .c:hover [data-x=".fake"]'), ["a", "b", "c"])
        self.assertEqual(R.selector_classes(".btn:not(.x):is(.y, .z)"), ["btn"])
        self.assertEqual(R.selector_scopes('[data-page="check"] .field, [data-page=x] .y'), ["check", "x"])


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ui = Path(self.tmp.name) / "ui"
        make_ui(self.ui)
        self.rep = R.build_report(self.ui, Path(self.tmp.name), top=50)

    def row(self, name):
        return next(r for r in self.rep["rows"] if r["page"] == name)

    def test_page_js_and_css_chain(self):
        a = self.row("a.html")
        self.assertEqual(sorted(a["js"]), ["pages/a.js", "shared/boot.js", "shared/dom.js", "shared/page-boot.js"])
        self.assertEqual(a["css"], ["shared/base.css", "pages/a.css", "shared/reset.css"])   # 注释里的 ghost.css 不算,@import 跟进
        self.assertEqual(a["lazy_js"], ["shared/dbg.js"])
        self.assertEqual(a["extras"], ["shared/icons.svg"])
        self.assertEqual(self.row("b.html")["js"].count("pages/b.js"), 1)
        self.assertNotIn("pages/b.js", a["js"])

    def test_byte_totals_match_disk(self):
        a = self.row("a.html")
        disk = lambda rels: sum((self.ui / r).stat().st_size for r in rels)
        self.assertEqual(a["js_bytes"], disk(a["js"]))
        self.assertEqual(a["css_bytes"], disk(a["css"]))
        self.assertEqual(a["total_bytes"], a["js_bytes"] + a["css_bytes"])
        self.assertEqual(a["requests"], 1 + len(a["js"]) + len(a["css"]) + 1)

    def test_shared_files_counted_per_page(self):
        by = {r["path"]: r for r in self.rep["shared"]}
        self.assertEqual(by["shared/dom.js"]["pages"], 2)
        self.assertTrue(by["shared/dom.js"]["every_page"])
        self.assertEqual(by["shared/dom.js"]["repeat_bytes"], by["shared/dom.js"]["bytes"])
        self.assertEqual(by["pages/a.css"]["pages"], 1)

    def test_unreferenced_and_backups(self):
        un = {u["path"]: u for u in self.rep["unreferenced"]}
        self.assertIn("pages/orphan.js", un)
        self.assertIn("pages/orphan.css", un)
        self.assertIn("shared/dup1.js", un)
        self.assertTrue(un["i18n/zh.json.before-N1"]["backup"])
        self.assertFalse(un["i18n/zh.json.before-N1"]["served"])
        self.assertNotIn("shared/dbg.js", un)                 # 只被按需加载也算被引用
        self.assertNotIn("shared/reset.css", un)              # 只被 @import 引用也算
        self.assertNotIn("shared/boot.js", un)

    def test_duplicate_content(self):
        files = [set(d["files"]) for d in self.rep["duplicates"]]
        self.assertTrue(any({"shared/dup1.js", "shared/dbg.js"} <= s for s in files))

    def test_missing_classes(self):
        miss = {m["class"]: m for m in self.rep["missing_all"]}
        self.assertIn("never-used", miss)
        self.assertIn("never-used-media", miss)
        self.assertEqual(miss["never-used"]["file"], "shared/base.css")
        self.assertEqual(miss["never-used"]["line"], 3)
        for used in ("card", "used-a"):                       # html 的 class 属性 / js 字符串里出现过
            self.assertNotIn(used, miss)
        self.assertIn("btn", miss)                            # 没人用的普通类名
        self.assertNotIn("ghost-class", miss)                 # :not() 里的不算"必须出现"
        self.assertNotIn("o", miss)                           # 没人加载的 css 不在类名清单里(它已在"无人引用文件"里)
        self.assertTrue(miss["tone-up"]["maybe_dynamic"])     # 'tone-' + 'up' 拼出来的:搜不到,但标记为疑似动态拼接
        self.assertFalse(miss["never-used"]["maybe_dynamic"])

    def test_dynamic_prefix_hint(self):
        write(self.ui, "pages/a.js", (self.ui / "pages/a.js").read_text(encoding="utf-8") + "const z = 'dyn-' + kind;\n")
        write(self.ui, "shared/base.css", (self.ui / "shared/base.css").read_text(encoding="utf-8") + ".dyn-hot { x: 1; }\n")
        rep = R.build_report(self.ui, Path(self.tmp.name))
        miss = {m["class"]: m for m in rep["missing_all"]}
        self.assertTrue(miss["dyn-hot"]["maybe_dynamic"])
        self.assertEqual(rep["missing_all"][-1]["class"], "dyn-hot")      # 疑似动态拼接的排在最后

    def test_per_page_dead_rules(self):
        d = {r["page"]: r for r in self.rep["per_page_dead"]}
        self.assertGreater(d["a.html"]["dead_bytes"], 0)
        # a 页:[data-page=b] .card 是死的,[data-page=a] .card 是活的
        a_css = next(f for f in d["a.html"]["files"] if f["path"] == "pages/a.css")
        z_rule = len('[data-page="b"] .card { z: 1; }'.encode())
        self.assertEqual(a_css["dead_bytes"], z_rule)
        b_css = next(f for f in d["b.html"]["files"] if f["path"] == "pages/b.css")
        self.assertEqual(b_css["dead_bytes"], 0)

    def test_markdown_renders_all_sections(self):
        md = R.render_md(self.rep, 50)
        for head in ("## 1.", "## 2.", "## 3.", "## 4.", "## 5.", "## 6."):
            self.assertIn(head, md)
        self.assertIn("a.html", md)
        self.assertIn("pages/orphan.js", md)

    def test_warnings_for_broken_reference(self):
        write(self.ui, "pages/a.js", "import { x } from './missing.js';\n")
        rep = R.build_report(self.ui, Path(self.tmp.name))
        self.assertTrue(any("missing.js" in w for w in rep["warnings"]))


class CliTests(unittest.TestCase):
    def run_cli(self, args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = R.main(args)
        return rc, out.getvalue(), err.getvalue()

    def test_read_only_and_outputs_elsewhere(self):
        with tempfile.TemporaryDirectory() as d:
            ui = Path(d) / "ui"
            make_ui(ui)
            before = tree_digest(ui)
            outdir = Path(d) / "out"
            outdir.mkdir()
            rc, _o, _e = self.run_cli(["--ui-root", str(ui), "--repo-root", d, "--out", str(outdir / "r.md"),
                                       "--json-out", str(outdir / "r.json"), "--classes-csv", str(outdir / "c.csv")])
            self.assertEqual(rc, 0)
            self.assertEqual(tree_digest(ui), before)               # ui 目录一个字节都没动
            self.assertIn("never-used", (outdir / "c.csv").read_text(encoding="utf-8"))
            self.assertIn('"rows"', (outdir / "r.json").read_text(encoding="utf-8"))
            self.assertTrue((outdir / "r.md").read_text(encoding="utf-8").startswith("# 静态资源体积体检"))

    def test_refuses_to_write_inside_ui(self):
        with tempfile.TemporaryDirectory() as d:
            ui = Path(d) / "ui"
            make_ui(ui)
            rc, _o, err = self.run_cli(["--ui-root", str(ui), "--repo-root", d, "--out", str(ui / "report.md")])
            self.assertEqual(rc, 2)
            self.assertFalse((ui / "report.md").exists())
            self.assertIn("只读", err)

    def test_stdout_by_default(self):
        with tempfile.TemporaryDirectory() as d:
            ui = Path(d) / "ui"
            make_ui(ui)
            rc, out, _e = self.run_cli(["--ui-root", str(ui), "--repo-root", d])
            self.assertEqual(rc, 0)
            self.assertIn("## 3.", out)

    def test_bad_ui_root(self):
        with tempfile.TemporaryDirectory() as d:
            rc, _o, err = self.run_cli(["--ui-root", d])
            self.assertEqual(rc, 2)
            self.assertIn("找不到", err)


class RealTreeSmokeTests(unittest.TestCase):
    """只读地扫真实的 dashboard/ui:每个引用都能解析,每页都有 js 和 css。"""

    def test_real_ui_resolves_cleanly(self):
        ui = ROOT / "dashboard" / "ui"
        if not (ui / "w").is_dir():
            self.skipTest("没有 dashboard/ui")
        rep = R.build_report(ui, ROOT, top=5)
        self.assertEqual(rep["warnings"], [])
        self.assertGreaterEqual(rep["pages"], 10)
        for r in rep["rows"]:
            self.assertGreater(r["js_bytes"], 0, r["page"])
            self.assertGreater(r["css_bytes"], 0, r["page"])
            self.assertIn("shared/page-boot.js", r["js"], r["page"])
            if r["data_page"]:
                self.assertIn(f"pages/{r['data_page']}.js", r["js"], r["page"])


if __name__ == "__main__":
    unittest.main()
