/* ==========================================================
 * 文件: ui/pages/watch.js —— 自选（旧页 /#/watch）
 * 接口: GET /api/meta、GET /api/table、POST /api/watchlist（add / remove）
 * 内容: 策略 + 开始日期、添加自选（代码格式校验）、11 列可排序表、移除、下载中自动刷新、导出 CSV、点行进个股详情
 * 网址(A08): 排序 / 筛选同步到 location.search（replaceState，不产生历史记录），打开页面时还原。参数前缀 wl_：
 *   m=市场 t=策略 d=开始日期 s=排序列 o=方向(a升 d降)；等于默认值的不写（市场总是写）
 * ========================================================== */

import { el } from '../shared/dom.js';
import { t } from '../shared/i18n.js';
import { setAriaLabel, tMaybe } from '../shared/a11y.js';
import { tDefault } from '../shared/t-fallback.js';
import { stockPage, rememberStockStrategy } from '../shared/stock-link.js';
import { getJSON, postJSON } from '../shared/api.js';
import { pickMarket, marketSwitch, fmt, pct, pill, strategySelect, field, sortTh, showError, downloadCSV, toneClass } from './u4-util.js';
import { pickState, writeState, nonDefault, dirToUrl, dirFromUrl, validDate } from '../shared/url-state.js';

const DEFAULT_STRATEGY = 'blend2';
const W = { strategy: DEFAULT_STRATEGY, start: '', sort: null, dir: -1 };
const NS = 'wl';          // 网址参数前缀
let restored = false;     // 网址只在第一次渲染时读一次；之后以 W 为准（只有用户动控件时才写回网址，光打开页面不改地址栏）
let meta = null;
let market = 'cn';
let rows = [];
let ui = null;
let timer = 0;

const COLS = [['name', 'u4.w.c.name'], ['d1', 'u4.c.d1'], ['next', 'u4.c.next'], ['cagr', 'u4.c.cagr'], ['sharpe', 'u4.c.sharpe'], ['maxdd', 'u4.c.maxdd'],
  ['y1', 'u4.c.y1'], ['win', 'u4.c.win'], ['trades', 'u4.w.c.trades'], ['b_cagr', 'u4.c.b_cagr'], ['b_maxdd', 'u4.c.b_maxdd']];

/** 网址 → 状态。网址是不可信输入：策略对照 meta、排序列对照表头、日期必须是真实存在的 YYYY-MM-DD，不合法的丢掉。
 *  排序只有「列 + 方向」成对出现才恢复；只有 wl_s 没有 wl_o 时按表格默认的降序 */
export function stateFromUrl(u, meta) {
  const out = {};
  const strategies = Object.entries(meta?.strategies ?? {}).filter(([k, v]) => v.kind === 'single' && k !== 'buy_hold').map(([k]) => k);
  if (strategies.includes(u.t)) out.strategy = u.t;
  if (validDate(u.d)) out.start = u.d;
  if (COLS.some(([k]) => k === u.s)) { out.sort = u.s; out.dir = dirFromUrl(u.o) === 'asc' ? 1 : -1; }
  return out;
}

/** 状态 → 网址参数（只含和默认不同的） */
export function stateToUrl(w, mkt) {
  return { m: mkt, t: nonDefault(w.strategy, DEFAULT_STRATEGY), d: w.start, s: w.sort ?? '', o: w.sort ? dirToUrl(w.dir === 1) : '' };
}

const syncUrl = (mkt = market) => writeState(NS, stateToUrl(W, mkt));

/** 代码格式：A 股 6 位（60/68/00/30 开头）；美股字母开头 */
const validCode = (c) => (market === 'cn' ? /^(60|68|00|30)\d{4}$/.test(c) : /^[A-Za-z][A-Za-z0-9.-]{0,9}$/.test(c));

function say(text) { ui.msg.hidden = !text; ui.msg.textContent = text; }

/** 添加 / 移除成功后的回执（G01）：和「计算中…」那条状态分开放，重新取数时不会被冲掉。
 *  以前添加、移除都没有任何文字反馈，移除还是一点就生效、没有退路；现在移除后给一个「撤销」（重新添加回来）。
 *  词条还没合并进基础字典时 tMaybe 返回 null，这条回执不显示（和以前一样），不露键名 */
function notice(text, undo = null) {
  ui.note.hidden = !text;
  ui.note.replaceChildren(...(text ? [text] : []), ...(text && undo ? [' ', undo] : []));
}

function undoButton(r) {
  const label = tMaybe('g01.w.undo');
  if (!label) return null;
  const b = setAriaLabel(el('button', { class: 'u4-link', type: 'button' }, label), 'g01.w.undo_aria', { n: r.name });
  b.addEventListener('click', async () => {
    b.disabled = true;
    if (await tryChange('add', r.symbol)) {
      notice(tMaybe('g01.w.restored', { n: r.name }));
      await load().catch((er) => showError(ui.msg, er));
    } else b.disabled = false;
  });
  return b;
}

/** 接口返回 HTTP 200 + {error} 时 postJSON 不抛错，这里统一再看一眼 */
async function tryChange(action, code) {
  try {
    const r = await postJSON(`/api/watchlist?market=${market}&action=${action}&code=${encodeURIComponent(code)}`);
    if (r?.error) { say(r.error); return false; }
    return true;
  } catch (err) { showError(ui.msg, err); return false; }
}

function drawTable() {
  const list = [...rows];
  if (W.sort) list.sort((a, b) => ((a[W.sort] ?? -1e9) > (b[W.sort] ?? -1e9) ? 1 : -1) * W.dir);
  const state = { sort: W.sort, dir: W.dir === 1 ? 'asc' : 'desc' };
  const thead = el('thead', {}, el('tr', {}, ...COLS.map(([k, n], i) => sortTh(t(n), k, state, i > 0)), el('th', { scope: 'col' })));
  const body = list.map((r) => {
    const name = el('td', {}, el('a', { href: stockPage(r.symbol, market) }, r.name), el('span', { class: 'u4-code' }, r.symbol), el('span', { class: 'badge u4-tag' }, r.kind === '个股' ? t('u4.w.kind_stock') : r.kind));
    let del = el('td');
    if (r.kind === '个股') {
      const b = setAriaLabel(el('button', { class: 'u4-link u4-link--remove', type: 'button' }, t('u4.w.remove')), 'a11y.watch_remove', { n: r.name });   // 每行都叫「移除」，读屏要带上是哪只
      b.addEventListener('click', async (e) => {
        e.stopPropagation();
        if (!await tryChange('remove', r.symbol)) return;
        notice(tMaybe('g01.w.removed', { n: r.name }), undoButton(r));
        await load().catch((er) => showError(ui.msg, er));
      });
      del = el('td', {}, b);
    }
    if (r.missing || r.error) {
      return el('tr', {}, name, el('td', { colspan: String(COLS.length - 1), class: 'muted' }, r.error ? r.error : t('u4.w.downloading')), del);
    }
    const tr = el('tr', { class: `u4-click ${r.kind === '个股' ? 'u4-pin' : ''}` }, name,
      el('td', { class: `num ${toneClass(r.d1)}` }, pct(r.d1, 2)),
      el('td', { class: 'num' }, fmt(r.next * 100, 0, '%'), ' ', pill(r.action)),
      el('td', { class: `num ${r.cagr > r.b_cagr ? 'u4-better' : ''}` }, fmt(r.cagr, 1, '%')),
      el('td', { class: `num ${r.sharpe > r.b_sharpe ? 'u4-better' : ''}` }, fmt(r.sharpe)),
      el('td', { class: 'num' }, fmt(r.maxdd, 1, '%')),
      el('td', { class: `num ${toneClass(r.y1)}` }, pct(r.y1)),
      el('td', { class: 'num' }, fmt(r.win, 0, '%')),
      el('td', { class: 'num' }, r.trades ?? '—'),
      el('td', { class: 'num' }, fmt(r.b_cagr, 1, '%')),
      el('td', { class: 'num' }, fmt(r.b_maxdd, 1, '%')), del);
    tr.addEventListener('click', (e) => {
      if (e.target.closest('a,button')) return;
      rememberStockStrategy(W.strategy);      // 个股页默认用这里选的策略（新旧两套界面都写）
      window.location.href = stockPage(r.symbol, market);   // G01：去新个股页
    });
    return tr;
  });
  ui.table.replaceChildren(thead, el('tbody', {}, ...body));
  ui.table.querySelectorAll('th button[data-k]').forEach((b) => b.addEventListener('click', () => {
    const k = b.dataset.k;
    W.dir = W.sort === k ? -W.dir : -1;
    W.sort = k;
    syncUrl();
    drawTable();
  }));
}

/** 取数并重绘；有正在下载的品种就 4 秒后再来一次 */
async function load() {
  clearTimeout(timer);
  say(t('u4.w.calc'));
  const q = new URLSearchParams({ market, strategy: W.strategy, start: W.start });
  const r = await getJSON(`/api/table?${q}`, { mock: 'watch-table' });
  if (r.error) { say(r.error); return; }
  rows = r.rows ?? [];
  say(r.pending?.length ? t('u4.w.pending', { list: r.pending.join(tDefault('a02.sep.list')) }) : '');
  drawTable();
  if (r.pending?.length && document.body.contains(ui.table)) {
    timer = setTimeout(() => { if (document.body.contains(ui.table)) load().catch((e) => showError(ui.msg, e)); }, 4000);
  }
}

export async function render(root) {
  clearTimeout(timer);
  meta = await getJSON('/api/meta', { mock: 'meta-u4' });
  const u = pickState(NS);
  market = pickMarket(meta, u.m);
  if (!restored) { restored = true; Object.assign(W, stateFromUrl(u, meta)); }
  const again = () => load().catch((e) => showError(ui.msg, e));
  ui = {
    msg: el('p', { class: 'u4-msg', role: 'status' }),
    note: el('p', { class: 'u4-msg', role: 'status', hidden: true }),
    table: el('table', { class: 'table' }),
  };
  const start = el('input', { class: 'ws-input', type: 'date', 'aria-label': t('u4.w.start'), value: W.start });
  start.addEventListener('change', () => { W.start = start.value; syncUrl(); again(); });
  const code = el('input', { class: 'ws-input', maxlength: '10', placeholder: t(market === 'cn' ? 'u4.w.ph_cn' : 'u4.w.ph_us'), 'aria-label': t('u4.w.add_label') });
  const add = async () => {
    const c = code.value.trim();
    if (!validCode(c)) { say(t(market === 'cn' ? 'u4.w.bad_cn' : 'u4.w.bad_us')); return; }
    if (await tryChange('add', c)) { code.value = ''; notice(tMaybe('g01.w.added', { code: c })); again(); }
  };
  const addBtn = el('button', { class: 'btn btn--sm btn--primary', type: 'button' }, t('u4.w.add_btn'));
  addBtn.addEventListener('click', add);
  code.addEventListener('keydown', (e) => { if (e.key === 'Enter') add(); });
  const exp = el('button', { class: 'btn btn--sm', type: 'button' }, t('u4.m.export'));
  exp.addEventListener('click', () => {
    const lab = meta.strategies[W.strategy]?.label ?? W.strategy;
    downloadCSV(`${t('u4.w.file')}_${lab}.csv`, [['symbol', t('u4.x.code')], ['name', t('u4.x.name')], ['kind', t('u4.x.kind')], ['action', t('u4.x.signal')], [(x) => fmt((x.next || 0) * 100, 0), t('u4.x.next')],
      ['cagr', t('u4.x.cagr')], ['sharpe', t('u4.c.sharpe')], ['maxdd', t('u4.x.maxdd')], ['y1', t('u4.x.y1')], ['win', t('u4.x.win')], ['trades', t('u4.x.trades')], ['b_cagr', t('u4.x.b_cagr')], ['b_maxdd', t('u4.x.b_maxdd')]], rows);
  });
  const strat = strategySelect(meta, W.strategy, { onChange: (v) => { W.strategy = v; syncUrl(); again(); } });
  root.replaceChildren(
    el('div', { class: 'u4-bar', style: { padding: '0 0 var(--space-3)', border: '0' } }, marketSwitch(meta, market, (k) => { syncUrl(k); window.dispatchEvent(new CustomEvent('langchange')); })),
    el('section', { class: 'card card--flush', 'aria-label': t('ui.nav.watch') },
      el('div', { class: 'u4-bar' }, field('u4.f.strategy', strat), field('u4.w.start', start), field('u4.w.add_label', code), addBtn, el('span', { class: 'u4-bar__spacer' }), exp),
      ui.note, ui.msg,
      el('div', { class: 'table-wrap' }, ui.table),
      el('p', { class: 'ws-note', style: { padding: '12px var(--ws-card-pad)' } }, t(market === 'cn' ? 'u4.w.note_cn' : 'u4.w.note_us', { bp: meta.markets[market]?.stock_cost_bp ?? '—' }))));
  await load();
}
