/* ==========================================================
 * 文件: shared/quota-bar.js
 * 版本: 0.1.0 · 更新: 2026-10-23 · 作者: Claude（为李泓锐）
 * 用途: 配额条。「今天已用 2 / 3 次回测 · 00:00 重置」+ 进度条；用完时按钮禁用并加锁。
 *   - 只负责「显示」：数字来自 /api/member/me 的 usage，前端不计数、不据此放行
 *     真正挡住请求的是后端 429 QUOTA_EXCEEDED（BACKEND_SPEC §3.6）
 *   - limit 为 null = 不限：只显示已用次数，不画进度条
 * ========================================================== */

import { el, svg } from './dom.js';
import { t } from './i18n.js';

/**
 * @param {'backtest'|'translate'|'share_links'|'watchlist'} feature
 * @param {{used:number, limit:number|null}} usage
 * @param {{ action?: string, iconsHref?: string }} [opt] action = 主按钮文案 key（可选）
 */
export function quotaBar(feature, usage, opt = {}) {
  const used = usage?.used ?? 0;
  const limit = usage?.limit ?? null;
  const out = limit !== null && used >= limit;
  const text = limit === null ? t(`quota.unlimited.${feature}`, { u: used }) : t(`quota.${feature}`, { u: used, l: limit });
  const box = el('div', { class: out ? 'quota-bar is-out' : 'quota-bar', dataset: { feature } },
    el('p', { class: 'quota-bar__text' }, out ? `${text} · ${t('quota.out')}` : text));
  if (limit !== null) {
    box.append(el('div', {
      class: out ? 'progress progress--warn' : 'progress', role: 'progressbar',
      'aria-valuemin': '0', 'aria-valuemax': String(limit), 'aria-valuenow': String(Math.min(used, limit)), 'aria-label': text,
      style: { '--p': String(Math.min(used / limit, 1)) },
    }, el('span', { class: 'progress__bar' })));
  }
  if (opt.action) {
    const btn = el('button', { class: 'btn btn--sm btn--primary', type: 'button', disabled: out ? '' : null, 'aria-disabled': out ? 'true' : null });
    if (out) {
      const icon = svg('svg', { class: 'btn__icon', 'aria-hidden': 'true' });
      icon.append(svg('use', { href: `${opt.iconsHref ?? '../shared/icons.svg'}#i-lock` }));
      btn.append(icon);
    }
    btn.append(el('span', {}, t(opt.action)));
    box.append(el('div', { class: 'quota-bar__row' }, btn, el('span', { class: 'muted', style: { 'font-size': 'var(--fs-xs)' } }, t('quota.display_only'))));
  }
  return box;
}
