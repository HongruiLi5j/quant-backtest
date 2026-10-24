#!/usr/bin/env python3
"""所有者手动有限验证，默认关闭；无行情许可/稳定性承诺。

超时参数最多 10 秒；真实 DNS 阻塞的硬上限尚未验证。
完整硬上限需 S2 独立工作进程及外层强制终止，本脚本尚无该保障。
"""
import argparse
from datetime import datetime
import json
import math
import os
from pathlib import Path
import stat
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.market_monitor.core import valid_symbol, validate_quote
from src.market_monitor.providers import TencentProvider, FeedError

OUTPUT = Path(__file__).resolve().parents[1] / 'data/market_monitor/validation'
CONCLUSION = '尚未真实验证；须所有者交易时段运行并对照广发'


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--run', action='store_true')
    modes.add_argument('--self-test', action='store_true')
    parser.add_argument('--code', default='600000')
    parser.add_argument('--market', choices=('SH', 'SZ'), default='SH')
    parser.add_argument('--count', type=int, default=3)
    parser.add_argument('--interval', type=float, default=60)
    parser.add_argument('--timeout', type=float, default=10)
    parser.add_argument('--stale-seconds', type=float, default=120)
    args = parser.parse_args(argv)
    if not valid_symbol(args.market.lower() + args.code):
        parser.error('只允许一只与市场匹配的沪深 A 股代码')
    if not 1 <= args.count <= 5:
        parser.error('请求数必须为 1–5')
    if not math.isfinite(args.interval) or args.interval < 60:
        parser.error('间隔必须至少 60 秒')
    if not math.isfinite(args.timeout) or not 0 < args.timeout <= 10:
        parser.error('总超时必须大于 0 且不超过 10 秒')
    if not math.isfinite(args.stale_seconds) or args.stale_seconds <= 0:
        parser.error('过旧阈值必须为有限正数（所有者验收目标）')
    return args


def secure_output(root):
    """用目录描述符逐层拒绝符号链接，防止检查与使用之间的路径替换。

    新目录 700；已有 monitor/validation 也必须 700，不自动改其权限。
    """
    root = Path(os.path.abspath(root))
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory = os.open(root.anchor, flags)
    try:
        for index, part in enumerate(root.parts[1:]):
            try:
                os.mkdir(part, mode=0o700, dir_fd=directory)
            except FileExistsError:
                pass
            child = os.open(part, flags, dir_fd=directory)
            os.close(directory)
            directory = child
            if index >= len(root.parts[1:]) - 2 and stat.S_IMODE(os.fstat(directory).st_mode) != 0o700:
                raise ValueError('输出目录权限必须为 700')
        name = 'tencent_' + datetime.now().strftime('%Y%m%dT%H%M%S_') + uuid.uuid4().hex + '.jsonl'
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        try:
            if stat.S_IMODE(os.fstat(fd).st_mode) != 0o600:
                raise ValueError('输出文件权限必须为 600')
            stream = os.fdopen(fd, 'w', encoding='utf-8')
        except Exception:
            os.close(fd)
            raise
        return stream, root / name
    finally:
        os.close(directory)


def probe(args, output=OUTPUT, *, provider_factory=TencentProvider, sleep=time.sleep,
          monotonic=time.monotonic, now=lambda: datetime.now().astimezone()):
    # 再检查传入的 Namespace，避免非 CLI 调用绕过边界。
    checked = arguments(['--run', '--code', args.code, '--market', args.market,
                         '--count', str(args.count), '--interval', str(args.interval),
                         '--timeout', str(args.timeout), '--stale-seconds', str(args.stale_seconds)])
    provider = provider_factory(min_interval=checked.interval, request_budget=checked.count,
                                timeout=checked.timeout, monotonic=monotonic, now=now)
    stream, path = secure_output(output)
    print(f'本次记录：{path}')
    symbol = checked.market.lower() + checked.code
    previous, last_start = None, None
    provider.start()
    try:
        with stream:
            for index in range(checked.count):
                if last_start is not None:
                    # 两层分别采样起始时间，留余量避免微小调度偏移使第二次请求被拒。
                    # Provider 仍独立执行原有间隔检查，不放宽 60 秒限制。
                    sleep(max(0, checked.interval - (monotonic() - last_start)) + 0.5)
                started = monotonic()
                record = {'request_number': index + 1, 'symbol': symbol,
                          'http_status': None, 'error_category': None,
                          'source_time_raw': None, 'source_time_beijing': None,
                          'fields_4_5_6_7_31': None, 'quality': None, 'reasons': [],
                          'conclusion': CONCLUSION, 'volume_unit': '未核实；不换算',
                          'calendar': '不可用；时段仅为周一至周五钟表提示，不确认休市'}
                failed = False
                try:
                    if last_start is not None and started - last_start < checked.interval:
                        raise FeedError('minimum_interval')
                    last_start = started
                    quote = provider.fetch((symbol,))[0]
                    quote = validate_quote(quote, symbol, previous, checked.stale_seconds)
                    record.update(http_status=200, source_time_raw=quote.source_time_raw,
                                  source_time_beijing=quote.source_time.isoformat() if quote.source_time else None,
                                  received_at_local=quote.received_at.isoformat(), elapsed_seconds=quote.elapsed_seconds,
                                  fields_4_5_6_7_31=list(quote.fields_raw), quality=quote.quality,
                                  reasons=list(quote.reasons), session=quote.session)
                    previous = quote
                    failed = bool(quote.reasons)
                except FeedError as exc:
                    record.update(http_status=exc.http_status, error_category=exc.category,
                                  received_at_local=now().isoformat(), elapsed_seconds=monotonic() - started)
                    failed = True
                except Exception:
                    record.update(error_category='validation_internal_error',
                                  received_at_local=now().isoformat(), elapsed_seconds=monotonic() - started)
                    failed = True
                stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + '\n')
                stream.flush()
                if failed:
                    print('有限验证已停止；错误类别与校验结果见记录。')
                    return 1
    finally:
        provider.stop()
    return 0


def self_test():
    # 测试模块仅使用假响应、假时钟和临时目录；不导入 app。
    import runpy
    import unittest
    module = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'tests/test_market_monitor.py'), run_name='market_monitor_offline_tests')
    suite = unittest.TestSuite()
    for item in module.values():
        if isinstance(item, type) and issubclass(item, unittest.TestCase):
            suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(item))
    return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1


def main(argv=None):
    args = arguments(argv)
    if args.self_test:
        return self_test()
    if not args.run:
        print('默认关闭；--self-test 离线自测；仅 --run 开启有限验证。')
        return 0
    try:
        return probe(args)
    except KeyboardInterrupt:
        print('已停止。')
        return 130
    except Exception:
        print('停止：configuration_or_output_error；不输出异常正文。')
        return 1


if __name__ == '__main__':
    sys.exit(main())
