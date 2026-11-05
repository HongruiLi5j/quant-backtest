"""A07：今日页「数据日期」醒目显示 + 清单过期提示条的检查。

只读 dashboard/ui 下的源文件，不启动服务、不读 data/；node 运行时检查在临时目录里写脚本。
1. A07 两份字典：合法 JSON、键完全一致、占位符一致、英文里没有中文、不和基础字典或其他 parts 撞键；代码用到的键都有、词条都被用到
2. 静态：today-date.js 有日期显示逻辑（Asia/Shanghai、15:30、el 建节点），today.js / today-personal.js 真的接上了；
   三个文件都不用 innerHTML / insertAdjacentHTML / document.write；today-date.js 里没有中文字符串字面量（文字全走字典）
3. （有 node 时）固定时刻下的判断：周中盘前 / 盘后 / 周末 / 当天盘中 / 跨时区 / 15:30 边界 / 未来日期 / 无效日期；
   合并字典后中英文各自正确，合并前提示条不露键名；非 A 股不出提示条
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
PAGES = UI / "pages"
ZH = parts_compat.part("A07", "zh")
EN = parts_compat.part("A07", "en")
CJK = re.compile(r"[㐀-鿿　-〿＀-￯]")
PLACEHOLDER = re.compile(r"\{(\w+)\}")
FILES = [PAGES / "today-date.js", PAGES / "today.js", PAGES / "today-personal.js"]
FORBIDDEN = ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write")


def strip_comments(src):
    """去掉 /* */ 和 // 注释（这三个文件的字符串里没有 //）"""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"//[^\n]*", "", src)


class A07DictTests(unittest.TestCase):
    def test_keys_identical_and_prefixed(self):
        self.assertEqual(set(ZH), set(EN))
        self.assertTrue(ZH)
        for k in ZH:
            self.assertTrue(k.startswith("a07."), k)

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
            self.assertEqual(parts_compat.missing_in_base("A07", lang), [], lang)

    def test_used_keys_exist_and_all_keys_used(self):
        used = set()
        for f in FILES:
            used |= set(re.findall(r"""['"](a07\.[a-z_.0-9]+)['"]""", f.read_text(encoding="utf-8")))
        self.assertEqual(used - set(ZH), set(), "代码里用了字典里没有的键")
        self.assertEqual(set(ZH) - used, set(), "字典里有没被用到的键")

    def test_date_line_has_required_format(self):
        self.assertEqual(ZH["a07.date.line"], "数据日期：{d}（北京时间 15:30 收盘后更新）")


class A07StaticTests(unittest.TestCase):
    def test_date_logic_present(self):
        src = (PAGES / "today-date.js").read_text(encoding="utf-8")
        self.assertIn("Asia/Shanghai", src)                   # 今天按北京时间算，不是电脑本地时区
        self.assertRegex(src, r"15\s*\*\s*60\s*\+\s*30")      # 15:30 分界
        self.assertIn("export function dateNotice", src)
        self.assertIn("export function bannerLines", src)
        self.assertIn("role: 'status'", src)
        self.assertIn("from '../shared/dom.js'", src)

    def test_uses_el_not_html_strings(self):
        for f in FILES:
            src = f.read_text(encoding="utf-8")
            for bad in FORBIDDEN:
                self.assertNotIn(bad, src, f"{f.name} 用了 {bad}")
        # 日期文字经 el() 建文本节点（el 内部是 document.createTextNode / setAttribute，见 shared/dom.js）
        dom = (UI / "shared" / "dom.js").read_text(encoding="utf-8")
        self.assertIn("document.createTextNode", dom)
        self.assertNotIn("innerHTML", dom)
        self.assertGreaterEqual((PAGES / "today-date.js").read_text(encoding="utf-8").count("el('"), 3)

    def test_wired_into_pages(self):
        today = (PAGES / "today.js").read_text(encoding="utf-8")
        self.assertIn("from './today-date.js'", today)
        self.assertRegex(today, r"dateNotice\(s\.data_date")
        personal = (PAGES / "today-personal.js").read_text(encoding="utf-8")
        self.assertIn("from './today-date.js'", personal)
        self.assertRegex(personal, r"dateLine\(r\.data_date")

    def test_no_chinese_string_literals(self):
        code = strip_comments((PAGES / "today-date.js").read_text(encoding="utf-8"))
        self.assertIsNone(CJK.search(code), "today-date.js 的文字必须走字典")

    def test_css_classes_exist(self):
        css = (PAGES / "U1.css").read_text(encoding="utf-8")
        for cls in (".u1-datebar", ".u1-datebanner", ".u1-datewrap"):
            self.assertIn(cls, css)


NODE_SCRIPT = r"""
const PART_KEYS = __PART_KEYS__;   // 部件已退役：键名单由 Python 从 tests/fixtures/i18n_parts_keys.json 填进来
const without = (d, keys) => Object.fromEntries(Object.entries(d).filter(([k]) => !keys.includes(k)));   // 合并前 = 基础字典去掉本任务的键
import fs from 'node:fs';
const [ui, merge] = process.argv.slice(2);
const uiUrl = new URL(`file://${ui}/`);
// 极简假 DOM：只够 el() 建节点并读出文字
class FNode {}
class FText extends FNode { constructor(s) { super(); this.s = s; } text() { return this.s; } }
class FEl extends FNode {
  constructor(tag) { super(); this.tag = tag; this.kids = []; this.attrs = {}; this.className = ''; }
  setAttribute(k, v) { this.attrs[k] = v; }
  append(...c) { this.kids.push(...c); }
  text() { return this.kids.map((k) => k.text()).join('|'); }
}
globalThis.Node = FNode;
globalThis.document = { createElement: (t) => new FEl(t), createTextNode: (s) => new FText(s),
  addEventListener() {}, documentElement: { dataset: {}, lang: '' }, querySelectorAll: () => [] };
globalThis.window = { location: { search: '' }, dispatchEvent() {} };
globalThis.CustomEvent = class { constructor(type, o) { this.type = type; this.detail = o?.detail; } };
globalThis.fetch = async (url) => {                     // 字典：基础字典；merge=1 时再叠上 A07 part（模拟早上合并）
  const s = String(url);
  const lang = s.endsWith('/en.json') ? 'en' : 'zh';
  const base = JSON.parse(fs.readFileSync(new URL(s), 'utf8'));
  return { ok: true, json: async () => (merge === '1' ? base : without(base, PART_KEYS)) };
};
const i18n = await import(new URL('shared/i18n.js', uiUrl));
const td = await import(new URL('pages/today-date.js', uiUrl));

// 固定时刻（UTC）。北京时间 = UTC + 8
const at = (iso) => new Date(iso);
const NOW = {
  thu_am: at('2026-10-29T02:00:00Z'),   // 周四 10:00
  fri_am: at('2026-10-30T02:00:00Z'),   // 周五 10:00（15:30 之前）
  fri_1529: at('2026-10-30T07:29:00Z'), // 周五 15:29
  fri_1530: at('2026-10-30T07:30:00Z'), // 周五 15:30
  fri_pm: at('2026-10-30T08:00:00Z'),   // 周五 16:00
  sat_am: at('2026-10-31T02:00:00Z'),   // 周六 10:00
  cross: at('2026-10-29T17:00:00Z'),    // UTC 还是 10-29，北京时间已是周五 10-30 01:00
};
const keys = (d, n) => td.bannerLines(d, td.shanghaiClock(NOW[n])).map((l) => l.key);
const out = {
  clock_cross: td.shanghaiClock(NOW.cross),
  clock_fri_1530: td.shanghaiClock(NOW.fri_1530),
  clock_sat: td.shanghaiClock(NOW.sat_am),
  fri_am_prev: keys('2026-10-29', 'fri_am'),
  fri_pm_prev: keys('2026-10-29', 'fri_pm'),
  fri_pm_same: keys('2026-10-30', 'fri_pm'),
  fri_am_same: keys('2026-10-30', 'fri_am'),
  fri_1529_same: keys('2026-10-30', 'fri_1529'),
  fri_1530_same: keys('2026-10-30', 'fri_1530'),
  sat_prev: keys('2026-10-30', 'sat_am'),
  cross_prev: keys('2026-10-29', 'cross'),
  future: keys('2026-11-02', 'fri_am'),
  bad_feb30: keys('2026-02-30', 'fri_am'),
  bad_null: keys(null, 'fri_am'),
  bad_empty: keys('', 'fri_am'),
  bad_fmt: keys('2026/10/29', 'fri_am'),
};
const texts = (node) => (node ? node.kids.map((k) => k.text()) : null);
for (const lang of ['zh', 'en']) {
  await i18n.setLang(lang);
  out[`${lang}.line_cn`] = td.dateLine('2026-10-29', { market: 'cn' }).text();
  out[`${lang}.line_us`] = td.dateLine('2026-10-29', { market: 'us', tz: 'TZ' }).text();
  out[`${lang}.line_bad`] = td.dateLine(undefined, { market: 'cn' }).text();
  out[`${lang}.banner_stale`] = texts(td.dateBanner('2026-10-29', { market: 'cn', now: NOW.fri_am }))?.map((p) => p) ?? null;
  out[`${lang}.banner_none`] = td.dateBanner('2026-10-30', { market: 'cn', now: NOW.fri_pm });
  out[`${lang}.banner_us`] = td.dateBanner('2020-01-01', { market: 'us', now: NOW.fri_am });
  const notice = td.dateNotice('2026-10-29', { market: 'cn', now: NOW.fri_am });
  out[`${lang}.notice_kids`] = notice.kids.map((k) => k && k.className);
  out[`${lang}.notice_none_kids`] = td.dateNotice('2026-10-30', { market: 'cn', now: NOW.fri_pm }).kids.length;
  out[`${lang}.banner_after_close_cls`] = td.dateBanner('2026-10-29', { market: 'cn', now: NOW.fri_pm })?.className ?? null;   // D90-5
  out[`${lang}.banner_role`] = td.dateBanner('2026-10-29', { market: 'cn', now: NOW.fri_am })?.attrs.role ?? null;
}
console.log(JSON.stringify(out));
"""
NODE_SCRIPT = NODE_SCRIPT.replace("__PART_KEYS__", json.dumps(parts_compat.part_keys("A07")))


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
    @classmethod
    def setUpClass(cls):
        cls.merged = run_node("1")
        cls.unmerged = run_node("0")

    def test_beijing_clock_not_utc(self):
        o = self.merged
        self.assertEqual(o["clock_cross"], {"date": "2026-10-30", "minutes": 60, "weekend": False})
        self.assertEqual(o["clock_fri_1530"], {"date": "2026-10-30", "minutes": 930, "weekend": False})
        self.assertTrue(o["clock_sat"]["weekend"])

    def test_banner_branches(self):
        o = self.merged
        stale = "a07.banner.stale"
        self.assertEqual(o["fri_am_prev"], [stale, "a07.banner.before_close"])
        self.assertEqual(o["fri_pm_prev"], [stale, "a07.banner.after_close"])
        self.assertEqual(o["cross_prev"], [stale, "a07.banner.before_close"])
        self.assertEqual(o["sat_prev"], [stale, "a07.banner.weekend"])
        self.assertEqual(o["fri_pm_same"], [])
        self.assertEqual(o["fri_am_same"], ["a07.banner.intraday"])
        self.assertEqual(o["fri_1529_same"], ["a07.banner.intraday"])
        self.assertEqual(o["fri_1530_same"], [])              # 15:30 整点起算收盘后
        self.assertEqual(o["future"], ["a07.banner.future"])
        for k in ("bad_feb30", "bad_null", "bad_empty", "bad_fmt"):
            self.assertEqual(o[k], ["a07.banner.unknown"], k)

    def test_after_merge_zh(self):
        o = self.merged
        self.assertEqual(o["zh.line_cn"], "数据日期：2026-10-29（北京时间 15:30 收盘后更新）")
        self.assertEqual(o["zh.line_us"], "数据日期：2026-10-29（TZ）")
        self.assertEqual(o["zh.line_bad"], ZH["a07.date.unknown"])
        self.assertEqual(o["zh.banner_stale"][0], "这是 2026-10-29 收盘后的清单，不是盘中信号。")
        self.assertIn("今天（2026-10-30）的 K 线还没收盘", o["zh.banner_stale"][1])
        self.assertEqual(o["zh.banner_role"], "status")

    def test_after_merge_en(self):
        o = self.merged
        self.assertEqual(o["en.line_cn"], "Data date: 2026-10-29 (updates after the 15:30 Beijing-time close)")
        self.assertIn("not an intraday signal", o["en.banner_stale"][0])
        for k in ("en.line_cn", "en.line_us", "en.line_bad"):
            self.assertIsNone(CJK.search(o[k]), k)
        for s in o["en.banner_stale"]:
            self.assertIsNone(CJK.search(s), s)

    def test_no_banner_when_fresh_or_not_cn(self):
        for lang in ("zh", "en"):
            self.assertIsNone(self.merged[f"{lang}.banner_none"])
            self.assertIsNone(self.merged[f"{lang}.banner_us"])
            self.assertEqual(self.merged[f"{lang}.notice_none_kids"], 1)    # 只有日期行，没有提示条
            # D90-5：周五上午看到周四的清单是正常情况，提示条用中性样式（不带 is-warn）；收盘后还没有今天的数据才是琥珀色
            self.assertEqual(self.merged[f"{lang}.notice_kids"], ["u1-datebar num", "card u1-note u1-datebanner"])
            self.assertEqual(self.merged[f"{lang}.banner_after_close_cls"], "card u1-note is-warn u1-datebanner")

    def test_before_merge_never_shows_keys(self):
        o = self.unmerged
        for lang in ("zh", "en"):
            self.assertEqual(o[f"{lang}.line_cn"], "2026-10-29")      # 词条没合并：只显示日期本身
            self.assertEqual(o[f"{lang}.line_us"], "2026-10-29")
            self.assertEqual(o[f"{lang}.line_bad"], "")
            self.assertEqual(o[f"{lang}.banner_stale"], None)          # 提示条整条丢掉，不露 a07.xxx
            self.assertEqual(o[f"{lang}.notice_none_kids"], 1)


if __name__ == "__main__":
    unittest.main()
