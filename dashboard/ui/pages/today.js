/* 今日页（U1）：对应旧界面 #/today。
 * 接口：/api/meta、/api/summary、/api/tracking（个人模式还有 today-personal.js 里的几个）。
 * G03 信息层级（从上到下）：数据日期 → 首屏三张卡（今日动作 / 持仓变化 / 需要注意，见 today-brief.js）→
 * 详细数据（市场概览、组合建议、其他策略、说明、提醒、关注股票，内容和以前一样）。
 * 所有文本走 textContent（el()），不拼 HTML。 */
import { el, toneClass, store } from '../shared/dom.js';
import { t } from '../shared/i18n.js';
import { getJSON } from '../shared/api.js';
import { actBadge, num, ratio, noteCard, cardHead, stockHref, softError } from './U1-common.js';
import { rememberStockStrategy } from '../shared/stock-link.js';
import { personalData, actionsCard } from './today-personal.js';
import { g3, usableList, matchPositions, riskItems, positionsCard, riskCard, briefGrid, detailsHead } from './today-brief.js';
import { dateNotice } from './today-date.js';

const MARKET_KEY = 'qb-ui-market';
let market = null;

function metric(label, value, tone = '', foot = null) {
  return el('div', { class: 'metric' },
    el('span', { class: 'metric__label' }, label),
    el('span', { class: `metric__value ${tone}` }, value),
    foot ? el('span', { class: 'metric__foot' }, foot) : null);
}

/** 市场概览：基准的 4 个指标 + 组合目标仓位，下面一行风险数字 */
function overview(s, bench, lead, mkName) {
  const head = el('div', { class: 'u1-ovhead' },
    el('h2', { class: 'ws-section-title' }, t('u1.today.overview', { m: mkName })),
    el('span', { class: 'muted' }, `${s.main_name} · ${s.main}`),
    el('span', { class: 'ws-head__spacer' }),
    el('span', { class: 'muted' }, bench ? t('u1.today.as_of', { d: s.data_date, tz: s.timezone_label || '' }) : t('u1.today.no_bench')));
  if (!bench) return el('section', {}, head);
  return el('section', {}, head,
    el('div', { class: 'ws-grid ws-grid--kpi' },
      metric(t('u1.today.close'), num(bench.close)),
      metric(t('u1.today.d1'), num(bench.d1, 2, '%', true), toneClass(bench.d1)),
      metric(t('u1.today.ytd'), num(bench.ytd, 1, '%', true), toneClass(bench.ytd)),
      metric(t('u1.today.ma300'), num(bench.ma300_gap, 1, '%', true), toneClass(bench.ma300_gap)),
      metric(t('u1.today.target'), lead ? ratio(lead.next_total) : '—', '', lead ? lead.label : null)),
    el('div', { class: 'card u1-riskline' },
      el('span', {}, t('u1.today.vol'), ' ', el('b', { class: 'num' }, num(bench.vol, 1, '%'))),
      el('span', {}, t('u1.today.dd1y'), ' ', el('b', { class: `num ${toneClass(bench.dd1y)}` }, num(bench.dd1y, 1, '%', true))),
      el('span', {}, 'RSI(14)', ' ', el('b', { class: 'num' }, num(bench.rsi14, 1)))));
}

const whyRow = (label, badge, value, tone = '') => el('div', { class: 'u1-why' },
  el('span', {}, label), el('span', {}, badge), el('b', { class: `num ${tone}` }, value));

/** 组合建议大卡（旧界面 today-hero） */
function leadCard(s, lead, bench) {
  if (!lead) return noteCard(t('u1.today.no_strategy'));
  const subs = s.strategies.filter((c) => c !== lead && !c.error && ['trend_ens', 'donchian_s1', 'rsi2'].includes(c.key));
  const reason = Number(lead.next_total) <= 1e-9 ? t('u1.today.flat_reason', { r: lead.reason }) : lead.reason;
  const stats = [[t('u1.today.cagr'), num(lead.stats['CAGR%'], 1, '%')], [t('u1.today.xsharpe'), num(lead.stats.ExcessSharpe, 2)],
    [t('u1.today.maxdd'), num(lead.stats['MaxDD%'], 1, '%')], [t('u1.today.volst'), num(lead.stats['Vol%'], 1, '%')]];
  const link = el('a', { class: 'btn btn--sm', href: stockHref(s.main, market) }, t('u1.today.open_stock', { n: s.main_name }));
  link.addEventListener('click', () => rememberStockStrategy(lead.key));   // 和旧界面一致：点进个股页时带上这个策略
  return el('section', { class: 'card' },
    el('div', { class: 'card__head' }, el('h2', { class: 'card__title' }, t('u1.today.advice', { n: lead.label })), actBadge(lead.action)),
    el('div', { class: 'u1-big' }, el('span', { class: 'num' }, ratio(lead.next_total)), el('span', { class: 'muted' }, t('u1.today.next_target'))),
    el('span', { class: 'progress', style: { margin: '8px 0' } }, el('span', { class: 'progress__bar', style: { '--p': Math.min(1, lead.next_total || 0).toFixed(3) } })),
    el('div', { class: 'u1-pair muted' }, el('span', {}, t('u1.today.now'), ' ', el('b', {}, lead.now)), el('span', {}, t('u1.today.next'), ' ', el('b', {}, lead.next))),
    el('p', { class: 'u1-reason' }, reason),
    el('dl', { class: 'ws-dl' }, ...stats.flatMap(([k, v]) => [el('dt', {}, k), el('dd', { class: 'num' }, v)])),
    el('p', { class: 'eyebrow u1-why-title' }, t('u1.today.why')),
    ...subs.map((c) => whyRow(c.label, actBadge(c.action), ratio(c.next_total))),
    bench ? [whyRow(t('u1.today.vs_ma300'), '', num(bench.ma300_gap, 1, '%', true), toneClass(bench.ma300_gap)),
      whyRow('RSI(14)', '', num(bench.rsi14, 1)), whyRow(t('u1.today.dd1y'), '', num(bench.dd1y, 1, '%', true), toneClass(bench.dd1y))] : null,
    el('p', { class: 'ws-note' }, t('u1.today.why_note')),
    link);
}

/** 其他策略表 */
function strategyTable(others) {
  const head = ['u1.col.strategy', 'u1.col.signal', 'u1.col.target', 'u1.today.xsharpe', 'u1.today.maxdd'];
  const body = others.map((c) => (c.error
    ? el('tr', {}, el('td', {}, c.label), el('td', { colspan: '4', class: 'muted' }, c.error))
    : el('tr', {}, el('td', {}, c.label), el('td', {}, actBadge(c.action)), el('td', { class: 'num' }, ratio(c.next_total)),
      el('td', { class: 'num' }, num(c.stats.ExcessSharpe, 2)), el('td', { class: 'num' }, num(c.stats['MaxDD%'], 1, '%')))));
  return el('section', { class: 'card card--flush' },
    el('div', { style: { padding: '16px 20px 0' } }, cardHead(t('u1.today.others'))),
    el('div', { class: 'table-wrap' }, el('table', { class: 'table' },
      el('thead', {}, el('tr', {}, ...head.map((k, i) => el('th', { scope: 'col', class: i > 1 ? 'num' : null }, t(k))))),
      el('tbody', {}, ...body))));
}

function listCard(title, items, emptyText) {
  return el('section', { class: 'card' }, cardHead(title),
    items.length ? el('ul', { class: 'u1-list' }, ...items) : el('p', { class: 'muted' }, emptyText));
}

/** 自选 / 观察股票表：点名称去新个股页（G01） */
function watchTable(watch) {
  const cols = [['u1.col.stock', 0], ['u1.today.close', 1], ['u1.today.d1', 1], ['u1.today.ytd', 1], ['u1.today.ma300', 1], ['u1.today.vol20', 1], ['u1.today.dd1y', 1], ['RSI(14)', 1]];
  const rows = watch.map((w) => el('tr', {},
    el('td', {}, el('a', { href: stockHref(w.symbol, market) }, w.name), ' ', el('span', { class: 'mono muted' }, w.symbol)),
    el('td', { class: 'num' }, num(w.close)), el('td', { class: `num ${toneClass(w.d1)}` }, num(w.d1, 2, '%', true)),
    el('td', { class: `num ${toneClass(w.ytd)}` }, num(w.ytd, 1, '%', true)), el('td', { class: `num ${toneClass(w.ma300_gap)}` }, num(w.ma300_gap, 1, '%', true)),
    el('td', { class: 'num' }, num(w.vol, 1, '%')), el('td', { class: `num ${toneClass(w.dd1y)}` }, num(w.dd1y, 1, '%', true)), el('td', { class: 'num' }, num(w.rsi14, 1))));
  return el('section', { class: 'card card--flush' },
    el('div', { style: { padding: '16px 20px 0' } }, cardHead(t('u1.today.watch'))),
    el('div', { class: 'table-wrap' }, el('table', { class: 'table' },
      el('thead', {}, el('tr', {}, ...cols.map(([k, n]) => el('th', { scope: 'col', class: n ? 'num' : null }, k.startsWith('u1') ? t(k) : k)))),
      el('tbody', {}, ...rows))));
}

function marketSwitch(markets, names, onPick) {
  if (markets.length < 2) return null;
  return el('div', { class: 'segmented', role: 'group', 'aria-label': t('u1.today.market') },
    ...markets.map((m) => {
      const b = el('button', { class: 'segmented__btn', type: 'button', 'aria-pressed': String(m === market) }, names[m]);
      b.addEventListener('click', () => onPick(m));
      return b;
    }));
}

export async function render(root) {
  const meta = await getJSON('/api/meta');
  const markets = Object.keys(meta.markets ?? {});
  const saved = store.get(MARKET_KEY);
  if (!market) market = markets.includes(saved) ? saved : (meta.default_market ?? markets[0] ?? 'cn');
  const names = Object.fromEntries(markets.map((m) => [m, meta.markets[m].name]));
  const [s, tracking] = await Promise.all([
    getJSON(`/api/summary?market=${market}`),
    getJSON(`/api/tracking?market=${market}`).catch((e) => { if (e.auth) throw e; return {}; }),   // 提醒读不到不影响主体
  ]);
  const host = root.closest('main');
  const pick = (m) => {
    market = m; store.set(MARKET_KEY, m);
    render(root).catch((e) => root.replaceChildren(softError(e)));
  };
  host.querySelector('[data-bind="market"]')?.replaceChildren(...[marketSwitch(markets, names, pick)].filter(Boolean));
  const metaEl = host.querySelector('[data-bind="meta"]');
  if (s.error) {
    metaEl.textContent = '';
    root.replaceChildren(noteCard(s.error));
    return;
  }
  // G03：数据日期在下面的日期行里已经醒目显示，页头只留更新时间；词条未合并时照旧显示完整一行
  metaEl.textContent = g3('g03.head.updated', { g: s.generated }, t('u1.today.meta', { d: s.data_date, tz: s.timezone_label || '', g: s.generated }));
  const bench = s.watch.find((w) => w.symbol === s.main) ?? null;
  const lead = s.strategies.find((c) => c.key === 'blend2' && !c.error) ?? s.strategies.find((c) => !c.error) ?? null;
  const notifs = (tracking.notifications ?? []).slice(0, 6).map((n) => el('li', {}, el('div', { class: 'muted' }, n.time), n.body));
  const personal = meta.personal_mode === true;
  const pd = personal ? await personalData(market) : null;
  const list = usableList(pd?.list?.v?.r);
  const held = pd?.paper?.v?.pf ? matchPositions(pd.paper.v.pf.positions, list) : [];
  root.replaceChildren(...[
    dateNotice(s.data_date, { market, tz: s.timezone_label || '' }),   // A07：数据日期醒目显示 + 过期提示条
    briefGrid([   // G03：首屏。系统健康提示（s.health）以前单独一条在最上面，现在并进「需要注意」
      personal ? actionsCard(market, s, pd.list) : null,
      positionsCard({ s, lead, personal, paper: pd?.paper ?? null, held, listOk: Boolean(list) }),
      riskCard(riskItems({ s, list, held }))]),
    detailsHead(),
    overview(s, bench, lead, names[market] ?? market),
    el('div', { class: 'ws-grid ws-grid--main u1-main u1-gap' },
      leadCard(s, lead, bench),
      el('div', { class: 'stack', style: { '--gap': '16px' } },
        strategyTable(s.strategies.filter((c) => c !== lead)),
        listCard(t('u1.today.notes'), s.notes.map((n) => el('li', {}, n)), t('u1.today.no_notes')),
        listCard(t('u1.today.notifs'), notifs, t('u1.today.no_notifs')))),
    el('div', { class: 'u1-gap' }, watchTable(s.watch))].filter(Boolean));
}
