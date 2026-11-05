"""每日任务运行状态：每次 daily.py 跑完，原子写一份 logs/daily_status.json；连续失败时发一条 Mac 通知。

为什么要有它：daily.py 原来只在成功时发通知，失败只写日志、退出码 1，launchd 15 分钟后再试。
连着几天失败，只能自己翻日志才发现。现在：
- 网页/脚本随时读这个文件就知道"上次每日任务：成功还是失败、什么时候、什么原因、已经连续失败几次"；
- 连续失败达到 NOTIFY_THRESHOLD 次，发一条通知；同一原因（市场 + 归一化后的错误文字）一天只发一次。

"失败"的定义和 daily.py 的退出码一致：某个市场的 run_market 抛了异常。
被 daily.py 有意吞掉的小问题（某只自选股行情拉不到、模拟盘/账本更新失败等）只记进该市场的 warnings，
不算失败、不计入连续失败次数——否则一次网络抖动就会触发通知。

本模块只用标准库；所有对外方法都不会抛异常（写文件或发通知失败只记日志），不能因为"记状态"拖垮每日任务。
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
STATUS_PATH = ROOT / "logs" / "daily_status.json"
VERSION = 1
NOTIFY_THRESHOLD = 2          # 连续失败多少次开始提醒
ALERT_TZ = "Asia/Shanghai"    # "一天只发一次"按北京时间的日期算
MAX_WARNINGS = 10             # 每个市场最多记多少条 warnings（防止日志式膨胀）
MAX_TEXT = 200                # 单条错误/警告文字最长字数

_current = None               # 正在运行的 DailyRun；看门狗处理函数靠它找到当前这次运行


def current_run():
    return _current


def note(market: str, text: str):
    """给当前这次运行的某个市场记一条"被吞掉的小问题"；没有正在进行的运行时什么都不做（测试/单独调用 run_market 时安全）。"""
    run = _current
    if run is not None:
        run.add_note(market, text)


def format_error(err) -> str:
    """异常或字符串 → 一行短文字，如 "RuntimeError: 主品种没有数据"。"""
    if isinstance(err, BaseException):
        text = f"{type(err).__name__}: {err}"
    else:
        text = str(err)
    return " ".join(text.split())[:MAX_TEXT]


def reason_key(market: str, error_text: str) -> str:
    """"同一原因"的判断依据：市场 + 把数字换成 #（去掉行数、时间戳、端口之类每次都变的部分）后的错误文字。"""
    normalized = re.sub(r"\d+", "#", error_text)
    return f"{market}|{normalized[:120]}"


def load(path=None):
    """读状态文件；不存在或损坏返回 None（调用方当作"没有历史"）。"""
    try:
        data = json.loads(Path(path or STATUS_PATH).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _write_atomic(path: Path, data: dict):
    """先写同目录下的临时文件，落盘后 os.replace 覆盖：读的人要么看到旧的完整文件，要么看到新的完整文件，不会读到写一半的。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


class DailyRun:
    """一次 daily.py 运行的状态记录。用法：start() → 每个市场 market_ok / market_error → finish()。"""

    def __init__(self, markets, auto=False, offline=False, labels=None, path=None, now_fn=None):
        self.markets = list(markets)
        self.auto, self.offline = bool(auto), bool(offline)
        self.labels = dict(labels or {})
        self.path = Path(path) if path else STATUS_PATH
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        self.prev = {}
        self.started_at = None
        self.results = {m: {"name": self.labels.get(m, m), "status": "pending", "error": None, "warnings": []}
                        for m in self.markets}
        self.doc = {}
        self.finished = False
        self.finished_at = None       # finish() 时才有值
        self.consecutive = None       # finish() 时才算出；运行中沿用上一次留下的数
        self.alerts = None            # 本次运行里（发过通知后）更新过的提醒记录

    # ---------------------------------------------------------------- 生命周期
    def start(self):
        global _current
        self.prev = load(self.path) or {}
        self.started_at = self._now()
        _current = self
        self._flush("running")
        return self

    def add_note(self, market: str, text: str):
        if self.finished:
            return
        r = self.results.get(market)
        if r is not None and len(r["warnings"]) < MAX_WARNINGS:
            r["warnings"].append(format_error(text))

    def market_ok(self, market: str):
        self._set(market, "ok")

    def market_error(self, market: str, err):
        self._set(market, "error", format_error(err))

    def abort(self, reason: str, notify_fn=None):
        """被看门狗等强行中断时调用：还没跑完的市场一律记为失败，然后正常收尾（计数、通知）。"""
        for m, r in self.results.items():
            if r["status"] == "pending":
                self._set(m, "error", reason, flush=False)
        return self.finish(notify_fn)

    def finish(self, notify_fn=None) -> dict:
        """计算连续失败次数、写最终状态、必要时发通知。返回写入的内容。"""
        global _current
        if self.finished:
            return self.doc
        self.finished = True
        for m, r in self.results.items():
            if r["status"] == "pending":   # 正常流程不会出现；保守起见当失败，别让"没跑完"看起来像成功
                r["status"], r["error"] = "error", "未运行完"
        failed = [m for m, r in self.results.items() if r["status"] == "error"]
        prev_n = int(self.prev.get("consecutive_failures") or 0)
        self.consecutive = prev_n + 1 if failed else 0
        self.finished_at = self._now()
        self._flush("failed" if failed else "ok")
        if failed and self.consecutive >= NOTIFY_THRESHOLD and notify_fn is not None:
            self._alert(failed, notify_fn)
        _current = None
        return self.doc

    # ---------------------------------------------------------------- 内部
    def _set(self, market, status, error=None, flush=True):
        if self.finished:   # 已经收尾（例如看门狗已记过超时）就不再改写最终状态
            return
        r = self.results.get(market)
        if r is None:
            r = self.results[market] = {"name": self.labels.get(market, market), "status": "pending",
                                        "error": None, "warnings": []}
        r["status"], r["error"] = status, error
        r["finished_at"] = self._now().isoformat(timespec="seconds")
        if flush:
            self._flush("running")

    def _alerts_today(self) -> dict:
        today = self._now().astimezone(ZoneInfo(ALERT_TZ)).date().isoformat()
        old = self.prev.get("alerts") if isinstance(self.prev.get("alerts"), dict) else {}
        if self.alerts is not None:
            old = self.alerts
        if old.get("date") != today:   # 新的一天：昨天发过的原因今天可以再提醒一次
            old = {"date": today, "reasons": [], "last_sent_at": old.get("last_sent_at")}
        return old

    def _flush(self, state):
        """拼出完整状态并原子写盘。写失败只记日志。"""
        failed, succeeded = state == "failed", state == "ok"
        finished_at = self.finished_at
        now_iso = (finished_at or self._now()).isoformat(timespec="seconds")
        consecutive = self.consecutive if self.consecutive is not None else int(self.prev.get("consecutive_failures") or 0)
        self.doc = {
            "version": VERSION,
            "state": state,                          # running / ok / failed
            "started_at": self.started_at.isoformat(timespec="seconds"),
            "finished_at": finished_at.isoformat(timespec="seconds") if finished_at else None,
            "duration_seconds": round((finished_at - self.started_at).total_seconds(), 1) if finished_at else None,
            "mode": {"auto": self.auto, "offline": self.offline},
            "markets": self.results,
            "consecutive_failures": consecutive,
            "last_success_at": now_iso if succeeded else self.prev.get("last_success_at"),
            "last_failure_at": now_iso if failed else self.prev.get("last_failure_at"),
            # 上一次留下的状态是 running = 那次进程没正常收尾（被杀、断电等）；只提示，不计入失败次数
            "previous_run_interrupted": self.prev.get("state") == "running",
            "alerts": self._alerts_today(),
        }
        try:
            _write_atomic(self.path, self.doc)
        except Exception:
            logging.exception("写每日任务状态文件失败（不影响每日任务）")

    def _alert(self, failed, notify_fn):
        alerts = self._alerts_today()
        sent = set(alerts.get("reasons", []))
        fresh = [(m, self.results[m]["error"] or "未知原因") for m in failed
                 if reason_key(m, self.results[m]["error"] or "未知原因") not in sent]
        if not fresh:
            return   # 这些原因今天都提醒过了
        lines = "；".join(f"{self.results[m]['name']}：{text}" for m, text in fresh)
        try:
            notify_fn("量化日报 · 每日任务连续失败",
                      f"已连续失败 {self.consecutive} 次。{lines}。详情见 logs/daily_status.json 和 logs/daily.log。")
        except Exception:
            logging.exception("发送失败提醒出错（下次运行再试）")
            return
        alerts["reasons"] = sorted(sent | {reason_key(m, t) for m, t in fresh})
        alerts["last_sent_at"] = self._now().isoformat(timespec="seconds")
        self.alerts = alerts
        self._flush("failed")   # 把"今天已发过这些原因"写进状态文件，下次运行才能去重
