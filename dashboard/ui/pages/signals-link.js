/* 信号页 × 持仓联动（U1，仅个人模式，只读）。对应旧界面 hLink*。
 * 隐私：持仓只放在这个模块的内存里，只用于“持有的卖出/减仓信号置顶 + 行内标注”；
 * 不写进信号数据、不进 CSV 导出、不写浏览器存储；离开页面或重新加载清单时清空。 */
import { el } from '../shared/dom.js';
import { t } from '../shared/i18n.js';
import { getJSON } from '../shared/api.js';

const SELL = ['卖出', '减仓'];
const BUY = ['买入', '加仓'];
export const emptyLink = () => ({ state: 'idle', map: null });

/** 台账不可用和台账为空要分开：不可用时绝不能把卖出信号标成“未持有” */
export async function loadLink() {
  try {
    const r = await getJSON('/api/holdings?trades_limit=1&include_voided=0', { mock: 'u1-holdings' });
    if (r.ok !== true || !Array.isArray(r.positions)) return { state: 'unavailable', map: null };
    // 只留联动要显示的字段，不保留成交明细、总资金、占比
    const map = new Map(r.positions.filter((p) => p && p.code).map((p) => [p.code, { shares: p.shares, avg_cost: p.avg_cost, held_days: p.held_days, floating_pnl: p.floating_pnl, floating_pct: p.floating_pct }]));
    return { state: map.size ? 'ok' : 'empty', map };
  } catch {
    return { state: 'unavailable', map: null };
  }
}

/** 持有的卖出/减仓信号置顶（稳定分区，只改显示顺序） */
export function order(link, rows) {
  if (link.state !== 'ok') return rows;
  const held = (r) => SELL.includes(r.action) && link.map.has(r.code);
  return [...rows.filter(held), ...rows.filter((r) => !held(r))];
}

// 持仓接口里的金额是字符串，只用于显示
const n = (v, d) => (v === null || v === undefined || Number.isNaN(Number(v)) ? '—' : Number(v).toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d }));

/** 行内标注：返回 { cls, node } 或 null */
export function noteFor(link, r) {
  if (link.state !== 'ok') return null;
  const pos = link.map.get(r.code);
  const tag = (text) => el('small', { class: 'u1-sub' }, text);
  if (SELL.includes(r.action)) {
    if (!pos) return { cls: 'u1-link-dim', node: tag(t('u1.link.not_held')) };
    const pnl = pos.floating_pnl === null || pos.floating_pnl === undefined ? '—' : `${n(pos.floating_pnl, 2)}（${n(Number(pos.floating_pct) * 100, 2)}%）`;
    return { cls: 'u1-link-held', node: tag(t('u1.link.held', { s: n(pos.shares, 0), c: n(pos.avg_cost, 3), d: pos.held_days ?? '—', p: pnl })) };
  }
  if (BUY.includes(r.action) && pos) return { cls: 'u1-link-owned', node: tag(t('u1.link.owned')) };
  return null;
}

export function banner(link) {
  if (link.state === 'idle') return null;
  return el('p', { class: 'muted u1-banner' }, t(`u1.link.banner_${link.state}`));
}
