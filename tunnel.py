"""把网页临时分享给不在同一个网络的人（比如国内的家人）：
起一条 Cloudflare Tunnel（不用注册账号、不用买域名），生成一个外网能访问的临时网址，
转发到这台电脑上的网页服务。

免费，但网址是随机的：每次隧道重新连接都会换一个新的。

稳定性设计（都不用花钱）：
  1. 记住"应该在分享"这个状态（存到文件里），哪怕网页服务本身重启了
     （比如你改了代码触发自动重启、或者定时重启），也能在几秒内自动把隧道接回来。
  2. 后台有个守护线程，每 10 秒检查一次隧道进程是不是还活着，死了就自动重新拉一条，
     不用你手动点"分享"。
  3. 新网址生成后，不会立刻当"能用"返回给前端——先自己试着访问几次，
     等 Cloudflare 那边真的把这个域名解析通了、能打开了，才标记为"可以发给别人"。
     （之前遇到的"打不开/DNS 解析不到"大概率就是网址刚生成、还没在全球 DNS 传开。）
  4. 网页每隔十几秒会自动向服务器要一次最新状态，所以就算隧道中途断线重连、
     换了新网址，你打开的这个网页会自动更新成新链接，不用手动刷新。

注意：如果这台 Mac 本身睡眠/关机/断网，分享必然会断——这个不是软件能解决的，
需要电脑保持开机联网（挂着电源、系统设置里"电源适配器"下关掉自动睡眠）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIR = ROOT / "data" / "daily" / "share"
LOG = DIR / "tunnel.log"
STATUS = DIR / "status.json"
SESSION = DIR / "session.json"
URL_RE = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")

_lock = threading.Lock()
_watch_gen = 0  # 每次 start 一个新进程就 +1，让旧的 _watch/_probe 线程知道自己已经过时该退出了


# ---------- 小工具：读写状态文件 ----------

def _write_json(path: Path, obj: dict):
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path, default: dict) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return dict(default)


def _write_status(obj: dict):
    _write_json(STATUS, obj)


def _read_status_raw() -> dict:
    return _read_json(STATUS, {"running": False, "url": None})


def _read_session() -> dict:
    return _read_json(SESSION, {"enabled": False, "port": None, "token": None})


def _write_session(obj: dict):
    _write_json(SESSION, obj)


def _pid_alive(pid) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True  # 进程存在，只是不是我们发起的，也算活着
    return True


def status() -> dict:
    """给前端用：如果记录的进程其实已经死了，如实反映（而不是一直显示假的"分享中"）。"""
    st = _read_status_raw()
    sess = _read_session()
    pid = st.get("pid")
    if st.get("running") and pid and not _pid_alive(pid):
        if sess.get("enabled"):
            return {**st, "running": True, "url": None, "starting": True,
                    "note": "隧道断开了，正在自动重连…（一般几秒到一分钟内）"}
        return {**st, "running": False, "url": None}
    return st


# ---------- 安装 cloudflared ----------

def _ensure_cloudflared() -> str | None:
    """返回 cloudflared 可执行文件路径；自动装：先试 Homebrew，不行就直接下官方二进制。"""
    exe = shutil.which("cloudflared")
    if exe:
        return exe
    for p in ("/opt/homebrew/bin/cloudflared", "/usr/local/bin/cloudflared"):
        if Path(p).exists():
            return p
    local = DIR / "bin" / "cloudflared"
    if local.exists():
        return str(local)

    for brew in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew", shutil.which("brew")):
        if brew and Path(brew).exists():
            try:
                subprocess.run([brew, "install", "cloudflared"], timeout=180, check=True, capture_output=True)
            except Exception:
                pass
            for p in ("/opt/homebrew/bin/cloudflared", "/usr/local/bin/cloudflared"):
                if Path(p).exists():
                    return p
            break

    import io
    import platform
    import tarfile
    arch = "arm64" if platform.machine() == "arm64" else "amd64"
    url = f"https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-{arch}.tgz"
    try:
        with urllib.request.urlopen(url, timeout=60) as resp:
            data = resp.read()
        (DIR / "bin").mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(data)) as tf:
            member = next((m for m in tf.getmembers() if m.name.endswith("cloudflared")), None)
            if not member:
                return None
            f = tf.extractfile(member)
            if f is None:
                return None
            local.write_bytes(f.read())
        local.chmod(0o755)
        return str(local) if local.exists() else None
    except Exception:
        return None


# ---------- 起进程 + 等网址 + 等 DNS 真正生效 ----------

def _url_ready(url: str) -> bool:
    """真的去访问一下，确认这个域名已经能解析、能连上了（不只是 cloudflared 自己说创建成功）。
    注意：网页服务本身对没带令牌的请求会回 403"未授权"——那也说明 DNS/连接已经通了，
    只有连不上（DNS 解析不到、连接超时/拒绝）才算没准备好。"""
    import urllib.error
    try:
        req = urllib.request.Request(url, method="HEAD")
        urllib.request.urlopen(req, timeout=6)
        return True
    except urllib.error.HTTPError:
        return True  # 服务器响应了（哪怕是 403/404），说明网络链路是通的
    except Exception:
        pass
    try:
        req = urllib.request.Request(url, method="GET")
        urllib.request.urlopen(req, timeout=6)
        return True
    except urllib.error.HTTPError:
        return True
    except Exception:
        return False


def _watch(pid: int, token: str, gen: int):
    """拿到 cloudflared 打印的网址后，先自己反复探测，等真的能打开了才标记为可用。"""
    url = None
    t0 = time.time()
    while time.time() - t0 < 45 and url is None:
        with _lock:
            if gen != _watch_gen:
                return
        time.sleep(1)
        if not _pid_alive(pid):
            break
        try:
            text = LOG.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            text = ""
        m = URL_RE.search(text)
        if m:
            url = m.group(0)

    with _lock:
        if gen != _watch_gen:
            return
        if not url:
            tail = ""
            try:
                tail = LOG.read_text(encoding="utf-8", errors="ignore")[-500:]
            except Exception:
                pass
            _write_status({"running": False, "url": None, "pid": pid,
                            "error": "45 秒内没拿到网址，可能是网络问题。日志：" + tail})
            return
        _write_status({"running": True, "url": None, "starting": True, "pid": pid,
                        "note": "网址已生成，正在等它在全球 DNS 上传开（通常几秒到一分钟）…"})

    # 等 DNS/边缘节点真正就绪，最多再等 90 秒，其间前端会显示"正在等它生效"
    t1 = time.time()
    ready = False
    while time.time() - t1 < 90:
        with _lock:
            if gen != _watch_gen:
                return
        if not _pid_alive(pid):
            break
        if _url_ready(url):
            ready = True
            break
        time.sleep(3)

    with _lock:
        if gen != _watch_gen:
            return
        if ready:
            _write_status({"running": True, "url": f"{url}/?t={token}", "raw_url": url,
                            "pid": pid, "started": time.strftime("%Y-%m-%d %H:%M")})
        elif _pid_alive(pid):
            # 进程还在，只是探测一直没通——还是把链接给出去，附带提示，好过完全不给
            _write_status({"running": True, "url": f"{url}/?t={token}", "raw_url": url,
                            "pid": pid, "started": time.strftime("%Y-%m-%d %H:%M"),
                            "note": "网址生成了，但这台电脑自己还没探测通，可能要再等一下下"})
        else:
            _write_status({"running": False, "url": None, "pid": pid,
                            "error": "隧道进程中途退出了"})


def _start_process(port: int, token: str) -> dict:
    global _watch_gen
    with _lock:
        cur_pid = _read_status_raw().get("pid")
        if _pid_alive(cur_pid):
            return status()
    exe = _ensure_cloudflared()
    if not exe:
        st = {"running": False, "url": None,
              "error": "自动安装 cloudflared 没成功（试了 Homebrew，也试了直接下官方二进制，可能是网络问题）。"
                       "可以在终端手动运行一次：brew install cloudflared，装完再点「分享给家人看」"}
        _write_status(st)
        return st
    DIR.mkdir(parents=True, exist_ok=True)
    logf = open(LOG, "w")
    p = subprocess.Popen([exe, "tunnel", "--url", f"http://127.0.0.1:{port}"],
                         stdout=logf, stderr=subprocess.STDOUT, start_new_session=True)
    with _lock:
        _watch_gen += 1
        gen = _watch_gen
    _write_status({"running": True, "url": None, "starting": True, "pid": p.pid})
    threading.Thread(target=_watch, args=(p.pid, token, gen), daemon=True).start()
    return status()


def start(port: int, token: str) -> dict:
    _write_session({"enabled": True, "port": port, "token": token})
    return _start_process(port, token)


def stop() -> bool:
    global _watch_gen
    _write_session({"enabled": False, "port": None, "token": None})
    with _lock:
        _watch_gen += 1  # 让正在跑的 _watch 线程知道自己过时了，不要再覆盖状态
        pid = _read_status_raw().get("pid")
    if pid and _pid_alive(pid):
        try:
            os.kill(int(pid), signal.SIGTERM)
            time.sleep(0.5)
            if _pid_alive(pid):
                os.kill(int(pid), signal.SIGKILL)
        except Exception:
            pass
    _write_status({"running": False, "url": None})
    return True


# ---------- 后台守护：进程死了自动重连 ----------

def _supervisor():
    while True:
        time.sleep(10)
        try:
            sess = _read_session()
            if not sess.get("enabled"):
                continue
            port, token = sess.get("port"), sess.get("token")
            if not port or not token:
                continue
            pid = _read_status_raw().get("pid")
            if _pid_alive(pid):
                continue
            _start_process(int(port), token)
        except Exception:
            pass


threading.Thread(target=_supervisor, daemon=True).start()
