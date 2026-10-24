/* ==========================================================
 * 文件: shared/toast.js
 * 版本: 0.1.0 · 更新: 2026-10-23 · 作者: Claude（为李泓锐）
 * 用途: 弹簧 Toast。CSS 用 --ease-spring 弹出；鼠标悬停时暂停倒计时（CSS 暂停进度条，
 *       JS 同步暂停计时器）。区域带 aria-live，读屏会播报。
 * 用法: toast({ title: '已导出', body: 'backtest.csv', ms: 4000 })
 *       或在按钮上写 data-toast="key.title|key.body"（文案走 i18n）
 * ========================================================== */

import { el } from './dom.js';
import { t } from './i18n.js';

let region = null;

function ensureRegion() {
  if (region) return region;
  region = el('div', { class: 'toast-region', role: 'status', 'aria-live': 'polite' });
  document.body.append(region);
  return region;
}

export function toast({ title, body = '', ms = 4000 }) {
  const node = el('div', { class: 'toast', style: { '--toast-ms': `${ms}ms` } },
    el('div', {}, el('div', { class: 'toast__title' }, title), body ? el('div', { class: 'toast__body' }, body) : null),
    el('span', { class: 'toast__timer', 'aria-hidden': 'true' }));
  ensureRegion().append(node);

  let remaining = ms;
  let startedAt = performance.now();
  let timer = setTimeout(close, remaining);

  function close() {
    node.classList.add('is-leaving');
    node.addEventListener('animationend', () => node.remove(), { once: true });
  }
  node.addEventListener('pointerenter', () => {
    clearTimeout(timer);
    remaining -= performance.now() - startedAt;
  });
  node.addEventListener('pointerleave', () => {
    startedAt = performance.now();
    timer = setTimeout(close, Math.max(600, remaining));
  });
}

/** 委托：任何 [data-toast="titleKey|bodyKey"] 被点击时弹出 */
document.addEventListener('click', (event) => {
  const trigger = event.target.closest('[data-toast]');
  if (!trigger) return;
  const [titleKey, bodyKey] = trigger.dataset.toast.split('|');
  toast({ title: t(titleKey), body: bodyKey ? t(bodyKey) : '' });
});
