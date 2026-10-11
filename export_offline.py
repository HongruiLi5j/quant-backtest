"""导出离线单文件网页：把当前数据写进一个 .html，双击就能用浏览器打开，不需要联网和后端。

用法：.venv/bin/python export_offline.py
输出：export/量化日报-离线版.html（数据停在导出那天，不会自动更新）

安全：
  - 不读取 .secret/（加载 app.py 时把令牌那一行换成占位值）
  - 回测实验室用 JavaScript 版引擎在浏览器里运行（dashboard/offline_engine.js，已与 Python 逐项对拍）；不含刷新、分享、第三方引擎页（这些要后端）
  - 默认不包含个人自选个股（PRIVATE_CODES），避免把你的持仓发给别人
"""
from __future__ import annotations

import base64, gzip, json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

PRIVATE_CODES = {"600531"}            # 不放进离线版的个人自选
PERIODS = ["6m", "1y", "3y"]   # 离线版只带这几个区间，选更长的区间时显示 3 年，避免文件太大
STRATS = ["blend2"]                   # 个股页预先算好的策略（其余策略离线版会显示默认策略）


def load_app():
    """加载 app.py，但不读 .secret：把 TOKEN = get_token() 换成占位值。"""
    src = (ROOT / "app.py").read_text(encoding="utf-8")
    assert "TOKEN = get_token()" in src, "app.py 里找不到 TOKEN = get_token()，请检查"
    src = src.replace("TOKEN = get_token()", 'TOKEN = "offline-export"', 1)
    mod = type(sys)("app_export")
    mod.__file__ = str(ROOT / "app.py")
    exec(compile(src, str(ROOT / "app.py"), "exec"), mod.__dict__)
    return mod


ENGINE_FROM = "2014-01-01"       # 离线回测只带这个日期之后的行情，控制文件大小
SCAN_TOP = {"blend2": 600}       # 全市场扫描每个策略带多少只（按 Sharpe 排名），其余策略默认 300
SCAN_TOP_DEFAULT = 300
SCAN_COLS = ["code", "name", "board", "industry", "last_date", "d1", "m1", "next", "action", "cagr", "sharpe", "maxdd",
             "y1", "win", "trades", "b_cagr", "b_sharpe", "b_maxdd", "sharpe_is", "sharpe_oos", "b_sharpe_is", "b_sharpe_oos", "mcap", "mcap_rank"]


LAB_STOCKS = 100                  # 回测实验室里额外带多少只排名靠前的个股（A 股）
LAB_STOCK_FROM = "2017-01-01"     # 这些个股只带这个日期之后的行情，控制文件大小
SCALE = 10000


def _enc(df, cal_idx):
    """把一只品种的行情压成整数增量：日期 = 在交易日历里的位置的增量；价格 = 放大 10000 倍取整后的增量。"""
    import numpy as np
    pos = cal_idx.reindex(df.index).to_numpy()
    out = {"di": np.diff(pos, prepend=0).astype(int).tolist()}
    for k, col in (("o", "Open"), ("h", "High"), ("l", "Low"), ("c", "Close")):
        v = np.round(df[col].to_numpy(dtype=float) * SCALE).astype("int64")
        out[k] = np.diff(v, prepend=0).tolist()
    return out


def engine_data(app, meta):
    """离线回测需要的行情（压缩）、品种配置、涨跌停幅度、「假设与局限」文字模板。"""
    import pandas as pd
    from config import MARKETS
    from src import allmarket, analytics
    out = {"prices": {}, "cfg": {}, "limits": {}, "assump": {}, "cal": {}}
    for m in meta["markets"]:
        cfg = MARKETS[m]
        prices = app.prices_for(m)
        wl = {k: v for k, v in app.datasource.load_watchlist(m).items() if k not in PRIVATE_CODES}
        codes = list(dict.fromkeys(list(cfg["universe"]) + list(wl) + [cfg["cash"]]))
        frames, names = {}, {}
        for code in codes:
            df = prices.get(code)
            if df is None or code in PRIVATE_CODES:
                continue
            df = df.loc[ENGINE_FROM:]
            if len(df) >= 300:
                frames[code] = df
        lab = {}
        if m == "cn":                  # 额外带排名靠前的个股，让回测实验室能选个股
            sc = scan_data()
            top = [r for r in (sc["rows"].get("blend2", []) if sc else []) if r["code"] not in PRIVATE_CODES][:LAB_STOCKS * 2]
            for r in top:
                if len(lab) >= LAB_STOCKS:
                    break
                try:
                    df = allmarket.load(r["code"]).loc[LAB_STOCK_FROM:]
                except Exception:
                    continue
                if len(df) >= 300:
                    frames[r["code"]] = df
                    lab[r["code"]] = r["name"]
        cal = sorted(set().union(*[set(d.index) for d in frames.values()]))
        cal_idx = pd.Series(range(len(cal)), index=pd.DatetimeIndex(cal))
        out["cal"][m] = [int(d.strftime("%Y%m%d")) for d in cal]
        out["prices"][m], out["limits"][m] = {}, {}
        for code, df in frames.items():
            out["prices"][m][code] = _enc(df, cal_idx)
            if m == "cn":
                nm = cfg["universe"].get(code) or wl.get(code) or lab.get(code) or ""
                out["limits"][m][code] = app.datasource.limit_pct(code, nm)
        meta["markets"][m]["lab_stocks"] = lab
        out["cfg"][m] = {"cash": cfg["cash"], "main": cfg["main"], "cost_bp": cfg["cost_bp"],
                         "stock_cost_bp": cfg.get("stock_cost_bp", cfg["cost_bp"]), "universe": cfg["universe"]}
        out["assump"][m] = [analytics.assumptions(m, "lab", 0, False), analytics.assumptions(m, "lab", 0, True)]
    return out


EXTRA_STOCKS = 150                 # 离线版每个市场额外带多少只个股的详情


def _slim(o, nd=3):
    """个股详情里的数字缩短小数位，减小文件；技术指标只留 2 位。"""
    if isinstance(o, float):
        return round(o, nd) if o == o and abs(o) != float("inf") else None
    if isinstance(o, list):
        return [_slim(x, nd) for x in o]
    if isinstance(o, dict):
        return {k: _slim(v, 2 if k == "ind" else nd) for k, v in o.items()}
    return o


def scan_data(market="cn"):
    """全市场扫描：每个策略只带排名靠前的股票（全部 A 股 × 7 个策略会让文件大几十倍）。"""
    if market == "us":
        from src import scan_us as scan
    else:
        from src import scan
    df, meta = scan.load_scan()
    if df is None:
        return None
    if "delisted" in df:
        df = df[~df["delisted"]]
    df = df[~df["code"].isin(PRIVATE_CODES)]
    rows = {}
    for st, g in df.groupby("strategy"):
        g = g.sort_values("sharpe", ascending=False, na_position="last").head(SCAN_TOP.get(st, SCAN_TOP_DEFAULT))
        rows[st] = json.loads(g[[c for c in SCAN_COLS if c in g.columns]].to_json(orient="records", force_ascii=False))
    kept = df[df["code"].isin({r["code"] for v in rows.values() for r in v})]
    return {"meta": meta, "rows": rows, "boards": sorted(kept["board"].dropna().unique().tolist()),
            "industries": sorted(x for x in kept["industry"].dropna().unique().tolist() if x)}


def scrub_watch(summary: dict) -> dict:
    summary = dict(summary)
    summary["watch"] = [w for w in summary.get("watch", []) if w["symbol"] not in PRIVATE_CODES]
    return summary


def main():
    app = load_app()
    from config import MARKETS
    from src import daily_report, detail, tracking

    meta = app.meta()
    data = {"meta": meta, "summary": {}, "tracking": {}, "table": {}, "stock": {}}
    for m, mv in meta["markets"].items():
        mv["stocks"] = {k: v for k, v in mv["stocks"].items() if k not in PRIVATE_CODES}
    for m in meta["markets"]:
        s = daily_report.load(m)
        if not s:
            continue
        data["summary"][m] = scrub_watch(s)
        trk = {"eval": tracking.load_eval(m) or {"empty": True},
               "replay": tracking.load_eval(m, replay=True) or {"empty": True}, "notifications": []}
        for k in ("eval", "replay"):   # 最近信号列表里去掉个人自选
            if trk[k].get("recent"):
                trk[k]["recent"] = [x for x in trk[k]["recent"] if x["code"] not in PRIVATE_CODES]
        data["tracking"][m] = trk
        cfg = MARKETS[m]
        prices = app.prices_for(m)
        prices = {k: v for k, v in prices.items() if k not in PRIVATE_CODES}
        data["table"][m] = {}
        for name, sv in meta["strategies"].items():
            if sv["kind"] != "single" or name == "buy_hold":
                continue
            try:
                rows = daily_report.table_rows(m, cfg, prices, name)
                data["table"][m][name] = {"rows": [r for r in rows if r["symbol"] not in PRIVATE_CODES], "pending": []}
            except Exception as e:
                print("表格失败", m, name, e)
        data["stock"][m] = {}
        wl = {k: v for k, v in app.datasource.load_watchlist(m).items() if k not in PRIVATE_CODES}
        codes = list(cfg["universe"]) + list(wl)
        for code in codes:
            data["stock"][m][code] = {}
            for st in STRATS:
                for pe in PERIODS:
                    try:
                        data["stock"][m][code][f"{st}|{pe}"] = detail.build(m, code, cfg, st, pe)
                    except Exception as e:
                        print("个股失败", m, code, st, pe, type(e).__name__, e)
    meta["strategies"] = {k: v for k, v in meta["strategies"].items() if v["kind"] == "single"}   # 离线版不含多品种轮动
    data.update(engine_data(app, meta))
    data["scan"] = scan_data()
    data["scan_us"] = scan_data("us")
    # 个股详情（近 1 年 K 线 + 趋势线族所需数据）：每个市场带排名靠前的 EXTRA_STOCKS 只，gzip 压缩后单独放一块，点进个股时才解压
    extra = {}
    for m, sc in (("cn", data["scan"]), ("us", data["scan_us"])):
        extra[m] = {}
        if not sc:
            continue
        for r in sc["rows"].get("blend2", []):
            if len(extra[m]) >= EXTRA_STOCKS:
                break
            c = r["code"]
            if c in PRIVATE_CODES or c in data["stock"].get(m, {}):
                continue
            try:
                extra[m][c] = _slim(detail.build(m, c, MARKETS[m], "blend2", "1y"))
            except Exception as e:
                print("扩展个股失败", m, c, e)
    xb64 = base64.b64encode(gzip.compress(json.dumps(extra, ensure_ascii=False, default=float, separators=(",", ":")).encode("utf-8"), 9)).decode()
    print("扩展个股", {m: len(v) for m, v in extra.items()}, "压缩后", len(xb64) // 1024, "KB")
    blob = json.dumps(data, ensure_ascii=False, default=float, separators=(",", ":"))
    blob = blob.replace("</", "<\\/")

    shim = """
const __D = %s;
let __xp = null;
function __X() {
  if (!__xp) __xp = (async () => {
    try {
      const b = Uint8Array.from(atob(document.getElementById("xdata").textContent.trim()), (c) => c.charCodeAt(0));
      return JSON.parse(await new Response(new Blob([b]).stream().pipeThrough(new DecompressionStream("gzip"))).text());
    } catch (e) { return null; }
  })();
  return __xp;
}
const __json = (x) => ({ status: 200, ok: true, json: async () => x });

function __scan(q) {
  const S = q.get("market") === "us" ? __D.scan_us : __D.scan; if (!S) return { empty: true, downloaded: 0, listed: null };
  const st = S.rows[q.get("strategy")] ? q.get("strategy") : "blend2";
  let d = S.rows[st].slice();
  const t = (q.get("q") || "").trim().slice(0, 20), bd = q.get("board") || "", ind = q.get("industry") || "", sg = q.get("signal") || "";
  if (t) d = d.filter((r) => String(r.code).includes(t) || String(r.name).includes(t));
  if (bd) d = d.filter((r) => r.board === bd);
  if (ind) d = d.filter((r) => r.industry === ind);
  const latest = S.meta ? S.meta.data_date : null;
  if (["买入", "卖出", "加仓", "减仓"].includes(sg)) d = d.filter((r) => r.action === sg && r.last_date === latest);
  else if (sg === "持仓") d = d.filter((r) => r.next > 0);
  else if (sg === "空仓") d = d.filter((r) => !(r.next > 0));
  else if (sg === "有效") d = d.filter((r) => r.sharpe > r.b_sharpe);
  else if (sg === "退市") d = [];
  let key = q.get("sort") || "sharpe"; if (q.get("strategy") === "raw" && !["code", "d1", "m1", "y1", "mcap"].includes(key)) key = d.some((r) => r.mcap) ? "mcap" : "d1";
  const asc = q.get("dir") === "asc";
  d.sort((a, b) => { const x = a[key], y = b[key], nx = x === null || x === undefined, ny = y === null || y === undefined; if (nx || ny) return nx && ny ? 0 : nx ? 1 : -1; return (x < y ? -1 : x > y ? 1 : 0) * (asc ? 1 : -1); });
  const total = d.length; let page = Math.max(0, parseInt(q.get("page") || "0") || 0), size = 50;
  if (q.get("export") === "1") { size = Math.max(total, 1); page = 0; }
  return { rows: d.slice(page * size, (page + 1) * size), total, page, size, meta: S.meta, boards: S.boards, industries: S.industries };
}
window.fetch = async (path) => {
  const u = new URL(path, location.href), p = u.pathname.replace(/^.*\\/api\\//, "/api/"), q = u.searchParams, m = q.get("market");
  if (p === "/api/meta") return __json(__D.meta);
  if (p === "/api/summary") return __json(__D.summary[m] || { error: "离线版没有这个市场的数据" });
  if (p === "/api/tracking") return __json(__D.tracking[m] || { eval: { empty: true }, replay: { empty: true }, notifications: [] });
  if (p === "/api/table") { const t = (__D.table[m] || {})[q.get("strategy")] || (__D.table[m] || {}).blend2; return __json(t || { error: "离线版没有这个策略的表格" }); }
  if (p === "/api/stock") {
    const c = (__D.stock[m] || {})[q.get("code")];
    if (!c) {
      const x = await __X(), e = x && x[m] && x[m][q.get("code")];
      return __json(e || { error: "离线版只包含主要品种、自选和扫描排名靠前的个股" });
    }
    const st = q.get("strategy") || "blend2", pe = q.get("period");
    return __json(c[st + "|" + pe] || c["blend2|" + pe] || c["blend2|3y"] || c["blend2|1y"] || { error: "离线版没有这段数据" });
  }
  if (p === "/api/search") {
    const k = (q.get("q") || "").toLowerCase(), mv = __D.meta.markets[m] || { universe: {}, stocks: {} };
    const sc = m === "us" ? __D.scan_us : __D.scan, seen = {}, ex = [];
    if (sc) for (const r of (sc.rows.blend2 || [])) if (!seen[r.code]) { seen[r.code] = 1; ex.push({ code: r.code, name: r.name, kind: "个股" }); }
    const all = ex.concat(Object.entries(mv.universe).map(([c, n]) => ({ code: c, name: n, kind: "ETF" })).concat(Object.entries(mv.stocks).map(([c, n]) => ({ code: c, name: n, kind: "个股" }))));
    return __json(all.filter((x) => x.code.toLowerCase().includes(k) || x.name.toLowerCase().includes(k)).slice(0, 8));
  }
  if (p === "/api/backtest") {
    try { return __json(OE.run(__D, m, q.get("strategy"), q.get("symbol") || __D.cfg[m].main, JSON.parse(q.get("params") || "{}"), q.get("cost") ? Number(q.get("cost")) : null, q.get("start") || null)); }
    catch (e) { return __json({ error: String(e.message || e) }); }
  }
  if (p === "/api/scan") return __json(__scan(q));
  if (p === "/api/status") return __json({ running: false, log: [] });
  if (p === "/api/watchlist") return __json({ stocks: (__D.meta.markets[m] || {}).stocks || {}, pending: [] });
  return __json({ error: "离线版不支持这个功能" });
};
""" % blob

    html = (ROOT / "dashboard" / "index.html").read_text(encoding="utf-8")
    appjs = (ROOT / "dashboard" / "app.js").read_text(encoding="utf-8")
    i18n = (ROOT / "dashboard" / "i18n.js").read_text(encoding="utf-8")
    analysis = (ROOT / "dashboard" / "analysis.js").read_text(encoding="utf-8")
    engine = (ROOT / "dashboard" / "offline_engine.js").read_text(encoding="utf-8")
    esc_js = lambda t: t.replace("</script", "<\\/script")
    html = re.sub(r'<script src="/app\.js[^"]*"></script>\s*<script src="/analysis\.js[^"]*"></script>\s*<script src="/engines\.js[^"]*"></script>\s*<script src="/i18n\.js[^"]*"></script>',
                  lambda _: "<script>" + esc_js(engine) + "</script>\n<script>" + shim + "</script>\n<script>" + esc_js(appjs) + "</script>\n<script>" + esc_js(analysis) + "</script>\n<script>" + esc_js(i18n) + "</script>", html, count=1)
    assert "__D" in html, "没有替换成功：index.html 里的 script 标签和预期不一样"
    html = html.replace("如 600531", "如 600000")
    date = max(v.get("data_date", "") for v in data["summary"].values())
    css = ("<style>a[data-p='bt'],a[data-p='rq'],a[data-p='cmp'],a[data-p='code'],"
           "#refresh,#shareBar,#shareBtn{display:none!important}.offline-bar{background:#fff7e0;color:#6b4e00;padding:8px 16px;"
           "font-size:13px;text-align:center}html[lang='en'] .offline-bar .zh,html:not([lang='en']) .offline-bar .en{display:none}</style>")
    bar = (f'<div class="offline-bar"><span class="zh">离线版：数据截至 {date}，不会自动更新。回测实验室可以正常使用；全市场扫描只含每个策略排名靠前的股票。</span>'
           f'<span class="en">Offline copy: data as of {date}; it does not update automatically. The Backtest Lab works; Market scan lists only the top-ranked stocks per strategy.</span></div>')
    html = html.replace("</body>", '<script type="text/plain" id="xdata">' + xb64 + "</script></body>", 1)
    html = html.replace("</head>", css + "</head>", 1).replace("<body>", "<body>" + bar, 1)
    out = ROOT / "export"
    out.mkdir(exist_ok=True)
    target = out / "量化日报-离线版.html"
    target.write_text(html, encoding="utf-8")
    print(f"已生成 {target}（{target.stat().st_size / 1024 / 1024:.1f} MB），数据截至 {date}")


if __name__ == "__main__":
    main()
