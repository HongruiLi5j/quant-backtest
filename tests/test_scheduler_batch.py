"""T016 S2：调度器批量模式 / MAX_WATCH 可配置测试。假请求、假时钟、临时目录；不联网、不读写 data/。"""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
from src.market_monitor import scheduler as sched_mod
from src.market_monitor import watchlist
from src.market_monitor.providers import FeedError
from src.market_monitor.providers_easy import BatchResult
from src.market_monitor.scheduler import MonitorScheduler
from test_market_monitor_scheduler import Harness, make_quote, until  # 复用现有测试的假时钟/假请求/假行情


def codes_of(n):
    """生成 n 个合法的沪市代码 600000.. 。"""
    return tuple('%06d' % (600000 + i) for i in range(n))


class WatchlistMaxTests(unittest.TestCase):
    def rows(self, n):
        return [{'code': c, 'action': '买入'} for c in codes_of(n)]

    def test_defaults(self):
        self.assertEqual(watchlist.DEFAULT_MAX_WATCH, 500)
        self.assertEqual(watchlist.HARD_MAX_WATCH, 5500)
        self.assertEqual(watchlist.MAX_WATCH, 500)       # 旧导入名仍在

    def test_old_call_style_still_works(self):
        plan = watchlist.build_watchlist(20, holdings_fn=lambda: ['600000'], watch_fn=lambda: [],
                                         signals_fn=lambda: self.rows(30))
        self.assertEqual(len(plan['codes']), 20)      # 持仓 600000 与信号第一只重复，去重
        self.assertFalse(plan['truncated'])

    def test_configurable_truncation_keeps_priority(self):
        plan = watchlist.build_watchlist(5, max_watch=5, holdings_fn=lambda: ['000001'],
                                         watch_fn=lambda: ['000002'], signals_fn=lambda: self.rows(10))
        self.assertTrue(plan['truncated'])
        self.assertEqual(plan['codes'][:2], ['000001', '000002'])    # 持仓 > 自选 > 信号
        self.assertEqual(len(plan['codes']), 5)
        self.assertTrue(set(plan['signals']) <= set(plan['codes']))

    def test_exceeds_default_without_truncation(self):
        plan = watchlist.build_watchlist(0, max_watch=100, holdings_fn=lambda: [], signals_fn=lambda: [],
                                         watch_fn=lambda: list(codes_of(100)))
        self.assertEqual(len(plan['codes']), 100)
        self.assertFalse(plan['truncated'])

    def test_empty_watchlist(self):
        plan = watchlist.build_watchlist(5, holdings_fn=lambda: [], watch_fn=lambda: [], signals_fn=lambda: [])
        self.assertEqual(plan['codes'], [])
        self.assertFalse(plan['truncated'])

    def test_invalid_max_watch_rejected(self):
        for bad in (0, -1, 5501, True, 1.5, None, '10'):
            with self.assertRaises(ValueError):
                watchlist.validate_max_watch(bad)
            with self.assertRaises(ValueError):
                watchlist.build_watchlist(0, max_watch=bad)
        self.assertEqual(watchlist.validate_max_watch(5500), 5500)
        self.assertEqual(watchlist.validate_max_watch(1), 1)

    def test_top_n_bounded_by_max_watch(self):
        with self.assertRaises(ValueError):
            watchlist.build_watchlist(11, max_watch=10)
        watchlist.build_watchlist(10, max_watch=10, holdings_fn=lambda: [], watch_fn=lambda: [], signals_fn=lambda: [])


class ConstructionTests(unittest.TestCase):
    def test_legacy_construction(self):
        s = MonitorScheduler(easy_available=lambda: True)
        st = s.status()
        self.assertEqual((st['max_watch'], st['batch_mode']), (500, True))     # S2 起默认批量（easyquotation 可用时）
        self.assertIs(s.request, sched_mod.process_batch_request)
        self.assertIs(MonitorScheduler(batch_mode=False).request, sched_mod.process_request)
        self.assertEqual(MonitorScheduler(batch_mode=False).status()['batch_mode'], False)
        self.assertEqual(s.interval, 60)

    def test_default_construction_falls_back_to_single_when_easy_missing(self):
        """没装 easyquotation：默认构造退回逐只模式（每股 60 秒/相邻 10 秒），不走腾讯批量兜底。"""
        s = MonitorScheduler(easy_available=lambda: False)
        st = s.status()
        self.assertEqual((st['batch_mode'], st['batch_requested']), (False, True))
        self.assertIs(s.request, sched_mod.process_request)
        with self.assertRaises(ValueError):
            s.set_config(10, 3)                       # 单只模式 30 秒下限
        injected = MonitorScheduler(request=lambda *a: [], easy_available=lambda: False)
        self.assertTrue(injected.status()['batch_mode'])      # 注入了请求函数则尊重 batch_mode

    def test_param_validation(self):
        for kwargs in ({'max_watch': 0}, {'max_watch': 5501}, {'max_watch': True}, {'batch_mode': 1},
                       {'batch_mode': None}, {'top_n': 11, 'max_watch': 10}):
            with self.assertRaises(ValueError):
                MonitorScheduler(**kwargs)
        MonitorScheduler(max_watch=5500, batch_mode=True)

    def test_interval_minimum_depends_on_mode(self):
        with self.assertRaises(ValueError):
            MonitorScheduler(interval=29, batch_mode=False)     # 单只模式仍是 30 秒下限
        with self.assertRaises(ValueError):
            MonitorScheduler(interval=9, batch_mode=True)       # 批量最小 10 秒
        s = MonitorScheduler(interval=10, batch_mode=True, request=lambda *a: [])
        s.set_config(10, 3)
        with self.assertRaises(ValueError):
            s.set_config(9, 3)
        with self.assertRaises(ValueError):
            MonitorScheduler(batch_mode=False).set_config(10, 3)

    def test_default_build_fn_uses_instance_max_watch(self):
        s = MonitorScheduler(max_watch=7, top_n=3)
        seen = {}
        orig = sched_mod.build_watchlist
        sched_mod.build_watchlist = lambda n, **kw: seen.update(n=n, **kw) or {'codes': []}
        try:
            s.build_fn(3)
        finally:
            sched_mod.build_watchlist = orig
        self.assertEqual(seen, {'n': 3, 'max_watch': 7})


class BatchModeTests(unittest.TestCase):
    def test_batch_single_request_with_whole_watchlist(self):
        codes = codes_of(40)
        h = Harness(self, codes=codes, batch_mode=True, interval=10)
        self.assertTrue(h.s.start())
        self.assertTrue(until(lambda: len(h.calls) >= 3))
        h.s.stop()
        for _, syms in h.calls:
            self.assertEqual(syms, tuple('sh' + c for c in codes))   # 每次都是整个名单
        self.assertEqual(h.s.status()['watch_count'], 40)

    def test_batch_ignores_multi_code_flag_and_per_symbol_gap(self):
        h = Harness(self, codes=codes_of(8), batch_mode=True, interval=10, multi_code=False)
        h.s.start()
        self.assertTrue(until(lambda: len(h.calls) >= 3))
        h.s.stop()
        times = [t for t, _ in h.calls]
        gaps = [b - a for a, b in zip(times, times[1:])]
        self.assertTrue(all(10 <= g < 20 for g in gaps), gaps)   # 同一只股票 10 秒就能再取，远小于 60

    def test_batch_global_throttle_min_10s(self):
        h = Harness(self, codes=codes_of(3), batch_mode=True, interval=10)
        h.s.start()
        self.assertTrue(until(lambda: len(h.calls) >= 4))
        h.s.stop()
        times = [t for t, _ in h.calls]
        self.assertTrue(all(b - a >= 10 for a, b in zip(times, times[1:])))

    def test_batch_off_keeps_one_by_one_and_60s_per_symbol(self):
        h = Harness(self, codes=codes_of(2), batch_mode=False)
        h.s.start()
        self.assertTrue(until(lambda: len(h.calls) >= 6))
        h.s.stop()
        self.assertTrue(all(len(syms) == 1 for _, syms in h.calls))
        times = [t for t, _ in h.calls]
        self.assertTrue(all(b - a >= 10 for a, b in zip(times, times[1:])))
        for sym in ('sh600000', 'sh600001'):
            ts = [t for t, syms in h.calls if sym in syms]
            self.assertTrue(all(b - a >= 60 for a, b in zip(ts, ts[1:])), ts)

    def test_default_is_batch_on_and_legacy_harness_pins_off(self):
        self.assertTrue(MonitorScheduler(easy_available=lambda: True).status()['batch_mode'])      # 构造默认 True
        h = Harness(self, codes=codes_of(3))                            # 老 Harness 显式固定 False
        h.s.start()
        self.assertTrue(until(lambda: len(h.calls) >= 3))
        h.s.stop()
        self.assertTrue(all(len(syms) == 1 for _, syms in h.calls))

    def test_top_n_500_accepted_501_rejected(self):
        s = MonitorScheduler(top_n=500, easy_available=lambda: True)
        s.set_config(60, 500)
        self.assertEqual(s.status()['top_n'], 500)
        with self.assertRaises(ValueError):
            s.set_config(60, 501)
        with self.assertRaises(ValueError):
            MonitorScheduler(top_n=501)
        self.assertEqual(sched_mod.validate_settings(60, 500), (60, 500))

    def test_empty_watchlist_makes_no_request(self):
        for batch in (True, False):
            h = Harness(self, codes=(), batch_mode=batch, interval=10 if batch else 30)
            h.s.start()
            self.assertTrue(until(lambda: h.clock.waits >= 5))
            h.s.stop()
            self.assertEqual(h.calls, [])
            self.assertEqual(h.s.status()['requests_used'], 0)

    def test_plan_truncated_to_instance_max_watch(self):
        h = Harness(self, codes=codes_of(30), batch_mode=True, interval=10, max_watch=10, top_n=5)
        h.s.start()
        self.assertTrue(until(lambda: len(h.calls) >= 1))
        h.s.stop()
        self.assertEqual(len(h.calls[0][1]), 10)
        self.assertEqual(h.s.status()['max_watch'], 10)

    def test_more_than_old_limit_in_one_batch(self):
        h = Harness(self, codes=codes_of(200), batch_mode=True, interval=10)
        h.s.start()
        self.assertTrue(until(lambda: len(h.calls) >= 1))
        h.s.stop()
        self.assertEqual(len(h.calls[0][1]), 200)          # 超过旧上限 60

    def test_budget_still_applies_in_batch(self):
        h = Harness(self, codes=codes_of(3), batch_mode=True, interval=10, request_budget=2)
        h.s.start()
        self.assertTrue(until(lambda: h.s.status()['state'] == 'paused'))
        self.assertEqual(len(h.calls), 2)
        self.assertEqual(h.s.status()['reason'], 'budget_exhausted')
        h.s.stop()

    def _custom(self, h, drop=(), raise_error=None, as_batch=True):
        """替换 Harness 的请求函数：丢掉 drop 里的代码（模拟缺失）或整批抛错。"""
        inner = h.s.request

        def request(symbols, cancel, timeout):
            if raise_error is not None:
                h.calls.append((h.clock.t, symbols))
                raise raise_error
            quotes = [q for q in inner(symbols, cancel, timeout) if q.symbol[2:] not in drop]
            missing = tuple(s for s in symbols if s[2:] in drop)
            return BatchResult(quotes, missing, 'sina', 1, 0.0) if as_batch else quotes
        h.s.request = request

    def test_batch_all_success_with_batchresult(self):
        h = Harness(self, codes=codes_of(6), batch_mode=True, interval=10)
        self._custom(h)
        h.s.start()
        self.assertTrue(until(lambda: h.s.status()['rounds'] >= 1))
        h.s.stop()
        st = h.s.status()
        self.assertEqual((st['missing_count'], st['missing_codes']), (0, []))

    def test_batch_partial_success_records_missing(self):
        codes = codes_of(6)
        h = Harness(self, codes=codes, batch_mode=True, interval=10)
        self._custom(h, drop=codes[:2])
        h.s.start()
        self.assertTrue(until(lambda: h.s.status()['rounds'] >= 2))
        st = h.s.status()
        self.assertEqual(st['state'], 'running')                  # 部分成功不算失败，不会暂停
        self.assertEqual((st['missing_count'], st['missing_codes']), (2, list(codes[:2])))
        by_code = {q['code']: q for q in h.s.quotes()}
        self.assertEqual([by_code[c]['quality'] for c in codes[:2]], ['pending', 'pending'])
        self.assertTrue(all(by_code[c]['quality'] == 'valid' for c in codes[2:]))
        h.s.stop()
        saved = h.s.store.load()
        self.assertGreaterEqual(saved['quality']['reasons'].get('missing_quote', 0), 4)

    def test_batch_partial_success_with_plain_list(self):
        codes = codes_of(4)
        h = Harness(self, codes=codes, batch_mode=True, interval=10)
        self._custom(h, drop=codes[3:], as_batch=False)           # 老式 list[Quote] 条数少于请求也按 symbol 对号入座
        h.s.start()
        self.assertTrue(until(lambda: h.s.status()['rounds'] >= 1))
        h.s.stop()
        self.assertEqual(h.s.status()['missing_codes'], list(codes[3:]))

    def test_batch_total_failure_degrades_to_single_then_pauses(self):
        """批量连续 3 次整批失败 → 降级为逐只（TencentProvider 路径）；逐只再连续失败 3 次才暂停。"""
        h = Harness(self, codes=codes_of(5), batch_mode=True, interval=10)
        self._custom(h, raise_error=FeedError('timeout'))
        h.s._fallback_request = h.s.request       # 降级后的逐只请求也失败
        h.s.start()
        self.assertTrue(until(lambda: h.s.status()['state'] == 'paused'))
        st = h.s.status()
        self.assertEqual((st['reason'], len(h.calls)), ('timeout', 6))
        self.assertEqual([len(syms) for _, syms in h.calls], [5, 5, 5, 1, 1, 1])     # 先整批 3 次，再逐只 3 次
        self.assertTrue(st['degraded'])
        h.s.stop()

    def test_degraded_single_mode_obeys_per_symbol_60s_and_10s(self):
        """降级后的速率约束必须回到每股 60 秒、相邻请求 10 秒，不能沿用批量的间隔。"""
        h = Harness(self, codes=codes_of(3), batch_mode=True, interval=10)
        h.errors = [FeedError('timeout')] * 3          # 前 3 次整批失败，之后成功
        h.s.start()
        self.assertTrue(until(lambda: len(h.calls) >= 3 + 6))
        running = h.s.status()['state']
        h.s.stop()
        self.assertEqual(running, 'running')
        self.assertTrue(h.s.status()['degraded'])
        single = [(t, syms) for t, syms in h.calls if len(syms) == 1]
        self.assertTrue(single)
        times = [t for t, _ in single]
        self.assertTrue(all(b - a >= 10 for a, b in zip(times, times[1:])), times)
        for sym in ('sh600000', 'sh600001', 'sh600002'):
            ts = [t for t, syms in h.calls if sym in syms]       # 含降级前的整批请求
            self.assertTrue(all(b - a >= 60 for a, b in zip(ts[2:], ts[3:])), ts)

    def test_batch_empty_result_is_total_failure(self):
        codes = codes_of(3)
        h = Harness(self, codes=codes, batch_mode=True, interval=10)
        self._custom(h, drop=codes)                               # 一条都没拿到
        h.s.start()
        self.assertTrue(until(lambda: h.s.status()['degraded']))
        # 降级后调度线程会立刻改成逐只请求（假时钟不等待），所以不能数全部请求：只数「整批」（多代码）请求 = 3 次失败
        batch_calls = [c for c in list(h.calls) if len(c[1]) > 1]
        self.assertEqual(len(batch_calls), 3)
        self.assertEqual(h.s.status()['state'], 'running')
        h.s.stop()

    def test_batch_recovers_after_transient_failure(self):
        h = Harness(self, codes=codes_of(3), batch_mode=True, interval=10)
        h.errors = [FeedError('timeout'), None, None]
        h.s.start()
        self.assertTrue(until(lambda: h.s.status()['rounds'] >= 3))
        self.assertEqual(h.s.status()['state'], 'running')
        h.s.stop()

    def test_batch_quotes_available_for_all_codes(self):
        h = Harness(self, codes=codes_of(5), batch_mode=True, interval=10)
        h.s.start()
        self.assertTrue(until(lambda: all(q['quality'] != 'pending' for q in h.s.quotes())
                              and len(h.s.quotes()) == 5))
        h.s.stop()


class BatchIntegrationTests(unittest.TestCase):
    """集成：调度器 _poll_group → 请求函数 → service._batch_child → BatchResult → _split_batch → 行情落库。
    请求函数在当前进程里跑 _batch_child（假 provider、假管道），其余路径全是真实代码；不联网、不开子进程。"""
    def make(self, codes, drop=(), fail=None):
        h = Harness(self, codes=codes, batch_mode=True, interval=10)
        h.provider_calls = []

        class Provider:
            def start(self):
                pass

            def stop(self):
                pass

            def fetch_many(inner, symbols):
                h.provider_calls.append(tuple(symbols))
                if fail:
                    raise fail
                quotes = [make_quote(s, 10.0, h.clock.now(), 100) for s in symbols if s[2:] not in drop]
                return BatchResult(quotes, tuple(s for s in symbols if s[2:] in drop), 'sina', 1, 0.0)

        def request(symbols, cancel, timeout):
            from src.market_monitor import providers_easy, service
            sent, orig = [], providers_easy.create_with_fallback

            class Pipe:
                send = sent.append

                def close(self):
                    pass
            providers_easy.create_with_fallback = lambda fallback_factory=None, **kw: (Provider(), None)
            try:
                service._batch_child(Pipe(), symbols, timeout, None)
            finally:
                providers_easy.create_with_fallback = orig
            kind, result = sent[0]
            if kind != 'ok':
                raise FeedError(result)      # 与 process_request 的协议一致
            return result
        h.s.request = request
        return h

    def test_fetch_batch_once_integration(self):
        codes = codes_of(8)
        h = self.make(codes, drop=codes[:1])
        h.s.start()
        self.assertTrue(until(lambda: h.s.status()['rounds'] >= 1))
        st = h.s.status()
        by_code = {q['code']: q['quality'] for q in h.s.quotes()}     # stop() 会清行情缓存，所以先读
        h.s.stop()
        self.assertEqual(h.provider_calls[0], tuple('sh' + c for c in codes))     # 一次 fetch_many 带整个名单
        self.assertEqual(st['missing_codes'], [codes[0]])
        self.assertEqual(by_code[codes[0]], 'pending')
        self.assertTrue(all(by_code[c] == 'valid' for c in codes[1:]))

    def test_integration_provider_error_degrades_after_three(self):
        h = self.make(codes_of(3), fail=FeedError('timeout'))
        h.s.start()
        self.assertTrue(until(lambda: h.s.status()['degraded']))
        self.assertEqual(len(h.provider_calls), 3)
        h.s.stop()

    def test_budget_counts_upstream_requests_not_scheduler_calls(self):
        """500 只拆成 3 批（sina/qq 取小值 200）：一次调度请求计 3 次预算。"""
        self.assertEqual([sched_mod._batch_cost(n) for n in (1, 200, 201, 500, 5500)], [1, 1, 2, 3, 28])
        h = Harness(self, codes=codes_of(500), batch_mode=True, interval=30, request_budget=7)
        h.s.start()
        self.assertTrue(until(lambda: h.s.status()['state'] == 'paused'))
        self.assertEqual((len(h.calls), h.s.status()['requests_used']), (2, 6))     # 第 3 次会超 7，先暂停
        self.assertEqual(h.s.status()['reason'], 'budget_exhausted')
        h.s.stop()

    def test_batch_interval_scales_with_watchlist_size(self):
        """名单大 → 轮间隔下限放大（拆批数 × 5 秒），保证上一轮最后一批与下一轮第一批也隔 ≥5 秒。"""
        h = Harness(self, codes=codes_of(500), batch_mode=True, interval=10)
        h.s.start()
        self.assertTrue(until(lambda: len(h.calls) >= 3))
        h.s.stop()
        times = [t for t, _ in h.calls]
        self.assertTrue(all(b - a >= 15 for a, b in zip(times, times[1:])), times)    # 3 批 × 5 秒

    def test_process_batch_request_relaxes_deadline_by_chunks(self):
        """process_batch_request 把子进程换成 _batch_child，截止时间按拆批数放宽（不真开子进程）。"""
        from src.market_monitor import service
        seen, orig = {}, service.process_request
        service.process_request = lambda symbols, cancelled, timeout, **kw: seen.update(kw, n=len(symbols)) or 'r'
        try:
            self.assertEqual(service.process_batch_request(tuple('sh%06d' % (600000 + i) for i in range(500)), None, 10), 'r')
        finally:
            service.process_request = orig
        self.assertIs(seen['_child'], service._batch_child)
        self.assertEqual(seen['_extra'], 3 * (10 + 5))


class BatchChildTests(unittest.TestCase):
    """service._batch_child 在当前进程里直接跑（假 provider、假管道），不开子进程、不联网。"""
    class Pipe:
        def __init__(self):
            self.sent, self.closed = [], False

        def send(self, item):
            self.sent.append(item)

        def close(self):
            self.closed = True

    class Fake:
        def __init__(self, quotes=None, exc=None):
            self.quotes, self.exc, self.stopped, self.fetched = quotes, exc, False, None

        def start(self):
            pass

        def stop(self):
            self.stopped = True

        def fetch_many(self, symbols):
            if self.exc:
                raise self.exc
            return BatchResult(self.quotes, (), 'sina', 1, 0.0)

        def fetch(self, symbols):
            self.fetched = tuple(symbols)
            return self.quotes

    def run_child(self, provider, reason, symbols):
        from src.market_monitor import providers_easy, service
        pipe, orig = self.Pipe(), providers_easy.create_with_fallback
        providers_easy.create_with_fallback = lambda fallback_factory=None, **kw: (provider, reason)
        try:
            service._batch_child(pipe, symbols, 10, None)
        finally:
            providers_easy.create_with_fallback = orig
        self.assertTrue(pipe.closed)
        self.assertTrue(provider.stopped)
        return pipe.sent[0]

    def test_easy_path_returns_batchresult(self):
        kind, result = self.run_child(self.Fake(quotes=['q']), None, ('sh600000',))
        self.assertEqual(kind, 'ok')
        self.assertIsInstance(result, BatchResult)

    def test_not_installed_reports_error_instead_of_tencent_batch(self):
        """没装 easyquotation：子进程不得兜底成腾讯多代码请求（会绕过每股 60 秒），只报 provider_error。"""
        from src.market_monitor import providers_easy, service
        pipe, orig = self.Pipe(), providers_easy.create_with_fallback

        def missing(fallback_factory=None, **kw):
            self.assertIsNone(fallback_factory)
            raise ImportError()
        providers_easy.create_with_fallback = missing
        try:
            service._batch_child(pipe, ('sh600000',), 10, None)
        finally:
            providers_easy.create_with_fallback = orig
        self.assertEqual(pipe.sent, [('error', 'provider_error')])
        self.assertTrue(pipe.closed)

    def test_errors_are_sanitized(self):
        kind, cat = self.run_child(self.Fake(exc=FeedError('timeout')), None, ('sh600000',))
        self.assertEqual((kind, cat), ('error', 'timeout'))
        kind, cat = self.run_child(self.Fake(exc=RuntimeError('http://secret')), None, ('sh600000',))
        self.assertEqual((kind, cat), ('error', 'provider_error'))


if __name__ == '__main__':
    unittest.main()
