#!/usr/bin/env python3
"""T006 S0c：默认关闭的有限次匿名验证；--self-test 全程离线。"""
from __future__ import annotations

import argparse
import contextlib
from datetime import datetime, timedelta, timezone
import importlib.metadata
import io
import json
import math
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

INTERFACE = "stock_zh_a_hist_min_em"
SAMPLES = ("SH600000",)
MAX_REQUESTS = 5
BEIJING = ZoneInfo("Asia/Shanghai")
ROOT = Path(__file__).resolve().parents[1]
URL = "https://push2his.eastmoney.com/api/qt/stock/trends2/get"
SUMMARY_LIMIT = 5
MAX_BARS = 2000


class FeedError(Exception):
    def __init__(self, category):
        self.category = category


def normalize_symbol(symbol):
    # 仅沪深 A 股代码，不支持基金、指数、北交所或任意 URL。
    import re
    value = symbol.upper()
    code = value[2:] if value.startswith(("SH", "SZ")) else value
    if not re.fullmatch(r"(?:60[0135]|688|000|001|002|003|300|301)[0-9]{3}", code):
        raise ValueError("仅允许沪深 A 股六位代码")
    market = "SH" if code.startswith("6") else "SZ"
    if len(value) != 6 and value != market + code:
        raise ValueError("代码与市场不符")
    return market + code


def beijing_timestamp(value):
    """兼容原 S0 的 UTC 毫秒转换离线回归。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise FeedError("interface_response_error")
    return datetime.fromtimestamp(value / 1000, timezone.utc).astimezone(BEIJING).isoformat()


def minute_time(value):
    # 源字符串没有时区；北京时间仅是显式假设，未取得上游时区说明。
    if not isinstance(value, str) or len(value) != 16:
        raise FeedError("interface_response_error")
    try:
        result = datetime.strptime(value, "%Y-%m-%d %H:%M")
    except ValueError:
        raise FeedError("interface_response_error") from None
    if result.strftime("%Y-%m-%d %H:%M") != value:
        raise FeedError("interface_response_error")
    return result.replace(tzinfo=BEIJING)


def classify_response(status, text):
    # 只分类，不输出、保存上游正文。验证码/封禁也立即停止。
    text = text.lower()
    if status == 429 or any(w in text for w in ("too many requests", "rate limit", "限流", "请求过于频繁", "访问频繁")):
        raise FeedError("rate_limited")
    if status in (401, 403) or any(w in text for w in (
        "captcha", "验证码", "登录", "认证", "unauthorized", "login", "forbidden",
        "封禁", "封锁", "封ip", "封 ip", "banned", "blocked", "access denied",
        "authentication", "cookie", "token required",
    )):
        raise FeedError("authentication_required")
    if status != 200:
        raise FeedError("interface_response_error")


def summarize_trends(payload, day, received):
    data = payload.get("data") if isinstance(payload, dict) else None
    trends = data.get("trends") if isinstance(data, dict) else None
    if not isinstance(trends, list) or not trends or len(trends) > MAX_BARS:
        raise FeedError("interface_response_error")
    parsed = []
    for raw in trends:
        if not isinstance(raw, str) or len(raw) > 512:
            raise FeedError("interface_response_error")
        fields = raw.split(",")
        if len(fields) != 8:
            raise FeedError("interface_response_error")
        stamp = minute_time(fields[0])
        try:
            values = [float(v) for v in fields[1:]]
        except (ValueError, TypeError):
            raise FeedError("interface_response_error") from None
        if any(not math.isfinite(v) or v < 0 for v in values) or values[1] <= 0:
            raise FeedError("interface_response_error")
        parsed.append((fields[0], stamp, values))
    selected = [r for r in parsed if r[1].date().isoformat() == day]
    stamps = [r[1] for r in selected]
    stamp_set = set(stamps)
    unique = sorted(stamp_set)
    # 只判断观察到的首尾之间；不把午休当缺根，不猜交易日/停牌状态。
    missing = 0
    if unique:
        expected = unique[0] + timedelta(minutes=1)
        while expected < unique[-1]:
            minute = expected.hour * 60 + expected.minute
            if (570 <= minute <= 690 or 780 <= minute <= 900) and expected not in stamp_set:
                missing += 1
            expected += timedelta(minutes=1)
    latest = max(stamps) if stamps else None
    tail = latest >= received.astimezone(BEIJING).replace(second=0, microsecond=0) - timedelta(minutes=1) if latest else None
    return {
        "upstream_returned_bars": len(parsed), "returned_bars": len(selected),
        "upstream_first_time_raw": parsed[0][0], "upstream_last_time_raw": parsed[-1][0],
        "upstream_first_time_beijing": parsed[0][1].isoformat(),
        "upstream_last_time_beijing": parsed[-1][1].isoformat(),
        "first_time_raw": selected[0][0] if selected else None,
        "last_time_raw": selected[-1][0] if selected else None,
        "first_time_beijing": stamps[0].isoformat() if stamps else None,
        "last_time_beijing": stamps[-1].isoformat() if stamps else None,
        "source_time_raw": selected[-1][0] if selected else None,
        "source_time_beijing": stamps[-1].isoformat() if stamps else None,
        "duplicates": len(stamps) - len(unique),
        "out_of_order": any(b < a for a, b in zip(stamps, stamps[1:])),
        "missing_minutes_between_observed": missing,
        "missing_scope": "仅首尾之间连续竞价分钟；午休排除；边缘、停牌与无成交口径未核实",
        "tail_suspected_incomplete": tail,
        "tail_rule": "最大时间 >= 接收北京时间整分钟减1分钟；起止标签语义未核实，保守疑似",
        "recent_bars": [dict(time_raw=r[0], time_beijing=r[1].isoformat(),
                             **dict(zip(("open", "close", "high", "low", "volume", "amount", "average"), r[2])))
                        for r in sorted(selected, key=lambda r: r[1])[-SUMMARY_LIMIT:]],
    }


def worker(symbol, timeout):
    """隔离进程内调用指定 AKShare 函数；HTTP 替身仅约束传输，不换端点。"""
    record = {"akshare_version": None, "interface": INTERFACE, "symbol": symbol,
              "period": "1", "adjust": "", "returned_fields": [],
              "source_time_raw": None, "source_time_beijing": None,
              "source_timezone": "未核实；无偏移字符串暂按 Asia/Shanghai 解释",
              "source_time_meaning": "分钟开始/结束标签未核实；不是逐笔撮合时间",
              "units": {"price": "元/股（待广发核实）", "volume": "上游原值，疑似手（未核实，不换算）", "amount": "疑似元（未核实）"},
              "error_category": None, "http_attempts": 0,
              "request_started_at_local": None}
    started = time.monotonic()
    try:
        validate_options([symbol], 1, 60, timeout)
        symbol = normalize_symbol(symbol)
        record["symbol"] = symbol
        record["akshare_version"] = importlib.metadata.version("akshare")
        if record["akshare_version"] != "1.18.97":
            raise FeedError("unsupported_dependency_version")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            import requests
            from akshare.stock_feature import stock_hist_em
            day = datetime.now(BEIJING).date().isoformat()
            record["requested_day_beijing"] = day
            summary = {}

            def guarded_get(url, **kwargs):
                if record["http_attempts"] or url != URL or kwargs.get("timeout") != 15:
                    raise FeedError("request_budget_violation")
                params = kwargs.get("params", {})
                if params.get("secid") != ("1." if symbol.startswith("SH") else "0.") + symbol[2:]:
                    raise FeedError("interface_response_error")
                record["http_attempts"] += 1
                record["request_started_at_local"] = datetime.now().astimezone().isoformat()
                with requests.Session() as session:
                    session.trust_env = False
                    session.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))
                    request_started = time.monotonic()
                    try:
                        response = session.get(url, params=params, timeout=15, allow_redirects=False)
                    except requests.Timeout:
                        raise FeedError("request_timeout") from None
                    except requests.RequestException:
                        raise FeedError("network_unreachable") from None
                    finally:
                        record["request_elapsed_seconds"] = round(time.monotonic() - request_started, 6)
                    received = datetime.now().astimezone()
                    record["received_at_local"] = received.isoformat()
                    classify_response(response.status_code, response.text)
                    try:
                        payload = response.json()
                    except ValueError:
                        raise FeedError("interface_response_error") from None
                    data = payload.get("data") if isinstance(payload, dict) else None
                    if not isinstance(data, dict) or str(data.get("code")) != symbol[2:]:
                        raise FeedError("interface_response_error")
                    summary.update(summarize_trends(payload, day, received))
                    record.update(summary)
                    return response

            # 仅覆盖此模块的 requests 绑定，不影响其他模块，也不使用默认 Session 凭据路径。
            import types
            transport = types.SimpleNamespace(get=guarded_get)
            with mock.patch.object(stock_hist_em, "requests", transport):
                frame = stock_hist_em.stock_zh_a_hist_min_em(
                    symbol=symbol[2:], start_date=day + " 00:00:00",
                    end_date=day + " 23:59:59", period="1", adjust="")
            record.update(summary)
            record["returned_fields"] = ["时间", "开盘", "收盘", "最高", "最低", "成交量", "成交额", "均价"]
            if not summary.get("returned_bars") or len(frame) != summary["returned_bars"]:
                raise FeedError("interface_response_error")
    except (importlib.metadata.PackageNotFoundError, ImportError):
        record["error_category"] = "missing_dependency"
    except FeedError as exc:
        record["error_category"] = exc.category
    except Exception:
        record["error_category"] = "interface_response_error"
    record.setdefault("received_at_local", datetime.now().astimezone().isoformat())
    record["elapsed_seconds"] = round(time.monotonic() - started, 6)
    return record


def isolated_request(symbol, timeout, fake=None):
    command = [sys.executable, "-B", str(Path(__file__).resolve()), "--worker", symbol,
               "--timeout", str(timeout), "--run"]
    if fake:
        command += ["--fake-worker", fake]
    started = time.monotonic()
    # 不继承任何凭据环境变量，也不把依赖错误输出写入日志。
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            env={"PYTHONDONTWRITEBYTECODE": "1"}, start_new_session=True)
    try:
        output, _ = proc.communicate(timeout=timeout + 15)
        result = json.loads(output) if proc.returncode == 0 else {"error_category": "worker_error"}
    except subprocess.TimeoutExpired:
        result = {"error_category": "request_timeout"}
    except (ValueError, UnicodeError):
        result = {"error_category": "worker_error"}
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        if proc.stdout:
            proc.stdout.close()
    # 超时/崩溃也有完整元数据；不会把接收时间充当源时间。
    try:
        version = importlib.metadata.version("akshare")
    except importlib.metadata.PackageNotFoundError:
        version = None
    base = {"interface": INTERFACE, "symbol": symbol,
            "akshare_version": version, "returned_fields": [],
            "source_time_raw": None, "source_time_beijing": None,
            "received_at_local": datetime.now().astimezone().isoformat(),
            "elapsed_seconds": round(time.monotonic() - started, 6)}
    base.update(result)
    return base


def validate_options(symbols, count, interval, timeout):
    if len(symbols) != 1:
        raise ValueError("只允许 1 只股票")
    normalize_symbol(symbols[0])
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= MAX_REQUESTS:
        raise ValueError("总请求数必须为 1–5")
    if not math.isfinite(interval) or interval < 60:
        raise ValueError("请求开始间隔至少 60 秒")
    if timeout != 15:
        raise ValueError("单次 timeout 固定为 15 秒")


def finite_run(symbols, count, interval, timeout, request=isolated_request,
               clock=time.monotonic, sleep=time.sleep, save=lambda row: None):
    validate_options(symbols, count, interval, timeout)
    records = []
    for index in range(count):  # 有界验证批次，无持续轮询、自动重试或换源。
        if index:
            deadline = clock() + interval  # 从完成起等待，保守满足开始间隔。
            sleep(max(0, deadline - clock()))
            if clock() < deadline:
                raise RuntimeError("时钟无法满足限速，安全停止")
        row = request(normalize_symbol(symbols[0]), timeout)
        row["request_number"] = index + 1
        save(row)
        records.append(row)
        if row.get("error_category"):
            break
    return records


def secure_output(root):
    """逐层拒绝符号链接；不修改任何已有目录或文件的权限。"""
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for name in ("data", "market_monitor", "validation"):
            try:
                os.mkdir(name, 0o700, dir_fd=fd)
            except FileExistsError:
                pass
            next_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
            if name in ("market_monitor", "validation") and stat.S_IMODE(os.fstat(fd).st_mode) != 0o700:
                raise ValueError("已有输出目录权限不是 700；停止且不修改")
        filename = "validation_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ") + ".jsonl"
        out_fd = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        return os.fdopen(out_fd, "w", encoding="utf-8"), filename
    finally:
        os.close(fd)


def conclusion(records):
    if not records or not any(not r.get("error_category") for r in records):
        return "尚未真实验证（没有成功样本）"
    return "有限样本已记录，真实性、时区与单位仍待人工核实"


def self_test():
    """不导入真实行情依赖、不联网；文件测试仅使用临时目录。"""
    import types

    class OfflineTests(unittest.TestCase):
        def payload(self, times=None):
            times = times or ["2026-10-21 09:30", "2026-10-21 09:31"]
            return {"data": {"code": "600000", "trends": [
                t + ",10,10.1,10.2,9.9,100,101000,10.1" for t in times]}}

        def summary(self, times=None):
            return summarize_trends(self.payload(times), "2026-10-21",
                                    datetime(2026, 10, 21, 9, 32, tzinfo=BEIJING))

        def test_parse_and_bounded_summary(self):
            row = self.summary()
            self.assertEqual(row["returned_bars"], 2)
            self.assertEqual(row["first_time_raw"], "2026-10-21 09:30")
            self.assertEqual(row["last_time_beijing"], "2026-10-21T09:31:00+08:00")
            self.assertEqual(row["recent_bars"][-1]["close"], 10.1)
            self.assertEqual(row["recent_bars"][-1]["volume"], 100)
            self.assertTrue(row["tail_suspected_incomplete"])
            self.assertEqual(row["duplicates"], 0)
            self.assertFalse(row["out_of_order"])
            self.assertEqual(row["missing_minutes_between_observed"], 0)
            times = ["2026-10-20 14:59"] + [f"2026-10-21 09:{m}" for m in range(30, 40)]
            row = self.summary(times)
            self.assertEqual(row["upstream_returned_bars"], 11)
            self.assertEqual(row["returned_bars"], 10)
            self.assertEqual(len(row["recent_bars"]), SUMMARY_LIMIT)
            for bad in ({}, {"data": {"trends": []}},
                        self.payload(["invalid"]), self.payload(["2026-02-30 09:30"])):
                with self.assertRaises(FeedError):
                    summarize_trends(bad, "2026-10-21", datetime.now(BEIJING))
            for value in ("NaN", "inf", "-1", "bad"):
                bad = self.payload()
                bad["data"]["trends"][0] = bad["data"]["trends"][0].replace(",10.1,", "," + value + ",", 1)
                with self.assertRaises(FeedError):
                    summarize_trends(bad, "2026-10-21", datetime.now(BEIJING))

        def test_timezone(self):
            for raw, local in (("2026-07-01 09:30", "2026-06-30T18:30:00-07:00"),
                               ("2026-12-01 09:30", "2026-11-30T17:30:00-08:00")):
                stamp = minute_time(raw)
                self.assertEqual(stamp.astimezone(ZoneInfo("America/Los_Angeles")).isoformat(), local)
                self.assertEqual(minute_time(raw).isoformat()[-6:], "+08:00")
            self.assertEqual(beijing_timestamp(1782869400000), "2026-07-01T09:30:00+08:00")
            for bad in (True, float("nan")):
                with self.assertRaises(FeedError):
                    beijing_timestamp(bad)

        def test_gaps_duplicates_order_and_tail(self):
            row = self.summary(["2026-10-21 09:33", "2026-10-21 09:30", "2026-10-21 09:30"])
            self.assertEqual(row["duplicates"], 1)
            self.assertTrue(row["out_of_order"])
            self.assertEqual(row["missing_minutes_between_observed"], 2)
            row = self.summary(["2026-10-21 11:30", "2026-10-21 13:00"])
            self.assertEqual(row["missing_minutes_between_observed"], 0)
            row = summarize_trends(self.payload(), "2026-10-21",
                                   datetime(2026, 10, 21, 10, 0, tzinfo=BEIJING))
            self.assertFalse(row["tail_suspected_incomplete"])

        def test_bounds_single_symbol_and_interval(self):
            for symbols, count, interval, timeout in (
                ([], 3, 60, 15), (["600000", "000001"], 3, 60, 15),
                (["SH000001"], 3, 60, 15), (["600000"], 6, 60, 15),
                (["600000"], 0, 60, 15), (["600000"], True, 60, 15),
                (["600000"], 3, 59.9, 15), (["600000"], 3, float("nan"), 15),
                (["600000"], 3, float("inf"), 15), (["600000"], 3, 60, 16)):
                request = mock.Mock()
                with self.assertRaises(ValueError):
                    finite_run(symbols, count, interval, timeout, request=request)
                request.assert_not_called()
            now, starts = [0.0], []

            def request(symbol, timeout):
                starts.append(now[0])
                self.assertEqual((symbol, timeout), ("SH600000", 15))
                now[0] += 2
                return {"error_category": None}

            def sleep(seconds):
                now[0] += seconds

            rows = finite_run(["600000"], 5, 60, 15, request, lambda: now[0], sleep)
            self.assertEqual(len(rows), MAX_REQUESTS)
            self.assertTrue(all(b - a >= 60 for a, b in zip(starts, starts[1:])))
            with self.assertRaises(RuntimeError):
                finite_run(["600000"], 2, 60, 15, request, lambda: now[0], lambda _: None)

        def test_classification_and_immediate_stop(self):
            for status, body, category in (
                (403, "", "authentication_required"), (401, "", "authentication_required"),
                (429, "", "rate_limited"), (200, "验证码", "authentication_required"),
                (200, "banned", "authentication_required"), (200, "login", "authentication_required"),
                (200, "rate limit", "rate_limited"), (500, "", "interface_response_error")):
                with self.assertRaises(FeedError) as caught:
                    classify_response(status, body)
                self.assertEqual(caught.exception.category, category)
                request = mock.Mock(return_value={"error_category": category})
                rows = finite_run(["600000"], 5, 60, 15, request, sleep=mock.Mock())
                self.assertEqual(len(rows), 1)
                request.assert_called_once()

        def fake_adapter(self, status=200, body="", error=None, twice=False, other_url=False):
            payload = self.payload([datetime.now(BEIJING).strftime("%Y-%m-%d") + " 09:30"])
            response = types.SimpleNamespace(status_code=status, text=body, json=lambda: payload)
            session = mock.MagicMock()
            session.__enter__.return_value = session
            session.get.return_value = response
            class RequestException(Exception):
                pass
            class Timeout(RequestException):
                pass
            if error:
                error_type = {"network": RequestException, "timeout": Timeout}.get(error, OSError)
                session.get.side_effect = error_type("sensitive upstream body")
            requests = types.ModuleType("requests")
            requests.Session = mock.Mock(return_value=session)
            requests.Timeout, requests.RequestException = Timeout, RequestException
            requests.adapters = types.SimpleNamespace(HTTPAdapter=mock.Mock())
            module = types.ModuleType("akshare.stock_feature.stock_hist_em")
            module.requests = requests
            calls = []

            def fake_function(**kwargs):
                calls.append(kwargs)
                day = datetime.now(BEIJING).date().isoformat()
                self.assertEqual(kwargs, dict(symbol="600000", start_date=day + " 00:00:00",
                                             end_date=day + " 23:59:59", period="1", adjust=""))
                for _ in range(2 if twice else 1):
                    module.requests.get(URL + ("/other" if other_url else ""), timeout=15,
                                        params={"secid": "1.600000"})
                return [1]

            module.stock_zh_a_hist_min_em = fake_function
            package = types.ModuleType("akshare")
            feature = types.ModuleType("akshare.stock_feature")
            feature.stock_hist_em = module
            package.stock_feature = feature
            modules = {"requests": requests, "akshare": package, "akshare.stock_feature": feature,
                       "akshare.stock_feature.stock_hist_em": module}
            with mock.patch.dict(sys.modules, modules), mock.patch.object(
                    importlib.metadata, "version", return_value="1.18.97"):
                row = worker("600000", 15)
            return row, session, requests, calls

        def test_anonymous_adapter_with_fake_http(self):
            row, session, requests, calls = self.fake_adapter()
            self.assertIsNone(row["error_category"])
            self.assertEqual(row["http_attempts"], 1)
            self.assertEqual(len(calls), 1)
            self.assertEqual(row["returned_bars"], 1)
            self.assertIn("未核实", row["source_timezone"])
            self.assertIsNotNone(datetime.fromisoformat(row["received_at_local"]).tzinfo)
            self.assertIsNotNone(datetime.fromisoformat(row["request_started_at_local"]).tzinfo)
            self.assertFalse(session.trust_env)
            self.assertEqual(session.get.call_args.kwargs["timeout"], 15)
            self.assertFalse(session.get.call_args.kwargs["allow_redirects"])
            self.assertEqual(set(session.get.call_args.kwargs), {"params", "timeout", "allow_redirects"})
            requests.adapters.HTTPAdapter.assert_called_once_with(max_retries=0)
            for kwargs, expected, attempts in (
                ({"twice": True}, "request_budget_violation", 1),
                ({"other_url": True}, "request_budget_violation", 0),
                ({"status": 429}, "rate_limited", 1),
                ({"status": 403, "body": "private credential"}, "authentication_required", 1),
                ({"body": "captcha"}, "authentication_required", 1),
                ({"error": "network"}, "network_unreachable", 1),
                ({"error": "timeout"}, "request_timeout", 1),
                ({"error": "unexpected"}, "interface_response_error", 1)):
                row, session, _, _ = self.fake_adapter(**kwargs)
                self.assertEqual(row["error_category"], expected)
                self.assertEqual(session.get.call_count, attempts)
                self.assertNotIn("private credential", json.dumps(row))
                self.assertNotIn("sensitive upstream body", json.dumps(row))

        def test_missing_dependency(self):
            with mock.patch.object(importlib.metadata, "version",
                                   side_effect=importlib.metadata.PackageNotFoundError):
                self.assertEqual(worker("600000", 15)["error_category"], "missing_dependency")

        def test_default_off_and_cli_bounds(self):
            with mock.patch.object(sys, "argv", ["validate_market_feed.py"]), \
                    mock.patch.object(sys.modules[__name__], "secure_output") as output, \
                    mock.patch.object(importlib.metadata, "version") as version, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(), 0)
                output.assert_not_called()
                version.assert_not_called()
            for options in (("--count", "6"), ("--interval", "59"),
                            ("--symbols", "600000", "000001"), ("--timeout", "14")):
                with mock.patch.object(sys, "argv", ["validate_market_feed.py", "--run", *options]), \
                        mock.patch.object(sys.modules[__name__], "secure_output") as output, \
                        mock.patch.object(importlib.metadata, "version") as version, \
                        contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(main(), 1)
                    output.assert_not_called()
                    version.assert_not_called()

        def test_isolated_fake_and_reaping(self):
            real_popen, children = subprocess.Popen, []

            def capture(*args, **kwargs):
                proc = real_popen(*args, **kwargs)
                children.append(proc)
                return proc

            with mock.patch.object(subprocess, "Popen", side_effect=capture):
                for fake, category in (("success", None), ("auth", "authentication_required")):
                    row = isolated_request("SH600000", 15, fake)
                    self.assertEqual(row["error_category"], category)
                # 参数固定 15；仅测试替换 communicate 的等待时间，快速验证超时回收。
                def hanging(*args, **kwargs):
                    proc = capture(*args, **kwargs)
                    communicate = proc.communicate
                    proc.communicate = lambda timeout: communicate(timeout=0.05)
                    return proc
                with mock.patch.object(subprocess, "Popen", side_effect=hanging):
                    self.assertEqual(isolated_request("SH600000", 15, "hang")["error_category"],
                                     "request_timeout")
            for proc in children:
                self.assertIsNotNone(proc.returncode)
                with self.assertRaises(ProcessLookupError):
                    os.kill(proc.pid, 0)

        def test_permissions_and_exclusive_create(self):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                with mock.patch.object(sys.modules[__name__], "datetime") as date:
                    date.now.return_value.strftime.return_value = "fixed"
                    out, name = secure_output(root)
                    with out:
                        out.write("offline\n")
                    with self.assertRaises(FileExistsError):
                        secure_output(root)
                folder = root / "data/market_monitor/validation"
                self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE((folder / name).stat().st_mode), 0o600)
                folder.chmod(0o755)
                with self.assertRaises(ValueError):
                    secure_output(root)
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "data").symlink_to(root, target_is_directory=True)
                with self.assertRaises(OSError):
                    secure_output(root)

    suite = unittest.defaultTestLoader.loadTestsFromTestCase(OfflineTests)
    # 防止未来自测误接真实网络；隔离子进程测试仅运行内置假 worker。
    with mock.patch("socket.socket.connect", side_effect=AssertionError("离线自测禁止网络")):
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="显式开启本次有限匿名请求；默认不联网、不写文件")
    parser.add_argument("--self-test", action="store_true", help="离线假数据自测，仅使用临时目录")
    parser.add_argument("--symbols", nargs="+", default=list(SAMPLES))
    parser.add_argument("--count", type=int, default=3, help="总请求数，硬上限 5")
    parser.add_argument("--interval", type=float, default=60)
    parser.add_argument("--timeout", type=float, default=15)
    parser.add_argument("--worker", help=argparse.SUPPRESS)
    parser.add_argument("--fake-worker", choices=("success", "auth", "hang"), help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        validate_options([args.worker], 1, 60, args.timeout)
        if not args.run and not args.fake_worker:
            parser.error("子进程联网也必须显式 --run")
        if args.fake_worker:
            if args.fake_worker == "hang":
                time.sleep(60)
            row = {"error_category": "authentication_required" if args.fake_worker == "auth" else None,
                   "source_time_beijing": beijing_timestamp(1782869400000)}
        else:
            row = worker(args.worker, args.timeout)
        print(json.dumps(row, ensure_ascii=False, allow_nan=False))
        return 0
    if args.self_test:
        return self_test()
    if not args.run:
        print("验证默认关闭；--self-test 离线自测；所有者可在交易时段用 --run 开启本次有限验证。")
        return 0
    try:
        validate_options(args.symbols, args.count, args.interval, args.timeout)
        importlib.metadata.version("akshare")
        out, filename = secure_output(ROOT)
        with out:
            def save(row):
                out.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
                out.flush()
                os.fsync(out.fileno())
            records = finite_run(args.symbols, args.count, args.interval, args.timeout, save=save)
        print(f"{conclusion(records)}；实际请求尝试 {len(records)} 次；记录 data/market_monitor/validation/{filename}")
        print("时间语义与时效仍需所有者交易时段核对；单次成功不代表已通过实时验收。")
        return 0 if not any(r.get("error_category") for r in records) else 1
    except (ValueError, OSError, importlib.metadata.PackageNotFoundError):
        print("当前不可用：参数、依赖或安全输出目录检查失败；未输出上游异常详情。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
