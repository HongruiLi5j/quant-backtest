// A08 探针：在 node 里用一套极简假 DOM + 假 fetch 真的渲染 market / watch / signals 三个页面，
// 模拟点击和下拉，读出「发给接口的参数」「地址栏的变化」「页面上的控件状态」，以 JSON 打印给 tests/test_url_state_a08.py 断言。
// 不联网、不读 data/；接口返回用 dashboard/ui/mock-data 里的假数据（只读）。
// 用法：node --experimental-default-type=module a08_probe.mjs <dashboard/ui 绝对路径>
import fs from 'node:fs';

const ui = process.argv[2];
const uiUrl = new URL(`file://${ui}/`);
const mockJson = (name) => JSON.parse(fs.readFileSync(`${ui}/mock-data/${name}.json`, 'utf8'));

/* ---------- 极简假 DOM（只实现这三个页面用到的部分）---------- */
class FNode { constructor() { this.parent = null; this.kids = []; } }
class FText extends FNode { constructor(s) { super(); this.s = String(s); } get textContent() { return this.s; } }
const camel = (s) => s.replace(/-(\w)/g, (_, c) => c.toUpperCase());
class FEl extends FNode {
  constructor(tag) {
    super();
    this.tag = tag; this.attrs = {}; this.dataset = {}; this.listeners = {}; this.hidden = false; this.disabled = false;
    this.className = ''; this.style = { setProperty() {} }; this._value = null;
  }
  setAttribute(k, v) { this.attrs[k] = v; if (k === 'hidden') this.hidden = true; }
  getAttribute(k) { return this.attrs[k] ?? null; }
  append(...c) { for (const x of c) { x.parent = this; this.kids.push(x); } }
  replaceChildren(...c) { this.kids = []; this.append(...c); }
  set textContent(s) { this.replaceChildren(new FText(s)); }
  get textContent() { return this.kids.map((k) => k.textContent).join(''); }
  get options() { return this.kids.filter((k) => k.tag === 'option'); }
  get value() {
    if (this.tag === 'select') return this._value ?? (this.options[0]?.attrs.value ?? '');
    return this._value ?? this.attrs.value ?? '';
  }
  set value(v) {
    if (this.tag === 'select') this._value = this.options.some((o) => o.attrs.value === v) ? v : '';
    else this._value = v;
  }
  addEventListener(type, fn) { (this.listeners[type] ??= []).push(fn); }
  fire(type, extra = {}) { for (const fn of this.listeners[type] ?? []) fn({ type, target: this, stopPropagation() {}, key: '', ...extra }); }
  click() { this.fire('click'); }
  closest() { return dummy; }   // 页面用 root.closest('main') 找绑定文字的容器；点击处理里的 e.target.closest('a,button') 在探针里不会触发（只 fire 表头按钮）
  matches(sel) {
    const m = /^([a-z0-9]+)?((?:\[[^\]]+\]|\.[\w-]+)*)$/.exec(sel);
    if (!m) return false;
    if (m[1] && m[1] !== this.tag) return false;
    for (const part of m[2].match(/\[[^\]]+\]|\.[\w-]+/g) ?? []) {
      if (part[0] === '.') { if (!this.className.split(/\s+/).includes(part.slice(1))) return false; continue; }
      const [, name, val] = /^\[([\w-]+)(?:="?([^"\]]*)"?)?\]$/.exec(part);
      const have = name.startsWith('data-') ? (this.dataset[camel(name.slice(5))] ?? this.attrs[name]) : this.attrs[name];
      if (have === undefined || (val !== undefined && String(have) !== val)) return false;
    }
    return true;
  }
  descendants() { return this.kids.flatMap((k) => (k instanceof FEl ? [k, ...k.descendants()] : [])); }
  querySelectorAll(sel) {
    const chain = sel.trim().split(/\s+/);
    let pool = [this];
    for (const part of chain) pool = [...new Set(pool.flatMap((p) => p.descendants().filter((d) => d.matches(part))))];
    return pool;
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] ?? null; }
}
const dummy = new FEl('div');   // document.querySelector / root.closest('main') 统一返回它：页面往里写的绑定文字不关心
dummy.querySelector = () => dummy;
dummy.closest = () => dummy;
globalThis.Node = FNode;
globalThis.document = { createElement: (t) => new FEl(t), createTextNode: (s) => new FText(s), addEventListener() {}, querySelector: () => dummy,
  body: { contains: () => true, append() {} }, documentElement: { dataset: {}, lang: '' }, querySelectorAll: () => [] };

/* ---------- 假地址栏：replaceState 会改 location.search / hash；pushState 一旦被调用就计数 ---------- */
const loc = { pathname: '/w/x.html', search: '', hash: '', hostname: '127.0.0.1', origin: 'http://127.0.0.1:8765' };
const hist = { state: null, replaced: 0, pushed: 0,
  replaceState(_s, _t, url) { this.replaced++; const u = new URL(url, 'http://127.0.0.1:8765'); loc.pathname = u.pathname; loc.search = u.search; loc.hash = u.hash; },
  pushState() { this.pushed++; } };
globalThis.window = { location: loc, history: hist, matchMedia: () => ({ matches: false, addEventListener() {} }), dispatchEvent() {}, addEventListener() {} };
globalThis.CustomEvent = class { constructor(type) { this.type = type; } };
globalThis.location = loc;
const setUrl = (path, search, hash = '') => { loc.pathname = path; loc.search = search; loc.hash = hash; hist.replaced = 0; hist.pushed = 0; };

/* ---------- 假接口：记录每次请求的参数，返回 mock-data 的内容 ---------- */
const calls = [];
const META = { ...mockJson('meta-u4'), personal_mode: false };
const SCAN = mockJson('market-scan');
const TRADE = mockJson('trade_list');
let scanTotal = null;   // 非 null 时 /api/scan 返回这个总数（用来测页码越界）
globalThis.fetch = async (path) => {
  const [p, qs = ''] = String(path).split('?');
  const q = Object.fromEntries(new URLSearchParams(qs));
  calls.push({ p, q });
  const ok = (body) => ({ ok: true, status: 200, json: async () => body });
  if (p === '/api/meta') return ok(META);
  if (p === '/api/main_strategy') return ok({ main: 'blend2' });
  if (p === '/api/trade_list_dates') return ok(mockJson('trade_list_dates'));
  if (p === '/api/trade_list') return ok(TRADE);
  if (p === '/api/table') return ok(mockJson('watch-table'));
  if (p === '/api/scan') {
    const total = scanTotal ?? SCAN.rows.length;
    return ok({ ...SCAN, total, page: Math.max(0, Number(q.page) || 0), size: 50, boards: ['主板', '创业板'], industries: ['银行', '电池'] });
  }
  return ok({});
};
const lastCall = (p) => [...calls].reverse().find((c) => c.p === p)?.q ?? null;
const callsTo = (p) => calls.filter((c) => c.p === p).length;

/* ---------- 工具 ---------- */
const wait = (ms = 20) => new Promise((r) => setTimeout(r, ms));
const params = () => Object.fromEntries(new URLSearchParams(loc.search));
const ctl = { n: 0 };
const fresh = (rel) => import(new URL(`${rel}?run=${++ctl.n}`, uiUrl));   // 加 ?run=N：每个场景拿一份全新的页面模块（模块变量 S 重新初始化）
const byTag = (root, tag) => root.descendants().filter((e) => e.tag === tag);
const sortButton = (root, k) => root.querySelector(`th button[data-k="${k}"]`) ?? byTag(root, 'button').find((b) => b.className.includes('table__sort') && b.kids[0]?.textContent === k);
const activeSortTh = (root) => byTag(root, 'th').find((th) => th.attrs['aria-sort'] && th.attrs['aria-sort'] !== 'false') ?? null;
const out = {};

const i18n = await import(new URL('shared/i18n.js', uiUrl));
// 字典：读基础字典 + 全部 parts，保证 t() 有中文可显示（探针只关心网址，不关心文案）
{
  const base = JSON.parse(fs.readFileSync(`${ui}/i18n/zh.json`, 'utf8'));
  const saved = globalThis.fetch;
  globalThis.fetch = async () => ({ ok: true, json: async () => base });
  try { await i18n.initI18n(); } catch { /* 字典加载失败时 t() 退回键名，不影响本探针 */ }
  globalThis.fetch = saved;
}

/* ===== url-state.js 纯函数 ===== */
const us = await import(new URL('shared/url-state.js', uiUrl));
out.us = {
  merge_keep_other: us.mergeSearch('?mock=1&mk_s=old&mk_o=a&foo=bar', 'mk', { s: 'cagr', o: 'd', q: '' }),
  merge_clear_all: us.mergeSearch('?mk_s=old&wl_s=keep', 'mk', {}),
  merge_empty: us.mergeSearch('', 'mk', { s: '', o: null, p: undefined, x: false }),
  merge_encode: us.mergeSearch('', 'mk', { q: '银 行&=#' }),
  pick: us.pickState('mk', '?mk_s=cagr&wl_s=x&mk_q=%E9%93%B6&foo=1'),
  has_yes: us.hasState('mk', '?mk_s=1'), has_no: us.hasState('mk', '?wl_s=1&market=us'),
  date_ok: us.validDate('2024-02-29'), date_feb30: us.validDate('2026-02-30'), date_fmt: us.validDate('2026/10/29'), date_null: us.validDate(null),
  nondef_same: us.nonDefault('a', 'a'), nondef_diff: us.nonDefault('b', 'a'),
  dir_to: [us.dirToUrl(true), us.dirToUrl(false)], dir_from: [us.dirFromUrl('a'), us.dirFromUrl('d'), us.dirFromUrl('x'), us.dirFromUrl(undefined)],
};
// writeState：保留 hash 和其他参数，不新增历史
setUrl('/w/market.html', '?mock=0&foo=1&mk_s=old', '#top');
us.writeState('mk', { s: 'cagr', o: 'a' });
out.us.write = { search: loc.search, hash: loc.hash, replaced: hist.replaced, pushed: hist.pushed };
us.writeState('mk', { s: 'cagr', o: 'a' });   // 没变化：不再调用 replaceState
out.us.write_same_replaced = hist.replaced;
hist.replaceState = () => { throw new Error('SecurityError'); };   // 浏览器限制调用频率时抛错：不能让页面挂掉
let threw = false;
try { us.writeState('mk', { s: 'x' }); } catch { threw = true; }
out.us.write_throw_swallowed = !threw;
hist.replaceState = function (_s, _t, url) { this.replaced++; const u = new URL(url, 'http://127.0.0.1:8765'); loc.pathname = u.pathname; loc.search = u.search; loc.hash = u.hash; };

/* ===== market.js ===== */
{
  const m = await fresh('pages/market.js');
  out.market_unit = {
    from_ok: m.stateFromUrl({ t: 'rsi2', q: '银行', b: '主板', i: '银行', g: '买入', s: 'cagr', o: 'a', p: '3' }, META),
    from_bad: m.stateFromUrl({ t: 'evil', q: 123, g: 'zzz', s: 'drop;table', o: 'x', p: '-3' }, META),
    from_page_zero: m.stateFromUrl({ p: '0' }, META), from_page_big: m.stateFromUrl({ p: '9999999' }, META), from_page_float: m.stateFromUrl({ p: '2.5' }, META),
    from_buy_hold: m.stateFromUrl({ t: 'buy_hold' }, META), from_raw: m.stateFromUrl({ t: 'raw', s: 'mcap' }, META),
    long_q: m.stateFromUrl({ q: 'x'.repeat(100) }, META).q.length,
    to_default: m.stateToUrl({ strategy: 'blend2', q: '', board: '', industry: '', signal: '', sort: 'sharpe', dir: 'desc', page: 0 }, 'cn'),
    to_full: m.stateToUrl({ strategy: 'rsi2', q: '银', board: '主板', industry: '银行', signal: '买入', sort: 'cagr', dir: 'asc', page: 2 }, 'us'),
    to_dir_only: m.stateToUrl({ strategy: 'blend2', q: '', board: '', industry: '', signal: '', sort: 'sharpe', dir: 'asc', page: 0 }, 'cn'),
  };
}
{   // 场景 1：从网址恢复全部筛选 → 接口收到对应参数，地址栏里别的参数保留
  calls.length = 0; scanTotal = 120;
  setUrl('/w/market.html', '?foo=1&mk_t=rsi2&mk_q=%E9%93%B6&mk_b=%E4%B8%BB%E6%9D%BF&mk_i=%E9%93%B6%E8%A1%8C&mk_g=%E4%B9%B0%E5%85%A5&mk_s=cagr&mk_o=a&mk_p=2', '#h');
  const m = await fresh('pages/market.js');
  const root = new FEl('div');
  await m.render(root); await wait();
  out.market_restore = { scan: lastCall('/api/scan'), url: params(), hash: loc.hash, pushed: hist.pushed, sortTh: activeSortTh(root)?.attrs['aria-sort'] };
  // 点另一列表头 → 网址跟着变；再点同列 → 方向翻转；点回默认的 sharpe（降序）→ 排序参数消失
  sortButton(root, 'd1')?.click(); await wait();
  out.market_click1 = { url: params(), scan: lastCall('/api/scan') };
  sortButton(root, 'd1')?.click(); await wait();
  out.market_click2 = { url: params() };
  sortButton(root, 'sharpe')?.click(); await wait();
  out.market_click3 = { url: params(), hash: loc.hash, pushed: hist.pushed, replaced_gt0: hist.replaced > 0 };
  // 语言切换 / R 刷新会再调用一次 render：状态保留，不被网址旧值覆盖
  await m.render(root); await wait();
  out.market_rerender = { scan: lastCall('/api/scan'), url: params() };
}
{   // 场景 2：网址里全是垃圾值 → 一律退回默认，且打开页面本身不改地址栏
  calls.length = 0; scanTotal = null;
  setUrl('/w/market.html', '?mk_t=evil&mk_s=x%3Bdrop&mk_o=zzz&mk_p=-3&mk_g=zzz&mk_m=nope');
  const m = await fresh('pages/market.js');
  const root = new FEl('div');
  await m.render(root); await wait();
  out.market_bad = { scan: lastCall('/api/scan'), url: params() };
}
{   // 场景 3：没有任何 mk_ 参数 → 打开页面不写地址栏；动了控件之后才写（含 mk_m）
  calls.length = 0; scanTotal = null;
  setUrl('/w/market.html', '?mock=0');
  const m = await fresh('pages/market.js');
  const root = new FEl('div');
  await m.render(root); await wait();
  out.market_plain_load = { search: loc.search, replaced: hist.replaced };
  sortButton(root, 'cagr')?.click(); await wait();
  out.market_plain_click = { url: params(), replaced_gt0: hist.replaced > 0 };
  const search = byTag(root, 'input').find((i) => i.attrs.type === 'search');
  search.value = '平安'; search.fire('input'); await wait(400);
  out.market_plain_search = { url: params(), scan: lastCall('/api/scan') };
  // 切到美股：网址里的市场跟着变，板块 / 行业 / 页码清零；随后（语言切换会触发的）重新渲染用网址里的市场
  byTag(root, 'button').find((b) => b.className.includes('segmented__btn') && b.textContent === '美股').click(); await wait();
  out.market_switch = { url: params() };
  await m.render(root); await wait();
  out.market_switch_render = { scan: lastCall('/api/scan') };
}
{   // 场景 4：页码越界 / 不存在的板块 → 纠正后重取一次，网址同步纠正
  calls.length = 0; scanTotal = 3;
  setUrl('/w/market.html', '?mk_p=99&mk_b=%E4%B8%8D%E5%AD%98%E5%9C%A8&mk_i=%E4%B9%B1%E5%86%99');
  const m = await fresh('pages/market.js');
  const root = new FEl('div');
  await m.render(root); await wait();
  out.market_clamp = { scan_calls: callsTo('/api/scan'), scan: lastCall('/api/scan'), url: params() };
  scanTotal = null;
}
{   // 场景 5：网址指定美股 → 接口 market=us；mk_m 非法 → 退回默认 cn
  calls.length = 0;
  setUrl('/w/market.html', '?mk_m=us');
  const m = await fresh('pages/market.js');
  const root = new FEl('div');
  await m.render(root); await wait();
  out.market_us = { scan: lastCall('/api/scan') };
}

/* ===== watch.js ===== */
{
  const w = await fresh('pages/watch.js');
  out.watch_unit = {
    from_ok: w.stateFromUrl({ t: 'rsi2', d: '2024-01-31', s: 'cagr', o: 'a' }, META),
    from_sort_no_dir: w.stateFromUrl({ s: 'sharpe' }, META),
    from_bad: w.stateFromUrl({ t: 'evil', d: '2026-02-31', s: 'symbol', o: 'a' }, META),
    to_default: w.stateToUrl({ strategy: 'blend2', start: '', sort: null, dir: -1 }, 'cn'),
    to_full: w.stateToUrl({ strategy: 'rsi2', start: '2024-01-31', sort: 'cagr', dir: 1 }, 'us'),
  };
}
{
  calls.length = 0;
  setUrl('/w/watch.html', '?foo=1&wl_t=rsi2&wl_d=2024-01-31&wl_s=cagr&wl_o=a');
  const w = await fresh('pages/watch.js');
  const root = new FEl('div');
  await w.render(root); await wait();
  const firstCol = () => byTag(root, 'tbody')[0].kids.map((tr) => tr.kids[0].kids.find((k) => k.tag === 'a')?.textContent ?? '');
  out.watch_restore = { table: lastCall('/api/table'), url: params(), order: firstCol(), sortTh: activeSortTh(root)?.attrs['aria-sort'], replaced: hist.replaced };
  sortButton(root, 'sharpe')?.click(); await wait();
  out.watch_click1 = { url: params(), pushed: hist.pushed };
  sortButton(root, 'sharpe')?.click(); await wait();
  out.watch_click2 = { url: params() };
  const start = byTag(root, 'input').find((i) => i.attrs.type === 'date');
  start.value = '2023-05-06'; start.fire('change'); await wait();
  out.watch_date = { url: params(), table: lastCall('/api/table') };
  const strat = byTag(root, 'select')[0];
  strat.value = 'trend_ens'; strat.fire('change'); await wait();
  out.watch_strategy = { url: params(), table: lastCall('/api/table') };
  start.value = ''; start.fire('change'); await wait();
  out.watch_date_cleared = { url: params() };
  byTag(root, 'button').find((b) => b.className.includes('segmented__btn') && b.textContent === '美股').click(); await wait();
  out.watch_switch = { url: params() };
  await w.render(root); await wait();
  out.watch_switch_render = { table: lastCall('/api/table') };
}
{
  calls.length = 0;
  setUrl('/w/watch.html', '?wl_d=2026-02-31&wl_t=evil&wl_s=symbol');
  const w = await fresh('pages/watch.js');
  const root = new FEl('div');
  await w.render(root); await wait();
  out.watch_bad = { table: lastCall('/api/table'), url: params(), sortTh: activeSortTh(root) === null };
}

/* ===== signals.js ===== */
{
  const sg = await fresh('pages/signals.js');
  const ctx = { strategies: ['blend2', 'rsi2'], tabs: ['buy', 'sell', 'adjust'] };
  out.signals_unit = {
    from_ok: sg.stateFromUrl({ t: 'rsi2', d: '2026-10-22', k: 'sell', q: '平安', l: '正常', s: 'close', o: 'a' }, ctx),
    from_code_default_dir: sg.stateFromUrl({ s: 'code' }, ctx).view, from_other_default_dir: sg.stateFromUrl({ s: 'd1' }, ctx).view,
    from_bad: sg.stateFromUrl({ t: 'evil', d: '2026-13-01', k: 'hack', q: 5, l: 'x', s: 'reason', o: 'a' }, ctx),
    from_unsortable_flags: sg.stateFromUrl({ s: 'flags' }, ctx),
    long_q: sg.stateFromUrl({ q: 'y'.repeat(99) }, ctx).q.length,
    to_auto: sg.stateToUrl({ strategy: 'blend2', date: '', tab: 'buy', q: ' 平安 ', liq: '', view: { sort: 'code', dir: 1, manual: false } }),
    to_manual: sg.stateToUrl({ strategy: 'rsi2', date: '2026-10-22', tab: 'sell', q: '', liq: '正常', view: { sort: 'close', dir: -1, manual: true } }),
  };
}
{
  calls.length = 0;
  setUrl('/w/signals.html', '?foo=1&sg_t=trend_ens&sg_d=2026-10-22&sg_k=sell&sg_q=%E5%B9%B3%E5%AE%89&sg_l=%E6%AD%A3%E5%B8%B8&sg_s=close&sg_o=a');
  const sg = await fresh('pages/signals.js');
  const root = new FEl('div');
  await sg.render(root); await wait(60);
  const pressed = () => byTag(root, 'button').filter((b) => b.attrs.role === 'tab' && b.attrs['aria-pressed'] === 'true').map((b) => b.dataset.tab);
  const searchBox = () => byTag(root, 'input').find((i) => i.attrs.type === 'search');
  out.signals_restore = { list: lastCall('/api/trade_list'), pressed: pressed(), search: searchBox()?.attrs.value, sortTh: activeSortTh(root)?.attrs['aria-sort'], replaced: hist.replaced };
  const tabBtn = (k) => byTag(root, 'button').find((b) => b.dataset.tab === k);
  tabBtn('buy').click(); await wait();
  out.signals_tab = { url: params(), pushed: hist.pushed };      // 换标签：手动排序被重置，sg_s/sg_o 消失
  // 桌面表格表头：每次重绘后重新取（旧节点已被替换）；第 1 列是「代码」，第 2 列是「收盘价」（排序键 close）
  const heads = () => byTag(root, 'th').flatMap((th) => th.kids.filter((x) => x.tag === 'button' && x.className.includes('table__sort')));
  heads()[1].click(); await wait();
  out.signals_sort1 = { url: params() };
  heads()[1].click(); await wait();
  out.signals_sort2 = { url: params() };
  heads()[0].click(); await wait();
  out.signals_sort3 = { url: params() };
  const search = searchBox();
  search.value = '600'; search.fire('input');
  out.signals_search_immediate = params().sg_q ?? null;       // 防抖：刚输入时网址还没写
  await wait(350);
  out.signals_search_later = params().sg_q ?? null;
  const selects = byTag(root, 'select');
  const liq = selects.find((s) => s.options.some((o) => o.attrs.value === '流动性差'));
  liq.value = '流动性差'; liq.fire('change'); await wait();
  out.signals_liq = { url: params() };
  const dateSel = selects.find((s) => s.options.some((o) => o.attrs.value === '2026-10-21'));
  const before = callsTo('/api/trade_list');
  dateSel.value = '2026-10-21'; dateSel.fire('change'); await wait(60);
  out.signals_date = { url: params(), reloaded: callsTo('/api/trade_list') - before, list: lastCall('/api/trade_list') };
}
{
  calls.length = 0;
  setUrl('/w/signals.html', '?sg_t=evil&sg_d=2099-01-01&sg_k=hack&sg_l=x&sg_s=flags');
  const sg = await fresh('pages/signals.js');
  const root = new FEl('div');
  await sg.render(root); await wait(60);
  out.signals_bad = { list: lastCall('/api/trade_list'), replaced: hist.replaced };
}
{   // 网址里的日期是合法格式但存档里没有 → render 清成「最新」（date 参数为空）
  calls.length = 0;
  setUrl('/w/signals.html', '?sg_d=2020-01-01');
  const sg = await fresh('pages/signals.js');
  const root = new FEl('div');
  await sg.render(root); await wait(60);
  out.signals_unknown_date = { list: lastCall('/api/trade_list') };
}

console.log(JSON.stringify(out));
