/* ==========================================================
 * 文件: ui/shared/i18n.js（来自 mockups/shared/i18n.js，字典改从 ui/i18n/ 加载）
 * 用途: 中英切换核心。
 *   - HTML 里默认写中文，并标 data-i18n="key"；切换时按字典用 textContent 替换
 *   - 属性翻译：data-i18n-attr="aria-label:key1;placeholder:key2"
 *   - JS 里动态文本用 t('key', { n: 3 })，字典中写 {n} 占位
 *   - 切换后派发 window 事件 'langchange'，页面渲染器监听后重绘动态文本
 * ========================================================== */

import { store } from './dom.js';

const DICTS = {};
let current = 'zh';

/** 各终端的增量词条：i18n/parts/<终端号>.<lang>.json，缺文件就跳过 */
const PARTS = ['U0', 'U1', 'U2', 'U3', 'U4', 'U5'];

async function fetchJSON(url) {
  try {
    const res = await fetch(url, { credentials: 'same-origin' });
    return res.ok ? await res.json() : null;
  } catch {
    return null;   // 网络错误或 JSON 损坏：当作没有这个文件
  }
}

/** 拉取一份字典 = 基础字典 + 所有 parts（同一语言只拉一次）；基础字典失败才算失败 */
async function loadDict(lang) {
  if (DICTS[lang]) return DICTS[lang];
  const base = await fetchJSON(new URL(`../i18n/${lang}.json`, import.meta.url));
  if (!base) throw new Error(`i18n ${lang} 加载失败`);
  const parts = await Promise.all(PARTS.map((id) => fetchJSON(new URL(`../i18n/parts/${id}.${lang}.json`, import.meta.url))));
  DICTS[lang] = Object.assign({}, base, ...parts.filter(Boolean));
  return DICTS[lang];
}

/** 取翻译；找不到 key 时回退中文，再回退 key 本身 */
export function t(key, vars = {}) {
  const raw = DICTS[current]?.[key] ?? DICTS.zh?.[key] ?? key;
  return raw.replace(/\{(\w+)\}/g, (_, name) => (name in vars ? String(vars[name]) : `{${name}}`));
}

export const lang = () => current;

/** 把字典应用到 root 下所有带 data-i18n / data-i18n-attr 的元素 */
export function applyI18n(root = document) {
  const dict = DICTS[current];
  if (!dict) return;
  for (const node of root.querySelectorAll('[data-i18n]')) {
    const text = dict[node.dataset.i18n];
    if (text !== undefined) node.textContent = text;   // 找不到 key 时保留原文
  }
  for (const node of root.querySelectorAll('[data-i18n-attr]')) {
    for (const pair of node.dataset.i18nAttr.split(';')) {
      const [attr, key] = pair.split(':').map((s) => s.trim());
      if (attr && key && dict[key] !== undefined) node.setAttribute(attr, dict[key]);
    }
  }
}

/** 切换语言：拉字典 → 替换文本 → 更新 <html lang> → 通知页面 */
export async function setLang(next) {
  try {
    await Promise.all([loadDict('zh'), loadDict(next)]);
  } catch {
    return;   // 字典拉取失败：保持当前语言，不打断页面
  }
  current = next;
  document.documentElement.lang = next === 'zh' ? 'zh-CN' : 'en';
  store.set('qb-lang', next);
  applyI18n();
  for (const btn of document.querySelectorAll('[data-action="toggle-lang"]')) {
    btn.setAttribute('aria-pressed', String(next === 'en'));
  }
  for (const btn of document.querySelectorAll('[data-action="set-lang"]')) {
    btn.setAttribute('aria-pressed', String(btn.dataset.lang === next));
  }
  window.dispatchEvent(new CustomEvent('langchange', { detail: { lang: next } }));
}

/** 初始化：读用户上次的选择；没选过默认中文 */
export async function initI18n() {
  const saved = store.get('qb-lang', document.documentElement.dataset.defaultLang || 'zh');
  await setLang(saved === 'en' ? 'en' : 'zh');
}

/** 委托点击：[data-action="toggle-lang"] 来回切换；[data-action="set-lang"][data-lang] 直接指定 */
document.addEventListener('click', (event) => {
  const btn = event.target.closest('[data-action="toggle-lang"], [data-action="set-lang"]');
  if (!btn) return;
  if (btn.dataset.action === 'set-lang') setLang(btn.dataset.lang === 'en' ? 'en' : 'zh');
  else setLang(current === 'zh' ? 'en' : 'zh');
});
