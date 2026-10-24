/* 今日页·个人模式区块（U1）：今日动作（主策略清单的统计）、个人模拟账户（仅本机）。
 * 对应旧界面 loadPersonalToday。每个区块独立失败，不拖垮整页；401/403 显示“登录已过期”。 */
import { el, fmt } from '../shared/dom.js';
import { t } from '../shared/i18n.js';
import { getJSON } from '../shared/api.js';
import { num, ratio, isNum, noteCard, cardHead, pageHref, softError, isLocalHost, tr } from './U1-common.js';

const ORDER = ['卖出', '减仓', '买入', '加仓'];   // 风险优先：先看要卖的

async function actionsCard(market, s) {
  const head = cardHead(t('u1.today.actions'), el('a', { href: pageHref('signals.html') }, t('u1.today.all_signals')));
  if (market !== 'cn') return el('section', { class: 'card' }, head, el('p', { class: 'muted' }, t('u1.today.us_unsupported')));
  const main = await getJSON('/api/main_strategy');
  if (!main.main) return el('section', { class: 'card' }, head, el('p', { class: 'muted' }, t('u1.today.no_main')));
  const r = await getJSON(`/api/trade_list?strategy=${encodeURIComponent(main.main)}`);
  if (r.error || r.empty) return el('section', { class: 'card' }, head, el('p', {}, r.error || r.message || ''), el('p', { class: 'muted' }, t('u1.today.no_actions')));
  const age = (Date.now() - new Date(`${r.data_date}T15:00:00+08:00`)) / 86400000;
  const stale = !Number.isFinite(age) || age > 3;
  const lead = s.strategies.find((c) => c.key === 'blend2' && !c.error);
  const counts = ORDER.map((a) => {
    const n = (r.rows ?? []).filter((x) => x.action === a).length;
    return el('div', { class: 'metric' }, el('span', { class: 'metric__label' }, tr('u1.act', a)), el('span', { class: 'metric__value' }, String(n)));
  });
  return el('section', { class: 'card' }, head,
    el('p', { class: 'muted' }, `${r.strategy} · ${r.data_date} · ${r.generated_at}`),
    el('p', { class: 'muted' }, t('u1.today.source_note')),
    stale ? el('p', { class: 'is-warn' }, t('u1.today.stale')) : null,
    el('div', { class: 'u1-counts' }, ...counts,
      el('div', { class: 'metric' }, el('span', { class: 'metric__label' }, t('u1.today.exceptions')), el('span', { class: 'metric__value' }, String((r.exceptions ?? []).length)))),
    lead ? el('p', { class: 'muted' }, t('u1.today.port_target', { n: lead.label, v: ratio(lead.next_total) })) : null);
}

async function paperCard(market) {
  const head = cardHead(t('u1.today.paper'));
  if (!isLocalHost()) return el('section', { class: 'card' }, head, el('p', { class: 'muted' }, t('u1.today.paper_local')));
  const me = await getJSON('/api/member/me');
  if (!me.user) return el('section', { class: 'card' }, head, el('p', { class: 'muted' }, t('u1.today.paper_none')));
  const pf = await getJSON(`/api/member/portfolio?market=${market}`);
  if (pf.error || !isNum(pf.total)) return el('section', { class: 'card' }, head, el('p', { class: 'muted' }, t('u1.today.paper_unavail')));
  const rows = (pf.positions ?? []).map((p) => el('tr', {}, el('td', { class: 'mono' }, p.code), el('td', {}, p.name), el('td', { class: 'num' }, String(p.qty))));
  return el('section', { class: 'card' }, head,
    el('p', { class: 'muted' }, t('u1.today.paper_note')),
    el('p', {}, el('b', { class: 'num' }, fmt(pf.total, 2)), ' · ', el('span', { class: 'num' }, num(pf.ret_pct, 2, '%', true))),
    rows.length ? el('div', { class: 'table-wrap' }, el('table', { class: 'table' },
      el('thead', {}, el('tr', {}, el('th', { scope: 'col' }, t('u1.col.code')), el('th', { scope: 'col' }, t('u1.col.name')), el('th', { scope: 'col', class: 'num' }, t('u1.today.qty')))),
      el('tbody', {}, ...rows))) : null);
}

/** 返回要插在页面里的节点数组；每块出错各自显示提示卡 */
export async function personalSections(market, s) {
  const safe = (p) => p.catch((e) => softError(e));
  const [actions, paper] = await Promise.all([safe(actionsCard(market, s)), safe(paperCard(market))]);
  return [el('div', { class: 'u1-personal u1-gap' }, actions, paper, noteCard(t('u1.today.risk_note')))];
}
