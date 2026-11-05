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


def _same_day_duplicate(items: list, rec: dict) -> int | None:
    """在已有记录里找同一天（按记录自己的时区日期，即 time 的前 10 位）、标题和正文都相同的一条，返回下标；没有返回 None。"""
    day = rec["time"][:10]
    for i, it in enumerate(items):
        if (isinstance(it, dict) and it.get("title") == rec["title"] and it.get("body") == rec["body"]
                and str(it.get("time", ""))[:10] == day):
            return i
    return None


def send(title: str, body: str, timezone: str = "Asia/Shanghai", timezone_label: str = "北京时间"):
    """发一条系统通知，并记到 notifications.json（网页上可以看到最近的提醒）。"""
    try:
        items = json.loads(LOG.read_text()) if LOG.exists() else []
    except Exception:
        items = []
    now = datetime.now(ZoneInfo(timezone))
    rec = {"time": now.strftime("%Y-%m-%d %H:%M"), "timestamp": now.isoformat(timespec="seconds"),
           "timezone_label": timezone_label, "title": title, "body": body}
    # D09（C11）：daily.py 收盘后每隔约 16 分钟重跑一次等当天数据，每次都发同一条日报，
    # 一个晚上会重复 6–12 条、响 6–12 次。同一天里标题和正文都相同的通知只保留一条：
    # 更新时间、累计次数，挪到最前面，不再弹窗。标题里已含市场和数据日期，数据日期变了照常通知。
    dup = _same_day_duplicate(items, rec)
    if dup is not None:
        old = items.pop(dup)
        rec["count"] = int(old.get("count", 1)) + 1
        rec["first_time"] = old.get("first_time", old.get("time"))
    items.insert(0, rec)
    tmp = LOG.with_suffix(".tmp")
    tmp.write_text(json.dumps(items[:100], ensure_ascii=False), encoding="utf-8")
    if len(json.loads(tmp.read_text(encoding="utf-8"))) != len(items[:100]):
        tmp.unlink(missing_ok=True)
        raise IOError("通知记录临时文件校验失败")
    tmp.replace(LOG)
    if dup is not None or platform.system() != "Darwin":
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
