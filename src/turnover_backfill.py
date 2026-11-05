"""限速补取腾讯真实成交额；命令行硬限时 15 分钟，每日自动步骤硬限时 10 分钟。"""
from __future__ import annotations

import argparse
import fcntl
import json
import logging
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from . import market_clock, turnover
from .datasource import CACHE

URL = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"


def fetch(code, start, end, *, timeout=10, get=None):
    """原始第 9 列成交额为万元；科创板量为股，其余沪深个股量为手。"""
    sym = ("sh" if code.startswith("6") else "sz") + code
    response = (get or requests.get)(URL, params={"_var": "kline_day", "param": f"{sym},day,{start},{end},80,"},
                                    headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}, timeout=timeout)
    response.raise_for_status()
    text = response.text
    data = json.loads(text[text.find("={") + 1:])["data"][sym]
    bars = data.get("day")
    if not bars or any(len(bar) < 9 for bar in bars):
        raise ValueError("腾讯原始日线未返回成交额字段，停止且不换来源")
    rows = []
    for bar in bars:
        day = str(bar[0])
        if not start <= day <= end:
            continue
        row = turnover.normalize(code, day, bar[8], bar[5], bar[2], bar[4], bar[3],
                                 source="腾讯历史", amount_unit="万元",
                                 volume_unit="股" if code.startswith("68") else "手")
        if row is None:
            raise ValueError(f"{code} {day} 成交额/成交量/价格校验失败，停止且不换来源")
        rows.append(row)
    return rows


def listed_codes(payload):
    return {row["code"] for pack in payload.get("strategies", {}).values()
            for row in pack.get("rows", []) + pack.get("exceptions", [])}


def scan_codes(base, day=None):
    """复用已成功扫描的动作列，不重新跑扫描、不读取全行情目录。"""
    meta = json.loads((base / "scan_cn_meta.json").read_text())
    release = meta["data_date"]
    if day is not None and day != release:
        raise ValueError("待补取日期与现有扫描日期不一致")
    frame = pd.read_parquet(base / "scan_cn.parquet")
    selected = frame[frame.action.isin(["买入", "卖出", "加仓", "减仓"])]
    selected = selected[~selected.delisted.fillna(False)]
    selected = selected[~selected.name.str.upper().str.contains("ST|退", regex=True, na=False)]
    return release, {str(code) for code in selected.code if re.fullmatch(r"(?:60|68|00|30)\d{4}", str(code))}


def _atomic_report(path, value):
    if path is None:
        return
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp.json")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=1), encoding="utf-8")
    temporary.replace(path)


def _failure(exc):
    if isinstance(exc, requests.Timeout):
        category = "请求超时"
    elif isinstance(exc, requests.RequestException):
        category = "网络或 HTTP 错误"
    elif "未返回完整" in str(exc):
        category = "历史日期缺失"
    elif isinstance(exc, (ValueError, KeyError, TypeError)):
        category = "字段或单位价格校验失败"
    elif isinstance(exc, OSError):
        category = "旁表写入失败"
    else:
        category = "其他错误"
    return {"category": category, "reason": str(exc)[:240]}


def run(*, codes=None, validation_samples=False, max_seconds=900, cache=None,
        fetcher=None, calendar=None, now=None, sleep=None, monotonic=None,
        from_scan=False, release_day=None, report_path=None):
    """每只保存一次以便续传；固定样本模式仅用于用户明确授权的这 8 只验收。"""
    base = Path(cache) if cache is not None else CACHE
    if from_scan:
        day, allowed = scan_codes(base, release_day)
    else:
        payload = json.loads((base / "trade_list_cn.json").read_text())
        day = payload["data_date"]
        allowed = set(turnover.SAMPLES) if validation_samples else listed_codes(payload)
    current = (now or datetime.now(ZoneInfo("Asia/Shanghai"))).astimezone(ZoneInfo("Asia/Shanghai"))
    if not market_clock.bar_usable(day, current):
        raise ValueError("清单日期尚未收盘，不能补取")
    selected = set(turnover.SAMPLES) if validation_samples and not codes else (set(codes) if codes else allowed)
    if selected - allowed:
        raise ValueError("普通模式只允许当前清单代码，样本模式只允许固定 8 只")
    calendar = calendar if calendar is not None else turnover.cached_calendar(base)
    days = sorted({pd.Timestamp(d).strftime("%Y-%m-%d") for d in calendar if str(d)[:10] <= day})[-20:]
    if len(days) != 20:
        raise ValueError("交易日历不足 20 个交易日")
    path = base / "turnover_cn.parquet"
    existing = turnover.load(path)
    suspension_file = base / "cn_all_suspended.json"
    suspended = json.loads(suspension_file.read_text()) if suspension_file.exists() else {}
    fetcher, sleep, monotonic = fetcher or fetch, sleep or time.sleep, monotonic or time.monotonic
    started, last_request = monotonic(), None
    report = {"day": day, "total": len(selected), "written": 0, "requested": 0, "skipped": 0,
              "completed": 0, "failed": {}, "unprocessed": sorted(selected), "time_limit": False}
    processed = set()
    def checkpoint():
        report["elapsed_seconds"] = round(monotonic() - started, 3)
        report["unprocessed"] = sorted(selected - processed)
        _atomic_report(report_path, report)
    checkpoint()
    for code in sorted(selected):
        have = set(existing.loc[existing.code == code, "date"]) if len(existing) else set()
        if all(d in have or code in suspended.get(d, []) for d in days):
            report["skipped"] += 1
            processed.add(code)
            continue
        if last_request is not None:
            pause = max(0, 1.05 - (monotonic() - last_request))
            if pause:
                sleep(pause)
        remaining = max_seconds - (monotonic() - started)
        if remaining <= 1:
            report["time_limit"] = True
            break
        last_request = monotonic()
        report["requested"] += 1
        checkpoint()
        try:
            rows = fetcher(code, days[0], days[-1], timeout=min(10, remaining))
            if monotonic() - started >= max_seconds:
                report["time_limit"] = True
                break
            expected = {d for d in days if code not in suspended.get(d, [])}
            rows = [row for row in rows if row["date"] in expected and row["code"] == code]
            if not expected.issubset({row["date"] for row in rows}):
                raise ValueError(f"{code} 未返回完整 20 日内的非停牌数据，不入库、不换来源")
            report["written"] += turnover.save(rows, path)
            report["completed"] += 1
            print(f"{code} 已保存 {len(rows)} 个交易日真实成交额", flush=True)
        except Exception as exc:
            report["failed"][code] = _failure(exc)
            logging.warning("成交额补取 %s 失败：%s", code, report["failed"][code])
        processed.add(code)
        checkpoint()
    checkpoint()
    return report


def run_bounded(*, max_seconds=900, cache=None, from_scan=False, release_day=None,
                codes=None, validation_samples=False, report_path=None):
    """父进程硬超时杀掉网络工作进程；保留已原子写入的旁表和逐股检查点。"""
    base = Path(cache) if cache is not None else CACHE
    base.mkdir(parents=True, exist_ok=True)
    report_path = Path(report_path) if report_path is not None else base / "turnover_backfill_report_cn.json"
    started = time.monotonic()
    with (base / "turnover_backfill_run.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"busy": True, "error": "已有补取运行，本次跳过"}
        command = [sys.executable, "-m", "src.turnover_backfill", "--worker", "--cache", str(base),
                   "--max-seconds", str(max_seconds), "--report-path", str(report_path)]
        if from_scan:
            command.append("--from-scan")
        if release_day:
            command.extend(["--date", release_day])
        if validation_samples:
            command.append("--validation-samples")
        if codes:
            command.extend(["--codes", *codes])
        # 启动前清空本次报告，不能把上次的失败/完成数冒充本次结果。
        _atomic_report(report_path, {"day": release_day, "status": "starting"})
        timed_out, failure = False, None
        with (base / "turnover_backfill_cn.log").open("a", encoding="utf-8") as output:
            try:
                completed = subprocess.run(command, cwd=Path(__file__).resolve().parents[1],
                                           stdout=output, stderr=subprocess.STDOUT,
                                           timeout=max(.001, max_seconds - (time.monotonic() - started)))
                if completed.returncode:
                    failure = f"补取子进程退出码 {completed.returncode}，详见成交额补取日志"
            except subprocess.TimeoutExpired:
                timed_out = True
                logging.warning("成交额补取达到 %s 秒硬上限，已终止，继续发布清单", max_seconds)
        result = json.loads(report_path.read_text())
        result.update(time_limit=timed_out or result.get("time_limit", False),
                      elapsed_seconds=round(time.monotonic() - started, 3))
        if failure:
            result["error"] = failure
        _atomic_report(report_path, result)
        return result


def automatic_once(day, *, cache=None, runner=None):
    """同一发布日最多尝试一次，先落盘占位；失败也不重试，不影响清单发布。"""
    base = Path(cache) if cache is not None else CACHE
    base.mkdir(parents=True, exist_ok=True)
    path = base / "turnover_backfill_auto.json"
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        record = json.loads(path.read_text()) if path.exists() else {"days": {}}
        if day in record["days"]:
            logging.info("%s 已执行过每日成交额补取，本次跳过", day)
            return {"skipped_day": True}
        record["days"][day] = {"status": "started", "started_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")}
        _atomic_report(path, record)
    try:
        report = (runner or run_bounded)(max_seconds=600, cache=base, from_scan=True, release_day=day,
                                        report_path=base / "turnover_backfill_auto_report_cn.json")
        logging.info("每日成交额补取结束：%s", report)
        status = {"status": "finished", "time_limit": report.get("time_limit", False), "error": report.get("error")}
    except Exception as exc:
        logging.exception("每日成交额补取失败，继续发布清单")
        report = {"error": str(exc)[:240]}
        status = {"status": "failed", "error": report["error"]}
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        record = json.loads(path.read_text())
        record["days"][day].update(status)
        _atomic_report(path, record)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codes", nargs="*", help="当前清单代码；不指定时为当前清单所有代码")
    parser.add_argument("--validation-samples", action="store_true", help="仅运行已授权的固定 8 股验收样本")
    parser.add_argument("--max-seconds", type=int, default=900)
    parser.add_argument("--from-scan", action="store_true", help="复用当前扫描中全部买卖加减仓信号代码")
    parser.add_argument("--date", help="校验当前扫描的发布日期")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--cache", type=Path, default=CACHE, help=argparse.SUPPRESS)
    parser.add_argument("--report-path", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 1 <= args.max_seconds <= 900:
        parser.error("总时长上限必须为 1～900 秒")
    if args.from_scan and args.validation_samples:
        parser.error("扫描模式不能与固定样本模式混用")
    runner = run if args.worker else run_bounded
    print(json.dumps(runner(codes=args.codes, validation_samples=args.validation_samples, max_seconds=args.max_seconds,
                           cache=args.cache, from_scan=args.from_scan, release_day=args.date, report_path=args.report_path), ensure_ascii=False))


if __name__ == "__main__":
    main()
