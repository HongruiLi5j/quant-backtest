/* 今日页·个人模式区块（U1）：今日动作（主策略清单的统计）、个人模拟账户（仅本机）。
 * 对应旧界面 loadPersonalToday。每个区块独立失败，不拖垮整页；401/403 显示“登录已过期”。
 * G03：先把两份数据（清单、模拟账户）各取一次，再交给首屏的三张卡用——今日动作卡在这里画，
 * 模拟账户并进 today-brief.js 的「持仓变化」卡（逐只对上今天清单的动作），风险条目汇总到「需要注意」卡。 */
import { el } from '../shared/dom.js';
import { t } from '../shared/i18n.js';
import { getJSON } from '../shared/api.js';
import { isNum, cardHead, pageHref, softError, isLocalHost, tr } from './U1-common.js';
import { dateLine, dateBanner } from './today-date.js';
import { g3 } from './today-brief.js';

const ORDER = ['卖出', '减仓', '买入', '加仓'];   // 风险优先：先看要卖的
const TAB = { 卖出: 'sell', 减仓: 'reduce', 买入: 'buy', 加仓: 'add' };   // 动作 → 信号页（个人模式）的标签名，见 signals.js tabsFor()

/** 主策略的已发布清单：{ note } 表示不适用（美股 / 主策略不可用），{ main, r } 是清单本身 */
async function loadList(market) {
  if (market !== 'cn') return { note: 'u1.today.us_unsupported' };
  const main = await getJSON('/api/main_strategy');
  if (!main.main) return { note: 'u1.today.no_main' };
  const r = await getJSON(`/api/trade_list?strategy=${encodeURIComponent(main.main)}`);
  return { main, r };
}

/** 个人模拟账户（仅本机）：{ note } 表示读不到的原因，{ pf } 是账户 */
async function loadPaper(market) {
  if (!isLocalHost()) return { note: 'u1.today.paper_local' };
  const me = await getJSON('/api/member/me');
  if (!me.user) return { note: 'u1.today.paper_none' };
  const pf = await getJSON(`/api/member/portfolio?market=${market}`);
  if (pf.error || !isNum(pf.total)) return { note: 'u1.today.paper_unavail' };
  return { pf };
}

/** 两份数据并行取；各自出错只记在自己身上（{ e }），成功是 { v } */
export async function personalData(market) {
  const settle = (p) => p.then((v) => ({ v }), (e) => ({ e }));
  const [list, paper] = await Promise.all([settle(loadList(market)), settle(loadPaper(market))]);
  return { list, paper };
}

/** 今日动作卡：主策略清单里卖出 / 减仓 / 买入 / 加仓 / 异常各几只。list 是 personalData 的 list */
export function actionsCard(market, s, list) {
  const head = cardHead(t('u1.today.actions'), el('a', { href: pageHref('signals.html') }, t('u1.today.all_signals')));
  const card = (...kids) => el('section', { class: 'card u1-actions' }, head, ...kids);
  if (list.e) return card(softError(list.e));
  if (list.v.note) return card(el('p', { class: 'muted' }, t(list.v.note)));
  const { main, r } = list.v;
  if (r.error || r.empty) return card(el('p', {}, r.error || r.message || ''), el('p', { class: 'muted' }, t('u1.today.no_actions')));
  // G01：每个格子是链接，直接打开信号页对应标签（同一策略），以前要先点顶栏「信号」再点标签，两次点击变一次。
  // 网址参数和信号页 A08 的约定一致（sg_t 策略、sg_k 标签），信号页会按白名单校验
  const counts = ORDER.map((a) => {
    const n = (r.rows ?? []).filter((x) => x.action === a).length;
    return el('a', { class: 'metric u1-count-link', href: pageHref('signals.html', { sg_t: main.main, sg_k: TAB[a] }), dataset: { tab: TAB[a] } },
      el('span', { class: 'metric__label' }, tr('u1.act', a)), el('span', { class: 'metric__value' }, String(n)));
  });
  const exc = (r.exceptions ?? []).length;
  // A07：这份买卖清单自己的数据日期；页顶的日期行看的是行情摘要日期。
  // G03：两个日期一样时页顶已经写了，这里不再重复；清单比摘要更旧时才补日期行 + 提示条
  const lagging = typeof r.data_date === 'string' && typeof s.data_date === 'string' && r.data_date < s.data_date;
  // G03：清单只列仓位变化超过阈值（或开 / 清仓）的股票，没有「持有」这一类——说清楚，不自己编一个持有数
  const hold = isNum(r.threshold) ? g3('g03.actions.hold_note', { pp: Math.round(r.threshold * 100) }) : '';
  return card(
    el('p', { class: 'muted u1-actions__src' }, `${r.strategy} · ${r.data_date} · ${r.generated_at}`),
    lagging ? [dateLine(r.data_date, { market }), dateBanner(r.data_date, { market })] : null,
    el('div', { class: 'u1-counts' }, ...counts,
      el('div', { class: 'metric' }, el('span', { class: 'metric__label' }, t('u1.today.exceptions')),
        el('span', { class: `metric__value${exc ? ' is-warn' : ''}` }, String(exc)))),
    hold ? el('p', { class: 'ws-note' }, hold) : null,
    el('p', { class: 'ws-note' }, t('u1.today.source_note')));
}
