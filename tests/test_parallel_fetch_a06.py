"""A06：信号页首屏取数并行化的检查。

只读 dashboard/ui 下的源文件；node 运行时检查把 pages/ 和 shared/ 复制到临时目录里跑，
用假的 fetch 记录“哪个请求在什么时候发出、什么时候返回”，不启动服务、不读 data/、不联网。
1. 静态：signals.js 用 Promise.all 并行取 meta / main_strategy / trade_list_dates，不再有串行的 await getJSON
2. （有 node 时）首次渲染三个请求同时在途，trade_list 必须等 dates 回来（它的 date 参数依赖 dates）
3. （有 node 时）非首次渲染不再请求 main_strategy；个人模式下持仓台账与清单并行
4. （有 node 时）错误处理保持原样：meta / dates 失败 → 渲染抛错；main_strategy 登录过期 → 抛错；其他错误 → 回退默认主策略
"""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "dashboard" / "ui"
SIGNALS = UI / "pages" / "signals.js"

NODE_SCRIPT = r"""
import assert from 'node:assert/strict';

// ---- 最小假 DOM：只够 signals.js 的 render 走完“清单报错”分支 ----
class FakeNode {
  constructor(tag = '') {
    this.tag = tag; this.children = []; this.attrs = {}; this.dataset = {}; this.textContent = ''; this.className = ''; this.value = '';
    this.style = { setProperty() {} };
    this.classList = { add() {}, remove() {}, toggle() {} };
  }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
  setAttribute(k, v) { this.attrs[k] = v; }
  addEventListener() {}
  querySelectorAll() { return []; }
  querySelector() { return null; }
  closest() { return this; }
}
globalThis.Node = FakeNode;
const noop = () => {};
globalThis.window = { matchMedia: () => ({ addEventListener: noop, matches: false }), location: { search: '', hostname: '127.0.0.1' }, addEventListener: noop, dispatchEvent: noop };
globalThis.localStorage = { getItem: () => null, setItem: noop };
globalThis.document = {
  documentElement: { lang: 'zh-CN', dataset: {} },
  body: new FakeNode('body'),
  createElement: (tag) => new FakeNode(tag),
  createTextNode: (s) => { const n = new FakeNode('#text'); n.textContent = s; return n; },
  addEventListener: noop,
  querySelectorAll: () => [],
};

// ---- 假 fetch：记录每个请求的发出 / 返回顺序；handlers 由各场景设置 ----
let clock = 0;
let log = [];
let handlers = {};
const DELAY = 15;
globalThis.fetch = (url) => {
  const path = String(url);
  const rec = { path, start: ++clock, end: null };
  log.push(rec);
  const base = path.split('?')[0];
  return new Promise((resolve) => setTimeout(() => {
    rec.end = ++clock;
    const h = handlers[base];
    const status = h?.status ?? 200;
    resolve({ ok: status < 400, status, json: async () => (h ? h.body : {}) });
  }, DELAY));
};
const find = (base) => log.find((r) => r.path.split('?')[0] === base);
const count = (base) => log.filter((r) => r.path.split('?')[0] === base).length;

const META = { personal_mode: false, strategies: { blend2: { kind: 'single', label: 'blend2' }, rsi2: { kind: 'single', label: 'rsi2' } } };
const OK = (body) => ({ body });
const fresh = () => ({
  '/api/meta': OK(META),
  '/api/main_strategy': OK({ main: 'rsi2' }),
  '/api/trade_list_dates': OK({ dates: ['2026-03-30'] }),
  '/api/trade_list': OK({ error: '测试：无清单' }),   // 走“清单报错”分支，不需要完整假 DOM
  '/api/holdings': OK({ ok: true, positions: [] }),
});
const hostRoot = () => {
  const host = new FakeNode('main');
  host.querySelector = () => new FakeNode('span');
  const root = new FakeNode('div');
  root.closest = () => host;
  return root;
};
let n = 0;
const load = async () => (await import(`./ui/pages/signals.js?case=${++n}`));   // 带不同查询串 → 每个场景一份全新的模块状态 S
const reset = (h) => { log = []; handlers = h; };

// 场景 1：首次渲染（非个人模式）——三个请求并行，trade_list 等 dates
{
  const { render } = await load();
  reset(fresh());
  await render(hostRoot());
  const meta = find('/api/meta'), main = find('/api/main_strategy'), dates = find('/api/trade_list_dates'), list = find('/api/trade_list');
  const firstEnd = Math.min(meta.end, main.end, dates.end);
  assert.ok(Math.max(meta.start, main.start, dates.start) < firstEnd, '三个请求应当都在任何一个返回之前发出');
  assert.ok(list.start > Math.max(meta.end, main.end, dates.end), 'trade_list 要等三个都回来');
  assert.equal(count('/api/holdings'), 0, '非个人模式不读持仓');
  assert.equal(log.length, 4);
}

// 场景 2：同一页面第二次渲染——不再请求 main_strategy，meta 与 dates 并行
{
  const { render } = await load();
  reset(fresh());
  await render(hostRoot());
  reset(fresh());
  await render(hostRoot());
  assert.equal(count('/api/main_strategy'), 0);
  const meta = find('/api/meta'), dates = find('/api/trade_list_dates');
  assert.ok(Math.max(meta.start, dates.start) < Math.min(meta.end, dates.end));
  assert.equal(log.length, 3);
}

// 场景 3：个人模式——持仓台账与清单并行（台账在清单返回之前就发出）
{
  const { render } = await load();
  reset({ ...fresh(), '/api/meta': OK({ ...META, personal_mode: true }) });
  await render(hostRoot());
  const list = find('/api/trade_list'), hold = find('/api/holdings');
  assert.ok(hold, '个人模式要读持仓');
  assert.ok(hold.start < list.end, '持仓台账应在清单返回之前发出');
  assert.ok(hold.start > find('/api/trade_list_dates').end, '台账在三个首屏请求之后、与清单一起发出');
}

// 场景 4：错误处理保持原样
async function rejects(overrides, check) {
  const { render } = await load();
  reset({ ...fresh(), ...overrides });
  let err = null;
  try { await render(hostRoot()); } catch (e) { err = e; }
  assert.ok(err, '应当抛错（页面启动器据此显示错误卡片）');
  if (check) check(err);
}
await rejects({ '/api/meta': { status: 500, body: { error: 'boom' } } });
await rejects({ '/api/trade_list_dates': { status: 500, body: { error: 'boom' } } });
await rejects({ '/api/main_strategy': { status: 401, body: {} } }, (e) => assert.equal(e.auth, true));
{
  // main_strategy 非登录类错误：回退默认主策略，渲染照常完成
  const { render } = await load();
  reset({ ...fresh(), '/api/main_strategy': { status: 500, body: {} } });
  await render(hostRoot());
  assert.equal(count('/api/trade_list'), 1);
  assert.match(find('/api/trade_list').path, /strategy=blend2/);
}
console.log('OK');
"""


class A06StaticTests(unittest.TestCase):
    def setUp(self):
        self.src = SIGNALS.read_text(encoding="utf-8")

    def test_first_screen_requests_use_promise_all(self):
        self.assertRegex(self.src, r"await Promise\.all\(\[\s*getJSON\('/api/meta'\),")
        self.assertIn("getJSON('/api/trade_list_dates')", self.src)
        self.assertNotIn("await getJSON('/api/meta')", self.src)
        self.assertNotIn("await getJSON('/api/trade_list_dates')", self.src)
        self.assertNotIn("await getJSON('/api/main_strategy')", self.src)

    def test_main_strategy_still_swallows_only_non_auth_errors(self):
        self.assertIn("if (e.auth) throw e; return {};", self.src)

    def test_dependent_list_request_stays_serial(self):
        # trade_list 的 strategy 来自 main_strategy / meta，date 要用 dates 校验：必须仍是单独的 await
        self.assertIn("const r = await getJSON(`/api/trade_list?", self.src)

    def test_no_html_injection_added(self):
        for bad in ("innerHTML", "insertAdjacentHTML", "document.write"):
            self.assertNotIn(bad, self.src)


@unittest.skipUnless(shutil.which("node"), "Node 不可用，静态检查仍运行")
class A06NodeTests(unittest.TestCase):
    def test_signals_render_requests_run_in_parallel(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "ui").mkdir()
            shutil.copytree(UI / "pages", tmp / "ui" / "pages", ignore=shutil.ignore_patterns("*.before-*"))
            shutil.copytree(UI / "shared", tmp / "ui" / "shared")
            (tmp / "ui" / "package.json").write_text('{"type": "module"}', encoding="utf-8")   # node 22.6 不会自动识别 ES module
            (tmp / "case.mjs").write_text(NODE_SCRIPT, encoding="utf-8")
            res = subprocess.run(["node", "case.mjs"], cwd=tmp, capture_output=True, text=True, timeout=60)
            self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
            self.assertIn("OK", res.stdout)


if __name__ == "__main__":
    unittest.main()
