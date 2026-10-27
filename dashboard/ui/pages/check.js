/* ==========================================================
 * 文件: ui/pages/check.js —— 上传收益序列体检（T031/T032/T033）
 * 接口: POST /api/check/upload（后台设置 upload_check_on 默认关闭；关着时接口返回 404，本页显示「功能未开启」）
 * 内容只在浏览器里解析 CSV、在服务器内存里计算，不保存、不记日志。结果文字一律 textContent。
 * 不预测、不推荐、不构成投资建议；通过检查不等于策略有效。
 * ========================================================== */

import { el, store } from '../shared/dom.js';
import { t, lang } from '../shared/i18n.js';
import { postJSON } from '../shared/api.js';

const MAX_ROWS = 10000;
const MAX_CHARS = 1500000;
const STATUS_CLASS = { pass: 'is-rise', warn: 'is-flat', fail: 'is-fall', na: 'muted' };

/** 解析 CSV/表格文本：第 1 列日期，第 2 列日收益(小数，0.01=1%)；可选第 3 列基准收益、第 4 列换手。自动跳过表头。 */
export function parseCsv(text) {
  const rows = String(text).split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
  if (!rows.length) throw new Error(t('chk.err.empty'));
  const out = { dates: [], returns: [], benchmark: [], turnover: [] };
  let cols = 0;
  for (const line of rows) {
    const p = line.split(/[,;\t]/).map((x) => x.trim());
    if (!/^\d{4}-\d{2}-\d{2}/.test(p[0])) { if (out.dates.length === 0) continue; throw new Error(t('chk.err.row'));
    }
    cols = Math.max(cols, p.length);
    const r = Number(p[1]);
    if (p[1] === '' || !Number.isFinite(r)) throw new Error(t('chk.err.row'));
    out.dates.push(p[0].slice(0, 10));
    out.returns.push(r);
    if (p.length > 2) out.benchmark.push(p[2] === '' ? NaN : Number(p[2]));
    if (p.length > 3) out.turnover.push(p[3] === '' ? NaN : Number(p[3]));
    if (out.dates.length > MAX_ROWS) throw new Error(t('chk.err.rows', { n: MAX_ROWS }));
  }
  if (!out.dates.length) throw new Error(t('chk.err.empty'));
  if (cols < 3) delete out.benchmark;
  else if (out.benchmark.some((x) => !Number.isFinite(x))) throw new Error(t('chk.err.row'));
  if (cols < 4) delete out.turnover;
  else if (out.turnover.some((x) => !Number.isFinite(x))) throw new Error(t('chk.err.row'));
  return out;
}

const field = (labelKey, input, hintKey) => el('label', { class: 'field' },
  el('span', { class: 'field__label' }, t(labelKey)), input, hintKey ? el('span', { class: 'muted' }, t(hintKey)) : null);

function resultView(out) {
  const zh = lang() === 'zh';
  const v = out.verdict || {};
  const rows = (out.checks || []).map((c) => el('tr', {},
    el('td', {}, c.id),
    el('td', { class: STATUS_CLASS[c.status] || '' }, t(`chk.status.${c.status}`)),
    el('td', {}, zh ? c.zh : c.en)));
  const list = (items) => el('ul', {}, ...(items || []).map((x) => el('li', {}, zh ? x.zh : x.en)));
  const up = out.upload || {};
  return el('div', { class: 'card' },
    el('h2', {}, zh ? v.title_zh : v.title_en),
    el('p', {}, zh ? v.zh : v.en),
    up.warnings?.length ? el('div', { class: 'card u1-note is-warn', role: 'alert' }, ...up.warnings.map((w) => el('div', {}, w))) : null,
    up.missing_inputs?.length ? el('p', { class: 'muted' }, t('chk.missing', { items: up.missing_inputs.join(', ') })) : null,
    el('div', { class: 'tablewrap' }, el('table', { class: 'table' },
      el('thead', {}, el('tr', {}, el('th', {}, t('chk.col.item')), el('th', {}, t('chk.col.status')), el('th', {}, t('chk.col.detail')))),
      el('tbody', {}, ...rows))),
    el('h3', {}, t('chk.caveats')), list(out.caveats),
    el('p', { class: 'muted' }, zh ? out.disclaimer?.zh : out.disclaimer?.en));
}

export async function render(root) {
  const ta = el('textarea', { class: 'input', rows: '8', placeholder: '2020-01-02,0.0012,0.0008,0.15', 'aria-label': t('chk.csv') });
  const file = el('input', { type: 'file', accept: '.csv,.txt,text/csv,text/plain' });
  const trials = el('input', { class: 'input', type: 'number', min: '1', step: '1', value: '1' });
  const trades = el('input', { class: 'input', type: 'number', min: '0', step: '1' });
  const split = el('input', { class: 'input', type: 'date' });
  const bp = el('input', { class: 'input', type: 'number', min: '0', max: '1000', step: '0.1' });
  const inc = el('select', { class: 'input' }, el('option', { value: '' }, t('chk.cost.unknown')),
    el('option', { value: 'true' }, t('chk.cost.net')), el('option', { value: 'false' }, t('chk.cost.gross')));
  const msg = el('div', { class: 'muted', role: 'status' });
  const out = el('div', {});
  const btn = el('button', { class: 'btn', type: 'button' }, t('chk.run'));

  file.addEventListener('change', () => {
    const f = file.files?.[0];
    if (!f) return;
    if (f.size > MAX_CHARS) { msg.textContent = t('chk.err.size'); return; }
    const r = new FileReader();
    r.onload = () => { ta.value = String(r.result).slice(0, MAX_CHARS); };
    r.readAsText(f);
  });

  btn.addEventListener('click', async () => {
    out.replaceChildren();
    msg.textContent = '';
    try {
      const d = parseCsv(ta.value);
      const body = { dates: d.dates, returns: d.returns, n_trials: Number(trials.value) };
      if (d.benchmark) body.benchmark = d.benchmark;
      if (d.turnover) body.turnover = d.turnover;
      if (trades.value !== '') body.n_trades = Number(trades.value);
      if (split.value) body.split_date = split.value;
      if (bp.value !== '') body.cost_bp = Number(bp.value);
      if (inc.value) body.cost_included = inc.value === 'true';
      btn.disabled = true;
      msg.textContent = t('chk.running');
      const res = await postJSON('/api/check/upload', body);
      msg.textContent = '';
      out.replaceChildren(resultView(res));
    } catch (e) {
      msg.textContent = e.status === 404 ? t('chk.off') : e.status === 429 ? t('chk.busy') : (e.body?.error || e.message || t('ui.state.error'));
    } finally {
      btn.disabled = false;
    }
  });

  root.replaceChildren(el('div', { class: 'card' },
    el('p', {}, t('chk.intro')),
    el('p', { class: 'muted' }, t('chk.privacy')),
    field('chk.csv', ta, 'chk.csv.hint'), file,
    field('chk.trials', trials, 'chk.trials.hint'),
    field('chk.trades', trades), field('chk.split', split),
    field('chk.cost.flag', inc), field('chk.bp', bp, 'chk.bp.hint'),
    btn, msg), out);
}
