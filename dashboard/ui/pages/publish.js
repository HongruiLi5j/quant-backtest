/* ==========================================================
 * 文件: ui/pages/publish.js —— 公开站发布页（N23，样式 A：三步走）
 * 所有者 2026-10-24 的决定：网站只做「导出并检查 + 本机预览 + 给出命令」，上传由所有者在终端手动执行；网站本身不上传、不碰 Cloudflare 登录。
 * 接口:
 *   GET  /api/publish/latest                                   → 最近一次检查结果、与上次已发布的差异、命令文字（N23 新增，只读）
 *   POST /api/admin/action { action: 'publish_check' }         → 导出并检查（T020 步骤 2；后台设置 publish_check_on 默认关闭）
 * 有发布阻断时接口不给上传命令，这里显示锁。输出一律 textContent，不拼 HTML。
 * ========================================================== */

import { el } from '../shared/dom.js';
import { t } from '../shared/i18n.js';
import { getJSON, postJSON } from '../shared/api.js';
import { toast } from '../shared/toast.js';

let lastOutput = '';

const dot = (lvl) => el('i', { class: `sys-dot sys-dot--${lvl}`, 'aria-hidden': 'true' });
const line = (lvl, text) => el('p', { class: 'pub-line' }, dot(lvl), text);

function copyBox(cmd) {
  const code = el('code', { class: 'pub-cmd mono' }, cmd);
  const btn = el('button', { class: 'btn btn--sm btn--ghost', type: 'button' }, t('pub.n23.copy'));
  btn.addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(cmd); toast({ title: t('pub.n23.copied') }); }
    catch {
      const r = document.createRange(); r.selectNodeContents(code);
      const s = window.getSelection(); s.removeAllRanges(); s.addRange(r);
      toast({ title: t('pub.n23.select') });
    }
  });
  return el('div', { class: 'pub-cmdbox' }, code, btn);
}

function list(titleKey, items, lvl) {
  if (!items?.length) return null;
  return el('section', { class: 'card pub-list' }, el('h2', { class: 'card__title' }, dot(lvl), ` ${t(titleKey, { n: items.length })}`),
    el('ul', {}, ...items.map((x) => el('li', {}, x))));
}

function diffCard(d) {
  if (!d) return null;
  const files = (key) => (d[key]?.length ? el('details', {}, el('summary', {}, t(`pub.n23.diff.${key}`, { n: d[`n_${key}`] })),
    el('ul', { class: 'mono pub-files' }, ...d[key].map((f) => el('li', {}, f)))) : null);
  return el('section', { class: 'card pub-list' }, el('h2', { class: 'card__title' }, t('pub.n23.diff.title')),
    el('p', { class: 'muted' }, d.baseline ? t('pub.n23.diff.sum', { a: d.n_added, c: d.n_changed, r: d.n_removed }) : t('pub.n23.diff.nobase', { a: d.n_added })),
    files('added'), files('changed'), files('removed'));
}

function stepCheck(r, rerender) {
  const msg = el('p', { class: 'muted', role: 'status' });
  const btn = el('button', { class: 'btn btn--sm btn--primary', type: 'button' }, t(r ? 'pub.n23.rerun' : 'pub.chk.run'));
  btn.addEventListener('click', async () => {
    msg.textContent = t('pub.chk.running');
    btn.disabled = true;
    try {
      const res = await postJSON('/api/admin/action', { action: 'publish_check' }, { timeoutMs: 330000 });   // 后台最长跑 300 秒，页面多等 30 秒
      lastOutput = String(res.output ?? '');
      toast({ title: t(res.ok ? 'pub.chk.pass' : 'pub.chk.fail') });
      await rerender();
    } catch (e) {
      msg.textContent = e.auth ? t('pub.n23.needadmin') : (e.body?.error || e.message || t('ui.state.error'));
      btn.disabled = false;
    }
  });
  const body = [];
  if (r?.status === 'ok') {
    body.push(line(r.ok ? 'ok' : 'bad', r.ok ? t('pub.n23.ok', { n: r.files }) : t('pub.n23.notok', { n: r.errors.length })));
    body.push(line(r.blockers.length ? 'bad' : 'ok', r.blockers.length ? t('pub.n23.blocked', { n: r.blockers.length }) : t('pub.n23.noblock')));
    if (r.warnings.length) body.push(line('warn', t('pub.n23.warns', { n: r.warnings.length })));
  } else {
    body.push(el('p', { class: 'muted' }, t('pub.n23.never')));
  }
  return el('section', { class: 'card pub-step' }, el('h2', { class: 'card__title' }, el('span', { class: 'pub-n' }, '1'), t('pub.n23.s1')),
    ...body, el('p', { class: 'muted pub-small' }, t('pub.n23.s1.hint')), btn, msg,
    lastOutput ? el('details', {}, el('summary', {}, t('pub.n23.raw')), el('pre', { class: 'mono pub-raw' }, lastOutput)) : null);
}

function stepPreview(r) {
  const c = r?.commands || {};
  return el('section', { class: 'card pub-step' }, el('h2', { class: 'card__title' }, el('span', { class: 'pub-n' }, '2'), t('pub.n23.s2')),
    el('p', {}, t('pub.n23.s2.body')),
    c.preview ? copyBox(c.preview) : el('p', { class: 'muted' }, t('pub.n23.s2.none')),
    c.preview_stop ? el('p', { class: 'muted pub-small' }, t('pub.n23.s2.stop')) : null,
    c.preview_stop ? copyBox(c.preview_stop) : null);
}

function stepUpload(r) {
  const c = r?.commands || {};
  const head = el('h2', { class: 'card__title' }, el('span', { class: 'pub-n' }, '3'), t('pub.n23.s3'));
  if (r?.is_published) return el('section', { class: 'card pub-step' }, head, line('ok', t('pub.n23.done', { id: r.build_id })));
  if (!c.deploy) {
    const why = r?.status !== 'ok' ? t('pub.n23.lock.none') : !r.ok ? t('pub.n23.lock.fail') : t('pub.n23.lock.block', { n: r.blockers.length });
    return el('section', { class: 'card pub-step pub-step--locked' }, head, el('p', { class: 'pub-lock' }, t('pub.n23.lock'), ' ', why));
  }
  return el('section', { class: 'card pub-step' }, head,
    el('p', {}, t('pub.n23.s3.body')), copyBox(c.deploy),
    el('p', { class: 'muted pub-small' }, t('pub.n23.s3.mark')), copyBox(c.mark));
}

export async function render(root) {
  const r = await getJSON('/api/publish/latest', { mock: 'publish_latest' });
  const head = document.querySelector('[data-bind="meta"]');
  if (head) {
    const at = r.built_at ? r.built_at.replace('T', ' ').slice(0, 16) + ' UTC' : '—';
    head.textContent = t('pub.n23.head', { at, pub: r.published?.build_id || t('pub.n23.nopub') });
  }
  const rerender = () => render(root);
  root.replaceChildren(el('div', { class: 'pub' },
    el('p', { class: 'muted pub-intro' }, t('pub.n23.intro')),
    el('div', { class: 'pub-steps' }, stepCheck(r, rerender), stepPreview(r), stepUpload(r)),
    r.status === 'ok' ? list('pub.n23.l.blockers', r.blockers, 'bad') : null,
    r.status === 'ok' ? list('pub.n23.l.errors', r.errors, 'bad') : null,
    r.status === 'ok' ? list('pub.n23.l.warnings', r.warnings, 'warn') : null,
    r.status === 'ok' ? diffCard(r.diff) : null));
}
