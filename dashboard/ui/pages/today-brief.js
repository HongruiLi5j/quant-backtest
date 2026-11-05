/* 今日页·首屏速览（G03）：站主每天打开第一眼要看的东西放在页面最上面——
 *   数据日期（today-date.js，原样）→ 今日动作（today-personal.js 的 actionsCard）→ 持仓变化 → 需要注意。
 * 市场概览、组合建议详情、其他策略、说明、提醒、关注股票都挪到这三张卡下面，内容不变。
 * 只用已有接口的已有字段（summary、trade_list、member/portfolio），不新增请求、不改接口。
 * 词条在 i18n/parts/G03.*.json。合并进基础字典之前 t() 会原样返回键名：标题退回基础字典里意思相近的已有词条，
 * 说明句直接不显示（不露键名，和 A03 / A07 一样）。所有文本走 el()（textContent），不拼 HTML。 */
import { el, fmt, toneClass } from '../shared/dom.js';
import { t } from '../shared/i18n.js';
import { tDefault } from '../shared/t-fallback.js';
import { actBadge, num, ratio, isNum, tr, cardHead, softError } from './U1-common.js';

const SELL = '卖出', REDUCE = '减仓', BUY = '买入', ADD = '加仓';   // 清单 rows[].action 的取值（后端中文枚举）
const RANK = { [SELL]: 0, [REDUCE]: 1, exc: 2, [ADD]: 3, [BUY]: 4, none: 5 };   // 风险优先：要卖的、异常的排前面
const SHOW = 5;   // 卡片里直接列几只模拟持仓，其余收进折叠

/** G03 词条：字典里有就用；还没合并（t() 返回键名）时用 fallback，默认空串 = 不显示 */
export function g3(key, vars = {}, fallback = '') {
  const s = t(key, vars);
  return s === key ? fallback : s;
}

/** 已发布清单能不能用：有 r、没报错、不是空清单 */
export const usableList = (r) => (r && !r.error && !r.empty ? r : null);

/** 清单是否可能过旧：清单日期北京时间 15:00 起算超过 3 天（和原来今日动作卡的判断一样，只是挪到「需要注意」） */
export function listStale(r, now = Date.now()) {
  const age = (now - new Date(`${r?.data_date}T15:00:00+08:00`)) / 86400000;
  return !Number.isFinite(age) || age > 3;
}

/** 每只模拟持仓在今天清单里的状态（纯函数）：sig = 清单动作 / 'exc'（异常停牌，不在正常清单）/ 'none'（清单无信号）；
 *  没有可用清单时 sig = null（不知道，不显示徽章）。按风险排序，同级按代码 */
export function matchPositions(positions, list) {
  const act = new Map((list?.rows ?? []).map((x) => [String(x.code), x.action]));
  const exc = new Set((list?.exceptions ?? []).map((x) => String(x.code)));
  const sigOf = (code) => (!list ? null : act.get(code) ?? (exc.has(code) ? 'exc' : 'none'));
  return (positions ?? []).map((p) => ({ ...p, sig: sigOf(String(p.code)) }))
    .sort((a, b) => (RANK[a.sig] ?? 9) - (RANK[b.sig] ?? 9) || String(a.code).localeCompare(String(b.code)));
}

/** 「需要注意」的条目（纯函数，返回文字数组）：系统健康提示、清单过旧、异常停牌数、价格缺失数、模拟持仓里的卖出 / 异常。
 *  只列接口里真有的情况，不自己定阈值判断行情好坏 */
export function riskItems({ s, list = null, held = [], now = Date.now() }) {
  const colon = tDefault('a02.sep.colon');
  const codes = (xs) => xs.map((p) => p.code).join(tDefault('a02.sep.list'));
  const out = [...(s?.health ?? [])];
  if (list) {
    if (listStale(list, now)) out.push(t('u1.today.stale'));
    const exc = (list.exceptions ?? []).length;
    if (exc > 0) out.push(g3('g03.risk.exceptions', { n: exc }, `${t('u1.today.exceptions')}${colon}${exc}`));
    if (isNum(list.price_unavailable) && list.price_unavailable > 0) out.push(g3('g03.risk.price', { n: list.price_unavailable }));
  }
  const sells = held.filter((p) => p.sig === SELL || p.sig === REDUCE);
  if (sells.length) out.push(g3('g03.risk.held_sell', { n: sells.length, codes: codes(sells) }, `${tr('u1.act', SELL)} / ${tr('u1.act', REDUCE)}${colon}${codes(sells)}`));
  const bad = held.filter((p) => p.sig === 'exc');
  if (bad.length) out.push(g3('g03.risk.held_exc', { n: bad.length, codes: codes(bad) }, `${t('u1.today.exceptions')}${colon}${codes(bad)}`));
  return out.filter(Boolean);
}

/** 持仓状态徽章 */
function sigBadge(sig) {
  if (sig === null || sig === undefined) return null;
  if (sig === 'exc') return el('span', { class: 'badge badge--warn' }, t('u1.today.exceptions'));
  if (sig === 'none') return el('span', { class: 'badge' }, g3('g03.pos.no_signal', {}, t('u1.act.none')));
  return actBadge(sig);
}

const posItem = (p) => el('li', {}, el('span', { class: 'mono' }, p.code), el('span', { class: 'u1-pos__name' }, p.name),
  el('span', { class: 'muted num' }, `${t('u1.today.qty')} ${p.qty}`), sigBadge(p.sig));

/** 个人模拟账户这一段：总资产 · 累计收益，持仓逐只对上今天清单的动作。paper / held 来自 today-personal.js */
function paperBlock(paper, held, listOk) {
  const head = el('p', { class: 'eyebrow u1-pos__sub' }, t('u1.today.paper'));
  if (paper.e) return el('div', { class: 'u1-pos__paper' }, head, softError(paper.e));
  if (paper.v.note) return el('div', { class: 'u1-pos__paper' }, head, el('p', { class: 'muted' }, t(paper.v.note)));
  const pf = paper.v.pf;
  const n = (sig) => held.filter((p) => p.sig === sig).length;
  const sum = listOk && held.length ? g3('g03.pos.paper_sum', { n: held.length, sell: n(SELL), reduce: n(REDUCE), buy: n(BUY), add: n(ADD), exc: n('exc'), none: n('none') }) : '';
  const rest = held.slice(SHOW);
  return el('div', { class: 'u1-pos__paper' }, head,
    el('p', {}, el('b', { class: 'num' }, fmt(pf.total, 2)), ' · ', el('span', { class: `num ${toneClass(pf.ret_pct)}` }, num(pf.ret_pct, 2, '%', true))),
    sum ? el('p', { class: 'ws-note' }, sum) : null,
    held.length ? el('ul', { class: 'u1-pos__list' }, ...held.slice(0, SHOW).map(posItem)) : el('p', { class: 'muted' }, g3('g03.pos.empty', {}, '—')),
    rest.length ? el('details', { class: 'u1-fold' }, el('summary', {}, g3('g03.pos.more', { n: rest.length }, `+${rest.length}`)),
      el('ul', { class: 'u1-pos__list' }, ...rest.map(posItem))) : null);
}

/** 持仓变化卡：组合策略在基准品种上「当前仓位 → 目标仓位」+ 动作（这里能看到「持有」）；个人模式再加模拟持仓对照今天清单 */
export function positionsCard({ s, lead, personal = false, paper = null, held = [], listOk = false }) {
  const title = g3('g03.pos.title', {}, t('u1.today.target'));
  const cell = (label, value, strong) => el('span', { class: 'u1-pos__cell' }, el('span', { class: 'metric__label' }, label),
    el(strong ? 'b' : 'span', { class: 'num' }, tr('u1.act', value)));
  const leadPart = lead ? [
    el('p', { class: 'muted u1-pos__label' }, `${lead.label} · ${s.main_name}`),
    el('div', { class: 'u1-pos__move' }, cell(t('u1.today.now'), lead.now, false), el('span', { class: 'u1-pos__arrow', 'aria-hidden': 'true' }, '→'),
      cell(t('u1.today.next'), lead.next, true), actBadge(lead.action)),
    el('span', { class: 'progress' }, el('span', { class: 'progress__bar', style: { '--p': Math.min(1, lead.next_total || 0).toFixed(3) } })),
    personal ? el('p', { class: 'ws-note' }, t('u1.today.port_target', { n: lead.label, v: ratio(lead.next_total) })) : null,
  ] : [el('p', { class: 'muted' }, t('u1.today.no_strategy'))];
  return el('section', { class: 'card u1-pos' }, cardHead(title), ...leadPart,
    personal && paper ? paperBlock(paper, held, listOk) : null);
}

/** 需要注意卡：有条目时琥珀色描边（不用通用的 is-warn：它会把整张卡的字都染成琥珀色），并标 role=status
 *  （系统健康提示以前是单独一条 role=alert，挪进来后仍是可朗读的提示区）；没有条目时一句「暂无额外提示」（词条未合并就空着）；
 *  最后固定一句通用风险提示 */
export function riskCard(items) {
  const none = g3('g03.risk.none');
  return el('section', { class: `card u1-risk${items.length ? ' u1-risk--on' : ''}`, role: items.length ? 'status' : null },
    cardHead(g3('g03.risk.title', {}, t('cmp.demo.warn'))),
    items.length ? el('ul', { class: 'u1-list u1-risk__list' }, ...items.map((x) => el('li', {}, x))) : (none ? el('p', { class: 'muted' }, none) : null),
    el('p', { class: 'ws-note' }, t('u1.today.risk_note')));
}

/** 首屏三张卡（非个人模式没有「今日动作」，两张） */
export function briefGrid(cards) {
  const list = cards.filter(Boolean);
  return el('div', { class: `u1-brief${list.length > 2 ? ' u1-brief--3' : ''}` }, ...list);
}

/** 首屏下面的「详细数据」小标题；词条未合并就不放 */
export function detailsHead() {
  const text = g3('g03.details');
  return text ? el('p', { class: 'eyebrow u1-details' }, text) : null;
}
