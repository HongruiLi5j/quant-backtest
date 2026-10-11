"use strict";
// 第三方引擎页面：backtrader（国外）、RQAlpha 米筐（国内）、三引擎对比、代码编辑
// 依赖 app.js 里的工具函数（$、api、esc、fmt、pct、cls、css、lineChart、store、emptyState）

const E = { meta: null, inited: {}, scanTimer: null, code: { key: null, orig: "", ro: false, preview: false } };
const ENG_NAMES = { inhouse: "自研引擎", backtrader: "backtrader（国外）", rqalpha: "RQAlpha 米筐（国内）", custom: "自定义策略" };
const ENG_COLOR = () => ({ inhouse: css("--blue"), backtrader: css("--indigo"), rqalpha: css("--orange"), custom: css("--teal") });
const ENG = {
  bt: {
    key: "backtrader", title: "backtrader", tag: "国外 · 最常用的开源 Python 事件驱动回测框架", code: "bt_strategies", halfSpan: 4,
    intro: `<b>backtrader</b>（backtrader.com）一根 K 线一根 K 线往前推：策略在收盘后决定目标仓位，第二天开盘（cheat-on-open，相当于集合竞价）按整手成交。
      A 股规则由本项目补上：<b>佣金+过户费万2.6、每笔最低 5 元，卖出印花税按当时税率（2023-08-28 前 0.1%，之后 0.05%），100 股一手，开盘涨停买不进、跌停卖不出</b>。
      策略用 backtrader 自带的指标库重新实现，和自研引擎"独立地再算一遍"。`,
  },
  rq: {
    key: "rqalpha", title: "RQAlpha 米筐", tag: "国内 · 米筐科技开源回测框架，内置 A 股交易规则", code: "rq_strategies", halfSpan: 3,
    intro: `<b>RQAlpha</b>（github.com/ricequant/rqalpha）是国内最有名的开源回测框架之一，<b>T+1、涨跌停不能成交、100 股一手、最低 5 元佣金、真实印花税、停牌不能交易</b>都由 RQAlpha 自己检查和撮合。
      策略在集合竞价阶段（open_auction）下单、按开盘价成交。数据没有用米筐的数据包，而是写了一个自定义数据源 Mod，把本项目的本地数据接进去——三个引擎吃同一份数据，差别只来自引擎本身。
      RQAlpha 每次回测约 1~2 秒，比 backtrader 慢，所以参数优化的组合数上限更小。`,
  },
};

// ================================================================ 通用
async function postJSON(path, body) {
  const r = await fetch(path, { method: "POST", credentials: "same-origin",
    headers: { "X-Requested-With": "dashboard", "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
  if (r.status === 401) throw new Error("登录已失效：在终端运行 bash scripts/open_dashboard.sh 重新打开");
  return r.json();
}
async function engMeta() { if (!E.meta) E.meta = await api("/api/eng/meta"); return E.meta; }
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function runJob(body, onProgress) {
  const r = await postJSON("/api/eng/job", body);
  if (r.error) throw new Error(r.error);
  for (;;) {
    await sleep(700);
    const g = await api(`/api/eng/job?id=${r.id}`);
    if (g.status === "running") { if (onProgress) onProgress(g.progress); continue; }
    if (g.status === "done") return g.result;
    const err = new Error(g.error || (g.result && g.result.error) || "运行失败");
    err.trace = g.result && g.result.trace;
    throw err;
  }
}
function errHTML(e) { return `<div class="errbox">${esc(e.message)}${e.trace ? "\n\n" + esc(e.trace) : ""}</div>`; }
function progText(p) { return p ? `${esc(p.msg || "运行中")}（${p.elapsed ?? 0} 秒）` : "运行中…"; }

function stockFields(pk) {
  return `<label class="field">股票代码<input id="${pk}Code" list="engWl" maxlength="6" inputmode="numeric" placeholder="如 600531" style="width:110px;min-width:0"></label>
    <label class="field">策略<select id="${pk}Strat"></select></label>
    <label class="field">开始统计日期<input type="date" id="${pk}Start"></label>
    <label class="field">初始资金（元）<input type="number" id="${pk}Cash" value="100000" min="10000" step="10000" style="width:120px;min-width:0"></label>`;
}
function stratDef(key) { return E.meta.strategies.find((s) => s.key === key) || E.meta.strategies[0]; }
function dec(step) { return (String(step).split(".")[1] || "").length; }
function renderParamInputs(el, key, values) {
  const st = stratDef(key);
  el.innerHTML = st.params.map((p) => {
    const v = values && values[p.key] !== undefined ? values[p.key] : p.default;
    return `<label class="field" title="${esc(p.help)}">${esc(p.label)}${p.help ? " ⓘ" : ""}
      <input type="number" data-key="${esc(p.key)}" value="${Number(v).toFixed(dec(p.step))}" min="${p.min}" max="${p.max}" step="${p.step}" style="width:120px;min-width:0"></label>`;
  }).join("") || `<span class="dim">这个策略没有可调参数</span>`;
}
function readParams(el) { const o = {}; el.querySelectorAll("input[data-key]").forEach((i) => { o[i.dataset.key] = Number(i.value); }); return o; }
function formValues(pk) {
  const code = $(`${pk}Code`).value.trim();
  if (!/^(60|68|00|30)\d{4}$/.test(code)) throw new Error("请输入 6 位沪深 A 股代码（60/68/00/30 开头）");
  store.set("engCode", code);
  return { code, strategy: $(`${pk}Strat`).value, start: $(`${pk}Start`).value || null, cash: Number($(`${pk}Cash`).value) || 100000 };
}
function setupForm(pk, paramsEl, onChange) {
  $(`${pk}Strat`).innerHTML = E.meta.strategies.map((s) => `<option value="${esc(s.key)}">${esc(s.label)}</option>`).join("");
  $(`${pk}Strat`).value = store.get(`${pk}Strat`, "ma_adx_original");
  if (!$(`${pk}Strat`).value) $(`${pk}Strat`).value = E.meta.strategies[0].key;
  $(`${pk}Code`).value = store.get("engCode", E.meta.default_code);
  const desc = () => { const d = $(`${pk}Desc`); if (d) d.textContent = stratDef($(`${pk}Strat`).value).desc; };
  $(`${pk}Strat`).addEventListener("change", () => { store.set(`${pk}Strat`, $(`${pk}Strat`).value); renderParamInputs(paramsEl, $(`${pk}Strat`).value); desc(); if (onChange) onChange(); });
  renderParamInputs(paramsEl, $(`${pk}Strat`).value); desc();
}
function ensureDatalist() {
  if ($("engWl")) return;
  const dl = document.createElement("datalist"); dl.id = "engWl";
  dl.innerHTML = Object.entries(E.meta.watchlist || {}).map(([k, v]) => `<option value="${esc(k)}">${esc(v)}</option>`).join("");
  document.body.appendChild(dl);
}
function libWarn() {
  return E.meta.lib_ok ? "" : `<div class="banner">没有找到引擎库（项目里的 .engines_lib 文件夹）。backtrader 和 RQAlpha 装在这个文件夹里，和主环境的 pandas 版本隔离。</div>`;
}

// ================================================================ 结果展示（单个引擎）
const TILE_DEFS = [
  ["年化收益", (s) => fmt(s["CAGR%"], 1, "%"), (b) => fmt(b["CAGR%"], 1, "%")],
  ["Sharpe", (s) => fmt(s.Sharpe), (b) => fmt(b.Sharpe)],
  ["最大回撤", (s) => fmt(s["MaxDD%"], 1, "%"), (b) => fmt(b["MaxDD%"], 1, "%")],
  ["总收益", (s) => fmt(s["TotalRet%"], 0, "%"), (b) => fmt(b["TotalRet%"], 0, "%")],
  ["期末资产", (s) => isNum(s.FinalValue) ? `${(s.FinalValue / 10000).toFixed(2)}万` : "—", () => "—"],
  ["交易次数", (s) => s.Trades ?? "—", () => "—"],
  ["胜率", (s) => fmt(s["Win%"], 0, "%"), () => "—"],
  ["手续费+税", (s) => isNum(s.Fees) ? `${Math.round(s.Fees).toLocaleString()} 元` : "—", () => "—"],
  ["持仓时间", (s) => fmt(s["Exposure%"], 0, "%"), () => "100%"],
];
function tradesTable(trades) {
  if (!trades.length) return `<tbody><tr><td class="dim">没有交易</td></tr></tbody>`;
  return `<thead><tr><th>买入</th><th>卖出</th><th>天数</th><th>买入价</th><th>卖出价</th><th>成交笔数</th><th>费用</th><th>盈亏（元）</th><th>收益</th></tr></thead><tbody>` +
    trades.map((t) => `<tr><td>${esc(t.entry)}</td><td>${t.open ? '<span class="pill buy">持仓中</span>' : esc(t.exit)}</td><td>${t.days}</td>
      <td>${fmt(t.entry_px)}</td><td>${fmt(t.exit_px)}</td><td>${t.fills}</td><td>${fmt(t.fees, 0)}</td><td class="${cls(t.pnl)}">${fmt(t.pnl, 0)}</td><td class="${cls(t.ret)}">${pct(t.ret, 2)}</td></tr>`).join("") + "</tbody>";
}
function renderEngineResult(el, r, pk) {
  const s = r.stats, b = r.bench_stats, color = ENG_COLOR()[r.engine];
  el.hidden = false;
  el.innerHTML = `
    <div class="enginehead" style="margin-top:10px"><h2 style="margin:6px 0">${esc(r.name)} <small>${esc(r.code)} · ${esc(r.label)} · ${esc(r.start)} → ${esc(r.end)}</small></h2>
      <span class="spacer" style="flex:1"></span><span class="badge">用时 ${fmt(r.job_seconds ?? r.elapsed, 1)} 秒</span></div>
    <div class="tiles">${TILE_DEFS.map(([k, f, g]) => `<div class="tile"><div class="k">${k}</div><div class="v">${f(s)}</div><div class="b">持有 ${g(b)}</div></div>`).join("")}</div>
    <div class="legend"><span><i style="background:${color}"></i>${esc(ENG_NAMES[r.engine])}</span><span><i style="background:${css("--gray")}"></i>一直持有</span></div>
    <div class="chart" id="${pk}Eq"></div>
    <div class="dim" style="margin:12px 0 4px">回撤</div><div class="chart" id="${pk}Dd"></div>
    <div class="two" style="margin-top:14px">
      <div><h2 style="margin-top:0">引擎规则</h2><ul class="notes">${(r.engine_notes || []).map((n) => `<li>${esc(n)}</li>`).join("")}</ul>
        ${r.price_note ? `<p class="dim">${esc(r.price_note)}</p>` : `<p class="dim">价格：后复权价整体乘以 ${fmt(r.price_scale, 4)}，让最后一天等于真实股价（不改变任何一天的涨跌幅，只让"一手多少钱"符合实际）。</p>`}
        ${r.native_summary ? `<p class="dim">RQAlpha 自带报告：总收益 ${pct((r.native_summary.total_returns || 0) * 100)}，年化 ${pct((r.native_summary.annualized_returns || 0) * 100)}，最大回撤 ${fmt((r.native_summary.max_drawdown || 0) * 100, 1, "%")}（从数据第一天算起，包含预热期，所以和上面的数字不同）</p>` : ""}</div>
      <div><h2 style="margin-top:0">提示 <small>涨跌停、拒单等</small></h2>${(r.events || []).length ? `<div class="tbl" style="max-height:220px;overflow-y:auto"><table><tbody>${r.events.slice().reverse().map((e) => `<tr><td>${esc(e.date)}</td><td style="text-align:left">${esc(e.msg)}</td></tr>`).join("")}</tbody></table></div>` : `<p class="dim">没有被拒的订单</p>`}</div>
    </div>
    <div id="${pk}Ana"></div>
    <h2>逐笔交易 <small>从空仓买入到全部卖出算一笔；中间的加减仓算在同一笔里</small></h2>
    <div class="tbl" style="max-height:380px;overflow-y:auto"><table>${tradesTable(r.trades)}</table></div>
    <details class="more"><summary>全部成交记录（${r.fills.length} 笔）</summary>
      <div class="tbl" style="max-height:320px;overflow-y:auto"><table><thead><tr><th>日期</th><th>方向</th><th>股数</th><th>成交价</th><th>费用</th></tr></thead><tbody>${
        r.fills.slice().reverse().map((f) => `<tr><td>${esc(f.date)}</td><td>${f.qty > 0 ? '<span class="pill buy">买入</span>' : '<span class="pill sell">卖出</span>'}</td><td>${Math.abs(f.qty).toLocaleString()}</td><td>${fmt(f.price)}</td><td>${fmt(f.fee)}</td></tr>`).join("")}</tbody></table></div></details>`;
  if (typeof renderAnalysis === "function") renderAnalysis($(`${pk}Ana`), r);
  lineChart($(`${pk}Eq`), { dates: r.dates, log: true, height: 280, series: [{ name: "一直持有", values: r.bench, color: css("--gray"), width: 1.5 }, { name: ENG_NAMES[r.engine], values: r.equity, color }] });
  lineChart($(`${pk}Dd`), { dates: r.dates, height: 120, zero: true, yfmt: (v) => `${v.toFixed(0)}%`, series: [{ name: "回撤", values: r.drawdown, color: css("--red"), width: 1.4, fill: true }] });
}

// ================================================================ 单引擎页面
function enginePageHTML(pk) {
  const c = ENG[pk];
  return `<div class="title"><h1>${c.title}</h1><span class="sub">${c.tag}</span></div>
    ${libWarn()}
    <div class="note" style="margin-bottom:14px">${c.intro}</div>
    <div class="card">
      <div class="toolbar">${stockFields(pk)}</div>
      <div class="dim" id="${pk}Desc"></div>
      <div class="toolbar" id="${pk}Params" style="margin-top:12px"></div>
      <div class="toolbar"><button class="btn primary" id="${pk}Run">运行回测</button><button class="btn plain" id="${pk}Reset">恢复默认参数</button>
        <a class="btn plain" href="#/code/${c.code}">查看 / 修改策略代码</a><span class="dim" id="${pk}Msg"></span></div>
      <div id="${pk}Result" hidden></div>
    </div>
    <h2>参数优化 <small>网格搜索 + 样本内/样本外检验</small></h2>
    <div class="card">
      <p class="dim" style="margin-top:0">用上面选的股票和策略。统计区间切成两段：<b>样本内</b>用来挑参数，<b>样本外</b>是挑完之后才看的"考试"。样本内挑出来的最佳参数，到了样本外往往就不灵了——这就是过拟合。</p>
      <div class="toolbar" id="${pk}OptBar"></div>
      <div class="toolbar"><button class="btn primary" id="${pk}OptRun">开始优化</button><span class="dim" id="${pk}OptMsg"></span></div>
      <div class="progress" id="${pk}OptProg" hidden><i style="width:0%"></i></div>
      <div id="${pk}OptOut"></div>
    </div>`;
}

function optBarHTML(pk) {
  const axis = (a, label) => `<label class="field">${label}<select id="${pk}Opt${a}Key"></select></label>
    <label class="field">从<input type="number" id="${pk}Opt${a}From" style="width:80px;min-width:0"></label>
    <label class="field">到<input type="number" id="${pk}Opt${a}To" style="width:80px;min-width:0"></label>
    <label class="field">步长<input type="number" id="${pk}Opt${a}Step" style="width:70px;min-width:0"></label>`;
  return axis("X", "参数 1") + `<span style="width:12px"></span>` + axis("Y", "参数 2（可不选）") +
    `<label class="field">优化目标<select id="${pk}OptMetric"><option value="sharpe">Sharpe</option><option value="cagr">年化收益</option><option value="calmar">Calmar（年化/回撤）</option><option value="maxdd">最大回撤最小</option></select></label>
     <label class="field">样本外从哪天开始<input type="date" id="${pk}OptSplit" title="不填 = 自动：统计区间的后 30%"></label>
     <span class="dim" id="${pk}OptCount"></span>`;
}
function fillAxis(pk, a) {
  const key = $(`${pk}Opt${a}Key`).value, st = stratDef($(`${pk}Strat`).value);
  const p = st.params.find((x) => x.key === key);
  const box = ["From", "To", "Step"].map((k) => $(`${pk}Opt${a}${k}`));
  box.forEach((b) => { b.disabled = !p; if (!p) b.value = ""; });
  if (!p) return optCount(pk);
  const hs = ENG[pk].halfSpan, d = dec(p.step), step = Number(p.step) * (p.max - p.min > 60 * p.step ? 2 : 1);
  box[0].value = Math.max(p.min, p.default - hs * step).toFixed(d);
  box[1].value = Math.min(p.max, p.default + hs * step).toFixed(d);
  box[2].value = step.toFixed(d);
  box.forEach((b) => { b.min = p.min; b.max = p.max; b.step = p.step; });
  optCount(pk);
}
function optCount(pk) {
  let n = 1;
  for (const a of ["X", "Y"]) {
    if (!$(`${pk}Opt${a}Key`).value) continue;
    const f = Number($(`${pk}Opt${a}From`).value), t = Number($(`${pk}Opt${a}To`).value), s = Number($(`${pk}Opt${a}Step`).value);
    n *= s > 0 && t >= f ? Math.floor((t - f) / s + 1e-9) + 1 : 0;
  }
  const lim = pk === "rq" ? 60 : 150;
  $(`${pk}OptCount`).innerHTML = `共 <b>${n}</b> 组（上限 ${lim}）${n > lim ? ' <span style="color:var(--red)">太多了</span>' : ""}`;
}
function setupOptBar(pk) {
  $(`${pk}OptBar`).innerHTML = optBarHTML(pk);
  const fill = () => {
    const st = stratDef($(`${pk}Strat`).value);
    const opts = st.params.map((p) => `<option value="${esc(p.key)}">${esc(p.label)}</option>`).join("");
    $(`${pk}OptXKey`).innerHTML = opts; $(`${pk}OptYKey`).innerHTML = `<option value="">不选</option>` + opts;
    if (st.params.length > 1) $(`${pk}OptYKey`).value = st.params[1].key;
    fillAxis(pk, "X"); fillAxis(pk, "Y");
    $(`${pk}OptRun`).disabled = !st.params.length;
  };
  fill();
  $(`${pk}OptXKey`).addEventListener("change", () => fillAxis(pk, "X"));
  $(`${pk}OptYKey`).addEventListener("change", () => fillAxis(pk, "Y"));
  $(`${pk}OptBar`).addEventListener("input", () => optCount(pk));
  return fill;
}

async function runEngine(pk) {
  const c = ENG[pk], msg = $(`${pk}Msg`), btn = $(`${pk}Run`);
  let body;
  try { body = { kind: "run", engine: c.key, ...formValues(pk), params: readParams($(`${pk}Params`)) }; } catch (e) { msg.textContent = e.message; return; }
  btn.disabled = true; msg.textContent = "运行中…";
  try {
    const r = await runJob(body, (p) => { msg.textContent = progText(p); });
    msg.textContent = ""; renderEngineResult($(`${pk}Result`), r, pk);
  } catch (e) { msg.textContent = ""; $(`${pk}Result`).hidden = false; $(`${pk}Result`).innerHTML = errHTML(e); } finally { btn.disabled = false; }
}

async function runOptimize(pk) {
  const c = ENG[pk], msg = $(`${pk}OptMsg`), btn = $(`${pk}OptRun`), bar = $(`${pk}OptProg`);
  let body;
  try {
    const ax = (a) => $(`${pk}Opt${a}Key`).value ? { key: $(`${pk}Opt${a}Key`).value, from: Number($(`${pk}Opt${a}From`).value), to: Number($(`${pk}Opt${a}To`).value), step: Number($(`${pk}Opt${a}Step`).value) } : null;
    body = { kind: "optimize", engine: c.key, ...formValues(pk), params: readParams($(`${pk}Params`)),
      opt: { x: ax("X"), y: ax("Y"), metric: $(`${pk}OptMetric`).value, split: $(`${pk}OptSplit`).value || null } };
  } catch (e) { msg.textContent = e.message; return; }
  btn.disabled = true; bar.hidden = false; bar.firstElementChild.style.width = "0%"; msg.textContent = "启动中…"; $(`${pk}OptOut`).innerHTML = "";
  try {
    const r = await runJob(body, (p) => { if (p) { bar.firstElementChild.style.width = `${(p.done / Math.max(1, p.total)) * 100}%`; msg.textContent = progText(p); } });
    msg.textContent = `完成，用时 ${fmt(r.job_seconds, 0)} 秒`; renderOptimize(pk, r);
  } catch (e) { msg.textContent = ""; $(`${pk}OptOut`).innerHTML = errHTML(e); } finally { btn.disabled = false; bar.hidden = true; }
}

function heatColor(v, lo, hi) {
  if (!isNum(v) || !(hi > lo)) return "";
  const t = (v - lo) / (hi - lo);
  return t >= 0.5 ? `background:color-mix(in srgb, var(--green) ${Math.round((t - 0.5) * 2 * 45)}%, transparent)`
    : `background:color-mix(in srgb, var(--red) ${Math.round((0.5 - t) * 2 * 40)}%, transparent)`;
}
function renderOptimize(pk, r) {
  const m = r.metric, seg = (row, s) => (row[s] ? row[s][m] : null);
  const fmtM = (v) => (m === "cagr" || m === "maxdd" ? fmt(v, 1, "%") : fmt(v, 2));
  const ok = r.rows.filter((x) => !x.error);
  const isV = ok.map((x) => seg(x, "is")).filter(isNum), oosV = ok.map((x) => seg(x, "oos")).filter(isNum);
  const [ax, ay] = r.axes;
  const same = (a, b) => ax && Math.abs(a.params[ax.key] - b.params[ax.key]) < 1e-9 && (!ay || Math.abs(a.params[ay.key] - b.params[ay.key]) < 1e-9);
  const pstr = (p) => r.axes.map((a) => `${a.label} = ${p[a.key]}`).join("，");
  const corr = r.rank_corr;
  const corrText = corr === null ? "组合太少，没法算" : corr > 0.5 ? `${fmt(corr)}：样本内好的参数，样本外大体也好，比较可信`
    : corr > 0.2 ? `${fmt(corr)}：关系比较弱，优化结果要打折扣看` : `${fmt(corr)}：样本内排名和样本外几乎没关系——挑出来的"最佳参数"<b>主要是运气</b>`;
  const table = (s, title) => {
    const vals = (s === "is" ? isV : oosV), lo = Math.min(...vals), hi = Math.max(...vals);
    if (!ay) {
      return `<h2 style="margin-top:0">${title}</h2><div class="tbl"><table class="heat"><thead><tr><th>${esc(ax.label)}</th><th>${esc(r.metric_label)}</th><th>年化</th><th>最大回撤</th></tr></thead><tbody>${
        ok.map((x) => `<tr><td>${x.params[ax.key]}</td><td class="${same(x, r.best) ? "best" : ""} ${r.default && same(x, r.default) ? "dflt" : ""}" style="${heatColor(seg(x, s), lo, hi)}">${fmtM(seg(x, s))}</td><td>${fmt(x[s].cagr, 1, "%")}</td><td>${fmt(x[s].maxdd, 1, "%")}</td></tr>`).join("")}</tbody></table></div>`;
    }
    const cell = (xv, yv) => ok.find((x) => Math.abs(x.params[ax.key] - xv) < 1e-9 && Math.abs(x.params[ay.key] - yv) < 1e-9);
    return `<h2 style="margin-top:0">${title}</h2><div class="tbl"><table class="heat"><thead><tr><th>${esc(ay.label)} ↓ / ${esc(ax.label)} →</th>${ax.values.map((v) => `<th>${v}</th>`).join("")}</tr></thead><tbody>${
      ay.values.map((yv) => `<tr><th>${yv}</th>${ax.values.map((xv) => { const c2 = cell(xv, yv); return c2 ? `<td class="${same(c2, r.best) ? "best" : ""} ${r.default && same(c2, r.default) ? "dflt" : ""}" style="${heatColor(seg(c2, s), lo, hi)}" title="年化 ${fmt(c2[s].cagr, 1)}%，回撤 ${fmt(c2[s].maxdd, 1)}%">${fmtM(seg(c2, s))}</td>` : "<td>—</td>"; }).join("")}</tr>`).join("")}</tbody></table></div>`;
  };
  $(`${pk}OptOut`).innerHTML = `
    <div class="note" style="margin:10px 0 14px">
      <div><b>样本内</b> ${esc(r.start)} → ${esc(r.split)}，<b>样本外</b> ${esc(r.split)} → ${esc(r.end)}；共 ${r.n} 组参数，按样本内「${esc(r.metric_label)}」挑最佳。</div>
      <div style="margin-top:6px">最佳参数：<b>${esc(pstr(r.best.params))}</b> —— 样本内 ${fmtM(seg(r.best, "is"))}，样本外 <b>${fmtM(seg(r.best, "oos"))}</b>（在 ${r.n} 组里排第 ${r.best_oos_rank}）</div>
      ${r.default ? `<div>默认参数：${esc(pstr(r.default.params))} —— 样本内 ${fmtM(seg(r.default, "is"))}，样本外 ${fmtM(seg(r.default, "oos"))}</div>` : ""}
      ${isNum(r.neighbor_avg) ? `<div>最佳参数周围一圈的平均值：${fmtM(r.sign < 0 ? -r.neighbor_avg : r.neighbor_avg)}（和最佳值差得少 = "参数平原"，比较稳健；差得多 = 孤立的尖峰，多半是过拟合）</div>` : ""}
      <div>样本内 vs 样本外的排名相关：${corrText}</div>
      <div class="dim" style="margin-top:6px">蓝框 = 样本内最佳，橙框 = 默认参数。绿色越深越好，红色越深越差。</div>
    </div>
    <div class="optgrid"><div>${table("is", "样本内（用来挑参数）")}</div><div>${table("oos", "样本外（检验）")}</div></div>
    <div class="toolbar" style="margin-top:12px"><button class="btn plain" id="${pk}UseBest">用最佳参数运行回测</button>${r.errors.length ? `<span class="dim">有 ${r.errors.length} 组失败：${esc(r.errors[0].error)}</span>` : ""}</div>`;
  $(`${pk}UseBest`).addEventListener("click", () => {
    renderParamInputs($(`${pk}Params`), r.strategy, r.best.params);
    window.scrollTo({ top: 0, behavior: "smooth" }); runEngine(pk);
  });
}

async function loadEnginePage(pk) {
  await engMeta(); ensureDatalist();
  if (!E.inited[pk]) {
    const sec = $(`p-${pk}`);
    sec.innerHTML = enginePageHTML(pk);
    let refillOpt = () => {};
    setupForm(pk, $(`${pk}Params`), () => refillOpt());
    refillOpt = setupOptBar(pk);
    $(`${pk}Run`).addEventListener("click", () => runEngine(pk));
    $(`${pk}Reset`).addEventListener("click", () => { renderParamInputs($(`${pk}Params`), $(`${pk}Strat`).value); runEngine(pk); });
    $(`${pk}Params`).addEventListener("keydown", (e) => { if (e.key === "Enter") runEngine(pk); });
    $(`${pk}Code`).addEventListener("keydown", (e) => { if (e.key === "Enter") runEngine(pk); });
    $(`${pk}OptRun`).addEventListener("click", () => runOptimize(pk));
    E.inited[pk] = true;
    runEngine(pk);
  } else {
    const c = store.get("engCode", ""); if (c && $(`${pk}Code`).value !== c) $(`${pk}Code`).value = c;
  }
}

// ================================================================ 三引擎对比
const CMP_ROWS = [["年化收益", "CAGR%", 1, "%"], ["Sharpe", "Sharpe", 2, ""], ["最大回撤", "MaxDD%", 1, "%"], ["年化波动", "Vol%", 1, "%"],
  ["总收益", "TotalRet%", 0, "%"], ["交易次数", "Trades", 0, ""], ["胜率", "Win%", 0, "%"], ["平均持有天数", "AvgHold", 0, ""],
  ["持仓时间", "Exposure%", 0, "%"], ["手续费+税（元）", "Fees", 0, ""]];
function renderCompare(r) {
  const out = $("cmpOut"); out.hidden = false;
  const engs = ["inhouse", "backtrader", "rqalpha"].filter((e) => r.results[e]);
  const col = ENG_COLOR();
  const first = r.results[engs[0]];
  let h = `<h2>${esc(r.name)} <small>${esc(r.code)} · ${esc(r.label)} · ${first ? esc(first.start) + " → " + esc(first.end) : ""}</small></h2>`;
  if (Object.keys(r.errors || {}).length) h += Object.entries(r.errors).map(([e, m]) => `<div class="errbox"><b>${esc(ENG_NAMES[e])} 出错：</b>\n${esc(m)}</div>`).join("");
  h += `<div class="tbl"><table><thead><tr><th>指标</th>${engs.map((e) => `<th style="color:${col[e]}">${esc(ENG_NAMES[e])}</th>`).join("")}<th>一直持有</th></tr></thead><tbody>${
    CMP_ROWS.map(([n, k, d, suf]) => `<tr><td>${n}</td>${engs.map((e) => `<td>${fmt(r.results[e].stats[k], d, suf)}</td>`).join("")}<td>${first && ["Trades", "Win%", "AvgHold", "Fees", "Exposure%"].includes(k) ? "—" : fmt(first ? first.bench_stats[k] : null, d, suf)}</td></tr>`).join("")}</tbody></table></div>
    <div class="legend" style="margin-top:14px">${engs.map((e) => `<span><i style="background:${col[e]}"></i>${esc(ENG_NAMES[e])}</span>`).join("")}<span><i style="background:${css("--gray")}"></i>一直持有</span></div>
    <div class="chart" id="cmpEq"></div>
    <h2>一致性 <small>两两比较</small></h2>
    <div class="tbl"><table><thead><tr><th>比较</th><th>每天持仓状态一致</th><th>日收益相关系数</th><th>期末净值差</th><th>仓位差 &gt; 5% 的天数</th><th>最早的几天</th></tr></thead><tbody>${
      Object.entries(r.agree).map(([k, a]) => { const [x, y] = k.split("|"); return `<tr><td>${esc(ENG_NAMES[x])} vs ${esc(ENG_NAMES[y])}</td><td>${fmt(a["same_state%"], 1, "%")}</td><td>${fmt(a.ret_corr, 4)}</td>
        <td class="${cls(a["final_gap%"])}">${pct(a["final_gap%"], 2)}</td><td>${a.n_diff}</td><td style="text-align:left;white-space:normal;font-size:12px">${esc(a.diff_days.slice(0, 6).join("、"))}${a.diff_days.length > 6 ? " …" : ""}</td></tr>`; }).join("")}</tbody></table></div>
    <div class="note" style="margin-top:12px"><b>差别从哪里来：</b>
      ① 自研引擎按百分比持仓、固定单边 8bp 成本（含滑点估计）；两个外部引擎按股数成交（100 股一手、每笔最低 5 元、印花税按真实税率、没有滑点）。
      ② backtrader 用它自带的指标库（ATR、ADX 从第二根 K 线起算，均线要等最长的那条算出来才开始交易），RQAlpha 版的指标和自研引擎完全相同。
      ③ 仓位是连续值的策略（波动率目标）差别更大：整手取整、调仓阈值会让实际股数略有不同。
      只要"每天持仓状态一致"接近 100%、日收益相关接近 1，就说明回测逻辑是对的。</div>
    <div class="two" style="margin-top:14px">${engs.map((e) => `<div><h2 style="margin-top:0;color:${col[e]}">${esc(ENG_NAMES[e])}</h2><ul class="notes">${(r.results[e].engine_notes || []).map((n) => `<li>${esc(n)}</li>`).join("")}</ul></div>`).join("")}</div>
    <h2>逐笔对照 <small>按买入日期对齐；空白 = 这个引擎那天没有开新仓</small></h2>
    <div class="tbl" style="max-height:420px;overflow-y:auto"><table><thead><tr><th>买入日</th>${engs.map((e) => `<th>${esc(ENG_NAMES[e])}</th>`).join("")}</tr></thead><tbody>${
      r.trade_rows.map((t) => `<tr><td>${esc(t.entry)}</td>${engs.map((e) => { const x = t[e]; return x ? `<td>${x.open ? "持仓中" : esc(x.exit)} · ${x.days}天 · <span class="${cls(x.ret)}">${pct(x.ret, 2)}</span></td>` : `<td class="dim">—</td>`; }).join("")}</tr>`).join("")}</tbody></table></div>`;
  out.innerHTML = h;
  if (first) {
    lineChart($("cmpEq"), { dates: first.dates, log: true, height: 300,
      series: [{ name: "一直持有", values: first.bench, color: css("--gray"), width: 1.3 }].concat(engs.map((e) => ({ name: ENG_NAMES[e], values: r.results[e].equity, color: col[e], width: 1.8 }))) });
  }
}
async function runCompare() {
  const msg = $("cmpMsg"), btn = $("cmpRun");
  let body;
  try { body = { kind: "compare", ...formValues("cmp"), params: readParams($("cmpParams")) }; } catch (e) { msg.textContent = e.message; return; }
  btn.disabled = true; msg.textContent = "运行中…";
  try { const r = await runJob(body, (p) => { msg.textContent = progText(p); }); msg.textContent = `完成，用时 ${fmt(r.job_seconds, 0)} 秒`; renderCompare(r); }
  catch (e) { msg.textContent = ""; $("cmpOut").hidden = false; $("cmpOut").innerHTML = errHTML(e); } finally { btn.disabled = false; }
}

// ---- 全市场三引擎扫描
async function loadScan() {
  const card = $("scanCard");
  let st;
  try { st = await api("/api/eng/scan"); } catch (e) { card.innerHTML = errHTML(e); return; }
  const p = st.progress, s = st.summary;
  let h = "";
  if (st.running && p) {
    const pc = (p.done / Math.max(1, p.total)) * 100;
    h += `<div class="enginehead"><span class="badge blue">扫描中</span><b>${p.done} / ${p.total} 只</b><span class="dim">已用 ${fmt(p.elapsed_min, 0)} 分钟，预计还要 ${fmt(p.eta_min, 0)} 分钟 · ${p.workers} 个进程并行${p.real_price ? "" : " · 没取到真实股价，按比例缩放"}</span><span class="spacer" style="flex:1"></span><button class="btn plain" id="scanStop">停止</button></div>
      <div class="progress"><i style="width:${pc}%"></i></div><p class="dim">可以关掉网页，扫描在后台继续；中途停止或电脑重启后再点「开始」会接着算。</p>`;
  } else {
    const fin = p && p.finished;
    h += `<div class="enginehead">${fin ? `<span class="badge green">已完成</span><span class="dim">${esc(p.finished)} 完成，${p.total} 只</span>` : p ? `<span class="badge">已暂停</span><span class="dim">完成 ${p.done} / ${p.total} 只</span>` : `<span class="dim">还没有扫描过</span>`}
      <span class="spacer" style="flex:1"></span><button class="btn primary" id="scanStart">${fin ? "重新扫描" : p ? "继续扫描" : "开始扫描全部 A 股"}</button></div>
      <p class="dim">每只股票 × 7 个策略 × 3 个引擎，默认参数。全部跑完大约 1~2 小时（M 系列 Mac），期间 Mac 不会自动睡眠（合上屏幕仍会睡眠）。</p>`;
  }
  if (s && s.strategies) {
    const cell = (x, k, d, suf) => (x ? fmt(x[k], d, suf) : "—");
    h += `<div class="tbl"><table><thead><tr><th>策略</th><th>年化中位数<br>自研 / bt / 米筐</th><th>跑赢持有的股票<br>自研 / bt / 米筐</th>
      <th>持仓状态一致<br>bt / 米筐</th><th>日收益相关<br>bt / 米筐</th><th>年化差（绝对值中位数）<br>bt / 米筐</th><th>年化差 &gt; 1% 的股票<br>bt / 米筐</th><th>"跑赢持有"结论一致<br>bt / 米筐</th></tr></thead><tbody>${
      Object.entries(s.strategies).map(([k, v]) => { const e = v.engines, i = e.inhouse, b = e.backtrader, q = e.rqalpha; return `<tr><td>${esc(v.label)}</td>
        <td>${cell(i, "median_cagr", 1, "%")} / ${cell(b, "median_cagr", 1, "%")} / ${cell(q, "median_cagr", 1, "%")}</td>
        <td>${cell(i, "beat_bh%", 0, "%")} / ${cell(b, "beat_bh%", 0, "%")} / ${cell(q, "beat_bh%", 0, "%")}</td>
        <td>${cell(b, "same_state%", 1, "%")} / ${cell(q, "same_state%", 1, "%")}</td>
        <td>${cell(b, "ret_corr", 3, "")} / ${cell(q, "ret_corr", 3, "")}</td>
        <td>${cell(b, "abs_cagr_gap", 2, "%")} / ${cell(q, "abs_cagr_gap", 2, "%")}</td>
        <td>${cell(b, "gap_over_1pct%", 0, "%")} / ${cell(q, "gap_over_1pct%", 0, "%")}</td>
        <td>${fmt(v["verdict_agree%"].backtrader, 0, "%")} / ${fmt(v["verdict_agree%"].rqalpha, 0, "%")}</td></tr>`; }).join("")}</tbody></table></div>
      <div class="note" style="margin-top:12px"><b>怎么看：</b>已统计 ${s.stocks} 只股票（${esc(s.updated)} 更新）。"持仓状态一致"和"日收益相关"越接近 100% / 1，说明三个引擎对同一套规则的执行越一致；
        "年化差"是外部引擎减自研引擎的年化收益，主要来自成本口径不同（自研单边 8bp 含滑点 vs 外部引擎真实佣金印花税、无滑点）和整手取整。
        最后一列最重要：同一只股票上"策略比一直持有好不好"，三个引擎的结论有多大比例一致——如果很高，自研引擎的全市场结论就可以信。</div>`;
  }
  card.innerHTML = h;
  const b1 = $("scanStart"), b2 = $("scanStop");
  if (b1) b1.addEventListener("click", async () => { b1.disabled = true; await postJSON("/api/eng/scan?action=start"); setTimeout(loadScan, 2500); });
  if (b2) b2.addEventListener("click", async () => { b2.disabled = true; await postJSON("/api/eng/scan?action=stop"); setTimeout(loadScan, 1500); });
  clearTimeout(E.scanTimer);
  if (st.running) E.scanTimer = setTimeout(() => { if (location.hash.startsWith("#/cmp")) loadScan(); }, 15000);
}

async function loadCompare() {
  await engMeta(); ensureDatalist();
  if (!E.inited.cmp) {
    $("cmpBar").innerHTML = stockFields("cmp");
    setupForm("cmp", $("cmpParams"));
    $("cmpRun").addEventListener("click", runCompare);
    $("cmpParams").addEventListener("keydown", (e) => { if (e.key === "Enter") runCompare(); });
    if (!E.meta.lib_ok) $("cmpMsg").innerHTML = "没有找到引擎库 .engines_lib";
    E.inited.cmp = true;
    runCompare();
  } else {
    const c = store.get("engCode", ""); if (c) $("cmpCode").value = c;
  }
  loadScan();
}

// ================================================================ 代码页
const PY_RE = /(#[^\n]*)|("""[\s\S]*?"""|'''[\s\S]*?'''|[rbfu]?"(?:\\.|[^"\\\n])*"|[rbfu]?'(?:\\.|[^'\\\n])*')|\b(def|class)(\s+)([A-Za-z_]\w*)|\b(False|None|True|and|as|assert|break|continue|del|elif|else|except|finally|for|from|global|if|import|in|is|lambda|nonlocal|not|or|pass|raise|return|try|while|with|yield|self)\b|\b(\d+(?:\.\d*)?(?:[eE][+-]?\d+)?)\b|\b(print|len|range|min|max|abs|sum|float|int|str|dict|list|tuple|set|isinstance|super|zip|enumerate|round|sorted|any|all|getattr|hasattr)\b(?=\()/g;
function highlight(src) {
  let out = "", last = 0, m;
  PY_RE.lastIndex = 0;
  while ((m = PY_RE.exec(src))) {
    out += esc(src.slice(last, m.index));
    if (m[1]) out += `<span class="c">${esc(m[1])}</span>`;
    else if (m[2]) out += `<span class="s">${esc(m[2])}</span>`;
    else if (m[3]) out += `<span class="k">${m[3]}</span>${m[4]}<span class="f">${esc(m[5])}</span>`;
    else if (m[6]) out += `<span class="k">${m[6]}</span>`;
    else if (m[7]) out += `<span class="n">${m[7]}</span>`;
    else if (m[8]) out += `<span class="b">${m[8]}</span>`;
    last = PY_RE.lastIndex;
    if (m[0].length === 0) PY_RE.lastIndex++;
  }
  return out + esc(src.slice(last)) + "\n";
}
function codeRender() {
  const ta = $("codeText"), v = ta.value;
  $("codeHl").innerHTML = highlight(v);
  const n = v.split("\n").length;
  let g = ""; for (let i = 1; i <= n; i++) g += i + "\n";
  $("codeGutter").textContent = g;
  codeSync();
  const dirty = !E.code.ro && !E.code.preview && v !== E.code.orig;
  $("codeSave").disabled = E.code.ro || (!dirty && !E.code.preview);
  $("codeRevert").disabled = !dirty && !E.code.preview;
  document.querySelectorAll("#editor .errline").forEach((x) => x.remove());
}
function codeSync() {
  const ta = $("codeText");
  $("codeHl").scrollTop = ta.scrollTop; $("codeHl").scrollLeft = ta.scrollLeft; $("codeGutter").scrollTop = ta.scrollTop;
  document.querySelectorAll("#editor .errline").forEach((x) => { x.style.top = `${Number(x.dataset.top) - ta.scrollTop}px`; });
}
function markErrLine(line) {
  const ta = $("codeText"), lh = parseFloat(getComputedStyle(ta).lineHeight) || 20;
  const d = document.createElement("div"); d.className = "errline"; d.dataset.top = String(12 + (line - 1) * lh);
  d.style.height = `${lh}px`; $("editor").appendChild(d); codeSync();
  ta.scrollTop = Math.max(0, (line - 6) * lh); codeSync();
}
function insertText(ta, text) {
  ta.focus();
  if (!document.execCommand || !document.execCommand("insertText", false, text)) ta.setRangeText(text, ta.selectionStart, ta.selectionEnd, "end");
  codeRender();
}
async function openCode(key) {
  if (E.code.key && E.code.key !== key && isDirty() && !confirm("当前文件还没保存，确定要离开吗？")) { location.hash = `#/code/${E.code.key}`; return; }
  const f = await api(`/api/code/file?f=${encodeURIComponent(key)}`);
  if (f.error) { $("codeMsg").textContent = f.error; return; }
  E.code = { key, orig: f.content, ro: !f.editable, preview: false };
  $("codeName").textContent = f.path.split("/").pop(); $("codePath").textContent = f.path; $("codeDesc").textContent = f.desc + (f.editable ? "" : "（只读）");
  const ta = $("codeText"); ta.value = f.content; ta.readOnly = !f.editable; ta.scrollTop = 0;
  $("editor").classList.toggle("ro", !f.editable);
  ["codeSave", "codeCheck", "codeRestore", "codeHist"].forEach((id) => { $(id).style.display = f.editable ? "" : "none"; });
  $("codeCheck").style.display = ["bt_strategies", "rq_strategies", "strategies", "bt_base", "rq_base"].includes(key) ? "" : "none";
  $("codeMsg").innerHTML = key === "strategies" ? `<span style="color:var(--orange)">注意：src/strategies.py 也被每日任务和全市场扫描使用，改错会影响每天的信号。保存后网页服务会自动重启（约 5 秒）。</span>` : "";
  $("codeCheckOut").innerHTML = "";
  const isCustom = key.startsWith("custom_");
  $("codeRunBar").style.display = isCustom ? "" : "none";
  if (isCustom) { $("codeRunCode").value = store.get("engCode", E.meta ? E.meta.default_code : ""); }
  else { $("codeRunResult").hidden = true; $("codeRunResult").innerHTML = ""; }
  document.querySelectorAll("#codeList a").forEach((a) => a.classList.toggle("on", a.dataset.k === key));
  codeRender(); loadHistory();
}
const isDirty = () => !E.code.ro && !E.code.preview && $("codeText").value !== E.code.orig;
async function loadHistory() {
  if (E.code.ro) return;
  const h = await api(`/api/code/history?f=${encodeURIComponent(E.code.key)}`);
  $("codeHist").innerHTML = `<option value="">当前版本</option>` + (h.versions || []).map((v) => `<option value="${esc(v.v)}">${esc(v.label)}</option>`).join("");
  $("codeRestore").disabled = true;
}
async function saveCode() {
  if (E.code.ro) return false;
  const content = $("codeText").value;
  $("codeMsg").textContent = "保存中…";
  const r = await postJSON("/api/code/save", { f: E.code.key, content });
  if (!r.ok) { $("codeMsg").innerHTML = `<span style="color:var(--red)">${esc(r.error || "保存失败")}（没有保存）</span>`; if (r.line) markErrLine(r.line); return false; }
  E.code.orig = content; E.code.preview = false;
  $("codeMsg").innerHTML = r.unchanged ? "没有改动" : `<span style="color:var(--green)">已保存 ${new Date().toLocaleTimeString()}</span>，旧版本已存进「历史版本」。下一次运行回测就会用新代码。`;
  codeRender(); loadHistory(); return true;
}
async function checkCode() {
  if (!(await saveCode())) return;
  const eng = { bt_strategies: "backtrader", bt_base: "backtrader", rq_strategies: "rqalpha", rq_base: "rqalpha", strategies: "inhouse" }[E.code.key];
  const out = $("codeCheckOut"); out.innerHTML = `<p class="dim">正在用 ${esc(store.get("engCode", "600531"))} 把 ${esc(ENG_NAMES[eng])} 的 7 个策略都跑一遍…</p>`;
  try {
    const r = await runJob({ kind: "check", engine: eng, code: store.get("engCode", E.meta ? E.meta.default_code : "600531") }, (p) => { out.innerHTML = `<p class="dim">${progText(p)}</p>`; });
    const bad = r.rows.filter((x) => !x.ok).length;
    out.innerHTML = `<h2>自检结果 <small>${esc(ENG_NAMES[eng])} · ${esc(r.code)}</small> ${bad ? `<span class="badge red">${bad} 个出错</span>` : '<span class="badge green">全部通过</span>'}</h2>
      <div class="tbl"><table><thead><tr><th>策略</th><th>结果</th><th>年化</th><th>Sharpe</th><th>交易次数</th><th>用时</th></tr></thead><tbody>${
        r.rows.map((x) => x.ok ? `<tr><td>${esc(x.label)}</td><td><span class="badge green">通过</span></td><td>${fmt(x.cagr, 1, "%")}</td><td>${fmt(x.sharpe)}</td><td>${x.trades ?? "—"}</td><td>${fmt(x.sec, 1)} 秒</td></tr>`
          : `<tr><td>${esc(x.label)}</td><td colspan="5" style="text-align:left"><span class="badge red">出错</span> ${esc(x.error)}<div class="errbox">${esc(x.trace)}</div></td></tr>`).join("")}</tbody></table></div>`;
  } catch (e) { out.innerHTML = errHTML(e); }
}
async function runCustom() {
  const msg = $("codeRunMsg"), btn = $("codeRunBtn");
  const code = $("codeRunCode").value.trim();
  if (!/^(60|68|00|30)\d{4}$/.test(code)) { msg.textContent = "请输入 6 位沪深 A 股代码（60/68/00/30 开头）"; return; }
  msg.textContent = "保存中…";
  if (!(await saveCode())) return;   // 先保存代码，再用保存后的版本跑（子进程是全新进程，一定会读到最新代码）
  store.set("engCode", code);
  btn.disabled = true; msg.textContent = "运行中…";
  try {
    const r = await runJob({ kind: "run", engine: "custom", strategy: E.code.key.slice("custom_".length), code,
      cash: Number($("codeRunCash").value) || 100000 }, (p) => { msg.textContent = progText(p); });
    msg.textContent = ""; renderEngineResult($("codeRunResult"), r, "codeRun");
  } catch (e) { msg.textContent = ""; $("codeRunResult").hidden = false; $("codeRunResult").innerHTML = errHTML(e); }
  finally { btn.disabled = false; }
}

async function loadCode() {
  await engMeta(); ensureDatalist();
  if (!E.inited.code) {
    const ta = $("codeText");
    ta.addEventListener("input", codeRender);
    ta.addEventListener("scroll", codeSync);
    ta.addEventListener("keydown", (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "s") { e.preventDefault(); saveCode(); return; }
      if (E.code.ro) return;
      if (e.key === "Tab" && !e.shiftKey) { e.preventDefault(); insertText(ta, "    "); }
      else if (e.key === "Enter") {       // 自动缩进：保持上一行的缩进，行尾是冒号就多缩进一级
        const before = ta.value.slice(0, ta.selectionStart), line = before.slice(before.lastIndexOf("\n") + 1);
        let ind = (line.match(/^\s*/) || [""])[0];
        if (/:\s*(#.*)?$/.test(line)) ind += "    ";
        e.preventDefault(); insertText(ta, "\n" + ind);
      }
    });
    $("codeSave").addEventListener("click", saveCode);
    $("codeCheck").addEventListener("click", checkCode);
    $("codeRevert").addEventListener("click", () => { $("codeText").value = E.code.orig; E.code.preview = false; $("codeHist").value = ""; $("codeMsg").textContent = "已放弃修改"; codeRender(); });
    $("codeHist").addEventListener("change", async () => {
      const v = $("codeHist").value;
      if (!v) { $("codeText").value = E.code.orig; E.code.preview = false; $("codeRestore").disabled = true; codeRender(); return; }
      const r = await api(`/api/code/version?f=${encodeURIComponent(E.code.key)}&v=${encodeURIComponent(v)}`);
      $("codeText").value = r.content || ""; E.code.preview = true; $("codeRestore").disabled = false;
      $("codeMsg").textContent = "正在查看历史版本（还没恢复）。点「恢复这个版本」才会保存成当前代码。"; codeRender();
    });
    $("codeRestore").addEventListener("click", async () => {
      const v = $("codeHist").value; if (!v) return;
      const r = await postJSON(`/api/code/restore?f=${encodeURIComponent(E.code.key)}&v=${encodeURIComponent(v)}`);
      if (r.ok === false || r.error) { $("codeMsg").textContent = r.error || "恢复失败"; return; }
      E.code.orig = $("codeText").value; E.code.preview = false; $("codeMsg").innerHTML = `<span style="color:var(--green)">已恢复</span>（恢复前的代码也存进了历史版本）`; codeRender(); loadHistory();
    });
    window.addEventListener("beforeunload", (e) => { if (isDirty()) { e.preventDefault(); e.returnValue = ""; } });
    $("codeRunBtn").addEventListener("click", runCustom);
    $("codeRunCode").addEventListener("keydown", (e) => { if (e.key === "Enter") runCustom(); });
    E.inited.code = true;
  }
  const list = await api("/api/code/list");
  const item = (f) => `<a data-k="${esc(f.key)}" href="#/code/${esc(f.key)}">${esc(f.path.split("/").pop())}<small>${esc(f.desc)}${f.versions ? ` · ${f.versions} 个历史版本` : ""}</small></a>`;
  const mine = list.files.filter((f) => f.custom), builtinEdit = list.files.filter((f) => !f.custom && f.editable), builtinRO = list.files.filter((f) => !f.custom && !f.editable);
  $("codeList").innerHTML = `<div class="grp" style="display:flex;align-items:center;justify-content:space-between">我的策略<button class="btn plain" id="codeNew" style="padding:2px 9px;font-size:12px">＋ 新建</button></div>${
    mine.map(item).join("") || `<div class="dim" style="padding:4px 14px 10px">还没有，点上面"＋ 新建"写第一个</div>`}
    <div class="grp">内置策略</div>${builtinEdit.map(item).join("")}<div class="grp">只读（引擎框架）</div>${builtinRO.map(item).join("")}`;
  $("codeNew").addEventListener("click", async () => {
    const name = window.prompt("给这个策略起个名字（比如：突破策略、双均线）：", "");
    if (name === null) return;
    const r = await postJSON("/api/code/new", { name });
    if (r.error) { alert(r.error); return; }
    location.hash = `#/code/${r.key}`; loadCode();
  });
  const key = S.routeArg || E.code.key || "bt_strategies";
  if (key !== E.code.key) await openCode(key);
  else document.querySelectorAll("#codeList a").forEach((a) => a.classList.toggle("on", a.dataset.k === key));
}

window.ENG_LOADERS = { bt: () => loadEnginePage("bt"), rq: () => loadEnginePage("rq"), cmp: loadCompare, code: loadCode };
