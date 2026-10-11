/* 后台页面：纯 JS，无外部依赖。
   所有文字都用 textContent 写入（日志、股票名里可能有任意字符），不使用 innerHTML，所以不会被注入脚本。 */
(() => {
'use strict';
const TABS = [['overview', '概览'], ['tasks', '任务'], ['data', '数据'], ['access', '访问与分享'], ['logs', '日志'], ['audit', '审计']];
const ACTION_LABEL = {
  login: '登录后台', refresh: '立即更新（联网）', kick_update: '触发每日检查', task_start: '启动任务', task_stop: '中止任务',
  scan_start: '启动全市场扫描', scan_stop: '中止全市场扫描', watch_add: '添加自选股', watch_remove: '移除自选股',
  update_stock: '更新个股', gap_start: '数据缺口检查', backup_make: '生成备份', backup_download: '下载备份',
  restart_server: '重启网页服务', share_start: '开启分享', share_stop: '停止分享', share_pass_set: '设置分享密码', share_pass_clear: '取消分享密码', clear_visits: '清空访客记录', member_disable: '停用会员', member_enable: '启用会员', member_delete: '删除会员',
  rotate_main_token: '更换主令牌', rotate_admin_token: '更换管理令牌', blocked_remote: '拦截了外网来的访问'
};
const MARKET_LABEL = { cn: 'A 股', us: '美股' };
const state = { tab: 'overview', log: { name: 'daily', n: 200, filter: '', auto: true }, gapRunning: false };
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
const busy = () => { const a = document.activeElement || {}; if (a.tagName === 'INPUT' && !['checkbox', 'button', 'radio'].includes(a.type)) return true; return [...document.querySelectorAll('#view input')].some(x => ['text', 'password', 'search', 'number'].includes(x.type) && x.value); };   // 正在输入（或输入框里已有内容）时不自动刷新：刷新会重建页面、丢掉输入

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
  if (r.status === 401) { banner('登录已过期（会话 8 小时有效）。请在终端运行：bash scripts/open_admin.sh'); throw new Error('未登录或会话已过期'); }
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
    ...memberCards(mem),
    h('div', { class: 'card' }, h('h3', {}, '令牌与登录安全'),
      h('p', { class: 'desc' }, `后台令牌和主页面令牌是分开的，页面上不显示任何令牌。后台会话 ${t.session_hours} 小时过期；10 分钟内输错 5 次令牌会暂停登录。`),
      h('table', {}, h('tbody', {},
        h('tr', {}, h('td', {}, '主页面令牌'), h('td', { class: 'dim' }, '上次更换：' + dt(t.main_rotated)),
          h('td', { style: 'text-align:right' }, btn('更换', () => act('rotate_main_token', {}, { title: '更换主页面令牌？', body: '旧令牌和所有已登录的浏览器会立刻失效，正在开启的分享会被停止。之后需运行 bash scripts/open_dashboard.sh 重新打开。', ok: '更换' }), 'sm danger'))),
        h('tr', {}, h('td', {}, '后台令牌'), h('td', { class: 'dim' }, '上次更换：' + dt(t.admin_rotated)),
          h('td', { style: 'text-align:right' }, btn('更换', () => act('rotate_admin_token', {}, { title: '更换后台令牌？', body: '当前后台会话会失效，需运行 bash scripts/open_admin.sh 重新登录。', ok: '更换' }), 'sm danger'))),
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
  return h('div', {}, h('h1', {}, '审计'), h('p', { class: 'lead' }, '后台上的每一个操作都会记录在这里（data/admin_audit.jsonl），最近 100 条。'),
    h('div', { class: 'card' }, h('div', { class: 'scroll', style: 'max-height:640px' }, h('table', {}, h('thead', {}, h('tr', {}, ['时间', '操作', '参数', '结果'].map(x => h('th', {}, x)))),
      h('tbody', {}, items.length ? items.map(i => h('tr', {}, h('td', { class: 'dim', style: 'white-space:nowrap' }, i.t), h('td', {}, ACTION_LABEL[i.action] || i.action),
        h('td', { class: 'dim' }, i.detail ? Object.entries(i.detail).map(([k, v]) => k + '=' + v).join(' ') : ''),
        h('td', {}, i.ok ? pill('成功', 'ok') : pill('失败', 'bad'), i.error ? h('span', { class: 'dim' }, ' ' + i.error) : null)))
        : h('tr', {}, h('td', { colSpan: 4, class: 'dim' }, '还没有记录')))))));
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
  return h('div', {}, h('h1', {}, '会员'), h('p', { class: 'lead' }, '点名字查看这个人的账户、持仓、全部成交和每一次操作。'), ...memberCards(d));
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

// ---------- 路由与刷新
let seq = 0;
async function refresh() {
  const tab = state.tab, my = ++seq;
  let node;
  try {
    if (tab === 'overview') node = viewOverview(await api('/api/admin/overview'));
    else if (tab === 'tasks') node = viewTasks(await api('/api/admin/overview'));
    else if (tab === 'data') node = viewData(await api('/api/admin/data'));
    else if (tab === 'access') { const [o, v, mm] = await Promise.all([api('/api/admin/overview'), api('/api/admin/visits'), api('/api/admin/members')]); node = viewAccess(o, v, mm); }
    else if (tab === 'members') node = state.member ? viewMember(await api('/api/admin/member?id=' + state.member)) : viewMembers(await api('/api/admin/members'));
    else if (tab === 'logs') node = await viewLogs();
    else if (tab === 'audit') node = viewAudit((await api('/api/admin/audit')).items);
  } catch (e) { if (my === seq && tab === state.tab) $('#view').replaceChildren(h('div', { class: 'banner' }, e.message)); return; }
  if (my !== seq || tab !== state.tab || !node) return;      // 期间切了页面：丢弃过期结果
  const sx = window.scrollX, sy = window.scrollY;
  $('#view').replaceChildren(node);
  window.scrollTo(sx, sy);
}
function route() {
  const mt = location.hash.match(/^#\/(\w+)(?:\/(\d+))?/) || [], t = mt[1];
  state.member = mt[2] ? +mt[2] : 0;
  state.tab = t === 'members' ? 'members' : TABS.some(x => x[0] === t) ? t : 'overview';
  $('#tabs').replaceChildren(...TABS.map(([k, label]) => h('a', { class: (k === state.tab || (state.tab === 'members' && k === 'access')) ? 'on' : '', href: '#/' + k }, label)));
  refresh();
}
function schedule() {
  const wait = state.tab === 'data' ? (state.gapRunning ? 2000 : 0) : state.tab === 'logs' ? (state.log.auto ? 4000 : 0) : state.tab === 'audit' ? 0 : 5000;
  setTimeout(async () => {
    if (wait && !document.hidden && !busy() && !document.querySelector('.modal-bg')) await refresh();
    schedule();
  }, wait || 2000);
}
function clock() { $('#clock').textContent = new Date().toLocaleTimeString('zh-CN', { hour12: false }); }

window.addEventListener('hashchange', route);
setInterval(clock, 1000); clock();
route(); schedule();
})();
