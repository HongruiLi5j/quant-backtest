"""E03：每日任务失败不再静默——daily.py 每次运行结束写 logs/daily_status.json，连续失败 ≥2 次发提醒。

全部在临时目录里构造状态文件；通知用假函数（不发 Mac 通知、不写 data/daily/notifications.json）；
run_market 用假函数（不联网、不读写 data/ 下的真实文件）。
运行：.venv/bin/python tests/test_daily_status_e03.py
"""
import contextlib
import io
import json
import logging
import os
import signal
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src import daily_status as DS
import daily
sys.path.insert(0, str(ROOT / 'tests'))
from realdata_guard import ZRealDataUntouchedTests  # noqa: E402,F401  真实数据守卫（必须在所有测试类之后运行）

T0 = datetime(2026, 10, 30, 8, 0, 0, tzinfo=timezone.utc)   # 北京时间 10-30 16:00
LABELS = {'cn': 'A 股', 'us': '美股'}


class Clock:
    """可手动拨动的假时钟；每次调用前进 1 秒，保证 started_at < finished_at。"""
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        self.t += timedelta(seconds=1)
        return self.t


class StatusFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='quant_daily_status_')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'logs' / 'daily_status.json'
        self.clock = Clock()
        self.notify = MagicMock()
        self.addCleanup(setattr, DS, '_current', None)

    def run_once(self, outcomes, notify=True, **kw):
        """outcomes: {'cn': None 表示成功 / 异常对象或字符串表示失败}；返回写入的状态。"""
        run = DS.DailyRun(list(outcomes), labels=LABELS, path=self.path, now_fn=self.clock, **kw).start()
        for m, err in outcomes.items():
            run.market_ok(m) if err is None else run.market_error(m, err)
        return run.finish(self.notify if notify else None)

    def read(self):
        return json.loads(self.path.read_text(encoding='utf-8'))

    # ------------------------------------------------------------ 状态文件内容
    def test_成功一次_写出完整字段且不通知(self):
        doc = self.run_once({'cn': None, 'us': None}, auto=True)
        self.assertEqual(self.read(), doc)
        self.assertEqual(doc['state'], 'ok')
        self.assertEqual(doc['consecutive_failures'], 0)
        self.assertEqual({m: r['status'] for m, r in doc['markets'].items()}, {'cn': 'ok', 'us': 'ok'})
        self.assertEqual(doc['markets']['cn']['name'], 'A 股')
        self.assertTrue(doc['started_at'] < doc['finished_at'])
        self.assertGreater(doc['duration_seconds'], 0)
        self.assertEqual(doc['mode'], {'auto': True, 'offline': False})
        self.assertEqual(doc['last_success_at'], doc['finished_at'])
        self.assertIsNone(doc['last_failure_at'])
        self.notify.assert_not_called()

    def test_运行中状态可见_市场还是pending(self):
        run = DS.DailyRun(['cn'], labels=LABELS, path=self.path, now_fn=self.clock).start()
        doc = self.read()
        self.assertEqual(doc['state'], 'running')
        self.assertIsNone(doc['finished_at'])
        self.assertEqual(doc['markets']['cn']['status'], 'pending')
        run.market_ok('cn')
        self.assertEqual(self.read()['markets']['cn']['status'], 'ok')   # 每个市场做完就落盘一次
        run.finish()

    def test_写盘是原子的_不留临时文件且中途失败不破坏旧文件(self):
        self.run_once({'cn': None})
        before = self.path.read_text(encoding='utf-8')
        with patch.object(DS.json, 'dump', side_effect=OSError('磁盘满')):
            with self.assertRaises(OSError):
                DS._write_atomic(self.path, {'x': 1})
        self.assertEqual(self.path.read_text(encoding='utf-8'), before)
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ['daily_status.json'])

    def test_状态文件写不进去也不抛异常(self):
        blocker = Path(self.temp.name) / 'file'
        blocker.write_text('x')
        run = DS.DailyRun(['cn'], path=blocker / 'sub' / 'daily_status.json', now_fn=self.clock)
        with self.assertLogs(level='ERROR'):
            run.start()
            run.market_ok('cn')
            run.finish()

    def test_状态文件损坏当作没有历史(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text('{坏的', encoding='utf-8')
        doc = self.run_once({'cn': 'RuntimeError: 没数据'})
        self.assertEqual(doc['consecutive_failures'], 1)

    # ------------------------------------------------------------ 连续失败计数
    def test_失败计数累加_成功后清零且保留上次失败时间(self):
        d1 = self.run_once({'cn': 'RuntimeError: 没数据'}, notify=False)
        d2 = self.run_once({'cn': 'RuntimeError: 没数据'}, notify=False)
        self.assertEqual((d1['consecutive_failures'], d2['consecutive_failures']), (1, 2))
        self.assertEqual(d2['state'], 'failed')
        self.assertEqual(d2['markets']['cn']['error'], 'RuntimeError: 没数据')
        d3 = self.run_once({'cn': None})
        self.assertEqual(d3['consecutive_failures'], 0)
        self.assertEqual(d3['state'], 'ok')
        self.assertEqual(d3['last_failure_at'], d2['last_failure_at'])
        self.assertEqual(d3['last_success_at'], d3['finished_at'])

    def test_任一市场失败就算这次失败(self):
        doc = self.run_once({'cn': None, 'us': ValueError('x')})
        self.assertEqual(doc['state'], 'failed')
        self.assertEqual(doc['markets']['us']['error'], 'ValueError: x')
        self.assertEqual(doc['consecutive_failures'], 1)

    def test_被吞掉的小问题只记warnings_不算失败(self):
        run = DS.DailyRun(['cn'], labels=LABELS, path=self.path, now_fn=self.clock).start()
        DS.note('cn', '模拟盘更新失败')
        DS.note('cn', '3 个品种行情更新失败：510300、510500、159915')
        run.market_ok('cn')
        doc = run.finish(self.notify)
        self.assertEqual(doc['state'], 'ok')
        self.assertEqual(doc['consecutive_failures'], 0)
        self.assertEqual(len(doc['markets']['cn']['warnings']), 2)
        self.notify.assert_not_called()

    def test_没有正在进行的运行时note什么都不做(self):
        self.assertIsNone(DS.current_run())
        DS.note('cn', '随便什么')   # 不应抛异常

    def test_warnings条数有上限(self):
        run = DS.DailyRun(['cn'], path=self.path, now_fn=self.clock).start()
        for i in range(50):
            DS.note('cn', f'问题 {i}')
        run.market_ok('cn')
        self.assertEqual(len(run.finish()['markets']['cn']['warnings']), DS.MAX_WARNINGS)

    def test_上次没正常收尾_提示但不计入失败(self):
        DS.DailyRun(['cn'], path=self.path, now_fn=self.clock).start()   # 开始后进程"被杀"，没有 finish
        DS._current = None
        doc = self.run_once({'cn': None})
        self.assertTrue(doc['previous_run_interrupted'])
        self.assertEqual(doc['consecutive_failures'], 0)
        self.assertFalse(self.run_once({'cn': None})['previous_run_interrupted'])

    def test_强制中断abort_没跑完的市场记为失败并计数(self):
        run = DS.DailyRun(['cn', 'us'], labels=LABELS, path=self.path, now_fn=self.clock).start()
        run.market_ok('us')
        doc = run.abort('自动任务运行超过 3 小时被强制退出')
        self.assertEqual(doc['state'], 'failed')
        self.assertEqual(doc['markets']['cn']['error'], '自动任务运行超过 3 小时被强制退出')
        self.assertEqual(doc['markets']['us']['status'], 'ok')
        self.assertEqual(doc['consecutive_failures'], 1)
        run.market_ok('cn')   # 收尾后再有调用也不会把最终状态改回去
        self.assertEqual(self.read()['state'], 'failed')

    # ------------------------------------------------------------ 提醒
    def test_第一次失败不提醒_连续第二次才提醒(self):
        self.run_once({'cn': RuntimeError('主品种 510300 没有数据')})
        self.notify.assert_not_called()
        self.run_once({'cn': RuntimeError('主品种 510300 没有数据')})
        self.assertEqual(self.notify.call_count, 1)
        title, body = self.notify.call_args.args
        self.assertIn('连续失败', title)
        self.assertIn('已连续失败 2 次', body)
        self.assertIn('A 股', body)
        self.assertIn('主品种 510300 没有数据', body)

    def test_同一原因一天只提醒一次_数字变化算同一原因(self):
        self.run_once({'cn': RuntimeError('超时 30 秒（第 1 次）')})
        self.run_once({'cn': RuntimeError('超时 30 秒（第 2 次）')})
        self.run_once({'cn': RuntimeError('超时 31 秒（第 3 次）')})
        self.run_once({'cn': RuntimeError('超时 32 秒（第 4 次）')})
        self.assertEqual(self.notify.call_count, 1)
        self.assertEqual(self.read()['consecutive_failures'], 4)   # 计数照常增长，只是不再重复弹
        self.assertEqual(len(self.read()['alerts']['reasons']), 1)

    def test_同一天出现新原因会再提醒(self):
        self.run_once({'cn': RuntimeError('网络不通')})
        self.run_once({'cn': RuntimeError('网络不通')})
        self.run_once({'cn': KeyError('主品种')})
        self.assertEqual(self.notify.call_count, 2)
        self.assertIn('KeyError', self.notify.call_args.args[1])
        self.assertNotIn('网络不通', self.notify.call_args.args[1])   # 老原因不重复写

    def test_不同市场同样的错误文字是不同原因(self):
        self.run_once({'cn': RuntimeError('没数据'), 'us': RuntimeError('没数据')})
        self.run_once({'cn': RuntimeError('没数据'), 'us': RuntimeError('没数据')})
        body = self.notify.call_args.args[1]
        self.assertEqual(self.notify.call_count, 1)   # 同一次运行的两个新原因合并成一条通知
        self.assertIn('A 股', body)
        self.assertIn('美股', body)

    def test_隔天同一原因再提醒一次(self):
        self.run_once({'cn': RuntimeError('网络不通')})
        self.run_once({'cn': RuntimeError('网络不通')})
        self.assertEqual(self.notify.call_count, 1)
        self.clock.t += timedelta(days=1)
        self.run_once({'cn': RuntimeError('网络不通')})
        self.assertEqual(self.notify.call_count, 2)

    def test_一天按北京时间算_UTC跨零点但北京同一天不重复提醒(self):
        # 北京时间 10-30 07:00 = UTC 10-29 23:00；之后到 UTC 10-30 00:30 仍是北京 10-30，同一天
        self.clock.t = datetime(2026, 10, 29, 23, 0, tzinfo=timezone.utc)
        self.run_once({'cn': RuntimeError('网络不通')})
        self.run_once({'cn': RuntimeError('网络不通')})
        self.clock.t = datetime(2026, 10, 30, 0, 30, tzinfo=timezone.utc)
        self.run_once({'cn': RuntimeError('网络不通')})
        self.assertEqual(self.notify.call_count, 1)

    def test_成功之后再失败_要重新连续两次才提醒(self):
        self.run_once({'cn': RuntimeError('网络不通')})
        self.run_once({'cn': None})
        self.run_once({'cn': RuntimeError('网络不通')})
        self.notify.assert_not_called()

    def test_通知函数抛异常不影响运行_下次再试(self):
        self.notify.side_effect = OSError('osascript 不可用')
        self.run_once({'cn': RuntimeError('网络不通')})
        with self.assertLogs(level='ERROR'):
            doc = self.run_once({'cn': RuntimeError('网络不通')})
        self.assertEqual(doc['state'], 'failed')
        self.assertEqual(doc['alerts']['reasons'], [])   # 没发成功，不能记成"已提醒"
        self.notify.side_effect = None
        self.run_once({'cn': RuntimeError('网络不通')})
        self.assertEqual(self.notify.call_count, 2)       # 第二次运行尝试失败 + 第三次运行重试成功
        self.assertEqual(len(self.read()['alerts']['reasons']), 1)

    def test_没传通知函数就只写状态(self):
        self.run_once({'cn': RuntimeError('a')}, notify=False)
        doc = self.run_once({'cn': RuntimeError('a')}, notify=False)
        self.assertEqual(doc['consecutive_failures'], 2)
        self.assertEqual(doc['alerts']['reasons'], [])

    def test_错误文字被截断且压成一行(self):
        text = DS.format_error(RuntimeError('第一行\n第二行 ' + 'x' * 1000))
        self.assertNotIn('\n', text)
        self.assertLessEqual(len(text), DS.MAX_TEXT)


class DailyMainIntegrationTests(unittest.TestCase):
    """真的调 daily.main()，run_market / 通知 / 导出离线版都换成假的。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='quant_daily_main_')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        (self.base / 'logs').mkdir()
        self.status = self.base / 'logs' / 'daily_status.json'
        # logging.basicConfig 在 root 已有 handler 时什么都不做，所以先摘下来，测完装回去
        root = logging.getLogger()
        saved, level = root.handlers[:], root.level
        for h in saved:
            root.removeHandler(h)
        self.addCleanup(self._restore_root, saved, level)
        old_umask = os.umask(0)
        os.umask(old_umask)
        self.addCleanup(os.umask, old_umask)      # main() 会改 umask
        self.addCleanup(patch.stopall)
        self.addCleanup(setattr, DS, '_current', None)
        # main() 会装一个 3 小时的 SIGALRM 看门狗；不取消的话会一直留在测试进程里，
        # 后面 market_monitor 的 deadline() 看到闹钟已被占用就拒绝工作（hard_timeout_unavailable）
        prev_alarm = signal.getsignal(signal.SIGALRM)
        self.addCleanup(signal.signal, signal.SIGALRM, prev_alarm)
        self.addCleanup(signal.alarm, 0)
        patch.object(daily, 'ROOT', self.base).start()
        # 同一进程里连着调多次 main()，上一次的锁文件对象还没被回收，真锁会让后面几次被判为"另一个任务在跑"；锁本身不是这里要测的
        patch.object(daily.fcntl, 'flock').start()
        patch.object(daily, 'LOG', self.base / 'logs' / 'daily.log').start()
        patch.object(DS, 'STATUS_PATH', self.status).start()
        self.notify = patch.object(daily.notify, 'send').start()
        patch('subprocess.run', return_value=MagicMock(returncode=0, stdout='ok', stderr='')).start()
        self.run_market = patch.object(daily, 'run_market').start()
        stdout = io.StringIO()
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(contextlib.redirect_stdout(stdout))

    @staticmethod
    def _restore_root(handlers, level):
        root = logging.getLogger()
        for h in root.handlers[:]:
            root.removeHandler(h)
            h.close()
        for h in handlers:
            root.addHandler(h)
        root.setLevel(level)

    def main(self, *args):
        """跑一次 main()，返回退出码（None 表示正常返回）。"""
        with patch.object(sys, 'argv', ['daily.py', *args]):
            try:
                daily.main()
            except SystemExit as e:
                return e.code
        return None

    def read(self):
        return json.loads(self.status.read_text(encoding='utf-8'))

    def test_成功运行写ok状态_退出码0(self):
        self.assertEqual(self.main('--market', 'cn'), 0)
        doc = self.read()
        self.assertEqual((doc['state'], doc['consecutive_failures']), ('ok', 0))
        self.assertEqual(doc['markets']['cn']['status'], 'ok')
        self.assertEqual(doc['markets']['cn']['name'], daily.MARKETS['cn']['name'])
        self.notify.assert_not_called()

    def test_连续两次失败_退出码1_第二次发提醒_第三次同原因不重复(self):
        self.run_market.side_effect = RuntimeError('A 股 主品种 510300 没有数据，先联网运行一次')
        self.assertEqual(self.main('--market', 'cn'), 1)
        self.assertEqual(self.read()['consecutive_failures'], 1)
        self.notify.assert_not_called()
        self.assertEqual(self.main('--market', 'cn'), 1)
        self.assertEqual(self.read()['consecutive_failures'], 2)
        self.assertEqual(self.notify.call_count, 1)
        self.assertIn('没有数据', self.notify.call_args.args[1])
        self.assertEqual(self.main('--market', 'cn'), 1)
        self.assertEqual(self.read()['consecutive_failures'], 3)
        self.assertEqual(self.notify.call_count, 1)

    def test_一个市场失败另一个成功_状态里分开记(self):
        self.run_market.side_effect = lambda m, *a, **k: (_ for _ in ()).throw(RuntimeError('x')) if m == 'us' else {}
        self.assertEqual(self.main('--market', 'cn', 'us'), 1)
        doc = self.read()
        self.assertEqual({m: r['status'] for m, r in doc['markets'].items()}, {'cn': 'ok', 'us': 'error'})

    def test_自动模式没有要更新的市场_静默退出_不改状态文件(self):
        self.status.write_text(json.dumps({'state': 'failed', 'consecutive_failures': 5}), encoding='utf-8')
        before = self.status.read_text(encoding='utf-8')
        with patch.object(daily, 'needs_update', return_value=False):
            self.assertIsNone(self.main('--auto'))
        self.assertEqual(self.status.read_text(encoding='utf-8'), before)
        self.run_market.assert_not_called()

    def test_自动模式没联网_静默退出_不改状态文件(self):
        with patch.object(daily, 'needs_update', return_value=True), patch.object(daily, 'online', return_value=False):
            self.assertIsNone(self.main('--auto', '--market', 'cn'))
        self.assertFalse(self.status.exists())
        self.run_market.assert_not_called()

    def test_看门狗超时_退出前把失败写进状态并发提醒(self):
        # 先留下一次失败，使这次超时成为"连续第 2 次"
        self.run_market.side_effect = RuntimeError('上一次的失败')
        self.main('--market', 'cn')
        captured = {}

        class Stop(BaseException):
            pass

        def hang(*_a, **_k):
            captured['handler']()   # 模拟 3 小时后 SIGALRM 触发，处理函数里会调 os._exit
            raise Stop()

        self.run_market.side_effect = hang
        with patch('signal.signal', side_effect=lambda sig, h: captured.__setitem__('handler', h)), \
                patch('signal.alarm'), patch.object(daily.os, '_exit') as exit_mock, \
                patch.object(daily, 'needs_update', return_value=True), patch.object(daily, 'online', return_value=True):
            with patch.object(sys, 'argv', ['daily.py', '--auto', '--market', 'cn']):
                with self.assertRaises(Stop):
                    daily.main()
        exit_mock.assert_called_once_with(2)
        doc = self.read()
        self.assertEqual((doc['state'], doc['consecutive_failures']), ('failed', 2))
        self.assertIn('3 小时', doc['markets']['cn']['error'])
        self.assertEqual(self.notify.call_count, 1)

    def test_run_market里的小问题进warnings(self):
        def fake(m, *a, **k):
            daily.daily_status.note(m, '模拟盘更新失败')
            return {}
        self.run_market.side_effect = fake
        self.assertEqual(self.main('--market', 'cn'), 0)
        self.assertEqual(self.read()['markets']['cn']['warnings'], ['模拟盘更新失败'])


if __name__ == '__main__':
    unittest.main()
