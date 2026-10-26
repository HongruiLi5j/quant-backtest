"""后台轮询调度：默认不启动，只由可信本机显式 start()；重启进程后一律是停止状态。

两种模式（batch_mode，默认 True）：
单只模式：同一代码至少 60 秒、相邻请求至少 10 秒、单请求最多 5 只（多代码写法未经真实验证，
默认逐只），请求复用 service.process_request（独立 spawn 进程里跑 TencentProvider）。
批量模式：整个名单一次交给 easyquotation（新浪+腾讯双源，允许部分成功）；轮间隔下限按名单大小
由 providers_easy.effective_interval 算出，预算按上游 HTTP 次数（拆批数）计。
速率约束不被突破：没装 easyquotation 时构造即退回单只模式；批量连续整批失败 3 次则降级为
单只模式（TencentProvider）继续，单只模式再连续失败 3 次才暂停。每次 start 有请求预算上限。
"""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
import importlib.util
import logging
from pathlib import Path
import threading
import time

from . import intraday
from .core import BEIJING, AlertState, Condition, alert_step, continuous_session, validate_quote
from .providers import FeedError
from .service import process_batch_request, process_request, symbol_for
from .store import STORE_ROOT, Store, StorageError, empty_state
from .providers_easy import REQUEST_GAP as BATCH_GAP, SOURCES as EASY_SOURCES, batch_size, effective_interval
from .watchlist import DEFAULT_MAX_WATCH, DEFAULT_TOP_N, build_watchlist, validate_max_watch

LOG = logging.getLogger('market_monitor')

MIN_INTERVAL, DEFAULT_INTERVAL, MAX_INTERVAL = 30, 60, 3600
SYMBOL_GAP, REQUEST_GAP = 60, 10     # 每股最小间隔 / 相邻请求最小间隔（秒）
DEFAULT_BUDGET = 600                 # 默认值保持不变(T024 只放宽上限，不改默认)
MAX_BUDGET = 1200                    # 每日请求预算上限（T024：原 600，监控全部信号股约需 480 次）
BATCH_MIN_INTERVAL = BATCH_GAP       # batch_mode=True 时的最小轮询间隔（秒），与 providers_easy.REQUEST_GAP 一致
MAX_MISSING_SHOWN = 200              # status 里最多列出的缺失代码数（完整数量见 missing_count）
MAX_BATCH = 5                     # 单请求最多股数
MAX_FAILURES = 3                     # 连续失败暂停
KEPT_ALERTS = 200
KEPT_SAMPLES = 600       # 每只股票内存里最多保留的当日报价样本数
KEPT_EVENTS = 200
TRACK_WRITE_GAP = 60     # 追踪记录写入 jsonl 的最小间隔（秒），即“每分钟更新”
WATCH_STORE_ROOT = STORE_ROOT.parent / 'market_monitor_watch'   # 与 S2 的 5 只快照分开，互不校验
KNOWN_ERRORS = ('timeout', 'network_error', 'worker_exit', 'http_status', 'redirect_refused', 'invalid_record')


def validate_settings(interval, top_n, max_watch=DEFAULT_MAX_WATCH, batch_mode=False):
    """严格校验：只接受真正的 int，拒绝 bool/浮点/越界。批量模式的最小间隔放宽到 10 秒。"""
    low = BATCH_MIN_INTERVAL if batch_mode else MIN_INTERVAL
    if (type(interval) is not int or not low <= interval <= MAX_INTERVAL
            or type(top_n) is not int or not 0 <= top_n <= max_watch):
        raise ValueError('invalid_config')
    return interval, top_n


def _easy_installed():
    """easyquotation 是否已安装（只查找，不导入）。"""
    return importlib.util.find_spec('easyquotation') is not None


def _batch_cost(count):
    """一次批量请求实际拆成的上游 HTTP 批数（与 service.process_batch_request 同口径）。"""
    return max(1, -(-count // min(batch_size(s) for s in EASY_SOURCES)))


def default_notify(title, body):
    """Mac 通知走现有 src.notify；不可用就静默（只记日志计数，不记内容）。"""
    from src import notify
    notify.send(title, body)


class MonitorScheduler:
    def __init__(self, *, store=None, request=None, build_fn=None, notify_fn=default_notify,
                 now=None, monotonic=None, wait=None, interval=DEFAULT_INTERVAL, top_n=DEFAULT_TOP_N,
                 request_budget=DEFAULT_BUDGET, multi_code=False, timeout=10,
                 intraday_dir=None, avg_volume_fn=None, max_watch=DEFAULT_MAX_WATCH, batch_mode=True,
                 easy_available=None):
        if type(batch_mode) is not bool:
            raise ValueError('invalid_config')
        validate_max_watch(max_watch)
        self.batch_requested = batch_mode
        # 默认请求函数（未注入 request）且没装 easyquotation：批量路径只能走腾讯兜底，会突破每股 60 秒约束，
        # 所以直接退回单只模式；注入了 request（测试/自定义）时尊重 batch_mode。
        if batch_mode and request is None and not (easy_available if easy_available is not None else _easy_installed)():
            batch_mode = False
        validate_settings(interval, top_n, max_watch, batch_mode)
        if type(request_budget) is not int or not 1 <= request_budget <= MAX_BUDGET or type(multi_code) is not bool:
            raise ValueError('invalid_config')
        self.store = store or Store(root=WATCH_STORE_ROOT)
        # 默认请求函数随模式：批量走 easyquotation 批量进程（未安装时兜底 Tencent），单只走原 Tencent 进程
        self._custom_request = request is not None
        self._easy_available = easy_available
        self.request = request or (process_batch_request if batch_mode else process_request)
        self._fallback_request = request or process_request   # 批量降级后逐只用的请求函数
        self.max_watch, self.batch_mode = max_watch, batch_mode
        # 默认名单构造器带上本实例的 max_watch；注入的 build_fn 保持 build_fn(top_n) 的旧签名
        self.build_fn = build_fn or (lambda n: build_watchlist(n, max_watch=self.max_watch))
        self.notify_fn = notify_fn
        self.now = now or (lambda: datetime.now(BEIJING))
        self.clock = monotonic or time.monotonic
        self.wait = wait or (lambda event, seconds: event.wait(seconds))
        # 实时分析（S3b）：追踪记录目录默认挂在存储根目录旁（默认即 data/market_monitor_intraday）；
        # 均量只在使用默认存储时才读本地日线缓存，注入了测试存储时默认不读任何真实数据。
        self.intraday_dir = Path(intraday_dir) if intraday_dir is not None else self.store.root.parent / 'market_monitor_intraday'
        self.avg_volume_fn = avg_volume_fn or (intraday.default_avg_volumes if store is None else (lambda codes: {}))
        self.interval, self.top_n = interval, top_n
        self.budget, self.multi_code, self.timeout = request_budget, multi_code, timeout
        self._lock = threading.RLock()
        self._lifecycle = threading.Lock()
        self._thread = None
        self._cancel = threading.Event()
        self._session = 0
        self._state, self._reason, self._phase = 'stopped', None, 'idle'
        self._degraded = False
        self._reset_run()

    @staticmethod
    def _new_intraday(day):
        return {'day': day, 'events': [], 'seen': set(), 'triggers': {}, 'rows': [], 'summary': intraday.summarize([]),
                'last_write': None, 'write_error': False}

    def _reset_run(self):
        self._used = self._failures = 0
        self._degraded = False
        self._last_request = None
        self._last_symbol = {}
        self._plan = {'codes': [], 'sources': {}, 'targets': {}, 'signals': {}, 'truncated': False}
        self._history, self._avg = {}, {}
        self._intraday = self._new_intraday('')
        self._previous, self._states, self._conditions = {}, {}, {}
        self._quotes, self._alerts = {}, []
        self._data = None
        self._missing = []       # 最近一次批量请求没拿到行情的代码（部分成功时非空）
        self._round = {'rounds': 0, 'ok': 0, 'failed': 0, 'alerts': 0}

    # ---- 控制 ----
    def _batch_active(self):
        return self.batch_mode and not self._degraded

    def set_config(self, interval, top_n):
        validate_settings(interval, top_n, self.max_watch, self._batch_active())
        with self._lock:
            self.interval, self.top_n = interval, top_n   # 运行中从下一轮生效

    @property
    def provider(self):
        """当前数据源名称：batch_mode 对应 easy（easyquotation 批量），否则 tencent（逐只）。"""
        return 'easy' if self.batch_mode else 'tencent'

    def set_runtime(self, max_watch=None, batch_mode=None, provider=None):
        """运行时切换 max_watch / batch_mode / provider；None 表示不改。先全部校验，通过才落地，失败 ValueError 且不改状态。

        provider 与 batch_mode 是同一件事的两种写法（tencent=逐只，easy=批量），同时给出且矛盾则拒绝。
        当前 interval / top_n 在新设置下必须仍然合法（间隔下限随模式、top_n 上界随 max_watch），否则拒绝。
        没装 easyquotation 且用默认请求函数时拒绝切到批量（不静默退回，免得前端以为生效了）。
        """
        if max_watch is not None:
            validate_max_watch(max_watch)
        if batch_mode is not None and type(batch_mode) is not bool:
            raise ValueError('invalid_config')
        if provider is not None:
            if type(provider) is not str or provider not in ('tencent', 'easy'):
                raise ValueError('invalid_config')
            if batch_mode is not None and batch_mode != (provider == 'easy'):
                raise ValueError('invalid_config')
            batch_mode = provider == 'easy'
        with self._lock:
            new_watch = self.max_watch if max_watch is None else max_watch
            new_batch = self.batch_mode if batch_mode is None else batch_mode
            validate_settings(self.interval, self.top_n, new_watch, new_batch)
            if new_batch and not self._custom_request and not (
                    self._easy_available if self._easy_available is not None else _easy_installed)():
                raise ValueError('invalid_config')
            self.max_watch = new_watch
            if new_batch != self.batch_mode:
                self.batch_mode, self.batch_requested = new_batch, new_batch
                self._degraded, self._failures = False, 0
                if not self._custom_request:
                    self.request = process_batch_request if new_batch else process_request

    def start(self):
        """幂等：已在运行返回 False；存储不可读时不启动（paused/storage_error）。"""
        with self._lifecycle:
            with self._lock:
                if self._thread is not None and self._thread.is_alive():
                    return False
                self._session += 1
                self._reset_run()
                try:
                    self._data = self.store.load()
                except StorageError:
                    self._data = empty_state()
                    self._state, self._reason, self._phase = 'paused', 'storage_error', 'idle'
                    return False
                self._cancel = threading.Event()
                self._state, self._reason, self._phase = 'running', None, 'waiting_session'
                self._thread = threading.Thread(target=self._run, args=(self._session, self._cancel),
                                                name='market-monitor-watch', daemon=True)
                self._thread.start()
                return True

    def stop(self):
        """幂等；等线程退出。线程内部的请求进程由 cancel 事件在 50ms 内终止并回收。"""
        with self._lifecycle:
            with self._lock:
                self._session += 1
                self._cancel.set()
                thread = self._thread
                self._state, self._reason, self._phase = 'stopped', None, 'idle'
                self._quotes = {}
                self._history, self._avg = {}, {}
                self._intraday = self._new_intraday('')
                self._previous.clear()
                self._states.clear()
            if thread is not None and thread is not threading.current_thread():
                thread.join()
            with self._lock:
                self._thread = None

    def _pause(self, reason):
        self._state, self._reason, self._phase = 'paused', reason, 'idle'
        self._cancel.set()

    # ---- 轮询 ----
    def _eligible(self):
        return continuous_session(self.now())

    def _live(self, session, cancel):
        return not cancel.is_set() and session == self._session

    def _run(self, session, cancel):
        try:
            while not cancel.is_set():
                cycle_start = self.clock()
                if not self._eligible():
                    with self._lock:
                        if self._live(session, cancel):
                            self._phase = 'waiting_session'
                    self.wait(cancel, self.interval)
                    continue
                with self._lock:
                    if not self._live(session, cancel):
                        return
                    self._phase = 'polling'
                    top_n = self.top_n
                try:
                    plan = self.build_fn(top_n)
                except Exception:
                    LOG.info('market_monitor: watchlist build failed')
                    plan = {'codes': [], 'sources': {}, 'targets': {}, 'signals': {}, 'truncated': False}
                with self._lock:
                    if not self._live(session, cancel):
                        return
                    self._apply_plan(plan)
                    codes = tuple(self._plan['codes'])
                if self._batch_active():
                    groups = [codes] if codes else []      # 批量模式：整个名单一次请求；空名单不发请求
                else:
                    size = MAX_BATCH if self.multi_code else 1
                    groups = [codes[i:i + size] for i in range(0, len(codes), size)]
                for group in groups:
                    if not self._poll_group(group, cycle_start, session, cancel):
                        return
                with self._lock:
                    if not self._live(session, cancel):
                        return
                    self._round['rounds'] += 1
                    self._intraday_step()
                    self._persist()
                    LOG.info('market_monitor: round done rounds=%d ok=%d failed=%d alerts=%d',
                             self._round['rounds'], self._round['ok'], self._round['failed'], self._round['alerts'])
                self.wait(cancel, max(0, cycle_start + self.interval - self.clock()))
        except Exception:
            LOG.warning('market_monitor: scheduler error')
            with self._lock:
                if session == self._session:
                    self._pause('service_error')

    def _poll_group(self, group, cycle_start, session, cancel):
        """发一次请求；返回 False 表示线程应退出。"""
        with self._lock:
            if not self._live(session, cancel):
                return False
            batch = self._batch_active()
            cost = _batch_cost(len(group)) if batch else 1
            if self._used + cost > self.budget:
                self._pause('budget_exhausted')
                return False
            if batch:      # 批量：相邻轮的起点间隔 ≥ effective_interval（按拆批数放大，批间 5 秒）
                gap, per_symbol = effective_interval(len(group)), []
            else:
                gap, per_symbol = REQUEST_GAP, [self._last_symbol[c] + SYMBOL_GAP for c in group if c in self._last_symbol]
            earliest = max([cycle_start]
                           + ([self._last_request + gap] if self._last_request is not None else [])
                           + per_symbol)
        self.wait(cancel, max(0, earliest - self.clock()))
        with self._lock:
            if not self._live(session, cancel):
                return False
            if not self._eligible():
                return True           # 等待期间收盘/午休：本轮剩余不再请求
            if any(c not in self._plan['codes'] for c in group):
                return True
            self._used += cost
            self._last_request = self.clock()
            for code in group:
                self._last_symbol[code] = self._last_request
        try:
            call = self.request if batch else self._fallback_request
            result, error = call(tuple(symbol_for(c) for c in group), cancel, self.timeout), None
        except FeedError as exc:
            result, error = [], exc.category
        except Exception:
            result, error = [], 'provider_error'
        with self._lock:
            if not self._live(session, cancel):
                return False
            if batch:
                group, result, error = self._split_batch(group, result, error)
            self._consume(group, result, error)
            return self._state == 'running'

    def _apply_plan(self, plan):
        self._plan = {'codes': list(plan['codes'])[:self.max_watch], 'sources': dict(plan.get('sources', {})),
                      'targets': dict(plan.get('targets', {})), 'signals': dict(plan.get('signals', {})),
                      'truncated': bool(plan.get('truncated'))}
        keep = set(self._plan['codes'])
        conditions = {}
        for code, target in self._plan['targets'].items():
            if code not in keep:
                continue
            symbol = symbol_for(code)
            # 买入目标价：价格下穿；卖出目标价：价格上穿。
            if 'buy' in target:
                conditions[code + ':down'] = Condition(symbol, 'down', target['buy'])
            if 'sell' in target:
                conditions[code + ':up'] = Condition(symbol, 'up', target['sell'])
        self._conditions = conditions
        self._quotes = {c: q for c, q in self._quotes.items() if c in keep}
        self._history = {c: h for c, h in self._history.items() if c in keep}
        self._states = {k: v for k, v in self._states.items() if k in conditions}

    def _split_batch(self, group, result, error):
        """批量结果允许部分成功：按 symbol 对号入座，只把拿到的交给 _consume，没拿到的记入 missing。

        result 可以是 BatchResult（有 .quotes）或普通 list[Quote]；不在请求里的/重复的 symbol 直接忽略。
        一条都没拿到按整批失败处理（invalid_record），连续失败 3 次降级为逐只模式（见 _consume）。
        """
        if error is not None:
            self._missing = list(group)
            return group, [], error
        by_symbol = {}
        for q in getattr(result, 'quotes', result) or []:
            by_symbol.setdefault(getattr(q, 'symbol', None), q)
        present = [c for c in group if symbol_for(c) in by_symbol]
        got = set(present)
        self._missing = [c for c in group if c not in got]
        if not present:
            return group, [], 'invalid_record'
        if self._missing:
            reasons = self._data['quality']['reasons']
            reasons['missing_quote'] = reasons.get('missing_quote', 0) + len(self._missing)
        return present, [by_symbol[symbol_for(c)] for c in present], None

    def _consume(self, group, result, error):
        checked = []
        try:
            if error is None:
                if len(result) != len(group):
                    raise ValueError()
                checked = [validate_quote(q, symbol_for(c), self._previous.get(c)) for c, q in zip(group, result)]
        except Exception:
            error = 'invalid_record'
        quality = self._data['quality']
        quality['requests'] += 1
        bad = error is not None or any(q.quality != 'valid' for q in checked)
        self._failures = self._failures + 1 if bad else 0
        self._round['failed' if bad else 'ok'] += 1
        if error:
            reason = error if error in KNOWN_ERRORS else 'provider_error'
            quality['failures'] += 1
            quality['reasons'][reason] = quality['reasons'].get(reason, 0) + 1
            self._previous.clear()
            self._states = {k: replace(v, baseline=None) for k, v in self._states.items()}
        else:
            reason = 'quality_error'
            for code, q in zip(group, checked):
                quality['valid' if q.quality == 'valid' else 'invalid'] += 1
                for item in q.reasons:
                    quality['reasons'][item] = quality['reasons'].get(item, 0) + 1
                self._previous[code] = q
                self._store_quote(code, q)
                self._evaluate(code, q)
        if self._failures >= MAX_FAILURES:
            if self._batch_active():
                # 批量连续整批失败：降级为逐只（TencentProvider），守回每股 60 秒/相邻 10 秒
                LOG.warning('market_monitor: batch failed %d times, degrade to single mode', MAX_FAILURES)
                self._degraded, self._failures, self._missing = True, 0, []
            else:
                self._pause(reason)

    def _store_quote(self, code, q):
        pct = None
        if q.price and q.previous_close and q.price > 0 and q.previous_close > 0:
            pct = (q.price / q.previous_close - 1) * 100
        if q.quality == 'valid':
            self._history.setdefault(code, []).append({
                't': q.received_at, 'price': q.price, 'prev_close': q.previous_close, 'open': q.opening,
                'volume': float(q.volume_raw),
                'name': q.name if isinstance(q.name, str) else ''})
            del self._history[code][:-KEPT_SAMPLES]
        triggered = self._quotes.get(code, {}).get('alert') == 'triggered'
        self._quotes[code] = {
            'code': code, 'name': q.name if isinstance(q.name, str) else '', 'price': q.price, 'pct': pct,
            'source_time': q.source_time.astimezone(BEIJING).isoformat() if q.source_time else None,
            'received_at': q.received_at.astimezone(BEIJING).isoformat(),
            'quality': q.quality, 'reasons': list(q.reasons), 'sources': list(self._plan['sources'].get(code, []))}
        if triggered:
            self._quotes[code]['alert'] = 'triggered'

    def _evaluate(self, code, q):
        for key, condition in self._conditions.items():
            if key[:6] != code:
                continue
            state = self._states.get(key, AlertState())
            limit = self._data['limits'].get(key)
            if limit and state.last_alert is None:     # 冷却/每日次数跨重启保留
                state = replace(state, day=limit['day'], count=limit['count'],
                                last_alert=datetime.fromisoformat(limit['last_alert']))
            state, alert = alert_step(state, condition, q)
            self._states[key] = state
            if not alert:
                continue
            self._data['limits'][key] = {'day': state.day, 'count': state.count,
                                         'last_alert': state.last_alert.isoformat()}
            record = {'code': code, 'direction': condition.direction, 'threshold': condition.threshold,
                      'previous_price': alert.previous_price, 'price': alert.price,
                      'source_time': alert.source_time.astimezone(BEIJING).isoformat(),
                      'received_at': alert.received_at.astimezone(BEIJING).isoformat(),
                      'triggered_at': self.now().astimezone(BEIJING).isoformat()}
            self._data['alerts'] = (self._data['alerts'] + [record])[-KEPT_ALERTS:]
            self._alerts = (self._alerts + [record])[-KEPT_ALERTS:]
            self._round['alerts'] += 1
            self._quotes[code]['alert'] = 'triggered'
            self._persist()
            self._notify(record)

    def _notify(self, record):
        if self.notify_fn is None:
            return
        side = '跌破买入目标价' if record['direction'] == 'down' else '突破卖出目标价'
        try:
            self.notify_fn('盯盘到价提醒', f"{record['code']} {side} {record['threshold']}，现价 {record['price']}")
        except Exception:
            LOG.info('market_monitor: notify unavailable')

    # ---- 实时分析（S3b）：每轮采样完后调用；任何异常都不能影响采集 ----
    def _intraday_step(self):
        try:
            self._intraday_compute()
        except Exception:
            LOG.info('market_monitor: intraday step failed')

    def _intraday_compute(self):
        now = self.now()
        day = now.astimezone(BEIJING).date().isoformat()
        st = self._intraday
        if st['day'] != day:
            st = self._intraday = self._new_intraday(day)
        codes, sources = self._plan['codes'], self._plan['sources']
        held = [c for c in codes if 'holding' in sources.get(c, [])]
        signals = [dict(v, code=c) for c, v in self._plan['signals'].items() if c in set(codes)]
        snapshot = {c: h[-1] for c, h in self._history.items() if h}
        if not self._avg and codes:
            self._avg = self.avg_volume_fn(list(codes)) or {}
        found = (intraday.execution_hints(now, signals, held, snapshot)
                 + intraday.detect_new_signals(day, self._history, held, avg_volumes=self._avg, now=now))
        fresh = []
        for ev in found:
            key = (ev['kind'], ev['code'], tuple(ev['params'].get('triggers', ())))
            if key not in st['seen']:
                st['seen'].add(key)
                fresh.append(ev)
        st['events'] = (st['events'] + fresh)[-KEPT_EVENTS:]
        intraday.register_triggers(st['triggers'], [e for e in fresh if e['kind'] in ('watch', 'consider_sell')])
        track = intraday.track_signals(day, signals, self._history, now=now, write=False)
        trig = intraday.track_triggers(day, st['triggers'], self._history, now=now, write=False)
        st['rows'], st['summary'] = track + trig, intraday.summarize(track)
        gap_ok = st['last_write'] is None or self.clock() - st['last_write'] >= TRACK_WRITE_GAP
        if gap_ok and (track or trig):
            st['last_write'] = self.clock()
            try:
                intraday.append_jsonl(day, track + trig, self.intraday_dir)
                st['write_error'] = False
            except Exception:
                st['write_error'] = True
                LOG.info('market_monitor: intraday write failed')

    def _persist(self):
        """只落盘告警/冷却/质量计数；报价与名单不落盘。存储失败立即暂停。"""
        try:
            self.store.save(deepcopy(self._data))
        except StorageError:
            self._pause('storage_error')

    # ---- 只读视图（拷贝，调用方改不了内部状态）----
    def status(self):
        with self._lock:
            return {'state': self._state, 'reason': self._reason, 'phase': self._phase,
                    'requests_used': self._used, 'request_budget': self.budget,
                    'interval': self.interval, 'top_n': self.top_n,
                    'watch_count': len(self._plan['codes']),
                    'missing_count': len(self._missing), 'missing_codes': list(self._missing[:MAX_MISSING_SHOWN]),
                    'max_watch': self.max_watch, 'batch_mode': self._batch_active(),
                    'provider': 'easy' if self._batch_active() else 'tencent',
                    'batch_requested': self.batch_requested, 'degraded': self._degraded,
                    'truncated': self._plan['truncated'], 'rounds': self._round['rounds'],
                    'timezone': '北京时间'}

    def quotes(self):
        with self._lock:
            rows = []
            for code in self._plan['codes']:
                row = deepcopy(self._quotes.get(code)) or {
                    'code': code, 'name': '', 'price': None, 'pct': None, 'source_time': None,
                    'received_at': None, 'quality': 'pending', 'reasons': [],
                    'sources': list(self._plan['sources'].get(code, []))}
                if 'alert' not in row:
                    row['alert'] = 'armed' if any(k[:6] == code for k in self._conditions) else 'none'
                rows.append(row)
            return rows

    def intraday_events(self):
        with self._lock:
            return deepcopy(self._intraday['events'])

    def intraday_tracking(self):
        with self._lock:
            st = self._intraday
            return deepcopy({'day': st['day'], 'rows': st['rows'], 'summary': st['summary'],
                             'write_error': st['write_error']})

    def alerts(self):
        with self._lock:
            return deepcopy(self._alerts)
