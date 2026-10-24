/* ==========================================================
 * 文件: shared/dom.js
 * 版本: 0.1.1 · 更新: 2026-10-24 · 作者: Claude（为李泓锐）
 * 用途: 极小的 DOM 工具。所有文本一律走 textContent（XSS 防护），本项目禁止拼 HTML 字符串。
 * ========================================================== */

/**
 * 创建元素：el('td', { class: 'num', dataset: { k: 1 } }, '12.3')
 * children 可以是字符串（按纯文本写入）或 Node，不解析 HTML。
 */
export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'dataset') for (const [k, v] of Object.entries(value)) { if (v !== null && v !== undefined && v !== false) node.dataset[k] = v; }   // null / false 的 data-* 不写（否则会变成字符串 "null"）
    else if (key === 'style') for (const [p, v] of Object.entries(value)) node.style.setProperty(p, v);
    else node.setAttribute(key, value === true ? '' : String(value));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

/** 创建 SVG 元素（图标、迷你图用） */
export function svg(tag, attrs = {}) {
  const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, String(value));
  return node;
}

export const qs = (sel, root = document) => root.querySelector(sel);
export const qsa = (sel, root = document) => [...root.querySelectorAll(sel)];

/** 用户是否允许动效（系统「减少动态效果」打开时返回 false） */
export const motionOK = () => !window.matchMedia('(prefers-reduced-motion: reduce)').matches;

/** 是否是精确指针（鼠标/触控板）；触屏返回 false */
export const finePointer = () => window.matchMedia('(pointer: fine)').matches;

/** 读 CSS 变量；数值型返回 number，读不到返回 fallback */
export function cssVar(name, fallback = '', root = document.documentElement) {
  const raw = getComputedStyle(root).getPropertyValue(name).trim();
  if (!raw) return fallback;
  const n = Number(raw);
  return Number.isFinite(n) && typeof fallback === 'number' ? n : raw;
}

/** 安全读写 localStorage（隐私模式下可能抛错） */
export const store = {
  get(key, fallback = null) {
    try { return localStorage.getItem(key) ?? fallback; } catch { return fallback; }
  },
  set(key, value) {
    try { localStorage.setItem(key, value); } catch { /* 隐私模式忽略 */ }
  },
};

/** 数字格式化：fmt(12.345, 2, '%') → "12.35%"；signed=true 时正数带 + 号 */
export function fmt(value, digits = 2, unit = '', signed = false) {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  const s = Number(value).toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits });
  return (signed && value > 0 ? '+' : '') + s + unit;
}

/** 涨跌 class：正 is-rise / 负 is-fall / 零 is-flat */
export const toneClass = (v) => (v > 0 ? 'is-rise' : v < 0 ? 'is-fall' : 'is-flat');
