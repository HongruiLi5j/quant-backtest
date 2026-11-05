"""今日买卖的隔离测试：不读取令牌、不联网、不触碰现有清单。

测试数据使用系统 TemporaryDirectory，退出后只清理本次测试自己创建的临时文件。
运行：.venv/bin/python tests/test_trade_list.py
"""
import ast
import copy
import hmac
import hashlib
import json
import sys
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from http.cookies import SimpleCookie
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src import registry, scan, trade_list as T, turnover, turnover_backfill, trade_signals, trade_private, trade_notifications, tracking, members
from src.portfolio import run_weights
import daily
sys.path.insert(0, str(ROOT / 'tests'))
from realdata_guard import ZRealDataUntouchedTests  # noqa: E402,F401  真实数据守卫（必须在所有测试类之后运行）

DAY = '2021-01-15'
NOW = datetime(2021, 1, 15, 16, tzinfo=ZoneInfo('Asia/Shanghai'))
INDEX = pd.bdate_range(end=DAY, periods=1000)
CLOSE = np.linspace(100, 200, len(INDEX))
CLOSE[-1] = 195
OPEN = np.r_[CLOSE[0], CLOSE[:-1]]
DF = pd.DataFrame({'Open': OPEN, 'Close': CLOSE,
                   'High': np.maximum(OPEN, CLOSE) * 1.01,
                   'Low': np.minimum(OPEN, CLOSE) * .99,
                   'Volume': 100000.0}, index=INDEX)


def scan_rows(code='600001', df=DF):
    rows = []
    for strategy in scan.STRATS:
        w, res, _ = T.replay(code, '测试股票', strategy, df)
        rows.append({'code': code, 'name': '测试股票', 'delisted': False, 'last_date': DAY,
                     'strategy': strategy, 'now': float(res.weights.iloc[-1].sum()),
                     'next': float(w.iloc[-1].sum()), 'd1': -2.5, 'sharpe_oos': 1.234})
    return rows


BASE_ROWS = scan_rows()



class TradeListTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='quant_trade_tests_')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        pending = patch.object(daily.trade_list, 'pending_day', return_value=None)
        pending.start()
        self.addCleanup(pending.stop)
        # 账本和模拟盘会读写 data/ 下的真实文件，在 daily.run_market 里被调用；测试里一律换成空函数
        for name in ('_ledger', '_simtrade'):
            isolate = patch.object(daily, name)
            isolate.start()
            self.addCleanup(isolate.stop)
        self.rows = copy.deepcopy(BASE_ROWS)
        self.write_json('cn_all_progress.json', {'trade_day': DAY, 'complete': True})
        self.write_json('scan_cn_meta.json', {'data_date': DAY, 'generated_at': NOW.isoformat()})
        self.write_json('cn_all_suspended.json', {DAY: []})
        pd.DataFrame({'trade_date': pd.bdate_range(end='2021-02-19', periods=1040)}).to_csv(self.base / 'cn_trade_calendar.csv', index=False)
        self.write_scan()

    def write_json(self, path, value):
        p = self.base / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')

    def write_scan(self):
        pd.DataFrame(self.rows).to_parquet(self.base / 'scan_cn.parquet')

    def generate(self, **kwargs):
        return T.generate(cache=self.base, now=NOW, loader=lambda code: DF,
                          scale=lambda *args: (2.0, '前复权'), **kwargs)

    def assert_rejected_preserving(self, **kwargs):
        latest, archive = self.base / 'trade_list_cn.json', self.base / 'trade_list' / f'{DAY}.json'
        self.write_json(latest.name, {'data_date': DAY, 'sentinel': '上一份有效清单'})
        self.write_json(f'trade_list/{DAY}.json', {'sentinel': '上一份有效存档'})
        before = latest.read_bytes(), archive.read_bytes()
        with self.assertRaises((T.PublishError, FileNotFoundError)):
            self.generate(**kwargs)
        self.assertEqual(before, (latest.read_bytes(), archive.read_bytes()))

    def test_signal_classification(self):
        self.assertEqual(T.classify(0, .4), '买入')
        self.assertEqual(T.classify(.4, 0), '卖出')
        self.assertEqual(T.classify(1e-12, .2), '买入')
        self.assertEqual(T.classify(.2, 1e-12), '卖出')
        for a, b in [(0, 0), (.4, .4), (np.nan, .4), (.4, None)]:
            self.assertIsNone(T.classify(a, b))

    def test_threshold_grid_matches_scan_exactly(self):
        grid = (0, 1e-12, 1e-9, .000001, .1, .2, .3, .4, .5, .9, 1.)
        for a in grid:
            for b in grid:
                expected = scan._action(a, b)
                self.assertEqual(T.classify(a, b), expected if expected in T.ACTIONS else None, (a, b))
        self.assertEqual(T.classify(.4, .55), '加仓')
        self.assertEqual(T.classify(.55, .4), '减仓')
        self.assertIsNone(T.classify(.4, .55, .2))

    def test_float_boundary_is_expected_engine_behavior(self):
        # 引擎 apply_band 和扫描都直接按浮点数比较；30%→40% 的差略大于 0.1。
        self.assertEqual(T.classify(.3, .4), '加仓')
        self.assertEqual(T.classify(.4, .3), '减仓')

    def test_binary_strategies_have_no_adjustments(self):
        for strategy in ('rsi2', 'ma_adx_original'):
            w, res, _ = T.replay('600001', '测试股票', strategy, DF)
            self.assertTrue(set(w.iloc[:, 0].unique()) <= {0., 1.})
            actions = [T.classify(a, b) for a, b in zip(res.weights.iloc[:, 0], w.iloc[:, 0])]
            self.assertFalse(set(actions) & {'加仓', '减仓'})
        self.assertNotIn('donchian_s1', T.BINARY)
        self.assertNotIn('ma_adx_plus', T.BINARY)

    def test_no_lookahead_when_final_bar_changes(self):
        other = DF.copy()
        other.iloc[-1, other.columns.get_indexer(['Open', 'High', 'Low', 'Close'])] *= 1.7
        for strategy in scan.STRATS:
            w1, r1, _ = T.replay('600001', '测试股票', strategy, DF)
            w2, r2, _ = T.replay('600001', '测试股票', strategy, other)
            pd.testing.assert_frame_equal(w1.iloc[:-1], w2.iloc[:-1])
            pd.testing.assert_frame_equal(r1.weights.iloc[:-1], r2.weights.iloc[:-1])
            a = [T.classify(x, y) for x, y in zip(r1.weights.iloc[:-1, 0], w1.iloc[:-1, 0])]
            b = [T.classify(x, y) for x, y in zip(r2.weights.iloc[:-1, 0], w2.iloc[:-1, 0])]
            self.assertEqual(a, b)

    def test_suspended_is_isolated(self):
        self.write_json('cn_all_suspended.json', {DAY: ['600001']})
        result = self.generate()
        pack = result['strategies']['rsi2']
        self.assertEqual(pack['rows'], [])
        self.assertEqual(pack['counts']['买入'], 0)
        self.assertIn('发布日停牌', pack['exceptions'][0]['flags'])

    def test_each_board_one_price_limits(self):
        for code, limit in [('600001', .1), ('000001', .1), ('300001', .2),
                            ('301001', .2), ('688001', .2), ('830001', .3), ('920001', .3)]:
            for previous in (10.01, 5.01, 3.33, 2.04, 100.):
                for sign, word in [(1, '涨停'), (-1, '跌停')]:
                    price = float((Decimal(str(previous)) * (1 + sign * Decimal(str(limit)))).quantize(Decimal('.01'), rounding=ROUND_HALF_UP))
                    frame = pd.DataFrame({key: [previous, price] for key in ('Open', 'High', 'Low', 'Close')})
                    flags = T.limit_flags(code, frame)
                    self.assertEqual(len(flags), 1, (code, previous, price))
                    self.assertIn(word, flags[0])
                    self.assertIn('明天可能仍', flags[0])
                    frame.loc[1, 'High'] += .001
                    self.assertEqual(T.limit_flags(code, frame), [])
            frame = pd.DataFrame({key: [100., 109.] for key in ('Open', 'High', 'Low', 'Close')})
            self.assertEqual(T.limit_flags(code, frame), [])

    def test_progress_and_meta_dates_must_match(self):
        self.write_json('cn_all_progress.json', {'trade_day': '2021-01-14', 'complete': True})
        self.assert_rejected_preserving()
        self.write_json('cn_all_progress.json', {'trade_day': DAY, 'complete': True})
        self.write_json('scan_cn_meta.json', {'data_date': '2021-01-14'})
        self.assert_rejected_preserving()

    def test_explicit_publication_date_must_match(self):
        self.assert_rejected_preserving(release_day='2021-01-14')

    def test_complete_must_be_exact_boolean_true(self):
        for value in (None, False, 1, 'true'):
            self.write_json('cn_all_progress.json', {'trade_day': DAY, 'complete': value})
            self.assert_rejected_preserving()

    def test_intraday_guard_uses_beijing_time(self):
        early = datetime(2021, 1, 15, 7, 29, tzinfo=ZoneInfo('UTC'))
        with self.assertRaises(T.PublishError):
            T.check_release({'trade_day': DAY, 'complete': True}, {'data_date': DAY}, DAY, early)
        T.check_release({'trade_day': DAY, 'complete': True}, {'data_date': DAY}, DAY,
                        datetime(2021, 1, 15, 7, 30, tzinfo=ZoneInfo('UTC')))
        with self.assertRaises(T.PublishError):
            T.check_release({'trade_day': '2021-01-18', 'complete': True}, {'data_date': '2021-01-18'}, '2021-01-18', NOW)

    def test_whole_scan_date_mismatch_refuses_publication(self):
        for row in self.rows:
            row['last_date'] = '2021-01-14'
        self.write_scan()
        self.assert_rejected_preserving()

    def test_individual_old_date_is_isolated(self):
        extra = {**self.rows[0], 'code': '600002', 'last_date': '2021-01-14', 'now': 0, 'next': 1}
        self.rows.append(extra)
        self.write_scan()
        result = self.generate()
        pack = result['strategies']['blend2']
        self.assertTrue(any(x['code'] == '600002' for x in pack['exceptions']))
        self.assertFalse(any(x['code'] == '600002' for x in pack['rows']))

    def test_individual_bar_date_must_match(self):
        self.rows += [{**row, 'code': '600002'} for row in BASE_ROWS]
        self.write_scan()
        out = T.generate(cache=self.base, now=NOW,
                         loader=lambda code: DF if code == '600001' else DF.iloc[:-1],
                         scale=lambda *args: (1, '前复权'))
        pack = out['strategies']['rsi2']
        self.assertEqual([x['code'] for x in pack['rows']], ['600001'])
        self.assertEqual([x['code'] for x in pack['exceptions']], ['600002'])

    def test_all_bar_dates_wrong_preserves_previous(self):
        with patch.object(T.allmarket, 'load', return_value=DF.iloc[:-1]):
            self.write_json('trade_list_cn.json', {'data_date': DAY, 'sentinel': '有效'})
            before = (self.base / 'trade_list_cn.json').read_bytes()
            with self.assertRaises(T.PublishError):
                T.generate(cache=self.base, now=NOW, scale=lambda *args: (1, '前复权'))
            self.assertEqual(before, (self.base / 'trade_list_cn.json').read_bytes())

    def test_replay_mismatch_is_not_a_new_signal_rule(self):
        for row in self.rows:
            row.update(now=0, next=.123456789)
        self.write_scan()
        self.assert_rejected_preserving()

    def test_too_few_oos_trades_and_sharpe_from_scan(self):
        result = self.generate()
        row = result['strategies']['rsi2']['rows'][0]
        p = row['performance']
        self.assertTrue(p['small_sample'])
        self.assertLess(p['trades'], 10)
        self.assertEqual(p['sharpe'], 1.234)
        self.assertGreaterEqual(p['start'], '2020-01-01')
        self.assertEqual(p['beat_bh'], p['total'] > p['b_total'])
        self.assertAlmostEqual(row['close'], 390)
        self.assertIn('RSI(2)=', row['reason'])
        self.assertEqual(row['price_note'], '前复权')

    def test_missing_display_price_is_not_mislabeled_as_forward_adjusted(self):
        out = T.generate(cache=self.base, now=NOW, loader=lambda code: DF,
                         scale=lambda *args: (1, '未取到真实价格，显示为后复权'))
        row = out['strategies']['rsi2']['rows'][0]
        self.assertIsNone(row['close'])
        self.assertIn('数据不足', row['price_note'])
        self.assertIn('MA200=数据不足', row['reason'])
        self.assertFalse(T.needs_generation(DAY, cache=self.base))

    def test_small_sample_boundary_counts_only_closed_in_period_trades(self):
        idx = pd.bdate_range(end=DAY, periods=1000)
        ret = pd.Series(.0001, index=idx)
        start = idx.searchsorted(pd.Timestamp('2020-01-01'))
        for count in (9, 10):
            trades = pd.DataFrame({'entry_date': idx[start:start + count],
                                   'exit_date': idx[start + 1:start + count + 1]})
            result = SimpleNamespace(returns=ret, trades=trades,
                                     weights=pd.DataFrame({'X': 0.}, index=idx))
            p = T.performance(result, result, None, len(idx))
            self.assertEqual(p['trades'], count)
            self.assertEqual(p['small_sample'], count < 10)
        result.weights.iloc[-1] = 1
        p = T.performance(result, result, None, len(idx))
        self.assertEqual(p['trades'], 9)

    def test_archive_write_failure_keeps_latest_and_is_retryable(self):
        self.write_json('trade_list_cn.json', {'data_date': '2021-01-14', 'sentinel': '有效'})
        before = (self.base / 'trade_list_cn.json').read_bytes()
        with patch.object(T, '_atomic_json', side_effect=OSError('模拟写入失败')):
            with self.assertRaises(OSError):
                self.generate()
        self.assertEqual(before, (self.base / 'trade_list_cn.json').read_bytes())
        self.assertTrue(T.needs_generation(DAY, cache=self.base))

    def test_full_history_precedes_oos_slice(self):
        w, res, bench = T.replay('600001', '测试股票', 'trend_ens', DF)
        p = T.performance(res, bench, 0, len(DF))
        returns = res.returns.loc[p['start']:]
        self.assertAlmostEqual(p['total'], ((1 + returns).prod() - 1) * 100)
        self.assertGreater(res.weights.loc[p['start']].sum(), 0)
        short = DF.loc['2020-01-01':]
        _, reset, _ = T.replay('600001', '测试股票', 'trend_ens', short)
        self.assertEqual(reset.weights.iloc[0].sum(), 0)
        self.assertNotAlmostEqual(p['total'], ((1 + reset.returns).prod() - 1) * 100)

    def test_initial_oos_loss_in_drawdown(self):
        idx = pd.bdate_range(end=DAY, periods=500)
        ret = pd.Series(0., index=idx)
        ret.loc[ret.index >= '2020-01-01'] = -.01
        result = SimpleNamespace(returns=ret, trades=pd.DataFrame(columns=['entry_date', 'exit_date']),
                                 weights=pd.DataFrame({'X': 0.}, index=idx))
        p = T.performance(result, result, None, 500)
        self.assertAlmostEqual(p['maxdd'], p['total'])
        self.assertEqual(p['trades'], 0)
        self.assertIsNone(p['sharpe'])
        self.assertIsNone(T.performance(result, result, None, 300)['total'])

    def test_explanations_follow_existing_indicators(self):
        for strategy in scan.STRATS:
            text = T.explain(strategy, DF)
            self.assertTrue(text)
        text = T.explain('trend_ens', DF)
        self.assertIn('1% 迟滞', text)
        self.assertIn('MA250=', text)
        self.assertIn('不含当天', T.explain('donchian_s1', DF))
        self.assertIn('加权结果', T.explain('blend2', DF))
        self.assertIn('实际退出', T.explain('ma_adx_plus', DF))

    def test_archive_loading_validation_and_retry(self):
        self.assertTrue(T.needs_generation(DAY, cache=self.base))
        self.generate()
        latest = T.load(cache=self.base)
        archive = T.load(day=DAY, cache=self.base)
        self.assertEqual(latest, archive)
        self.assertFalse(T.needs_generation(DAY, cache=self.base))
        for value in ('../foo', '2021-02-30', '2021-1-1'):
            with self.assertRaises(T.PublishError):
                T.load(day=value, cache=self.base)
        with self.assertRaises(T.PublishError):
            T.load(strategy='dual_momentum', cache=self.base)
        self.assertTrue(T.load(day='2021-01-14', cache=self.base)['empty'])
        self.write_json('scan_cn_meta.json', {'data_date': DAY, 'generated_at': '重新扫描'})
        self.assertTrue(T.needs_generation(DAY, cache=self.base))

    def test_sources_changing_during_generation_refuse_publish(self):
        def scale(*args):
            self.write_json('cn_all_progress.json', {'trade_day': DAY, 'complete': False})
            return 1, '前复权'
        self.write_json('trade_list_cn.json', {'data_date': DAY, 'sentinel': '有效'})
        before = (self.base / 'trade_list_cn.json').read_bytes()
        with self.assertRaises(T.PublishError):
            T.generate(cache=self.base, now=NOW, loader=lambda code: DF, scale=scale)
        self.assertEqual(before, (self.base / 'trade_list_cn.json').read_bytes())

    def test_missing_suspension_or_incomplete_strategies_refuses_publish(self):
        self.write_json('cn_all_suspended.json', {})
        self.assert_rejected_preserving()
        self.write_json('cn_all_suspended.json', {DAY: []})
        self.rows = self.rows[:1]
        self.write_scan()
        self.assert_rejected_preserving()

    def test_daily_auto_detects_failed_listing(self):
        fake_cfg = {'cn': {**daily.MARKETS['cn'], 'close_ready': '00:00'}}
        today = datetime.now(ZoneInfo('Asia/Shanghai')).date()
        # 日历固定到周五，避免测试依赖当天是否周末或假日。
        while today.weekday() >= 5:
            today -= pd.Timedelta(days=1)
        day = today.isoformat()
        with patch.object(daily, 'MARKETS', fake_cfg), \
             patch.object(daily.daily_report, 'load', return_value={'data_date': day}), \
             patch.object(daily.allmarket, 'trade_calendar', return_value=[today]), \
             patch.object(daily.allmarket, 'cached_codes', return_value=['600001']), \
             patch.object(daily.allmarket, 'load_progress', return_value={'trade_day': day, 'complete': True}), \
             patch.object(daily.scan, 'load_scan', return_value=(None, {'data_date': day})), \
             patch.object(daily.trade_list, 'needs_generation', return_value=True) as retry:
            self.assertTrue(daily.needs_update('cn'))
            retry.assert_called_once_with(day)

    def test_daily_incomplete_or_failed_scan_never_generates(self):
        summary = {'data_date': DAY, 'main': '510300'}
        for complete, fails in [(False, False), (True, True), (True, False)]:
            with patch.object(daily.scan, 'load_scan', return_value=(None, {'data_date': DAY})), \
                 patch.object(daily.allmarket, 'load_progress', return_value={'complete': complete}), \
                 patch.object(daily.allmarket, 'cached_codes', return_value=['600001']), \
                 patch.object(daily.scan, 'run', side_effect=RuntimeError('扫描失败') if fails else None,
                              return_value={'data_date': DAY, 'stocks': 1, 'seconds': 0}), \
                 patch.object(daily.datasource, 'load_market', return_value={'510300': DF}), \
                 patch.object(daily.daily_report, 'build', return_value=summary), \
                 patch.object(daily.daily_report, 'save'), patch.object(daily.daily_report, 'log_signals'), \
                 patch.object(daily, 'signals_and_alerts'), patch.object(daily.logging, 'exception'), \
                 patch.object(daily.trade_list, 'generate', return_value={'data_date': DAY}) as generate:
                daily.run_market('cn', offline=True)
                self.assertEqual(generate.call_count, int(complete and not fails))

    def test_main_and_small_counts_and_exact_threshold(self):
        with patch.object(T, 'TRADE_LIST_MIN_TARGET', .0625), patch.object(T, 'replay') as replay:
            original = globals()['BASE_ROWS']
            # 选择二进制可精确表示的阈值，不给 0.05 边界增加舍入容差。
            for value, expected in ((.0625, False), (.03125, True), (.125, False)):
                self.rows = copy.deepcopy(original)
                for row in self.rows:
                    row.update(now=0., next=value)
                self.write_scan()
                weights = pd.DataFrame({'600001': value}, index=DF.index)
                result = SimpleNamespace(weights=pd.DataFrame({'600001': 0.}, index=DF.index))
                replay.return_value = (weights, result, None)
                with patch.object(T, 'performance', return_value={}):
                    out = self.generate()
                for pack in out['strategies'].values():
                    self.assertIn('counts', pack)
                    self.assertEqual(pack['rows'][0]['small_target'], expected)
                    for action in T.ACTIONS:
                        self.assertEqual(pack['counts_main'][action] + pack['counts_small'][action], pack['counts'][action])
                self.assertNotIn('buy_hold', out['strategies'])

    def test_only_missing_listing_skips_download_and_scan(self):
        with patch.object(T, 'pending_day', return_value=DAY), \
             patch.object(T, 'try_generate', return_value={'data_date': DAY}) as generate, \
             patch.object(daily.scan, 'run') as scan_run, \
             patch.object(daily.allmarket, 'daily_update') as update, \
             patch.object(daily.datasource, 'update') as etf_update, \
             patch.object(daily.daily_report, 'load', return_value={'data_date': DAY}):
            daily.run_market('cn', auto=True)
            generate.assert_called_once_with('cn', DAY, automatic=True)
            scan_run.assert_not_called()
            update.assert_not_called()
            etf_update.assert_not_called()

    def test_retry_reason_control_characters_escaped(self):
        self.write_json('trade_list_attempts.json', {'day': DAY, 'count': 3, 'last_attempt': NOW.isoformat(), 'error': 'bad\n2026-01-01 FAKE LOG\r\x1b[31mred\x00\x7f\x85\x9b'})
        ok, reason = T.attempt_status(DAY, cache=self.base, now=NOW + timedelta(minutes=90))
        self.assertFalse(ok)
        for ch in ('\n', '\r', '\x1b', '\x00', '\x7f', '\x85', '\x9b'):
            self.assertNotIn(ch, reason)                                          # 不能伪造日志行或终端控制序列
        self.assertIn('FAKE LOG', reason)

    def test_retry_refusal_reason_includes_last_error(self):
        self.write_json('trade_list_attempts.json', {'day': DAY, 'count': 3, 'last_attempt': NOW.isoformat(), 'error': 'RuntimeError: 价格缺失'})
        ok, reason = T.attempt_status(DAY, cache=self.base, now=NOW + timedelta(minutes=90))
        self.assertFalse(ok)
        self.assertIn('已自动尝试 3 次', reason)
        self.assertIn('RuntimeError: 价格缺失', reason)
        self.write_json('trade_list_attempts.json', {'day': DAY, 'count': 1, 'last_attempt': NOW.isoformat(), 'error': 'ValueError: x'})
        ok, reason = T.attempt_status(DAY, cache=self.base, now=NOW + timedelta(minutes=5))
        self.assertFalse(ok)
        self.assertIn('不足 30 分钟', reason)
        self.assertIn('ValueError: x', reason)
        self.write_json('trade_list_attempts.json', {'day': DAY, 'count': 3, 'last_attempt': NOW.isoformat(), 'error': ''})
        self.assertEqual(T.attempt_status(DAY, cache=self.base, now=NOW + timedelta(minutes=90))[1], '同一发布日已自动尝试 3 次')   # 没有错误记录时文案不变

    def test_retry_limit_interval_and_next_day_reset(self):
        with patch.object(T, 'generate', side_effect=RuntimeError('模拟失败')) as generate, \
             patch.object(T.logging, 'exception'):
            for minute in (0, 30, 60):
                T.try_generate('cn', DAY, automatic=True, cache=self.base, now=NOW + timedelta(minutes=minute))
            T.try_generate('cn', DAY, automatic=True, cache=self.base, now=NOW + timedelta(minutes=90))
            self.assertEqual(generate.call_count, 3)
            self.assertFalse(T.needs_generation(DAY, cache=self.base, now=NOW + timedelta(minutes=90)))
            attempt = json.loads((self.base / 'trade_list_attempts.json').read_text())
            self.assertEqual(set(attempt), {'day', 'count', 'last_attempt', 'error'})
            self.assertEqual(attempt['count'], 3)
            self.assertTrue(T.attempt_status('2021-01-18', cache=self.base, now=NOW + timedelta(days=3))[0])
            T.try_generate('cn', '2021-01-18', automatic=True, cache=self.base, now=NOW + timedelta(days=3))
            attempt = json.loads((self.base / 'trade_list_attempts.json').read_text())
            self.assertEqual(attempt['count'], 1)
        self.write_json('trade_list_attempts.json', {'day': DAY, 'count': 1, 'last_attempt': NOW.isoformat(), 'error': ''})
        self.assertFalse(T.needs_generation(DAY, cache=self.base, now=NOW + timedelta(minutes=29)))
        self.assertTrue(T.needs_generation(DAY, cache=self.base, now=NOW + timedelta(minutes=30)))

    def test_missing_prices_retry_only_failed_codes_and_then_publish(self):
        with patch.object(T, '_wait_price_cooldown') as wait:
            calls = []
            def scale(market, code, df):
                calls.append(code)
                return (1., '未取到真实价格') if len(calls) == 1 else (2., '前复权')
            out = T.generate(cache=self.base, now=NOW, loader=lambda code: DF, scale=scale)
            self.assertEqual(calls, ['600001', '600001'])
            wait.assert_called_once()
            self.assertEqual(out['price_unavailable'], 0)
        with patch.object(T, '_wait_price_cooldown') as wait:
            out = T.generate(cache=self.base, now=NOW, loader=lambda code: DF, scale=lambda *args: (1., '未取到真实价格'))
            self.assertEqual(wait.call_count, 2)
            self.assertGreater(out['price_unavailable'], 0)
            self.assertFalse(T.needs_generation(DAY, cache=self.base))

    def test_default_loader_drops_unfinished_bars_like_scan(self):
        future = DF.iloc[[-1]].copy()
        future.index = pd.to_datetime(['2021-01-18'])
        cached = pd.concat([DF, future])
        current = datetime(2021, 1, 18, 13, tzinfo=ZoneInfo('Asia/Shanghai'))
        with patch.object(T.allmarket, 'load', return_value=cached), patch.object(T.allmarket, '_now_bj', return_value=current):
            out = T.generate(cache=self.base, now=current, scale=lambda *args: (2., '前复权'))
        self.assertEqual(out['strategies']['rsi2']['rows'][0]['last_date'], DAY)

    def test_explanation_parameters_are_not_hardcoded(self):
        text = T.explain('rsi2', DF, params={'rsi_len': 3, 'trend_len': 150, 'entry_th': 12, 'exit_ma': 7, 'max_hold': 8})
        for value in ('RSI(3)', 'RSI<12', 'MA150', 'MA7', '8 天'):
            self.assertIn(value, text)
        text = T.explain('ma_adx_plus', DF, params={'fast': 8, 'slow': 30, 'adx_min': 25, 'atr_mult': 4., 'target_vol': .2, 'regime_len': 180, 'atr_len': 16, 'vol_len': 25})
        for value in ('MA8', 'MA30', 'ADX>25', 'MA180', 'ATR(16)', '4×ATR', '20.00%', '25 日'):
            self.assertIn(value, text)
        text = T.explain('donchian_s1', DF, params={'entry_len': 30, 'exit_len': 15, 'atr_mult': 4.})
        self.assertIn('前 30 日最高', text)
        self.assertIn('前 15 日最低', text)
        self.assertIn('4×ATR', text)

    def test_available_archive_dates(self):
        self.generate()
        self.write_json('trade_list/2021-01-14.json', {})
        self.write_json('trade_list/2021-02-30.json', {})
        result = T.dates(cache=self.base)
        self.assertEqual(result, {'dates': [DAY, '2021-01-14'], 'latest': DAY})

    def test_turnover_unit_normalization_and_bad_units(self):
        em = turnover.normalize('600001', DAY, 1000000, 1000, 10, 9, 11, source='东方财富', amount_unit='元', volume_unit='手')
        sina = turnover.normalize('600001', DAY, 1000000, 100000, 10, 9, 11, source='新浪', amount_unit='元', volume_unit='股')
        tx = turnover.normalize('600001', DAY, 100, 1000, 10, 9, 11, source='腾讯历史', amount_unit='万元', volume_unit='手')
        for record in (em, sina, tx):
            self.assertEqual(record['turnover_yuan'], 1000000)
            self.assertEqual(record['volume_shares'], 100000)
        self.assertIsNone(turnover.normalize('600001', DAY, 1000000, 1000, 10, 9, 11, source='错误单位', amount_unit='元', volume_unit='股'))
        self.assertIsNone(turnover.normalize('600001', DAY, None, 1000, 10, 9, 11, source='缺失', amount_unit='元', volume_unit='手'))
        path = self.base / 'turnover_cn.parquet'
        turnover.save([em], path)
        turnover.save([tx], path)
        self.assertEqual(len(turnover.load(path)), 1)

    def test_turnover_average_suspend_missing_and_threshold(self):
        days = DF.index[-20:]
        rows = [{'date': str(day.date()), 'code': '600001', 'turnover_yuan': 50000000} for day in days]
        table = pd.DataFrame(rows)
        def average(frame=table, suspended=None):
            return turnover.average_20d('600001', DAY, days, frame, suspended or {}, 50000000)
        self.assertEqual(average()['liquidity'], '正常')
        self.assertEqual(average()['avg_turnover_20d'], 50000000)
        self.assertEqual(average(table.iloc[1:])['liquidity'], '流动性未知')
        self.assertIsNone(average(table.iloc[1:])['avg_turnover_20d'])
        result = average(table.iloc[1:], {str(days[0].date()): ['600001']})
        self.assertEqual(result['avg_turnover_20d'], 47500000)
        self.assertEqual(result['liquidity'], '流动性差')
        self.assertEqual(turnover.lot_cost(10.5, True), 1050)
        self.assertIsNone(turnover.lot_cost(1000, False))
        self.assertIsNone(turnover.lot_cost(None, True))

    def test_turnover_missing_never_causes_generation_retry(self):
        out = self.generate()
        row = out['strategies']['rsi2']['rows'][0]
        self.assertEqual(row['liquidity'], '流动性未知')
        self.assertEqual(row['lot_cost'], 39000)
        self.assertFalse(T.needs_generation(DAY, cache=self.base))
        turnover.save([turnover.normalize('600001', DAY, 1000000, 100000, 10, 9, 11, source='样本', amount_unit='元', volume_unit='股')], self.base / 'turnover_cn.parquet')
        self.assertFalse(T.needs_generation(DAY, cache=self.base))

    def test_side_table_write_failure_does_not_change_snapshot(self):
        n = 1001
        raw = pd.DataFrame({'代码': [str(600000+i) for i in range(n)], '名称': '测试股',
                            '今开': 10., '最高': 11., '最低': 9., '最新价': 10., '昨收': 10.,
                            '成交量': 1000., '成交额': 1000000.})
        before = raw.copy(deep=True)
        import akshare as ak
        with patch.object(ak, 'stock_zh_a_spot_em', return_value=raw) as request, \
             patch.object(turnover, 'capture_snapshot', side_effect=OSError('模拟磁盘失败')), \
             patch.object(T.allmarket, '_now_bj', return_value=NOW), patch.object(T.allmarket.log, 'warning') as warning:
            frame = T.allmarket.spot_snapshot()
            self.assertEqual(list(frame.columns), ['name','open','high','low','close','prev_close','volume'])
            self.assertEqual(len(frame), n)
            self.assertEqual(request.call_count, 1)
            warning.assert_called_once()
        pd.testing.assert_frame_equal(before, raw)

    def test_snapshot_capture_uses_explicit_source_units_and_closed_day(self):
        raw = pd.DataFrame({'代码':['600001'], '成交额':[1000000.], '成交量':[1000.], '最新价':[10.], '最低':[9.], '最高':[11.]})
        path = self.base / 'turnover_cn.parquet'
        self.assertEqual(turnover.capture_snapshot(raw, '东方财富', NOW, path=path), 1)
        self.assertEqual(turnover.load(path).iloc[0].volume_shares, 100000)
        self.assertEqual(turnover.capture_snapshot(raw, '东方财富', NOW.replace(hour=13), path=path), 0)
        raw['成交量'] = 100000.
        self.assertEqual(turnover.capture_snapshot(raw, '新浪', NOW, path=path), 1)
        self.assertEqual(turnover.load(path).iloc[0].volume_shares, 100000)

    def test_snapshot_calendar_rejects_weekends_holiday_and_missing_calendar(self):
        raw = pd.DataFrame({'代码':['600001'], '成交额':[1000000.], '成交量':[1000.], '最新价':[10.], '最低':[9.], '最高':[11.]})
        calendar = ['2021-01-15', '2021-01-18', '2021-02-10', '2021-02-18']
        pd.DataFrame({'trade_date':calendar}).to_csv(self.base/'cn_trade_calendar.csv', index=False)
        path = self.base/'turnover_cn.parquet'
        for text in ('2021-01-16T16:00', '2021-01-17T16:00', '2021-02-12T16:00', '2021-01-15T15:29:59'):
            with self.subTest(now=text):
                now = datetime.fromisoformat(text).replace(tzinfo=NOW.tzinfo)
                self.assertEqual(turnover.capture_snapshot(raw, '东方财富', now, path=path), 0)
                self.assertFalse(path.exists())
        self.assertEqual(turnover.capture_snapshot(raw, '东方财富', NOW, path=path), 1)
        self.assertEqual(turnover.load(path).iloc[0]['date'], DAY)
        with tempfile.TemporaryDirectory() as other:
            self.assertEqual(turnover.capture_snapshot(raw, '东方财富', NOW, path=Path(other)/'turnover_cn.parquet'), 0)
            self.assertFalse((Path(other)/'turnover_cn.parquet').exists())

    def test_turnover_fetch_requires_amount_and_valid_units(self):
        class Response:
            text = 'kline_day=' + json.dumps({'data':{'sh600001':{'day':[[DAY,'10','10','11','9','1000','','0','100']]}}})
            def raise_for_status(self):
                pass
        rows = turnover_backfill.fetch('600001', DAY, DAY, get=lambda *a, **k: Response())
        self.assertEqual(rows[0]['turnover_yuan'], 1000000)
        Response.text = 'kline_day=' + json.dumps({'data':{'sh600001':{'day':[[DAY,'10','10','11','9','1000']]}}})
        with self.assertRaises(ValueError):
            turnover_backfill.fetch('600001', DAY, DAY, get=lambda *a, **k: Response())

    def test_backfill_resume_scope_spacing_and_budget(self):
        self.write_json('trade_list_cn.json', {'data_date':DAY, 'strategies':{'x':{'rows':[{'code':'600001'},{'code':'600002'}],'exceptions':[]}}})
        clock = [0.]
        requests = []
        def sleeper(seconds):
            clock[0] += seconds
        def fetcher(code, start, end, timeout):
            requests.append(clock[0])
            clock[0] += .1
            return [turnover.normalize(code, day, 1000000, 100000, 10, 9, 11, source='样本', amount_unit='元', volume_unit='股') for day in DF.index[-20:]]
        kwargs = dict(cache=self.base, calendar=DF.index, now=NOW, fetcher=fetcher, sleep=sleeper, monotonic=lambda:clock[0])
        first = turnover_backfill.run(**kwargs)
        self.assertEqual(first['requested'], 2)
        self.assertGreaterEqual(requests[1]-requests[0], 1.)
        second = turnover_backfill.run(**kwargs)
        self.assertEqual(second['requested'], 0)
        self.assertEqual(second['skipped'], 2)
        with self.assertRaises(ValueError):
            turnover_backfill.run(codes=['600999'], **kwargs)
        self.write_json('trade_list_cn.json', {'data_date':DAY, 'strategies':{'x':{'rows':[{'code':'600003'}],'exceptions':[]}}})
        limit = turnover_backfill.run(max_seconds=1, **kwargs)
        self.assertTrue(limit['time_limit'])
        self.assertEqual(limit['requested'], 0)

    def test_backfill_failures_are_isolated_and_reported_without_alternate_source(self):
        self.write_json('trade_list_cn.json', {'data_date':DAY, 'strategies':{'x':{'rows':[{'code':'600001'},{'code':'600002'}],'exceptions':[]}}})
        calls, clock = [], [0.]
        def fetcher(code, *args, **kwargs):
            calls.append(code)
            if code == '600001':
                raise ValueError('成交额/成交量/价格校验失败')
            return [turnover.normalize(code, day, 1000000, 100000, 10, 9, 11, source='样本', amount_unit='元', volume_unit='股') for day in DF.index[-20:]]
        result = turnover_backfill.run(cache=self.base, calendar=DF.index, now=NOW, fetcher=fetcher,
                                      monotonic=lambda:clock[0], sleep=lambda value:clock.__setitem__(0,clock[0]+value))
        self.assertEqual(calls, ['600001','600002'])
        self.assertEqual(result['failed']['600001']['category'], '字段或单位价格校验失败')
        self.assertEqual(result['completed'], 1)
        self.assertEqual(set(turnover.load(self.base/'turnover_cn.parquet').code), {'600002'})

    def test_backfill_uses_scan_signals_before_new_listing_exists(self):
        for row in self.rows:
            row['action'] = scan._action(row['now'], row['next'])
        self.write_scan()
        day, codes = turnover_backfill.scan_codes(self.base, DAY)
        self.assertEqual(day, DAY)
        self.assertEqual(codes, {'600001'})
        with self.assertRaises(ValueError):
            turnover_backfill.scan_codes(self.base, '2021-01-14')

    def test_bounded_backfill_enforces_process_timeout_and_keeps_checkpoint(self):
        path = self.base/'report.json'
        def timeout(*args, **kwargs):
            self.assertGreater(kwargs['timeout'], 590)
            self.assertLessEqual(kwargs['timeout'], 600)
            turnover_backfill._atomic_report(path, {'completed':1,'unprocessed':['600002']})
            raise subprocess.TimeoutExpired(args[0], kwargs['timeout'])
        with patch.object(turnover_backfill.subprocess, 'run', side_effect=timeout):
            result = turnover_backfill.run_bounded(cache=self.base, max_seconds=600, report_path=path)
        self.assertTrue(result['time_limit'])
        self.assertEqual(result['completed'], 1)
        self.assertEqual(result['unprocessed'], ['600002'])

    def test_automatic_backfill_once_per_release_day_even_after_failure(self):
        calls = []
        def runner(**kwargs):
            calls.append(kwargs)
            raise RuntimeError('模拟请求失败')
        result = turnover_backfill.automatic_once(DAY, cache=self.base, runner=runner)
        self.assertIn('error', result)
        self.assertTrue(turnover_backfill.automatic_once(DAY, cache=self.base, runner=runner)['skipped_day'])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['max_seconds'], 600)
        self.assertTrue(calls[0]['from_scan'])
        turnover_backfill.automatic_once('2021-01-18', cache=self.base, runner=runner)
        self.assertEqual(len(calls), 2)
        self.assertTrue(turnover_backfill.automatic_once(DAY, cache=self.base, runner=runner)['skipped_day'])

    def test_automatic_backfill_does_not_affect_needs_generation(self):
        self.generate()
        before = T.needs_generation(DAY, cache=self.base)
        turnover_backfill.automatic_once(DAY, cache=self.base, runner=lambda **kwargs:{'time_limit':True})
        self.assertEqual(T.needs_generation(DAY, cache=self.base), before)
        self.assertFalse(before)

    def test_daily_backfill_error_and_timeout_still_publish_without_retry(self):
        summary = {'data_date':DAY}
        for error in (RuntimeError('补取失败'), subprocess.TimeoutExpired('worker',600)):
            with patch.object(T, 'pending_day', return_value=DAY), \
                 patch.object(daily.daily_report, 'load', return_value=summary), \
                 patch.object(turnover_backfill, 'automatic_once', side_effect=error) as backfill, \
                 patch.object(T, 'try_generate', return_value=summary) as publish, \
                 patch.object(daily.scan, 'run') as scan_run:
                self.assertEqual(daily.run_market('cn'), summary)
                backfill.assert_called_once_with(DAY)
                publish.assert_called_once_with('cn',DAY,automatic=False)
                scan_run.assert_not_called()

    def test_scan_success_backfills_before_publication_and_isolates_failure(self):
        calls = []
        cfg = {'cn':{**daily.MARKETS['cn'], 'universe':[]}}
        def backfill(day):
            calls.append('backfill')
            raise RuntimeError('模拟失败')
        with patch.object(daily, 'MARKETS', cfg), \
             patch.object(daily.datasource, 'load_watchlist', return_value={}), patch.object(daily.datasource, 'save_watchlist'), \
             patch.object(daily.allmarket, 'daily_update', return_value={}), \
             patch.object(daily.allmarket, 'load_progress', return_value={'complete':True}), \
             patch.object(daily.allmarket, 'cached_codes', return_value=['600001']), \
             patch.object(daily.scan, 'run', return_value={'data_date':DAY,'stocks':1,'seconds':0}), \
             patch.object(turnover_backfill, 'automatic_once', side_effect=backfill), \
             patch.object(T, 'try_generate', side_effect=lambda *args,**kwargs:calls.append('publish')), \
             patch.object(daily.datasource, 'load_market', return_value={'510300':DF}), \
             patch.object(daily.daily_report, 'build', return_value={'data_date':DAY}), \
             patch.object(daily.daily_report, 'save'), patch.object(daily.daily_report, 'log_signals'), \
             patch.object(daily, 'signals_and_alerts'):
            daily.run_market('cn')
        self.assertEqual(calls, ['backfill','publish'])

    def test_separate_tracking_preserves_original_full_output_and_column_order(self):
        original = pd.DataFrame([{'market':'cn','signal_date':'2020-06-01','code':'600001','name':'测试股','strategy':'rsi2','action':'买入','next':1.,'recorded_at':'2020-06-01T16:00:00+08:00'}], columns=trade_signals.BASE_COLUMNS)
        old_path = self.base / 'signals_history.parquet'
        original.to_parquet(old_path, index=False)
        original_bytes = old_path.read_bytes()
        clock = SimpleNamespace(now=lambda tz=None: NOW.astimezone(tz) if tz else NOW)
        with patch.object(tracking, 'HIST', old_path), patch.object(tracking, '_prices', return_value=DF), patch.object(tracking, 'datetime', clock):
            before = tracking.evaluate('cn', daily.MARKETS['cn'])
            self.generate()
            after = tracking.evaluate('cn', daily.MARKETS['cn'])
        self.assertEqual(before, after)
        self.assertEqual(old_path.read_bytes(), original_bytes)
        self.assertEqual(pd.read_parquet(old_path).columns.tolist(), trade_signals.BASE_COLUMNS)
        table = pd.read_parquet(self.base / 'trade_signals_history.parquet')
        self.assertEqual(table.columns.tolist()[:8], trade_signals.BASE_COLUMNS)
        self.assertEqual(set(table.source), {'trade_list'})

    def test_recording_is_immutable_and_does_not_replay_old_days(self):
        out = self.generate()
        history = self.base / 'trade_signals_history.parquet'
        self.assertEqual(trade_signals.record_publication(out, cache=self.base, now=NOW), 0)
        original = pd.read_parquet(history)
        altered = copy.deepcopy(out)
        altered['strategies']['rsi2']['rows'][0]['reason'] = '事后改变的理由'
        trade_signals.record_publication(altered, cache=self.base, now=NOW)
        pd.testing.assert_frame_equal(original, pd.read_parquet(history))
        with tempfile.TemporaryDirectory() as other:
            count = trade_signals.record_publication(out, cache=other, now=NOW+timedelta(days=3))
            self.assertEqual(count, 0)
            self.assertFalse((Path(other) / 'trade_signals_history.parquet').exists())

    def test_recording_window_midnight_weekend_next_open_and_launch_time(self):
        payload = self.generate()
        cases = [('2021-01-16T00:30', '2021-01-16T00:15', True),
                 ('2021-01-16T16:00', '2021-01-15T16:00', True),
                 ('2021-01-17T16:00', '2021-01-15T16:00', True),
                 ('2021-01-18T09:29:59', '2021-01-15T16:00', True),
                 ('2021-01-18T09:30', '2021-01-15T16:00', False),
                 ('2021-01-18T10:00', '2021-01-15T16:00', False),
                 ('2021-01-15T15:29', '2021-01-15T16:00', False)]
        for now_text, generated_text, expected in cases:
            for use_calendar in (True, False):
                with self.subTest(now=now_text, calendar=use_calendar), tempfile.TemporaryDirectory() as other:
                    base = Path(other)
                    if use_calendar:
                        pd.DataFrame({'trade_date':[DAY,'2021-01-18']}).to_csv(base/'cn_trade_calendar.csv',index=False)
                    trade_signals.initialize(cache=base, now=NOW-timedelta(hours=1))
                    value = copy.deepcopy(payload)
                    value['generated_at'] = datetime.fromisoformat(generated_text).replace(tzinfo=NOW.tzinfo).isoformat()
                    now = datetime.fromisoformat(now_text).replace(tzinfo=NOW.tzinfo)
                    recorded = trade_signals.record_publication(value, cache=base, now=now)
                    self.assertEqual(recorded > 0, expected)
                    self.assertEqual(trade_signals.record_publication(value, cache=base, now=now), 0)
        with tempfile.TemporaryDirectory() as other:
            trade_signals.initialize(cache=other, now=NOW+timedelta(hours=1))
            self.assertEqual(trade_signals.record_publication(payload,cache=other,now=NOW+timedelta(days=1)),0)

    def test_recording_window_uses_next_trading_day_over_holiday(self):
        payload = self.generate()
        with tempfile.TemporaryDirectory() as other:
            base = Path(other)
            pd.DataFrame({'trade_date':[DAY,'2021-01-20']}).to_csv(base/'cn_trade_calendar.csv',index=False)
            trade_signals.initialize(cache=base,now=NOW-timedelta(hours=1))
            self.assertGreater(trade_signals.record_publication(payload,cache=base,now=NOW+timedelta(days=4)),0)
            self.assertEqual(trade_signals.record_publication(payload,cache=base,now=NOW+timedelta(days=5)),0)

    def test_snapshot_not_overwritten_and_legacy_fields_stay_missing(self):
        out = self.generate()
        path = self.base / 'trade_list' / f'{DAY}.json'
        archive = path.read_bytes()
        T.generate(cache=self.base, now=NOW, loader=lambda code: DF, scale=lambda *args:(3., '前复权'))
        self.assertEqual(path.read_bytes(), archive)
        self.assertFalse(T.needs_generation(DAY, cache=self.base))
        old = copy.deepcopy(out)
        for pack in old['strategies'].values():
            for row in pack['rows']:
                row.pop('avg_turnover_20d', None)
                row.pop('liquidity', None)
                row.pop('lot_cost', None)
        old['data_date'] = '2021-01-14'
        self.write_json('trade_list/2021-01-14.json', old)
        loaded = T.load(day='2021-01-14', strategy='rsi2', cache=self.base)
        self.assertNotIn('avg_turnover_20d', loaded['rows'][0])
        self.assertNotIn('lot_cost', loaded['rows'][0])

    def test_new_tracking_counts_completed_windows_and_sells_separately(self):
        out = self.generate()
        sold = copy.deepcopy(out['strategies']['rsi2']['rows'][0])
        sold.update(code='600002', action='卖出', now=1., next=0.)
        out['strategies']['rsi2']['rows'].append(sold)
        trade_signals.record_publication(out, cache=self.base, now=NOW)
        days = pd.bdate_range('2021-01-18', periods=70)
        price = 10 * 1.01 ** np.arange(70)
        stock = pd.DataFrame({'Open':price,'Close':price}, index=days)
        bench = pd.DataFrame({'Open':10.,'Close':10.}, index=days)
        stats = tracking.evaluate_trade_list('rsi2', cache=self.base, prices_loader=lambda market, code, cfg: bench if code=='510300' else stock)
        groups = {group['action']:group for group in stats['groups']}
        self.assertEqual(groups['买入']['n5'], 1)
        self.assertAlmostEqual(groups['买入']['avg5'], (1.01**4-1)*100)
        self.assertEqual(groups['买入']['beat20'], 100.)
        self.assertEqual(groups['卖出']['n60'], 1)
        self.assertIn('卖出策略收益', stats['note'])
        partial = tracking.evaluate_trade_list('rsi2', cache=self.base, prices_loader=lambda market, code, cfg: (bench if code=='510300' else stock).iloc[:3])
        self.assertTrue(all(group['n5']==0 for group in partial['groups']))

    def test_mine_is_private_and_fixed_stock_is_first(self):
        self.generate()
        private = trade_private.mine('rsi2', user={'id':1}, cache=self.base,
                                     watchlist={'600009':'自选九'}, positions=['600001'])
        self.assertEqual(private['rows'][0]['code'], '600531')
        row = next(row for row in private['rows'] if row['code']=='600001')
        self.assertTrue(row['in_member_positions'])
        for path in (self.base/'trade_list_cn.json', self.base/'trade_list'/f'{DAY}.json'):
            content = path.read_text()
            for key in ('in_member_positions','in_watchlist','member_logged_in'):
                self.assertNotIn(key, content)

    def test_notification_summary_deduplicates_and_contains_only_counts(self):
        payload={'data_date':DAY, 'default_strategy':'blend2', 'strategies':{'blend2':{'rows':[
            {'code':'600001','action':'买入','small_target':False},
            {'code':'600001','action':'买入','small_target':False},
            {'code':'600002','action':'买入','small_target':True},
            {'code':'600003','action':'卖出'}, {'code':'600003','action':'卖出'}]}}}
        counts=trade_notifications.summary(payload, ['600003','600003'])
        self.assertEqual(counts, {'buy':1,'sell':1,'related_sell':1})
        sent=[]
        self.assertTrue(trade_notifications.send_publication(payload, ['600003'], recent=[], sender=lambda *args:sent.append(args)))
        self.assertNotIn('600003', sent[0][1])
        self.assertFalse(trade_notifications.send_publication(payload, [], recent=[{'title':sent[0][0]}], sender=lambda *args:sent.append(args)))
        self.assertEqual(len(sent),1)

    def test_only_verified_live_local_session_hashes_contribute_positions(self):
        database=self.base/'members.db'
        with patch.object(members, 'DB', database):
            with members._conn() as connection:
                for uid, disabled in ((1,0),(2,1),(3,0)):
                    connection.execute('INSERT INTO users(id,name,salt,pw,created,disabled) VALUES(?,?,?,?,?,?)', (uid,'测试'+str(uid),b'x',b'x','',disabled))
                    connection.execute('INSERT INTO sessions VALUES(?,?,?)', (str(uid)*64,uid,members.time.time()+(1000 if uid!=3 else -1000)))
                    connection.execute('INSERT INTO positions VALUES(?,?,?,?,?,?)',(uid,'cn','60000'+str(uid),'测试',100,10))
            self.assertEqual(members.position_codes_for_sessions(['1'*64,'2'*64,'3'*64]), {'600001'})
            self.assertEqual(members.position_codes_for_sessions([]), set())

    def test_frontend_loading_search_sort_tabs_and_errors(self):
        self.generate()
        payload = T.load(cache=self.base)
        row = copy.deepcopy(T.load(strategy='rsi2', cache=self.base)['rows'][0])
        first = {**row, 'code': '600001', 'name': '测试甲', 'action': '买入',
                 'performance': {**row['performance'], 'total': 5}, 'liquidity': '正常', 'avg_turnover_20d': 90000000}
        second = {**row, 'code': '600002', 'name': '测试乙', 'action': '买入',
                  'performance': {**row['performance'], 'total': 50}, 'liquidity': '正常', 'avg_turnover_20d': 60000000}
        small = {**row, 'code': '600003', 'name': '小仓位丙', 'action': '买入', 'small_target': True, 'next': .03125, 'liquidity': '正常', 'avg_turnover_20d': 70000000}
        first['next'], second['next'] = .25, .75
        illiquid = {**row, 'code': '600004', 'name': '未知丁', 'action': '买入', 'liquidity': '流动性未知', 'avg_turnover_20d': None}
        payload.update(rows=[second, first, small, illiquid], exceptions=[], binary=False)
        script = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
class Element {
  constructor() { this.value = ''; this.textContent = ''; this.innerHTML = ''; this.events = {};
    this.style = {setProperty() {}}; this.classList = {toggle() {}}; this.dataset = {}; }
  addEventListener(name, callback) { this.events[name] = callback; }
  querySelectorAll() { return []; }
  setAttribute() {}
  // 和浏览器 Element.replaceChildren 一致：先清空原有子节点，再按参数顺序追加；非节点参数(字符串等)变成文本节点
  replaceChildren(...nodes) {
    this.childNodes = nodes.map(n => (n !== null && typeof n === 'object') ? n : {nodeType: 3, textContent: String(n)});
    this.innerHTML = '';
    this.textContent = this.childNodes.map(n => n.textContent ?? '').join('');
  }
}
const elements = new Map();
const get = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
const tabs = ['buy', 'sell', 'adjust'].map(tab => { const el = new Element(); el.dataset.tab = tab; return el; });
get('tradeTabs').querySelectorAll = () => tabs;
let response = input.payload, calls = 0;
const document = {getElementById: get, querySelector: () => ({offsetHeight: 110}),
  documentElement: {getAttribute: () => 'term'}, querySelectorAll: () => []};
const context = vm.createContext({document, window: {addEventListener() {}},
  console, URLSearchParams, setTimeout, clearTimeout,
  fetch: async (url) => { calls++; return {status: 200, json: async () => url === '/api/trade_list_dates' ? {dates: ['2021-01-15'], latest: '2021-01-15'} : url.startsWith('/api/trade_list_tracking') ? {empty: true, groups: [], note: '只统计上线后信号'} : response}; }});
const source = fs.readFileSync('dashboard/app.js', 'utf8').replace(/\ninit\(\);\s*$/, '\n');
vm.runInContext(source, context);
context.input = input;
vm.runInContext('S.meta = {strategies: input.strategies}; S.market = "cn";', context);
(async () => {
  await vm.runInContext('loadTrade()', context);
  assert.ok(get('tradeTable').innerHTML.indexOf('测试甲') < get('tradeTable').innerHTML.indexOf('测试乙'));
  assert.ok(get('tradeTable').innerHTML.includes('#/stock/600001'));
  assert.equal(get('tradeStrategy').value, 'blend2');
  assert.ok(!get('tradeTable').innerHTML.includes('小仓位丙'));
  assert.ok(get('tradeSmallTable').innerHTML.includes('小仓位丙'));
  assert.ok(!get('tradeSmall').open);
  assert.ok(get('tradeTable').innerHTML.includes('未知丁'));
  assert.ok(!get('tradeCoverageWarning').hidden);
  assert.ok(get('tradeCoverageWarning').textContent.includes('75.0% 已知'));
  assert.ok(!get('tradeIlliquid').open);
  for (const knownCount of [0,5,8,10]) {
    context.coverageRows = Array.from({length:10}, (_, i) => ({...input.payload.rows[0],
      code: String(600001+i), name: '覆盖率'+i, small_target:false,
      liquidity: i < knownCount ? (i===0 ? '流动性差' : '正常') : '流动性未知',
      avg_turnover_20d: i < knownCount ? (i===0 ? 1000 : 60000000+i*1000000) : null})).reverse();
    vm.runInContext('TRADE.data = {...input.payload, rows:coverageRows}; TRADE.manualSort=false; renderTrade();',context);
    const incomplete = knownCount < 8;
    assert.equal(get('tradeCoverageWarning').hidden, !incomplete);
    assert.equal(get('tradeLiquidityWarning').hidden, !incomplete);
    assert.equal(vm.runInContext('TRADE.sort',context), incomplete ? 'code' : 'avg_turnover_20d');
    if (incomplete) {
      assert.ok(get('tradeCoverageWarning').textContent.includes(`${knownCount*10}.0% 已知`));
      assert.ok(get('tradeTable').innerHTML.indexOf('#/stock/600002') < get('tradeTable').innerHTML.indexOf('#/stock/600010'));
    } else {
      const top = knownCount===8 ? '600008' : '600010';
      assert.ok(get('tradeTable').innerHTML.indexOf('#/stock/'+top) < get('tradeTable').innerHTML.indexOf('#/stock/600002'));
    }
    if (knownCount>0) {
      assert.ok(!get('tradeTable').innerHTML.includes('#/stock/600001'));
      assert.ok(get('tradeIlliquidTable').innerHTML.includes('#/stock/600001'));
    }
    if (knownCount<10) {
      assert.equal(get('tradeTable').innerHTML.includes('#/stock/600010'),incomplete);
      assert.equal(get('tradeIlliquidTable').innerHTML.includes('#/stock/600010'),!incomplete);
    }
    get('tradeLiquidity').value='流动性未知'; vm.runInContext('renderTrade()',context);
    assert.equal(get('tradeCoverageWarning').hidden,!incomplete);
    get('tradeLiquidity').value='';
  }
  vm.runInContext('TRADE.data=input.payload; TRADE.manualSort=false; renderTrade();',context);
  vm.runInContext('downloadCSV = (name, header, rows) => { globalThis.csvExport = {name, header: header.map(x => x[1]), codes: rows.map(x => x.code)}; }; exportTrade();', context);
  assert.ok(context.csvExport.header.includes('20日平均成交额（元）'));
  assert.equal(context.csvExport.codes.length, 4);
  assert.ok(context.csvExport.codes.includes('600004'));
  get('tradeLiquidity').value = '流动性未知'; vm.runInContext('renderTrade(); exportTrade();', context);
  assert.equal(context.csvExport.codes.join(','), '600004');
  get('tradeLiquidity').value = ''; vm.runInContext('renderTrade()', context);
  assert.ok(!get('tradeStrategy').innerHTML.includes('buy_hold'));
  vm.runInContext('TRADE.sort = "next"; TRADE.dir = -1; TRADE.manualSort=true; renderTrade();', context);
  assert.ok(get('tradeTable').innerHTML.indexOf('测试乙') < get('tradeTable').innerHTML.indexOf('测试甲'));
  assert.ok(get('tradeMeta').textContent.includes('2021-01-15 16:00 北京时间'));
  get('tradeSearch').value = '测试乙'; vm.runInContext('renderTrade()', context);
  assert.ok(get('tradeTable').innerHTML.includes('测试乙'));
  assert.ok(!get('tradeTable').innerHTML.includes('测试甲'));
  get('tradeSearch').value = '';
  vm.runInContext('TRADE.sort = "total"; TRADE.dir = -1; TRADE.manualSort=true; renderTrade();', context);
  assert.ok(get('tradeTable').innerHTML.indexOf('测试乙') < get('tradeTable').innerHTML.indexOf('测试甲'));
  vm.runInContext('TRADE.data.binary = true; TRADE.tab = "adjust"; renderTrade();', context);
  assert.equal(get('tradeMessage').textContent, '该策略仅有空仓或满仓，没有加减仓信号');
  vm.runInContext('S.market = "us";', context);
  const before = calls; await vm.runInContext('loadTrade()', context); assert.equal(calls, before);
  assert.ok(get('tradeMessage').textContent.includes('美股'));
  vm.runInContext('S.market = "cn";', context);
  response = {empty: true, message: '尚无有效清单'};
  await vm.runInContext('loadTrade()', context); assert.equal(get('tradeMessage').textContent, response.message);
  response = {error: '日期无效'};
  await vm.runInContext('loadTrade()', context); assert.equal(get('tradeMessage').textContent, response.error);
  console.log('前端加载、默认代码排序、搜索、手动排序、标签、空态、错误态验证通过');
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        # 使用轻量 DOM 替身验证脚本逻辑，不打开浏览器，也不访问受限的本机页面。
        script = script.replace(r'/\\ninit\\(\\);\\s*$/', r'/\ninit\(\);\s*$/')
        script = script.replace("'\\\\n'", "'\\n'")
        process = subprocess.run(['node', '-e', script], cwd=ROOT, text=True,
                                 input=json.dumps({'payload': payload, 'strategies': registry.STRATEGIES}),
                                 capture_output=True, timeout=20)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn('验证通过', process.stdout)

    def test_api_auth_and_shared_readonly_without_loading_secrets(self):
        self.generate()
        # 只编译 Handler 需要的方法，不导入 app.py；该模块顶层会读取令牌。
        tree = ast.parse((ROOT / 'app.py').read_text())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'Handler')
        methods = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in ('do_GET', '_guard', '_authed', '_remote', '_maintenance_blocked', '_owner', '_local_or_owner')]
        safe_class = ast.ClassDef(name='SafeHandler', bases=[], keywords=[], body=methods, decorator_list=[])
        code = ast.fix_missing_locations(ast.Module(body=[safe_class], type_ignores=[]))
        namespace = {'urlparse': urlparse, 'parse_qs': parse_qs, 'SimpleCookie': SimpleCookie,
                     'hmac': hmac, 'COOKIE': 'qb_session', 'TOKEN': 'fake-test-token',
                     'MEMBER_COOKIE': 'qb_member', 'ALLOWED_HOSTS': {'127.0.0.1:8765'}, '_bad_login': [], '_alive': {},
                     'time': __import__('time'), 'trade_list': T, 'trade_private': trade_private, 'tracking': tracking, 'scan': scan, 'admin': SimpleNamespace(is_local_request=lambda headers, allowed: headers.get('Host') in allowed and not headers.get('X-Forwarded-For'))}
        exec(compile(code, '<isolated routes>', 'exec'), namespace)
        handler = namespace['SafeHandler']()
        handler.path = '/api/trade_list?strategy=rsi2'
        handler._share_gate = lambda u: False
        handler._member_user = lambda: None
        handler._owner_cfg = lambda: None          # 真实方法会读 owner_access.json（主人通道配置）；这里按"没配主人通道"处理，不碰真实文件
        handler._personal_mode = False
        handler._personal_blocked = lambda: False  # 真实方法会读 personal_mode.json；这里按"个人模式关闭"（默认，不改变既有分派）处理
        replies = []
        handler._send = lambda status, *args: replies.append((status, None))
        handler._json = lambda body, status=200: replies.append((status, body))
        real_load = T.load
        with patch.object(T, 'load', side_effect=lambda *args: real_load(*args, cache=self.base)):
            for host in ('127.0.0.1:8765', 'test.trycloudflare.com'):
                handler.headers = {'Host': host}
                handler.do_GET()
                self.assertEqual(replies[-1][0], 401)
                handler.headers['Cookie'] = 'qb_session=fake-test-token'
                handler.do_GET()
                self.assertEqual(replies[-1][0], 200)
                self.assertEqual(replies[-1][1]['strategy'], 'rsi2')
            handler.path = '/api/trade_list_dates'
            with patch.object(T, 'dates', return_value={'dates': [DAY], 'latest': DAY}):
                handler.do_GET()
                self.assertEqual(replies[-1], (200, {'dates': [DAY], 'latest': DAY}))
            handler.path = '/api/trade_list_mine'
            handler.do_GET()
            self.assertEqual(replies[-1][0], 403)
            handler.headers = {'Host': '127.0.0.1:8765', 'Cookie': 'qb_session=fake-test-token', 'X-Forwarded-For': '192.0.2.1'}
            handler.do_GET()
            self.assertEqual(replies[-1][0], 403)
            handler.headers.pop('X-Forwarded-For')
            with patch.object(trade_private, 'mine', return_value={'rows': []}):
                handler.do_GET()
                self.assertEqual(replies[-1], (200, {'rows': []}))
            handler.path = '/api/trade_list?date=../../secret'
            handler.do_GET()
            self.assertEqual(replies[-1][0], 400)


class ZIsolationTests(unittest.TestCase):
    def test_daily_ledger_and_simtrade_are_isolated_in_setup(self):
        """run_market 会调用 daily._ledger/_simtrade，它们必须在测试里被替换，否则会真的写账本。"""
        t = TradeListTests('setUp')
        t.setUp()
        try:
            self.assertIsInstance(daily._ledger, MagicMock)
            self.assertIsInstance(daily._simtrade, MagicMock)
        finally:
            t.doCleanups()
        self.assertNotIsInstance(daily._ledger, MagicMock)         # 清理后恢复原函数


if __name__ == '__main__':
    unittest.main(verbosity=2)
