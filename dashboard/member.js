"use strict";
(function () {
  var S = { user: null, meta: null, market: "cn", tab: "mkt", pf: null, hide: false, strat: "blend2" };
  try { S.hide = localStorage.getItem("mb-hide") === "1"; S.strat = localStorage.getItem("mb-strat") || "blend2"; } catch (e) {}
  var T = { side: "buy", code: "", sig: null, qty: "", busy: false };
  var app = document.getElementById("app"), CUR = { cn: "¥", us: "$" };

  function h(tag, attrs) {
    var e = document.createElement(tag);
    for (var k in (attrs || {})) {
      var v = attrs[k];
      if (k === "class") e.className = v; else if (k.slice(0, 2) === "on") e.addEventListener(k.slice(2), v);
      else if (k === "value") e.value = v; else if (v !== undefined && v !== null && v !== false) e.setAttribute(k, v);
    }
    (function add(list) {
      list.forEach(function (c) {
        if (c == null || c === false) return;
        if (Array.isArray(c)) return add(c);
        e.appendChild(typeof c === "object" ? c : document.createTextNode(String(c)));
      });
    })([].slice.call(arguments, 2));
    return e;
  }
  function toast(t) { var el = h("div", { class: "toast" }, t); document.body.appendChild(el); setTimeout(function () { el.remove(); }, 2200); }
  function api(path, body) {
    var o = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json", "X-Requested-With": "dashboard" }, body: JSON.stringify(body) };
    return fetch(path, o).then(function (r) { return r.json().then(function (j) { if (!r.ok || j.error) throw new Error(j.error || r.status); return j; }); });
  }
  function num(n, d) { return n == null || isNaN(n) ? "—" : Number(n).toLocaleString("en-US", { minimumFractionDigits: d == null ? 2 : d, maximumFractionDigits: d == null ? 2 : d }); }
  function money(n, d) { return S.hide ? "****" : num(n, d); }
  function sgn(n, d) { return n == null ? "—" : (n > 0 ? "+" : "") + num(n, d); }
  function cls(n) { return n > 0 ? "up" : n < 0 ? "down" : ""; }
  function pct(n) { return n == null ? "—" : sgn(n, 2) + "%"; }
  function mk() { return CUR[S.market]; }

  // ---------------------------------------------------------------- 登录 / 注册
  function loginView() {
    var mode = "login", box = h("div", { class: "login" });
    function draw() {
      box.textContent = "";
      var name = h("input", { placeholder: "用户名（3–20 位）", autocomplete: "username" });
      var pw = h("input", { type: "password", placeholder: "密码（至少 8 位）", autocomplete: mode === "login" ? "current-password" : "new-password" });
      var msg = h("div", { class: "warn" });
      function go() { msg.textContent = ""; api("/api/member/" + mode, { name: name.value, password: pw.value }).then(function (j) { S.user = j.user; boot(); }).catch(function (e) { msg.textContent = e.message; }); }
      pw.addEventListener("keydown", function (e) { if (e.key === "Enter") go(); });
      box.appendChild(h("div", { style: "font-size:22px;font-weight:700" }, "模拟交易"));
      box.appendChild(h("div", { class: "mut sm" }, mode === "login" ? "登录你的模拟账户" : "注册后获得模拟账户：A 股 ¥1,000,000，美股 $100,000（全部是虚拟资金）"));
      box.appendChild(name); box.appendChild(pw);
      if (mode === "register") box.appendChild(h("div", { class: "mut sm" }, "说明：站长可以看到会员的登录时间、网络地址、查看过的策略信号和模拟交易记录。请不要使用你在其他网站用的密码。"));
      box.appendChild(h("button", { class: "btn", onclick: go }, mode === "login" ? "登录" : "注册并登录"));
      box.appendChild(h("button", { class: "btn g", onclick: function () { mode = mode === "login" ? "register" : "login"; draw(); } }, mode === "login" ? "没有账号？去注册" : "已有账号？去登录"));
      box.appendChild(msg);
      box.appendChild(h("a", { href: "/", class: "mut sm", style: "text-align:center" }, "← 回到主页"));
    }
    draw(); app.textContent = ""; app.appendChild(box);
  }

  // ---------------------------------------------------------------- 外壳：头部 + 底部标签
  function shell(content) {
    var p = S.pf;
    app.textContent = "";
    var hd = h("div", { class: "hd" },
      h("div", { class: "top" }, h("a", { href: "/" }, "‹ 主页"), h("b", null, "模拟交易"),
        h("div", { class: "pill" }, [["cn", "A 股"], ["us", "美股"]].map(function (m) {
          return h("button", { class: S.market === m[0] ? "on" : "", onclick: function () { if (S.market !== m[0]) { S.market = m[0]; T.code = ""; T.sig = null; loadPf(); } } }, m[1]);
        }))),
      h("div", { class: "big" }, "总资产（" + (S.market === "cn" ? "元" : "美元") + "）", h("button", { onclick: function () { S.hide = !S.hide; try { localStorage.setItem("mb-hide", S.hide ? "1" : "0"); } catch (e) {} render(); } }, S.hide ? "🙈" : "👁")),
      h("div", { class: "amt" }, p ? money(p.total) : "—"),
      p && h("div", { class: "g3" },
        h("div", null, "累计收益", h("b", null, S.hide ? "****" : sgn(p.total - p.initial) + "（" + pct(p.ret_pct) + "）")),
        h("div", null, "持仓市值", h("b", null, money(p.market_value))),
        h("div", null, "可用资金", h("b", null, money(p.cash)))));
    app.appendChild(hd);
    app.appendChild(content);
    var tabs = [["mkt", "📈", "行情"], ["hold", "📊", "持仓"], ["trade", "🔁", "交易"], ["deals", "🧾", "成交"], ["me", "👤", "我的"]];
    app.appendChild(h("div", { class: "tabbar" }, tabs.map(function (t) {
      return h("button", { class: S.tab === t[0] ? "on" : "", onclick: function () { S.tab = t[0]; render(); window.scrollTo(0, 0); } }, h("i", null, t[1]), t[2]);
    })));
  }

  function curve(pts) {
    var NS = "http://www.w3.org/2000/svg", svg = document.createElementNS(NS, "svg");
    svg.setAttribute("class", "cv"); svg.setAttribute("viewBox", "0 0 600 110"); svg.setAttribute("preserveAspectRatio", "none");
    if (pts.length < 2) { var t = document.createElementNS(NS, "text"); t.setAttribute("x", 8); t.setAttribute("y", 40); t.setAttribute("fill", "#8a8f99"); t.setAttribute("font-size", "22"); t.textContent = "每天打开页面会记录一个净值点，明天起显示曲线"; svg.appendChild(t); return svg; }
    var v = pts.map(function (p) { return p[1]; }), lo = Math.min.apply(null, v), hi = Math.max.apply(null, v), sp = hi - lo || 1, d = "";
    pts.forEach(function (p, i) { d += (i ? "L" : "M") + (i / (pts.length - 1) * 600).toFixed(1) + "," + (100 - (p[1] - lo) / sp * 90).toFixed(1); });
    var up = v[v.length - 1] >= v[0], path = document.createElementNS(NS, "path");
    path.setAttribute("d", d); path.setAttribute("fill", "none"); path.setAttribute("stroke", up ? "#e23b34" : "#12a05a"); path.setAttribute("stroke-width", "2"); path.setAttribute("vector-effect", "non-scaling-stroke");
    svg.appendChild(path); return svg;
  }

  // ---------------------------------------------------------------- 行情
  var BOARD = {};
  function svg(tag, a, par) { var e = document.createElementNS("http://www.w3.org/2000/svg", tag); for (var k in a) e.setAttribute(k, a[k]); if (par) par.appendChild(e); return e; }
  function spark(vals, up) {
    var W = 70, H = 28, mn = Math.min.apply(null, vals), mx = Math.max.apply(null, vals), rg = mx - mn || 1;
    var el = svg("svg", { viewBox: "0 0 " + W + " " + H, width: W, height: H });
    svg("polyline", { fill: "none", stroke: up ? "var(--up)" : "var(--down)", "stroke-width": 1.5, points: vals.map(function (v, i) { return (i / (vals.length - 1) * W).toFixed(1) + "," + (H - 2 - (v - mn) / rg * (H - 4)).toFixed(1); }).join(" ") }, el);
    return el;
  }
  function mktView() {
    var b = BOARD[S.market], box = h("div");
    if (!b) { api("/api/member/board?market=" + S.market).then(function (r) { BOARD[S.market] = r; if (S.tab === "mkt") render(); }).catch(function (e) { toast(e.message); }); box.appendChild(h("div", { class: "empty" }, "行情加载中…")); return box; }
    if (b.indices.length) {
      var g = h("div", { style: "display:grid;grid-template-columns:repeat(auto-fill,minmax(105px,1fr));gap:8px" });
      b.indices.forEach(function (x) {
        g.appendChild(h("div", { class: "ix", onclick: function () { gotoTrade("buy", x.code); } }, h("div", { class: "sm mut" }, x.name), h("b", { class: cls(x.chg), style: "font-size:17px" }, num(x.price)), h("div", { class: "sm " + cls(x.chg) }, pct(x.chg))));
      });
      box.appendChild(h("div", { class: "sec" }, h("h3", null, "主要指数 / ETF"), g));
    }
    var sec = h("div", { class: "sec" }, h("div", { class: "row sp" }, h("h3", null, "自选行情"), h("span", { class: "sm mut" }, "点一行看 K 线并交易")));
    if (!b.watch.length) sec.appendChild(h("div", { class: "empty" }, "站长的自选列表是空的，可以去「交易」页直接搜索"));
    b.watch.slice().sort(function (a, c) { return c.chg - a.chg; }).forEach(function (x) {
      sec.appendChild(h("div", { class: "tr mk", onclick: function () { gotoTrade("buy", x.code); } },
        h("span", null, h("b", null, x.name), h("div", { class: "sm mut" }, x.code)), h("span", { style: "display:flex;justify-content:center" }, spark(x.spark, x.chg >= 0)),
        h("span", { style: "text-align:right" }, h("b", { class: cls(x.chg) }, num(x.price)), h("div", { class: "sm " + cls(x.chg) }, pct(x.chg)))));
    });
    box.appendChild(sec);
    box.appendChild(h("div", { class: "sm mut", style: "padding:4px 16px" }, "数据为日线收盘价，涨跌幅 = 最新收盘较前一交易日。"));
    return box;
  }
  function kline(k, big) {
    var W = big ? 760 : 340, H = big ? 290 : 190, ml = 4, mr = 40, mt = 6, mb = 16, n = k.c.length;
    var lo = Math.min.apply(null, k.l.filter(function (v) { return v != null; })), hi = Math.max.apply(null, k.h.filter(function (v) { return v != null; })), rg = hi - lo || 1;
    var X = function (i) { return ml + (i + 0.5) * (W - ml - mr) / n; }, Y = function (v) { return mt + (hi - v) / rg * (H - mt - mb); };
    var el = svg("svg", { viewBox: "0 0 " + W + " " + H, style: "width:100%;height:auto;display:block" });
    for (var g = 0; g <= 3; g++) { var gv = lo + rg * g / 3, gy = Y(gv); svg("line", { x1: ml, x2: W - mr, y1: gy, y2: gy, stroke: "var(--sep)", "stroke-width": 1 }, el); svg("text", { x: W - mr + 3, y: gy + 3, "font-size": big ? 11 : 9, fill: "var(--text2)" }, el).textContent = gv.toFixed(2); }
    var bw = Math.max(1.2, (W - ml - mr) / n * 0.7);
    for (var i = 0; i < n; i++) {
      var up = k.c[i] >= k.o[i], col = up ? "var(--up)" : "var(--down)";
      svg("line", { x1: X(i), x2: X(i), y1: Y(k.h[i]), y2: Y(k.l[i]), stroke: col, "stroke-width": 1 }, el);
      svg("rect", { x: X(i) - bw / 2, y: Math.min(Y(k.o[i]), Y(k.c[i])), width: bw, height: Math.max(1, Math.abs(Y(k.o[i]) - Y(k.c[i]))), fill: col }, el);
    }
    [["ma5", "#f5a623"], ["ma20", "#4c93ff"]].forEach(function (m) {
      var pts = []; k[m[0]].forEach(function (v, i) { if (v != null) pts.push(X(i).toFixed(1) + "," + Y(v).toFixed(1)); });
      if (pts.length > 1) svg("polyline", { fill: "none", stroke: m[1], "stroke-width": 1.2, points: pts.join(" ") }, el);
    });
    svg("text", { x: ml, y: H - 3, "font-size": 9, fill: "var(--text2)" }, el).textContent = k.dates[0];
    svg("text", { x: W - mr, y: H - 3, "font-size": 9, "text-anchor": "end", fill: "var(--text2)" }, el).textContent = k.dates[n - 1];
    return h("div", null, el, h("div", { class: "sm mut" }, h("span", { style: "color:#f5a623" }, "━ MA5"), "　", h("span", { style: "color:#4c93ff" }, "━ MA20"), "　近 " + n + " 个交易日 · 前复权"));
  }

  // ---------------------------------------------------------------- 持仓
  function holdView() {
    var p = S.pf, box = h("div");
    if (p.risk && p.risk.halted) box.appendChild(h("div", { class: "sec", style: "background:#fff3f0;color:#c0392b" }, "风控：账户从最高点回撤已超过 " + p.risk.halt_dd + "%，暂停买入，只能卖出。"));
    box.appendChild(h("div", { class: "sec" }, h("div", { class: "row sp" }, h("h3", null, "资产走势"), h("span", { class: "sm mut" }, "已实现盈亏 ", h("span", { class: cls(p.realized) }, S.hide ? "****" : sgn(p.realized)))), curve(p.curve)));
    var sec = h("div", { class: "sec" }, h("div", { class: "row sp" }, h("h3", null, "我的持仓（" + p.positions.length + "）"), h("span", { class: "sm mut" }, "点一行看详情")),
      h("div", { class: "th" }, h("span", null, "名称/代码"), h("span", null, "市值/数量"), h("span", null, "现价/成本"), h("span", null, "盈亏/比例")));
    if (!p.positions.length) sec.appendChild(h("div", { class: "empty" }, "还没有持仓，去「交易」页买入吧"));
    p.positions.forEach(function (r) {
      sec.appendChild(h("div", { class: "tr", onclick: function () { detail(r); } },
        h("div", { class: "n" }, r.name, h("small", null, r.code)),
        h("div", null, money(r.value), h("small", null, S.hide ? "****" : num(r.qty, 0))),
        h("div", { class: cls(r.pnl) }, num(r.last), h("small", null, S.hide ? "****" : num(r.cost))),
        h("div", { class: cls(r.pnl) }, S.hide ? "****" : sgn(r.pnl), h("small", { class: cls(r.pnl) }, pct(r.pnl_pct)))));
    });
    box.appendChild(sec);
    return box;
  }

  function sheet(inner) {
    var s = h("div", { class: "sheet", onclick: function (e) { if (e.target === s) s.remove(); } }, h("div", null, inner(function () { s.remove(); })));
    document.body.appendChild(s);
  }
  function detail(r) {
    var mine = S.pf.trades.filter(function (t) { return t.code === r.code; });
    sheet(function (close) {
      return [
        h("div", { class: "row sp" }, h("div", null, h("b", { style: "font-size:18px" }, r.name), " ", h("span", { class: "mut" }, r.code)), h("button", { class: "btn g", style: "width:auto;padding:4px 12px", onclick: close }, "关闭")),
        h("div", { class: "kv" },
          h("div", null, "持仓市值", h("b", null, money(r.value))),
          h("div", null, "持仓盈亏", h("b", { class: cls(r.pnl) }, S.hide ? "****" : sgn(r.pnl) + "（" + pct(r.pnl_pct) + "）")),
          h("div", null, "持仓数量", h("b", null, S.hide ? "****" : num(r.qty, 0))),
          h("div", null, S.market === "cn" ? "可卖数量（T+1）" : "可卖数量", h("b", null, S.hide ? "****" : num(r.sellable, 0))),
          h("div", null, "成本价", h("b", null, S.hide ? "****" : num(r.cost, 3))),
          h("div", null, "现价（最新收盘）", h("b", null, num(r.last))),
          h("div", null, "仓位占比", h("b", null, num(r.weight, 1) + "%")),
          h("div", null, "首次买入", h("b", { style: "font-size:14px" }, (r.first || "").slice(0, 16)))),
        h("div", { class: "row" },
          h("button", { class: "btn", onclick: function () { close(); gotoTrade("buy", r.code); } }, "买入"),
          h("button", { class: "btn s", onclick: function () { close(); gotoTrade("sell", r.code); } }, "卖出")),
        h("div", { class: "sm mut", style: "margin:14px 0 4px" }, "这只股票的成交记录"),
        mine.length ? mine.map(function (t) { return h("div", { class: "row sp sm", style: "padding:6px 0;border-bottom:1px solid var(--sep)" }, h("span", null, h("span", { class: "tag " + (t.side === "buy" ? "" : "s") }, t.side === "buy" ? "买" : "卖"), " ", num(t.qty, 0), " 股 @ ", num(t.price)), h("span", { class: "mut" }, t.ts.slice(5, 16))); }) : h("div", { class: "mut sm" }, "无")
      ];
    });
  }
  function gotoTrade(side, code) { S.tab = "trade"; T.side = side; T.code = code; T.sig = null; T.qty = ""; render(); loadSig(); }

  // ---------------------------------------------------------------- 交易
  var tradeBox = null, SLOT = null, KL = {};
  function posOf(code) { return (S.pf.positions || []).filter(function (p) { return p.code === code; })[0]; }
  function loadSig() {
    if (!T.code) return;
    T.busy = true; drawTrade();
    api("/api/member/signal?market=" + S.market + "&code=" + encodeURIComponent(T.code) + "&strategy=" + S.strat).then(function (s) { T.sig = s; T.busy = false; T.err = ""; drawTrade(); })
      .catch(function (e) { T.sig = null; T.busy = false; T.err = e.message; drawTrade(); });
  }
  function selectCode(code, side) { T.code = code; if (side) T.side = side; T.sig = null; T.qty = ""; loadSig(); if (WIDE.matches) markActive(); }
  function markActive() { document.querySelectorAll("[data-code]").forEach(function (e) { e.classList.toggle("act", e.getAttribute("data-code") === T.code); }); }
  function maxQty() {
    var s = T.sig; if (!s) return 0;
    var q = s.quote, lot = q.lot;
    if (T.side === "sell") { var p = posOf(q.code); return p ? p.sellable : 0; }
    return Math.floor(S.pf.cash / (q.price * (1 + q.fee_bp / 1e4)) / lot) * lot;
  }
  function tradeView() { tradeBox = h("div"); drawTrade(); return tradeBox; }
  function searchBox() {
    var list = h("div", { class: "list", style: "display:none" }), timer = null;
    var inp = h("input", { placeholder: S.market === "cn" ? "搜索代码或名称，如 600519 / 茅台" : "搜索美股代码，如 AAPL", value: WIDE.matches ? "" : T.code, autocomplete: "off" });
    function pick(c) { list.style.display = "none"; inp.value = WIDE.matches ? "" : c; if (WIDE.matches) inp.blur(); selectCode(c); }
    inp.addEventListener("input", function () {
      clearTimeout(timer); var v = inp.value.trim();
      if (!v) { list.style.display = "none"; return; }
      timer = setTimeout(function () {
        api("/api/search?market=" + S.market + "&q=" + encodeURIComponent(v)).then(function (r) {
          list.textContent = "";
          (r || []).slice(0, 8).forEach(function (x) { list.appendChild(h("div", { onclick: function () { pick(x.code); } }, h("span", null, x.name), h("span", { class: "mut" }, x.code))); });
          if (/^[0-9]{6}$/.test(v) || (S.market === "us" && /^[A-Za-z.-]{1,6}$/.test(v))) list.appendChild(h("div", { onclick: function () { pick(S.market === "us" ? v.toUpperCase() : v); } }, h("span", null, "直接用代码 " + v), h("span", { class: "mut" }, "›")));
          list.style.display = list.children.length ? "block" : "none";
        }).catch(function () {});
      }, 250);
    });
    inp.addEventListener("keydown", function (e) { if (e.key === "Enter") pick(inp.value.trim()); });
    return h("div", { class: "dd" }, inp, list);
  }
  function drawTrade() {
    var s = T.sig, wide = WIDE.matches;
    if (wide ? !SLOT : !tradeBox) return;
    var side = h("div", { class: "bs" }, h("button", { class: "b" + (T.side === "buy" ? " on" : ""), onclick: function () { T.side = "buy"; T.qty = ""; drawTrade(); } }, "买入"),
      h("button", { class: "s" + (T.side === "sell" ? " on" : ""), onclick: function () { T.side = "sell"; T.qty = ""; drawTrade(); } }, "卖出"));
    var sel = h("select", { onchange: function () { S.strat = sel.value; try { localStorage.setItem("mb-strat", S.strat); } catch (e) {} if (T.code) loadSig(); } });
    Object.keys(S.meta.strategies).filter(function (k) { return S.meta.strategies[k].kind === "single"; }).forEach(function (k) { var o = h("option", { value: k }, "策略：" + S.meta.strategies[k].label); if (k === S.strat) o.selected = true; sel.appendChild(o); });
    var status = [T.busy ? h("div", { class: "mut sm" }, "计算中…") : null, T.err ? h("div", { class: "warn" }, T.err) : null];
    var quote = null, kb = null, sig = null, ticket = [];
    if (s) {
      var q = s.quote, have = posOf(q.code), mx = maxQty(), chgEl = h("div", { class: "sm mut" }, q.day + " 收盘价");
      quote = h("div", { class: "row sp qh" }, h("div", null, h("b", { style: "font-size:" + (wide ? 22 : 17) + "px" }, q.name), " ", h("span", { class: "mut" }, q.code)), h("div", { style: "text-align:right" }, h("b", { id: "qpx", style: "font-size:" + (wide ? 26 : 20) + "px" }, num(q.price)), chgEl));
      kb = h("div", { class: "kbox" }, h("div", { class: "sm mut" }, "K 线加载中…"));
      (function (code, mkt) {
        function show(k) {
          kb.textContent = ""; kb.appendChild(kline(k, wide));
          var n = k.c.length; if (n > 1) { var ch = (k.c[n - 1] / k.c[n - 2] - 1) * 100, px = document.getElementById("qpx"); if (px) px.className = cls(ch); chgEl.className = "sm " + cls(ch); chgEl.textContent = pct(ch) + " · " + q.day + " 收盘价"; }
        }
        var key = mkt + code; if (KL[key]) return show(KL[key]);
        api("/api/member/kline?market=" + mkt + "&code=" + encodeURIComponent(code)).then(function (k) { KL[key] = k; show(k); }).catch(function () { kb.textContent = ""; });
      })(q.code, S.market);
      sig = h("div", { class: "sig" },
        h("div", null, "策略「", s.strategy, "」信号：", h("b", null, s.action), "　目标仓位 ", h("b", null, num(s.target_weight * 100, 0) + "%")),
        h("div", null, "当前持有 ", h("b", null, num(s.have, 0)), " 股，建议持有 ", h("b", null, num(s.suggest_qty, 0)), " 股 → ", h("b", { class: cls(s.delta) }, s.delta ? (s.delta > 0 ? "买入 " : "卖出 ") + num(Math.abs(s.delta), 0) + " 股" : "不动")),
        s.delta ? h("button", { class: "btn g", style: "padding:7px", onclick: function () { T.side = s.delta > 0 ? "buy" : "sell"; T.qty = String(Math.abs(s.delta)); drawTrade(); } }, "按建议填入数量") : null,
        h("div", { class: "mut sm" }, s.rule + "。信号只是参考。"));
      var qty = h("input", { type: "number", inputmode: "numeric", placeholder: "数量（股）", value: T.qty, step: String(q.lot) });
      var est = h("div", { class: "sm mut" });
      var upd = function () { T.qty = qty.value; var n = parseInt(qty.value, 10) || 0; est.textContent = n > 0 ? "预计金额 " + mk() + num(n * q.price) + "，手续费约 " + mk() + num(n * q.price * q.fee_bp / 1e4) : ""; };
      qty.addEventListener("input", upd); upd();
      ticket = [
        h("div", { class: "row sp sm mut" }, h("span", null, "成交价（最新收盘）"), h("b", { style: "color:var(--text)" }, num(q.price))),
        h("div", { class: "step" }, h("button", { onclick: function () { qty.value = String(Math.max(0, (parseInt(qty.value, 10) || 0) - q.lot)); upd(); } }, "−"), qty, h("button", { onclick: function () { qty.value = String((parseInt(qty.value, 10) || 0) + q.lot); upd(); } }, "+")),
        h("div", { class: "chips" }, [["1/4", 4], ["1/3", 3], ["1/2", 2], ["全部", 1]].map(function (c) {
          return h("button", { onclick: function () { var n = c[1] === 1 ? mx : Math.floor(mx / c[1] / q.lot) * q.lot; qty.value = String(n); upd(); } }, c[0]);
        })),
        h("div", { class: "row sp sm mut" }, h("span", null, T.side === "buy" ? "最多可买 " + num(mx, 0) + " 股" : "可卖 " + num(mx, 0) + " 股" + (S.market === "cn" ? "（T+1）" : "")), h("span", null, "可用 " + mk() + num(S.pf.cash))),
        est,
        h("button", { class: "btn" + (T.side === "sell" ? " s" : ""), onclick: function () { submit(q, have); } }, (T.side === "buy" ? "买入 " : "卖出 ") + q.name)];
    }
    if (wide) {
      SLOT.center.textContent = ""; SLOT.right.textContent = "";
      if (!s) SLOT.center.appendChild(h("div", { class: "empty" }, T.busy ? "加载中…" : (T.err || "在左侧选一只股票，或用顶部搜索")));
      else SLOT.center.appendChild(h("div", { class: "cwrap" }, quote, kb, sig, h("div", { class: "row" }, sel), status));
      SLOT.right.appendChild(h("div", { class: "tk" }, h("h3", null, "下单"), side, ticket.length ? ticket : h("div", { class: "mut sm" }, "选好股票后在这里填数量")));
      SLOT.right.appendChild(acctCard());
      markActive();
    } else {
      var box = tradeBox; box.textContent = "";
      var sec = h("div", { class: "sec", style: "display:flex;flex-direction:column;gap:12px" }, side, searchBox(), sel, status, quote, kb, sig, ticket);
      box.appendChild(sec);
    }
  }
  function submit(q, have) {
    var n = parseInt(T.qty, 10);
    if (!(n > 0)) { toast("先填数量"); return; }
    var side = T.side;
    sheet(function (close) {
      var msg = h("div", { class: "warn" });
      return [h("div", { style: "font-size:17px;font-weight:700;margin-bottom:6px" }, "确认" + (side === "buy" ? "买入" : "卖出")),
        h("div", { class: "kv" }, h("div", null, "品种", h("b", null, q.name + " " + q.code)), h("div", null, "成交价（最新收盘）", h("b", null, num(q.price))), h("div", null, "数量", h("b", null, num(n, 0) + " 股")), h("div", null, "预计金额", h("b", null, mk() + num(n * q.price)))),
        h("div", { class: "row" }, h("button", { class: "btn g", onclick: close }, "取消"),
          h("button", { class: "btn" + (side === "sell" ? " s" : ""), onclick: function () {
            api("/api/member/order", { market: S.market, code: q.code, side: side, qty: n, strategy: S.strat, signal: T.sig ? T.sig.action : "" }).then(function (r) {
              close(); toast((side === "buy" ? "买入成功 " : "卖出成功 ") + num(n, 0) + " 股，手续费 " + num(r.fee)); T.qty = ""; loadPf(function () { loadSig(); });
            }).catch(function (e) { msg.textContent = e.message; });
          } }, "确认")), msg];
    });
  }

  // ---------------------------------------------------------------- 成交
  function dealsView() {
    var p = S.pf, sec = h("div", { class: "sec" }, h("h3", null, "成交记录（最近 200 笔）"));
    if (!p.trades.length) sec.appendChild(h("div", { class: "empty" }, "还没有成交"));
    p.trades.forEach(function (t) {
      sec.appendChild(h("div", { style: "padding:10px 0;border-bottom:1px solid var(--sep)" },
        h("div", { class: "row sp" }, h("span", null, h("span", { class: "tag " + (t.side === "buy" ? "" : "s") }, t.side === "buy" ? "买" : "卖"), " ", h("b", null, t.name), " ", h("span", { class: "mut sm" }, t.code)), h("span", { class: "mut sm" }, t.ts.slice(5, 16))),
        h("div", { class: "row sp sm", style: "margin-top:4px" }, h("span", null, num(t.qty, 0) + " 股 @ " + num(t.price) + "　金额 " + num(t.qty * t.price)), t.pnl == null ? h("span", { class: "mut" }, "手续费 " + num(t.fee)) : h("span", { class: cls(t.pnl) }, "盈亏 " + sgn(t.pnl))),
        t.strategy ? h("div", { class: "sm mut", style: "margin-top:2px" }, "策略：" + ((S.meta.strategies[t.strategy] || {}).label || t.strategy) + (t.signal ? "（信号：" + t.signal + "）" : "")) : null));
    });
    return sec;
  }

  // ---------------------------------------------------------------- 我的
  var RANK = null;
  function loadRank() { api("/api/member/rank?market=" + S.market).then(function (r) { RANK = r; render(); }).catch(function () {}); }
  function riskView() {
    var p = S.pf, k = p.risk || {}, box = h("div");
    box.appendChild(h("div", { class: "sec" }, h("h3", null, "风险与绩效"),
      h("div", { class: "sm", style: "line-height:1.9" },
        "当前回撤 ", h("b", { class: cls(k.dd_pct) }, S.hide ? "****" : pct(k.dd_pct)), "（超过 -" + k.halt_dd + "% 暂停买入）", h("br"),
        "已平仓 " + (k.closed || 0) + " 笔，胜率 " + (k.win_pct == null ? "—" : k.win_pct.toFixed(0) + "%"), h("br"),
        "单只股票上限 " + k.max_weight + "% 总资产")));
    var at = p.attribution || [];
    var sec = h("div", { class: "sec" }, h("h3", null, "盈亏归因"), h("div", { class: "th" }, h("span", null, "名称"), h("span", null, "已实现"), h("span", null, "未实现"), h("span", null, "合计")));
    if (!at.length) sec.appendChild(h("div", { class: "empty" }, "还没有交易"));
    at.slice(0, 15).forEach(function (a) {
      sec.appendChild(h("div", { class: "tr" }, h("span", null, a.name || a.code), h("span", { class: cls(a.realized) }, S.hide ? "****" : sgn(a.realized)), h("span", { class: cls(a.unrealized) }, S.hide ? "****" : sgn(a.unrealized)), h("span", { class: cls(a.total) }, S.hide ? "****" : sgn(a.total))));
    });
    box.appendChild(sec);
    var lb = h("div", { class: "sec" }, h("h3", null, "收益排行榜"));
    if (!RANK) lb.appendChild(h("div", { class: "empty" }, "加载中…"));
    else if (!RANK.rows.length) lb.appendChild(h("div", { class: "empty" }, "还没有人交易"));
    else RANK.rows.forEach(function (x) { lb.appendChild(h("div", { class: "tr", style: x.me ? "font-weight:700" : "" }, h("span", null, "#" + x.rank), h("span", null, x.me ? "我" : x.name), h("span", { class: cls(x.ret) }, pct(x.ret)))); });
    lb.appendChild(h("div", { class: "sm mut" }, "共 " + (RANK ? RANK.total : 0) + " 人，按最近一次净值快照"));
    box.appendChild(lb);
    return box;
  }
  function acctActions() {
    return h("div", { class: "sec" },
      h("div", { class: "item", onclick: function () { var o = window.prompt("旧密码"); if (!o) return; var n = window.prompt("新密码（至少 8 位）"); if (!n) return; api("/api/member/password", { old: o, new: n }).then(function () { toast("密码已改，请重新登录"); setTimeout(function () { location.reload(); }, 1200); }).catch(function (e) { toast(e.message); }); } }, h("span", null, "修改密码"), h("span", { class: "mut" }, "›")),
      h("div", { class: "item", onclick: function () { sheet(function (close) { return [h("div", { style: "font-size:17px;font-weight:700" }, "重置" + (S.market === "cn" ? "A 股" : "美股") + "账户？"), h("p", { class: "mut" }, "清空这个市场的全部持仓、成交和净值记录，现金恢复到初始金额。不能撤销。"), h("div", { class: "row" }, h("button", { class: "btn g", onclick: close }, "取消"), h("button", { class: "btn", onclick: function () { api("/api/member/reset", { market: S.market, confirm: true }).then(function () { close(); toast("已重置"); loadPf(); }).catch(function (e) { toast(e.message); }); } }, "确认重置"))]; }); } }, h("span", null, "重置当前市场账户"), h("span", { class: "mut" }, "›")),
      h("div", { class: "item", onclick: function () { api("/api/member/logout", {}).then(function () { location.reload(); }); } }, h("span", null, "退出登录"), h("span", { class: "mut" }, "›")));
  }
  function meView() {
    var box = h("div");
    if (!RANK) loadRank();
    box.appendChild(h("div", { class: "sec" }, h("div", { class: "row sp" }, h("div", null, h("b", { style: "font-size:18px" }, S.user.name), h("div", { class: "sm mut" }, "模拟账户（虚拟资金）")))));
    box.appendChild(acctActions());
    box.appendChild(riskView());
    box.appendChild(h("div", { class: "sec sm mut", style: "line-height:1.7" },
      h("b", { style: "color:var(--text)" }, "交易规则"), h("br"),
      "· 成交价 = 最新收盘价（A 股为前复权真实价），不模拟盘中滑点、涨跌停和成交量限制", h("br"),
      "· A 股买入 100 股一手，当天买入的第二个交易日才能卖（T+1）；美股 1 股起", h("br"),
      "· 手续费按网站回测使用的费率；策略信号只是参考，买卖由你自己决定", h("br"),
      "· 仅用于练习，不构成投资建议。"));
    return box;
  }

  // ---------------------------------------------------------------- 流程
  var WIDE = window.matchMedia("(min-width: 980px)"), THEME = "dark", BT = "pos";
  try { THEME = localStorage.getItem("mb-theme") || "dark"; } catch (e) {}
  function applyTheme() { document.documentElement.setAttribute("data-t", WIDE.matches ? THEME : ""); }
  WIDE.addEventListener("change", function () { applyTheme(); if (S.user && S.pf) render(); });
  function table(cols, rows, empty) {
    var t = h("table", { class: "dt" }, h("thead", null, h("tr", null, cols.map(function (c) { return h("th", { class: c[2] || "" }, c[0]); }))));
    var tb = h("tbody"); t.appendChild(tb);
    rows.forEach(function (r) { tb.appendChild(h("tr", { "data-code": r.code || "", onclick: r.click }, cols.map(function (c, i) { var v = c[1](r); return h("td", { class: c[2] || "" }, v); }))); });
    if (!rows.length) tb.appendChild(h("tr", null, h("td", { colspan: cols.length, class: "mut", style: "text-align:center;padding:26px" }, empty || "暂无")));
    return h("div", { class: "dwrap" }, t);
  }
  function acctCard() {
    var p = S.pf, k = p.risk || {};
    return h("div", { class: "tk" }, h("h3", null, "账户"),
      h("div", { class: "kv2" },
        h("div", null, "总资产", h("b", null, money(p.total))), h("div", null, "累计收益", h("b", { class: cls(p.ret_pct) }, S.hide ? "****" : pct(p.ret_pct))),
        h("div", null, "持仓市值", h("b", null, money(p.market_value))), h("div", null, "可用资金", h("b", null, money(p.cash))),
        h("div", null, "当前回撤", h("b", { class: cls(k.dd_pct) }, S.hide ? "****" : pct(k.dd_pct))), h("div", null, "已平仓胜率", h("b", null, k.win_pct == null ? "—" : k.win_pct.toFixed(0) + "%"))),
      curve(p.curve),
      h("div", { class: "sm mut" }, "风控：单只 ≤ " + k.max_weight + "% 总资产；回撤超 " + k.halt_dd + "% 暂停买入"));
  }
  function bottomPanel() {
    var p = S.pf, B = [["pos", "持仓 (" + p.positions.length + ")"], ["deals", "成交"], ["attr", "盈亏归因"], ["rank", "排行榜"]];
    var tabs = h("div", { class: "btabs" }, B.map(function (b) { return h("button", { class: BT === b[0] ? "on" : "", onclick: function () { BT = b[0]; if (b[0] === "rank" && !RANK) loadRank(); render(); } }, b[1]); }));
    var body;
    var M = function (v) { return S.hide ? "****" : v; };
    if (BT === "pos") body = table([["名称", function (r) { return h("span", null, h("b", null, r.name), " ", h("span", { class: "mut" }, r.code)); }], ["数量", function (r) { return M(num(r.qty, 0)); }, "r"], ["可卖", function (r) { return M(num(r.sellable, 0)); }, "r"],
      ["成本", function (r) { return M(num(r.cost, 3)); }, "r"], ["现价", function (r) { return num(r.last); }, "r"], ["市值", function (r) { return M(num(r.value)); }, "r"],
      ["盈亏", function (r) { return h("span", { class: cls(r.pnl) }, M(sgn(r.pnl))); }, "r"], ["盈亏%", function (r) { return h("span", { class: cls(r.pnl_pct) }, pct(r.pnl_pct)); }, "r"], ["仓位", function (r) { return num(r.weight, 1) + "%"; }, "r"],
      ["", function (r) { return h("span", null, h("button", { class: "mini b", onclick: function (e) { e.stopPropagation(); selectCode(r.code, "buy"); } }, "买"), h("button", { class: "mini s", onclick: function (e) { e.stopPropagation(); selectCode(r.code, "sell"); } }, "卖")); }, "r"]],
      p.positions.map(function (r) { r.click = function () { selectCode(r.code); }; return r; }), "还没有持仓");
    else if (BT === "deals") body = table([["时间", function (t) { return t.ts.slice(5, 16); }], ["方向", function (t) { return h("span", { class: "tag " + (t.side === "buy" ? "" : "s") }, t.side === "buy" ? "买" : "卖"); }], ["名称", function (t) { return t.name + " " + t.code; }],
      ["数量", function (t) { return num(t.qty, 0); }, "r"], ["价格", function (t) { return num(t.price); }, "r"], ["金额", function (t) { return num(t.qty * t.price); }, "r"], ["手续费", function (t) { return num(t.fee); }, "r"],
      ["盈亏", function (t) { return t.pnl == null ? "—" : h("span", { class: cls(t.pnl) }, sgn(t.pnl)); }, "r"], ["策略", function (t) { return t.strategy ? ((S.meta.strategies[t.strategy] || {}).label || t.strategy) : "—"; }]], p.trades, "还没有成交");
    else if (BT === "attr") body = table([["名称", function (a) { return a.name || a.code; }], ["已实现", function (a) { return h("span", { class: cls(a.realized) }, M(sgn(a.realized))); }, "r"], ["未实现", function (a) { return h("span", { class: cls(a.unrealized) }, M(sgn(a.unrealized))); }, "r"], ["合计", function (a) { return h("b", { class: cls(a.total) }, M(sgn(a.total))); }, "r"]], p.attribution || [], "还没有交易");
    else body = table([["#", function (x) { return x.rank; }], ["会员", function (x) { return x.me ? "我" : x.name; }], ["收益率", function (x) { return h("span", { class: cls(x.ret) }, pct(x.ret)); }, "r"]], RANK ? RANK.rows : [], RANK ? "还没有人交易" : "加载中…");
    return [tabs, body];
  }
  function terminal() {
    var p = S.pf, k = p.risk || {};
    app.textContent = "";
    var top = h("div", { class: "Ttop" },
      h("a", { href: "/", class: "brand" }, "‹ 主页"), h("b", { class: "brand2" }, "模拟交易"),
      h("div", { class: "pill" }, [["cn", "A 股"], ["us", "美股"]].map(function (m) {
        return h("button", { class: S.market === m[0] ? "on" : "", onclick: function () { if (S.market !== m[0]) { S.market = m[0]; T.code = ""; T.sig = null; RANK = null; loadPf(); } } }, m[1]);
      })),
      h("div", { class: "Tsearch" }, searchBox()),
      h("div", { class: "grow" }),
      h("div", { class: "st" }, "总资产", h("b", null, mk() + money(p.total))),
      h("div", { class: "st" }, "累计收益", h("b", { class: cls(p.ret_pct) }, S.hide ? "****" : sgn(p.total - p.initial) + "（" + pct(p.ret_pct) + "）")),
      h("div", { class: "st" }, "持仓市值", h("b", null, money(p.market_value))),
      h("div", { class: "st" }, "可用资金", h("b", null, money(p.cash))),
      h("button", { class: "ib", title: "隐藏金额", onclick: function () { S.hide = !S.hide; try { localStorage.setItem("mb-hide", S.hide ? "1" : "0"); } catch (e) {} render(); } }, S.hide ? "🙈" : "👁"),
      h("button", { class: "ib", title: "明暗切换", onclick: function () { THEME = THEME === "dark" ? "light" : "dark"; try { localStorage.setItem("mb-theme", THEME); } catch (e) {} applyTheme(); render(); } }, THEME === "dark" ? "☀" : "🌙"),
      h("button", { class: "ib nm", onclick: function () { sheet(function (close) { return [h("div", { class: "row sp" }, h("b", null, S.user.name), h("button", { class: "btn g", style: "width:auto;padding:4px 12px", onclick: close }, "关闭")), acctActions()]; }); } }, "👤 " + S.user.name));
    SLOT = { left: h("div", { class: "pn Tl" }), center: h("div", { class: "pn Tc" }), right: h("div", { class: "pn Tr" }), bottom: h("div", { class: "pn Tb" }) };
    var b = BOARD[S.market];
    if (!b) { api("/api/member/board?market=" + S.market).then(function (r) { BOARD[S.market] = r; if (WIDE.matches && S.user) render(); }).catch(function () {}); SLOT.left.appendChild(h("div", { class: "empty" }, "行情加载中…")); }
    else {
      var item = function (x) {
        return h("div", { class: "wl" + (x.code === T.code ? " act" : ""), "data-code": x.code, onclick: function () { selectCode(x.code, "buy"); } },
          h("span", null, h("b", null, x.name), h("div", { class: "sm mut" }, x.code)), spark(x.spark, x.chg >= 0), h("span", { style: "text-align:right" }, h("b", { class: cls(x.chg) }, num(x.price)), h("div", { class: "sm " + cls(x.chg) }, pct(x.chg))));
      };
      SLOT.left.appendChild(h("h3", { class: "ph" }, "主要指数 / ETF")); b.indices.forEach(function (x) { SLOT.left.appendChild(item(x)); });
      SLOT.left.appendChild(h("h3", { class: "ph" }, "自选行情")); b.watch.slice().sort(function (a, c) { return c.chg - a.chg; }).forEach(function (x) { SLOT.left.appendChild(item(x)); });
      if (!T.code && !T.busy) { var f = (b.watch[0] || b.indices[0]); if (f) { T.code = f.code; setTimeout(loadSig, 0); } }
    }
    bottomPanel().forEach(function (e) { SLOT.bottom.appendChild(e); });
    app.appendChild(top);
    app.appendChild(h("div", { class: "Tgrid" }, SLOT.left, SLOT.center, SLOT.right, SLOT.bottom));
    drawTrade();
  }
  function render() {
    if (!S.user) return loginView();
    if (!S.pf) { app.textContent = ""; app.appendChild(h("div", { class: "empty", style: "padding-top:120px" }, "加载中…")); return; }
    applyTheme();
    if (WIDE.matches) return terminal();
    var v = S.tab === "mkt" ? mktView() : S.tab === "hold" ? holdView() : S.tab === "trade" ? tradeView() : S.tab === "deals" ? dealsView() : meView();
    shell(v);
  }
  function loadPf(cb) {
    api("/api/member/portfolio?market=" + S.market).then(function (p) { S.pf = p; render(); if (cb) cb(); }).catch(function (e) { if (/登录/.test(e.message)) { S.user = null; loginView(); } else toast(e.message); });
  }
  function boot() { if (!S.user) return loginView(); api("/api/meta").then(function (m) { S.meta = m; loadPf(); }); }
  api("/api/member/me").then(function (j) { S.user = j.user; boot(); }).catch(function () { loginView(); });
})();
