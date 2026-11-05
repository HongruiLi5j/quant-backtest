/* 信号页·下方折叠区（U1）：发布后跟踪统计、我的相关（仅本机）、信号核对（账本）、上线记录 / 历史回放。
 * 每个区块各自读接口、各自失败，互不影响。对应旧界面 loadTradeFollowups / 核对 / 上线记录。 */
import { el, toneClass } from '../shared/dom.js';
import { t, lang } from '../shared/i18n.js';
import { getJSON } from '../shared/api.js';
import { actBadge, numNA, num, ratio, tr, softError, stockHref } from './U1-common.js';
import { targetText } from './signals-table.js';
import { headline, isNum, isNewLedger } from './accuracy-headline.js';

/** R8：晋升检验结论是后端的中文整句，英文界面按两种固定句式显示（认不出的原样） */
function promoMsg(m) {
  const x = String(m || '');
  if (lang() !== 'en') return x;
  if (/^尚无策略通过晋升检验$/.test(x)) return t('p9.none');
  const r = /^通过晋升检验：(.+)$/.exec(x);
  return r ? t('p9.pass', { s: r[1].replace(/、/g, ', ') }) : x;
}

const HORIZONS = [5, 20, 60];
const th = (k, numeric) => el('th', { scope: 'col', class: numeric ? 'num' : null }, t(k));
const table = (head, rows) => el('div', { class: 'table-wrap' }, el('table', { class: 'table' }, el('thead', {}, el('tr', {}, ...head)), el('tbody', {}, ...rows)));

/** 一个折叠区：打开时才读数据（lazy）或立即读（eager） */
export function fold(title, load, { open = false, note = '' } = {}) {
  const body = el('div', { class: 'u1-fold__body' }, el('p', { class: 'muted' }, t('u1.loading')));
  // D40（T-1）：note = 一行「数据来源 / 基准 / 样本口径」，放在标题下面，折叠时也看得到
  const box = el('details', { class: 'card u1-fold' },
    el('summary', {}, title, note ? el('small', { class: 'muted', style: { display: 'block', 'font-weight': '400', 'margin-top': '4px' } }, note) : null), body);
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

/* ---- D40 方案 A：信号页只留一个「信号准不准」块，用 quant D16（revquant 已复核）的口径 ----
 * 主表：信号账本（/api/ledger）主策略的方向正确率、同日随机挑股基线、比随机多多少（按互不重叠的期算 95% 区间）、
 *   独立期数、12 个完整交易周之前只显示「积累中」。基准都是全市场个股等权平均，卖出 / 减仓按「之后跑输」算对。
 * 副表：已发布清单（/api/trade_list_tracking 的 ew_*，同一基准、同样翻转方向），没有区间，所以只写「积累中」。
 * 账本文件还是 D16 之前生成的（行里没有 edge 字段）时，退回旧显示并提示，等下一次每日更新。 */
const pp = (v) => (isNum(v) ? `${v > 0 ? '+' : ''}${(v * 100).toFixed(1)}` : '—');   // 小数 → 百分点

function ledgerTable(rows) {
  const head = ['c40a.col.action', 'c40a.col.h', 'c40a.col.sample', 'c40a.col.dir', 'c40a.col.random', 'c40a.col.edge', 'c40a.col.excess', 'c40a.col.verdict']
    .map((k, i) => th(k, i > 0 && i < 7));
  const body = rows.map((p) => el('tr', { class: p.enough ? null : 'muted' },
    el('td', {}, tr('u1.act', p.action)),
    el('td', { class: 'num' }, String(p.h)),
    el('td', { class: 'num' }, t('c40a.sample', { n: p.n, d: p.days, k: p.periods ?? '—' })),
    el('td', { class: 'num' }, ratio(p.hit_excess, 1),
      isNum(p.hit_excess_lo) ? el('span', { class: 'u1-sub' }, `${ratio(p.hit_excess_lo, 0)}–${ratio(p.hit_excess_hi, 0)}`) : null),
    el('td', { class: 'num' }, ratio(p.hit_random, 1)),
    el('td', { class: `num ${isNum(p.edge_lo) && (p.edge_lo > 0 || p.edge_hi < 0) ? 'u1-ci-excl' : ''}`, style: isNum(p.edge_lo) && (p.edge_lo > 0 || p.edge_hi < 0) ? { 'font-weight': '600' } : null }, t('c40a.pp', { v: pp(p.edge) }),
      el('span', { class: 'u1-sub' }, isNum(p.edge_lo) ? t('c40a.ci', { lo: pp(p.edge_lo), hi: pp(p.edge_hi) }) : t('c40a.no_ci'))),
    el('td', { class: 'num' }, ratio(p.mean_excess, 2)),
    el('td', {}, tr('c40a.v', p.verdict), p.verdict === '积累中'   // revquant：英文界面结论列用英文（和 quant D89 的 verdict_en 同一措辞）
       && p.weeks_needed ? el('span', { class: 'u1-sub' }, t('c40a.weeks', { w: p.weeks, n: p.weeks_needed })) : null)));
  return table(head, body);
}

function listTable(s) {
  const groups = (s?.groups ?? []).filter((g) => HORIZONS.some((h) => g[`ew_n${h}`] > 0));
  if (!groups.length) return el('p', { class: 'muted' }, t('c40a.list_empty'));
  const head = [th('c40a.col.action'), ...HORIZONS.flatMap((h) => ['c40a.l.n', 'c40a.l.dir', 'c40a.l.edge'].map((k) => { const c = th(k, true); c.textContent = t(k, { n: h }); return c; }))];
  const body = groups.map((g) => el('tr', {}, el('td', {}, tr('u1.act', g.action)),
    ...HORIZONS.flatMap((h) => (g[`ew_n${h}`] > 0
      ? [el('td', { class: 'num' }, String(g[`ew_n${h}`])), el('td', { class: 'num' }, `${num(g[`ew_dir${h}`], 1, '%')} / ${num(g[`ew_random${h}`], 1, '%')}`),
        el('td', { class: 'num' }, t('c40a.pp', { v: isNum(g[`ew_edge${h}`]) ? `${g[`ew_edge${h}`] > 0 ? '+' : ''}${g[`ew_edge${h}`].toFixed(1)}` : '—' }))]
      : [el('td', { class: 'muted', colspan: '3' }, t('u1.rec.wait'))]))));
  return table(head, body);
}

// 一句话结论 headline() 在 accuracy-headline.js（今日页也用），约束见那个文件的说明

/** 表里有几行区间不含 0（只用来提示「单独一行不能当结论」，不改结论） */
const excludesZero = (rows) => rows.filter((p) => p.enough && isNum(p.edge_lo) && (p.edge_lo > 0 || p.edge_hi < 0)).length;

/** 信号页的「信号准不准」：一个块，主表来自账本，副表是已发布清单（同一基准） */
export async function accuracyBody(strategy) {
  const [d, s] = await Promise.all([getJSON('/api/ledger'), getJSON(`/api/trade_list_tracking?strategy=${encodeURIComponent(strategy)}`).catch(() => null)]);
  if (d.error || d.empty) return el('p', { class: 'muted' }, d.error || d.message || t('ui.state.empty'));
  const main = d.main_pick?.main || d.main;
  const rows = d.precision?.[main] ?? [];
  if (!isNewLedger(rows)) {   // 账本还是旧算法生成的
    return el('div', { class: 'stack', style: { '--gap': '12px' } }, el('p', { class: 'is-warn' }, t('c40a.old_ledger')), await reviewBody(d));
  }
  return el('div', { class: 'stack', style: { '--gap': '12px' } },
    el('p', {}, el('b', {}, headline(rows, main)), ' ', t('c40a.how')),
    el('p', { class: 'muted' }, t('u1.rev.count', { s: (d.signal_days ?? []).length, e: (d.evaluated_days ?? []).length }), ' · ', t('c40a.bench')),
    ledgerTable(rows),
    el('p', { class: 'muted' }, t('c40a.many_rows', { k: rows.length, x: excludesZero(rows) })),
    ...(d.notes ?? []).map((n) => el('p', { class: 'muted' }, n)),
    el('h3', { class: 'eyebrow' }, t('c40a.list_title')),
    el('p', { class: 'muted' }, t('c40a.list_note')),
    listTable(s),
    el('p', { class: 'muted' }, promoMsg(d.promotion?.message)),
    ...(d.caveats ?? []).map((n) => el('p', { class: 'muted' }, n)),
    el('p', {}, el('a', { href: '/#/review' }, t('u1.rev.full'))));
}

/** 旧版账本（D16 之前生成的 latest.json）的显示，只在 accuracyBody 里兜底用 */
async function reviewBody(pre = null) {
  const d = pre || await getJSON('/api/ledger');
  if (d.error || d.empty) return el('p', { class: 'muted' }, d.error || d.message || t('ui.state.empty'));
  const main = d.main_pick?.main || d.main;
  const rec = d.main_pick?.record;
  const recRows = rec ? [['u1.rev.pre', rec.pre], ['u1.rev.post', rec.post]].filter(([, r]) => r).map(([k, r]) => el('tr', {},
    el('td', {}, t(k)), el('td', { class: 'num' }, String(r.n)), el('td', { class: 'num' }, String(r.days)), el('td', { class: 'num' }, ratio(r.hit, 1)), el('td', { class: 'num' }, ratio(r.score, 2)))) : [];
  const prec = (d.precision?.[main] ?? []).map((p) => el('tr', { class: p.enough ? null : 'muted' },
    el('td', {}, tr('u1.act', p.action)), el('td', { class: 'num' }, String(p.h)), el('td', { class: 'num' }, String(p.n)),
    el('td', { class: 'num' }, ratio(p.hit_raw, 1)), el('td', { class: 'num' }, ratio(p.hit_excess, 1)), el('td', { class: 'num' }, ratio(p.mean_ret, 2)), el('td', { class: 'num' }, ratio(p.mean_excess, 2)),
    el('td', {}, tr('c40a.v', p.verdict) || (p.enough ? '' : t('u1.rev.small')), el('span', { class: 'u1-sub' }, t('u1.rev.ci', { lo: ratio(p.hit_excess_lo), hi: ratio(p.hit_excess_hi), med: ratio(p.median_excess, 2), net: ratio(p.net_excess, 2) })))));
  return el('div', { class: 'stack', style: { '--gap': '12px' } },
    el('p', { class: 'muted' }, t('u1.rev.count', { s: (d.signal_days ?? []).length, e: (d.evaluated_days ?? []).length })),
    el('p', { class: 'muted' }, t('u1.rev.source')),
    el('p', {}, el('b', {}, main), ' ', promoMsg(d.promotion?.message)), el('p', { class: 'muted' }, d.main_pick?.reason || ''),
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
