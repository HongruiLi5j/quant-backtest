/* ==========================================================
 * 文件: ui/shared/glossary.js
 * 用途: D10（CP1）指标解释。全站统一的术语表：每个指标一条定义（公式 / 统计区间 / 注意事项），
 *   词条在 i18n 的 gl.<术语> 里（中英文同一次提交）。页面在指标名后面放一个 ⓘ：
 *   - 真正的 <button>，键盘可以 Tab 到、回车 / 空格打开；手机上点一下打开（title 悬停在触屏上看不到）
 *   - 说明框用 aria-describedby 关联按钮，aria-expanded 标开关状态；Esc 或焦点离开时关闭
 *   - 说明框用 position: fixed 按按钮位置放，避免被表格的横向滚动容器裁掉
 * 词条缺失（t() 返回键名）时不显示 ⓘ，不把键名露给用户。所有文字走 textContent。
 * ========================================================== */
import { el } from './dom.js';
import { t } from './i18n.js';

let seq = 0;
let openTip = null;   // 同一时间只开一个说明框

function close(tip, btn) {
  tip.hidden = true;
  btn.setAttribute('aria-expanded', 'false');
  if (openTip?.tip === tip) openTip = null;
}

function place(tip, btn) {
  const r = btn.getBoundingClientRect();
  const w = Math.min(320, window.innerWidth - 32);
  const left = Math.max(16, Math.min(r.left, window.innerWidth - w - 16));
  tip.style.setProperty('width', `${w}px`);
  tip.style.setProperty('left', `${left}px`);
  // 下方放不下就放到按钮上方
  const below = r.bottom + 6;
  tip.style.setProperty('top', `${below}px`);
  const h = tip.getBoundingClientRect().height;
  if (below + h > window.innerHeight - 8 && r.top - h - 6 > 8) tip.style.setProperty('top', `${r.top - h - 6}px`);
}

/** 术语存在吗（词条已在字典里） */
export const hasTerm = (term) => t(`gl.${term}`) !== `gl.${term}`;

/** 一个 ⓘ 按钮 + 说明框；术语不存在返回 null */
export function info(term) {
  if (!hasTerm(term)) return null;
  const id = `gl-${term}-${++seq}`;
  const tip = el('span', {
    class: 'gl-tip', id, role: 'tooltip', hidden: true,
    style: { position: 'fixed', 'z-index': '1000', padding: '10px 12px', 'border-radius': '8px', 'font-size': '13px', 'line-height': '1.55',
      'font-weight': '400', 'font-family': 'var(--font-ui, inherit)', 'letter-spacing': 'normal', 'text-transform': 'none', 'text-align': 'left', 'white-space': 'normal', background: 'var(--bg-card, #fff)',
      color: 'var(--text-primary, inherit)', border: '1px solid var(--border-strong, rgba(127,127,127,.35))', 'box-shadow': '0 6px 24px rgba(0,0,0,.18)' },
  }, t(`gl.${term}`));
  const btn = el('button', {
    type: 'button', class: 'gl-i', 'aria-label': t('gl._label', { n: t(`gl.${term}.name`) === `gl.${term}.name` ? term : t(`gl.${term}.name`) }),
    'aria-describedby': id, 'aria-expanded': 'false',
    style: { border: '0', background: 'transparent', color: 'var(--text-secondary, inherit)', cursor: 'help', padding: '2px 4px',
      'min-width': '24px', 'min-height': '24px', 'font-size': '13px', 'line-height': '1', 'vertical-align': 'middle' },
  }, 'ⓘ');
  btn.addEventListener('click', (e) => {
    e.stopPropagation();   // 不触发表头排序之类的外层点击
    if (!tip.hidden) { close(tip, btn); return; }
    if (openTip) close(openTip.tip, openTip.btn);
    tip.hidden = false;
    btn.setAttribute('aria-expanded', 'true');
    place(tip, btn);
    openTip = { tip, btn };
  });
  btn.addEventListener('keydown', (e) => { if (e.key === 'Escape') close(tip, btn); });
  btn.addEventListener('blur', () => close(tip, btn));
  return el('span', { class: 'gl', style: { 'white-space': 'nowrap' } }, btn, tip);
}

/** 指标名 + ⓘ（术语缺失时只有指标名）。返回可以直接放进 el() 的子节点数组 */
export const withInfo = (label, term) => [label, info(term)].filter(Boolean);

// 页面滚动或窗口变化时关掉打开的说明框（fixed 定位不会跟着按钮走）
window.addEventListener('scroll', () => { if (openTip) close(openTip.tip, openTip.btn); }, { passive: true, capture: true });
window.addEventListener('resize', () => { if (openTip) close(openTip.tip, openTip.btn); });
