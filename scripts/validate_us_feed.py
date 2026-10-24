#!/usr/bin/env python3
"""所有者手动有限验证（美股 yfinance），默认关闭；无行情许可/稳定性承诺。

用途：在非开盘时段也能测试网络连通性和 yfinance 是否可用（非时段行情会被标成 off_session，这是正常的）。
示例：python scripts/validate_us_feed.py --run --symbols AAPL,MSFT --count 3 --interval 60 --timeout 10

限制（写死在参数校验里，非 CLI 调用也绕不过）：
- 代码 1–5 只，必须通过美股代码校验；轮数 1–5；请求总数（只数×轮数）最多 10。
- 同一代码相邻两轮至少相隔 60 秒；同一轮里相邻两只至少相隔 10 秒；单次超时 ≤ 10 秒。
- 每次请求走独立 spawn 子进程（service.process_us_request），带硬截止：DNS/TLS 阻塞也会被终止并回收。
- 遇到任何失败立刻停止，不重试（限流时继续请求只会加重限流）。
- 输出文件在 data/us_market_monitor/validation/，目录 0700、文件 0600、逐层拒绝符号链接；
  只记录数值与固定错误类别，不记录上游异常文本。
"""
import argparse
from datetime import datetime
import json
import math
import os
from pathlib import Path
import stat
import sys
import threading
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.market_monitor.providers import FeedError
from src.us_market_monitor.core import US_EASTERN, valid_symbol, validate_quote
from src.us_market_monitor.service import KNOWN_ERRORS, process_us_request

OUTPUT = Path(__file__).resolve().parents[1] / 'data/us_market_monitor/validation'
CONCLUSION = '尚未真实验证；须所有者联网运行并与行情软件对照'
MAX_SYMBOLS, MAX_COUNT, MAX_TOTAL = 5, 5, 10
MIN_INTERVAL, REQUEST_GAP = 60, 10


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--run', action='store_true')
    modes.add_argument('--self-test', action='store_true')
    parser.add_argument('--symbols', default='AAPL,MSFT')
    parser.add_argument('--count', type=int, default=3)
    parser.add_argument('--interval', type=float, default=60)
    parser.add_argument('--timeout', type=float, default=10)
    parser.add_argument('--stale-seconds', type=float, default=1200)
    args = parser.parse_args(argv)
    symbols = [s for s in args.symbols.split(',')]
    if not 1 <= len(symbols) <= MAX_SYMBOLS or len(set(symbols)) != len(symbols) or not all(valid_symbol(s) for s in symbols):
        parser.error('代码必须是 1–5 只互不相同的美股代码（大写字母，如 AAPL、BRK-B）')
    args.symbols = tuple(symbols)
    if not 1 <= args.count <= MAX_COUNT:
        parser.error('轮数必须为 1–5')
    if len(symbols) * args.count > MAX_TOTAL:
        parser.error('请求总数（代码数×轮数）最多 10')
    if not math.isfinite(args.interval) or args.interval < MIN_INTERVAL:
        parser.error('间隔必须至少 60 秒')
    if not math.isfinite(args.timeout) or not 0 < args.timeout <= 10:
        parser.error('单次超时必须大于 0 且不超过 10 秒')
    if not math.isfinite(args.stale_seconds) or args.stale_seconds <= 0:
        parser.error('过旧阈值必须为有限正数')
    return args


def secure_output(root):
    """用目录描述符逐层拒绝符号链接，防止检查与使用之间的路径替换。

    新目录 700；已有的 us_market_monitor/validation 也必须 700，不自动改其权限。
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
        name = 'us_' + datetime.now().strftime('%Y%m%dT%H%M%S_') + uuid.uuid4().hex + '.jsonl'
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


def _eastern(value):
    return value.astimezone(US_EASTERN).isoformat() if isinstance(value, datetime) and value.tzinfo else None


def probe(args, output=OUTPUT, *, request=process_us_request, sleep=time.sleep,
          monotonic=time.monotonic, now=lambda: datetime.now().astimezone()):
    # 再检查传入的 Namespace，避免非 CLI 调用绕过边界。
    checked = arguments(['--run', '--symbols', ','.join(args.symbols), '--count', str(args.count),
                         '--interval', str(args.interval), '--timeout', str(args.timeout),
                         '--stale-seconds', str(args.stale_seconds)])
    stream, path = secure_output(output)
    print(f'本次记录：{path}')
    cancelled = threading.Event()
    previous, last_start, last_symbol, number = {}, None, {}, 0
    with stream:
        for round_no in range(1, checked.count + 1):
            for symbol in checked.symbols:
                # 同一代码至少 interval；相邻请求至少 REQUEST_GAP。两层分别计算，留 0.5 秒余量。
                due = max([0.0] + ([last_start + REQUEST_GAP] if last_start is not None else [])
                          + ([last_symbol[symbol] + checked.interval] if symbol in last_symbol else []))
                sleep(max(0.0, due - monotonic()) + (0.5 if due > 0 else 0))
                started = monotonic()
                last_start = last_symbol[symbol] = started
                number += 1
                record = {'request_number': number, 'round': round_no, 'symbol': symbol, 'error_category': None,
                          'conclusion': CONCLUSION, 'volume_unit': '股（不换算）',
                          'calendar': '不可用；时段仅为美东周一至周五钟表提示，不确认休市'}
                failed = False
                try:
                    result = request((symbol,), cancelled, checked.timeout)
                    quote = validate_quote(result[0], symbol, previous.get(symbol), checked.stale_seconds)
                    record.update(received_at_local=quote.received_at.isoformat(),
                                  received_at_eastern=_eastern(quote.received_at),
                                  source_time_eastern=_eastern(quote.timestamp),
                                  elapsed_seconds=quote.elapsed_seconds, price=quote.price,
                                  previous_close=quote.previous_close, opening=quote.opening, high=quote.high,
                                  low=quote.low, volume=quote.volume, quality=quote.quality,
                                  reasons=list(quote.reasons), session=quote.session)
                    previous[symbol] = quote
                    # off_session / after_hours 不是故障：非开盘时段测连通性时正是这些结果
                    failed = quote.quality not in ('valid', 'off_session', 'after_hours')
                except FeedError as exc:
                    record.update(error_category=exc.category if exc.category in KNOWN_ERRORS else 'provider_error',
                                  received_at_local=now().isoformat(), elapsed_seconds=monotonic() - started)
                    failed = True
                except ImportError:
                    record.update(error_category='yfinance_not_installed', received_at_local=now().isoformat(),
                                  elapsed_seconds=monotonic() - started)
                    failed = True
                except Exception:
                    record.update(error_category='validation_internal_error', received_at_local=now().isoformat(),
                                  elapsed_seconds=monotonic() - started)
                    failed = True
                stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + '\n')
                stream.flush()
                print(f"[{number}] {symbol} {record.get('quality') or record['error_category']}")
                if failed:
                    print('有限验证已停止；错误类别与校验结果见记录。')
                    return 1
    print('有限验证完成；结论仍是“尚未真实验证”，请对照行情软件核对价格。')
    return 0


def self_test():
    # 只跑离线测试（假请求/假时钟/临时目录）；不联网。
    import runpy
    import unittest
    suite = unittest.TestSuite()
    for name in ('test_us_market_monitor_core.py', 'test_us_market_monitor_service.py',
                 'test_us_market_monitor_scheduler.py', 'test_us_market_monitor_intraday.py'):
        module = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'tests' / name), run_name='us_offline_tests')
        for item in module.values():
            if isinstance(item, type) and issubclass(item, unittest.TestCase) and item is not unittest.TestCase:
                suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(item))
    return 0 if unittest.TextTestRunner(verbosity=1).run(suite).wasSuccessful() else 1


def main(argv=None):
    args = arguments(argv)
    if args.self_test:
        return self_test()
    if not args.run:
        print('默认关闭；--self-test 离线自测；仅 --run 开启有限联网验证。')
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
