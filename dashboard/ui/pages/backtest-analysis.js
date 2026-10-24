/* ==========================================================
 * 文件: ui/pages/backtest-analysis.js
 * 用途: 回测「分析」区（对应旧 analysis.js 的 renderAnalysis），数据来自 r.analysis（src/analytics.py）：
 *   假设与局限 / 核心指标 / 收益分布 / 样本内外 / 月度收益 / 滚动 Sharpe / 仓位占用 / 多基准
 *   / 交易分布与直方图 / 手续费敏感性 / 蒙特卡洛 / 参数敏感性 / CSV 导出
 *   图表部分在 backtest-ana-charts.js
 * ========================================================== */
import { el, fmt } from '../shared/dom.js';
import { t } from '../shared/i18n.js';
import { renderHeatmap } from '../shared/chart-heatmap.js';
import { card, downloadCSV, isNum, pct, table, tb, toneClass } from './u2-lib.js';
import { chartSection, histogram } from './backtest-ana-charts.js';

const num = (v, d = 2, unit = '') => ({ v: fmt(v, d, unit), num: true });
/** 指标行：策略值比持有好时加高亮 */
function metricRow(label, v, b, d = 2, unit = '', good = 'high') {
  const better = good === 'high' && isNum(v) && isNum(b) && v > b;
  return [label, { v: fmt(v, d, unit), num: true, cls: better ? 'is-rise' : '' }, num(b, d, unit)];
}

function assumptions(a) {
  return card(t('u2.ana.assume'), t('u2.ana.assume_sub'),
    el('dl', { class: 'ws-dl' }, ...(a.assumptions ?? []).flatMap(([k, v]) => [el('dt', {}, tb(k)), el('dd', { class: 'u2-ass' }, v)])));
}

function coreCards(a) {
  const f = a.full ?? {}; const bf = a.bench_full ?? {}; const ex = a.extra ?? {};
  const core = table([{ text: t('u2.ana.metric') }, { text: t('chart.strategy'), num: true }, { text: t('u2.bt.hold'), num: true }], [
    metricRow(t('u2.ana.total'), f.total, bf.total, 1, '%'), metricRow(t('metric.cagr'), f.cagr, bf.cagr, 1, '%'),
    metricRow(t('metric.vol'), f.vol, bf.vol, 1, '%', 'low'), metricRow('Sharpe', f.sharpe, bf.sharpe),
    metricRow(t('u2.ana.sortino'), f.sortino, bf.sortino), metricRow(t('u2.ana.calmar'), f.calmar, bf.calmar),
    metricRow(t('metric.maxdd'), f.maxdd, bf.maxdd, 1, '%', '')]);
  const dist = table([], [
    [t('u2.ana.best_day'), { v: pct(ex.best_day, 2), num: true, cls: 'is-rise' }], [t('u2.ana.worst_day'), { v: pct(ex.worst_day, 2), num: true, cls: 'is-fall' }],
    [t('u2.ana.var'), { v: pct(ex.var95, 2), num: true }], [t('u2.ana.skew'), num(ex.skew)], [t('u2.ana.kurt'), num(ex.kurt)],
    [t('u2.ana.pos_months'), num(ex.pos_months, 0, '%')]]);
  return el('div', { class: 'ws-grid ws-grid--2' }, card(t('u2.ana.core'), null, core), card(t('u2.ana.dist'), null, dist));
}

/** 样本内 / 样本外：前 70% / 后 30%，附带判断语 */
function split(a, r) {
  const sp = a.split ?? {};
  if (!sp.date) return card(t('u2.ana.split'), null, el('p', { class: 'muted' }, t('u2.ana.split_short')));
  const seg = (k) => sp[k] ?? {};
  const row = (name, k, kb) => [name, num(seg(k).cagr, 1, '%'), num(seg(k).sharpe), num(seg(k).maxdd, 1, '%'), num(seg(kb).cagr, 1, '%'), num(seg(kb).sharpe), num(seg(kb).maxdd, 1, '%')];
  const heads = ['u2.ana.period', 'u2.ana.s_cagr', 'u2.ana.s_sharpe', 'u2.ana.s_dd', 'u2.ana.h_cagr', 'u2.ana.h_sharpe', 'u2.ana.h_dd'].map((k, i) => ({ text: t(k), num: i > 0 }));
  const si = seg('is').sharpe; const so = seg('oos').sharpe;
  const verdict = isNum(si) && isNum(so) ? t(so >= si * 0.6 ? 'u2.ana.oos_ok' : 'u2.ana.oos_bad') : '';
  return card(t('u2.ana.split'), null,
    table(heads, [row(t('u2.ana.is', { a: r.start, b: sp.date }), 'is', 'b_is'), row(t('u2.ana.oos', { a: sp.date, b: r.end }), 'oos', 'b_oos')]),
    el('p', { class: 'ws-note' }, verdict, ' ', t('u2.ana.split_warn')));
}

/** 月度收益：热力图（策略）+ 年度对比表（策略全年 vs 持有全年） */
function monthly(a) {
  const mo = a.monthly ?? { years: [], matrix: [], year_total: [] };
  if (!mo.years.length) return null;
  const byYear = {}; const totals = {};
  mo.years.forEach((y, i) => { byYear[String(y)] = mo.matrix[i]; totals[String(y)] = mo.year_total[i]; });
  const box = el('div', { class: 'table-wrap' });
  const by = a.bench_year ?? [];
  const yearTable = table([{ text: t('u2.ana.year') }, { text: t('u2.ana.year_s'), num: true }, { text: t('u2.ana.year_h'), num: true }],
    mo.years.map((y, i) => [String(y), { v: pct(mo.year_total[i], 1), num: true, cls: toneClass(mo.year_total[i]) }, { v: pct(by[i], 1), num: true, cls: toneClass(by[i]) }]).reverse());
  queueMicrotask(() => renderHeatmap(box, byYear, totals));
  return card(t('backtest.heatmap.title'), t('u2.ana.monthly_hint'), box, el('div', { class: 'u2-gap' }, yearTable));
}

function tradeStats(a) {
  const tr = a.trades ?? {};
  if (!tr.n) return card(t('u2.ana.trades'), null, el('p', { class: 'muted' }, t('u2.ana.no_trades')));
  const rows = [
    [t('metric.trades'), num(tr.n, 0), t('metric.win'), num(tr.win, 1, '%')],
    [t('u2.ana.avg_win'), { v: pct(tr.avg_win, 2), num: true, cls: 'is-rise' }, t('u2.ana.avg_loss'), { v: pct(tr.avg_loss, 2), num: true, cls: 'is-fall' }],
    [t('u2.ana.payoff'), num(tr.payoff), t('u2.ana.expect'), { v: pct(tr.expectancy, 2), num: true, cls: toneClass(tr.expectancy) }],
    [t('u2.ana.best_trade'), { v: pct(tr.best, 2), num: true, cls: 'is-rise' }, t('u2.ana.worst_trade'), { v: pct(tr.worst, 2), num: true, cls: 'is-fall' }],
    [t('u2.ana.avg_days'), num(tr.avg_days, 1), t('u2.ana.streak'), num(tr.max_lose_streak, 0)]];
  const h1 = el('div', { class: 'u2-hist' }); const h2 = el('div', { class: 'u2-hist' });
  queueMicrotask(() => requestAnimationFrame(() => { histogram(h1, tr.ret_hist, 'pct'); histogram(h2, tr.days_hist, 'days'); }));
  return card(t('u2.ana.trades'), null, table([], rows),
    el('div', { class: 'ws-grid ws-grid--2 u2-gap' },
      el('div', {}, el('p', { class: 'eyebrow' }, t('u2.ana.hist_ret')), h1), el('div', {}, el('p', { class: 'eyebrow' }, t('u2.ana.hist_days')), h2)));
}

function costSens(a) {
  const cs = a.cost_sens ?? [];
  if (!cs.length) return null;
  const name = (l) => (l === '0' ? t('u2.ana.cost0') : l === '1x' ? t('u2.ana.cost1') : t('u2.ana.cost_n', { n: l.replace('x', '') }));
  const note = cs.length >= 4 && isNum(cs[3].sharpe) ? t(cs[3].sharpe > 0.3 ? 'u2.ana.cost_ok' : 'u2.ana.cost_bad') : '';
  return card(t('u2.ana.cost'), t('u2.ana.cost_sub'),
    table([t('u2.ana.fee_level'), t('u2.ana.one_way'), t('metric.cagr'), 'Sharpe', t('metric.maxdd')].map((x, i) => ({ text: x, num: i > 0 })),
      cs.map((c) => [name(c.label), num(c.cost_bp, 1, 'bp'), num(c.cagr, 1, '%'), num(c.sharpe), num(c.maxdd, 1, '%')])),
    el('p', { class: 'ws-note' }, note));
}

function monteCarlo(a) {
  const mc = a.mc;
  if (!mc) return null;
  return card(t('u2.ana.mc'), t('u2.ana.mc_sub'),
    table([{ text: '' }, ...mc.pct.map((q) => ({ text: t('u2.ana.pctile', { q }), num: true }))], [
      [t('metric.cagr'), ...mc.cagr.map((v) => ({ v: fmt(v, 1, '%'), num: true, cls: toneClass(v) }))],
      [t('metric.maxdd'), ...mc.maxdd.map((v) => ({ v: fmt(v, 1, '%'), num: true, cls: 'is-fall' }))]]),
    el('p', { class: 'ws-note' }, t('u2.ana.mc_note', { block: mc.block, n: mc.n, p: fmt(mc.p_loss, 1, '%') })));
}

function paramSens(a) {
  const ps = a.param_sens ?? [];
  if (!ps.length) return null;
  return card(t('u2.ana.param'), t('u2.ana.param_sub'),
    ...ps.map((p) => el('div', { class: 'u2-gap' }, el('p', { class: 'eyebrow' }, p.label),
      table([{ text: t('u2.ana.value') }, ...p.pts.map((x) => ({ text: String(x.v), num: true }))],
        [['Sharpe', ...p.pts.map((x) => num(x.sharpe))], [t('metric.cagr'), ...p.pts.map((x) => num(x.cagr, 1, '%'))]]))),
    el('p', { class: 'ws-note' }, t('u2.ana.param_note')));
}

/** 导出：净值 CSV、交易明细 CSV（和旧页面同样的列） */
function exports(r) {
  const id = `${r.symbol || r.code || 'result'}_${r.strategy}`;
  const eqBtn = el('button', { class: 'btn btn--sm', type: 'button' }, t('u2.ana.export_eq'));
  eqBtn.addEventListener('click', () => downloadCSV(`${t('u2.csv.eq')}_${id}.csv`,
    [['date', t('u2.csv.date')], [(x) => x.eq, t('u2.csv.eq_s')], [(x) => x.bh, t('u2.csv.eq_h')], [(x) => x.dd, t('u2.csv.dd')]],
    r.dates.map((d, i) => ({ date: d, eq: r.equity[i], bh: r.bench[i], dd: r.drawdown[i] }))));
  const btns = [eqBtn];
  if ((r.trades ?? []).length) {
    const trBtn = el('button', { class: 'btn btn--sm', type: 'button' }, t('u2.ana.export_tr'));
    trBtn.addEventListener('click', () => downloadCSV(`${t('u2.csv.trades')}_${id}.csv`,
      [['entry', t('u2.csv.entry')], [(x) => (x.open ? t('u2.bt.holding') : x.exit), t('u2.csv.exit')], ['days', t('u2.csv.days')], ['ret', t('u2.csv.ret')],
        ['entry_px', t('u2.csv.entry_px')], ['exit_px', t('u2.csv.exit_px')], ['pnl', t('u2.csv.pnl')]], r.trades));
    btns.push(trBtn);
  }
  return el('div', { class: 'ws-filter u2-exports' }, ...btns);
}

/** 返回分析区的节点数组；r.analysis 为空（接口没给）时返回 [] */
export function analysisNodes(r) {
  const a = r.analysis;
  if (!a) return [];
  const gap = (n) => (n ? el('div', { class: 'u2-gap' }, n) : null);
  return [
    assumptions(a), gap(coreCards(a)), gap(split(a, r)), gap(monthly(a)), gap(chartSection(a, r)), gap(tradeStats(a)),
    gap(costSens(a)), gap(monteCarlo(a)), gap(paramSens(a)), exports(r)].filter(Boolean);
}
