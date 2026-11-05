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

/** 大数字的中文简写（D02）：1611594574 → "16.1亿"，53425168 → "5340万"（3 位有效数字）；不到 1 万原样加千分位。
 *  用浏览器自带的 Intl（CLDR 中文规则），不引库；英文界面给 "1.6B"。完整数字请另放 title，导出 CSV 用原始值。 */
export function fmtCN(value, sig = 3) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '—';
  const locale = document.documentElement.lang === 'en' ? 'en-US' : 'zh-CN';
  if (Math.abs(value) < 10000) return Number(value).toLocaleString(locale, { maximumFractionDigits: 0 });
  return new Intl.NumberFormat(locale, { notation: 'compact', maximumSignificantDigits: sig }).format(value);
}

/** D49-3（revtech A11-02）：表格 tbody；没有行时放一行跨满所有列的「暂时没有数据」（读屏 role=status），
 *  不再只剩表头——用户分不清是空的还是坏了。text 由调用方传（一般是 t('ui.state.empty')），这里不依赖 i18n。 */
export function tbodyOrEmpty(rows, cols, text) {
  if (rows.length) return el('tbody', {}, ...rows);
  return el('tbody', {}, el('tr', {}, el('td', { colspan: String(Math.max(1, cols)), class: 'muted table__empty' }, el('span', { role: 'status' }, text))));
}

/** 涨跌 class：正 is-rise / 负 is-fall / 零 is-flat */
export const toneClass = (v) => (v > 0 ? 'is-rise' : v < 0 ? 'is-fall' : 'is-flat');

/** D48（T-9）：下载文件名清理。策略名之类的中文标签里有全角冒号「：」、「+」、空格、斜杠，
 *  在 Windows / 网盘 / 命令行里都麻烦。把这些换成下划线并合并连续下划线；保留中文、字母、数字、点、短横线。 */
export function safeFileName(name) {
  const s = String(name ?? '').replace(/[\\/:*?"<>|：；;，,+\s（）()]+/g, '_').replace(/_+/g, '_').replace(/_(?=\.)|^_+|_+$/g, '');
  return s || 'export.csv';
}
