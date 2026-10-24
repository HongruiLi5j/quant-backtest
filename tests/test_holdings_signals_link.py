"""T013 · S5 信号页 × 持仓联动：静态检查 + Node 合成数据示例。
只用合成数据；不请求服务、不读写 data/。验证：
- 持有的卖出/减仓置顶并标注；未持有降权标“未持有”；买入已持有标“已持有”；
- 台账不可用/为空时不误标；
- 持仓不进 TRADE.data / exportRows，CSV 导出不含持仓；
- 请求头齐全、无浏览器存储、非个人模式零请求。"""
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / 'dashboard/app.js').read_text()
HTML = (ROOT / 'dashboard/index.html').read_text()
CSS = (ROOT / 'dashboard/terminal.css').read_text()

START = '// ================================================================ 信号页 × 持仓联动'
LINK_FULL = APP[APP.index(START):APP.index('// ================================================================ 全市场')]
# 去掉整行注释和行尾注释后再做禁用词检查（注释里允许写“不写进 TRADE.exportRows”这类说明）
LINK = '\n'.join(re.sub(r'\s//\s.*$', '', l) for l in LINK_FULL.splitlines() if not l.lstrip().startswith('//'))


class LinkStaticTests(unittest.TestCase):
    def test_link_block_is_read_only_and_private(self):
        for bad in ('innerHTML', 'outerHTML', 'insertAdjacentHTML', 'eval(', 'new Function', 'localStorage',
                    'sessionStorage', 'indexedDB', 'document.cookie', 'store.set', 'pRequest', 'pInvalidate',
                    'console.', 'fetch(', 'window.HLINK', 'globalThis'):
            self.assertNotIn(bad, LINK, bad)
        # 唯一的取数出口是持仓页已有的 hRequest，且路径固定
        self.assertEqual(LINK.count('hRequest('), 1)
        self.assertIn('hRequest("/api/holdings?trades_limit=1&include_voided=0")', LINK)
        # 联动不得写入信号数据对象
        for bad in ('TRADE.data.', 'TRADE.exportRows', 'row.holding', 'r.holding', 'r.held'):
            self.assertNotIn(bad, LINK, bad)

    def test_export_paths_do_not_touch_holdings(self):
        csv = APP[APP.index('function tradeCSVData'):APP.index('function tradeHeaderHeight')]
        for bad in ('HLINK', 'hLink', 'HOLD', 'holding', 'hold-link'):
            self.assertNotIn(bad, csv, bad)
        # 导出行在置顶排序之前确定：exportRows 只按原 compare 排序
        render = APP[APP.index('function renderTrade'):APP.index('async function loadTradeFollowups')]
        self.assertIn('TRADE.exportRows = [...filtered].sort(compare);', render)
        self.assertLess(render.index('TRADE.exportRows ='), render.index('hLinkOrder('))
        # XLSX 由服务端接口生成，请求参数里只有策略和日期
        self.assertIn('new URLSearchParams({ strategy: $("tradeStrategy").value, date: $("tradeDate").value })', APP)

    def test_wiring(self):
        self.assertIn('id="tradeHoldLink"', HTML)
        self.assertIn('if (p !== "trade") hLinkClear();', APP)
        load = APP[APP.index('async function loadTrade()'):APP.index('// ================================================================ 信号页 × 持仓联动')]
        self.assertIn('hLinkClear();', load)
        self.assertIn('hLinkRefresh(request)', load)
        # 异常信号表不标注
        self.assertIn('options.exception ? null : hLinkNote(r)', APP)

    def test_css_scoped_to_personal_mode(self):
        block = CSS[CSS.index('/* T013 S5'):CSS.index('/* T013 持仓页')]
        clean = re.sub(r'/\*.*?\*/', '', block, flags=re.S)
        sels = re.findall(r'([^{}]+)\{', clean)
        self.assertTrue(sels)
        for s in sels:
            for part in s.split(','):
                self.assertTrue(part.strip().startswith('html.personal-mode'), part)
        self.assertIn('.hold-link-dim td { opacity', block)

    @unittest.skipUnless(shutil.which('node'), 'Node 不可用，静态检查仍运行')
    def test_node_syntax(self):
        r = subprocess.run(['node', '--check', 'dashboard/app.js'], cwd=ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)

    @unittest.skipUnless(shutil.which('node'), '需要 Node')
    def test_node_synthetic_scenarios(self):
        r = subprocess.run(['node', '-e', NODE_SCRIPT], cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('LINK OK', r.stdout)


NODE_SCRIPT = r'''
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const tick = () => new Promise(r => setImmediate(r));
class El {
  constructor(tag = 'div') {
    this.tagName = tag; this.children = []; this.dataset = {}; this.attrs = {}; this.events = {}; this._text = '';
    this.value = ''; this.hidden = false; this.parentElement = null; this._classes = new Set();
    const c = this._classes;
    this.classList = {add: (...x) => x.forEach(k => c.add(k)), remove: x => c.delete(x), contains: x => c.has(x), toggle: (x, on) => (on === undefined ? !c.has(x) : on) ? c.add(x) : c.delete(x)};
  }
  set textContent(v) { this._text = String(v ?? ''); this.children = []; }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(' '); }
  set innerHTML(v) { throw Error('innerHTML used'); }
  append(...n) { for (const x of n) { this.children.push(x); x.parentElement = this; } }
  prepend(...n) { for (const x of n.reverse()) { this.children.unshift(x); x.parentElement = this; } }
  replaceChildren(...n) { this.children = []; this._text = ''; this.append(...n); }
  setAttribute(k, v) { this.attrs[k] = v; }
  addEventListener() {}
  querySelectorAll(sel) {
    const hit = c => sel.startsWith('.') ? c._classes.has(sel.slice(1)) : c.tagName === sel;
    return this.children.flatMap(c => [...(hit(c) ? [c] : []), ...c.querySelectorAll(sel)]);
  }
}
const byId = {};
async function boot({personal = true} = {}) {
  const requests = [], storage = [];
  const state = {response: null};
  const window = {AI_I18N: {language: 'zh'}, addEventListener() {}, scrollTo() {}, crypto: {randomUUID: () => 'u'}};
  const ctx = vm.createContext({window, console, Set, Map, URLSearchParams, Intl, Date, Math, Number, Object, Array, Promise, JSON, String, BigInt,
    location: {hostname: '127.0.0.1', hash: '#/trade'}, history: {replaceState() {}},
    document: {documentElement: new El('html'), getElementById: id => (byId[id + ctx.__n] ||= Object.assign(new El(), {_id: id})),
      createElement: t => new El(t), querySelector: () => null, querySelectorAll: () => []},
    localStorage: {getItem() { return null; }, setItem(...a) { storage.push(a); }, removeItem(...a) { storage.push(a); }},
    sessionStorage: {setItem(...a) { storage.push(a); }}, setTimeout, clearTimeout, setInterval() {},
    fetch: async (path, opts = {}) => {
      requests.push({path, method: opts.method || 'GET', headers: opts.headers || {}});
      const r = typeof state.response === 'function' ? state.response() : state.response;
      if (r === 'throw') throw new Error('offline 99999.99');
      return {status: r.status, json: async () => r.body};
    }});
  ctx.__n = boot.n = (boot.n || 0) + 1;
  vm.runInContext(fs.readFileSync('dashboard/app.js', 'utf8').replace(/\ninit\(\);\s*$/, '\n'), ctx);
  vm.runInContext(`S.meta = {personal_mode: ${personal}}; TRADE.tab = "sell"; TRADE.manualSort = true; TRADE.sort = "avg_turnover_20d"; TRADE.dir = -1;`, ctx);
  const run = c => vm.runInContext(c, ctx);
  const get = id => ctx.document.getElementById(id);
  return {ctx, run, get, requests, storage, state, window};
}
const row = (code, action, turnover, extra = {}) => ({code, name: '名' + code, last_date: '2026-10-21', action, close: 10, d1: 1, avg_turnover_20d: turnover,
  liquidity: '正常', lot_cost: 1000, now: 1, next: 0, reason: 'r', flags: [], performance: {total: 5, trades: 12, sharpe: 1}, ...extra});
const synth = () => ({strategy: 'blend2', data_date: '2026-10-21', binary: false, min_target: 0.05, exceptions: [], counts: {},
  rows: [row('600999', '卖出', 9e8), row('600000', '卖出', 5e8), row('600555', '减仓', 3e8), row('000333', '买入', 4e8), row('601111', '买入', 2e8)]});
const holdingsOk = (positions) => ({status: 200, body: {ok: true, as_of: '2026-10-22', capital: '100000.00', summary: {}, closed: [], trades: [{id: 1, note: 'SECRET-NOTE', qty: 777777}], positions}});
const P = (code, o = {}) => ({code, name: 'n', shares: 1000, avg_cost: '10.340', first_buy: '2026-10-01', held_days: 21, price: '12.100', price_date: '2026-10-21', market_value: '12100.00', floating_pnl: '1100.00', floating_pct: '0.1064', weight: '0.1210', realized: '0.00', dividends: '0.00', ...o});
const codesOf = t => t.get('tradeTable').querySelectorAll('tr').filter(r => r.parentElement.tagName === 'tbody').map(r => r.children[0].querySelectorAll('a')[0].textContent.split(' ')[1]);
const trs = t => t.get('tradeTable').querySelectorAll('tr').filter(r => r.parentElement.tagName === 'tbody');

(async () => {
  // ---- 场景 1：有持仓。卖出页：持有的置顶并标注；未持有降权；API 请求头齐全
  {
    const t = await boot();
    t.run(`TRADE.data = ${JSON.stringify(synth())}; TRADE.request = 1;`);
    t.run('renderTrade()');
    assert.deepEqual(codesOf(t), ['600999', '600000']);   // 联动前：按成交额降序
    assert.equal(t.get('tradeHoldLink').children.length, 0);
    const before = JSON.stringify(t.run('TRADE.data'));
    t.state.response = holdingsOk([P('600000'), P('000333', {shares: 300, avg_cost: '50.000', held_days: 2, floating_pnl: null, floating_pct: null})]);
    await t.run('hLinkRefresh(1)');
    assert.equal(t.requests.length, 1);
    assert.equal(t.requests[0].path, '/api/holdings?trades_limit=1&include_voided=0');
    assert.equal(t.requests[0].headers['X-Requested-With'], 'dashboard');
    assert.deepEqual(codesOf(t), ['600000', '600999']);   // 持有的卖出信号置顶
    let rs = trs(t);
    assert(rs[0].classList.contains('hold-link-held'));
    const note = rs[0].children[0].children.find(c => c._classes.has('hold-link-note')).textContent;
    assert.equal(note, '你持有 1,000 股，成本 10.340，持有 21 天，浮动盈亏 1,100.00（+10.64%）');
    assert(rs[1].classList.contains('hold-link-dim'));
    assert(rs[1].children[0].textContent.includes('未持有'));
    assert(!rs[1].children[0].textContent.includes('你持有'));
    // 英文
    const en = rs[0].children[0].children.find(c => c._classes.has('hold-link-note')).dataset.en;
    assert.equal(en, 'You hold 1,000 shares, cost 10.340, held 21 days, floating P&L 1,100.00（+10.64%）');
    // 横幅含盯盘占位说明
    const banner = t.get('tradeHoldLink').textContent;
    assert(banner.includes('已按你的持仓台账标注') && banner.includes('盯盘盈亏') && banner.includes('占位'));
    // 导出：数据对象不变；exportRows 顺序仍是原排序；CSV 里没有任何持仓信息
    assert.equal(JSON.stringify(t.run('TRADE.data')), before);
    assert.deepEqual(t.run('TRADE.exportRows.map(r => r.code)'), ['600999', '600000']);
    const csv = JSON.stringify(t.run('tradeCSVData()'));
    assert(!csv.includes('10.340') && !csv.includes('1100.00') && !csv.includes('SECRET-NOTE') && !csv.includes('未持有'));
    assert.deepEqual(t.run('tradeCSVData().header.map(h => h[1])').includes('持有天数'), false);
    // 减仓页：同样规则；买入页：已持有标注，不重排
    t.run('TRADE.tab = "reduce"; renderTrade()');
    assert(trs(t)[0].classList.contains('hold-link-dim'));   // 600555 未持有
    t.run('TRADE.tab = "buy"; renderTrade()');
    assert.deepEqual(codesOf(t), ['000333', '601111']);
    rs = trs(t);
    assert(rs[0].children[0].textContent.includes('已持有'));
    assert(rs[0].classList.contains('hold-link-owned'));
    assert(!rs[1].children[0].textContent.includes('已持有') && !rs[1].classList.contains('hold-link-dim'));
    // 离开信号页：清空
    t.run('hLinkClear()');
    assert.equal(t.run('HLINK.state'), 'idle'); assert.equal(t.run('HLINK.map'), null);
    t.run('TRADE.tab = "sell"; renderTrade()');
    assert.deepEqual(codesOf(t), ['600999', '600000']);
    assert(trs(t).every(r => !r.classList.contains('hold-link-dim') && !r.classList.contains('hold-link-held')));
    assert.equal(t.storage.length, 0);
  }
  // ---- 场景 2：台账不可用（网络错误 / 401 / 404 / 500）：不误标
  for (const resp of ['throw', {status: 401, body: {error: 'x'}}, {status: 404, body: null}, {status: 500, body: {ok: false, error: 'HOLDINGS_INTERNAL'}}]) {
    const t = await boot();
    t.run(`TRADE.data = ${JSON.stringify(synth())}; TRADE.request = 1;`);
    t.state.response = resp;
    await t.run('hLinkRefresh(1)');
    assert.equal(t.run('HLINK.state'), 'unavailable');
    assert.deepEqual(codesOf(t), ['600999', '600000']);
    assert(trs(t).every(r => !r.classList.contains('hold-link-dim') && !r.classList.contains('hold-link-held')));
    assert(!t.get('tradeTable').textContent.includes('未持有'));
    const b = t.get('tradeHoldLink').textContent;
    assert(b.includes('持仓暂不可用') && b.includes('盯盘盈亏'));
    assert(!b.includes('99999'));   // 异常文本不回显
  }
  // ---- 场景 3：台账为空：不标注、不降权
  {
    const t = await boot();
    t.run(`TRADE.data = ${JSON.stringify(synth())}; TRADE.request = 1;`);
    t.state.response = holdingsOk([]);
    await t.run('hLinkRefresh(1)');
    assert.equal(t.run('HLINK.state'), 'empty');
    assert(trs(t).every(r => !r.classList.contains('hold-link-dim')));
    assert(t.get('tradeHoldLink').textContent.includes('持仓台账为空'));
  }
  // ---- 场景 4：过期请求丢弃；非个人模式零请求
  {
    const t = await boot();
    t.run(`TRADE.data = ${JSON.stringify(synth())}; TRADE.request = 5;`);
    t.state.response = holdingsOk([P('600000')]);
    await t.run('hLinkRefresh(4)');   // 请求号已变
    assert.equal(t.run('HLINK.state'), 'idle');
    const t2 = await boot({personal: false});
    t2.state.response = holdingsOk([P('600000')]);
    await t2.run('hLinkRefresh(0)');
    assert.equal(t2.requests.length, 0);
    assert.equal(t2.run('HLINK.state'), 'idle');
  }
  // ---- 场景 5：缺价格/缺天数不崩溃；异常信号表不标注
  {
    const t = await boot();
    const d = synth(); d.exceptions = [row('600000', '卖出', 1)];
    t.run(`TRADE.data = ${JSON.stringify(d)}; TRADE.request = 1;`);
    t.state.response = holdingsOk([P('600000', {floating_pnl: null, floating_pct: null, held_days: null})]);
    await t.run('hLinkRefresh(1)');
    const n = trs(t)[0].children[0].children.find(c => c._classes.has('hold-link-note')).textContent;
    assert.equal(n, '你持有 1,000 股，成本 10.340，持有 — 天，浮动盈亏 —');
    const ex = t.get('tradeExceptions').querySelectorAll('tr').filter(r => r.parentElement.tagName === 'tbody');
    assert(ex.length && ex.every(r => !r.classList.contains('hold-link-held')));
  }
  console.log('LINK OK');
})().catch(e => { console.error(e); process.exit(1); });
'''

if __name__ == '__main__':
    unittest.main()
