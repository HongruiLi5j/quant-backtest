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

// ================================================================ 路由
const PAGES = ["today", "market", "watch", "stock", "lab", "track", "bt", "rq", "cmp", "code"];
function go(hash) { if (location.hash !== hash) location.hash = hash; else route(); }
function route() {
  const [, page = "today", arg = null] = (location.hash || "#/today").split("/");
  const p = PAGES.includes(page) ? page : "today";
  S.routeArg = arg;
  document.querySelectorAll(".page").forEach((el) => el.classList.toggle("on", el.id === `p-${p}`));
  const stockPage = ["market", "watch", "stock"].includes(p);
  document.querySelectorAll("#tabs a[data-p]").forEach((a) => a.classList.toggle("on", a.dataset.p === p));
  const mo = document.querySelector("#tabs .more"); if (mo) mo.classList.toggle("on", ["bt", "rq", "cmp", "code"].includes(p));
  window.scrollTo({ top: 0 });
  ({ today: loadToday, market: loadMarket, watch: loadWatch, stock: loadStock, lab: loadLab, track: loadTrack, ...(window.ENG_LOADERS || {}) })[p]()
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

// ================================================================ 全市场
const M = { sort: "sharpe", dir: "desc", page: 0, inited: false, last: null };
const MCOLS = [["code", "股票"], ["d1", "今日"], ["next", "明日仓位"], ["cagr", "策略年化"], ["sharpe", "超额Sharpe"], ["maxdd", "策略回撤"],
  ["sharpe_is", "样本内"], ["sharpe_oos", "样本外"], ["y1", "近1年"], ["win", "胜率"], ["b_cagr", "持有年化"], ["b_sharpe", "持有Sharpe"], ["b_maxdd", "持有回撤"]];

function marketQuery(extra = {}) {
  return new URLSearchParams({ market: S.market, strategy: $("sStrategy").value || "blend2", q: $("sQ").value, board: $("sBoard").value, industry: $("sInd").value,
    signal: $("sSig").value, sort: M.sort, dir: M.dir, page: M.page, ...extra });
}
async function loadMarket() {
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
  if (r.error) { $("tMsg").textContent = r.error; return; }
  T.rows = r.rows;
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
  const params = {}; document.querySelectorAll("#params input").forEach((i) => { params[i.dataset.key] = Number(i.value); });
  const q = new URLSearchParams({ market: S.market, strategy: $("strategy").value, symbol: $("symbol").value, cost: $("cost").value, start: $("start").value, params: JSON.stringify(params) });
  $("runBtn").disabled = true; $("btErr").textContent = "";
  try {
    const r = await api(`/api/backtest?${q}`);
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
  if (!s.returncode) { S.labRan = false; route(); }
  setTimeout(() => { const box = $("refreshProgress"); if (box && !$("refresh").disabled) box.hidden = true; }, s.returncode ? 12000 : 4500);
}
function renderMarkets() {
  const mk = Object.entries(S.meta.markets);
  $("mkt").style.display = mk.length > 1 ? "" : "none";
  $("mkt").innerHTML = mk.map(([k, v]) => `<button data-m="${k}" class="${k === S.market ? "on" : ""}">${esc(v.name)} · ${esc(v.timezone_label || "")}</button>`).join("");
  $("mkt").querySelectorAll("button").forEach((b) => b.addEventListener("click", () => { S.market = b.dataset.m; store.set("market", S.market); S.labRan = false; renderMarkets(); route(); }));
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
  try { S.meta = await api("/api/meta"); } catch (e) { document.querySelector("main").innerHTML = emptyState(esc(e.message)); return; }
  const saved = store.get("market", S.meta.default_market);
  S.market = S.meta.markets[saved] ? saved : S.meta.default_market;
  renderMarkets(); setupSearch();
  $("refresh").addEventListener("click", refresh);
  window.addEventListener("hashchange", route);
  window.addEventListener("resize", debounce(() => { if (location.hash.startsWith("#/stock") && D.data) kChart($("kChart"), D.data); }, 250));
  route();
  loadShare();
  setInterval(loadShare, 15000); // 持续轮询，隧道自动重连/换网址时页面能自己跟上
}
init();
