"""默认停止的有限盯盘会话；后台线程监督单个可取消的请求进程。"""
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
import multiprocessing
import threading
import time

from .core import (BEIJING, AlertState, Condition, alert_step, continuous_session,
                   positive, valid_symbol, validate_quote)
from .providers import FeedError, TencentProvider
from .store import Store, StorageError, empty_state


def symbol_for(code):
    if not isinstance(code, str) or len(code) != 6 or not code.isascii() or not code.isdigit():
        raise ValueError('invalid_code')
    symbol = ('sh' if code.startswith('6') else 'sz') + code
    if not valid_symbol(symbol):
        raise ValueError('invalid_code')
    return symbol


@dataclass(frozen=True)
class Config:
    poll_seconds: float = 60
    request_budget: int = 5
    multi_code_verified: bool = False
    timeout_seconds: float = 10

    def __post_init__(self):
        if (not positive(self.poll_seconds) or self.poll_seconds < 60
                or type(self.request_budget) is not int or not 1 <= self.request_budget <= 600
                or type(self.multi_code_verified) is not bool
                or not positive(self.timeout_seconds) or self.timeout_seconds > 10):
            raise ValueError('invalid_config')


def _request_child(pipe, symbols, timeout, factory):
    """spawn 子进程主线程，S1 的 SIGALRM 可用；父进程另设硬截止。"""
    provider = None
    try:
        provider = factory(request_budget=1, timeout=timeout)
        provider.start()
        pipe.send(('ok', provider.fetch(symbols)))
    except FeedError as exc:
        # 白名单类别，绝不把异常文本或上游原文传出。
        known = {'timeout', 'network_error', 'http_status', 'redirect_refused',
                 'gbk_decode_error', 'response_count_mismatch', 'response_format_error',
                 'insufficient_fields', 'code_mismatch', 'whitelist_field_too_long',
                 'response_too_large', 'hard_timeout_unavailable'}
        pipe.send(('error', exc.category if exc.category in known else 'provider_error'))
    except Exception:
        pipe.send(('error', 'provider_error'))
    finally:
        if provider is not None:
            provider.stop()
        pipe.close()


def _batch_child(pipe, symbols, timeout, factory):
    """批量请求子进程：只走 easyquotation（fetch_many，允许部分成功）。

    未安装时不在这里兜底到腾讯：腾讯多代码写法既未验证、又会绕过调度层的每股 60 秒约束。
    这里直接报 provider_error，由调度层连续失败 3 次后降级为逐只模式（TencentProvider，守原速率约束）。
    """
    from .providers_easy import create_with_fallback
    provider = None
    try:
        provider, _ = create_with_fallback(None, timeout=timeout)    # 未安装 → ImportError → provider_error
        provider.start()
        pipe.send(('ok', provider.fetch_many(symbols)))
    except FeedError as exc:
        known = {'timeout', 'network_error', 'http_status', 'redirect_refused', 'gbk_decode_error',
                 'response_count_mismatch', 'response_format_error', 'insufficient_fields', 'code_mismatch',
                 'whitelist_field_too_long', 'response_too_large', 'hard_timeout_unavailable',
                 'empty_response', 'budget_exceeded', 'minimum_interval'}
        pipe.send(('error', exc.category if exc.category in known else 'provider_error'))
    except Exception:
        pipe.send(('error', 'provider_error'))
    finally:
        if provider is not None:
            provider.stop()
        pipe.close()


def process_batch_request(symbols, cancelled, timeout):
    """批量版 process_request：返回 BatchResult。一次请求可能内部拆成多批（每批之间等 5 秒），截止时间按批数放宽。"""
    from .providers_easy import MIN_BATCH_INTERVAL, batch_size
    chunks = max(1, -(-len(symbols) // min(batch_size(s) for s in ('sina', 'qq'))))
    return process_request(symbols, cancelled, timeout, provider_factory=None, _child=_batch_child,
                           _extra=chunks * (timeout + MIN_BATCH_INTERVAL))


def process_request(symbols, cancelled, timeout, *, provider_factory=TencentProvider, _child=_request_child, _extra=0):
    """每次独立 Provider；服务负责跨进程预算/间隔，阻塞 DNS 也能终止。"""
    if cancelled.is_set():
        raise FeedError('cancelled')
    ctx = multiprocessing.get_context('spawn')
    parent, child = ctx.Pipe(duplex=False)
    process = ctx.Process(target=_child, args=(child, symbols, timeout, provider_factory))
    deadline = time.monotonic() + timeout + 2 + _extra
    try:
        process.start()
        child.close()
        while not cancelled.is_set():
            if parent.poll(.05):
                try:
                    kind, result = parent.recv()
                except EOFError:
                    raise FeedError('worker_exit') from None
                if kind != 'ok':
                    raise FeedError(result)
                return result
            if not process.is_alive():
                raise FeedError('worker_exit')
            if time.monotonic() >= deadline:
                raise FeedError('timeout')
        raise FeedError('cancelled')
    finally:
        if process.pid is not None:
            if process.is_alive():
                process.terminate()
            process.join(1)
            if process.is_alive():
                process.kill()
                process.join()
            process.close()
        parent.close()
        child.close()


class MonitorService:
    def __init__(self, *, store=None, config=None, now=None, monotonic=None,
                 request=None, wait=None, calendar=None):
        self.store = store or Store()
        self.config = config or Config()
        self.now = now or (lambda: datetime.now(BEIJING))
        self.clock = monotonic or time.monotonic
        self.request = request or process_request
        self.wait = wait or (lambda event, seconds: event.wait(seconds))
        self.calendar = calendar
        self._lock = threading.RLock()
        self._lifecycle = threading.Lock()
        self._thread = None
        self._cancel = threading.Event()
        self._session = 0
        self._version = 0
        self._status, self._reason = 'stopped', None
        self._used, self._failures = 0, 0
        self._last_request = None
        self._last_symbols = {}
        self._previous, self._states = {}, {}
        try:
            self._data = self.store.load()
            # 持久化内容仍需按业务边界核对；损坏时禁止 start。
            if len(self._data['watch']) > 5 or len(set(self._data['watch'])) != len(self._data['watch']):
                raise ValueError()
            for code in self._data['watch']:
                symbol_for(code)
            for key, raw in self._data['rules'].items():
                condition = Condition(**raw)
                if key != condition.symbol[2:] + ':' + condition.direction or key[:6] not in self._data['watch']:
                    raise ValueError()
        except (StorageError, ValueError, TypeError, KeyError):
            self._data = empty_state()
            self._status, self._reason = 'paused', 'storage_error'

    def _save(self, candidate):
        try:
            self.store.save(candidate)
        except StorageError:
            self._pause('storage_error')
            raise
        self._data = candidate

    def _pause(self, reason):
        self._status, self._reason = 'paused', reason
        self._cancel.set()
        self._previous.clear()
        self._states.clear()

    def configure(self, operation, code, condition=None):
        symbol_for(code)
        with self._lock:
            if self._reason == 'storage_error':
                raise StorageError('storage_error')
            candidate = deepcopy(self._data)
            if operation == 'watch_add':
                if code not in candidate['watch']:
                    if len(candidate['watch']) >= 5:
                        raise ValueError('watch_limit')
                    candidate['watch'].append(code)
            elif operation == 'watch_remove':
                candidate['watch'] = [c for c in candidate['watch'] if c != code]
                candidate['rules'] = {k: v for k, v in candidate['rules'].items() if k[:6] != code}
            elif operation == 'rule_set':
                if code not in candidate['watch'] or condition.symbol != symbol_for(code):
                    raise ValueError('not_watched')
                candidate['rules'][code + ':' + condition.direction] = asdict(condition)
            elif operation == 'rule_delete':
                candidate['rules'].pop(code + ':' + condition, None)
            else:
                raise ValueError('invalid_operation')
            self._save(candidate)
            self._version += 1
            self._previous.clear()
            self._states.clear()

    def start(self):
        with self._lifecycle:
            with self._lock:
                if self._thread is not None and self._thread.is_alive():
                    return False
                if self._reason == 'storage_error':
                    raise StorageError('storage_error')
                self._session += 1
                self._used, self._failures = 0, 0
                self._previous, self._states = {}, {}
                self._cancel = threading.Event()
                self._status, self._reason = 'running', None
                self._thread = threading.Thread(target=self._run, args=(self._session, self._cancel),
                                                name='market-monitor', daemon=True)
                self._thread.start()
                return True

    def stop(self):
        with self._lifecycle:
            with self._lock:
                self._session += 1
                self._cancel.set()
                thread = self._thread
                self._status, self._reason = 'stopped', self._reason if self._reason == 'storage_error' else None
                self._previous.clear()
                self._states.clear()
            if thread is not None:
                thread.join()
            with self._lock:
                self._thread = None

    def _eligible(self):
        at = self.now()
        if not continuous_session(at):
            return False
        if self.calendar is not None:
            try:
                # None 表示日历不可用；不猜节假日，由行情新鲜度和预算兜底。
                if self.calendar(at.astimezone(BEIJING).date()) is False:
                    return False
            except Exception:
                pass
        return True

    def _run(self, session, cancel):
        try:
            while not cancel.is_set():
                cycle_start = self.clock()
                if not self._eligible():
                    self.wait(cancel, self.config.poll_seconds)
                    continue
                with self._lock:
                    codes = tuple(self._data['watch'])
                groups = [codes] if self.config.multi_code_verified and codes else [(c,) for c in codes]
                for group in groups:
                    with self._lock:
                        if cancel.is_set() or session != self._session:
                            return
                        if self._used >= self.config.request_budget:
                            self._pause('budget_exhausted')
                            return
                        earliest = max([cycle_start] +
                                       ([self._last_request + 10] if self._last_request is not None else []) +
                                       [self._last_symbols[c] + self.config.poll_seconds for c in group if c in self._last_symbols])
                    self.wait(cancel, max(0, earliest - self.clock()))
                    with self._lock:
                        if cancel.is_set() or session != self._session:
                            return
                        if not self._eligible():
                            break
                        # 配置变更之后旧分组不再发请求。
                        if any(c not in self._data['watch'] for c in group):
                            continue
                        version = self._version
                        self._used += 1
                        self._last_request = self.clock()
                        for code in group:
                            self._last_symbols[code] = self._last_request
                    try:
                        result = self.request(tuple(symbol_for(c) for c in group), cancel, self.config.timeout_seconds)
                        error = None
                    except FeedError as exc:
                        result, error = [], exc.category
                    except Exception:
                        result, error = [], 'provider_error'
                    with self._lock:
                        if session != self._session or version != self._version or cancel.is_set():
                            continue
                        self._consume(group, result, error)
                        if cancel.is_set():
                            return
                with self._lock:
                    if self._used >= self.config.request_budget:
                        self._pause('budget_exhausted')
                        return
                self.wait(cancel, max(0, cycle_start + self.config.poll_seconds - self.clock()))
        except StorageError:
            pass
        except Exception:
            with self._lock:
                self._pause('service_error')

    def _consume(self, codes, result, error):
        candidate = deepcopy(self._data)
        quality = candidate['quality']
        quality['requests'] += 1
        checked = []
        try:
            if error is None:
                if len(result) != len(codes):
                    raise ValueError()
                checked = [validate_quote(q, symbol_for(c), self._previous.get(c)) for c, q in zip(codes, result)]
        except Exception:
            error = 'invalid_record'
        bad = error is not None or any(q.quality != 'valid' for q in checked)
        self._failures = self._failures + 1 if bad else 0
        if error:
            # 固定枚举，不信任注入 provider 的任意错误文字。
            reason = error if error in ('timeout', 'network_error', 'worker_exit', 'http_status',
                                        'redirect_refused', 'invalid_record') else 'provider_error'
            quality['failures'] += 1
            quality['reasons'][reason] = quality['reasons'].get(reason, 0) + 1
            self._previous.clear()
            self._states = {k: replace(v, baseline=None) for k, v in self._states.items()}
        else:
            reason = 'quality_error'
            for code, q in zip(codes, checked):
                quality['valid' if q.quality == 'valid' else 'invalid'] += 1
                for item in q.reasons:
                    quality['reasons'][item] = quality['reasons'].get(item, 0) + 1
                if q.source_time is not None and isinstance(q.received_at, datetime) and q.received_at.tzinfo is not None:
                    age = (q.received_at - q.source_time).total_seconds()
                    ages = quality['age_seconds']
                    ages['count'] += 1
                    ages['sum'] += age
                    ages['max'] = age if ages['max'] is None else max(ages['max'], age)
                quality['elapsed_seconds_sum'] += q.elapsed_seconds if positive(q.elapsed_seconds) else 0
                prior = self._previous.get(code)
                if (prior is not None and isinstance(q.received_at, datetime) and q.received_at.tzinfo is not None
                        and isinstance(prior.received_at, datetime) and prior.received_at.tzinfo is not None):
                    quality['max_gap_seconds'] = max(quality['max_gap_seconds'],
                        (q.received_at - prior.received_at).total_seconds())
                if q.quality != 'valid':
                    quality['alerts_suppressed'] += sum(raw['symbol'] == q.symbol for raw in candidate['rules'].values())
                self._previous[code] = q
                if q.quality == 'valid':
                    points = candidate['samples'].setdefault(code, [])
                    points.append({'price': q.price, 'source_time': q.source_time.astimezone(BEIJING).isoformat(),
                                   'received_at': q.received_at.astimezone(BEIJING).isoformat(), 'label': '采样价格'})
                    candidate['samples'][code] = points[-90:]
                for key, raw in candidate['rules'].items():
                    if raw['symbol'] != q.symbol:
                        continue
                    condition = Condition(**raw)
                    state = self._states.get(key, AlertState())
                    # 重启/改配置不清空同日上限与冷却；仅比较基线重建。
                    limit = candidate['limits'].get(key)
                    if limit and state.last_alert is None:
                        state = replace(state, day=limit['day'], count=limit['count'],
                                        last_alert=datetime.fromisoformat(limit['last_alert']))
                    state, alert = alert_step(state, condition, q)
                    self._states[key] = state
                    if alert:
                        candidate['limits'][key] = {'day': state.day, 'count': state.count,
                                                    'last_alert': state.last_alert.isoformat()}
                        candidate['alerts'].append({'code': code, 'direction': condition.direction,
                            'threshold': condition.threshold, 'previous_price': alert.previous_price,
                            'price': alert.price, 'source_time': alert.source_time.astimezone(BEIJING).isoformat(),
                            'received_at': alert.received_at.astimezone(BEIJING).isoformat(),
                            'triggered_at': self.now().astimezone(BEIJING).isoformat()})
        self._save(candidate)
        if self._failures >= 3:
            self._pause(reason)

    def status(self):
        with self._lock:
            latest = {}
            for code in self._data['watch']:
                points = self._data['samples'].get(code, [])
                if points:
                    point = deepcopy(points[-1])
                    age = (self.now() - datetime.fromisoformat(point['source_time'])).total_seconds()
                    observed = self._previous.get(code)
                    point.update(age_seconds=age, stale=age > 120 or age < -5,
                                 quality=observed.quality if observed else 'cached',
                                 reasons=list(observed.reasons) if observed else [])
                    latest[code] = point
            return {'state': self._status, 'reason': self._reason, 'session': self._session,
                    'requests_used': self._used, 'request_budget': self.config.request_budget,
                    'calendar': '不可用；仅钟表时段提示，节假日由源时间新鲜度判断' if self.calendar is None else 'configured',
                    'watch': list(self._data['watch']), 'latest': latest, 'rules': deepcopy(self._data['rules']),
                    'quality': deepcopy(self._data['quality']), 'timezone': '北京时间',
                    'alerts_paused': self._status != 'running'}

    def alerts(self):
        with self._lock:
            return deepcopy(self._data['alerts'])

    def export_quality(self):
        with self._lock:
            return {'quality': deepcopy(self._data['quality']), 'conclusion': '尚未真实验证',
                    'requests_used': self._used, 'timezone': '北京时间'}

    def trend(self, code):
        symbol_for(code)
        with self._lock:
            cutoff = self.now().astimezone(BEIJING) - timedelta(minutes=30)
            points = deepcopy([p for p in self._data['samples'].get(code, [])
                               if cutoff <= datetime.fromisoformat(p['source_time']) <= self.now()])
        changes = {}
        for minutes in (5, 15):
            eligible = [p for p in points if points and datetime.fromisoformat(p['source_time']) <=
                        datetime.fromisoformat(points[-1]['source_time']) - timedelta(minutes=minutes)]
            if not eligible:
                changes[str(minutes)] = {'status': '数据不足', 'span_seconds': None}
            else:
                first, last = eligible[-1], points[-1]
                span = (datetime.fromisoformat(last['source_time']) - datetime.fromisoformat(first['source_time'])).total_seconds()
                changes[str(minutes)] = {'status': 'ok', 'span_seconds': span,
                                        'percent': (last['price'] / first['price'] - 1) * 100}
        return {'code': code, 'label': '采样价格', 'timezone': '北京时间', 'window_minutes': 30,
                'points': points, 'changes': changes,
                'span_seconds': (datetime.fromisoformat(points[-1]['source_time']) - datetime.fromisoformat(points[0]['source_time'])).total_seconds() if points else 0}
