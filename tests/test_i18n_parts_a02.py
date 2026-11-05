"""A02：新界面页面脚本里残留的中文移进字典 parts/A02.*.json 的检查。

只读 dashboard/ui 下的源文件，不启动服务、不读 data/；node 运行时检查在临时目录里写脚本。
1. A02 两份字典：合法 JSON、键完全一致、占位符一致、英文里没有中文、不和基础字典撞键
2. 页面 / 公共模块里用到的 t('a02.…') 都有词条，词条也都被用到
3. pages/*.js、shared/*.js 里还剩的中文字符串字面量，只能是白名单里的业务值或日志
4. （有 node 时）模拟早上合并后的字典，api.js 的网络错误提示、括号格式在英文下是英文
"""
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "dashboard" / "ui"
PARTS = UI / "i18n" / "parts"
ZH = json.loads((PARTS / "A02.zh.json").read_text(encoding="utf-8"))
EN = json.loads((PARTS / "A02.en.json").read_text(encoding="utf-8"))
CJK = re.compile(r"[㐀-鿿　-〿＀-￯]")
PLACEHOLDER = re.compile(r"\{(\w+)\}")
# 另一个线程负责的页面，本任务不扫
OTHER_THREAD = ("system", "reports", "publish")

# 允许留在 js 里的中文字面量：接口返回的业务枚举值（只用来比较 / 当筛选参数发给接口）、
# 控制台日志和内部错误、语言选择按钮上的自称「中文」、已按 zh 参数二选一的导出标题、没人用的市场标签
ALLOWED = {
    "'买入'", "'卖出'", "'加仓'", "'减仓'", "'换仓'", "'持仓'", "'空仓'", "'有效'", "'退市'", "'个股'",
    "'正常'", "'流动性差'", "'流动性未知'",
    "`u4-click ${r.kind === '个股' ? 'u4-pin' : ''}`",
    "'holdings 返回异常'", "'monitor 返回异常'", "'api 只接受以 / 开头的相对路径'", "`i18n ${lang} 加载失败`",
    "'debug-fields 加载失败'", "'页面渲染失败'", "'字典加载失败，按页面里写的中文显示'",
    "'中文'", "'## 局限'", "'A股'",
}


def js_strings(src):
    """取出 js 源码里的字符串字面量（跳过 // 和 /* */ 注释、正则字面量），返回 [(行号, 字面量原文)]"""
    out, i, line, n, prev = [], 0, 1, len(src), ""
    while i < n:
        c = src[i]
        if c == "\n":
            line += 1; i += 1; continue
        if src.startswith("//", i):
            while i < n and src[i] != "\n":
                i += 1
            continue
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            line += src.count("\n", i, j); i = j; continue
        if c == "/" and (prev in ("", "return") or (len(prev) == 1 and prev in "(,=:[!&|?{};+-*%<>~^")):
            j, in_cls = i + 1, False                          # 正则字面量：跳到结尾的 /
            while j < n and src[j] != "\n":
                if src[j] == "\\":
                    j += 2; continue
                if src[j] == "[":
                    in_cls = True
                elif src[j] == "]":
                    in_cls = False
                elif src[j] == "/" and not in_cls:
                    break
                j += 1
            i, prev = j + 1, "x"; continue
        if c in "'\"`":
            j, depth, start = i + 1, 0, line
            while j < n:
                d = src[j]
                if d == "\\":
                    j += 2; continue
                if d == "\n":
                    line += 1
                if c == "`" and src.startswith("${", j):
                    depth += 1; j += 2; continue
                if c == "`" and depth and d == "}":
                    depth -= 1; j += 1; continue
                if d == c and not depth:
                    break
                j += 1
            out.append((start, src[i:j + 1]))
            i, prev = j + 1, "x"; continue
        if not c.isspace():
            if c.isalnum() or c in "_$":
                j = i
                while j < n and (src[j].isalnum() or src[j] in "_$"):
                    j += 1
                prev = "return" if src[i:j] == "return" else "x"
                i = j; continue
            prev = "x" if c in ")]" else c
        i += 1
    return out


def scoped_js():
    files = sorted((UI / "pages").glob("*.js")) + sorted((UI / "shared").glob("*.js"))
    return [f for f in files if not f.stem.startswith(OTHER_THREAD)]


class A02DictTests(unittest.TestCase):
    def test_keys_identical_and_prefixed(self):
        self.assertEqual(set(ZH), set(EN))
        self.assertTrue(ZH)
        for k in ZH:
            self.assertTrue(k.startswith("a02."), k)

    def test_placeholders_match(self):
        for k in ZH:
            self.assertEqual(sorted(PLACEHOLDER.findall(ZH[k])), sorted(PLACEHOLDER.findall(EN[k])), k)

    def test_english_has_no_chinese(self):
        for k, v in EN.items():
            self.assertIsNone(CJK.search(v), f"{k}: {v}")

    def test_no_collision_with_base_or_other_parts(self):
        for lang in ("zh", "en"):
            base = json.loads((UI / "i18n" / f"{lang}.json").read_text(encoding="utf-8"))
            self.assertFalse(set(ZH) & set(base), lang)
            for p in PARTS.glob(f"*.{lang}.json"):
                if p.name.startswith("A02."):
                    continue
                self.assertFalse(set(ZH) & set(json.loads(p.read_text(encoding="utf-8"))), p.name)

    def test_used_keys_exist_and_all_keys_used(self):
        used = set()
        for f in scoped_js():
            used |= set(re.findall(r"""t\(\s*['"](a02\.[a-z_.0-9]+)['"]""", f.read_text(encoding="utf-8")))
        # api.js 里超时 / 断网两条是三元表达式选键：t(cond ? 'a02.err.timeout' : 'a02.err.network')
        api = (UI / "shared" / "api.js").read_text(encoding="utf-8")
        used |= set(re.findall(r"'(a02\.err\.(?:timeout|network))'", api))
        self.assertEqual(used - set(ZH), set(), "代码里用了字典里没有的键")
        self.assertEqual(set(ZH) - used, set(), "字典里有没被用到的键")


class ResidualChineseTests(unittest.TestCase):
    def test_scanner_skips_comments(self):
        src = "// '注释'\n/* '块' */ const a = '可见'; const r = /'正则'/; const b = `${x ? '甲' : ''}`;"
        self.assertEqual([s for _, s in js_strings(src) if CJK.search(s)], ["'可见'", "`${x ? '甲' : ''}`"])

    def test_only_allowed_chinese_literals_remain(self):
        bad = []
        for f in scoped_js():
            for line, s in js_strings(f.read_text(encoding="utf-8")):
                if CJK.search(s) and s not in ALLOWED:
                    bad.append(f"{f.relative_to(UI)}:{line}: {s}")
        self.assertEqual(bad, [])


NODE_SCRIPT = r"""
import fs from 'node:fs';
const [ui] = process.argv.slice(2);
const uiUrl = new URL(`file://${ui}/`);
globalThis.document = { addEventListener() {}, documentElement: { dataset: {}, lang: '' }, querySelectorAll: () => [] };
globalThis.window = { location: { search: '' }, dispatchEvent() {} };
globalThis.CustomEvent = class { constructor(type, o) { this.type = type; this.detail = o?.detail; } };
let mode = '';
globalThis.fetch = async (url) => {
  const s = String(url);
  if (s.startsWith('file:') && s.endsWith('.json')) {          // 字典：基础 + A02 part（模拟早上合并）
    const lang = s.endsWith('/en.json') ? 'en' : 'zh';
    const base = JSON.parse(fs.readFileSync(new URL(s), 'utf8'));
    const part = JSON.parse(fs.readFileSync(`${ui}/i18n/parts/A02.${lang}.json`, 'utf8'));
    return { ok: true, json: async () => ({ ...base, ...part }) };
  }
  if (mode === 'abort') { const e = new Error('aborted'); e.name = 'AbortError'; throw e; }
  if (mode === 'down') throw new TypeError('fetch failed');
  return { ok: true, status: 200, json: async () => { throw new SyntaxError('not json'); } };
};
const i18n = await import(new URL('shared/i18n.js', uiUrl));
const api = await import(new URL('shared/api.js', uiUrl));
const dbg = await import(new URL('shared/debug-fields.js', uiUrl));
const out = {};
for (const lang of ['zh', 'en']) {
  await i18n.setLang(lang);
  for (const m of ['abort', 'down', 'html']) {
    mode = m;
    try { await api.getJSON('/api/x'); out[`${lang}.${m}`] = 'no error'; } catch (e) { out[`${lang}.${m}`] = `${e.name}:${e.status}:${e.message}`; }
  }
  out[`${lang}.name_code`] = i18n.t('a02.fmt.name_code', { name: 'CSI300 ETF', code: '510300' });
  out[`${lang}.pnl`] = i18n.t('a02.fmt.pnl_pct', { pnl: '1,234.50', pct: '3.20' });
  out[`${lang}.list`] = ['a', 'b'].join(i18n.t('a02.sep.list'));
  out[`${lang}.notobj`] = dbg.check(['x'], null).missing[0];
}
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(shutil.which("node"), "需要 node")
class NodeRuntimeTests(unittest.TestCase):
    def test_messages_follow_language_after_merge(self):
        with tempfile.TemporaryDirectory() as d:
            script = Path(d) / "probe.mjs"
            script.write_text(NODE_SCRIPT, encoding="utf-8")
            r = subprocess.run(["node", "--experimental-default-type=module", str(script), str(UI)],
                               capture_output=True, text=True, timeout=30, cwd=d)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual(out["zh.abort"], "ApiError:0:请求超时")
        self.assertEqual(out["zh.down"], "ApiError:0:连不上网页服务")
        self.assertEqual(out["zh.html"], "ApiError:200:返回内容不是 JSON")
        self.assertEqual(out["en.abort"], "ApiError:0:Request timed out")
        self.assertEqual(out["en.down"], "ApiError:0:Can't reach the web server")
        self.assertEqual(out["en.html"], "ApiError:200:The response is not JSON")
        self.assertEqual(out["zh.name_code"], "CSI300 ETF（510300）")
        self.assertEqual(out["en.name_code"], "CSI300 ETF (510300)")
        self.assertEqual(out["zh.pnl"], "1,234.50（3.20%）")
        self.assertEqual(out["en.pnl"], "1,234.50 (3.20%)")
        self.assertEqual((out["zh.list"], out["en.list"]), ("a、b", "a, b"))
        self.assertEqual((out["zh.notobj"], out["en.notobj"]), ("(不是对象)", "(not an object)"))


if __name__ == "__main__":
    unittest.main()
