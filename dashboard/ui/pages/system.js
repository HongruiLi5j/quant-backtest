/* ==========================================================
 * 文件: ui/pages/system.js —— 系统页（N21，只读运行状态，样式 A：状态灯 + 详情卡）
 * 接口（全部已有，本页不新增接口、不改任何数据）:
 *   GET /api/admin/overview            → server / launchd / daily_running / markets / disk / tasks / gap / token / refresh / share
 *   GET /api/admin/log?name=&n=        → 日志最后几行
 *   GET /api/market_monitor/status     → 盯盘状态（个人模式关时 404，这一格显示「未开启」）
 * 登录：后台接口要后台登录；401 / 403 / 404 → 显示「进入后台」链接（/admin?enter=1 本机点一下即登录，回到本页）。
 * 每 30 秒自动刷新（只换内容，不闪骨架屏）；离开页面或重绘后旧定时器自动停。文字一律 textContent。
 * ========================================================== */

import { el, svg } from '../shared/dom.js';
import { t, lang } from '../shared/i18n.js';
import { getJSON, isMock } from '../shared/api.js';
import { versioned } from '../shared/asset-v.js';

const REFRESH_MS = 30000;
const LOGS = ['daily', 'update', 'server'];
const ENTER = '/admin?enter=1&to=%2Fui%2Fw%2Fsystem.html';
const needLogin = (e) => e && [401, 403, 404].includes(e.status);
let timer = null;
let logName = 'daily';

/* ---------- 小工具 ---------- */
const GB = 1024 ** 3;
function bytes(n) {
  if (n === null || n === undefined || !Number.isFinite(n)) return '—';
  if (n >= GB) return `${(n / GB).toFixed(1)} GB`;
  if (n >= 1024 ** 2) return `${(n / 1024 ** 2).toFixed(0)} MB`;
  return `${Math.max(0, Math.round(n / 1024))} KB`;
}
/* D46：页面上所有时间统一用北京时间并标出来。原来卡片按浏览器所在时区（所有者在美国时是太平洋时间）、
 * 日志行是 UTC、生成时间是 +08:00 原样截断，同一页三种时区。词条只有两三个，就地写，不动共享字典。 */
const TZ = 'Asia/Shanghai';
const tzTag = () => (lang() === 'en' ? ' (Beijing)' : '（北京时间）');
function bj(d, withSec = false) {
  return d.toLocaleString(lang() === 'en' ? 'en-US' : 'zh-CN', { timeZone: TZ, month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', ...(withSec ? { second: '2-digit' } : {}), hour12: false });
}
function when(sec) {
  if (!sec) return '—';
  return bj(new Date(sec * 1000)) + tzTag();
}
/** ISO 时间字符串（带时区）→ 北京时间；解析不了就原样截断 */
function whenIso(s) {
  if (!s) return '—';
  const d = new Date(s);
  return Number.isNaN(d.getTime()) ? String(s).replace('T', ' ').slice(0, 16) : bj(d) + tzTag();
}
/** 日志行开头的 ISO 时间（daily.log 写的是 UTC）换成北京时间；其他行不动 */
const LOG_TS = /^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d))/;
function logLine(line) {
  const m = LOG_TS.exec(line);
  if (!m) return line;
  const d = new Date(m[1]);
  return Number.isNaN(d.getTime()) ? line : bj(d, true) + line.slice(m[1].length);
}
function uptime(s) {
  if (!Number.isFinite(s)) return '—';
  return t('sys.uptime', { d: Math.floor(s / 86400), h: Math.floor((s % 86400) / 3600) });
}
const dot = (lvl) => el('i', { class: `sys-dot sys-dot--${lvl}`, 'aria-hidden': 'true' });

/** 一盏状态灯：lvl = ok | warn | bad | idle；文字同时写出状态，颜色不是唯一信息 */
function light(label, lvl, value, foot) {
  return el('div', { class: `sys-light sys-light--${lvl}` },
    el('span', { class: 'sys-light__k' }, dot(lvl), label, el('span', { class: 'sr-only' }, ` · ${t(`sys.lvl.${lvl}`)}`)),
    el('span', { class: 'sys-light__v' }, value),
    foot ? el('span', { class: 'sys-light__f' }, foot) : null);
}
const kv = (k, v, cls) => el('div', { class: 'sys-kv' }, el('span', {}, k), el('span', { class: `num${cls ? ` ${cls}` : ''}` }, v));
function card(titleKey, ...body) {
  return el('section', { class: 'card sys-card' }, el('h2', { class: 'card__title' }, t(titleKey)), ...body);
}

/* ---------- 判断红黄绿 ---------- */
function ageLevel(age) {
  if (age === null || age === undefined) return 'bad';
  if (age <= 3) return 'ok';          // 周末、节假日会有 2–3 天空档
  return age <= 7 ? 'warn' : 'bad';
}
function ageText(m) {
  if (!m?.data_date) return t('sys.none');
  return m.age_days > 0 ? t('sys.age', { d: m.age_days }) : t('sys.fresh');
}
const job = (ld, label) => (ld || []).find((x) => x.label === label) || null;
function updateLevel(o) {
  if (o.daily_running || o.refresh?.running) return ['ok', t('sys.upd.running')];
  const j = job(o.launchd, 'com.quantdaily.update');
  if (!j || j.available === false) return ['idle', t('sys.upd.unknown')];
  if (!j.loaded) return ['bad', t('sys.upd.unloaded')];
  if (j.last_exit && j.last_exit !== 0) return ['warn', t('sys.upd.exit', { c: j.last_exit })];
  return ['ok', t('sys.upd.ok')];
}
function diskLevel(free) {
  if (!Number.isFinite(free)) return 'idle';
  return free > 20 * GB ? 'ok' : free > 5 * GB ? 'warn' : 'bad';
}
function monitorLight(m) {
  if (!m) return light(t('sys.l.monitor'), 'idle', t('sys.mon.off'));
  const st = m.status?.state || m.state;
  const run = st === 'running';
  return light(t('sys.l.monitor'), run ? 'ok' : 'idle', run ? t('sys.mon.running') : t('sys.mon.stopped'), st && !run ? String(st) : null);
}

/* ---------- 各块 ---------- */
function lights(o, mon) {
  const [ul, ut] = updateLevel(o);
  const cn = o.markets?.cn, us = o.markets?.us;
  const fails = o.token?.failed_logins_10min ?? 0;
  return el('div', { class: 'sys-lights' },
    light(t('sys.l.server'), 'ok', uptime(o.server?.uptime), o.server?.managed ? t('sys.managed') : t('sys.unmanaged')),
    light(t('sys.l.update'), ul, ut),
    light(t('sys.l.cn'), ageLevel(cn?.age_days), ageText(cn), cn?.data_date || null),
    light(t('sys.l.us'), ageLevel(us?.age_days), ageText(us), us?.data_date || null),
    light(t('sys.l.disk'), diskLevel(o.disk?.free), t('sys.free', { v: bytes(o.disk?.free) })),
    light(t('sys.l.login'), fails > 0 ? 'warn' : 'ok', fails > 0 ? t('sys.fails', { n: fails }) : t('sys.nofail')),
    monitorLight(mon));
}

function dataCard(o) {
  const rows = [];
  for (const [k, m] of Object.entries(o.markets || {})) {
    rows.push(kv(t('sys.d.date', { m: m.name || k }), m.data_date || '—', m.data_date ? `sys-t--${ageLevel(m.age_days)}` : null));
    rows.push(kv(t('sys.d.gen', { m: m.name || k }), whenIso(m.generated_at)));
  }
  const all = o.markets?.cn?.all;
  if (all) rows.push(kv(t('sys.d.cache'), `${all.cached ?? '—'} / ${all.listed ?? '—'}`));
  return card('sys.c.data', ...rows);
}

function jobsCard(o) {
  const name = (lb) => t(`sys.job.${lb.split('.').pop()}`);
  const rows = (o.launchd || []).map((j) => {
    if (j.available === false) return kv(name(j.label), t('sys.job.na'));
    if (!j.loaded) return kv(name(j.label), t('sys.job.unloaded'), 'sys-t--bad');
    if (j.pid) {
      // D46：「网站服务」这个后台任务的 PID 和下面「网站服务」卡片里本进程的 PID 不同时（例如沙盒/手动启动的服务），
      // 写清楚它是另一个进程，免得以为同一个服务有两个 PID
      const other = j.label === 'com.quantdaily.server' && o.server?.pid && j.pid !== o.server.pid;
      const note = other ? (lang() === 'en' ? ' · another process, not this server' : ' · 另一个进程，不是当前这个网站服务') : '';
      return kv(name(j.label), t('sys.job.running', { pid: j.pid }) + note, other ? 'sys-t--warn' : 'sys-t--ok');
    }
    return kv(name(j.label), j.last_exit ? t('sys.job.exit', { c: j.last_exit }) : t('sys.job.loaded'), j.last_exit ? 'sys-t--warn' : 'sys-t--ok');
  });
  for (const task of Object.values(o.tasks || {})) {
    if (task.running || task.started) rows.push(kv(task.label, task.running ? t('sys.task.running') : t('sys.task.done', { c: task.returncode ?? '—' })));
  }
  if (o.share) rows.push(kv(t('sys.share'), o.share.running ? t('sys.on') : t('sys.off'), o.share.running ? 'sys-t--warn' : null));
  return card('sys.c.jobs', ...rows);
}

function diskCard(o) {
  const d = o.disk || {};
  return card('sys.c.disk',
    kv(t('sys.disk.free'), bytes(d.free), `sys-t--${diskLevel(d.free)}`),
    kv(t('sys.disk.data'), bytes(d.data)), kv(t('sys.disk.logs'), bytes(d.logs)), kv(t('sys.disk.export'), bytes(d.export)),
    ...(d.top || []).slice(0, 4).map((p) => kv(p.name, bytes(p.bytes), 'muted')));
}

function serverCard(o) {
  const s = o.server || {};
  return card('sys.c.server',
    kv(t('sys.srv.started'), when(s.started)), kv(t('sys.srv.uptime'), uptime(s.uptime)),
    kv('Python', s.python || '—'), kv('PID', String(s.pid ?? '—')),
    kv(t('sys.srv.launchd'), s.managed ? t('sys.yes') : t('sys.no'), s.managed ? null : 'sys-t--warn'));
}

function logCard(log, redraw) {
  const tabs = el('div', { class: 'segmented', role: 'tablist' }, ...LOGS.map((n) => {
    const b = el('button', { class: 'segmented__btn', type: 'button', role: 'tab', 'aria-selected': String(n === logName) }, t(`sys.log.${n}`));
    b.addEventListener('click', () => { logName = n; redraw(); });
    return b;
  }));
  const lines = log?.lines || [];
  const shown = lines.map(logLine);                                   // D46：行首时间换成北京时间
  const converted = shown.some((x, i) => x !== lines[i]);
  const body = lines.length ? shown.join('\n') : t('sys.log.empty');
  return el('section', { class: 'card sys-card sys-card--log' },
    el('div', { class: 'sys-card__head' }, el('h2', { class: 'card__title' }, t('sys.c.log')), tabs),
    el('p', { class: 'muted sys-small' }, (log?.mtime ? t('sys.log.mtime', { v: when(log.mtime) }) : '') + (converted ? (lang() === 'en' ? ' · log times shown in Beijing time' : ' · 日志时间已换成北京时间') : '')),
    el('pre', { class: 'sys-log mono' }, body));
}

function loginBox() {
  const icon = svg('svg', { class: 'state__icon', 'aria-hidden': 'true' });
  icon.append(svg('use', { href: `${versioned(new URL('../shared/icons.svg', import.meta.url))}#i-cloud-off` }));   // D39：带哈希，与其他图标同一地址
  return el('div', { class: 'card state', role: 'alert' }, icon,
    el('p', { class: 'state__title' }, t('sys.auth.title')),
    el('p', { class: 'muted' }, t('sys.auth.body')),
    el('a', { class: 'btn btn--sm btn--primary', href: ENTER }, t('sys.auth.go')));
}

/* ---------- 取数 + 画 ---------- */
async function load() {
  const o = await getJSON('/api/admin/overview', { mock: 'system_overview' });
  const soft = (p) => p.catch(() => null);
  const [log, mon] = await Promise.all([
    soft(getJSON(`/api/admin/log?name=${logName}&n=14`, { mock: `system_log_${logName}` })),
    soft(getJSON('/api/market_monitor/status', { mock: 'system_monitor_status' }))]);
  return { o, log, mon };
}

function draw(root, { o, log, mon }) {
  const head = document.querySelector('[data-bind="meta"]');
  if (head) head.textContent = t('sys.head', { v: when(o.now || Date.now() / 1000), s: REFRESH_MS / 1000 });
  const redraw = () => tick(root);
  root.replaceChildren(el('div', { class: 'sys' },
    lights(o, mon),
    el('div', { class: 'sys-cards' }, dataCard(o), jobsCard(o), diskCard(o), serverCard(o)),
    logCard(log, redraw),
    el('p', { class: 'muted sys-small' }, t('sys.readonly'), ' ', el('a', { href: 'publish.html' }, t('sys.publish')))));
}

async function tick(root) {
  if (!root.isConnected) { clearInterval(timer); timer = null; return; }
  try { draw(root, await load()); } catch { /* 自动刷新失败就保留上一次的内容，下次再试 */ }
}

export async function render(root) {
  clearInterval(timer);
  timer = null;
  let data;
  try {
    data = await load();
  } catch (e) {
    if (needLogin(e)) { root.replaceChildren(loginBox()); return; }
    throw e;
  }
  draw(root, data);
  if (!isMock()) timer = setInterval(() => tick(root), REFRESH_MS);
}
