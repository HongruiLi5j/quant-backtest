"""yfinance 的有限请求适配（美股）；yfinance 是 Yahoo 非官方公开接口的封装，仅个人少量自用。

- yfinance 在首次 fetch 时才 import；未安装则抛 ImportError 并把原因记在 last_error，
  不消耗请求预算（上层可据此降级为"数据源不可用"）。
- 严格速率：同一代码两次请求至少相隔 min_interval 秒（不得小于 60）；一次最多 5 只；
  失败的请求同样计入预算和间隔。每只代码单独调用 Ticker(sym).history()（预算按只计，一只扣 1；
  注意：这只是 chart 请求数，yfinance 库内部还会额外发时区、cookie、crumb 请求，本模块未计入预算，
  首次请求某只代码时实际 HTTP 请求数约为预算计数的 2~4 倍；每只代码 chart 请求的 60 秒间隔仍严格成立。
  整批预算不足则一只都不发）。不用 yf.download：1.7.0 的 download 会把每只的异常
  （含 429、超时）吞进私有表并返回空表，调用方无法区分限流与无数据。
- 令 yf.config.debug.hide_exceptions = False，使超时/网络错误从 history() 抛出；
  限流（YFRateLimitError）本来就一定抛出。遇到限流/超时/网络错误立即中止整批，不再请求后面的代码。
- 不使用代理（清除进程内代理环境变量，并令 yf.config.network.proxy = None），
  用户代码不设置任何自定义请求头，不传凭据（库内部行为见下方透明说明）。
- 失败分类是固定集合，不回传上游异常文本或原始数据。
- 单次失败最多重试 2 次，指数退避（间隔 min_interval × 2^k），限流(429)不重试；
  整批代码都被上游明确判为无数据（退市/写错，symbol_missing）也不重试。
- 超时按单只 history() 的总耗时事后判定；首次请求可能含时区/crumb 等多个子请求，偶尔会误判为 timeout。
- 未安装时 fetch 抛 ImportError（不是 FeedError），由 S2 子进程封装统一处理；404 等 HTTP 错误按 network 重试。
- 整体硬上限（DNS/TLS 阻塞）需由独立子进程外层强制终止保障（S2）；此处只传 timeout 并在事后核对耗时。

# yfinance 库的透明说明 (2026-10-22 所有者批准):
# yfinance 1.7.0 内部通过 curl_cffi 做 Chrome TLS 指纹和 User-Agent 伪装
# (见 yfinance/_http.py:61 的 new_session 实现)。
# 用户代码本身不设置任何 User-Agent, Cookie, Referer, Accept 等 header,
# 也不启用任何伪装机制。只是接受 yfinance 的默认行为。
# 此为个人自用, 每股每 60 秒刷一次, 流量极小。
"""
from contextlib import contextmanager
from datetime import datetime
import math
import os
import threading
import time

from ..market_monitor.providers import FeedError
from .core import Quote, US_EASTERN, valid_symbol

MAX_SYMBOLS = 5
MIN_INTERVAL = 60
MAX_TIMEOUT = 10
MAX_RETRIES = 2
MAX_BUDGET = 5000
# 可重试的瞬时失败；rate_limited / bad_response 不重试，避免加重限流或重复拿到坏数据
RETRYABLE = frozenset({'timeout', 'network', 'service_error', 'empty'})
PROXY_VARS = ('http_proxy', 'https_proxy', 'all_proxy', 'no_proxy')
IMPORT_ERROR_TEXT = 'yfinance_not_installed'


@contextmanager
def no_proxy_env():
    """临时移除进程内代理环境变量（含大小写），退出时原样恢复。"""
    saved = {k: v for k, v in os.environ.items() if k.lower() in PROXY_VARS}
    for k in saved:
        del os.environ[k]
    try:
        yield
    finally:
        os.environ.update(saved)


def load_yfinance():
    import yfinance
    return yfinance


def classify(exc):
    """把异常映射到固定类别；只看类型名和是否 429，不外传异常文本。"""
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    if 'ratelimit' in name or '429' in text or 'too many requests' in text:
        return 'rate_limited'
    if isinstance(exc, TimeoutError) or 'timeout' in name or 'timed out' in text:
        return 'timeout'
    if isinstance(exc, (ConnectionError, OSError)) or 'connection' in name or 'network' in name:
        return 'network'
    if 'missing' in name:
        # YFPricesMissingError / YFTzMissingError：代码无数据（退市、写错），按空数据处理
        return 'empty'
    return 'service_error'


def number(value):
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def frame_for(frame, symbol):
    """从 Ticker.history 的 DataFrame 取单只代码的列；兼容 (Ticker, Price) 与 (Price, Ticker) 两种列层级。"""
    columns = frame.columns
    if getattr(columns, 'nlevels', 1) == 1:
        return frame
    for level in range(columns.nlevels):
        if symbol in columns.get_level_values(level):
            return frame.xs(symbol, axis=1, level=level)
    return None


def build_quote(frame, symbol, received_at, elapsed):
    """由 1 分钟 K 线（5 个交易日）汇总出当日报价：

    现价=最后一根收盘；开盘=当日第一根开盘；最高/最低=当日极值；成交量=当日各根之和（股）；
    昨收=前一个交易日最后一根的收盘（近似官方收盘价，可能有微小差异）；
    行情时间=最后一根的起始时间（偏保守，比实际数据时刻略早）。
    """
    sub = frame_for(frame, symbol)
    if sub is None:
        return None
    try:
        sub = sub[['Open', 'High', 'Low', 'Close', 'Volume']].dropna(subset=['Close'])
    except (KeyError, TypeError):
        raise FeedError('bad_response') from None
    if len(sub) == 0:
        return None
    index = sub.index
    if getattr(index, 'tz', None) is None:
        raise FeedError('bad_response')
    local = index.tz_convert(US_EASTERN)
    days = local.date
    today = days[-1]
    mask = days == today
    today_rows = sub[mask]
    earlier = sub[~mask]
    previous_close = number(earlier['Close'].iloc[-1]) if len(earlier) else None
    volume_sum = number(today_rows['Volume'].fillna(0).sum())
    volume = int(volume_sum) if volume_sum is not None and volume_sum >= 0 else None
    return Quote(symbol, number(today_rows['Close'].iloc[-1]), previous_close,
                 number(today_rows['Open'].iloc[0]), number(today_rows['High'].max()),
                 number(today_rows['Low'].min()), volume,
                 local[mask][-1].to_pydatetime(), received_at, elapsed)


class YFinanceProvider:
    """默认未 start；start 只启用预算，不发请求。"""
    def __init__(self, *, min_interval=MIN_INTERVAL, request_budget=2500, timeout=MAX_TIMEOUT,
                 max_retries=MAX_RETRIES, loader=load_yfinance, cache_dir=None,
                 monotonic=time.monotonic, sleep=time.sleep, now=lambda: datetime.now().astimezone()):
        if (not math.isfinite(min_interval) or min_interval < MIN_INTERVAL
                or type(request_budget) is not int or not 1 <= request_budget <= MAX_BUDGET
                or not math.isfinite(timeout) or not 0 < timeout <= MAX_TIMEOUT
                or type(max_retries) is not int or not 0 <= max_retries <= MAX_RETRIES):
            raise ValueError('非法请求限制')
        self._min_interval, self._request_budget, self._timeout = min_interval, request_budget, timeout
        self._max_retries, self._loader, self._cache_dir = max_retries, loader, cache_dir
        self._clock, self._sleep, self._now = monotonic, sleep, now
        self._active, self._used, self._last = False, 0, {}
        self._yf = None
        self.last_error = None
        self._lock = threading.Lock()

    min_interval = property(lambda self: self._min_interval)
    request_budget = property(lambda self: self._request_budget)
    timeout = property(lambda self: self._timeout)
    requests_used = property(lambda self: self._used)

    def start(self):
        with self._lock:
            if self._active:
                raise FeedError('already_started')
            # 只清预算；保留每只代码的上次请求时间，stop 后马上 start 也绕不过 60 秒间隔
            self._active, self._used = True, 0

    def stop(self):
        with self._lock:
            self._active = False

    def available(self):
        """yfinance 是否可用；不可用时 last_error 记录原因，不抛错。"""
        try:
            self._module()
            return True
        except ImportError:
            return False

    def _module(self):
        if self._yf is None:
            try:
                module = self._loader()
            except ImportError:
                self.last_error = IMPORT_ERROR_TEXT
                raise ImportError(IMPORT_ERROR_TEXT) from None
            # 不走代理；缓存目录（可选）指到调用者给的位置，避免写进项目目录
            network = getattr(getattr(module, 'config', None), 'network', None)
            if network is not None:
                network.proxy = None
            debug = getattr(getattr(module, 'config', None), 'debug', None)
            if debug is not None:
                debug.hide_exceptions = False
            if self._cache_dir and hasattr(module, 'set_tz_cache_location'):
                module.set_tz_cache_location(str(self._cache_dir))
            self._yf = module
        return self._yf

    def fetch(self, symbols):
        symbols = tuple(symbols)
        if (not 1 <= len(symbols) <= MAX_SYMBOLS or len(set(symbols)) != len(symbols)
                or not all(valid_symbol(s) for s in symbols)):
            raise FeedError('invalid_symbols')
        # 整次调用（含退避等待）持锁，不允许并发重叠或并发重置计数。
        with self._lock:
            if not self._active:
                raise FeedError('not_started')
            module = self._module()
            attempt = 0
            while True:
                try:
                    return self._attempt(module, symbols)
                except FeedError as exc:
                    if exc.category not in RETRYABLE or attempt >= self._max_retries:
                        raise
                    self._sleep(self._min_interval * 2 ** attempt)
                    attempt += 1

    def _attempt(self, module, symbols):
        started = self._clock()
        # 一只=一次真实 HTTP 请求，预算按只计；整批预算不足则一只都不发
        if self._request_budget - self._used < len(symbols):
            raise FeedError('budget_exceeded')
        for symbol in symbols:
            last = self._last.get(symbol)
            if last is not None and started - last < self._min_interval:
                raise FeedError('minimum_interval')
        quotes = []
        missing = 0
        with no_proxy_env():
            for symbol in symbols:
                try:
                    quote = self._request_one(module, symbol)
                except FeedError as exc:
                    # 个别代码无数据只跳过；限流/超时/网络等立即中止整批，不继续加压
                    if exc.category == 'empty':
                        missing += 1
                        continue
                    raise
                if quote is not None:
                    quotes.append(quote)
        # 个别代码缺失时只返回拿到的；全部缺失才算失败。调用者按 symbol 对应。
        if not quotes:
            # 全部是上游明确的"无此代码"异常：重试无意义，不重试；返回空表才按 empty 重试
            raise FeedError('symbol_missing' if missing == len(symbols) else 'empty')
        return quotes

    def _request_one(self, module, symbol):
        began = self._clock()
        self._used += 1
        self._last[symbol] = began
        try:
            frame = module.Ticker(symbol).history(period='5d', interval='1m', prepost=False,
                                                  actions=False, timeout=self._timeout)
        except FeedError:
            raise
        except Exception as exc:
            raise FeedError(classify(exc)) from None
        elapsed = self._clock() - began
        if elapsed > self._timeout:
            raise FeedError('timeout')
        if frame is None or getattr(frame, 'empty', True):
            return None
        return build_quote(frame, symbol, self._now(), elapsed)
