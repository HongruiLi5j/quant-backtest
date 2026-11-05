/* ==========================================================
 * 文件: ui/pages/check.js —— 上传收益序列体检（T031/T032/T033）
 * 接口: POST /api/check/upload（后台设置 upload_check_on 默认关闭；关着时接口返回 404，本页显示「功能未开启」）
 * 内容只在浏览器里解析 CSV、在服务器内存里计算，不保存、不记日志。结果文字一律 textContent。
 * 不预测、不推荐、不构成投资建议；通过检查不等于策略有效。
 * ========================================================== */

import { info } from '../shared/glossary.js';   // D111：系数 / t 值 / p 值的 ⓘ
import { el, store } from '../shared/dom.js';
import { t, lang } from '../shared/i18n.js';
import { getJSON, postJSON } from '../shared/api.js';

const MAX_ROWS = 10000;
const MAX_CHARS = 1500000;
const STATUS_CLASS = { pass: 'is-rise', warn: 'is-flat', fail: 'is-fall', na: 'muted' };

/** 浏览器端下载一个文本文件（只用 Blob，不经过服务器，不执行任何内容）。 */
function download(name, text, mime) {
  const url = URL.createObjectURL(new Blob([text], { type: `${mime};charset=utf-8` }));
  const a = el('a', { href: url, download: name });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

const TEMPLATE = 'date,ret,benchmark,turnover\n2020-01-02,0.0012,0.0008,0.15\n2020-01-03,-0.0021,-0.0015,0.10\n';

/** 合成示例：确定性伪随机（固定种子的线性同余），约 3 年日收益，均值接近 0——不是任何真实策略。 */
export function sampleCsv(n = 750) {
  let x = 20261027;
  const rnd = () => { x = (x * 1664525 + 1013904223) % 4294967296; return x / 4294967296; };
  const gauss = () => { const u = Math.max(rnd(), 1e-12); return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * rnd()); };
  const lines = ['date,ret,benchmark,turnover'];
  const d = new Date(Date.UTC(2021, 0, 4));
  for (let i = 0; i < n; i++) {
    while (d.getUTCDay() === 0 || d.getUTCDay() === 6) d.setUTCDate(d.getUTCDate() + 1);
    const m = gauss() * 0.009;
    lines.push(`${d.toISOString().slice(0, 10)},${(0.0003 + m * 0.6 + gauss() * 0.006).toFixed(6)},${(0.0002 + m).toFixed(6)},${(0.1 + rnd() * 0.1).toFixed(3)}`);
    d.setUTCDate(d.getUTCDate() + 1);
  }
  return lines.join('\n') + '\n';
}

/** 把体检结果整理成 Markdown 文本（纯文本拼接，供下载）。 */
export function resultMarkdown(out, zh) {
  const v = out.verdict || {};
  const L = [`# ${zh ? v.title_zh : v.title_en}`, '', zh ? v.zh : v.en, '', '| id | status | detail |', '|---|---|---|'];
  for (const c of out.checks || []) L.push(`| ${c.id} | ${c.status} | ${String(zh ? c.zh : c.en).replace(/\|/g, '/').replace(/\n/g, ' ')} |`);
  L.push('', zh ? '## 局限' : '## Caveats');
  for (const c of out.caveats || []) L.push(`- ${zh ? c.zh : c.en}`);
  L.push('', `> ${zh ? out.disclaimer?.zh : out.disclaimer?.en}`);
  return L.join('\n') + '\n';
}

/** 解析 CSV/表格文本：第 1 列日期(严格 YYYY-MM-DD)，第 2 列日收益(小数，0.01=1%)；可选第 3 列基准收益、第 4 列换手。
 *  只允许第一行是表头(第一列不是日期)；每行列数必须一致(2 到 4 列)；任何一行有问题都整体拒绝，不悄悄丢弃或改写。 */
export function parseCsv(text) {
  const rows = String(text).replace(/^\uFEFF/, '').split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
  if (!rows.length) throw new Error(t('chk.err.empty'));
  const DATE = /^\d{4}-\d{2}-\d{2}$/;
  const split = (l) => l.split(/[,;\t]/).map((x) => x.trim());
  const first = split(rows[0]);
  // 表头只认明确的写法：第一格必须是 date/日期 之类的名字，其余格只含字母/汉字/下划线/空格；其他任何不是日期的首行(含全角数字、NaN 等)都按坏数据整体拒绝
  const isHeader = !DATE.test(first[0]) && /^(date|datetime|day|time|日期|时间|交易日)$/i.test(first[0].replace(/^\uFEFF/, '')) && first.slice(1).every((c) => /^[A-Za-z_\u4e00-\u9fa5 ]+$/.test(c));
  const body = isHeader ? rows.slice(1) : rows;
  if (!body.length) throw new Error(t('chk.err.empty'));
  if (body.length > MAX_ROWS) throw new Error(t('chk.err.rows', { n: MAX_ROWS }));
  const cols = split(body[0]).length;
  if (cols < 2 || cols > 4) throw new Error(t('chk.err.row'));
  if (isHeader && first.length !== cols) throw new Error(t('chk.err.row'));      // 表头列数必须和数据一致
  const num = (x) => { if (x === '') throw new Error(t('chk.err.row')); const v = Number(x); if (!Number.isFinite(v)) throw new Error(t('chk.err.row')); return v; };
  const out = { dates: [], returns: [] };
  if (cols >= 3) out.benchmark = [];
  if (cols >= 4) out.turnover = [];
  for (const line of body) {
    const p = split(line);
    if (p.length !== cols || !DATE.test(p[0])) throw new Error(t('chk.err.row'));
    out.dates.push(p[0]);
    out.returns.push(num(p[1]));
    if (cols >= 3) out.benchmark.push(num(p[2]));
    if (cols >= 4) out.turnover.push(num(p[3]));
  }
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

const f3 = (x) => (x === null || x === undefined || !Number.isFinite(x) ? '—' : x.toFixed(3));
const f5 = (x) => (x === null || x === undefined || !Number.isFinite(x) ? '—' : x.toFixed(5));

function attributionView(fa) {
  if (!fa) return null;
  if (fa.status !== 'ok') {
    return el('div', { class: 'card' }, el('h3', {}, t('chk.fa.title')), el('p', { class: 'muted' }, t('chk.fa.na', { n: fa.n_obs })), fa.note ? el('p', { class: 'muted' }, fa.note) : null);
  }
  const rows = Object.entries(fa.coef).map(([k, v]) => el('tr', {}, el('td', {}, k), el('td', { class: 'num' }, f5(v.coef)), el('td', { class: 'num' }, f3(v.t)), el('td', { class: 'num' }, f3(v.p))));
  return el('div', { class: 'card' },
    el('h3', {}, t('chk.fa.title')),
    el('p', { class: 'muted' }, t('chk.fa.sum', { model: fa.model, n: fa.n_obs, a: fa.first, b: fa.last, r2: f3(fa.r2), alpha: f3(fa.alpha_annual) })),
    el('div', { class: 'tablewrap' }, el('table', { class: 'table' },
      el('thead', {}, el('tr', {}, el('th', {}, t('chk.fa.col.term')), el('th', {}, t('chk.fa.col.coef'), info('coef')), el('th', {}, t('chk.fa.col.t'), info('tstat')), el('th', {}, t('chk.fa.col.p'), info('pval'))),),
      el('tbody', {}, ...rows))),
    el('p', { class: 'muted' }, t('chk.fa.caveat')));
}

/** D41：功能开关是否打开。原来要把整张表填完、点「开始体检」才告诉你「未开启」。
 *  开关从 /api/meta 的 features.upload_check 读（只读、本机才有这个字段；页面启动器已经请求过 meta，有缓存）。
 *  以前用「空 POST 看 404」探测，tester 指出每次打开都留一条红色 404、还多发一个 POST，改掉了。
 *  字段缺失（旧后端 / 远程）时不挡，交给提交时的正常报错。 */
async function uploadCheckOn() {
  try {
    const meta = await getJSON('/api/meta', { mock: 'u2-meta' });
    return meta?.features?.upload_check !== false;
  } catch {
    return true;
  }
}

export async function render(root) {
  const on = await uploadCheckOn();
  const ta = el('textarea', { class: 'input', rows: '8', placeholder: '2020-01-02,0.0012,0.0008,0.15', 'aria-label': t('chk.csv') });
  const file = el('input', { type: 'file', accept: '.csv,.txt,text/csv,text/plain', 'aria-label': t('chk.csv') });   // axe：文件框要有可访问名称
  const trials = el('input', { class: 'input', type: 'number', min: '1', step: '1', value: '1' });
  const trades = el('input', { class: 'input', type: 'number', min: '0', step: '1' });
  const split = el('input', { class: 'input', type: 'date' });
  const bp = el('input', { class: 'input', type: 'number', min: '0', max: '1000', step: '0.1' });
  const inc = el('select', { class: 'input' }, el('option', { value: '' }, t('chk.cost.unknown')),
    el('option', { value: 'true' }, t('chk.cost.net')), el('option', { value: 'false' }, t('chk.cost.gross')));
  const fm = el('select', { class: 'input' }, el('option', { value: '' }, t('chk.fm.none')), el('option', { value: 'ff3' }, t('chk.fm.ff3')), el('option', { value: 'ff4' }, t('chk.fm.ff4')));
  const msg = el('div', { class: 'muted', role: 'status' });
  const out = el('div', {});
  const btn = el('button', { class: 'btn', type: 'button' }, t('chk.run'));
  const tplBtn = el('button', { class: 'btn btn--sm btn--ghost', type: 'button' }, t('chk.btn.template'));
  const sampleBtn = el('button', { class: 'btn btn--sm btn--ghost', type: 'button' }, t('chk.btn.sample'));
  const exportBtn = el('button', { class: 'btn btn--sm btn--ghost', type: 'button', hidden: true }, t('chk.btn.export'));
  let lastResult = null;
  tplBtn.addEventListener('click', () => download('returns_template.csv', TEMPLATE, 'text/csv'));
  sampleBtn.addEventListener('click', () => { ta.value = sampleCsv(); trials.value = '5'; msg.textContent = t('chk.sample.note'); });
  exportBtn.addEventListener('click', () => { if (lastResult) download('strategy_check.md', resultMarkdown(lastResult, lang() === 'zh'), 'text/markdown'); });

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
      if (fm.value) body.factor_model = fm.value;
      btn.disabled = true;
      msg.textContent = t('chk.running');
      const res = await postJSON('/api/check/upload', body);
      msg.textContent = '';
      out.replaceChildren(resultView(res), ...[attributionView(res.factor_attribution)].filter(Boolean));
      lastResult = res;
      exportBtn.hidden = false;
    } catch (e) {
      msg.textContent = e.status === 404 ? t('chk.off') : e.status === 429 ? t('chk.busy') : (e.body?.error || e.message || t('ui.state.error'));
    } finally {
      btn.disabled = !on;
    }
  });

  const offNote = on ? null : el('p', { class: 'is-warn', role: 'status' }, t('chk.off'));
  if (!on) btn.disabled = true;   // 开关关着：一进页面就说清楚，「开始体检」不可点（模板 / 示例按钮照常能用）
  root.replaceChildren(el('div', { class: 'card' },
    offNote,
    el('p', {}, t('chk.intro')),
    el('p', { class: 'muted' }, t('chk.privacy')),
    field('chk.csv', ta, 'chk.csv.hint'), file,
    field('chk.trials', trials, 'chk.trials.hint'),
    field('chk.trades', trades), field('chk.split', split),
    field('chk.cost.flag', inc), field('chk.bp', bp, 'chk.bp.hint'), field('chk.fm', fm, 'chk.fm.hint'),
    el('div', { class: 'row' }, btn, tplBtn, sampleBtn, exportBtn), msg), out);
}
