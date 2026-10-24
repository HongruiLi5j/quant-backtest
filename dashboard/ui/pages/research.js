/* ==========================================================
 * 文件: ui/pages/research.js —— 研究（旧页「信号跟踪」/#/track 的内容）
 * 接口: GET /api/meta、GET /api/tracking?market=
 * 只有「信号跟踪」有真实接口；Replay 排行 / Validation / Factor Scan / Monotonic 在后端没有 HTTP 接口，
 * 这几个 tab 显示「暂未提供」，不放假数据。
 * 信号跟踪: 实盘记录里 20 天样本不足 30 条时，改用「历史回放」并在顶部说明（和旧页规则一致）。
 * ========================================================== */

import { el, store } from '../shared/dom.js';
import { t } from '../shared/i18n.js';
import { getJSON } from '../shared/api.js';
import { dataMarket, marketSwitch, fmt, pct, pill, toneClass } from './u4-util.js';

const KEY = 'qb-ws-research';
const TABS = ['tracking', 'replay', 'validation', 'factor', 'monotonic'];
let tab = 'tracking';
let ctx = null;   // { meta, market, data }

function soon(k) {
  return el('div', { class: 'card state' },
    el('p', { class: 'state__title' }, t('u4.res.soon')),
    el('p', { class: 'muted' }, t(`u4.res.soon_${k}`)));
}

/** 5 / 20 / 60 天三组列：样本、平均、跑赢大盘 */
function cells(s, n) {
  if (!s[`n${n}`]) return [el('td', { class: 'num muted' }, t('u4.res.wait')), el('td', { class: 'num muted' }, '—'), el('td', { class: 'num muted' }, '—')];
  return [el('td', { class: 'num' }, String(s[`n${n}`])), el('td', { class: `num ${toneClass(s[`avg${n}`])}` }, pct(s[`avg${n}`], 2)),
    el('td', { class: `num ${s[`beat${n}`] > 50 ? 'u4-better' : ''}` }, fmt(s[`beat${n}`], 0, '%'))];
}

function tracking() {
  const { meta, data } = ctx;
  const live = data.eval;
  const rep = data.replay;
  const liveN = live && !live.empty ? live.summary.reduce((a, x) => a + (x.n20 || 0), 0) : 0;
  const useRep = liveN < 30 && rep && !rep.empty;
  const ev = useRep ? rep : live;
  if (!ev || ev.empty) return el('div', { class: 'card state' }, el('p', { class: 'state__title' }, t('u4.res.none')));
  const liveTotal = live && !live.empty ? live.total : 0;
  const no5 = ev.summary.every((x) => !(x.n5 > 0));
  const label = (k) => meta.strategies[k]?.label ?? k;
  const tz = ev.timezone_label || '';
  const status = useRep
    ? t('u4.res.status_rep', { n: ev.total, a: ev.since, b: ev.latest, tz, live: liveTotal, first: live?.expected_first_5 || t('u4.res.tbd') })
    : t('u4.res.status', { n: ev.total, a: ev.since, b: ev.latest, d: ev.latest_price_date || ev.latest, tz }) +
      (no5 ? t('u4.res.no5', { first: ev.expected_first_5 || t('u4.res.tbd') }) : '');
  const grp = (n) => [t('u4.res.h_n', { n }), t('u4.res.h_avg', { n }), t('u4.res.h_beat')];
  const sumHead = [t('u4.s.strategy'), t('u4.res.h_sig'), t('u4.res.h_count'), ...grp(5), ...grp(20), ...grp(60)];
  const summary = [...ev.summary].sort((a, b) => b.signals - a.signals).map((s) => el('tr', {},
    el('td', {}, label(s.strategy)), el('td', {}, pill(s.action)), el('td', { class: 'num' }, String(s.signals)), ...cells(s, 5), ...cells(s, 20), ...cells(s, 60)));
  const recentHead = [t('u4.res.r_date'), t('u4.c.stock'), t('u4.s.strategy'), t('u4.res.h_sig'), t('u4.res.r5'), t('u4.res.r20'), t('u4.res.r60'), t('u4.res.r_now'), t('u4.res.r_x')];
  const recent = ev.recent.map((x) => {
    const tr = el('tr', { class: 'u4-click' },
      el('td', {}, x.signal_date), el('td', {}, el('a', { href: `/#/stock/${encodeURIComponent(x.code)}` }, x.name), el('span', { class: 'u4-code' }, x.code)),
      el('td', {}, label(x.strategy)), el('td', {}, pill(x.action)),
      ...['r5', 'r20', 'r60', 'r_now', 'x_now'].map((k) => el('td', { class: `num ${toneClass(x[k])}` }, pct(x[k]))));
    tr.addEventListener('click', (e) => { if (!e.target.closest('a')) window.location.href = `/#/stock/${encodeURIComponent(x.code)}`; });
    return tr;
  });
  const table = (head, body, numFrom) => el('div', { class: 'table-wrap' }, el('table', { class: 'table' },
    el('thead', {}, el('tr', {}, ...head.map((h, i) => el('th', { scope: 'col', class: i >= numFrom ? 'num' : null }, h)))), el('tbody', {}, ...body)));
  return el('div', {},
    useRep ? el('div', { class: 'card u4-banner' }, el('strong', {}, t('u4.res.rep_h')), ' ', t('u4.res.rep_b', { n: liveTotal })) : null,
    el('p', { class: 'muted', style: { 'margin-bottom': 'var(--space-3)', 'font-size': 'var(--fs-sm)' } }, status),
    el('section', { class: 'card card--flush', 'aria-label': t('u4.res.sum_title') },
      el('div', { class: 'card__head' }, el('h2', { class: 'card__title' }, t('u4.res.sum_title'))), table(sumHead, summary, 2)),
    el('section', { class: 'card card--flush', style: { 'margin-top': 'var(--space-4)' }, 'aria-label': t('u4.res.recent_title') },
      el('div', { class: 'card__head' }, el('h2', { class: 'card__title' }, t('u4.res.recent_title'))), table(recentHead, recent, 4)));
}

function draw(root) {
  const tabs = el('div', { class: 'segmented u4-tabs', role: 'tablist', 'aria-label': t('ui.nav.research') },
    ...TABS.map((k) => {
      const b = el('button', { class: 'segmented__btn', type: 'button', role: 'tab', 'aria-selected': String(k === tab) }, t(`u4.res.tab_${k}`));
      b.addEventListener('click', () => { tab = k; store.set(KEY, JSON.stringify({ tab })); draw(root); });
      return b;
    }));
  root.replaceChildren(
    el('div', { class: 'u4-bar', style: { padding: '0 0 var(--space-3)', border: '0' } },
      marketSwitch(ctx.meta, ctx.market, () => window.dispatchEvent(new CustomEvent('langchange')))),
    tabs, tab === 'tracking' ? tracking() : soon(tab));
}

export async function render(root) {
  try { const s = JSON.parse(store.get(KEY, '{}')); if (TABS.includes(s?.tab)) tab = s.tab; } catch { /* 用默认 tab */ }
  const meta = await getJSON('/api/meta', { mock: 'meta-u4' });
  const market = dataMarket(meta);
  const data = await getJSON(`/api/tracking?market=${market}`, { mock: 'research-tracking' });
  ctx = { meta, market, data };
  draw(root);
}
