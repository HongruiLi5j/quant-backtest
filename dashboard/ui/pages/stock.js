/* ==========================================================
 * 文件: ui/pages/stock.js
 * 用途: 个股详情（旧 #/stock/<代码>）。地址：stock.html?code=600000&market=cn
 *   头部（名称 / 代码 / 今日·近1月·近1年 / 明日仓位与动作）→ 工具条（策略 / 区间 / 技术指标 / 去回测实验室 / 自选）
 *   → K 线（stock-chart.js）→ 策略对比表 → 净值曲线 → 交易记录
 *   接口：GET /api/stock、GET /api/meta、POST /api/watchlist
 *   策略、区间、指标的选择记在 localStorage（换股票也保留）
 * ========================================================== */
import { el, fmt, store } from '../shared/dom.js';
import { t } from '../shared/i18n.js';
import { activateOnKey } from '../shared/a11y.js';
import { getJSON, postJSON, isMock } from '../shared/api.js';
import { toast } from '../shared/toast.js';
import { canZoomRecent, kChart } from './stock-chart.js';
import { actionBadge, card, legend, lineChart, paletteVars, pct, table, tb, toneClass } from './u2-lib.js';

const PERIODS = ['6m', '1y', '3y', '5y', 'all'];
const INDS = ['all', 'macd', 'rsi', 'kdj', 'off'];
const VIEWS = ['recent', 'all'];     // 数据多时 K 线的可见范围：最近约 1 年 / 全部
const KEY = { strategy: 'qb-u2-stock-strategy', period: 'qb-u2-stock-period', ind: 'qb-u2-stock-ind', view: 'qb-a12-stock-view' };

let data = null;      // 当前 /api/stock 返回，切换指标时只重画 K 线用
let kc = null;        // 当前 K 线的 { chart, showRange }，切换可见范围时不重画、只移动窗口

function head(d, meta, market) {
  const i = d.info;
  const sub = [tb(i.kind), tb(i.board), i.industry].filter(Boolean).join(' · ');
  const stat = (label, node) => el('div', { class: 'u2-stat' }, el('span', { class: 'muted' }, label), node);
  const num = (v, dg) => el('b', { class: `num ${toneClass(v)}` }, pct(v, dg));
  return el('section', { class: 'card u2-shead' },
    el('div', {}, el('p', { class: 'muted' }, sub), el('h2', { class: 'u2-shead__name' }, i.name, el('span', { class: 'u2-code num' }, i.code))),
    el('div', { class: 'u2-stats' },
      stat(t('u2.st.today'), num(i.d1, 2)), stat(t('u2.st.m1'), num(i.m1)), stat(t('u2.st.y1'), num(i.y1)),
      stat(t('u2.st.tomorrow', { s: meta.strategies[d.strategy]?.label ?? d.strategy }), el('span', {}, el('b', { class: 'num' }, fmt(d.next * 100, 0, '%')), ' ', actionBadge(d.action)))));
}

function select(options, value, label, onChange) {
  const s = el('select', { class: 'ws-select', 'aria-label': label }, ...options.map(([v, text]) => el('option', { value: v }, text)));
  s.value = value;
  s.addEventListener('change', () => onChange(s.value));
  return s;
}

function toolbar(d, meta, market, code, st, actions) {
  const singles = Object.entries(meta.strategies).filter(([, v]) => v.kind === 'single').map(([k, v]) => [k, v.label]);
  const period = el('div', { class: 'segmented', role: 'group', 'aria-label': t('u2.st.period') },
    ...PERIODS.map((p) => el('button', { class: 'segmented__btn', type: 'button', 'aria-pressed': String(p === st.period), dataset: { p } }, t(`u2.st.p.${p}`))));
  period.addEventListener('click', (e) => { const b = e.target.closest('[data-p]'); if (b) actions.set('period', b.dataset.p); });
  const view = canZoomRecent(d.dates.length)
    ? el('div', { class: 'u2-field' }, el('span', { class: 'muted' }, t('a12.range.label')),
      el('div', { class: 'segmented', role: 'group', 'aria-label': t('a12.range.label') },
        ...VIEWS.map((v) => el('button', { class: 'segmented__btn', type: 'button', 'aria-pressed': String(v === st.view), dataset: { v } }, t(`a12.range.${v}`)))))
    : null;
  if (view) view.addEventListener('click', (e) => { const b = e.target.closest('[data-v]'); if (b) actions.set('view', b.dataset.v); });
  const labQ = new URLSearchParams({ market, symbol: code, strategy: st.strategy });
  if (isMock()) labQ.set('mock', '1');   // G01：预览模式把 ?mock=1 带过去（以前丢了，预览里点过去是 404 错误卡片）
  const lab = el('a', { class: 'btn btn--sm', href: `backtest.html?${labQ}` }, t('u2.st.to_lab'));
  const nodes = [
    el('label', { class: 'u2-field' }, el('span', { class: 'muted' }, t('u2.bt.strategy')), select(singles, st.strategy, t('u2.bt.strategy'), (v) => actions.set('strategy', v))),
    el('div', { class: 'u2-field' }, el('span', { class: 'muted' }, t('u2.st.period')), period),
    view,
    el('label', { class: 'u2-field' }, el('span', { class: 'muted' }, t('u2.st.ind')), select(INDS.map((k) => [k, t(`u2.st.ind.${k}`)]), st.ind, t('u2.st.ind'), (v) => actions.set('ind', v))),
    el('span', { class: 'ws-head__spacer' }), lab];
  if (d.info.kind === '个股') {
    const w = el('button', { class: 'btn btn--sm', type: 'button' }, t(d.info.watch ? 'u2.st.unwatch' : 'u2.st.watch'));
    w.addEventListener('click', () => actions.watch(!d.info.watch));
    nodes.push(w);
  }
  return el('div', { class: 'ws-filter u2-toolbar' }, ...nodes);
}

function chartCard(d, meta, market, st, actions, code) {
  const box = el('div', { class: 'chart u2-k', role: 'img', 'aria-label': t('u2.st.chart_aria', { n: d.info.name }) });
  const items = d.lines.map((L, k) => ({ label: L.name, color: L.dash ? 'var(--chart-dd)' : paletteVars[k % 4] }));
  const markers = [{ label: t('u2.st.legend_buy'), color: 'var(--color-rise)' }, { label: t('u2.st.legend_sell'), color: 'var(--color-fall)' }, { label: t('u2.st.legend_pos'), color: 'var(--chart-2)' }];
  const i = d.info;
  const note = t('u2.st.note', { first: i.first_date, last: i.last_date, rows: i.rows, tz: tb(meta.markets[market]?.timezone_label ?? ''), cost: fmt(d.cost_bp, 0) });
  queueMicrotask(() => requestAnimationFrame(() => { kc = kChart(box, d, st.ind, st.view); }));
  box.dataset.kchart = '1';
  const hint = canZoomRecent(d.dates.length) ? el('p', { class: 'ws-note' }, t('a12.range.hint', { n: d.dates.length })) : null;
  return el('section', { class: 'card' }, toolbar(d, meta, market, code, st, actions), legend([...items, ...markers]), box, el('p', { class: 'ws-note' }, note), hint);
}

function compareCard(d, meta, actions) {
  const rows = d.compare.map((c) => (c.error
    ? { cells: [c.label, { v: c.error, span: 6, cls: 'muted' }] }
    : { cls: `u2-click ${c.key === d.strategy ? 'u2-pin' : ''}`, dataset: { k: c.key }, cells: [c.label, { v: fmt(c.cagr, 1, '%'), num: true }, { v: fmt(c.sharpe), num: true },
      { v: fmt(c.maxdd, 1, '%'), num: true }, { v: fmt(c.win, 0, '%'), num: true }, { v: c.trades ?? '—', num: true }, actionBadge(c.action)] }));
  const heads = ['u2.st.c.strategy', 'metric.cagr', 'u2.bt.excess_sharpe', 'metric.maxdd', 'metric.win', 'metric.trades', 'u2.st.c.next'].map((k, n) => ({ text: t(k), num: n > 0 && n < 6 }));
  const node = card(t('u2.st.compare'), null, table(heads, rows));
  node.querySelectorAll('tbody tr[data-k]').forEach((tr) => {
    tr.tabIndex = 0;
    tr.addEventListener('click', () => actions.set('strategy', tr.dataset.k));
    activateOnKey(tr, () => actions.set('strategy', tr.dataset.k));   // Enter 和空格都能选（A11）
  });
  return node;
}

function equityCard(d, meta) {
  const label = meta.strategies[d.strategy]?.label ?? d.strategy;
  const box = el('div', { class: 'chart chart--sm u2-chart' });
  queueMicrotask(() => requestAnimationFrame(() => lineChart(box, d.dates, [
    { values: d.bench, color: (c) => c.bench, width: 1 }, { values: d.equity, color: (c) => c.line, width: 2 }], {})));
  return card(t('u2.st.equity'), t('u2.st.equity_sub'), legend([{ label, color: 'var(--chart-1)' }, { label: t('u2.bt.hold'), color: 'var(--chart-bench)' }]), box);
}

function tradesCard(d) {
  const body = d.trades.length
    ? table(['u2.st.t.entry', 'u2.st.t.exit', 'backtest.trades.days', 'u2.st.t.px', 'u2.st.t.acct'].map((k, n) => ({ text: t(k), num: n > 1 })),
      d.trades.map((x) => [{ v: x.entry, num: true }, x.open ? el('span', { class: 'badge badge--accent' }, t('u2.bt.holding')) : { v: x.exit, num: true }, { v: String(x.days), num: true },
        { v: pct(x.px_ret, 2), num: true, cls: toneClass(x.px_ret) }, { v: pct(x.ret, 2), num: true, cls: toneClass(x.ret) }]), { scroll: true })
    : el('p', { class: 'muted' }, t('u2.st.no_trades'));
  return card(t('u2.st.trades'), null, body);
}

export async function render(root) {
  const q = new URLSearchParams(window.location.search);
  const code = (q.get('code') || '').trim();
  if (!code) {
    root.replaceChildren(card(t('u2.st.no_code'), null, el('p', { class: 'muted' }, t('u2.st.no_code_hint')), el('a', { class: 'btn btn--sm', href: 'market.html' }, t('u2.st.goto_market'))));
    return undefined;
  }
  const meta = await getJSON('/api/meta', { mock: 'u2-meta' });
  const market = meta.markets[q.get('market')] ? q.get('market') : meta.default_market;
  const st = { strategy: store.get(KEY.strategy, 'blend2'), period: store.get(KEY.period, '1y'), ind: store.get(KEY.ind, 'all'), view: store.get(KEY.view, 'recent') };
  if (!VIEWS.includes(st.view)) st.view = 'recent';
  if (!meta.strategies[st.strategy]) st.strategy = 'blend2';
  if (!PERIODS.includes(st.period)) st.period = '1y';
  if (!INDS.includes(st.ind)) st.ind = 'all';

  const again = () => render(root).catch((err) => {
    root.replaceChildren(card(err.auth ? t('ui.auth.expired') : t('ui.state.error'), null, el('p', { class: 'muted' }, err.message ?? ''), err.auth ? el('a', { class: 'btn btn--sm', href: '/' }, t('ui.auth.home')) : null));
  });
  const actions = {
    set(key, value) {
      store.set(KEY[key], value);
      if (key === 'view') {                              // 只换可见范围：不请求、不重画，移动窗口并同步按钮状态
        st.view = value;
        root.querySelectorAll('[data-v]').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.v === value)));
        kc?.showRange(value);
        return;
      }
      if (key !== 'ind') { again(); return; }
      st.ind = value;                                    // 只换指标：不重新请求，直接重画 K 线
      const box = root.querySelector('[data-kchart]');
      if (box && data) kc = kChart(box, data, value, st.view);
    },
    async watch(add) {
      const r = await postJSON(`/api/watchlist?market=${market}&action=${add ? 'add' : 'remove'}&code=${encodeURIComponent(code)}`);
      if (r.error) { toast({ title: r.error }); return; }
      again();
    },
  };

  const d = await getJSON(`/api/stock?${new URLSearchParams({ market, code, strategy: st.strategy, period: st.period })}`, { mock: 'u2-stock' });
  if (d.error) {
    root.replaceChildren(card(t('u2.st.err_title'), null, el('p', { class: 'is-warn', role: 'alert' }, d.error)));
    return undefined;
  }
  data = d;
  root.replaceChildren(head(d, meta, market), el('div', { class: 'u2-gap' }, chartCard(d, meta, market, st, actions, code)),
    el('div', { class: 'ws-grid ws-grid--2 u2-gap' }, compareCard(d, meta, actions), el('div', { class: 'u2-stack' }, equityCard(d, meta), tradesCard(d))));
  return undefined;
}
