"""A03：新界面「加载中 / 出错 / 空数据」三种状态卡片的检查。

只读 dashboard/ui 下的源文件，不启动服务、不读 data/；node 运行时检查在临时目录里写脚本。
1. A03 两份字典：合法 JSON、键完全一致、占位符一致、英文里没有中文、不和基础字典或其他 parts 撞键；代码用到的键都有、词条都被用到
2. 出错卡片的「回到首页 / 重试」靠 hidden 二选一：.btn 的 display 会盖掉 [hidden]，css 里必须有 .state [hidden] 兜底
3. page-boot.js 的出错 / 空状态都经过 state-card.js 填说明行；各页面骨架里有 state-card 依赖的节点
4. （有 node 时）stateLines 的分支；合并字典前不显示说明行（不露键名），合并后中英文各自正确
"""
import json
import sys
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "dashboard" / "ui"
sys.path.insert(0, str(Path(__file__).resolve().parent))
import i18n_parts_compat as parts_compat  # noqa: E402  部件（parts/）已退役，键名单来自快照、值来自基础字典
ZH = parts_compat.part("A03", "zh")
EN = parts_compat.part("A03", "en")
CJK = re.compile(r"[㐀-鿿　-〿＀-￯]")
PLACEHOLDER = re.compile(r"\{(\w+)\}")
# 本任务检查的页面：w/ 下除 _template、system、reports、publish 以外
PAGES = sorted(p.stem for p in (UI / "w").glob("*.html")
               if p.stem not in ("_template", "system", "reports", "publish"))
USERS = [UI / "shared" / "state-card.js", UI / "pages" / "holdings.js", UI / "pages" / "monitor.js"]


class A03DictTests(unittest.TestCase):
    def test_keys_identical_and_prefixed(self):
        self.assertEqual(set(ZH), set(EN))
        self.assertTrue(ZH)
        for k in ZH:
            self.assertTrue(k.startswith("a03."), k)

    def test_placeholders_match(self):
        for k in ZH:
            self.assertEqual(sorted(PLACEHOLDER.findall(ZH[k])), sorted(PLACEHOLDER.findall(EN[k])), k)

    def test_english_has_no_chinese(self):
        for k, v in EN.items():
            self.assertIsNone(CJK.search(v), f"{k}: {v}")

    def test_all_keys_in_base_dictionary(self):
        """部件已合并进基础字典并退役（parts/ 移到 _to_delete/）：本任务的每个键都必须在基础字典里。
        原来的「不和基础字典撞键」检查针对合并前的状态，合并后必然撞键，已不适用。"""
        for lang in ("zh", "en"):
            self.assertEqual(parts_compat.missing_in_base("A03", lang), [], lang)

    def test_used_keys_exist_and_all_keys_used(self):
        used = set()
        for f in USERS:
            used |= set(re.findall(r"""['"](a03\.[a-z_.0-9]+)['"]""", f.read_text(encoding="utf-8")))
        self.assertEqual(used - set(ZH), set(), "代码里用了字典里没有的键")
        self.assertEqual(set(ZH) - used, set(), "字典里有没被用到的键")


class StateCardStaticTests(unittest.TestCase):
    def test_pages_list_not_empty(self):
        self.assertIn("today", PAGES)
        self.assertEqual(len(PAGES), 13)

    def test_btn_display_overrides_hidden_so_state_needs_fallback(self):
        core = (UI / "shared" / "components-core.css").read_text(encoding="utf-8")
        self.assertRegex(core, r"\.btn\s*\{[^}]*display:\s*inline-flex", "前提变了：.btn 不再设 display，可重新评估")
        data = (UI / "shared" / "components-data.css").read_text(encoding="utf-8")
        self.assertRegex(data, r"\.state \[hidden\]\s*\{\s*display:\s*none;?\s*\}")
        self.assertRegex(data, r"\.state__hint\s*\{")

    def test_page_boot_routes_error_and_empty_through_state_card(self):
        src = (UI / "shared" / "page-boot.js").read_text(encoding="utf-8")
        self.assertIn("from './state-card.js'", src)
        self.assertEqual(src.count("setState('empty')"), 1, "空状态应该只在 showEmpty 里设置")
        self.assertRegex(src, r"function showEmpty\(\) \{\s*fillStateCard\(")
        self.assertRegex(src, r"fillStateCard\(box\?\.querySelector\('\.when-error'\), resolveLines\(stateLines\('error', err\)\)\)")
        self.assertNotIn("setState('empty'); return;", src)

    def test_state_card_uses_text_nodes_only(self):
        src = (UI / "shared" / "state-card.js").read_text(encoding="utf-8")
        for bad in ("inner" + "HTML", "outer" + "HTML", "insertAdjacent" + "HTML", "document." + "write"):
            self.assertNotIn(bad, src)
        self.assertIsNone(CJK.search(re.sub(r"/\*.*?\*/|//[^\n]*", "", src, flags=re.S)), "脚本里不该有中文字面量")

    def test_every_page_has_nodes_state_card_relies_on(self):
        for name in PAGES:
            html = (UI / "w" / f"{name}.html").read_text(encoding="utf-8")
            err = re.search(r'<div class="card state when-error"[^>]*>(.*?)</div>', html, re.S)
            empty = re.search(r'<div class="card state when-empty"[^>]*>(.*?)</div>', html, re.S)
            self.assertTrue(err and empty, name)
            self.assertIn('class="state__title" data-bind="error-title"', err.group(1), name)
            self.assertRegex(err.group(1), r'data-bind="login-link"[^>]*\shidden\b', name)
            self.assertIn('data-action="retry"', err.group(1), name)
            self.assertIn('class="state__title"', empty.group(1), name)


NODE_SCRIPT = r"""
const PART_KEYS = __PART_KEYS__;   // 部件已退役：键名单由 Python 从 tests/fixtures/i18n_parts_keys.json 填进来
const without = (d, keys) => Object.fromEntries(Object.entries(d).filter(([k]) => !keys.includes(k)));   // 合并前 = 基础字典去掉本任务的键
import fs from 'node:fs';
const [ui, merge] = process.argv.slice(2);
const uiUrl = new URL(`file://${ui}/`);
globalThis.document = { addEventListener() {}, documentElement: { dataset: {}, lang: '' }, querySelectorAll: () => [] };
globalThis.window = { location: { search: '' }, dispatchEvent() {} };
globalThis.CustomEvent = class { constructor(type, o) { this.type = type; this.detail = o?.detail; } };
globalThis.fetch = async (url) => {                     // 字典：基础字典；merge=1 时再叠上 A03 part（模拟早上合并）
  const s = String(url);
  const lang = s.endsWith('/en.json') ? 'en' : 'zh';
  const base = JSON.parse(fs.readFileSync(new URL(s), 'utf8'));
  return { ok: true, json: async () => (merge === '1' ? base : without(base, PART_KEYS)) };
};
const i18n = await import(new URL('shared/i18n.js', uiUrl));
const api = await import(new URL('shared/api.js', uiUrl));
const sc = await import(new URL('shared/state-card.js', uiUrl));
const cases = {
  forced: ['error', null],
  http404: ['error', new api.ApiError(404, 'HTTP 404')],
  noMsg: ['error', new api.ApiError(500, '')],
  auth: ['error', new api.ApiError(401, 'unauthorized')],
  script: ['error', new TypeError('x is undefined')],
  empty: ['empty', null],
  loading: ['loading', null],
};
const out = {};
for (const [k, [state, err]] of Object.entries(cases)) out[`keys.${k}`] = sc.stateLines(state, err).map((l) => l.key);
for (const lang of ['zh', 'en']) {
  await i18n.setLang(lang);
  for (const [k, [state, err]] of Object.entries(cases)) out[`${lang}.${k}`] = sc.resolveLines(sc.stateLines(state, err));
  out[`${lang}.bad_reply`] = i18n.t('a03.err.bad_reply', { api: 'holdings' });
}
console.log(JSON.stringify(out));
"""
NODE_SCRIPT = NODE_SCRIPT.replace("__PART_KEYS__", json.dumps(parts_compat.part_keys("A03")))


def run_node(merge):
    with tempfile.TemporaryDirectory() as d:
        script = Path(d) / "probe.mjs"
        script.write_text(NODE_SCRIPT, encoding="utf-8")
        r = subprocess.run(["node", "--experimental-default-type=module", str(script), str(UI), merge],
                           capture_output=True, text=True, timeout=30, cwd=d)
    if r.returncode != 0:
        raise AssertionError(r.stderr)
    return json.loads(r.stdout.strip().splitlines()[-1])


@unittest.skipUnless(shutil.which("node"), "需要 node")
class NodeRuntimeTests(unittest.TestCase):
    def test_branches(self):
        out = run_node("1")
        self.assertEqual(out["keys.forced"], ["a03.state.error_hint"])
        self.assertEqual(out["keys.http404"], ["a03.state.error_hint", "a03.state.error_reason"])
        self.assertEqual(out["keys.noMsg"], ["a03.state.error_hint"])
        self.assertEqual(out["keys.auth"], [])
        self.assertEqual(out["keys.script"], ["a03.state.error_hint", "a03.state.error_script"])
        self.assertEqual(out["keys.empty"], ["a03.state.empty_hint"])
        self.assertEqual(out["keys.loading"], [])

    def test_after_merge_both_languages(self):
        out = run_node("1")
        self.assertEqual(out["zh.http404"], [ZH["a03.state.error_hint"], "原因：HTTP 404"])
        self.assertEqual(out["en.http404"], [EN["a03.state.error_hint"], "Reason: HTTP 404"])
        self.assertEqual(out["zh.empty"], [ZH["a03.state.empty_hint"]])
        self.assertEqual(out["en.script"], [EN["a03.state.error_hint"], EN["a03.state.error_script"]])
        self.assertEqual(out["zh.bad_reply"], "holdings 接口返回的内容不对")
        self.assertEqual(out["en.bad_reply"], "The holdings API returned an unexpected reply")
        for k, v in out.items():
            if k.startswith("en.") and isinstance(v, list):
                for s in v:
                    self.assertIsNone(CJK.search(s), f"{k}: {s}")

    def test_before_merge_hides_lines_instead_of_showing_keys(self):
        out = run_node("0")
        for lang in ("zh", "en"):
            for k in ("forced", "http404", "noMsg", "auth", "script", "empty", "loading"):
                self.assertEqual(out[f"{lang}.{k}"], [], f"{lang}.{k}")


if __name__ == "__main__":
    unittest.main()
