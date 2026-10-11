"""Mac 通知：每天更新完，把自选股和主要 ETF 的信号变化弹出来。

用 macOS 自带的 osascript，不需要安装任何东西、不经过任何服务器。
第一次弹通知时，系统可能会问是否允许"脚本编辑器"发送通知，点允许。
"""
from __future__ import annotations

import json
import platform
import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo

from .datasource import CACHE

LOG = CACHE / "notifications.json"


def send(title: str, body: str, timezone: str = "Asia/Shanghai", timezone_label: str = "北京时间"):
    """发一条系统通知，并记到 notifications.json（网页上可以看到最近的提醒）。"""
    try:
        items = json.loads(LOG.read_text()) if LOG.exists() else []
    except Exception:
        items = []
    now = datetime.now(ZoneInfo(timezone))
    items.insert(0, {"time": now.strftime("%Y-%m-%d %H:%M"), "timestamp": now.isoformat(timespec="seconds"),
                     "timezone_label": timezone_label, "title": title, "body": body})
    tmp = LOG.with_suffix(".tmp")
    tmp.write_text(json.dumps(items[:100], ensure_ascii=False), encoding="utf-8")
    if len(json.loads(tmp.read_text(encoding="utf-8"))) != len(items[:100]):
        tmp.unlink(missing_ok=True)
        raise IOError("通知记录临时文件校验失败")
    tmp.replace(LOG)
    if platform.system() != "Darwin":
        return
    esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
    script = f'display notification "{esc(body[:240])}" with title "{esc(title)}" sound name "Glass"'
    try:
        subprocess.run(["osascript", "-e", script], timeout=10, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def recent(n: int = 20) -> list:
    try:
        return json.loads(LOG.read_text())[:n] if LOG.exists() else []
    except Exception:
        return []
