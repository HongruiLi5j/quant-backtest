/* ==========================================================
 * 文件: ui/pages/accuracy-headline.js
 * 用途: 「信号准不准」的一句话结论，信号页（signals-extra.js）和今日页（today.js）共用，保证两页说法一致。
 *   约束来自 revquant C-D40：
 *   ① 满 12 个完整交易周之前一律中性——表里最多 16 行，就算信号完全随机，也有约 32–46% 的概率至少一行区间不含 0；
 *   ② 满了以后只用事先指定的两行（买入 5 日、卖出 5 日）的后端 verdict；周数取「买入 5 日」这一行，不写死。
 * ========================================================== */
import { t } from '../shared/i18n.js';

export const isNum = (v) => typeof v === 'number' && Number.isFinite(v);

/** 账本是不是 D16 新算法生成的（行里有 edge 字段） */
export const isNewLedger = (rows) => Array.isArray(rows) && rows.some((p) => p && 'edge' in p);

export function headline(rows, main) {
  const ref = rows.find((p) => p.action === '买入' && p.h === 5 && isNum(p.weeks_needed)) || rows.find((p) => isNum(p.weeks_needed)) || {};
  const w = ref.weeks ?? 0, need = ref.weeks_needed ?? 12;
  if (w < need) return t('c40a.headline_wait', { m: main, w, n: need });
  const pick = (a) => rows.find((p) => p.action === a && p.h === 5)?.verdict || t('u1.na');
  return t('c40a.headline_final', { m: main, b: pick('买入'), s: pick('卖出') });
}
