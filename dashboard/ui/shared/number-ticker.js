/* ==========================================================
 * 文件: shared/number-ticker.js
 * 版本: 0.1.0 · 更新: 2026-10-23 · 作者: Claude（为李泓锐）
 * 用途: 数字滚动（Number Ticker）。requestAnimationFrame + easeOutQuart，
 *       从旧值平滑滚到新值；变化时加 .is-flash 发一次光。
 * 用法: <span data-ticker data-digits="2" data-unit="%" data-signed>0</span>
 *       tickTo(node, 12.34)  或  initTickers(root) 让进入视口的数字从 0 滚到 data-value
 * ========================================================== */

import { motionOK, fmt } from './dom.js';

const easeOutQuart = (x) => 1 - (1 - x) ** 4;
const running = new WeakMap();

/** 把 node 的数字从当前值滚动到 target */
export function tickTo(node, target, duration = 1100) {
  const digits = Number(node.dataset.digits ?? 2);
  const unit = node.dataset.unit ?? '';
  const signed = 'signed' in node.dataset;
  const from = Number(node.dataset.current ?? 0);
  node.dataset.current = String(target);
  const write = (v) => { node.textContent = fmt(v, digits, unit, signed); };

  if (!motionOK() || from === target) { write(target); return; }
  cancelAnimationFrame(running.get(node));
  const start = performance.now();
  const frame = (now) => {
    const p = Math.min(1, (now - start) / duration);
    write(from + (target - from) * easeOutQuart(p));
    if (p < 1) running.set(node, requestAnimationFrame(frame));
  };
  running.set(node, requestAnimationFrame(frame));
  node.classList.remove('is-flash');
  void node.offsetWidth;          // 重启 CSS 动画
  node.classList.add('is-flash');
}

/** 进入视口时从 0 滚到 data-value（只触发一次） */
export function initTickers(root = document) {
  const nodes = [...root.querySelectorAll('[data-ticker][data-value]')];
  if (!('IntersectionObserver' in window)) {
    nodes.forEach((n) => tickTo(n, Number(n.dataset.value), 0));
    return;
  }
  const io = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue;
      io.unobserve(entry.target);
      tickTo(entry.target, Number(entry.target.dataset.value));
    }
  }, { threshold: 0.4 });
  nodes.forEach((n) => io.observe(n));
}
