/* ==========================================================
 * 文件: ui/shared/t-fallback.js（G01）
 * 用途: A02 的分隔符 / 格式词条（i18n/parts/A02.*.json）还没合并进基础字典时，t() 会原样返回键名，
 *   页面上就露出「a02.sep.semi」「a02.punct.end」这样的字（信号页异常标记列、回测结果里的仓位说明都能看到）。
 *   tDefault() 先走 t()；拿回来的是键名时，改查 parts/A02.<语言>.json 本身——兜底文字就是 A02 部件里的原文，不在代码里另抄一份。
 *   词条合并进基础字典之后 t() 直接返回字典里的值，这里的兜底不再生效。
 *   - 两份部件在模块加载时取一次（顶层 await，和基础字典一样是同源的静态 json）；取不到就当没有，tDefault 退回 t() 的结果
 *   - 只兜底 A02 一个部件：它只有分隔符、标点和格式串，没有需要审校的句子
 * ========================================================== */

import { t, lang } from './i18n.js';

const PARTS = {};

async function loadPart(code) {
  try {
    const res = await fetch(new URL(`../i18n/parts/A02.${code}.json`, import.meta.url), { credentials: 'same-origin' });
    PARTS[code] = res.ok ? await res.json() : {};
  } catch {
    PARTS[code] = {};   // 网络错误或 JSON 损坏：没有兜底，照旧返回 t() 的结果
  }
}

await Promise.all([loadPart('zh'), loadPart('en')]);

/** 和 t() 用法一样；词条缺失时用 parts/A02 的原文兜底（英文缺再用中文，和 t() 的回退顺序一致）；部件里也没有的键照旧返回键名 */
export function tDefault(key, vars = {}) {
  const s = t(key, vars);
  if (s !== key) return s;
  const raw = PARTS[lang()]?.[key] ?? PARTS.zh?.[key];
  return typeof raw === 'string' ? raw.replace(/\{(\w+)\}/g, (_, name) => (name in vars ? String(vars[name]) : `{${name}}`)) : s;
}
