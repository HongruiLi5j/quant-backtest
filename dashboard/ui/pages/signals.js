/* 信号页（U1）：对应旧界面 #/trade（今日买卖）。
 * 接口：/api/meta、/api/main_strategy、/api/trade_list_dates、/api/trade_list；下方折叠区见 signals-extra.js。
 * 筛选、排序、换标签只重画列表；换策略或日期才重新读清单。 */
import { el, store } from '../shared/dom.js';
import { t } from '../shared/i18n.js';
import { getJSON, isMock } from '../shared/api.js';
import { isNum, noteCard, softError, isLocalHost, tr } from './U1-common.js';
import { COLUMNS, UNSORTABLE, buildList, sortRows, downloadCSV } from './signals-table.js';
import { fold, trackingBody, mineBody, reviewBody, recordsBody } from './signals-extra.js';
import { emptyLink, loadLink, order, noteFor, banner } from './signals-link.js';

const TAB_ACTIONS = { buy: ['买入'], sell: ['卖出'], adjust: ['加仓', '减仓'], reduce: ['减仓'], add: ['加仓'] };
const LIQ = ['', '正常', '流动性差', '流动性未知'];
const FILTER_KEY = 'qb-ui-signals';   // 只存界面偏好：搜索词以外的标签、流动性
const mobileQuery = window.matchMedia('(max-width: 600px)');

const S = { inited: false, strategies: [], strategy: 'blend2', date: '', tab: 'buy', q: '', liq: '', view: { sort: 'code', dir: 1, manual: false },
  data: null, personal: false, link: emptyLink(), req: 0, redraw: null, mainPick: {} };
mobileQuery.addEventListener('change', () => S.redraw?.());

function saveFilter() { store.set(FILTER_KEY, JSON.stringify({ tab: S.tab, liq: S.liq })); }
function restoreFilter(tabs) {
  try {
    const s = JSON.parse(store.get(FILTER_KEY, '{}')) ?? {};
    if (tabs.includes(s.tab)) S.tab = s.tab;
    if (LIQ.includes(s.liq)) S.liq = s.liq;
  } catch { /* 坏数据就用默认值 */ }
}

const tabsFor = () => (S.personal ? ['sell', 'reduce', 'buy', 'add'] : ['buy', 'sell', 'adjust']);   // 个人模式风险优先

/** 把列表区重画一遍（不重新读接口） */
function drawList(box, infoBox, shownEl) {
  const r = S.data;
  const query = S.q.trim().toLowerCase();
  const matches = (row) => !query || `${row.code} ${row.name}`.toLowerCase().includes(query);
  const allowed = TAB_ACTIONS[S.tab];
  const tabRows = (r.rows ?? []).filter((row) => allowed.includes(row.action));
  // 覆盖率按整个标签算：搜索或过滤不会让缺失的数据显得更完整
  const known = tabRows.filter((row) => isNum(row.avg_turnover_20d) && ['正常', '流动性差'].includes(row.liquidity)).length;
  const incomplete = tabRows.length > 0 && known * 5 < tabRows.length * 4;
  if (!S.view.manual) {
    S.view.sort = incomplete || !tabRows.length ? 'code' : 'avg_turnover_20d';
    S.view.dir = S.view.sort === 'code' ? 1 : -1;
  }
  const reduceFirst = S.personal && S.tab === 'adjust';
  const sorted = (rows) => sortRows(rows, S.view, reduceFirst);
  const filtered = tabRows.filter((row) => matches(row) && (!S.liq || (row.liquidity || '流动性未知') === S.liq));
  S.exportRows = sorted(filtered);
  const fold_ = (row) => row.liquidity === '流动性差' || (row.liquidity !== '正常' && !incomplete);
  const normal = filtered.filter((row) => !fold_(row));
  const illiquid = order(S.link, sorted(filtered.filter(fold_)));
  const small = S.tab === 'buy' ? sorted(normal.filter((row) => row.small_target === true)) : [];
  const rows = order(S.link, sorted(normal.filter((row) => S.tab !== 'buy' || row.small_target !== true)));
  const exceptions = (r.exceptions ?? []).filter(matches).sort((a, b) => a.code.localeCompare(b.code));
  const mobile = mobileQuery.matches;
  const onSort = (key) => {
    S.view.dir = S.view.sort === key ? -S.view.dir : 1; S.view.sort = key; S.view.manual = true; S.redraw?.();
  };
  const list = (rs, exception = false) => buildList(rs, S.view, { exception, mobile, onSort, noteFor: (row) => noteFor(S.link, row) });
  const foldBox = (title, content) => el('details', { class: 'card u1-fold' }, el('summary', {}, title), content);
  const minPct = ((r.min_target ?? 0.05) * 100).toFixed(2).replace(/\.?0+$/, '');
  shownEl.textContent = t('u1.sig.shown', { n: rows.length });
  infoBox.replaceChildren(...[
    incomplete ? el('p', { class: 'is-warn', role: 'alert' }, t('u1.sig.coverage_warn', { p: (known / tabRows.length * 100).toFixed(1) })) : null,
    el('p', { class: 'muted' }, ['reduce', 'add', 'adjust'].includes(S.tab) && r.binary ? t('u1.sig.binary') : t('u1.sig.tab_count', { n: rows.length })),
    banner(S.link)].filter(Boolean));   // replaceChildren 不会跳过 null，要先滤掉
  box.replaceChildren(...[
    el('section', { class: 'card card--flush' }, list(rows)),
    small.length ? foldBox(t('u1.sig.small_title', { m: minPct, n: small.length }), list(small)) : null,
    illiquid.length ? foldBox(t('u1.sig.illiquid_title', { k: incomplete ? '' : t('u1.sig.or_unknown'), n: illiquid.length }), list(illiquid)) : null,
    foldBox(t('u1.sig.exc_title', { all: (r.exceptions ?? []).length, n: exceptions.length }), list(exceptions, true))].filter(Boolean));
}

function controls(root, dates, onChange) {
  const sel = (cls, label, options, value, onPick) => {
    const s = el('select', { class: `ws-select ${cls}`, 'aria-label': label }, ...options);
    s.value = value; s.addEventListener('change', () => onPick(s.value)); return s;
  };
  const opt = (v, text) => el('option', { value: v }, text);
  const strat = sel('', t('u1.col.strategy'), S.strategies.map(([k, label]) => opt(k, label)), S.strategy, (v) => { S.strategy = v; S.view.manual = false; onChange(); });
  const date = sel('mono', t('u1.sig.date'), [opt('', t('u1.sig.latest')), ...dates.map((d) => opt(d, d))], S.date, (v) => { S.date = v; S.view.manual = false; onChange(); });
  const liq = sel('', t('u1.sig.col.liquidity'), LIQ.map((v) => opt(v, v ? tr('u1.liq', v) : t('u1.sig.liq_all'))), S.liq, (v) => { S.liq = v; saveFilter(); S.redraw?.(); });
  const search = el('input', { class: 'ws-input mono', type: 'search', value: S.q, maxlength: '20', placeholder: t('u1.sig.search'), 'aria-label': t('u1.sig.search'), autocomplete: 'off' });
  search.addEventListener('input', () => { S.q = search.value; S.redraw?.(); });
  const sortKeys = COLUMNS.filter(([k]) => !UNSORTABLE.includes(k));
  const msort = sel('u1-mobile-only', t('u1.sig.sort_by'), sortKeys.map(([k, label]) => opt(k, t(label))), S.view.sort, (v) => { S.view.sort = v; S.view.dir = v === 'code' ? 1 : -1; S.view.manual = true; S.redraw?.(); });
  const mdir = el('button', { class: 'btn btn--sm u1-mobile-only', type: 'button' }, t('u1.sig.sort_dir'));
  mdir.addEventListener('click', () => { S.view.dir = -S.view.dir; S.view.manual = true; S.redraw?.(); });
  const csv = el('button', { class: 'btn btn--sm', type: 'button' }, t('u1.sig.csv'));
  csv.addEventListener('click', () => S.data && downloadCSV(`trade_${S.data.strategy}_${S.data.data_date}_${S.tab}.csv`, S.exportRows ?? []));
  const xq = new URLSearchParams({ strategy: S.strategy, date: S.date });
  const xlsx = el('a', { class: 'btn btn--sm', href: isMock() ? '#' : `/api/trade_list_export?${xq}` }, t('u1.sig.xlsx'));
  const tabs = el('div', { class: 'segmented', role: 'tablist', 'aria-label': t('u1.col.signal') }, ...tabsFor().map((k) => {
    const b = el('button', { class: 'segmented__btn', type: 'button', role: 'tab', dataset: { tab: k }, 'aria-pressed': String(S.tab === k), 'aria-selected': String(S.tab === k) }, t(`u1.sig.tab.${k}`));
    b.addEventListener('click', () => {
      S.tab = k; S.view.manual = false; saveFilter();
      tabs.querySelectorAll('button').forEach((x) => { x.setAttribute('aria-pressed', String(x === b)); x.setAttribute('aria-selected', String(x === b)); });
      S.redraw?.();
    });
    return b;
  }));
  return el('div', { class: 'card u1-controls' }, el('div', { class: 'ws-filter' }, strat, date, tabs), el('div', { class: 'ws-filter' }, search, liq, msort, mdir, csv, xlsx));
}

export async function render(root) {
  const req = ++S.req;
  const meta = await getJSON('/api/meta');
  S.personal = meta.personal_mode === true;
  if (!S.inited) {
    S.strategies = Object.entries(meta.strategies ?? {}).filter(([k, v]) => v.kind === 'single' && k !== 'buy_hold').map(([k, v]) => [k, v.label]);
    S.tab = tabsFor()[0];
    restoreFilter(tabsFor());
    S.mainPick = await getJSON('/api/main_strategy').catch((e) => { if (e.auth) throw e; return {}; });   // 主策略 = 信号核对里准确率最高的（读不到就用 blend2）
    const pick = S.mainPick.main;
    S.strategy = pick && S.strategies.some(([k]) => k === pick) ? pick : (S.strategies[0]?.[0] ?? 'blend2');
    S.inited = true;
  }
  const host = root.closest('main');
  const bind = (k) => host.querySelector(`[data-bind="${k}"]`);
  const reload = () => render(root).catch((e) => root.replaceChildren(softError(e)));
  const archive = await getJSON('/api/trade_list_dates');
  const dates = archive.dates ?? [];
  if (!dates.includes(S.date)) S.date = '';
  const r = await getJSON(`/api/trade_list?${new URLSearchParams({ strategy: S.strategy, date: S.date })}`);
  if (req !== S.req) return;
  S.link = emptyLink();
  S.data = null;
  S.redraw = null;
  const bar = controls(root, dates, reload);
  if (r.error || r.empty) {
    bind('date').textContent = ''; bind('stats').textContent = ''; bind('shown').textContent = '';
    root.replaceChildren(bar, noteCard(r.error || r.message || t('ui.state.empty')));
    return;
  }
  S.data = r;
  const generated = new Date(r.generated_at);
  const genText = Number.isNaN(generated.getTime()) ? t('u1.na') : new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).format(generated);
  const minPct = ((r.min_target ?? 0.05) * 100).toFixed(2).replace(/\.?0+$/, '');
  bind('date').textContent = r.data_date;
  bind('stats').textContent = t('u1.sig.gen', { g: genText, l: r.label });
  const counts = ['买入', '卖出', '加仓', '减仓'].map((k) => el('span', {}, tr('u1.act', k), ' ', el('b', { class: 'num' }, String(r.counts?.[k] || 0)), ' ', t('u1.sig.unit'),
    k === '买入' ? t('u1.sig.small_split', { m: minPct, a: r.counts_main?.[k] ?? t('u1.na'), b: r.counts_small?.[k] ?? t('u1.na') }) : ''));
  const infoBox = el('div', { class: 'stack', style: { '--gap': '4px' } });
  const listBox = el('div', { class: 'stack', style: { '--gap': '12px' } });
  const mine = !isLocalHost() ? null : fold(t('u1.sig.mine_title'), () => mineBody(S.strategy, S.date), { open: true });
  const limits = el('details', { class: 'card u1-fold' }, el('summary', {}, t('u1.sig.limits')), el('p', {}, r.disclaimer), el('ul', { class: 'u1-list' }, ...(r.limitations ?? []).map((x) => el('li', {}, x))));
  const labels = Object.fromEntries(Object.entries(meta.strategies ?? {}).map(([k, v]) => [k, v.label]));
  root.replaceChildren(bar,
    el('div', { class: 'card u1-counts-line' }, ...counts),
    el('p', { class: 'muted' }, r.performance_note), infoBox, listBox,
    el('div', { class: 'stack u1-gap', style: { '--gap': '12px' } },
      fold(t('u1.sig.track_title'), () => trackingBody(S.strategy), { open: true }).node, mine?.node ?? null,
      fold(t('u1.sig.review_title'), reviewBody).node, fold(t('u1.sig.records_title'), () => recordsBody(labels)).node, limits));
  S.redraw = () => drawList(listBox, infoBox, bind('shown'));
  S.redraw();
  if (S.personal) {   // 持仓台账读完再联动重画；失败只提示，不影响清单
    const link = await loadLink();
    if (req === S.req) { S.link = link; S.redraw?.(); }
  }
}
