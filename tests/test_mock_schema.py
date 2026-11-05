"""A10：?mock=1 用的假数据（dashboard/ui/mock-data/*.json）和页面脚本 / 接口字段登记表是否一致。

只读 dashboard/ui 下的源文件和 mock 文件，不启动服务、不读 data/、不联网；分析代码在 tests/mock_schema_lib.py。
1. 工具自检：用临时目录里的小页面验证「缺字段会被发现、有保护的读取只提示、没数据分支和出错行不算」等规则
2. 页面读的字段 → mock 里都有：静态找出页面对 getJSON 返回值的属性访问（别名 / 回调参数 / 传给本地函数的参数 / 解构 / 状态对象），
   对每个 mock 文件逐项核对；没有保护（`?.`、`??`、`||`、`!x`、三元）而 mock 里缺的才算失败
3. 页面用到的 mock 文件都存在、每个 mock 文件都有页面在用
4. shared/debug-fields.js 里登记的接口字段清单（N04，来自后端实现）→ mock 里都有（需要 node 把登记表读出来）
已知的例外写在下面的 KNOWN_*，每条带原因；例外必须真的还存在（修好了要同步删掉，否则测试失败提醒）。
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
from mock_schema_lib import (Analyzer, Program, all_keys, check_reads, mask_js, nodes_at, pair_map, split_top)  # noqa: E402

UI = ROOT / "dashboard" / "ui"
PAGES = UI / "pages"
MOCKS = UI / "mock-data"

# 盯盘页的接口路径前缀是变量（${base}/quotes），静态看不出对应哪个 mock：这里写死两个市场的对应关系
UNRESOLVED = {
    "/quotes": [("market_monitor_quotes", "/api/market_monitor/quotes"), ("us_market_monitor_quotes", "/api/us_market_monitor/quotes")],
    "/alerts": [("market_monitor_alerts", "/api/market_monitor/alerts"), ("us_market_monitor_alerts", "/api/us_market_monitor/alerts")],
}

# 页面请求了、但 mock-data 里没有的文件。两处都有 try/catch，缺了只是预览时少一块，不会白屏
KNOWN_MISSING_MOCKS = {
    "ledger_summary": "ledger.js 请求 /api/ledger/summary，按命名规则应读 ledger_summary.json；现有 ledger.json 是 /api/ledger（信号页用）的样本，不是同一个接口。预览时账本页显示「未开启」",
    "search": "holdings-form.js 和命令面板请求 /api/search?...；读不到时都 catch 成空列表（股票搜索没有结果），不影响其他功能",
}

# 页面没有保护地读了、mock 里却没有的字段。(页面文件, mock 名, 读取所在路径, 字段名) → 原因
KNOWN_UNGUARDED = {
    ("monitor-config.js", "us_market_monitor_quotes", "status", "provider"):
        "美股盯盘接口的 status 本来就没有 provider（后端 us_market_monitor 的 status 里没这个键），mock 与后端一致；页面读到 undefined，不会崩",
    ("publish.js", "publish_latest", "commands", "mark"):
        "publish.js 在 `if (!c.deploy) return` 之后才读 c.mark：mock 表示「还没有可发布的构建」，commands 里本来就没有 deploy / mark。提前 return 的保护静态分析认不出",
}

# debug-fields.js 登记的字段在 mock 里没有的：mock 名 → 缺的路径。这些 mock 是页面够用的精简样本
KNOWN_SPEC_GAPS = {
    "research-tracking": {"replay.summary[].avg60", "replay.summary[].beat60", "replay.summary[].n60"},
    "u1-holdings": {"as_of", "capital", "closed", "closed[]", "positions[].first_buy", "positions[].market_value",
                    "positions[].price", "positions[].price_date", "positions[].weight", "summary", "trades", "trades[]"},
}

# 静态分析至少要读到字段的页面（防止分析器悄悄失效、什么都没读到却全绿）
MUST_READ_SOMETHING = ["today", "signals", "market", "watch", "stock", "backtest", "holdings", "monitor", "ledger", "research", "settings"]


def load_mocks():
    return {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in sorted(MOCKS.glob("*.json"))}


def analyze_all():
    """分析 w/*.html 对应的所有页面入口：返回 {页面: Analyzer}（reads() 已经跑过）。"""
    names = sorted(p.stem for p in MOCKS.glob("*.json"))
    fields = set()
    for data in load_mocks().values():
        fields |= all_keys(data)
    out = {}
    for html in sorted((UI / "w").glob("*.html")):
        entry = PAGES / f"{html.stem}.js"
        if entry.exists():
            an = Analyzer(Program.load(entry), names, UNRESOLVED, fields)
            an.page_reads = an.reads()
            out[html.stem] = an
    return out


# ---------------------------------------------------------------- 1. 工具自检

class JsHelperTests(unittest.TestCase):
    def test_mask_blanks_comments_and_strings_but_keeps_template_expressions(self):
        src = "const a = 'x.y'; // r.zzz\n/* r.w */ const b = `v ${r.val} t`;\n"
        code, raw = mask_js(src)
        self.assertEqual(len(code), len(src))
        self.assertNotIn("x.y", code)
        self.assertNotIn("r.zzz", code)
        self.assertNotIn("r.w", code)
        self.assertIn("r.val", code)                  # 模板字符串里的表达式是真代码
        self.assertIn("'x.y'", raw)                   # raw 保留字符串（读接口路径用）
        self.assertNotIn("r.zzz", raw)

    def test_regex_literal_with_quote_does_not_break_pairing(self):
        code, _ = mask_js("function f(s) { return /[\",\\n]/.test(s) ? `\"${s}\"` : s; }\n")
        self.assertEqual(code.count("{"), 2)          # 函数体 + 模板字符串里的 ${…}，正则和字符串里的符号都已抹掉
        self.assertEqual(len(pair_map(code, "{", "}")), 2)
        self.assertNotIn('",', code)

    def test_split_top_ignores_nested_commas(self):
        code = "a, f(b, c), [d, e], {g: 1, h: 2}, i"
        self.assertEqual(len(split_top(code, 0, len(code))), 5)

    def test_nodes_at_walks_arrays_and_maps(self):
        data = {"rows": [{"a": 1}, {"a": 2}], "m": {"x": {"k": 1}, "y": {"k": 2}}}
        self.assertEqual(len(nodes_at(data, ("rows", "[]"))), 2)
        self.assertEqual(nodes_at(data, ("m", "*")), [{"k": 1}, {"k": 2}])
        self.assertEqual(nodes_at(data, ("nope", "[]")), [])


PAGE_JS = """
import { getJSON, postJSON } from '../shared/api.js';
import { helper } from './lib.js';
const st = { snap: null };
const fetchThing = (q) => getJSON(`/api/thing?${q}`, { mock: 'thing_mock' });

export async function render(root) {
  const r = await getJSON('/api/alpha?market=cn');
  const [b, c] = await Promise.all([getJSON('/api/beta'), getJSON(`/api/gamma?x=${r.id}`, { mock: 'gamma-sample' })]);
  const t = await fetchThing('a=1');
  st.snap = await getJSON('/api/delta');
  if (r.error) { return r.message; }
  if (r.empty) { show(r.downloaded); return; }
  const title = r.title;
  const need = r.need_field;
  const maybe = r.maybe_field ?? 0;
  const tip = r.tip_field ? 1 : 0;
  r.rows.map((row, i) => row.code + row.nope_field);
  for (const x of r.rows) { use(x.name, x.optional_here?.deep); }
  const { aa, bb = 2 } = r.meta;
  helper(r.rows.filter((q) => q.code), b);
  show(b.items[0].label, c.sub.k, t.tt, st.snap.dd);
  render2(st.snap);
}

function render2(s) { return s.positions.map((p) => p.shares + p.missing_in_mock); }
function other() { const r = fakeOther(); return r.need_field_not_from_api; }
"""
LIB_JS = "export function helper(rows, beta) { return rows.map((x) => x.code2) + beta.lib_field; }\n"


class AnalyzerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        d = Path(cls.tmp.name)
        (d / "page.js").write_text(PAGE_JS, encoding="utf-8")
        (d / "lib.js").write_text(LIB_JS, encoding="utf-8")
        cls.mocks = {
            "alpha": {"id": 1, "title": "t", "rows": [{"code": "a", "name": "n"}, {"code": "b", "name": "n"}], "meta": {"aa": 1}},
            "beta": {"items": [{"label": "L"}]},
            "gamma-sample": {"sub": {"k": 1}},
            "thing_mock": {"tt": 1},
            "delta": {"positions": [{"shares": 1}], "dd": 1},
        }
        an = Analyzer(Program.load(d / "page.js"), list(cls.mocks), {})
        cls.reads = an.reads()
        cls.an = an
        cls.hard, cls.soft = check_reads(cls.reads, cls.mocks)
        cls.hard_keys = {(r.mock, r.path, r.key) for r in cls.hard}
        cls.soft_keys = {(r.mock, r.path, r.key) for r in cls.soft}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_roots_cover_await_promise_all_wrapper_and_state_property(self):
        got = {a.mock for a in self.an.aliases if a.path == ()}
        self.assertEqual(got, set(self.mocks))
        self.assertEqual(self.an.links["alpha"], {"/api/alpha"})
        self.assertEqual(self.an.links["gamma-sample"], {"/api/gamma"})      # { mock: '名字' } 指定了文件名

    def test_missing_unguarded_fields_are_hard_failures(self):
        self.assertEqual(self.hard_keys, {
            ("alpha", (), "need_field"),                                    # 直接读返回对象
            ("alpha", ("rows", "[]"), "nope_field"),                        # .map 回调的参数
            ("delta", ("positions", "[]"), "missing_in_mock"),              # st.snap 传给本地函数、再 .map
            ("beta", (), "lib_field"),                                      # 另一个文件里的函数形参（跨文件传参）
        })

    def test_guarded_reads_are_only_soft(self):
        self.assertEqual(self.soft_keys, {
            ("alpha", (), "maybe_field"),                                   # ?? 兜底
            ("alpha", (), "tip_field"),                                     # 三元条件
            ("alpha", ("rows", "[]"), "optional_here"),                     # ?. 可选链（for...of 的变量）
            ("alpha", ("meta",), "bb"),                                     # 解构里有默认值
        })

    def test_sentinel_branches_and_unrelated_variables_are_ignored(self):
        keys = {r.key for r in self.reads}
        self.assertNotIn("downloaded", keys)                                # if (r.empty) 分支里的字段
        self.assertNotIn("need_field_not_from_api", keys)                   # 另一个函数里同名变量 r，和接口无关（块作用域）
        self.assertNotIn("code2", {r.key for r in self.hard})               # 传给 lib.js 的数组元素字段，mock 里有

    def test_error_rows_are_skipped_and_empty_arrays_are_not_checked(self):
        mocks = {"m": {"rows": [{"a": 1}, {"error": "boom"}], "empty_rows": []}}
        from mock_schema_lib import Read
        reads = [Read(Path("x.js"), 1, "m", ("rows", "[]"), "a", False, "r.a"),
                 Read(Path("x.js"), 2, "m", ("rows", "[]"), "b", False, "r.b"),
                 Read(Path("x.js"), 3, "m", ("empty_rows", "[]"), "c", False, "r.c")]
        hard, _ = check_reads(reads, mocks)
        self.assertEqual([r.key for r in hard], ["b"])                      # 出错行不参与；空数组没法查


# ---------------------------------------------------------------- 2/3. 真实页面 vs 真实 mock

class RealMockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mocks = load_mocks()
        cls.analyzers = analyze_all()

    def test_every_mock_file_is_a_json_object_or_array(self):
        for name, data in self.mocks.items():
            self.assertIsInstance(data, (dict, list), f"{name}.json 顶层应是对象或数组")

    def test_page_scripts_parse_cleanly(self):
        """括号配对不上说明遮罩出了问题（字符串 / 正则 / 模板没处理好），后面的分析就不可信。"""
        for page, an in self.analyzers.items():
            for f in an.prog.files.values():
                self.assertTrue(f.balanced(), f"{f.path.name} 的括号配不上（页面 {page}）")

    def test_analyzer_still_reads_fields_from_main_pages(self):
        for page in MUST_READ_SOMETHING:
            self.assertGreater(len(self.analyzers[page].page_reads), 0, f"{page} 页没有分析出任何字段读取，分析器可能失效")

    def test_mock_files_requested_by_pages_exist(self):
        wanted = {}
        for page, an in self.analyzers.items():
            for mock in an.links:
                wanted.setdefault(mock, set()).add(page)
        missing = {m for m in wanted if m not in self.mocks}
        self.assertEqual(missing, set(KNOWN_MISSING_MOCKS),
                         f"页面请求但不存在的 mock：{sorted(missing)}；已知例外：{sorted(KNOWN_MISSING_MOCKS)}")

    def test_every_mock_file_is_used_by_some_page(self):
        used = {m for an in self.analyzers.values() for m in an.links}
        self.assertEqual(sorted(set(self.mocks) - used), [], "这些 mock 文件没有任何页面在读（改名或页面改了接口？）")

    def test_fields_read_by_pages_exist_in_mock(self):
        found = {}
        for page, an in self.analyzers.items():
            hard, _ = check_reads(an.page_reads, self.mocks)
            for r in hard:
                found[(r.file.name, r.mock, ".".join(r.path), r.key)] = f"{r.file.name}:{r.line} {r.expr}"
        unexpected = {k: v for k, v in found.items() if k not in KNOWN_UNGUARDED}
        # 键是元组，json.dumps 不能直接序列化（以前这里会抛 TypeError，把真正的失败原因盖住）
        self.assertEqual(unexpected, {}, "页面没有保护地读了这些字段，mock 里却没有：" + json.dumps({" | ".join(k): v for k, v in unexpected.items()}, ensure_ascii=False, indent=1))
        stale = [k for k in KNOWN_UNGUARDED if k not in found]
        self.assertEqual(stale, [], f"这些已知例外已经不存在了，请从 KNOWN_UNGUARDED 里删掉：{stale}")


# ---------------------------------------------------------------- 4. debug-fields.js 登记表

NODE_SCRIPT = r"""
const fs = require('fs'), vm = require('vm');
const s = fs.readFileSync(process.argv[1], 'utf8');
const a = s.indexOf('const MAX_ITEMS'), b = s.indexOf('/* ---------- 检查');
const ctx = {}; vm.createContext(ctx);
vm.runInContext(s.slice(a, b).replace('export const SPECS', 'var SPECS') + '\nthis.out = SPECS;', ctx);
process.stdout.write(JSON.stringify(ctx.out));
"""
MAX_ITEMS = 30


def spec_missing(spec, body):
    """和 debug-fields.js 的 check() 同一套规则：a.b 逐层；x[] 取数组前 30 项；* 取对象每个键；末尾 ? 是可选；empty / error 节点不再往下查。"""
    def skip(o):
        return o.get("empty") is True or isinstance(o.get("error"), str) or o.get("missing") is True

    def walk(node, segs, base, out):
        if not isinstance(node, (dict, list)) or not segs or (isinstance(node, dict) and skip(node)):
            return
        head, rest = segs[0], segs[1:]
        arr = head.endswith("[]")
        key = head[:-2] if arr else head
        keys = list(node) if key == "*" and isinstance(node, dict) else [key]
        for k in keys:
            path = f"{base}.{k}" if base else k
            path += "[]" if arr else ""
            if not (isinstance(node, dict) and k in node):
                out.add(path)
                continue
            child = node[k]
            if not rest:
                continue
            if arr:
                if isinstance(child, list):
                    for item in child[:MAX_ITEMS]:
                        walk(item, rest, path, out)
            else:
                walk(child, rest, path, out)

    missing = set()
    if isinstance(body, dict) and skip(body):
        return missing
    for raw in spec:
        if not raw.endswith("?"):
            walk(body, raw.split("."), "", missing)
    return missing


@unittest.skipUnless(shutil.which("node"), "需要 node 把 debug-fields.js 里的登记表读出来")
class SpecVsMockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        res = subprocess.run(["node", "-e", NODE_SCRIPT, str(UI / "shared" / "debug-fields.js")], capture_output=True, text=True, timeout=60)
        if res.returncode != 0:
            raise RuntimeError("node 读登记表失败：" + res.stderr[:300])
        cls.specs = json.loads(res.stdout)
        cls.mocks = load_mocks()
        cls.analyzers = analyze_all()

    def test_spec_port_matches_known_behaviour(self):
        body = {"a": [{"x": 1}, {}], "b": {"k": {"v": 1}, "m": {}}, "e": {"error": "x"}}
        self.assertEqual(spec_missing(["a[].x", "b.*.v", "e.deep", "nope", "opt?"], body), {"a[].x", "b.m.v", "nope"})     # * 展开成实际的键；error 节点不往下查；? 结尾的可选字段不查

    def test_registry_is_not_empty_and_covers_main_endpoints(self):
        self.assertGreaterEqual(len(self.specs), 15)
        for ep in ("/api/meta", "/api/summary", "/api/trade_list", "/api/stock", "/api/holdings", "/api/market_monitor/quotes"):
            self.assertIn(ep, self.specs)

    def test_mock_has_every_registered_field(self):
        gaps = {}
        for an in self.analyzers.values():
            for mock, endpoints in an.links.items():
                for ep in endpoints:
                    if ep in self.specs and mock in self.mocks:
                        miss = spec_missing(self.specs[ep], self.mocks[mock])
                        if miss:
                            gaps[mock] = miss
        self.assertEqual({k: sorted(v) for k, v in gaps.items()}, {k: sorted(v) for k, v in KNOWN_SPEC_GAPS.items()},
                         "mock 与 debug-fields.js 登记表的差异和已知例外不一致")


if __name__ == "__main__":
    unittest.main()
