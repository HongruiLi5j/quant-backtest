"""T015 S1：美股盯盘 core + YFinanceProvider。全程假 yfinance/假时钟，不联网，不读写 data/。"""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import socket
import sys
import unittest
from unittest.mock import patch

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.market_monitor.providers import FeedError
from src.us_market_monitor.core import (US_EASTERN, Quote, Condition, AlertState, alert_step,
                                        us_continuous_session, valid_symbol, validate_quote)
from src.us_market_monitor.providers import YFinanceProvider, classify

UTC = timezone.utc


def utc(*args):
    return datetime(*args, tzinfo=UTC)


def et(*args):
    return datetime(*args, tzinfo=US_EASTERN)


def quote(symbol='AAPL', price=101.0, at=False, received=None, volume=1000, **kw):
    at = et(2026, 10, 22, 10, 0) if at is False else at
    fields = dict(symbol=symbol, price=price, previous_close=100.0, opening=100.5, high=102.0,
                  low=99.5, volume=volume, timestamp=at, received_at=received or (at or et(2026, 10, 22, 10, 0)) + timedelta(seconds=30))
    fields.update(kw)
    return Quote(**fields)


class NoNetworkCase(unittest.TestCase):
    """任何真实套接字连接/DNS 都直接失败，保证测试不联网。"""
    def setUp(self):
        def refuse(*a, **k):
            raise AssertionError('测试中不得联网')
        for target in ('socket.socket.connect', 'socket.getaddrinfo', 'socket.create_connection'):
            patcher = patch(target, refuse)
            patcher.start()
            self.addCleanup(patcher.stop)


class SymbolTests(unittest.TestCase):
    def test_accept(self):
        for s in ('AAPL', 'BRK-B', 'BF-B', 'A', 'GOOGL'):
            self.assertTrue(valid_symbol(s), s)

    def test_reject(self):
        for s in ('BF.B', 'BRK.B', '^GSPC', 'ES=F', 'aapl', 'AAPL ', 'AAPL\n', '', 'TOOLONGX', 'AAPL-ABC',
                  'ＡＡＰＬ', None, 123, b'AAPL', 'sh600000'):
            self.assertFalse(valid_symbol(s), repr(s))


class SessionTests(unittest.TestCase):
    def test_monday_edt_boundaries(self):
        # 2026-10-19 周一，夏令时 EDT（UTC-4）：09:30 ET = 13:30 UTC
        self.assertTrue(us_continuous_session(utc(2026, 10, 19, 13, 30, 0)))
        self.assertFalse(us_continuous_session(utc(2026, 10, 19, 13, 29, 59)))
        self.assertTrue(us_continuous_session(utc(2026, 10, 19, 20, 0, 0)))
        self.assertFalse(us_continuous_session(utc(2026, 10, 19, 20, 0, 1)))

    def test_premarket_and_afterhours_excluded(self):
        self.assertFalse(us_continuous_session(et(2026, 10, 19, 8, 0)))
        self.assertFalse(us_continuous_session(et(2026, 10, 19, 17, 0)))

    def test_weekend(self):
        self.assertFalse(us_continuous_session(et(2026, 10, 24, 12, 0)))
        self.assertFalse(us_continuous_session(et(2026, 10, 25, 12, 0)))

    def test_dst_transitions_use_tz_database(self):
        # 2026-03-08 周日（切换日、休市）；03-09 周一 EDT：13:30 UTC 开盘
        self.assertFalse(us_continuous_session(utc(2026, 3, 8, 15, 0)))
        self.assertTrue(us_continuous_session(utc(2026, 3, 9, 13, 30)))
        self.assertFalse(us_continuous_session(utc(2026, 3, 9, 13, 29)))
        # 2026-11-01 周日；11-02 周一 EST（UTC-5）：14:30 UTC 开盘，13:30 UTC 还没开
        self.assertFalse(us_continuous_session(utc(2026, 11, 1, 15, 0)))
        self.assertTrue(us_continuous_session(utc(2026, 11, 2, 14, 30)))
        self.assertFalse(us_continuous_session(utc(2026, 11, 2, 13, 30)))
        self.assertTrue(us_continuous_session(utc(2026, 11, 2, 21, 0)))
        self.assertFalse(us_continuous_session(utc(2026, 11, 2, 21, 1)))

    def test_other_timezone_input(self):
        beijing = timezone(timedelta(hours=8))
        # 北京 2026-10-19 21:30 = 美东 09:30
        self.assertTrue(us_continuous_session(datetime(2026, 10, 19, 21, 30, tzinfo=beijing)))
        self.assertFalse(us_continuous_session(datetime(2026, 10, 19, 21, 29, tzinfo=beijing)))

    def test_naive_rejected(self):
        with self.assertRaises(ValueError):
            us_continuous_session(datetime(2026, 10, 19, 10, 0))

    def test_holiday_clock_true_but_stale_blocks(self):
        # 2026-11-26 感恩节休市：钟表仍是 True，靠行情过旧拦截
        now = et(2026, 11, 26, 10, 0)
        self.assertTrue(us_continuous_session(now))
        checked = validate_quote(quote(at=et(2026, 11, 25, 15, 59), received=now), 'AAPL')
        self.assertEqual((checked.quality, checked.reasons), ('stale', ('stale',)))
        state, alert = alert_step(AlertState(), Condition('AAPL', 'up', 101), checked)
        self.assertIsNone(alert)
        self.assertIsNone(state.baseline)

    def test_half_day_after_close_data_not_used(self):
        # 2026-11-27 半日市 13:00 收盘；14:00 收到的 13:00 行情已过旧
        checked = validate_quote(quote(at=et(2026, 11, 27, 13, 0), received=et(2026, 11, 27, 14, 0)), 'AAPL')
        self.assertEqual(checked.quality, 'stale')


class ValidateQuoteTests(unittest.TestCase):
    def test_valid(self):
        checked = validate_quote(quote(), 'AAPL')
        self.assertEqual((checked.quality, checked.reasons, checked.session),
                         ('valid', (), 'regular_session_hint'))

    def test_off_session_receive_is_not_valid(self):
        at = et(2026, 10, 22, 16, 0)
        checked = validate_quote(quote(at=at, received=et(2026, 10, 22, 16, 0, 30)), 'AAPL')
        self.assertEqual((checked.quality, checked.session), ('off_session', 'off_session'))

    def test_after_hours_timestamp_marked_not_alerting(self):
        checked = validate_quote(quote(at=et(2026, 10, 22, 16, 0, 1), received=et(2026, 10, 22, 16, 0)), 'AAPL')
        self.assertEqual((checked.quality, checked.reasons), ('after_hours', ('after_hours',)))
        self.assertEqual(validate_quote(quote(at=et(2026, 10, 22, 16, 0), received=et(2026, 10, 22, 16, 0, 5)),
                                        'AAPL').reasons, ())

    def test_pre_market_timestamp(self):
        checked = validate_quote(quote(at=et(2026, 10, 22, 9, 29, 59), received=et(2026, 10, 22, 9, 30, 10)), 'AAPL')
        self.assertEqual((checked.quality, checked.reasons), ('after_hours', ('pre_market',)))

    def test_bad_prices(self):
        for bad in (0, -1, float('nan'), float('inf'), True, None, '101'):
            self.assertIn('invalid_price', validate_quote(quote(price=bad), 'AAPL').reasons, repr(bad))
        self.assertIn('invalid_price', validate_quote(quote(previous_close=None), 'AAPL').reasons)
        self.assertIn('invalid_price', validate_quote(quote(opening=0), 'AAPL').reasons)

    def test_range(self):
        self.assertIn('invalid_range', validate_quote(quote(high=99, low=100), 'AAPL').reasons)
        self.assertIn('invalid_range', validate_quote(quote(high=None, low=100), 'AAPL').reasons)
        self.assertEqual(validate_quote(quote(high=None, low=None), 'AAPL').quality, 'valid')

    def test_volume(self):
        for bad in (True, -1, 1.5, None, '10'):
            self.assertIn('invalid_volume', validate_quote(quote(volume=bad), 'AAPL').reasons, repr(bad))
        self.assertEqual(validate_quote(quote(volume=0), 'AAPL').quality, 'valid')

    def test_symbol_mismatch(self):
        self.assertIn('code_mismatch', validate_quote(quote(symbol='MSFT'), 'AAPL').reasons)
        self.assertIn('code_mismatch', validate_quote(quote(), 'aapl').reasons)

    def test_time_problems(self):
        self.assertIn('invalid_source_time', validate_quote(quote(at=None), 'AAPL').reasons)
        naive = validate_quote(quote(at=datetime(2026, 10, 22, 10, 0)), 'AAPL')
        self.assertIn('invalid_source_time', naive.reasons)
        future = validate_quote(quote(at=et(2026, 10, 22, 10, 0, 10), received=et(2026, 10, 22, 10, 0)), 'AAPL')
        self.assertIn('future_time', future.reasons)
        invalid = validate_quote(quote(received=datetime(2026, 10, 22, 10, 0)), 'AAPL')
        self.assertIn('invalid_received_time', invalid.reasons)
        self.assertEqual(invalid.session, 'unknown')

    def test_stale_threshold(self):
        at = et(2026, 10, 22, 10, 0)
        self.assertEqual(validate_quote(quote(received=at + timedelta(seconds=1200)), 'AAPL').quality, 'valid')
        self.assertEqual(validate_quote(quote(received=at + timedelta(seconds=1201)), 'AAPL').quality, 'stale')
        with self.assertRaises(ValueError):
            validate_quote(quote(), 'AAPL', stale_seconds=0)

    def test_previous_checks(self):
        first = validate_quote(quote(volume=500), 'AAPL')
        same = validate_quote(quote(volume=500), 'AAPL', first)
        self.assertIn('duplicate_timestamp', same.reasons)
        older = validate_quote(quote(at=et(2026, 10, 22, 9, 59), received=et(2026, 10, 22, 10, 0)), 'AAPL', first)
        self.assertIn('timestamp_regression', older.reasons)
        later = quote(at=et(2026, 10, 22, 10, 5), volume=400)
        self.assertIn('volume_regression', validate_quote(later, 'AAPL', first).reasons)
        # 跨美东日期成交量重置不算回退
        next_day = quote(at=et(2026, 10, 23, 10, 0), volume=10)
        self.assertNotIn('volume_regression', validate_quote(next_day, 'AAPL', first).reasons)


class AlertTests(unittest.TestCase):
    CONDITION = Condition('AAPL', 'up', 101.0, cooldown_seconds=300, daily_limit=2)

    def step(self, state, minute, price, condition=None, **kw):
        at = et(2026, 10, 22, 10, minute)
        q = validate_quote(quote(price=price, at=at, received=at + timedelta(seconds=5)), 'AAPL')
        return alert_step(state, condition or self.CONDITION, q, **kw)

    def test_condition_validation(self):
        for args in (('aapl', 'up', 1), ('AAPL', 'sideways', 1), ('AAPL', 'up', 0), ('AAPL', 'up', True)):
            with self.assertRaises(ValueError):
                Condition(*args)
        with self.assertRaises(ValueError):
            Condition('AAPL', 'up', 1, daily_limit=0)

    def test_first_observation_only_baseline(self):
        state, alert = self.step(AlertState(), 0, 102)
        self.assertIsNone(alert)
        self.assertIsNotNone(state.baseline)

    def test_cross_up_once_then_cooldown_and_limit(self):
        state, _ = self.step(AlertState(), 0, 100)
        state, alert = self.step(state, 1, 101)
        self.assertEqual((alert.previous_price, alert.price), (100.0, 101.0))
        self.assertEqual(state.count, 1)
        # 保持在阈值之上不重复提醒
        state, alert = self.step(state, 2, 103)
        self.assertIsNone(alert)
        # 冷却内重新穿越：不补发
        state, _ = self.step(state, 3, 100)
        state, alert = self.step(state, 4, 102)
        self.assertIsNone(alert)
        # 冷却后（>=300 秒）再次穿越
        state, _ = self.step(state, 9, 100)
        state, alert = self.step(state, 10, 102)
        self.assertIsNotNone(alert)
        self.assertEqual(state.count, 2)
        # 达到每日上限后不再提醒
        state, _ = self.step(state, 16, 100)
        state, alert = self.step(state, 17, 102)
        self.assertIsNone(alert)

    def test_cross_down(self):
        down = Condition('AAPL', 'down', 100.0)
        state, _ = self.step(AlertState(), 0, 101, down)
        state, alert = self.step(state, 1, 100, down)
        self.assertIsNotNone(alert)

    def test_disconnect_and_none_reset_baseline_but_keep_count(self):
        state, _ = self.step(AlertState(), 0, 100)
        state, alert = self.step(state, 1, 102)
        self.assertEqual(state.count, 1)
        state, alert = self.step(state, 2, 100, connected=False)
        self.assertIsNone(state.baseline)
        self.assertEqual(state.count, 1)
        state, alert = alert_step(state, self.CONDITION, None)
        self.assertIsNone(alert)

    def test_invalid_quote_does_not_alert(self):
        state, _ = self.step(AlertState(), 0, 100)
        state, alert = self.step(state, 1, float('nan'))
        self.assertIsNone(alert)
        self.assertIsNone(state.baseline)

    def test_unchecked_quote_rejected(self):
        at = et(2026, 10, 22, 10, 1)
        state, alert = alert_step(AlertState(), self.CONDITION, quote(price=102, at=at))
        self.assertIsNone(alert)
        self.assertIsNone(state.baseline)

    def test_gap_too_large_no_alert(self):
        state, _ = self.step(AlertState(), 0, 100)
        state, alert = self.step(state, 30, 102)
        self.assertIsNone(alert)
        with self.assertRaises(ValueError):
            self.step(state, 31, 102, max_gap_seconds=0)

    def test_new_us_day_resets_count(self):
        state, _ = self.step(AlertState(), 0, 100)
        state, alert = self.step(state, 1, 102)
        self.assertEqual((state.day, state.count), ('2026-10-22', 1))
        at = et(2026, 10, 23, 10, 0)
        q = validate_quote(quote(price=100, at=at, received=at + timedelta(seconds=5)), 'AAPL')
        state, alert = alert_step(state, self.CONDITION, q)
        self.assertEqual((state.day, state.count), ('2026-10-23', 0))
        self.assertIsNone(alert)

    def test_changed_condition_rebuilds_baseline(self):
        state, _ = self.step(AlertState(), 0, 100)
        other = replace(self.CONDITION, threshold=100.5)
        state, alert = self.step(state, 1, 102, other)
        self.assertIsNone(alert)


# ---------- Provider ----------

def make_frame(symbols=('AAPL',), today_close=(101.0, 102.0), tz='America/New_York', drop=()):
    """前一交易日 15:58/15:59 两根 + 今日 09:30/09:31 两根的 1 分钟 K 线（MultiIndex: Ticker, Price）。"""
    stamps = [datetime(2026, 10, 21, 15, 58), datetime(2026, 10, 21, 15, 59),
              datetime(2026, 10, 22, 9, 30), datetime(2026, 10, 22, 9, 31)]
    index = pd.DatetimeIndex(stamps)
    index = index.tz_localize(tz) if tz else index
    frames = {}
    for i, symbol in enumerate(symbols):
        if symbol in drop:
            continue
        base = 100.0 + i
        frames[symbol] = pd.DataFrame({
            'Open': [base, base, base + 0.5, base + 1],
            'High': [base + 1, base + 1, base + 2, base + 3],
            'Low': [base - 1, base - 1, base, base + 0.4],
            'Close': [base, base + 0.2, today_close[0] + i, today_close[1] + i],
            'Volume': [10, 20, 100, 200]}, index=index)
    return pd.concat(frames, axis=1)


class YFRateLimitError(Exception):
    """与 yfinance 1.7.0 同名的限流异常（history() 对它一律向上抛出）。"""


def single_symbol_frame(kw):
    """默认行为：返回单只代码的单层列 DataFrame（与 Ticker.history 的真实形状一致）。"""
    symbol = kw['tickers']
    order = ('AAPL', 'MSFT', 'BRK-B', 'NVDA')  # 位置决定基准价，与原按批返回的数值一致
    if symbol in order:
        return make_frame(order)[symbol]
    return make_frame((symbol,))[symbol]


class FakeTicker:
    def __init__(self, owner, symbol):
        self.owner, self.symbol = owner, symbol

    def history(self, **kw):
        kw = dict(kw, tickers=self.symbol)
        owner = self.owner
        owner.calls.append(kw)
        owner.env_during.append({k: v for k, v in os.environ.items() if k.lower().endswith('_proxy')})
        try:
            return owner.behaviour(kw)
        except YFRateLimitError:
            raise  # 真实库：限流一律抛出
        except Exception:
            # 真实库 1.7.0：debug.hide_exceptions 为 True 时，其他异常（超时、断网）被吞掉并返回空表
            if owner.config.debug.hide_exceptions:
                return pd.DataFrame()
            raise


class FakeYF:
    """模仿 yfinance 1.7.0：没有按批下载的入口可用，只有 Ticker(sym).history()。"""
    def __init__(self, behaviour=None):
        self.calls, self.cache = [], []
        self.config = type('Config', (), {
            'network': type('Network', (), {'proxy': 'unset'})(),
            'debug': type('Debug', (), {'hide_exceptions': True})()})()
        self.behaviour = behaviour or single_symbol_frame
        self.env_during = []

    def set_tz_cache_location(self, path):
        self.cache.append(path)

    def Ticker(self, symbol):
        return FakeTicker(self, symbol)


class Clock:
    def __init__(self):
        self.t = 1000.0
        self.sleeps = []

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds


NOW = lambda: et(2026, 10, 22, 9, 32)


class ProviderBase(NoNetworkCase):
    def build(self, yf=None, **kw):
        self.yf = yf or FakeYF()
        self.clock = Clock()
        provider = YFinanceProvider(loader=lambda: self.yf, monotonic=self.clock.monotonic,
                                    sleep=self.clock.sleep, now=NOW, **kw)
        provider.start()
        return provider


class ProviderTests(ProviderBase):
    def test_success_fields(self):
        provider = self.build()
        (q,) = provider.fetch(['AAPL'])
        self.assertEqual((q.symbol, q.price, q.previous_close, q.opening, q.high, q.low, q.volume),
                         ('AAPL', 102.0, 100.2, 100.5, 103.0, 100.0, 300))
        self.assertEqual(q.timestamp, et(2026, 10, 22, 9, 31))
        self.assertEqual(q.timestamp.utcoffset(), timedelta(hours=-4))
        self.assertEqual(q.quality, 'unchecked')
        checked = validate_quote(q, 'AAPL')
        self.assertEqual(checked.quality, 'valid')

    def test_utc_index_converted_to_eastern(self):
        frame = make_frame()
        frame.index = frame.index.tz_convert('UTC')  # 同一时刻，改用 UTC 表示
        provider = self.build(FakeYF(lambda kw: frame))
        (q,) = provider.fetch(['AAPL'])
        self.assertEqual(q.timestamp, et(2026, 10, 22, 9, 31))
        self.assertEqual(q.previous_close, 100.2)

    def test_request_arguments(self):
        provider = self.build()
        provider.fetch(['AAPL', 'MSFT', 'BRK-B'])
        # 一批 3 只 = 3 次独立请求，每次一只
        self.assertEqual([kw['tickers'] for kw in self.yf.calls], ['AAPL', 'MSFT', 'BRK-B'])
        for kw in self.yf.calls:
            self.assertEqual((kw['timeout'], kw['period'], kw['interval']), (10, '5d', '1m'))
            self.assertIs(kw['prepost'], False)
            # 不传 session/headers/cookies 等任何伪装或凭据
            self.assertFalse({'session', 'headers', 'cookies', 'proxy', 'user_agent'} & set(kw))
        self.assertIs(self.yf.config.debug.hide_exceptions, False)

    def test_multi_symbol_each_parsed(self):
        provider = self.build()
        quotes = provider.fetch(['AAPL', 'MSFT'])
        self.assertEqual([q.symbol for q in quotes], ['AAPL', 'MSFT'])
        self.assertEqual(quotes[1].price, 103.0)

    def test_missing_symbol_dropped_but_not_all(self):
        provider = self.build(FakeYF(lambda kw: make_frame(('AAPL', 'MSFT'), drop=('MSFT',))))
        quotes = provider.fetch(['AAPL', 'MSFT'])
        self.assertEqual([q.symbol for q in quotes], ['AAPL'])

    def test_constructor_limits(self):
        for kw in ({'min_interval': 59}, {'min_interval': float('nan')}, {'timeout': 11}, {'timeout': 0},
                   {'request_budget': 0}, {'request_budget': 5001}, {'max_retries': 3}, {'max_retries': -1}):
            with self.assertRaises(ValueError, msg=str(kw)):
                YFinanceProvider(**kw)

    def test_invalid_symbol_lists(self):
        provider = self.build()
        for bad in ([], ['AAPL'] * 2, ['A', 'B', 'C', 'D', 'E', 'F'], ['aapl'], ['^GSPC'], [None]):
            with self.assertRaises(FeedError, msg=str(bad)) as ctx:
                provider.fetch(bad)
            self.assertEqual(ctx.exception.category, 'invalid_symbols')
        self.assertEqual(self.yf.calls, [])
        self.assertEqual(len(provider.fetch(['A', 'B', 'C', 'D', 'E'])), 5)

    def test_not_started_and_double_start(self):
        provider = YFinanceProvider(loader=FakeYF)
        with self.assertRaises(FeedError) as ctx:
            provider.fetch(['AAPL'])
        self.assertEqual(ctx.exception.category, 'not_started')
        provider.start()
        with self.assertRaises(FeedError) as ctx:
            provider.start()
        self.assertEqual(ctx.exception.category, 'already_started')
        provider.stop()
        with self.assertRaises(FeedError):
            provider.fetch(['AAPL'])


class RateLimitTests(ProviderBase):
    def test_same_symbol_within_interval_refused(self):
        provider = self.build()
        provider.fetch(['AAPL'])
        self.clock.t += 59.9
        with self.assertRaises(FeedError) as ctx:
            provider.fetch(['AAPL'])
        self.assertEqual(ctx.exception.category, 'minimum_interval')
        self.assertEqual((len(self.yf.calls), provider.requests_used), (1, 1))
        self.clock.t += 0.1
        provider.fetch(['AAPL'])
        self.assertEqual(len(self.yf.calls), 2)

    def test_overlapping_symbol_in_batch_refused_other_symbol_ok(self):
        provider = self.build()
        provider.fetch(['AAPL'])
        provider.fetch(['MSFT'])
        self.clock.t += 10
        with self.assertRaises(FeedError) as ctx:
            provider.fetch(['MSFT', 'NVDA'])
        self.assertEqual(ctx.exception.category, 'minimum_interval')
        self.assertEqual(len(self.yf.calls), 2)

    def test_min_interval_cannot_be_lowered_but_can_be_raised(self):
        provider = self.build(min_interval=300)
        provider.fetch(['AAPL'])
        self.clock.t += 120
        with self.assertRaises(FeedError):
            provider.fetch(['AAPL'])
        self.clock.t += 180
        provider.fetch(['AAPL'])

    def test_failed_request_still_counts(self):
        provider = self.build(FakeYF(lambda kw: (_ for _ in ()).throw(RuntimeError('x'))), max_retries=0)
        with self.assertRaises(FeedError):
            provider.fetch(['AAPL'])
        self.assertEqual(provider.requests_used, 1)
        with self.assertRaises(FeedError) as ctx:
            provider.fetch(['AAPL'])
        self.assertEqual(ctx.exception.category, 'minimum_interval')

    def test_budget_exceeded(self):
        provider = self.build(request_budget=2)
        provider.fetch(['AAPL'])
        provider.fetch(['MSFT'])
        with self.assertRaises(FeedError) as ctx:
            provider.fetch(['NVDA'])
        self.assertEqual(ctx.exception.category, 'budget_exceeded')
        self.assertEqual(len(self.yf.calls), 2)

    def test_restart_resets_budget_but_not_symbol_interval(self):
        provider = self.build(request_budget=1)
        provider.fetch(['AAPL'])
        provider.stop()
        provider.start()
        self.assertEqual(provider.requests_used, 0)
        # stop/start 不能绕过每只代码 60 秒的间隔
        with self.assertRaises(FeedError) as ctx:
            provider.fetch(['AAPL'])
        self.assertEqual(ctx.exception.category, 'minimum_interval')
        self.assertEqual(len(self.yf.calls), 1)
        self.clock.t += 60
        provider.fetch(['AAPL'])
        self.assertEqual(len(self.yf.calls), 2)

    def test_budget_counts_per_symbol(self):
        provider = self.build()
        provider.fetch(['AAPL', 'MSFT', 'NVDA'])
        self.assertEqual((provider.requests_used, len(self.yf.calls)), (3, 3))

    def test_batch_larger_than_remaining_budget_sends_nothing(self):
        provider = self.build(request_budget=2)
        with self.assertRaises(FeedError) as ctx:
            provider.fetch(['AAPL', 'MSFT', 'NVDA'])
        self.assertEqual(ctx.exception.category, 'budget_exceeded')
        self.assertEqual((len(self.yf.calls), provider.requests_used), (0, 0))


class RetryTests(ProviderBase):
    def test_timeout_retried_twice_with_exponential_backoff(self):
        provider = self.build(FakeYF(lambda kw: (_ for _ in ()).throw(TimeoutError('t'))))
        with self.assertRaises(FeedError) as ctx:
            provider.fetch(['AAPL'])
        self.assertEqual(ctx.exception.category, 'timeout')
        self.assertEqual(len(self.yf.calls), 3)
        self.assertEqual(self.clock.sleeps, [60, 120])
        self.assertEqual(provider.requests_used, 3)

    def test_retry_then_success(self):
        outcomes = [ConnectionError('down'), None]

        def behaviour(kw):
            outcome = outcomes.pop(0)
            if outcome:
                raise outcome
            return make_frame()
        provider = self.build(FakeYF(behaviour))
        (q,) = provider.fetch(['AAPL'])
        self.assertEqual(q.price, 102.0)
        self.assertEqual(self.clock.sleeps, [60])

    def test_network_down_classified_and_retried(self):
        provider = self.build(FakeYF(lambda kw: (_ for _ in ()).throw(ConnectionError('secret detail'))))
        with self.assertRaises(FeedError) as ctx:
            provider.fetch(['AAPL'])
        self.assertEqual(ctx.exception.category, 'network')
        self.assertNotIn('secret', str(ctx.exception))
        self.assertEqual(len(self.yf.calls), 3)

    def test_rate_limited_not_retried(self):
        provider = self.build(FakeYF(lambda kw: (_ for _ in ()).throw(YFRateLimitError('Too Many Requests'))))
        with self.assertRaises(FeedError) as ctx:
            provider.fetch(['AAPL'])
        self.assertEqual(ctx.exception.category, 'rate_limited')
        self.assertEqual((len(self.yf.calls), self.clock.sleeps), (1, []))

    def test_rate_limit_mid_batch_aborts_rest(self):
        def behaviour(kw):
            if kw['tickers'] == 'MSFT':
                raise YFRateLimitError('Too Many Requests')
            return single_symbol_frame(kw)
        provider = self.build(FakeYF(behaviour))
        with self.assertRaises(FeedError) as ctx:
            provider.fetch(['AAPL', 'MSFT', 'NVDA'])
        self.assertEqual(ctx.exception.category, 'rate_limited')
        # NVDA 没有被请求；已发出的 2 次计入预算；没有退避重试
        self.assertEqual([kw['tickers'] for kw in self.yf.calls], ['AAPL', 'MSFT'])
        self.assertEqual((provider.requests_used, self.clock.sleeps), (2, []))

    def test_timeout_is_raised_not_swallowed_as_empty(self):
        # 真实库默认 hide_exceptions=True 时超时会被吞成空表；provider 必须关掉它，才能区分超时与无数据
        yf = FakeYF(lambda kw: (_ for _ in ()).throw(TimeoutError('t')))
        self.assertIs(yf.config.debug.hide_exceptions, True)
        provider = self.build(yf)
        with self.assertRaises(FeedError) as ctx:
            provider.fetch(['AAPL'])
        self.assertEqual(ctx.exception.category, 'timeout')
        self.assertIs(yf.config.debug.hide_exceptions, False)

    def test_prices_missing_symbol_skipped_not_retried(self):
        class YFPricesMissingError(Exception):
            pass

        def behaviour(kw):
            if kw['tickers'] == 'ZZZZ':
                raise YFPricesMissingError('possibly delisted')
            return single_symbol_frame(kw)
        provider = self.build(FakeYF(behaviour))
        quotes = provider.fetch(['AAPL', 'ZZZZ'])
        self.assertEqual([q.symbol for q in quotes], ['AAPL'])
        self.assertEqual(self.clock.sleeps, [])
        self.assertEqual(classify(YFPricesMissingError('x')), 'empty')

    def test_all_symbols_missing_not_retried(self):
        class YFPricesMissingError(Exception):
            pass

        def behaviour(kw):
            raise YFPricesMissingError('possibly delisted')
        provider = self.build(FakeYF(behaviour))
        with self.assertRaises(FeedError) as ctx:
            provider.fetch(['ZZZZ'])
        self.assertEqual(ctx.exception.category, 'symbol_missing')
        self.assertEqual(len(self.yf.calls), 1)
        self.assertEqual(self.clock.sleeps, [])

    def test_empty_retried_then_fails(self):
        provider = self.build(FakeYF(lambda kw: pd.DataFrame()))
        with self.assertRaises(FeedError) as ctx:
            provider.fetch(['AAPL'])
        self.assertEqual(ctx.exception.category, 'empty')
        self.assertEqual(len(self.yf.calls), 3)

    def test_none_frame_is_empty(self):
        provider = self.build(FakeYF(lambda kw: None), max_retries=0)
        with self.assertRaises(FeedError) as ctx:
            provider.fetch(['AAPL'])
        self.assertEqual(ctx.exception.category, 'empty')

    def test_unknown_error_is_service_error(self):
        provider = self.build(FakeYF(lambda kw: (_ for _ in ()).throw(RuntimeError('boom'))), max_retries=1)
        with self.assertRaises(FeedError) as ctx:
            provider.fetch(['AAPL'])
        self.assertEqual(ctx.exception.category, 'service_error')
        self.assertEqual(len(self.yf.calls), 2)

    def test_slow_response_counts_as_timeout(self):
        yf = FakeYF()
        original = yf.behaviour

        def slow(kw):
            self.clock.t += 11
            return original(kw)
        yf.behaviour = slow
        provider = self.build(yf, max_retries=0)
        with self.assertRaises(FeedError) as ctx:
            provider.fetch(['AAPL'])
        self.assertEqual(ctx.exception.category, 'timeout')

    def test_bad_response_not_retried(self):
        frame = make_frame(tz=None)
        provider = self.build(FakeYF(lambda kw: frame))
        with self.assertRaises(FeedError) as ctx:
            provider.fetch(['AAPL'])
        self.assertEqual(ctx.exception.category, 'bad_response')
        self.assertEqual(len(self.yf.calls), 1)

    def test_missing_columns_bad_response(self):
        frame = make_frame()
        frame = frame.drop(columns=[('AAPL', 'Volume')])
        provider = self.build(FakeYF(lambda kw: frame))
        with self.assertRaises(FeedError) as ctx:
            provider.fetch(['AAPL'])
        self.assertEqual(ctx.exception.category, 'bad_response')

    def test_budget_runs_out_during_retries(self):
        provider = self.build(FakeYF(lambda kw: (_ for _ in ()).throw(TimeoutError())), request_budget=2)
        with self.assertRaises(FeedError) as ctx:
            provider.fetch(['AAPL'])
        self.assertEqual(ctx.exception.category, 'budget_exceeded')
        self.assertEqual(len(self.yf.calls), 2)

    def test_classify_table(self):
        self.assertEqual(classify(OSError('x')), 'network')
        self.assertEqual(classify(TimeoutError()), 'timeout')
        self.assertEqual(classify(Exception('HTTP Error 429')), 'rate_limited')
        self.assertEqual(classify(KeyError('k')), 'service_error')


class DegradationAndHygieneTests(ProviderBase):
    def test_yfinance_not_installed(self):
        def missing():
            raise ImportError("No module named 'yfinance' (private path)")
        provider = YFinanceProvider(loader=missing)
        provider.start()
        self.assertFalse(provider.available())
        with self.assertRaises(ImportError) as ctx:
            provider.fetch(['AAPL'])
        self.assertEqual(str(ctx.exception), 'yfinance_not_installed')
        self.assertEqual(provider.last_error, 'yfinance_not_installed')
        self.assertEqual(provider.requests_used, 0)

    def test_default_loader_with_blocked_module(self):
        with patch.dict(sys.modules, {'yfinance': None}):
            provider = YFinanceProvider()
            provider.start()
            self.assertFalse(provider.available())
            with self.assertRaises(ImportError):
                provider.fetch(['AAPL'])
            self.assertEqual(provider.last_error, 'yfinance_not_installed')

    def test_recovers_after_install(self):
        state = {'installed': False}

        def loader():
            if not state['installed']:
                raise ImportError('x')
            return FakeYF()
        provider = YFinanceProvider(loader=loader, now=NOW)
        provider.start()
        with self.assertRaises(ImportError):
            provider.fetch(['AAPL'])
        state['installed'] = True
        self.assertEqual(len(provider.fetch(['AAPL'])), 1)

    def test_proxy_env_removed_during_call_and_restored(self):
        env = {'HTTP_PROXY': 'http://127.0.0.1:9', 'https_proxy': 'http://127.0.0.1:9', 'ALL_PROXY': 'socks5://x'}
        with patch.dict(os.environ, env):
            provider = self.build()
            provider.fetch(['AAPL'])
            self.assertEqual(self.yf.env_during, [{}])
            self.assertEqual(os.environ['HTTP_PROXY'], 'http://127.0.0.1:9')
            self.assertEqual(os.environ['https_proxy'], 'http://127.0.0.1:9')

    def test_proxy_env_restored_after_failure(self):
        with patch.dict(os.environ, {'HTTPS_PROXY': 'http://127.0.0.1:9'}):
            provider = self.build(FakeYF(lambda kw: (_ for _ in ()).throw(RuntimeError())), max_retries=0)
            with self.assertRaises(FeedError):
                provider.fetch(['AAPL'])
            self.assertEqual(os.environ['HTTPS_PROXY'], 'http://127.0.0.1:9')

    def test_yfinance_proxy_config_cleared_and_cache_dir(self):
        provider = self.build(cache_dir='/tmp/us_monitor_cache_test')
        provider.fetch(['AAPL'])
        self.assertIsNone(self.yf.config.network.proxy)
        self.assertEqual(self.yf.cache, ['/tmp/us_monitor_cache_test'])

    def test_no_real_network_used(self):
        # NoNetworkCase 已把套接字函数替换成断言；这里确认替换确实生效
        with self.assertRaises(AssertionError):
            socket.getaddrinfo('example.com', 443)


if __name__ == '__main__':
    unittest.main()
