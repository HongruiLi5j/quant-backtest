/* ==========================================================
 * 文件: ui/pages/market.js —— 全市场（旧页 /#/market）
 * 接口: GET /api/meta、GET /api/scan、POST /api/watchlist（加自选）
 * 内容: 策略在全市场有没有用（汇总表，点一行切换策略）+ 逐只结果（筛选、13 列排序、分页、导出 CSV、＋自选）
 * 状态: 筛选条件放在模块变量 S 里，语言切换 / R 刷新重绘时保留
 * ========================================================== */

import { el } from '../shared/dom.js';
import { t } from '../shared/i18n.js';
import { getJSON, postJSON } from '../shared/api.js';
import { dataMarket, marketSwitch, fmt, pct, isNum, capFmt, pill, strategySelect, field, sortTh, showError, downloadCSV, debounce, toneClass } from './u4-util.js';

const S = { strategy: 'blend2', q: '', board: '', industry: '', signal: '', sort: 'sharpe', dir: 'desc', page: 0 };
let meta = null;
let market = 'cn';
let watched = new Set();
let ui = null;            // 当前这次渲染创建出来的控件和容器
let last = null;          // 最近一次 /api/scan 的结果

/* 列: [字段, i18n 键] —— 顺序与旧页一致 */
const COLS = [['code', 'u4.c.stock'], ['d1', 'u4.c.d1'], ['next', 'u4.c.next'], ['cagr', 'u4.c.cagr'], ['sharpe', 'u4.c.sharpe'], ['maxdd', 'u4.c.maxdd'],
  ['sharpe_is', 'u4.c.is'], ['sharpe_oos', 'u4.c.oos'], ['y1', 'u4.c.y1'], ['win', 'u4.c.win'], ['b_cagr', 'u4.c.b_cagr'], ['b_sharpe', 'u4.c.b_sharpe'], ['b_maxdd', 'u4.c.b_maxdd']];
const RAW_SORT = ['code', 'd1', 'm1', 'y1', 'mcap'];
const SIGNALS = [['', 'u4.sig.all'], ['买入', 'u4.sig.buy'], ['卖出', 'u4.sig.sell'], ['持仓', 'u4.sig.hold'], ['空仓', 'u4.sig.flat'], ['有效', 'u4.sig.good'], ['退市', 'u4.sig.delisted']];

function query(extra = {}) {
  return new URLSearchParams({ market, strategy: S.strategy, q: S.q, board: S.board, industry: S.industry, signal: S.signal, sort: S.sort, dir: S.dir, page: S.page, ...extra });
}

const fetchScan = (extra) => getJSON(`/api/scan?${query(extra)}`, { mock: 'market-scan' });

async function addToWatch(code) {
  try {
    const r = await postJSON(`/api/watchlist?market=${market}&action=add&code=${encodeURIComponent(code)}`);
    if (r?.error) { ui.msg.hidden = false; ui.msg.textContent = r.error; } else { watched.add(code); ui.msg.hidden = true; }
  } catch (err) { showError(ui.msg, err); }
  drawRows();
}

/* ---------- 汇总表：策略在全市场有没有用 ---------- */
function drawSummary(m) {
  const head = ['u4.s.strategy', 'u4.s.beat', 'u4.s.is', 'u4.s.oos', 'u4.s.persist', 'u4.s.top'];
  if (m.delisted) head.push('u4.s.delisted');
  head.push('u4.s.buy', 'u4.s.sell', 'u4.s.holding');
  const rows = Object.entries(m.strategies ?? {}).map(([k, v]) => {
    const tr = el('tr', { class: 'u4-click', tabindex: '0', 'aria-selected': String(k === S.strategy) },
      el('td', {}, v.label),
      el('td', { class: `num ${v.beat_bh_pct > 50 ? 'u4-better' : ''}` }, fmt(v.beat_bh_pct, 0, '%')),
      el('td', { class: 'num' }, fmt(v.beat_is, 0, '%')),
      el('td', { class: `num ${v.beat_oos > 50 ? 'u4-better' : ''}` }, fmt(v.beat_oos, 0, '%')),
      el('td', { class: 'num' }, fmt(v.persistence, 2)),
      el('td', { class: 'num' }, isNum(v.top_pick_oos) ? [fmt(v.top_pick_oos, 2), ' ', el('span', { class: 'muted' }, `vs ${fmt(v.all_oos, 2)}`)] : '—'),
      m.delisted ? el('td', { class: 'num' }, fmt(v.with_delisted, 0, '%')) : null,
      el('td', { class: 'num' }, String(v.buy)), el('td', { class: 'num' }, String(v.sell)), el('td', { class: 'num' }, fmt(v.holding_pct, 0, '%')));
    const pick = () => { S.strategy = k; S.page = 0; ui.strategy.value = k; load().catch((e) => showError(ui.msg, e)); };
    tr.addEventListener('click', pick);
    tr.addEventListener('keydown', (e) => { if (e.key === 'Enter') pick(); });
    return tr;
  });
  ui.summary.replaceChildren(el('thead', {}, el('tr', {}, ...head.map((k, i) => el('th', { scope: 'col', class: i ? 'num' : null }, t(k))))), el('tbody', {}, ...rows));
  const split = m.split ? '2020' : null;
  ui.explain.replaceChildren(
    el('p', {}, el('strong', {}, t('u4.x.how')), ' ', t('u4.x.beat')),
    el('p', {}, split ? t('u4.x.split', { y: split }) : t('u4.x.half')),
    el('p', {}, el('strong', {}, t('u4.x.persist_h')), ' ', t('u4.x.persist'), ' ', el('strong', {}, t('u4.x.luck'))),
    el('p', {}, t('u4.x.top'), ' ', t('u4.x.click')));
}

/* ---------- 逐只结果表 ---------- */
function stockCell(x) {
  return el('td', {}, el('a', { href: `/#/stock/${encodeURIComponent(x.code)}` }, x.name),
    el('span', { class: 'u4-code' }, x.code), el('span', { class: 'badge u4-tag' }, x.board),
    x.industry ? el('span', { class: 'u4-sub' }, x.industry) : null);
}

function watchCell(x) {
  if (watched.has(x.code)) return el('td', {}, el('span', { class: 'u4-link u4-link--gray' }, t('u4.w.added')));
  const b = el('button', { class: 'u4-link', type: 'button' }, t('u4.w.add'));
  b.addEventListener('click', (e) => { e.stopPropagation(); addToWatch(x.code); });
  return el('td', {}, b);
}

function drawRows() {
  const r = last;
  if (!r) return;
  const raw = S.strategy === 'raw';
  const hasCap = r.rows.some((x) => isNum(x.mcap));
  if (raw && !RAW_SORT.includes(S.sort)) S.sort = hasCap ? 'mcap' : 'd1';
  const cols = raw
    ? [['code', 'u4.c.stock'], ...(hasCap ? [['mcap', 'u4.c.mcap']] : []), ['d1', 'u4.c.d1'], ['m1', 'u4.c.m1'], ['y1', 'u4.c.y1'], ['last_date', 'u4.c.date']]
    : (hasCap ? [COLS[0], ['mcap', 'u4.c.mcap'], ...COLS.slice(1)] : COLS);
  const thead = el('thead', {}, el('tr', {}, ...cols.map(([k, n], i) => (raw && k === 'last_date'
    ? el('th', { scope: 'col', class: 'num' }, t(n)) : sortTh(t(n), k, S, i > 0))), el('th', { scope: 'col' })));
  const body = r.rows.map((x) => {
    const cells = raw ? [
      stockCell(x), hasCap ? el('td', { class: 'num' }, capFmt(x.mcap)) : null,
      el('td', { class: `num ${toneClass(x.d1)}` }, pct(x.d1, 2)), el('td', { class: `num ${toneClass(x.m1)}` }, pct(x.m1, 1)),
      el('td', { class: `num ${toneClass(x.y1)}` }, pct(x.y1)), el('td', { class: 'num muted' }, x.last_date || ''),
    ] : [
      stockCell(x), hasCap ? el('td', { class: 'num' }, capFmt(x.mcap)) : null,
      el('td', { class: `num ${toneClass(x.d1)}` }, pct(x.d1, 2)),
      el('td', { class: 'num' }, fmt((x.next || 0) * 100, 0, '%'), ' ', pill(x.action)),
      el('td', { class: 'num' }, fmt(x.cagr, 1, '%')),
      el('td', { class: `num ${x.sharpe > x.b_sharpe ? 'u4-better' : ''}` }, fmt(x.sharpe)),
      el('td', { class: 'num' }, fmt(x.maxdd, 1, '%')),
      el('td', { class: 'num' }, fmt(x.sharpe_is)), el('td', { class: 'num' }, fmt(x.sharpe_oos)),
      el('td', { class: `num ${toneClass(x.y1)}` }, pct(x.y1)), el('td', { class: 'num' }, fmt(x.win, 0, '%')),
      el('td', { class: 'num' }, fmt(x.b_cagr, 1, '%')), el('td', { class: 'num' }, fmt(x.b_sharpe)), el('td', { class: 'num' }, fmt(x.b_maxdd, 1, '%')),
    ];
    const tr = el('tr', { class: `u4-click ${watched.has(x.code) ? 'u4-pin' : ''}` }, ...cells, watchCell(x));
    tr.addEventListener('click', (e) => { if (!e.target.closest('a,button')) window.location.href = `/#/stock/${encodeURIComponent(x.code)}`; });
    return tr;
  });
  ui.table.replaceChildren(thead, el('tbody', {}, ...body));
  ui.table.querySelectorAll('th button[data-k]').forEach((b) => b.addEventListener('click', () => {
    const k = b.dataset.k;
    S.dir = S.sort === k && S.dir === 'desc' ? 'asc' : 'desc';
    S.sort = k; S.page = 0;
    load().catch((e) => showError(ui.msg, e));
  }));
}

function fillSelect(sel, items, value) {
  sel.replaceChildren(el('option', { value: '' }, t('u4.f.all')), ...items.map((x) => el('option', { value: x }, x)));
  sel.value = items.includes(value) ? value : '';
}

/** 拉一次 /api/scan 并刷新页面上的各块；出错时往上抛 */
async function load() {
  const r = await getJSON(`/api/scan?${query()}`, { mock: 'market-scan' });
  ui.msg.hidden = true;
  if (r.empty) {
    ui.body.hidden = true;
    const cmd = market === 'us' ? '.venv/bin/python -m src.scan_us' : 'caffeinate -i .venv/bin/python backfill.py';
    ui.empty.hidden = false;
    ui.empty.replaceChildren(el('p', { class: 'state__title' }, t('u4.m.empty_title')),
      el('p', {}, t('u4.m.empty_a', { n: r.downloaded, listed: r.listed ? ` / ${r.listed}` : '' }), ' ', el('code', { class: 'u4-empty-cmd' }, cmd), ' ', t('u4.m.empty_b')));
    ui.meta.textContent = '';
    return;
  }
  ui.empty.hidden = true; ui.body.hidden = false; last = r;
  const m = r.meta;
  const excluded = m.excluded ? Object.values(m.excluded).reduce((a, b) => a + b, 0) : 0;
  ui.meta.textContent = [t('u4.m.date', { d: m.data_date }),
    m.excluded ? t('u4.m.count_ex', { n: m.stocks, x: excluded }) : t('u4.m.count_st', { n: m.stocks, x: m.excluded_st ?? 0 }),
    m.delisted ? t('u4.m.delisted', { n: m.delisted }) : null].filter(Boolean).join(' · ');
  drawSummary(m);
  if (ui.board.options.length <= 1) fillSelect(ui.board, r.boards ?? [], S.board);
  if (ui.industry.options.length <= 1) fillSelect(ui.industry, r.industries ?? [], S.industry);
  const pages = Math.max(1, Math.ceil(r.total / r.size));
  ui.count.textContent = t('u4.m.total', { n: r.total });
  ui.pageNo.textContent = `${r.page + 1} / ${pages}`;
  ui.prev.disabled = r.page <= 0;
  ui.next.disabled = r.page + 1 >= pages;
  drawRows();
}

async function exportCsv() {
  ui.exp.disabled = true;
  try {
    const r = await fetchScan({ export: 1 });
    const lab = S.strategy === 'raw' ? t('u4.f.raw') : (meta.strategies[S.strategy]?.label ?? S.strategy);
    downloadCSV(`${t('u4.m.file')}_${lab}_${r.meta.data_date}.csv`, [['code', t('u4.x.code')], ['name', t('u4.x.name')], ['board', t('u4.x.board')], ['industry', t('u4.x.industry')], ['action', t('u4.x.signal')],
      [(x) => fmt((x.next || 0) * 100, 0), t('u4.x.next')], ['cagr', t('u4.x.cagr')], ['sharpe', t('u4.c.sharpe')], ['maxdd', t('u4.x.maxdd')], ['sharpe_is', t('u4.c.is')],
      ['sharpe_oos', t('u4.c.oos')], ['y1', t('u4.x.y1')], ['win', t('u4.x.win')], ['trades', t('u4.x.trades')], ['b_cagr', t('u4.x.b_cagr')], ['b_sharpe', t('u4.c.b_sharpe')],
      ['b_maxdd', t('u4.x.b_maxdd')], ['last_date', t('u4.c.date')]], r.rows);
  } catch (err) { showError(ui.msg, err); } finally { ui.exp.disabled = false; }
}

export async function render(root) {
  meta = await getJSON('/api/meta', { mock: 'meta-u4' });
  market = dataMarket(meta);
  watched = new Set(Object.keys(meta.markets[market]?.stocks ?? {}));
  const again = () => load().catch((e) => showError(ui.msg, e));
  const sel = (items, key, onChange) => {
    const s = el('select', { class: 'ws-select', 'aria-label': t(key) }, ...items);
    s.addEventListener('change', () => onChange(s.value));
    return s;
  };
  ui = {
    meta: document.querySelector('[data-bind="meta"]'),
    msg: el('p', { class: 'u4-msg', role: 'alert', hidden: true }),
    empty: el('div', { class: 'card state', hidden: true }),
    summary: el('table', { class: 'table' }),
    explain: el('div', { class: 'u4-explain' }),
    table: el('table', { class: 'table' }),
    count: el('span', { class: 'u4-pager__count' }),
    pageNo: el('span', { class: 'num' }),
    prev: el('button', { class: 'btn btn--sm', type: 'button' }, t('u4.m.prev')),
    next: el('button', { class: 'btn btn--sm', type: 'button' }, t('u4.m.next')),
    exp: el('button', { class: 'btn btn--sm', type: 'button' }, t('u4.m.export')),
    board: sel([el('option', { value: '' }, t('u4.f.all'))], 'u4.f.board', (v) => { S.board = v; S.page = 0; again(); }),
    industry: sel([el('option', { value: '' }, t('u4.f.all'))], 'u4.f.industry', (v) => { S.industry = v; S.page = 0; again(); }),
  };
  ui.strategy = strategySelect(meta, S.strategy, { raw: true, onChange: (v) => { S.strategy = v; S.page = 0; again(); } });
  const signal = sel(SIGNALS.map(([v, k]) => el('option', { value: v }, t(k))), 'u4.f.signal', (v) => { S.signal = v; S.page = 0; again(); });
  signal.value = S.signal;
  const search = el('input', { class: 'ws-input', type: 'search', placeholder: t('u4.f.search_ph'), 'aria-label': t('u4.f.search'), value: S.q });
  search.addEventListener('input', debounce(() => { S.q = search.value; S.page = 0; again(); }, 300));
  ui.prev.addEventListener('click', () => { if (S.page > 0) { S.page--; again(); } });
  ui.next.addEventListener('click', () => { S.page++; again(); });
  ui.exp.addEventListener('click', exportCsv);

  ui.body = el('div', {},
    el('section', { class: 'card card--flush', 'aria-label': t('u4.s.title') },
      el('div', { class: 'card__head' }, el('h2', { class: 'card__title' }, t('u4.s.title'))),
      el('div', { class: 'table-wrap' }, ui.summary), ui.explain),
    el('section', { class: 'card card--flush', style: { 'margin-top': 'var(--space-4)' }, 'aria-label': t('u4.r.title') },
      el('div', { class: 'card__head' }, el('h2', { class: 'card__title' }, t('u4.r.title'))),
      el('div', { class: 'u4-bar' }, field('u4.f.strategy', ui.strategy), field('u4.f.search', search), field('u4.f.board', ui.board),
        field('u4.f.industry', ui.industry), field('u4.f.signal', signal), el('span', { class: 'u4-bar__spacer' }), ui.exp),
      el('div', { class: 'table-wrap' }, ui.table),
      el('div', { class: 'u4-pager' }, ui.count, ui.prev, ui.pageNo, ui.next)));
  root.replaceChildren(
    el('div', { class: 'u4-bar', style: { padding: '0 0 var(--space-3)', border: '0' } }, marketSwitch(meta, market, () => { S.page = 0; S.board = ''; S.industry = ''; window.dispatchEvent(new CustomEvent('langchange')); })),
    ui.msg, ui.empty, ui.body);
  await load();
}
