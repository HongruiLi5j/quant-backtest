"""网页服务观察成功发布，结合已验证的本机会话发一次数量通知。"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

from . import datasource, notify


def summary(payload, related_codes):
    pack = payload["strategies"].get(payload.get("default_strategy", "blend2"), {})
    buys = {row["code"] for row in pack.get("rows", []) if row["action"] == "买入" and not row.get("small_target", False)}
    sells = {row["code"] for row in pack.get("rows", []) if row["action"] == "卖出"}
    return {"buy": len(buys), "sell": len(sells), "related_sell": len(sells & set(related_codes))}


def send_publication(payload, related_codes, *, recent=None, sender=None):
    title = f"今日买卖 · {payload['data_date']}"
    records = notify.recent(100) if recent is None else recent
    if any(item.get("title") == title for item in records):
        return False
    counts = summary(payload, related_codes)
    body = (f"默认组合买入主要信号 {counts['buy']} 只，卖出 {counts['sell']} 只；"
            f"本机会话的模拟持仓或自选中有 {counts['related_sell']} 只出现卖出信号。")
    (sender or notify.send)(title, body)
    return True


def start_watcher(local_position_codes, *, path=None):
    """只观察后续成功发布；重启时不补发旧文件，去重沿用现有通知日志。"""
    path = Path(path) if path is not None else datasource.CACHE / "trade_list_cn.json"
    def stamp():
        return (path.stat().st_mtime_ns, path.stat().st_size) if path.exists() else None
    def watch():
        previous = stamp()
        while True:
            time.sleep(5)
            current = stamp()
            if current == previous or current is None:
                continue
            try:
                payload = json.loads(path.read_text())
                from . import trade_signals
                trade_signals.record_publication(payload)  # 同日记录失败时重试；旧日清单不会回填。
                related = set(datasource.load_watchlist("cn")) | set(local_position_codes())
                send_publication(payload, related)
                previous = current
            except Exception:
                logging.exception("今日买卖汇总通知暂未发送，下次观察重试")
    threading.Thread(target=watch, daemon=True).start()
