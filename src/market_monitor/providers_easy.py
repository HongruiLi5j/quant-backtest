"""easyquotation 批量行情适配（新浪为主、腾讯 qq 为备）；仅个人验证，无官方许可。

与 TencentProvider 的接口保持一致：start()/stop()/fetch(symbols) -> list[Quote]，
另有 fetch_many(symbols) -> BatchResult（允许部分成功）。TencentProvider 原样保留作兜底，
easyquotation 未安装时构造会抛 ImportError，可用 create_with_fallback 切回。

请求头政策（所有者 D1）：本文件的用户代码不构造、不覆盖任何自定义请求头、会话或代理设置；
网络请求完全由 easyquotation 内部发出，库内部的默认请求细节属第三方实现，不是我们的主动伪装。
速率：每次 start 一份预算（默认 20000 次，失败也计入）；相邻两次请求至少间隔 5 秒；
单次请求最多 10 秒。真实 DNS 阻塞的硬时限与 TencentProvider 一样依赖主线程信号，未验证。
成交量单位：新浪与腾讯口径不同且未核实，volume_raw 原样保留，Quote.provider 区分来源，
消费方必须按 provider 分别建立成交量基线，切源时清基线。
"""
from dataclasses import dataclass
from datetime import datetime
import importlib
import logging
import math
import threading
import time

from .core import Quote, valid_symbol
from .providers import FeedError, deadline, number

LOG = logging.getLogger('market_monitor')

SOURCES = ('sina', 'qq')
# 单次返回条数上限（保守值，所有者问题 4）。
MAX_PER_REQUEST = {'sina': 500, 'qq': 800}
# URL 长度上限的安全值（上限 × 0.7）折算出的单次代码数：腾讯约 2800 字符、新浪约 1400 字符。
URL_SAFE_PER_REQUEST = {'sina': 200, 'qq': 400}
REQUEST_GAP = 10            # 调度层相邻两轮的最小间隔（秒）
MIN_BATCH_INTERVAL = 5      # 相邻两次批量请求的最小间隔（秒）
DEFAULT_BUDGET = 20000
MAX_SYMBOLS = 5500


def batch_size(source):
    """单次 fetch 的实际代码数 = min(返回上限, URL 安全上限)。"""
    return min(MAX_PER_REQUEST[source], URL_SAFE_PER_REQUEST[source])


def effective_interval(count, source='sina', gap=MIN_BATCH_INTERVAL):
    """一轮刷新的最小间隔：不少于 REQUEST_GAP，也不少于把名单拆成各批后批间等待的总和。"""
    chunks = max(1, math.ceil(count / batch_size(source)))
    return max(REQUEST_GAP, chunks * gap)


@dataclass(frozen=True)
class BatchResult:
    quotes: list
    missing: tuple
    source: str
    requests_made: int
    elapsed: float
    degraded: bool = False      # 是否有批次用了非首选源


def _text(value, limit):
    text = '' if value is None else str(value)
    if len(text) > limit:
        raise FeedError('whitelist_field_too_long')
    return text


def _source_time(source, item):
    """统一成 14 位 YYYYMMDDHHMMSS；缺失或格式不符返回空串，由下游质量检查标记。"""
    if source == 'sina':
        raw = (str(item.get('date', '')) + str(item.get('time', ''))).replace('-', '').replace(':', '')
    else:
        raw = str(item.get('datetime', ''))
    return raw if len(raw) == 14 and raw.isdigit() else ''


def _to_quote(source, symbol, item, received_at, elapsed):
    # 只取白名单字段；不保存整条上游原文。
    name = _text(item.get('name'), 80)
    volume_raw = _text(item.get('volume'), 64)
    # 新浪的昨收字段叫 close，腾讯同名；两者含义一致。
    price, prev, opening = (number(item.get(k)) for k in ('now', 'close', 'open'))
    return Quote(symbol, name, price, prev, opening, volume_raw, _source_time(source, item), None,
                 received_at, elapsed, provider='easyquotation_' + source,
                 fields_raw=tuple(str(item.get(k, '')) for k in ('now', 'close', 'open', 'volume')))


class EasyQuotationProvider:
    """默认未 start；start 只启用预算，不发请求。无重试，失败也计入预算。

    易错点：同一次 fetch 内拆成多批时，批间用 sleep 等待最小间隔；
    跨两次 fetch 若间隔不足则直接抛 minimum_interval（与 TencentProvider 一致，由调度层决定何时再来）。
    """
    def __init__(self, *, sources=SOURCES, request_budget=DEFAULT_BUDGET, min_interval=MIN_BATCH_INTERVAL,
                 timeout=10, importer=None, monotonic=time.monotonic, sleep=time.sleep,
                 now=lambda: datetime.now().astimezone()):
        sources = tuple(sources)
        if (not sources or any(s not in SOURCES for s in sources) or len(set(sources)) != len(sources)
                or type(request_budget) is not int or request_budget < 1
                or not math.isfinite(min_interval) or min_interval < MIN_BATCH_INTERVAL
                or not math.isfinite(timeout) or not 0 < timeout <= 10):
            raise ValueError('非法请求限制')
        self._sources, self._request_budget = sources, request_budget
        self._min_interval, self._timeout = min_interval, timeout
        self._clock, self._sleep, self._now = monotonic, sleep, now
        try:
            self._module = (importer or (lambda: importlib.import_module('easyquotation')))()
        except ImportError:
            LOG.warning('easyquotation 未安装，EasyQuotationProvider 不可用，请改用 TencentProvider')
            raise
        self._clients = {}
        self._active, self._used, self._last_start = False, 0, None
        self._lock = threading.Lock()

    @property
    def sources(self):
        return self._sources

    @property
    def request_budget(self):
        return self._request_budget

    @property
    def min_interval(self):
        return self._min_interval

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

    def _client(self, source):
        if source not in self._clients:
            try:
                self._clients[source] = self._module.use(source)
            except Exception:
                raise FeedError('client_init_error') from None
        return self._clients[source]

    def _wait_gap(self):
        """保证相邻两次请求起点至少相隔 min_interval 秒。"""
        if self._last_start is not None:
            remaining = self._min_interval - (self._clock() - self._last_start)
            if remaining > 0:
                self._sleep(remaining)

    def _request(self, source, symbols):
        if self._used >= self._request_budget:
            raise FeedError('budget_exceeded')
        client = self._client(source)
        self._wait_gap()
        started = self._clock()
        self._used += 1
        self._last_start = started
        codes = [s[2:] for s in symbols]
        try:
            with deadline(self._timeout):
                data = client.stocks(codes)
        except FeedError:
            raise
        except TimeoutError:
            raise FeedError('timeout') from None
        except Exception:
            raise FeedError('network_error') from None
        elapsed = self._clock() - started
        if elapsed > self._timeout:
            raise FeedError('timeout')
        if not isinstance(data, dict):
            raise FeedError('response_format_error')
        received = self._now()
        quotes = {}
        for symbol in symbols:
            item = data.get(symbol[2:])
            if isinstance(item, dict):
                try:
                    quotes[symbol] = _to_quote(source, symbol, item, received, elapsed)
                except FeedError:
                    continue
        if not quotes:
            raise FeedError('empty_response')
        return quotes

    def fetch_many(self, symbols):
        symbols = tuple(symbols)
        if (len(symbols) > MAX_SYMBOLS or len(set(symbols)) != len(symbols)
                or not all(valid_symbol(s) for s in symbols)):
            raise FeedError('invalid_symbols')
        if not symbols:
            return BatchResult([], (), self._sources[0], 0, 0.0)
        with self._lock:
            started = self._clock()
            if not self._active:
                raise FeedError('not_started')
            if self._last_start is not None and started - self._last_start < self._min_interval:
                raise FeedError('minimum_interval')
            used_before, found, degraded, last_error, abort = self._used, {}, False, None, False
            primary = self._sources[0]
            # 备用源的单批上限可能更小，保守按各源最小值拆批，保证任一源都不超限。
            size = min(batch_size(s) for s in self._sources)
            for i in range(0, len(symbols), size):
                chunk = symbols[i:i + size]
                for source in self._sources:
                    try:
                        found.update(self._request(source, chunk))
                        degraded = degraded or source != primary
                        break
                    except FeedError as exc:
                        last_error = exc
                        LOG.warning('easyquotation 数据源 %s 失败：%s', source, exc.category)
                        if exc.category in ('budget_exceeded', 'hard_timeout_unavailable'):
                            abort = True
                            break
                if abort:
                    break
            if not found:
                raise last_error or FeedError('empty_response')
            missing = tuple(s for s in symbols if s not in found)
            return BatchResult([found[s] for s in symbols if s in found], missing, primary,
                               self._used - used_before, self._clock() - started, degraded)

    def fetch(self, symbols):
        return self.fetch_many(symbols).quotes


def create_with_fallback(fallback_factory=None, **kwargs):
    """优先 EasyQuotationProvider；未安装 easyquotation 时降级为 fallback_factory()（通常是 TencentProvider）。

    返回 (provider, degraded_reason)；未降级时 reason 为 None。没有兜底工厂则原样抛 ImportError。
    """
    try:
        return EasyQuotationProvider(**kwargs), None
    except ImportError:
        if fallback_factory is None:
            raise
        return fallback_factory(), 'easyquotation_not_installed'
