"""后台管理（/admin）的逻辑层：不碰 HTTP，方便单独测试。

安全约定（app.py 的路由负责执行，判断函数都在这里）：
  1. 只接受本机请求：Host 必须是 127.0.0.1 / localhost，并且不能带隧道或代理头。
     Cloudflare 分享链接转进来的请求一定带 CF-Connecting-IP 之类的头，所以分享链接进不了后台。
  2. 独立的管理令牌 .secret/admin_token（权限 600），和主页面令牌不是同一个。
  3. 会话 8 小时过期：cookie = 时间戳.HMAC(管理令牌, 时间戳)，过期或被改过都不认。
  4. 危险操作（重启、轮换令牌、清空访客记录、开分享）必须带 confirm=true。
  5. 每个后台操作写一行审计日志 data/admin_audit.jsonl（不记录任何令牌）。
  6. 页面上不显示任何令牌；备份文件里不含 .secret。

这个文件在 import 时只用标准库（pandas 等只在用到时才导入），所以不装 pandas 也能跑 tests/test_admin.py。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path
from urllib.parse import urlparse

SESSION_SECONDS = 8 * 3600
BOOT_TIME = time.time()
LABELS = ["com.quantdaily.update", "com.quantdaily.server"]

# 隧道 / 反向代理一定会带的请求头：出现任何一个，就说明请求不是直接从本机浏览器来的
PROXY_HEADERS = {"cf-connecting-ip", "cf-ray", "cf-visitor", "cf-ipcountry", "x-forwarded-for",
                 "x-forwarded-host", "x-forwarded-proto", "x-real-ip", "true-client-ip", "forwarded", "via"}

# 要二次确认的操作：重启、换令牌、清记录、把网站开到外网
CONFIRM_ACTIONS = {"restart_server", "rotate_main_token", "rotate_admin_token", "clear_visits", "share_start", "member_delete"}

# 后台可以启动的后台任务（白名单，参数写死，不接受网页传来的命令）
TASKS = {
    "rebacktest_cn": {"label": "重新回测 A 股（不联网）", "argv": ["daily.py", "--market", "cn", "--offline"]},
    "rebacktest_us": {"label": "重新回测美股（不联网）", "argv": ["daily.py", "--market", "us", "--offline"]},
    "backfill": {"label": "补下载缺失的全市场历史（可中止，下次续传）", "argv": ["backfill.py"], "caffeinate": True},
}


class AdminError(ValueError):
    """给用户看的错误信息（会原样显示在页面上）。"""


# ---------------------------------------------------------------- 路径（测试时可以用 set_root 换成临时目录）
def set_root(root: Path):
    global ROOT, SECRET_MAIN, SECRET_ADMIN, DATA, DAILY, LOGS, EXPORT, AUDIT, GAP_FILE
    ROOT = Path(root)
    SECRET_MAIN = ROOT / ".secret" / "token"
    SECRET_ADMIN = ROOT / ".secret" / "admin_token"
    DATA = ROOT / "data"
    DAILY = DATA / "daily"
    LOGS = ROOT / "logs"
    EXPORT = ROOT / "export"
    AUDIT = DATA / "admin_audit.jsonl"
    GAP_FILE = DAILY / "admin_gapcheck.json"


set_root(Path(__file__).resolve().parent.parent)


# ---------------------------------------------------------------- 令牌与会话
def _write_secret(path: Path) -> str:
    """生成新的随机令牌，原子写入，权限 600。"""
    path.parent.mkdir(mode=0o700, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tok = secrets.token_urlsafe(32)
    tmp = path.with_suffix(".new")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(tok)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return tok


def get_admin_token() -> str:
    if not SECRET_ADMIN.exists():
        _write_secret(SECRET_ADMIN)
    os.chmod(SECRET_ADMIN, 0o600)
    return SECRET_ADMIN.read_text().strip()


def rotate_admin_token() -> str:
    return _write_secret(SECRET_ADMIN)


def rotate_main_token() -> str:
    return _write_secret(SECRET_MAIN)


def _sig(token: str, ts: str) -> str:
    return hmac.new(token.encode(), ts.encode(), hashlib.sha256).hexdigest()


def make_session(token: str, now: float | None = None) -> str:
    ts = str(int(now if now is not None else time.time()))
    return f"{ts}.{_sig(token, ts)}"


def check_session(value: str, token: str, now: float | None = None, max_age: int = SESSION_SECONDS) -> bool:
    try:
        ts_s, sig = value.split(".", 1)
        ts = int(ts_s)
    except (ValueError, AttributeError):
        return False
    now = time.time() if now is None else now
    if ts > now + 60 or now - ts > max_age:      # 来自"未来"或已过期
        return False
    return hmac.compare_digest(sig, _sig(token, ts_s))


_bad_logins: list[float] = []


def login_blocked(now: float | None = None) -> bool:
    """10 分钟内输错 5 次令牌，就暂停登录 10 分钟（比主页面更严）。"""
    now = time.time() if now is None else now
    return len([t for t in _bad_logins if t > now - 600]) >= 5


def record_bad_login(now: float | None = None):
    now = time.time() if now is None else now
    _bad_logins.append(now)
    del _bad_logins[:-50]


def failed_logins_10min() -> int:
    return len([t for t in _bad_logins if t > time.time() - 600])


# ---------------------------------------------------------------- 请求判断
def is_local_request(headers, allowed_hosts) -> bool:
    """Host 必须是本机地址，并且没有任何隧道 / 代理头。"""
    host = headers.get("Host") or ""
    if host not in allowed_hosts:
        return False
    names = {k.lower() for k in headers.keys()}
    return not (names & PROXY_HEADERS)


def origin_ok(headers, allowed_hosts) -> bool:
    """写操作额外检查 Origin：浏览器发 POST 时一定带，来源不是本机页面就拒绝（防跨站请求）。"""
    origin = headers.get("Origin")
    if not origin:
        return True
    u = urlparse(origin)
    return u.scheme == "http" and u.netloc in allowed_hosts


# ---------------------------------------------------------------- 审计日志
_last_blocked = 0.0


def audit(action: str, detail: dict | None = None, ok: bool = True, error: str | None = None):
    rec = {"t": time.strftime("%Y-%m-%d %H:%M:%S"), "action": action, "ok": ok}
    if detail:
        rec["detail"] = {k: (str(v)[:200]) for k, v in detail.items()}
    if error:
        rec["error"] = error[:300]
    try:
        AUDIT.parent.mkdir(parents=True, exist_ok=True)
        with open(AUDIT, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        os.chmod(AUDIT, 0o600)
    except OSError:
        pass


def audit_blocked(path: str):
    """外网来的请求打到后台：记录一下，但每分钟最多一条，免得被刷屏。"""
    global _last_blocked
    if time.time() - _last_blocked > 60:
        _last_blocked = time.time()
        audit("blocked_remote", {"path": path[:60]}, ok=False)


def audit_tail(n: int = 100) -> list[dict]:
    out = []
    for ln in reversed(tail_lines(AUDIT, n)):
        try:
            out.append(json.loads(ln))
        except ValueError:
            pass
    return out


# ---------------------------------------------------------------- 读文件的小工具
def read_json(p: Path, default=None):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return default


def tail_lines(path: Path, n: int, max_bytes: int = 400_000) -> list[str]:
    try:
        size = Path(path).stat().st_size
        with open(path, "rb") as f:
            f.seek(max(0, size - max_bytes))
            data = f.read()
    except OSError:
        return []
    lines = data.decode("utf-8", "replace").splitlines()
    return [ln[:2000] for ln in lines[-n:]]


def _mtime(p: Path):
    try:
        return int(Path(p).stat().st_mtime)
    except OSError:
        return None


def _du(path: Path) -> int:
    total, stack = 0, [str(path)]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        pass
        except OSError:
            pass
    return total


# ---------------------------------------------------------------- 日志
def log_files() -> dict:
    d = {"daily": LOGS / "daily.log", "update": LOGS / "launchd_update.log", "server": LOGS / "launchd_server.log",
         "tunnel": DAILY / "share" / "tunnel.log", "scan": DAILY / "engine_scan" / "stderr.log",
         "audit": AUDIT}
    for k in TASKS:
        d["task_" + k] = LOGS / f"admin_task_{k}.log"
    return d


def log_tail(name: str, n: int = 200) -> dict:
    files = log_files()
    if name not in files:
        raise AdminError("未知日志")
    n = max(10, min(int(n), 1000))
    p = files[name]
    return {"name": name, "file": str(p.relative_to(ROOT)), "size": p.stat().st_size if p.exists() else 0,
            "mtime": _mtime(p), "lines": tail_lines(p, n)}


# ---------------------------------------------------------------- 系统监控
def launchd_state() -> list[dict]:
    if sys.platform != "darwin":
        return [{"label": lb, "available": False} for lb in LABELS]
    out = []
    for label in LABELS:
        try:
            r = subprocess.run(["launchctl", "list", label], capture_output=True, text=True, timeout=6)
        except Exception:
            out.append({"label": label, "available": False})
            continue
        if r.returncode != 0:
            out.append({"label": label, "available": True, "loaded": False})
            continue
        pid = re.search(r'"PID"\s*=\s*(\d+);', r.stdout)
        last = re.search(r'"LastExitStatus"\s*=\s*(-?\d+);', r.stdout)
        out.append({"label": label, "available": True, "loaded": True,
                    "pid": int(pid.group(1)) if pid else None,
                    "last_exit": int(last.group(1)) if last else None})
    return out


def daily_running() -> bool:
    """daily.py 运行时会锁住 logs/.daily.lock：试着拿一下锁，拿不到就是正在跑。"""
    p = LOGS / ".daily.lock"
    if not p.exists():
        return False
    try:
        import fcntl
        with open(p, "a") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(f, fcntl.LOCK_UN)
    except Exception:
        pass
    return False


def _age_days(date_str, tz: str):
    if not date_str:
        return None
    try:
        from datetime import date, datetime
        from zoneinfo import ZoneInfo
        today = datetime.now(ZoneInfo(tz)).date()
        return (today - date.fromisoformat(str(date_str)[:10])).days
    except Exception:
        return None


def market_status() -> dict:
    out = {}
    for m, tz in (("cn", "Asia/Shanghai"), ("us", "America/New_York")):
        s = read_json(DAILY / f"summary_{m}.json", {}) or {}
        out[m] = {"name": s.get("market_name", m), "data_date": s.get("data_date"),
                  "generated_at": s.get("generated_at"), "age_days": _age_days(s.get("data_date"), tz),
                  "signals_mtime": _mtime(DAILY / f"signals_{m}.csv"),
                  "scan_mtime": _mtime(DAILY / f"scan_{m}.parquet")}
    prog = read_json(DAILY / "cn_all_progress.json", {}) or {}
    meta = read_json(DAILY / "scan_cn_meta.json", {}) or {}
    try:
        cached = sum(1 for e in os.scandir(DAILY / "cn_all") if e.name.endswith(".parquet"))
    except OSError:
        cached = 0
    out["cn"]["all"] = {"cached": cached, "listed": meta.get("listed"), "scanned": meta.get("stocks"),
                        "progress": prog, "scan_generated": meta.get("generated")}
    return out


_disk_cache = {"t": 0.0, "v": None}


def disk_usage() -> dict:
    now = time.time()
    if _disk_cache["v"] and now - _disk_cache["t"] < 120:
        return _disk_cache["v"]
    parts = []
    try:
        for e in os.scandir(DAILY):
            if e.is_dir(follow_symlinks=False):
                parts.append({"name": f"data/daily/{e.name}", "bytes": _du(Path(e.path))})
    except OSError:
        pass
    parts.sort(key=lambda x: -x["bytes"])
    free = None
    try:
        free = shutil.disk_usage(ROOT).free
    except OSError:
        pass
    v = {"data": _du(DATA), "logs": _du(LOGS), "export": _du(EXPORT), "free": free, "top": parts[:6]}
    _disk_cache.update(t=now, v=v)
    return v


def token_info() -> dict:
    return {"main_rotated": _mtime(SECRET_MAIN), "admin_rotated": _mtime(SECRET_ADMIN),
            "failed_logins_10min": failed_logins_10min(), "session_hours": SESSION_SECONDS // 3600}


def overview(ctx: dict | None = None) -> dict:
    ld = launchd_state()
    managed = any(s["label"] == "com.quantdaily.server" and s.get("pid") == os.getpid() for s in ld)
    out = {"now": int(time.time()),
           "server": {"pid": os.getpid(), "started": int(BOOT_TIME), "uptime": int(time.time() - BOOT_TIME),
                      "python": sys.version.split()[0], "managed": managed},
           "launchd": ld, "daily_running": daily_running(), "markets": market_status(),
           "disk": disk_usage(), "tasks": task_status(), "gap": gap_state(), "token": token_info()}
    if ctx:
        out.update(ctx["app_status"]())
    return out


# ---------------------------------------------------------------- 后台任务（子进程）
_procs: dict = {}


def task_status() -> dict:
    out = {}
    for name, t in TASKS.items():
        h = _procs.get(name)
        running = bool(h and h["proc"].poll() is None)
        out[name] = {"label": t["label"], "running": running, "started": h["started"] if h else None,
                     "returncode": (h["proc"].returncode if h and not running else None),
                     "tail": tail_lines(LOGS / f"admin_task_{name}.log", 3)}
    return out


def task_start(name: str) -> bool:
    if name not in TASKS:
        raise AdminError("未知任务")
    h = _procs.get(name)
    if h and h["proc"].poll() is None:
        return False
    t = TASKS[name]
    LOGS.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(ROOT / t["argv"][0]), *t["argv"][1:]]
    if t.get("caffeinate") and shutil.which("caffeinate"):
        cmd = ["caffeinate", "-i"] + cmd              # 任务期间不让 Mac 睡眠
    log = open(LOGS / f"admin_task_{name}.log", "w")
    proc = subprocess.Popen(cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    _procs[name] = {"proc": proc, "started": int(time.time())}
    return True


def task_stop(name: str) -> bool:
    h = _procs.get(name)
    if not h or h["proc"].poll() is not None:
        return False
    try:
        os.killpg(os.getpgid(h["proc"].pid), signal.SIGTERM)
        return True
    except OSError:
        return False


def kick_update() -> bool:
    """让 launchd 现在就跑一次"每日检查"（和每 15 分钟自动跑的那个一样，没到收盘/没更新需求时会直接退出）。"""
    if sys.platform != "darwin":
        raise AdminError("只有 macOS 上能用 launchd")
    r = subprocess.run(["launchctl", "kickstart", f"gui/{os.getuid()}/com.quantdaily.update"],
                       capture_output=True, text=True, timeout=15)
    if r.returncode != 0:
        raise AdminError("launchd 启动失败：" + (r.stderr or r.stdout).strip()[:200])
    return True


# ---------------------------------------------------------------- 数据缺口检查（后台线程）
_gap = {"running": False, "done": 0, "total": 0, "started": None, "error": None}
_gap_lock = threading.Lock()


def gap_state() -> dict:
    return {**_gap, "result": read_json(GAP_FILE, None)}


def gap_start() -> bool:
    with _gap_lock:
        if _gap["running"]:
            return False
        _gap.update(running=True, done=0, total=0, started=int(time.time()), error=None)
    threading.Thread(target=_gap_work, daemon=True).start()
    return True


def _gap_work():
    t0 = time.time()
    try:
        import pandas as pd
        from collections import Counter
        d = DAILY / "cn_all"
        codes = sorted(p.stem for p in d.glob("*.parquet"))
        _gap["total"] = len(codes)
        last, bad = {}, []
        for i, c in enumerate(codes):
            try:
                try:
                    idx = pd.read_parquet(d / f"{c}.parquet", columns=["close"]).index
                except Exception:
                    idx = pd.read_parquet(d / f"{c}.parquet").index
                last[c] = str(idx.max().date()) if len(idx) else None
            except Exception:
                bad.append(c)
            if i % 50 == 0:
                _gap["done"] = i
        names = {}
        try:
            lst = pd.read_csv(DAILY / "cn_all_list.csv", dtype=str).fillna("")
            names = dict(zip(lst["code"], lst["name"]))
        except Exception:
            pass
        by_date = Counter(v for v in last.values() if v)
        ref = by_date.most_common(1)[0][0] if by_date else None
        susp = set((read_json(DAILY / "cn_all_suspended.json", {}) or {}).get(ref, [])) if ref else set()
        stale = sorted(({"code": c, "name": names.get(c, ""), "last": v, "suspended": c in susp}
                        for c, v in last.items() if v and ref and v < ref), key=lambda x: x["last"])
        missing = [{"code": c, "name": n} for c, n in sorted(names.items()) if c not in last and c not in bad]
        res = {"finished": int(time.time()), "seconds": round(time.time() - t0, 1), "checked": len(codes),
               "ref_date": ref, "by_date": by_date.most_common(8),
               "stale_count": len(stale), "stale": stale[:300],
               "missing_count": len(missing), "missing": missing[:300], "unreadable": bad[:50]}
        GAP_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = GAP_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, GAP_FILE)
    except Exception as e:
        _gap["error"] = f"{type(e).__name__}: {e}"
    finally:
        _gap.update(running=False, done=_gap["total"])


# ---------------------------------------------------------------- 数据管理
def _last_csv_date(p: Path):
    lines = tail_lines(p, 1, max_bytes=2000)
    return lines[0].split(",")[0] if lines else None


def data_overview(ctx: dict) -> dict:
    watch = {}
    for m, stocks in ctx["watchlists"]().items():
        rows = []
        for code, name in stocks.items():
            p = DAILY / m / f"{code}.csv"
            rows.append({"code": code, "name": name, "last": _last_csv_date(p) if p.exists() else None,
                         "size": p.stat().st_size if p.exists() else 0, "mtime": _mtime(p)})
        watch[m] = rows
    return {"watch": watch, "pending": ctx["pending"](), "gap": gap_state(), "backups": backup_list()}


# ---------------------------------------------------------------- 备份（只打包配置、自选、记录，不含令牌和行情大文件）
BACKUP_NAME = re.compile(r"backup-\d{8}-\d{4,6}\.zip")
BACKUP_FILES = ["data/daily/watchlist_cn.json", "data/daily/watchlist_us.json", "data/daily/state.json",
                "data/daily/notifications.json", "data/daily/signals_history.parquet",
                "data/daily/signals_cn.csv", "data/daily/signals_us.csv", "data/daily/summary_cn.json",
                "data/daily/summary_us.json", "data/daily/cn_all_progress.json", "data/daily/cn_all_suspended.json",
                "data/daily/tracking_cn.json", "data/daily/tracking_us.json", "data/share_visits.jsonl",
                "data/admin_audit.jsonl", "data/members.db", "config.py", "CLAUDE.md", "README.md", "项目交接.md"]
BACKUP_DIRS = ["data/daily/code_history", "dashboard", "src", "engines", "scripts", "tests"]


def backup_list() -> list[dict]:
    out = []
    if EXPORT.exists():
        for p in sorted(EXPORT.glob("backup-*.zip"), reverse=True):
            if BACKUP_NAME.fullmatch(p.name):
                out.append({"name": p.name, "size": p.stat().st_size, "mtime": int(p.stat().st_mtime)})
    return out


def backup_path(name: str):
    """下载用：文件名必须符合固定格式，并且就在 export/ 里（不接受任何路径）。"""
    if not name or not BACKUP_NAME.fullmatch(name):
        return None
    p = EXPORT / name
    return p if p.is_file() else None


def backup_make() -> dict:
    EXPORT.mkdir(parents=True, exist_ok=True)
    name = time.strftime("backup-%Y%m%d-%H%M%S.zip")
    tmp = EXPORT / (name + ".tmp")
    n = 0
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        def add(p: Path):
            nonlocal n
            if ".secret" in p.parts or "__pycache__" in p.parts or p.name == ".DS_Store" or not p.is_file():
                return
            z.write(p, p.relative_to(ROOT).as_posix())
            n += 1
        for rel in BACKUP_FILES:
            add(ROOT / rel)
        for rel in BACKUP_DIRS:
            for p in sorted((ROOT / rel).rglob("*")):
                add(p)
    os.replace(tmp, EXPORT / name)
    for old in backup_list()[10:]:                   # 只留最新 10 份
        (EXPORT / old["name"]).unlink(missing_ok=True)
    return {"name": name, "size": (EXPORT / name).stat().st_size, "files": n}


# ---------------------------------------------------------------- 访客记录
def clear_visits() -> dict:
    p = DATA / "share_visits.jsonl"
    if not p.exists() or p.stat().st_size == 0:
        return {"cleared": 0}
    n = sum(1 for _ in open(p, encoding="utf-8"))
    bak = DATA / time.strftime("share_visits.%Y%m%d-%H%M%S.bak")
    shutil.copy2(p, bak)
    os.chmod(bak, 0o600)
    p.write_text("", encoding="utf-8")
    return {"cleared": n, "backup": bak.name}


# ---------------------------------------------------------------- 操作入口
def dispatch(action: str, body: dict, ctx: dict) -> dict:
    market = body.get("market")
    if market is not None and market not in ("cn", "us"):
        raise AdminError("未知市场")
    if action in CONFIRM_ACTIONS and body.get("confirm") is not True:
        raise AdminError("这个操作需要二次确认")
    detail = {k: body[k] for k in ("market", "code", "task", "uid") if k in body}
    try:
        res = _do(action, body, ctx, market, str(body.get("code", "")).strip())
    except AdminError as e:
        audit(action, detail, ok=False, error=str(e))
        raise
    except Exception as e:
        audit(action, detail, ok=False, error=f"{type(e).__name__}: {e}")
        raise AdminError(f"{type(e).__name__}: {e}")
    audit(action, detail, ok=True)
    return res


def _do(action: str, body: dict, ctx: dict, market, code: str) -> dict:
    if action == "refresh":
        started = ctx["start_refresh"](market)
        return {"ok": True, "started": started, "note": "" if started else "已经有一个更新在运行"}
    if action == "kick_update":
        return {"ok": kick_update()}
    if action == "task_start":
        started = task_start(str(body.get("task", "")))
        return {"ok": True, "started": started, "note": "" if started else "这个任务已经在运行"}
    if action == "task_stop":
        return {"ok": True, "stopped": task_stop(str(body.get("task", "")))}
    if action == "scan_start":
        return {"ok": True, "started": ctx["scan_start"]()}
    if action == "scan_stop":
        return {"ok": True, "stopped": ctx["scan_stop"]()}
    if action in ("watch_add", "watch_remove"):
        if not market:
            raise AdminError("缺少市场")
        err = ctx["watch_change"]("add" if action == "watch_add" else "remove", market, code)
        if err:
            raise AdminError(err)
        return {"ok": True}
    if action == "update_stock":
        if not market:
            raise AdminError("缺少市场")
        err = ctx["update_stock"](market, code)
        if err:
            raise AdminError(err)
        return {"ok": True}
    if action == "gap_start":
        return {"ok": True, "started": gap_start()}
    if action == "backup_make":
        return {"ok": True, **backup_make()}
    if action == "restart_server":
        managed = any(s["label"] == "com.quantdaily.server" and s.get("pid") == os.getpid() for s in launchd_state())
        if not managed:
            raise AdminError("网页服务不是由 launchd 启动的，重启会让它直接停止，不会自动拉起。"
                             "请在终端运行 bash scripts/open_dashboard.sh 或 bash scripts/install_mac.sh")
        threading.Timer(0.8, os._exit, (0,)).start()   # launchd 的 KeepAlive 会立刻重新拉起
        return {"ok": True, "note": "服务正在重启，几秒后页面会自动重连"}
    if action == "share_start":
        return {"ok": True, **ctx["share_start"]()}
    if action == "share_pass_set":
        from src import sharepass
        try:
            sharepass.set_password(str(body.get("password", "")))
        except ValueError as e:
            raise AdminError(str(e))
        return {"ok": True}
    if action == "share_pass_clear":
        from src import sharepass
        sharepass.clear()
        return {"ok": True}
    if action == "share_stop":
        return {"ok": True, "stopped": ctx["share_stop"]()}
    if action in ("member_disable", "member_enable", "member_delete"):
        from src import members
        try:
            uid = int(body.get("uid"))
        except (TypeError, ValueError):
            raise AdminError("会员编号不对")
        if action == "member_delete":
            members.delete_user(uid)
        else:
            members.set_disabled(uid, action == "member_disable")
        return {"ok": True}
    if action == "clear_visits":
        return {"ok": True, **clear_visits()}
    if action == "rotate_main_token":
        return {"ok": True, **ctx["rotate_main_token"]()}
    if action == "rotate_admin_token":
        ctx["rotate_admin_token"]()
        return {"ok": True, "note": "管理令牌已更换，当前会话已失效。请在终端运行 bash scripts/open_admin.sh 重新登录"}
    raise AdminError("未知操作")
