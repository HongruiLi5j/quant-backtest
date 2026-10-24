/* ==========================================================
 * 文件: ui/pages/admin.js —— 后台（页面入口，由 shared/page-boot.js 调用 render）
 * 布局来自设计稿 mockups/…/w/admin.html：左侧分区栏、顶部搜索、开关行、底部「有 N 项修改还没保存」。
 * 登录：后台接口要管理令牌或主人通道。401 / 403（外网没有主人身份时是 404）→ 页面显示「需要后台登录」+ 指向 /admin 的链接，
 *       页面里不碰令牌。旧的 /admin 页面保留作退路。
 * 预览：地址栏 ?mock=1 读 ui/mock-data/admin_*.json（字段和真实接口一致），保存不发请求、不改任何数据。
 * 保存：一次 POST /api/admin/settings（只含改动的键），再逐个退出被标记的登录设备；
 *       有影响的修改（开注册、开维护、关页面、退出设备）集中确认一次。
 * ========================================================== */

import { el } from '../shared/dom.js';
import { t } from '../shared/i18n.js';
import { toast } from '../shared/toast.js';
import { getJSON, postJSON, isMock } from '../shared/api.js';
import { S, on, loginCard, confirmDialog } from './admin-lib.js';
import { VIEWS, buildView } from './admin-views.js';

let host = null;
let wired = false;

/** 需要登录：401 / 403，以及外网没有主人身份时后台一律返回的 404 */
const needLogin = (err) => err && [401, 403, 404].includes(err.status);

/** settings 必须成功（也用来判断有没有登录）；其余接口失败只是那一块显示「暂不可用」 */
async function load() {
  S.settings = await getJSON('/api/admin/settings');
  const soft = (p) => p.catch(() => null);
  const [overview, sessions, audit] = await Promise.all([
    soft(getJSON('/api/admin/overview')), soft(getJSON('/api/admin/owner/sessions')), soft(getJSON('/api/admin/audit'))]);
  S.overview = overview;
  S.sessions = sessions;
  S.audit = audit && Array.isArray(audit.items) ? audit.items : [];
}

/* ---------- 保存 ---------- */

const sectionName = (id) => t(`ui.nav.${id}`);

/** 草稿里有影响的修改，写成确认框里的一行行说明 */
function riskLines() {
  const d = S.draft, lines = [];
  if (d.registration_open === true) lines.push(t('adm.confirm.reg'));
  if (d.maintenance_on === true) lines.push(t('adm.confirm.maint'));
  const closing = Object.entries(d).filter(([k, v]) => k.startsWith('sections:') && v === false).map(([k]) => sectionName(k.slice(9)));
  if (closing.length) lines.push(t('adm.confirm.sections', { names: closing.join(' / ') }));
  const n = Object.keys(d).filter((k) => k.startsWith('revoke:')).length;
  if (n) lines.push(t('adm.confirm.revoke', { n }));
  return lines;
}

async function save() {
  const keys = Object.keys(S.draft);
  if (!keys.length || S.saving) return;
  const lines = riskLines();
  if (lines.length && !await confirmDialog({ title: t('adm.confirm.title', { n: lines.length }), lines, ok: t('adm.confirm.ok') })) return;
  if (isMock()) { toast({ title: t('adm.mock_nosave') }); return; }   // 预览：不发请求，草稿保留，方便反复看
  S.saving = true;
  S.redraw();
  const body = {}, secs = {};
  const revokes = [];
  for (const [k, v] of Object.entries(S.draft)) {
    if (k.startsWith('revoke:')) revokes.push(k.slice(7));
    else if (k.startsWith('sections:')) secs[k.slice(9)] = { on: v };
    else body[k] = v;
  }
  if (Object.keys(secs).length) body.sections = secs;
  let saved = 0, out = 0;
  try {
    if (Object.keys(body).length) {
      const res = await postJSON('/api/admin/settings', body);
      if (res.settings) S.settings = res.settings;
      saved = (res.changed || []).length;
      for (const k of Object.keys(S.draft)) if (!k.startsWith('revoke:')) delete S.draft[k];
    }
    for (const id of revokes) {
      try { await postJSON(`/api/admin/owner/sessions/${encodeURIComponent(id)}/revoke`, {}); out += 1; delete S.draft[`revoke:${id}`]; }
      catch (err) { toast({ title: t('adm.err.revoke'), body: err.message }); }   // 失败的留在草稿里，其余继续
    }
    toast({ title: t('adm.saved', { n: saved, m: out }) });
  } catch (err) {
    const key = err.body?.key;
    toast({ title: needLogin(err) ? t('adm.auth.title') : t('adm.err.save'), body: key ? `${err.message} · ${key}` : err.message });
  }
  S.saving = false;
  try { await load(); } catch { /* 刷新失败：保留当前显示 */ }
  S.redraw();
}

/* ---------- 绘制 ---------- */

function navBar() {
  const nav = el('nav', { class: 'adm-nav', 'aria-label': t('adm.nav_aria') },
    ...VIEWS.map((v) => {
      const count = v === 'sections' ? Object.keys(S.draft).filter((k) => k.startsWith('sections:')).length
        : v === 'site' ? Object.keys(S.draft).filter((k) => !k.includes(':')).length
          : v === 'system' ? Object.keys(S.draft).filter((k) => k.startsWith('revoke:')).length : 0;
      return on(el('button', { class: 'adm-nav__item', type: 'button', 'aria-current': v === S.view && !S.query ? 'page' : null },
        el('span', {}, t(`adm.view.${v}`)), count ? el('span', { class: 'adm-nav__count' }, String(count)) : null), 'click', () => {
        S.view = v;
        S.query = '';
        const box = document.querySelector('[data-adm-search]');
        if (box) box.value = '';
        S.redraw();
      });
    }));
  return el('div', {},
    nav,
    el('div', { class: 'adm-legend' },
      el('div', { class: 'adm-legend__item' }, el('span', { class: 'adm-st adm-st--t017' }, t('adm.st.need')), t('adm.st.need.tip')),
      el('a', { class: 'btn btn--sm btn--ghost', href: '/admin' }, t('adm.open_classic'))));
}

function saveBar() {
  const n = Object.keys(S.draft).length;
  if (!n) return null;
  return el('div', { class: 'adm-savebar', role: 'status' },
    el('span', {}, t('adm.unsaved', { n })),
    on(el('button', { class: 'btn btn--sm btn--ghost', type: 'button' }, t('adm.discard')), 'click', () => { S.draft = {}; S.redraw(); }),
    on(el('button', { class: 'btn btn--sm btn--primary', type: 'button', disabled: S.saving }, t('adm.save')), 'click', save));
}

function draw() {
  const bar = saveBar();
  host.replaceChildren(el('div', { class: 'adm' }, navBar(), el('div', { class: 'adm-main' }, ...buildView(S.view))), ...(bar ? [bar] : []));
}

/** 只绑一次：顶部搜索、离开页面提醒 */
function wire() {
  if (wired) return;
  wired = true;
  document.querySelector('[data-adm-search]')?.addEventListener('input', (e) => { S.query = e.target.value.trim().toLowerCase(); S.redraw(); });
  window.addEventListener('beforeunload', (e) => { if (Object.keys(S.draft).length) { e.preventDefault(); e.returnValue = ''; } });
}

export async function render(root) {
  host = root;
  S.redraw = draw;
  wire();
  const tag = document.querySelector('[data-bind="meta"]');
  if (tag) tag.textContent = t(isMock() ? 'adm.env.mock' : 'adm.env.live');
  try {
    await load();
  } catch (err) {
    if (needLogin(err)) { host.replaceChildren(loginCard()); return; }
    throw err;   // 其他错误交给 page-boot 的统一错误卡片
  }
  draw();
}
