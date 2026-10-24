"""T013 · S4 持仓页前端：静态约束 + 离线 Node 合成 DOM。只用合成数据，不请求服务、不读 data/。"""
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / 'dashboard/app.js').read_text()
I18N = (ROOT / 'dashboard/i18n.js').read_text()
HTML = (ROOT / 'dashboard/index.html').read_text()
CSS = (ROOT / 'dashboard/terminal.css').read_text()

START = '// ================================================================ 持仓台账'
BLOCK = APP[APP.index(START):APP.index('const PAGES =')]
TRANSLATIONS = I18N[I18N.index('const holdingsTranslations'):I18N.index('window.HOLDINGS_I18N')]


class HoldingsStaticTests(unittest.TestCase):
    def test_nav_sixth_item_personal_only(self):
        nav = re.search(r'const PERSONAL_NAV = (.*);', APP).group(1)
        pages = re.findall(r'\["(\w+)",', nav)
        self.assertEqual(len(pages), 7)
        self.assertEqual(pages[5], 'holdings')
        self.assertEqual(pages[6], 'monitor')
        self.assertIn('["holdings", "持仓"]', nav)
        self.assertIn('["monitor", "盯盘"]', nav)
        # 完整模式的静态导航里没有持仓入口；路由在非个人模式回今日。
        self.assertNotIn('data-p="holdings"', HTML)
        self.assertIn('((name !== "holdings" && name !== "monitor") || personalMode())', APP)
        self.assertIn('if (p !== "holdings") holdingsLeave();', APP)
        self.assertIn('async function loadHoldings() {\n  if (!personalMode()) return;', APP)
        self.assertIn('id="p-holdings"', HTML)
        self.assertIn('"持仓": "Holdings"', I18N)

    def test_text_nodes_only(self):
        for bad in ('innerHTML', 'outerHTML', 'insertAdjacentHTML', 'document.write', 'DOMParser', 'eval(', 'new Function'):
            self.assertNotIn(bad, BLOCK, bad)
        self.assertIn('textContent', BLOCK)

    def test_no_browser_persistence_or_leaks(self):
        for bad in ('localStorage', 'sessionStorage', 'indexedDB', 'document.cookie', 'store.set', 'store.get',
                    'PERSONAL.cache', 'pRequest', 'pInvalidate', 'console.', 'globalThis.HOLD', 'caches.'):
            self.assertFalse(bad in BLOCK, f'持仓代码不应出现 {bad}')
        # 持仓状态变量不能挂到全局对象上（HOLDINGS_I18N 是文案表，不含数据）
        self.assertIsNone(re.search(r'window\.HOLD(?!INGS_I18N)', BLOCK))
        # 持仓块之外也不能用浏览器存储碰持仓
        outside = APP.replace(BLOCK, '')
        for line in outside.splitlines():
            if re.search(r'holding|HOLD', line):
                self.assertNotRegex(line, r'localStorage|sessionStorage|indexedDB|store\.set|document\.cookie')

    def test_requests_carry_required_headers(self):
        self.assertEqual(BLOCK.count('fetch('), 1)  # 只有 hRequest 一个出口
        req = BLOCK[BLOCK.index('async function hRequest'):BLOCK.index('function hErrorKey')]
        self.assertIn('"X-Requested-With": "dashboard"', req)
        self.assertIn('credentials: "same-origin"', req)
        self.assertIn('cache: "no-store"', req)
        # 请求头在 GET 与 POST 共用的 opts 里，不是只在 POST 分支
        self.assertLess(req.index('"X-Requested-With"'), req.index('if (body !== undefined)'))
        # 错误展示只用固定文案，不显示服务端 message 或异常文本
        self.assertNotIn('.message', BLOCK)
        self.assertNotIn('e.message', BLOCK)

    def test_i18n_keys_complete(self):
        keys = {k: (zh, en) for k, zh, en in re.findall(r'"(hold\.[\w.]+)": \["((?:[^"\\]|\\.)+)", "((?:[^"\\]|\\.)+)"\]', TRANSLATIONS)}
        self.assertGreater(len(keys), 100)
        for k, (zh, en) in keys.items():
            self.assertTrue(zh.strip() and en.strip(), k)
            # 占位符中英文必须一致
            self.assertEqual(sorted(re.findall(r'\{\w+\}', zh)), sorted(re.findall(r'\{\w+\}', en)), k)
            self.assertTrue(re.search(r'[一-鿿]', zh), k)
            self.assertFalse(re.search(r'[一-鿿]', en), k)
        used = set(re.findall(r'"(hold\.[\w.]+)"', BLOCK))
        used |= {f'hold.side.{s}' for s in ('buy', 'sell', 'bonus', 'dividend')}
        self.assertFalse(sorted(used - set(keys)), '代码里用到但 i18n 里没有的键')
        # 接口所有错误码都有映射
        codes = set()
        for f in ('api.py', 'calc.py', 'store.py'):
            codes |= set(re.findall(r'HOLDINGS_[A-Z_]+', (ROOT / 'src/holdings' / f).read_text()))
        missing = sorted(c for c in codes if f'hold.err.{c}' not in keys)
        self.assertEqual(missing, [])
        for c in ('AUTH', 'UNAVAILABLE', 'NETWORK', 'generic'):
            self.assertIn(f'hold.err.{c}', keys)

    def test_css_scoped_and_mobile(self):
        block = CSS[CSS.index('/* T013 持仓页'):]
        clean = re.sub(r'/\*.*?\*/', '', block, flags=re.S)
        for selector in re.findall(r'([^{}]+)\{', clean):
            if selector.strip().startswith('@media'):
                continue
            for part in re.split(r',(?![^()]*\))', selector):
                self.assertTrue(part.strip().startswith('html.personal-mode #p-holdings'), selector)
        self.assertIn('@media (max-width: 600px)', block)
        self.assertIn('minmax(0, 1fr)', block)
        self.assertIn('overflow-wrap: anywhere', block)
        self.assertIn('.hold-over { color: var(--red)', block)
        self.assertIn('.hold-cell-label { display: none; }', block)
        self.assertNotIn('overflow-x: scroll', block)

    @unittest.skipUnless(shutil.which('node'), 'Node 不可用，静态检查仍运行')
    def test_node_syntax(self):
        for f in ('app.js', 'i18n.js'):
            r = subprocess.run(['node', '--check', f'dashboard/{f}'], cwd=ROOT, capture_output=True, text=True, timeout=30)
            self.assertEqual(r.returncode, 0, r.stderr)


NODE_SCRIPT = r'''
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const tick = () => new Promise(r => setImmediate(r));
class El {
  constructor(tag = 'div') {
    this.tagName = tag; this.children = []; this.dataset = {}; this.attrs = {}; this.events = {}; this._text = '';
    this.value = ''; this.hidden = false; this.disabled = false; this.checked = false; this.parentElement = null;
    const classes = new Set(); this._classes = classes;
    this.classList = {add: (...x) => x.forEach(c => classes.add(c)), remove: x => classes.delete(x), contains: x => classes.has(x), toggle: (x, on) => (on === undefined ? !classes.has(x) : on) ? classes.add(x) : classes.delete(x)};
  }
  set textContent(v) { this._text = String(v ?? ''); this.children = []; }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(' '); }
  set innerHTML(v) { throw Error('innerHTML used'); }
  set id(v) { this._id = v; } get id() { return this._id; }
  get firstChild() { return this.children[0]; }
  append(...nodes) { for (const n of nodes) { if (n.parentElement) n.parentElement.children.splice(n.parentElement.children.indexOf(n), 1); this.children.push(n); n.parentElement = this; } }
  replaceChildren(...nodes) { this.children.forEach(n => n.parentElement = null); this.children = []; this._text = ''; this.append(...nodes); }
  setAttribute(k, v) { this.attrs[k] = v; if (k === 'id') this._id = v; }
  getAttribute(k) { return this.attrs[k]; }
  addEventListener(k, fn) { (this.events[k] ||= []).push(fn); }
  focus() {}
  matches(sel) {
    if (sel.startsWith('#')) return this._id === sel.slice(1);
    if (sel.startsWith('.')) return this._classes.has(sel.slice(1));
    return this.tagName === sel;
  }
  querySelectorAll(sel) { return this.children.flatMap(c => [...(c.matches(sel) ? [c] : []), ...c.querySelectorAll(sel)]); }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  async fire(k, ev = {}) { for (const fn of this.events[k] || []) await fn({preventDefault() {}, ...ev}); await tick(); }
}
function fixture() {
  const root = new El('html');
  for (const id of ['tabs', 'shareBar', 'main']) { const e = new El(id === 'tabs' ? 'nav' : 'div'); e.id = id; root.append(e); }
  for (const id of ['p-holdings', 'p-today']) { const e = new El('section'); e.id = id; e.classList.add('page'); root.append(e); }
  return root;
}
async function boot({personal}) {
  const root = fixture(), requests = [], events = {}, loads = [], storage = [];
  const responses = {get: null, add: null, void: null, capital: null, search: [{code: '600000', name: '浦发银行'}]};
  const serve = (path, opts) => {
    if (path.startsWith('/api/search')) return {status: 200, body: responses.search};
    const key = path.startsWith('/api/holdings/add') ? 'add' : path.startsWith('/api/holdings/void') ? 'void' : path.startsWith('/api/holdings/capital') ? 'capital' : 'get';
    const r = typeof responses[key] === 'function' ? responses[key](path, opts) : responses[key];
    if (r === 'throw') throw new Error('offline secret 123456.78');
    return r;
  };
  let uuid = 0;
  const window = {AI_I18N: {language: 'zh'}, addEventListener: (k, f) => (events[k] ||= []).push(f), scrollTo() {},
    crypto: {randomUUID: () => 'uuid-test-' + String(++uuid).padStart(6, '0')}};
  const ctx = vm.createContext({window, console, Set, URLSearchParams, Intl, Date, Math, Number, Object, Array, Promise, JSON, String,
    location: {hostname: '127.0.0.1', hash: '#/holdings'}, history: {replaceState(a, b, h) { ctx.location.hash = h; }},
    document: {documentElement: root, getElementById: id => root.querySelector('#' + id), createElement: t => new El(t),
      querySelector: s => root.querySelector(s), querySelectorAll: s => root.querySelectorAll(s)},
    localStorage: {getItem() { return null; }, setItem(...a) { storage.push(a); }, removeItem(...a) { storage.push(a); }},
    sessionStorage: {setItem(...a) { storage.push(a); }}, setTimeout, clearTimeout, setInterval() {},
    fetch: async (path, opts = {}) => {
      requests.push({path, method: opts.method || 'GET', headers: opts.headers || {}, credentials: opts.credentials, cache: opts.cache, body: opts.body ? JSON.parse(opts.body) : null});
      const r = await serve(path, opts);
      return {status: r.status, json: async () => { if (typeof r.body === 'string') throw new Error('not json'); return r.body; }};
    }});
  vm.runInContext(fs.readFileSync('dashboard/app.js', 'utf8').replace(/\ninit\(\);\s*$/, '\n'), ctx);
  // 真实的持仓文案表（与网站同一份）：从 i18n.js 抽出来运行
  const i18n = fs.readFileSync('dashboard/i18n.js', 'utf8');
  const slice = i18n.slice(i18n.indexOf('const holdingsTranslations'), i18n.indexOf('const personalNavLabels'));
  vm.runInContext('let language = "zh";' + slice, ctx);
  vm.runInContext(`renderMarkets = setupSearch = () => {}; loadToday = async () => loads.push('today');`, ctx);
  ctx.loads = loads;
  vm.runInContext(`S.meta = {personal_mode: ${personal}}`, ctx);
  return {root, requests, events, loads, storage, responses, ctx, window, run: c => vm.runInContext(c, ctx), get: id => root.querySelector('#' + id)};
}
const snapshot = () => ({ok: true, as_of: '2026-10-22', capital: '100000.00',
  summary: {market_value: '17850.00', total_cost: '16000.00', floating_pnl: '1850.00', realized: '-120.50', dividends: '30.00', invested_ratio: '0.1785', missing_price_count: 1},
  positions: [
    {code: '600001', name: 'B股票', shares: 500, avg_cost: '10.000', first_buy: '2026-10-10', held_days: 12, price: '11.500', price_date: '2026-10-22', market_value: '5750.00', floating_pnl: '750.00', floating_pct: '0.1500', weight: '0.0575', realized: '0.00', dividends: '0.00'},
    {code: '600000', name: '<img src=x onerror=bad()>浦发', shares: 1000, avg_cost: '10.340', first_buy: '2026-10-01', held_days: 21, price: '12.100', price_date: '2026-10-14', market_value: '12100.00', floating_pnl: '1100.00', floating_pct: '0.1064', weight: '0.1210', realized: '0.00', dividends: '0.00'},
    {code: '000333', name: '无价格', shares: 100, avg_cost: '50.000', first_buy: '2026-10-20', held_days: 2, price: null, price_date: null, market_value: null, floating_pnl: null, floating_pct: null, weight: null, realized: '0.00', dividends: '0.00'}],
  closed: [{code: '000001', name: '平安银行', first_buy: '2026-09-01', last_sell: '2026-09-20', held_days: 19, pnl: '-120.50'}],
  trades: [
    {id: 7, trade_date: '2026-10-20', code: '000333', name: '无价格', side: 'buy', qty: 100, price: '50.000', fee: '5.00', fee_estimated: true, signal_ref: null, note: '备注X', voided: false, created_at: 'x'},
    {id: 6, trade_date: '2026-10-19', code: '600001', name: 'B股票', side: 'buy', qty: 500, price: '10.000', fee: '5.00', fee_estimated: false, signal_ref: null, note: '', voided: true, created_at: 'x'}]});
const ok = body => ({status: 200, body: {ok: true, ...body}});
const colText = (row) => row.children.map(td => td.children[1].textContent);

(async () => {
  // ---- 非个人模式：#/holdings 回今日，零持仓请求，不构建页面
  {
    const t = await boot({personal: false});
    t.run('route()'); await tick();
    assert.deepEqual(t.loads, ['today']);
    assert.equal(t.requests.filter(r => r.path.includes('/api/holdings')).length, 0);
    assert.equal(t.run('HOLD.built'), false);
    assert.equal(t.get('p-holdings').children.length, 0);
  }
  // ---- 个人模式：导航第 6 项
  {
    const t = await boot({personal: true});
    t.run('setupPersonalPages = () => {}');
    const tabs = t.get('tabs'); t.run('setupPersonalNavigation()');
    assert.deepEqual(tabs.children.map(x => x.dataset.p), ['today', 'trade', 'market', 'watch', 'lab', 'holdings', 'monitor']);
    assert.equal(tabs.children[5].textContent, '持仓');
    assert.equal(tabs.children[6].textContent, '盯盘');
  }
  // ---- 主流程
  const t = await boot({personal: true});
  t.responses.get = ok(snapshot());
  t.run('route()'); await tick();
  let req = t.requests.filter(r => r.path.startsWith('/api/holdings'));
  assert.equal(req.length, 1);
  assert.equal(req[0].path, '/api/holdings?trades_limit=200&include_voided=0');
  assert.equal(req[0].headers['X-Requested-With'], 'dashboard');
  assert.equal(req[0].credentials, 'same-origin');
  assert.equal(req[0].cache, 'no-store');
  assert(t.get('p-holdings').classList.contains('on'));
  // 汇总
  const sum = t.get('holdSummaryBody').textContent;
  for (const s of ['100,000.00', '17,850.00', '17.9%', '1,850.00', '-120.50', '30.00', '1 只持仓价格不可用']) assert(sum.includes(s), s);
  // 持仓表：按占比降序，缺价格排最后；>10% 标红
  const rows = t.get('holdPositionsTable').querySelectorAll('tr').filter(r => r.parentElement.tagName === 'tbody');
  assert.deepEqual(rows.map(r => r.children[0].children[1].textContent), ['600000', '600001', '000333']);
  assert(rows[0].classList.contains('hold-row-over'));
  assert(!rows[1].classList.contains('hold-row-over'));
  const w0 = rows[0].children[6];
  assert(w0.querySelector('.hold-over')); assert(w0.textContent.includes('12.10%')); assert(w0.textContent.includes('超 10%'));
  assert(!rows[1].children[6].querySelector('.hold-over'));
  assert(rows[0].textContent.includes('价格偏旧'));      // 价格日期 10-14，距 as_of 8 天
  assert(!rows[1].textContent.includes('价格偏旧'));
  assert(rows[2].textContent.includes('价格不可用'));    // 缺失就是缺失，不用 0 顶替
  assert.equal(colText(rows[0])[8], '21'); assert.equal(colText(rows[0])[7], '2026-10-01');
  assert(colText(rows[0])[1].includes('<img src=x onerror=bad()>'));          // 仅作为文本
  assert(!t.root.querySelector('img'));
  // 历史与已清仓
  const hist = t.get('holdHistoryTable').querySelectorAll('tr').filter(r => r.parentElement.tagName === 'tbody');
  assert.equal(hist.length, 2);
  assert(hist[0].textContent.includes('估算')); assert(hist[0].textContent.includes('备注X'));
  assert(hist[1].classList.contains('hold-voided')); assert(!hist[1].querySelector('.hold-void'));
  assert(t.get('holdClosedBody').textContent.includes('平安银行'));
  assert(t.get('p-holdings').textContent.includes('盯盘盈亏：尚未接入'));
  assert(t.get('p-holdings').textContent.includes('不连接券商、不下单'));
  // 日期默认北京今天
  assert(/^\d{4}-\d{2}-\d{2}$/.test(t.get('holdDate').value));

  // ---- 代码名称提示（先查已有台账，再用只读搜索接口）
  t.get('holdCode').value = '600000'; await t.get('holdCode').fire('input');
  assert(t.get('holdNameHint').textContent.includes('浮') === false);
  assert(t.get('holdNameHint').textContent.includes('img'));      // 台账里已有的名称
  t.get('holdCode').value = '510300'; t.responses.search = [{code: '510300', name: '沪深300ETF'}];
  await t.get('holdCode').fire('input'); assert.equal(t.get('holdNameHint').textContent, '沪深300ETF');
  t.get('holdCode').value = '600519'; t.responses.search = []; await t.get('holdCode').fire('input');
  assert(t.get('holdNameHint').textContent.includes('未找到名称'));

  // ---- 录入：先显示确认摘要，不立刻发请求
  const post = path => t.requests.filter(r => r.path === path);
  const form = {code: '510300', side: 'buy', qty: '1000', price: '10.000', fee: '', note: 'n1'};
  const fill = () => { t.get('holdCode').value = form.code; t.get('holdSide').value = form.side; t.get('holdQty').value = form.qty; t.get('holdPrice').value = form.price; t.get('holdFee').value = form.fee; t.get('holdNote').value = form.note; };
  t.get('holdCode').value = '510300'; t.responses.search = [{code: '510300', name: '沪深300ETF'}]; await t.get('holdCode').fire('input');
  fill();
  await t.get('holdEntry').fire('submit');
  assert.equal(post('/api/holdings/add').length, 0);
  const confirm = t.get('holdConfirm');
  assert.equal(confirm.hidden, false);
  assert(confirm.textContent.includes('买入 510300 沪深300ETF 1000 股 @10.000 元，费用 按费率估算'), confirm.textContent);
  // 返回修改 → 摘要消失，仍未请求
  await t.get('holdConfirmBack').fire('click'); assert.equal(confirm.hidden, true); assert.equal(post('/api/holdings/add').length, 0);
  // 不完整输入：提示且不出摘要
  t.get('holdQty').value = ''; await t.get('holdEntry').fire('submit');
  assert(t.get('holdFormMessage').textContent.includes('请填写')); assert.equal(confirm.hidden, true);
  // 提交 → 确认 → 只发一次 POST（连点两次也只一次）
  fill(); await t.get('holdEntry').fire('submit');
  t.responses.add = ok({duplicate: false, trade: {id: 8}});
  const before = t.requests.filter(r => r.path.startsWith('/api/holdings?')).length;
  const okBtn = t.get('holdConfirmOk');
  await Promise.all([okBtn.fire('click'), okBtn.fire('click')]);
  const adds = post('/api/holdings/add'); assert.equal(adds.length, 1);
  assert.equal(adds[0].method, 'POST'); assert.equal(adds[0].headers['X-Requested-With'], 'dashboard'); assert.equal(adds[0].credentials, 'same-origin');
  assert.deepEqual(Object.keys(adds[0].body).sort(), ['client_id', 'code', 'note', 'price', 'qty', 'side', 'trade_date']);
  assert.equal(adds[0].body.qty, 1000); assert.equal(adds[0].body.code, '510300'); assert.equal(adds[0].body.price, '10.000');
  assert(!('fee' in adds[0].body));                                  // 费用留空 → 不发，由服务端估算
  assert(/^[A-Za-z0-9_-]{8,64}$/.test(adds[0].body.client_id));
  assert.equal(t.requests.filter(r => r.path.startsWith('/api/holdings?')).length, before + 1);   // 成功后重新读取
  assert.equal(t.get('holdCode').value, ''); assert.equal(t.get('holdQty').value, '');
  assert(t.get('holdMessage').textContent.includes('已入账'));
  const firstId = adds[0].body.client_id;
  fill(); await t.get('holdEntry').fire('submit'); await t.get('holdConfirmOk').fire('click');
  assert.notEqual(post('/api/holdings/add')[1].body.client_id, firstId);   // 新的一笔用新 id
  // 服务端拒绝：只显示固定文案，不回显服务端 message/数值
  t.responses.add = {status: 400, body: {error: 'HOLDINGS_OVERSELL', message: 'SERVER-MSG 99999', field: 'qty'}};
  fill(); form.side = 'sell'; fill(); await t.get('holdEntry').fire('submit'); await t.get('holdConfirmOk').fire('click');
  const fm = t.get('holdFormMessage').textContent;
  assert(fm.includes('卖出数量超过')); assert(!fm.includes('SERVER-MSG')); assert(!fm.includes('99999'));
  // 送转：不需要价格
  form.side = 'bonus'; form.price = ''; fill(); t.get('holdSide').value = 'bonus'; await t.get('holdSide').fire('change');
  await t.get('holdEntry').fire('submit');
  assert(t.get('holdConfirm').textContent.includes('送转 510300'));

  // ---- 撤销需二次确认
  const voidBtn = () => t.get('holdHistoryTable').querySelector('.hold-void');
  await voidBtn().fire('click');
  assert.equal(post('/api/holdings/void').length, 0);
  const cell = voidBtn() ? null : t.get('holdHistoryTable').querySelector('.hold-void-ok');
  assert(cell, '应出现确认撤销按钮');
  t.responses.void = ok({});
  await t.get('holdHistoryTable').querySelector('.hold-void-ok').fire('click');
  assert.equal(post('/api/holdings/void').length, 1);
  assert.deepEqual(post('/api/holdings/void')[0].body, {id: 7});
  assert.equal(post('/api/holdings/void')[0].headers['X-Requested-With'], 'dashboard');
  assert(t.get('holdMessage').textContent.includes('已撤销'));
  // 取消路径：不请求
  await voidBtn().fire('click'); const cancel = t.get('holdHistoryTable').querySelectorAll('button').find(b => b.textContent === '取消');
  await cancel.fire('click'); assert.equal(post('/api/holdings/void').length, 1); assert(voidBtn());
  // 撤销被拒（VOID_BREAKS_HISTORY）：固定提示，按钮恢复
  t.responses.void = {status: 400, body: {error: 'HOLDINGS_VOID_BREAKS_HISTORY', message: 'x'}};
  await voidBtn().fire('click'); await t.get('holdHistoryTable').querySelector('.hold-void-ok').fire('click');
  assert(t.get('holdMessage').textContent.includes('请先撤销后面的卖出')); assert(voidBtn());
  // 显示已撤销开关
  t.get('holdShowVoided').checked = true; await t.get('holdShowVoided').fire('change');
  assert(t.requests.at(-1).path.includes('include_voided=1'));

  // ---- 总资金就地编辑
  await t.get('holdCapitalEdit').fire('click');
  const input = t.get('holdCapitalInput'); assert(input); input.value = '200000';
  t.responses.capital = ok({});
  const save = t.get('holdSummaryBody').querySelectorAll('button').find(b => b.textContent === '保存'); await save.fire('click');
  assert.deepEqual(post('/api/holdings/capital')[0].body, {capital: '200000'});
  assert.equal(post('/api/holdings/capital')[0].headers['X-Requested-With'], 'dashboard');

  // ---- 语言切换：成对文案跟随；错误文案、表头也有英文
  t.window.AI_I18N.language = 'en'; for (const f of t.events['dashboard-language-change']) f();
  assert(t.get('p-holdings').textContent.includes('Current positions'));
  assert(t.get('holdPositionsTable').textContent.includes('Days held'));
  assert(t.get('holdEntry').querySelector('button').textContent === 'Submit');
  assert(t.get('holdCode').attrs.placeholder.includes('Code'));
  t.window.AI_I18N.language = 'zh'; for (const f of t.events['dashboard-language-change']) f();
  assert(t.get('p-holdings').textContent.includes('当前持仓'));

  // ---- 空状态
  t.responses.get = ok({as_of: '2026-10-22', capital: null, summary: {market_value: '0.00', total_cost: '0.00', floating_pnl: '0.00', realized: '0.00', dividends: '0.00', invested_ratio: null, missing_price_count: 0}, positions: [], closed: [], trades: []});
  t.run('loadHoldings()'); await tick();
  assert(t.get('holdPositionsBody').textContent.includes('还没有持仓'));
  assert(t.get('holdHistoryBody').textContent.includes('暂无成交记录'));
  assert(t.get('holdClosedBody').textContent.includes('暂无已清仓'));
  assert(t.get('holdSummaryBody').textContent.includes('未设置'));
  assert(!t.get('holdSummaryBody').textContent.includes('价格不可用'));

  // ---- 错误：固定文案，不回显原文
  const errCase = async (resp, expect, forbid = []) => {
    t.responses.get = resp; t.run('loadHoldings()'); await tick();
    const m = t.get('holdMessage').textContent; assert(m.includes(expect), m + ' / ' + expect);
    for (const f of forbid) assert(!m.includes(f), f);
    assert.equal(t.get('holdPositionsBody').textContent, '');       // 失败时不留旧数据
  };
  await errCase({status: 401, body: 'unauthorized'}, '登录已失效');
  await errCase({status: 404, body: 'not found'}, '持仓功能不可用');
  await errCase({status: 500, body: {error: 'HOLDINGS_INTERNAL', message: 'SECRET 777'}}, '服务内部错误', ['SECRET', '777']);
  await errCase({status: 500, body: {error: 'HOLDINGS_STORE_UNSAFE'}}, '持仓文件不安全');
  await errCase({status: 500, body: {error: 'WHATEVER_NEW_CODE <b>', message: 'm'}}, '操作失败', ['WHATEVER', '<b>']);
  await errCase('throw', '网络错误', ['secret', '123456.78']);
  await errCase({status: 200, body: 'not json'}, '操作失败');

  // ---- 离开页面：清空内存快照与表单
  t.responses.get = ok(snapshot()); t.run('loadHoldings()'); await tick();
  assert(t.get('holdPositionsBody').textContent.includes('600000'));
  t.get('holdCode').value = '600000'; t.get('holdQty').value = '100';
  t.ctx.location.hash = '#/today'; t.run('route()'); await tick();
  assert.equal(t.run('HOLD.snap'), null);
  assert.equal(t.get('holdPositionsBody').textContent, ''); assert.equal(t.get('holdSummaryBody').textContent, '');
  assert.equal(t.get('holdHistoryBody').textContent, ''); assert.equal(t.get('holdCode').value, ''); assert.equal(t.get('holdQty').value, '');
  assert(!t.get('p-holdings').classList.contains('on'));
  // 离开后迟到的响应不再渲染
  let release; t.responses.get = () => new Promise(r => release = r);
  t.ctx.location.hash = '#/holdings'; t.run('route()'); await tick();
  t.ctx.location.hash = '#/today'; t.run('route()'); await tick();
  release(ok(snapshot())); await tick();
  assert.equal(t.run('HOLD.snap'), null); assert.equal(t.get('holdPositionsBody').textContent, '');
  // ---- 没有任何浏览器存储写入
  assert.equal(t.storage.length, 0);
  console.log('PASS holdings frontend');
})().catch(e => { console.error(e); process.exitCode = 1; });
'''


class HoldingsNodeTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node 不可用')
    def test_node_synthetic_dom(self):
        r = subprocess.run(['node', '-e', NODE_SCRIPT], cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        print(r.stdout, end='')


if __name__ == '__main__':
    unittest.main(verbosity=2)
