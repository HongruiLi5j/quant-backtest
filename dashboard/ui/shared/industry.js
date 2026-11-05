/* ==========================================================
 * 文件: ui/shared/industry.js
 * 用途: R8 证监会行业名的显示。接口给的是「C15酒、饮料和精制茶制造业」（门类字母 + 两位代码 + 中文名）；
 *   英文界面按 3 位代码查字典 ind.<代码> 换成英文名，中文界面和查不到时保持原样（不改接口原值，筛选 / 网址参数不受影响）。
 * ========================================================== */
import { t } from './i18n.js';

/** 只改显示：「C15酒、饮料…」→ 英文界面显示 "Beverages and refined tea manufacturing"；不是这种格式原样返回 */
export function industryName(raw) {
  const s = String(raw ?? '');
  if (s.length < 4) return s;
  const key = `ind.${s.slice(0, 3)}`;
  const v = t(key);
  return v === key || !v ? s.slice(3) : v;
}
