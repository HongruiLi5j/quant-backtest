/* 后台页面：纯 JS，无外部依赖。
   所有文字都用 textContent 写入（日志、股票名里可能有任意字符），不使用 innerHTML，所以不会被注入脚本。 */
(() => {
'use strict';
const TABS = [['overview', '概览'], ['site', '站点开关'], ['sections', '分区开关'], ['members', '会员'], ['access', '分享与访问'], ['tasks', '回测与任务'], ['data', '数据'], ['system', '系统'], ['logs', '日志'], ['audit', '操作记录']];
const NAV_GROUPS = [['', ['overview']], ['站点', ['site', 'sections']], ['人与分享', ['members', 'access']], ['运行', ['tasks', 'data', 'system']], ['记录', ['logs', 'audit']]];
const NEED_BACKEND = '需要后端改动';
const ACTION_LABEL = {
  login: '登录后台', refresh: '立即更新（联网）', kick_update: '触发每日检查', task_start: '启动任务', task_stop: '中止任务',
  scan_start: '启动全市场扫描', scan_stop: '中止全市场扫描', watch_add: '添加自选股', watch_remove: '移除自选股',
  update_stock: '更新个股', gap_start: '数据缺口检查', backup_make: '生成备份', backup_download: '下载备份',
  restart_server: '重启网页服务', share_start: '开启分享', share_stop: '停止分享', share_pass_set: '设置分享密码', share_pass_clear: '取消分享密码', clear_visits: '清空访客记录', member_disable: '停用会员', member_enable: '启用会员', member_delete: '删除会员',
  rotate_main_token: '更换主令牌', rotate_admin_token: '更换管理令牌', setting: '修改后台设置', blocked_remote: '拦截了外网来的访问'
};
const MARKET_LABEL = { cn: 'A 股', us: '美股' };
const state = { tab: 'overview', log: { name: 'daily', n: 200, filter: '', auto: true }, gapRunning: false, draft: {}, site: null, saving: false, sessions: [], sysOverview: null };
const $ = (s, r = document) => r.querySelector(s);

// ---------- 小工具
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (k === 'style') el.setAttribute('style', v);
    else if (k in el) el[k] = v;
    else el.setAttribute(k, v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return el;
}
const bytes = n => n == null ? '—' : n < 1024 ? n + ' B' : n < 1048576 ? (n / 1024).toFixed(1) + ' KB' : n < 1073741824 ? (n / 1048576).toFixed(1) + ' MB' : (n / 1073741824).toFixed(2) + ' GB';
const dur = s => s < 60 ? s + ' 秒' : s < 3600 ? Math.floor(s / 60) + ' 分钟' : s < 86400 ? (s / 3600).toFixed(1) + ' 小时' : (s / 86400).toFixed(1) + ' 天';
const ago = ts => { if (!ts) return '—'; const s = Math.max(0, Math.floor(Date.now() / 1000 - ts)); return s < 60 ? '刚刚' : dur(s) + '前'; };
const dt = ts => ts ? new Date(ts * 1000).toLocaleString('zh-CN', { hour12: false }) : '—';
const pill = (text, kind) => h('span', { class: 'pill ' + (kind || '') }, text);
const tile = (k, v, s, p) => h('div', { class: 'tile' }, h('div', { class: 'k' }, k, p || ''), h('div', { class: 'v' }, v), h('div', { class: 's' }, s || ''));
const busy = () => { if (Object.keys(state.draft).length) return true; const a = document.activeElement || {}; if (a.tagName === 'INPUT' && !['checkbox', 'button', 'radio'].includes(a.type)) return true; return [...document.querySelectorAll('#view input')].some(x => ['text', 'password', 'search', 'number'].includes(x.type) && x.value); };   // 正在输入（或输入框里已有内容）时不自动刷新：刷新会重建页面、丢掉输入

function toast(msg, err) {
  const d = h('div', { class: err ? 'err' : '' }, msg);
  $('#toast').append(d);
  setTimeout(() => d.remove(), err ? 6000 : 3500);
}
function banner(msg, info) {
  const b = $('#banner');
  b.replaceChildren();
  if (msg) b.append(h('div', { class: 'banner' + (info ? ' info' : '') }, msg));
}
function confirmDialog({ title, body, ok = '确认', danger = true }) {
  return new Promise(resolve => {
    const done = v => { bg.remove(); resolve(v); };
    const bg = h('div', { class: 'modal-bg', onclick: e => { if (e.target === bg) done(false); } },
      h('div', { class: 'modal', role: 'dialog' }, h('h3', {}, title), h('p', {}, body),
        h('div', { class: 'row', style: 'justify-content:flex-end' },
          h('button', { class: 'btn', onclick: () => done(false) }, '取消'),
          h('button', { class: 'btn ' + (danger ? 'danger' : 'primary'), onclick: () => done(true) }, ok))));
    document.body.append(bg);
  });
}

// ---------- 请求
async function api(path, opts = {}) {
  const init = { method: 'GET', headers: { 'X-Requested-With': 'dashboard' }, credentials: 'same-origin' };
  if (opts.body) { init.method = 'POST'; init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(opts.body); }
  let r;
  try { r = await fetch(path, init); } catch (e) { throw new Error('连不上网页服务（可能正在重启，几秒后会自动重连）'); }
  if (r.status === 401) { banner('登录已过期（会话 8 小时有效）。请在终端运行：cd ~/Documents/Projects/quant-backtest && bash scripts/open_admin.sh'); throw new Error('未登录或会话已过期'); }
  let j = null;
  try { j = await r.json(); } catch (e) { /* 不是 JSON */ }
  if (!r.ok) throw new Error((j && j.error) || ('请求失败 ' + r.status));
  banner('');
  return j;
}
async function act(action, body = {}, ask) {
  if (ask) {
    if (!await confirmDialog(ask)) return null;
    body = { ...body, confirm: true };
  }
  try {
    const j = await api('/api/admin/action', { body: { action, ...body } });
    toast(j.note || '已完成');
    setTimeout(refresh, 400);
    return j;
  } catch (e) { toast(e.message, true); return null; }
}
// 设计稿里的开关行：live=本机已有接口可用；否则灰色不可点，旁边写“需要后端改动”
const swOff = (on) => h('span', { class: 'sw off', 'aria-disabled': 'true', title: NEED_BACKEND }, h('span', { class: 'knob' }), on === true ? '开' : '关', h('span', { class: 'tag wait' }, NEED_BACKEND));
const swRow = (title, hint, ctl, tag) => h('div', { class: 'swrow', 'data-q': (title + ' ' + hint).toLowerCase() },
  h('div', {}, h('div', { class: 't' }, title, tag ? h('span', { class: 'tag ' + (tag === '本机已有' ? 'live' : 'wait') }, tag) : null), h('div', { class: 'd' }, hint)), ctl);
const offRow = (title, hint, on) => swRow(title, hint, swOff(on), '接入后可用');
const offCard = (title, desc, rows) => h('div', { class: 'card' }, h('h3', {}, title), desc ? h('p', { class: 'desc' }, desc) : null, ...rows);
const btn = (label, onclick, cls = '', disabled) => h('button', { class: 'btn ' + cls, onclick, disabled: !!disabled }, label);

// ---------- 概览
function ageBadge(age) {
  if (age == null) return pill('未知', '');
  if (age <= 3) return pill('最新', 'ok');          // 周末、假期最多差 3 天左右
  return pill('落后 ' + age + ' 天', age <= 7 ? 'warn' : 'bad');
}
function refreshBox(r) {
  if (!r || !(r.running || r.returncode != null)) return null;
  const bad = !r.running && r.returncode;
  return h('div', { style: 'margin-top:10px' },
    h('div', { class: 'row between' }, h('b', {}, r.running ? '正在更新：' + r.phase : (bad ? '上次手动更新失败' : '上次手动更新已完成')),
      r.percent != null ? h('span', { class: 'muted' }, r.percent + '%') : null),
    h('div', { class: 'bar-track' }, h('div', { class: 'bar-fill' + (r.running && r.percent == null ? ' busy' : ''), style: 'width:' + (r.percent == null ? 100 : r.percent) + '%' })),
    h('div', { class: 'dim' }, r.message || ''));
}
function viewOverview(o) {
  const cn = o.markets.cn, us = o.markets.us, all = cn.all || {}, prog = all.progress || {};
  const upd = o.launchd.find(x => x.label.endsWith('.update')) || {};
  let updPill = pill('已加载', 'ok');
  if (upd.available === false) updPill = pill('无 launchd', '');
  else if (!upd.loaded) updPill = pill('未加载', 'bad');
  else if (upd.last_exit) updPill = pill('上次退出码 ' + upd.last_exit, 'warn');
  const share = o.share || {};
  const complete = prog.complete !== false;
  const gen = m => (m.generated_at || '—').replace('T', ' ').slice(0, 16);
  return h('div', {},
    h('h1', {}, '概览'), h('p', { class: 'lead' }, '系统是否在正常运转：数据新不新、定时任务在不在跑、有没有东西卡住。每 5 秒自动刷新。'),
    h('div', { class: 'tiles' },
      tile('网页服务', '运行中', `PID ${o.server.pid} · 已运行 ${dur(o.server.uptime)}`, o.server.managed ? pill('launchd 托管', 'ok') : pill('手动启动', 'warn')),
      tile('每日任务', o.daily_running ? '运行中' : '空闲', '每 15 分钟自动检查一次', updPill),
      tile('A 股数据', cn.data_date || '—', '生成于 ' + gen(cn), ageBadge(cn.age_days)),
      tile('美股数据', us.data_date || '—', '生成于 ' + gen(us), ageBadge(us.age_days)),
      tile('全市场数据', `${(all.cached || 0).toLocaleString()} / ${all.listed ? Number(all.listed).toLocaleString() : '?'}`,
        `交易日 ${prog.trade_day || '—'} · 更新于 ${(prog.updated || '—').replace('T', ' ').slice(5, 16)}`,
        complete ? pill('已更新完', 'ok') : pill('未完成 剩 ' + (prog.remaining ?? '?'), 'warn')),
      tile('外网分享', share.running ? (share.starting ? '连接中' : '开启中') : '未开启', share.running ? '分享链接进不了后台' : '只有本机能访问',
        share.running ? pill('已对外', 'warn') : pill('关闭', 'ok')),
      tile('磁盘', bytes(o.disk.data), `日志 ${bytes(o.disk.logs)} · 可用 ${bytes(o.disk.free)}`)),
    refreshBox(o.refresh) && h('div', { class: 'card', style: 'margin-top:14px' }, refreshBox(o.refresh)),
    h('h2', {}, 'launchd 后台任务', h('small', {}, '开机自启，由系统守护')),
    h('div', { class: 'card' }, h('table', {}, h('thead', {}, h('tr', {}, h('th', {}, '任务'), h('th', {}, '状态'), h('th', { class: 'num' }, 'PID'), h('th', { class: 'num' }, '上次退出码'))),
      h('tbody', {}, o.launchd.map(x => h('tr', {}, h('td', {}, x.label), h('td', {}, x.available === false ? pill('不可用', '') : x.loaded ? pill(x.pid ? '运行中' : '空闲待命', 'ok') : pill('未加载', 'bad')),
        h('td', { class: 'num' }, x.pid || '—'), h('td', { class: 'num' }, x.last_exit ?? '—')))))),
    h('h2', {}, '磁盘占用', h('small', {}, '每 2 分钟统计一次')),
    h('div', { class: 'card' }, h('table', {}, h('thead', {}, h('tr', {}, h('th', {}, '位置'), h('th', { class: 'num' }, '大小'))),
      h('tbody', {}, [['data/ 合计', o.disk.data], ...o.disk.top.map(t => [t.name, t.bytes]), ['logs/', o.disk.logs], ['export/', o.disk.export]]
        .map(([n, b]) => h('tr', {}, h('td', {}, n), h('td', { class: 'num' }, bytes(b))))))));
}

// ---------- 任务
function viewTasks(o) {
  const r = o.refresh || {};
  const scan = o.scan || {}, sp = scan.progress || {};
  const spct = sp.total ? Math.round(100 * sp.done / sp.total) : null;
  return h('div', {},
    h('h1', {}, '任务'), h('p', { class: 'lead' }, '手动触发数据更新、回测、全市场扫描。同一时间只会有一个更新在跑，重复点击会提示已在运行。'),
    h('div', { class: 'card' }, h('h3', {}, '数据更新'),
      h('p', { class: 'desc' }, '联网下载最新行情并回测，和主页面右上角「立即更新」一样。A 股全市场约需几十分钟。'),
      h('div', { class: 'row' }, btn('更新全部', () => act('refresh'), 'primary', r.running), btn('只更新 A 股', () => act('refresh', { market: 'cn' }), '', r.running),
        btn('只更新美股', () => act('refresh', { market: 'us' }), '', r.running), btn('触发一次每日检查', () => act('kick_update'), '', o.daily_running),
        o.daily_running ? pill('每日任务正在运行', 'warn') : null),
      refreshBox(r)),
    h('div', { class: 'card' }, h('h3', {}, '回测与回补'), h('p', { class: 'desc' }, '不联网的重新回测，以及补下载缺失的历史数据（可随时中止，下次续传）。'),
      h('table', {}, h('tbody', {}, Object.entries(o.tasks).map(([k, t]) => h('tr', {},
        h('td', {}, h('div', {}, t.label), t.tail.length ? h('pre', { class: 'log mini' }, t.tail.join('\n')) : null),
        h('td', { style: 'white-space:nowrap' }, t.running ? pill('运行中 · ' + ago(t.started).replace('前', ''), 'warn') : t.returncode == null ? pill('未运行', '') : pill(t.returncode === 0 ? '上次成功' : '上次失败 ' + t.returncode, t.returncode === 0 ? 'ok' : 'bad')),
        h('td', { style: 'text-align:right' }, t.running ? btn('中止', () => act('task_stop', { task: k }), 'sm') : btn('启动', () => act('task_start', { task: k }), 'sm primary'))))))),
    h('div', { class: 'card' }, h('h3', {}, '三引擎全市场扫描'),
      h('p', { class: 'desc' }, '全部 A 股 × 7 个策略 × 3 个引擎，用来验证引擎一致性，约 1~3 小时，可中断续跑。'),
      h('div', { class: 'row' }, scan.running ? btn('中止扫描', () => act('scan_stop'), 'danger') : btn('启动扫描', () => act('scan_start'), 'primary'),
        scan.running ? pill('运行中', 'warn') : sp.finished ? pill('上次完成于 ' + sp.finished, 'ok') : pill('未运行', '')),
      sp.total ? h('div', {}, h('div', { class: 'bar-track' }, h('div', { class: 'bar-fill', style: `width:${spct}%` })),
        h('div', { class: 'dim' }, `${(sp.done || 0).toLocaleString()} / ${sp.total.toLocaleString()} 只 · ${spct}%${sp.started ? ' · 开始于 ' + sp.started : ''}`)) : null),
    offCard('队列与配额', '会员回测队列、配额、功能开关等后端做好后接入。', [offRow('暂停回测队列', '暂停后新任务排队不执行。', false), offRow('会员可跑回测', '', true), offRow('可用引擎', '', true), offRow('每日配额', '免费 / Pro 的回测与翻译次数。', true)]),
    h('div', { class: 'card' }, h('h3', {}, '服务维护'),
      h('p', { class: 'desc' }, '改了代码后服务会在 5 秒内自动重启，一般不用手动重启。' + (o.server.managed ? '' : '（当前不是 launchd 启动的，重启会被拒绝。）')),
      btn('重启网页服务', () => act('restart_server', {}, { title: '重启网页服务？', body: '服务会中断几秒，launchd 会自动拉起。正在跑的网页回测任务会中断，后台子进程（更新、扫描）不受影响。', ok: '重启' }), 'danger', !o.server.managed)));
}

// ---------- 数据
function gapCard(g) {
  const res = g.result;
  const pct = g.total ? Math.round(100 * g.done / g.total) : 0;
  const body = [];
  if (g.running) body.push(h('div', {}, h('div', { class: 'bar-track' }, h('div', { class: 'bar-fill', style: `width:${pct}%` })), h('div', { class: 'dim' }, `正在检查 ${g.done.toLocaleString()} / ${g.total.toLocaleString()}`)));
  if (g.error) body.push(h('div', { class: 'banner' }, '检查失败：' + g.error));
  if (res) {
    body.push(h('div', { class: 'row', style: 'margin:8px 0' }, pill('检查 ' + res.checked.toLocaleString() + ' 只', ''), pill('基准日 ' + (res.ref_date || '—'), ''),
      pill('落后 ' + res.stale_count, res.stale_count ? 'warn' : 'ok'), pill('缺失 ' + res.missing_count, res.missing_count ? 'bad' : 'ok'),
      res.unreadable.length ? pill('读不了 ' + res.unreadable.length, 'bad') : null, h('span', { class: 'dim' }, `完成于 ${dt(res.finished)}，用时 ${res.seconds} 秒`)));
    body.push(h('div', { class: 'dim' }, '最后数据日期分布：' + res.by_date.map(([d, n]) => `${d}: ${n}`).join(' · ')));
    if (res.stale.length) body.push(h('h2', {}, '数据落后的股票', h('small', {}, res.stale_count > res.stale.length ? `只显示最落后的 ${res.stale.length} 只` : '停牌的股票落后是正常的')),
      h('div', { class: 'scroll' }, h('table', {}, h('thead', {}, h('tr', {}, h('th', {}, '代码'), h('th', {}, '名称'), h('th', {}, '最后日期'), h('th', {}, ''))),
        h('tbody', {}, res.stale.map(s => h('tr', {}, h('td', {}, s.code), h('td', {}, s.name), h('td', {}, s.last), h('td', {}, s.suspended ? pill('当日停牌', 'ok') : pill('需要补', 'warn'))))))));
    if (res.missing.length) body.push(h('h2', {}, '没有数据文件的股票', h('small', {}, '在上市列表里但没下载到；可运行「补下载缺失的全市场历史」')),
      h('div', { class: 'scroll' }, h('table', {}, h('tbody', {}, res.missing.map(s => h('tr', {}, h('td', {}, s.code), h('td', {}, s.name)))))));
  }
  return h('div', { class: 'card', id: 'gapcard' }, h('h3', {}, '全市场数据缺口检查'),
    h('p', { class: 'desc' }, '逐只读取 A 股全市场数据的最后日期，找出落后和缺失的股票（约 1 分钟，只读不改）。'),
    btn(g.running ? '检查中…' : '开始检查', () => act('gap_start'), 'primary', g.running), body);
}
function watchCard(m, rows, pending) {
  const input = h('input', { type: 'text', placeholder: m === 'cn' ? '6 位代码，如 600519' : '代码，如 AAPL', maxLength: 12, style: 'width:190px' });
  const add = () => { const c = input.value.trim(); if (c) act('watch_add', { market: m, code: c }).then(j => { if (j) input.value = ''; }); };
  input.addEventListener('keydown', e => { if (e.key === 'Enter') add(); });
  return h('div', { class: 'card' }, h('div', { class: 'row between' }, h('h3', {}, MARKET_LABEL[m] + '自选股'), h('span', { class: 'dim' }, rows.length + ' / 50')),
    h('div', { class: 'row', style: 'margin:6px 0 12px' }, input, btn('添加', add, 'primary sm')),
    h('div', { class: 'scroll' }, h('table', {}, h('thead', {}, h('tr', {}, h('th', {}, '代码'), h('th', {}, '名称'), h('th', {}, '最后数据'), h('th', { class: 'num' }, '大小'), h('th', {}, ''))),
      h('tbody', {}, rows.length ? rows.map(s => h('tr', {}, h('td', {}, s.code), h('td', {}, s.name || '—'),
        h('td', {}, pending.includes(s.code) ? pill('下载中', 'warn') : (s.last || pill('无数据', 'bad'))), h('td', { class: 'num' }, bytes(s.size)),
        h('td', { style: 'text-align:right; white-space:nowrap' }, btn('更新', () => act('update_stock', { market: m, code: s.code }), 'sm'), ' ',
          btn('移除', () => act('watch_remove', { market: m, code: s.code }, { title: `移除 ${s.code}？`, body: '只是从自选列表里去掉，已下载的行情文件不会被删除。', ok: '移除' }), 'sm danger'))))
        : h('tr', {}, h('td', { colSpan: 5, class: 'dim' }, '自选列表是空的'))))));
}
function backupCard(list) {
  return h('div', { class: 'card' }, h('div', { class: 'row between' }, h('h3', {}, '备份导出'), btn('立即生成备份', () => act('backup_make'), 'primary sm')),
    h('p', { class: 'desc' }, '打包自选股、信号历史、策略代码历史、配置和网页代码，不含令牌和行情大文件。保存在 export/，只留最新 10 份。'),
    list.length ? h('table', {}, h('tbody', {}, list.map(b => h('tr', {}, h('td', {}, b.name), h('td', { class: 'num' }, bytes(b.size)), h('td', { class: 'dim' }, ago(b.mtime)),
      h('td', { style: 'text-align:right' }, h('a', { class: 'btn sm', href: '/api/admin/backup/download?f=' + encodeURIComponent(b.name), style: 'color:var(--text)' }, '下载'))))))
      : h('div', { class: 'dim' }, '还没有备份'));
}
function viewData(d) {
  state.gapRunning = !!d.gap.running;
  return h('div', {}, h('h1', {}, '数据'), h('p', { class: 'lead' }, '自选股、全市场数据完整度和备份。'),
    h('div', { class: 'split' }, Object.entries(d.watch).map(([m, rows]) => watchCard(m, rows, d.pending))),
    gapCard(d.gap), backupCard(d.backups));
}

// ---------- 访问与分享
function viewAccess(o, v, mem) {   // mem = { members, feed }
  const sh = o.share || {}, t = o.token;
  const vis = v.visitors || [];
  return h('div', {}, h('h1', {}, '访问与分享'), h('p', { class: 'lead' }, '谁能访问你的网站、外网分享链接的开关、令牌管理。后台本身永远不能通过分享链接访问。'),
    h('div', { class: 'card' }, h('div', { class: 'row between' }, h('h3', {}, '外网分享（Cloudflare 隧道）'), sh.running ? pill(sh.starting ? '连接中' : '开启中', 'warn') : pill('未开启', 'ok')),
      h('p', { class: 'desc' }, '开启后，拿到链接的人能查看主页面（只读，可跑回测，不能改自选、代码和刷新数据）。链接每次重连会变，且带着主页面令牌，请不要公开发布。' + (sh.note ? ' ' + sh.note : '')),
      h('div', { class: 'row' }, sh.running ? btn('停止分享', () => act('share_stop'), 'danger') : btn('开启分享', () => act('share_start', {}, { title: '把网站开到外网？', body: '会生成一个临时公网链接，任何拿到链接的人都能查看你的行情和回测结果。\n后台、代码编辑、分享管理不会对外开放。', ok: '开启分享' }), 'primary'),
        sh.raw_url ? h('span', { class: 'dim' }, '当前网址：' + sh.raw_url) : null),
      (() => { const inp = h('input', { type: 'password', placeholder: sh.password_set ? '输入新密码可修改（6–64 位）' : '设置访问密码（6–64 位）', autocomplete: 'new-password', style: 'max-width:260px' });
        return h('div', { class: 'row', style: 'margin-top:12px;flex-wrap:wrap' }, h('span', {}, '访问密码：'), sh.password_set ? pill('已设置', 'ok') : pill('未设置（拿到链接就能看）', 'warn'), inp,
          btn(sh.password_set ? '修改密码' : '设置密码', () => { if (inp.value.length < 6) { toast('密码至少 6 位'); return; } act('share_pass_set', { password: inp.value }); inp.value = ''; }, 'primary'),
          sh.password_set ? btn('取消密码', () => act('share_pass_clear', {}, { title: '取消分享密码？', body: '取消后，拿到分享链接的人不用密码就能看。', ok: '取消密码' }), 'danger') : null); })()),
    h('div', { class: 'card' }, h('div', { class: 'row between' }, h('h3', {}, '分享链接访客'), h('span', { class: 'dim' }, `共 ${v.total || 0} 次访问 · 现在在线 ${v.online || 0}`)),
      vis.length ? h('div', { class: 'scroll' }, h('table', {}, h('thead', {}, h('tr', {}, ['状态', '网络地址', '国家', '设备', '首次', '最近', '次数'].map(x => h('th', {}, x)))),
        h('tbody', {}, vis.map(x => h('tr', {}, h('td', {}, x.online ? pill('在线', 'ok') : pill('离开', '')), h('td', {}, x.ip || '—'), h('td', {}, x.country || '—'), h('td', {}, x.device),
          h('td', { class: 'dim' }, x.first), h('td', { class: 'dim' }, x.last), h('td', { class: 'num' }, x.n))))))
        : h('div', { class: 'dim', style: 'margin-top:8px' }, '还没有访客记录'),
      h('div', { class: 'row', style: 'margin-top:12px' }, btn('清空访客记录', () => act('clear_visits', {}, { title: '清空访客记录？', body: '会先把现有记录备份成 data/share_visits.*.bak，再清空。', ok: '清空' }), 'sm danger'))),
    offCard('分享链接管理', '按链接撤销、设置到期需要后端。', [offRow('只读分享链接', '为单个页面生成可撤销的链接。', false)]),
    ...memberCards(mem),
    h('div', { class: 'card' }, h('h3', {}, '令牌与登录安全'),
      h('p', { class: 'desc' }, `后台令牌和主页面令牌是分开的，页面上不显示任何令牌。后台会话 ${t.session_hours} 小时过期；10 分钟内输错 5 次令牌会暂停登录。`),
      h('table', {}, h('tbody', {},
        h('tr', {}, h('td', {}, '主页面令牌'), h('td', { class: 'dim' }, '上次更换：' + dt(t.main_rotated)),
          h('td', { style: 'text-align:right' }, btn('更换', () => act('rotate_main_token', {}, { title: '更换主页面令牌？', body: '旧令牌和所有已登录的浏览器会立刻失效，正在开启的分享会被停止。之后需运行 bash scripts/open_dashboard.sh 重新打开。', ok: '更换' }), 'sm danger'))),
        h('tr', {}, h('td', {}, '后台令牌'), h('td', { class: 'dim' }, '上次更换：' + dt(t.admin_rotated)),
          h('td', { style: 'text-align:right' }, btn('更换', () => act('rotate_admin_token', {}, { title: '更换后台令牌？', body: '当前后台会话会失效，需运行 cd ~/Documents/Projects/quant-backtest && bash scripts/open_admin.sh 重新登录。', ok: '更换' }), 'sm danger'))),
        h('tr', {}, h('td', {}, '近 10 分钟登录失败'), h('td', {}, t.failed_logins_10min ? pill(t.failed_logins_10min + ' 次', 'warn') : pill('0 次', 'ok')), h('td', {}))))));
}

// ---------- 日志
async function viewLogs() {
  const L = state.log;
  const wrap = h('div', {}, h('h1', {}, '日志'), h('p', { class: 'lead' }, '只读，显示文件末尾的内容。'));
  const sel = h('select', { onchange: e => { L.name = e.target.value; refresh(); } },
    [['daily', '每日任务 daily.log'], ['update', 'launchd 更新输出'], ['server', '网页服务输出'], ['tunnel', '分享隧道'], ['scan', '三引擎扫描错误'],
      ['task_rebacktest_cn', '后台任务：重新回测 A 股'], ['task_rebacktest_us', '后台任务：重新回测美股'], ['task_backfill', '后台任务：补下载历史'], ['audit', '后台审计原始记录']]
      .map(([k, t]) => h('option', { value: k, selected: k === L.name }, t)));
  const n = h('select', { onchange: e => { L.n = +e.target.value; refresh(); } }, [100, 200, 500, 1000].map(x => h('option', { value: x, selected: x === L.n }, '最后 ' + x + ' 行')));
  const f = h('input', { type: 'text', placeholder: '过滤关键词', value: L.filter, style: 'width:180px', oninput: e => { L.filter = e.target.value; paint(); } });
  const auto = h('label', { class: 'dim' }, h('input', { type: 'checkbox', checked: L.auto, onchange: e => { L.auto = e.target.checked; } }), ' 自动刷新');
  const meta = h('div', { class: 'dim', style: 'margin:8px 0' });
  const pre = h('pre', { class: 'log' });
  let data = null;
  const paint = () => {
    if (!data) return;
    const q = L.filter.trim().toLowerCase();
    const lines = q ? data.lines.filter(x => x.toLowerCase().includes(q)) : data.lines;
    pre.textContent = lines.join('\n') || '（空）';
    pre.scrollTop = pre.scrollHeight;
    meta.textContent = `${data.file} · ${bytes(data.size)} · 更新于 ${ago(data.mtime)} · 显示 ${lines.length} 行`;
  };
  wrap.append(h('div', { class: 'row' }, sel, n, f, auto, btn('刷新', refresh, 'sm')), meta, pre);
  try { data = await api(`/api/admin/log?name=${encodeURIComponent(L.name)}&n=${L.n}`); } catch (e) { pre.textContent = e.message; }
  paint();
  return wrap;
}

// ---------- 审计
function viewAudit(items) {
  return h('div', {}, h('h1', {}, '审计'), h('p', { class: 'lead' }, '后台上的每一个操作都会记录在这里（data/admin_audit.jsonl），最近 100 条。每条带操作人（所有者邮箱或本机）、来源 IP 和国家；改开关的记录写在“保存设置”那一步，旧记录这三栏为空。'),
    h('div', { class: 'card' }, h('div', { class: 'scroll', style: 'max-height:640px' }, h('table', {}, h('thead', {}, h('tr', {}, ['时间', '操作', '参数', '操作人', 'IP', '国家', '结果'].map(x => h('th', {}, x)))),
      h('tbody', {}, items.length ? items.map(i => h('tr', {}, h('td', { class: 'dim', style: 'white-space:nowrap' }, i.t), h('td', {}, ACTION_LABEL[i.action] || i.action),
        h('td', { class: 'dim' }, i.detail ? Object.entries(i.detail).map(([k, v]) => k + '=' + v).join(' ') : ''),
        h('td', { class: 'dim' }, i.actor || '—'), h('td', { class: 'dim' }, i.ip || '—'), h('td', { class: 'dim' }, i.country || '—'),
        h('td', {}, i.ok ? pill('成功', 'ok') : pill('失败', 'bad'), i.error ? h('span', { class: 'dim' }, ' ' + i.error) : null)))
        : h('tr', {}, h('td', { colSpan: 7, class: 'dim' }, '还没有记录')))))));
}


// ---------- 会员：谁注册了、做了什么
const MACT = { register: '注册', login: '登录', login_fail: '登录失败', logout: '退出登录', order: '下单', signal: '查看策略信号', reset: '重置账户', password: '修改密码', password_fail: '改密码失败' };
const mnum = (n, d = 2) => n == null ? '—' : Number(n).toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d });
const mcol = n => n > 0 ? 'color:#d70015' : n < 0 ? 'color:#1a8f3c' : '';
const actTable = (items, showName) => h('div', { class: 'scroll', style: 'max-height:560px' }, h('table', {}, h('thead', {}, h('tr', {}, ['时间', showName ? '会员' : null, '动作', '内容', '网络地址', '设备'].filter(Boolean).map(x => h('th', {}, x)))),
  h('tbody', {}, items.length ? items.map(a => h('tr', {}, h('td', { class: 'dim', style: 'white-space:nowrap' }, a.ts),
    showName ? h('td', {}, a.uid ? h('a', { href: '#/members/' + a.uid }, a.name) : (a.name || '—')) : null,
    h('td', {}, MACT[a.action] || a.action), h('td', {}, a.detail || ''), h('td', { class: 'dim' }, (a.ip || '—') + (a.country ? ' ' + a.country : '')), h('td', { class: 'dim' }, deviceOf(a.ua))))
    : h('tr', {}, h('td', { colSpan: 6, class: 'dim' }, '还没有记录')))));
function deviceOf(ua) { ua = ua || ''; const os = /iPhone|iPad/.test(ua) ? 'iPhone' : /Android/.test(ua) ? 'Android' : /Mac OS/.test(ua) ? 'Mac' : /Windows/.test(ua) ? 'Windows' : '其他'; const b = /MicroMessenger/.test(ua) ? '微信' : /Edg/.test(ua) ? 'Edge' : /Chrome/.test(ua) ? 'Chrome' : /Safari/.test(ua) ? 'Safari' : ''; return os + (b ? ' · ' + b : ''); }
function memberCards(d) {
  const mem = d.members || [];
  return [
    h('div', { class: 'card' }, h('div', { class: 'row between' }, h('h3', {}, '会员（点名字看详情）'), h('span', { class: 'dim' }, `共 ${mem.length} 人`)),
      mem.length ? h('div', { class: 'scroll' }, h('table', {}, h('thead', {}, h('tr', {}, ['用户名', '注册时间', '最近活动', '登录次数', '成交笔数', '最近地址', '状态', ''].map(x => h('th', {}, x)))),
        h('tbody', {}, mem.map(x => h('tr', {}, h('td', {}, h('a', { href: '#/members/' + x.id }, x.name)), h('td', { class: 'dim' }, x.created), h('td', { class: 'dim' }, x.last_active || '—'), h('td', { class: 'num' }, x.logins), h('td', { class: 'num' }, x.trades),
          h('td', { class: 'dim' }, (x.last_ip || '—') + (x.last_country ? ' ' + x.last_country : '')), h('td', {}, x.disabled ? pill('已停用', 'warn') : pill('正常', 'ok')),
          h('td', {}, h('div', { class: 'row' }, btn(x.disabled ? '启用' : '停用', () => act(x.disabled ? 'member_enable' : 'member_disable', { uid: x.id }), 'sm'),
            btn('删除', () => act('member_delete', { uid: x.id }, { title: '删除会员 ' + x.name + '？', body: '会连同他的模拟账户、持仓和成交一起删除，不能恢复。', ok: '删除' }), 'sm danger')))))))) : h('div', { class: 'dim', style: 'margin-top:8px' }, '还没有会员')),
    h('div', { class: 'card' }, h('h3', {}, '最近的所有操作（最新 150 条）'), actTable(d.feed || [], true))];
}
function viewMembers(d) {
  return h('div', {}, h('h1', {}, '会员'), h('p', { class: 'lead' }, '点名字查看这个人的账户、持仓、全部成交和每一次操作。'),
    offCard('会员功能总开关与申请', '升降 Pro、申请审批要等后端。', [offRow('会员功能总开关', '关闭后只剩所有者视角。', true), offRow('Pro 申请审批', '审批会员的 Pro 申请。', false)]), ...memberCards(d));
}
function viewMember(d) {
  const u = d.user, mk = { cn: '¥', us: '$' }, mn = { cn: 'A 股', us: '美股' };
  const pf = d.portfolio || {};
  return h('div', {}, h('p', {}, h('a', { href: '#/access' }, '‹ 返回')), h('h1', {}, u.name), h('p', { class: 'lead' }, `注册 ${u.created} · 最近登录 ${u.last_login || '—'} · 登录 ${u.logins} 次 · ${u.disabled ? '已停用' : '正常'}`),
    ...['cn', 'us'].map(m => { const p = pf[m]; if (!p) return null;
      return h('div', { class: 'card' }, h('div', { class: 'row between' }, h('h3', {}, mn[m] + ' 账户'), h('span', { class: 'dim' }, `初始 ${mk[m]}${mnum(p.initial, 0)}`)),
        h('p', {}, `总资产 ${mk[m]}${mnum(p.total)} · 累计收益 `, h('b', { style: mcol(p.ret_pct) }, (p.ret_pct > 0 ? '+' : '') + mnum(p.ret_pct) + '%'), ` · 现金 ${mk[m]}${mnum(p.cash)} · 持仓市值 ${mk[m]}${mnum(p.market_value)} · 已实现盈亏 `, h('b', { style: mcol(p.realized) }, mnum(p.realized))),
        p.positions.length ? h('div', { class: 'scroll' }, h('table', {}, h('thead', {}, h('tr', {}, ['代码', '名称', '数量', '成本', '现价', '市值', '盈亏', '盈亏%', '首次买入'].map(x => h('th', {}, x)))),
          h('tbody', {}, p.positions.map(r => h('tr', {}, h('td', {}, r.code), h('td', {}, r.name), h('td', { class: 'num' }, mnum(r.qty, 0)), h('td', { class: 'num' }, mnum(r.cost, 3)), h('td', { class: 'num' }, mnum(r.last)), h('td', { class: 'num' }, mnum(r.value)),
            h('td', { class: 'num', style: mcol(r.pnl) }, mnum(r.pnl)), h('td', { class: 'num', style: mcol(r.pnl_pct) }, mnum(r.pnl_pct) + '%'), h('td', { class: 'dim' }, (r.first || '').slice(0, 16))))))) : h('div', { class: 'dim' }, '没有持仓'),
        h('h3', { style: 'margin-top:14px' }, '全部成交'),
        p.trades.length ? h('div', { class: 'scroll', style: 'max-height:360px' }, h('table', {}, h('thead', {}, h('tr', {}, ['时间', '方向', '代码', '名称', '数量', '价格', '手续费', '盈亏', '策略 / 当时信号'].map(x => h('th', {}, x)))),
          h('tbody', {}, p.trades.map(t => h('tr', {}, h('td', { class: 'dim', style: 'white-space:nowrap' }, t.ts), h('td', {}, t.side === 'buy' ? '买入' : '卖出'), h('td', {}, t.code), h('td', {}, t.name), h('td', { class: 'num' }, mnum(t.qty, 0)), h('td', { class: 'num' }, mnum(t.price)), h('td', { class: 'num' }, mnum(t.fee)),
            h('td', { class: 'num', style: mcol(t.pnl) }, t.pnl == null ? '—' : mnum(t.pnl)), h('td', { class: 'dim' }, (t.strategy || '—') + (t.signal ? ' / ' + t.signal : ''))))))) : h('div', { class: 'dim' }, '没有成交')); }),
    h('div', { class: 'card' }, h('h3', {}, '操作记录（最近 400 条）'), actTable(d.activity || [], false)));
}


// ---------- 可用的开关：先进“草稿”，底部出现“有 N 项修改还没保存”，点保存时集中确认一次再 POST
const SETTING_LABEL = { registration_open: '开放注册', maintenance_on: '维护模式', maintenance_until: '预计恢复时间' };
// 分区开关的草稿键写成 'sections:<页面id>'，保存时再合成 sections: {id: {on}}
const savedValue = (key) => key.startsWith('sections:') ? (((state.site || {}).sections || {})[key.slice(9)] || { on: true }).on : (state.site || {})[key];
const settingValue = (key) => key in state.draft ? state.draft[key] : savedValue(key);
function toggleDraft(key) {
  const saved = savedValue(key), next = !settingValue(key);
  if (next === saved) delete state.draft[key]; else state.draft[key] = next;
  repaintSite();
}
const swLive = (key, label) => {
  const on = settingValue(key) === true;
  return h('button', { type: 'button', class: 'sw live' + (on ? ' on' : '') + (key in state.draft ? ' changed' : ''), role: 'switch', 'aria-checked': String(on), 'aria-label': label, onclick: () => toggleDraft(key) },
    h('span', { class: 'knob' }), on ? '开' : '关');
};
// 预计恢复时间（文本，≤ 40 字）：失焦后进草稿；和已保存的一样就撤销草稿
const untilInput = () => h('input', { type: 'text', maxlength: '40', placeholder: '如 2026-11-01 18:00（北京时间）', 'aria-label': '预计恢复时间',
  value: String(settingValue('maintenance_until') || ''),
  onchange: (e) => { const v = e.target.value.trim(), saved = (state.site || {}).maintenance_until || ''; if (v === saved) delete state.draft.maintenance_until; else state.draft.maintenance_until = v; repaintSite(); } });
async function saveDraft() {
  const keys = Object.keys(state.draft);
  if (!keys.length || state.saving) return;
  // 有影响的开关（打开注册）在保存时集中确认一次
  if (state.draft.registration_open === true && !await confirmDialog({ title: '开放注册？', body: '保存后，任何能打开本站的人都可以注册新账号（同一网络地址每小时最多 5 次，会员总数上限 200）。已有会员不受影响。', ok: '开放注册', danger: true })) return;
  // 关闭分区页面也集中确认一次（访客会看到“暂未开放”页）
  const closing = Object.entries(state.draft).filter(([k, v]) => k.startsWith('sections:') && v === false).map(([k]) => SECTION_LABEL[k.slice(9)]);
  if (closing.length && !await confirmDialog({ title: '关闭 ' + closing.length + ' 个页面？', body: '保存后，除你自己以外的访问者打开「' + closing.join('、') + '」会看到“该页面暂未开放”。你自己（本机或主人通道）不受影响。', ok: '关闭页面', danger: true })) return;
  // 开启维护模式也在保存时集中确认（访客会看到 503，所有者不受影响）
  if (state.draft.maintenance_on === true && !await confirmDialog({ title: '开启维护模式？', body: '保存后，除你自己（本机或主人通道）以外的访问都会看到“网站维护中”（503）。搜索引擎的 robots.txt 仍可访问。', ok: '开启维护模式', danger: true })) return;
  // 退出登录设备：集中确认一次（点“退出此设备”只是进草稿）
  const revokes = Object.keys(state.draft).filter(k => k.startsWith('revoke:'));
  if (revokes.length && !await confirmDialog({ title: '退出 ' + revokes.length + ' 台登录设备？', body: '保存后，这些设备上的后台登录立刻失效，需要重新登录。当前正在用的这台不会被退出。', ok: '退出 ' + revokes.length + ' 台设备', danger: true })) return;
  state.saving = true;
  try {
    const body = {}, secs = {};
    for (const [k, v] of Object.entries(state.draft)) { if (k.startsWith('revoke:')) continue; if (k.startsWith('sections:')) secs[k.slice(9)] = { on: v }; else body[k] = v; }
    if (Object.keys(secs).length) body.sections = secs;
    let msg = '';
    if (Object.keys(body).length) {
      const j = await api('/api/admin/settings', { body });
      state.site = j.settings;
      msg = j.changed && j.changed.length ? '已保存 ' + j.changed.length + ' 项修改' : '没有实际变化';
    }
    let out = 0;
    for (const k of revokes) {
      try { await api('/api/admin/owner/sessions/' + encodeURIComponent(k.slice(7)) + '/revoke', { body: {} }); out++; delete state.draft[k]; }
      catch (e) { toast(e.message, true); }      // 失败的留在草稿里，其余继续
    }
    if (revokes.length) { msg = (msg ? msg + '；' : '') + '已退出 ' + out + ' 台设备'; try { state.sessions = await api('/api/admin/owner/sessions'); } catch (e) { /* 下次刷新再取 */ } }
    for (const k of Object.keys(state.draft)) if (!k.startsWith('revoke:')) delete state.draft[k];
    toast(msg);
  } catch (e) { toast(e.message, true); }
  state.saving = false;
  repaintSite();
}
function saveBar() {
  const n = Object.keys(state.draft).length;
  return n ? h('div', { class: 'savebar', id: 'savebar' }, h('span', {}, '有 ' + n + ' 项修改还没保存'),
    h('span', { class: 'row' }, btn('放弃修改', () => { state.draft = {}; repaintSite(); }, 'sm'), btn('保存', saveDraft, 'sm primary', state.saving))) : null;
}
function repaintSite() {
  if (state.tab === 'sections' && state.site) { const sx = window.scrollX, sy = window.scrollY; $('#view').replaceChildren(viewSections()); applySearch(); window.scrollTo(sx, sy); return; }
  if (state.tab === 'system' && state.sysOverview) { const sx = window.scrollX, sy = window.scrollY; $('#view').replaceChildren(viewSystem(state.sysOverview)); applySearch(); window.scrollTo(sx, sy); return; }
  if (state.tab !== 'site' || !state.siteOverview) return;
  const sx = window.scrollX, sy = window.scrollY;
  $('#view').replaceChildren(viewSite(state.siteOverview));
  applySearch();
  window.scrollTo(sx, sy);
}
window.addEventListener('beforeunload', e => { if (Object.keys(state.draft).length) { e.preventDefault(); e.returnValue = ''; } });

// ---------- 站点开关 / 分区开关 / 系统（设计稿布局；后端还没有的开关灰显）
function viewSite(o) {
  const sh = o.share || {};
  state.siteOverview = o;
  return h('div', {}, h('h1', {}, '站点开关'), h('p', { class: 'lead' }, '全站级别的开关。灰色的需要后端先加接口，现在不能点，也不会假装生效。'),
    offCard('站点状态', '', [
      offRow('站点在线', '关闭后访客看到“暂未开放”页。', true),
      swRow('维护模式', '开启后访客看到 503 维护页（带 Retry-After），所有者不受影响。', swLive('maintenance_on', '维护模式'), '本机已有'),
      swRow('预计恢复时间', '可不填（不填时 Retry-After 为 3600 秒）。格式 YYYY-MM-DD HH:MM，北京时间，最多 40 字。', untilInput(), '本机已有'),
      swRow('开放注册', '关闭时新用户不能注册，已有会员不受影响。默认关闭。', swLive('registration_open', '开放注册'), '本机已有'),
      offRow('允许游客浏览', '未登录的人是否能看公开页面。', true),
      offRow('展示页', '对外展示的示例页面。', false),
      offRow('站点公告', '同一时间只显示一条，可设到期日。', false),
      offRow('新会员默认档位', '注册后是免费还是 Pro。', false),
      swRow('私人模式', '仅所有者可见全部功能，由配置文件决定，这里只读。', h('span', { class: 'pill' }, '只读'), '本机已有')]),
    saveBar(),
    h('div', { class: 'card' }, h('h3', {}, '外网分享（已有功能）'), h('p', { class: 'desc' }, '这个开关是真的：和「分享与访问」页里是同一个。'),
      swRow('分享隧道（Cloudflare）', '开启后拿到链接的人可以只读查看主页面。', sh.running
        ? btn('停止分享', () => act('share_stop'), 'sm danger')
        : btn('开启分享', () => act('share_start', {}, { title: '把网站开到外网？', body: '会生成一个临时公网链接，任何拿到链接的人都能查看你的行情和回测结果。\\n后台、代码编辑、分享管理不会对外开放。', ok: '开启分享' }), 'sm primary'), '本机已有')));
}
const SECTION_LABEL = { today: '今日', signals: '信号', market: '行情', watch: '自选', backtest: '回测', holdings: '持仓', monitor: '监控', research: '研究', settings: '设置' };
function viewSections() {
  const groups = [['行情', ['today', 'market', 'watch']], ['研究与交易', ['backtest', 'research', 'signals']], ['账户', ['holdings', 'monitor', 'settings']]];
  return h('div', {}, h('h1', {}, '分区开关'), h('p', { class: 'lead' }, '关掉的页面，访客打开时看到“该页面暂未开放”（不是 404）；你自己（本机或主人通道）不受影响。修改先进草稿，点保存时集中确认。'),
    ...groups.map(([g, ids]) => offCard(g, '', ids.map(id => swRow(SECTION_LABEL[id], '整页开关（新界面 /ui/w/' + id + '.html）。', swLive('sections:' + id, SECTION_LABEL[id]), '本机已有')))),
    saveBar());
}
// 我的登录设备：退出单台先进草稿（和开关一样底部出现“有 N 项修改还没保存”），点保存时集中确认；当前这台不能退出
function sessionsCard() {
  const rows = (state.sessions || []).map(x => {
    const key = 'revoke:' + x.id, queued = key in state.draft;
    const ctl = x.current ? pill('当前设备', 'ok')
      : queued ? btn('撤销退出', () => { delete state.draft[key]; repaintSite(); }, 'sm')
        : btn('退出此设备', () => { state.draft[key] = true; repaintSite(); }, 'sm danger');
    return swRow(x.device + (queued ? '（待退出）' : ''), `${x.ip || '—'}${x.country ? ' · ' + x.country : ''} · 最近活动 ${dt(x.last_active)}`, ctl, '本机已有');
  });
  return h('div', { class: 'card' }, h('h3', {}, '我的登录设备'),
    h('p', { class: 'desc' }, '用管理令牌登录过的浏览器（8 小时有效）。可以退出别的设备，不能退出当前这台；通过 Cloudflare 主人通道进来的访问不在此列。'),
    ...(rows.length ? rows : [h('p', { class: 'dim' }, '没有登录记录')]));
}
function viewSystem(o) {
  state.sysOverview = o;
  const t = o.token || {};
  return h('div', {}, h('h1', {}, '系统'), h('p', { class: 'lead' }, '重启、换令牌、备份下载。这些是本机已有功能，会弹出确认框。'),
    h('div', { class: 'card' }, h('h3', {}, '系统操作'),
      swRow('重启网页服务', '服务会中断几秒，launchd 自动拉起。' + (o.server.managed ? '' : '（当前不是 launchd 启动的，已禁用。）'),
        btn('重启', () => act('restart_server', {}, { title: '重启网页服务？', body: '服务会中断几秒，launchd 会自动拉起。正在跑的网页回测任务会中断，后台子进程（更新、扫描）不受影响。', ok: '重启' }), 'sm danger', !o.server.managed), '本机已有'),
      swRow('更换主页面令牌', '上次更换：' + dt(t.main_rotated),
        btn('更换', () => act('rotate_main_token', {}, { title: '更换主页面令牌？', body: '旧令牌和所有已登录的浏览器会立刻失效，正在开启的分享会被停止。之后需运行 bash scripts/open_dashboard.sh 重新打开。', ok: '更换' }), 'sm danger'), '本机已有'),
      swRow('更换后台令牌', '上次更换：' + dt(t.admin_rotated),
        btn('更换', () => act('rotate_admin_token', {}, { title: '更换后台令牌？', body: '当前后台会话会失效，需运行 cd ~/Documents/Projects/quant-backtest && bash scripts/open_admin.sh 重新登录。', ok: '更换' }), 'sm danger'), '本机已有'),
      swRow('下载备份', '备份文件在「数据」页生成并下载。', h('a', { class: 'btn sm', href: '#/data', style: 'color:var(--text)' }, '去数据页'), '本机已有'),
      ),
    sessionsCard(),
    saveBar(),
    h('div', { class: 'card' }, h('h3', {}, '运行状态'), h('p', { class: 'desc' }, `PID ${o.server.pid} · 已运行 ${dur(o.server.uptime)} · 磁盘可用 ${bytes(o.disk.free)}。更多见「概览」。`)));
}

// ---------- 顶部搜索：过滤侧栏分区、当前页的开关行和表格行
function applySearch() {
  const q = $('#search').value.trim().toLowerCase();
  document.querySelectorAll('#tabs a').forEach(a => a.classList.toggle('nomatch', !!q && !a.textContent.toLowerCase().includes(q)));
  document.querySelectorAll('#view .swrow').forEach(r => r.classList.toggle('hide', !!q && !(r.dataset.q || r.textContent.toLowerCase()).includes(q)));
  document.querySelectorAll('#view tbody tr').forEach(r => { r.style.display = q && !r.textContent.toLowerCase().includes(q) ? 'none' : ''; });
}

// ---------- 路由与刷新
let seq = 0;
async function refresh() {
  const tab = state.tab, my = ++seq;
  let node;
  try {
    if (tab === 'overview') node = viewOverview(await api('/api/admin/overview'));
    else if (tab === 'tasks') node = viewTasks(await api('/api/admin/overview'));
    else if (tab === 'site') { const [o, s] = await Promise.all([api('/api/admin/overview'), api('/api/admin/settings')]); state.site = s; node = viewSite(o); }
    else if (tab === 'sections') { state.site = await api('/api/admin/settings'); node = viewSections(); }
    else if (tab === 'system') { const [o, ss] = await Promise.all([api('/api/admin/overview'), api('/api/admin/owner/sessions').catch(() => state.sessions)]); state.sessions = ss; node = viewSystem(o); }
    else if (tab === 'data') node = viewData(await api('/api/admin/data'));
    else if (tab === 'access') { const [o, v, mm] = await Promise.all([api('/api/admin/overview'), api('/api/admin/visits'), api('/api/admin/members')]); node = viewAccess(o, v, mm); }
    else if (tab === 'members') node = state.member ? viewMember(await api('/api/admin/member?id=' + state.member)) : viewMembers(await api('/api/admin/members'));
    else if (tab === 'logs') node = await viewLogs();
    else if (tab === 'audit') node = viewAudit((await api('/api/admin/audit')).items);
  } catch (e) { if (my === seq && tab === state.tab) $('#view').replaceChildren(h('div', { class: 'banner' }, e.message)); return; }
  if (my !== seq || tab !== state.tab || !node) return;      // 期间切了页面：丢弃过期结果
  const sx = window.scrollX, sy = window.scrollY;
  $('#view').replaceChildren(node);
  applySearch();
  window.scrollTo(sx, sy);
}
function route() {
  const mt = location.hash.match(/^#\/(\w+)(?:\/(\d+))?/) || [], t = mt[1];
  state.member = mt[2] ? +mt[2] : 0;
  state.tab = t === 'members' ? 'members' : TABS.some(x => x[0] === t) ? t : 'overview';
  const label = Object.fromEntries(TABS);
  $('#tabs').replaceChildren(...NAV_GROUPS.flatMap(([g, ks]) => [g ? h('div', { class: 'grp' }, g) : null,
    ...ks.map(k => h('a', { class: k === state.tab ? 'on' : '', href: '#/' + k }, label[k]))]));
  applySearch();
  refresh();
}
function schedule() {
  const wait = state.tab === 'sections' ? 0 : state.tab === 'data' ? (state.gapRunning ? 2000 : 0) : state.tab === 'logs' ? (state.log.auto ? 4000 : 0) : state.tab === 'audit' ? 0 : 5000;
  setTimeout(async () => {
    if (wait && !document.hidden && !busy() && !document.querySelector('.modal-bg')) await refresh();
    schedule();
  }, wait || 2000);
}
function clock() { $('#clock').textContent = new Date().toLocaleTimeString('zh-CN', { hour12: false }); }

window.addEventListener('hashchange', route);
$('#search').addEventListener('input', applySearch);
setInterval(clock, 1000); clock();
route(); schedule();
})();
