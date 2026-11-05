"""B01：每日自动任务在“全市场已做完、只差主 ETF 日线”时不再重复全市场快照和回测。

背景：新浪的 ETF 日线常到北京时间晚上 8–9 点才有当天数据，在此之前汇总日期落后一天，
needs_update 每 15 分钟判定一次“需要更新”，旧代码每次都整套重跑，重跑扫描会刷新 scan_cn_meta.json 的
generated_at，已发布清单被判为过期，用完每天 3 次自动发布后反复打印「今日买卖暂不自动重试」。

全部在临时目录里构造状态文件，不联网、不读写 data/ 下的真实文件。
运行：.venv/bin/python tests/test_daily_rerun.py
"""
import json
import logging
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src import trade_list as T
import daily
sys.path.insert(0, str(ROOT / 'tests'))
from realdata_guard import ZRealDataUntouchedTests  # noqa: E402,F401  真实数据守卫（必须在所有测试类之后运行）

DAY = '2021-01-15'
NOW = datetime(2021, 1, 15, 21, tzinfo=ZoneInfo('Asia/Shanghai'))
SCAN_AT = '2021-01-15T20:17:38+08:00'
INDEX = pd.bdate_range(end='2021-01-14', periods=300)   # 主 ETF 日线还停在前一交易日
ETF = pd.DataFrame({'Open': 1.0, 'High': 1.0, 'Low': 1.0, 'Close': np.linspace(1, 2, len(INDEX)), 'Volume': 1.0},
                   index=INDEX)
CALENDAR = [date(2021, 1, 14), date(2021, 1, 15)]


class DailyRerunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='quant_daily_rerun_')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        # 与 10-29 真实日志同构的状态：扫描完成、清单已按这次扫描发布、当天 3 次自动尝试都已成功用完
        self.write_json('cn_all_progress.json', {'trade_day': DAY, 'complete': True})
        self.write_json('scan_cn_meta.json', {'data_date': DAY, 'generated_at': SCAN_AT})
        listing = {'version': T.VERSION, 'data_date': DAY, 'threshold': T.TRADE_LIST_THRESHOLD,
                   'min_target': T.TRADE_LIST_MIN_TARGET, 'min_turnover': T.TRADE_LIST_MIN_TURNOVER,
                   'scan_generated_at': SCAN_AT, 'strategies': {}}
        self.write_json('trade_list_cn.json', listing)
        self.write_json(f'trade_list/{DAY}.json', listing)
        self.write_json('trade_list_attempts.json', {'day': DAY, 'count': 3, 'last_attempt': SCAN_AT, 'error': ''})
        self.addCleanup(patch.stopall)
        patch.object(T, 'CACHE', self.base).start()   # pending_day / needs_generation 默认读 CACHE，指到临时目录
        patch.object(T.allmarket, 'trade_calendar', return_value=CALENDAR).start()
        # 账本和模拟盘会读写 data/ 下的真实文件，一律换成空函数
        self.ledger, self.simtrade = patch.object(daily, '_ledger').start(), patch.object(daily, '_simtrade').start()

    def write_json(self, name, value):
        p = self.base / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')

    def run_market(self, auto):
        """只替换联网/重计算/写真实文件的函数；current_day、pending_day 用真实实现读临时目录。"""
        cfg = {'cn': {**daily.MARKETS['cn'], 'universe': ['510300']}}
        with patch.object(daily, 'MARKETS', cfg), \
             patch.object(T, 'ready_day', return_value=DAY), \
             patch.object(daily.datasource, 'update', return_value=(ETF, None, False)) as etf_update, \
             patch.object(daily.datasource, 'load_watchlist', return_value={}), \
             patch.object(daily.datasource, 'save_watchlist'), \
             patch.object(daily.time, 'sleep'), \
             patch.object(daily.allmarket, 'daily_update', return_value={}) as snapshot, \
             patch.object(daily.allmarket, 'load_progress', return_value={'complete': True}), \
             patch.object(daily.allmarket, 'cached_codes', return_value=['600001']), \
             patch.object(daily.scan, 'run', side_effect=self.rescan) as scan_run, \
             patch.object(daily, '_backfill_trade_turnover'), \
             patch.object(T, 'generate', return_value={'data_date': DAY}) as generate, \
             patch.object(daily.datasource, 'load_market', return_value={'510300': ETF}), \
             patch.object(daily.daily_report, 'load', return_value={'data_date': '2021-01-14'}), \
             patch.object(daily.daily_report, 'build', return_value={'data_date': '2021-01-14'}) as build, \
             patch.object(daily.daily_report, 'save'), patch.object(daily.daily_report, 'log_signals'), \
             patch.object(daily, 'signals_and_alerts'), \
             self.assertLogs(level='INFO') as logs:
            daily.run_market('cn', auto=auto)
        return SimpleNamespace(etf_update=etf_update, snapshot=snapshot, scan_run=scan_run, generate=generate,
                               build=build, logs=logs.output)

    def rescan(self, *args, **kwargs):
        """模拟真实 scan.run：数据没变也会用当前时间重写扫描元数据。"""
        self.write_json('scan_cn_meta.json', {'data_date': DAY, 'generated_at': '2021-01-15T21:47:54+08:00'})
        return {'data_date': DAY, 'stocks': 1, 'seconds': 0}

    def test_current_day_only_when_progress_scan_and_listing_all_current(self):
        self.assertEqual(T.current_day('cn', cache=self.base), DAY)
        self.assertIsNone(T.current_day('us', cache=self.base))
        self.rescan()   # 扫描在清单发布后又被重写：清单不再算最新
        self.assertIsNone(T.current_day('cn', cache=self.base))
        self.write_json('scan_cn_meta.json', {'data_date': DAY, 'generated_at': SCAN_AT})
        self.write_json('cn_all_progress.json', {'trade_day': DAY, 'complete': False})   # 全市场还在续传
        self.assertIsNone(T.current_day('cn', cache=self.base))

    def test_current_day_is_read_only_and_never_logs_retry(self):
        before = {p: p.read_bytes() for p in self.base.rglob('*') if p.is_file()}
        with self.assertNoLogs(level='INFO'):
            T.current_day('cn', cache=self.base)
        self.assertEqual(before, {p: p.read_bytes() for p in self.base.rglob('*') if p.is_file()})

    def test_rescan_after_publish_is_what_triggers_retry_message(self):
        """复现根因：清单本身没问题，只是扫描时间戳被重写，3 次机会已用完，于是 needs_generation 打印提示。"""
        with self.assertNoLogs(level='INFO'):
            self.assertFalse(T.needs_generation(DAY, cache=self.base, now=NOW))
        self.rescan()
        with self.assertLogs(level='INFO') as logs:
            self.assertFalse(T.needs_generation(DAY, cache=self.base, now=NOW))
        self.assertIn('今日买卖暂不自动重试：同一发布日已自动尝试 3 次', logs.output[0])

    def test_auto_run_with_current_listing_only_refreshes_etf_and_summary(self):
        calls = self.run_market(auto=True)
        calls.etf_update.assert_called_once()        # 主 ETF 照常重新下载，晚到的当天日线能补上
        calls.build.assert_called_once()             # 汇总照常重建
        calls.snapshot.assert_not_called()           # 不再每 15 分钟请求一次全市场快照（东方财富接口每天只用一次）
        calls.scan_run.assert_not_called()           # 不再重扫，扫描时间戳不变
        calls.generate.assert_not_called()           # 清单已是最新，不重新生成、不消耗自动尝试次数
        self.assertEqual(json.loads((self.base / 'trade_list_attempts.json').read_text(encoding='utf-8'))['count'], 3)
        self.ledger.assert_not_called()
        self.simtrade.assert_not_called()
        self.assertEqual(T.current_day('cn', cache=self.base), DAY)
        self.assertFalse(any('暂不自动重试' in line for line in calls.logs))
        self.assertTrue(any('[步骤 2/2] 汇总' in line for line in calls.logs))
        with self.assertNoLogs(level='INFO'):        # 下一次 needs_update 里也不会再打印
            self.assertFalse(T.needs_generation(DAY, cache=self.base, now=NOW))

    def test_auto_run_still_does_full_update_when_listing_outdated(self):
        self.rescan()   # 扫描比清单新：不是“全做完”的状态，必须走原来的完整流程
        calls = self.run_market(auto=True)
        calls.snapshot.assert_called_once()
        calls.scan_run.assert_called_once()
        self.assertTrue(any('[步骤 3/6]' in line for line in calls.logs))
        # 3 次自动机会已用完：这是原有设计（失败重试上限），本次修复不改它
        calls.generate.assert_not_called()
        self.assertTrue(any('暂不自动重试' in line for line in calls.logs))

    def test_manual_run_keeps_full_update(self):
        with patch.object(T, 'current_day', wraps=T.current_day) as current:
            calls = self.run_market(auto=False)
        current.assert_not_called()
        calls.snapshot.assert_called_once()
        calls.scan_run.assert_called_once()
        calls.generate.assert_called_once()          # 手动运行不受 3 次上限约束，行为与修复前相同


if __name__ == '__main__':
    logging.disable(logging.NOTSET)
    unittest.main(verbosity=2)
