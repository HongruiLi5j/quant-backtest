/* ==========================================================
 * 文件: shared/custom-cursor.js
 * 版本: 0.1.0 · 更新: 2026-10-23 · 作者: Claude（为李泓锐）
 * 用途: 自定义光标：发光圆点 + 跟随光环（lerp 平滑）。
 *   - 悬停可点击元素：光环放大
 *   - 悬停数字（.num / [data-ticker]）：光环变竖线
 *   - 悬停图表（[data-cursor="chart"]）：隐藏光环，让图表十字线接管
 *   - 只在 (pointer: fine) 且允许动效时启用；系统光标始终保留（可访问性）
 * ========================================================== */

import { el, finePointer, motionOK, cssVar } from './dom.js';

export function initCursor() {
  if (!finePointer() || !motionOK()) return;
  const dot = el('div', { class: 'cursor-dot', 'aria-hidden': 'true' });
  const ring = el('div', { class: 'cursor-ring', 'aria-hidden': 'true' });
  document.body.append(dot, ring);

  const lerp = cssVar('--cursor-lerp', 0.18);
  let tx = -100;
  let ty = -100;
  let rx = tx;
  let ry = ty;

  window.addEventListener('pointermove', (e) => {
    tx = e.clientX;
    ty = e.clientY;
    dot.style.transform = `translate3d(${tx}px, ${ty}px, 0)`;
    document.documentElement.classList.add('has-cursor');
  }, { passive: true });

  document.addEventListener('pointerleave', () => document.documentElement.classList.remove('has-cursor'));

  // 光环状态：用事件委托，只在 pointerover 时判断一次，不在每帧查 DOM
  document.addEventListener('pointerover', (e) => {
    const t = e.target;
    ring.classList.toggle('is-chart', !!t.closest('[data-cursor="chart"]'));
    ring.classList.toggle('is-text', !!t.closest('.num, [data-ticker], .metric__value'));
    ring.classList.toggle('is-link', !!t.closest('a, button, [role="button"], [role="tab"], summary, label'));
  });

  const frame = () => {
    rx += (tx - rx) * lerp;
    ry += (ty - ry) * lerp;
    ring.style.transform = `translate3d(${rx}px, ${ry}px, 0)`;
    requestAnimationFrame(frame);
  };
  requestAnimationFrame(frame);
}
