#!/usr/bin/env python3
"""S0d：手动、有限次通达信一分钟线验证；默认不导入行情依赖、不联网。"""
import argparse
import ast
from contextlib import contextmanager
from datetime import datetime, timedelta
import errno
import importlib
import importlib.metadata
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import signal
import socket
import stat
import sys
import time
from zoneinfo import ZoneInfo

BEIJING = ZoneInfo('Asia/Shanghai')
OUTPUT = Path(__file__).resolve().parents[1] / 'data/market_monitor/validation'
MAX_REQUESTS = 5
MIN_INTERVAL = 60
MAX_TIMEOUT = 10
MAX_BARS = 30
FREQUENCY = 8


class DependencyMissing(Exception):
    pass


class ConfigurationError(Exception):
    pass


def arguments(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument('--run', action='store_true')
    mode.add_argument('--self-test', action='store_true')
    p.add_argument('--code', default='600000')
    p.add_argument('--market', choices=('SH', 'SZ'), default='SH')
    p.add_argument('--count', type=int, default=3)
    p.add_argument('--interval', type=float, default=60)
    p.add_argument('--timeout', type=float, default=10)
    p.add_argument('--bars', type=int, default=30)
    a = p.parse_args(argv)
    try:
        validate(a)
    except ValueError as e:
        p.error(str(e))
    return a


def validate(a):
    # 限定常见沪深 A 股代码段，不猜测基金、指数、北交所或多代码输入。
    pattern = r'(600|601|603|605|688)\d{3}' if a.market == 'SH' else r'(000|001|002|003|300|301)\d{3}'
    if not re.fullmatch(pattern, a.code):
        raise ValueError('只允许一只与 market 匹配的沪深 A 股六位代码')
    if not 1 <= a.count <= MAX_REQUESTS:
        raise ValueError('请求数必须为 1–5')
    if not math.isfinite(a.interval) or a.interval < MIN_INTERVAL:
        raise ValueError('请求开始间隔必须不少于 60 秒')
    if not math.isfinite(a.timeout) or not 0 < a.timeout <= MAX_TIMEOUT:
        raise ValueError('连接与读取超时必须大于 0 且不超过 10 秒')
    if not 1 <= a.bars <= MAX_BARS:
        raise ValueError('分钟根数必须为 1–30')


def classify(exc):
    # 仅检查异常链、类型及 errno，绝不记录异常正文。
    seen = set()
    current = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        number = getattr(current, 'errno', None)
        if isinstance(current, DependencyMissing):
            return 'dependency_not_installed'
        if isinstance(current, ConnectionRefusedError) or number == errno.ECONNREFUSED:
            return 'connection_refused'
        if isinstance(current, (ConnectionResetError, BrokenPipeError)) or number in (errno.ECONNRESET, errno.EPIPE):
            return 'connection_reset'
        if isinstance(current, TimeoutError) or number == errno.ETIMEDOUT:
            return 'timeout'
        if number in (errno.ENETUNREACH, errno.EHOSTUNREACH):
            return 'network_unreachable'
        current = getattr(current, 'original_exception', None) or current.__cause__ or current.__context__
    return 'configuration_error' if isinstance(exc, ConfigurationError) else 'protocol_or_parse_error'


def error_record(exc):
    return {'category': classify(exc), 'exception_class': type(exc).__name__,
            'cause_class': type(exc.__cause__).__name__ if exc.__cause__ else None,
            'original_class': type(getattr(exc, 'original_exception', None)).__name__ if getattr(exc, 'original_exception', None) is not None else None,
            'original_errno': getattr(getattr(exc, 'original_exception', None), 'errno', None)}


def first_endpoint(value):
    """只解释配置中的第一个端点，不探测列表，不换服务器。"""
    if isinstance(value, dict):
        for key in ('stock', 'HQ', 'hq', 'std', 'quotes', 'QUOTE_SERVERS'):
            if key in value:
                return first_endpoint(value[key])
        host = value.get('ip', value.get('host'))
        port = value.get('port')
    elif isinstance(value, (list, tuple)) and value:
        if len(value) == 3 and isinstance(value[1], str) and isinstance(value[2], int):
            _, host, port = value
        elif len(value) == 2 and isinstance(value[0], str) and isinstance(value[1], int):
            host, port = value
        else:
            return first_endpoint(value[0])
    else:
        raise ConfigurationError('不支持的固定服务器配置')
    try:
        ipaddress.IPv4Address(host)
    except (ValueError, TypeError) as exc:
        raise ConfigurationError('配置服务器必须为 IPv4 字面地址') from exc
    if type(port) is not int or not 1 <= port <= 65535:
        raise ConfigurationError('端口不合法')
    return host, port


def bundled_server(dist):
    # 仅读安装包自带静态配置，不读 ~/.mootdx、用户配置或环境变量。
    names = ('stock_hosts', 'hq_hosts', 'HQ_HOSTS', 'HQ_SERVERS', 'TDX_SERVERS',
             'QUOTE_SERVERS', 'SERVERS', 'servers', 'hosts')
    candidates = ('mootdx/config/hosts.py', 'mootdx/consts.py', 'mootdx/config.py',
                  'mootdx/config/server.json', 'mootdx/config/servers.json',
                  'mootdx/config/hosts.json')
    files = {str(f) for f in (dist.files or ())}
    for name in candidates:
        if name not in files:
            continue
        path = Path(dist.locate_file(name))
        if path.is_symlink() or path.stat().st_size > 500_000:
            raise ConfigurationError('拒绝非普通或过大配置')
        source = path.read_text(encoding='utf-8')
        if name.endswith('.json'):
            host, port = first_endpoint(json.loads(source))
            return {'host': host, 'port': port, 'source_file': str(path),
                    'source_location': 'JSON 第一个沪深行情端点（索引 0）'}
        tree = ast.parse(source)
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if not any(t in names for t in targets):
                continue
            try:
                value = ast.literal_eval(node.value)
            except (ValueError, TypeError):
                continue
            host, port = first_endpoint(value)
            return {'host': host, 'port': port, 'source_file': str(path),
                    'source_location': f'第 {node.lineno} 行 {targets[0]}，第一个端点（索引 0）'}
    raise ConfigurationError('未找到支持的包内固定行情配置；停止，不扫描或测速')


def load_dependency():
    # 只有 --run 的执行路径到这里；不使用高层 Quotes.factory 的选服/重试逻辑。
    try:
        dist = importlib.metadata.distribution('mootdx')
    except importlib.metadata.PackageNotFoundError as exc:
        raise DependencyMissing('mootdx 未安装；请按报告在独立虚拟环境安装后手动运行') from exc
    server = bundled_server(dist)
    files = {str(f) for f in (dist.files or ())}
    # mootdx 0.11.x 的协议层来自 tdxpy 包（tdxpy.hq）；旧版本可能是 mootdx.hq 或 pytdx.hq。
    candidates = []
    if 'mootdx/hq.py' in files or 'mootdx/hq/__init__.py' in files:
        candidates.append('mootdx.hq')
    candidates += ['tdxpy.hq', 'pytdx.hq']
    last_error = None
    for module in candidates:
        try:
            api = importlib.import_module(module).TdxHq_API
        except (ModuleNotFoundError, AttributeError) as exc:
            last_error = exc
            continue
        return api, server, dist.version, module
    raise DependencyMissing('mootdx 或其协议依赖未安装完整；不自动安装') from last_error


@contextmanager
def deadline(seconds):
    # macOS/Unix 主线程总时限，防止多次 recv 各等十秒。
    if not hasattr(signal, 'setitimer'):
        raise ConfigurationError('运行环境不支持硬超时；拒绝联网')
    def expired(signum, frame):
        raise TimeoutError('操作硬超时')
    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, min(seconds, MAX_TIMEOUT))
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


@contextmanager
def network_fence(server, timeout):
    """约束依赖：仅一个目标、一次 connect、一个 socket；禁止隐含测速/重连。"""
    original = socket.socket
    state = {'sockets': 0, 'connections': 0}
    class RestrictedSocket(original):
        def __init__(self, *args, **kwargs):
            state['sockets'] += 1
            if state['sockets'] > 1:
                raise ConfigurationError('禁止额外连接或测速')
            super().__init__(*args, **kwargs)
            super().settimeout(timeout)
        def settimeout(self, value):
            if value is None or not 0 < value <= MAX_TIMEOUT:
                raise ConfigurationError('依赖尝试放大超时')
            super().settimeout(min(value, timeout))
        def connect(self, address):
            state['connections'] += 1
            if state['connections'] != 1 or address != (server['host'], server['port']):
                raise ConfigurationError('禁止换服务器或重连')
            return super().connect(address)
        def connect_ex(self, address):
            raise ConfigurationError('不允许隐式连接路径')
    socket.socket = RestrictedSocket
    try:
        yield state
    finally:
        socket.socket = original


def secure_output(root):
    # 从现有祖先逐层检查；不跟随符号链接，不修正已有目录权限。
    root = Path(root).absolute()
    for part in reversed((root,) + tuple(root.parents)):
        if part.exists() or part.is_symlink():
            info = part.lstat()
            if not stat.S_ISDIR(info.st_mode):
                raise ConfigurationError('输出路径含非目录/符号链接')
        else:
            part.mkdir(mode=0o700)
        if part in (root, root.parent) and stat.S_IMODE(part.stat().st_mode) != 0o700:
            raise ConfigurationError('输出目录权限必须为 700')
    name = 'tdx_' + datetime.now().strftime('%Y%m%dT%H%M%S_%f') + '.jsonl'
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(root / name, flags, 0o600)
    if stat.S_IMODE(os.fstat(fd).st_mode) != 0o600:
        os.close(fd)
        raise ConfigurationError('文件权限必须为 600')
    return os.fdopen(fd, 'w', encoding='utf-8'), root / name


def interpret_time(raw):
    if not isinstance(raw, str) or len(raw) > 40:
        raise ValueError('原始时间字段不合法')
    parsed = datetime.fromisoformat(raw)
    # 无偏移分钟标签暂按北京时间解释；不是已核实的上游时区。
    return parsed.replace(tzinfo=BEIJING) if parsed.tzinfo is None else parsed.astimezone(BEIJING)


def trading_minute(t):
    minute = t.hour * 60 + t.minute
    return 570 <= minute <= 690 or 780 <= minute <= 900


def summarize(rows, received, limit):
    if not isinstance(rows, (list, tuple)) or not 1 <= len(rows) <= min(limit, MAX_BARS):
        raise ValueError('返回根数为空或超过请求上限')
    bars, times = [], []
    for row in rows:
        raw = row['datetime']
        t = interpret_time(raw)
        bar = {'time_raw': raw, 'time_beijing': t.isoformat()}
        for field in ('open', 'high', 'low', 'close', 'vol', 'amount'):
            value = row[field]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError('非法价格或量额')
            bar[field] = value
        bars.append(bar)
        times.append(t)
    ordered = sorted(set(times))
    # 仅查已观察首尾内、同一天连续竞价分钟；不推断交易日/停牌。
    gaps = []
    for left, right in zip(ordered, ordered[1:]):
        if left.date() != right.date():
            continue
        t = left + timedelta(minutes=1)
        for _ in range(1440):
            if t >= right:
                break
            if trading_minute(t):
                gaps.append(t.isoformat())
            t += timedelta(minutes=1)
    return {'returned_bars': len(bars), 'bars': bars,
            'first_time_raw': bars[0]['time_raw'], 'last_time_raw': bars[-1]['time_raw'],
            'first_time_beijing': bars[0]['time_beijing'], 'last_time_beijing': bars[-1]['time_beijing'],
            'earliest_time_beijing': ordered[0].isoformat(), 'latest_time_beijing': ordered[-1].isoformat(),
            'duplicate_count': len(times) - len(ordered),
            'out_of_order': any(b < a for a, b in zip(times, times[1:])),
            'missing_minutes': gaps, 'missing_count': len(gaps),
            'gap_basis': '仅同日首尾内部，排除午休；边界/停牌/无成交口径未核实',
            'tail_suspected_incomplete': ordered[-1] >= received.astimezone(BEIJING).replace(second=0, microsecond=0) - timedelta(minutes=1)}


def probe(a, output=OUTPUT, loader=load_dependency, sleep=time.sleep, monotonic=time.monotonic):
    validate(a)
    # 先创建审计记录，依赖未安装也可留类别；仅 --run 调用。
    stream, path = secure_output(output)
    print(f'本次记录：{path}')
    server, version, api, phase = None, None, None, 'dependency'
    operation_started = monotonic()
    with stream:
        def write(record):
            record.update({'server': server, 'mootdx_version': version, 'code': a.code,
                           'market': a.market, 'frequency': FREQUENCY,
                           'source_timezone': '未核实；无偏移字段暂按 Asia/Shanghai 解释',
                           'minute_label': '开始/结束标签未核实',
                           'units': {'open/high/low/close': '疑似元/股，待核实',
                                     'vol': '上游原值，股/手待核实，不换算', 'amount': '疑似元，待核实'},
                           'conclusion': '尚未真实验证（有限样本不等于验收）'})
            stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + '\n')
            stream.flush()
        try:
            api_class, server, version, module = loader()
            operation_started = monotonic()
            phase = 'connect'
            with network_fence(server, a.timeout):
                with deadline(a.timeout):
                    api = api_class(multithread=False, heartbeat=False, auto_retry=False, raise_exception=True)
                    if not api.connect(server['host'], server['port'], time_out=a.timeout):
                        raise ConnectionError('连接失败且库未提供异常类型')
                last_start = None
                for index in range(a.count):
                    if last_start is not None:
                        sleep(max(0, a.interval - (monotonic() - last_start)))
                        if monotonic() - last_start < max(MIN_INTERVAL, a.interval):
                            raise ConfigurationError('开始间隔不足，停止')
                    phase = 'bars'
                    started = monotonic()
                    last_start = started
                    record = {'request_number': index + 1, 'phase': phase,
                              'request_started_at_local': datetime.now().astimezone().isoformat(),
                              'protocol_module': module}
                    try:
                        with deadline(a.timeout):
                            rows = api.get_security_bars(FREQUENCY, 1 if a.market == 'SH' else 0, a.code, 0, a.bars)
                        received = datetime.now().astimezone()
                        record.update({'received_at_local': received.isoformat(),
                                       'elapsed_seconds': monotonic() - started})
                        record.update(summarize(rows, received, a.bars))
                        write(record)
                    except Exception as exc:
                        record.update({'received_at_local': datetime.now().astimezone().isoformat(),
                                       'elapsed_seconds': monotonic() - started,
                                       'error': error_record(exc), 'returned_bars': None,
                                       'first_time_raw': None, 'last_time_raw': None})
                        write(record)
                        return 1
        except Exception as exc:
            write({'phase': phase, 'error': error_record(exc),
                   'elapsed_seconds': monotonic() - operation_started,
                   'received_at_local': datetime.now().astimezone().isoformat(),
                   'returned_bars': None, 'first_time_raw': None, 'last_time_raw': None})
            if isinstance(exc, DependencyMissing):
                print('mootdx 未安装或依赖不完整；不尝试安装。请按 T006_S0d 报告使用独立虚拟环境。')
            else:
                print(f'停止：{classify(exc)} ({type(exc).__name__})')
            return 1
        finally:
            if api is not None:
                try:
                    with deadline(a.timeout):
                        api.disconnect()
                except Exception:
                    pass
    return 0


def self_test():
    import io
    import tempfile
    import types
    import unittest
    from unittest.mock import patch
    from contextlib import redirect_stdout, redirect_stderr

    def row(at):
        return dict(datetime=at, open=1., high=2., low=1., close=1.5, vol=10., amount=15.)

    class OfflineTests(unittest.TestCase):
        def test_lazy_import_missing_dependency(self):
            with patch.object(importlib, 'import_module', side_effect=AssertionError('禁止导入')), patch.object(importlib.metadata, 'distribution', side_effect=AssertionError('禁止查询')), redirect_stdout(io.StringIO()):
                self.assertEqual(main([]), 0)
            with patch.object(importlib.metadata, 'distribution', side_effect=importlib.metadata.PackageNotFoundError), self.assertRaises(DependencyMissing):
                load_dependency()
            with tempfile.TemporaryDirectory() as d, redirect_stdout(io.StringIO()) as out:
                def absent():
                    raise DependencyMissing('不应泄露的正文')
                self.assertEqual(probe(arguments(['--run']), Path(d).resolve()/'monitor'/'validation', loader=absent), 1)
                self.assertIn('不尝试安装', out.getvalue())
                record = json.loads(next((Path(d).resolve()/'monitor'/'validation').glob('*.jsonl')).read_text())
                self.assertEqual(record['error']['category'], 'dependency_not_installed')
                self.assertNotIn('不应泄露', json.dumps(record))

        def test_bounds(self):
            for argv in (['--code','600000,000001'], ['--code','000001'], ['--count','6'], ['--count','0'], ['--interval','59'], ['--interval','nan'], ['--timeout','11'], ['--timeout','0'], ['--timeout','nan'], ['--bars','31']):
                with self.subTest(argv=argv), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    arguments(argv)
            self.assertEqual(arguments(['--market','SZ','--code','300001']).code, '300001')

        def test_error_categories(self):
            for exc, expected in [(ConnectionRefusedError(), 'connection_refused'), (ConnectionResetError(), 'connection_reset'), (TimeoutError(), 'timeout'), (ValueError(), 'protocol_or_parse_error'), (DependencyMissing(), 'dependency_not_installed')]:
                self.assertEqual(classify(exc), expected)
                self.assertEqual(error_record(exc)['exception_class'], type(exc).__name__)
            wrapped = RuntimeError('正文')
            wrapped.__cause__ = ConnectionResetError()
            self.assertEqual(classify(wrapped), 'connection_reset')

        def test_timezone_and_quality(self):
            received = datetime.fromisoformat('2026-10-21T19:33:05-07:00')
            self.assertEqual(interpret_time('2026-10-22 02:31:00+00:00').hour, 10)
            self.assertEqual(interpret_time('2026-10-22 10:31').utcoffset(), timedelta(hours=8))
            self.assertEqual(received.astimezone(BEIJING).day, 22)
            rows = [row('2026-10-22 10:30'), row('2026-10-22 10:33'), row('2026-10-22 10:30')]
            result = summarize(rows, received, 30)
            self.assertEqual(result['duplicate_count'], 1)
            self.assertTrue(result['out_of_order'])
            self.assertEqual(result['missing_count'], 2)
            self.assertTrue(result['tail_suspected_incomplete'])
            lunch = summarize([row('2026-10-22 11:30'), row('2026-10-22 13:00')], received, 30)
            self.assertEqual(lunch['missing_count'], 0)
            with self.assertRaises(ValueError):
                summarize([row('2026-10-22 10:30')]*31, received, 30)

        def test_permissions_and_exclusive(self):
            with tempfile.TemporaryDirectory() as d:
                root = Path(d).resolve()/'monitor'/'validation'
                stream, path = secure_output(root)
                stream.close()
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
                with patch.object(sys.modules[__name__], 'datetime') as clock:
                    clock.now.return_value.strftime.return_value = path.stem[4:]
                    with self.assertRaises(FileExistsError):
                        secure_output(root)
                link = Path(d).resolve()/'link'
                link.symlink_to(root, target_is_directory=True)
                with self.assertRaises(ConfigurationError):
                    secure_output(link)
                root.chmod(0o755)
                with self.assertRaises(ConfigurationError):
                    secure_output(root)

        def test_bundled_config_first_only(self):
            with tempfile.TemporaryDirectory() as d:
                path = Path(d).resolve()/'hosts.py'
                path.write_text("stock_hosts = [('first','1.2.3.4',7709),('second','5.6.7.8',7709)]\n")
                dist = types.SimpleNamespace(files=['mootdx/config/hosts.py'], locate_file=lambda _:path)
                result = bundled_server(dist)
                self.assertEqual(result['host'], '1.2.3.4')
                self.assertIn('第 1 行', result['source_location'])

        def test_no_scanning_static(self):
            tree = ast.parse(Path(__file__).read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    self.assertFalse(isinstance(node.func, ast.Attribute) and node.func.attr in ('factory', 'bestip', 'select_best_ip'))
                    self.assertNotIn('bestip', [k.arg for k in node.keywords])

        def test_hard_timeout_and_connection_fence(self):
            with self.assertRaises(TimeoutError):
                with deadline(0.01):
                    time.sleep(0.1)
            # 假 socket 只记调用，绝不接触网络。
            class FakeSocket:
                def __init__(self, *args, **kwargs):
                    pass
                def settimeout(self, value):
                    self.timeout = value
                def connect(self, address):
                    self.address = address
            with patch.object(socket, 'socket', FakeSocket):
                with network_fence({'host':'1.2.3.4','port':7709}, 5):
                    sock = socket.socket()
                    self.assertEqual(sock.timeout, 5)
                    with self.assertRaises(ConfigurationError):
                        sock.settimeout(11)
                    with self.assertRaises(ConfigurationError):
                        sock.connect(('5.6.7.8',7709))
                    with self.assertRaises(ConfigurationError):
                        socket.socket()
                with network_fence({'host':'1.2.3.4','port':7709}, 5):
                    sock = socket.socket()
                    sock.connect(('1.2.3.4',7709))
                    with self.assertRaises(ConfigurationError):
                        sock.connect(('1.2.3.4',7709))

        def test_fake_module_bounded_batch_and_stop(self):
            for fail in (False, True):
                calls, ticks = [], [0.]
                class FakeAPI:
                    def __init__(self, **kw):
                        self.kw = kw
                        self_test_case.assertEqual(kw, dict(multithread=False, heartbeat=False, auto_retry=False, raise_exception=True))
                    def connect(self, host, port, time_out):
                        calls.append(('connect', host, port, time_out))
                        return self
                    def get_security_bars(self, *args):
                        calls.append(('bars', ticks[0], args))
                        if fail:
                            raise ConnectionResetError('上游敏感正文')
                        return [row('2026-10-22 10:30')]
                    def disconnect(self):
                        calls.append(('disconnect',))
                self_test_case = self
                fake = types.ModuleType('pytdx.hq')
                fake.TdxHq_API = FakeAPI
                with tempfile.TemporaryDirectory() as d, redirect_stdout(io.StringIO()), patch.object(importlib, 'import_module', return_value=fake) as importer, patch.object(importlib.metadata, 'distribution') as distribution, patch.object(sys.modules[__name__], 'bundled_server', return_value={'host':'1.2.3.4','port':7709}):
                    distribution.return_value.files = []
                    distribution.return_value.version = 'fake'
                    a = arguments(['--run','--count','5'])
                    def advance(seconds):
                        ticks[0] += seconds
                    result = probe(a, Path(d).resolve()/'monitor'/'validation', sleep=advance, monotonic=lambda:ticks[0])
                    self.assertEqual(result, int(fail))
                    bars = [c for c in calls if c[0] == 'bars']
                    self.assertEqual(len(bars), 1 if fail else 5)
                    self.assertTrue(all(b[1]-a[1] >= 60 for a,b in zip(bars,bars[1:])))
                    self.assertTrue(all(c[2] == (8,1,'600000',0,30) for c in bars))
                    self.assertEqual(sum(c[0]=='connect' for c in calls), 1)
                    self.assertEqual(calls[-1], ('disconnect',))
                    content = next((Path(d).resolve()/'monitor'/'validation').glob('*.jsonl')).read_text()
                    self.assertNotIn('上游敏感正文', content)
                    importer.assert_called_once_with('tdxpy.hq')

    suite = unittest.defaultTestLoader.loadTestsFromTestCase(OfflineTests)
    return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1


def main(argv=None):
    a = arguments(argv)
    if a.self_test:
        return self_test()
    if not a.run:
        print('验证默认关闭；--self-test 离线自测；--run 才导入依赖并开启有限验证。')
        return 0
    try:
        return probe(a)
    except KeyboardInterrupt:
        print('已停止本次有限验证。')
        return 130
    except Exception as exc:
        print(f'停止：{classify(exc)} ({type(exc).__name__})；不输出异常正文。')
        return 1


if __name__ == '__main__':
    sys.exit(main())
