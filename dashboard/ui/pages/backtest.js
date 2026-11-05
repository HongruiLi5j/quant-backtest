/* ==========================================================
 * 文件: ui/pages/backtest.js
 * 用途: 回测实验室（旧 #/lab）：选策略 / 品种 / 起始日 / 单边成本 / 参数 → GET /api/backtest → 结果区
 *   - 结果区拆在 backtest-result.js（指标卡、净值图、最近交易）和 backtest-analysis.js（分析）
 *   - 个股页「在回测实验室调参数」会带 ?symbol=&strategy=&market= 过来
 *   - 语言切换会重画整页：表单值和上次结果存在模块变量里，不重新请求；按 R 才重新跑
 *   - 首次进入时的接口错误交给页面启动器（401/403 显示「登录已过期」）；之后点按钮的错误显示在按钮旁
 *   - 输入校验（A09）：规则在 backtest-validate.js（照后端 app.py / registry.py 写）；这里只负责把结果画在输入框旁边、
 *     有错时禁用「运行回测」。每次输入、换策略 / 品种、回测结束都会重新校验
 *   - D06：每次回测成功后把和默认不同的输入写进网址（前缀 bt_，见 shared/url-state.js），刷新 / 收藏 / 发链接能还原；
 *     网址里的值按白名单校验后只填进表单，不自动运行，要用户自己点「运行回测」
 * ========================================================== */
import { el } from '../shared/dom.js';
import { lang, t } from '../shared/i18n.js';
import { getJSON } from '../shared/api.js';
import { analysisNodes } from './backtest-analysis.js';
import { recentAndTrades, resultNodes } from './backtest-result.js';
import { checkParam, validateAll } from './backtest-validate.js';
import { pickState, writeState, nonDefault, validDate } from '../shared/url-state.js';

const form = { strategy: '', symbol: '', start: '', cost: '', params: {}, period: 'all' };
// D43：区间预设，和后端 registry.PERIODS 一致（quant e1df1b3）。不给自由填写的结束日，避免挑区间凑结果；
// recent 截止 2026-03-31，符合「研究只用到 2026-03-31」的约定。显示文字是日期本身，只有「全部」走词条
const PERIODS = [['all', null], ['is', '2013 – 2019'], ['recent', '2020 – 2026-03']];
let meta = null;
let market = 'cn';
let last = null;          // 上一次回测结果
let lastLang = null;
let busy = false;
let seeded = false;
let uid = 0;              // 提示位的 id 计数（aria-describedby 要用）
const dataEnds = {};      // `${market}:${symbol}` → 上次回测返回的最后一个数据日；校验「开始日早于最新数据日」用，还没跑过的品种不比较

const mk = () => meta.markets[market];
const stratLabel = (k) => meta.strategies[k]?.label ?? k;
const names = () => ({ ...mk().universe, ...(mk().stocks ?? {}) });
const defaultCost = (sym) => (mk().universe[sym] === undefined ? mk().stock_cost_bp : mk().cost_bp);
const decimals = (s) => (String(s).split('.')[1] || '').length;

function paramDefaults(key) {
  return Object.fromEntries((meta.strategies[key]?.params ?? []).map((p) => [p.key, Number(p.default)]));
}

/** 下拉里的「名称（代码）」。A02 词条还没合并进基础字典时 t() 返回键名，下拉里就是一排「a02.fmt.name_code」（G01 走查发现），
 *  这时退成「名称 代码」，和其他页面股票名后面跟代码的写法一样；名称就是代码时只写一次 */
function nameCode(name, code) {
  const s = t('a02.fmt.name_code', { name, code });
  if (s !== 'a02.fmt.name_code') return s;
  return name && name !== code ? `${name} ${code}` : code;
}

/** 品种下拉：自选股 / ETF / 排名靠前的个股 / 其他（从个股页带过来的代码） */
function symbolOptions(extra) {
  const m = mk();
  const group = (label, entries) => (entries.length ? el('optgroup', { label }, ...entries.map(([k, v]) => el('option', { value: k }, nameCode(v || k, k)))) : null);
  const known = (k) => (m.stocks ?? {})[k] !== undefined || m.universe[k] !== undefined || (m.lab_stocks ?? {})[k] !== undefined;
  return [group(t('u2.bt.g_watch'), Object.entries(m.stocks ?? {})), group('ETF', Object.entries(m.universe)),
    group(t('u2.bt.g_top'), Object.entries(m.lab_stocks ?? {})),
    extra && !known(extra) ? group(t('u2.bt.g_other'), [[extra, extra]]) : null];
}

function seedForm() {
  const q = new URLSearchParams(window.location.search);
  const want = q.get('strategy');
  form.strategy = want && Object.hasOwn(meta.strategies, want) ? want : (meta.strategies.blend2 ? 'blend2' : Object.keys(meta.strategies)[0]);
  form.symbol = q.get('symbol') || mk().main;
  form.cost = String(defaultCost(form.symbol));
  form.params = paramDefaults(form.strategy);
  seeded = true;
  if (loadUrlState()) return 'url';
  return Boolean(q.get('symbol') || q.get('strategy'));
}

const SYMBOL_RE = /^[0-9A-Za-z.\-]{1,12}$/;   // A 股 6 位代码 / 美股代码（BRK.B 之类）
const NUM_RE = /^-?\d+(\.\d+)?$/;

/** D06：从网址 bt_ 参数读回表单。网址是不可信输入：策略必须在 meta 里；代码只许字母数字点横线；日期必须真实存在；
 *  成本、参数只收纯数字，参数名只收当前策略声明过的。超出范围的数字照样填进去，交给 A09 校验标红、禁用运行按钮。
 *  返回 true 表示网址里有可用的回测参数（这时不自动运行）。 */
function loadUrlState() {
  const u = pickState('bt');
  let used = false;
  if (typeof u.s === 'string' && Object.hasOwn(meta.strategies, u.s)) { form.strategy = u.s;   // hasOwn：防 ?bt_s=constructor 之类原型链上的键（revtech C-D06）
    form.params = paramDefaults(u.s); used = true; }
  if (SYMBOL_RE.test(u.y ?? '')) { form.symbol = u.y; form.cost = String(defaultCost(u.y)); used = true; }
  if (validDate(u.d)) { form.start = u.d; used = true; }
  if (PERIODS.some(([k]) => k === u.r)) { form.period = u.r; used = true; }
  if (NUM_RE.test(u.c ?? '')) { form.cost = u.c; used = true; }
  for (const p of meta.strategies[form.strategy]?.params ?? []) {
    const v = u[`p_${p.key}`];
    if (NUM_RE.test(v ?? '')) { form.params[p.key] = Number(v); used = true; }
  }
  return used;
}

/** D06：把这次回测的输入写进网址，只写和默认不同的（网址短、默认链接干净） */
function saveUrlState() {
  const dflt = paramDefaults(form.strategy);
  const values = {
    s: nonDefault(form.strategy, meta.strategies.blend2 ? 'blend2' : Object.keys(meta.strategies)[0]),
    y: nonDefault(form.symbol, mk().main), d: form.start, r: nonDefault(form.period, 'all'), c: nonDefault(String(form.cost), String(defaultCost(form.symbol))),
  };
  for (const [k, v] of Object.entries(form.params)) if (v !== dflt[k]) values[`p_${k}`] = v;
  writeState('bt', values);
}

/** 提示位：输入框正下方的一行小字，输入框用 aria-describedby 指向它（屏幕阅读器会读） */
function msgSlot(input) {
  const slot = el('span', { class: 'u2-field__msg', id: `u2-msg-${++uid}`, 'aria-live': 'polite' });
  input.setAttribute('aria-describedby', slot.id);
  return slot;
}

function field(label, control, slot = null) {
  return el('label', { class: 'u2-field' }, el('span', { class: 'muted' }, label), control, slot);
}

/** 词条翻成文字；词条还没合并进基础字典时 t() 原样返回键名，这种情况不露键名（只剩红框），和 A03 / A07 一样 */
function msgText(issue) {
  const s = t(issue.key, issue.vars);
  return s === issue.key ? '' : s;
}

/** 校验结果画到输入框旁边：红（错误，挡运行）> 黄（提醒，不挡）> 灰（说明） */
function paint(f, res) {
  const issue = res.error || res.warn || res.hint;
  f.slot.textContent = issue ? msgText(issue) : '';
  f.slot.className = `u2-field__msg is-${res.error ? 'err' : res.warn ? 'warn' : 'hint'}`;
  f.input.classList.toggle('is-invalid', Boolean(res.error));
  if (res.error) f.input.setAttribute('aria-invalid', 'true'); else f.input.removeAttribute('aria-invalid');
}

function buildControls(ui) {
  const strat = el('select', { class: 'ws-select', 'aria-label': t('u2.bt.strategy') },
    ...Object.entries(meta.strategies).map(([k, v]) => el('option', { value: k }, v.label)));
  strat.value = form.strategy;
  const sym = el('select', { class: 'ws-select', 'aria-label': t('u2.bt.symbol') }, ...symbolOptions(form.symbol));
  sym.value = form.symbol;
  const start = el('input', { class: 'ws-input', type: 'date', value: form.start, 'aria-label': t('u2.bt.start') });
  const period = el('select', { class: 'ws-select', 'aria-label': t('u2.st.period') },
    ...PERIODS.map(([k, label]) => el('option', { value: k }, label ?? t('u2.st.p.all'))));
  period.value = form.period;
  const cost = el('input', { class: 'ws-input u2-narrow', type: 'number', min: '0', max: '100', step: '1', value: form.cost, 'aria-label': t('u2.bt.cost') });
  const desc = el('p', { class: 'ws-note' });
  const params = el('div', { class: 'u2-params' });
  const run = el('button', { class: 'btn btn--primary btn--sm', type: 'button' }, t('u2.bt.run'));
  const reset = el('button', { class: 'btn btn--sm', type: 'button' }, t('u2.bt.reset'));
  ui.run = run; ui.msg = el('span', { class: 'u2-err', role: 'alert' });
  ui.block = el('span', { class: 'u2-err' });   // 「有 N 项输入要改」汇总，和接口错误 ui.msg 分开，互不覆盖
  const fields = { symbol: { input: sym, slot: msgSlot(sym) }, start: { input: start, slot: msgSlot(start) }, cost: { input: cost, slot: msgSlot(cost) } };
  let pfields = {};   // 当前策略的参数输入框：key → { input, slot }，换策略时重建

  /** 读当前所有输入 → 校验 → 画提示 → 有错就禁用「运行回测」。返回错误个数（运行前再调一次，防止绕过按钮） */
  ui.sync = () => {
    const st = meta.strategies[form.strategy];
    const raw = { symbol: form.symbol, start: start.value, cost: cost.value, params: {} };
    const bad = { start: start.validity?.badInput, cost: cost.validity?.badInput, params: {} };
    for (const [k, f] of Object.entries(pfields)) { raw.params[k] = f.input.value; bad.params[k] = f.input.validity?.badInput; }
    const v = validateAll({ market, raw, bad, specs: st.params, known: names()[form.symbol] !== undefined || (mk().lab_stocks ?? {})[form.symbol] !== undefined,
      dataEnd: dataEnds[`${market}:${form.symbol}`] ?? null, costDefault: defaultCost(form.symbol) });
    paint(fields.symbol, v.symbol); paint(fields.start, v.start); paint(fields.cost, v.cost);
    for (const [k, f] of Object.entries(pfields)) paint(f, v.params[k]);
    ui.block.textContent = v.errors ? msgText({ key: 'a09.sum.blocked', vars: { n: v.errors } }) : '';
    run.disabled = busy || v.errors > 0;
    // D06 fix（tester 复现：换策略自动运行还没回来时改参数再点运行，网址只剩 bt_s）：网址不再只在「回测成功后」才写，
    // 而是每次输入校验通过就跟着表单走——不依赖哪个请求先回来，也就没有竞态。链接本来就不会自动运行，所以网址里是
    // 「还没跑过的参数」也不要紧：打开链接只是把表单填好。form.params 里只存合格的值（见 drawParams），不会把错值写进网址。
    if (!v.errors) saveUrlState();
    return v.errors;
  };

  const drawParams = () => {
    const st = meta.strategies[form.strategy];
    desc.textContent = st.desc + (st.kind === 'multi' ? t('u2.bt.multi_note') : '');
    pfields = {};
    params.replaceChildren(...(st.params.length ? st.params.map((p) => {
      const input = el('input', { class: 'ws-input u2-narrow', type: 'number', min: p.min, max: p.max, step: p.step, dataset: { key: p.key }, 'aria-label': p.label,
        value: Number(form.params[p.key] ?? p.default).toFixed(decimals(p.step)) });
      const slot = msgSlot(input);
      pfields[p.key] = { input, slot };
      // 只把合格的值写进 form.params（以前清空输入框会变成 Number('') = 0 发给后端）；不合格时按钮是禁用的，不会发请求
      input.addEventListener('input', () => { if (!checkParam(input.value, p, input.validity?.badInput)) form.params[p.key] = Number(input.value); ui.sync(); });
      return el('label', { class: 'u2-field', title: p.help || null }, el('span', { class: 'muted' }, p.label + (p.help ? ' ⓘ' : '')), input, slot);
    }) : [el('span', { class: 'muted' }, t('u2.bt.no_params'))]));
    ui.sync();
  };
  strat.addEventListener('change', () => { form.strategy = strat.value; form.params = paramDefaults(form.strategy); drawParams(); ui.run.click(); });
  sym.addEventListener('change', () => { form.symbol = sym.value; form.cost = String(defaultCost(form.symbol)); cost.value = form.cost; ui.sync(); ui.run.click(); });
  period.addEventListener('change', () => { form.period = period.value; ui.run.click(); });
  const onStart = () => { form.start = start.value; ui.sync(); };
  start.addEventListener('input', onStart);
  start.addEventListener('change', onStart);
  cost.addEventListener('input', () => { form.cost = cost.value; ui.sync(); });
  reset.addEventListener('click', () => { form.params = paramDefaults(form.strategy); drawParams(); ui.run.click(); });
  params.addEventListener('keydown', (e) => { if (e.key === 'Enter') ui.run.click(); });
  const head = el('div', { class: 'card__head' }, el('h2', { class: 'card__title' }, t('u2.bt.title')), el('span', { class: 'card__sub' }, t('u2.bt.sub')));
  const card = el('section', { class: 'card' }, head,
    el('div', { class: 'u2-form' }, field(t('u2.bt.strategy'), strat), field(t('u2.bt.symbol'), sym, fields.symbol.slot),
      field(t('u2.bt.start'), start, fields.start.slot), field(t('u2.st.period'), period), field(t('u2.bt.cost'), cost, fields.cost.slot)),
    desc, params,
    el('div', { class: 'u2-actions' }, run, reset, ui.block, ui.msg));
  drawParams();   // 里面会 sync()，所以放在 card 搭好之后
  return card;
}

function showResult(ui, r) {
  const label = stratLabel(r.strategy);
  ui.out.replaceChildren(resultNodes(r, label, names()), ...analysisNodes(r), recentAndTrades(r));
  ui.out.hidden = false;
}

/** 运行中的样子（G01）：按钮写「回测中…」，上一次的结果变淡并标 aria-busy，免得把旧结果当成新参数的结果；结束后恢复 */
function setRunning(ui, on) {
  ui.run.textContent = on ? (msgText({ key: 'g01.bt.running' }) || t('cmp.demo.running')) : t('u2.bt.run');   // G01 词条未合并时用已有的「运行中…」
  if (on) ui.out.setAttribute('aria-busy', 'true'); else ui.out.removeAttribute('aria-busy');
}

/** 请求参数和旧页面完全一致：market / strategy / symbol / cost / start / params(JSON) */
async function runBacktest(ui, strict) {
  if (busy || ui.sync()) return;   // 输入有错：不发请求（按钮已禁用，这里防 Enter / 程序化 click 绕过）
  busy = true; ui.run.disabled = true; ui.msg.replaceChildren();
  setRunning(ui, true);
  try {
    const q = new URLSearchParams({ market, strategy: form.strategy, symbol: form.symbol, cost: form.cost, start: form.start, period: form.period, params: JSON.stringify(form.params) });
    const r = await getJSON(`/api/backtest?${q}`, { mock: 'u2-backtest' });
    if (r.error) { ui.msg.textContent = r.error; ui.out.hidden = true; return; }
    last = r; lastLang = lang();
    dataEnds[`${market}:${r.symbol}`] = r.end;   // 记下这只品种最新数据日，开始日期校验要用
    showResult(ui, r);
    saveUrlState();
  } catch (err) {
    if (strict && err.auth) throw err;
    ui.out.hidden = true;
    ui.msg.replaceChildren(err.auth ? el('span', {}, t('ui.auth.expired'), ' ', el('a', { href: '/' }, t('ui.auth.home'))) : (err.message || t('ui.state.error')));
  } finally {
    busy = false; setRunning(ui, false); ui.sync();   // 恢复按钮：没有输入错误才启用；并用刚拿到的最新数据日重新校验开始日期
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
  if (preset === 'url') {   // D06：网址带来的参数只填表，不自动运行
    ui.sync();
    ui.msg.textContent = msgText({ key: 'ux.bt.from_url' });
    return undefined;
  }
  await runBacktest(ui, true);
  return undefined;
}
