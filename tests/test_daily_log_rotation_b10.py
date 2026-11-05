"""B10：daily.py 的日志按大小轮转，不再无限增长。

背景：logs/daily.log 原来用 logging.FileHandler，只追加不截断，定时任务每 15 分钟跑一次，文件会一直变大。
现在改用 RotatingFileHandler（默认 5MB × 5 份备份），日志格式（UTC 时间 + 级别 + 内容）和文件名不变。

全部在临时目录里写日志（把 daily.LOG 指向临时文件，并临时清空/还原 root logger 的 handler），
不读写 logs/ 和 data/ 下的真实文件。
运行：.venv/bin/python tests/test_daily_log_rotation_b10.py
"""
import contextlib
import io
import logging
import os
import re
import sys
import tempfile
import unittest
from logging.handlers import RotatingFileHandler
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import daily
sys.path.insert(0, str(ROOT / 'tests'))
from realdata_guard import ZRealDataUntouchedTests  # noqa: E402,F401  真实数据守卫（必须在所有测试类之后运行）

LINE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00 (INFO|WARNING|ERROR) .+$')


class DailyLogRotationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = Path(self.tmp.name) / 'logs' / 'daily.log'
        # logging.basicConfig 在 root 已有 handler 时什么都不做，所以先把现有的摘下来，测完再装回去
        root = logging.getLogger()
        saved_handlers, saved_level = root.handlers[:], root.level
        for h in saved_handlers:
            root.removeHandler(h)
        self.addCleanup(self._restore_root, saved_handlers, saved_level)
        self.stdout = io.StringIO()
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(daily, 'LOG', self.log))
        stack.enter_context(contextlib.redirect_stdout(self.stdout))

    @staticmethod
    def _restore_root(handlers, level):
        root = logging.getLogger()
        for h in root.handlers[:]:
            root.removeHandler(h)
            h.close()
        for h in handlers:
            root.addHandler(h)
        root.setLevel(level)

    def _file_handler(self):
        hs = [h for h in logging.getLogger().handlers if isinstance(h, logging.FileHandler)]
        self.assertEqual(len(hs), 1)
        return hs[0]

    def test_默认参数是5MB乘5份(self):
        self.assertEqual(daily.LOG_MAX_BYTES, 5 * 1024 * 1024)
        self.assertEqual(daily.LOG_BACKUP_COUNT, 5)

    def test_文件处理器是轮转型且文件名不变(self):
        daily.setup_logging()
        h = self._file_handler()
        self.assertIsInstance(h, RotatingFileHandler)
        self.assertEqual(h.baseFilename, os.path.abspath(self.log))   # logging 用 abspath，不解析 macOS 的 /var 软链接
        self.assertEqual(h.maxBytes, daily.LOG_MAX_BYTES)
        self.assertEqual(h.backupCount, daily.LOG_BACKUP_COUNT)
        self.assertEqual(h.encoding, 'utf-8')
        self.assertEqual(self.log.name, 'daily.log')

    def test_日志格式保持不变且中文不乱码(self):
        daily.setup_logging()
        logging.info('[步骤 %d/%d] %s', 1, 6, '下载行情')
        logging.warning('另一个 daily.py 还在运行')
        self._file_handler().flush()
        lines = self.log.read_text(encoding='utf-8').splitlines()
        self.assertEqual(len(lines), 2)
        for line in lines:
            self.assertRegex(line, LINE_RE)
        self.assertTrue(lines[0].endswith(' INFO [步骤 1/6] 下载行情'))
        # 屏幕（给 launchd 抓 stdout 用）那一路也要保持同样的格式
        self.assertEqual(self.stdout.getvalue().splitlines(), lines)

    def test_写满后轮转且份数有上限(self):
        max_bytes, backups = 2000, 3
        with patch.object(daily, 'LOG_MAX_BYTES', max_bytes), patch.object(daily, 'LOG_BACKUP_COUNT', backups):
            daily.setup_logging()
        for i in range(300):
            logging.info('第 %d 行 %s', i, '数据' * 10)
        self._file_handler().flush()
        names = sorted(p.name for p in self.log.parent.iterdir())
        self.assertEqual(names, ['daily.log', 'daily.log.1', 'daily.log.2', 'daily.log.3'])   # 没有 .4：最老的被丢弃
        for p in self.log.parent.iterdir():
            self.assertLessEqual(p.stat().st_size, max_bytes, p.name)
        # 总占用有上限：(备份数 + 1) × 单文件上限
        self.assertLessEqual(sum(p.stat().st_size for p in self.log.parent.iterdir()), (backups + 1) * max_bytes)

    def test_轮转后新旧文件接得上且最新一行在当前文件里(self):
        with patch.object(daily, 'LOG_MAX_BYTES', 1500), patch.object(daily, 'LOG_BACKUP_COUNT', 5):
            daily.setup_logging()
        for i in range(40):
            logging.info('序号 %03d %s', i, 'x' * 40)
        self._file_handler().flush()
        current = self.log.read_text(encoding='utf-8').splitlines()
        self.assertTrue(current[-1].endswith('序号 039 ' + 'x' * 40))
        self.assertTrue(self.log.with_name('daily.log.1').exists())
        # 备份文件里最新的一份（.1）紧挨着当前文件的第一行：序号连续、没有丢行
        newest_backup = self.log.with_name('daily.log.1').read_text(encoding='utf-8').splitlines()
        first_now = int(current[0].split('序号 ')[1][:3])
        last_backup = int(newest_backup[-1].split('序号 ')[1][:3])
        self.assertEqual(first_now, last_backup + 1)
        for line in current + newest_backup:
            self.assertRegex(line, LINE_RE)

    def test_没写满时不产生备份文件(self):
        daily.setup_logging()
        for i in range(20):
            logging.info('小日志 %d', i)
        self._file_handler().flush()
        self.assertEqual([p.name for p in self.log.parent.iterdir()], ['daily.log'])


if __name__ == '__main__':
    unittest.main()
