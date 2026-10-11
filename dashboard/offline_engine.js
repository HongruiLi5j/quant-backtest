"use strict";
// 离线版回测引擎：把 Python 的指标、策略、回测和分析用 JavaScript 重写一遍，让单个 html 文件也能跑回测。
// 逻辑和 src/indicators.py、strategies.py、portfolio.py（单品种）、registry.run、analytics.py 一一对应。
// 导出脚本会把它和数据一起写进离线版；tests 用 Python 结果逐项对比过。
const OE = (() => {
  const TD = 252, isN = Number.isFinite;
  const nan = (n) => new Float64Array(n).fill(NaN);

  // ------------------------------------------------------------ 基础
  function rollApply(a, n, fn) {           // 窗口内全部有效才输出（等同 pandas min_periods = n）
    const L = a.length, o = nan(L);
    for (let i = n - 1; i < L; i++) {
      let ok = true;
      for (let j = i - n + 1; j <= i; j++) if (!isN(a[j])) { ok = false; break; }
      if (ok) o[i] = fn(a, i - n + 1, i);
    }
    return o;
  }
  const sma = (a, n) => rollApply(a, n, (x, s, e) => { let t = 0; for (let j = s; j <= e; j++) t += x[j]; return t / (e - s + 1); });
  const rmax = (a, n) => rollApply(a, n, (x, s, e) => { let m = -Infinity; for (let j = s; j <= e; j++) if (x[j] > m) m = x[j]; return m; });
  const rmin = (a, n) => rollApply(a, n, (x, s, e) => { let m = Infinity; for (let j = s; j <= e; j++) if (x[j] < m) m = x[j]; return m; });
  const rstd = (a, n) => rollApply(a, n, (x, s, e) => {
    let m = 0; const k = e - s + 1; for (let j = s; j <= e; j++) m += x[j]; m /= k;
    let v = 0; for (let j = s; j <= e; j++) v += (x[j] - m) ** 2; return Math.sqrt(v / (k - 1));
  });
  const shift = (a, k = 1) => { const o = nan(a.length); for (let i = k; i < a.length; i++) o[i] = a[i - k]; return o; };
  const diff = (a) => { const o = nan(a.length); for (let i = 1; i < a.length; i++) o[i] = a[i] - a[i - 1]; return o; };
  const pct = (a) => { const o = nan(a.length); for (let i = 1; i < a.length; i++) o[i] = a[i] / a[i - 1] - 1; return o; };

  function wilder(x, n) {
    const L = x.length, o = nan(L);
    let start = -1, cnt = 0;
    for (let i = 0; i < L; i++) if (isN(x[i])) { if (start < 0) start = i; cnt++; }
    if (cnt < n) return o;
    const seedEnd = start + n - 1;
    let s = 0, k = 0;
    for (let i = start; i <= seedEnd; i++) if (isN(x[i])) { s += x[i]; k++; }
    o[seedEnd] = s / k;
    for (let t = seedEnd + 1; t < L; t++) o[t] = o[t - 1] + (x[t] - o[t - 1]) / n;
    return o;
  }
  function trueRange(H, L, C) {
    const n = H.length, o = nan(n);
    o[0] = H[0] - L[0];
    for (let i = 1; i < n; i++) o[i] = Math.max(H[i] - L[i], Math.abs(H[i] - C[i - 1]), Math.abs(L[i] - C[i - 1]));
    return o;
  }
  const atr = (d, n) => wilder(trueRange(d.h, d.l, d.c), n);
  function adx(d, n) {
    const L = d.c.length, up = diff(d.h), dn = diff(d.l).map((v) => -v);
    const pdm = nan(L), mdm = nan(L);
    for (let i = 1; i < L; i++) {
      pdm[i] = up[i] > dn[i] && up[i] > 0 ? up[i] : 0;
      mdm[i] = dn[i] > up[i] && dn[i] > 0 ? dn[i] : 0;
    }
    const tr = trueRange(d.h, d.l, d.c); tr[0] = NaN;
    const ts = wilder(tr, n), p = wilder(pdm, n), m = wilder(mdm, n), dx = nan(L);
    for (let i = 0; i < L; i++) {
      const pdi = 100 * p[i] / ts[i], mdi = 100 * m[i] / ts[i];
      dx[i] = 100 * Math.abs(pdi - mdi) / (pdi + mdi);
    }
    return wilder(dx, n);
  }
  function rsi(c, n) {
    const L = c.length, d = diff(c), g = nan(L), l = nan(L);
    for (let i = 0; i < L; i++) if (isN(d[i])) { g[i] = Math.max(d[i], 0); l[i] = Math.max(-d[i], 0); }
    const G = wilder(g, n), Ls = wilder(l, n), o = nan(L);
    for (let i = 0; i < L; i++) if (isN(G[i])) o[i] = Ls[i] > 0 ? 100 - 100 / (1 + G[i] / Ls[i]) : 100;
    return o;
  }
  const realizedVol = (c, n) => rstd(pct(c), n).map((v) => v * Math.sqrt(TD));
  function cross(a, b) {
    const o = new Uint8Array(a.length);
    for (let i = 1; i < a.length; i++) o[i] = a[i] > b[i] && a[i - 1] <= b[i - 1] ? 1 : 0;
    return o;
  }
  function volScale(c, tv, vlen, maxLev = 1) {
    if (tv === null || tv === undefined) return new Float64Array(c.length).fill(1);
    return realizedVol(c, vlen).map((v) => (isN(v) ? Math.min(maxLev, tv / v) : NaN));
  }
  const fill0 = (a) => a.map((v) => (isN(v) ? v : 0));
  const mulV = (state, v) => fill0(state.map((s, i) => s * v[i]));

  // ------------------------------------------------------------ 策略
  function trendVol(d, o) {
    const { sma_len = 200, buffer = 0.01, target_vol = 0.15, vol_len = 20, max_lev = 1 } = o;
    const c = d.c, m = sma(c, sma_len), st = new Float64Array(c.length);
    let on = false;
    for (let t = 0; t < c.length; t++) {
      if (!isN(m[t])) continue;
      if (!on && c[t] > m[t] * (1 + buffer)) on = true;
      else if (on && c[t] < m[t] * (1 - buffer)) on = false;
      st[t] = on ? 1 : 0;
    }
    return mulV(st, volScale(c, target_vol, vol_len, max_lev));
  }
  function trendEns(d, o) {
    const { buffer = 0.01, target_vol = 0.15, vol_len = 20, max_lev = 1 } = o, lens = [100, 150, 200, 250];
    const sts = lens.map((n) => trendVol(d, { sma_len: n, buffer, target_vol: null }));
    const avg = d.c.map((_, i) => sts.reduce((s, a) => s + a[i], 0) / sts.length);
    return mulV(avg, volScale(d.c, target_vol, vol_len, max_lev));
  }
  function donchian(d, o) {
    const { entry_len = 55, exit_len = 20, atr_len = 20, atr_mult = 3, target_vol = 0.15, vol_len = 20 } = o;
    const c = d.c, hi = shift(rmax(d.h, entry_len)), lo = shift(rmin(d.l, exit_len)), a = atr(d, atr_len), st = new Float64Array(c.length);
    let on = false, peak = 0;
    for (let t = 0; t < c.length; t++) {
      if (!isN(hi[t]) || !isN(a[t])) continue;
      if (!on) { if (c[t] > hi[t]) { on = true; peak = c[t]; } }
      else { peak = Math.max(peak, c[t]); if (c[t] < lo[t] || c[t] < peak - atr_mult * a[t]) on = false; }
      st[t] = on ? 1 : 0;
    }
    return mulV(st, volScale(c, target_vol, vol_len));
  }
  const donchianS1 = (d, o) => donchian(d, { entry_len: o.entry_len ?? 20, exit_len: o.exit_len ?? 10, atr_len: 20, atr_mult: o.atr_mult ?? 2, target_vol: o.target_vol === undefined ? 0.15 : o.target_vol });
  function rsi2(d, o) {
    const { trend_len = 200, rsi_len = 2, entry_th = 10, exit_ma = 5, max_hold = 10 } = o;
    const c = d.c, tr = sma(c, trend_len), r = rsi(c, rsi_len), ex = sma(c, exit_ma), st = new Float64Array(c.length);
    let on = false, held = 0;
    for (let t = 0; t < c.length; t++) {
      if (!isN(tr[t]) || !isN(r[t])) continue;
      if (!on) { if (c[t] > tr[t] && r[t] < entry_th) { on = true; held = 0; } }
      else { held++; if (c[t] > ex[t] || held >= max_hold) on = false; }
      st[t] = on ? 1 : 0;
    }
    return st;
  }
  function maAdxPlus(d, o) {
    const { fast = 5, slow = 20, adx_len = 14, adx_min = 20, regime_len = 200, atr_len = 14, atr_mult = 3, target_vol = 0.15, vol_len = 20 } = o;
    const c = d.c, mf = sma(c, fast), ms = sma(c, slow), cu = cross(mf, ms), cd = cross(ms, mf), ax = adx(d, adx_len);
    const reg = sma(c, regime_len), a = atr(d, atr_len), st = new Float64Array(c.length);
    let on = false, peak = 0;
    for (let t = 0; t < c.length; t++) {
      if (!isN(reg[t]) || !isN(a[t])) continue;
      const up = cu[t] && ax[t] > adx_min;
      if (!on) { if (up && c[t] > reg[t]) { on = true; peak = c[t]; } }
      else { peak = Math.max(peak, c[t]); if (cd[t] || c[t] < peak - atr_mult * a[t] || c[t] < reg[t]) on = false; }
      st[t] = on ? 1 : 0;
    }
    return mulV(st, volScale(c, target_vol, vol_len));
  }
  function maAdxOrig(d, o) {
    const { fast = 5, slow = 20, adx_len = 14, adx_min = 20 } = o;
    const mf = sma(d.c, fast), ms = sma(d.c, slow), cu = cross(mf, ms), cd = cross(ms, mf), ax = adx(d, adx_len), st = new Float64Array(d.c.length);
    let on = false;
    for (let t = 0; t < st.length; t++) {
      const up = cu[t] && ax[t] > adx_min;
      if (!on && up) on = true; else if (on && cd[t]) on = false;
      st[t] = on ? 1 : 0;
    }
    return st;
  }
  function targetWeights(name, d, p) {
    const q = { ...p };
    if ("target_vol" in q) q.target_vol = q.target_vol ? q.target_vol : null;
    switch (name) {
      case "buy_hold": return new Float64Array(d.c.length).fill(1);
      case "trend_ens": return trendEns(d, q);
      case "trend_vol": return trendVol(d, q);
      case "donchian_s1": return donchianS1(d, q);
      case "rsi2": return rsi2(d, q);
      case "ma_adx_original": return maAdxOrig(d, q);
      case "ma_adx_plus": return maAdxPlus(d, q);
      case "blend2": {
        const tot = p.w_trend + p.w_rsi + p.w_donchian;
        const mix = tot <= 0 ? [1 / 3, 1 / 3, 1 / 3] : [p.w_trend / tot, p.w_rsi / tot, p.w_donchian / tot];
        const a = trendEns(d, {}), b = rsi2(d, {}), c = donchianS1(d, {});
        return a.map((_, i) => mix[0] * a[i] + mix[1] * b[i] + mix[2] * c[i]);
      }
    }
    throw new Error("离线版暂不支持这个策略");
  }

  // ------------------------------------------------------------ 回测（单品种，对应 portfolio.run_weights）
  function applyBand(t, band) {
    if (band <= 0) return t;
    const o = new Float64Array(t.length); let cur = 0;
    for (let i = 0; i < t.length; i++) {
      const change = Math.abs(t[i] - cur) > band || ((t[i] === 0) !== (cur === 0));
      if (change) cur = t[i];
      o[i] = cur;
    }
    return o;
  }
  function runWeights(d, target, cost, band, cashRet, limit) {
    const n = d.c.length, O = d.o, C = d.c;
    let tgt = Float64Array.from(target, (v) => (isN(v) ? Math.max(v, 0) : 0));
    tgt = applyBand(tgt, band);
    let held = shift(tgt).map((v) => (isN(v) ? v : 0));
    const gap = new Float64Array(n);
    for (let i = 1; i < n; i++) gap[i] = O[i] / C[i - 1] - 1;
    if (limit) {
      let prev = 0; const act = new Float64Array(n);
      for (let i = 0; i < n; i++) {
        let w = held[i];
        if ((w > prev && gap[i] >= limit - 0.002) || (w < prev && gap[i] <= -limit + 0.002)) w = prev;
        act[i] = prev = w;
      }
      held = act;
    }
    const hp = shift(held).map((v) => (isN(v) ? v : 0));
    const ri = new Float64Array(n), intra = new Float64Array(n), wc = new Float64Array(n), turn = new Float64Array(n);
    for (let i = 0; i < n; i++) { ri[i] = C[i] / O[i] - 1; intra[i] = held[i] * ri[i]; wc[i] = held[i] * (1 + ri[i]) / (1 + intra[i]); turn[i] = Math.abs(held[i] - hp[i]); }
    const ret = new Float64Array(n);
    for (let i = 0; i < n; i++) {
      const g = (i > 0 ? wc[i - 1] : 0) * gap[i];
      ret[i] = (1 + g) * (1 - cost * turn[i]) * (1 + intra[i]) - 1;
      if (cashRet) ret[i] += Math.max(1 - held[i], 0) * (isN(cashRet[i]) ? cashRet[i] : 0);
    }
    return { ret, held, trades: splitTrades(held, ret) };
  }
  function splitTrades(w, r) {
    const rows = []; let start = -1; const n = w.length;
    const prod = (a, b) => { let p = 1; for (let i = a; i <= b; i++) p *= 1 + r[i]; return p - 1; };
    for (let i = 0; i < n; i++) {
      if (w[i] > 0 && start < 0) start = i;
      else if (!(w[i] > 0) && start >= 0) { rows.push({ a: start, b: i, bars: i - start, ret: prod(start, i) }); start = -1; }
    }
    if (start >= 0) rows.push({ a: start, b: n - 1, bars: n - 1 - start, ret: prod(start, n - 1) });
    return rows;
  }

  // ------------------------------------------------------------ 统计（metrics.stats / analytics）
  const mean = (a) => a.reduce((s, v) => s + v, 0) / a.length;
  const sd = (a) => { if (a.length < 2) return NaN; const m = mean(a); return Math.sqrt(a.reduce((s, v) => s + (v - m) ** 2, 0) / (a.length - 1)); };
  const sharpe = (r) => { const s = sd(r); return s > 0 ? mean(r) / s * Math.sqrt(TD) : NaN; };
  function equityOf(r) { const e = new Float64Array(r.length); let v = 1; for (let i = 0; i < r.length; i++) { v *= 1 + r[i]; e[i] = v; } return e; }
  function maxdd(e) { let pk = -Infinity, m = 0; for (const v of e) { pk = Math.max(pk, v); m = Math.min(m, v / pk - 1); } return m; }
  const cl = (v, d = 4) => (isN(v) ? Math.round(v * 10 ** d) / 10 ** d : null);
  function basic(r) {
    r = Array.from(r); if (r.length < 20) return {};
    const e = equityOf(r), years = r.length / TD, cagr = e[e.length - 1] ** (1 / years) - 1, vol = sd(r) * Math.sqrt(TD);
    const dn = sd(r.filter((v) => v < 0)) * Math.sqrt(TD), mdd = maxdd(e);
    return { days: r.length, total: cl((e[e.length - 1] - 1) * 100, 2), cagr: cl(cagr * 100, 2), vol: cl(vol * 100, 2),
      sharpe: cl(vol > 0 ? mean(r) * TD / vol : NaN, 3), sortino: cl(dn > 0 ? mean(r) * TD / dn : NaN, 3), maxdd: cl(mdd * 100, 2), calmar: cl(mdd < 0 ? cagr / -mdd : NaN, 3) };
  }
  function pctile(a, q) {
    const s = Array.from(a).sort((x, y) => x - y), k = (s.length - 1) * q / 100, f = Math.floor(k), c = Math.min(f + 1, s.length - 1);
    return s[f] + (s[c] - s[f]) * (k - f);
  }
  function monthly(r, dates) {
    const m = new Map(), y = new Map();
    for (let i = 0; i < r.length; i++) {
      const yy = +dates[i].slice(0, 4), mm = +dates[i].slice(5, 7), k = yy * 100 + mm;
      m.set(k, (m.get(k) ?? 1) * (1 + r[i])); y.set(yy, (y.get(yy) ?? 1) * (1 + r[i]));
    }
    const years = [...y.keys()].sort((a, b) => a - b);
    return { years, matrix: years.map((yy) => Array.from({ length: 12 }, (_, i) => (m.has(yy * 100 + i + 1) ? cl((m.get(yy * 100 + i + 1) - 1) * 100, 2) : null))), year_total: years.map((yy) => cl((y.get(yy) - 1) * 100, 2)) };
  }
  function rollSharpe(r, w = 126) {
    const o = new Array(r.length).fill(null);
    for (let i = w - 1; i < r.length; i++) { const s = r.slice(i - w + 1, i + 1), sdv = sd(s); o[i] = sdv > 0 ? cl(mean(s) / sdv * Math.sqrt(TD), 3) : null; }
    return o;
  }
  function hist(v, bins = 14) {
    v = v.filter(isN); if (v.length < 3) return { edges: [], counts: [] };
    let lo = pctile(v, 1), hi = pctile(v, 99); if (hi <= lo) hi = lo + 1;
    const counts = new Array(bins).fill(0), w = (hi - lo) / bins;
    for (const x0 of v) { const x = Math.min(Math.max(x0, lo), hi); counts[Math.min(bins - 1, Math.floor((x - lo) / w))]++; }
    return { edges: Array.from({ length: bins + 1 }, (_, i) => cl(lo + w * i, 2)), counts };
  }
  function tradeStats(tr, days) {
    if (!tr.length) return {};
    const w = tr.filter((x) => x > 0), l = tr.filter((x) => x <= 0); let mx = 0, cur = 0;
    for (const x of tr) { cur = x <= 0 ? cur + 1 : 0; mx = Math.max(mx, cur); }
    return { n: tr.length, win: cl(w.length / tr.length * 100, 1), avg_win: cl(w.length ? mean(w) : 0, 2), avg_loss: cl(l.length ? mean(l) : 0, 2), best: cl(Math.max(...tr), 2), worst: cl(Math.min(...tr), 2),
      payoff: cl(w.length && l.length && mean(l) < 0 ? mean(w) / -mean(l) : NaN, 2), expectancy: cl(mean(tr), 2), max_lose_streak: mx, avg_days: cl(days.length ? mean(days) : NaN, 1), ret_hist: hist(tr), days_hist: hist(days, 12) };
  }
  function skewKurt(r) {
    const n = r.length, m = mean(r), s = sd(r); let s3 = 0, s4 = 0;
    for (const x of r) { const z = (x - m) / s; s3 += z ** 3; s4 += z ** 4; }
    return { skew: n > 2 ? n / ((n - 1) * (n - 2)) * s3 : NaN, kurt: n > 3 ? n * (n + 1) / ((n - 1) * (n - 2) * (n - 3)) * s4 - 3 * (n - 1) ** 2 / ((n - 2) * (n - 3)) : NaN };
  }
  function analyze(r, b, dates, trRets, trDays, expo, benchmarks) {
    const n = r.length, cut = Math.floor(n * 0.7), mo = monthly(r, dates), sk = skewKurt(r);
    const flat = mo.matrix.flat().filter((v) => v !== null);
    const out = { full: basic(r), bench_full: basic(b),
      split: { date: cut > 0 && cut < n ? dates[cut] : null, is: basic(r.slice(0, cut)), oos: basic(r.slice(cut)), b_is: basic(b.slice(0, cut)), b_oos: basic(b.slice(cut)) },
      monthly: mo, bench_year: monthly(b, dates).year_total, rolling_sharpe: rollSharpe(r), bench_rolling_sharpe: rollSharpe(b),
      extra: { skew: cl(sk.skew, 2), kurt: cl(sk.kurt, 2), var95: cl(pctile(r, 5) * 100, 2), best_day: cl(Math.max(...r) * 100, 2), worst_day: cl(Math.min(...r) * 100, 2), pos_months: flat.length ? cl(flat.filter((v) => v > 0).length / flat.length * 100, 1) : null } };
    out.trades = tradeStats(trRets, trDays);
    out.exposure = Array.from(expo, (v) => cl(v, 3));
    if (benchmarks && Object.keys(benchmarks).length) out.benchmarks = benchmarks;
    return out;
  }

  // ------------------------------------------------------------ 对外：一次回测（对应 registry.run(detail=True)）
  function cleanParams(def, raw) {
    const o = {};
    for (const p of def.params) {
      let v = Number((raw || {})[p.key] ?? p.default); if (!isN(v)) v = p.default;
      v = Math.min(Math.max(v, p.min), p.max);
      o[p.key] = Number.isInteger(p.step) ? Math.round(v) : v;
    }
    return o;
  }
  const key = (d) => `${String(d).slice(0, 4)}-${String(d).slice(4, 6)}-${String(d).slice(6, 8)}`;
  const SCALE = 10000, cache = new Map();
  function dec(a, scale) { const o = new Float64Array(a.length); let s = 0; for (let i = 0; i < a.length; i++) { s += a[i]; o[i] = s / scale; } return o; }
  function load(D, m, code) {
    const p = D.prices[m][code]; if (!p) return null;
    const ck = m + "|" + code; if (cache.has(ck)) return cache.get(ck);
    const pos = dec(p.di, 1), cal = D.cal[m], di = Array.from(pos, (i) => cal[i]);
    const r = { dates: di.map(key), di, o: dec(p.o, SCALE), h: dec(p.h, SCALE), l: dec(p.l, SCALE), c: dec(p.c, SCALE) };
    cache.set(ck, r); return r;
  }
  function run(D, m, name, sym, rawParams, costBp, start) {
    const def = D.meta.strategies[name], cfg = D.cfg[m];
    if (!def || def.kind !== "single") throw new Error("离线版只支持单品种策略");
    const d = load(D, m, sym); if (!d) throw new Error(`${sym} 在离线版里没有数据`);
    const params = cleanParams(def, rawParams), isStock = !(sym in cfg.universe);
    if (costBp === null || costBp === undefined || !isN(costBp)) costBp = isStock ? cfg.stock_cost_bp : cfg.cost_bp;
    const cost = costBp / 1e4;
    let cashRet = null;
    const cd = load(D, m, cfg.cash);
    if (cd) { const pc = pct(cd.c), mp = new Map(cd.di.map((x, i) => [x, pc[i]])); cashRet = Float64Array.from(d.di, (x) => (mp.has(x) && isN(mp.get(x)) ? mp.get(x) : 0)); }
    const limit = m === "cn" ? (D.limits[sym] ?? 0.1) : null;
    const W = targetWeights(name, d, params), band = name !== "buy_hold" ? 0.1 : 0;
    const res = runWeights(d, W, cost, band, cashRet, limit);
    const bench = runWeights(d, new Float64Array(d.c.length).fill(1), cost, 0, null, limit);
    const n = d.c.length, warmI = Math.min(260, n - 1);
    let t0 = warmI;
    if (start) { const s = Number(start.replace(/-/g, "")); const j = d.di.findIndex((x) => x >= s); if (j >= 0) t0 = Math.max(t0, j); else t0 = n - 1; }
    const idx = []; for (let i = t0; i < n; i++) idx.push(i);
    const r = idx.map((i) => res.ret[i]), b = idx.map((i) => bench.ret[i]), dates = idx.map((i) => d.dates[i]);
    const rf = cashRet ? idx.map((i) => cashRet[i]) : null;
    const wsum = idx.map((i) => res.held[i]);
    const inRange = res.trades.filter((t) => t.a >= t0);   // 进场日在统计区间内
    const stat = (x, withTr) => {
      const e = equityOf(x), years = x.length / TD, cagr = e[e.length - 1] ** (1 / years) - 1, vol = sd(x) * Math.sqrt(TD), dn = sd(x.filter((v) => v < 0)) * Math.sqrt(TD), mdd = maxdd(e);
      const s = { "CAGR%": cagr * 100, "Vol%": vol * 100, Sharpe: sharpe(x), Sortino: dn > 0 ? mean(x) * TD / dn : NaN, "MaxDD%": mdd * 100, Calmar: mdd < 0 ? cagr / -mdd : NaN };
      s.ExcessSharpe = rf ? sharpe(x.map((v, i) => v - rf[i])) : s.Sharpe;
      s["TotalRet%"] = (e[e.length - 1] - 1) * 100;
      if (withTr) {
        s["Exposure%"] = wsum.filter((v) => v > 0).length / wsum.length * 100;
        const on = wsum.filter((v) => v > 0); s.AvgPos = on.length ? mean(on) : 0;
        if (inRange.length) {
          const w = inRange.filter((t) => t.ret > 0), l = inRange.filter((t) => t.ret <= 0), ls = l.reduce((a, t) => a + t.ret, 0);
          s.Trades = inRange.length; s["Win%"] = w.length / inRange.length * 100; s.PF = ls < 0 ? w.reduce((a, t) => a + t.ret, 0) / -ls : Infinity; s.AvgHold = mean(inRange.map((t) => t.bars));
        }
      }
      return Object.fromEntries(Object.entries(s).map(([k, v]) => [k, isN(v) ? v : null]));
    };
    const eq = equityOf(r), beq = equityOf(b); let pkv = 0; const dd = eq.map((v) => { pkv = Math.max(pkv, v); return v / pkv - 1; });
    const tail = (k) => { const x = r.slice(-k); return (x.reduce((a, v) => a * (1 + v), 1) - 1) * 100; };
    const lastYear = dates[dates.length - 1].slice(0, 4);
    const ytd = (r.filter((_, i) => dates[i].startsWith(lastYear)).reduce((a, v) => a * (1 + v), 1) - 1) * 100;
    const wNow = res.held[n - 1], wNext = W[n - 1] || 0;
    const allT = res.trades.slice(-30).reverse();
    const trades = allT.map((t, i) => ({ entry: d.dates[t.a], exit: d.dates[t.b], days: t.bars, ret: Math.round(t.ret * 1e4) / 100, open: i === 0 && wNow > 0 }));
    // 分析区
    const trR = inRange.map((t) => t.ret * 100), trD = inRange.map((t) => t.bars);
    const bm = {};
    for (const c of (m === "cn" ? ["510300", "510500"] : ["SPY", "QQQ"])) {
      if (c === sym || !D.prices[m][c]) continue;
      const o = load(D, m, c), mp = new Map(o.di.map((x, i) => [x, o.c[i]]));
      let last = null, first = null; const arr = [];
      for (const i of idx) { const v = mp.get(d.di[i]); if (v !== undefined) last = v; if (first === null && last !== null) first = last; arr.push(last === null ? null : Math.round(last / first * 1e5) / 1e5); }
      bm[cfg.universe[c] || c] = arr;
    }
    const ana = analyze(r, b, dates, trR, trD, wsum, bm);
    ana.cost_sens = [["0", 0], ["1x", 1], ["2x", 2], ["4x", 4]].map(([lab, k]) => {
      const x = runWeights(d, W, cost * k, band, cashRet, limit).ret, rr = idx.map((i) => x[i]), s = basic(rr);
      return { label: lab, cost_bp: Math.round(cost * k * 1e5) / 10, cagr: s.cagr, sharpe: s.sharpe, maxdd: s.maxdd };
    });
    const tpl = (D.assump[m] || {})[isStock ? 1 : 0] || [];
    ana.assumptions = tpl.map(([k, v]) => [k, v.replace("单边成本 0bp", `单边成本 ${+costBp.toFixed(2)}bp`)]);
    const rnd = (a, k) => Array.from(a, (v) => Math.round(v * 10 ** k) / 10 ** k);
    return {
      analysis: ana, strategy: name, symbol: sym, params, cost_bp: costBp, is_stock: isStock,
      start: dates[0], end: dates[dates.length - 1], dates, equity: rnd(eq, 5), bench: rnd(beq, 5), drawdown: rnd(dd.map((v) => v * 100), 3),
      stats: stat(r, true), bench_stats: stat(b, false),
      recent: { "1D": tail(1), "1W": tail(5), "1M": tail(21), "3M": tail(63), "1Y": tail(252), YTD: ytd },
      position_now: wNow > 1e-9 ? { [sym]: wNow } : {}, position_next: wNext > 1e-9 ? { [sym]: wNext } : {}, trades,
    };
  }
  return { run, _t: { sma, adx, rsi, wilder, targetWeights, runWeights, load } };
})();
if (typeof module !== "undefined") module.exports = OE;
