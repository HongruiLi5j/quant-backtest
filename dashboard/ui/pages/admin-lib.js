/* ==========================================================
 * 文件: ui/pages/admin-lib.js —— 后台页的共用状态和小部件
 *   - S：页面状态（已保存的设置、草稿、当前分区、搜索词）。草稿键沿用旧 /admin 的约定：
 *       registration_open / maintenance_on / maintenance_until   → 直接对应 /api/admin/settings 的键
 *       sections:<页面id>                                          → 保存时合成 sections: { id: { on } }
 *       revoke:<登录id>                                            → 保存时逐个 POST /api/admin/owner/sessions/<id>/revoke
 *   - 开关一律先进草稿，底部出现「有 N 项修改还没保存」，保存时有影响的修改集中确认一次
 *   - 所有文字 textContent（el() 不解析 HTML），不拼 HTML 字符串
 * ========================================================== */

import { el, svg } from '../shared/dom.js';
import { t } from '../shared/i18n.js';

/** 页面状态；redraw 由 admin.js 在启动时挂上 */
export const S = {
  settings: null, overview: null, sessions: null, audit: null,
  draft: {}, view: 'overview', query: '', saving: false, redraw: () => {},
};

export const on = (node, ev, fn) => { node.addEventListener(ev, fn); return node; };

/* ---------- 草稿 ---------- */

export function savedValue(key) {
  const s = S.settings || {};
  if (key.startsWith('sections:')) return ((s.sections || {})[key.slice(9)] || { on: true }).on;
  if (key.startsWith('revoke:')) return false;
  return s[key];
}
export const value = (key) => (key in S.draft ? S.draft[key] : savedValue(key));

/** 改草稿；改回已保存的值就撤销这条草稿 */
export function setDraft(key, next) {
  if (next === savedValue(key)) delete S.draft[key]; else S.draft[key] = next;
  S.redraw();
}
export const toggleDraft = (key) => setDraft(key, !value(key));

/* ---------- 小部件 ---------- */

/** 状态标签：live = 已接入真实接口；need = 后端还没有，灰色不可点 */
export const statusTag = (s) => el('span', { class: `adm-st adm-st--${s === 'live' ? 'live' : 't017'}`, title: t(`adm.st.${s}.tip`) }, t(`adm.st.${s}`));

/** 开关（role=switch）。live 的点了改草稿；locked 的灰色，显示后端已存的值（没有就显示关） */
export function toggle(key, label, { locked = false, checked } = {}) {
  const isOn = locked ? checked === true : value(key) === true;
  const dirty = !locked && key in S.draft;
  const b = el('button', {
    class: `adm-switch${dirty ? ' is-dirty' : ''}`, type: 'button', role: 'switch',
    'aria-checked': String(isOn), 'aria-label': label, disabled: locked, title: locked ? t('adm.need_backend') : null,
  }, el('span', { class: 'adm-switch__knob', 'aria-hidden': 'true' }), el('span', { class: 'adm-switch__text' }, t(isOn ? 'adm.on' : 'adm.off')));
  if (!locked) on(b, 'click', () => toggleDraft(key));
  return b;
}

/** 一行设置：标题 + 标签 + 说明 + 控件；locked 的行在标题旁写「需要后端改动」 */
export function row(r) {
  return el('div', { class: 'adm-row', dataset: { row: r.id } },
    el('div', { class: 'adm-row__text' },
      el('p', { class: 'adm-row__title' }, t(r.title), r.status === 'need' ? ' ' : null, r.status === 'need' ? statusTag(r.status) : null),
      el('p', { class: 'adm-row__hint' }, t(r.hint))),
    el('div', { class: 'adm-row__ctl' }, r.control()));
}

export function card(id, titleKey, children, { flush = false, head = null } = {}) {
  return el('section', { class: `card${flush ? ' card--flush' : ''}`, 'aria-labelledby': `adm-${id}` },
    el('div', { class: 'card__head' }, el('h2', { class: 'card__title', id: `adm-${id}` }, t(titleKey)), head),
    ...children);
}

/** 说明文字；flush 卡片（表格贴边）里要带左右内边距，普通卡片里不用 */
export const note = (key, flush = false) => el('p', { class: flush ? 'adm-lead' : 'adm-lead adm-lead--tight' }, t(key));

export const table = (cols, rows) => el('div', { class: 'table-wrap' }, el('table', { class: 'table adm-table' },
  el('thead', {}, el('tr', {}, ...cols.map((c) => el('th', { scope: 'col' }, c ? t(c) : '')))),
  el('tbody', {}, ...rows)));

export const kpi = (labelKey, val, foot, tone = '') => el('div', { class: 'card metric adm-kpi' },
  el('p', { class: 'metric__label' }, t(labelKey)),
  el('p', { class: `metric__value ${tone}` }, val),
  foot ? el('p', { class: 'metric__foot' }, foot) : null);

/* ---------- 时间 ---------- */

const p2 = (n) => String(n).padStart(2, '0');
export function fmtTime(sec) {
  if (!sec) return '—';
  const d = new Date(sec * 1000);
  return `${d.getFullYear()}-${p2(d.getMonth() + 1)}-${p2(d.getDate())} ${p2(d.getHours())}:${p2(d.getMinutes())}`;
}
export function fmtUptime(sec) {
  const s = Math.max(0, Math.floor(sec || 0));
  return t('adm.uptime', { d: Math.floor(s / 86400), h: Math.floor((s % 86400) / 3600), m: Math.floor((s % 3600) / 60) });
}

/* ---------- 确认框（原生 <dialog>）：有影响的修改在保存时集中确认一次 ---------- */

export function confirmDialog({ title, lines, ok }) {
  return new Promise((resolve) => {
    const dlg = el('dialog', { class: 'dialog adm-confirm', 'aria-label': title },
      el('p', { class: 'card__title' }, title),
      el('ul', { class: 'adm-confirm__body' }, ...lines.map((l) => el('li', {}, l))),
      el('div', { class: 'adm-actions adm-confirm__btns' },
        el('button', { class: 'btn btn--sm btn--ghost', type: 'button', dataset: { act: 'cancel' } }, t('adm.cancel')),
        el('button', { class: 'btn btn--sm adm-danger-fill', type: 'button', dataset: { act: 'ok' } }, ok)));
    let answer = false;
    dlg.addEventListener('click', (e) => {
      const act = e.target.closest('[data-act]')?.dataset.act;
      if (act) { answer = act === 'ok'; dlg.close(); } else if (e.target === dlg) dlg.close();
    });
    dlg.addEventListener('close', () => { dlg.remove(); resolve(answer); });
    document.body.append(dlg);
    dlg.showModal();
  });
}

/* ---------- 需要后台登录（401 / 403 / 外网 404）：只给链接，不在这里处理令牌 ---------- */

export function loginCard() {
  const icon = svg('svg', { class: 'state__icon', 'aria-hidden': 'true' });
  icon.append(svg('use', { href: '../shared/icons.svg#i-cloud-off' }));
  return el('div', { class: 'card state adm-login', role: 'alert' }, icon,
    el('p', { class: 'state__title' }, t('adm.auth.title')),
    el('p', { class: 'muted' }, t('adm.auth.body')),
    el('a', { class: 'btn btn--sm btn--primary', href: '/admin' }, t('adm.auth.go')),
    el('button', { class: 'btn btn--sm btn--ghost', type: 'button', dataset: { action: 'retry' } }, t('state.retry')));
}
