"""本地网页服务：仪表盘 + 在线换策略回测。

  python app.py          # 启动（定时任务安装后会开机自动启动，一般不用手动跑）
  ./scripts/open_dashboard.sh   # 用带令牌的地址打开网页

安全设计（别人不能访问）：
  1. 只监听 127.0.0.1：只有这台 Mac 自己能连，同一 Wi-Fi 的其他设备连不上
  2. 访问令牌：第一次用 ?t=<令牌> 打开，之后靠 HttpOnly + SameSite=Strict 的 cookie；
     令牌存在 .secret/token，权限 600（只有你的账户能读）
  3. 检查 Host 头，防止 DNS rebinding（恶意网页借你的浏览器访问本机服务）
  4. 只开放固定的几个接口，不提供任何文件浏览；所有参数白名单 + 范围校验
  5. 不保存任何券商账号密码；数据源都是公开行情，不需要登录
  6. 只看行情、只回测，不连交易接口，不会下单
"""
from __future__ import annotations

import hmac
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import warnings
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

warnings.filterwarnings("ignore")

import pandas as pd

from config import ENABLED, MARKETS, SERVER_HOST, SERVER_PORT
from src import admin, allmarket, members, sharepass, daily_report, datasource, detail, notify, registry, scan, scan_us, tracking, usdata
from engines import codefiles, jobs
import tunnel

ROOT = Path(__file__).resolve().parent
SECRET = ROOT / ".secret" / "token"
DASH = ROOT / "dashboard"
COOKIE = "qb_session"


def get_token() -> str:
    SECRET.parent.mkdir(mode=0o700, exist_ok=True)
    os.chmod(SECRET.parent, 0o700)
    if not SECRET.exists():
        fd = os.open(SECRET, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(32))
    os.chmod(SECRET, 0o600)
    return SECRET.read_text().strip()


TOKEN = get_token()
ADMIN_TOKEN = admin.get_admin_token()      # 后台 /admin 用的独立令牌（.secret/admin_token）
ADMIN_COOKIE = "qb_admin"
ALLOWED_HOSTS = {f"127.0.0.1:{SERVER_PORT}", f"localhost:{SERVER_PORT}"}

# ---- 行情缓存：文件没变就不重复读
_price_cache: dict = {}
_lock = threading.Lock()
_offline_lock = threading.Lock()


def prices_for(market: str) -> dict:
    cfg = MARKETS[market]
    syms = list(cfg["universe"]) + list(datasource.load_watchlist(market))
    key = tuple((s, datasource.cache_path(market, s).stat().st_mtime)
                for s in syms if datasource.cache_path(market, s).exists())
    with _lock:
        hit = _price_cache.get(market)
        if hit and hit[0] == key:
            return hit[1]
        p = datasource.load_market(market, cfg)
        _price_cache[market] = (key, p)
        return p


# ---- 全市场扫描结果：文件没变就不重复读
_scan_cache: dict = {}


def scan_data(market: str = "cn"):
    mod = scan_us if market == "us" else scan
    p = mod.OUT
    if not p.exists():
        return None, None
    mt = p.stat().st_mtime
    hit = _scan_cache.get(market)
    if hit and hit[0] == mt:
        return hit[1], hit[2]
    df, meta = mod.load_scan()
    _scan_cache[market] = (mt, df, meta)
    return df, meta


SCAN_SORT = {"code", "d1", "m1", "next", "cagr", "sharpe", "maxdd", "y1", "win", "trades",
             "b_cagr", "b_sharpe", "b_maxdd", "b_y1", "sharpe_is", "sharpe_oos", "mcap"}


def scan_query(q: dict) -> dict:
    market = "us" if q.get("market") == "us" else "cn"
    df, meta = scan_data(market)
    if df is None and market == "us":
        return {"empty": True, "downloaded": len(usdata.codes()), "listed": None, "market": "us"}
    if df is None:
        n_all = len(allmarket.cached_codes())
        try:
            listed = len(allmarket.stock_list())
        except Exception:
            listed = None
        return {"empty": True, "downloaded": n_all, "listed": listed}
    strat = q.get("strategy", "blend2")
    raw = strat == "raw"          # 原本数据：只看行情，不带回测结果
    if raw:
        strat = "blend2"
    elif strat not in scan.STRATS:
        strat = "blend2"
    d = df[df["strategy"] == strat]
    if "delisted" in d:
        d = d[d["delisted"]] if q.get("signal") == "退市" else d[~d["delisted"]]
    text = q.get("q", "").strip()[:20]
    if text:
        d = d[d["code"].str.contains(text, regex=False) | d["name"].str.contains(text, regex=False)]
    board = q.get("board", "")
    if board:
        d = d[d["board"] == board]
    ind = q.get("industry", "")
    if ind:
        d = d[d["industry"] == ind]
    sig = q.get("signal", "")
    latest = meta["data_date"] if meta else None
    if sig in ("买入", "卖出", "加仓", "减仓"):
        d = d[(d["action"] == sig) & (d["last_date"] == latest)]
    elif sig == "持仓":
        d = d[d["next"] > 0]
    elif sig == "空仓":
        d = d[d["next"] <= 0]
    elif sig == "有效" and not raw:
        d = d[d["sharpe"] > d["b_sharpe"]]
    key = q.get("sort", "sharpe")
    if raw and key not in ("code", "d1", "m1", "y1", "mcap"):
        key = "mcap" if "mcap" in d.columns else "d1"
    key = key if key in SCAN_SORT else "sharpe"
    asc = q.get("dir", "desc") == "asc"
    d = d.sort_values(key, ascending=asc, na_position="last")
    total = len(d)
    try:
        page = max(0, int(q.get("page", 0)))
    except ValueError:
        page = 0
    size = 50
    if q.get("export") == "1":      # 导出：当前筛选条件下的全部行
        size, page = max(total, 1), 0
    part = d.iloc[page * size:(page + 1) * size]
    cols = ["code", "name", "board", "industry", "last_date", "d1", "m1", "next", "action",
            "cagr", "sharpe", "maxdd", "y1", "win", "trades", "b_cagr", "b_sharpe", "b_maxdd",
            "sharpe_is", "sharpe_oos", "b_sharpe_is", "b_sharpe_oos", "mcap", "mcap_rank"]
    part = part[[c for c in cols if c in part.columns]]
    rows = json.loads(part.to_json(orient="records", force_ascii=False))
    return {"rows": rows, "total": total, "page": page, "size": size, "meta": meta,
            "boards": sorted(df["board"].dropna().unique().tolist()),
            "industries": sorted(x for x in df["industry"].dropna().unique().tolist() if x)}


# ---- 搜索：代码或名称
_list_cache: dict = {}


def search(market: str, text: str) -> list:
    text = (text or "").strip()[:20]
    if market not in MARKETS or not text:
        return []
    cfg = MARKETS[market]
    out = [{"code": k, "name": v, "kind": "ETF"} for k, v in cfg["universe"].items()
           if text.upper() in k.upper() or text in v]
    out += [{"code": k, "name": v or k, "kind": "个股"} for k, v in datasource.load_watchlist(market).items()
            if text.upper() in k.upper() or text.lower() in (v or "").lower()]
    if market == "us" and len(text) >= 1:
        u = usdata.universe()
        if len(u):
            hit = u[u["yahoo"].str.upper().str.startswith(text.upper()) | u["name"].str.lower().str.contains(text.lower(), regex=False)].head(12)
            have = {x["code"] for x in out}
            out += [{"code": r.yahoo, "name": r.name or r.yahoo, "kind": usdata.EXCHANGE.get(r.exchange, "个股")} for r in hit.itertuples() if r.yahoo not in have and usdata.has(r.yahoo)]
    if market == "cn":
        p = allmarket.LIST_FILE
        if p.exists():
            mt = p.stat().st_mtime
            if _list_cache.get("mt") != mt:
                _list_cache.update(mt=mt, df=pd.read_csv(p, dtype=str).fillna(""))
            df = _list_cache["df"]
            hit = df[df["code"].str.startswith(text) | df["name"].str.contains(text, regex=False)].head(15)
            out += [{"code": r.code, "name": r.name, "kind": r.board} for r in hit.itertuples()]
    return out[:15]


# ---- 手动刷新（后台跑 daily.py）
_refresh = {"proc": None, "log_pos": 0, "market": None}


def start_refresh(market: str | None):
    p = _refresh["proc"]
    if p and p.poll() is None:
        return False
    args = [sys.executable, str(ROOT / "daily.py")]
    if market:
        args += ["--market", market]
    log_path = ROOT / "logs" / "daily.log"
    _refresh.update(log_pos=log_path.stat().st_size if log_path.exists() else 0, market=market)
    _refresh["proc"] = subprocess.Popen(args, cwd=ROOT, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL)
    return True


def refresh_status() -> dict:
    p = _refresh["proc"]
    running = bool(p and p.poll() is None)
    log = ROOT / "logs" / "daily.log"
    lines = []
    if log.exists():
        with log.open("rb") as f:
            f.seek(min(_refresh.get("log_pos", 0), log.stat().st_size))
            lines = f.read().decode("utf-8", errors="replace").splitlines()
    tail = lines[-8:]
    msg = tail[-1].split("INFO ", 1)[-1] if tail else "正在启动更新…"
    # 使用真实下载/扫描计数作为进度；阶段间显示当前阶段，不伪造精确百分比。
    percent, phase = (None if running else 100), ("启动中" if running else "已完成")
    latest = lines[-1] if lines else ""
    if "汇总完成" in latest:
        percent, phase = (None if running else 100), "正在收尾" if running else "已完成"
    elif "全市场回测：" in latest:
        percent, phase = None, "全市场回测完成"
    elif "正在进行 A 股全市场回测" in latest or "全市场回测中" in latest:
        percent, phase = None, "全市场回测中"
    elif "每日缺口修复进度" in latest or "回补进度" in latest:
        import re
        progress_lines = [line for line in lines if "每日缺口修复进度" in line or "回补进度" in line]
        match = re.findall(r"(?:每日缺口修复|回补)进度 (\d+)/(\d+)", progress_lines[-1]) if progress_lines else []
        if match and int(match[-1][1]):
            done, total = map(int, match[-1])
            percent, phase = int(100 * done / total), f"全市场数据 {done:,}/{total:,}"
        else:
            phase = "正在补齐全市场数据"
    elif "全市场数据更新" in latest:
        percent, phase = None, "正在整理全市场数据"
    elif lines:
        updates = sum(1 for x in lines if " 行，最新 " in x)
        markets = [_refresh["market"]] if _refresh.get("market") else list(ENABLED)
        targets = sum(len(MARKETS[m]["universe"]) + len(datasource.load_watchlist(m)) for m in markets)
        percent, phase = min(100, int(100 * updates / max(targets, 1))), f"行情下载 {min(updates, targets)}/{targets}"
    if not running and p and p.returncode:
        percent = None
        phase, msg = "更新失败", (tail[-1] if tail else f"进程退出码 {p.returncode}")
    return {"running": running, "log": tail, "percent": percent, "phase": phase, "message": msg,
            "returncode": p.returncode if p and not running else None}


# ---- 自选股：后台下载新加的股票
_pending: set = set()


def download_stock(market: str, code: str):
    """后台下载一只个股的历史数据，并补上股票名称。"""
    def work():
        try:
            cfg = MARKETS[market]
            if market == "cn":
                datasource.update_stock(market, code, cfg)
            else:
                datasource.update(market, code, cfg)
            with _lock:
                known = datasource.load_watchlist(market).get(code)
            name = known or (datasource.stock_name(code) if market == "cn" else code)
            with _lock:
                wl = datasource.load_watchlist(market)
                if code in wl and name and not wl[code]:
                    wl[code] = name
                    datasource.save_watchlist(market, wl)
        finally:
            _pending.discard(code)
    _pending.add(code)
    threading.Thread(target=work, daemon=True).start()


def add_stock(market: str, code: str):
    with _lock:
        wl = datasource.load_watchlist(market)
        if code in wl:
            return "已经在自选股里了"
        if len(wl) >= 50:
            return "自选股最多 50 只"
        wl[code] = ""
        datasource.save_watchlist(market, wl)
    download_stock(market, code)
    return None


def remove_stock(market: str, code: str):
    with _lock:
        wl = datasource.load_watchlist(market)
        wl.pop(code, None)
        datasource.save_watchlist(market, wl)


def meta() -> dict:
    return {
        "markets": {k: {"name": v["name"], "main": v["main"], "universe": v["universe"],
                        "stocks": datasource.load_watchlist(k),
                        "timezone": v["tz"],
                        "timezone_label": "北京时间" if k == "cn" else "美东时间",
                        "stock_cost_bp": v.get("stock_cost_bp", v["cost_bp"]),
                        "cost_bp": v["cost_bp"], "source": v["source"]}
                    for k, v in ((k, MARKETS[k]) for k in ENABLED)},
        "default_market": ENABLED[0],
        "strategies": {k: {kk: v[kk] for kk in ("label", "desc", "kind", "params")}
                       for k, v in registry.STRATEGIES.items()},
    }


def _ua_info(ua: str) -> str:
    osn = next((n for k, n in (("Windows NT 10", "Windows"), ("Windows", "Windows"), ("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"), ("Mac OS X", "Mac"), ("Linux", "Linux")) if k in ua), "未知系统")
    br = next((n for k, n in (("Edg/", "Edge"), ("MicroMessenger", "微信"), ("QQBrowser", "QQ浏览器"), ("Quark", "夸克"), ("UCBrowser", "UC"), ("Firefox/", "Firefox"), ("Chrome/", "Chrome"), ("Safari/", "Safari")) if k in ua), "未知浏览器")
    return f"{osn} · {br}"


def share_visits() -> dict:
    p = ROOT / "data" / "share_visits.jsonl"
    rows = []
    if p.exists():
        for ln in p.read_text(encoding="utf-8").splitlines()[-2000:]:
            try:
                rows.append(json.loads(ln))
            except ValueError:
                pass
    g: dict = {}
    for r in rows:
        k = (r["ip"], r["ua"])
        e = g.setdefault(k, {"ip": r["ip"], "country": r["country"], "device": _ua_info(r["ua"]), "lang": r.get("lang", ""), "first": r["t"], "last": r["t"], "n": 0})
        e["last"], e["n"] = r["t"], e["n"] + 1
    now = time.time()
    for k, e in g.items():
        e["online"] = now - _alive.get((k[0], k[1][:300]), 0) < 40
    return {"total": len(rows), "online": sum(1 for e in g.values() if e["online"]),
            "visitors": sorted(g.values(), key=lambda e: (not e["online"], e["last"]), reverse=False)[:20]}


_bad_login: list = []
_alive: dict = {}


class Handler(BaseHTTPRequestHandler):
    server_version = "local"
    sys_version = ""

    def log_message(self, fmt, *args):  # 不在终端刷屏
        pass

    # ---------- 工具
    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                         "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False, default=float).encode(), "application/json; charset=utf-8")

    def _authed(self) -> bool:
        c = SimpleCookie(self.headers.get("Cookie", ""))
        v = c.get(COOKIE)
        return bool(v) and hmac.compare_digest(v.value, TOKEN)

    # ---------- 分享链接的访问记录（只记录 HTTP 请求本来就带的信息：时间、网络地址、国家、浏览器和系统）
    def _remote(self) -> bool:
        return self.headers.get("Host", "").endswith(".trycloudflare.com")

    def _log_visit(self, path: str):
        ua = self.headers.get("User-Agent", "")
        rec = {"t": time.strftime("%Y-%m-%d %H:%M:%S"), "ip": (self.headers.get("CF-Connecting-IP") or self.headers.get("X-Forwarded-For") or "").split(",")[0].strip(),
               "country": self.headers.get("CF-IPCountry", ""), "lang": self.headers.get("Accept-Language", "")[:20], "ua": ua[:300], "path": path[:60]}
        try:
            with open(ROOT / "data" / "share_visits.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _guard(self) -> bool:
        host = self.headers.get("Host", "")
        # 临时分享用的 Cloudflare Tunnel 网址（*.trycloudflare.com）也放行：
        # 令牌校验（_authed）没跳过，光知道这个随机网址进不来
        if host not in ALLOWED_HOSTS and not host.endswith(".trycloudflare.com"):
            self._send(403, b"forbidden host", "text/plain")
            return False
        return True

    # ---------- 后台 /admin：只给本机、独立令牌、不走分享链接（判断逻辑都在 src/admin.py）
    def _admin_authed(self) -> bool:
        c = SimpleCookie(self.headers.get("Cookie", ""))
        v = c.get(ADMIN_COOKIE)
        return bool(v) and admin.check_session(v.value, ADMIN_TOKEN)

    def _admin_get(self, u, q):
        if not admin.is_local_request(self.headers, ALLOWED_HOSTS):     # 分享链接 / 隧道来的请求：装作没有这个页面
            admin.audit_blocked(u.path)
            return self._send(404, b"not found", "text/plain")
        if u.path == "/admin" and "t" in q:                              # 用管理令牌登录
            if admin.login_blocked():
                return self._send(429, "尝试太多次，请 10 分钟后再试".encode(), "text/plain; charset=utf-8")
            if hmac.compare_digest(q["t"], ADMIN_TOKEN):
                admin.audit("login")
                return self._send(303, b"", "text/plain", {
                    "Location": "/admin",
                    "Set-Cookie": f"{ADMIN_COOKIE}={admin.make_session(ADMIN_TOKEN)}; HttpOnly; SameSite=Strict; "
                                  f"Path=/; Max-Age={admin.SESSION_SECONDS}"})
            admin.record_bad_login()
            admin.audit("login", ok=False, error="令牌不对")
            return self._send(401, "令牌不对".encode(), "text/plain; charset=utf-8")
        if not self._admin_authed():
            return self._send(401, "未授权：请在终端运行 bash scripts/open_admin.sh 打开后台".encode(),
                              "text/plain; charset=utf-8")
        try:
            if u.path == "/admin":
                self._send(200, (DASH / "admin.html").read_bytes(), "text/html; charset=utf-8")
            elif u.path == "/admin.js":
                self._send(200, (DASH / "admin.js").read_bytes(), "text/javascript; charset=utf-8")
            elif u.path == "/api/admin/overview":
                self._json(admin.overview(ADMIN_CTX))
            elif u.path == "/api/admin/data":
                self._json(admin.data_overview(ADMIN_CTX))
            elif u.path == "/api/admin/log":
                try:
                    n = int(q.get("n", 200))
                except ValueError:
                    n = 200
                self._json(admin.log_tail(q.get("name", ""), n))
            elif u.path == "/api/admin/audit":
                self._json({"items": admin.audit_tail(100)})
            elif u.path == "/api/admin/members":
                return self._json({"members": members.list_users(), "feed": members.activity(None, 150)})
            elif u.path == "/api/admin/member":
                uid = int(q.get("id", "0") or 0)
                info = members.user_detail(uid)
                if not info:
                    return self._json({"error": "没有这个会员"}, 404)
                pf = {m: members.portfolio(uid, m, _member_last_price(m), time.strftime("%Y-%m-%d")) for m in members.INITIAL_CASH}
                return self._json({"user": info, "portfolio": pf, "activity": members.activity(uid, 400)})
            elif u.path == "/api/admin/visits":
                self._json(share_visits())
            elif u.path == "/api/admin/backup/download":
                f = admin.backup_path(q.get("f", ""))
                if not f:
                    return self._json({"error": "没有这个备份"}, 404)
                admin.audit("backup_download", {"file": f.name})
                self._send(200, f.read_bytes(), "application/zip",
                           {"Content-Disposition": f'attachment; filename="{f.name}"'})
            else:
                self._send(404, b"not found", "text/plain")
        except admin.AdminError as e:
            self._json({"error": str(e)}, 400)
        except Exception as e:
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def _admin_post(self, u):
        if not admin.is_local_request(self.headers, ALLOWED_HOSTS):
            admin.audit_blocked(u.path)
            return self._send(404, b"not found", "text/plain")
        if (not self._admin_authed() or self.headers.get("X-Requested-With") != "dashboard"
                or not admin.origin_ok(self.headers, ALLOWED_HOSTS)):
            return self._send(401, b"unauthorized", "text/plain")
        if u.path != "/api/admin/action":
            return self._send(404, b"not found", "text/plain")
        try:
            body = self._body()
            action = str(body.pop("action", ""))
            self._json(admin.dispatch(action, body, ADMIN_CTX))
        except admin.AdminError as e:
            self._json({"error": str(e)}, 400)
        except Exception as e:
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    # ---------- 路由
    def do_GET(self):
        if not self._guard():
            return
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path in ("/admin", "/admin.js") or u.path.startswith("/api/admin/"):
            return self._admin_get(u, q)
        if self._share_gate(u):
            return
        if self._remote():              # 通过分享链接来的访客
            _alive[(self.headers.get("CF-Connecting-IP") or "", self.headers.get("User-Agent", "")[:300])] = time.time()   # 页面每 15 秒会自动请求一次，用来判断“现在在看”：只读，不能碰设置、代码、分享和下载离线版
            if u.path == "/":
                self._log_visit("/")
            if u.path.startswith(("/api/code/", "/api/share", "/api/offline", "/api/eng/scan")):
                return self._json({"error": "分享链接里不能用这个功能（只有你自己的电脑上可以）"}, 403)
        if u.path == "/" and "t" in q and len([t for t in _bad_login if t > time.time() - 600]) >= 10:   # 10 分钟内错 10 次就暂停登录，防止有人猜令牌
            return self._send(429, "尝试太多次，请 10 分钟后再试".encode(), "text/plain; charset=utf-8")
        if u.path == "/" and "t" in q:          # 用令牌登录，设置 cookie 后去掉地址栏里的令牌
            if hmac.compare_digest(q["t"], TOKEN):
                self._send(303, b"", "text/plain", {
                    "Location": "/",
                    "Set-Cookie": f"{COOKIE}={TOKEN}; HttpOnly; SameSite=Strict; Path=/; Max-Age=31536000"})
            else:
                _bad_login.append(time.time())
                self._send(401, "令牌不对".encode(), "text/plain; charset=utf-8")
            return
        if not self._authed():
            self._send(401, "未授权：请在终端运行 scripts/open_dashboard.sh 打开网页".encode(),
                       "text/plain; charset=utf-8")
            return

        if u.path in ("/member", "/member.js") or u.path.startswith("/api/member/"):
            return self._member_get(u, q)
        try:
            if u.path == "/":
                self._send(200, (DASH / "index.html").read_bytes(), "text/html; charset=utf-8")
            elif u.path == "/terminal.css":
                self._send(200, (DASH / "terminal.css").read_bytes(), "text/css; charset=utf-8")
            elif u.path in ("/app.js", "/engines.js", "/i18n.js", "/analysis.js", "/skin.js"):
                self._send(200, (DASH / u.path[1:]).read_bytes(), "text/javascript; charset=utf-8")
            elif u.path == "/api/meta":
                self._json(meta())
            elif u.path == "/api/summary":
                m = q.get("market", "")
                if m not in MARKETS:
                    return self._json({"error": "未知市场"}, 400)
                s = daily_report.load(m)
                self._json(s or {"error": "还没有数据：点「立即更新」或等定时任务运行"})
            elif u.path == "/api/backtest":
                self._json(self._backtest(q))
            elif u.path == "/api/status":
                self._json(refresh_status())
            elif u.path == "/api/stock":
                m = q.get("market", "")
                code = q.get("code", "")
                valid_stock = (datasource.is_valid_stock(code) if m == "cn" else
                               (code in datasource.load_watchlist(m) or (m == "us" and usdata.has(code))) if m in MARKETS else False)
                if m not in MARKETS or not (code in MARKETS[m]["universe"] or valid_stock):
                    return self._json({"error": "代码不对"}, 400)
                self._json(detail.build(m, code, MARKETS[m], q.get("strategy", "blend2"), q.get("period", "1y")))
            elif u.path == "/api/offline":
                # 下载离线版单文件：现场运行 export_offline.py 生成，5 分钟内重复点击直接用刚生成的文件
                if self.headers.get("X-Requested-With") != "dashboard":
                    return self._json({"error": "缺少请求头"}, 400)
                out = ROOT / "export" / "量化日报-离线版.html"
                with _offline_lock:
                    fresh = out.exists() and (time.time() - out.stat().st_mtime) < 300
                    if not fresh:
                        r = subprocess.run([sys.executable, str(ROOT / "export_offline.py")], cwd=str(ROOT),
                                           capture_output=True, text=True, timeout=170)
                        if r.returncode != 0 or not out.exists():
                            return self._json({"error": "生成离线版失败：" + (r.stderr or r.stdout)[-300:]}, 500)
                    body = out.read_bytes()
                self._send(200, body, "text/html; charset=utf-8", {
                    "Content-Disposition": "attachment; filename=\"quant-dashboard-offline.html\"; "
                                           "filename*=UTF-8''%E9%87%8F%E5%8C%96%E6%97%A5%E6%8A%A5-%E7%A6%BB%E7%BA%BF%E7%89%88.html"})
            elif u.path == "/api/search":
                self._json(search(q.get("market", ""), q.get("q", "")))
            elif u.path == "/api/tracking":
                m = q.get("market", "")
                if m not in MARKETS:
                    return self._json({"error": "未知市场"}, 400)
                self._json({"eval": tracking.load_eval(m) or {"empty": True},
                            "replay": tracking.load_eval(m, replay=True) or {"empty": True},
                            "notifications": notify.recent(20)})
            elif u.path == "/api/scan":
                self._json(scan_query(q))
            elif u.path == "/api/table":
                self._json(self._table(q))
            elif u.path == "/api/watchlist":
                m = q.get("market", "cn")
                if m not in MARKETS:
                    return self._json({"error": "未知市场"}, 400)
                self._json({"stocks": datasource.load_watchlist(m), "pending": sorted(_pending)})
            # ---- 第三方引擎（backtrader / RQAlpha）、对比、代码
            elif u.path == "/api/eng/meta":
                self._json(eng_meta())
            elif u.path == "/api/eng/job":
                self._json(jobs.get(q.get("id", "")))
            elif u.path == "/api/eng/scan":
                from engines import scan_all
                self._json(scan_all.status())
            elif u.path == "/api/share":
                self._json(tunnel.status())
            elif u.path == "/api/share_visits":
                self._json(share_visits())
            elif u.path == "/api/code/list":
                self._json({"files": codefiles.listing()})
            elif u.path == "/api/code/file":
                self._json(codefiles.read(q.get("f", "")))
            elif u.path == "/api/code/history":
                self._json({"versions": codefiles.history(q.get("f", ""))})
            elif u.path == "/api/code/version":
                self._json({"content": codefiles.version(q.get("f", ""), q.get("v", ""))})
            else:
                self._send(404, b"not found", "text/plain")
        except Exception as e:
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def do_POST(self):
        if not self._guard():
            return
        if urlparse(self.path).path.startswith("/api/admin/"):
            return self._admin_post(urlparse(self.path))
        if urlparse(self.path).path == "/__share_login":
            return self._share_login()
        if self._share_gate(urlparse(self.path)):
            return
        if not self._authed() or self.headers.get("X-Requested-With") != "dashboard":
            return self._send(401, b"unauthorized", "text/plain")
        u = urlparse(self.path)
        if u.path.startswith("/api/member/"):
            return self._member_post(u)
        # 通过分享链接来的访客：只能跑回测/引擎对比（只算数，结果在临时任务目录里），不能改自选、刷新数据、改代码、动分享
        if self._remote() and u.path != "/api/eng/job":
            return self._json({"error": "分享链接里不能改动网站内容（自选、刷新、代码），只能查看和运行回测"}, 403)
        if u.path == "/api/watchlist":
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            market = q.get("market", "")
            code = q.get("code", "")
            if market not in MARKETS:
                return self._json({"error": "未知市场"}, 400)
            if market == "us":
                code = code.upper()
                valid = bool(__import__("re").fullmatch(r"[A-Z][A-Z0-9.-]{0,9}", code))
            else:
                valid = datasource.is_valid_stock(code)
            if not valid:
                hint = "请输入美股代码，例如 AAPL" if market == "us" else "请输入 6 位沪深 A 股代码（60/68/00/30 开头）"
                return self._json({"error": hint}, 400)
            if q.get("action") == "add":
                err = add_stock(market, code)
                return self._json({"error": err} if err else {"ok": True})
            if q.get("action") == "remove":
                remove_stock(market, code)
                return self._json({"ok": True})
            return self._json({"error": "未知操作"}, 400)
        if u.path.startswith("/api/eng/") or u.path.startswith("/api/code/") or u.path == "/api/share":
            try:
                return self._json(self._post_engine(u))
            except Exception as e:
                return self._json({"error": f"{e}"}, 400)
        if u.path == "/api/refresh":
            m = parse_qs(u.query).get("market", [None])[0]
            if m is not None and m not in MARKETS:
                return self._json({"error": "未知市场"}, 400)
            self._json({"started": start_refresh(m)})
        else:
            self._send(404, b"not found", "text/plain")

    # ---------- 分享链接密码：只拦通过外网分享链接来的访客（本机不受影响）
    def _external(self) -> bool:
        """不是从本机直接访问的（分享链接域名，或者带代理/隧道头）。"""
        return self._remote() or not admin.is_local_request(self.headers, ALLOWED_HOSTS)

    def _share_gate(self, u) -> bool:
        """返回 True 表示已经拦住并回复了（没通过密码）。"""
        if not self._external() or not sharepass.is_set():
            return False
        v = SimpleCookie(self.headers.get("Cookie", "")).get("qb_share")
        if sharepass.check_cookie(v.value if v else None):
            return False
        if u.path == "/share_gate.js":
            self._send(200, (DASH / "share_gate.js").read_bytes(), "text/javascript; charset=utf-8")
        elif u.path.startswith("/api/") or self.command == "POST":
            self._json({"error": "需要访问密码"}, 401)
        else:
            self._send(401, (DASH / "share_gate.html").read_bytes(), "text/html; charset=utf-8")
        return True

    def _share_login(self):
        origin = self.headers.get("Origin")
        if not self._external() or (origin and urlparse(origin).netloc != self.headers.get("Host", "")):
            return self._send(404, b"not found", "text/plain")
        if members.hit("sharepw", self._client_ip(), 5, 600):
            return self._json({"error": "尝试太多次，请 10 分钟后再试"}, 429)
        try:
            pw = str(self._body().get("password", ""))
        except Exception:
            return self._json({"error": "请求不对"}, 400)
        if not sharepass.is_set() or not sharepass.verify(pw):
            return self._json({"error": "密码不对"}, 401)
        self._send(200, b'{"ok": true}', "application/json", {
            "Set-Cookie": f"qb_share={sharepass.make_cookie()}; HttpOnly; SameSite=Strict; Path=/; Max-Age={sharepass.COOKIE_SECONDS}; Secure"})

    # ---------- 会员 / 模拟交易（已经通过了网站令牌的人才能到这里）
    def _client_ip(self) -> str:
        return (self.headers.get("CF-Connecting-IP") or self.client_address[0] or "").split(",")[0].strip()

    def _mlog(self, user, action: str, detail: str = "", name: str = ""):
        members.log(user["id"] if user else None, user["name"] if user else name, self._client_ip(),
                    self.headers.get("CF-IPCountry", ""), self.headers.get("User-Agent", ""), action, detail)

    def _member_user(self):
        v = SimpleCookie(self.headers.get("Cookie", "")).get(MEMBER_COOKIE)
        return members.user_for(v.value if v else None)

    def _member_get(self, u, q):
        try:
            if u.path == "/member":
                return self._send(200, (DASH / "member.html").read_bytes(), "text/html; charset=utf-8")
            if u.path == "/member.js":
                return self._send(200, (DASH / "member.js").read_bytes(), "text/javascript; charset=utf-8")
            user = self._member_user()
            if u.path == "/api/member/me":
                return self._json({"user": user})
            if not user:
                return self._json({"error": "请先登录"}, 401)
            m = q.get("market", "cn")
            if m not in members.INITIAL_CASH:
                return self._json({"error": "未知市场"}, 400)
            if u.path == "/api/member/portfolio":
                today = time.strftime("%Y-%m-%d")
                return self._json(members.portfolio(user["id"], m, _member_last_price(m), today))
            if u.path == "/api/member/board":
                return self._json(member_board(m))
            if u.path == "/api/member/kline":
                return self._json(member_kline(m, q.get("code", "").strip()))
            if u.path == "/api/member/rank":
                return self._json(members.leaderboard(m, user["id"]))
            if u.path == "/api/member/signal":
                r = member_signal(user, m, q.get("code", "").strip(), q.get("strategy", "blend2"))
                self._mlog(user, "signal", f"{m} {r['quote']['code']} {r['quote']['name']} 策略={r['strategy']} 信号={r['action']} 目标仓位={r['target_weight']:.0%}")
                return self._json(r)
            self._send(404, b"not found", "text/plain")
        except (members.MemberError, ValueError) as e:
            self._json({"error": str(e)}, 400)
        except Exception as e:
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def _member_post(self, u):
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).netloc != self.headers.get("Host", ""):
            return self._json({"error": "来源不对"}, 403)
        ip = self._client_ip()
        secure = "; Secure" if self._remote() else ""
        try:
            b = self._body()
            if u.path == "/api/member/register":
                if members.hit("reg", ip, 5, 3600):
                    return self._json({"error": "注册太频繁，请一小时后再试"}, 429)
                user = members.register(str(b.get("name", "")), str(b.get("password", "")))
                who, tok = members.login(user["name"], str(b.get("password", "")))
                self._mlog(who, "register", "注册并登录")
                return self._send(200, json.dumps({"user": who}, ensure_ascii=False).encode(), "application/json; charset=utf-8", {
                    "Set-Cookie": f"{MEMBER_COOKIE}={tok}; HttpOnly; SameSite=Strict; Path=/; Max-Age={members.SESSION_SECONDS}{secure}"})
            if u.path == "/api/member/login":
                if members.hit("login", ip, 10, 600):
                    return self._json({"error": "尝试太多次，请 10 分钟后再试"}, 429)
                try:
                    who, tok = members.login(str(b.get("name", "")), str(b.get("password", "")))
                except members.MemberError as e:
                    self._mlog(None, "login_fail", str(e), name=str(b.get("name", "")))
                    raise
                self._mlog(who, "login", "登录成功")
                return self._send(200, json.dumps({"user": who}, ensure_ascii=False).encode(), "application/json; charset=utf-8", {
                    "Set-Cookie": f"{MEMBER_COOKIE}={tok}; HttpOnly; SameSite=Strict; Path=/; Max-Age={members.SESSION_SECONDS}{secure}"})
            if u.path == "/api/member/logout":
                v = SimpleCookie(self.headers.get("Cookie", "")).get(MEMBER_COOKIE)
                self._mlog(self._member_user(), "logout", "退出登录")
                members.logout(v.value if v else None)
                return self._send(200, b'{"ok": true}', "application/json", {"Set-Cookie": f"{MEMBER_COOKIE}=; HttpOnly; Path=/; Max-Age=0{secure}"})
            user = self._member_user()
            if not user:
                return self._json({"error": "请先登录"}, 401)
            if u.path == "/api/member/password":
                try:
                    members.change_password(user["id"], str(b.get("old", "")), str(b.get("new", "")))
                except members.MemberError as e:
                    self._mlog(user, "password_fail", str(e))
                    raise
                self._mlog(user, "password", "修改了密码")
                return self._json({"ok": True, "note": "密码已改，其他设备需要重新登录；这台设备请重新登录"})
            m = str(b.get("market", ""))
            if u.path == "/api/member/reset":
                if b.get("confirm") is not True:
                    return self._json({"error": "需要确认"}, 400)
                members.reset_account(user["id"], m)
                self._mlog(user, "reset", f"重置了 {m} 账户")
                return self._json({"ok": True})
            if u.path == "/api/member/order":
                code = str(b.get("code", "")).strip()
                q = member_quote(m, code)
                q["equity"] = members.portfolio(user["id"], m, _member_last_price(m), q["day"])["total"]
                r = members.order(user["id"], m, q["code"], str(b.get("side", "")), b.get("qty"), q,
                                  str(b.get("strategy", "")), str(b.get("signal", "")))
                self._mlog(user, "order", f"{'买入' if b.get('side') == 'buy' else '卖出'} {m} {q['code']} {q['name']} {r['qty']}股 @{r['price']:.3f} 手续费{r['fee']} 策略={b.get('strategy') or '无'} 当时信号={b.get('signal') or '无'}")
                return self._json(r | {"name": q["name"], "day": q["day"]})
            self._send(404, b"not found", "text/plain")
        except (members.MemberError, ValueError) as e:
            self._json({"error": str(e)}, 400)
        except Exception as e:
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        if n > 1_000_000:
            raise ValueError("请求太大")
        obj = json.loads(self.rfile.read(n).decode("utf-8"))
        return obj if isinstance(obj, dict) else {}

    def _post_engine(self, u) -> dict:
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/api/share":
            if q.get("action") == "start":
                return tunnel.start(SERVER_PORT, TOKEN)
            if q.get("action") == "stop":
                return {"stopped": tunnel.stop()}
            raise ValueError("未知操作")
        if u.path == "/api/eng/job":
            return {"id": jobs.start(self._body())}
        if u.path == "/api/eng/scan":
            if q.get("action") == "start":
                return {"started": jobs.scan_start()}
            if q.get("action") == "stop":
                return {"stopped": jobs.scan_stop()}
            raise ValueError("未知操作")
        if u.path == "/api/code/new":
            b = self._body()
            return codefiles.create(str(b.get("name", "")))
        if u.path == "/api/code/save":
            b = self._body()
            return codefiles.save(str(b.get("f", "")), b.get("content", ""))
        if u.path == "/api/code/restore":
            text = codefiles.version(q.get("f", ""), q.get("v", ""))
            return codefiles.save(q.get("f", ""), text)
        raise ValueError("未知接口")

    def _backtest(self, q: dict) -> dict:
        m = q.get("market", "")
        name = q.get("strategy", "")
        if m not in MARKETS or name not in registry.STRATEGIES:
            return {"error": "参数不对"}
        cfg = MARKETS[m]
        sym = q.get("symbol", cfg["main"])
        allowed = set(cfg["universe"]) | set(datasource.load_watchlist(m))
        prices = prices_for(m)
        if sym not in allowed:
            # 全市场里的任意一只股票（已下载的）也可以回测
            if m == "cn" and datasource.is_valid_stock(sym) and allmarket.path(sym).exists():
                prices = dict(prices)
                prices[sym] = allmarket.load(sym)
            elif m == "us" and usdata.has(sym):
                prices = dict(prices)
                prices[sym] = usdata.load(sym)
            else:
                return {"error": "品种不在列表里"}
        if sym not in prices:
            return {"error": f"{sym} 还没有数据"}
        try:
            params = json.loads(q.get("params", "{}"))
            if not isinstance(params, dict):
                params = {}
        except json.JSONDecodeError:
            params = {}
        try:
            cost = min(max(float(q["cost"]), 0.0), 100.0) if q.get("cost") else None
        except ValueError:
            cost = None   # 用默认成本（个股 8bp，ETF 5bp）
        start = q.get("start") or None
        if start:
            try:
                start = str(pd.Timestamp(start).date())
            except Exception:
                start = None
        return registry.run(name, prices, sym, params, cfg, cost_bp=cost, start=start, market=m, detail=True)

    def _table(self, q: dict) -> dict:
        m, name = q.get("market", ""), q.get("strategy", "")
        if m not in MARKETS or name not in registry.STRATEGIES:
            return {"error": "参数不对"}
        start = q.get("start") or None
        if start:
            try:
                start = str(pd.Timestamp(start).date())
            except Exception:
                start = None
        try:
            rows = daily_report.table_rows(m, MARKETS[m], prices_for(m), name, start)
        except ValueError as e:
            return {"error": str(e)}
        return {"rows": rows, "pending": sorted(_pending)}


def eng_meta() -> dict:
    """第三方引擎页面需要的信息：7 个策略及参数、引擎库是否装好、默认股票。"""
    single = ["ma_adx_original", "ma_adx_plus", "trend_vol", "trend_ens", "donchian_s1", "rsi2", "blend2"]
    wl = datasource.load_watchlist("cn")
    return {
        "strategies": [{"key": k, "label": registry.STRATEGIES[k]["label"], "desc": registry.STRATEGIES[k]["desc"],
                        "params": registry.STRATEGIES[k]["params"]} for k in single],
        "lib_ok": (jobs.LIB / "backtrader").exists() and (jobs.LIB / "rqalpha").exists(),
        "watchlist": wl, "default_code": next(iter(wl), "600531"),
        "engines": {"inhouse": "自研引擎", "backtrader": "backtrader（国外）", "rqalpha": "RQAlpha 米筐（国内）"},
    }


# ---- 后台 /admin 要用的回调：把 app.py 里的功能交给 src/admin.py 调用
def watch_change(action: str, market: str, code: str):
    """自选股增删（和 /api/watchlist 同样的代码校验），返回错误信息或 None。"""
    import re
    if market not in MARKETS:
        return "未知市场"
    if market == "us":
        code = code.upper()
        valid = bool(re.fullmatch(r"[A-Z][A-Z0-9.-]{0,9}", code))
    else:
        valid = datasource.is_valid_stock(code)
    if not valid:
        return "请输入美股代码，例如 AAPL" if market == "us" else "请输入 6 位沪深 A 股代码（60/68/00/30 开头）"
    if action == "add":
        return add_stock(market, code)
    remove_stock(market, code)
    return None


def admin_update_stock(market: str, code: str):
    """立即更新一只自选股（增量补齐最新行情）。"""
    if market not in MARKETS:
        return "未知市场"
    if code not in datasource.load_watchlist(market):
        return "只能更新自选股里的股票"
    if code in _pending:
        return "这只股票正在下载"
    download_stock(market, code)
    return None


def admin_app_status() -> dict:
    from engines import scan_all
    share = {k: v for k, v in tunnel.status().items() if k != "url"} | {"password_set": sharepass.is_set()}      # url 里带着主页面令牌，后台不显示；raw_url 是不带令牌的网址
    return {"refresh": refresh_status(), "share": share, "scan": scan_all.status(),
            "pending": sorted(_pending)}


def admin_share_start() -> dict:
    return tunnel.start(SERVER_PORT, TOKEN)


def admin_rotate_main_token() -> dict:
    """换掉主页面令牌：旧令牌和旧 cookie 立刻失效；分享链接里带着旧令牌，所以先停掉分享。"""
    global TOKEN
    shared = bool(tunnel.status().get("running"))
    if shared:
        tunnel.stop()
    TOKEN = admin.rotate_main_token()
    _bad_login.clear()
    note = "主令牌已更换，旧的登录会话已失效。请在终端运行 bash scripts/open_dashboard.sh 重新打开主页面。"
    return {"stopped_share": shared, "note": note + ("分享链接已停止，需要的话请重新开启。" if shared else "")}


def admin_rotate_admin_token():
    global ADMIN_TOKEN
    ADMIN_TOKEN = admin.rotate_admin_token()


ADMIN_CTX = {
    "app_status": admin_app_status,
    "watchlists": lambda: {m: datasource.load_watchlist(m) for m in ENABLED},
    "pending": lambda: sorted(_pending),
    "start_refresh": start_refresh,
    "scan_start": jobs.scan_start,
    "scan_stop": jobs.scan_stop,
    "watch_change": watch_change,
    "update_stock": admin_update_stock,
    "share_start": admin_share_start,
    "share_stop": tunnel.stop,
    "rotate_main_token": admin_rotate_main_token,
    "rotate_admin_token": admin_rotate_admin_token,
}


# ---------------------------------------------------------------- 会员 / 模拟交易
MEMBER_COOKIE = "qb_member"
STOCK_MAX_WEIGHT = 0.20      # 建议仓位：单只最多占账户总资产的 20%（策略信号 1.0 = 20%）


def member_quote(market: str, code: str) -> dict:
    """最新收盘价（前复权显示价，A 股与行情软件一致）、名称、日期、手续费率。拿不到就抛 ValueError。"""
    from src import display_price
    if market not in MARKETS:
        raise ValueError("未知市场")
    mcfg = MARKETS[market]
    if market == "us":
        code = code.upper()
    df, info = detail.lookup(market, code, mcfg)
    if df is None or len(df) < 1:
        raise ValueError(f"{code} 没有数据")
    df, _k, note = display_price.apply(market, code, df)
    if market == "cn" and "未取到" in note:
        raise ValueError("暂时取不到真实股价，稍后再试")
    fee = mcfg.get("stock_cost_bp", mcfg["cost_bp"]) if info["kind"] == "个股" else mcfg["cost_bp"]
    return {"code": code, "price": float(df["Close"].iloc[-1]), "name": info["name"], "kind": info["kind"],
            "day": df.index[-1].strftime("%Y-%m-%d"), "fee_bp": fee, "lot": 100 if market == "cn" else 1}


_BOARD: dict = {}


def _board_item(market: str, code: str, name: str = ""):
    from src import display_price
    df, info = detail.lookup(market, code, MARKETS[market])
    if df is None or len(df) < 3:
        return None
    df, _k, note = display_price.apply(market, code, df)
    if market == "cn" and "未取到" in note:
        return None
    c = df["Close"].astype(float)
    return {"code": code, "name": info["name"] or name, "price": float(c.iloc[-1]), "chg": float(c.iloc[-1] / c.iloc[-2] - 1) * 100,
            "day": df.index[-1].strftime("%Y-%m-%d"), "spark": [round(float(x), 4) for x in c.iloc[-40:]]}


def member_board(market: str) -> dict:
    """行情页：主要指数/ETF + 自选股，每个带最新价、涨跌幅、近 40 日走势。缓存 60 秒。"""
    hit = _BOARD.get(market)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    mcfg = MARKETS[market]
    idx, wl = [], []
    for code, nm in list(mcfg["universe"].items())[:8]:
        try:
            it = _board_item(market, code, nm)
            if it:
                idx.append(it)
        except Exception:
            pass
    for code, nm in list(datasource.load_watchlist(market).items())[:40]:
        try:
            it = _board_item(market, code, nm)
            if it:
                wl.append(it)
        except Exception:
            pass
    out = {"market": market, "indices": idx, "watch": wl}
    _BOARD[market] = (time.time(), out)
    return out


def member_kline(market: str, code: str, n: int = 100) -> dict:
    from src import display_price
    code = code.upper() if market == "us" else code
    df, info = detail.lookup(market, code, MARKETS[market])
    if df is None or len(df) < 20:
        raise ValueError(f"{code} 没有数据")
    df, _k, _n = display_price.apply(market, code, df)
    ma5, ma20 = df["Close"].rolling(5).mean(), df["Close"].rolling(20).mean()
    t = df.iloc[-n:]
    f = lambda x: [None if pd.isna(v) else round(float(v), 4) for v in x]
    return {"code": code, "name": info["name"], "dates": [d.strftime("%m-%d") for d in t.index],
            "o": f(t["Open"]), "h": f(t["High"]), "l": f(t["Low"]), "c": f(t["Close"]), "ma5": f(ma5.iloc[-n:]), "ma20": f(ma20.iloc[-n:])}


def _member_last_price(market: str):
    def f(code):
        try:
            q = member_quote(market, code)
            return q["price"], q["day"]
        except Exception:
            return None
    return f


def member_signal(user: dict, market: str, code: str, strategy: str) -> dict:
    q = member_quote(market, code)
    d = detail.build(market, q["code"], MARKETS[market], strategy, "6m")
    if d.get("error"):
        raise ValueError(d["error"])
    pf = members.portfolio(user["id"], market, _member_last_price(market), q["day"])
    mine = next((p for p in pf["positions"] if p["code"] == q["code"]), None)
    have = mine["qty"] if mine else 0
    w = float(d["next"])
    want = pf["total"] * STOCK_MAX_WEIGHT * w / q["price"]
    lot = q["lot"]
    want = int(want // lot) * lot
    return {"quote": q, "strategy": d["strategy"], "target_weight": w, "action": d["action"],
            "holding_signal": d["position"][-1] if d.get("position") else None,
            "have": have, "suggest_qty": want, "delta": want - have, "cash": pf["cash"], "total": pf["total"],
            "rule": f"建议仓位 = 策略目标仓位 × 账户总资产的 {int(STOCK_MAX_WEIGHT * 100)}%（按 {lot} 股取整）"}


def _watched_files() -> list[Path]:
    return [ROOT / "app.py", ROOT / "config.py", ROOT / "engines" / "jobs.py", ROOT / "engines" / "codefiles.py",
            *sorted((ROOT / "src").glob("*.py"))]


def auto_reload(interval: float = 5.0):
    """代码文件（app.py、config.py、src/*.py）一改，网页服务就自己退出；
    launchd 的 KeepAlive 会马上用新代码重新启动它，所以改完代码不用手动重启。
    退出前先检查语法，改到一半、有语法错误时不重启，免得服务起不来。
    （engines/ 里的回测引擎代码每次都在子进程里重新加载，不需要重启。）"""
    def snap():
        return {p: p.stat().st_mtime for p in _watched_files() if p.exists()}

    def loop():
        import time
        old = snap()
        while True:
            time.sleep(interval)
            new = snap()
            if new == old:
                continue
            try:
                for p in new:
                    compile(p.read_text(encoding="utf-8"), str(p), "exec")
            except SyntaxError:
                old = new            # 有语法错误：先不重启，等下一次修改
                continue
            time.sleep(1.5)          # 等可能同时在写的其他文件写完
            os._exit(0)
    if os.environ.get("QB_NO_RELOAD") != "1":
        threading.Thread(target=loop, daemon=True).start()


def main():
    srv = ThreadingHTTPServer((SERVER_HOST, SERVER_PORT), Handler)
    auto_reload()
    jobs.on_server_start()      # 没跑完的全市场三引擎扫描自动继续
    # 自选股里还没下载过数据的（比如默认的 600531），启动时顺便下载
    for market in ENABLED:
        wl = datasource.load_watchlist(market)
        datasource.save_watchlist(market, wl)
        for code in wl:
            if not datasource.cache_path(market, code).exists():
                download_stock(market, code)
    print(f"仪表盘已启动：http://{SERVER_HOST}:{SERVER_PORT}/?t=<令牌>   （用 scripts/open_dashboard.sh 打开）")
    srv.serve_forever()


if __name__ == "__main__":
    main()
