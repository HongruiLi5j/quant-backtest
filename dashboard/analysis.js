"use strict";
// 回测「分析」区：假设与局限、核心指标、样本内/外、月度收益、滚动 Sharpe、交易分布、成本敏感性、多基准、导出。
// 实验室和三个引擎页共用。数据来自后端 r.analysis（见 src/analytics.py）。

function anaHeat(v, lim) {
  if (!isNum(v)) return "";
  const t = Math.max(-1, Math.min(1, v / lim));
  return t >= 0 ? `background:color-mix(in srgb, var(--green) ${Math.round(t * 55)}%, transparent)`
    : `background:color-mix(in srgb, var(--red) ${Math.round(-t * 55)}%, transparent)`;
}

function anaBars(el, h, color, xfmt) {
  const c = h.counts || [], e = h.edges || [];
  if (!c.length) { el.innerHTML = `<p class="dim">样本太少</p>`; return; }
  const W = Math.max(280, el.clientWidth || 420), H = 150, m = { l: 8, r: 8, t: 8, b: 22 };
  const mx = Math.max(...c, 1), bw = (W - m.l - m.r) / c.length;
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}` });
  c.forEach((n, i) => {
    const hh = (n / mx) * (H - m.t - m.b);
    const neg = xfmt === "pct" && (e[i] + e[i + 1]) / 2 < 0;
    const r = svgEl("rect", { x: m.l + i * bw + 1, y: H - m.b - hh, width: Math.max(1, bw - 2), height: hh, rx: 2, fill: neg ? css("--red") : color, opacity: 0.85 }, svg);
    svgEl("title", {}, r).textContent = `${e[i]} ~ ${e[i + 1]}：${n} 笔`;
  });
  [0, Math.floor(c.length / 2), c.length].forEach((i) => {
    svgEl("text", { x: m.l + i * bw, y: H - 6, "text-anchor": i === 0 ? "start" : i === c.length ? "end" : "middle", "font-size": 11, fill: css("--text-3") }, svg).textContent = xfmt === "pct" ? `${e[i]}%` : `${e[i]}天`;
  });
  el.innerHTML = ""; el.appendChild(svg);
}

function renderAnalysis(el, r) {
  const a = r.analysis;
  if (!a) { el.innerHTML = ""; return; }
  const id = "ana" + Math.random().toString(36).slice(2, 7);
  const f = a.full || {}, bf = a.bench_full || {}, ex = a.extra || {}, sp = a.split || {};
  const row = (k, v, b, d = 2, suf = "", good = "high") =>
    `<tr><td>${k}</td><td class="${good === "high" && isNum(v) && isNum(b) && v > b ? "better" : ""}">${fmt(v, d, suf)}</td><td>${fmt(b, d, suf)}</td></tr>`;
  const core = `<div class="tbl"><table><thead><tr><th>指标</th><th>策略</th><th>一直持有</th></tr></thead><tbody>
    ${row("总收益", f.total, bf.total, 1, "%")}${row("年化收益", f.cagr, bf.cagr, 1, "%")}${row("年化波动", f.vol, bf.vol, 1, "%", "low")}
    ${row("Sharpe", f.sharpe, bf.sharpe)}${row("Sortino（只算下跌波动）", f.sortino, bf.sortino)}${row("Calmar（年化÷最大回撤）", f.calmar, bf.calmar)}
    ${row("最大回撤", f.maxdd, bf.maxdd, 1, "%", "")}</tbody></table></div>`;
  const dist = `<div class="tbl"><table><tbody>
    <tr><td>最好的一天</td><td class="up">${pct(ex.best_day, 2)}</td></tr><tr><td>最差的一天</td><td class="down">${pct(ex.worst_day, 2)}</td></tr>
    <tr><td>单日 VaR(95%)：最差的 5% 天数至少亏</td><td>${pct(ex.var95, 2)}</td></tr><tr><td>偏度（负数 = 容易出大跌）</td><td>${fmt(ex.skew)}</td></tr>
    <tr><td>峰度（越大 = 极端行情越多）</td><td>${fmt(ex.kurt)}</td></tr><tr><td>上涨月份占比</td><td>${fmt(ex.pos_months, 0, "%")}</td></tr></tbody></table></div>`;
  const seg = (k) => (sp[k] || {});
  const oosRow = (name, k, kb) => `<tr><td>${name}</td><td>${fmt(seg(k).cagr, 1, "%")}</td><td>${fmt(seg(k).sharpe)}</td><td>${fmt(seg(k).maxdd, 1, "%")}</td><td>${fmt(seg(kb).cagr, 1, "%")}</td><td>${fmt(seg(kb).sharpe)}</td><td>${fmt(seg(kb).maxdd, 1, "%")}</td></tr>`;
  const split = sp.date ? `<div class="tbl"><table><thead><tr><th>时间段</th><th>策略年化</th><th>策略Sharpe</th><th>策略回撤</th><th>持有年化</th><th>持有Sharpe</th><th>持有回撤</th></tr></thead><tbody>
    ${oosRow(`样本内（${esc(r.start)} → ${esc(sp.date)}，前 70%）`, "is", "b_is")}${oosRow(`样本外（${esc(sp.date)} → ${esc(r.end)}，后 30%）`, "oos", "b_oos")}</tbody></table></div>
    <p class="dim" style="margin:6px 0 0">${isNum(seg("is").sharpe) && isNum(seg("oos").sharpe) ? (seg("oos").sharpe >= seg("is").sharpe * 0.6 ? "样本外 Sharpe 没有明显低于样本内，参数相对稳健。" : "样本外 Sharpe 明显低于样本内，可能有过拟合，请谨慎。") : ""} 注意：策略规则和默认参数是事后看过全部历史才定的，这里的拆分只是粗略的参考，不是严格的样本外检验。</p>` : "";
  const mo = a.monthly || { years: [], matrix: [], year_total: [] };
  const by = a.bench_year || [];
  const mrow = (y, i) => `<tr><td><b>${y}</b></td>${mo.matrix[i].map((v) => `<td style="${anaHeat(v, 6)};text-align:center;font-size:12px">${isNum(v) ? v.toFixed(1) : ""}</td>`).join("")}
    <td class="${cls(mo.year_total[i])}" style="font-weight:600">${pct(mo.year_total[i], 1)}</td><td class="${cls(by[i])}">${pct(by[i], 1)}</td></tr>`;
  const monthly = mo.years.length ? `<div class="tbl" style="overflow-x:auto"><table class="strategy-compact"><thead><tr><th>年</th>${[1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12].map((m) => `<th>${m}月</th>`).join("")}<th>策略全年</th><th>持有全年</th></tr></thead><tbody>${mo.years.map((y, i) => mrow(y, i)).reverse().join("")}</tbody></table></div>` : "";
  const t = a.trades || {};
  const tstats = t.n ? `<div class="tbl"><table><tbody>
    <tr><td>交易笔数</td><td>${t.n}</td><td>胜率</td><td>${fmt(t.win, 1, "%")}</td></tr>
    <tr><td>平均盈利</td><td class="up">${pct(t.avg_win, 2)}</td><td>平均亏损</td><td class="down">${pct(t.avg_loss, 2)}</td></tr>
    <tr><td>盈亏比（平均盈利÷平均亏损）</td><td>${fmt(t.payoff)}</td><td>每笔期望收益</td><td class="${cls(t.expectancy)}">${pct(t.expectancy, 2)}</td></tr>
    <tr><td>最好一笔</td><td class="up">${pct(t.best, 2)}</td><td>最差一笔</td><td class="down">${pct(t.worst, 2)}</td></tr>
    <tr><td>平均持仓天数</td><td>${fmt(t.avg_days, 1)}</td><td>最长连续亏损笔数</td><td>${t.max_lose_streak}</td></tr></tbody></table></div>` : `<p class="dim">没有逐笔交易（一直持仓或多品种轮动），不显示交易分布。</p>`;
  const cs = a.cost_sens || [];
  const cost = cs.length ? `<div class="tbl"><table><thead><tr><th>手续费水平</th><th>单边成本</th><th>年化</th><th>Sharpe</th><th>最大回撤</th></tr></thead><tbody>${cs.map((c) =>
    `<tr><td>${c.label === "0" ? "零成本" : c.label === "1x" ? "当前设置" : c.label.replace("x", " 倍")}</td><td>${fmt(c.cost_bp, 1)}bp</td><td>${fmt(c.cagr, 1, "%")}</td><td>${fmt(c.sharpe)}</td><td>${fmt(c.maxdd, 1, "%")}</td></tr>`).join("")}</tbody></table></div>
    <p class="dim" style="margin:6px 0 0">${cs.length >= 4 && isNum(cs[3].sharpe) ? (cs[3].sharpe > 0.3 ? "成本涨到 4 倍后仍然有正的 Sharpe，对手续费不算敏感。" : "成本涨到 4 倍后 Sharpe 已很低，策略对手续费敏感。") : ""}</p>` : "";
  const mc = a.mc;
  const mcHtml = mc ? `<div class="tbl"><table><thead><tr><th></th>${mc.pct.map((q) => `<th>${q}% 分位</th>`).join("")}</tr></thead><tbody>
    <tr><td>年化收益</td>${mc.cagr.map((v) => `<td class="${cls(v)}">${fmt(v, 1, "%")}</td>`).join("")}</tr>
    <tr><td>最大回撤</td>${mc.maxdd.map((v) => `<td class="down">${fmt(v, 1, "%")}</td>`).join("")}</tr></tbody></table></div>
    <p class="dim" style="margin:6px 0 0">把历史日收益按 ${mc.block} 天一块随机重排 ${mc.n} 次。亏钱的概率 ${fmt(mc.p_loss, 1, "%")}；回撤的 5% 分位是「运气差时」的最大回撤，比回测里那一个数字更值得参考。</p>` : "";
  const ps = a.param_sens || [];
  const psHtml = ps.length ? ps.map((p) => `<div class="dim" style="margin:8px 0 2px">${esc(p.label)}</div><div class="tbl"><table><thead><tr><th>取值</th>${p.pts.map((x) => `<th>${x.v}</th>`).join("")}</tr></thead><tbody>
    <tr><td>Sharpe</td>${p.pts.map((x) => `<td>${fmt(x.sharpe)}</td>`).join("")}</tr><tr><td>年化</td>${p.pts.map((x) => `<td>${fmt(x.cagr, 1, "%")}</td>`).join("")}</tr></tbody></table></div>`).join("")
    + `<p class="dim" style="margin:6px 0 0">每个参数单独在默认值两边各挪两档（范围的 10%、20%）。数字变化不大说明稳健；只有默认值附近好看，说明可能过拟合。</p>` : "";
  const ass = (a.assumptions || []).map(([k, v]) => `<div class="ass-row"><b>${esc(k)}</b><span>${esc(v)}</span></div>`).join("");
  el.innerHTML = `
    <div class="card ana-assume"><h2 style="margin-top:0">假设与局限 <small>看结果前先读这里</small></h2><div class="ass-grid">${ass}</div></div>
    <h2>核心指标 <small>区间 ${esc(r.start)} → ${esc(r.end)}</small></h2>
    <div class="two"><div>${core}</div><div>${dist}</div></div>
    <h2>样本内 / 样本外</h2>${split || `<p class="dim">区间太短，无法拆分。</p>`}
    <h2>月度收益 <small>%，绿涨红跌</small></h2>${monthly}
    <div class="two"><div><h2>滚动 Sharpe <small>近 126 个交易日</small></h2><div class="legend"><span><i style="background:var(--blue)"></i>策略</span><span><i style="background:var(--gray)"></i>一直持有</span></div><div class="chart" id="${id}rs"></div></div>
      <div><h2>仓位占用 <small>100% = 满仓</small></h2><div class="chart" id="${id}ex"></div></div></div>
    ${a.benchmarks ? `<h2>和其他指数比 <small>起点都设为 1</small></h2><div class="legend" id="${id}bl"></div><div class="chart" id="${id}bm"></div>` : ""}
    <h2>交易分布</h2>${tstats}
    ${t.n ? `<div class="two"><div><div class="dim" style="margin:8px 0 2px">每笔收益分布</div><div id="${id}h1"></div></div><div><div class="dim" style="margin:8px 0 2px">持仓天数分布</div><div id="${id}h2"></div></div></div>` : ""}
    ${cost ? `<h2>手续费敏感性 <small>把成本放大后，策略还赚钱吗</small></h2>${cost}` : ""}
    ${mcHtml ? `<h2>蒙特卡洛 <small>历史换个顺序发生会怎样</small></h2>${mcHtml}` : ""}
    ${psHtml ? `<h2>参数敏感性 <small>参数稍微变一下，结果稳不稳</small></h2>${psHtml}` : ""}
    <div class="toolbar" style="margin-top:14px"><button class="btn plain" id="${id}c1">导出净值 CSV</button>${(r.trades || []).length ? `<button class="btn plain" id="${id}c2">导出交易明细 CSV</button>` : ""}</div>`;
  const blue = css("--blue"), gray = css("--gray");
  lineChart($(`${id}rs`), { dates: r.dates, height: 160, zero: true, series: [{ name: "一直持有", values: a.bench_rolling_sharpe, color: gray, width: 1.2 }, { name: "策略", values: a.rolling_sharpe, color: blue, width: 1.6 }] });
  if (a.exposure) lineChart($(`${id}ex`), { dates: r.dates, height: 160, zero: true, yfmt: (v) => `${(v * 100).toFixed(0)}%`, series: [{ name: "仓位", values: a.exposure, color: css("--indigo"), width: 1.2, fill: true }] });
  if (a.benchmarks) {
    const pal = [css("--orange"), css("--teal"), css("--pink")];
    const ser = [{ name: "一直持有", values: r.bench, color: gray, width: 1.4 }, { name: "策略", values: r.equity, color: blue }]
      .concat(Object.entries(a.benchmarks).map(([k, v], i) => ({ name: k, values: v, color: pal[i % 3], width: 1.3 })));
    $(`${id}bl`).innerHTML = ser.map((s) => `<span><i style="background:${s.color}"></i>${esc(s.name)}</span>`).join("");
    lineChart($(`${id}bm`), { dates: r.dates, log: true, height: 220, series: ser });
  }
  if (t.n) { anaBars($(`${id}h1`), t.ret_hist, blue, "pct"); anaBars($(`${id}h2`), t.days_hist, css("--indigo"), "days"); }
  $(`${id}c1`).addEventListener("click", () => downloadCSV(`净值_${r.symbol || r.code || "result"}_${r.strategy}.csv`,
    [["date", "日期"], [(x) => x.eq, "策略净值"], [(x) => x.bh, "持有净值"], [(x) => x.dd, "回撤%"]], r.dates.map((d, i) => ({ date: d, eq: r.equity[i], bh: r.bench[i], dd: r.drawdown[i] }))));
  const c2 = $(`${id}c2`);
  if (c2) c2.addEventListener("click", () => downloadCSV(`交易明细_${r.symbol || r.code || "result"}_${r.strategy}.csv`,
    [["entry", "买入日"], [(x) => (x.open ? "持仓中" : x.exit), "卖出日"], ["days", "持仓天数"], ["ret", "收益%"], ["entry_px", "买入价"], ["exit_px", "卖出价"], ["pnl", "盈亏(元)"]], r.trades));
}
