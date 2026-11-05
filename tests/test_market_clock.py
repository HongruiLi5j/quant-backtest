"""E06：src/market_clock.py（北京时间 15:30 前不用当天 K 线）的测试。

三部分：
  1. MarketClockUnit：函数本身的边界（15:29:59 / 15:30:00 / 15:31、周末、节假日、跨日、跨时区、美国夏令时、日历缺失）。
  2. OldLogicEquivalence / FileSiteEquivalence：把各调用点「改之前」的写法逐字抄成对照答案（oracle），
     在一批时间点上和现在的调用点输出比对，证明替换没改变行为。
  3. SingleSourceGuard：src/ 和 scripts/ 里不能再出现 15:30 的字面量（防止规则又被抄一份）。

全部用临时目录和合成数据；不联网，不读写 data/。
运行：.venv/bin/python -B tests/test_market_clock.py
"""
import importlib.util
import json
import os
import re
import sys
import tempfile
import unittest
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from src import allmarket, market_clock, resultcache, simtrade, trade_list, trade_signals, turnover, turnover_backfill  # noqa: E402
from src.dataquality.report import scan_dir  # noqa: E402
from src.market_monitor import eod_analysis, signals_readonly  # noqa: E402

BJ = ZoneInfo("Asia/Shanghai")
UTC = timezone.utc
NY = ZoneInfo("America/New_York")


def bj(y, mo, d, h=0, mi=0, s=0, us=0):
    return datetime(y, mo, d, h, mi, s, us, tzinfo=BJ)


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# 2026-03-04（周三）当假节假日：日历里没有它，用来验证「节假日自动往前找」
CAL = [d.date() for d in pd.bdate_range("2026-02-02", "2026-03-31") if d.date() != date(2026, 3, 4)]


# ---- 改之前各处的写法（对照答案）。每个函数对应一种旧写法，参数就是旧代码里的变量 ----
def old_ready_minutes(now):          # allmarket / simtrade / pipeline_check / resultcache / trade_signals 的写法
    return now.hour * 60 + now.minute >= 15 * 60 + 30


def old_ready_time(now):             # trade_list / turnover / eod_analysis / signal_backfill / run_factor_scan 的写法
    return not (now.time() < time(15, 30))


def old_ready_tuple(now):            # turnover_backfill / dataquality / fundamental 脚本的写法
    return not ((now.hour, now.minute) < (15, 30))


def old_cutoff(now):
    return now.date() if old_ready_minutes(now) else now.date() - timedelta(days=1)


def minute_grid(days):
    """给定若干天，每天每分钟一个时间点，外加每个 15:30 边界附近的秒级时间点。"""
    out = [bj(d.year, d.month, d.day, h, m) for d in days for h in range(24) for m in range(60)]
    for d in days:
        for h, m, s, us in [(15, 29, 59, 0), (15, 29, 59, 999999), (15, 30, 0, 0), (15, 30, 0, 1), (15, 30, 59, 0), (0, 0, 0, 0), (23, 59, 59, 999999)]:
            out.append(bj(d.year, d.month, d.day, h, m, s, us))
    return out


WEEK = [date(2026, 3, d) for d in (5, 6, 7, 8, 9)]            # 周四、周五、周六、周日、周一
COARSE = [bj(2026, 3, 6, h, m, s) for h, m, s in
          [(0, 0, 0), (9, 30, 0), (10, 0, 0), (15, 0, 0), (15, 29, 59), (15, 30, 0), (15, 30, 1), (15, 31, 0), (16, 0, 0), (23, 59, 59)]]


# =====================================================================================
class MarketClockUnit(unittest.TestCase):
    def test_boundary_15_29_15_30_15_31(self):
        f = market_clock.last_usable_bar_date
        self.assertEqual(f(bj(2026, 3, 6, 15, 29, 59)), date(2026, 3, 5))        # 15:29:59 → 昨天
        self.assertEqual(f(bj(2026, 3, 6, 15, 29, 59, 999999)), date(2026, 3, 5))
        self.assertEqual(f(bj(2026, 3, 6, 15, 30, 0)), date(2026, 3, 6))         # 15:30:00 整 → 今天
        self.assertEqual(f(bj(2026, 3, 6, 15, 31)), date(2026, 3, 6))
        self.assertEqual(f(bj(2026, 3, 6, 15, 29)), date(2026, 3, 5))
        self.assertFalse(market_clock.is_close_ready(bj(2026, 3, 6, 15, 29, 59)))
        self.assertTrue(market_clock.is_close_ready(bj(2026, 3, 6, 15, 30, 0)))
        self.assertTrue(market_clock.is_close_ready(bj(2026, 3, 6, 15, 31)))

    def test_weekend_without_calendar_is_pure_time_rule(self):
        f = market_clock.last_usable_bar_date
        self.assertEqual(f(bj(2026, 3, 7, 10, 0)), date(2026, 3, 6))             # 周六早上 → 周五
        self.assertEqual(f(bj(2026, 3, 7, 16, 0)), date(2026, 3, 7))             # 周六下午：不带日历只看钟点 → 周六本身（没有这根 K 线，过滤时自然没数据）
        self.assertEqual(f(bj(2026, 3, 9, 10, 0)), date(2026, 3, 8))             # 周一早上 → 周日（同上）

    def test_weekend_with_calendar_goes_back_to_friday(self):
        f = lambda now: market_clock.last_usable_bar_date(now, CAL)
        for now in (bj(2026, 3, 7, 10, 0), bj(2026, 3, 7, 16, 0), bj(2026, 3, 8, 16, 0), bj(2026, 3, 9, 10, 0), bj(2026, 3, 9, 15, 29, 59)):
            self.assertEqual(f(now), date(2026, 3, 6), now)
        self.assertEqual(f(bj(2026, 3, 9, 15, 30)), date(2026, 3, 9))            # 周一 15:30 起算周一

    def test_holiday_with_calendar(self):
        f = lambda now: market_clock.last_usable_bar_date(now, CAL)
        self.assertEqual(f(bj(2026, 3, 4, 20, 0)), date(2026, 3, 3))             # 3/4 不在日历里
        self.assertEqual(f(bj(2026, 3, 5, 10, 0)), date(2026, 3, 3))             # 周四早上：昨天是假日 → 周二

    def test_cross_midnight(self):
        f = market_clock.last_usable_bar_date
        self.assertEqual(f(bj(2026, 3, 6, 23, 59, 59)), date(2026, 3, 6))
        self.assertEqual(f(bj(2026, 3, 7, 0, 0, 0)), date(2026, 3, 6))           # 跨日后日期变了，但仍是上一根
        self.assertEqual(f(bj(2026, 3, 5, 23, 59, 59)), date(2026, 3, 5))
        self.assertEqual(f(bj(2026, 3, 6, 0, 0, 0)), date(2026, 3, 5))

    def test_timezone_conversion_and_us_dst(self):
        f = market_clock.last_usable_bar_date
        # UTC 07:30 = 北京 15:30
        self.assertEqual(f(datetime(2026, 3, 6, 7, 29, 59, tzinfo=UTC)), date(2026, 3, 5))
        self.assertEqual(f(datetime(2026, 3, 6, 7, 30, 0, tzinfo=UTC)), date(2026, 3, 6))
        self.assertEqual(f(datetime(2026, 3, 6, 16, 0, tzinfo=UTC)), date(2026, 3, 6))   # = 北京周六 00:00，仍取周五
        # 纽约夏令时（EDT, UTC-4）与冬令时（EST, UTC-5）：同一个北京 15:30 对应不同的纽约墙上时间，结果只看北京时间
        self.assertFalse(market_clock.is_close_ready(datetime(2026, 7, 10, 3, 29, 59, tzinfo=NY)))     # 07:29:59Z
        self.assertTrue(market_clock.is_close_ready(datetime(2026, 7, 10, 3, 30, 0, tzinfo=NY)))       # 07:30:00Z
        self.assertFalse(market_clock.is_close_ready(datetime(2026, 1, 9, 2, 29, 59, tzinfo=NY)))      # 07:29:59Z
        self.assertTrue(market_clock.is_close_ready(datetime(2026, 1, 9, 2, 30, 0, tzinfo=NY)))        # 07:30:00Z
        # 美国夏令时切换日（2026-03-08）当天：纽约 03:30 EDT = 07:30Z = 北京 15:30
        self.assertTrue(market_clock.is_close_ready(datetime(2026, 3, 8, 3, 30, tzinfo=NY)))
        self.assertFalse(market_clock.is_close_ready(datetime(2026, 3, 8, 3, 29, 59, tzinfo=NY)))
        # 北京 15:30 在任何一天、任何夏令时状态下都一样：用 UTC 偏移扫一遍全年
        for day in pd.date_range("2026-01-01", "2026-12-31", freq="7D"):
            utc_ready = datetime(day.year, day.month, day.day, 7, 30, tzinfo=UTC).astimezone(NY)
            utc_not = (datetime(day.year, day.month, day.day, 7, 30, tzinfo=UTC) - timedelta(seconds=1)).astimezone(NY)
            self.assertTrue(market_clock.is_close_ready(utc_ready), day)
            self.assertFalse(market_clock.is_close_ready(utc_not), day)

    def test_naive_datetime_is_beijing_wall_clock(self):
        self.assertTrue(market_clock.is_close_ready(datetime(2026, 3, 6, 15, 30)))
        self.assertFalse(market_clock.is_close_ready(datetime(2026, 3, 6, 15, 29, 59)))
        self.assertEqual(market_clock.last_usable_bar_date(datetime(2026, 3, 6, 10, 0)), date(2026, 3, 5))

    def test_now_none_uses_current_time_in_beijing(self):
        self.assertEqual(market_clock.to_beijing().utcoffset(), timedelta(hours=8))
        fixed = bj(2026, 3, 6, 15, 30)
        with mock.patch.object(market_clock, "datetime", wraps=datetime) as dt:
            dt.now.return_value = fixed
            self.assertEqual(market_clock.last_usable_bar_date(), date(2026, 3, 6))

    def test_calendar_missing_raises_like_max_of_empty(self):
        with self.assertRaises(ValueError):
            market_clock.last_usable_bar_date(bj(2026, 3, 6, 16, 0), [])
        with self.assertRaises(ValueError):
            market_clock.last_usable_bar_date(bj(2026, 3, 6, 16, 0), [date(2026, 3, 7)])         # 日历全在截止日之后

    def test_bar_usable(self):
        now_before, now_after = bj(2026, 3, 6, 15, 29, 59), bj(2026, 3, 6, 15, 30)
        for day in (date(2026, 3, 6), "2026-03-06", pd.Timestamp("2026-03-06"), datetime(2026, 3, 6)):
            self.assertFalse(market_clock.bar_usable(day, now_before), repr(day))
            self.assertTrue(market_clock.bar_usable(day, now_after), repr(day))
        self.assertTrue(market_clock.bar_usable(date(2026, 3, 5), now_before))                    # 昨天的随时可用
        self.assertFalse(market_clock.bar_usable(date(2026, 3, 7), now_after))                    # 明天的永远不可用
        self.assertFalse(market_clock.bar_usable("2026-03-07", now_after))

    def test_close_ready_at_and_clock_state(self):
        t = market_clock.close_ready_at(date(2026, 3, 6))
        self.assertEqual(t.isoformat(), "2026-03-06T15:30:00+08:00")
        self.assertEqual(market_clock.clock_state(bj(2026, 3, 6, 15, 29)), ("2026-03-06", False))
        self.assertEqual(market_clock.clock_state(bj(2026, 3, 6, 15, 30)), ("2026-03-06", True))
        self.assertEqual(market_clock.CLOSE_READY, time(15, 30))


# =====================================================================================
class OldLogicEquivalence(unittest.TestCase):
    """调用点的输出 == 改之前写法的输出（纯函数类的调用点，不碰文件）。"""

    def test_allmarket_last_trading_day(self):
        for now in minute_grid(WEEK):
            expected = max(d for d in CAL if d <= old_cutoff(now))
            self.assertEqual(allmarket.last_trading_day(now, CAL), expected, now)

    def test_simtrade_expected_day(self):
        for now in minute_grid(WEEK):
            cutoff = old_cutoff(now)
            expected = max(d for d in CAL if d <= cutoff).isoformat()
            self.assertEqual(simtrade.expected_day(CAL, now), expected, now)
        # 日历不够新：旧代码用截止日判断，新代码必须同样拒绝
        with self.assertRaises(simtrade.StaleListError):
            simtrade.expected_day(CAL[:2], bj(2026, 3, 6, 16, 0))
        with self.assertRaises(simtrade.StaleListError):
            simtrade.expected_day([], bj(2026, 3, 6, 16, 0))

    def test_resultcache_and_trade_signals_clock_state(self):
        for now in minute_grid(WEEK):
            old = (str(now.date()), old_ready_minutes(now))
            self.assertEqual(resultcache.bj_clock_state(now), old, now)
            with mock.patch.object(allmarket, "_now_bj", return_value=now):
                self.assertEqual(trade_signals._clock_state(), old, now)

    def test_trade_signals_recording_window_start(self):
        for d in WEEK:
            with tempfile.TemporaryDirectory() as t:
                start, end = trade_signals.recording_window(d.isoformat(), Path(t))
            self.assertEqual(start, datetime.combine(d, time(15, 30), ZoneInfo("Asia/Shanghai")))
            self.assertEqual(start.utcoffset(), timedelta(hours=8))

    def test_trade_list_ready_day(self):
        with mock.patch.object(allmarket, "trade_calendar", return_value=CAL):
            for now in minute_grid(WEEK):
                day = old_cutoff(now)
                self.assertEqual(trade_list.ready_day(now), max(d for d in CAL if d <= day).isoformat(), now)
        with mock.patch.object(allmarket, "trade_calendar", side_effect=RuntimeError("日历不可用")):
            for now in minute_grid(WEEK):                      # 日历失败 → 旧写法退回「往前找工作日」
                day = old_cutoff(now)
                while day.weekday() >= 5:
                    day -= timedelta(days=1)
                self.assertEqual(trade_list.ready_day(now), day.isoformat(), now)

    def test_trade_list_check_release(self):
        for now in minute_grid(WEEK):
            for release in (date(2026, 3, 5), date(2026, 3, 6), date(2026, 3, 7), date(2026, 3, 9)):
                day = release.isoformat()
                old_refuse = release > now.date() or (release == now.date() and now.time() < time(15, 30))
                progress, meta = {"complete": True, "trade_day": day}, {"data_date": day}
                if old_refuse:
                    with self.assertRaises(trade_list.PublishError) as cm:
                        trade_list.check_release(progress, meta, day, now)
                    self.assertIn("15:30", str(cm.exception))
                else:
                    trade_list.check_release(progress, meta, day, now)            # 不应抛

    def test_turnover_capture_snapshot_gate(self):
        class Reached(Exception):
            pass
        for now in minute_grid([date(2026, 3, 6)]):
            with mock.patch.object(turnover, "cached_calendar", side_effect=Reached):
                if old_ready_time(now):
                    with self.assertRaises(Reached):
                        turnover.capture_snapshot(None, "东方财富", now=now)          # 过了 15:30 才会去查日历
                else:
                    self.assertEqual(turnover.capture_snapshot(None, "东方财富", now=now), 0)

    def test_turnover_backfill_gate(self):
        for now in minute_grid([date(2026, 3, 5), date(2026, 3, 6)]):
            for release in (date(2026, 3, 5), date(2026, 3, 6), date(2026, 3, 7)):
                day = release.isoformat()
                old_refuse = day > now.date().isoformat() or (day == now.date().isoformat() and not old_ready_tuple(now))
                with tempfile.TemporaryDirectory() as t:
                    base = Path(t)
                    (base / "trade_list_cn.json").write_text(json.dumps({"data_date": day}))
                    with self.assertRaises(ValueError) as cm:
                        # 样本模式 + 空日历：没被「尚未收盘」拦下时，下一道关是「交易日历不足」
                        turnover_backfill.run(validation_samples=True, cache=base, now=now, calendar=[])
                self.assertEqual("尚未收盘" in str(cm.exception), old_refuse, (now, day))
                if not old_refuse:
                    self.assertIn("交易日历不足", str(cm.exception))

    def test_signals_readonly_generated_time_gate(self):
        day = date(2026, 3, 6)
        payload = {"version": 4, "market": "cn", "data_date": day.isoformat(),
                   "strategies": {"blend2": {"rows": [{"code": "600000", "action": "买入", "last_date": day.isoformat()}]}}}
        far_later = bj(2026, 3, 20, 12, 0)
        generated_cases = [bj(2026, 3, 5, 20, 0), bj(2026, 3, 6, 9, 0), bj(2026, 3, 6, 15, 29, 59), bj(2026, 3, 6, 15, 30), bj(2026, 3, 6, 15, 31),
                           bj(2026, 3, 6, 23, 59), bj(2026, 3, 7, 9, 0), bj(2026, 3, 9, 9, 0),
                           datetime(2026, 3, 6, 7, 29, 59, tzinfo=UTC), datetime(2026, 3, 6, 7, 30, tzinfo=UTC),
                           datetime(2026, 3, 6, 3, 30, tzinfo=NY), datetime(2026, 3, 5, 22, 30, tzinfo=NY)]
        with tempfile.TemporaryDirectory() as t:
            sig = Path(t).resolve() / "trade_list_cn.json"             # resolve：read_signals 逐层拒绝符号链接，macOS 的 /var 是链接
            for generated in generated_cases:
                payload["generated_at"] = generated.isoformat()
                sig.write_text(json.dumps(payload, ensure_ascii=False))
                g = generated.astimezone(BJ)
                old_empty = g.date() < day or (g.date() == day and g.time() < time(15, 30))
                with mock.patch.object(signals_readonly, "SIGNAL_FILE", sig):
                    res = signals_readonly.read_signals(("600000",), now=far_later)
                self.assertEqual(res["status"], "无信号" if old_empty else "有信号", generated)


# =====================================================================================
def write_panel(directory: Path, last=date(2026, 3, 6), codes=("600001", "600002", "600003"), n=40):
    """合成日线：每只 n 根工作日 K 线，最后一根是 last。"""
    directory.mkdir(parents=True, exist_ok=True)
    idx = pd.bdate_range(end=last, periods=n)
    rng = np.random.default_rng(7)
    for i, code in enumerate(codes):
        c = 10 * np.cumprod(1 + rng.normal(0, 0.005, n))
        pd.DataFrame({"Open": c, "High": c * 1.005, "Low": c * 0.995, "Close": c, "Volume": np.full(n, 1e6)}, index=idx).to_parquet(directory / f"{code}.parquet")


class FileSiteEquivalence(unittest.TestCase):
    """读文件的调用点：丢不丢当天那根 K 线，和旧写法一致。"""

    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.tmp = Path(self._t.name)
        self.addCleanup(self._t.cleanup)
        self.daily = self.tmp / "cn_all"
        write_panel(self.daily)
        self.today = pd.Timestamp("2026-03-06")
        self.yesterday = pd.Timestamp("2026-03-05")

    def expected_last(self, now):
        return self.today if old_ready_time(now) else self.yesterday

    def test_allmarket_drop_unfinished(self):
        df = pd.read_parquet(self.daily / "600001.parquet")
        future = pd.concat([df, df.iloc[[-1]].set_axis([pd.Timestamp("2026-03-09")])])         # 明天的 K 线任何时候都不能留
        for now in minute_grid([date(2026, 3, 6)]):
            with mock.patch.object(allmarket, "_now_bj", return_value=now):
                self.assertEqual(allmarket._drop_unfinished(future).index.max(), self.expected_last(now), now)
                self.assertTrue(allmarket._drop_unfinished(df.iloc[0:0]).empty)

    def test_allmarket_strip_intraday(self):
        saved_cases = [bj(2026, 3, 6, 9, 0), bj(2026, 3, 6, 15, 29, 59), bj(2026, 3, 6, 15, 30), bj(2026, 3, 6, 18, 0),
                       bj(2026, 3, 7, 9, 0), bj(2026, 3, 5, 20, 0), bj(2026, 3, 5, 15, 29)]
        for saved in saved_cases:
            with tempfile.TemporaryDirectory() as t:
                folder = Path(t) / "cn_all"
                write_panel(folder, codes=("600001",))
                os.utime(folder / "600001.parquet", (saved.timestamp(), saved.timestamp()))
                ready = pd.Timestamp(saved.date()) if saved.hour * 60 + saved.minute >= 15 * 60 + 30 else pd.Timestamp(saved.date()) - pd.Timedelta(days=1)
                before = pd.read_parquet(folder / "600001.parquet")
                with mock.patch.object(allmarket, "ALL", folder):
                    fixed = allmarket.strip_intraday()
                after = pd.read_parquet(folder / "600001.parquet")
                self.assertEqual(fixed, int((before.index > ready).any()), saved)
                self.assertEqual(list(after.index), [i for i in before.index if i <= ready], saved)

    def test_eod_load_panel(self):
        for now in COARSE + [bj(2026, 3, 6, 15, 29, 59, 999999)]:
            if old_ready_time(now):
                self.assertEqual(eod_analysis.load_panel(self.daily, "2026-03-06", now=now).close.index.max(), self.today, now)
            else:
                with self.assertRaises(eod_analysis.EodError) as cm:
                    eod_analysis.load_panel(self.daily, "2026-03-06", now=now)
                self.assertEqual(cm.exception.args[0], "no_bar_for_day", now)

    def test_dataquality_scan_dir_and_data_freshness(self):
        freshness = load_script("data_freshness")
        for now in COARSE + [datetime(2026, 3, 6, 7, 29, 59, tzinfo=UTC), datetime(2026, 3, 6, 7, 30, tzinfo=UTC)]:
            g = now.astimezone(BJ)
            old_drop = not old_ready_tuple(g)
            r1 = scan_dir(self.daily, today=date(2026, 3, 6), now=now)
            self.assertEqual(r1.intraday_dropped, old_drop, now)
            self.assertEqual(r1.intraday_dropped_count, 3 if old_drop else 0, now)
            r2 = freshness.scan(self.daily, today=date(2026, 3, 6), now=now)
            self.assertEqual(r2["intraday_dropped"], old_drop, now)
            self.assertEqual(r2["intraday_dropped_count"], 3 if old_drop else 0, now)
        # today 不是 now 的日期：不丢
        self.assertFalse(scan_dir(self.daily, today=date(2026, 3, 5), now=bj(2026, 3, 6, 10, 0)).intraday_dropped)

    def test_script_loaders(self):
        backfill, scan = load_script("signal_backfill"), load_script("run_factor_scan")
        for now in COARSE + [bj(2026, 3, 6, 15, 29, 59, 999999)]:
            _, close = backfill.load_open_close(self.daily, now)
            self.assertEqual(close.index.max(), self.expected_last(now), now)
            panel = scan.load_history(self.daily, pd.Timestamp("2026-03-06"), now)
            self.assertEqual(panel.close.index.max(), self.expected_last(now), now)

    def test_fundamental_scripts_gate(self):
        fetch, rank = load_script("fetch_fundamental"), load_script("run_fundamental_rank")
        today = date(2026, 3, 6)
        nows = COARSE + [datetime(2026, 3, 6, 15, 30), datetime(2026, 3, 6, 15, 29, 59), datetime(2026, 3, 6, 7, 29, 59, tzinfo=UTC),
                         datetime(2026, 3, 6, 7, 30, tzinfo=UTC)]
        for now in nows:
            bj_now = now.astimezone(timezone(timedelta(hours=8))) if now.tzinfo else now          # 旧代码：naive 当北京时间
            old_refuse = not old_ready_tuple(bj_now)
            for fn, asof in ((fetch.check_asof, today), (lambda a, n: rank.check_close_ready(pd.Timestamp(a), n), today)):
                if old_refuse:
                    with self.assertRaises(Exception) as cm:
                        fn(asof, now)
                    self.assertIn("15:30", str(cm.exception), now)
                else:
                    fn(asof, now)
                fn(today - timedelta(days=1), now)                       # 排名日是昨天：任何时刻都放行

    def test_pipeline_check_expected_day(self):
        pipeline = load_script("pipeline_check")

        class FakeDatetime(datetime):
            current = None

            @classmethod
            def now(cls, tz=None):
                return cls.current

        for now in minute_grid(WEEK):
            FakeDatetime.current = now
            with mock.patch.object(pipeline, "datetime", FakeDatetime), mock.patch.object(allmarket, "trade_calendar", return_value=CAL):
                self.assertEqual(pipeline.expected_day(), max(d for d in CAL if d <= old_cutoff(now)).isoformat(), now)


# =====================================================================================
class SingleSourceGuard(unittest.TestCase):
    """15:30 的规则只能在 src/market_clock.py 里写一遍；别处再出现字面量就是又抄了一份。"""

    LITERAL = re.compile(r"time\(15,\s*30\)|15\s*\*\s*60\s*\+\s*30|\(15,\s*30\)|\bCUTOFF_HOUR\b\s*,\s*CUTOFF_MINUTE\s*=\s*15")
    # replay_factor_check.py 属于 T008 回放实验链路，本任务有意没动，见 E06 报告「需要站主决定」
    ALLOWED = {"src/market_clock.py", "scripts/replay_factor_check.py"}

    def test_no_stray_1530_literals(self):
        hits = []
        for folder in ("src", "scripts"):
            for p in sorted((ROOT / folder).rglob("*.py")):
                rel = p.relative_to(ROOT).as_posix()
                if rel in self.ALLOWED:
                    continue
                for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                    code = line.split("#", 1)[0]                          # 注释里写「15:30」没关系，只查代码
                    if self.LITERAL.search(code):
                        hits.append(f"{rel}:{n}: {line.strip()}")
        self.assertEqual(hits, [], "发现 15:30 规则又被抄了一份，请改用 src/market_clock.py")

    def test_config_cn_close_ready_matches(self):
        """config.MARKETS['cn']['close_ready'] 是字符串配置（datasource.drop_incomplete_bar / daily.needs_update 读它），
        不便改成引用 market_clock；用这条测试保证两边数字不会悄悄分叉。"""
        from config import MARKETS
        hh, mm = map(int, MARKETS["cn"]["close_ready"].split(":"))
        self.assertEqual(time(hh, mm), market_clock.CLOSE_READY)
        self.assertEqual(MARKETS["cn"]["tz"], "Asia/Shanghai")


if __name__ == "__main__":
    unittest.main()
