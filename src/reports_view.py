"""报告页（N22）：只读列出 reports/ 下的 markdown 报告，读取单份报告的文字，给新界面 ui/w/reports.html 用。

边界：
- 只读 reports/ 目录里的 .md 普通文件；不跟随符号链接；跳过隐藏文件/目录和 .before-* 备份；
- 读单份报告时，路径必须是当前清单里出现过的相对路径（先列清单再比对），再确认解析后仍在 reports/ 里；
- 单份最多返回 MAX_BYTES 字节，超出截断并标出 truncated；去掉除换行/制表符外的控制字符；
- 不写任何文件，不联网。鉴权（本机或主人通道、登录、请求头、限流）在 app.py 的 _reports 里。
"""
from __future__ import annotations

import os
import re
import stat
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"
MAX_FILES = 2000
MAX_DEPTH = 4
MAX_BYTES = 600_000
MAX_PATH = 300
CACHE_SECONDS = 30
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_DATE = re.compile(r"(20\d\d)-?(\d\d)-?(\d\d)")
_cache = {"t": 0.0, "root": None, "v": None}
_lock = threading.Lock()


class ReportError(Exception):
    """固定文案的错误，可以安全地显示给页面。"""


def _skip(name: str) -> bool:
    return name.startswith(".") or ".before-" in name


def _kind(rel: str) -> str:
    s = rel.lower()
    for key, words in (("replay", ("replay",)), ("weekly", ("weekly", "-w")), ("quality", ("quality",)),
                       ("factor", ("factor", "fc_", "fill_")), ("signal", ("signal",)), ("eod", ("eod",)),
                       ("progress", ("progress",))):
        if any(w in s for w in words):
            return key
    return "other"


def _title(path: Path, fallback: str) -> str:
    try:
        with open(path, "rb") as f:
            head = f.read(4096).decode("utf-8", errors="replace")
    except OSError:
        return fallback
    for line in head.splitlines():
        if line.startswith("# "):
            t = _CTRL.sub("", line[2:]).strip()
            return t[:200] or fallback
    return fallback


def _date(group: str, mtime: int) -> str:
    m = _DATE.search(group)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return time.strftime("%Y-%m-%d", time.localtime(mtime))


def _scan(root: Path) -> list[dict]:
    out = []
    base = len(root.parts)
    for d, dirs, files in os.walk(root, followlinks=False):
        here = Path(d)
        depth = len(here.parts) - base
        dirs[:] = sorted(x for x in dirs if not _skip(x)) if depth < MAX_DEPTH else []
        for name in sorted(files):
            if _skip(name) or not name.lower().endswith(".md"):
                continue
            p = here / name
            try:
                st = os.lstat(p)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):           # 符号链接、设备文件等一律跳过
                continue
            rel = p.relative_to(root).as_posix()
            group = rel.split("/", 1)[0] if "/" in rel else ""
            mtime = int(st.st_mtime)
            out.append({"path": rel, "title": _title(p, name[:-3]), "kind": _kind(rel), "group": group,
                        "date": _date(group or name, mtime), "size": int(st.st_size), "mtime": mtime})
            if len(out) >= MAX_FILES:
                return out
    return out


def _listing(root: Path | None = None) -> list[dict]:
    root = root or REPORTS
    with _lock:
        if _cache["v"] is not None and _cache["root"] == str(root) and time.time() - _cache["t"] < CACHE_SECONDS:
            return _cache["v"]
    items = _scan(root) if root.is_dir() and not root.is_symlink() else []
    items.sort(key=lambda r: (r["date"], r["mtime"]), reverse=True)
    with _lock:
        _cache.update(t=time.time(), root=str(root), v=items)
    return items


def list_reports(root: Path | None = None) -> dict:
    items = _listing(root)
    return {"reports": items, "total": len(items), "truncated": len(items) >= MAX_FILES}


def read_report(rel, root: Path | None = None) -> dict:
    root = root or REPORTS
    if not isinstance(rel, str) or not rel or len(rel) > MAX_PATH or "\\" in rel or "\x00" in rel or rel.startswith("/"):
        raise ReportError("没有这份报告")
    if any(part in ("", ".", "..") for part in rel.split("/")):
        raise ReportError("没有这份报告")
    meta = next((r for r in _listing(root) if r["path"] == rel), None)
    if meta is None:
        raise ReportError("没有这份报告")
    p = root / rel
    try:
        real_root = root.resolve(strict=True)
        p.resolve(strict=True).relative_to(real_root)
        st = os.lstat(p)
        if not stat.S_ISREG(st.st_mode):
            raise ReportError("没有这份报告")
        with open(p, "rb") as f:
            raw = f.read(MAX_BYTES + 1)
    except (OSError, ValueError):
        raise ReportError("没有这份报告")
    truncated = len(raw) > MAX_BYTES
    text = raw[:MAX_BYTES].decode("utf-8", errors="replace")
    text = _CTRL.sub("", text.replace("\r\n", "\n"))
    return {**meta, "size": int(st.st_size), "text": text, "truncated": truncated, "max_bytes": MAX_BYTES}
