"""A09：回测实验室参数输入校验的检查。

只读 dashboard/ui 和 src 下的源文件，不启动服务、不读 data/；node 运行时检查在临时目录里写脚本、拷文件。
1. A09 两份字典：合法 JSON、键完全一致、占位符一致、英文里没有中文、不和基础字典或其他 parts 撞键；代码用到的键都有、词条都被用到
2. 静态：校验模块没有中文字面量（文字全走字典）、不用 innerHTML 一类；回测页不用 alert / confirm / prompt；
   前端常量和后端（app.py 成本夹取、datasource.STOCK_RE）一致；backtest.js 真的接上了校验并禁用按钮
3. 前端 ⇔ 后端一致：对 registry 里每个策略的每个参数、成本，枚举边界附近的值，
   「前端报错」当且仅当「后端 clean_params / 成本夹取会改写这个值」；
   开始日期的边界用合成价格实测后端：晚于最新数据日会崩（IndexError），等于最新数据日只剩 1 天
4. （有 node 时）假 DOM 真的渲染 backtest.js：敲键盘 / 选下拉 / 点按钮，读提示文字、按钮禁用状态、发给接口的参数
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "dashboard" / "ui"
PARTS = UI / "i18n" / "parts"
PAGES = UI / "pages"
TESTS = ROOT / "tests"
sys.path.insert(0, str(ROOT))

ZH = json.loads((PARTS / "A09.zh.json").read_text(encoding="utf-8"))
EN = json.loads((PARTS / "A09.en.json").read_text(encoding="utf-8"))
CJK = re.compile(r"[㐀-鿿　-〿＀-￯]")
PLACEHOLDER = re.compile(r"\{(\w+)\}")
VALIDATE = PAGES / "backtest-validate.js"
BACKTEST = PAGES / "backtest.js"
FORBIDDEN = ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write")


def strip_comments(src):
    """去掉 /* */ 和 // 注释（这两个文件的字符串里没有 //）"""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(?<![:'\"])//[^\n]*", "", src)


class A09DictTests(unittest.TestCase):
    def test_keys_identical_and_prefixed(self):
        self.assertEqual(set(ZH), set(EN))
        self.assertTrue(ZH)
        for k in ZH:
            self.assertTrue(k.startswith("a09."), k)

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
                if not p.name.startswith("A09."):
                    self.assertFalse(set(ZH) & set(json.loads(p.read_text(encoding="utf-8"))), p.name)

    def test_used_keys_exist_and_all_keys_used(self):
        used = set()
        for f in (VALIDATE, BACKTEST):
            used |= set(re.findall(r"""['"](a09\.[a-z_.0-9]+)['"]""", f.read_text(encoding="utf-8")))
        self.assertEqual(used - set(ZH), set(), "代码里用了字典里没有的键")
        self.assertEqual(set(ZH) - used, set(), "字典里有没被用到的键")


class A09StaticTests(unittest.TestCase):
    def test_no_html_strings_or_dialogs(self):
        for f in (VALIDATE, BACKTEST):
            src = strip_comments(f.read_text(encoding="utf-8"))
            for bad in FORBIDDEN:
                self.assertNotIn(bad, src, f"{f.name} 用了 {bad}")
            self.assertIsNone(re.search(r"\b(alert|confirm|prompt)\s*\(", src), f"{f.name} 用了浏览器弹窗")

    def test_validate_module_has_no_chinese_literals_and_no_dom(self):
        code = strip_comments(VALIDATE.read_text(encoding="utf-8"))
        self.assertIsNone(CJK.search(code), "backtest-validate.js 的文字必须走字典")
        for dom in ("document", "window", "import "):
            self.assertNotIn(dom, code, "校验模块必须是不碰 DOM 的纯函数")

    def test_wired_into_backtest_page(self):
        src = BACKTEST.read_text(encoding="utf-8")
        self.assertIn("from './backtest-validate.js'", src)
        self.assertRegex(src, r"run\.disabled\s*=\s*busy\s*\|\|\s*v\.errors\s*>\s*0")      # 有错就禁用
        self.assertRegex(src, r"if \(busy \|\| ui\.sync\(\)\) return;")                    # 运行前再校验一次
        self.assertIn("aria-invalid", src)
        self.assertIn("aria-describedby", src)

    def test_constants_match_backend(self):
        js = VALIDATE.read_text(encoding="utf-8")
        app = (ROOT / "app.py").read_text(encoding="utf-8")
        m = re.search(r'cost = min\(max\(float\(q\["cost"\]\), ([\d.]+)\), ([\d.]+)\)', app)
        self.assertIsNotNone(m, "app.py 的成本夹取写法变了，前端 COST_MIN / COST_MAX 要重新对一遍")
        self.assertEqual((float(m.group(1)), float(m.group(2))),
                         (float(re.search(r"COST_MIN = ([\d.]+)", js).group(1)), float(re.search(r"COST_MAX = ([\d.]+)", js).group(1))))
        ds = (ROOT / "src" / "datasource.py").read_text(encoding="utf-8")
        py_re = re.search(r'STOCK_RE = .*?\.compile\(r"([^"]+)"\)', ds).group(1)
        js_re = re.search(r"CN_STOCK = /(.+?)/;", js).group(1)
        self.assertEqual(py_re, js_re)

    def test_css_classes_exist(self):
        css = (PAGES / "U2.css").read_text(encoding="utf-8")
        for cls in (".u2-field__msg", ".u2-field__msg.is-err", ".u2-field__msg.is-warn", ".is-invalid"):
            self.assertIn(cls, css)


def _spec_cases():
    """(策略, 参数规格, 值) 枚举：端点、端点外一步、中点、非整数中点"""
    from src import registry
    out = []
    for name, st in registry.STRATEGIES.items():
        for p in st["params"]:
            lo, hi, step = float(p["min"]), float(p["max"]), float(p["step"])
            vals = {lo, hi, lo - step, hi + step, lo - 1, hi + 1, (lo + hi) / 2, float(p["default"]), lo + 0.5, hi - 0.5, lo + step / 2, 0.0, -1.0}
            out.extend((name, p, v) for v in sorted(vals))
    return out


class A09BackendAgreementTests(unittest.TestCase):
    """前端校验的结论必须和后端“会不会改写这个值”一致（前端不比后端松，也不挡后端原样接受的值）"""

    def test_registry_has_params_to_check(self):
        self.assertGreater(len(_spec_cases()), 100)

    def test_param_cases_match_clean_params(self):
        from src import registry
        cases = _spec_cases()
        js_cases = [{"spec": p, "raw": repr(v)} for _, p, v in cases]
        verdict = run_node_json(NODE_PURE, js_cases)["params"]
        for (name, p, v), js_err in zip(cases, verdict):
            cleaned = registry.clean_params(name, {p["key"]: v})[p["key"]]
            backend_changes = float(cleaned) != float(v)
            self.assertEqual(js_err is not None, backend_changes, f"{name}.{p['key']}={v!r}: 前端 {js_err}，后端清洗后 {cleaned!r}")

    def test_cost_cases_match_backend_clamp(self):
        raws = ["-1", "0", "0.5", "5", "8", "99.99", "100", "100.01", "150", "1e2", "abc", "nan", "inf", "-0", ""]
        verdict = run_node_json(NODE_PURE, [{"spec": None, "raw": r} for r in raws])["costs"]
        for raw, js_err in zip(raws, verdict):
            if raw == "":
                self.assertIsNone(js_err, "后端把空成本当作用默认值，前端不能挡")
                continue
            try:
                v = float(raw)
            except ValueError:
                v = None
            if v is None or v != v:   # 后端 float() 失败 / NaN：后端会用默认值或让成本变 NaN，前端一律挡
                self.assertIsNotNone(js_err, raw)
                continue
            clamped = min(max(v, 0.0), 100.0)
            self.assertEqual(js_err is not None, clamped != v, f"cost={raw!r}: 前端 {js_err}")

    def test_start_date_boundary_on_synthetic_prices(self):
        """后端：开始日晚于最新数据日会崩；等于最新数据日只剩 1 天；比数据开头早则悄悄夹到预热期之后。前端据此要求「严格早于最新数据日」"""
        import numpy as np
        import pandas as pd
        from src import registry
        idx = pd.bdate_range("2018-01-01", periods=1200)

        def mk(seed):
            c = 100 * np.cumprod(1 + np.random.default_rng(seed).normal(0.0004, 0.01, len(idx)))
            return pd.DataFrame({"Open": c, "High": c * 1.01, "Low": c * 0.99, "Close": c, "Volume": 1e6}, index=idx)

        prices = {"AAA": mk(1), "CASH": mk(2)}
        mcfg = {"universe": {"AAA": "a"}, "cost_bp": 5, "cash": "CASH", "main": "AAA"}
        last = idx[-1]

        def go(start):
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                return registry.run("buy_hold", prices, "AAA", {}, mcfg, start=start, detail=True)

        self.assertEqual(len(go(str((last - pd.Timedelta(days=30)).date()))["dates"]) > 1, True)
        one = go(str(last.date()))
        self.assertEqual(one["start"], one["end"])                      # 等于最新数据日：只剩 1 个交易日
        with self.assertRaises(IndexError):
            go(str((last + pd.Timedelta(days=1)).date()))               # 晚于最新数据日：后端崩
        early = go("1900-01-01")
        self.assertGreater(early["start"], "2018-01-01")                # 太早：夹到预热期之后，不报错


NODE_PURE = r"""
import fs from 'node:fs';
const [validate, input] = process.argv.slice(2);
const v = await import(`file://${validate}`);
const cases = JSON.parse(fs.readFileSync(input, 'utf8'));
const out = { params: [], costs: [] };
for (const c of cases) {
  if (c.spec) out.params.push(v.checkParam(c.raw, c.spec, false));
  else out.costs.push(v.checkCost(c.raw, false));
}
console.log(JSON.stringify(out));
"""


def run_node_json(script, cases):
    with tempfile.TemporaryDirectory() as d:
        s, i = Path(d) / "probe.mjs", Path(d) / "cases.json"
        s.write_text(script, encoding="utf-8")
        i.write_text(json.dumps(cases), encoding="utf-8")
        r = subprocess.run(["node", "--experimental-default-type=module", str(s), str(VALIDATE), str(i)],
                           capture_output=True, text=True, timeout=60, cwd=d)
    if r.returncode != 0:
        raise AssertionError(r.stderr)
    return json.loads(r.stdout.strip().splitlines()[-1])


def build_probe_tree(dst):
    """拷出一份最小的 ui 目录：真实的 backtest.js / backtest-validate.js / shared / 字典，结果区两个模块换成空桩"""
    for rel in ("shared/dom.js", "shared/i18n.js", "shared/api.js", "i18n/zh.json", "i18n/en.json",
                "i18n/parts/A09.zh.json", "i18n/parts/A09.en.json", "pages/backtest.js", "pages/backtest-validate.js"):
        (dst / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(UI / rel, dst / rel)
    (dst / "pages" / "backtest-result.js").write_text(
        "export const resultNodes = () => document.createElement('div');\nexport const recentAndTrades = () => document.createElement('div');\n", encoding="utf-8")
    (dst / "pages" / "backtest-analysis.js").write_text("export const analysisNodes = () => [];\n", encoding="utf-8")


def probe_meta():
    """/api/meta 的形状，策略和参数范围取自真实 registry；两个市场各给最少的字段"""
    from src import registry
    m = {"name": "x", "universe": {"510300": "沪深300ETF"}, "stocks": {"600531": "豫光金铅"}, "timezone": "Asia/Shanghai", "timezone_label": "北京时间",
         "stock_cost_bp": 8, "cost_bp": 5, "source": "sina", "main": "510300"}
    return {"markets": {"cn": m, "us": {**m, "universe": {"SPY": "SPY"}, "stocks": {}, "main": "SPY"}}, "default_market": "cn",
            "strategies": {k: {kk: v[kk] for kk in ("label", "desc", "kind", "params")} for k, v in registry.STRATEGIES.items()}}


def run_probe(merge):
    with tempfile.TemporaryDirectory() as d:
        tree = Path(d) / "ui"
        build_probe_tree(tree)
        meta = Path(d) / "meta.json"
        meta.write_text(json.dumps(probe_meta()), encoding="utf-8")
        r = subprocess.run(["node", "--experimental-default-type=module", str(TESTS / "a09_probe.mjs"), str(tree), str(meta), merge],
                           capture_output=True, text=True, timeout=60, cwd=d)
    if r.returncode != 0:
        raise AssertionError(r.stderr)
    return json.loads(r.stdout.strip().splitlines()[-1])


@unittest.skipUnless(shutil.which("node"), "需要 node")
class A09PureFunctionTests(unittest.TestCase):
    def test_start_checks(self):
        script = r"""
        const v = await import(`file://${process.argv[2]}`);
        const k = (r) => ({ e: r.error?.key ?? null, w: r.warn?.key ?? null, h: r.hint?.key ?? null });
        const shift = (iso, n) => new Date(Date.parse(`${iso}T00:00:00Z`) - n * 86400000).toISOString().slice(0, 10);   // 往前推 n 天
        const END = '2026-10-23';
        console.log(JSON.stringify({
          blank: k(v.checkStart('', END)), blank_unknown: k(v.checkStart('', null)),
          late: k(v.checkStart('2026-10-24', END)), equal: k(v.checkStart(END, END)),
          day_before: k(v.checkStart('2026-10-22', END)), short_n: v.checkStart(shift(END, 30), END).warn.vars.n,
          edge_174: k(v.checkStart(shift(END, 174), END)), edge_175: k(v.checkStart(shift(END, 175), END)),
          ok: k(v.checkStart('2020-01-01', END)), unknown_end: k(v.checkStart('2030-01-01', null)),
          feb30: k(v.checkStart('2026-02-30', END)), bad_fmt: k(v.checkStart('2026/10/01', END)),
          half: k(v.checkStart('', END, true)), leap: k(v.checkStart('2024-02-29', END)),
        }));"""
        with tempfile.TemporaryDirectory() as d:
            s = Path(d) / "p.mjs"
            s.write_text(script, encoding="utf-8")
            r = subprocess.run(["node", "--experimental-default-type=module", str(s), str(VALIDATE)], capture_output=True, text=True, timeout=30, cwd=d)
        self.assertEqual(r.returncode, 0, r.stderr)
        o = json.loads(r.stdout.strip().splitlines()[-1])
        end = "a09.hint.end"
        self.assertEqual(o["blank"], {"e": None, "w": None, "h": end})
        self.assertEqual(o["blank_unknown"], {"e": None, "w": None, "h": None})            # 还不知道最新数据日：不比较
        self.assertEqual(o["late"]["e"], "a09.err.start_late")
        self.assertEqual(o["equal"]["e"], "a09.err.start_late")                            # 等于最新数据日也挡：只剩 1 天收益
        self.assertEqual(o["day_before"], {"e": None, "w": "a09.warn.short", "h": None})   # 只差 1 天：能跑但提醒样本太少
        self.assertEqual(o["short_n"], 30)                                                 # 提示里的天数就是实际相隔的自然日
        self.assertEqual(o["edge_174"]["w"], "a09.warn.short")                             # 174 天：提醒
        self.assertEqual(o["edge_175"], {"e": None, "w": None, "h": end})                  # 175 天：不提醒
        self.assertEqual(o["ok"], {"e": None, "w": None, "h": end})
        self.assertEqual(o["unknown_end"], {"e": None, "w": None, "h": None})
        for k in ("feb30", "bad_fmt", "half"):
            self.assertEqual(o[k]["e"], "a09.err.date_bad", k)
        self.assertIsNone(o["leap"]["e"])

    def test_symbol_checks(self):
        script = r"""
        const v = await import(`file://${process.argv[2]}`);
        const k = (r) => r?.key ?? null;
        console.log(JSON.stringify({
          empty: k(v.checkSymbol('', 'cn', true)), known_any: k(v.checkSymbol('510300', 'cn', true)),
          cn_ok: ['600519', '688981', '000001', '300750'].map((c) => k(v.checkSymbol(c, 'cn', false))),
          cn_bad: ['510300', '12345', '1234567', 'abc', '900001', '600519 x'].map((c) => k(v.checkSymbol(c, 'cn', false))),
          us_ok: ['AAPL', 'brk.b', 'BF-B', 'A', 'ABCDEFGHIJ'].map((c) => k(v.checkSymbol(c, 'us', false))),
          us_bad: ['', '1ABC', 'ABCDEFGHIJK', '../x', 'A B'].map((c) => k(v.checkSymbol(c, 'us', false))),
        }));"""
        with tempfile.TemporaryDirectory() as d:
            s = Path(d) / "p.mjs"
            s.write_text(script, encoding="utf-8")
            r = subprocess.run(["node", "--experimental-default-type=module", str(s), str(VALIDATE)], capture_output=True, text=True, timeout=30, cwd=d)
        self.assertEqual(r.returncode, 0, r.stderr)
        o = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual(o["empty"], "a09.err.symbol_required")
        self.assertIsNone(o["known_any"])
        self.assertEqual(o["cn_ok"], [None] * 4)
        self.assertEqual(o["cn_bad"], ["a09.err.symbol_cn"] * 6)
        self.assertEqual(o["us_ok"], [None] * 5)
        self.assertEqual(o["us_bad"], ["a09.err.symbol_required"] + ["a09.err.symbol_us"] * 4)


@unittest.skipUnless(shutil.which("node"), "需要 node")
class A09RenderedPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = run_probe("1")
        cls.u = run_probe("0")

    def test_initial_state_valid_and_runs_once(self):
        o = self.m["initial"]
        self.assertFalse(o["run_disabled"])
        self.assertEqual(o["calls"], 1)                       # 首次进入自动跑一次
        self.assertEqual(o["block"], "")
        self.assertEqual(o["fields"]["start"]["msg"], "回测算到最新数据日 2026-10-23，没有单独的结束日期")
        self.assertIn("is-hint", o["fields"]["start"]["cls"])
        for k in ("symbol", "start", "cost"):
            self.assertFalse(o["fields"][k]["invalid"], k)
            self.assertTrue(o["fields"][k]["describedby"], k)  # 每个输入框都用 aria-describedby 指向自己的提示位

    def test_cost_validation(self):
        c = self.m["cost"]
        for name in ("over", "neg", "text"):
            self.assertTrue(c[name]["disabled"], name)
            self.assertTrue(c[name]["invalid"], name)
        self.assertEqual(c["over"]["msg"], "要在 0 ~ 100 之间")
        self.assertEqual(c["text"]["msg"], "请输入数字")
        for name in ("blank", "dec", "top", "zero", "sci"):
            self.assertFalse(c[name]["disabled"], name)
            self.assertFalse(c[name]["invalid"], name)
        self.assertEqual(c["blank"]["msg"], "留空则用默认 5 bp")     # 空 = 后端用默认成本（510300 是 ETF：5bp）

    def test_blocked_run_sends_nothing_and_fix_runs(self):
        self.assertEqual(self.m["cost_blocked_calls"], 0)      # 点禁用按钮、绕过禁用直接触发 click，都不发请求
        self.assertEqual(self.m["cost_fixed"], {"calls": 1, "cost": "12", "disabled": False})

    def test_start_validation(self):
        s = self.m["start"]
        self.assertEqual(s["late"]["msg"], "要早于最新数据日 2026-10-23")
        self.assertTrue(s["late"]["disabled"])
        self.assertTrue(s["equal"]["disabled"])
        self.assertFalse(s["short"]["disabled"])                # 区间短只提醒不挡
        self.assertIn("is-warn", s["short"]["cls"])
        self.assertFalse(s["ok"]["disabled"])
        self.assertFalse(s["blank"]["disabled"])
        self.assertTrue(s["half"]["disabled"])                  # 半截日期（浏览器 badInput）
        self.assertEqual(s["half"]["msg"], "请选一个有效日期")
        self.assertTrue(s["feb30"]["disabled"])
        self.assertEqual(self.m["start_sent"], {"n": 1, "start": "2020-01-01"})

    def test_param_validation_after_strategy_switch(self):
        o = self.m
        self.assertEqual(o["params_after_switch"]["strategy"], "ma_adx_original")
        self.assertEqual(o["params_after_switch"]["keys"], ["fast", "slow", "adx_min"])
        self.assertFalse(o["params_after_switch"]["disabled"])
        f = o["fast"]
        self.assertEqual(f["blank"]["msg"], "必填，不能留空")
        self.assertEqual(f["text"]["msg"], "请输入数字")
        self.assertEqual(f["frac"]["msg"], "请填整数")
        self.assertEqual(f["low"]["msg"], "要在 2 ~ 50 之间")
        self.assertEqual(f["high"]["msg"], "要在 2 ~ 50 之间")
        for name in ("blank", "text", "frac", "low", "high"):
            self.assertTrue(f[name]["disabled"] and f[name]["invalid"], name)
        for name in ("min", "max", "ok"):
            self.assertFalse(f[name]["disabled"] or f[name]["invalid"], name)

    def test_cleared_param_is_not_sent_as_zero(self):
        self.assertEqual(self.m["enter_blocked_calls"], 0)
        self.assertEqual(self.m["fast_sent"]["n"], 1)
        self.assertEqual(self.m["fast_sent"]["params"]["fast"], 10)
        self.assertEqual(self.m["fast_sent"]["params"]["slow"], 20)

    def test_fast_ge_slow_warns_but_does_not_block(self):
        o = self.m["fast_ge_slow"]
        self.assertFalse(o["disabled"])
        self.assertIn("is-warn", o["slow_cls"])
        self.assertEqual(o["slow_msg"], "快线 30 不小于慢线 20，均线交叉没有意义")
        self.assertEqual(o["fast_msg"], "")

    def test_reset_clears_param_errors(self):
        o = self.m["after_reset"]
        self.assertFalse(o["disabled"])
        self.assertEqual(o["msg"], "")
        self.assertEqual(o["value"], "5")

    def test_symbol_from_url(self):
        b = self.m["bad_symbol"]
        self.assertTrue(b["run_disabled"])
        self.assertEqual(b["calls"], 0)                         # 格式不对的代码不发请求
        self.assertTrue(b["fields"]["symbol"]["invalid"])
        self.assertEqual(b["fields"]["symbol"]["msg"], "A 股代码要 6 位数字，以 60 / 68 / 00 / 30 开头")
        fixed = self.m["bad_symbol_fixed"]
        self.assertFalse(fixed["run_disabled"])
        self.assertEqual(fixed["calls"], 1)
        self.assertEqual(fixed["symbol"], "510300")
        self.assertFalse(self.m["ok_symbol"]["run_disabled"])
        self.assertEqual(self.m["ok_symbol"]["calls"], 1)
        self.assertTrue(self.m["us_symbol"]["run_disabled"])
        self.assertEqual(self.m["us_symbol"]["calls"], 0)

    def test_english(self):
        self.assertEqual(self.m["en"]["msg"], "Must be between 0 and 100")
        self.assertEqual(self.m["en"]["block"], "1 input(s) need fixing before you can run")
        self.assertTrue(self.m["en"]["disabled"])

    def test_unmerged_dictionary_never_shows_keys(self):
        # 词条还没合并进基础字典：按钮照样禁用、红框照样在，但文字位是空的，不露 a09.xxx
        c = self.u["cost"]
        self.assertTrue(c["over"]["disabled"] and c["over"]["invalid"])
        self.assertEqual(c["over"]["msg"], "")
        self.assertEqual(self.u["initial"]["block"], "")
        self.assertEqual(self.u["en"]["msg"], "")
        self.assertEqual(self.u["en"]["block"], "")
        self.assertTrue(self.u["en"]["disabled"])
        self.assertNotIn("a09.", json.dumps(self.u, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
