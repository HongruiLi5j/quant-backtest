"""腾讯公开地址的有限请求适配；无官方文档/明确许可，仅个人验证。

逗号分隔的多代码请求尚未真实验证；默认仅 sh600000。成交量单位未核实。
不使用字段 34/35 或其他未核实字段，不保存上游原文。
"""
from contextlib import contextmanager
from datetime import datetime
import http.client
import math
import re
import signal
import socket
import threading
import time
import urllib.error
import urllib.request

from .core import Quote, valid_symbol


class FeedError(Exception):
    def __init__(self, category, http_status=None):
        self.category = category
        self.http_status = http_status
        super().__init__(category)


@contextmanager
def deadline(seconds):
    """Unix 主线程信号时限；不支持时安全拒绝。

    DNS 的 C 阻塞调用能否被及时中断尚未验证，不能保证整体硬上限。
    S2 需独立工作进程加外层强制终止；本阶段不实现该生命周期。
    不覆盖宿主已有闹钟。
    超时抛 FeedError（不是 OSError），避免被 socket.create_connection 的
    多地址回退或 urllib 的网络错误捕获逻辑吞掉，超时后不得继续下一地址。
    """
    if (not hasattr(signal, 'setitimer') or threading.current_thread() is not threading.main_thread()
            or signal.getitimer(signal.ITIMER_REAL) != (0.0, 0.0)):
        raise FeedError('hard_timeout_unavailable')
    def expired(signum, frame):
        raise FeedError('timeout')
    previous = signal.signal(signal.SIGALRM, expired)
    try:
        signal.setitimer(signal.ITIMER_REAL, seconds)
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def connect_once(address, timeout, source_address=None):
    """只尝试 DNS 返回的首个地址；包括 FeedError 在内的异常均关闭套接字。"""
    host, port = address
    addresses = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    if not addresses:
        raise OSError('no_address')
    family, kind, protocol, _, target = addresses[0]
    connection = socket.socket(family, kind, protocol)
    try:
        connection.settimeout(timeout)
        if source_address:
            connection.bind(source_address)
        connection.connect(target)
        return connection
    except BaseException:
        connection.close()
        raise


class TimeoutSafeHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = connect_once

    def connect(self):
        try:
            super().connect()
        except BaseException:
            self.close()
            raise


class TimeoutSafeHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(TimeoutSafeHTTPSConnection, req, context=self._context)


def number(raw):
    try:
        value = float(raw)
        return value if math.isfinite(value) else None
    except (ValueError, OverflowError):
        return None


def parse_response(body, symbols, received_at, elapsed_seconds):
    try:
        decoded = body.decode('gbk', errors='strict')
    except UnicodeDecodeError:
        raise FeedError('gbk_decode_error') from None
    lines = decoded.strip().splitlines()
    if len(lines) != len(symbols):
        raise FeedError('response_count_mismatch')
    records = {}
    for line in lines:
        match = re.fullmatch(r'v_((?:sh|sz)\d{6})="([^"\r\n]*)";', line.strip())
        if match is None:
            raise FeedError('response_format_error')
        symbol, payload = match.groups()
        fields = payload.split('~')
        if len(fields) < 31:
            raise FeedError('insufficient_fields')
        if symbol not in symbols or fields[2] != symbol[2:] or symbol in records:
            raise FeedError('code_mismatch')
        if len(fields[1]) > 80 or any(len(fields[i]) > 64 for i in (3, 4, 5, 6, 30)):
            raise FeedError('whitelist_field_too_long')
        # 只保留已获准的白名单字段；原值不截断，不保存整行上游原文。
        records[symbol] = Quote(symbol, fields[1], number(fields[3]), number(fields[4]),
                                number(fields[5]), fields[6], fields[30], None,
                                received_at, elapsed_seconds,
                                fields_raw=tuple(fields[i] for i in (3, 4, 5, 6, 30)))
    return [records[symbol] for symbol in symbols]


class TencentProvider:
    """默认未 start；start 只启用预算，不发请求。无重试，失败也计入预算。

    每次 start 分配一份预算，单次 start 内最多 5 次；同实例再次 start
    不得刷新间隔，活动期间不能重置预算。urllib 默认请求头含默认的客户端身份头，
    未添加自定义头；上游是否接受该默认身份尚未真实验证。
    只尝试 DNS 首个地址，不做地址回退。timeout 参数最多 10 秒，但真实 DNS
    阻塞的硬时限未验证；整体硬上限需 S2 独立进程的外层强制终止保障。
    """
    def __init__(self, *, min_interval=60, request_budget=3, timeout=10,
                 monotonic=time.monotonic, now=lambda: datetime.now().astimezone()):
        if (not math.isfinite(min_interval) or min_interval < 60
                or type(request_budget) is not int or not 1 <= request_budget <= 5
                or not math.isfinite(timeout) or not 0 < timeout <= 10):
            raise ValueError('非法请求限制')
        self._min_interval, self._request_budget, self._timeout = min_interval, request_budget, timeout
        self._clock, self._now = monotonic, now
        self._active, self._used, self._last_start = False, 0, None
        self._lock = threading.Lock()

    @property
    def min_interval(self):
        return self._min_interval

    @property
    def request_budget(self):
        return self._request_budget

    @property
    def timeout(self):
        return self._timeout

    @property
    def requests_used(self):
        return self._used

    def start(self):
        with self._lock:
            if self._active:
                raise FeedError('already_started')
            self._active, self._used = True, 0

    def stop(self):
        with self._lock:
            self._active = False

    def fetch(self, symbols=('sh600000',)):
        symbols = tuple(symbols)
        if (not 1 <= len(symbols) <= 5 or len(set(symbols)) != len(symbols)
                or not all(valid_symbol(s) for s in symbols)):
            raise FeedError('invalid_symbols')
        # 整次请求持锁，不允许并发重叠或并发重置计数。
        with self._lock:
            started = self._clock()
            if not self._active:
                raise FeedError('not_started')
            if self._used >= self.request_budget:
                raise FeedError('budget_exceeded')
            if self._last_start is not None and started - self._last_start < self.min_interval:
                raise FeedError('minimum_interval')
            self._used += 1
            self._last_start = started
            try:
                with deadline(self.timeout):
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
                                                         TimeoutSafeHTTPSHandler())
                    # 使用 urllib 的默认请求头，不添加任何自定义请求头，也不改变其默认身份。
                    request = urllib.request.Request('https://qt.gtimg.cn/q=' + ','.join(symbols))
                    with opener.open(request, timeout=self.timeout) as response:
                        status = response.status
                        if status != 200:
                            raise FeedError('http_status', status)
                        body = response.read(65537)
                        if len(body) > 65536:
                            raise FeedError('response_too_large', status)
                elapsed = self._clock() - started
                if elapsed > self.timeout:
                    raise FeedError('timeout')
                try:
                    return parse_response(body, symbols, self._now(), elapsed)
                except FeedError as exc:
                    exc.http_status = 200
                    raise
            except FeedError:
                raise
            except urllib.error.HTTPError as exc:
                status = exc.code
                exc.close()
                raise FeedError('redirect_refused' if 300 <= status < 400 else 'http_status', status) from None
            except http.client.HTTPException:
                raise FeedError('network_error') from None
            except (TimeoutError, urllib.error.URLError, OSError) as exc:
                cause = getattr(exc, 'reason', exc)
                raise FeedError('timeout' if isinstance(cause, TimeoutError) else 'network_error') from None
