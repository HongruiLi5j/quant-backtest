/* 信号页·下方折叠区（U1）：发布后跟踪统计、我的相关（仅本机）、信号核对（账本）、上线记录 / 历史回放。
 * 每个区块各自读接口、各自失败，互不影响。对应旧界面 loadTradeFollowups / 核对 / 上线记录。 */
import { el, toneClass } from '../shared/dom.js';
import { t } from '../shared/i18n.js';
import { getJSON } from '../shared/api.js';
import { actBadge, numNA, num, ratio, tr, softError, stockHref } from './U1-common.js';
import { targetText } from './signals-table.js';

const HORIZONS = [5, 20, 60];
const th = (k, numeric) => el('th', { scope: 'col', class: numeric ? 'num' : null }, t(k));
const table = (head, rows) => el('div', { class: 'table-wrap' }, el('table', { class: 'table' }, el('thead', {}, el('tr', {}, ...head)), el('tbody', {}, ...rows)));

/** 一个折叠区：打开时才读数据（lazy）或立即读（eager） */
export function fold(title, load, { open = false } = {}) {
  const body = el('div', { class: 'u1-fold__body' }, el('p', { class: 'muted' }, t('u1.loading')));
  const box = el('details', { class: 'card u1-fold' }, el('summary', {}, title), body);
  let done = false;
  const run = async () => {
    if (done) return;
    done = true;
    try { body.replaceChildren(await load()); } catch (e) { body.replaceChildren(softError(e)); }
  };
  box.addEventListener('toggle', () => { if (box.open) run(); });
  if (open) { box.open = true; run(); }
  return { node: box, reload: () => { done = false; body.replaceChildren(el('p', { class: 'muted' }, t('u1.loading'))); if (box.open) run(); } };
}

/** 发布后跟踪统计 */
export async function trackingBody(strategy) {
  const s = await getJSON(`/api/trade_list_tracking?strategy=${encodeURIComponent(strategy)}`);
  if (s.error) return el('p', { class: 'muted' }, t('u1.sig.track_unavail'));
  const note = el('p', { class: 'muted' }, `${s.note || ''} ${t('u1.sig.track_follow')}`);
  if (s.empty) {
    return el('div', {}, note, el('p', { class: 'muted' }, t('u1.sig.track_empty')),
      el('div', { class: 'ws-grid ws-grid--3' }, ...HORIZONS.map((h) => el('div', { class: 'metric' },
        el('span', { class: 'metric__label' }, t('u1.sig.horizon', { n: h })), el('span', { class: 'metric__value' }, t('u1.na')),
        el('span', { class: 'metric__foot' }, t('u1.sig.track_foot'))))));
  }
  const hth = (k, h) => { const c = th(k, true); c.textContent = t(k, { n: h }); return c; };   // 带天数的表头
  const head = [th('u1.sig.col.action'), ...HORIZONS.flatMap((h) => [hth('u1.sig.avg_h', h), hth('u1.sig.beat_h', h), hth('u1.sig.done_h', h)])];
  const rows = (s.groups ?? []).map((g) => el('tr', {},
    el('td', {}, tr('u1.act', g.action), g.action === '卖出' ? el('span', { class: 'u1-sub' }, t('u1.sig.sell_note')) : null),
    ...HORIZONS.flatMap((h) => [el('td', { class: 'num' }, numNA(g[`avg${h}`], '%')), el('td', { class: 'num' }, `${numNA(g[`beat${h}`], '%')} / ${g[`bench_n${h}`]}`), el('td', { class: 'num' }, String(g[`n${h}`]))])));
  return el('div', {}, note, el('p', { class: 'muted' }, t('u1.sig.track_meta', { d: s.latest_price_date || t('u1.na'), n: s.recorded_signals })), table(head, rows));
}

/** 我的相关：只有本机才读；接口 403 / 失败时给提示 */
export async function mineBody(strategy, date) {
  const q = new URLSearchParams({ strategy, date });
  const m = await getJSON(`/api/trade_list_mine?${q}`);
  if (m.error) return el('p', { class: 'muted' }, t('u1.sig.mine_unavail'));
  const rows = (m.rows ?? []).map((r) => el('tr', { class: r.action === '卖出' ? 'u1-sell-row' : null },
    el('td', {}, el('a', { href: stockHref(r.code) }, `${r.name} ${r.code}`)),
    el('td', {}, [r.in_watchlist ? t('u1.sig.in_watch') : '', r.in_member_positions ? t('u1.sig.in_paper') : ''].filter(Boolean).join(' ')),
    el('td', {}, r.action ? actBadge(r.action) : t('u1.sig.list_none')), el('td', { class: 'num' }, targetText(r))));
  return el('div', {}, el('p', { class: 'muted' }, t('u1.sig.mine_note'), ' ', t(m.member_logged_in ? 'u1.sig.mine_in' : 'u1.sig.mine_out')),
    table([th('u1.sig.col.code'), th('u1.sig.mine_src'), th('u1.sig.col.action'), th('u1.sig.col.next', true)], rows));
}

/** 信号核对（/api/ledger）：只放主结论 + 主策略准确率 + 说明，细表留在旧界面 */
export async function reviewBody() {
  const d = await getJSON('/api/ledger');
  if (d.error || d.empty) return el('p', { class: 'muted' }, d.error || d.message || t('ui.state.empty'));
  const main = d.main_pick?.main || d.main;
  const rec = d.main_pick?.record;
  const recRows = rec ? [['u1.rev.pre', rec.pre], ['u1.rev.post', rec.post]].filter(([, r]) => r).map(([k, r]) => el('tr', {},
    el('td', {}, t(k)), el('td', { class: 'num' }, String(r.n)), el('td', { class: 'num' }, String(r.days)), el('td', { class: 'num' }, ratio(r.hit, 1)), el('td', { class: 'num' }, ratio(r.score, 2)))) : [];
  const prec = (d.precision?.[main] ?? []).map((p) => el('tr', { class: p.enough ? null : 'muted' },
    el('td', {}, tr('u1.act', p.action)), el('td', { class: 'num' }, String(p.h)), el('td', { class: 'num' }, String(p.n)),
    el('td', { class: 'num' }, ratio(p.hit_raw, 1)), el('td', { class: 'num' }, ratio(p.hit_excess, 1)), el('td', { class: 'num' }, ratio(p.mean_ret, 2)), el('td', { class: 'num' }, ratio(p.mean_excess, 2)),
    el('td', {}, p.verdict || (p.enough ? '' : t('u1.rev.small')), el('span', { class: 'u1-sub' }, t('u1.rev.ci', { lo: ratio(p.hit_excess_lo), hi: ratio(p.hit_excess_hi), med: ratio(p.median_excess, 2), net: ratio(p.net_excess, 2) })))));
  return el('div', { class: 'stack', style: { '--gap': '12px' } },
    el('p', { class: 'muted' }, t('u1.rev.count', { s: (d.signal_days ?? []).length, e: (d.evaluated_days ?? []).length })),
    el('p', { class: 'muted' }, t('u1.rev.source')),
    el('p', {}, el('b', {}, main), ' ', d.promotion?.message || ''), el('p', { class: 'muted' }, d.main_pick?.reason || ''),
    el('p', { class: 'muted' }, t('u1.rev.samples', { w: d.promotion?.complete_weeks ?? '—', d: (d.evaluated_days ?? []).length })),
    recRows.length ? table([th('u1.rev.status'), th('u1.rev.n', true), th('u1.rev.days', true), th('u1.rev.hit', true), th('u1.rev.excess', true)], recRows) : null,
    prec.length ? table([th('u1.sig.col.action'), th('u1.rev.days', true), th('u1.rev.n', true), th('u1.rev.hit_raw', true), th('u1.rev.hit', true), th('u1.rev.ret', true), th('u1.rev.excess', true), th('u1.rev.verdict')], prec) : null,
    ...(d.notes ?? []).map((n) => el('p', { class: 'muted' }, n)), ...(d.caveats ?? []).map((n) => el('p', { class: 'muted' }, n)),
    el('p', {}, el('a', { href: '/#/review' }, t('u1.rev.full'))));
}

/** 上线记录 / 历史回放（/api/tracking 的 eval / replay） */
export async function recordsBody(labels) {
  const r = await getJSON('/api/tracking?market=cn');
  const live = r.eval; const rep = r.replay;
  const liveN = live && !live.empty ? live.summary.reduce((s, x) => s + (x.n20 || 0), 0) : 0;
  const useRep = liveN < 30 && rep && !rep.empty;   // 实盘样本太少时自动显示历史回放（和旧界面一致）
  const ev = useRep ? rep : live;
  if (!ev || ev.empty) return el('p', { class: 'muted' }, t('u1.rec.none'));
  const liveTotal = live && !live.empty ? live.total : 0;
  const no5 = ev.summary.every((x) => !(x.n5 > 0));
  const status = useRep
    ? t('u1.rec.status_rep', { n: ev.total, a: ev.since, b: ev.latest, tz: ev.timezone_label || '', l: liveTotal, e: live?.expected_first_5 || t('u1.rec.pending') })
    : t('u1.rec.status', { n: ev.total, a: ev.since, b: ev.latest, d: ev.latest_price_date || ev.latest, tz: ev.timezone_label || '' }) + (no5 ? t('u1.rec.no5', { e: ev.expected_first_5 || t('u1.rec.pending') }) : '');
  const label = (k) => labels[k] || k;
  const cells = (s, n) => (s[`n${n}`]
    ? [el('td', { class: 'num' }, String(s[`n${n}`])), el('td', { class: `num ${toneClass(s[`avg${n}`])}` }, num(s[`avg${n}`], 2, '%', true)), el('td', { class: `num ${s[`beat${n}`] > 50 ? 'is-rise' : ''}` }, num(s[`beat${n}`], 0, '%'))]
    : [el('td', { class: 'muted', colspan: '3' }, t('u1.rec.wait'))]);
  const head = [th('u1.col.strategy'), th('u1.col.signal'), th('u1.rec.times', true), ...HORIZONS.flatMap((h) => ['u1.rec.n', 'u1.rec.avg', 'u1.rec.beat'].map((k) => { const c = th(k, true); c.textContent = t(k, { n: h }); return c; }))];
  const sum = [...ev.summary].sort((a, b) => b.signals - a.signals).map((s) => el('tr', {}, el('td', {}, label(s.strategy)), el('td', {}, actBadge(s.action)), el('td', { class: 'num' }, String(s.signals)), ...HORIZONS.flatMap((h) => cells(s, h))));
  const pc = (v) => el('td', { class: `num ${toneClass(v)}` }, num(v, 2, '%', true));
  const recent = (ev.recent ?? []).map((x) => el('tr', {}, el('td', {}, x.signal_date), el('td', {}, el('a', { href: stockHref(x.code) }, x.name), ' ', el('span', { class: 'mono muted' }, x.code)),
    el('td', {}, label(x.strategy)), el('td', {}, actBadge(x.action)), pc(x.r5), pc(x.r20), pc(x.r60), pc(x.r_now), pc(x.x_now)));
  return el('div', { class: 'stack', style: { '--gap': '12px' } }, useRep ? el('p', { class: 'is-warn' }, t('u1.rec.replay_warn', { l: liveTotal })) : null,
    el('p', { class: 'muted' }, status), table(head, sum),
    el('h3', { class: 'eyebrow' }, t('u1.rec.recent')),
    table([th('u1.rec.day'), th('u1.col.stock'), th('u1.col.strategy'), th('u1.col.signal'), th('u1.rec.r5', true), th('u1.rec.r20', true), th('u1.rec.r60', true), th('u1.rec.now', true), th('u1.rec.xnow', true)], recent));
}
