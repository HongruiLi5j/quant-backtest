/* ==========================================================
 * 文件: ui/pages/admin-views.js —— 后台页各分区的内容
 * 数据字段都按真实接口（以代码为准）：
 *   GET /api/admin/settings        → { default_tier, membership_on, share_links_on, registration_open, maintenance_on,
 *                                      maintenance_until, features{}, quota{}, sections{ <页面id>: { on } } }
 *   GET /api/admin/overview        → { server{pid,started,uptime,python,managed}, share{running,...}, ... }
 *   GET /api/admin/audit           → { items: [{ t, action, ok, actor, ip, country, detail{}, error }] }
 *   GET /api/admin/owner/sessions  → [{ id, device, ip, country, last_active, current }]
 * 已接入的开关：开放注册、维护模式（含预计恢复时间）、分区开关、退出单台登录。
 * 后端还没有的（会员升降、配额生效、队列、站点公告等，等 T017）：灰色，旁边写「需要后端改动」，不假装能用。
 * 公开站访问（accessiblequant.com）：只放跳到 Cloudflare Web Analytics 的链接和开启步骤；本机不取数、不存数、不碰 Cloudflare 令牌。
 * ========================================================== */

import { el } from '../shared/dom.js';
import { t } from '../shared/i18n.js';
import { S, on, value, setDraft, toggle, row, card, note, table, kpi, fmtTime, fmtUptime } from './admin-lib.js';

export const SECTIONS = ['today', 'signals', 'market', 'watch', 'backtest', 'holdings', 'monitor', 'research', 'settings'];
export const VIEWS = ['overview', 'site', 'sections', 'members', 'share', 'public', 'compute', 'system', 'audit'];
const FEATURES = ['member_backtest', 'engines', 'translate'];
const QUOTAS = ['free_backtest', 'pro_backtest', 'free_translate', 'pro_translate'];

const cfg = () => S.settings || {};
const badge = (text) => el('span', { class: 'badge' }, String(text));

function untilInput() {
  const input = el('input', { class: 'field__input mono', type: 'text', maxlength: '40', placeholder: t('adm.until.ph'), 'aria-label': t('adm.row.maintenance_until'), value: String(value('maintenance_until') || '') });
  return on(input, 'change', () => setDraft('maintenance_until', input.value.trim()));
}

/** 所有开关行（分区页和搜索共用）：{ id, view, status: 'live'|'need', title, hint, control } */
export function rows() {
  const c = cfg();
  const need = (id, view, control) => ({ id, view, status: 'need', title: `adm.row.${id}`, hint: `adm.row.${id}.hint`, control });
  const lockedSw = (id, checked) => () => toggle(id, t(`adm.row.${id}`), { locked: true, checked });
  const lockedVal = (v) => () => badge(v ?? '—');
  const link = (labelKey) => () => el('a', { class: 'btn btn--sm btn--ghost', href: '/admin' }, t(labelKey));
  const live = (id, view, control) => ({ id, view, status: 'live', title: `adm.row.${id}`, hint: `adm.row.${id}.hint`, control });
  return [
    live('registration_open', 'site', () => toggle('registration_open', t('adm.row.registration_open'))),
    live('maintenance_on', 'site', () => toggle('maintenance_on', t('adm.row.maintenance_on'))),
    live('maintenance_until', 'site', untilInput),
    live('new_ui_home', 'site', () => toggle('new_ui_home', t('adm.row.new_ui_home'))),
    live('admin_quick_enter', 'site', () => toggle('admin_quick_enter', t('adm.row.admin_quick_enter'))),
    live('upload_check_on', 'site', () => toggle('upload_check_on', t('adm.row.upload_check_on'))),
    live('publish_check_on', 'site', () => toggle('publish_check_on', t('adm.row.publish_check_on'))),
    live('ledger_page_on', 'site', () => toggle('ledger_page_on', t('adm.row.ledger_page_on'))),
    need('online', 'site', lockedSw('online', true)),
    need('guest', 'site', lockedSw('guest', false)),
    need('showcase', 'site', lockedSw('showcase', false)),
    need('banner', 'site', lockedSw('banner', false)),
    need('default_tier', 'site', lockedVal(c.default_tier)),
    ...SECTIONS.map((id) => ({ id: `sec_${id}`, view: 'sections', status: 'live', title: `ui.nav.${id}`, hint: 'adm.sec.hint', control: () => toggle(`sections:${id}`, t(`ui.nav.${id}`)) })),
    need('membership_on', 'members', lockedSw('membership_on', c.membership_on)),
    need('member_tier', 'members', link('adm.open_classic')),
    need('pro_requests', 'members', lockedVal(0)),
    live('tunnel', 'share', () => badge(t(S.overview?.share?.running ? 'adm.on' : 'adm.off'))),
    need('share_links_on', 'share', lockedSw('share_links_on', c.share_links_on)),
    need('queue_paused', 'compute', lockedSw('queue_paused', false)),
    ...FEATURES.map((f) => need(`feat_${f}`, 'compute', lockedSw(`feat_${f}`, (c.features || {})[f]))),
    ...QUOTAS.map((q) => need(`quota_${q}`, 'compute', lockedVal((c.quota || {})[q]))),
    live('restart', 'system', link('adm.open_classic')),
    live('token', 'system', link('adm.open_classic')),
    live('backup', 'system', link('adm.open_classic')),
    need('signout_all', 'system', link('adm.open_classic')),
  ];
}
const pick = (...ids) => rows().filter((r) => ids.includes(r.id));
const ofView = (v) => rows().filter((r) => r.view === v);

/* ---------- 操作记录 ---------- */

function actionLabel(a) {
  const k = `adm.act.${a}`;
  const s = t(k);
  return s === k ? String(a) : s;
}
const detailText = (d) => (d && typeof d === 'object' ? Object.entries(d).map(([k, v]) => `${k}=${v}`).join(' · ') : '');

function auditTable(list, compact = false) {
  if (!list.length) return note('adm.audit.empty', true);
  const cols = compact ? ['adm.audit.ts', 'adm.audit.action', 'adm.audit.result'] : ['adm.audit.ts', 'adm.audit.actor', 'adm.audit.ip', 'adm.audit.country', 'adm.audit.action', 'adm.audit.detail', 'adm.audit.result'];
  return table(cols, list.map((a) => el('tr', { class: a.ok === false ? 'is-off' : null },
    el('td', { class: 'mono' }, a.t || '—'), ...(compact ? [] : [el('td', {}, a.actor || '—'), el('td', { class: 'mono' }, a.ip || '—'), el('td', {}, a.country || '—')]),
    el('td', {}, actionLabel(a.action)), ...(compact ? [] : [el('td', { class: 'mono' }, detailText(a.detail) || a.error || '')]),
    el('td', {}, el('span', { class: `adm-pill ${a.ok === false ? 'adm-pill--off' : 'adm-pill--ok'}` }, t(a.ok === false ? 'adm.audit.fail' : 'adm.audit.ok'))))));
}

/* ---------- 我的登录设备：点「退出此设备」只进草稿，保存时才真的退出 ---------- */

function sessionsCard() {
  const list = S.sessions;
  let body;
  if (!Array.isArray(list)) body = note('adm.sess.unavailable', true);
  else if (!list.length) body = note('adm.sess.empty', true);
  else {
    body = table(['adm.sess.device', 'adm.audit.ip', 'adm.audit.country', 'adm.sess.last', ''], list.map((s) => {
      const key = `revoke:${s.id}`;
      const marked = key in S.draft;
      let act = null;
      if (s.current) act = el('span', { class: 'adm-pill adm-pill--ok' }, t('adm.sess.current'));
      else {
        const b = el('button', { class: `btn btn--sm btn--ghost${marked ? '' : ' adm-danger'}`, type: 'button' }, t(marked ? 'adm.sess.undo' : 'adm.sess.revoke'));
        act = on(b, 'click', () => setDraft(key, !marked));
      }
      return el('tr', { class: marked ? 'is-off' : null }, el('td', {}, s.device || '—'), el('td', { class: 'mono' }, s.ip || '—'), el('td', {}, s.country || '—'),
        el('td', { class: 'mono' }, fmtTime(s.last_active)), el('td', { class: 'adm-td-act' }, act));
    }));
  }
  return card('sessions', 'adm.sess.title', [note('adm.sess.hint', true), body], { flush: true });
}

/* ---------- 公开站访问：数字在 Cloudflare Web Analytics 里看，这里只给入口和说明 ---------- */

const CF_PAGES_PROJECT = 'accessiblequant';
const PUBLIC_LINKS = [
  ['adm.pub.open_wa', 'https://dash.cloudflare.com/?to=/:account/web-analytics', 'btn--primary'],
  ['adm.pub.open_pages', `https://dash.cloudflare.com/?to=/:account/workers-and-pages/view/${CF_PAGES_PROJECT}`, 'btn--ghost'],
  ['adm.pub.open_site', 'https://accessiblequant.com/', 'btn--ghost'],
];

/** 外链一律新标签页打开，并断开 opener、不带来源地址 */
const extLink = ([key, href, tone]) => el('a', { class: `btn btn--sm ${tone}`, href, target: '_blank', rel: 'noopener noreferrer' }, t(key));
const list = (tag, keys) => el(tag, { class: 'adm-pub-list' }, ...keys.map((k) => el('li', {}, t(k))));

function publicVisits() {
  const n = (prefix, count) => Array.from({ length: count }, (_, i) => `${prefix}${i + 1}`);
  return [
    card('public', 'adm.pub.title', [note('adm.pub.lead'),
      el('div', { class: 'adm-pub-links' }, ...PUBLIC_LINKS.map(extLink)),
      el('p', { class: 'adm-lead adm-lead--tight muted' }, t('adm.pub.ext'))]),
    card('public-setup', 'adm.pub.setup.title', [list('ol', n('adm.pub.setup.', 4))]),
    card('public-see', 'adm.pub.see.title', [
      el('div', { class: 'adm-two' },
        el('div', {}, el('h3', { class: 'adm-pub-sub' }, t('adm.pub.see.yes')), list('ul', n('adm.pub.see.y', 5))),
        el('div', {}, el('h3', { class: 'adm-pub-sub' }, t('adm.pub.see.no')), list('ul', n('adm.pub.see.n', 3)))),
      el('p', { class: 'adm-lead adm-lead--foot' }, t('adm.pub.caveat'))]),
  ];
}

/* ---------- 各分区 ---------- */

function overview() {
  const o = S.overview, sv = o?.server;
  const info = [
    [t('adm.kpi.uptime'), sv ? fmtUptime(sv.uptime) : '—'],
    [t('adm.kpi.managed'), sv ? t(sv.managed ? 'adm.yes' : 'adm.no') : '—'],
    [t('adm.kpi.tunnel'), o?.share ? t(o.share.running ? 'adm.on' : 'adm.off') : '—'],
    ['Python', sv ? `${sv.python} · PID ${sv.pid}` : '—'],
  ].map(([k, v]) => `${k} ${v}`).join('  ·  ');
  return [
    el('div', { class: 'adm-two' },
      card('quick', 'adm.quick', pick('registration_open', 'maintenance_on', 'tunnel').map(row)),
      card('recent', 'adm.recent', [auditTable((S.audit || []).slice(0, 5), true)], { flush: true,
        head: on(el('button', { class: 'btn btn--sm btn--ghost', type: 'button' }, t('adm.see_all')), 'click', () => { S.view = 'audit'; S.redraw(); }) })),
    el('p', { class: 'adm-lead adm-lead--tight mono' }, info),
  ];
}

const panel = (id, titleKey, leadKey) => [card(id, titleKey, [note(leadKey), ...ofView(id).map(row)])];

export function buildView(view) {
  if (S.query) {
    const q = S.query;
    const hits = rows().filter((r) => `${t(r.title)} ${t(r.hint)}`.toLowerCase().includes(q));
    return [card('search', 'adm.search.title', hits.length ? hits.map(row) : [note('adm.search.none')])];
  }
  switch (view) {
    case 'site': return panel('site', 'adm.view.site', 'adm.site.lead');
    case 'sections': return panel('sections', 'adm.view.sections', 'adm.sections.lead');
    case 'members': return panel('members', 'adm.view.members', 'adm.members.lead');
    case 'share': return panel('share', 'adm.view.share', 'adm.share.lead');
    case 'public': return publicVisits();
    case 'compute': return panel('compute', 'adm.view.compute', 'adm.compute.lead');
    case 'system': return [...panel('system', 'adm.view.system', 'adm.system.lead'), sessionsCard()];
    case 'audit': return [card('audit', 'adm.view.audit', [note('adm.audit.lead', true), auditTable(S.audit || [])], { flush: true })];
    default: return overview();
  }
}
