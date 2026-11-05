"""G01：自用站易用性走查后的前端改动检查。

只读 dashboard/ui 下的源文件和字典，不启动服务、不读 data/；node 探针（tests/g01_probe.mjs）用假浏览器全局对象真的导入模块。
1. 词条：parts/G01.zh.json 和 G01.en.json 键完全一致、占位符一致、非空；js 里用到的 g01.* 都有词条，词条也都被用到
2. 兜底文字：shared/t-fallback.js 在 A02 词条未合并时给出的文字，和 parts/A02.*.json 逐条相同；字典里有词条时优先用字典；
   回测页下拉在没有 A02 词条时退成「名称 代码」，不露键名
3. 个股链接：pages/ 和 shared/ 的代码里不再有旧界面 /#/stock/ 链接；新链接带市场、预览模式带 mock=1、代码被编码
4. 今日页动作格子 → 信号页标签：用的标签名都在信号页个人模式的标签白名单里
5. 安全 / 规模：改动的文件不用 innerHTML 等、不出现外部网址，单文件 ≤ 500 行
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
PROBE = ROOT / "tests" / "g01_probe.mjs"
CHANGED = [
    UI / "shared" / "stock-link.js", UI / "shared" / "t-fallback.js", UI / "shared" / "page-boot.js",
    UI / "pages" / "U1-common.js", UI / "pages" / "today.js", UI / "pages" / "today-personal.js",
    UI / "pages" / "signals.js", UI / "pages" / "signals-table.js", UI / "pages" / "signals-link.js",
    UI / "pages" / "backtest.js", UI / "pages" / "backtest-result.js", UI / "pages" / "stock.js",
    UI / "pages" / "watch.js", UI / "pages" / "holdings.js", UI / "pages" / "holdings-form.js",
    UI / "pages" / "market.js", UI / "pages" / "research.js", UI / "pages" / "u4-util.js",
    UI / "pages" / "U1.css", UI / "pages" / "U2.css",
]
FORBIDDEN = ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write")


def strip_comments(src):
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(?<!:)//[^\n]*", "", src)


def load(name):
    """「G01.zh.json」这种部件文件名 → 该任务的词条（键来自快照，值来自基础字典）"""
    part, lang, _ = name.split(".")
    return parts_compat.part(part, lang)


def placeholders(s):
    return sorted(re.findall(r"\{(\w+)\}", s))


class I18nTests(unittest.TestCase):
    def test_keys_match_between_languages(self):
        zh, en = load("G01.zh.json"), load("G01.en.json")
        self.assertEqual(set(zh), set(en))
        self.assertTrue(zh)
        for k in zh:
            self.assertTrue(k.startswith("g01."), k)
            self.assertTrue(zh[k].strip() and en[k].strip(), k)
            self.assertEqual(placeholders(zh[k]), placeholders(en[k]), k)

    def test_used_keys_exist_and_all_keys_used(self):
        zh = load("G01.zh.json")
        used = set()
        for f in list((UI / "pages").glob("*.js")) + list((UI / "shared").glob("*.js")):
            used |= set(re.findall(r"'(g01\.[a-z_.]+)'", f.read_text(encoding="utf-8")))
        self.assertTrue(used)
        self.assertEqual(used - set(zh), set(), "js 里用了但词条里没有")
        self.assertEqual(set(zh) - used, set(), "词条里有但没被用到")

    def test_g01_keys_merged_into_base(self):
        """部件已合并并退役：原来要求 G01 词条只在 parts/ 里，现在要求它们都在基础字典里"""
        for lang in ("zh", "en"):
            self.assertEqual(parts_compat.missing_in_base("G01", lang), [], lang)

    def test_new_text_hidden_until_merged(self):
        """新词条没合并进基础字典前，代码一律经 tMaybe / 已有词条兜底，不直接 t('g01.…')（否则会露键名）"""
        for f in CHANGED:
            if f.suffix != ".js":
                continue
            code = strip_comments(f.read_text(encoding="utf-8"))
            self.assertNotRegex(code, r"(?<![A-Za-z])t\('g01\.", f.name)

    def test_backtest_dropdown_never_shows_key(self):
        """backtest.js 只能用 i18n / dom / api 三个共享模块（A09 的探针只拷这几个），所以兜底写在页面里：缺词条时退成「名称 代码」"""
        src = (UI / "pages" / "backtest.js").read_text(encoding="utf-8")
        self.assertIn("if (s !== 'a02.fmt.name_code') return s;", src)
        self.assertIn("nameCode(v || k, k)", src)
        imports = set(re.findall(r"from '\.\./shared/([\w-]+\.js)'", src))
        self.assertLessEqual(imports, {"dom.js", "i18n.js", "api.js", "url-state.js"})   # url-state.js：ux D06（回测参数写进网址）


@unittest.skipUnless(shutil.which("node"), "没有 node，跳过探针")
class ProbeTests(unittest.TestCase):
    @classmethod
    def run_probe(cls, mode):
        res = subprocess.run(["node", "--experimental-default-type=module", str(PROBE), str(UI), mode],
                             capture_output=True, text=True, timeout=60, cwd=ROOT)
        if res.returncode != 0:
            raise AssertionError(res.stderr)
        return json.loads(res.stdout.strip().splitlines()[-1])

    @classmethod
    def setUpClass(cls):
        cls.base = cls.run_probe("base")
        cls.sentinel = cls.run_probe("sentinel")

    def test_fallback_equals_a02_parts(self):
        for lang in ("zh", "en"):
            a02 = load(f"A02.{lang}.json")
            got = self.base["fallback"][lang]
            for k, v in got.items():
                self.assertEqual(v, a02[k], f"{lang} {k}")
            self.assertFalse([v for v in got.values() if v.startswith("a02.")])
        self.assertEqual(self.base["fallback"]["zh_vars"], "示例银行（600000）")
        self.assertEqual(self.base["fallback"]["en_vars"], "示例银行 (600000)")

    def test_dictionary_wins_and_unknown_keys_pass_through(self):
        self.assertEqual(self.sentinel["fallback"]["zh"]["a02.sep.list"], "§")
        self.assertEqual(self.sentinel["fallback"]["zh"]["a02.sep.semi"], "；")
        for lang in ("zh", "en"):
            self.assertEqual(self.base["fallback"][f"{lang}_unknown"], "zz.not.a.key")
        self.assertEqual(self.base["fallback"]["zh_existing"], "运行回测")
        self.assertEqual(self.base["fallback"]["en_existing"], "Run backtest")

    def test_stock_links(self):
        links = self.base["links"]
        self.assertEqual(links["mock"], "stock.html?code=600000&market=cn&mock=1")
        self.assertEqual(links["mockNoMarket"], "stock.html?code=600000&mock=1")
        self.assertEqual(links["u1Default"], "stock.html?code=600222&market=cn&mock=1")
        self.assertEqual(links["u1Us"], "stock.html?code=SPY&market=us&mock=1")
        self.assertEqual(links["live"], "stock.html?code=600000&market=cn")
        # 代码里的 & # / 被编码，不能拼出额外参数或跳到别处
        self.assertEqual(links["weird"], "stock.html?code=x%26mock%3D0%23y%2F..%2F&market=cn&mock=1")
        for v in links.values():
            self.assertNotIn("//", v)
            self.assertFalse(v.startswith("/"), v)

    def test_page_href_backward_compatible(self):
        links = self.base["links"]
        self.assertEqual(links["pagePlainMock"], "signals.html?mock=1")
        self.assertEqual(links["livePagePlain"], "signals.html")
        self.assertEqual(links["pageMock"], "signals.html?sg_t=blend2&sg_k=buy&mock=1")
        self.assertEqual(links["livePage"], "signals.html?sg_t=blend2&sg_k=buy")

    def test_remember_strategy_writes_both_keys(self):
        st = self.base["store"]
        self.assertEqual(st["newKey"], "rsi2")
        self.assertEqual(st["oldKey"], "rsi2")
        self.assertEqual(st["afterEmpty"], "rsi2", "空策略不应覆盖已有偏好")


class StaticTests(unittest.TestCase):
    def test_no_classic_stock_links_left(self):
        for f in list((UI / "pages").glob("*.js")) + list((UI / "shared").glob("*.js")):
            code = strip_comments(f.read_text(encoding="utf-8"))
            self.assertNotIn("#/stock/", code, f.name)

    def test_today_tiles_use_signal_tabs(self):
        tp = (UI / "pages" / "today-personal.js").read_text(encoding="utf-8")
        tabs = set(re.findall(r"[卖减买加][出仓入]: '(\w+)'", tp))
        self.assertEqual(tabs, {"sell", "reduce", "buy", "add"})
        sig = (UI / "pages" / "signals.js").read_text(encoding="utf-8")
        personal = re.search(r"S\.personal \? \[([^\]]+)\]", sig).group(1)
        self.assertEqual(set(re.findall(r"'(\w+)'", personal)), tabs)
        self.assertIn("sg_k: TAB[a]", tp)
        self.assertIn("sg_t: main.main", tp)

    def test_safe_and_small(self):
        for f in CHANGED:
            src = f.read_text(encoding="utf-8")
            self.assertLessEqual(len(src.splitlines()), 500, f.name)
            if f.suffix == ".js":
                code = strip_comments(src)
                for bad in FORBIDDEN:
                    self.assertNotIn(bad, code, f"{f.name}: {bad}")
            self.assertIsNone(re.search(r"https?://(?!127\.0\.0\.1|localhost|www\.w3\.org)", strip_comments(src)), f.name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
