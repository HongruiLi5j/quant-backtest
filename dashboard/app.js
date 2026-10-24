"use strict";
// 量化日报 · 前端（无外部依赖，图表用 SVG 手画）

// ================================================================ 工具
const $ = (id) => document.getElementById(id);
const S = { meta: null, market: "cn", summary: null, labRan: false, routeArg: null };
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const isNum = (v) => v !== null && v !== undefined && !Number.isNaN(Number(v)) && Number.isFinite(Number(v));
const fmt = (v, d = 2, suf = "") => (isNum(v) ? `${Number(v).toFixed(d)}${suf}` : "—");
const pct = (v, d = 1) => (isNum(v) ? `${v > 0 ? "+" : ""}${Number(v).toFixed(d)}%` : "—");
const cls = (v) => (isNum(v) ? (v > 0 ? "up" : v < 0 ? "down" : "") : "");
// 「科技风」下图表背景是浅灰，图表里的线条/文字改用更深的颜色，保证看得清
const CHART_ON_LIGHT = { "--blue": "#0a6fa0", "--green": "#0f9d6a", "--red": "#d63348", "--gray": "#55647a", "--orange": "#c27a10", "--indigo": "#4f5fd0", "--teal": "#0a8f9f", "--pink": "#c43aa5",
  "--text": "#1c2738", "--fill-2": "#c9d1dc", "--fill": "#bcc5d2", "--surface": "#c9d1dc", "--up": "#d63348", "--down": "#0f9d6a", "--text-2": "#2c394d", "--text-3": "#46556b", "--sep": "#aab5c4" };
const css = (name) => (document.documentElement.getAttribute("data-skin") === "term" && CHART_ON_LIGHT[name]) || getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const store = { get(k, d) { try { return localStorage.getItem(k) ?? d; } catch { return d; } }, set(k, v) { try { localStorage.setItem(k, v); } catch {} } };

async function api(path, opts = {}) {
  const r = await fetch(path, { credentials: "same-origin", ...opts });
  if (r.status === 401) throw new Error("登录已失效：在终端运行 bash scripts/open_dashboard.sh 重新打开");
  return r.json();
}
const post = (path) => api(path, { method: "POST", headers: { "X-Requested-With": "dashboard" } });

function pill(a) {
  if (!a) return `<span class="pill dl">退市</span>`;
  const k = ["买入", "加仓", "换仓"].includes(a) ? "buy" : ["卖出", "减仓"].includes(a) ? "sell" : "hold";
  return `<span class="pill ${k}">${esc(a)}</span>`;
}
function stratOptions(filter = () => true) {
  return Object.entries(S.meta.strategies).filter(([k, v]) => filter(k, v))
    .map(([k, v]) => `<option value="${esc(k)}">${esc(v.label)}</option>`).join("");
}
function emptyState(text) {
  return `<div class="card empty"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="12" cy="12" r="9"/><path d="M12 7v6M12 16.5v.5" stroke-linecap="round"/></svg><div>${text}</div></div>`;
}
function downloadCSV(name, header, rows) {
  const q = (v) => { const s = v === null || v === undefined ? "" : String(v); return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s; };
  const text = "﻿" + [header.map((h) => q(h[1])).join(",")].concat(rows.map((r) => header.map((h) => q(typeof h[0] === "function" ? h[0](r) : r[h[0]])).join(","))).join("\n");
  const url = URL.createObjectURL(new Blob([text], { type: "text/csv;charset=utf-8" }));
  const a = document.createElement("a"); a.href = url; a.download = name; document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };

// ================================================================ 图表：折线
const PALETTE = () => [css("--blue"), css("--orange"), css("--teal"), css("--pink"), css("--indigo"), css("--green")];
const NS = "http://www.w3.org/2000/svg";
function svgEl(tag, attrs, parent) { const e = document.createElementNS(NS, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); if (parent) parent.appendChild(e); return e; }
function yearTicks(dates, x, W, m, parent, H) {
  const ys = []; let last = null;
  dates.forEach((d, i) => { const y = d.slice(0, 4); if (y !== last) { ys.push([i, y]); last = y; } });
  let marks = ys;
  if (ys.length <= 2) {    // 区间短：按月标
    marks = []; let lm = null;
    dates.forEach((d, i) => { const mth = d.slice(0, 7); if (mth !== lm) { marks.push([i, d.slice(2, 7).replace("-", "/")]); lm = mth; } });
  }
  if (marks.length > 1 && x(marks[1][0]) - x(marks[0][0]) < 40) marks.shift();
  const every = Math.max(1, Math.ceil(marks.length / Math.max(2, Math.floor((W - m.l - m.r) / 64))));
  marks.forEach(([i, t], k) => { if (k % every) return; const e = svgEl("text", { x: x(i), y: H - 6, "text-anchor": "middle", "font-size": 11, fill: css("--text-3") }, parent); e.textContent = t; });
}

function lineChart(el, { dates, series, log = false, height = 260, yfmt = (v) => v.toFixed(2), zero = false }) {
  el.innerHTML = "";
  const W = Math.max(300, el.clientWidth || 800), H = height, m = { l: 50, r: 10, t: 10, b: 24 };
  const n = dates.length; if (!n) return;
  const step = Math.max(1, Math.ceil(n / 1200)); const idx = [];
  for (let i = 0; i < n; i += step) idx.push(i); if (idx[idx.length - 1] !== n - 1) idx.push(n - 1);
  const tf = log ? (v) => Math.log(Math.max(v, 1e-9)) : (v) => v;
  let lo = Infinity, hi = -Infinity;
  for (const s of series) for (const i of idx) { const v = s.values[i]; if (!isNum(v)) continue; const t = tf(v); lo = Math.min(lo, t); hi = Math.max(hi, t); }
  if (zero) { hi = Math.max(hi, 0); lo = Math.min(lo, 0); }
  if (!(hi > lo)) { hi = lo + 1; }
  const pad = (hi - lo) * 0.06; hi += pad; if (!zero) lo -= pad;
  const x = (i) => m.l + (i / Math.max(1, n - 1)) * (W - m.l - m.r);
  const y = (v) => m.t + (1 - (tf(v) - lo) / (hi - lo)) * (H - m.t - m.b);
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}` });
  const grid = css("--sep"), muted = css("--text-3");
  for (let k = 0; k <= 4; k++) {
    const tv = lo + (hi - lo) * (k / 4), yy = m.t + (1 - k / 4) * (H - m.t - m.b);
    svgEl("line", { x1: m.l, x2: W - m.r, y1: yy, y2: yy, stroke: grid, "stroke-width": 1 }, svg);
    svgEl("text", { x: m.l - 8, y: yy + 4, "text-anchor": "end", "font-size": 11, fill: muted }, svg).textContent = yfmt(log ? Math.exp(tv) : tv);
  }
  yearTicks(dates, x, W, m, svg, H);
  for (const s of series) {
    let d = "", pen = false;
    for (const i of idx) { const v = s.values[i]; if (!isNum(v)) { pen = false; continue; } d += `${pen ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`; pen = true; }
    if (s.fill) svgEl("path", { d: `${d}L${x(n - 1)},${y(0)}L${x(0)},${y(0)}Z`, fill: s.color, opacity: 0.14 }, svg);
    svgEl("path", { d, fill: "none", stroke: s.color, "stroke-width": s.width || 2, "stroke-linejoin": "round", "stroke-linecap": "round" }, svg);
  }
  hover(el, svg, W, H, m, n, x, (i) => `<div class="d">${dates[i]}</div>` + series.map((s) =>
    `<div><span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${s.color};margin-right:6px"></span>${esc(s.name)}　<b>${isNum(s.values[i]) ? yfmt(s.values[i]) : "—"}</b></div>`).join(""),
    series.map((s) => ({ color: s.color, y: (i) => (isNum(s.values[i]) ? y(s.values[i]) : null) })));
}

function hover(el, svg, W, H, m, n, x, html, dots = []) {
  const vline = svgEl("line", { y1: m.t, y2: H - m.b, stroke: css("--text-3"), "stroke-width": 1, "stroke-dasharray": "3 3", visibility: "hidden" }, svg);
  const ds = dots.map((d) => svgEl("circle", { r: 4, fill: d.color, stroke: css("--surface"), "stroke-width": 2, visibility: "hidden" }, svg));
  const hit = svgEl("rect", { x: m.l, y: m.t, width: W - m.l - m.r, height: H - m.t - m.b, fill: "transparent" }, svg);
  el.appendChild(svg);
  const tip = document.createElement("div"); tip.className = "tip"; el.appendChild(tip);
  const show = (evt) => {
    const rect = svg.getBoundingClientRect();
    const px = ((evt.clientX - rect.left) / rect.width) * W;
    const i = Math.min(n - 1, Math.max(0, Math.round(((px - m.l) / (W - m.l - m.r)) * (n - 1))));
    vline.setAttribute("x1", x(i)); vline.setAttribute("x2", x(i)); vline.setAttribute("visibility", "visible");
    dots.forEach((d, k) => { const yy = d.y(i); if (yy === null) { ds[k].setAttribute("visibility", "hidden"); return; } ds[k].setAttribute("cx", x(i)); ds[k].setAttribute("cy", yy); ds[k].setAttribute("visibility", "visible"); });
    tip.innerHTML = html(i); tip.style.display = "block";
    const left = (x(i) / W) * rect.width;
    tip.style.left = `${left + 14 + tip.offsetWidth > rect.width ? left - tip.offsetWidth - 14 : left + 14}px`;
    tip.style.top = "8px";
  };
  const hide = () => { vline.setAttribute("visibility", "hidden"); ds.forEach((d) => d.setAttribute("visibility", "hidden")); tip.style.display = "none"; };
  hit.addEventListener("mousemove", show); hit.addEventListener("mouseleave", hide);
  hit.addEventListener("touchmove", (e) => { show(e.touches[0]); e.preventDefault(); }, { passive: false });
  hit.addEventListener("touchend", hide);
}

// ================================================================ 图表：K 线
function kChart(el, d) {
  el.innerHTML = "";
  const n = d.dates.length; if (!n) return;
  const W = Math.max(300, el.clientWidth || 900);
  const m = { l: 56, r: 10, t: 12, b: 24 };
  const IS = d.ind ? store.get("dInd", "all") : "off", PAN = IS === "all" ? ["macd", "rsi", "kdj"] : ["macd", "rsi", "kdj"].includes(IS) ? [IS] : [], hI = 90;
  const hP = 300, hPos = 14, hVol = 60, hSub = d.sub ? 80 : 0, gap = 10;
  const H = m.t + hP + gap + hPos + gap + hVol + (hSub ? gap + hSub : 0) + PAN.length * (gap + hI) + m.b;
  const up = css("--up"), down = css("--down"), grid = css("--sep"), muted = css("--text-3");
  const pal = [css("--orange"), css("--indigo"), css("--teal"), css("--pink")];
  const x = (i) => m.l + ((i + 0.5) / n) * (W - m.l - m.r);
  const bw = Math.max(1, ((W - m.l - m.r) / n) * 0.7);
  const candles = n <= 300;

  let lo = Infinity, hi = -Infinity;
  for (let i = 0; i < n; i++) {
    const l = candles ? d.low[i] : d.close[i], h = candles ? d.high[i] : d.close[i];
    if (isNum(l)) lo = Math.min(lo, l); if (isNum(h)) hi = Math.max(hi, h);
    for (const L of d.lines) { const v = L.values[i]; if (isNum(v)) { lo = Math.min(lo, v); hi = Math.max(hi, v); } }
  }
  const pad = (hi - lo) * 0.06; lo -= pad; hi += pad;
  const top = m.t;
  const y = (v) => top + (1 - (v - lo) / (hi - lo)) * hP;
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}` });

  for (let k = 0; k <= 4; k++) {
    const v = lo + (hi - lo) * (k / 4), yy = y(v);
    svgEl("line", { x1: m.l, x2: W - m.r, y1: yy, y2: yy, stroke: grid, "stroke-width": 1 }, svg);
    svgEl("text", { x: m.l - 8, y: yy + 4, "text-anchor": "end", "font-size": 11, fill: muted }, svg).textContent = v.toFixed(v < 10 ? 2 : 1);
  }
  // 价格
  if (candles) {
    for (let i = 0; i < n; i++) {
      const o = d.open[i], c = d.close[i], h = d.high[i], l = d.low[i]; if (!isNum(o) || !isNum(c)) continue;
      const col = c >= o ? up : down;
      svgEl("line", { x1: x(i), x2: x(i), y1: y(h), y2: y(l), stroke: col, "stroke-width": 1 }, svg);
      svgEl("rect", { x: x(i) - bw / 2, y: y(Math.max(o, c)), width: bw, height: Math.max(1, Math.abs(y(o) - y(c))), fill: col, rx: Math.min(1.5, bw / 3) }, svg);
    }
  } else {
    let p = ""; for (let i = 0; i < n; i++) if (isNum(d.close[i])) p += `${p ? "L" : "M"}${x(i).toFixed(1)},${y(d.close[i]).toFixed(1)}`;
    svgEl("path", { d: `${p}L${x(n - 1)},${top + hP}L${x(0)},${top + hP}Z`, fill: css("--blue"), opacity: 0.08 }, svg);
    svgEl("path", { d: p, fill: "none", stroke: css("--text-2"), "stroke-width": 1.4 }, svg);
  }
  if (IS !== "off" && d.ind) {                    // 布林带 (20, 2)：上轨、中轨、下轨
    const bl = (arr, col, dash) => { let p = "", pen = false; for (let i = 0; i < n; i++) { const v = arr[i]; if (!isNum(v)) { pen = false; continue; } p += `${pen ? "L" : "M"}${x(i).toFixed(1)},${Math.max(top, Math.min(top + hP, y(v))).toFixed(1)}`; pen = true; } svgEl("path", { d: p, fill: "none", stroke: col, "stroke-width": 1, "stroke-dasharray": dash || "none", opacity: 0.8 }, svg); };
    bl(d.ind.boll.up, css("--indigo"), "4 3"); bl(d.ind.boll.low, css("--indigo"), "4 3"); bl(d.ind.boll.mid, css("--orange") || "#f90");
  }
  d.lines.forEach((L, k) => {
    let p = "", pen = false;
    for (let i = 0; i < n; i++) { const v = L.values[i]; if (!isNum(v)) { pen = false; continue; } p += `${pen ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`; pen = true; }
    svgEl("path", { d: p, fill: "none", stroke: L.dash ? css("--red") : pal[k % pal.length], "stroke-width": 1.5, "stroke-dasharray": L.dash ? "4 3" : "none", opacity: 0.95 }, svg);
  });
  // 买卖点
  const pos = new Map(d.dates.map((dt, i) => [dt, i]));
  const mark = (i, price, buy) => {
    const cx = x(i), cy = buy ? y(isNum(d.low[i]) ? d.low[i] : price) + 14 : y(isNum(d.high[i]) ? d.high[i] : price) - 14;
    const col = buy ? css("--blue") : css("--orange");
    svgEl("circle", { cx, cy, r: 8, fill: col, stroke: css("--surface"), "stroke-width": 1.5 }, svg);
    svgEl("text", { x: cx, y: cy + 3.5, "text-anchor": "middle", "font-size": 9.5, "font-weight": 700, fill: "#fff" }, svg).textContent = buy ? "买" : "卖";
  };
  for (const t of d.trades) {
    if (pos.has(t.entry)) mark(pos.get(t.entry), t.entry_px, true);
    if (t.exit && pos.has(t.exit)) mark(pos.get(t.exit), t.exit_px, false);
  }
  // 持仓条
  const yPos = top + hP + gap;
  svgEl("text", { x: m.l - 8, y: yPos + 10, "text-anchor": "end", "font-size": 10, fill: muted }, svg).textContent = "仓位";
  for (let i = 0; i < n; i++) {
    const w = d.position[i]; if (!isNum(w) || w <= 0) continue;
    svgEl("rect", { x: x(i) - (W - m.l - m.r) / n / 2, y: yPos, width: (W - m.l - m.r) / n + 0.5, height: hPos, fill: css("--blue"), opacity: 0.15 + 0.75 * Math.min(1, w) }, svg);
  }
  // 成交量
  const yVol = yPos + hPos + gap;
  let vmax = 0; for (const v of d.volume) if (isNum(v)) vmax = Math.max(vmax, v);
  svgEl("text", { x: m.l - 8, y: yVol + 10, "text-anchor": "end", "font-size": 10, fill: muted }, svg).textContent = "成交量";
  for (let i = 0; i < n; i++) {
    const v = d.volume[i]; if (!isNum(v) || !vmax) continue;
    const hgt = (v / vmax) * hVol;
    svgEl("rect", { x: x(i) - Math.max(0.5, bw / 2), y: yVol + hVol - hgt, width: Math.max(1, bw), height: hgt, fill: d.close[i] >= d.open[i] ? up : down, opacity: 0.45 }, svg);
  }
  // 副图
  let ySub = 0, ys = null;
  if (d.sub) {
    ySub = yVol + hVol + gap;
    const [a, b] = d.sub.range;
    ys = (v) => ySub + (1 - (v - a) / (b - a)) * hSub;
    svgEl("rect", { x: m.l, y: ySub, width: W - m.l - m.r, height: hSub, fill: css("--fill-2"), rx: 6 }, svg);
    for (const lv of d.sub.levels) {
      svgEl("line", { x1: m.l, x2: W - m.r, y1: ys(lv), y2: ys(lv), stroke: muted, "stroke-dasharray": "3 3", "stroke-width": 1 }, svg);
      svgEl("text", { x: m.l - 8, y: ys(lv) + 4, "text-anchor": "end", "font-size": 10, fill: muted }, svg).textContent = lv;
    }
    let p = "", pen = false;
    for (let i = 0; i < n; i++) { const v = d.sub.values[i]; if (!isNum(v)) { pen = false; continue; } p += `${pen ? "L" : "M"}${x(i).toFixed(1)},${ys(Math.min(b, Math.max(a, v))).toFixed(1)}`; pen = true; }
    svgEl("path", { d: p, fill: "none", stroke: css("--indigo"), "stroke-width": 1.4 }, svg);
    svgEl("text", { x: m.l + 8, y: ySub + 14, "font-size": 11, fill: muted }, svg).textContent = d.sub.name;
  }
  let yb = (d.sub ? ySub + hSub : yVol + hVol);
  const pline = (arr, f, col, w) => { let p = "", pen = false; for (let i = 0; i < n; i++) { const v = arr[i]; if (!isNum(v)) { pen = false; continue; } p += `${pen ? "L" : "M"}${x(i).toFixed(1)},${f(v).toFixed(1)}`; pen = true; } svgEl("path", { d: p, fill: "none", stroke: col, "stroke-width": w || 1.2 }, svg); };
  PAN.forEach((k) => {                            // MACD / RSI / KDJ 副图
    const yT = yb + gap; yb = yT + hI;
    svgEl("rect", { x: m.l, y: yT, width: W - m.l - m.r, height: hI, fill: css("--fill-2"), rx: 6 }, svg);
    let f, nm, lv = [];
    if (k === "macd") {
      const A = [...d.ind.macd.dif, ...d.ind.macd.dea, ...d.ind.macd.hist].filter(isNum), mx = Math.max(...A.map(Math.abs), 1e-9);
      f = (v) => yT + (1 - (v + mx) / (2 * mx)) * hI; lv = [0]; nm = "MACD (12, 26, 9)　DIF · DEA · 柱";
      for (let i = 0; i < n; i++) { const v = d.ind.macd.hist[i]; if (isNum(v)) svgEl("rect", { x: x(i) - Math.max(0.5, bw / 2), y: Math.min(f(0), f(v)), width: Math.max(1, bw), height: Math.abs(f(v) - f(0)), fill: v >= 0 ? up : down, opacity: 0.6 }, svg); }
      pline(d.ind.macd.dif, f, css("--indigo")); pline(d.ind.macd.dea, f, css("--orange") || "#f90");
    } else if (k === "rsi") {
      f = (v) => yT + (1 - v / 100) * hI; lv = [30, 70]; nm = "RSI (14)";
      pline(d.ind.rsi, f, css("--indigo"), 1.4);
    } else {
      const A = [...d.ind.kdj.k, ...d.ind.kdj.d, ...d.ind.kdj.j].filter(isNum), a = Math.min(0, ...A), b = Math.max(100, ...A);
      f = (v) => yT + (1 - (v - a) / (b - a)) * hI; lv = [20, 80]; nm = "KDJ (9, 3, 3)　K · D · J";
      pline(d.ind.kdj.k, f, css("--indigo")); pline(d.ind.kdj.d, f, css("--orange") || "#f90"); pline(d.ind.kdj.j, f, css("--purple") || "#a5a");
    }
    for (const v of lv) { svgEl("line", { x1: m.l, x2: W - m.r, y1: f(v), y2: f(v), stroke: muted, "stroke-dasharray": "3 3", "stroke-width": 1 }, svg); svgEl("text", { x: m.l - 8, y: f(v) + 4, "text-anchor": "end", "font-size": 10, fill: muted }, svg).textContent = v; }
    svgEl("text", { x: m.l + 8, y: yT + 14, "font-size": 11, fill: muted }, svg).textContent = nm;
  });
  yearTicks(d.dates, x, W, m, svg, H);
  // 各图之间的深色分割条（价格 / 持仓 / 成交量 / 副图 / MACD·RSI·KDJ）
  if (document.documentElement.getAttribute("data-skin") === "term") {
    const tops = [yPos, yVol]; if (d.sub) tops.push(ySub);
    let yy = d.sub ? ySub + hSub : yVol + hVol; PAN.forEach(() => { tops.push(yy + gap); yy += gap + hI; });
    tops.forEach((t) => svgEl("rect", { x: 0, y: t - gap / 2 - 1, width: W, height: 2, fill: "#7f8ca1", opacity: 0.85 }, svg));
  }

  const mm = { l: m.l, r: m.r, t: m.t, b: m.b };
  hover(el, svg, W, H, mm, n, x, (i) => {
    const chg = i > 0 && isNum(d.close[i - 1]) ? (d.close[i] / d.close[i - 1] - 1) * 100 : null;
    let h = `<div class="d">${d.dates[i]}</div>`;
    h += `<div>开 <b>${fmt(d.open[i])}</b>　高 <b>${fmt(d.high[i])}</b></div><div>低 <b>${fmt(d.low[i])}</b>　收 <b class="${cls(chg)}">${fmt(d.close[i])}</b> <span class="${cls(chg)}">${pct(chg, 2)}</span></div>`;
    d.lines.forEach((L, k) => { if (isNum(L.values[i])) h += `<div><span style="color:${L.dash ? css("--red") : pal[k % pal.length]}">●</span> ${esc(L.name)} <b>${fmt(L.values[i])}</b></div>`; });
    if (d.sub && isNum(d.sub.values[i])) h += `<div>${esc(d.sub.name)} <b>${fmt(d.sub.values[i], 1)}</b></div>`;
    if (d.ind) {
      const I = d.ind, f2 = (v) => (isNum(v) ? fmt(v, 2) : "—");
      if (IS !== "off") h += `<div class="dim">布林带 ${f2(I.boll.low[i])} / ${f2(I.boll.mid[i])} / ${f2(I.boll.up[i])}</div>`;
      if (PAN.includes("macd")) h += `<div class="dim">MACD DIF ${f2(I.macd.dif[i])} DEA ${f2(I.macd.dea[i])}</div>`;
      if (PAN.includes("rsi")) h += `<div class="dim">RSI(14) ${f2(I.rsi[i])}</div>`;
      if (PAN.includes("kdj")) h += `<div class="dim">KDJ ${f2(I.kdj.k[i])} / ${f2(I.kdj.d[i])} / ${f2(I.kdj.j[i])}</div>`;
    }
    h += `<div>仓位 <b>${fmt((d.position[i] || 0) * 100, 0, "%")}</b></div>`;
    return h;
  }, [{ color: css("--text"), y: (i) => (isNum(d.close[i]) ? y(d.close[i]) : null) }]);
}

// ================================================================ 信号核对（只读）
function renderBoard(d) {
  const b = d.board || {};
  const pc = (v, dg = 1) => (isNum(v) ? `${(v * 100).toFixed(dg)}%` : "—");
  const sg = (v, dg = 2) => (isNum(v) ? `${v > 0 ? "+" : ""}${(v * 100).toFixed(dg)}%` : "—");
  const cl = (v) => (v > 0 ? "up" : v < 0 ? "down" : "");
  const label = (k) => (S.meta && S.meta.strategies && S.meta.strategies[k] ? S.meta.strategies[k].label : (d.labels || {})[k] || k);
  const fam = (k) => (d.families || {})[k] || "正式";
  const mp = d.main_pick || {};
  const pr = d.promotion || {};
  const ps = pr.strategies || {};
  const gate = pr.gate || {};
  const main = mp.main || d.main;
  const ci = (x) => (isNum(x.hit_lo) ? `<small class="dim"> [${pc(x.hit_lo, 0)}–${pc(x.hit_hi, 0)}]</small>` : "");
  const tag = (k) => (k === main ? ` <small class="up">主策略${mp.default ? "（默认策略，未经晋升检验）" : ""}</small>` : "");
  // 排名：样本够的按 5 日得分从高到低，样本不足的排在最后且不给名次
  const order = Object.entries(b).sort((x, y) => {
    const ax = x[1].h5.enough ? 1 : 0, ay = y[1].h5.enough ? 1 : 0;
    return ay - ax || (y[1].h5.score ?? -9) - (x[1].h5.score ?? -9);
  });
  const rows = order.map(([k, v]) => {
    const h5 = v.h5, h1 = v.h1, last = (h5.daily || []).slice(-1)[0] || (h1.daily || []).slice(-1)[0] || {};
    const rk = fam(k) === "对照" ? '<span class="dim">对照</span>' : h5.enough ? `#${h5.rank}` : '<span class="dim">数据不足</span>';
    return `<tr class="${h5.enough ? "" : "dim"}"><td>${rk}</td><td>${esc(label(k))}${tag(k)}<small class="dim"> ${esc(fam(k))}</small></td>
      <td>${h5.n}<small> / ${h5.days} 天</small></td><td>${pc(h5.hit, 0)}${ci(h5)}</td><td class="${cl(h5.score)}">${sg(h5.score)}</td>
      <td>${pc(h1.hit, 0)}</td><td class="${cl(h1.score)}">${sg(h1.score)}</td>
      <td class="${cl(last.d_hit)}">${sg(last.d_hit, 1)}</td></tr>`;
  }).join("");
  const nTxt = (x) => `${x.weeks ?? 0}/${gate.min_weeks || 12} 周 · ${x.n ?? 0}/${gate.min_n || 500} 条`;
  const left = (x) => (x.weeks_left == null ? (x.weeks ? '<span class="dim">现有数据下追不上（优势 ≤ 0）</span>' : "—") : x.weeks_left === 0 ? "已达标" : x.weeks_left > 104 ? '<span class="dim">超过 2 年（现有优势太小）</span>' : `至少 ${x.weeks_left} 周`);
  const mde = (x) => (isNum(x.mde) ? `${sg(x.mde)}<small class="dim"> → 12 周 ${sg(x.mde_12w)} → 26 周 ${sg(x.mde_26w)}</small>` : x.mde_possible === false ? '<span class="dim">对照数不够</span>' : "—");
  const prRows = Object.entries(ps).filter(([k]) => fam(k) !== "对照").sort((x, y) => (y[1].advantage ?? -9) - (x[1].advantage ?? -9)).map(([k, x]) => {
    const noise = x.noise ? `<span class="${x.noise === "显著高于随机" ? "up" : x.noise === "显著低于随机" ? "down" : "dim"}">${esc(x.noise)}</span>` : "—";
    return `<tr class="${x.data_ok ? "" : "dim"}"><td>${esc(label(k))}${tag(k)}</td><td>${nTxt(x)}</td><td>${sg(x.stat)}</td>
      <td class="${cl(x.advantage)}">${sg(x.advantage)}</td><td>${noise}${isNum(x.ctrl_lo) ? `<small class="dim"> 随机 95% 范围 ${sg(x.ctrl_lo)} ~ ${sg(x.ctrl_hi)}</small>` : ""}</td>
      <td class="${cl(x.hit_diff)}">${sg(x.hit_diff, 1)}</td><td>${isNum(x.p) ? x.p.toFixed(4) : "—"}</td>
      <td>${esc(x.status || "—")}</td><td>${left(x)}</td><td>${mde(x)}</td></tr>`;
  }).join("");
  // 主策略：晋升前 / 晋升后分开
  const rec = mp.record || {};
  const seg = (s, title, note, formal) => {
    if (!s) return "";
    const vc = s.vs_control || {};
    return `<tr class="${formal ? "" : "dim"}"><td><b>${title}</b><div class="dim"><small>${note}</small></div></td><td>${s.n ?? 0} 条 / ${s.days ?? 0} 天</td>
      <td>${s.n ? pc(s.hit, 1) + ci(s) : "—"}</td><td class="${cl(s.score)}">${s.n ? sg(s.score) : "—"}</td>
      <td>${vc.weeks ? `${vc.weeks} 周：比随机 <span class="${cl(vc.advantage)}">${sg(vc.advantage)}</span>，${esc(vc.noise || "")}（p=${isNum(vc.p) ? vc.p.toFixed(3) : "—"}）` : '<span class="dim">还没有完整交易周</span>'}</td></tr>`;
  };
  const recCard = rec.strategy ? `<h2>主策略成绩：晋升前 / 晋升后 <small>只有晋升后的才算正式成绩</small></h2>
    <div class="card tbl"><table><thead><tr><th>${esc(label(rec.strategy))}${mp.default ? "（默认策略，未经晋升检验）" : ""} · 晋升日 ${esc(rec.promoted_on || "—")}</th><th>5 日信号</th><th>命中率 [95%]</th><th>每条得分</th><th>对随机对照（按周）</th></tr></thead><tbody>
      ${seg(rec.pre, "晋升前", mp.default ? "上线检验以前的历史，不算战绩" : "选中它的依据（样本内），不算战绩", false)}
      ${seg(rec.post, "晋升后 · 正式成绩", mp.default ? "从检验上线日之后的信号日起算" : "晋升日之后的信号日", true)}
    </tbody></table></div>` : "";
  const days = [...new Set(Object.values(b).flatMap((v) => (v.h1.daily || []).map((x) => x.day)))].sort();
  const keys = Object.keys(b);
  const hist = days.slice(-15).reverse().map((day) => `<tr><td>${esc(day)}</td>${keys.map((k) => {
    const x = (b[k].h1.daily || []).find((y) => y.day === day);
    return x ? `<td title="当天 ${x.n} 条，命中 ${pc(x.hit, 0)}；累计命中 ${pc(x.cum_hit, 1)}"><span class="${cl(x.d_hit)}">${sg(x.d_hit, 1)}</span> <small class="dim">${pc(x.cum_hit, 0)}</small></td>` : "<td>—</td>";
  }).join("")}</tr>`).join("");
  const none = !pr.any_passed;
  return `<h2>主策略晋升检验 <small>${esc(pr.message || "尚无策略通过晋升检验")}</small></h2>
    <div class="card"><b>主策略：${esc(label(main))}</b>${mp.default ? ' <span class="tag">默认策略，未经晋升检验</span>' : ` <span class="tag">晋升于 ${esc(mp.promoted_on || "")}</span>`}
      <div class="${none ? "down" : "up"}" style="margin-top:6px"><b>${none ? "尚无策略通过晋升检验" : esc(pr.message)}</b>${pr.error ? ` <small class="dim">（检验没有运行：${esc(pr.error)}）</small>` : ""}</div>
      <div class="dim" style="margin-top:4px">${esc(mp.reason || "")}</div>
      <div class="dim" style="margin-top:4px">晋升门槛（缺一不可）：≥${gate.min_weeks || 12} 个完整交易周、≥${gate.min_n || 500} 条已走满 5 日的信号；每个策略都和 ${pr.k || 1000} 个“匹配的随机对照”比
        （每天保持它的买卖条数不变，把信号随机分配给股票）；按 ${pr.m || 25} 个策略做多重检验修正（Holm，第一步要求经验 p ≤ ${isNum(pr.alpha) && pr.m ? (pr.alpha / pr.m).toFixed(3) : "0.002"}）；
        连续 ${gate.streak || 4} 个周检查点都通过；现任满 ${Math.round((gate.tenure_days || 84) / 7)} 周任期；相对现任还要多 ${pc(gate.beat_gain || 0.003, 1)}（除非现任已不通过）。满足前不会切换主策略。
        候选（影子）策略只参与核对，不能当主策略。当前已有 ${pr.complete_weeks ?? 0} 个完整交易周。</div></div>
    ${recCard}
    <h2>各策略准确率 <small>命中率 = 跑赢全市场的信号占比，方括号是 95% 置信区间（把信号当独立样本，偏窄）；得分 = 每条信号平均比全市场多赚多少（扣费后）。样本不足（&lt;${gate.board_min_n || 100} 条或 &lt;${gate.board_min_days || 10} 个信号日）的不排名</small></h2>
    <div class="card tbl"><table><thead><tr><th>名次</th><th>策略</th><th>5 日条数</th><th>5 日命中率 [95% 区间]</th><th>5 日得分</th><th>次日命中率</th><th>次日得分</th><th>最近一天命中率变化</th></tr></thead><tbody>${rows || '<tr><td colspan="8" class="dim">等第一批信号走满一个交易日。</td></tr>'}</tbody></table></div>
    <h2>和随机对照比 <small>按交易周聚合；“比随机高”= 周均得分 − 1000 个对照的平均；“最小可检测效应”= 在当前周数下要比随机至少高多少才可能通过多重检验，按每周独立、效应不变外推到 12 / 26 周（近似，不是保证）</small></h2>
    <div class="card tbl"><table><thead><tr><th>策略</th><th>数据量（周 · 条）</th><th>周均得分</th><th>比随机高</th><th>是否在噪声内</th><th>命中率比随机高</th><th>经验 p</th><th>晋升检验</th><th>距晋升还差</th><th>最小可检测效应</th></tr></thead><tbody>${prRows || '<tr><td colspan="10" class="dim">还没有走满 5 日的信号，也没有完整交易周。</td></tr>'}</tbody></table></div>
    <div class="dim" style="margin:6px 0">“是否在噪声内”用对照的 2.5%~97.5% 分位，没做多重检验修正；是否够晋升看“晋升检验”一列。对照不保留“同一只股票连续多天被持有”的相关性，所以噪声范围可能偏窄。</div>
    ${hist ? `<h2>每天准确率提升了多少 <small>次日口径：当天累计准确率的变化（小字是累计准确率）</small></h2><div class="card tbl"><table><thead><tr><th>信号日</th>${keys.map((k) => `<th>${esc(label(k))}</th>`).join("")}</tr></thead><tbody>${hist}</tbody></table></div>` : ""}`;
}

function renderTune(t) {
  if (!t) return "";
  if (t.error) return `<h2>自动调参</h2><div class="card dim">本次没运行：${esc(t.error)}</div>`;
  const pc = (v, dg = 2) => (isNum(v) ? `${(v * 100).toFixed(dg)}%` : "—");
  const a = t.active_after || {};
  const rows = (t.candidates || []).slice().sort((x, y) => (y.t ?? -99) - (x.t ?? -99)).map((c) =>
    `<tr class="${c.banned ? "dim" : ""}"><td>阈值 ${pc(c.params.thr, 0)} · 小仓位线 ${pc(c.params.mt, 0)}</td><td class="${c.gain > 0 ? "up" : c.gain < 0 ? "down" : ""}">${pc(c.gain)}</td><td>${isNum(c.t) ? c.t.toFixed(1) : "—"}</td><td>${c.weeks ?? c.days}</td><td>${isNum(c.coverage) ? pc(c.coverage, 0) : "—"}</td></tr>`).join("");
  const ser = (t.series || []).map((x) => `<tr><td>${esc(x.day)}</td><td>${pc(x.active)}</td><td>${pc(x.default)}</td><td>${x.n}</td></tr>`).join("");
  return `<h2>自动调参 <small>每天并行比较 12 组参数，但完整交易周不足 20 周之前参数固定不动；之后换参数要按周 t 通过多重检验修正。只调这两个参数，不改策略公式</small></h2>
    <div class="card"><div class="kv"><span>当前：阈值 <b>${pc(a.thr, 0)}</b> · 小仓位线 <b>${pc(a.mt, 0)}</b></span> <span>可核对 ${t.scorable_days} 天 · ${t.weeks ?? 0}/${t.freeze_weeks ?? 20} 个完整交易周${(t.weeks ?? 0) < (t.freeze_weeks ?? 20) ? "（冻结中）" : ""}</span></div>
    <div style="margin-top:6px">${esc(t.why || "")}</div>${t.watch ? `<div class="dim" style="margin-top:4px">换参数后 ${t.watch.post_days} 个新信号日，比旧参数 ${pc(t.watch.gain_vs_prev)}</div>` : ""}</div>
    <div class="card tbl"><table><thead><tr><th>备选参数（每天并行比较）</th><th>比当前每条信号好</th><th>按周 t</th><th>周数</th><th>信号数占比</th></tr></thead><tbody>${rows}</tbody></table></div>
    ${ser ? `<div class="card tbl"><table><thead><tr><th>信号日</th><th>当前参数每条信号得分</th><th>默认参数</th><th>信号数</th></tr></thead><tbody>${ser}</tbody></table></div>` : ""}`;
}

async function loadReview() {
  const box = $("reviewBody"), meta = $("reviewMeta");
  box.innerHTML = '<div class="skeleton"></div>';
  const d = await api("/api/ledger");
  if (!d || d.error || d.empty) { box.innerHTML = `<div class="card dim">${esc((d && (d.message || d.error)) || "暂无数据")}</div>`; meta.textContent = ""; return; }
  const pct = (v, dg = 1) => (isNum(v) ? `${(v * 100).toFixed(dg)}%` : "—");
  const cls = (v) => (v > 0 ? "up" : v < 0 ? "down" : "");
  meta.textContent = `已记录 ${d.signal_days.length} 个信号日，已核对 ${d.evaluated_days.length} 个`;
  const notes = (d.notes || []).map((n) => `<li>${esc(n)}</li>`).join("");
  const hName = { 1: "次日", 5: "5 日", 10: "10 日", 20: "20 日" };
  const precision = Object.entries(d.precision || {}).map(([s, rows]) => {
    const body = rows.map((p) => `<tr class="${p.enough ? "" : "dim"}"><td>${esc(p.action)}</td><td>${hName[p.h] || p.h}</td><td>${p.n}${p.untradable ? ` <small>(另有 ${p.untradable} 条开盘即涨/跌停)</small>` : ""}</td><td>${pct(p.hit_raw, 0)}</td><td>${pct(p.hit_excess, 0)}</td><td class="${cls(p.mean_ret)}">${pct(p.mean_ret, 2)}</td><td class="${cls(p.mean_excess)}">${pct(p.mean_excess, 2)}</td><td title="命中率 95% 区间 ${pct(p.hit_excess_lo, 0)}–${pct(p.hit_excess_hi, 0)}；中位数 ${pct(p.median_excess, 2)}；扣费后 ${pct(p.net_excess, 2)}">${esc(p.verdict || (p.enough ? "" : "样本不足"))}</td></tr>`).join("");
    return `<h2>${esc(s)}${s === d.main ? " <small>主策略</small>" : ""}</h2><div class="card tbl"><table><thead><tr><th>信号</th><th>看多久</th><th>条数</th><th>方向命中</th><th>跑赢全市场</th><th>平均涨跌</th><th>相对全市场</th><th>结论</th></tr></thead><tbody>${body}</tbody></table></div>`;
  }).join("");
  const rec = (d.recall || []).map((r) => `<div class="card" style="margin-bottom:10px"><b>${esc(r.day)}</b> 之后 ${r.h} 日涨幅居前的 ${r.winners} 只：${Object.entries(r.categories).map(([k, v]) => `${esc(k)} ${v}`).join("、")}；召回率 ${pct(r.recall, 0)}
    ${(r.missed || []).length ? `<div class="tbl" style="margin-top:8px"><table><thead><tr><th>股票</th><th>涨幅</th><th>当时为什么没买</th></tr></thead><tbody>${r.missed.map((m) => `<tr><td>${esc(m.name || "")} <small>${esc(m.code)}</small></td><td class="up">${pct(m.ret, 1)}</td><td>${esc(m.category)}</td></tr>`).join("")}</tbody></table></div>` : ""}</div>`).join("");
  const lvl = { ok: "up", warn: "down", info: "dim" };
  const refl = (d.reflection || []).map((r) => `<li class="${lvl[r.level] || ""}">${esc(r.text)}${r.suggestion ? ` <small class="dim">建议：${esc(r.suggestion)}</small>` : ""}</li>`).join("");
  box.innerHTML = `<div class="card"><ul class="list">${notes}</ul></div>
    <h2>每日反思 <small>只给建议，改策略要你同意</small></h2><div class="card"><ul class="list">${refl || "<li class=dim>暂无</li>"}</ul></div>
    ${renderBoard(d)}
    ${renderTune(d.tune)}
    <h2>漏掉的机会 <small>开盘涨停和连板短炒已剔除</small></h2>${rec || '<div class="card dim">今天开始记录当时的持仓状态，等走满 5 个交易日后才会有。</div>'}
    <h2>信号准确率</h2>${precision || '<div class="card dim">等第一批信号走满一个交易日。</div>'}
    <div class="dim" style="margin-top:10px">${(d.caveats || []).map(esc).join(" ")}</div>`;
}

// ================================================================ AI 账户（只读）
async function loadAi() {
  const box = $("aiBody"), meta = $("aiMeta");
  box.innerHTML = '<div class="skeleton"></div>';
  const d = await api("/api/sim");
  if (!d || d.error || d.empty || !(d.accounts || []).length) { box.innerHTML = `<div class="card dim">${esc((d && (d.message || d.error)) || "暂无数据")}</div>`; meta.textContent = ""; return; }
  const sign = (v) => (v > 0 ? "up" : v < 0 ? "down" : "");
  const money = (v) => (isNum(v) ? Number(v).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : "—");
  const sideTxt = (s) => (s === "buy" ? "买入" : "卖出");
  meta.textContent = d.fee_verified ? "" : "费率为默认值，待和真实账户核对";
  box.innerHTML = d.accounts.map((a) => {
    const pos = (a.positions || []).map((p) => { const v = p.close * p.qty, pnl = (p.close / p.cost - 1) * 100; return `<tr><td>${esc(p.name)} <small>${esc(p.code)}</small></td><td>${p.qty}</td><td>${fmt(p.cost)}</td><td>${fmt(p.close)}${p.stale ? "*" : ""}</td><td>${money(v)}</td><td class="${sign(pnl)}">${fmt(pnl, 2, "%")}</td></tr>`; }).join("");
    const pend = (a.pending || []).map((o) => `<tr><td>${esc(o.name)} <small>${esc(o.code)}</small></td><td>${sideTxt(o.side)}</td><td>${o.qty}</td><td>${esc(o.fill_day)}</td><td class="dim">${esc(o.reason)}</td></tr>`).join("");
    const skip = (a.skipped || []).slice(0, 60).map((o) => `<tr><td>${esc(o.name)} <small>${esc(o.code)}</small></td><td>${esc(o.signal_day)}</td><td class="dim">${esc(o.reason)}</td></tr>`).join("");
    const rej = (a.rejected || []).slice(0, 40).map((o) => `<tr><td>${esc(o.name)} <small>${esc(o.code)}</small></td><td>${sideTxt(o.side)}</td><td>${esc(o.fill_day)}</td><td class="dim">${esc(o.reason)}</td></tr>`).join("");
    const trd = (a.trades || []).map((t) => `<tr><td>${esc(t.day)}</td><td>${esc(t.name)} <small>${esc(t.code)}</small></td><td>${sideTxt(t.side)}</td><td>${t.qty}</td><td>${fmt(t.price)}</td><td>${money(t.amount)}</td><td title="佣金 ${t.commission} / 过户费 ${t.transfer} / 印花税 ${t.stamp}">${money(t.fee)}</td><td class="${sign(t.pnl)}">${isNum(t.pnl) ? money(t.pnl) : "—"}</td></tr>`).join("");
    const notes = (a.summaries || []).filter((s) => (s.lines || []).length).map((s) => `<div class="card" style="margin-bottom:10px"><b>${esc(s.day)}</b>${(s.lines || []).map((l) => `<div class="dim" style="margin-top:4px">${esc(l)}</div>`).join("")}</div>`).join("");
    const tbl = (h, rows, empty) => rows ? `<div class="card tbl"><table><thead><tr>${h.map((x) => `<th>${x}</th>`).join("")}</tr></thead><tbody>${rows}</tbody></table></div>` : `<div class="card dim">${empty}</div>`;
    return `<div class="card"><h2 style="margin-top:0">${esc(a.label)} <small>开始日 ${esc(a.created)} · 单只上限 ${Math.round(a.max_weight * 100)}% · 最多 ${a.max_positions} 只</small></h2>
      <div class="kv"><span>净值 <b>${money(a.value)}</b></span> <span>本金 ${money(a.initial)}</span> <span class="${sign(a.ret_pct)}">累计 ${fmt(a.ret_pct, 2, "%")}</span> <span>现金 ${money(a.cash)}</span></div></div>
      <h2>持仓</h2>${tbl(["股票", "股数", "成本", "最新收盘", "市值", "浮盈亏"], pos, "目前没有持仓。今天挂的买单要等下一个交易日开盘后才会成交。")}
      <h2>明天要成交的挂单 <small>成交价=下一交易日开盘价</small></h2>${tbl(["股票", "方向", "股数", "撮合日", "原因"], pend, "没有挂单。")}
      <h2>每日总结</h2>${notes || '<div class="card dim">第一次撮合之后才会有总结。</div>'}
      <h2>成交记录 <small>费用含佣金、过户费、印花税，悬停看明细</small></h2>${tbl(["日期", "股票", "方向", "股数", "价格", "金额", "费用", "盈亏"], trd, "还没有成交。")}
      <h2>没成交的订单 <small>涨停买不进、跌停卖不出、停牌、资金不足</small></h2>${tbl(["股票", "方向", "撮合日", "原因"], rej, "暂无。")}
      <h2>信号有、但没下单 <small>被规则挡掉，用来之后检验这些规则对不对</small></h2>${tbl(["股票", "信号日", "原因"], skip, "暂无。")}`;
  }).join("");
}

// ================================================================ 路由
// 只接受服务端严格布尔值；缺字段、请求失败均不擅自开启。
function personalMode() { return S.meta?.personal_mode === true; }
const PERSONAL_NAV = [["today", "今日"], ["trade", "信号"], ["market", "全市场"], ["watch", "自选"], ["lab", "回测"], ["holdings", "持仓"], ["monitor", "盯盘"], ["usmonitor", "美股盯盘"]];
const PERSONAL_PAGES = new Set([...PERSONAL_NAV.map(([page]) => page), "stock"]);
function personalHash(hash) {
  if (!personalMode()) return hash;
  const match = /^(#\/)([^/?]*)(.*)$/.exec(hash);
  if (!match) return "#/today";
  if (["review", "track"].includes(match[2])) return `#/trade${match[3]}`;
  return PERSONAL_PAGES.has(match[2]) ? hash : "#/today";
}
function setupPersonalNavigation() {
  const tabs = $("tabs");
  if (personalMode()) {
    document.documentElement.classList.add("personal-mode");
    tabs.replaceChildren(...PERSONAL_NAV.map(([page, label]) => {
      const link = document.createElement("a");
      link.href = `#/${page}`; link.dataset.p = page; link.classList.add("personal-nav");
      link.textContent = label; link.setAttribute("aria-label", label);
      return link;
    }));
    $("shareBar").hidden = true;
  }
  setupPersonalPages();
  // 未开启时保留原 DOM、更多菜单和所有标签；等待 meta 后才显示导航。
  tabs.hidden = false;
}
// 个人布局只移动现有节点；完整模式不调用，旧页面和计算函数保留。
const PERSONAL_HEADERS = {"Code":"代码", "Close":"收盘价", "1D %":"当日涨跌 %", "Name":"名称", "Quantity":"股数", "Date":"日期", "Value":"资产", "Action":"信号", "Days":"天数", "N":"样本数", "Hit":"方向命中", "Excess hit":"超额命中", "Return":"收益", "Excess":"超额收益", "Verdict":"结论", "Winners":"领先股票", "Recall":"召回率", "Strategy":"策略", "Target":"目标仓位", "Status":"状态", "Weeks":"周数", "Advantage":"相对优势", "Noise":"噪声判断", "p":"p", "5D %":"5日 %", "20D %":"20日 %", "60D %":"60日 %"};
for (const h of [5, 20, 60]) {
  PERSONAL_HEADERS[`${h}D N / Avg / Beat`] = `${h}日 样本 / 平均收益 / 跑赢基准`;
  PERSONAL_HEADERS[`${h}D N / Avg / Beat / Benchmark N`] = `${h}日 样本 / 平均收益 / 跑赢基准 / 可比样本`;
}
const PERSONAL = { cache: new Map(), epoch: 0, view: 0, section: "", track: "live" };
function pNode(tag, text, english) {
  const el = document.createElement(tag);
  if (english !== undefined) {
    el.classList.add("personal-copy"); el.dataset.zh = text; el.dataset.en = english;
    el.textContent = window.AI_I18N?.language === "en" ? english : text;
  } else if (text !== undefined) el.textContent = String(text ?? "—");
  return el;
}
function pRatio(v, digits = 1) { return isNum(v) ? fmt(v * 100, digits, "%") : "—"; }
function pCopy(zh, en) { return pNode("p", zh, en); }
function pState(zh, en) {
  const el = pCopy(zh, en); el.classList.add("personal-state"); el.setAttribute("role", "status"); return el;
}
// 只翻译已知术语；服务端名称、理由和错误仍按原文作为文本显示。
const PERSONAL_TERMS = {"买入":"Buy", "卖出":"Sell", "减仓":"Reduce", "加仓":"Add", "数据不足":"Insufficient data", "无信号":"No signal", "是":"Yes", "否":"No", "正常":"Normal", "流动性差":"Low liquidity", "流动性未知":"Unknown liquidity"};
const PERSONAL_TRADE_LABELS = {code:"Stock / code", close:"Close", d1:"1D change", avg_turnover_20d:"20D average turnover (CNY)", liquidity:"Liquidity", lot_cost:"Cost per lot (CNY)", action:"Signal", now:"Current weight", next:"Next target weight", reason:"Signal reason", total:"OOS total return", b_total:"OOS buy-and-hold return", beat_bh:"OOS beat buy and hold", maxdd:"OOS maximum drawdown", trades:"OOS completed trades", sharpe:"OOS excess Sharpe", flags:"Tradability"};
window.addEventListener("dashboard-language-change", () => {
  document.querySelectorAll(".personal-copy").forEach(el => {
    el.textContent = window.AI_I18N?.language === "en" ? el.dataset.en : el.dataset.zh;
  });
});
function pTable(headers, rows, decorate = () => {}) {
  const wrap = pNode("div"), table = pNode("table"), head = pNode("thead"), body = pNode("tbody"), tr = pNode("tr");
  wrap.classList.add("tbl"); wrap.tabIndex = 0; wrap.setAttribute("role", "region"); wrap.setAttribute("aria-label", "数据表 / Data table"); headers.forEach(h => tr.append(Array.isArray(h) ? pNode("th", ...h) : pNode("th", PERSONAL_HEADERS[h] || h, h))); head.append(tr);
  rows.forEach((row, i) => { const r = pNode("tr"); decorate(r, i); row.forEach(v => r.append(v?.nodeType ? v : pNode("td", v))); body.append(r); });
  table.append(head, body); wrap.append(table);
  if (!rows.length) wrap.append(pState("暂无数据", "No data"));
  return wrap;
}
function pFold(zh, en, ...nodes) {
  const el = pNode("details"); el.append(pNode("summary", zh, en), ...nodes); return el;
}
function pSection(id, zh, en) {
  const el = pNode("section"); el.id = id; el.classList.add("card"); el.append(pNode("h2", zh, en)); return el;
}
function pTradeRows(rows, table, sortable, options) {
  const heads = TRADE_COLUMNS.map(([key, label]) => {
    const th = pNode("th", label, PERSONAL_TRADE_LABELS[key]);
    if (sortable && !["reason", "flags"].includes(key)) {
      th.tabIndex = 0; th.classList.add("sort");
      th.setAttribute("aria-sort", TRADE.sort === key ? (TRADE.dir === 1 ? "ascending" : "descending") : "none");
      const act = () => { TRADE.dir = TRADE.sort === key ? -TRADE.dir : 1; TRADE.sort = key; TRADE.manualSort = true; renderTrade(); };
      th.addEventListener("click", act); th.addEventListener("keydown", e => { if (["Enter", " "].includes(e.key)) { e.preventDefault(); act(); } });
    }
    return th;
  });
  const head = pNode("thead"), tr = pNode("tr"), body = pNode("tbody");
  heads.forEach(h => tr.append(h)); head.append(tr);
  for (const r of rows) {
    const perf = r.performance || {}, row = pNode("tr"), name = pNode("td"), link = pNode("a", `${r.name} ${r.code}`);
    link.href = `#/stock/${encodeURIComponent(r.code)}`;
    link.addEventListener("click", () => store.set("dStrategy", $("tradeStrategy").value));
    name.append(link, pNode("small", r.last_date));
    const note = options.exception ? null : hLinkNote(r);   // 持仓联动标注：只加 DOM，不改 r
    if (note) { name.append(note.node); row.classList.add(note.cls); }
    row.append(name);
    [tradeNumber(r.close), options.exception ? "—" : pct(r.d1, 2), tradeNumber(r.avg_turnover_20d,"",0), r.liquidity,
      tradeNumber(r.lot_cost), r.action, tradePosition(r.now), tradeTargetText(r), r.reason,
      `${tradeNumber(perf.total,"%")} · ${perf.start || "—"} → ${perf.end || "—"}`, tradeNumber(perf.b_total,"%"),
      typeof perf.beat_bh === "boolean" ? (perf.beat_bh ? "是" : "否") : "数据不足", tradeNumber(perf.maxdd,"%"),
      tradeNumber(perf.trades,"",0), tradeNumber(perf.sharpe), (r.flags || []).join("；")].forEach(v => {
      const cell = pNode("td"); cell.append(pNode("span", v, Object.hasOwn(PERSONAL_TERMS, v) ? PERSONAL_TERMS[v] : v)); row.append(cell);
    });
    if (perf.small_sample) row.children[14].append(pNode("small", "样本太少，不可靠", "Small sample, unreliable"));
    body.append(row);
  }
  if (!rows.length) { const row = pNode("tr"), cell = pNode("td", "无匹配记录", "No matching records"); cell.colSpan = heads.length; row.append(cell); body.append(row); }
  table.classList.add("personal-signal-table"); table.setAttribute("role", "table");
  table.setAttribute("aria-label", "信号 / Signals");
  // 保留所有列、排序和导出；窄屏只改变同一份行数据的呈现。
  for (const row of body.children) {
    row.setAttribute("role", "row");
    if (!rows.length) continue;
    [...row.children].forEach((cell, i) => {
      cell.setAttribute("role", "cell");
      const [key, zh] = TRADE_COLUMNS[i];
      const label = pNode("span", zh, PERSONAL_TRADE_LABELS[key]); label.classList.add("personal-cell-label");
      cell.prepend(label);
    });
  }
  table.replaceChildren(head, body);
}
async function pTradeFollowups(request) {
  const strategy = $("tradeStrategy").value, date = $("tradeDate").value;
  const jobs = [["tradeTrackingBody", `/api/trade_list_tracking?strategy=${encodeURIComponent(strategy)}`]];
  if (pLocal()) jobs.push(["tradeMineBody", `/api/trade_list_mine?${new URLSearchParams({strategy, date})}`]);
  $("tradeMine").hidden = !pLocal();
  await Promise.all(jobs.map(async ([id, path]) => {
    const box = $(id);
    try {
      const d = await api(path);
      if (request !== TRADE.request) return;
      box.replaceChildren();
      if (d.error) throw new Error(d.error);
      if (id === "tradeMineBody") {
        const rows = d.rows || [];
        box.append(pCopy("来源：当前会话自选 / 模拟持仓，仅本机", "Source: current session watchlist / paper holdings, local only"),
          pCopy("600531 固定第一，其余卖出信号置顶。", "600531 is always first; other sell signals follow."),
          d.member_logged_in ? pCopy("已包含当前会员的模拟持仓。", "Current member paper holdings are included.")
            : pCopy("登录模拟交易会员后可查看当前账户的相关股票（关闭个人模式后登录）。", "Sign in to the paper trading account in full mode to view its related stocks."),
          pTable(["Code", "Name", ["相关来源", "Related source"], "Action", "Target"], rows.map(r => [r.code, r.name,
            pNode("td", [r.in_watchlist ? "自选" : "", r.in_member_positions ? "模拟持仓" : ""].filter(Boolean).join(" / "),
              [r.in_watchlist ? "Watchlist" : "", r.in_member_positions ? "Paper holdings" : ""].filter(Boolean).join(" / ")),
            r.action || "—", tradeTargetText(r)]), (row, i) => { if (rows[i].action === "卖出") row.classList.add("trade-mine-sell"); }));
      } else {
        box.append(pCopy("来源：发布后清单样本；5 / 20 / 60 日，卖出行是股票后续收益，不是卖出策略收益。", "Source: published-list samples; 5 / 20 / 60 days. Sell rows show subsequent stock returns, not selling-strategy returns."), pCopy("基准：沪深300ETF，未扣费用和滑点，信号不保证成交。", "Benchmark: CSI 300 ETF, excluding fees and slippage; signals do not guarantee fills."), pNode("p", d.note),
          pNode("p", `${d.since || "—"} → ${d.latest_price_date || "—"} · ${d.recorded_signals ?? "—"}`),
          pTable(["Action", "5D N / Avg / Beat / Benchmark N", "20D N / Avg / Beat / Benchmark N", "60D N / Avg / Beat / Benchmark N"], (d.groups || []).map(g => [g.action, ...[5,20,60].map(h => `${g[`n${h}`] ?? "—"} / ${tradeNumber(g[`avg${h}`],"%")} / ${tradeNumber(g[`beat${h}`],"%")} / ${g[`bench_n${h}`] ?? "—"}`)])));
        if (d.empty) box.append(pCopy("暂无上线样本，不自动回放", "No live samples; no automatic replay"));
      }
    } catch (e) { if (request === TRADE.request) box.replaceChildren(pState("区块暂不可用", "Section unavailable"), pNode("p", e.message)); }
  }));
}

function pInvalidate() { PERSONAL.epoch++; PERSONAL.cache.clear(); }
function pRequest(path) {
  const key = `${S.market}:${path}`;
  if (!PERSONAL.cache.has(key)) {
    const promise = api(path).then(d => {
      if (d?.error && PERSONAL.cache.get(key) === promise) PERSONAL.cache.delete(key);
      return d;
    }).catch(e => { if (PERSONAL.cache.get(key) === promise) PERSONAL.cache.delete(key); throw e; });
    PERSONAL.cache.set(key, promise);
  }
  return PERSONAL.cache.get(key);
}
function pList(strategy, date = "") {
  return pRequest(`/api/trade_list?${new URLSearchParams({strategy, date})}`);
}
function pLocal() { return ["127.0.0.1", "localhost", "[::1]", "::1"].includes(location.hostname); }
function setupPersonalPages() {
  if (!personalMode()) return;
  const today = $("p-today");
  // 源码和旧 DOM 保留，个人首页用独立的四个区块。
  [...today.children].forEach(el => { el.hidden = true; });
  const heading = pNode("div"); heading.classList.add("title"); heading.append(pNode("h1", "今日", "Today")); today.append(heading);
  $("p-lab").querySelector("h1").replaceChildren(pNode("span", "回测", "Backtest"));
  today.append(pSection("personalActions", "今日动作", "Today's actions"),
    pSection("personalMonitor", "盯盘", "Monitor"), pSection("personalPortfolio", "个人模拟账户", "Personal paper account"),
    pSection("personalRisks", "风险提示", "Risks"), pSection("personalReference", "参考", "Reference"));
  $("personalMonitor").append(pState("尚未接入网站", "Not connected to the website"));
  const trade = $("p-trade");
  trade.querySelector("h1").replaceChildren(pNode("span", "信号", "Signals"));
  const tracking = $("tradeTracking"), mine = $("tradeMine"), limits = trade.querySelector(".trade-limitations");
  trade.append(mine, tracking); trade.append(limits);
  const strategyState = pNode("p"); strategyState.id = "personalStrategyState";
  trade.querySelector(".title").append(strategyState);
  for (const [page, zh, en] of [["review", "核对统计", "Signal review"], ["track", "上线记录 / 历史回放", "Live records / historical replay"]]) {
    const details = pFold(zh, en); details.id = `personal-${page}`;
    const old = $(`p-${page}`), title = old.querySelector(".title"), sub = title.querySelector(".sub");
    title.replaceChildren(pNode("h2", zh, en), ...(sub ? [sub] : []));
    while (old.firstChild) details.append(old.firstChild);
    trade.append(details);
    if (page === "track") details.querySelector(".note").hidden = true;
    details.addEventListener("toggle", () => { if (details.open) pLoadSection(page); });
  }
  const select = pNode("select"); select.id = "personalTrackSource";
  select.setAttribute("aria-label", "上线记录 / 历史回放 · Live / replay");
  for (const [value, zh, en] of [["live", "上线记录", "Live records"], ["replay", "历史回放", "Historical replay"]]) {
    const option = pNode("option", zh, en); option.value = value; select.append(option);
  }
  $("personal-track").insertBefore(select, $("trEmpty"));
  select.addEventListener("change", () => { PERSONAL.track = select.value; pLoadSection("track"); });
  // 风险优先，只改变默认标签和标签顺序；原过滤、排序、导出范围不变。
  const buttons = [...$("tradeTabs").children];
  const adjust = buttons.find(b => b.dataset.tab === "adjust"); adjust.hidden = true;
  const extraTabs = [["reduce", "减仓", "Reduce"], ["add", "加仓", "Add"]].map(([key, zh, en]) => {
    const b = pNode("button", zh, en); b.type = "button"; b.dataset.tab = key; b.setAttribute("role", "tab"); return b;
  });
  $("tradeTabs").append(buttons.find(b => b.dataset.tab === "sell"), extraTabs[0], buttons.find(b => b.dataset.tab === "buy"), extraTabs[1], adjust);
  TRADE.tab = "sell";
  const market = $("mBody"), summary = $("mSummary").closest(".card");
  market.prepend(pCopy("历史回测仅供发现候选，不是今日推荐；留意日期、覆盖率和样本。", "Historical backtests help discover candidates; they are not today's recommendations. Check dates, coverage and samples."));
  market.append(pFold("全市场历史回测与口径", "Market backtests and methodology", summary));
  const watchActions = pSection("personalWatchActions", "自选当日相关动作", "Today's watchlist actions");
  $("stockTbl").closest(".tbl").before(watchActions);
  const out = $("btOut");
  out.append(pFold("详细分析", "Detailed analysis", $("rptOut"), $("anaOut")));
  $("p-lab").insertBefore(pCopy("研究用回测：警惕过拟合、多重检验、样本不足和交易成本。", "Research backtests: beware of overfitting, multiple testing, small samples and trading costs."), $("p-lab").firstChild);
}
async function loadPersonalToday() {
  const view = PERSONAL.view, market = S.market;
  const current = () => view === PERSONAL.view && market === S.market;
  const actions = $("personalActions"), assets = $("personalPortfolio"), risks = $("personalRisks"), ref = $("personalReference");
  actions.replaceChildren(pNode("h2", "今日动作", "Today's actions"));
  assets.replaceChildren(pNode("h2", "个人模拟账户", "Personal paper account"));
  risks.replaceChildren(pNode("h2", "风险提示", "Risks"), pCopy("成本、涨跌停和停牌可能阻止成交；样本不足，默认策略未经晋升检验。", "Costs, price limits and suspensions can prevent fills; samples may be insufficient and default strategies are not promoted."));
  ref.replaceChildren(pNode("h2", "参考", "Reference"));
  const link = pNode("a", "完整信号", "All signals"); link.href = "#/trade"; actions.append(link);
  const jobs = [async () => {
    if (market !== "cn") { actions.append(pState("美股清单暂不支持", "US action lists are not supported")); return; }
    const main = await pRequest("/api/main_strategy");
    if (!current()) return;
    if (!main.main) { actions.append(pState("主策略不可用", "Main strategy unavailable")); return; }
    const r = await pList(main.main);
    if (!current()) return;
    if (r.error || r.empty) { actions.append(pNode("p", r.error || r.message), pState("暂无已发布动作", "No published actions")); return; }
    actions.append(pNode("p", `${r.strategy} · ${r.data_date} · ${r.generated_at}`), pCopy("来源：已发布清单，非账户持仓", "Source: published list, not account holdings"));
    const age = (Date.now() - new Date(`${r.data_date}T15:00:00+08:00`)) / 86400000;
    if (!Number.isFinite(age) || age > 3) actions.append(pCopy("数据日期需核对，可能过旧", "Check the data date; it may be stale"));
    for (const [zh, en] of [["卖出", "Sell"], ["减仓", "Reduce"], ["买入", "Buy"], ["加仓", "Add"]]) {
      const count = (r.rows || []).filter(row => row.action === zh).length;
      actions.append(pNode("h3", `${zh} · ${count}`, `${en} · ${count}`));
    }
    actions.append(pCopy("异常 / 停牌", "Exceptions / suspensions"), pNode("p", (r.exceptions || []).length));
  }, async () => {
    const summary = await pRequest(`/api/summary?market=${market}`);
    if (!current()) return;
    if (summary.error) { risks.append(pNode("p", summary.error)); return; }
    (summary.health || []).forEach(h => { risks.append(pNode("p", h)); actions.append(pNode("p", h)); });
    const lead = (summary.strategies || []).find(c => c.key === "blend2" && !c.error);
    if (lead) actions.append(pCopy("组合目标（独立口径，不与逐股仓位相加）", "Portfolio target (separate basis; do not add to stock weights)"), pNode("p", `${lead.label} · ${tradePosition(lead.next_total)}`));
    ref.append(pFold("基准行情", "Benchmark quotes", pNode("p", `${summary.main_name} · ${summary.data_date}`),
      pTable(["Code", "Close", "1D %"], (summary.watch || []).filter(w => w.symbol === summary.main).map(w => [w.symbol, fmt(w.close), pct(w.d1)]))));
  }, async () => {
    if (!pLocal()) { assets.append(pCopy("仅本机可读取个人模拟账户", "Personal paper account is available locally only")); return; }
    const me = await api("/api/member/me");
    if (!current()) return;
    if (!me.user) { assets.append(pState("未加载个人模拟账户", "Personal paper account not loaded")); return; }
    const pf = await api(`/api/member/portfolio?market=${market}`);
    if (!current()) return;
    if (pf.error || !isNum(pf.total)) { assets.append(pState("个人模拟账户不可用", "Personal paper account unavailable")); return; }
    assets.append(pCopy("来源：当前会话模拟账户；资产与累计收益", "Source: current session paper account; assets and cumulative return"), pNode("p", `${fmt(pf.total)} · ${pct(pf.ret_pct)}`),
      pTable(["Code", "Name", "Quantity"], (pf.positions || []).map(p => [p.code, p.name, p.qty])));
    ref.append(pFold("模拟资产走势", "Paper account history", pTable(["Date", "Value"], (pf.curve || []).map(p => [p[0], p[1]]))));
  }, async () => {
    const tr = await pRequest(`/api/tracking?market=${market}`);
    if (current()) ref.append(pFold("通知", "Notifications", ...(tr.notifications || []).slice(0, 6).map(n => pNode("p", `${n.time} · ${n.body}`))));
  }];
  await Promise.all(jobs.map((job, i) => job().catch(() => {
    if (current()) (i === 2 ? assets : i === 3 ? ref : actions).append(pState("区块暂不可用", "Section unavailable"));
  })));
}
async function pWatchActions(rows) {
  const view = PERSONAL.view, market = S.market, token = (PERSONAL.watch || 0) + 1;
  PERSONAL.watch = token;
  const box = $("personalWatchActions"); box.replaceChildren(pNode("h2", "自选当日相关动作", "Today's watchlist actions"));
  if (market !== "cn") { box.append(pState("美股清单暂不支持", "US action lists are not supported")); return; }
  try {
    const main = await pRequest("/api/main_strategy");
    if (!main.main) throw new Error("Main strategy unavailable / 主策略不可用");
    const d = await pList(main.main);
    if (view !== PERSONAL.view || market !== S.market || token !== PERSONAL.watch) return;
    if (d.error) throw new Error(d.error);
    const codes = new Set(rows.map(r => r.symbol));
    box.append(pNode("p", `${d.strategy || main.main} · ${d.data_date || "—"}`), pTable(["Code", "Name", "Action", "Target"],
      (d.rows || []).filter(r => codes.has(r.code)).map(r => [r.code, r.name, r.action, tradeTargetText(r)])));
  } catch (e) { if (view === PERSONAL.view && token === PERSONAL.watch) box.append(pState("区块暂不可用", "Section unavailable"), pNode("p", e.message)); }
}
async function pLoadSection(page) {
  const view = PERSONAL.view, market = S.market, source = PERSONAL.track;
  const id = page === "review" ? "reviewBody" : "trEmpty", box = $(id);
  const token = (PERSONAL[page] || 0) + 1; PERSONAL[page] = token;
  box.replaceChildren(pState("读取中…", "Loading…"));
  if (page === "review") $("reviewMeta").replaceChildren();
  if (page === "track") $("trBody").hidden = true;
  const current = () => view === PERSONAL.view && market === S.market && token === PERSONAL[page];
  if (page === "review" && market !== "cn") { box.replaceChildren(pState("美股核对暂不支持", "US signal review is not supported")); return; }
  try {
    const d = await pRequest(page === "review" ? "/api/ledger" : `/api/tracking?market=${market}`);
    if (!current()) return;
    box.replaceChildren();
    if (d.error || d.empty) { box.append(pNode("p", d.error || d.message), pState("暂无数据", "No data")); return; }
    if (page === "review") {
      $("reviewMeta").replaceChildren(pNode("span", `已记录 ${(d.signal_days || []).length} 个信号日，已核对 ${(d.evaluated_days || []).length} 个`,
        `${(d.signal_days || []).length} signal days recorded, ${(d.evaluated_days || []).length} evaluated`));
      const main = d.main_pick?.main || d.main;
      box.append(pCopy("来源：全市场核对；1 / 5 / 10 / 20 日，未计费用和滑点；与发布清单跟踪分开。", "Source: market-wide ledger; 1 / 5 / 10 / 20 days, excluding fees and slippage; separate from published-list tracking."),
        pNode("p", main), pNode("p", d.promotion?.message), pNode("p", d.main_pick?.reason),
        pCopy("晋升样本：完整交易周 / 已评估信号日", "Promotion samples: complete trading weeks / evaluated signal days"),
        pNode("p", `${d.promotion?.complete_weeks ?? "—"} / ${(d.evaluated_days || []).length}`),
        pCopy("样本不足的结果仅供观察；晋升规则和冻结期保持原口径。", "Small samples are observational only; promotion rules and freeze periods retain their original basis."));
      if (d.main_pick?.default) box.append(pCopy("默认策略，未经晋升检验", "Default strategy, not promoted"));
      const record = d.main_pick?.record;
      if (record) box.append(pCopy("主策略：晋升前 / 晋升后（正式成绩）", "Main strategy: before / after promotion (formal record)"),
        pTable(["Status", "N", "Days", "Hit", "Excess"], [["晋升前 / Before", record.pre], ["晋升后 / After", record.post]].filter(([, r]) => r).map(([label, r]) => [label, r.n, r.days, pRatio(r.hit), pRatio(r.score, 2)])));
      (d.notes || []).forEach(n => box.append(pNode("p", n)));
      const precision = key => pTable(["Action", "Days", "N", "Hit", "Excess hit", "Return", "Excess", "Verdict"],
        (d.precision?.[key] || []).map(p => {
          const count = pNode("td", p.n), verdict = pNode("td");
          if (p.untradable) count.append(pNode("small", `（另有 ${p.untradable} 条开盘即涨/跌停）`, ` (${p.untradable} opening price-limit signals excluded)`));
          verdict.append(pNode("span", p.verdict || (p.enough ? "" : "样本不足"), p.verdict || (p.enough ? "" : "Insufficient sample")),
            pNode("small", `命中率 95% 区间 ${pRatio(p.hit_excess_lo, 0)}–${pRatio(p.hit_excess_hi, 0)}；中位数 ${pRatio(p.median_excess, 2)}；扣费后 ${pRatio(p.net_excess, 2)}`,
              `Hit rate 95% interval ${pRatio(p.hit_excess_lo, 0)}–${pRatio(p.hit_excess_hi, 0)}; median ${pRatio(p.median_excess, 2)}; net ${pRatio(p.net_excess, 2)}`));
          return [p.action, p.h, count, pRatio(p.hit_raw, 1), pRatio(p.hit_excess, 1), pRatio(p.mean_ret, 2), pRatio(p.mean_excess, 2), verdict];
        }), (row, i) => { if (!d.precision[key][i].enough) row.classList.add("dim"); });
      box.append(precision(main), pCopy("漏掉的机会 / 召回", "Missed opportunities / recall"),
        pTable(["Date", "Days", "Winners", "Recall"], (d.recall || []).map(r => [r.day, r.h, r.winners, pRatio(r.recall, 1)])));
      for (const r of d.recall || []) {
        box.append(pNode("p", r.day), pTable([["涨幅居前股票分类", "Winner categories"], "N"], Object.entries(r.categories || {})),
          pTable([["股票", "Stock"], ["涨幅", "Return"], ["当时为什么没买", "Why not bought then"]],
            (r.missed || []).map(m => [`${m.name || ""} ${m.code}`, pRatio(m.ret, 1), m.category])));
      }
      box.append(pCopy("每日反思", "Daily reflection"), ...(d.reflection || []).map(r => pNode("p", `${r.text} ${r.suggestion || ""}`)));
      const extra = pFold("全策略 / 随机对照 / 调参状态", "All strategies / random controls / tuning");
      Object.keys(d.precision || {}).filter(k => k !== main).forEach(k => extra.append(pNode("h3", k), precision(k)));
      extra.append(pCopy("各策略信号得分（扣费后超额），与上表未扣费准确率分开", "Strategy scores (net excess return), separate from the gross precision table above"),
        pTable(["Strategy", "N", "Hit", "Excess"], Object.entries(d.board || {}).map(([k, v]) => [k, v.h5?.n, pRatio(v.h5?.hit, 1), pRatio(v.h5?.score, 2)])));
      extra.append(pTable(["Strategy", "Weeks", "N", "Advantage", "Noise", "p", "Status"], Object.entries(d.promotion?.strategies || {}).map(([k, v]) => [k, v.weeks, v.n, pRatio(v.advantage, 2), v.noise, v.p, v.status])));
      const tune = d.tune || {};
      extra.append(pCopy("自动调参观察 / 冻结状态", "Tuning observations / freeze status"), pNode("p", tune.why),
        pNode("p", `${tune.weeks ?? "—"} / ${tune.freeze_weeks ?? 20}`));
      box.append(extra, ...(d.caveats || []).map(c => pNode("p", c)));
    } else {
      const ev = source === "replay" ? d.replay : d.eval;
      box.append(pCopy("来源：ETF 基准跟踪，5 / 20 / 60 日；与全市场核对、发布后清单样本分开。", "Source: ETF benchmark tracking, 5 / 20 / 60 days; separate from the market ledger and published-list samples."));
      box.append(pCopy("按次日开盘入场计算价格收益，未扣费用和滑点；基准", "Price returns from the next open, excluding fees and slippage; benchmark"), pNode("p", S.meta.markets?.[market]?.main || (market === "cn" ? "沪深300ETF" : "SPY")));
      if (source === "replay") box.append(pCopy("历史回放：事后规则重算，非当时上线记录，可能偏乐观。", "Historical replay: recomputed with hindsight, not live records; may be optimistic."));
      if (!ev || ev.empty) { box.append(pCopy("暂无数据，不自动切换历史回放", "No data; historical replay is never selected automatically")); return; }
      box.append(pNode("p", `${ev.total} · ${ev.since} → ${ev.latest} · ${ev.latest_price_date || "—"}`),
        pTable(["Strategy", "Action", "N", "5D N / Avg / Beat", "20D N / Avg / Beat", "60D N / Avg / Beat"],
          (ev.summary || []).map(r => [S.meta.strategies[r.strategy]?.label || r.strategy, r.action, r.signals, ...[5,20,60].map(h => `${r[`n${h}`] ?? "—"} / ${fmt(r[`avg${h}`],2,"%")} / ${fmt(r[`beat${h}`],1,"%")}`)])),
        pTable(["Date", "Name", "Strategy", "Action", "5D %", "20D %", "60D %", ["至今", "To date"], ["至今比大盘", "Excess to date"]], (ev.recent || []).map(r => {
          const cell = pNode("td"), link = pNode("a", `${r.name || ""} ${r.code}`);
          link.href = `#/stock/${encodeURIComponent(r.code)}`; cell.append(link);
          return [r.signal_date, cell, S.meta.strategies[r.strategy]?.label || r.strategy, r.action, pct(r.r5), pct(r.r20), pct(r.r60), pct(r.r_now), pct(r.x_now)];
        })));
      if ((ev.summary || []).every(r => !(r.n5 > 0))) box.append(pCopy(
        `尚无信号完成 5 个持有交易日，预计首批统计日期 ${ev.expected_first_5 || "待数据更新"}（按工作日估算，遇市场假期顺延）`,
        `No signals have completed 5 holding sessions; first statistics expected ${ev.expected_first_5 || "pending data update"} (weekday estimate, deferred for market holidays)`));
    }
  } catch (e) { if (current()) box.replaceChildren(pState("区块暂不可用", "Section unavailable"), pNode("p", e.message)); }
}

// ================================================================ 持仓台账（T013 · S4，仅个人模式）
// 隐私约束：持仓只放在下面这个模块内存变量里；不进个人页的请求缓存，不写任何浏览器持久化存储，
// 不挂全局对象，不进日志；所有内容用 textContent / createElement 渲染；离开本页即清空。
const HOLD = { snap: null, token: 0, built: false, failed: "", includeVoided: false, capitalEdit: false, draft: null, clientId: "", busy: false, nameToken: 0, attrs: [] };
const HOLD_SIDES = ["buy", "sell", "bonus", "dividend"];
function hLang() { return window.AI_I18N?.language === "en" ? "en" : "zh"; }
function hText(key, values = {}, lang = hLang()) {
  return window.HOLDINGS_I18N ? window.HOLDINGS_I18N.text(key, values, lang) : key;
}
// 中英文成对写入 dataset，切换语言时由 dashboard-language-change 统一刷新（与 pNode 同一机制）。
function hNode(tag, key, values = {}) { return pNode(tag, hText(key, values, "zh"), hText(key, values, "en")); }
function hNodeL(tag, key, valuesFor) { return pNode(tag, hText(key, valuesFor("zh"), "zh"), hText(key, valuesFor("en"), "en")); }
function hRetext(el, key, values = {}) {
  el.dataset.zh = hText(key, values, "zh"); el.dataset.en = hText(key, values, "en");
  el.textContent = el.dataset[hLang()];
}
function hAttr(el, attr, key) { HOLD.attrs.push([el, attr, key]); el.setAttribute(attr, hText(key)); }
// 接口给的是定点小数字符串；显示时按十进制字符串四舍五入（避免二进制浮点把 17.85 显示成 17.8）。
function hFixed(v, digits) {
  const m = /^(-?)(\d+)(?:\.(\d+))?$/.exec(String(v).trim());
  if (!m) return Number(v).toFixed(digits);
  const frac = (m[3] || "").padEnd(digits + 1, "0");
  let n = BigInt(m[2] + frac.slice(0, digits));
  if (frac[digits] >= "5") n += 1n;
  const s = n.toString().padStart(digits + 1, "0");
  const out = digits ? `${s.slice(0, -digits)}.${s.slice(-digits)}` : s;
  return m[1] && n !== 0n ? `-${out}` : out;
}
function hNum(v, digits = 2) {
  if (v === null || v === undefined || !isNum(v)) return "—";
  const [int, frac] = hFixed(v, digits).split(".");
  return int.replace(/\B(?=(\d{3})+(?!\d))/g, ",") + (frac ? `.${frac}` : "");
}
function hRatio(v, digits = 2, signed = false) {
  if (v === null || v === undefined || !isNum(v)) return "—";
  const pctText = /^-?\d+(\.\d+)?$/.test(String(v).trim()) ? hFixed(hPercentString(v), digits) : (Number(v) * 100).toFixed(digits);
  return `${signed && Number(pctText) > 0 ? "+" : ""}${pctText}%`;
}
// 小数点右移两位：0.1785 -> 17.85（纯字符串，无浮点误差）
function hPercentString(v) {
  const m = /^(-?)(\d+)(?:\.(\d+))?$/.exec(String(v).trim());
  const frac = (m[3] || "").padEnd(2, "0");
  return `${m[1]}${(m[2] + frac.slice(0, 2)).replace(/^0+(?=\d)/, "")}${frac.length > 2 ? "." + frac.slice(2) : ""}`;
}
function hSign(v) { return v !== null && v !== undefined && isNum(v) ? cls(Number(v)) : ""; }
// 北京日期：服务器或浏览器不在东八区时也不会差一天。
function hToday() {
  return new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date());
}
function hClientId() {
  const c = window.crypto || globalThis.crypto;
  if (c?.randomUUID) return c.randomUUID();
  return `c${Date.now().toString(36)}${Math.random().toString(36).slice(2, 12)}`.slice(0, 40).padEnd(8, "0");
}
// 专用请求函数：每次都带 X-Requested-With，GET 也带；错误只返回固定码，不读请求缓存。
async function hRequest(path, body) {
  const opts = { credentials: "same-origin", cache: "no-store", headers: { "X-Requested-With": "dashboard" } };
  if (body !== undefined) { opts.method = "POST"; opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  let r;
  try { r = await fetch(path, opts); } catch { return { ok: false, code: "NETWORK" }; }
  let data = null;
  try { data = await r.json(); } catch { data = null; }
  if (r.status === 401) return { ok: false, code: "AUTH" };
  if (r.status === 404 && !data?.error) return { ok: false, code: "UNAVAILABLE" };
  if (!(r.status >= 200 && r.status < 300) || !data || data.ok !== true) return { ok: false, code: typeof data?.error === "string" ? data.error : "generic", field: data?.field };
  return { ok: true, data };
}
function hErrorKey(code) {
  const key = `hold.err.${code}`;
  return window.HOLDINGS_I18N?.has(key) ? key : "hold.err.generic";
}
function hShow(key, kind = "error") {
  const box = $("holdMessage"); if (!box) return;
  const node = hNode("p", key); node.setAttribute("role", kind === "error" ? "alert" : "status");
  node.classList.add(kind === "error" ? "hold-error" : "hold-ok");
  box.replaceChildren(node);
}
function hClear() { $("holdMessage")?.replaceChildren(); }
function hCell(labelKey, ...children) {
  const td = document.createElement("td");
  const label = hNode("span", labelKey); label.classList.add("hold-cell-label");
  const value = document.createElement("span"); value.classList.add("hold-cell-value"); value.append(...children);
  td.append(label, value); return td;
}
function hText2(v) { const s = document.createElement("span"); s.textContent = v; return s; }
function hTable(columns, rows, id) {
  const wrap = document.createElement("div"), table = document.createElement("table"), head = document.createElement("thead"), tr = document.createElement("tr"), body = document.createElement("tbody");
  wrap.classList.add("tbl"); table.classList.add("hold-table"); if (id) table.id = id;
  columns.forEach(key => tr.append(hNode("th", key))); head.append(tr); body.append(...rows); table.append(head, body); wrap.append(table);
  return wrap;
}
function hSideLabel(side, tag = "span") { return hNode(tag, `hold.side.${HOLD_SIDES.includes(side) ? side : "buy"}`); }
function hSortPositions(list) {
  const mv = p => (isNum(p.market_value) && p.market_value !== null ? Number(p.market_value) : -Infinity);
  const has = p => p.weight !== null && p.weight !== undefined && isNum(p.weight);
  return [...list].sort((a, b) => (has(a) && has(b) ? Number(b.weight) - Number(a.weight) : has(a) ? -1 : has(b) ? 1 : mv(b) - mv(a)) || String(a.code).localeCompare(String(b.code)));
}
// 价格日期距 as_of 超过 5 个自然日（约 3 个交易日加周末）算偏旧；只是提示，不影响计算。
function hStale(priceDate, asOf) {
  if (!priceDate || !asOf) return false;
  const gap = (Date.parse(`${asOf}T00:00:00Z`) - Date.parse(`${priceDate}T00:00:00Z`)) / 86400000;
  return Number.isFinite(gap) && gap > 5;
}
function hNameOf(code) {
  const hit = (HOLD.snap?.positions || []).find(p => p.code === code) || (HOLD.snap?.trades || []).find(t => t.code === code) || (HOLD.snap?.closed || []).find(c => c.code === code);
  return hit && hit.name && hit.name !== code ? hit.name : "";
}

function holdingsBuild() {
  if (HOLD.built) return;
  HOLD.built = true;
  const page = $("p-holdings");
  const title = pNode("div"); title.classList.add("title"); title.append(hNode("h1", "hold.title"));
  const note = hNode("p", "hold.note"); note.classList.add("dim");
  const message = document.createElement("div"); message.id = "holdMessage";
  const sections = {};
  for (const [id, key] of [["holdSummary", "hold.sum.title"], ["holdPositions", "hold.pos.title"], ["holdForm", "hold.form.title"], ["holdHistory", "hold.hist.title"]]) {
    const sec = document.createElement("section"); sec.id = id; sec.classList.add("card", "hold-card"); sec.append(hNode("h2", key));
    const body = document.createElement("div"); body.id = `${id}Body`; sec.append(body); sections[id] = sec;
  }
  const closed = document.createElement("details"); closed.id = "holdClosed"; closed.classList.add("hold-closed");
  closed.append(hNode("summary", "hold.closed.title")); const closedBody = document.createElement("div"); closedBody.id = "holdClosedBody"; closed.append(closedBody);
  const monitor = hNode("p", "hold.monitor"); monitor.classList.add("dim");
  page.replaceChildren(title, note, message, sections.holdSummary, sections.holdPositions, monitor, sections.holdForm, sections.holdHistory, closed);
  holdingsBuildForm($("holdFormBody"));
  window.addEventListener("dashboard-language-change", () => { HOLD.attrs.forEach(([el, attr, key]) => el.setAttribute(attr, hText(key))); });
}
function holdingsBuildForm(box) {
  const form = document.createElement("form"); form.id = "holdEntry"; form.noValidate = true; form.autocomplete = "off";
  const field = (labelKey, control, hintKey) => {
    const label = document.createElement("label"); label.classList.add("hold-field");
    const text = hNode("span", labelKey); label.append(text, control);
    if (hintKey) { const hint = hNode("small", hintKey); hint.classList.add("dim"); label.append(hint); }
    return { label, text };
  };
  const date = document.createElement("input"); date.type = "date"; date.id = "holdDate"; date.value = hToday(); date.required = true;
  const code = document.createElement("input"); code.type = "text"; code.id = "holdCode"; code.inputMode = "numeric"; code.maxLength = 6; code.pattern = "[0-9]{6}"; code.autocomplete = "off";
  const side = document.createElement("select"); side.id = "holdSide";
  HOLD_SIDES.forEach(s => { const o = hNode("option", `hold.side.${s}`); o.value = s; side.append(o); });
  const qty = document.createElement("input"); qty.type = "number"; qty.id = "holdQty"; qty.min = "1"; qty.step = "1"; qty.inputMode = "numeric";
  const price = document.createElement("input"); price.type = "text"; price.id = "holdPrice"; price.inputMode = "decimal";
  const fee = document.createElement("input"); fee.type = "text"; fee.id = "holdFee"; fee.inputMode = "decimal";
  const note = document.createElement("input"); note.type = "text"; note.id = "holdNote"; note.maxLength = 200;
  const nameHint = document.createElement("small"); nameHint.id = "holdNameHint"; nameHint.classList.add("dim");
  const fDate = field("hold.form.date", date), fCode = field("hold.form.code", code), fSide = field("hold.form.side", side);
  fCode.label.append(nameHint);
  const fQty = field("hold.form.qty", qty), fPrice = field("hold.form.price", price), fFee = field("hold.form.fee", fee, "hold.form.fee_hint"), fNote = field("hold.form.note", note);
  const grid = document.createElement("div"); grid.classList.add("hold-grid");
  grid.append(fDate.label, fCode.label, fSide.label, fQty.label, fPrice.label, fFee.label, fNote.label);
  const submit = hNode("button", "hold.form.submit"); submit.type = "submit"; submit.id = "holdSubmit"; submit.classList.add("btn", "primary");
  const formMsg = document.createElement("div"); formMsg.id = "holdFormMessage"; formMsg.setAttribute("aria-live", "polite");
  const confirmBox = document.createElement("div"); confirmBox.id = "holdConfirm"; confirmBox.classList.add("hold-confirm"); confirmBox.hidden = true;
  form.append(grid, submit, formMsg, confirmBox); box.append(form);
  HOLD.clientId = hClientId();
  // 方向切换时调整“数量/价格”的含义；送转没有价格。
  const syncSide = () => {
    const s = side.value;
    hRetext(fQty.text, s === "dividend" ? "hold.form.qty_div" : s === "bonus" ? "hold.form.qty_bonus" : "hold.form.qty");
    hRetext(fPrice.text, s === "dividend" ? "hold.form.price_div" : "hold.form.price");
    fPrice.label.hidden = fFee.label.hidden = s === "bonus";
    if (s === "bonus") { price.value = ""; fee.value = ""; }
  };
  side.addEventListener("change", () => { syncSide(); holdingsCancelConfirm(); });
  // 代码输入满 6 位后：先看台账里已有的名称，再用已有的搜索接口只读提示名称；查不到也允许录入。
  code.addEventListener("input", async () => {
    holdingsCancelConfirm();
    const value = code.value.trim(), token = ++HOLD.nameToken;
    nameHint.textContent = "";
    if (!/^\d{6}$/.test(value)) return;
    let name = hNameOf(value);
    if (!name) {
      try {
        const items = await api(`/api/search?market=cn&q=${encodeURIComponent(value)}`);
        name = (Array.isArray(items) ? items : []).find(x => x?.code === value)?.name || "";
      } catch { name = ""; }
    }
    if (token !== HOLD.nameToken) return;
    if (name) { nameHint.classList.remove("personal-copy"); delete nameHint.dataset.zh; delete nameHint.dataset.en; nameHint.textContent = name; }
    else { hRetext(nameHint, "hold.form.name_unknown"); nameHint.classList.add("personal-copy"); }
  });
  [date, price, fee, qty, note].forEach(el => el.addEventListener("input", holdingsCancelConfirm));
  form.addEventListener("submit", ev => { ev.preventDefault(); holdingsPrepare(); });
  hAttr(code, "placeholder", "hold.form.code");
  hAttr(fee, "placeholder", "hold.form.fee_auto");
}
function holdingsCancelConfirm() {
  HOLD.draft = null;
  const box = $("holdConfirm"); if (box) { box.hidden = true; box.replaceChildren(); }
}
function holdingsReadForm() {
  const side = $("holdSide").value, code = $("holdCode").value.trim(), qtyText = $("holdQty").value.trim();
  const price = $("holdPrice").value.trim(), fee = $("holdFee").value.trim(), note = $("holdNote").value.trim();
  const body = { trade_date: $("holdDate").value, code, side, qty: /^\d+$/.test(qtyText) ? Number(qtyText) : qtyText };
  if (side !== "bonus") { body.price = price; if (fee) body.fee = fee; }
  if (note) body.note = note;
  const complete = /^\d{6}$/.test(code) && qtyText !== "" && body.trade_date && (side === "bonus" || price !== "");
  return { body, complete };
}
// 先显示确认摘要，用户再点“确认入账”才发请求。
function holdingsPrepare() {
  if (HOLD.busy) return;
  const formMsg = $("holdFormMessage"); formMsg.replaceChildren(); hClear();
  const { body, complete } = holdingsReadForm();
  if (!complete) { const warn = hNode("p", "hold.form.incomplete"); warn.setAttribute("role", "alert"); warn.classList.add("hold-error"); formMsg.append(warn); return; }
  HOLD.draft = body;
  const name = $("holdNameHint").textContent && !$("holdNameHint").classList.contains("personal-copy") ? $("holdNameHint").textContent : hNameOf(body.code);
  const box = $("holdConfirm"); box.hidden = false;
  const feeFor = lang => (body.fee ? hText("hold.form.fee_value", { fee: body.fee }, lang) : hText("hold.form.fee_auto", {}, lang));
  const line = hNodeL("p", body.side === "bonus" ? "hold.form.confirm_text_bonus" : "hold.form.confirm_text", lang => ({
    side: hText(`hold.side.${body.side}`, {}, lang), code: body.code, name, qty: body.qty, price: body.price ?? "", fee: feeFor(lang), date: body.trade_date }));
  line.classList.add("hold-confirm-text");
  const ok = hNode("button", "hold.form.confirm_ok"); ok.type = "button"; ok.id = "holdConfirmOk"; ok.classList.add("btn", "primary");
  const back = hNode("button", "hold.form.back"); back.type = "button"; back.id = "holdConfirmBack"; back.classList.add("btn", "plain");
  ok.addEventListener("click", holdingsSubmit); back.addEventListener("click", holdingsCancelConfirm);
  box.replaceChildren(hNode("h3", "hold.form.confirm_title"), line, ok, back);
}
async function holdingsSubmit() {
  if (HOLD.busy || !HOLD.draft) return;
  HOLD.busy = true;
  const ok = $("holdConfirmOk"); if (ok) { ok.disabled = true; hRetext(ok, "hold.form.saving"); }
  const r = await hRequest("/api/holdings/add", { ...HOLD.draft, client_id: HOLD.clientId });
  HOLD.busy = false;
  if (!personalMode() || !$("p-holdings")?.classList.contains("on")) return;
  if (!r.ok) {
    holdingsCancelConfirm();
    const formMsg = $("holdFormMessage"), node = hNode("p", hErrorKey(r.code)); node.setAttribute("role", "alert"); node.classList.add("hold-error"); formMsg.replaceChildren(node);
    return;
  }
  // 成功：清空表单、换新的 client_id（同一次提交重试才会幂等，新的一笔必须是新 id）。
  const duplicate = r.data.duplicate === true;
  holdingsCancelConfirm();
  ["holdCode", "holdQty", "holdPrice", "holdFee", "holdNote"].forEach(id => { $(id).value = ""; });
  $("holdNameHint").textContent = ""; $("holdDate").value = hToday();
  HOLD.clientId = hClientId();
  $("holdFormMessage").replaceChildren();
  await loadHoldings();
  hShow(duplicate ? "hold.form.dup" : "hold.form.saved", "ok");
}

async function loadHoldings() {
  if (!personalMode()) return;
  holdingsBuild();
  const token = ++HOLD.token;
  if (!HOLD.snap) holdingsRender();
  const r = await hRequest(`/api/holdings?trades_limit=200&include_voided=${HOLD.includeVoided ? 1 : 0}`);
  if (token !== HOLD.token) return; // 已离开本页或有更新的请求
  if (!r.ok) { HOLD.snap = null; HOLD.failed = r.code; hClear(); holdingsRender(); hShow(hErrorKey(r.code)); return; }
  HOLD.snap = r.data; HOLD.failed = ""; hClear(); holdingsRender();
}
// 离开持仓页：清空内存快照、草稿和表单内容（私人数据不留在页面里）。
function holdingsLeave() {
  if (!HOLD.built) return;
  HOLD.token++; HOLD.snap = null; HOLD.failed = ""; HOLD.draft = null; HOLD.capitalEdit = false; HOLD.busy = false;
  ["holdSummaryBody", "holdPositionsBody", "holdHistoryBody", "holdClosedBody", "holdMessage", "holdFormMessage"].forEach(id => $(id)?.replaceChildren());
  holdingsCancelConfirm();
  ["holdCode", "holdQty", "holdPrice", "holdFee", "holdNote"].forEach(id => { if ($(id)) $(id).value = ""; });
  if ($("holdNameHint")) $("holdNameHint").textContent = "";
}
function holdingsRender() {
  const snap = HOLD.snap;
  const state = (key) => { const p = hNode("p", key); p.classList.add("personal-state"); p.setAttribute("role", "status"); return p; };
  if (!snap) {
    ["holdSummaryBody", "holdPositionsBody", "holdHistoryBody", "holdClosedBody"].forEach(id => $(id).replaceChildren(...(HOLD.failed ? [] : [state("hold.loading")])));
    return;
  }
  holdingsRenderSummary(snap); holdingsRenderPositions(snap); holdingsRenderHistory(snap); holdingsRenderClosed(snap);
}
function hTile(labelKey, value, klass = "") {
  const tile = document.createElement("div"), k = hNode("div", labelKey), v = document.createElement("div");
  tile.classList.add("tile"); k.classList.add("k"); v.classList.add("v"); if (klass) v.classList.add(klass); v.textContent = value;
  tile.append(k, v); return tile;
}
function holdingsRenderSummary(snap) {
  const s = snap.summary || {};
  const tiles = document.createElement("div"); tiles.classList.add("tiles", "hold-tiles");
  // 总资金：可就地编辑
  const capital = document.createElement("div"); capital.classList.add("tile", "hold-capital");
  const k = hNode("div", "hold.sum.capital"); k.classList.add("k");
  const v = document.createElement("div"); v.classList.add("v");
  if (HOLD.capitalEdit) {
    const input = document.createElement("input"); input.type = "text"; input.id = "holdCapitalInput"; input.inputMode = "decimal"; input.value = snap.capital ?? "";
    hAttr(input, "aria-label", "hold.sum.capital_label");
    const save = hNode("button", "hold.sum.save"); save.type = "button"; save.classList.add("btn", "primary");
    const cancel = hNode("button", "hold.sum.cancel"); cancel.type = "button"; cancel.classList.add("btn", "plain");
    save.addEventListener("click", async () => {
      save.disabled = true;
      const r = await hRequest("/api/holdings/capital", { capital: input.value.trim() });
      if (!$("p-holdings")?.classList.contains("on")) return;
      if (!r.ok) { save.disabled = false; hShow(hErrorKey(r.code)); return; }
      HOLD.capitalEdit = false; await loadHoldings(); hShow("hold.capital_saved", "ok");
    });
    cancel.addEventListener("click", () => { HOLD.capitalEdit = false; holdingsRender(); });
    input.addEventListener("keydown", e => { if (e.key === "Enter") save.click(); });
    v.append(input); capital.append(k, v, save, cancel);
  } else {
    if (snap.capital === null || snap.capital === undefined) v.append(hNode("span", "hold.sum.capital_unset")); else v.textContent = hNum(snap.capital);
    const edit = hNode("button", "hold.sum.edit"); edit.type = "button"; edit.id = "holdCapitalEdit"; edit.classList.add("btn", "plain");
    edit.addEventListener("click", () => { HOLD.capitalEdit = true; holdingsRender(); $("holdCapitalInput")?.focus?.(); });
    capital.append(k, v, edit);
  }
  tiles.append(capital,
    hTile("hold.sum.market_value", hNum(s.market_value)),
    hTile("hold.sum.invested", hRatio(s.invested_ratio, 1)),
    hTile("hold.sum.floating", hNum(s.floating_pnl), hSign(s.floating_pnl)),
    hTile("hold.sum.realized", hNum(s.realized), hSign(s.realized)),
    hTile("hold.sum.dividends", hNum(s.dividends)));
  const hint = hNode("p", "hold.sum.capital_hint"); hint.classList.add("dim");
  const parts = [tiles, hint];
  if (Number(s.missing_price_count) > 0) { const w = hNode("p", "hold.sum.missing", { n: Number(s.missing_price_count) }); w.classList.add("hold-warn"); parts.push(w); }
  $("holdSummaryBody").replaceChildren(...parts);
}
function holdingsRenderPositions(snap) {
  const list = hSortPositions(snap.positions || []);
  if (!list.length) { const p = hNode("p", "hold.pos.empty"); p.classList.add("personal-state"); p.setAttribute("role", "status"); $("holdPositionsBody").replaceChildren(p); return; }
  const rows = list.map(p => {
    const tr = document.createElement("tr");
    const link = document.createElement("a"); link.href = `#/stock/${encodeURIComponent(p.code)}`; link.textContent = p.code;
    const price = document.createElement("span");
    if (p.price === null || p.price === undefined) price.append(hNode("span", "hold.pos.noprice"));
    else {
      price.append(hText2(`${hNum(p.price, 3)} · ${p.price_date || "—"}`));
      if (hStale(p.price_date, snap.as_of)) { const st = hNode("small", "hold.pos.stale"); st.classList.add("hold-warn"); price.append(document.createElement("br"), st); }
    }
    const pnl = document.createElement("span"); pnl.classList.add(hSign(p.floating_pnl) || "flat");
    pnl.textContent = p.floating_pnl === null || p.floating_pnl === undefined ? "—" : `${hNum(p.floating_pnl)} (${hRatio(p.floating_pct, 2, true)})`;
    if (p.floating_pnl === null || p.floating_pnl === undefined) pnl.replaceChildren(hNode("span", "hold.pos.noprice"));
    const over = p.weight !== null && p.weight !== undefined && isNum(p.weight) && Number(p.weight) > 0.1;
    const weight = document.createElement("span"); weight.textContent = hRatio(p.weight, 2);
    if (over) { weight.classList.add("hold-over"); const flag = hNode("small", "hold.pos.over"); flag.classList.add("hold-over"); weight.append(document.createElement("br"), flag); tr.classList.add("hold-row-over"); }
    tr.append(hCell("hold.pos.code", link), hCell("hold.pos.name", hText2(p.name || p.code)), hCell("hold.pos.shares", hText2(hNum(p.shares, 0))),
      hCell("hold.pos.avg", hText2(hNum(p.avg_cost, 3))), hCell("hold.pos.price", price), hCell("hold.pos.pnl", pnl), hCell("hold.pos.weight", weight),
      hCell("hold.pos.first", hText2(p.first_buy || "—")), hCell("hold.pos.days", hText2(p.held_days ?? "—")));
    return tr;
  });
  const foot = hNode("p", "hold.pos.foot"); foot.classList.add("dim");
  $("holdPositionsBody").replaceChildren(hTable(["hold.pos.code", "hold.pos.name", "hold.pos.shares", "hold.pos.avg", "hold.pos.price", "hold.pos.pnl", "hold.pos.weight", "hold.pos.first", "hold.pos.days"], rows, "holdPositionsTable"), foot);
}
function holdingsRenderHistory(snap) {
  const toggle = document.createElement("label"); toggle.classList.add("hold-toggle");
  const box = document.createElement("input"); box.type = "checkbox"; box.id = "holdShowVoided"; box.checked = HOLD.includeVoided;
  box.addEventListener("change", () => { HOLD.includeVoided = box.checked; loadHoldings(); });
  toggle.append(box, hNode("span", "hold.hist.show_voided"));
  const trades = snap.trades || [];
  if (!trades.length) { const p = hNode("p", "hold.hist.empty"); p.classList.add("personal-state"); p.setAttribute("role", "status"); $("holdHistoryBody").replaceChildren(toggle, p); return; }
  const rows = trades.map(t => {
    const tr = document.createElement("tr"); if (t.voided) tr.classList.add("hold-voided");
    const stock = document.createElement("span"); stock.textContent = t.name && t.name !== t.code ? `${t.code} ${t.name}` : t.code;
    const fee = document.createElement("span"); fee.textContent = hNum(t.fee);
    if (t.fee_estimated) { const est = hNode("small", "hold.hist.est"); est.classList.add("dim"); fee.append(hText2(" "), est); }
    const action = document.createElement("span");
    if (t.voided) action.append(hNode("small", "hold.hist.voided"));
    else {
      const btn = hNode("button", "hold.hist.void"); btn.type = "button"; btn.classList.add("btn", "plain", "hold-void");
      btn.addEventListener("click", () => holdingsAskVoid(action, t.id));
      action.append(btn);
    }
    tr.append(hCell("hold.hist.date", hText2(t.trade_date)), hCell("hold.hist.side", hSideLabel(t.side)), hCell("hold.hist.stock", stock),
      hCell("hold.hist.qty", hText2(hNum(t.qty, 0))), hCell("hold.hist.price", hText2(t.side === "bonus" ? "—" : hNum(t.price, 3))), hCell("hold.hist.fee", fee),
      hCell("hold.hist.note", hText2(t.note || "")), hCell("hold.hist.void", action));
    return tr;
  });
  $("holdHistoryBody").replaceChildren(toggle, hTable(["hold.hist.date", "hold.hist.side", "hold.hist.stock", "hold.hist.qty", "hold.hist.price", "hold.hist.fee", "hold.hist.note", "hold.hist.void"], rows, "holdHistoryTable"));
}
// 撤销二次确认：点“撤销”后在原地换成确认提示，再点“确认撤销”才发请求。
function holdingsAskVoid(cell, id) {
  const ask = hNode("span", "hold.hist.void_ask"), ok = hNode("button", "hold.hist.void_ok"), cancel = hNode("button", "hold.hist.void_cancel");
  ok.type = cancel.type = "button"; ok.classList.add("btn", "primary", "hold-void-ok"); cancel.classList.add("btn", "plain");
  const original = [...cell.children];
  cancel.addEventListener("click", () => cell.replaceChildren(...original));
  ok.addEventListener("click", async () => {
    ok.disabled = true;
    const r = await hRequest("/api/holdings/void", { id });
    if (!$("p-holdings")?.classList.contains("on")) return;
    if (!r.ok) { cell.replaceChildren(...original); hShow(hErrorKey(r.code)); return; }
    await loadHoldings(); hShow("hold.hist.void_done", "ok");
  });
  cell.replaceChildren(ask, ok, cancel);
}
function holdingsRenderClosed(snap) {
  const closed = snap.closed || [];
  if (!closed.length) { const p = hNode("p", "hold.closed.empty"); p.classList.add("personal-state"); $("holdClosedBody").replaceChildren(p); return; }
  const rows = closed.map(c => {
    const tr = document.createElement("tr"), pnl = document.createElement("span"); pnl.textContent = hNum(c.pnl); pnl.classList.add(hSign(c.pnl) || "flat");
    tr.append(hCell("hold.pos.code", hText2(c.name && c.name !== c.code ? `${c.code} ${c.name}` : c.code)), hCell("hold.pos.first", hText2(c.first_buy || "—")),
      hCell("hold.closed.last", hText2(c.last_sell || "—")), hCell("hold.pos.days", hText2(c.held_days ?? "—")), hCell("hold.closed.pnl", pnl));
    return tr;
  });
  $("holdClosedBody").replaceChildren(hTable(["hold.pos.code", "hold.pos.first", "hold.closed.last", "hold.pos.days", "hold.closed.pnl"], rows, "holdClosedTable"));
}

// ================================================================ 美股盯盘（T015 S3）
// 结构镜像下面的 A 股盯盘页；只请求 /api/us_market_monitor/*，全部用 textContent 渲染，上游文本只当文本。
const USM = { built: false, token: 0, timer: null, status: null, quotes: [], alerts: [], intraday: null, busy: false };
function usmKey(prefix, value, fallback) { const key = `${prefix}.${value}`; return window.MONITOR_I18N?.has(key) ? key : fallback; }
function usmErrorKey(code) { return usmKey("usmon.err", code, mErrorKey(code)); }
function usmShow(key, kind = "error") {
  const box = $("usmMessage"); if (!box) return;
  const node = mNode("p", key); node.setAttribute("role", kind === "error" ? "alert" : "status");
  node.classList.add(kind === "error" ? "hold-error" : "hold-ok");
  box.replaceChildren(node);
}
function usmCard(id, titleKey) {
  const card = document.createElement("section"); card.classList.add("card", "hold-card", "mon-ia-card");
  const body = document.createElement("div"); body.id = id; card.append(mNode("h2", titleKey), body); return card;
}
function usmEventLine(ev, extraNode) {
  const p = monitorEventLine(ev, extraNode);
  if (ev.display_only) { const n = mNode("small", "usmon.ev.display_only"); n.classList.add("dim"); p.append(" ", n); }
  return p;
}
function usmRenderIntraday() {
  const ia = USM.intraday || { events: [], tracking: null };
  const events = Array.isArray(ia.events) ? ia.events : [];
  const list = (kinds, emptyKey, extra) => {
    const rows = events.filter(e => kinds.includes(e.kind)).slice().reverse().map(e => usmEventLine(e, extra ? extra(e) : null));
    if (!rows.length) { const p = mNode("p", emptyKey); p.classList.add("personal-state"); p.setAttribute("role", "status"); return [p]; }
    return rows;
  };
  $("usmExec")?.replaceChildren(...list(EV_EXEC, "usmon.ia.exec.empty"));
  const tracking = ia.tracking || { rows: [], summary: null };
  const rows = Array.isArray(tracking.rows) ? tracking.rows : [];
  const sinceTrigger = e => {
    const r = rows.find(x => x.type === "trigger" && x.code === e.code && x.kind === e.kind);
    if (!r || !isNum(r.return_since_trigger)) return null;
    const v = { ret: fmt(r.return_since_trigger, 2, "%") };
    const span = pNode("span", mText("mon.ev.since_trigger", v, "zh"), mText("mon.ev.since_trigger", v, "en"));
    span.classList.add(cls(r.return_since_trigger) || "flat"); return span;
  };
  $("usmOpp")?.replaceChildren(...list(EV_OPP, "usmon.ia.opp.empty", sinceTrigger));
  const sum = tracking.summary, parts = [];
  if (sum && sum.total > 0) {
    const vals = { total: sum.total, winners: sum.winners, losers: sum.losers, mean: fmt(sum.mean_return, 2), median: fmt(sum.median_return, 2) };
    const line = mNode("p", "mon.ia.summary", vals); line.classList.add("mon-ia-summary"); parts.push(line);
  } else { const line = mNode("p", "mon.ia.summary_empty"); line.classList.add("dim", "mon-ia-summary"); parts.push(line); }
  if (tracking.write_error) { const w = mNode("p", "mon.ia.write_error"); w.classList.add("hold-error"); parts.push(w); }
  const pct = v => mPlain(isNum(v) ? fmt(v, 2, "%") : "—");
  const trows = rows.filter(r => r.type === "track").map(r => {
    const tr = document.createElement("tr");
    const chg = pct(r.change_vs_open_pct); if (isNum(r.change_vs_open_pct)) chg.classList.add(cls(r.change_vs_open_pct) || "flat");
    const cum = pct(r.return_since_open); if (isNum(r.return_since_open)) cum.classList.add(cls(r.return_since_open) || "flat");
    tr.append(mCell(mPlain(r.code)), mCell(EV_ACT[r.action] ? mNode("span", EV_ACT[r.action]) : mPlain(r.action)),
      mCell(mPlain(isNum(r.open) ? fmt(r.open, 2) : "—")), mCell(mPlain(isNum(r.price) ? fmt(r.price, 2) : "—")), mCell(chg), mCell(cum),
      mCell(pct(r.to_day_high_pct)), mCell(pct(r.to_day_low_pct)));
    return tr;
  });
  parts.push(mTable(["mon.ia.col.code", "mon.ia.col.action", "mon.ia.col.open", "mon.ia.col.price", "mon.ia.col.chg", "mon.ia.col.cum", "usmon.ia.col.high", "usmon.ia.col.low"], trows, "usmon.ia.track.empty"));
  $("usmTrack")?.replaceChildren(...parts);
}
function usmBuild() {
  if (USM.built) return;
  USM.built = true;
  const page = $("p-usmonitor");
  const title = pNode("div"); title.classList.add("title"); title.append(mNode("h1", "usmon.title"));
  const note = mNode("p", "usmon.note"); note.classList.add("dim");
  const message = document.createElement("div"); message.id = "usmMessage";
  const ia = document.createElement("section"); ia.classList.add("mon-ia");
  const iaNote = mNode("p", "usmon.ia.note"); iaNote.classList.add("dim");
  const iaGrid = document.createElement("div"); iaGrid.classList.add("mon-ia-grid");
  iaGrid.append(usmCard("usmExec", "mon.ia.exec.title"), usmCard("usmTrack", "mon.ia.track.title"), usmCard("usmOpp", "mon.ia.opp.title"));
  ia.append(mNode("h2", "mon.ia.title"), iaNote, iaGrid);
  const ctl = document.createElement("section"); ctl.classList.add("card", "hold-card"); ctl.append(mNode("h2", "mon.ctl.title"));
  const line = document.createElement("div"); line.id = "usmStatus";
  const buttons = document.createElement("div");
  const start = mNode("button", "mon.btn.start"), stop = mNode("button", "mon.btn.stop");
  start.id = "usmStart"; stop.id = "usmStop";
  for (const b of [start, stop]) { b.type = "button"; b.classList.add("btn"); }
  start.classList.add("primary"); stop.classList.add("plain");
  start.addEventListener("click", () => usmAct("/api/us_market_monitor/start"));
  stop.addEventListener("click", () => usmAct("/api/us_market_monitor/stop"));
  buttons.append(start, stop);
  const cfg = document.createElement("form"); cfg.id = "usmConfig"; cfg.classList.add("hold-grid");
  const field = (id, key, value, min, max) => {
    const wrapL = document.createElement("label"); wrapL.classList.add("hold-field");
    const input = document.createElement("input"); input.type = "number"; input.id = id; input.min = min; input.max = max; input.step = "1"; input.value = value; input.inputMode = "numeric";
    wrapL.append(mNode("span", key), input); return wrapL;
  };
  const save = mNode("button", "mon.cfg.save"); save.type = "submit"; save.classList.add("btn", "plain");
  cfg.append(field("usmInterval", "usmon.cfg.interval", "300", 60, 3600), field("usmTopN", "usmon.cfg.top_n", "0", 0, 30), save);
  cfg.addEventListener("submit", ev => { ev.preventDefault(); usmSaveConfig(); });
  const hint = mNode("p", "usmon.cfg.hint"); hint.classList.add("dim");
  ctl.append(line, buttons, cfg, hint);
  const quotes = document.createElement("section"); quotes.classList.add("card", "hold-card"); quotes.append(mNode("h2", "mon.q.title"));
  const qt = mNode("p", "usmon.q.time_note"); qt.classList.add("dim");
  const qBody = document.createElement("div"); qBody.id = "usmQuotes"; quotes.append(qt, qBody);
  const alerts = document.createElement("section"); alerts.classList.add("card", "hold-card"); alerts.append(mNode("h2", "mon.a.title"));
  const aBody = document.createElement("div"); aBody.id = "usmAlerts"; alerts.append(aBody);
  page.replaceChildren(title, note, message, ia, ctl, quotes, alerts);
}
function usmRenderStatus() {
  const s = USM.status, box = $("usmStatus"); if (!box) return;
  if (!s) { box.replaceChildren(); return; }
  const parts = [];
  const st = lang => mText(usmKey("mon.state", s.state, "mon.state.stopped"), {}, lang), ph = lang => mText(usmKey("usmon.phase", s.phase, "usmon.phase.idle"), {}, lang);
  parts.push(pNode("p", mText("mon.status.line", { state: st("zh"), phase: ph("zh") }, "zh"), mText("mon.status.line", { state: st("en"), phase: ph("en") }, "en")));
  parts.push(mNode("p", "mon.status.stats", { used: s.requests_used, budget: s.request_budget, count: s.watch_count, max: s.max_watch, rounds: s.rounds }));
  if (s.state === "paused" && s.reason) {
    const rk = usmKey("usmon.reason", s.reason, "usmon.reason.provider_error");
    parts.push(pNode("p", mText("mon.reason.line", { reason: mText(rk, {}, "zh") }, "zh"), mText("mon.reason.line", { reason: mText(rk, {}, "en") }, "en")));
  }
  if (s.truncated) parts.push(mNode("p", "mon.status.truncated"));
  box.replaceChildren(...parts);
  const running = s.state === "running";
  if ($("usmStart")) $("usmStart").disabled = running || USM.busy;
  if ($("usmStop")) $("usmStop").disabled = !running && s.state !== "paused";
  const iv = $("usmInterval"), tn = $("usmTopN");
  if (iv && document.activeElement !== iv) iv.value = s.interval;
  if (tn && document.activeElement !== tn) tn.value = s.top_n;
}
function usmRenderQuotes() {
  const rows = USM.quotes.map(q => {
    const tr = document.createElement("tr");
    const pct = mPlain(isNum(q.pct) ? fmt(q.pct, 2, "%") : "—"); if (isNum(q.pct)) pct.classList.add(cls(q.pct) || "flat");
    const src = document.createElement("span");
    (Array.isArray(q.sources) ? q.sources : []).forEach((x, i) => { if (i) src.append(" / "); src.append(mNode("span", mKey("mon.src", x, "mon.src.watchlist"))); });
    tr.append(mCell(mPlain(q.code)), mCell(mPlain(q.name || "—")), mCell(mPlain(isNum(q.price) ? fmt(q.price, 2) : "—")), mCell(pct),
      mCell(mPlain(mTime(q.received_at))), mCell(mNode("span", mKey("mon.alert", q.alert, "mon.alert.none"))),
      mCell(src), mCell(mNode("span", usmKey("usmon.quality", q.quality, "usmon.quality.invalid"))));
    return tr;
  });
  $("usmQuotes")?.replaceChildren(mTable(["mon.col.code", "mon.col.name", "mon.col.price", "mon.col.pct", "mon.col.time", "mon.col.alert", "mon.col.source", "mon.col.quality"], rows, "usmon.q.empty"));
}
function usmRenderAlerts() {
  const rows = USM.alerts.slice().reverse().map(a => {
    const tr = document.createElement("tr");
    tr.append(mCell(mPlain(mTime(a.triggered_at))), mCell(mPlain(a.code)), mCell(mNode("span", mKey("mon.dir", a.direction, "mon.dir.up"))),
      mCell(mPlain(fmt(a.threshold, 2))), mCell(mPlain(`${fmt(a.previous_price, 2)} → ${fmt(a.price, 2)}`)));
    return tr;
  });
  $("usmAlerts")?.replaceChildren(mTable(["mon.a.col.time", "mon.a.col.code", "mon.a.col.dir", "mon.a.col.threshold", "mon.a.col.price"], rows, "mon.a.empty"));
}
function usmRenderAll() { usmRenderStatus(); usmRenderQuotes(); usmRenderAlerts(); usmRenderIntraday(); }
async function usmRefresh() {
  if (!personalMode() || !$("p-usmonitor")?.classList.contains("on")) return;
  const token = ++USM.token;
  const [q, a] = await Promise.all([hRequest("/api/us_market_monitor/quotes"), hRequest("/api/us_market_monitor/alerts")]);
  if (token !== USM.token) return; // 已离开本页或有更新的请求
  if (!q.ok || !a.ok) {
    USM.status = null; USM.quotes = []; USM.alerts = []; USM.intraday = null;
    usmRenderAll(); usmShow(usmErrorKey((!q.ok ? q : a).code)); return;
  }
  $("usmMessage")?.replaceChildren();
  USM.status = q.data.status; USM.quotes = q.data.quotes || []; USM.alerts = a.data.alerts || []; USM.intraday = q.data.intraday || null;
  usmRenderAll();
}
async function usmAct(path) {
  if (USM.busy) return;
  USM.busy = true;
  const r = await hRequest(path, {});
  USM.busy = false;
  if (!$("p-usmonitor")?.classList.contains("on")) return;
  if (!r.ok) { usmShow(usmErrorKey(r.code)); return; }
  await usmRefresh();
}
async function usmSaveConfig() {
  const interval = Number($("usmInterval").value), topN = Number($("usmTopN").value);
  const r = await hRequest("/api/us_market_monitor/config", { interval, top_n: topN });
  if (!$("p-usmonitor")?.classList.contains("on")) return;
  if (!r.ok) { usmShow(usmErrorKey(r.code)); return; }
  usmShow("mon.cfg.saved", "ok");
  await usmRefresh();
}
async function loadUsMonitor() {
  if (!personalMode()) return;
  usmBuild();
  if (!USM.timer) USM.timer = setInterval(usmRefresh, 10000); // 只在本页打开时轮询本机接口，离开即清除
  await usmRefresh();
}
// 离开美股盯盘页：停止页面轮询并清空内存里的报价；后台采集不受影响，需点“停止”。
function usmLeave() {
  if (USM.timer) { clearInterval(USM.timer); USM.timer = null; }
  if (!USM.built) return;
  USM.token++; USM.status = null; USM.quotes = []; USM.alerts = []; USM.intraday = null;
  ["usmStatus", "usmQuotes", "usmAlerts", "usmMessage", "usmExec", "usmTrack", "usmOpp"].forEach(id => $(id)?.replaceChildren());
}

// ================================================================ 实时盯盘（T006 S3）
// 仅个人模式存在；全部用 textContent 构建；请求带 X-Requested-With（复用 hRequest）；报价只放内存，不写任何浏览器存储。
const MON = { built: false, token: 0, timer: null, status: null, quotes: [], alerts: [], intraday: null, failed: "", busy: false };
function mText(key, values = {}, lang = hLang()) { return window.MONITOR_I18N ? window.MONITOR_I18N.text(key, values, lang) : key; }
function mNode(tag, key, values = {}) { return pNode(tag, mText(key, values, "zh"), mText(key, values, "en")); }
function mKey(prefix, value, fallback) { const key = `${prefix}.${value}`; return window.MONITOR_I18N?.has(key) ? key : fallback; }
function mErrorKey(code) { return mKey("mon.err", code, "mon.err.generic"); }
function mShow(key, kind = "error") {
  const box = $("monMessage"); if (!box) return;
  const node = mNode("p", key); node.setAttribute("role", kind === "error" ? "alert" : "status");
  node.classList.add(kind === "error" ? "hold-error" : "hold-ok");
  box.replaceChildren(node);
}
function mTime(iso) { return typeof iso === "string" && iso.length >= 19 ? iso.slice(11, 19) : "—"; }
function mTable(headKeys, rows, emptyKey) {
  if (!rows.length) { const p = mNode("p", emptyKey); p.classList.add("personal-state"); p.setAttribute("role", "status"); return p; }
  const wrap = document.createElement("div"), table = document.createElement("table"), head = document.createElement("thead"), tr = document.createElement("tr"), body = document.createElement("tbody");
  wrap.classList.add("tbl"); wrap.tabIndex = 0;
  headKeys.forEach(k => tr.append(mNode("th", k))); head.append(tr);
  rows.forEach(r => body.append(r)); table.append(head, body); wrap.append(table); return wrap;
}
function mCell(...children) { const td = document.createElement("td"); td.append(...children); return td; }
function mPlain(text) { const s = document.createElement("span"); s.textContent = String(text ?? "—"); return s; }
// 实时分析（S3b）：三栏；事件种类 -> 文案键用显式表，不拼接字符串
const EV_KEYS = { buy_window: "mon.ev.buy_window", limit_up_no_buy: "mon.ev.limit_up_no_buy", limit_down_no_sell: "mon.ev.limit_down_no_sell", holding_drop: "mon.ev.holding_drop", watch: "mon.ev.watch", consider_sell: "mon.ev.consider_sell" };
const EV_TRIG = { momentum: "mon.ev.trig.momentum", volume: "mon.ev.trig.volume", breakout: "mon.ev.trig.breakout" };
const EV_ACT = { "买入": "mon.ia.act.buy", "卖出": "mon.ia.act.sell", "加仓": "mon.ia.act.add", "减仓": "mon.ia.act.trim" };
const EV_EXEC = ["buy_window", "limit_up_no_buy", "limit_down_no_sell", "holding_drop"], EV_OPP = ["watch", "consider_sell"];
function monitorIaCard(id, titleKey) {
  const card = document.createElement("section"); card.classList.add("card", "hold-card", "mon-ia-card");
  const body = document.createElement("div"); body.id = id; card.append(mNode("h2", titleKey), body); return card;
}
function monitorEventLine(ev, extraNode) {
  const p = document.createElement("p"); p.classList.add("mon-ev", ev.severity === "ok" ? "mon-ev-ok" : ev.severity === "watch" ? "mon-ev-watch" : "mon-ev-danger");
  const params = ev.params || {};
  const trig = lang => (Array.isArray(params.triggers) ? params.triggers : []).map(t => (EV_TRIG[t] ? mText(EV_TRIG[t], {}, lang) : "")).join(" / ");
  const vals = lang => ({ code: String(ev.code), minutes: String(params.minutes ?? ""), pct: isNum(params.pct) ? Math.abs(params.pct).toFixed(2) : "", triggers: trig(lang) });
  const key = EV_KEYS[ev.kind] || EV_KEYS.watch;
  p.append(mPlain(mTime(ev.timestamp) + " "), pNode("span", mText(key, vals("zh"), "zh"), mText(key, vals("en"), "en")));
  if (extraNode) p.append(" ", extraNode);
  if (ev.reference_only) { const note = mNode("small", "mon.ev.note"); note.classList.add("dim"); p.append(" ", note); }
  return p;
}
function monitorRenderIntraday() {
  const ia = MON.intraday || { events: [], tracking: null };
  const events = Array.isArray(ia.events) ? ia.events : [];
  const list = (kinds, emptyKey, extra) => {
    const rows = events.filter(e => kinds.includes(e.kind)).slice().reverse().map(e => monitorEventLine(e, extra ? extra(e) : null));
    if (!rows.length) { const p = mNode("p", emptyKey); p.classList.add("personal-state"); p.setAttribute("role", "status"); return [p]; }
    return rows;
  };
  $("monExec")?.replaceChildren(...list(EV_EXEC, "mon.ia.exec.empty"));
  const tracking = ia.tracking || { rows: [], summary: null };
  const rows = Array.isArray(tracking.rows) ? tracking.rows : [];
  const sinceTrigger = e => {
    const r = rows.find(x => x.type === "trigger" && x.code === e.code && x.kind === e.kind);
    if (!r || !isNum(r.return_since_trigger)) return null;
    const v = { ret: fmt(r.return_since_trigger, 2, "%") };
    const span = pNode("span", mText("mon.ev.since_trigger", v, "zh"), mText("mon.ev.since_trigger", v, "en"));
    span.classList.add(cls(r.return_since_trigger) || "flat"); return span;
  };
  $("monOpp")?.replaceChildren(...list(EV_OPP, "mon.ia.opp.empty", sinceTrigger));
  const sum = tracking.summary, parts = [];
  if (sum && sum.total > 0) {
    const vals = { total: sum.total, winners: sum.winners, losers: sum.losers, mean: fmt(sum.mean_return, 2), median: fmt(sum.median_return, 2) };
    const line = mNode("p", "mon.ia.summary", vals); line.classList.add("mon-ia-summary"); parts.push(line);
  } else { const line = mNode("p", "mon.ia.summary_empty"); line.classList.add("dim", "mon-ia-summary"); parts.push(line); }
  if (tracking.write_error) { const w = mNode("p", "mon.ia.write_error"); w.classList.add("hold-error"); parts.push(w); }
  const pct = v => mPlain(isNum(v) ? fmt(v, 2, "%") : "—");
  const trows = rows.filter(r => r.type === "track").map(r => {
    const tr = document.createElement("tr");
    const chg = pct(r.change_vs_open_pct); if (isNum(r.change_vs_open_pct)) chg.classList.add(cls(r.change_vs_open_pct) || "flat");
    const cum = pct(r.return_since_open); if (isNum(r.return_since_open)) cum.classList.add(cls(r.return_since_open) || "flat");
    tr.append(mCell(mPlain(r.code)), mCell(EV_ACT[r.action] ? mNode("span", EV_ACT[r.action]) : mPlain(r.action)),
      mCell(mPlain(isNum(r.open) ? fmt(r.open, 2) : "—")), mCell(mPlain(isNum(r.price) ? fmt(r.price, 2) : "—")), mCell(chg), mCell(cum),
      mCell(pct(r.to_limit_up_pct)), mCell(pct(r.to_limit_down_pct)));
    return tr;
  });
  parts.push(mTable(["mon.ia.col.code", "mon.ia.col.action", "mon.ia.col.open", "mon.ia.col.price", "mon.ia.col.chg", "mon.ia.col.cum", "mon.ia.col.up", "mon.ia.col.down"], trows, "mon.ia.track.empty"));
  $("monTrack")?.replaceChildren(...parts);
}
function monitorBuild() {
  if (MON.built) return;
  MON.built = true;
  const page = $("p-monitor");
  const title = pNode("div"); title.classList.add("title"); title.append(mNode("h1", "mon.title"));
  const note = mNode("p", "mon.note"); note.classList.add("dim");
  const message = document.createElement("div"); message.id = "monMessage";
  const ia = document.createElement("section"); ia.id = "monIntraday"; ia.classList.add("mon-ia");
  const iaNote = mNode("p", "mon.ia.note"); iaNote.classList.add("dim");
  const iaGrid = document.createElement("div"); iaGrid.classList.add("mon-ia-grid");
  iaGrid.append(monitorIaCard("monExec", "mon.ia.exec.title"), monitorIaCard("monTrack", "mon.ia.track.title"), monitorIaCard("monOpp", "mon.ia.opp.title"));
  ia.append(mNode("h2", "mon.ia.title"), iaNote, iaGrid);
  const ctl = document.createElement("section"); ctl.classList.add("card", "hold-card"); ctl.append(mNode("h2", "mon.ctl.title"));
  const line = document.createElement("div"); line.id = "monStatus";
  const buttons = document.createElement("div");
  const start = mNode("button", "mon.btn.start"), stop = mNode("button", "mon.btn.stop");
  start.id = "monStart"; stop.id = "monStop";
  for (const b of [start, stop]) { b.type = "button"; b.classList.add("btn"); }
  start.classList.add("primary"); stop.classList.add("plain");
  start.addEventListener("click", () => monitorAct("/api/market_monitor/start"));
  stop.addEventListener("click", () => monitorAct("/api/market_monitor/stop"));
  buttons.append(start, stop);
  const cfg = document.createElement("form"); cfg.id = "monConfig"; cfg.classList.add("hold-grid");
  const field = (id, key, value, min, max) => {
    const wrapL = document.createElement("label"); wrapL.classList.add("hold-field");
    const input = document.createElement("input"); input.type = "number"; input.id = id; input.min = min; input.max = max; input.step = "1"; input.value = value; input.inputMode = "numeric";
    wrapL.append(mNode("span", key), input); return wrapL;
  };
  const save = mNode("button", "mon.cfg.save"); save.type = "submit"; save.classList.add("btn", "plain");
  // T016 S4：数据源下拉 + 批量开关 + 名单上限。provider 与 batch_mode 是同一件事（tencent=逐只，easy=批量），两个控件互相同步
  const provWrap = document.createElement("label"); provWrap.classList.add("hold-field");
  const prov = document.createElement("select"); prov.id = "monProvider";
  for (const [value, key] of [["tencent", "mon.cfg.provider.tencent"], ["easy", "mon.cfg.provider.easy"]]) {
    const opt = mNode("option", key); opt.value = value; prov.append(opt);
  }
  provWrap.append(mNode("span", "mon.cfg.provider"), prov);
  const batchWrap = document.createElement("label"); batchWrap.classList.add("hold-field");
  const batch = document.createElement("input"); batch.type = "checkbox"; batch.id = "monBatch";
  batchWrap.append(mNode("span", "mon.cfg.batch_mode"), batch);
  prov.addEventListener("change", () => { batch.checked = prov.value === "easy"; });
  batch.addEventListener("change", () => { prov.value = batch.checked ? "easy" : "tencent"; });
  cfg.append(field("monInterval", "mon.cfg.interval", "60", MON_BATCH_MIN_INTERVAL, 3600), field("monTopN", "mon.cfg.top_n", "20", 0, 5500),
    provWrap, field("monMaxWatch", "mon.cfg.max_watch", "500", 1, 5500), batchWrap, save);
  cfg.addEventListener("submit", ev => { ev.preventDefault(); monitorSaveConfig(); });
  const hint = mNode("p", "mon.cfg.hint"); hint.classList.add("dim");
  const hint2 = mNode("p", "mon.cfg.hint_provider"); hint2.classList.add("dim");
  ctl.append(line, buttons, cfg, hint, hint2);
  const quotes = document.createElement("section"); quotes.classList.add("card", "hold-card"); quotes.append(mNode("h2", "mon.q.title"));
  const qt = mNode("p", "mon.q.time_note"); qt.classList.add("dim");
  const qBody = document.createElement("div"); qBody.id = "monQuotes"; quotes.append(qt, qBody);
  const alerts = document.createElement("section"); alerts.classList.add("card", "hold-card"); alerts.append(mNode("h2", "mon.a.title"));
  const aBody = document.createElement("div"); aBody.id = "monAlerts"; alerts.append(aBody);
  page.replaceChildren(title, note, message, ia, ctl, quotes, alerts);
}
function monitorRenderStatus() {
  const s = MON.status, box = $("monStatus"); if (!box) return;
  if (!s) { box.replaceChildren(); return; }
  const parts = [];
  const mk = (zh, en) => { const p = pNode("p", zh, en); return p; };
  const st = (lang) => mText(mKey("mon.state", s.state, "mon.state.stopped"), {}, lang), ph = (lang) => mText(mKey("mon.phase", s.phase, "mon.phase.idle"), {}, lang);
  parts.push(mk(mText("mon.status.line", { state: st("zh"), phase: ph("zh") }, "zh"), mText("mon.status.line", { state: st("en"), phase: ph("en") }, "en")));
  const vals = { used: s.requests_used, budget: s.request_budget, count: s.watch_count, max: s.max_watch, rounds: s.rounds };
  parts.push(mNode("p", "mon.status.stats", vals));
  if (s.state === "paused" && s.reason) {
    const rk = mKey("mon.reason", s.reason, "mon.reason.provider_error");
    parts.push(mk(mText("mon.reason.line", { reason: mText(rk, {}, "zh") }, "zh"), mText("mon.reason.line", { reason: mText(rk, {}, "en") }, "en")));
  }
  if (s.truncated) parts.push(mNode("p", "mon.status.truncated"));
  // 降级（批量连续失败，实际在用 tencent）与“请求了批量但没装 easyquotation”是两种不同状态
  if (s.degraded) parts.push(mNode("p", "mon.status.degraded"));
  else if (s.batch_requested === true && s.batch_mode === false) parts.push(mNode("p", "mon.status.easy_missing"));
  box.replaceChildren(...parts);
  const running = s.state === "running";
  if ($("monStart")) $("monStart").disabled = running || MON.busy;
  if ($("monStop")) $("monStop").disabled = !running && s.state !== "paused";
  const iv = $("monInterval"), tn = $("monTopN");
  if (iv && document.activeElement !== iv) iv.value = s.interval;
  if (tn && document.activeElement !== tn) tn.value = s.top_n;
  // 数据源相关控件：用户正在操作时不覆盖
  const pv = $("monProvider"), mw = $("monMaxWatch"), bm = $("monBatch");
  // 下拉框显示“用户请求的”数据源（降级时仍是 easy），实际生效的由上面的状态提示说明
  const reqProv = monitorRequestedProvider(s);
  if (pv && document.activeElement !== pv && (reqProv === "easy" || reqProv === "tencent")) pv.value = reqProv;
  if (bm && document.activeElement !== bm && (reqProv === "easy" || reqProv === "tencent")) bm.checked = reqProv === "easy";
  if (mw && document.activeElement !== mw && isNum(s.max_watch)) mw.value = s.max_watch;
}
function monitorRenderQuotes() {
  const rows = MON.quotes.map(q => {
    const tr = document.createElement("tr");
    const pct = mPlain(isNum(q.pct) ? fmt(q.pct, 2, "%") : "—"); if (isNum(q.pct)) pct.classList.add(cls(q.pct) || "flat");
    const src = document.createElement("span");
    (Array.isArray(q.sources) ? q.sources : []).forEach((x, i) => { if (i) src.append(" / "); src.append(mNode("span", mKey("mon.src", x, "mon.src.watchlist"))); });
    tr.append(mCell(mPlain(q.code)), mCell(mPlain(q.name || "—")), mCell(mPlain(isNum(q.price) ? fmt(q.price, 2) : "—")), mCell(pct),
      mCell(mPlain(mTime(q.received_at))), mCell(mNode("span", mKey("mon.alert", q.alert, "mon.alert.none"))),
      mCell(src), mCell(mNode("span", mKey("mon.quality", q.quality, "mon.quality.pending"))));
    return tr;
  });
  $("monQuotes")?.replaceChildren(mTable(["mon.col.code", "mon.col.name", "mon.col.price", "mon.col.pct", "mon.col.time", "mon.col.alert", "mon.col.source", "mon.col.quality"], rows, "mon.q.empty"));
}
function monitorRenderAlerts() {
  const rows = MON.alerts.slice().reverse().map(a => {
    const tr = document.createElement("tr");
    tr.append(mCell(mPlain(mTime(a.triggered_at))), mCell(mPlain(a.code)), mCell(mNode("span", mKey("mon.dir", a.direction, "mon.dir.up"))),
      mCell(mPlain(fmt(a.threshold, 2))), mCell(mPlain(`${fmt(a.previous_price, 2)} → ${fmt(a.price, 2)}`)));
    return tr;
  });
  $("monAlerts")?.replaceChildren(mTable(["mon.a.col.time", "mon.a.col.code", "mon.a.col.dir", "mon.a.col.threshold", "mon.a.col.price"], rows, "mon.a.empty"));
}
async function monitorRefresh() {
  if (!personalMode() || !$("p-monitor")?.classList.contains("on")) return;
  const token = ++MON.token;
  const [q, a] = await Promise.all([hRequest("/api/market_monitor/quotes"), hRequest("/api/market_monitor/alerts")]);
  if (token !== MON.token) return; // 已离开本页或有更新的请求
  if (!q.ok || !a.ok) {
    MON.status = null; MON.quotes = []; MON.alerts = []; MON.intraday = null;
    monitorRenderStatus(); monitorRenderQuotes(); monitorRenderAlerts(); monitorRenderIntraday(); mShow(mErrorKey((!q.ok ? q : a).code)); return;
  }
  $("monMessage")?.replaceChildren();
  MON.status = q.data.status; MON.quotes = q.data.quotes || []; MON.alerts = a.data.alerts || []; MON.intraday = q.data.intraday || null;
  monitorRenderStatus(); monitorRenderQuotes(); monitorRenderAlerts(); monitorRenderIntraday();
}
async function monitorAct(path) {
  if (MON.busy) return;
  MON.busy = true;
  const r = await hRequest(path, {});
  MON.busy = false;
  if (!$("p-monitor")?.classList.contains("on")) return;
  if (!r.ok) { mShow(mErrorKey(r.code)); return; }
  await monitorRefresh();
}
// 逐只（tencent）模式的最小采样间隔，与后端 MIN_INTERVAL 一致
const MON_SINGLE_MIN_INTERVAL = 30;
// 批量模式的最小采样间隔，与后端 BATCH_MIN_INTERVAL（= providers_easy.REQUEST_GAP = 10）一致
const MON_BATCH_MIN_INTERVAL = 10;
// 用户“请求的”数据源：降级时 status.provider 是实际生效的 tencent，但用户请求的仍是 easy（batch_requested）
// 只有“降级”时请求值才和实际值不同；没装 easyquotation 时 batch_requested=true 但后端不会因提交 tencent 而改它，所以那种情况按实际值算
function monitorRequestedProvider(s) {
  if (s.degraded === true && typeof s.batch_requested === "boolean") return s.batch_requested ? "easy" : "tencent";
  return s.provider;
}
// 纯函数：根据当前状态和表单值，算出要依次提交的请求体（不碰 DOM，便于单测）
function monitorConfigPlan(s, form) {
  s = s || {};
  const body = { interval: form.interval, top_n: form.topN };
  // 与“请求值”比较而不是与实际生效值比较：降级后用户选 tencent 才会真正提交，降级状态才会被固化成回滚
  if (form.provider !== monitorRequestedProvider(s)) { body.provider = form.provider; body.batch_mode = form.provider === "easy"; }
  if (form.maxWatch !== s.max_watch) body.max_watch = form.maxWatch;
  const plan = [];
  // 后端 set_runtime 先拿旧间隔按新模式校验；批量(<30 秒)切回 tencent 并同时把间隔改到 30 秒以上时，先单独保存间隔
  if (body.provider === "tencent" && typeof s.interval === "number" && s.interval < MON_SINGLE_MIN_INTERVAL
      && form.interval >= MON_SINGLE_MIN_INTERVAL) plan.push({ interval: form.interval });
  plan.push(body);
  return plan;
}
async function monitorSaveConfig() {
  const form = { interval: Number($("monInterval").value), topN: Number($("monTopN").value),
    provider: $("monProvider").value, maxWatch: Number($("monMaxWatch").value) };
  for (const body of monitorConfigPlan(MON.status, form)) {
    const r = await hRequest("/api/market_monitor/config", body);
    if (!$("p-monitor")?.classList.contains("on")) return;
    if (!r.ok) {
      // 选 easy 被拒绝最常见的原因是本机没装 easyquotation，页面分不出具体原因，给固定提示
      mShow(body.provider === "easy" ? "mon.cfg.err_easy" : mErrorKey(r.code)); return;
    }
  }
  mShow("mon.cfg.saved", "ok");
  await monitorRefresh();
}
async function loadMonitor() {
  if (!personalMode()) return;
  monitorBuild();
  if (!MON.timer) MON.timer = setInterval(monitorRefresh, 10000); // 只在本页打开时轮询本机接口，离开即清除
  await monitorRefresh();
}
// 离开盯盘页：停止页面轮询并清空内存里的报价（盯盘数据不留在页面里）；后台采集不受影响，需点“停止”。
function monitorLeave() {
  if (MON.timer) { clearInterval(MON.timer); MON.timer = null; }
  if (!MON.built) return;
  MON.token++; MON.status = null; MON.quotes = []; MON.alerts = []; MON.intraday = null;
  ["monStatus", "monQuotes", "monAlerts", "monMessage", "monExec", "monTrack", "monOpp"].forEach(id => $(id)?.replaceChildren());
}

const PAGES = ["trade", "review", "ai", "today", "market", "watch", "stock", "lab", "track", "bt", "rq", "cmp", "code", "translator", "holdings", "monitor", "usmonitor"];
function go(hash) { if (location.hash !== hash) location.hash = hash; else route(); }
function route() {
  if (personalMode()) {
    PERSONAL.view++;
    TRADE.request++;
    const alias = /^#\/(review|track)(?:[/?]|$)/.exec(location.hash);
    if (alias) PERSONAL.section = alias[1];
  }
  const target = personalHash(location.hash || "#/today");
  if (personalMode() && target !== location.hash) {
    // 替换当前条目，刷新/前进/后退均经过同一兜底；不丢失别名后的参数。
    history.replaceState(null, "", target);
  }
  const [, page = "today", arg = null] = (target).split("/");
  const name = personalMode() ? page.split("?")[0] : page;
  // 持仓页只在个人模式存在；其他情况一律回今日，且不发任何 /api/holdings 请求。
  const p = PAGES.includes(name) && ((name !== "holdings" && name !== "monitor") || personalMode()) && (name !== "usmonitor" || personalMode()) ? name : "today";
  if (p !== "holdings") holdingsLeave();
  if (p !== "trade") hLinkClear();
  if (p !== "monitor") monitorLeave();
  if (p !== "usmonitor") usmLeave();
  S.routeArg = arg;
  if (personalMode() && p === "trade") {
    const query = new URLSearchParams(target.split("?")[1] || "");
    const path = target.split("?")[0].split("/");
    S.personalSelection = {strategy: query.get("strategy") || path[2], date: query.get("date") || path[3]};
    for (const page of ["review", "track"]) {
      const block = $(`personal-${page}`);
      if (PERSONAL.section === page) { block.open = true; block.scrollIntoView?.(); }
      if (block.open) pLoadSection(page);
    }
    PERSONAL.section = "";
  }
  document.querySelectorAll(".page").forEach((el) => el.classList.toggle("on", el.id === `p-${p}`));
  const stockPage = ["market", "watch", "stock"].includes(p);
  document.querySelectorAll("#tabs a[data-p]").forEach((a) => a.classList.toggle("on", a.dataset.p === p));
  const mo = document.querySelector("#tabs .more"); if (mo) mo.classList.toggle("on", ["bt", "rq", "cmp", "code"].includes(p));
  window.scrollTo({ top: 0 });
  ({ trade: loadTrade, review: loadReview, ai: loadAi, translator: async () => window.AI_TRANSLATOR?.load(), today: loadToday, market: loadMarket, watch: loadWatch, stock: loadStock, lab: loadLab, track: loadTrack, holdings: loadHoldings, monitor: loadMonitor, usmonitor: loadUsMonitor, ...(window.ENG_LOADERS || {}) })[p]()
    .catch((e) => { console.error(e); });
}
async function updateFresh() {
  const el = $("fresh"); if (!el || !S.meta) return;
  try {
    const parts = await Promise.all(Object.keys(S.meta.markets).map(async (k) => {
      const s = await api(`/api/summary?market=${k}`);
      if (!s || s.error || !s.data_date) return "";
      const age = Math.floor((Date.now() - new Date(s.data_date + "T12:00:00").getTime()) / 86400000);
      const c = age > 6 ? "old" : age > 3 ? "warn" : "";
      return `<i class="${c}" title="数据日期 ${esc(s.data_date)}（${esc(s.timezone_label || "")}），距今 ${age} 天${c ? "，数据偏旧，建议更新" : ""}">${esc(S.meta.markets[k].name)} ${esc(s.data_date.slice(5))}</i>`;
    }));
    el.innerHTML = parts.join(""); el.hidden = !parts.some(Boolean);
  } catch { el.hidden = true; }
}
const openStock = (code) => go(`#/stock/${code}`);

// ================================================================ 今日
async function loadToday() {
  if (personalMode()) return loadPersonalToday();
  $("cards").innerHTML = '<div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div>';
  const [s, tr] = await Promise.all([api(`/api/summary?market=${S.market}`), api(`/api/tracking?market=${S.market}`)]);
  S.summary = s;
  updateFresh();
  if (s.error) { $("marketOverview").innerHTML = ""; $("cards").innerHTML = emptyState(esc(s.error)); $("notes").innerHTML = ""; $("watchTbl").innerHTML = ""; return; }
  $("todayMeta").textContent = `数据日期 ${s.data_date} · ${s.timezone_label || ""} · 更新于 ${s.generated} ${s.timezone_label || ""}`;
  $("health").innerHTML = s.health.length ? `<div class="banner">${s.health.map((h) => `<div>${esc(h)}</div>`).join("")}</div>` : "";
  const benchmark = s.watch.find((w) => w.symbol === s.main);
  $("marketOverview").innerHTML = benchmark ? `
    <div class="market-head"><h2>${esc(S.meta.markets[S.market].name)}市场概览 <small>${esc(s.main_name)} · ${esc(s.main)}</small></h2><span class="dim">截至 ${esc(s.data_date)} · ${esc(s.timezone_label || "")}</span></div>
    <div class="market-kpis">
      <div class="market-kpi"><span>收盘价</span><b>${fmt(benchmark.close)}</b></div>
      <div class="market-kpi"><span>今日涨跌</span><b class="${cls(benchmark.d1)}">${pct(benchmark.d1, 2)}</b></div>
      <div class="market-kpi"><span>今年以来</span><b class="${cls(benchmark.ytd)}">${pct(benchmark.ytd)}</b></div>
      <div class="market-kpi"><span>距 MA300</span><b class="${cls(benchmark.ma300_gap)}">${pct(benchmark.ma300_gap)}</b></div>
    </div>
    <div class="market-riskline"><span>20 日年化波动率 <b>${fmt(benchmark.vol, 1, "%")}</b></span><span>距 52 周高点 <b class="${cls(benchmark.dd1y)}">${pct(benchmark.dd1y)}</b></span><span>RSI(14) <b>${fmt(benchmark.rsi14, 1)}</b></span></div>`
    : `<div class="market-head"><h2>${esc(S.meta.markets[S.market].name)}市场概览</h2><span class="dim">暂无基准行情</span></div>`;
  const lead = s.strategies.find((c) => c.key === "blend2" && !c.error) || s.strategies.find((c) => !c.error);
  $("cards").innerHTML = lead ? `
    <div class="card scard today-hero" data-k="${esc(lead.key)}">
      <div class="head"><span class="name">组合建议 · ${esc(lead.label)}</span>${pill(lead.action)}</div>
      <div class="big">${fmt(lead.next_total * 100, 0, "%")}<small>下一交易日目标仓位</small></div>
      <div class="meter"><i style="width:${Math.min(100, (lead.next_total || 0) * 100)}%"></i></div>
      <div class="hero-meta"><span class="dim">当前仓位　<b>${esc(lead.now)}</b></span><span class="dim">目标仓位　<b>${esc(lead.next)}</b></span></div>
      <div class="reason">${esc(Number(lead.next_total) <= 1e-9 ? `当前建议空仓，原因：${lead.reason}` : lead.reason)}</div>
      <div class="kv"><div><span>组合年化</span><b>${fmt(lead.stats["CAGR%"], 1, "%")}</b></div><div><span>超额 Sharpe</span><b>${fmt(lead.stats.ExcessSharpe, 2)}</b></div><div><span>最大回撤</span><b>${fmt(lead.stats["MaxDD%"], 1, "%")}</b></div><div><span>年化波动</span><b>${fmt(lead.stats["Vol%"], 1, "%")}</b></div></div>
      <div class="why"><h3>判断依据</h3>
        ${s.strategies.filter((c) => c !== lead && !c.error && ["trend_ens", "donchian_s1", "rsi2"].includes(c.key)).map((c) => `<div class="why-row"><span>${esc(c.label)}</span><span>${pill(c.action)}</span><b>${fmt(c.next_total * 100, 0, "%")}</b></div>`).join("")}
        ${benchmark ? `<div class="why-row"><span>价格相对 MA300</span><span></span><b class="${cls(benchmark.ma300_gap)}">${pct(benchmark.ma300_gap)}</b></div><div class="why-row"><span>RSI(14)</span><span></span><b>${fmt(benchmark.rsi14, 1)}</b></div><div class="why-row"><span>距 52 周高点</span><span></span><b class="${cls(benchmark.dd1y)}">${pct(benchmark.dd1y)}</b></div>` : ""}
        <div class="dim" style="margin-top:8px;font-size:12px">组合仓位是三个子策略仓位的加权平均；三个子策略全部空仓时，组合就是空仓。</div>
      </div>
    </div>` : emptyState("当前没有可用的策略信号");
  const others = s.strategies.filter((c) => c !== lead);
  $("strategyTbl").innerHTML = `<thead><tr><th>策略</th><th>信号</th><th>目标仓位</th><th>超额 Sharpe</th><th>最大回撤</th></tr></thead><tbody>` +
    others.map((c) => c.error ? `<tr><td>${esc(c.label)}</td><td colspan="4" class="dim">${esc(c.error)}</td></tr>` : `<tr><td>${esc(c.label)}</td><td>${pill(c.action)}</td><td>${fmt(c.next_total * 100, 0, "%")}</td><td>${fmt(c.stats.ExcessSharpe, 2)}</td><td>${fmt(c.stats["MaxDD%"], 1, "%")}</td></tr>`).join("") + "</tbody>";
  document.querySelectorAll(".scard").forEach((el) => el.addEventListener("click", () => {
    store.set("dStrategy", el.dataset.k); openStock(s.main);
  }));
  $("notes").innerHTML = s.notes.map((n) => `<li>${esc(n)}</li>`).join("");
  const nt = tr.notifications || [];
  $("notifs").innerHTML = nt.length ? nt.slice(0, 6).map((n) => `<li><div><div class="dim">${esc(n.time)}</div>${esc(n.body)}</div></li>`).join("") : `<li class="dim">还没有提醒。每天更新完会在 Mac 右上角弹出通知。</li>`;
  $("watchTbl").innerHTML = `<thead><tr><th>股票 / Stock · 代码 / Symbol</th><th>收盘</th><th>今日涨跌</th><th>今年以来</th><th>距 MA300</th><th>20日波动率</th><th>距52周高点</th><th>RSI(14)</th></tr></thead><tbody>` +
    s.watch.map((w) => `<tr class="click" data-c="${esc(w.symbol)}"><td>${esc(w.name)}<span class="code">${esc(w.symbol)}</span></td>
      <td>${fmt(w.close)}</td><td class="${cls(w.d1)}">${pct(w.d1, 2)}</td><td class="${cls(w.ytd)}">${pct(w.ytd)}</td>
      <td class="${cls(w.ma300_gap)}">${pct(w.ma300_gap)}</td><td>${fmt(w.vol, 1, "%")}</td><td class="${cls(w.dd1y)}">${pct(w.dd1y)}</td><td>${fmt(w.rsi14, 1)}</td></tr>`).join("") + "</tbody>";
  $("watchTbl").querySelectorAll("tr.click").forEach((tr2) => tr2.addEventListener("click", () => openStock(tr2.dataset.c)));
}

// ================================================================ 今日买卖
const TRADE = { inited: false, data: null, tab: "buy", sort: "code", dir: 1, manualSort: false, request: 0 };
const TRADE_COLUMNS = [["code", "股票 / 代码"], ["close", "收盘价"], ["d1", "当天涨跌幅"],
  ["avg_turnover_20d", "20 日平均成交额（元）"], ["liquidity", "流动性"], ["lot_cost", "一手约需（元）"],
  ["action", "信号"], ["now", "今日仓位"], ["next", "明日目标仓位"], ["reason", "触发原因"],
  ["total", "样本外累计收益"], ["b_total", "样本外持有累计收益"], ["beat_bh", "样本外跑赢持有"],
  ["maxdd", "样本外最大回撤"], ["trades", "样本外完整交易"], ["sharpe", "样本外超额 Sharpe"], ["flags", "可交易性"]];
const tradeValue = (row, key) => row.performance?.[key] ?? row[key] ?? null;
const tradeNumber = (v, suffix = "", digits = 2) => isNum(v) ? `${Number(v).toFixed(digits)}${suffix}` : "数据不足";
// 手机宽度（≤600px）用卡片代替宽表格；桌面保持表格。
const TRADE_MOBILE_QUERY = "(max-width: 600px)";
const tradeIsMobile = () => typeof window !== "undefined" && typeof window.matchMedia === "function" && window.matchMedia(TRADE_MOBILE_QUERY).matches;
const tradePosition = (v) => isNum(v) ? tradeNumber(v * 100, "%", 1) : "数据不足";
// 没有信号的相关股票（仓位和标记都为空）显示“无信号”；有异常标记但仓位缺失仍显示“数据不足”。
const tradeTargetText = (r) => (!r.action && !isNum(r.now) && !isNum(r.next) && !(r.flags?.length)) ? "无信号" : tradePosition(r.next);
function tradeCard(r, exception) {
  const p = r.performance || {};
  const flags = r.flags?.length ? r.flags.map(esc).join("；") : "未发现本页列出的限制（不保证成交）";
  const change = exception ? "—" : (isNum(r.d1) ? pct(r.d1, 2) : "数据不足");
  const outcome = typeof p.beat_bh === "boolean" ? (p.beat_bh ? "是" : "否") : "数据不足";
  return `<article class="trade-card${exception ? " trade-card-exception" : ""}">
    <div class="trade-card-head"><a href="#/stock/${encodeURIComponent(r.code)}" data-trade-stock="${esc(r.code)}"><strong>${esc(r.name)}</strong><span class="sub2">${esc(r.code)} · 数据 ${esc(r.last_date)}</span></a><span class="trade-card-signal">${r.action ? pill(r.action) : "无有效动作"}</span></div>
    ${exception ? `<div class="trade-card-flags trade-warning">${flags}</div>` : ""}
    <dl class="trade-card-grid">
      <div><dt>今日 → 明日目标仓位</dt><dd class="trade-target">${tradePosition(r.now)} → <strong>${tradeTargetText(r)}</strong></dd></div>
      <div><dt>当天涨跌幅</dt><dd class="${exception ? "" : cls(r.d1)}">${change}</dd></div>
      <div><dt>流动性</dt><dd>${esc(r.liquidity || "数据不足")}</dd></div>
      <div><dt>一手约需（元）</dt><dd title="参考，不含手续费；次日开盘价可能不同；A 股 T+1，今天买入当天不能卖出">${tradeNumber(r.lot_cost)}</dd></div>
    </dl>
    <details class="trade-card-more"><summary>查看详情</summary>
      <div class="trade-card-reason">${esc(r.reason)}</div>
      <dl class="trade-card-detail">
        <div><dt>收盘价</dt><dd>${tradeNumber(r.close)} <span class="sub2">${esc(r.price_note || "数据不足")}</span></dd></div>
        <div><dt>20 日平均成交额（元）</dt><dd>${tradeNumber(r.avg_turnover_20d, "", 0)}</dd></div>
        <div><dt>样本外累计收益</dt><dd>${tradeNumber(p.total, "%")}</dd></div>
        <div><dt>样本外持有累计收益</dt><dd>${tradeNumber(p.b_total, "%")}</dd></div>
        <div><dt>样本外跑赢持有</dt><dd>${outcome}</dd></div>
        <div><dt>样本外最大回撤</dt><dd>${tradeNumber(p.maxdd, "%")}</dd></div>
        <div><dt>样本外完整交易</dt><dd>${tradeNumber(p.trades, "", 0)}${p.small_sample ? '<span class="sub2 trade-warning">样本太少，不可靠</span>' : ""}</dd></div>
        <div><dt>样本外超额 Sharpe</dt><dd>${tradeNumber(p.sharpe)}</dd></div>
        <div class="wide"><dt>可交易性</dt><dd>${flags}</dd></div>
      </dl>
    </details></article>`;
}
function tradeCards(rows, table, exception) {
  table.innerHTML = '<tbody><tr><td class="trade-cards-cell"><div class="trade-cards">' +
    (rows.length ? rows.map((r) => tradeCard(r, exception)).join("") : '<div class="empty">无匹配记录</div>') + "</div></td></tr></tbody>";
  table.querySelectorAll("[data-trade-stock]").forEach((a) => a.addEventListener("click", () => {
    store.set("dStrategy", $("tradeStrategy").value);
  }));
}
// 手机上没有表头可点，用下拉框和方向按钮代替排序；窗口跨过 600px 时重新渲染。
function tradeInitMobile() {
  const sortable = TRADE_COLUMNS.filter(([key]) => !["reason", "flags"].includes(key));
  if (personalMode()) $("tradeSortMobile").replaceChildren(...sortable.map(([key, label]) => { const o = pNode("option", label, PERSONAL_TRADE_LABELS[key]); o.value = key; return o; }));
  else $("tradeSortMobile").innerHTML = sortable.map(([key, label]) => `<option value="${esc(key)}">${esc(label)}</option>`).join("");
  $("tradeSortMobile").addEventListener("change", () => {
    TRADE.sort = $("tradeSortMobile").value; TRADE.dir = TRADE.sort === "code" ? 1 : -1; TRADE.manualSort = true; renderTrade();
  });
  $("tradeSortDir").addEventListener("click", () => { TRADE.dir = -TRADE.dir; TRADE.manualSort = true; renderTrade(); });
  if (typeof window !== "undefined" && typeof window.matchMedia === "function") {
    const media = window.matchMedia(TRADE_MOBILE_QUERY);
    const redraw = () => renderTrade();
    if (media.addEventListener) media.addEventListener("change", redraw); else if (media.addListener) media.addListener(redraw);
  }
}
function tradeSyncSortControls() {
  const select = $("tradeSortMobile"), button = $("tradeSortDir");
  if (select) select.value = TRADE.sort;
  if (button) {
    button.textContent = TRADE.dir === 1 ? "↑ 升序" : "↓ 降序";
    button.setAttribute("aria-label", TRADE.dir === 1 ? "当前升序，点击改为降序" : "当前降序，点击改为升序");
  }
}
function tradeRows(rows, table, sortable = true, options = {}) {
  if (personalMode()) return pTradeRows(rows, table, sortable, options);
  const exception = options.exception === true;
  if (tradeIsMobile()) { tradeCards(rows, table, exception); return; }
  const head = TRADE_COLUMNS.map(([key, label]) => {
    const sort = sortable && !["reason", "flags"].includes(key);
    const active = sort && TRADE.sort === key;
    return `<th scope="col" ${sort ? `class="sort ${active ? "on" : ""}" data-sort="${key}" tabindex="0" aria-sort="${active ? (TRADE.dir === 1 ? "ascending" : "descending") : "none"}"` : ""}>${esc(label)}${active ? (TRADE.dir === 1 ? " ↑" : " ↓") : ""}</th>`;
  }).join("");
  table.innerHTML = `<thead><tr>${head}</tr></thead><tbody>` + rows.map((r) => {
    const p = r.performance || {}, position = tradePosition;
    const flags = r.flags?.length ? r.flags.map(esc).join("；") : "未发现本页列出的限制（不保证成交）";
    return `<tr><td><a href="#/stock/${encodeURIComponent(r.code)}" data-trade-stock="${esc(r.code)}">${esc(r.name)}<span class="sub2">${esc(r.code)}</span></a><span class="sub2">数据 ${esc(r.last_date)}</span></td>
      <td>${tradeNumber(r.close)}<span class="sub2">${esc(r.price_note || "数据不足")}</span></td>
      <td class="${exception ? "" : cls(r.d1)}">${exception ? "—" : (isNum(r.d1) ? pct(r.d1, 2) : "数据不足")}</td>
      <td>${tradeNumber(r.avg_turnover_20d, "", 0)}</td><td>${esc(r.liquidity || "数据不足")}</td><td title="参考，不含手续费；次日开盘价可能不同；A 股 T+1，今天买入当天不能卖出">${tradeNumber(r.lot_cost)}</td><td>${r.action ? pill(r.action) : "无有效动作"}</td>
      <td>${position(r.now)}</td><td class="trade-target"><strong>${tradeTargetText(r)}</strong></td>
      <td class="trade-reason"><details><summary>查看指标和触发条件</summary>${esc(r.reason)}</details></td>
      <td>${tradeNumber(p.total, "%")}<span class="sub2">${p.start ? `${esc(p.start)} → ${esc(p.end)}` : "数据不足"}</span></td>
      <td>${tradeNumber(p.b_total, "%")}</td><td>${typeof p.beat_bh === "boolean" ? (p.beat_bh ? "是" : "否") : "数据不足"}</td>
      <td>${tradeNumber(p.maxdd, "%")}</td><td>${tradeNumber(p.trades, "", 0)}${p.small_sample ? '<span class="sub2 trade-warning">样本太少，不可靠</span>' : ""}</td>
      <td>${tradeNumber(p.sharpe)}</td><td class="trade-flags">${flags}</td></tr>`;
  }).join("") + (rows.length ? "" : `<tr><td colspan="${TRADE_COLUMNS.length}" class="empty">无匹配记录</td></tr>`) + "</tbody>";
  table.querySelectorAll("[data-trade-stock]").forEach((a) => a.addEventListener("click", () => {
    store.set("dStrategy", $("tradeStrategy").value);
  }));
  table.querySelectorAll("[data-sort]").forEach((th) => {
    const act = () => { TRADE.dir = TRADE.sort === th.dataset.sort ? -TRADE.dir : 1; TRADE.sort = th.dataset.sort; TRADE.manualSort = true; renderTrade(); };
    th.addEventListener("click", act);
    th.addEventListener("keydown", (e) => { if (["Enter", " "].includes(e.key)) { e.preventDefault(); act(); } });
  });
}
function renderTrade() {
  const r = TRADE.data;
  $("tradeTabs").querySelectorAll("button").forEach((button) => {
    const selected = button.dataset.tab === TRADE.tab;
    button.classList.toggle("on", selected); button.setAttribute("aria-selected", String(selected));
  });
  if (!r) return;
  const query = $("tradeSearch").value.trim().toLowerCase();
  const matches = (row) => !query || `${row.code} ${row.name}`.toLowerCase().includes(query);
  const allowed = TRADE.tab === "buy" ? ["买入"] : TRADE.tab === "sell" ? ["卖出"] : personalMode() && TRADE.tab === "reduce" ? ["减仓"] : personalMode() && TRADE.tab === "add" ? ["加仓"] : ["加仓", "减仓"];
  // 覆盖率按整个当前标签计算，搜索或过滤不会让缺失数据显得更完整。
  const tabRows = (r.rows || []).filter(row => allowed.includes(row.action));
  const known = tabRows.filter(row => isNum(row.avg_turnover_20d) && ["正常", "流动性差"].includes(row.liquidity)).length;
  const incomplete = tabRows.length > 0 && known * 5 < tabRows.length * 4;
  const warning = incomplete ? `成交额数据补充中，当前 ${(known / tabRows.length * 100).toFixed(1)}% 已知；流动性过滤暂不可靠` : "";
  for (const id of ["tradeLiquidityWarning", "tradeCoverageWarning"]) {
    $(id).textContent = warning; $(id).hidden = !incomplete;
  }
  if (!TRADE.manualSort) {
    TRADE.sort = incomplete || !tabRows.length ? "code" : "avg_turnover_20d";
    TRADE.dir = TRADE.sort === "code" ? 1 : -1;
  }
  const compare = (a, b) => {
    if (personalMode() && TRADE.tab === "adjust" && a.action !== b.action) return a.action === "减仓" ? -1 : 1;
    const x = tradeValue(a, TRADE.sort), y = tradeValue(b, TRADE.sort);
    if (x === null || y === null) return x === y ? a.code.localeCompare(b.code) : x === null ? 1 : -1;
    const delta = typeof x === "string" ? x.localeCompare(String(y), "zh-CN") : Number(x) - Number(y);
    return delta * TRADE.dir || a.code.localeCompare(b.code);
  };
  const liquidity = $("tradeLiquidity").value;
  const filtered = tabRows.filter((row) => matches(row) && (!liquidity || (row.liquidity || "流动性未知") === liquidity));
  TRADE.exportRows = [...filtered].sort(compare);
  const fold = row => row.liquidity === "流动性差" || (row.liquidity !== "正常" && !incomplete);
  const normal = filtered.filter(row => !fold(row));
  const illiquid = hLinkOrder(filtered.filter(fold).sort(compare));
  const small = TRADE.tab === "buy" ? normal.filter((row) => row.small_target === true).sort(compare) : [];
  const rows = hLinkOrder(normal.filter((row) => TRADE.tab !== "buy" || row.small_target !== true).sort(compare));
  $("tradeIlliquid").hidden = !illiquid.length;
  $("tradeIlliquidTitle").textContent = `流动性差${incomplete ? "" : "/流动性未知"} · ${illiquid.length} 只（可展开查看）`;
  tradeRows(illiquid, $("tradeIlliquidTable"));
  $("tradeSmall").hidden = TRADE.tab !== "buy" || !small.length;
  $("tradeSmallTitle").textContent = `小仓位买入（目标仓位低于 ${fmt((r.min_target ?? 0.05) * 100, 2).replace(/\.?0+$/, "")}%）· ${small.length} 只`;
  tradeRows(small, $("tradeSmallTable"));
  const exceptions = (r.exceptions || []).filter(matches).sort((a, b) => a.code.localeCompare(b.code));
  tradeRows(rows, $("tradeTable")); tradeRows(exceptions, $("tradeExceptions"), false, {exception: true});
  $("tradeExceptionCount").textContent = `共 ${(r.exceptions || []).length} 只，搜索匹配 ${exceptions.length} 只`;
  $("tradeMessage").textContent = ["adjust", "reduce", "add"].includes(TRADE.tab) && r.binary
    ? "该策略仅有空仓或满仓，没有加减仓信号"
    : `当前标签 ${rows.length} 只；异常/停牌记录单列，不进入正常信号列表。`;
  hLinkBanner();
  tradeSyncSortControls();
}
async function loadTradeFollowups(request) {
  if (personalMode()) return pTradeFollowups(request);
  const strategy = $("tradeStrategy").value;
  const params = new URLSearchParams({strategy, date: $("tradeDate").value});
  const local = typeof location !== "undefined" && ["127.0.0.1", "localhost", "[::1]", "::1"].includes(location.hostname) && $("tradeMine");
  const tasks = [api(`/api/trade_list_tracking?strategy=${encodeURIComponent(strategy)}`)];
  if (local) tasks.push(api(`/api/trade_list_mine?${params}`));
  const results = await Promise.allSettled(tasks);
  if (request !== TRADE.request) return;
  const stats = results[0].status === "fulfilled" ? results[0].value : null;
  const body = $("tradeTrackingBody");
  if (stats && !stats.error) {
    body.innerHTML = `<p class="dim">${esc(stats.note || "")} 跟踪统计随后续行情更新，不属于历史快照。</p>` + (stats.empty ? '<p class="dim">暂无上线后当时记录的信号，不自动回放旧清单。</p><div class="tiles">' + [5,20,60].map(horizon => `<div class="tile"><div class="k">${horizon} 个交易日</div><div class="v">数据不足</div><div class="b">已完成 0 · 跑赢大盘：数据不足</div></div>`).join('') + '</div>' :
      `<div class="dim">行情截至 ${esc(stats.latest_price_date || "数据不足")}；所选策略已记录 ${stats.recorded_signals} 条。</div><div class="tbl"><table><thead><tr><th>信号</th><th>5日平均收益</th><th>跑赢大盘/可比样本</th><th>已完成数</th><th>20日平均收益</th><th>跑赢大盘/可比样本</th><th>已完成数</th><th>60日平均收益</th><th>跑赢大盘/可比样本</th><th>已完成数</th></tr></thead><tbody>` +
      stats.groups.map(group => `<tr><td>${esc(group.action)}${group.action === "卖出" ? '<span class="sub2">卖出后股票收益，非卖出策略收益</span>' : ""}</td>` + [5,20,60].map(horizon => `<td>${tradeNumber(group[`avg${horizon}`], "%")}</td><td>${tradeNumber(group[`beat${horizon}`], "%")} / ${group[`bench_n${horizon}`]}</td><td>${group[`n${horizon}`]}</td>`).join("") + '</tr>').join("") + '</tbody></table></div>');
  } else body.textContent = "信号跟踪暂不可用。";
  if (local && results[1]) {
    const mine = results[1].status === "fulfilled" ? results[1].value : null;
    $("tradeMineBody").innerHTML = mine && !mine.error ? `<div class="dim">600531 固定第一，其余卖出信号置顶。${mine.member_logged_in ? "已包含当前会员的模拟持仓。" : "登录模拟交易会员后可查看当前账户的相关股票。"}</div><div class="tbl"><table><thead><tr><th>股票</th><th>相关来源</th><th>信号</th><th>明日目标</th></tr></thead><tbody>` + (mine.rows || []).map(row => `<tr class="${row.action === "卖出" ? "trade-mine-sell" : ""}"><td><a href="#/stock/${encodeURIComponent(row.code)}">${esc(row.name)} ${esc(row.code)}</a></td><td>${row.in_watchlist ? "自选 " : ""}${row.in_member_positions ? "模拟持仓" : ""}</td><td>${row.action ? pill(row.action) : "清单无信号"}</td><td>${tradeTargetText(row)}</td></tr>`).join("") + '</tbody></table></div>' : "我的相关暂不可用。";
  }
}

function tradeCSVData() {
  const header = [["code", "代码"], ["name", "名称"], ["last_date", "数据日期"], ["action", "信号"], ["now", "今日模拟仓位"], ["next", "明日目标仓位"], ["close", "前复权收盘价"], ["d1", "当天涨跌幅%"], [row => row.avg_turnover_20d ?? "数据不足", "20日平均成交额（元）"], [row => row.liquidity ?? "数据不足", "流动性"], [row => row.lot_cost ?? "数据不足", "一手参考金额（元）"], ["reason", "触发原因"],
    [row => row.performance?.total ?? "数据不足", "样本外策略累计收益%"], [row => row.performance?.b_total ?? "数据不足", "样本外持有累计收益%"], [row => row.performance?.maxdd ?? "数据不足", "样本外最大回撤%"], [row => row.performance?.trades ?? "数据不足", "样本外完整交易次数"], [row => row.performance?.sharpe ?? "数据不足", "样本外超额Sharpe"], [row => (row.flags || []).join("；"), "可交易性标记"]];
  return {header, rows: TRADE.exportRows || []};
}
function exportTrade() {
  if (!TRADE.data) return;
  const data = tradeCSVData();
  downloadCSV(`今日买卖_${TRADE.data.strategy}_${TRADE.data.data_date}_${TRADE.tab}.csv`, data.header, data.rows);
}

function tradeHeaderHeight() {
  $("p-trade").style.setProperty("--trade-header-height", `${document.querySelector("header").offsetHeight}px`);
}
async function loadTrade() {
  const entryView = PERSONAL.view;
  tradeHeaderHeight();
  if (!TRADE.inited) {
    if (personalMode()) {
      $("tradeStrategy").replaceChildren(...Object.entries(S.meta.strategies).filter(([key, spec]) => spec.kind === "single" && key !== "buy_hold").map(([key, spec]) => {
        const option = pNode("option", spec.label); option.value = key; return option;
      }));
    } else $("tradeStrategy").innerHTML = stratOptions((key, spec) => spec.kind === "single" && key !== "buy_hold");
    $("tradeStrategy").value = "blend2";
    try {   // 主策略 = 信号核对里准确率最高的策略（样本不够时沿用 blend2）
      const mp = await (personalMode() ? pRequest("/api/main_strategy") : api("/api/main_strategy"));
      if (mp && mp.main && [...$("tradeStrategy").options].some((o) => o.value === mp.main)) $("tradeStrategy").value = mp.main;
      TRADE.mainPick = mp || {};
    } catch (e) { TRADE.mainPick = {}; }
    if (personalMode() && entryView !== PERSONAL.view) return;
    for (const id of ["tradeStrategy", "tradeDate"]) $(id).addEventListener("change", () => {
      TRADE.manualSort = false; if (personalMode()) pInvalidate(); loadTrade();
    });
    $("tradeSearch").addEventListener("input", renderTrade);
    $("tradeLiquidity").addEventListener("change", renderTrade);
    $("tradeExport").addEventListener("click", exportTrade);
    $("tradeExportXlsx").addEventListener("click", () => {
      const params = new URLSearchParams({ strategy: $("tradeStrategy").value, date: $("tradeDate").value });
      window.location.assign(`/api/trade_list_export?${params}`);
    });
    $("tradeTabs").querySelectorAll("button").forEach((button) => button.addEventListener("click", () => {
      TRADE.tab = button.dataset.tab; TRADE.manualSort = false; renderTrade();
    }));
    window.addEventListener("resize", debounce(tradeHeaderHeight, 150));
    tradeInitMobile();
    TRADE.inited = true;
  }
  if (personalMode() && S.personalSelection) {
    const {strategy, date} = S.personalSelection;
    if (strategy && [...$("tradeStrategy").options].some(o => o.value === strategy)) $("tradeStrategy").value = strategy;
    TRADE.routeDate = date; S.personalSelection = null;
  }
  if (personalMode()) $("personalStrategyState").replaceChildren(TRADE.mainPick?.main
    ? pCopy("来源：已发布主策略清单；所选策略可手动切换", "Source: published main-strategy list; strategy selection is manual")
    : pCopy("主策略不可用；当前手动选择不代表主策略", "Main strategy unavailable; manual selection does not identify the main strategy"));
  const request = ++TRADE.request;
  hLinkClear();
  TRADE.data = null; TRADE.exportRows = [];
  for (const id of ["tradeTrackingBody", "tradeMineBody"]) {
    if (!$(id)) continue;
    if (personalMode()) $(id).replaceChildren(pState("读取中…", "Loading…"));
    else $(id).textContent = "读取中…";
  }
  $("tradeSmall").hidden = $("tradeIlliquid").hidden = true;
  $("tradeLiquidityWarning").hidden = $("tradeCoverageWarning").hidden = true;
  for (const id of ["tradeIlliquidTable", "tradeTable", "tradeSmallTable", "tradeExceptions", "tradeCounts", "tradeExceptionCount", "tradeMeta", "tradePerformanceNote"]) $(id).textContent = "";
  if (S.market !== "cn") { $("tradeMessage").textContent = "美股今日买卖暂未支持，请切换到 A 股。"; return; }
  $("tradeMessage").textContent = "正在读取已发布清单…";
  try {
    const archive = await (personalMode() ? pRequest("/api/trade_list_dates") : api("/api/trade_list_dates"));
    if (request !== TRADE.request) return;
    const selected = personalMode() && TRADE.routeDate !== undefined ? TRADE.routeDate : $("tradeDate").value;
    TRADE.routeDate = undefined;
    if (personalMode()) $("tradeDate").replaceChildren(...["", ...(archive.dates || [])].map(day => {
      const option = day ? pNode("option", day) : pNode("option", "最新清单", "Latest list"); option.value = day; return option;
    }));
    else $("tradeDate").innerHTML = '<option value="">最新清单</option>' + (archive.dates || []).map(day => `<option value="${esc(day)}">${esc(day)}</option>`).join("");
    $("tradeDate").value = (archive.dates || []).includes(selected) ? selected : "";
    const query = new URLSearchParams({ strategy: $("tradeStrategy").value, date: $("tradeDate").value });
    const r = await (personalMode() ? pList($("tradeStrategy").value, $("tradeDate").value) : api(`/api/trade_list?${query}`));
    if (request !== TRADE.request) return;
    if (r.error || r.empty) { $("tradeMessage").textContent = r.error || r.message; return; }
    TRADE.data = r;
    const generated = new Date(r.generated_at);
    const generatedText = Number.isNaN(generated.getTime()) ? "数据不足" : new Intl.DateTimeFormat("sv-SE", {timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false}).format(generated);
    $("tradeMeta").textContent = `数据日期 ${r.data_date} · 生成 ${generatedText} 北京时间 · ${r.label}`;
    const minTarget = fmt((r.min_target ?? 0.05) * 100, 2).replace(/\.?0+$/, "");
    if (personalMode()) $("tradeCounts").replaceChildren(...[["卖出", "Sell"], ["减仓", "Reduce"], ["买入", "Buy"], ["加仓", "Add"]].map(([key, en]) => pNode("span",
      `${key} ${r.counts?.[key] || 0} 只${key === "买入" ? `（目标仓位 ≥${minTarget}% 的 ${r.counts_main?.[key] ?? "数据不足"} 只，另有 ${r.counts_small?.[key] ?? "数据不足"} 只为小仓位）` : ""}`,
      `${en} ${r.counts?.[key] || 0}${key === "买入" ? ` (target ≥${minTarget}%: ${r.counts_main?.[key] ?? "N/A"}; small targets: ${r.counts_small?.[key] ?? "N/A"})` : ""}`)));
    else $("tradeCounts").innerHTML = ["买入", "卖出", "加仓", "减仓"].map((key) => `<span>${key} <b>${r.counts[key] || 0}</b> 只${key === "买入" ? `（目标仓位 ≥${minTarget}% 的 ${r.counts_main?.[key] ?? "数据不足"} 只，另有 ${r.counts_small?.[key] ?? "数据不足"} 只为小仓位）` : ""}</span>`).join("");
    $("tradePerformanceNote").textContent = r.performance_note;
    renderTrade();
    const link = hLinkRefresh(request);   // 个人模式：读台账后联动并重画；失败只提示，不影响清单
    await loadTradeFollowups(request);
    await link;
  } catch (e) { if (request === TRADE.request) $("tradeMessage").textContent = `清单读取失败：${e.message}`; }
}

// ================================================================ 信号页 × 持仓联动（T013 · S5，仅个人模式，只读）
// 隐私约束：持仓只经 hRequest 取 /api/holdings，只放在 HLINK 内存里；只用于“排序（置顶）+ 行内标注”，
// 不写进 TRADE.data / TRADE.exportRows（所以 CSV 导出不含持仓；XLSX 由服务端生成，本来就不含）；
// 不进请求缓存、不写浏览器存储、不挂全局；离开信号页或重新加载清单时清空。
const HLINK = { state: "idle", map: null };   // state: idle 未读取 / ok 有持仓 / empty 台账为空 / unavailable 台账不可用
const HLINK_SELL = ["卖出", "减仓"], HLINK_BUY = ["买入", "加仓"];
function hLinkClear() { HLINK.state = "idle"; HLINK.map = null; }
// 台账不可用与台账为空要区分：不可用时绝不能把卖出信号误标成“未持有”。
async function hLinkRefresh(request) {
  if (!personalMode()) return;
  const r = await hRequest("/api/holdings?trades_limit=1&include_voided=0");
  if (request !== TRADE.request || !personalMode()) return;
  if (!r.ok || !Array.isArray(r.data?.positions)) { HLINK.state = "unavailable"; HLINK.map = null; }
  else {
    // 只留联动要显示的字段，不保留成交明细、总资金、占比
    HLINK.map = new Map(r.data.positions.filter(p => p && p.code).map(p => [p.code, { shares: p.shares, avg_cost: p.avg_cost, held_days: p.held_days, floating_pnl: p.floating_pnl, floating_pct: p.floating_pct }]));
    HLINK.state = HLINK.map.size ? "ok" : "empty";
  }
  renderTrade();
}
// 持有的卖出/减仓信号置顶（稳定分区，保持原有排序）；只改显示顺序，不改数据。
function hLinkOrder(rows) {
  if (!personalMode() || HLINK.state !== "ok") return rows;
  const held = r => HLINK_SELL.includes(r.action) && HLINK.map.has(r.code);
  return [...rows.filter(held), ...rows.filter(r => !held(r))];
}
function hLinkTag(zh, en) { const n = pNode("small", zh, en); n.classList.add("hold-link-note"); return n; }
function hLinkNote(r) {
  if (HLINK.state !== "ok") return null;
  const pos = HLINK.map.get(r.code);
  if (HLINK_SELL.includes(r.action)) {
    if (!pos) return { cls: "hold-link-dim", node: hLinkTag("未持有", "Not held") };
    const pnl = pos.floating_pnl === null || pos.floating_pnl === undefined ? "—" : `${hNum(pos.floating_pnl)}（${hRatio(pos.floating_pct, 2, true)}）`;
    const days = pos.held_days ?? "—";
    return { cls: "hold-link-held", node: hLinkTag(
      `你持有 ${hNum(pos.shares, 0)} 股，成本 ${hNum(pos.avg_cost, 3)}，持有 ${days} 天，浮动盈亏 ${pnl}`,
      `You hold ${hNum(pos.shares, 0)} shares, cost ${hNum(pos.avg_cost, 3)}, held ${days} days, floating P&L ${pnl}`) };
  }
  if (HLINK_BUY.includes(r.action) && pos) return { cls: "hold-link-owned", node: hLinkTag("已持有", "Already held") };
  return null;
}
function hLinkBanner() {
  const box = $("tradeHoldLink"); if (!box) return;
  if (!personalMode() || HLINK.state === "idle") { box.replaceChildren(); return; }
  const text = {
    ok: ["已按你的持仓台账标注：持有的卖出/减仓信号置顶；仅在本页内存中使用，不写入信号数据，不影响导出。", "Annotated from your holdings ledger: held sell/reduce signals are pinned; used in page memory only, not written to signal data, not in exports."],
    empty: ["持仓台账为空，未做联动。", "Holdings ledger is empty; no linking applied."],
    unavailable: ["持仓暂不可用，未做联动（信号照常显示，不会标成“未持有”）。", "Holdings unavailable; no linking applied (signals shown as usual, none marked \"not held\")."],
  }[HLINK.state];
  box.replaceChildren(pNode("p", text[0], text[1]),
    pNode("p", "盯盘盈亏：盯盘接口接入前暂不提供，此处仅占位。", "Monitor P&L: not available until the monitor feed is connected; placeholder only."));
}

// ================================================================ 全市场
const M ={ sort: "sharpe", dir: "desc", page: 0, inited: false, last: null };
const MCOLS = [["code", "股票"], ["d1", "今日"], ["next", "明日仓位"], ["cagr", "策略年化"], ["sharpe", "超额Sharpe"], ["maxdd", "策略回撤"],
  ["sharpe_is", "样本内"], ["sharpe_oos", "样本外"], ["y1", "近1年"], ["win", "胜率"], ["b_cagr", "持有年化"], ["b_sharpe", "持有Sharpe"], ["b_maxdd", "持有回撤"]];

function marketQuery(extra = {}) {
  return new URLSearchParams({ market: S.market, strategy: $("sStrategy").value || "blend2", q: $("sQ").value, board: $("sBoard").value, industry: $("sInd").value,
    signal: $("sSig").value, sort: M.sort, dir: M.dir, page: M.page, ...extra });
}
async function loadMarket() {
  const view = PERSONAL.view;
  if (!M.inited) {
    $("sStrategy").innerHTML = stratOptions((k, v) => v.kind === "single" && k !== "buy_hold");
    $("sStrategy").innerHTML += `<option value="raw">原本数据（不回测）</option>`;
    $("sStrategy").value = "blend2";
    for (const id of ["sStrategy", "sBoard", "sInd", "sSig"]) $(id).addEventListener("change", () => { M.page = 0; loadMarket(); });
    $("sQ").addEventListener("input", debounce(() => { M.page = 0; loadMarket(); }, 300));
    $("sPrev").addEventListener("click", () => { if (M.page > 0) { M.page--; loadMarket(); } });
    $("sNext").addEventListener("click", () => { M.page++; loadMarket(); });
    $("sExport").addEventListener("click", exportMarket);
    M.inited = true;
  }
  const r = await api(`/api/scan?${marketQuery()}`);
  if (personalMode() && view !== PERSONAL.view) return;
  if (r.empty) {
    $("mBody").hidden = true;
    $("mEmpty").innerHTML = emptyState(`还没有全市场数据（已下载 ${r.downloaded}${r.listed ? " / " + r.listed : ""} 只）。在终端运行 <b>${S.market === "us" ? ".venv/bin/python -m src.scan_us" : "caffeinate -i .venv/bin/python backfill.py"}</b> 下载历史数据，之后每天会自动更新。`);
    return;
  }
  $("mEmpty").innerHTML = ""; $("mBody").hidden = false; M.last = r;
  const m = r.meta;
  $("mMeta").textContent = `数据日期 ${m.data_date} · ${m.stocks} 只（${m.excluded ? `另有 ${Object.values(m.excluded).reduce((a, b) => a + b, 0)} 只因上市不满一年、流动性差、低价或数据异常未参与回测` : `排除 ST ${m.excluded_st} 只`}）${m.delisted ? ` · 含退市 ${m.delisted} 只做统计` : ""}`;
  const S2 = m.strategies;
  $("mSummary").innerHTML = `<thead><tr><th>策略</th><th>比持有好的股票</th><th>样本内</th><th>样本外</th><th>持续性</th><th>样本内前10% → 样本外</th>${m.delisted ? "<th>含退市</th>" : ""}<th>今日买入</th><th>今日卖出</th><th>持仓比例</th></tr></thead><tbody>` +
    Object.entries(S2).map(([k, v]) => `<tr class="click" data-k="${esc(k)}"><td>${esc(v.label)}</td>
      <td class="${v.beat_bh_pct > 50 ? "better" : ""}">${fmt(v.beat_bh_pct, 0, "%")}</td>
      <td>${fmt(v.beat_is, 0, "%")}</td><td class="${v.beat_oos > 50 ? "better" : ""}">${fmt(v.beat_oos, 0, "%")}</td>
      <td>${fmt(v.persistence, 2)}</td>
      <td>${isNum(v.top_pick_oos) ? `${fmt(v.top_pick_oos, 2)} <span class="dim">vs ${fmt(v.all_oos, 2)}</span>` : "—"}</td>
      ${m.delisted ? `<td>${fmt(v.with_delisted, 0, "%")}</td>` : ""}
      <td>${v.buy}</td><td>${v.sell}</td><td>${fmt(v.holding_pct, 0, "%")}</td></tr>`).join("") + "</tbody>";
  $("mSummary").querySelectorAll("tr.click").forEach((tr) => tr.addEventListener("click", () => { $("sStrategy").value = tr.dataset.k; M.page = 0; loadMarket(); }));
  $("mExplain").innerHTML = `<b>怎么看这张表：</b>"比持有好的股票"= 策略的超额 Sharpe 高于一直持有这只股票的比例，超过 50% 才说明策略整体有用。
    样本内 = ${m.split ? "2020 年以前" : "前半段"}，样本外 = ${m.split ? "2020 年以后" : "后半段"}。
    <b>持续性</b>是每只股票"策略比持有好多少"在样本内和样本外的秩相关：接近 0 说明样本内表现好的股票，样本外并不会更好——<b>按历史回测挑股票基本是运气</b>。
    "样本内前10% → 样本外"是把样本内最好的 10% 股票拿出来，看它们在样本外比全部股票（灰色数字）好多少。点一行可切换下面表格的策略。`;
  const fill = (id, arr) => { const cur = $(id).value; $(id).innerHTML = `<option value="">全部</option>` + arr.map((x) => `<option>${esc(x)}</option>`).join(""); $(id).value = cur; };
  if ($("sBoard").options.length <= 1) fill("sBoard", r.boards);
  if ($("sInd").options.length <= 1) fill("sInd", r.industries);
  const pages = Math.max(1, Math.ceil(r.total / r.size));
  $("sCount").textContent = `共 ${r.total} 只`; $("sPage").textContent = `${r.page + 1} / ${pages}`;
  $("sPrev").disabled = r.page <= 0; $("sNext").disabled = r.page + 1 >= pages;
  const wl = S.meta.markets[S.market].stocks || {};
  const RAW = $("sStrategy").value === "raw";
  if (RAW && !["code", "d1", "m1", "y1", "mcap"].includes(M.sort)) M.sort = r.rows.some((x) => isNum(x.mcap)) ? "mcap" : "d1";
  const HAS_CAP = r.rows.some((x) => isNum(x.mcap)), cols = HAS_CAP ? [MCOLS[0], ["mcap", "市值"], ...MCOLS.slice(1)] : MCOLS;
  const capFmt = (v) => (!isNum(v) ? "—" : v >= 1e12 ? `${(v / 1e12).toFixed(2)}T` : v >= 1e9 ? `${(v / 1e9).toFixed(1)}B` : `${(v / 1e6).toFixed(0)}M`);
  if (RAW) {
    const rc = [["code", "股票"], ...(HAS_CAP ? [["mcap", "市值"]] : []), ["d1", "今日"], ["m1", "近1月"], ["y1", "近1年"], ["last_date", "数据日期"]];
    $("scanTbl").innerHTML = "<thead><tr>" + rc.map(([k, n]) => `<th class="${k === "last_date" ? "" : "sort"} ${M.sort === k ? "on" : ""}" data-k="${k}">${n}${M.sort === k ? (M.dir === "asc" ? " ↑" : " ↓") : ""}</th>`).join("") + "<th></th></tr></thead><tbody>" +
      r.rows.map((x) => `<tr class="click ${wl[x.code] !== undefined ? "pin" : ""}" data-c="${esc(x.code)}"><td>${esc(x.name)}<span class="code">${esc(x.code)}</span><span class="tag">${esc(x.board)}</span>${x.industry ? `<span class="sub2">${esc(x.industry)}</span>` : ""}</td>${HAS_CAP ? `<td>${capFmt(x.mcap)}</td>` : ""}<td class="${cls(x.d1)}">${pct(x.d1, 2)}</td><td class="${cls(x.m1)}">${pct(x.m1, 1)}</td><td class="${cls(x.y1)}">${pct(x.y1)}</td><td>${esc(x.last_date || "")}</td><td>${wl[x.code] === undefined ? `<span class="link" data-add="${esc(x.code)}">＋自选</span>` : `<span class="link gray">已自选</span>`}</td></tr>`).join("") + "</tbody>";
  } else
  $("scanTbl").innerHTML = "<thead><tr>" + cols.map(([k, n]) => `<th class="sort ${M.sort === k ? "on" : ""}" data-k="${k}">${n}${M.sort === k ? (M.dir === "asc" ? " ↑" : " ↓") : ""}</th>`).join("") + "<th></th></tr></thead><tbody>" +
    r.rows.map((x) => `<tr class="click ${wl[x.code] !== undefined ? "pin" : ""}" data-c="${esc(x.code)}">
      <td>${esc(x.name)}<span class="code">${esc(x.code)}</span><span class="tag">${esc(x.board)}</span>${x.industry ? `<span class="sub2">${esc(x.industry)}</span>` : ""}</td>${HAS_CAP ? `<td>${capFmt(x.mcap)}</td>` : ""}
      <td class="${cls(x.d1)}">${pct(x.d1, 2)}</td>
      <td>${fmt((x.next || 0) * 100, 0, "%")} ${pill(x.action)}</td>
      <td>${fmt(x.cagr, 1, "%")}</td><td class="${x.sharpe > x.b_sharpe ? "better" : ""}">${fmt(x.sharpe)}</td><td>${fmt(x.maxdd, 1, "%")}</td>
      <td>${fmt(x.sharpe_is)}</td><td>${fmt(x.sharpe_oos)}</td>
      <td class="${cls(x.y1)}">${pct(x.y1)}</td><td>${fmt(x.win, 0, "%")}</td>
      <td>${fmt(x.b_cagr, 1, "%")}</td><td>${fmt(x.b_sharpe)}</td><td>${fmt(x.b_maxdd, 1, "%")}</td>
      <td>${wl[x.code] === undefined ? `<span class="link" data-add="${esc(x.code)}">＋自选</span>` : `<span class="link gray">已自选</span>`}</td></tr>`).join("") + "</tbody>";
  $("scanTbl").querySelectorAll("th.sort").forEach((th) => th.addEventListener("click", () => {
    const k = th.dataset.k; M.dir = M.sort === k && M.dir === "desc" ? "asc" : "desc"; M.sort = k; M.page = 0; loadMarket();
  }));
  $("scanTbl").querySelectorAll("tr.click").forEach((tr) => tr.addEventListener("click", (e) => { if (!e.target.dataset.add) openStock(tr.dataset.c); }));
  $("scanTbl").querySelectorAll("[data-add]").forEach((el) => el.addEventListener("click", async (e) => { e.stopPropagation(); await addStock(el.dataset.add); loadMarket(); }));
}
async function exportMarket() {
  $("sExport").disabled = true;
  try {
    const r = await api(`/api/scan?${marketQuery({ export: 1 })}`);
    const lab = $("sStrategy").value === "raw" ? "原本数据" : S.meta.strategies[$("sStrategy").value].label;
    downloadCSV(`全市场_${lab}_${r.meta.data_date}.csv`, [["code", "代码"], ["name", "名称"], ["board", "板块"], ["industry", "行业"], ["action", "信号"],
      [(x) => fmt((x.next || 0) * 100, 0), "明日仓位%"], ["cagr", "策略年化%"], ["sharpe", "超额Sharpe"], ["maxdd", "策略最大回撤%"], ["sharpe_is", "样本内Sharpe"],
      ["sharpe_oos", "样本外Sharpe"], ["y1", "近1年%"], ["win", "胜率%"], ["trades", "交易次数"], ["b_cagr", "持有年化%"], ["b_sharpe", "持有Sharpe"], ["b_maxdd", "持有最大回撤%"], ["last_date", "数据日期"]], r.rows);
  } finally { $("sExport").disabled = false; }
}

// ================================================================ 自选
const T = { rows: [], sort: null, dir: -1, inited: false };
async function addStock(code) {
  const r = await post(`/api/watchlist?market=${S.market}&action=add&code=${encodeURIComponent(code)}`);
  if (r.error) { alertMsg(r.error); return false; }
  S.meta = await api("/api/meta"); return true;
}
function alertMsg(t) { $("tMsg").textContent = t; }
async function loadWatch() {
  const view = PERSONAL.view;
  if (!T.inited) {
    $("tStrategy").innerHTML = stratOptions((k, v) => v.kind === "single" && k !== "buy_hold"); $("tStrategy").value = "blend2";
    $("tStrategy").addEventListener("change", loadWatch); $("tStart").addEventListener("change", loadWatch);
    const add = async () => {
      const c = $("addCode").value.trim();
      const valid = S.market === "cn" ? /^(60|68|00|30)\d{4}$/.test(c) : /^[A-Za-z][A-Za-z0-9.-]{0,9}$/.test(c);
      if (!valid) { alertMsg(S.market === "cn" ? "请输入 6 位沪深 A 股代码（60/68/00/30 开头）" : "请输入美股代码，例如 AAPL"); return; }
      if (await addStock(c)) { $("addCode").value = ""; loadWatch(); }
    };
    $("addBtn").addEventListener("click", add); $("addCode").addEventListener("keydown", (e) => { if (e.key === "Enter") add(); });
    $("tExport").addEventListener("click", () => downloadCSV(`自选_${S.meta.strategies[$("tStrategy").value].label}.csv`,
      [["symbol", "代码"], ["name", "名称"], ["kind", "类型"], ["action", "信号"], [(x) => fmt((x.next || 0) * 100, 0), "明日仓位%"], ["cagr", "策略年化%"], ["sharpe", "超额Sharpe"],
        ["maxdd", "策略最大回撤%"], ["y1", "近1年%"], ["win", "胜率%"], ["trades", "交易次数"], ["b_cagr", "持有年化%"], ["b_maxdd", "持有最大回撤%"]], T.rows));
    T.inited = true;
  }
  $("addWrap").style.display = $("addBtn").style.display = "";
  $("addCode").placeholder = S.market === "cn" ? "6 位代码" : "美股代码，如 AAPL";
  $("tMsg").textContent = "计算中…";
  const q = new URLSearchParams({ market: S.market, strategy: $("tStrategy").value, start: $("tStart").value });
  const r = await api(`/api/table?${q}`);
  if (personalMode() && view !== PERSONAL.view) return;
  if (r.error) { $("tMsg").textContent = r.error; return; }
  T.rows = r.rows;
  if (personalMode()) pWatchActions(r.rows);
  $("tMsg").textContent = r.pending.length ? `正在下载：${r.pending.join("、")}，好了会自动出现` : "";
  renderWatch();
  if (r.pending.length) setTimeout(async () => { S.meta = await api("/api/meta"); if (location.hash.startsWith("#/watch")) loadWatch(); }, 4000);
}
const TCOLS = [["name", "品种"], ["d1", "今日"], ["next", "明日仓位"], ["cagr", "策略年化"], ["sharpe", "超额Sharpe"], ["maxdd", "策略回撤"], ["y1", "近1年"], ["win", "胜率"], ["trades", "交易"], ["b_cagr", "持有年化"], ["b_maxdd", "持有回撤"]];
function renderWatch() {
  let rows = [...T.rows];
  if (T.sort) rows.sort((a, b) => ((a[T.sort] ?? -1e9) > (b[T.sort] ?? -1e9) ? 1 : -1) * T.dir);
  $("stockTbl").innerHTML = "<thead><tr>" + TCOLS.map(([k, n]) => `<th class="sort ${T.sort === k ? "on" : ""}" data-k="${k}">${n}</th>`).join("") + "<th></th></tr></thead><tbody>" +
    rows.map((r) => {
      const name = `${esc(r.name)}<span class="code">${esc(r.symbol)}</span><span class="tag">${esc(r.kind)}</span>`;
      const del = r.kind === "个股" ? `<span class="link gray" data-del="${esc(r.symbol)}">移除</span>` : "";
      if (r.missing || r.error) return `<tr><td>${name}</td><td colspan="${TCOLS.length - 1}" style="text-align:left" class="dim">${r.error ? esc(r.error) : "下载中，稍等"}</td><td>${del}</td></tr>`;
      return `<tr class="click ${r.kind === "个股" ? "pin" : ""}" data-c="${esc(r.symbol)}"><td>${name}</td>
        <td class="${cls(r.d1)}">${pct(r.d1, 2)}</td><td>${fmt(r.next * 100, 0, "%")} ${pill(r.action)}</td>
        <td class="${r.cagr > r.b_cagr ? "better" : ""}">${fmt(r.cagr, 1, "%")}</td><td class="${r.sharpe > r.b_sharpe ? "better" : ""}">${fmt(r.sharpe)}</td>
        <td>${fmt(r.maxdd, 1, "%")}</td><td class="${cls(r.y1)}">${pct(r.y1)}</td><td>${fmt(r.win, 0, "%")}</td><td>${r.trades ?? "—"}</td>
        <td>${fmt(r.b_cagr, 1, "%")}</td><td>${fmt(r.b_maxdd, 1, "%")}</td><td>${del}</td></tr>`;
    }).join("") + "</tbody>";
  $("stockTbl").querySelectorAll("th.sort").forEach((th) => th.addEventListener("click", () => { const k = th.dataset.k; T.dir = T.sort === k ? -T.dir : -1; T.sort = k; renderWatch(); }));
  $("stockTbl").querySelectorAll("tr.click").forEach((tr) => tr.addEventListener("click", (e) => { if (!e.target.dataset.del) { store.set("dStrategy", $("tStrategy").value); openStock(tr.dataset.c); } }));
  $("stockTbl").querySelectorAll("[data-del]").forEach((el) => el.addEventListener("click", async (e) => {
    e.stopPropagation(); await post(`/api/watchlist?market=${S.market}&action=remove&code=${el.dataset.del}`); S.meta = await api("/api/meta"); loadWatch();
  }));
}

// ================================================================ 个股详情
const D = { inited: false, data: null };
const PERIODS = [["6m", "6个月"], ["1y", "1年"], ["3y", "3年"], ["5y", "5年"], ["all", "全部"]];
async function loadStock() {
  const code = S.routeArg;
  if (!code) { go("#/market"); return; }
  if (!D.inited) {
    $("dStrategy").innerHTML = stratOptions((k, v) => v.kind === "single");
    $("dStrategy").addEventListener("change", () => { store.set("dStrategy", $("dStrategy").value); loadStock(); });
    $("dPeriod").innerHTML = PERIODS.map(([k, n]) => `<button data-p="${k}">${n}</button>`).join("");
    $("dPeriod").querySelectorAll("button").forEach((b) => b.addEventListener("click", () => { store.set("dPeriod", b.dataset.p); loadStock(); }));
    $("dInd").value = store.get("dInd", "all");
    $("dInd").addEventListener("change", () => { store.set("dInd", $("dInd").value); if (D.data) kChart($("kChart"), D.data); });
    $("dLab").addEventListener("click", () => { S.labPreset = { symbol: S.routeArg, strategy: $("dStrategy").value, name: D.data?.info?.name }; go("#/lab"); });
    $("dWatch").addEventListener("click", async () => {
      const inWl = D.data?.info?.watch;
      if (inWl) await post(`/api/watchlist?market=${S.market}&action=remove&code=${S.routeArg}`); else await addStock(S.routeArg);
      S.meta = await api("/api/meta"); loadStock();
    });
    D.inited = true;
  }
  const strat = store.get("dStrategy", "blend2"), period = store.get("dPeriod", "1y");
  $("dStrategy").value = S.meta.strategies[strat] ? strat : "blend2";
  $("dPeriod").querySelectorAll("button").forEach((b) => b.classList.toggle("on", b.dataset.p === period));
  $("kChart").innerHTML = '<div class="skeleton" style="height:420px"></div>';
  const d = await api(`/api/stock?market=${S.market}&code=${encodeURIComponent(code)}&strategy=${$("dStrategy").value}&period=${period}`);
  if (d.error) { $("sdHead").innerHTML = ""; $("kChart").innerHTML = emptyState(esc(d.error)); return; }
  D.data = d; const i = d.info;
  $("sdHead").innerHTML = `<div class="shead"><div><div class="dim">${esc(i.kind)}${i.board ? " · " + esc(i.board) : ""}${i.industry ? " · " + esc(i.industry) : ""}</div>
      <h1>${esc(i.name)}<span class="code">${esc(i.code)}</span></h1></div>
    <div class="stats"><div><span>今日</span><b class="${cls(i.d1)}">${pct(i.d1, 2)}</b></div><div><span>近1月</span><b class="${cls(i.m1)}">${pct(i.m1)}</b></div>
      <div><span>近1年</span><b class="${cls(i.y1)}">${pct(i.y1)}</b></div><div><span>明日（${esc(S.meta.strategies[d.strategy].label)}）</span><b>${fmt(d.next * 100, 0, "%")}</b> ${pill(d.action)}</div></div></div>`;
  $("dWatch").textContent = i.kind !== "个股" ? "" : i.watch ? "移出自选" : "＋ 加入自选";
  $("dWatch").style.display = i.kind === "个股" ? "" : "none";
  const pal = [css("--orange"), css("--indigo"), css("--teal"), css("--pink")];
  $("dLegend").innerHTML = d.lines.map((L, k) => `<span><i style="background:${L.dash ? css("--red") : pal[k % pal.length]}"></i>${esc(L.name)}</span>`).join("") +
    `<span><i style="background:${css("--blue")};height:8px;width:8px;border-radius:50%"></i>买入（次日开盘）</span><span><i style="background:${css("--orange")};height:8px;width:8px;border-radius:50%"></i>卖出</span><span><i style="background:${css("--blue")};opacity:.5"></i>持仓（越深仓位越重）</span>`;
  kChart($("kChart"), d);
  $("dNote").textContent = `价格为前复权（以最新收盘为基准，历史价按除权调整，最新价与行情软件一致）。数据 ${i.first_date} → ${i.last_date}，共 ${i.rows} 个交易日（${S.meta.markets[S.market].timezone_label}）；单边成本 ${fmt(d.cost_bp, 0)}bp。`;
  $("dCompare").innerHTML = `<thead><tr><th>策略</th><th>年化</th><th>超额Sharpe</th><th>最大回撤</th><th>胜率</th><th>交易</th><th>明日</th></tr></thead><tbody>` +
    d.compare.map((c) => c.error ? `<tr><td>${esc(c.label)}</td><td colspan="6" class="dim">${esc(c.error)}</td></tr>` :
      `<tr class="click ${c.key === d.strategy ? "pin" : ""}" data-k="${esc(c.key)}"><td>${esc(c.label)}</td><td>${fmt(c.cagr, 1, "%")}</td>
       <td>${fmt(c.sharpe)}</td><td>${fmt(c.maxdd, 1, "%")}</td><td>${fmt(c.win, 0, "%")}</td><td>${c.trades ?? "—"}</td><td>${pill(c.action)}</td></tr>`).join("") + "</tbody>";
  $("dCompare").querySelectorAll("tr.click").forEach((tr) => tr.addEventListener("click", () => { store.set("dStrategy", tr.dataset.k); loadStock(); }));
  lineChart($("dEquity"), { dates: d.dates, height: 200, yfmt: (v) => v.toFixed(2),
    series: [{ name: "一直持有", values: d.bench, color: css("--gray"), width: 1.5 }, { name: S.meta.strategies[d.strategy].label, values: d.equity, color: css("--blue") }] });
  $("dTrades").innerHTML = d.trades.length ? `<thead><tr><th>买入</th><th>卖出</th><th>天数</th><th>价格涨跌</th><th>账户收益</th></tr></thead><tbody>` +
    d.trades.map((t) => `<tr><td>${esc(t.entry)}</td><td>${t.open ? '<span class="pill buy">持仓中</span>' : esc(t.exit)}</td><td>${t.days}</td>
      <td class="${cls(t.px_ret)}">${pct(t.px_ret, 2)}</td><td class="${cls(t.ret)}">${pct(t.ret, 2)}</td></tr>`).join("") + "</tbody>"
    : `<tbody><tr><td class="dim">这个策略没有交易记录</td></tr></tbody>`;
}

// ================================================================ 回测实验室
let labInited = false;
function renderParams() {
  const st = S.meta.strategies[$("strategy").value];
  $("sdesc").textContent = st.desc + (st.kind === "multi" ? "（多品种策略：「品种」只影响对比基准）" : "");
  const dec = (s) => (String(s).split(".")[1] || "").length;
  $("params").innerHTML = st.params.map((p) => `<label class="field" title="${esc(p.help)}">${esc(p.label)}${p.help ? " ⓘ" : ""}
    <input type="number" data-key="${esc(p.key)}" value="${Number(p.default).toFixed(dec(p.step))}" min="${p.min}" max="${p.max}" step="${p.step}" style="width:130px;min-width:0"></label>`).join("") || `<span class="dim">这个策略没有可调参数</span>`;
}
function syncCost() { const mk = S.meta.markets[S.market]; $("cost").value = mk.universe[$("symbol").value] === undefined ? mk.stock_cost_bp : mk.cost_bp; }
function labSymbols(extra) {
  const mk = S.meta.markets[S.market];
  const st = Object.entries(mk.stocks || {}).map(([k, v]) => `<option value="${esc(k)}">${esc(v || k)}（${esc(k)}）</option>`).join("");
  const ls = Object.entries(mk.lab_stocks || {}).map(([k, v]) => `<option value="${esc(k)}">${esc(v || k)}（${esc(k)}）</option>`).join("");
  const et = Object.entries(mk.universe).map(([k, v]) => `<option value="${esc(k)}">${esc(v)}（${esc(k)}）</option>`).join("");
  const ex = extra && !(mk.stocks || {})[extra.symbol] && !mk.universe[extra.symbol] ? `<optgroup label="其他"><option value="${esc(extra.symbol)}">${esc(extra.name || extra.symbol)}（${esc(extra.symbol)}）</option></optgroup>` : "";
  $("symbol").innerHTML = (st ? `<optgroup label="自选股">${st}</optgroup>` : "") + `<optgroup label="ETF">${et}</optgroup>` + (ls ? `<optgroup label="排名靠前的个股（Blend 夏普）">${ls}</optgroup>` : "") + ex;
}
async function loadLab() {
  if (!labInited) {
    $("strategy").innerHTML = stratOptions();
    $("strategy").addEventListener("change", () => { renderParams(); runBacktest(); });
    $("symbol").addEventListener("change", () => { syncCost(); runBacktest(); });
    $("runBtn").addEventListener("click", runBacktest);
    $("resetBtn").addEventListener("click", () => { renderParams(); runBacktest(); });
    $("params").addEventListener("keydown", (e) => { if (e.key === "Enter") runBacktest(); });
    labInited = true; S.labRan = false;
  }
  const pre = S.labPreset; S.labPreset = null;
  labSymbols(pre);
  if (pre) { $("strategy").value = pre.strategy; $("symbol").value = pre.symbol; renderParams(); syncCost(); S.labRan = false; }
  else if (!S.labRan) { $("strategy").value = "blend2"; $("symbol").value = S.meta.markets[S.market].main; renderParams(); syncCost(); }
  if (!S.labRan) runBacktest();
}
async function runBacktest() {
  const view = PERSONAL.view;
  const params = {}; document.querySelectorAll("#params input").forEach((i) => { params[i.dataset.key] = Number(i.value); });
  const q = new URLSearchParams({ market: S.market, strategy: $("strategy").value, symbol: $("symbol").value, cost: $("cost").value, start: $("start").value, params: JSON.stringify(params) });
  $("runBtn").disabled = true; $("btErr").textContent = "";
  try {
    const r = await api(`/api/backtest?${q}`);
    if (personalMode() && view !== PERSONAL.view) return;
    if (r.error) { $("btErr").textContent = r.error; $("btOut").hidden = true; return; }
    S.labRan = true; renderResult(r);
  } catch (e) { $("btErr").textContent = e.message; } finally { $("runBtn").disabled = false; }
}
function renderReport(g) {
  const el = $("rptOut"); if (!el) return;
  if (!g || !g.length) { el.innerHTML = ""; return; }
  const F = (it) => { const v = it.v; if (v == null || !isFinite(v)) return "—";
    return it.f === "pct" ? v.toFixed(2) + "%" : it.f === "int" ? String(Math.round(v)) : it.f === "days" ? Math.round(v) + " 天" : it.f === "x" ? v.toFixed(2) + "x" : (Math.abs(v) < 0.1 && v !== 0 ? v.toFixed(4) : v.toFixed(2)); };
  el.innerHTML = `<div class="card rpt"><h3>详细指标</h3>` + g.map((x) => `<div class="rg"><div class="rgh">${esc(x.group)}</div><div class="rgrid">` +
    x.items.map((it) => `<div class="ri" title="${esc(it.tip || "")}"><span>${esc(it.k)}</span><b>${F(it)}</b></div>`).join("") + `</div></div>`).join("") + `</div>`;
}
function renderResult(r) {
  $("btOut").hidden = false;
  const s = r.stats, b = r.bench_stats;
  $("tiles").innerHTML = [["年化收益", fmt(s["CAGR%"], 1, "%"), fmt(b["CAGR%"], 1, "%")], ["超额 Sharpe", fmt(s.ExcessSharpe), fmt(b.ExcessSharpe)],
    ["最大回撤", fmt(s["MaxDD%"], 1, "%"), fmt(b["MaxDD%"], 1, "%")], ["年化波动", fmt(s["Vol%"], 1, "%"), fmt(b["Vol%"], 1, "%")],
    ["Calmar", fmt(s.Calmar), fmt(b.Calmar)], ["持仓时间", fmt(s["Exposure%"], 0, "%"), "100%"], ["交易次数", s.Trades ?? "—", "—"], ["胜率", fmt(s["Win%"], 0, "%"), "—"]]
    .map(([k, v, bb]) => `<div class="tile"><div class="k">${k}</div><div class="v">${v}</div><div class="b">持有 ${bb}</div></div>`).join("");
  renderReport(r.report);
  const label = S.meta.strategies[r.strategy].label; $("lgS").textContent = label;
  lineChart($("eqChart"), { dates: r.dates, log: true, height: 280, series: [{ name: "买入持有", values: r.bench, color: css("--gray"), width: 1.5 }, { name: label, values: r.equity, color: css("--blue") }] });
  lineChart($("ddChart"), { dates: r.dates, height: 120, zero: true, yfmt: (v) => `${v.toFixed(0)}%`, series: [{ name: "回撤", values: r.drawdown, color: css("--red"), width: 1.4, fill: true }] });
  const names = { ...S.meta.markets[S.market].universe, ...(S.meta.markets[S.market].stocks || {}) };
  const ps = (p) => Object.keys(p).length ? Object.entries(p).map(([k, v]) => `${names[k] || k} ${(v * 100).toFixed(0)}%`).join("、") : "空仓";
  $("posText").innerHTML = `区间 ${esc(r.start)} → ${esc(r.end)}，单边成本 ${fmt(r.cost_bp, 0)}bp。当前持仓 <b>${esc(ps(r.position_now))}</b>，明日开盘目标 <b>${esc(ps(r.position_next))}</b>。`;
  $("recentTbl").innerHTML = `<tbody>${[["今日", "1D"], ["近1周", "1W"], ["近1月", "1M"], ["近3月", "3M"], ["近1年", "1Y"], ["今年", "YTD"]].map(([n, k]) => `<tr><td>${n}</td><td class="${cls(r.recent[k])}">${pct(r.recent[k], 2)}</td></tr>`).join("")}</tbody>`;
  if (typeof renderAnalysis === "function") renderAnalysis($("anaOut"), r);
  $("tradeTbl").innerHTML = r.trades.length ? `<thead><tr><th>买入</th><th>卖出</th><th>天数</th><th>收益</th></tr></thead><tbody>` + r.trades.map((t) =>
    `<tr><td>${esc(t.entry)}</td><td>${t.open ? '<span class="pill buy">持仓中</span>' : esc(t.exit)}</td><td>${t.days}</td><td class="${cls(t.ret)}">${pct(t.ret, 2)}</td></tr>`).join("") + "</tbody>"
    : `<tbody><tr><td class="dim">没有逐笔交易（一直持仓或多品种轮动）</td></tr></tbody>`;
}

// ================================================================ 信号跟踪
async function loadTrack() {
  const r = await api(`/api/tracking?market=${S.market}`);
  const live = r.eval, rep = r.replay;
  const liveN = live && !live.empty ? live.summary.reduce((t, x) => t + (x.n20 || 0), 0) : 0;
  const useRep = liveN < 30 && rep && !rep.empty;
  const ev = useRep ? rep : live;
  if (!ev || ev.empty) { $("trBody").hidden = true; $("trEmpty").innerHTML = emptyState("还没有信号记录。每天收盘更新后会自动记录，过几天就能看到统计。"); return; }
  $("trEmpty").innerHTML = useRep ? `<div class="card" style="padding:12px 16px;margin-bottom:12px;background:rgba(0,122,255,.08)"><b>当前显示：历史回放</b>。用现在的规则把过去 3 年每天会发出的信号重算了一遍，再按同样的规则计算 5、20、60 天后的收益。它不是当时记录下来的信号，规则是事后定的，结果会偏乐观，只能当参考。实盘记录已有 ${live && !live.empty ? live.total : 0} 条，等累计到 30 个以上的 20 天样本，这里会自动改成实盘数据。</div>` : "";
  $("trBody").hidden = false;
  const no5 = ev.summary.every((x) => !(x.n5 > 0));
  $("trStatus").textContent = `已记录 ${ev.total} 条信号 · 最早 ${ev.since} · 最近 ${ev.latest} · 数据截至 ${ev.latest_price_date || ev.latest}（${ev.timezone_label || ""}）` +
    (no5 ? ` · 尚无信号完成 5 个持有交易日，预计首批统计日期 ${ev.expected_first_5 || "待数据更新"}（按工作日估算，遇市场假期顺延）` : "");
  if (useRep) $("trStatus").textContent = `历史回放：${ev.total} 条信号 · ${ev.since} 至 ${ev.latest}（${ev.timezone_label || ""}）· 实盘记录 ${live && !live.empty ? live.total : 0} 条，首批 5 天统计预计 ${(live && live.expected_first_5) || "待更新"}`;
  const label = (k) => S.meta.strategies[k]?.label || k;
  const H = (n) => `<th>${n}天 样本</th><th>${n}天 平均</th><th>跑赢大盘</th>`;
  const cell = (s, n) => s[`n${n}`] ? `<td>${s[`n${n}`]}</td><td class="${cls(s[`avg${n}`])}">${pct(s[`avg${n}`], 2)}</td><td class="${s[`beat${n}`] > 50 ? "better" : ""}">${fmt(s[`beat${n}`], 0, "%")}</td>` : `<td class="dim">等待样本</td><td class="dim">—</td><td class="dim">—</td>`;
  $("trSummary").innerHTML = `<thead><tr><th>策略</th><th>信号</th><th>次数</th>${H(5)}${H(20)}${H(60)}</tr></thead><tbody>` +
    ev.summary.sort((a, b) => b.signals - a.signals).map((s) => `<tr><td>${esc(label(s.strategy))}</td><td>${pill(s.action)}</td><td>${s.signals}</td>${cell(s, 5)}${cell(s, 20)}${cell(s, 60)}</tr>`).join("") + "</tbody>";
  $("trRecent").innerHTML = `<thead><tr><th>信号日</th><th>股票</th><th>策略</th><th>信号</th><th>5天</th><th>20天</th><th>60天</th><th>至今</th><th>至今比大盘</th></tr></thead><tbody>` +
    ev.recent.map((x) => `<tr class="click" data-c="${esc(x.code)}"><td>${esc(x.signal_date)}</td><td>${esc(x.name)}<span class="code">${esc(x.code)}</span></td><td>${esc(label(x.strategy))}</td><td>${pill(x.action)}</td>
      <td class="${cls(x.r5)}">${pct(x.r5)}</td><td class="${cls(x.r20)}">${pct(x.r20)}</td><td class="${cls(x.r60)}">${pct(x.r60)}</td><td class="${cls(x.r_now)}">${pct(x.r_now)}</td><td class="${cls(x.x_now)}">${pct(x.x_now)}</td></tr>`).join("") + "</tbody>";
  $("trRecent").querySelectorAll("tr.click").forEach((tr) => tr.addEventListener("click", () => openStock(tr.dataset.c)));
}

// ================================================================ 搜索 / 刷新 / 市场切换
function setupSearch() {
  const box = $("q"), res = $("qres"); let items = [], hl = -1;
  const render = () => { res.innerHTML = items.map((x, i) => `<a href="#/stock/${esc(x.code)}" class="${i === hl ? "hl" : ""}"><span>${esc(x.name)}<small> ${esc(x.code)}</small></span><small>${esc(x.kind)}</small></a>`).join(""); res.style.display = items.length ? "block" : "none"; };
  box.addEventListener("input", debounce(async () => { const q = box.value.trim(); items = q ? await api(`/api/search?market=${S.market}&q=${encodeURIComponent(q)}`) : []; hl = -1; render(); }, 180));
  box.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") { hl = Math.min(items.length - 1, hl + 1); render(); e.preventDefault(); }
    else if (e.key === "ArrowUp") { hl = Math.max(0, hl - 1); render(); e.preventDefault(); }
    else if (e.key === "Enter" && items.length) { openStock(items[Math.max(0, hl)].code); box.value = ""; items = []; render(); box.blur(); }
    else if (e.key === "Escape") { items = []; render(); box.blur(); }
  });
  res.addEventListener("click", () => { box.value = ""; items = []; render(); });
  document.addEventListener("click", (e) => { if (!e.target.closest(".search")) { res.style.display = "none"; } });
  document.addEventListener("keydown", (e) => { if ((e.metaKey || e.ctrlKey) && e.key === "k") { e.preventDefault(); box.focus(); } });
}
async function refresh() {
  if (personalMode()) pInvalidate();
  const btn = $("refresh"); btn.classList.add("spin"); btn.disabled = true;
  showRefreshProgress({ running: true, percent: null, phase: "正在启动", message: "准备更新…" });
  try {
    const r = await post(`/api/refresh?market=${S.market}`);
    if (!r.started) { showRefreshProgress({ running: true, percent: null, phase: "已有更新正在运行", message: "正在读取当前进度…" }); }
    poll();
  } catch (e) {
    btn.classList.remove("spin"); btn.disabled = false;
    showRefreshProgress({ running: false, percent: 0, phase: "启动失败", message: e.message, returncode: 1 });
  }
}
function showRefreshProgress(s) {
  const box = $("refreshProgress"); if (!box) return;
  box.hidden = false;
  box.classList.toggle("running", !!s.running);
  box.classList.toggle("failed", !!s.returncode);
  const indeterminate = !!s.running && s.percent == null;
  box.classList.toggle("indeterminate", indeterminate);
  box.title = s.message || s.phase || "";
  const fill = $("refreshProgressBar");
  fill.style.width = indeterminate ? "28%" : `${Math.max(0, Math.min(100, s.percent ?? 0))}%`;
  fill.style.backgroundColor = s.returncode ? "var(--red)" : "#007aff";
  $("refreshProgressPercent").textContent = s.percent == null ? "" : `${s.percent}%`;
  $("refreshProgressText").textContent = s.returncode ? `${s.phase || "更新失败"}：${s.message || "请查看更新日志"}` : (s.phase || "更新中");
  $("refresh").title = s.message || s.phase || "刷新数据";
}
async function poll() {
  let s;
  try { s = await api("/api/status"); }
  catch (e) { showRefreshProgress({ running: false, percent: 0, phase: "状态读取失败", message: e.message, returncode: 1 }); $("refresh").classList.remove("spin"); $("refresh").disabled = false; return; }
  showRefreshProgress(s);
  if (s.running) { setTimeout(poll, 2000); return; }
  $("refresh").classList.remove("spin"); $("refresh").disabled = false;
  if (!s.returncode) { if (personalMode()) pInvalidate(); S.labRan = false; route(); }
  setTimeout(() => { const box = $("refreshProgress"); if (box && !$("refresh").disabled) box.hidden = true; }, s.returncode ? 12000 : 4500);
}
function renderMarkets() {
  const mk = Object.entries(S.meta.markets);
  $("mkt").style.display = mk.length > 1 ? "" : "none";
  $("mkt").innerHTML = mk.map(([k, v]) => `<button data-m="${k}" class="${k === S.market ? "on" : ""}">${esc(v.name)} · ${esc(v.timezone_label || "")}</button>`).join("");
  $("mkt").querySelectorAll("button").forEach((b) => b.addEventListener("click", () => { S.market = b.dataset.m; if (personalMode()) pInvalidate(); store.set("market", S.market); S.labRan = false; renderMarkets(); route(); }));
}

// ---------------------------------------------------------------- 分享给外部（比如国内的家人）
// 网页每隔一段时间自动问一次服务器现在的分享状态：隧道要是自己断线重连、换了新网址，
// 这里会自动跟着更新，不用手动刷新页面。
const SHARE_AS_DOWNLOAD = false;   // true：顶部条显示下载离线版；改成 false 可恢复临时网址分享
let shareLastUrl = null;
function offlineBtnHtml() {
  return `<button class="btn plain" id="offlineDl">下载离线版（发给家人）</button><span class="dim" id="offlineMsg">单个网页文件，对方用浏览器双击就能打开，不用联网；数据停在下载这一刻，不会自动更新。</span>`;
}
function bindOfflineBtn() {
  if (personalMode()) return;
    $("offlineDl").addEventListener("click", async () => {
      const btn = $("offlineDl"), msg = $("offlineMsg");
      btn.disabled = true; btn.textContent = "正在生成…"; msg.textContent = "第一次生成大约需要几十秒，请稍等。";
      try {
        const r = await fetch("/api/offline", { credentials: "same-origin", headers: { "X-Requested-With": "dashboard" } });
        if (!r.ok) { let e = ""; try { e = (await r.json()).error; } catch (x) {} throw new Error(e || ("HTTP " + r.status)); }
        const url = URL.createObjectURL(await r.blob());
        const a = document.createElement("a"); a.href = url; a.download = "量化日报-离线版.html"; document.body.appendChild(a); a.click(); a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 5000);
        msg.textContent = "已下载。把这个文件发给对方即可。";
      } catch (e) { msg.textContent = "下载失败：" + e.message; msg.style.color = "var(--red)"; }
      btn.disabled = false; btn.textContent = "下载离线版（发给家人）";
    });
}
function renderShare(st) {
  if (personalMode()) return;
  const bar = $("shareBar"), el = $("shareBarIn");
  bar.hidden = false;
  if (st.running && st.url) {
    const changed = st.url !== shareLastUrl;
    shareLastUrl = st.url;
    el.innerHTML = `<span>分享中：</span><a class="url" href="${st.url}" target="_blank" id="shareUrl">${st.url}</a>
      <button class="btn plain" id="shareCopy">复制链接</button><button class="btn plain" id="shareStop">停止分享</button>
      <span class="dim">${changed ? "网址刚更新过，发给对方前请用这个最新的链接。" : ""}对方打开这个链接就能看到和你现在一样的网页；网址是临时的，断线重连会换一个（这个页面会自动跟着更新）</span>`;
    $("shareCopy").addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(st.url); $("shareCopy").textContent = "已复制"; setTimeout(() => { $("shareCopy").textContent = "复制链接"; }, 1500); }
      catch (e) { alert("复制失败，长按/选中链接手动复制吧"); }
    });
    $("shareStop").addEventListener("click", async () => { $("shareStop").disabled = true; await postJSON("/api/share?action=stop"); loadShare(); });
  } else if (st.running && st.starting !== false) {
    shareLastUrl = null;
    el.innerHTML = `<span>${esc(st.note || "正在生成分享链接…（通常几秒到一分钟）")}</span>`;
  } else {
    shareLastUrl = null;
    el.innerHTML = `<button class="btn plain" id="shareStart">分享给家人看</button>${st.error ? `<span class="dim" style="color:var(--red)">${esc(st.error)}</span>` : `<span class="dim">生成一个临时网址，对方不用在同一个网络也能打开这个网页；断线会自动重连</span>`}`;
    $("shareStart").addEventListener("click", async () => { $("shareStart").disabled = true; $("shareStart").textContent = "正在开始…"; await postJSON("/api/share?action=start"); loadShare(); });
  }
  el.insertAdjacentHTML("beforeend", offlineBtnHtml()); bindOfflineBtn();
  el.insertAdjacentHTML("beforeend", '<div id="visitBox" style="width:100%"></div>');
}
async function loadShare() {
  if (personalMode()) return;
  try {
    const st = await api("/api/share"); renderShare(st);
    if (st.running && st.url && $("visitBox")) {
      const v = await api("/api/share_visits");
      $("visitBox").innerHTML = !v.total ? `<span class="dim">还没有人打开过这个链接。</span>` :
        `<div style="margin:4px 0"><b style="color:${v.online ? "var(--green, #1a9d4a)" : "inherit"}">${v.online ? "● 现在有 " + v.online + " 人在看" : "○ 现在没有人在看"}</b> <span class="dim">共被打开 ${v.total} 次，${v.visitors.length} 台不同设备（只记录时间、网络地址、国家、浏览器和系统）：</span></div>` +
        `<table class="tbl" style="font-size:12px"><thead><tr><th>状态</th><th>设备</th><th>网络地址</th><th>国家</th><th>首次</th><th>最近</th><th>次数</th></tr></thead><tbody>` +
        v.visitors.slice(0, 8).map((x) => `<tr><td>${x.online ? "● 在看" : "离开"}</td><td>${esc(x.device)}</td><td>${esc(x.ip)}</td><td>${esc(x.country)}</td><td>${esc(x.first)}</td><td>${esc(x.last)}</td><td>${x.n}</td></tr>`).join("") + "</tbody></table>";
    }
  } catch (e) { /* 忽略，下次轮询再试 */ }
}

async function init() {
  try { S.meta = await api("/api/meta"); } catch (e) {
    const message = document.createElement("div"); message.className = "card empty";
    message.textContent = e.message; document.querySelector("main").replaceChildren(message); return;
  }
  setupPersonalNavigation();
  const saved = store.get("market", S.meta.default_market);
  S.market = S.meta.markets[saved] ? saved : S.meta.default_market;
  renderMarkets(); setupSearch();
  $("refresh").addEventListener("click", refresh);
  window.addEventListener("hashchange", route);
  window.addEventListener("resize", debounce(() => { if (location.hash.startsWith("#/stock") && D.data) kChart($("kChart"), D.data); }, 250));
  route();
  if (!personalMode()) {
    loadShare();
    setInterval(loadShare, 15000); // 持续轮询，隧道自动重连/换网址时页面能自己跟上
  }
}
init();
