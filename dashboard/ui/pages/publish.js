/* ==========================================================
 * 文件: ui/pages/publish.js —— 公开站「导出并检查」（T020 步骤 2）
 * 接口: POST /api/admin/action { action: 'publish_check' }（需要后台登录；后台设置 publish_check_on 默认关闭）
 * 只在本机导出并检查，不上传、不联网。上传由所有者在终端手动执行，输出里会给出针对这份快照的命令。
 * 输出一律 textContent（<pre>），不拼 HTML。
 * ========================================================== */

import { el } from '../shared/dom.js';
import { t } from '../shared/i18n.js';
import { postJSON } from '../shared/api.js';

export async function render(root) {
  const out = el('pre', { class: 'mono', style: 'white-space:pre-wrap;overflow:auto;max-height:60vh' });
  const msg = el('div', { class: 'muted', role: 'status' });
  const btn = el('button', { class: 'btn', type: 'button' }, t('pub.chk.run'));
  btn.addEventListener('click', async () => {
    out.textContent = '';
    msg.textContent = t('pub.chk.running');
    btn.disabled = true;
    try {
      const res = await postJSON('/api/admin/action', { action: 'publish_check' }, { timeoutMs: 330000 });   // 后台最长跑 300 秒，页面多等 30 秒
      msg.textContent = t(res.ok ? 'pub.chk.pass' : 'pub.chk.fail');
      out.textContent = String(res.output ?? '');
    } catch (e) {
      msg.textContent = e.auth ? t('ui.auth.expired') : (e.body?.error || e.message || t('ui.state.error'));
    } finally {
      btn.disabled = false;
    }
  });
  root.replaceChildren(el('div', { class: 'card' },
    el('p', {}, t('pub.chk.intro')),
    el('p', { class: 'muted' }, t('pub.chk.manual')),
    btn, msg, out));
}
