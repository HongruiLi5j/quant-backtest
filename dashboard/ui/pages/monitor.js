/* ==========================================================
 * 文件: ui/pages/monitor.js
 * 用途: Workspace · 盯盘（A 股）与美股盯盘共用的页面逻辑。地址 monitor.html 是 A 股，monitor.html?market=us 是美股
 *   （nav.js 的「美股盯盘」就指向后者；对应旧界面 #/monitor 与 #/usmonitor）
 *   控制卡（状态 / 开始 / 停止 / 配置）+ 实时分析（pages/monitor-ia.js）+ 监控股票表 + 告警历史
 * 接口: A 股 /api/market_monitor/{quotes,alerts,start,stop,config}；美股 /api/us_market_monitor/ 同名
 * 说明: 页面打开期间每 10 秒轮询本机接口；换语言 / 按 R 重新渲染时旧定时器会先清掉。报价只放内存
 * ========================================================== */

import { el } from '../shared/dom.js';
import { t } from '../shared/i18n.js';
import { getJSON, ApiError } from '../shared/api.js';
import { messageBox, errKey, post, isNum, pctText, tone, span, table, td, hasKey } from './u3-common.js';
import { intradaySection, clock } from './monitor-ia.js';
import { buildConfig } from './monitor-config.js';

const POLL_MS = 10000;
let timer = null;

const marketOf = () => (new URLSearchParams(window.location.search).get('market') === 'us' ? 'us' : 'cn');
const keyIf = (key, fallback) => (hasKey(key) ? key : fallback);

/** 状态 / 阶段 / 原因 / 质量 的词条：美股优先用 usmon.*，找不到回退 */
function stateKeys(us, s) {
  return {
    state: keyIf(`mon.state.${s.state}`, 'mon.state.stopped'),
    phase: us ? keyIf(`usmon.phase.${s.phase}`, 'usmon.phase.idle') : keyIf(`mon.phase.${s.phase}`, 'mon.phase.idle'),
    reason: us ? keyIf(`usmon.reason.${s.reason}`, 'usmon.reason.provider_error') : keyIf(`mon.reason.${s.reason}`, 'mon.reason.provider_error'),
  };
}

function statusLines(s, us, cfg) {
  if (!s) return [];
  const k = stateKeys(us, s);
  const lines = [
    el('p', {}, t('mon.status.line', { state: t(k.state), phase: t(k.phase) })),
    el('p', { class: 'muted' }, t('mon.status.stats', { used: s.requests_used, budget: s.request_budget, count: s.watch_count, max: s.max_watch, rounds: s.rounds })),
  ];
  if (s.state === 'paused' && s.reason) lines.push(el('p', { class: 'u3-warn' }, t('mon.reason.line', { reason: t(k.reason) })));
  if (s.truncated) lines.push(el('p', { class: 'u3-warn' }, t('mon.status.truncated')));
  if (!us) {
    // 降级（批量连续失败，实际在用 tencent）与「请求了批量但没装 easyquotation」是两种不同状态
    if (s.degraded) lines.push(el('p', { class: 'u3-warn' }, t('mon.status.degraded')));
    else if (s.batch_requested === true && s.batch_mode === false) lines.push(el('p', { class: 'u3-warn' }, t('mon.status.easy_missing')));
  }
  return lines;
}

function quotesTable(quotes, us) {
  const rows = quotes.map((q) => {
    const sources = (Array.isArray(q.sources) ? q.sources : []).map((x) => t(keyIf(`mon.src.${x}`, 'mon.src.watchlist'))).join(' / ');
    const qualityKey = us ? keyIf(`usmon.quality.${q.quality}`, 'usmon.quality.invalid') : keyIf(`mon.quality.${q.quality}`, 'mon.quality.pending');
    const bad = q.quality === 'invalid' || q.quality === 'stale';
    return el('tr', {},
      td(q.code, false, 'mono'), td(q.name || '—'),
      td(isNum(q.price) ? Number(q.price).toFixed(2) : '—', true),
      td(span(pctText(q.pct), tone(q.pct) || 'is-flat'), true),
      td(clock(q.received_at), true, 'muted'),
      td(t(keyIf(`mon.alert.${q.alert}`, 'mon.alert.none'))),
      td(sources, false, 'muted'),
      td(el('span', { class: `badge ${bad ? 'badge--warn' : ''}`.trim() }, t(qualityKey))));
  });
  const heads = ['mon.col.code', 'mon.col.name', 'mon.col.price', 'mon.col.pct', 'mon.col.time', 'mon.col.alert', 'mon.col.source', 'mon.col.quality'];
  return table(heads, rows, us ? 'usmon.q.empty' : 'mon.q.empty', { numCols: [2, 3, 4] });
}

function alertsTable(alerts) {
  const rows = alerts.slice().reverse().map((a) => el('tr', {},
    td(clock(a.triggered_at), true, 'muted'), td(a.code, false, 'mono'), td(t(keyIf(`mon.dir.${a.direction}`, 'mon.dir.up'))),
    td(Number(a.threshold).toFixed(2), true), td(`${Number(a.previous_price).toFixed(2)} → ${Number(a.price).toFixed(2)}`, true)));
  return table(['mon.a.col.time', 'mon.a.col.code', 'mon.a.col.dir', 'mon.a.col.threshold', 'mon.a.col.price'], rows, 'mon.a.empty', { numCols: [0, 3, 4] });
}

/** 顶上的 A 股 / 美股切换（普通链接，保留 ?mock=1） */
function marketTabs(market) {
  const mock = new URLSearchParams(window.location.search).get('mock') === '1';
  const href = (m) => `monitor.html${[m === 'us' ? 'market=us' : '', mock ? 'mock=1' : ''].filter(Boolean).map((x, i) => (i ? '&' : '?') + x).join('')}`;
  return el('div', { class: 'segmented u3-tabs', role: 'tablist', 'aria-label': t('mon.title') },
    ...['cn', 'us'].map((m) => el('a', { class: 'segmented__btn', role: 'tab', href: href(m), 'aria-selected': String(m === market), 'aria-pressed': String(m === market) }, t(`u3.mon.tab.${m}`))));
}

function section(titleKey, ...children) {
  return el('section', { class: 'card', 'aria-label': t(titleKey) }, el('div', { class: 'card__head' }, el('h2', { class: 'card__title' }, t(titleKey))), ...children);
}

export async function render(root) {
  if (timer) { clearInterval(timer); timer = null; }
  const market = marketOf(), us = market === 'us';
  const base = us ? '/api/us_market_monitor' : '/api/market_monitor';
  const h1 = document.querySelector('.ws-head__title');
  if (h1) { h1.dataset.i18n = us ? 'usmon.title' : 'mon.title'; h1.textContent = t(us ? 'usmon.title' : 'mon.title'); }
  document.title = `${t(us ? 'usmon.title' : 'mon.title')} · Workspace · Quant Backtest`;

  const st = { status: null, quotes: [], alerts: [], intraday: null, busy: false };
  const msg = messageBox();
  const statusBox = el('div', { id: 'monStatus' });
  const quotesBox = el('div', { id: 'monQuotes' });
  const alertsBox = el('div', { id: 'monAlerts' });
  const iaBox = el('div', { id: 'monIntraday' });

  const start = el('button', { class: 'btn btn--primary', type: 'button', id: 'monStart' }, t('mon.btn.start'));
  const stop = el('button', { class: 'btn btn--ghost', type: 'button', id: 'monStop' }, t('mon.btn.stop'));
  const cfg = buildConfig({ market, base, msg, getStatus: () => st.status, refresh: () => refresh() });

  function draw() {
    statusBox.replaceChildren(...statusLines(st.status, us));
    const running = st.status?.state === 'running';
    start.disabled = running || st.busy;
    stop.disabled = !running && st.status?.state !== 'paused';
    cfg.sync(st.status);
    quotesBox.replaceChildren(quotesTable(st.quotes, us));
    alertsBox.replaceChildren(alertsTable(st.alerts));
    iaBox.replaceChildren(intradaySection(st.intraday, market));
  }

  async function fetchAll() {
    const [q, a] = await Promise.all([getJSON(`${base}/quotes`), getJSON(`${base}/alerts`)]);
    if (q?.ok !== true || a?.ok !== true) throw new ApiError(200, 'monitor 返回异常', q?.ok !== true ? q : a);
    st.status = q.status; st.quotes = q.quotes || []; st.alerts = a.alerts || []; st.intraday = q.intraday || null;
  }

  /** 轮询 / 操作后刷新：失败时清掉报价（旧价格不留在页面上）并提示 */
  async function refresh() {
    if (document.hidden) return;
    try {
      await fetchAll();
      msg.clear();
    } catch (err) {
      st.status = null; st.quotes = []; st.alerts = []; st.intraday = null;
      msg.show(errKey(err, us ? ['usmon', 'mon'] : ['mon']));
    }
    draw();
  }

  async function act(path) {
    if (st.busy) return;
    st.busy = true; draw();
    const r = await post(path, {});
    st.busy = false;
    if (!r.ok) { msg.show(errKey(r.err, us ? ['usmon', 'mon'] : ['mon'])); draw(); return; }
    await refresh();
  }
  start.addEventListener('click', () => act(`${base}/start`));
  stop.addEventListener('click', () => act(`${base}/stop`));

  await fetchAll();   // 首次失败直接抛出，由页面启动器显示错误卡片（登录过期有专门提示）
  // .ws-mon[data-market="us"] 只在本页区块内把涨跌色换成美股的绿涨红跌，不改全局偏好
  root.replaceChildren(el('div', { class: 'ws-mon', dataset: { market } },
    marketTabs(market),
    el('p', { class: 'ws-note' }, t(us ? 'usmon.note' : 'mon.note')),
    msg.node,
    section('mon.ctl.title', statusBox, el('div', { class: 'u3-btn-row' }, start, stop), cfg.node),
    iaBox,
    section('mon.q.title', el('p', { class: 'ws-note' }, t(us ? 'usmon.q.time_note' : 'mon.q.time_note')), quotesBox),
    section('mon.a.title', alertsBox)));
  draw();
  timer = setInterval(refresh, POLL_MS);
}
