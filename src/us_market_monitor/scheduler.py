"""美股后台轮询调度：默认不启动，只由可信本机显式 start()；重启进程后一律是停止状态。镜像 src/market_monitor/scheduler.py。

速率/预算（方案 A，见 docs/us_market_monitor_plan.md 6.3）：同一代码至少 300 秒、相邻请求至少 10 秒、
一次只请求一只（yfinance 没有可靠的批量接口）、每次 start 有请求预算上限 2500。30 只名单每 300 秒轮一圈，
全时段约 2340 次请求，低于预算。请求本身复用 service.process_us_request（独立 spawn 子进程 + 硬截止），
本模块不改 providers/core/store/service 的任何行为。

稳定性（"出错不崩溃、能自愈、能说清楚"，不是"永不出错"）：
- 单次失败只计数；连续 3 次失败暂停并说明原因，需所有者手动重新开始，不无限重试。
- 失败后下一次请求至少再等 10×2^失败数 秒；限流（rate_limited）冷却 600 秒后才会再请求（冷却期间状态为 cooling_down）。
- 没安装 yfinance 这类无法自愈的错误立即暂停。
- 非美东常规时段不发请求；休市日靠"行情时间过旧"连续 3 次质量异常而暂停。
"""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
import logging
from pathlib import Path
import threading
import time

from ..market_monitor.providers import FeedError
from . import intraday
from .core import US_EASTERN, AlertState, Condition, alert_step, us_continuous_session, validate_quote
from .service import REASONS, process_us_request, symbol_for
from .store import STORE_ROOT, Store, StorageError, empty_state
from .watchlist import DEFAULT_MAX_WATCH, DEFAULT_TOP_N, build_us_watchlist, validate_max_watch

LOG = logging.getLogger('us_market_monitor')

MIN_INTERVAL, DEFAULT_INTERVAL, MAX_INTERVAL = 60, 300, 3600
SYMBOL_GAP, REQUEST_GAP = 300, 10     # 每股最小间隔 / 相邻请求最小间隔（秒）
RATE_LIMIT_COOLDOWN = 600             # 限流后的冷却时间（秒；设计值，未经 Yahoo 实测）
MAX_BUDGET = 2500                     # 美股独立预算上限（不改 A 股的 600）
MAX_FAILURES = 3                      # 连续失败暂停
MAX_BACKOFF_FACTOR = 16
IDLE_POLL = 60                        # 非时段时检查是否开盘的间隔上限（秒）
KEPT_ALERTS = 200
KEPT_SAMPLES = 600
KEPT_EVENTS = 200
TRACK_WRITE_GAP = 60
WATCH_STORE_ROOT = STORE_ROOT.parent / 'us_market_monitor_watch'   # 与 service 的 5 只快照分开，互不校验
EMPTY_PLAN = {'codes': [], 'sources': {}, 'targets': {}, 'signals': {}, 'truncated': False}


def validate_settings(interval, top_n, max_watch=DEFAULT_MAX_WATCH):
    """严格校验：只接受真正的 int，拒绝 bool/浮点/越界。"""
    if (type(interval) is not int or not MIN_INTERVAL <= interval <= MAX_INTERVAL
            or type(top_n) is not int or not 0 <= top_n <= max_watch):
        raise ValueError('invalid_config')
    return interval, top_n


def default_notify(title, body):
    """Mac 通知走现有 src.notify；不可用就静默（只记日志，不记内容）。"""
    from src import notify
    notify.send(title, body)


class USMonitorScheduler:
    def __init__(self, *, store=None, request=None, build_fn=None, notify_fn=default_notify,
                 now=None, monotonic=None, wait=None, interval=DEFAULT_INTERVAL, top_n=DEFAULT_TOP_N,
                 request_budget=MAX_BUDGET, timeout=10, intraday_dir=None, avg_volume_fn=None,
                 max_watch=DEFAULT_MAX_WATCH, thresholds=None):
        validate_max_watch(max_watch)
        validate_settings(interval, top_n, max_watch)
        if (type(request_budget) is not int or not 1 <= request_budget <= MAX_BUDGET
                or isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 10):
            raise ValueError('invalid_config')
        intraday._thresholds(thresholds)       # 阈值写错在构造时就报错，不等到盘中
        self.store = store or Store(root=WATCH_STORE_ROOT)
        self.request = request or process_us_request
        self.max_watch = max_watch
        self.build_fn = build_fn or (lambda n: build_us_watchlist(n, max_watch=self.max_watch))
        self.notify_fn = notify_fn
        self.now = now or (lambda: datetime.now(US_EASTERN))
        self.clock = monotonic or time.monotonic
        self.wait = wait or (lambda event, seconds: event.wait(seconds))
        # 追踪记录目录默认挂在存储根目录旁；均量只在使用默认存储时才读本地日线缓存，
        # 注入了测试存储时默认不读任何真实数据。
        self.intraday_dir = Path(intraday_dir) if intraday_dir is not None else self.store.root.parent / 'us_market_monitor_intraday'
        self.avg_volume_fn = avg_volume_fn or (intraday.default_avg_volumes if store is None else (lambda codes: {}))
        self.thresholds = dict(thresholds or {})
        self.interval, self.top_n = interval, top_n
        self.budget, self.timeout = request_budget, timeout
        self._lock = threading.RLock()
        self._lifecycle = threading.Lock()
        self._thread = None
        self._cancel = threading.Event()
        self._session = 0
        self._state, self._reason, self._phase = 'stopped', None, 'idle'
        # 请求时间戳跨 start/stop 保留：停止后马上再开始，也绕不过每只 300 秒的间隔
        self._last_request = None
        self._last_symbol = {}
        self._reset_run()

    @staticmethod
    def _new_intraday(day):
        return {'day': day, 'events': [], 'seen': set(), 'triggers': {}, 'rows': [], 'summary': intraday.summarize([]),
                'last_write': None, 'write_error': False}

    def _reset_run(self):
        self._used = self._failures = 0
        self._not_before = 0.0
        self._plan = dict(EMPTY_PLAN, codes=[], sources={}, targets={}, signals={})
        self._history, self._avg = {}, {}
        self._intraday = self._new_intraday('')
        self._previous, self._states, self._conditions = {}, {}, {}
        self._quotes, self._alerts = {}, []
        self._data = None
        self._round = {'rounds': 0, 'ok': 0, 'failed': 0, 'alerts': 0}

    # ---- 控制 ----
    def set_config(self, interval, top_n):
        validate_settings(interval, top_n, self.max_watch)
        with self._lock:
            self.interval, self.top_n = interval, top_n   # 运行中从下一轮生效

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
                                                name='us-market-monitor-watch', daemon=True)
                self._thread.start()
                return True

    def stop(self):
        """幂等；等线程退出。线程内部的请求子进程由 cancel 事件在 50ms 内终止并回收。"""
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
        return us_continuous_session(self.now())

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
                    # 不能等整个 interval：interval 最长 1 小时，会错过开盘
                    self.wait(cancel, min(self.interval, IDLE_POLL))
                    continue
                with self._lock:
                    if not self._live(session, cancel):
                        return
                    self._phase = 'polling'
                    top_n = self.top_n
                try:
                    plan = self.build_fn(top_n)
                except Exception:
                    LOG.info('us_market_monitor: watchlist build failed')
                    plan = EMPTY_PLAN
                with self._lock:
                    if not self._live(session, cancel):
                        return
                    self._apply_plan(plan)
                    codes = tuple(self._plan['codes'])
                for code in codes:
                    if not self._poll_one(code, cycle_start, session, cancel):
                        return
                with self._lock:
                    if not self._live(session, cancel):
                        return
                    self._round['rounds'] += 1
                    self._intraday_step()
                    self._persist()
                    LOG.info('us_market_monitor: round done rounds=%d ok=%d failed=%d alerts=%d',
                             self._round['rounds'], self._round['ok'], self._round['failed'], self._round['alerts'])
                    # 失败后指数拉长下一轮间隔（不加压）；一次成功即恢复
                    factor = min(2 ** self._failures, MAX_BACKOFF_FACTOR)
                self.wait(cancel, max(0, cycle_start + self.interval * factor - self.clock()))
        except Exception:
            LOG.warning('us_market_monitor: scheduler error')
            with self._lock:
                if session == self._session:
                    self._pause('service_error')

    def _poll_one(self, code, cycle_start, session, cancel):
        """发一次请求；返回 False 表示线程应退出。"""
        with self._lock:
            if not self._live(session, cancel):
                return False
            if self._used >= self.budget:
                self._pause('budget_exhausted')
                return False
            earliest = max([cycle_start, self._not_before]
                           + ([self._last_request + REQUEST_GAP] if self._last_request is not None else [])
                           + ([self._last_symbol[code] + SYMBOL_GAP] if code in self._last_symbol else []))
            cooling = self._not_before > self.clock()
            if cooling:
                self._phase = 'cooling_down'
        self.wait(cancel, max(0, earliest - self.clock()))
        with self._lock:
            if not self._live(session, cancel):
                return False
            self._phase = 'polling'
            if not self._eligible():
                return True           # 等待期间收盘：本轮剩余不再请求（下一圈会回到等待开盘）
            if code not in self._plan['codes']:
                return True
            self._used += 1
            self._last_request = self.clock()
            self._last_symbol[code] = self._last_request
        try:
            result, error = self.request((symbol_for(code),), cancel, self.timeout), None
        except FeedError as exc:
            result, error = [], exc.category
        except ImportError:
            result, error = [], 'yfinance_not_installed'
        except Exception:
            result, error = [], 'provider_error'
        with self._lock:
            if not self._live(session, cancel):
                return False
            self._consume(code, result, error)
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
            symbol_for(code)
            # 买入目标价：价格下穿；卖出目标价：价格上穿。
            if 'buy' in target:
                conditions[code + ':down'] = (code, Condition(code, 'down', target['buy']))
            if 'sell' in target:
                conditions[code + ':up'] = (code, Condition(code, 'up', target['sell']))
        self._conditions = conditions
        self._quotes = {c: q for c, q in self._quotes.items() if c in keep}
        self._history = {c: h for c, h in self._history.items() if c in keep}
        self._states = {k: v for k, v in self._states.items() if k in conditions}

    def _consume(self, code, result, error):
        checked = None
        try:
            if error is None:
                matches = [q for q in result if getattr(q, 'symbol', None) == symbol_for(code)]
                if len(matches) != 1:
                    raise ValueError()
                checked = validate_quote(matches[0], symbol_for(code), self._previous.get(code))
        except Exception:
            error = 'invalid_record'
        quality = self._data['quality']
        quality['requests'] += 1
        bad = error is not None or checked.quality != 'valid'
        self._failures = self._failures + 1 if bad else 0
        self._round['failed' if bad else 'ok'] += 1
        if error:
            # 固定枚举，不信任注入 provider 的任意错误文字
            reason = error if error in REASONS else 'provider_error'
            quality['failures'] += 1
            quality['reasons'][reason] = quality['reasons'].get(reason, 0) + 1
            self._previous.clear()
            self._states = {k: replace(v, baseline=None) for k, v in self._states.items()}
            self._backoff(reason)
        else:
            reason = 'quality_error'
            quality['valid' if checked.quality == 'valid' else 'invalid'] += 1
            for item in checked.reasons:
                quality['reasons'][item] = quality['reasons'].get(item, 0) + 1
            self._previous[code] = checked
            self._store_quote(code, checked)
            self._evaluate(code, checked)
            if bad:
                self._backoff(reason)
        # 达到连续失败上限，或没装 yfinance 这类无法自愈的错误，立即暂停
        if self._failures >= MAX_FAILURES or error == 'yfinance_not_installed':
            self._pause(reason)

    def _backoff(self, reason):
        """失败后：下一次请求至少再等 10×2^失败数 秒；限流则冷却 600 秒。只往后推，不往前拉。"""
        wait = RATE_LIMIT_COOLDOWN if reason == 'rate_limited' else REQUEST_GAP * 2 ** self._failures
        self._not_before = max(self._not_before, self.clock() + wait)

    def _store_quote(self, code, q):
        pct = None
        if q.price and q.previous_close and q.price > 0 and q.previous_close > 0:
            pct = (q.price / q.previous_close - 1) * 100
        if q.quality == 'valid':
            self._history.setdefault(code, []).append({
                't': q.received_at, 'price': q.price, 'prev_close': q.previous_close, 'open': q.opening,
                'high': q.high, 'low': q.low, 'volume': float(q.volume), 'name': ''})
            del self._history[code][:-KEPT_SAMPLES]
        triggered = self._quotes.get(code, {}).get('alert') == 'triggered'
        self._quotes[code] = {
            'code': code, 'name': '', 'price': q.price, 'pct': pct,
            'source_time': q.timestamp.astimezone(US_EASTERN).isoformat() if q.timestamp else None,
            'received_at': q.received_at.astimezone(US_EASTERN).isoformat(),
            'quality': q.quality, 'reasons': list(q.reasons), 'sources': list(self._plan['sources'].get(code, []))}
        if triggered:
            self._quotes[code]['alert'] = 'triggered'

    def _evaluate(self, code, q):
        for key, (owner, condition) in self._conditions.items():
            if owner != code:
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
                      'source_time': alert.source_time.astimezone(US_EASTERN).isoformat(),
                      'received_at': alert.received_at.astimezone(US_EASTERN).isoformat(),
                      'triggered_at': self.now().astimezone(US_EASTERN).isoformat()}
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
            self.notify_fn('美股盯盘到价提醒', f"{record['code']} {side} {record['threshold']}，现价 {record['price']}")
        except Exception:
            LOG.info('us_market_monitor: notify unavailable')

    # ---- 实时分析：每轮采样完后调用；任何异常都不能影响采集 ----
    def _intraday_step(self):
        try:
            self._intraday_compute()
        except Exception:
            LOG.info('us_market_monitor: intraday step failed')

    def _intraday_compute(self):
        now = self.now()
        day = now.astimezone(US_EASTERN).date().isoformat()       # 当日一律按美东日期
        st = self._intraday
        if st['day'] != day:
            st = self._intraday = self._new_intraday(day)
        codes, sources = self._plan['codes'], self._plan['sources']
        held = [c for c in codes if 'holding' in sources.get(c, [])]
        signals = [dict(v, code=c) for c, v in self._plan['signals'].items() if c in set(codes)]
        snapshot = {c: h[-1] for c, h in self._history.items() if h}
        if not self._avg and codes:
            self._avg = self.avg_volume_fn(list(codes)) or {}
        found = (intraday.execution_hints(now, signals, held, snapshot, thresholds=self.thresholds)
                 + intraday.detect_new_signals(day, self._history, held, avg_volumes=self._avg, now=now,
                                               thresholds=self.thresholds))
        fresh = []
        for ev in found:
            key = (ev['kind'], ev['code'], tuple(ev['params'].get('triggers', ())), bool(ev.get('display_only')))
            if key not in st['seen']:
                st['seen'].add(key)
                fresh.append(ev)
        st['events'] = (st['events'] + fresh)[-KEPT_EVENTS:]
        # 流动性不足的只展示，不登记追踪
        intraday.register_triggers(st['triggers'], [e for e in fresh if e['kind'] in ('watch', 'consider_sell')
                                                    and not e.get('display_only')])
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
                LOG.info('us_market_monitor: intraday write failed')

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
                    'missing_count': 0, 'missing_codes': [],
                    'max_watch': self.max_watch, 'batch_mode': False,
                    'truncated': self._plan['truncated'], 'rounds': self._round['rounds'],
                    'timezone': '美东时间（America/New_York，自动夏令时）'}

    def quotes(self):
        with self._lock:
            rows = []
            for code in self._plan['codes']:
                row = deepcopy(self._quotes.get(code)) or {
                    'code': code, 'name': '', 'price': None, 'pct': None, 'source_time': None,
                    'received_at': None, 'quality': 'pending', 'reasons': [],
                    'sources': list(self._plan['sources'].get(code, []))}
                if 'alert' not in row:
                    row['alert'] = 'armed' if any(owner == code for owner, _ in self._conditions.values()) else 'none'
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
