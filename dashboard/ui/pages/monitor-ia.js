/* ==========================================================
 * 文件: ui/pages/monitor-ia.js
 * 用途: 盯盘 / 美股盯盘共用的「实时分析」三栏：执行提醒 | 信号追踪 | 新机会
 *   数据来自 quotes 接口里的 intraday { events[], tracking { rows[], summary, write_error } }
 *   market = 'cn'：追踪表最后两列是距涨停 / 距跌停；market = 'us'：距当日高点 / 低点，事件多一个「仅展示」标记
 * ========================================================== */

import { el, fmt } from '../shared/dom.js';
import { t } from '../shared/i18n.js';
import { isNum, pctText, tone, span, table, td, hasKey } from './u3-common.js';

/** 事件种类 → 文案键（显式表，不拼接字符串） */
const EV_KEYS = { buy_window: 'mon.ev.buy_window', limit_up_no_buy: 'mon.ev.limit_up_no_buy', limit_down_no_sell: 'mon.ev.limit_down_no_sell',
  holding_drop: 'mon.ev.holding_drop', watch: 'mon.ev.watch', consider_sell: 'mon.ev.consider_sell' };
const EV_TRIG = { momentum: 'mon.ev.trig.momentum', volume: 'mon.ev.trig.volume', breakout: 'mon.ev.trig.breakout' };
const EV_ACT = { 买入: 'mon.ia.act.buy', 卖出: 'mon.ia.act.sell', 加仓: 'mon.ia.act.add', 减仓: 'mon.ia.act.trim' };
const EV_EXEC = ['buy_window', 'limit_up_no_buy', 'limit_down_no_sell', 'holding_drop'];
const EV_OPP = ['watch', 'consider_sell'];

/** ISO 时间 → HH:MM:SS（接口已经是各自市场的本地时间） */
export const clock = (iso) => (typeof iso === 'string' && iso.length >= 19 ? iso.slice(11, 19) : '—');

function eventLine(ev, market, extra) {
  const params = ev.params || {};
  const trig = (Array.isArray(params.triggers) ? params.triggers : []).map((x) => (EV_TRIG[x] ? t(EV_TRIG[x]) : '')).join(' / ');
  const text = t(EV_KEYS[ev.kind] || EV_KEYS.watch, {
    code: String(ev.code), minutes: String(params.minutes ?? ''), pct: isNum(params.pct) ? Math.abs(params.pct).toFixed(2) : '', triggers: trig });
  const sev = ev.severity === 'ok' ? 'ok' : ev.severity === 'watch' ? 'watch' : 'danger';
  const p = el('p', { class: `u3-ev u3-ev--${sev}` }, span(`${clock(ev.timestamp)} `, 'muted'), span(text));
  if (extra) p.append(' ', extra);
  if (ev.reference_only) p.append(' ', el('small', { class: 'muted' }, t('mon.ev.note')));
  if (market === 'us' && ev.display_only) p.append(' ', el('small', { class: 'muted' }, t('usmon.ev.display_only')));
  return p;
}

function card(titleKey, ...children) {
  return el('section', { class: 'card u3-ia-card' }, el('div', { class: 'card__head' }, el('h2', { class: 'card__title' }, t(titleKey))), ...children);
}

function eventList(events, kinds, emptyKey, market, extra) {
  const rows = events.filter((e) => kinds.includes(e.kind)).slice().reverse().map((e) => eventLine(e, market, extra ? extra(e) : null));
  return rows.length ? rows : [el('p', { class: 'ws-note u3-empty', role: 'status' }, t(emptyKey))];
}

function trackingBlock(tracking, market) {
  const rows = Array.isArray(tracking.rows) ? tracking.rows : [];
  const parts = [];
  const sum = tracking.summary;
  if (sum && sum.total > 0) {
    parts.push(el('p', { class: 'u3-ia-summary' }, t('mon.ia.summary', { total: sum.total, winners: sum.winners, losers: sum.losers, mean: fmt(sum.mean_return, 2), median: fmt(sum.median_return, 2) })));
  } else {
    parts.push(el('p', { class: 'ws-note u3-ia-summary' }, t('mon.ia.summary_empty')));
  }
  if (tracking.write_error) parts.push(el('p', { class: 'u3-msg u3-msg--error' }, t('mon.ia.write_error')));
  const us = market === 'us';
  const a = us ? 'to_day_high_pct' : 'to_limit_up_pct', b = us ? 'to_day_low_pct' : 'to_limit_down_pct';
  const pct = (v) => (isNum(v) ? span(pctText(v), tone(v) || 'is-flat') : span('—'));
  const trs = rows.filter((r) => r.type === 'track').map((r) => el('tr', {},
    td(r.code, false, 'mono'), td(EV_ACT[r.action] ? t(EV_ACT[r.action]) : r.action),
    td(isNum(r.open) ? fmt(r.open, 2) : '—', true), td(isNum(r.price) ? fmt(r.price, 2) : '—', true),
    td(pct(r.change_vs_open_pct), true), td(pct(r.return_since_open), true),
    td(span(pctText(r[a])), true, 'muted'), td(span(pctText(r[b])), true, 'muted')));
  const heads = ['mon.ia.col.code', 'mon.ia.col.action', 'mon.ia.col.open', 'mon.ia.col.price', 'mon.ia.col.chg', 'mon.ia.col.cum',
    us ? 'usmon.ia.col.high' : 'mon.ia.col.up', us ? 'usmon.ia.col.low' : 'mon.ia.col.down'];
  parts.push(table(heads, trs, us ? 'usmon.ia.track.empty' : 'mon.ia.track.empty', { numCols: [2, 3, 4, 5, 6, 7] }));
  return parts;
}

/** 返回「实时分析」整段（标题 + 说明 + 三栏） */
export function intradaySection(intraday, market) {
  const ia = intraday || { events: [], tracking: null };
  const events = Array.isArray(ia.events) ? ia.events : [];
  const tracking = ia.tracking || { rows: [], summary: null };
  const rows = Array.isArray(tracking.rows) ? tracking.rows : [];
  const us = market === 'us';
  const sinceTrigger = (e) => {
    const r = rows.find((x) => x.type === 'trigger' && x.code === e.code && x.kind === e.kind);
    if (!r || !isNum(r.return_since_trigger)) return null;
    return span(t('mon.ev.since_trigger', { ret: fmt(r.return_since_trigger, 2, '%') }), tone(r.return_since_trigger) || 'is-flat');
  };
  const noteKey = us && hasKey('usmon.ia.note') ? 'usmon.ia.note' : 'mon.ia.note';
  return el('section', { class: 'u3-ia', 'aria-label': t('mon.ia.title') },
    el('h2', { class: 'u3-ia__title' }, t('mon.ia.title')),
    el('p', { class: 'ws-note' }, t(noteKey)),
    el('div', { class: 'u3-ia-grid' },
      card('mon.ia.exec.title', ...eventList(events, EV_EXEC, us ? 'usmon.ia.exec.empty' : 'mon.ia.exec.empty', market)),
      card('mon.ia.track.title', ...trackingBlock(tracking, market)),
      card('mon.ia.opp.title', ...eventList(events, EV_OPP, us ? 'usmon.ia.opp.empty' : 'mon.ia.opp.empty', market, sinceTrigger))));
}
