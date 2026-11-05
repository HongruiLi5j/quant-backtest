/* ==========================================================
 * 文件: ui/pages/holdings.js
 * 用途: Workspace · 持仓（对应旧界面 #/holdings，旧代码 app.js 的 holdings*）
 *   汇总 6 格（总资金可就地编辑）/ 持仓表 / 录入表单（pages/holdings-form.js）/ 历史成交 / 已清仓
 * 接口: GET /api/holdings?trades_limit=200&include_voided=0|1；POST /api/holdings/capital
 * 说明: 金额都是字符串，只在显示时转 Number；私人数据只放在本模块的变量里，不写任何浏览器存储
 * ========================================================== */

import { el, motionOK } from '../shared/dom.js';
import { t } from '../shared/i18n.js';
import { getJSON, ApiError } from '../shared/api.js';
import { closeOnEscape } from '../shared/a11y.js';
import { stockPage } from '../shared/stock-link.js';
import { num, ratio, tone, isNum, span, table, td, messageBox, errKey, post } from './u3-common.js';
import { buildForm, historyCard } from './holdings-form.js';

const st = { snap: null, includeVoided: false, capitalEdit: false, root: null, msg: null, form: null };

/** 价格日期距 as_of 超过 5 个自然日（约 3 个交易日加周末）算偏旧；只是提示 */
/** 最新价的位数：A 股个股价格是两位小数(10.10)，ETF 是三位(1.234)；不是整分的才补到三位 */
function priceDigits(v) {
  const x = Number(v) * 100;
  return Math.abs(x - Math.round(x)) < 1e-6 ? 2 : 3;
}

function stale(priceDate, asOf) {
  if (!priceDate || !asOf) return false;
  const gap = (Date.parse(`${asOf}T00:00:00Z`) - Date.parse(`${priceDate}T00:00:00Z`)) / 86400000;
  return Number.isFinite(gap) && gap > 5;
}

/** 权重降序；没有权重的按市值降序；再按代码 */
function sortPositions(list) {
  const has = (p) => isNum(p.weight);
  const mv = (p) => (isNum(p.market_value) ? Number(p.market_value) : -Infinity);
  return [...list].sort((a, b) => (has(a) && has(b) ? Number(b.weight) - Number(a.weight) : has(a) ? -1 : has(b) ? 1 : mv(b) - mv(a))
    || String(a.code).localeCompare(String(b.code)));
}

function tile(labelKey, value, cls = '') {
  return el('div', { class: 'metric' },
    el('span', { class: 'metric__label' }, t(labelKey)),
    el('span', { class: `metric__value ${cls}`.trim() }, value));
}

/** 总资金格：显示 / 编辑两种状态 */
function capitalTile(snap) {
  const label = el('span', { class: 'metric__label' }, t('hold.sum.capital'));
  if (st.capitalEdit) {
    const input = el('input', { class: 'ws-input u3-capital-input', type: 'text', inputmode: 'decimal', value: snap.capital ?? '', 'aria-label': t('hold.sum.capital_label') });
    const save = el('button', { class: 'btn btn--sm btn--primary', type: 'button' }, t('hold.sum.save'));
    const cancel = el('button', { class: 'btn btn--sm btn--ghost', type: 'button' }, t('hold.sum.cancel'));
    save.addEventListener('click', async () => {
      save.disabled = true;
      const r = await post('/api/holdings/capital', { capital: input.value.trim() });
      if (!r.ok) { save.disabled = false; st.msg.show(errKey(r.err, ['hold'])); return; }
      st.capitalEdit = false;
      await reload();
      st.root.querySelector('#holdCapitalEdit')?.focus();   // 编辑格被换掉了，焦点回到「编辑」按钮（A11）
      st.msg.show('hold.capital_saved', 'ok');
    });
    cancel.addEventListener('click', () => { st.capitalEdit = false; draw(); st.root.querySelector('#holdCapitalEdit')?.focus(); });
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') save.click(); });
    closeOnEscape(input, () => cancel.click());   // 输入框里按 Esc 等于点「取消」（A11）
    return el('div', { class: 'metric u3-capital' }, label, input, el('div', { class: 'u3-btn-row' }, save, cancel));
  }
  const edit = el('button', { class: 'btn btn--sm btn--ghost', type: 'button', id: 'holdCapitalEdit' }, t('hold.sum.edit'));
  edit.addEventListener('click', () => { st.capitalEdit = true; draw(); st.root.querySelector('.u3-capital-input')?.focus(); });
  const value = isNum(snap.capital) ? num(snap.capital) : t('hold.sum.capital_unset');
  return el('div', { class: 'metric u3-capital' }, label, el('span', { class: 'metric__value' }, value), edit);
}

function summaryCard(snap) {
  const s = snap.summary || {};
  const parts = [
    el('div', { class: 'ws-grid ws-grid--kpi6 u3-kpi' },
      capitalTile(snap),
      tile('hold.sum.market_value', num(s.market_value)),
      tile('hold.sum.invested', ratio(s.invested_ratio, 1)),
      tile('hold.sum.floating', num(s.floating_pnl), tone(s.floating_pnl)),
      tile('hold.sum.realized', num(s.realized), tone(s.realized)),
      tile('hold.sum.dividends', num(s.dividends))),
    el('p', { class: 'ws-note' }, t('hold.sum.capital_hint')),
  ];
  if (Number(s.missing_price_count) > 0) parts.push(el('p', { class: 'u3-warn' }, t('hold.sum.missing', { n: Number(s.missing_price_count) })));
  // G01：录入表单在持仓表下面，1440×900 屏幕上要往下滚一屏才看到；汇总卡右上角放一个「录入成交」直接跳过去并把光标放进代码框
  const jump = el('button', { class: 'btn btn--sm', type: 'button' }, t('hold.form.title'), el('span', { 'aria-hidden': 'true' }, ' ↓'));
  jump.addEventListener('click', () => {
    document.getElementById('holdEntry')?.scrollIntoView({ block: 'center', behavior: motionOK() ? 'smooth' : 'auto' });
    document.getElementById('holdCode')?.focus({ preventScroll: true });
  });
  return el('section', { class: 'card', 'aria-labelledby': 'hold-sum-t' },
    el('div', { class: 'card__head' }, el('h2', { class: 'card__title', id: 'hold-sum-t' }, t('hold.sum.title')), jump), ...parts);
}

function positionRow(p, snap) {
  const over = isNum(p.weight) && Number(p.weight) > 0.1;
  const price = isNum(p.price)
    ? el('span', {}, `${num(p.price, priceDigits(p.price))} · ${p.price_date || '—'}`, stale(p.price_date, snap.as_of) ? el('small', { class: 'u3-warn u3-block' }, t('hold.pos.stale')) : null)
    : span(t('hold.pos.noprice'), 'muted');
  const pnl = isNum(p.floating_pnl)
    ? span(`${num(p.floating_pnl)} (${ratio(p.floating_pct, 2, true)})`, tone(p.floating_pnl))
    : span(t('hold.pos.noprice'), 'muted');
  const weight = el('span', { class: over ? 'u3-over' : null }, ratio(p.weight, 2), over ? el('small', { class: 'u3-block' }, t('hold.pos.over')) : null);
  return el('tr', { class: over ? 'is-over' : null },
    td(el('a', { class: 'mono', href: stockPage(p.code, 'cn') }, p.code)),   // 持仓只有 A 股（6 位代码）；G01 改去新个股页
    td(p.name || p.code),
    td(num(p.shares, 0), true), td(num(p.avg_cost, 3), true), td(price, true), td(pnl, true), td(weight, true),
    td(p.first_buy || '—', true, 'muted'), td(p.held_days ?? '—', true, 'muted'),
    // D13（C19）：组合策略对这只持仓怎么看。先占位，draw() 之后由 fillStrategy() 逐只填（不阻塞页面）
    td(span('…', 'muted c13-sig'), false), td(span('…', 'muted c13-next'), true));
}

/** D13：组合策略对每只持仓的信号和明日目标仓位（只读，不下单；策略计算全在后端，前端只取字段）。
 *  ① 先用今天已发布的买卖清单（和信号页同一份：/api/main_strategy + /api/trade_list），这样两页对同一只股票的说法一致；
 *  ② 不在清单里的持仓（仓位变化没超过阈值等），才逐只查个股页同一个接口 /api/stock，并标注「未进今日清单」；
 *     最多同时 3 个请求（每个请求后端要算全部策略，约 0.1–0.9 秒）。
 *  市场取持仓自己的 market 字段（没有就按 A 股）。没有止损线列：要等 quant 确认策略确实按它交易 */
const FALLBACK_PARALLEL = 3;
const actText = (a) => { const k = `u1.act.${a}`; return a ? (t(k) !== k ? t(k) : a) : '—'; };   // 后端给中文动作，有词条就翻译

async function todayList() {
  try {
    const main = await getJSON('/api/main_strategy');
    if (!main?.main) return null;
    const r = await getJSON(`/api/trade_list?strategy=${encodeURIComponent(main.main)}`);
    return r && !r.error && !r.empty && Array.isArray(r.rows) ? r : null;
  } catch { return null; }
}

async function fillStrategy(tbody, positions) {
  const rows = [...tbody.querySelectorAll('tr')];
  const cells = positions.map((p, i) => ({ p, sig: rows[i]?.querySelector('.c13-sig'), nxt: rows[i]?.querySelector('.c13-next') })).filter((c) => c.sig && c.nxt);
  const set = (c, sig, nxt, note) => {
    c.sig.replaceChildren(sig, note ? el('small', { class: 'muted u3-block' }, note) : '');
    c.sig.className = ''; c.nxt.textContent = nxt; c.nxt.className = '';
  };
  const list = await todayList();
  const byCode = new Map((list?.rows ?? []).map((r) => [r.code, r]));
  const rest = [];
  for (const c of cells) {
    const r = byCode.get(c.p.code);
    if (r) set(c, actText(r.action), isNum(r.next) ? ratio(r.next, 0) : '—', t('c13.from_list', { d: list.data_date }));
    else rest.push(c);
  }
  // 不在清单里的：限并发逐只查
  let i = 0;
  const worker = async () => {
    while (i < rest.length) {
      const c = rest[i++];
      try {
        const market = c.p.market || 'cn';
        const strat = list?.strategy || 'blend2';
        const d = await getJSON(`/api/stock?market=${encodeURIComponent(market)}&code=${encodeURIComponent(c.p.code)}&strategy=${encodeURIComponent(strat)}&period=1y`, { mock: 'u2-stock' });   // 演示模式（?mock=1）用个股页同一份 mock
        if (d?.error || !isNum(d?.next)) throw new Error('no data');
        set(c, actText(d.action), ratio(d.next, 0), t('c13.not_in_list'));
      } catch {
        set(c, t('u1.na'), '—', '');
      }
    }
  };
  await Promise.all(Array.from({ length: Math.min(FALLBACK_PARALLEL, rest.length) }, worker));
}

function positionsCard(snap) {
  const list = sortPositions(snap.positions || []);
  const heads = ['hold.pos.code', 'hold.pos.name', 'hold.pos.shares', 'hold.pos.avg', 'hold.pos.price', 'hold.pos.pnl', 'hold.pos.weight', 'hold.pos.first', 'hold.pos.days', 'c13.sig', 'c13.next'];
  return el('section', { class: 'card card--flush', 'aria-labelledby': 'hold-pos-t' },
    el('div', { class: 'card__head' }, el('h2', { class: 'card__title', id: 'hold-pos-t' }, t('hold.pos.title'))),
    table(heads, list.map((p) => positionRow(p, snap)), 'hold.pos.empty', { numCols: [2, 3, 4, 5, 6, 7, 8, 10], id: 'holdPositionsTable' }),
    list.length ? el('p', { class: 'ws-note u3-pad' }, t('c13.note')) : null,
    el('p', { class: 'ws-note u3-pad' }, t('hold.pos.foot')));   // D11（C17）：去掉占位文字「盯盘盈亏：尚未接入」，实现之前不显示
}

function closedCard(snap) {
  const label = (c) => (c.name && c.name !== c.code ? `${c.code} ${c.name}` : c.code);
  const rows = (snap.closed || []).map((c) => el('tr', {},
    td(label(c)), td(c.first_buy || '—', true, 'muted'), td(c.last_sell || '—', true, 'muted'), td(c.held_days ?? '—', true, 'muted'),
    td(span(num(c.pnl), tone(c.pnl)), true)));
  const heads = ['hold.pos.code', 'hold.pos.first', 'hold.closed.last', 'hold.pos.days', 'hold.closed.pnl'];
  return el('details', { class: 'card card--flush u3-closed' },
    el('summary', { class: 'card__title u3-summary' }, t('hold.closed.title')),
    table(heads, rows, 'hold.closed.empty', { numCols: [1, 2, 3, 4], id: 'holdClosedTable' }));
}

/** 用当前 st.snap 重画整页（表单是同一个节点，输入不丢） */
function draw() {
  const snap = st.snap;
  const asof = document.querySelector('[data-bind="asof"]');
  if (asof) asof.textContent = t('u3.hold.asof', { d: snap.as_of || '—' });
  st.root.replaceChildren(
    el('p', { class: 'ws-note' }, t('hold.note')),
    st.msg.node,
    summaryCard(snap),
    positionsCard(snap),
    el('section', { class: 'card', 'aria-labelledby': 'hold-form-t' },
      el('div', { class: 'card__head' }, el('h2', { class: 'card__title', id: 'hold-form-t' }, t('hold.form.title'))), st.form.node),
    historyCard(snap, { includeVoided: st.includeVoided, msg: st.msg, reload,
      setVoided: (v) => { st.includeVoided = v; reload(); } }),
    closedCard(snap));
  const tbody = st.root.querySelector('#holdPositionsTable tbody');
  if (tbody) fillStrategy(tbody, sortPositions(snap.positions || []));   // 顺序与表格行一致
}

async function fetchSnap() {
  const snap = await getJSON(`/api/holdings?trades_limit=200&include_voided=${st.includeVoided ? 1 : 0}`);
  if (!snap || snap.ok !== true) throw new ApiError(200, t('a03.err.bad_reply', { api: 'holdings' }), snap);   // 文字会显示在错误卡片的「原因」里，跟随语言
  return snap;
}

/** 操作成功后重新拉取并重画；失败只在消息条提示，页面保留旧数据 */
async function reload() {
  try {
    st.snap = await fetchSnap();
    draw();
  } catch (err) {
    st.msg.show(errKey(err, ['hold']));
  }
}

export async function render(root) {
  const old = st.form?.capture();
  st.root = root;
  st.capitalEdit = false;
  st.msg = messageBox();
  st.snap = await fetchSnap();
  st.form = buildForm({ msg: st.msg, reload, getSnap: () => st.snap });
  if (old) st.form.restore(old);
  draw();
}
