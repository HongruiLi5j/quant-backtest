"""网页服务里的"任务管理"：把回测/优化/对比放到独立子进程里跑，网页轮询进度。

只用标准库（网页服务进程里不导入 backtrader / RQAlpha，它们只在子进程里用）。
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JOBS = ROOT / "data" / "daily" / "engine_jobs"
LIB = ROOT / ".engines_lib"          # backtrader / RQAlpha 及其依赖（和主环境的 pandas 版本隔离）
TIMEOUT = {"run": 300, "compare": 600, "optimize": 1800, "check": 900}
MAX_RUNNING = 3
CODE_RE = re.compile(r"^(60|68|00|30)\d{4}$")
ENGINES = ("backtrader", "rqalpha", "inhouse", "custom")

_procs: dict = {}
_lock = threading.Lock()


def env() -> dict:
    """子进程的环境：优先使用 .engines_lib 里的库（RQAlpha 需要 pandas 2.x，主环境是 pandas 3）。"""
    e = dict(os.environ)
    e["PYTHONPATH"] = os.pathsep.join([str(LIB), str(ROOT)] + ([e["PYTHONPATH"]] if e.get("PYTHONPATH") else []))
    e["PYTHONWARNINGS"] = "ignore"
    return e


def _clean_old():
    if not JOBS.exists():
        return
    ds = sorted((p for p in JOBS.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime)
    for p in ds[:-80]:                       # 只留最近 80 个任务
        shutil.rmtree(p, ignore_errors=True)


def validate(job: dict) -> dict:
    kind = job.get("kind")
    if kind not in TIMEOUT:
        raise ValueError("未知任务类型")
    out = {"kind": kind}
    if kind in ("run", "optimize", "check"):
        if job.get("engine") not in ENGINES:
            raise ValueError("未知引擎")
        out["engine"] = job["engine"]
    code = str(job.get("code") or "600531").strip()
    if not CODE_RE.match(code):
        raise ValueError("请输入 6 位沪深 A 股代码（60/68/00/30 开头）")
    out["code"] = code
    if kind != "check":
        out["strategy"] = str(job.get("strategy", ""))[:40]
        p = job.get("params") or {}
        out["params"] = {str(k)[:40]: float(v) for k, v in p.items() if isinstance(v, (int, float))} if isinstance(p, dict) else {}
        s = str(job.get("start") or "")[:10]
        out["start"] = s if re.match(r"^\d{4}-\d{2}-\d{2}$", s) else None
        try:
            cash = float(job.get("cash") or 100000)
        except (TypeError, ValueError):
            cash = 100000.0
        out["cash"] = min(max(cash, 10_000.0), 100_000_000.0)
    if kind == "optimize":
        o = job.get("opt") or {}
        opt = {"metric": str(o.get("metric", "sharpe"))[:10]}
        sp = str(o.get("split") or "")[:10]
        opt["split"] = sp if re.match(r"^\d{4}-\d{2}-\d{2}$", sp) else None
        for ax in ("x", "y"):
            a = o.get(ax)
            if isinstance(a, dict) and a.get("key"):
                opt[ax] = {"key": str(a["key"])[:40], "from": float(a["from"]), "to": float(a["to"]),
                           "step": float(a["step"])}
        out["opt"] = opt
    return out


def start(job: dict) -> str:
    job = validate(job)
    with _lock:
        running = [j for j, (p, _, _) in _procs.items() if p.poll() is None]
        if len(running) >= MAX_RUNNING:
            raise RuntimeError(f"已经有 {len(running)} 个任务在跑，等一下再试")
        _clean_old()
        jid = time.strftime("%m%d%H%M%S") + "-" + secrets.token_hex(3)
        d = JOBS / jid
        d.mkdir(parents=True, exist_ok=True)
        (d / "job.json").write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
        log = open(d / "stderr.log", "w")
        p = subprocess.Popen([sys.executable, "-m", "engines.runner", str(d)], cwd=ROOT, env=env(),
                             stdout=subprocess.DEVNULL, stderr=log)
        _procs[jid] = (p, time.time(), TIMEOUT[job["kind"]])
    return jid


def get(jid: str) -> dict:
    if not re.match(r"^\d{10}-[0-9a-f]{6}$", jid or ""):
        return {"status": "error", "error": "任务编号不对"}
    d = JOBS / jid
    if not d.exists():
        return {"status": "error", "error": "任务不存在（可能已被清理）"}
    res = d / "result.json"
    if res.exists():
        r = json.loads(res.read_text(encoding="utf-8"))
        return {"status": "error" if "error" in r else "done", "result": r}
    item = _procs.get(jid)
    prog = None
    try:
        prog = json.loads((d / "progress.json").read_text(encoding="utf-8"))
    except Exception:
        pass
    if item:
        p, t0, limit = item
        if p.poll() is None and time.time() - t0 > limit:
            p.kill()
            return {"status": "error", "error": f"超时（超过 {limit} 秒），已停止。可能是代码里有死循环，或者组合太多"}
        if p.poll() is not None:
            err = (d / "stderr.log").read_text(encoding="utf-8", errors="ignore")[-3000:]
            return {"status": "error", "error": "子进程异常退出", "result": {"trace": err}}
    return {"status": "running", "progress": prog}


# ---------------------------------------------------------------- 全市场三引擎扫描
def scan_start() -> bool:
    from engines import scan_all
    if scan_all.is_running():
        return False
    scan_all.DIR.mkdir(parents=True, exist_ok=True)
    scan_all.REQUEST.touch()
    log = open(scan_all.DIR / "stderr.log", "a")
    cmd = [sys.executable, "-m", "engines.scan_all"]
    if shutil.which("caffeinate"):
        cmd = ["caffeinate", "-i"] + cmd       # 扫描期间不让 Mac 自动睡眠
    subprocess.Popen(cmd, cwd=ROOT, env=env(), stdout=subprocess.DEVNULL, stderr=log,
                     start_new_session=True)
    return True


def scan_stop() -> bool:
    from engines import scan_all
    scan_all.REQUEST.unlink(missing_ok=True)
    try:
        pid = int(scan_all.LOCK.read_text())
        os.killpg(os.getpgid(pid), 15)
        return True
    except Exception:
        return False


def on_server_start():
    """网页服务启动时：如果之前要求过全市场扫描但没跑完，自动继续；如果有自检请求，跑一次自检。"""
    from engines import scan_all
    try:
        if scan_all.REQUEST.exists() and not scan_all.is_running():
            scan_start()
    except Exception:
        pass
    flag = ROOT / "logs" / ".engine_selftest"
    if flag.exists():
        flag.unlink(missing_ok=True)

        def work():
            for eng in ("backtrader", "rqalpha"):
                try:
                    jid = start({"kind": "check", "engine": eng, "code": "600531"})
                    for _ in range(300):
                        time.sleep(2)
                        g = get(jid)
                        if g["status"] != "running":
                            (ROOT / "logs" / f"engine_selftest_{eng}.json").write_text(
                                json.dumps(g, ensure_ascii=False, default=float)[:20000], encoding="utf-8")
                            break
                except Exception as e:
                    (ROOT / "logs" / f"engine_selftest_{eng}.json").write_text(str(e), encoding="utf-8")
        threading.Thread(target=work, daemon=True).start()
