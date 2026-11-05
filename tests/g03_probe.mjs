// G03 探针：在 node 里用一套极简假 DOM + 假 fetch 真的渲染今日页（pages/today.js），按场景读出首屏结构，
// 以 JSON 打印给 tests/test_today_brief_g03.py 断言。不联网、不读 data/；接口返回用 dashboard/ui/mock-data 的假数据（只读），
// 个别场景在内存里改几个字段。
// 用法：node --experimental-default-type=module g03_probe.mjs <dashboard/ui 绝对路径> <base|merged>
//   base   = 基础字典原样（G03 词条还没合并：检验不露键名、标题退回已有词条）
//   merged = 基础字典叠上全部 parts（模拟早上合并之后）
import fs from 'node:fs';

const [ui, mode = 'base'] = process.argv.slice(2);
const uiUrl = new URL(`file://${ui}/`);
const mockJson = (name) => JSON.parse(fs.readFileSync(`${ui}/mock-data/${name}.json`, 'utf8'));
const clone = (x) => JSON.parse(JSON.stringify(x));

/* ---------- 极简假 DOM（只实现今日页用到的部分）---------- */
class FNode { constructor() { this.parent = null; } get textContent() { return ''; } }
class FText extends FNode { constructor(s) { super(); this.s = String(s); } get textContent() { return this.s; } }
class FEl extends FNode {
  constructor(tag) { super(); this.tag = tag; this.kids = []; this.attrs = {}; this.dataset = {}; this.className = ''; this.style = { setProperty() {} }; }
  setAttribute(k, v) { this.attrs[k] = v; }
  getAttribute(k) { return this.attrs[k] ?? null; }
  append(...c) { for (const x of c) { x.parent = this; this.kids.push(x); } }
  replaceChildren(...c) { this.kids = []; this.append(...c); }
  set textContent(s) { this.replaceChildren(new FText(s)); }
  get textContent() { return this.kids.map((k) => k.textContent).join(''); }
  addEventListener() {}
  has(cls) { return this.className.split(/\s+/).includes(cls); }
  descendants() { return this.kids.flatMap((k) => (k instanceof FEl ? [k, ...k.descendants()] : [])); }
  find(cls) { return this.descendants().filter((d) => d.has(cls)); }
  querySelector() { return null; }
  closest() { return main; }
}
const main = new FEl('main');
main.querySelector = () => new FEl('span');   // 页头的 data-bind 位置：页面往里写文字，探针不关心
globalThis.Node = FNode;
globalThis.document = { createElement: (t) => new FEl(t), createTextNode: (s) => new FText(s), addEventListener() {},
  documentElement: { dataset: {}, lang: '' }, querySelectorAll: () => [] };
const mem = new Map();
globalThis.localStorage = { getItem: (k) => (mem.has(k) ? mem.get(k) : null), setItem: (k, v) => mem.set(k, String(v)) };
const loc = { search: '', hostname: '127.0.0.1', pathname: '/ui/w/today.html' };
globalThis.window = { location: loc, matchMedia: () => ({ matches: false, addEventListener() {} }), dispatchEvent() {}, addEventListener() {} };
globalThis.CustomEvent = class { constructor(type, o) { this.type = type; this.detail = o?.detail; } };

/* ---------- 假 fetch：字典读文件（merged 时叠上 parts）；/api/* 读 api 表，记录调用 ---------- */
const PARTS = (lang) => fs.readdirSync(`${ui}/i18n/parts`).filter((f) => f.endsWith(`.${lang}.json`))
  .reduce((acc, f) => ({ ...acc, ...JSON.parse(fs.readFileSync(`${ui}/i18n/parts/${f}`, 'utf8')) }), {});
let api = {};
const calls = [];
globalThis.fetch = async (url) => {
  const s = String(url);
  if (s.startsWith('file:')) {
    const path = new URL(s).pathname;
    const body = JSON.parse(fs.readFileSync(path, 'utf8'));
    const m = /\/i18n\/(zh|en)\.json$/.exec(path);
    if (m && mode === 'merged') Object.assign(body, PARTS(m[1]));
    return { ok: true, status: 200, json: async () => body };
  }
  const p = s.split('?')[0];
  calls.push(s);
  const v = api[p];
  if (v && v.__status) return { ok: false, status: v.__status, json: async () => ({ error: `HTTP ${v.__status}` }) };
  return { ok: true, status: 200, json: async () => clone(v ?? {}) };
};

const i18n = await import(new URL('shared/i18n.js', uiUrl));
await i18n.setLang('zh');
const brief = await import(new URL('pages/today-brief.js', uiUrl));

/* ---------- 场景数据 ---------- */
const TRADE = mockJson('trade_list');
const codeOf = (a) => TRADE.rows.find((r) => r.action === a).code;
const POSITIONS = [   // 7 只：卖出、减仓、异常、加仓、买入、两只清单无信号（多于 SHOW=5，后两只进折叠）
  { code: '900002', name: '无信号乙', qty: 100 }, { code: codeOf('买入'), name: '买入股', qty: 100 },
  { code: codeOf('加仓'), name: '加仓股', qty: 200 }, { code: TRADE.exceptions[0].code, name: '停牌股', qty: 300 },
  { code: codeOf('减仓'), name: '减仓股', qty: 400 }, { code: codeOf('卖出'), name: '卖出股', qty: 500 },
  { code: '900001', name: '无信号甲', qty: 600 },
];
const baseApi = () => ({
  '/api/meta': mockJson('meta'), '/api/summary': mockJson('summary'), '/api/tracking': mockJson('tracking'),
  '/api/main_strategy': mockJson('main_strategy'), '/api/trade_list': clone(TRADE),
  '/api/member/me': mockJson('member_me'), '/api/member/portfolio': { ...mockJson('member_portfolio'), positions: clone(POSITIONS) },
});
const T_FRESH = Date.parse('2026-10-24T02:00:00Z');   // 北京时间周六 10:00：清单（10-23）不到 3 天
const T_STALE = Date.parse('2026-10-30T02:00:00Z');   // 7 天后：清单过旧
const realNow = Date.now;

/* ---------- 读出首屏结构 ---------- */
const title = (card) => card.find('card__title')[0]?.textContent ?? null;
function snapshot(root) {
  const top = root.kids.map((k) => k.className);
  const grid = root.kids.find((k) => k.has('u1-brief'));
  const cards = grid ? grid.kids : [];
  const pick = (cls) => cards.find((c) => c.has(cls)) ?? null;
  const act = pick('u1-actions'), pos = pick('u1-pos'), risk = pick('u1-risk');
  const tiles = act ? act.find('metric') : [];
  const items = (card, cls) => (card ? card.find(cls).flatMap((u) => u.kids.map((li) => li)) : []);
  const posLis = items(pos, 'u1-pos__list');
  return {
    top, grid_class: grid?.className ?? null, card_classes: cards.map((c) => c.className), titles: cards.map(title),
    counts: Object.fromEntries(tiles.map((m) => [m.dataset.tab ?? 'exc', m.find('metric__value')[0]?.textContent ?? null])),
    exc_warn: tiles.find((m) => !m.dataset.tab)?.find('metric__value')[0]?.has('is-warn') ?? null,
    act_links: tiles.filter((m) => m.tag === 'a').map((m) => m.attrs.href),
    act_text: act?.textContent ?? null,
    act_has_dateline: act ? act.find('u1-datebar').length : null,
    pos_text: pos?.textContent ?? null,
    pos_move: pos ? pos.find('u1-pos__move')[0]?.textContent ?? null : null,
    pos_items: posLis.map((li) => ({ code: li.kids[0]?.textContent, badge: li.kids[3]?.textContent ?? null })),
    pos_visible: pos ? (pos.find('u1-pos__list')[0]?.kids.length ?? 0) : 0,
    pos_fold: pos ? pos.descendants().filter((d) => d.tag === 'summary').map((d) => d.textContent) : [],
    risk_warn: risk ? risk.has('u1-risk--on') : null,
    risk_role: risk ? risk.attrs.role ?? null : null,
    risk_items: risk ? risk.find('u1-risk__list').flatMap((u) => u.kids.map((li) => li.textContent)) : [],
    risk_text: risk?.textContent ?? null,
    all_text: root.textContent,
  };
}

let run = 0;
async function scenario(name, { patch = () => {}, now = T_FRESH, host = '127.0.0.1', market = null } = {}) {
  api = baseApi(); patch(api); calls.length = 0; mem.clear();
  if (market) mem.set('qb-ui-market', market);
  loc.hostname = host;
  Date.now = () => now;
  const page = await import(new URL(`pages/today.js?run=${++run}`, uiUrl));   // 每个场景一份新模块（模块变量 market 重置）
  const root = new FEl('div');
  root.closest = () => main;
  let error = null;
  try { await page.render(root); } catch (e) { error = String(e?.message ?? e); }
  Date.now = realNow;
  return { error, calls: calls.map((c) => c.split('?')[0]), ...snapshot(root) };
}

const out = { mode };
out.personal = await scenario('personal');
out.stale = await scenario('stale', { now: T_STALE });
out.nonpersonal = await scenario('nonpersonal', { patch: (a) => { a['/api/meta'] = { ...a['/api/meta'], personal_mode: false }; } });
out.remote = await scenario('remote', { host: 'me.example.test' });
out.list_error = await scenario('list_error', { patch: (a) => { a['/api/trade_list'] = { error: '示例：清单还没生成' }; } });
out.auth = await scenario('auth', { patch: (a) => { a['/api/trade_list'] = { __status: 401 }; } });
out.clean = await scenario('clean', { patch: (a) => {
  a['/api/summary'] = { ...a['/api/summary'], health: [] };
  a['/api/trade_list'] = { ...a['/api/trade_list'], exceptions: [], price_unavailable: 0 };
  a['/api/member/portfolio'] = { ...a['/api/member/portfolio'], positions: [{ code: '900001', name: '无信号甲', qty: 100 }] };
} });
out.lagging = await scenario('lagging', { patch: (a) => { a['/api/trade_list'] = { ...a['/api/trade_list'], data_date: '2026-10-22' }; } });
out.us = await scenario('us', { market: 'us', patch: (a) => {
  a['/api/meta'] = { ...a['/api/meta'], markets: { ...a['/api/meta'].markets, us: { name: 'US', main: 'SPY', timezone_label: 'ET' } } };
} });

/* ---------- 纯函数 ---------- */
out.pure = {
  match_null: brief.matchPositions([{ code: '1' }], null).map((p) => p.sig),
  match_order: brief.matchPositions(clone(POSITIONS), TRADE).map((p) => [p.code, p.sig]),
  match_numeric_code: brief.matchPositions([{ code: 600000 }], TRADE).map((p) => p.sig),
  stale_fresh: brief.listStale({ data_date: '2026-10-23' }, T_FRESH),
  stale_old: brief.listStale({ data_date: '2026-10-23' }, T_STALE),
  stale_bad: brief.listStale({ data_date: 'xx' }, T_FRESH),
  usable: [brief.usableList(null), brief.usableList({ error: 'x' }), brief.usableList({ empty: true }), Boolean(brief.usableList({ rows: [] }))],
  risk_order: brief.riskItems({ s: { health: ['H'] }, list: { data_date: '2026-10-23', exceptions: [{}], price_unavailable: 2 },
    held: [{ code: 'A', sig: '卖出' }, { code: 'B', sig: 'exc' }], now: T_STALE }),
  risk_empty: brief.riskItems({ s: { health: [] }, list: null, held: [] }),
  g3_missing: brief.g3('g03.not.a.key', {}, 'FB'),
};
console.log(JSON.stringify(out));
