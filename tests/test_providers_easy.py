"""T016 S1 EasyQuotationProvider 测试：全程假 easyquotation、假时钟；不联网、不读写 data/。"""
from pathlib import Path
import re
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.market_monitor import providers_easy as pe
from src.market_monitor.core import Quote
from src.market_monitor.providers import FeedError
from src.market_monitor.providers_easy import EasyQuotationProvider, create_with_fallback


def code_to_symbol(code):
    return ('sh' if code.startswith('6') else 'sz') + code


def make_symbols(n):
    """生成 n 个合法的沪市 600xxx / 601xxx 代码（每个前缀 1000 个）。"""
    return tuple('sh%d%03d' % (600 + i // 1000, i % 1000) for i in range(n))


def sina_item(code):
    return {'name': '测试' + code, 'open': '10.0', 'close': '9.9', 'now': '10.2', 'volume': '12345',
            'date': '2026-10-22', 'time': '10:00:00'}


def qq_item(code):
    return {'name': '测试' + code, 'open': 10.0, 'close': 9.9, 'now': 10.2, 'volume': 123,
            'datetime': '20261022100000'}


class FakeClient:
    def __init__(self, source, behavior):
        self.source, self.behavior, self.calls = source, behavior, []

    def stocks(self, codes):
        self.calls.append(list(codes))
        if self.behavior == 'ok':
            make = sina_item if self.source == 'sina' else qq_item
            return {c: make(c) for c in codes}
        if self.behavior == 'partial':
            make = sina_item if self.source == 'sina' else qq_item
            return {c: make(c) for c in codes[::2]}
        if self.behavior == 'empty':
            return {}
        if self.behavior == 'timeout':
            raise TimeoutError('慢')
        if self.behavior == 'sleep':
            time.sleep(0.5)
            return {}
        if self.behavior == 'bad':
            return ['not', 'dict']
        raise RuntimeError('boom 含 secret 文本')


class FakeModule:
    def __init__(self, **behaviors):
        self.behaviors = {'sina': 'ok', 'qq': 'ok'}
        self.behaviors.update(behaviors)
        self.clients = {}
        self.use_calls = []

    def use(self, source):
        self.use_calls.append(source)
        self.clients[source] = FakeClient(source, self.behaviors[source])
        return self.clients[source]


class Clock:
    def __init__(self):
        self.t = 1000.0
        self.sleeps = []

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds


def build(module=None, **kwargs):
    module = module or FakeModule()
    clock = Clock()
    provider = EasyQuotationProvider(importer=lambda: module, monotonic=clock.monotonic,
                                     sleep=clock.sleep, **kwargs)
    provider.start()
    return provider, module, clock


class ReturnTests(unittest.TestCase):
    def test_sina_batch_returns_quotes(self):
        provider, module, _ = build()
        quotes = provider.fetch(['sh600000', 'sz000001', 'sz300001'])
        self.assertEqual([q.symbol for q in quotes], ['sh600000', 'sz000001', 'sz300001'])
        self.assertTrue(all(isinstance(q, Quote) for q in quotes))
        q = quotes[0]
        self.assertEqual((q.price, q.previous_close, q.opening), (10.2, 9.9, 10.0))
        self.assertEqual((q.provider, q.volume_raw, q.source_time_raw), ('easyquotation_sina', '12345', '20261022100000'))
        self.assertEqual(module.clients['sina'].calls, [['600000', '000001', '300001']])
        self.assertEqual(provider.requests_used, 1)

    def test_qq_item_format(self):
        provider, _, _ = build(FakeModule(sina='error'))
        quotes = provider.fetch(['sh600000'])
        self.assertEqual(quotes[0].provider, 'easyquotation_qq')
        self.assertEqual(quotes[0].source_time_raw, '20261022100000')

    def test_partial_success_reports_missing(self):
        provider, _, _ = build(FakeModule(sina='partial'))
        result = provider.fetch_many(['sh600000', 'sh600001', 'sh600002'])
        self.assertEqual([q.symbol for q in result.quotes], ['sh600000', 'sh600002'])
        self.assertEqual(result.missing, ('sh600001',))
        self.assertFalse(result.degraded)

    def test_empty_input_makes_no_request(self):
        provider, module, _ = build()
        self.assertEqual(provider.fetch([]), [])
        self.assertEqual(provider.requests_used, 0)
        self.assertEqual(module.use_calls, [])

    def test_invalid_symbols_rejected(self):
        provider, _, _ = build()
        for bad in (['sh600000', 'sh600000'], ['bj430001'], ['600000'], ['sh600000;x']):
            with self.assertRaises(FeedError) as ctx:
                provider.fetch(bad)
            self.assertEqual(ctx.exception.category, 'invalid_symbols')

    def test_must_start(self):
        module = FakeModule()
        provider = EasyQuotationProvider(importer=lambda: module)
        with self.assertRaises(FeedError) as ctx:
            provider.fetch(['sh600000'])
        self.assertEqual(ctx.exception.category, 'not_started')


class LimitTests(unittest.TestCase):
    def test_constants_and_batch_size(self):
        self.assertEqual((pe.MAX_PER_REQUEST['qq'], pe.MAX_PER_REQUEST['sina']), (800, 500))
        # URL 长度：每个代码 ',' + 'sh600000' 约 7 字符，安全长度内的代码数不得超过常量。
        self.assertLessEqual(pe.URL_SAFE_PER_REQUEST['qq'] * 7, 4096 * 0.7)
        self.assertLessEqual(pe.URL_SAFE_PER_REQUEST['sina'] * 7, 2048 * 0.7)
        for source in pe.SOURCES:
            self.assertEqual(pe.batch_size(source),
                             min(pe.MAX_PER_REQUEST[source], pe.URL_SAFE_PER_REQUEST[source]))
        self.assertEqual((pe.batch_size('sina'), pe.batch_size('qq')), (200, 400))

    def test_split_into_chunks_never_exceeds_limit(self):
        provider, module, clock = build()
        symbols = make_symbols(500)
        result = provider.fetch_many(symbols)
        calls = module.clients['sina'].calls
        self.assertEqual([len(c) for c in calls], [200, 200, 100])
        self.assertEqual(len(result.quotes), 500)
        self.assertEqual(result.requests_made, 3)
        self.assertEqual(clock.sleeps, [5, 5])

    def test_chunk_size_uses_smallest_source_limit(self):
        provider, module, _ = build(sources=('qq', 'sina'))
        provider.fetch(make_symbols(450))
        self.assertTrue(all(len(c) <= 200 for c in module.clients['qq'].calls))

    def test_effective_interval_formula(self):
        self.assertEqual(pe.effective_interval(20), 10)
        self.assertEqual(pe.effective_interval(200), 10)
        self.assertEqual(pe.effective_interval(500), 15)
        self.assertEqual(pe.effective_interval(5000, 'qq'), 65)
        self.assertGreaterEqual(pe.effective_interval(1), pe.REQUEST_GAP)

    def test_budget_exceeded(self):
        provider, _, clock = build(request_budget=2)
        provider.fetch(['sh600000'])
        clock.t += 6
        provider.fetch(['sh600000'])
        clock.t += 6
        with self.assertRaises(FeedError) as ctx:
            provider.fetch(['sh600000'])
        self.assertEqual(ctx.exception.category, 'budget_exceeded')
        self.assertEqual(provider.requests_used, 2)

    def test_budget_stops_mid_call_keeps_partial(self):
        provider, _, _ = build(request_budget=1)
        result = provider.fetch_many(make_symbols(300))
        self.assertEqual(len(result.quotes), 200)
        self.assertEqual(len(result.missing), 100)

    def test_default_budget_and_validation(self):
        provider = EasyQuotationProvider(importer=lambda: FakeModule())
        self.assertEqual((provider.request_budget, provider.min_interval, provider.timeout), (20000, 5, 10))
        for kwargs in ({'min_interval': 4}, {'timeout': 11}, {'timeout': 0}, {'request_budget': 0},
                       {'request_budget': True}, {'sources': ('foo',)}, {'sources': ()}):
            with self.assertRaises(ValueError):
                EasyQuotationProvider(importer=lambda: FakeModule(), **kwargs)

    def test_minimum_interval_between_calls(self):
        provider, _, clock = build()
        provider.fetch(['sh600000'])
        clock.t += 4.9
        with self.assertRaises(FeedError) as ctx:
            provider.fetch(['sh600000'])
        self.assertEqual(ctx.exception.category, 'minimum_interval')
        self.assertEqual(provider.requests_used, 1)
        clock.t += 0.2
        self.assertEqual(len(provider.fetch(['sh600000'])), 1)

    def test_start_resets_budget(self):
        provider, _, _ = build()
        provider.fetch(['sh600000'])
        with self.assertRaises(FeedError):
            provider.start()
        provider.stop()
        provider.start()
        self.assertEqual(provider.requests_used, 0)


class FailureTests(unittest.TestCase):
    def test_timeout_error_mapped(self):
        provider, _, _ = build(FakeModule(sina='timeout', qq='timeout'))
        with self.assertRaises(FeedError) as ctx:
            provider.fetch(['sh600000'])
        self.assertEqual(ctx.exception.category, 'timeout')

    def test_hard_deadline_fires(self):
        provider, _, _ = build(FakeModule(sina='sleep', qq='sleep'), timeout=0.05)
        started = time.monotonic()
        with self.assertRaises(FeedError) as ctx:
            provider.fetch(['sh600000'])
        self.assertEqual(ctx.exception.category, 'timeout')
        self.assertLess(time.monotonic() - started, 0.4)

    def test_exception_hides_upstream_text(self):
        provider, _, _ = build(FakeModule(sina='error', qq='error'))
        with self.assertRaises(FeedError) as ctx:
            provider.fetch(['sh600000'])
        self.assertEqual(ctx.exception.category, 'network_error')
        self.assertNotIn('secret', str(ctx.exception))
        self.assertEqual(provider.requests_used, 2)

    def test_bad_format_and_empty(self):
        for behavior, category in (('bad', 'response_format_error'), ('empty', 'empty_response')):
            provider, _, _ = build(FakeModule(sina=behavior, qq=behavior))
            with self.assertRaises(FeedError) as ctx:
                provider.fetch(['sh600000'])
            self.assertEqual(ctx.exception.category, category)

    def test_client_init_error(self):
        class Broken:
            def use(self, source):
                raise ValueError('x')
        provider, _, _ = build(Broken())
        with self.assertRaises(FeedError) as ctx:
            provider.fetch(['sh600000'])
        self.assertEqual(ctx.exception.category, 'client_init_error')


class SourceSwitchTests(unittest.TestCase):
    def test_default_sina_first(self):
        provider, module, _ = build()
        provider.fetch(['sh600000'])
        self.assertEqual(module.use_calls, ['sina'])

    def test_sina_failure_falls_back_to_qq_with_gap(self):
        provider, module, clock = build(FakeModule(sina='error'))
        result = provider.fetch_many(['sh600000', 'sz000001'])
        self.assertEqual(module.use_calls, ['sina', 'qq'])
        self.assertTrue(result.degraded)
        self.assertEqual({q.provider for q in result.quotes}, {'easyquotation_qq'})
        self.assertEqual(result.requests_made, 2)
        self.assertEqual(clock.sleeps, [5])

    def test_custom_order(self):
        provider, module, _ = build(sources=('qq',))
        provider.fetch(['sh600000'])
        self.assertEqual(module.use_calls, ['qq'])


class ImportFallbackTests(unittest.TestCase):
    def missing(self):
        raise ImportError('No module named easyquotation')

    def test_import_error_raises_and_logs(self):
        with self.assertLogs('market_monitor', level='WARNING') as logs:
            with self.assertRaises(ImportError):
                EasyQuotationProvider(importer=self.missing)
        self.assertTrue(any('easyquotation' in line for line in logs.output))

    def test_create_with_fallback(self):
        sentinel = object()
        provider, reason = create_with_fallback(lambda: sentinel, importer=self.missing)
        self.assertIs(provider, sentinel)
        self.assertEqual(reason, 'easyquotation_not_installed')
        provider, reason = create_with_fallback(lambda: sentinel, importer=lambda: FakeModule())
        self.assertIsInstance(provider, EasyQuotationProvider)
        self.assertIsNone(reason)
        with self.assertRaises(ImportError):
            create_with_fallback(None, importer=self.missing)

    def test_tencent_provider_still_available(self):
        from src.market_monitor.providers import TencentProvider
        self.assertEqual(TencentProvider().min_interval, 60)


class NoCustomHeaderTests(unittest.TestCase):
    def test_source_has_no_disguise_strings(self):
        """用户代码不得出现伪装头相关字符串；检查关键词用拼接构造，使本测试文件自身也不含字面量。"""
        words = ['User' + '-Agent', 'Coo' + 'kie', 'Ref' + 'erer', 'add_' + 'header', 'best' + 'ip',
                 'head' + 'ers', 'Sess' + 'ion']
        for path in (ROOT / 'src/market_monitor/providers_easy.py', Path(__file__)):
            text = path.read_text(encoding='utf-8')
            for word in words:
                self.assertIsNone(re.search(word, text, re.I), (path.name, word))

    def test_provider_has_no_direct_network_imports(self):
        text = (ROOT / 'src/market_monitor/providers_easy.py').read_text(encoding='utf-8')
        for word in ('requests', 'urllib', 'http.client', 'socket'):
            self.assertNotIn('import ' + word, text)


if __name__ == '__main__':
    unittest.main()
