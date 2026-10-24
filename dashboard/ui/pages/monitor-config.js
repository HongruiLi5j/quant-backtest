/* ==========================================================
 * 文件: ui/pages/monitor-config.js
 * 用途: 盯盘页的「配置」表单（从 monitor.js 拆出）
 *   A 股：采样间隔 / 信号前 N 只 / 数据源（tencent 逐只、easy 批量）/ 名单上限 / 批量开关
 *   美股：只有采样间隔和信号前 N 只
 * 说明: 用户正在编辑的控件，轮询回来的状态不会覆盖它（document.activeElement 判断）
 * ========================================================== */

import { el } from '../shared/dom.js';
import { t } from '../shared/i18n.js';
import { errKey, post, isNum } from './u3-common.js';

/** 逐只（tencent）模式的最小采样间隔，与后端 MIN_INTERVAL 一致 */
const SINGLE_MIN = 30;
/** 批量模式的最小采样间隔，与后端 BATCH_MIN_INTERVAL 一致 */
const BATCH_MIN = 10;

/** 用户「请求的」数据源：降级时 status.provider 是实际生效的 tencent，但用户请求的仍是 easy（batch_requested） */
function requestedProvider(s) {
  if (s.degraded === true && typeof s.batch_requested === 'boolean') return s.batch_requested ? 'easy' : 'tencent';
  return s.provider;
}

/**
 * 纯函数：根据当前状态和表单值，算出要依次提交的请求体。
 * 后端 set_runtime 先拿旧间隔按新模式校验：批量(<30 秒)切回 tencent 且同时把间隔改到 30 秒以上时，要先单独保存间隔。
 */
export function configPlan(s, form) {
  s = s || {};
  const body = { interval: form.interval, top_n: form.topN };
  // 与「请求值」比较而不是与实际生效值比较：降级后用户选 tencent 才会真正提交
  if (form.provider !== requestedProvider(s)) { body.provider = form.provider; body.batch_mode = form.provider === 'easy'; }
  if (form.maxWatch !== s.max_watch) body.max_watch = form.maxWatch;
  const plan = [];
  if (body.provider === 'tencent' && typeof s.interval === 'number' && s.interval < SINGLE_MIN && form.interval >= SINGLE_MIN) plan.push({ interval: form.interval });
  plan.push(body);
  return plan;
}

function numberField(id, labelKey, value, min, max) {
  const input = el('input', { class: 'ws-input', type: 'number', id, min, max, step: 1, value, inputmode: 'numeric' });
  return { input, label: el('label', { class: 'u3-field' }, el('span', {}, t(labelKey)), input) };
}

/** ctx = { market, base, msg, getStatus, refresh }；返回 { node, sync(status) } */
export function buildConfig({ market, base, msg, getStatus, refresh }) {
  const us = market === 'us';
  const errPrefixes = us ? ['usmon', 'mon'] : ['mon'];
  const interval = numberField('monInterval', us ? 'usmon.cfg.interval' : 'mon.cfg.interval', us ? 300 : 60, us ? 60 : BATCH_MIN, 3600);
  const topN = numberField('monTopN', us ? 'usmon.cfg.top_n' : 'mon.cfg.top_n', us ? 0 : 20, 0, us ? 30 : 5500);
  const save = el('button', { class: 'btn btn--sm', type: 'submit' }, t('mon.cfg.save'));
  const fields = [interval.label, topN.label];

  let prov = null, maxWatch = null, batch = null;
  if (!us) {
    // provider 与 batch_mode 是同一件事（tencent = 逐只，easy = 批量），两个控件互相同步
    prov = el('select', { class: 'ws-input', id: 'monProvider' },
      el('option', { value: 'tencent' }, t('mon.cfg.provider.tencent')), el('option', { value: 'easy' }, t('mon.cfg.provider.easy')));
    maxWatch = numberField('monMaxWatch', 'mon.cfg.max_watch', 500, 1, 5500);
    batch = el('input', { type: 'checkbox', id: 'monBatch' });
    prov.addEventListener('change', () => { batch.checked = prov.value === 'easy'; });
    batch.addEventListener('change', () => { prov.value = batch.checked ? 'easy' : 'tencent'; });
    fields.push(el('label', { class: 'u3-field' }, el('span', {}, t('mon.cfg.provider')), prov), maxWatch.label,
      el('label', { class: 'u3-field u3-field--check' }, batch, el('span', {}, t('mon.cfg.batch_mode'))));
  }
  fields.push(save);
  const hints = us
    ? [el('p', { class: 'ws-note' }, t('usmon.cfg.hint'))]
    : [el('p', { class: 'ws-note' }, t('mon.cfg.hint')), el('p', { class: 'ws-note' }, t('mon.cfg.hint_provider'))];
  const form = el('form', { class: 'u3-form-grid u3-config', id: 'monConfig' }, ...fields);
  const node = el('div', {}, el('h3', { class: 'u3-sub' }, t('mon.cfg.title')), form, ...hints);

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    if (us) {
      const r = await post(`${base}/config`, { interval: Number(interval.input.value), top_n: Number(topN.input.value) });
      if (!r.ok) { msg.show(errKey(r.err, errPrefixes)); return; }
    } else {
      const f = { interval: Number(interval.input.value), topN: Number(topN.input.value), provider: prov.value, maxWatch: Number(maxWatch.input.value) };
      for (const body of configPlan(getStatus(), f)) {
        const r = await post(`${base}/config`, body);
        if (!r.ok) {
          // 选 easy 被拒绝最常见的原因是本机没装 easyquotation，页面分不出具体原因，给固定提示
          msg.show(body.provider === 'easy' ? 'mon.cfg.err_easy' : errKey(r.err, errPrefixes));
          return;
        }
      }
    }
    msg.show('mon.cfg.saved', 'ok');
    await refresh();
  });

  /** 用最新状态回填控件；正在编辑的不动 */
  function sync(s) {
    if (!s) return;
    const free = (x) => document.activeElement !== x;
    if (free(interval.input) && isNum(s.interval)) interval.input.value = s.interval;
    if (free(topN.input) && isNum(s.top_n)) topN.input.value = s.top_n;
    if (us) return;
    const req = requestedProvider(s);   // 下拉框显示「用户请求的」数据源，降级时实际生效的由状态提示说明
    if (free(prov) && (req === 'easy' || req === 'tencent')) prov.value = req;
    if (free(batch) && (req === 'easy' || req === 'tencent')) batch.checked = req === 'easy';
    if (free(maxWatch.input) && isNum(s.max_watch)) maxWatch.input.value = s.max_watch;
  }
  return { node, sync };
}
