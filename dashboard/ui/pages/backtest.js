/* ==========================================================
 * 文件: ui/pages/backtest.js
 * 用途: 回测实验室（旧 #/lab）：选策略 / 品种 / 起始日 / 单边成本 / 参数 → GET /api/backtest → 结果区
 *   - 结果区拆在 backtest-result.js（指标卡、净值图、最近交易）和 backtest-analysis.js（分析）
 *   - 个股页「在回测实验室调参数」会带 ?symbol=&strategy=&market= 过来
 *   - 语言切换会重画整页：表单值和上次结果存在模块变量里，不重新请求；按 R 才重新跑
 *   - 首次进入时的接口错误交给页面启动器（401/403 显示「登录已过期」）；之后点按钮的错误显示在按钮旁
 * ========================================================== */
import { el } from '../shared/dom.js';
import { lang, t } from '../shared/i18n.js';
import { getJSON } from '../shared/api.js';
import { analysisNodes } from './backtest-analysis.js';
import { recentAndTrades, resultNodes } from './backtest-result.js';

const form = { strategy: '', symbol: '', start: '', cost: '', params: {} };
let meta = null;
let market = 'cn';
let last = null;          // 上一次回测结果
let lastLang = null;
let busy = false;
let seeded = false;

const mk = () => meta.markets[market];
const stratLabel = (k) => meta.strategies[k]?.label ?? k;
const names = () => ({ ...mk().universe, ...(mk().stocks ?? {}) });
const defaultCost = (sym) => (mk().universe[sym] === undefined ? mk().stock_cost_bp : mk().cost_bp);
const decimals = (s) => (String(s).split('.')[1] || '').length;

function paramDefaults(key) {
  return Object.fromEntries((meta.strategies[key]?.params ?? []).map((p) => [p.key, Number(p.default)]));
}

/** 品种下拉：自选股 / ETF / 排名靠前的个股 / 其他（从个股页带过来的代码） */
function symbolOptions(extra) {
  const m = mk();
  const group = (label, entries) => (entries.length ? el('optgroup', { label }, ...entries.map(([k, v]) => el('option', { value: k }, `${v || k}（${k}）`))) : null);
  const known = (k) => (m.stocks ?? {})[k] !== undefined || m.universe[k] !== undefined || (m.lab_stocks ?? {})[k] !== undefined;
  return [group(t('u2.bt.g_watch'), Object.entries(m.stocks ?? {})), group('ETF', Object.entries(m.universe)),
    group(t('u2.bt.g_top'), Object.entries(m.lab_stocks ?? {})),
    extra && !known(extra) ? group(t('u2.bt.g_other'), [[extra, extra]]) : null];
}

function seedForm() {
  const q = new URLSearchParams(window.location.search);
  const want = q.get('strategy');
  form.strategy = meta.strategies[want] ? want : (meta.strategies.blend2 ? 'blend2' : Object.keys(meta.strategies)[0]);
  form.symbol = q.get('symbol') || mk().main;
  form.cost = String(defaultCost(form.symbol));
  form.params = paramDefaults(form.strategy);
  seeded = true;
  return Boolean(q.get('symbol') || q.get('strategy'));
}

function field(label, control) {
  return el('label', { class: 'u2-field' }, el('span', { class: 'muted' }, label), control);
}

function buildControls(ui) {
  const strat = el('select', { class: 'ws-select', 'aria-label': t('u2.bt.strategy') },
    ...Object.entries(meta.strategies).map(([k, v]) => el('option', { value: k }, v.label)));
  strat.value = form.strategy;
  const sym = el('select', { class: 'ws-select', 'aria-label': t('u2.bt.symbol') }, ...symbolOptions(form.symbol));
  sym.value = form.symbol;
  const start = el('input', { class: 'ws-input', type: 'date', value: form.start, 'aria-label': t('u2.bt.start') });
  const cost = el('input', { class: 'ws-input u2-narrow', type: 'number', min: '0', max: '100', step: '1', value: form.cost, 'aria-label': t('u2.bt.cost') });
  const desc = el('p', { class: 'ws-note' });
  const params = el('div', { class: 'u2-params' });
  const run = el('button', { class: 'btn btn--primary btn--sm', type: 'button' }, t('u2.bt.run'));
  const reset = el('button', { class: 'btn btn--sm', type: 'button' }, t('u2.bt.reset'));
  ui.run = run; ui.msg = el('span', { class: 'u2-err', role: 'alert' });

  const drawParams = () => {
    const st = meta.strategies[form.strategy];
    desc.textContent = st.desc + (st.kind === 'multi' ? t('u2.bt.multi_note') : '');
    params.replaceChildren(...(st.params.length ? st.params.map((p) => {
      const input = el('input', { class: 'ws-input u2-narrow', type: 'number', min: p.min, max: p.max, step: p.step, dataset: { key: p.key },
        value: Number(form.params[p.key] ?? p.default).toFixed(decimals(p.step)) });
      input.addEventListener('input', () => { form.params[p.key] = Number(input.value); });
      return el('label', { class: 'u2-field', title: p.help || null }, el('span', { class: 'muted' }, p.label + (p.help ? ' ⓘ' : '')), input);
    }) : [el('span', { class: 'muted' }, t('u2.bt.no_params'))]));
  };
  strat.addEventListener('change', () => { form.strategy = strat.value; form.params = paramDefaults(form.strategy); drawParams(); ui.run.click(); });
  sym.addEventListener('change', () => { form.symbol = sym.value; form.cost = String(defaultCost(form.symbol)); cost.value = form.cost; ui.run.click(); });
  start.addEventListener('change', () => { form.start = start.value; });
  cost.addEventListener('input', () => { form.cost = cost.value; });
  reset.addEventListener('click', () => { form.params = paramDefaults(form.strategy); drawParams(); ui.run.click(); });
  params.addEventListener('keydown', (e) => { if (e.key === 'Enter') ui.run.click(); });
  drawParams();
  return el('section', { class: 'card' },
    el('div', { class: 'card__head' }, el('h2', { class: 'card__title' }, t('u2.bt.title')), el('span', { class: 'card__sub' }, t('u2.bt.sub'))),
    el('div', { class: 'u2-form' }, field(t('u2.bt.strategy'), strat), field(t('u2.bt.symbol'), sym), field(t('u2.bt.start'), start), field(t('u2.bt.cost'), cost)),
    desc, params,
    el('div', { class: 'u2-actions' }, run, reset, ui.msg));
}

function showResult(ui, r) {
  const label = stratLabel(r.strategy);
  ui.out.replaceChildren(resultNodes(r, label, names()), ...analysisNodes(r), recentAndTrades(r));
  ui.out.hidden = false;
}

/** 请求参数和旧页面完全一致：market / strategy / symbol / cost / start / params(JSON) */
async function runBacktest(ui, strict) {
  if (busy) return;
  busy = true; ui.run.disabled = true; ui.msg.replaceChildren();
  try {
    const q = new URLSearchParams({ market, strategy: form.strategy, symbol: form.symbol, cost: form.cost, start: form.start, params: JSON.stringify(form.params) });
    const r = await getJSON(`/api/backtest?${q}`, { mock: 'u2-backtest' });
    if (r.error) { ui.msg.textContent = r.error; ui.out.hidden = true; return; }
    last = r; lastLang = lang();
    showResult(ui, r);
  } catch (err) {
    if (strict && err.auth) throw err;
    ui.out.hidden = true;
    ui.msg.replaceChildren(err.auth ? el('span', {}, t('ui.auth.expired'), ' ', el('a', { href: '/' }, t('ui.auth.home'))) : (err.message || t('ui.state.error')));
  } finally {
    busy = false; ui.run.disabled = false;
  }
}

export async function render(root) {
  meta = await getJSON('/api/meta', { mock: 'u2-meta' });
  market = new URLSearchParams(window.location.search).get('market') || meta.default_market;
  if (!meta.markets[market]) market = meta.default_market;
  const preset = !seeded ? seedForm() : false;
  const ui = { out: el('div', { class: 'u2-result', hidden: true }) };
  root.replaceChildren(buildControls(ui), ui.out);
  ui.run.addEventListener('click', () => runBacktest(ui, false));
  // 语言切换（上次结果还在、语言变了）：直接重画；否则（首次 / R 刷新）重新跑一次
  if (last && lastLang !== lang() && !preset) { lastLang = lang(); showResult(ui, last); return undefined; }
  await runBacktest(ui, true);
  return undefined;
}
