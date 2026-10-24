"""S2 离线契约测试：只用临时目录、合成清单、假 provider/时钟。"""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
import importlib
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.market_monitor import api, signals_readonly
from src.market_monitor.core import BEIJING, Quote, Condition
from src.market_monitor.providers import FeedError
from src.market_monitor.service import Config, MonitorService, process_request
from src.market_monitor.store import Store, StorageError, empty_state

AT = datetime(2026, 10, 22, 9, 30, tzinfo=BEIJING)


def quote(symbol='sh600000', at=AT, price=9):
    return Quote(symbol, '合成', price, 9, 9, '100', at.strftime('%Y%m%d%H%M%S'), None,
                 at + timedelta(seconds=1), .01, fields_raw=('禁止导出原始字段',))


class Clock:
    def __init__(self):
        self.tick = 0
    def now(self):
        return AT + timedelta(seconds=self.tick)
    def wait(self, event, seconds):
        if not event.is_set():
            self.tick += seconds
        return event.is_set()


class ChildFake:
    """真实 spawn 子进程使用的假 provider，不碰网络。"""
    def __init__(self, **kwargs):
        assert kwargs['request_budget'] == 1
        assert 0 < kwargs['timeout'] <= 10
    def start(self):
        pass
    def stop(self):
        pass
    def fetch(self, symbols):
        return [quote(s) for s in symbols]


class ChildHang(ChildFake):
    def fetch(self, symbols):
        time.sleep(60)


class ChildExit(ChildFake):
    def fetch(self, symbols):
        os._exit(2)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.clock = Clock()
        self.calls = []
        self.services = []
        self.signal_patch = patch.object(signals_readonly, 'SIGNAL_FILE', self.root / 'missing.json')
        self.signal_patch.start()
        self.addCleanup(self.signal_patch.stop)
        for target in ('socket.socket', 'socket.create_connection', 'socket.getaddrinfo', 'urllib.request.OpenerDirector.open'):
            guard = patch(target, side_effect=AssertionError('测试禁止联网'))
            guard.start()
            self.addCleanup(guard.stop)
        self.addCleanup(self.cleanup_services)

    def cleanup_services(self):
        for service in self.services:
            service.stop()

    def service(self, **kw):
        def request(symbols, event, timeout):
            self.calls.append((self.clock.tick, symbols))
            return [quote(s, self.clock.now()) for s in symbols]
        options = dict(store=Store(root=self.root / 'monitor'), monotonic=lambda: self.clock.tick,
                       now=self.clock.now, wait=self.clock.wait, request=request)
        options.update(kw)
        service = MonitorService(**options)
        self.services.append(service)
        return service

    def add(self, service, code='600000'):
        self.assertTrue(api.handle(service, 'watch_add', {'code': code}, is_local_owner=True)['ok'])

    def run_session(self, service):
        self.assertTrue(service.start())
        service._thread.join(3)
        self.assertFalse(service._thread.is_alive())
        return service.status()

    def test_default_closed_restart_and_cached_status(self):
        service = self.service()
        self.assertFalse((self.root/'monitor').exists())
        for _ in range(3):
            self.assertEqual(api.handle(service, 'status', is_local_owner=True)['data']['state'], 'stopped')
        self.assertIsNone(service._thread)
        self.assertEqual(self.calls, [])
        self.add(service)
        self.run_session(service)
        reloaded = self.service()
        self.assertEqual(reloaded.status()['watch'], ['600000'])
        self.assertEqual(reloaded.status()['state'], 'stopped')
        self.assertIsNone(reloaded._thread)
        self.assertEqual(len(self.calls), 5)

    def test_budget_interval_and_explicit_config(self):
        service = self.service(config=Config(request_budget=7, poll_seconds=65))
        self.add(service)
        status = self.run_session(service)
        self.assertEqual((status['requests_used'], status['reason']), (7, 'budget_exhausted'))
        self.assertTrue(all(b[0]-a[0] >= 65 for a,b in zip(self.calls, self.calls[1:])))
        for kw in ({'request_budget':601}, {'request_budget':True}, {'request_budget':0},
                   {'poll_seconds':59}, {'poll_seconds':float('nan')}, {'timeout_seconds':11},
                   {'multi_code_verified':1}):
            with self.assertRaises(ValueError):
                Config(**kw)
        service.stop()
        old = self.calls[-1][0]
        self.run_session(service)
        self.assertGreaterEqual(self.calls[7][0] - old, 65)

    def test_staggered_multi_code_and_verified_batch(self):
        for multi, expected in [(False, 5), (True, 15)]:
            with self.subTest(multi=multi):
                self.calls.clear()
                service = self.service(store=Store(root=self.root / str(multi)),
                                       config=Config(multi_code_verified=multi))
                for code in ('600000','000001','300001'):
                    self.add(service, code)
                self.run_session(service)
                self.assertEqual(sum(len(v) for _,v in self.calls), expected)
                self.assertTrue(all(b[0]-a[0] >= 10 for a,b in zip(self.calls,self.calls[1:])))
                for symbol in ('sh600000','sz000001','sz300001'):
                    times = [t for t, group in self.calls if symbol in group]
                    self.assertTrue(all(b-a >= 60 for a,b in zip(times,times[1:])))
                self.assertTrue(all(len(v) == (3 if multi else 1) for _,v in self.calls))

    def test_three_failures_pause_and_sanitization(self):
        def failed(*args):
            self.calls.append(True)
            raise FeedError('PRIVATE upstream text')
        service = self.service(request=failed, config=Config(request_budget=20))
        self.add(service)
        state = self.run_session(service)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(state['reason'], 'provider_error')
        self.assertNotIn('PRIVATE', json.dumps(service.export_quality()))

    def test_quality_failure_and_gap_no_alert(self):
        counter = [0]
        def stale(symbols, event, timeout):
            counter[0] += 1
            return [replace(quote(s, self.clock.now() - timedelta(seconds=121), 11), received_at=self.clock.now()) for s in symbols]
        service = self.service(request=stale)
        self.add(service)
        service.configure('rule_set','600000',Condition('sh600000','up',10,0))
        state = self.run_session(service)
        self.assertEqual((counter[0], state['reason']), (3, 'quality_error'))
        self.assertEqual(service.alerts(), [])
        self.assertEqual(state['quality']['reasons']['stale'], 3)

    def test_stop_join_discard_old_session_and_config_result(self):
        entered, release = threading.Event(), threading.Event()
        def delayed(symbols, cancel, timeout):
            entered.set()
            release.wait(2)
            return [quote(symbols[0], self.clock.now(), 11)]
        service = self.service(request=delayed)
        self.add(service)
        service.start()
        self.assertTrue(entered.wait(1))
        self.assertFalse(service.start())
        self.add(service, '000001')
        stopper = threading.Thread(target=service.stop)
        stopper.start()
        # 等待 stop 作废会话，再让旧请求返回。
        deadline = time.monotonic() + 1
        while service.status()['state'] != 'stopped' and time.monotonic() < deadline:
            time.sleep(.001)
        release.set()
        stopper.join(2)
        self.assertFalse(stopper.is_alive())
        self.assertIsNone(service._thread)
        self.assertEqual(service.export_quality()['quality']['requests'], 0)
        self.assertEqual(service.trend('600000')['points'], [])
        service.request = lambda symbols, cancel, timeout: [quote(s, self.clock.now()) for s in symbols]
        self.run_session(service)
        self.assertGreater(service.export_quality()['quality']['requests'],0)

    def test_config_change_discard_inflight_without_stop(self):
        entered, release = threading.Event(), threading.Event()
        def delayed(symbols, cancel, timeout):
            entered.set()
            release.wait(2)
            return [quote(s,self.clock.now()) for s in symbols]
        service = self.service(request=delayed, config=Config(request_budget=1))
        self.add(service)
        service.start()
        self.assertTrue(entered.wait(1))
        service.configure('watch_remove', '600000')
        release.set()
        service._thread.join(2)
        self.assertEqual(service.trend('600000')['points'], [])

    def test_off_session_and_calendar_unavailable(self):
        for mode in ('lunch','weekend','holiday'):
            stop_wait = lambda event, seconds: event.set()
            options = {'wait': stop_wait}
            if mode == 'lunch':
                options['now'] = lambda: AT.replace(hour=12)
            elif mode == 'weekend':
                options['now'] = lambda: AT + timedelta(days=2)
            else:
                options['calendar'] = lambda day: False
            service = self.service(**options)
            self.add(service)
            self.run_session(service)
        self.assertEqual(self.calls, [])
        def unavailable(day):
            raise OSError()
        service = self.service(calendar=unavailable)
        self.add(service)
        self.run_session(service)
        self.assertEqual(len(self.calls), 5)

    def test_storage_permissions_symlinks_exclusive_and_reload(self):
        store = Store(root=self.root/'private'/'monitor')
        store.save(empty_state())
        file = next(store.root.glob('snapshot_*.json'))
        self.assertEqual(stat.S_IMODE(file.stat().st_mode),0o600)
        for path in (store.root, store.root.parent):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode),0o700)
        with patch('src.market_monitor.store.uuid.uuid4') as unique:
            unique.return_value.hex = 'collision'
            store._revision = 9
            store.save(empty_state())
            store._revision = 9
            with self.assertRaises(StorageError):
                store.save(empty_state())
        link = self.root/'link'
        link.symlink_to(store.root, target_is_directory=True)
        for target in (link, link/'child'):
            with self.assertRaises(StorageError):
                Store(root=target).save(empty_state())
            with self.assertRaises(StorageError):
                Store(root=target).load()
        store.root.chmod(0o755)
        with self.assertRaises(StorageError):
            store.load()

    def test_storage_symlink_file_and_corrupt_fail_closed(self):
        for case in ('link','corrupt','mode','hardlink'):
            root = self.root/case
            store = Store(root=root)
            store.save(empty_state())
            original = next(root.glob('*.json'))
            nextfile = root/'snapshot_999999999999_bad.json'
            if case == 'link':
                nextfile.symlink_to(original)
            elif case == 'corrupt':
                nextfile.write_text('{')
                nextfile.chmod(0o600)
            elif case == 'mode':
                original.chmod(0o644)
            else:
                os.link(original, nextfile)
            service = self.service(store=store)
            self.assertEqual(service.status()['reason'],'storage_error')
            with self.assertRaises(StorageError):
                service.start()

    def test_storage_failure_pauses_before_exposing_alert(self):
        service = self.service()
        self.add(service)
        service.configure('rule_set','600000',Condition('sh600000','up',10,0))
        with service._lock:
            service._consume(('600000',),[quote(price=9)],None)
            with patch.object(service.store,'save',side_effect=StorageError('disk_full')):
                with self.assertRaises(StorageError):
                    service._consume(('600000',),[quote(at=AT+timedelta(seconds=60),price=11)],None)
        self.assertEqual(service.alerts(), [])
        self.assertEqual(service.status()['reason'], 'storage_error')
        self.assertTrue(service.status()['alerts_paused'])
        service.stop()
        with self.assertRaises(StorageError):
            service.start()

    def test_ring_trend_window_changes_and_insufficient(self):
        service = self.service()
        self.add(service)
        for i in range(100):
            self.clock.tick = i*60
            with service._lock:
                service._consume(('600000',),[quote(at=self.clock.now(),price=10+i)],None)
        self.assertEqual(len(service._data['samples']['600000']),90)
        trend = service.trend('600000')
        self.assertEqual(trend['label'],'采样价格')
        self.assertEqual(len(trend['points']),31)
        self.assertEqual(trend['span_seconds'],1800)
        self.assertEqual(trend['changes']['5']['span_seconds'],300)
        self.assertAlmostEqual(trend['changes']['15']['percent'],(109/94-1)*100)
        self.assertEqual(service.trend('000001')['changes']['5']['status'],'数据不足')
        self.clock.tick += 3600
        self.assertEqual(service.trend('600000')['points'],[])

    def test_crossing_dedup_cooldown_daily_limit_survives_restart(self):
        service = self.service()
        self.add(service)
        condition = Condition('sh600000','up',10,180,2)
        service.configure('rule_set','600000',condition)
        for i,price in enumerate((9,11,9,11,9,11,9,11)):
            self.clock.tick = i*60
            service._consume(('600000',),[quote(at=self.clock.now(),price=price)],None)
        self.assertEqual(len(service.alerts()),2)
        for field in ('code','direction','threshold','previous_price','price','source_time','received_at','triggered_at'):
            self.assertIn(field,service.alerts()[0])
        reloaded = self.service()
        for i,price in enumerate((9,11)):
            self.clock.tick = 600+i*60
            reloaded._consume(('600000',),[quote(at=self.clock.now(),price=price)],None)
        self.assertEqual(len(reloaded.alerts()),2)
        self.assertNotIn('禁止导出',json.dumps(service.export_quality(),ensure_ascii=False))
        self.assertNotIn('fields_raw',json.dumps(service._data))

    def test_api_authorization_validation_and_operations(self):
        service = self.service()
        for action in api.SCHEMA:
            for context in (False, None, 1, 'true'):
                self.assertEqual(api.handle(service,action,{},is_local_owner=context)['error'],'OWNER_REQUIRED')
        bad = [('watch_add',{'code':'../foo'}), ('watch_add',{'code':'60000０'}),
               ('watch_add',{'code':'sh600000'}),('watch_add',{'code':600000}),
               ('watch_add',{'code':'900001'}),('start',{'request_budget':600}),
               ('status',{'path':'private'}),('rule_set',{'code':'600000','direction':'x','threshold':1}),
               ('rule_set',{'code':'600000','direction':'up','threshold':float('nan')}),
               ('rule_set',{'code':'600000','direction':'up','threshold':True}),
               ('rule_set',{'code':'600000','direction':'up','threshold':0}),
               ('rule_set',{'code':'600000','direction':'up','threshold':1,'cooldown_seconds':False}),
               ('rule_set',{'code':'600000','direction':'up','threshold':1,'daily_limit':True}),
               ('status',b'{"x":1,"x":2}'),('status',b'NaN'),('status',b'[]'),('status',b'\xff')]
        for action,body in bad:
            with self.subTest(action=action,body=body):
                response = api.handle(service,action,body,is_local_owner=True)
                self.assertEqual(response,{'ok':False,'error':'INVALID_INPUT'})
        self.assertEqual(api.handle(service,'status',' '*8193,is_local_owner=True)['error'],'BODY_TOO_LARGE')
        self.assertEqual(api.handle(service,'unknown',is_local_owner=True)['error'],'UNKNOWN_ACTION')
        for code in ('600000','000001','300001','688001','601001'):
            self.add(service,code)
        self.assertFalse(api.handle(service,'watch_add',{'code':'002001'},is_local_owner=True)['ok'])
        self.assertTrue(api.handle(service,'rule_set',{'code':'600000','direction':'up','threshold':10},is_local_owner=True)['ok'])
        self.assertTrue(api.handle(service,'rule_delete',{'code':'600000','direction':'up'},is_local_owner=True)['ok'])
        self.assertEqual(service.status()['rules'],{})
        self.assertTrue(api.handle(service,'watch_remove',{'code':'600000'},is_local_owner=True)['ok'])
        self.assertTrue(api.handle(service,'trend',{'code':'600000'},is_local_owner=True)['ok'])
        for action in ('alerts','export_quality','stop','status'):
            self.assertTrue(api.handle(service,action,is_local_owner=True)['ok'])
        self.assertEqual(self.calls,[])

    def test_readonly_signals_structure_missing_no_writes(self):
        path = self.root/'signals.json'
        self.assertEqual(signals_readonly.read_signals(['600000'])['status'],'无信号')
        payload = {'version':4,'market':'cn','data_date':'2026-10-21',
                   'generated_at':'2026-10-21T15:40:00+08:00','strategies':{
                       'blend2':{'rows':[{'code':'600000','action':'买入','last_date':'2026-10-21'}],
                                 'exceptions':[{'code':'000001'}]}}}
        with patch.object(signals_readonly,'SIGNAL_FILE',path):
            path.write_text(json.dumps(payload))
            before = path.read_bytes(), path.stat().st_mtime_ns
            original_open = os.open
            def readonly(file, flags, *args, **kw):
                self.assertEqual(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC), 0)
                return original_open(file, flags, *args, **kw)
            with patch('os.open',side_effect=readonly):
                result = signals_readonly.read_signals(['600000'],now=AT)
                self.assertEqual(result['signals'][0]['direction'],'买入')
                self.assertEqual(result['signals'][0]['signal_date'],'2026-10-21')
                self.assertEqual(signals_readonly.read_signals(['000001'],now=AT)['status'],'无信号')
                self.assertEqual(signals_readonly.read_signals([],now=AT)['signals'],[])
            self.assertEqual(before,(path.read_bytes(),path.stat().st_mtime_ns))
            for field,value in [('version',3),('generated_at','2026-10-21T14:00:00+08:00'),
                                ('data_date','tomorrow'),('strategies',[])]:
                bad = deepcopy(payload)
                bad[field] = value
                path.write_text(json.dumps(bad))
                self.assertEqual(signals_readonly.read_signals(['600000'],now=AT)['status'],'无信号')
            path.write_text('{')
            self.assertEqual(signals_readonly.read_signals(['600000'])['status'],'无信号')

    def test_import_no_threads_network_files_or_app(self):
        code = '''
from unittest.mock import patch
with patch('threading.Thread.start',side_effect=AssertionError('thread')), patch('socket.socket',side_effect=AssertionError('network')), patch('pathlib.Path.read_text',side_effect=AssertionError('read')), patch('os.open',side_effect=AssertionError('storage')):
 import src.market_monitor
 import src.market_monitor.api
 import src.market_monitor.service
 import src.market_monitor.store
 import src.market_monitor.signals_readonly
 import sys
 assert 'app' not in sys.modules
print('import safe')
'''
        result = subprocess.run([sys.executable,'-B','-c',code],cwd=ROOT,text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(result.stdout.strip(),'import safe')

    def test_cache_ages_no_query_requests_and_quality_summary(self):
        service = self.service(config=Config(request_budget=1))
        self.add(service)
        self.assertTrue(api.handle(service,'start',is_local_owner=True)['ok'])
        service._thread.join(2)
        calls = len(self.calls)
        self.clock.tick += 121
        status = api.handle(service,'status',is_local_owner=True)['data']
        self.assertTrue(status['latest']['600000']['stale'])
        self.assertEqual(len(self.calls),calls)
        summary = service.export_quality()['quality']
        self.assertEqual(summary['age_seconds']['count'],1)
        self.assertEqual(summary['age_seconds']['max'],1)
        self.assertEqual(summary['elapsed_seconds_sum'],.01)

    def test_failure_resets_baseline_and_next_day_resets_limit(self):
        service = self.service()
        self.add(service)
        service.configure('rule_set','600000',Condition('sh600000','up',10,0,1))
        for seconds, price, error in ((0,9,None),(60,11,'network_error'),
                                      (120,11,None),(180,9,None),(240,11,None),
                                      (300,9,None),(360,11,None),(86400,9,None),(86460,11,None)):
            self.clock.tick = seconds
            service._consume(('600000',),[quote(at=self.clock.now(),price=price)] if error is None else [],error)
        self.assertEqual(len(service.alerts()),2)
        self.assertEqual(service.alerts()[0]['source_time'],(AT+timedelta(seconds=240)).isoformat())
        self.assertEqual(service.alerts()[1]['source_time'],(AT+timedelta(seconds=86460)).isoformat())

    def test_trend_irregular_actual_span_and_api_storage_error(self):
        service = self.service()
        self.add(service)
        for seconds in (0,200,400,601):
            self.clock.tick = seconds
            service._consume(('600000',),[quote(at=self.clock.now())],None)
        trend = api.handle(service,'trend',{'code':'600000'},is_local_owner=True)['data']
        self.assertEqual(trend['changes']['5']['span_seconds'],401)
        self.assertEqual(trend['changes']['15']['status'],'数据不足')
        with patch.object(service.store,'save',side_effect=StorageError()):
            result = api.handle(service,'watch_remove',{'code':'600000'},is_local_owner=True)
        self.assertEqual(result,{'ok':False,'error':'STORAGE_ERROR'})
        self.assertTrue(service.status()['alerts_paused'])

    def test_real_process_fake_provider_success_timeout_cancel_and_exit(self):
        from src.market_monitor import service as module
        pids = []
        from multiprocessing.process import BaseProcess
        original_start = BaseProcess.start
        def tracked_start(process):
            original_start(process)
            pids.append(process.pid)
        with patch.object(BaseProcess, 'start', tracked_start):
            self.assertEqual(process_request(('sh600000',),threading.Event(),1,provider_factory=ChildFake)[0].price,9)
            for factory,expected in ((ChildHang,'timeout'),(ChildExit,'worker_exit')):
                with self.assertRaises(FeedError) as caught:
                    process_request(('sh600000',),threading.Event(),.05,provider_factory=factory)
                self.assertEqual(caught.exception.category,expected)
            cancel = threading.Event()
            timer = threading.Timer(.15, cancel.set)
            timer.start()
            try:
                with self.assertRaises(FeedError) as caught:
                    process_request(('sh600000',),cancel,1,provider_factory=ChildHang)
                self.assertEqual(caught.exception.category,'cancelled')
            finally:
                timer.join()
            # 服务 stop 必须等到请求进程回收，丢弃未完成结果。
            started = threading.Event()
            def run_child(symbols,event,timeout):
                started.set()
                return process_request(symbols,event,timeout,provider_factory=ChildHang)
            service = self.service(request=run_child)
            self.add(service)
            service.start()
            self.assertTrue(started.wait(1))
            service.stop()
            self.assertIsNone(service._thread)
            self.assertEqual(service.export_quality()['quality']['requests'],0)
        for pid in pids:
            with self.assertRaises(ProcessLookupError):
                os.kill(pid,0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
