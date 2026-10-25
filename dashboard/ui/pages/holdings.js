/* ==========================================================
 * 文件: ui/pages/holdings.js
 * 用途: Workspace · 持仓（对应旧界面 #/holdings，旧代码 app.js 的 holdings*）
 *   汇总 6 格（总资金可就地编辑）/ 持仓表 / 录入表单（pages/holdings-form.js）/ 历史成交 / 已清仓
 * 接口: GET /api/holdings?trades_limit=200&include_voided=0|1；POST /api/holdings/capital
 * 说明: 金额都是字符串，只在显示时转 Number；私人数据只放在本模块的变量里，不写任何浏览器存储
 * ========================================================== */

import { el } from '../shared/dom.js';
import { t } from '../shared/i18n.js';
import { getJSON, ApiError } from '../shared/api.js';
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
      st.msg.show('hold.capital_saved', 'ok');
    });
    cancel.addEventListener('click', () => { st.capitalEdit = false; draw(); });
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') save.click(); });
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
  return el('section', { class: 'card', 'aria-labelledby': 'hold-sum-t' },
    el('div', { class: 'card__head' }, el('h2', { class: 'card__title', id: 'hold-sum-t' }, t('hold.sum.title'))), ...parts);
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
    td(el('a', { class: 'mono', href: `/#/stock/${encodeURIComponent(p.code)}` }, p.code)),
    td(p.name || p.code),
    td(num(p.shares, 0), true), td(num(p.avg_cost, 3), true), td(price, true), td(pnl, true), td(weight, true),
    td(p.first_buy || '—', true, 'muted'), td(p.held_days ?? '—', true, 'muted'));
}

function positionsCard(snap) {
  const list = sortPositions(snap.positions || []);
  const heads = ['hold.pos.code', 'hold.pos.name', 'hold.pos.shares', 'hold.pos.avg', 'hold.pos.price', 'hold.pos.pnl', 'hold.pos.weight', 'hold.pos.first', 'hold.pos.days'];
  return el('section', { class: 'card card--flush', 'aria-labelledby': 'hold-pos-t' },
    el('div', { class: 'card__head' }, el('h2', { class: 'card__title', id: 'hold-pos-t' }, t('hold.pos.title'))),
    table(heads, list.map((p) => positionRow(p, snap)), 'hold.pos.empty', { numCols: [2, 3, 4, 5, 6, 7, 8], id: 'holdPositionsTable' }),
    el('p', { class: 'ws-note u3-pad' }, t('hold.pos.foot')),
    el('p', { class: 'ws-note u3-pad' }, t('hold.monitor')));
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
}

async function fetchSnap() {
  const snap = await getJSON(`/api/holdings?trades_limit=200&include_voided=${st.includeVoided ? 1 : 0}`);
  if (!snap || snap.ok !== true) throw new ApiError(200, 'holdings 返回异常', snap);
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
