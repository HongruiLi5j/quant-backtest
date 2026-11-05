/* 今日页（U1）：对应旧界面 #/today。
 * 接口：/api/meta、/api/summary、/api/tracking（个人模式还有 today-personal.js 里的几个）。
 * G03 信息层级（从上到下）：数据日期 → 首屏三张卡（今日动作 / 持仓变化 / 需要注意，见 today-brief.js）→
 * 详细数据（市场概览、组合建议、其他策略、说明、提醒、关注股票，内容和以前一样）。
 * 所有文本走 textContent（el()），不拼 HTML。 */
import { el, toneClass, store, tbodyOrEmpty } from '../shared/dom.js';
import { t, lang } from '../shared/i18n.js';
import { getJSON } from '../shared/api.js';
import { actBadge, num, ratio, noteCard, cardHead, stockHref, softError } from './U1-common.js';
import { rememberStockStrategy } from '../shared/stock-link.js';
import { personalData, actionsCard } from './today-personal.js';
import { g3, usableList, matchPositions, riskItems, positionsCard, riskCard, briefGrid, detailsHead } from './today-brief.js';
import { dateNotice, bannerLines } from './today-date.js';
import { heroOn, heroToggle, heroRow } from './today-hero.js';   // D91：答案优先首屏（预览，默认关）
import { withInfo } from '../shared/glossary.js';   // D10：指标名后面的 ⓘ 解释
import { headline, isNewLedger } from './accuracy-headline.js';
import { welcomeStrip } from './welcome.js';   // D72：第一次来时的「每页做什么」提示条

const MARKET_KEY = 'qb-ui-market';
let market = null;

function metric(label, value, tone = '', foot = null, term = null) {
  return el('div', { class: 'metric' },
    el('span', { class: 'metric__label' }, term ? withInfo(label, term) : label),
    el('span', { class: `metric__value ${tone}` }, value),
    foot ? el('span', { class: 'metric__foot' }, foot) : null);
}

/** 市场概览：基准的 4 个指标。
 * D08（C12）：以前还有「组合目标仓位」和一行风险数字（波动率 / 距 52 周高点 / RSI14），
 * 前者首屏「持仓变化」卡已显示，后者在下方关注表里同一行都有，去掉重复 */
function overview(s, bench, mkName) {
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
      metric(t('u1.today.ytd'), num(bench.ytd, 1, '%', true), toneClass(bench.ytd), null, 'ytd'),
      metric(t('u1.today.ma300'), num(bench.ma300_gap, 1, '%', true), toneClass(bench.ma300_gap), null, 'ma300')));
}

const whyRow = (label, badge, value, tone = '') => el('div', { class: 'u1-why' },
  el('span', {}, label), el('span', {}, badge), el('b', { class: `num ${tone}` }, value));

/** D08（C07）：判断依据 = 每个子策略自己的条件。文字直接用后端 summary.strategies[].reason（不在前端重算），
 *  这样页面写的条件和策略真正用的条件永远一致（以前显示 MA300 / RSI14，没有子策略用它们） */
const whyBlock = (c) => el('div', { class: 'u1-why-item' },
  whyRow(c.label, actBadge(c.action), ratio(c.next_total)),
  c.reason ? el('p', { class: 'ws-note u1-why-reason' }, c.reason) : null);

/** D08（C06）：回测数字的区间写清楚，并补上线以来（真正样本外）的收益，和买入持有对比。
 *  since_launch 是后端算好的累计收益（%），这里只做显示 */
function periodNote(s, lead) {
  const bh = s.strategies.find((c) => c.key === 'buy_hold' && !c.error);
  const parts = [t('c08.stats_period')];
  if (s.launch && Number.isFinite(lead.since_launch)) {
    parts.push(t('c08.since_launch', { d: s.launch, v: num(lead.since_launch, 2, '%', true),
      b: bh && Number.isFinite(bh.since_launch) ? num(bh.since_launch, 2, '%', true) : '—' }));
  }
  return el('p', { class: 'ws-note' }, parts.length > 1 ? withInfo(parts.join(' '), 'since_launch') : parts[0]);
}

/** 组合建议大卡（旧界面 today-hero） */
function leadCard(s, lead) {
  if (!lead) return noteCard(t('u1.today.no_strategy'));
  const subs = s.strategies.filter((c) => c !== lead && !c.error && ['trend_ens', 'donchian_s1', 'rsi2'].includes(c.key));
  const reason = lead.reason;   // D08（C12）：以前空仓时再包一层「当前建议空仓，原因：…」，和原因句本身重复
  const stats = [[withInfo(t('u1.today.cagr'), 'cagr'), num(lead.stats['CAGR%'], 1, '%', true)], [withInfo(t('u1.today.xsharpe'), 'sharpe_cash'), num(lead.stats.ExcessSharpe, 2)],
    [withInfo(t('u1.today.maxdd'), 'maxdd'), num(lead.stats['MaxDD%'], 1, '%')], [withInfo(t('u1.today.volst'), 'vol'), num(lead.stats['Vol%'], 1, '%')]];
  const link = el('a', { class: 'btn btn--sm', href: stockHref(s.main, market) }, t('u1.today.open_stock', { n: s.main_name }));
  link.addEventListener('click', () => rememberStockStrategy(lead.key));   // 和旧界面一致：点进个股页时带上这个策略
  return el('section', { class: 'card' },
    el('div', { class: 'card__head' }, el('h2', { class: 'card__title' }, t('u1.today.advice', { n: lead.label })), actBadge(lead.action)),
    el('div', { class: 'u1-big' }, el('span', { class: 'num' }, ratio(lead.next_total)), el('span', { class: 'muted' }, t('u1.today.next_target'))),
    el('span', { class: 'progress', style: { margin: '8px 0' } }, el('span', { class: 'progress__bar', style: { '--p': Math.min(1, lead.next_total || 0).toFixed(3) } })),
    // D08（C12）：「当前仓位 → 目标仓位」首屏「持仓变化」卡已经有，这里不再重复
    el('p', { class: 'u1-reason' }, reason),
    el('dl', { class: 'ws-dl' }, ...stats.flatMap(([k, v]) => [el('dt', {}, k), el('dd', { class: 'num' }, v)])),
    periodNote(s, lead),
    el('p', { class: 'eyebrow u1-why-title' }, t('u1.today.why')),
    ...subs.map(whyBlock),
    el('p', { class: 'ws-note' }, t('u1.today.why_note')),
    link);
}

/** 其他策略表 */
function strategyTable(others) {
  const head = ['u1.col.strategy', 'u1.col.signal', 'u1.col.target', 'u1.today.xsharpe', 'u1.today.maxdd'];
  const terms = { 'u1.today.xsharpe': 'sharpe_cash', 'u1.today.maxdd': 'maxdd' };
  const body = others.map((c) => (c.error
    ? el('tr', {}, el('td', {}, c.label), el('td', { colspan: '4', class: 'muted' }, c.error))
    : el('tr', {}, el('td', {}, c.label), el('td', {}, actBadge(c.action)), el('td', { class: 'num' }, c.next || ratio(c.next_total)),   // D08（C10）：多品种策略要看出拿的是哪个品种
      el('td', { class: 'num' }, num(c.stats.ExcessSharpe, 2)), el('td', { class: 'num' }, num(c.stats['MaxDD%'], 1, '%')))));
  return el('section', { class: 'card card--flush' },
    el('div', { style: { padding: '16px 20px 0' } }, cardHead(t('u1.today.others'))),
    el('div', { class: 'table-wrap' }, el('table', { class: 'table' },
      el('thead', {}, el('tr', {}, ...head.map((k, i) => el('th', { scope: 'col', class: i > 1 ? 'num' : null }, terms[k] ? withInfo(t(k), terms[k]) : t(k))))),
      tbodyOrEmpty(body, head.length, t('ui.state.empty')))));   // D49-3
}

/** D09（C11）：提醒列表。① 去掉标题里写着其他市场名的（A 股页不显示「量化日报 · 美股」）；
 *  ② 相邻两条标题和正文都相同就合并成一条，显示「×N（最早–最晚）」。
 *  后端 notify.py 已经不再写重复记录（带 count / first_time）；这里兼容改之前留下的旧记录。纯函数，不改数据 */
// 标题里不写市场名、但只属于 A 股的提醒（今日买卖清单、A 股盯盘到价），在其他市场页不显示。
// 标题来源：src/trade_notifications.py「今日买卖 · 日期」、src/market_monitor/scheduler.py「盯盘到价提醒」
const CN_ONLY_PREFIX = ['今日买卖', '盯盘到价提醒'];

/** R8：提醒记录是后端写盘时的中文原文，英文界面只在显示时按固定句式翻译（认不出的原样显示，不改记录、不影响合并去重） */
const nmEn = (v) => { const [, nm, code = ''] = /^(.*?)(\(\w+\))?$/.exec(v); const k = `nm.${nm}`; const r = t(k); return (r === k ? nm : r) + code; };
const actEn = (v) => { const k = `u1.act.${v}`; const r = t(k); return r === k ? v : r; };
export function notifTitle(x) {
  if (lang() !== 'en') return x;
  let m = /^量化日报 · (.+?) (\d{4}-\d{2}-\d{2})$/.exec(x);
  if (m) return t('n9.t.report', { m: nmEn(m[1]), d: m[2] });
  if (/^量化日报 · 每日任务连续失败$/.test(x)) return t('n9.t.report_fail');
  m = /^今日买卖 · (.+)$/.exec(x);
  if (m) return t('n9.t.trades', { d: m[1] });
  if (/^盯盘到价提醒$/.test(x)) return t('n9.t.price');
  return /^盯盘止损提醒$/.test(x) ? t('n9.t.stop') : x;
}
export function notifBody(x) {
  if (lang() !== 'en') return x;
  return x.split(/(?<=。)\s*|；(?=[^；]*（明日目标)/).filter(Boolean).map((seg) => {
    let m = /^自选股和主要 ETF 今天没有新信号。$/.exec(seg);
    if (m) return t('n9.b.nosig');
    m = /^全市场（组合策略）：买入 (\d+) 只，卖出 (\d+) 只。$/.exec(seg);
    if (m) return t('n9.b.scan', { b: m[1], s: m[2] });
    m = /^默认组合买入主要信号 (\d+) 只，卖出 (\d+) 只；本机会话的模拟持仓或自选中有 (\d+) 只出现卖出信号。$/.exec(seg);
    if (m) return t('n9.b.trades', { b: m[1], s: m[2], r: m[3] });
    m = /^(.+?)：(\S+)（明日目标 (\d+)%）$/.exec(seg);
    if (m) return t('n9.b.alert2', { n: nmEn(m[1]), a: actEn(m[2]), p: m[3] });
    m = /^(.+?)：(.+?) (\S+)（明日目标 (\d+)%）$/.exec(seg);
    if (m) { const lk = `n9.l.${m[2]}`; return t('n9.b.alert', { n: nmEn(m[1]), l: t(lk) === lk ? m[2] : t(lk), a: actEn(m[3]), p: m[4] }); }
    m = /^(\S+) (跌破买入目标价|突破卖出目标价|跌破固定止损线|跌破海龟出场线|跌破止损线) (\S+)，现价 (\S+?)(（盘中预警，规则以收盘价为准）)?$/.exec(seg);
    if (m) return t('n9.b.hit', { c: m[1], side: t(/买入/.test(m[2]) ? 'n9.s.down' : /卖出/.test(m[2]) ? 'n9.s.up' : 'n9.s.stop'), th: m[3], px: m[4], tail: m[5] ? t('n9.tail.stop') : '' });
    return seg;
  }).join(' ');
}

export function foldNotifs(items, otherMarketNames = [], market = 'cn') {
  const out = [];
  for (const n of items) {
    const title = String(n?.title || '');
    if (!n || otherMarketNames.some((x) => x && title.includes(x))) continue;
    if (market !== 'cn' && CN_ONLY_PREFIX.some((p) => title.startsWith(p))) continue;
    const prev = out[out.length - 1];
    const c = Number(n.count) > 1 ? Number(n.count) : 1;
    if (prev && prev.title === n.title && prev.body === n.body) {
      prev.count += c;
      prev.first = n.first_time || n.time;   // 列表是新到旧，越往后越早
    } else {
      out.push({ title: n.title, body: n.body, time: n.time, first: n.first_time || n.time, count: c });
    }
  }
  return out;
}

/** 首屏下一行「这些信号准不准」（content 待办，和信号页同一句结论，规则见 accuracy-headline.js）。
 *  账本有 ~75KB，所以页面先画完再取，取不到就整行不显示，不影响首屏 */
async function fillAccuracy(line) {
  try {
    const d = await getJSON('/api/ledger', { mock: 'ledger' });
    const main = d?.main_pick?.main || d?.main;
    const rows = d?.precision?.[main];
    if (!main || !Array.isArray(rows) || !rows.length) return;
    const text = isNewLedger(rows) ? headline(rows, main) : t('c41.acc_old');
    line.replaceChildren(el('b', {}, t('c41.acc_title')), ' ', text, ' ', el('a', { href: 'signals.html' }, t('c41.acc_link')));
    line.hidden = false;
  } catch { /* 取不到就不显示 */ }
}

function listCard(title, items, emptyText) {
  return el('section', { class: 'card' }, cardHead(title),
    items.length ? el('ul', { class: 'u1-list' }, ...items) : el('p', { class: 'muted' }, emptyText));
}

/** D60：本地数据比应有的最新交易日落后时，在名称后标「数据停在 日期」（后端 summary.watch[].behind，A 股才有）。
 */
function staleMark(w) {
  if (!(Number(w.behind) > 0)) return null;
  const txt = t('d60.watch.stale', { d: w.last_date, n: w.behind });
  return el('span', { class: 'badge badge--warn', style: { marginLeft: '6px' } }, txt);
}

/** 自选 / 观察股票表：点名称去新个股页（G01） */
function watchTable(watch) {
  const cols = [['u1.col.stock', 0], ['u1.today.close', 1], ['u1.today.d1', 1], ['u1.today.ytd', 1, 'ytd'], ['u1.today.ma300', 1, 'ma300'], ['u1.today.vol20', 1, 'vol20'], ['u1.today.dd1y', 1, 'dd1y'], ['RSI(14)', 1, 'rsi14']];
  const rows = watch.map((w) => el('tr', {},
    el('td', {}, el('a', { href: stockHref(w.symbol, market) }, w.name), ' ', el('span', { class: 'mono muted' }, w.symbol), staleMark(w)),
    el('td', { class: 'num' }, num(w.close)), el('td', { class: `num ${toneClass(w.d1)}` }, num(w.d1, 2, '%', true)),
    el('td', { class: `num ${toneClass(w.ytd)}` }, num(w.ytd, 1, '%', true)), el('td', { class: `num ${toneClass(w.ma300_gap)}` }, num(w.ma300_gap, 1, '%', true)),
    el('td', { class: 'num' }, num(w.vol, 1, '%')), el('td', { class: `num ${toneClass(w.dd1y)}` }, num(w.dd1y, 1, '%', true)), el('td', { class: 'num' }, num(w.rsi14, 1))));
  return el('section', { class: 'card card--flush' },
    el('div', { style: { padding: '16px 20px 0' } }, cardHead(t('u1.today.watch'))),
    el('div', { class: 'table-wrap' }, el('table', { class: 'table' },
      el('thead', {}, el('tr', {}, ...cols.map(([k, n, term]) => el('th', { scope: 'col', class: n ? 'num' : null },
        term ? withInfo(k.startsWith('u1') ? t(k) : k, term) : (k.startsWith('u1') ? t(k) : k))))),
      tbodyOrEmpty(rows, cols.length, t('ui.state.empty')))));   // D49-3
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
  const notifs = foldNotifs(tracking.notifications ?? [], markets.filter((m) => m !== market).map((m) => names[m]), market)
    .slice(0, 6).map((n) => el('li', {},
      // 灰色行带上标题：标题里有市场和数据日期（例如「量化日报 · A股 2026-11-03」），否则两天的同文提醒看起来一样
      el('div', { class: 'muted' }, [notifTitle(n.title), n.count > 1 ? t('c09.folded', { n: n.count, a: n.first, b: n.time }) : n.time].filter(Boolean).join(' · ')),
      notifBody(n.body)));
  const personal = meta.personal_mode === true;
  const pd = personal ? await personalData(market) : null;
  const list = usableList(pd?.list?.v?.r);
  const held = pd?.paper?.v?.pf ? matchPositions(pd.paper.v.pf.positions, list) : [];
  const accLine = el('p', { class: 'ws-note u1-acc-line', hidden: true });
  if (market === 'cn') fillAccuracy(accLine);
  // D91：「答案优先」预览开着时，一行首屏（目标仓位 + 原因 + 今日动作数 + 数据新鲜度）代替日期提示和前两张卡，「需要注意」照旧
  const hero = heroOn();
  const warn = market === 'cn' && bannerLines(s.data_date).some((l) => ['a07.banner.after_close', 'a07.banner.future', 'a07.banner.unknown'].includes(l.key));
  const redraw = () => render(root).catch((e) => root.replaceChildren(softError(e)));
  root.replaceChildren(...[
    heroToggle(redraw),
    hero ? heroRow({ s, lead, list, mainKey: pd?.list?.v?.main?.main, warn, benchName: bench?.name }) : null,
    hero ? null : dateNotice(s.data_date, { market, tz: s.timezone_label || '', listDate: list?.data_date }),   // A07：数据日期醒目显示 + 过期提示条
    hero ? riskCard(riskItems({ s, list, held })) : briefGrid([   // G03：首屏。系统健康提示（s.health）以前单独一条在最上面，现在并进「需要注意」
      personal ? actionsCard(market, s, pd.list) : null,
      positionsCard({ s, lead, personal, paper: pd?.paper ?? null, held, listOk: Boolean(list) }),
      riskCard(riskItems({ s, list, held }))]),
    welcomeStrip(),   // D90-1：首次访问提示条放到首屏三张卡后面，先给答案再给导览
    market === 'cn' ? accLine : null,
    detailsHead(),
    overview(s, bench, names[market] ?? market),
    el('div', { class: 'ws-grid ws-grid--main u1-main u1-gap' },
      leadCard(s, lead),
      el('div', { class: 'stack', style: { '--gap': '16px' } },
        strategyTable(s.strategies.filter((c) => c !== lead)),
        listCard(t('u1.today.notes'), s.notes.map((n) => el('li', {}, n)), t('u1.today.no_notes')),
        listCard(t('u1.today.notifs'), notifs, t('u1.today.no_notifs')))),
    el('div', { class: 'u1-gap' }, watchTable(s.watch))].filter(Boolean));
}
