"""T016 S3：handle_config 新增 provider / max_watch / batch_mode 的校验测试。
只用假调度器，不联网、不读写 data/、不启动采集。"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.market_monitor import wiring
from src.market_monitor.scheduler import MonitorScheduler
from src.market_monitor.store import Store

GOOD = wiring.Ctx(True, True, True, True)


class FakeScheduler:
    """支持运行参数的假调度器：记录调用，status 反映当前值。"""
    def __init__(self, with_runtime=True, batch_mode=False):
        self.calls, self.interval, self.top_n = [], 60, 20
        self.max_watch, self.batch_mode = 500, batch_mode
        if with_runtime:
            self.set_runtime = self._set_runtime

    def status(self):
        return {'interval': self.interval, 'top_n': self.top_n,
                'max_watch': self.max_watch, 'batch_mode': self.batch_mode}

    def _set_runtime(self, max_watch=None, batch_mode=None, provider=None):
        self.calls.append(('runtime', max_watch, batch_mode))
        if max_watch is not None:
            self.max_watch = max_watch
        if batch_mode is not None:
            self.batch_mode = batch_mode

    def set_config(self, interval, top_n):
        self.calls.append(('config', interval, top_n))
        self.interval, self.top_n = interval, top_n


def post(sched, data, ctx=GOOD):
    body = data if isinstance(data, (bytes, str)) else json.dumps(data)
    return wiring.handle_config(ctx, sched, body)


class ConfigProviderTests(unittest.TestCase):
    def test_provider_values(self):
        s = FakeScheduler()
        code, obj = post(s, {'provider': 'easy'})
        self.assertEqual(code, 200)
        self.assertEqual((obj['provider'], obj['batch_mode']), ('easy', True))
        code, obj = post(s, {'provider': 'tencent'})
        self.assertEqual((code, obj['provider'], obj['batch_mode']), (200, 'tencent', False))

    def test_provider_invalid(self):
        for bad in ('sina', '', 'EASY', None, 1, True, ['easy']):
            s = FakeScheduler()
            self.assertEqual(post(s, {'provider': bad}), wiring.INVALID, bad)
            self.assertEqual(s.calls, [])

    def test_max_watch_range(self):
        for ok in (1, 60, 500, 5500):
            s = FakeScheduler()
            s.top_n = 0
            code, obj = post(s, {'max_watch': ok})
            self.assertEqual(code, 200, ok)
            self.assertEqual(obj['max_watch'], ok)
        for bad in (0, -1, 5501, 10 ** 9, 1.0, 500.5, '500', None, True, False):
            s = FakeScheduler()
            self.assertEqual(post(s, {'max_watch': bad}), wiring.INVALID, bad)
            self.assertEqual(s.calls, [])

    def test_batch_mode_must_be_bool(self):
        for bad in (1, 0, 'true', 'yes', None, [], 1.0):
            s = FakeScheduler()
            self.assertEqual(post(s, {'batch_mode': bad}), wiring.INVALID, bad)
            self.assertEqual(s.calls, [])
        s = FakeScheduler()
        code, obj = post(s, {'batch_mode': True})
        self.assertEqual((code, obj['batch_mode'], obj['provider']), (200, True, 'easy'))

    def test_provider_and_batch_mode_conflict_rejected(self):
        s = FakeScheduler()
        self.assertEqual(post(s, {'provider': 'tencent', 'batch_mode': True}), wiring.INVALID)
        self.assertEqual(post(s, {'provider': 'easy', 'batch_mode': False}), wiring.INVALID)
        self.assertEqual(s.calls, [])
        code, obj = post(s, {'provider': 'easy', 'batch_mode': True})
        self.assertEqual((code, obj['provider']), (200, 'easy'))

    def test_unknown_keys_rejected(self):
        for data in ({'budget': 1}, {'interval': 60, 'rate': 1}, {'Provider': 'easy'}, {'max_watch': 100, 'x': 1}):
            s = FakeScheduler()
            self.assertEqual(post(s, data), wiring.INVALID, data)
            self.assertEqual(s.calls, [])

    def test_duplicate_keys_and_bad_json_rejected(self):
        s = FakeScheduler()
        for body in ('{"max_watch": 100, "max_watch": 200}', '{"max_watch": NaN}', '[]', '{}', 'x', b'\xff'):
            self.assertEqual(post(s, body), wiring.INVALID, body)
        self.assertEqual(post(s, ' ' * 3000 + '{"max_watch": 100}'), wiring.INVALID)
        self.assertEqual(s.calls, [])

    def test_top_n_bound_follows_new_max_watch(self):
        s = FakeScheduler()
        s.top_n = 300
        self.assertEqual(post(s, {'max_watch': 100}), wiring.INVALID)   # 现有 top_n=300 超过新上限
        self.assertEqual(s.calls, [])
        code, obj = post(s, {'max_watch': 100, 'top_n': 100})
        self.assertEqual((code, obj['max_watch'], s.top_n), (200, 100, 100))
        self.assertEqual(post(s, {'max_watch': 100, 'top_n': 101}), wiring.INVALID)
        code, _ = post(s, {'max_watch': 5500, 'top_n': 5500})
        self.assertEqual(code, 200)

    def test_interval_floor_follows_new_batch_mode(self):
        s = FakeScheduler(batch_mode=False)
        self.assertEqual(post(s, {'interval': 10})[0], 400)          # 单只模式下限 30 秒
        self.assertEqual(post(s, {'batch_mode': True, 'interval': 10})[0], 200)
        self.assertEqual(post(s, {'provider': 'tencent', 'interval': 10}), wiring.INVALID)

    def test_failed_validation_is_atomic(self):
        s = FakeScheduler()
        self.assertEqual(post(s, {'provider': 'easy', 'max_watch': 100, 'top_n': 200}), wiring.INVALID)
        self.assertEqual((s.max_watch, s.batch_mode, s.calls), (500, False, []))

    def test_scheduler_without_set_runtime_rejects_runtime_keys(self):
        s = FakeScheduler(with_runtime=False)
        for data in ({'provider': 'easy'}, {'max_watch': 100}, {'batch_mode': True}):
            self.assertEqual(post(s, data), wiring.INVALID)
        self.assertEqual(s.calls, [])

    def test_response_has_new_fields(self):
        s = FakeScheduler()
        code, obj = post(s, {'provider': 'easy', 'max_watch': 800, 'top_n': 300})
        self.assertEqual(code, 200)
        self.assertEqual((obj['provider'], obj['max_watch'], obj['batch_mode']), ('easy', 800, True))
        for key in ('provider', 'max_watch', 'batch_mode', 'interval', 'top_n'):
            self.assertIn(key, obj['status'])
        self.assertEqual(s.calls, [('runtime', 800, True), ('config', 60, 300)])

    def test_only_requested_fields_reach_set_runtime(self):
        """Fix2：请求没带 batch_mode/provider 时不能把 status 里的实际值回写。"""
        s = FakeScheduler(batch_mode=False)
        post(s, {'max_watch': 800})
        self.assertEqual(s.calls[0], ('runtime', 800, None))
        s2 = FakeScheduler()
        post(s2, {'batch_mode': True})
        self.assertEqual(s2.calls[0], ('runtime', None, True))

    def test_response_fields_on_legacy_request(self):
        s = FakeScheduler(batch_mode=True)
        code, obj = post(s, {'interval': 90})
        self.assertEqual(code, 200)
        self.assertEqual((obj['provider'], obj['max_watch'], obj['batch_mode']), ('easy', 500, True))
        self.assertEqual(obj['status']['interval'], 90)

    def test_non_owner_rejected(self):
        for ctx in (wiring.Ctx(), wiring.Ctx(True, True, True, False), wiring.Ctx(True, False, True, True),
                    wiring.Ctx(True, True, False, True), wiring.Ctx(False, True, True, True),
                    wiring.Ctx(1, 'yes', 1, 1)):
            s = FakeScheduler()
            for data in ({'provider': 'easy'}, {'max_watch': 100}, {'batch_mode': True}, {'interval': 60}):
                self.assertEqual(post(s, data, ctx), wiring.FORBIDDEN)
            self.assertEqual(s.calls, [])

    def test_legacy_interval_and_top_n_unchanged(self):
        s = FakeScheduler()
        self.assertEqual(post(s, {'interval': 120, 'top_n': 50})[0], 200)
        self.assertEqual(s.calls, [('config', 120, 50)])   # 不带运行参数时不调用 set_runtime
        for bad in ({'interval': 29}, {'interval': True}, {'interval': 60.0}, {'top_n': -1}, {'top_n': 501},
                    {'top_n': '5'}, {'interval': 10 ** 6}):
            self.assertEqual(post(s, bad), wiring.INVALID, bad)
        s2 = FakeScheduler(with_runtime=False)
        self.assertEqual(post(s2, {'interval': 90})[0], 200)   # 没有 set_runtime 的旧调度器照常可用


class RealSchedulerWiringTests(unittest.TestCase):
    """Fix1：用真实 MonitorScheduler（不启动线程、不联网、存储指向不创建的临时路径）。"""

    def make(self, batch_mode=False, easy=True):
        root = Path(tempfile.gettempdir()) / 'mm_unused_never_created'
        return MonitorScheduler(store=Store(root=root), batch_mode=batch_mode, easy_available=lambda: easy)

    def test_switch_provider_and_max_watch(self):
        s = self.make()
        code, obj = post(s, {'provider': 'easy', 'max_watch': 1000})
        self.assertEqual((code, obj['provider'], obj['max_watch'], obj['batch_mode']), (200, 'easy', 1000, True))
        self.assertEqual((s.provider, s.max_watch, s.batch_mode), ('easy', 1000, True))
        self.assertEqual(obj['status']['provider'], 'easy')
        code, obj = post(s, {'provider': 'tencent'})
        self.assertEqual((code, obj['provider'], s.max_watch), (200, 'tencent', 1000))
        self.assertEqual(wiring.handle_status(GOOD, s)[1]['status']['provider'], 'tencent')

    def test_invalid_leaves_real_scheduler_unchanged(self):
        s = self.make()
        for data in ({'max_watch': 5501}, {'provider': 'sina'}, {'batch_mode': 1}, {'max_watch': 100, 'top_n': 200}):
            self.assertEqual(post(s, data), wiring.INVALID, data)
        self.assertEqual((s.provider, s.max_watch, s.batch_mode), ('tencent', 500, False))

    def test_easy_unavailable_rejected(self):
        s = self.make(easy=False)
        self.assertEqual(post(s, {'provider': 'easy'}), wiring.INVALID)
        self.assertEqual(s.provider, 'tencent')

    def test_interval_top_n_still_work(self):
        s = self.make()
        code, obj = post(s, {'interval': 90, 'top_n': 30})
        self.assertEqual((code, obj['status']['interval'], obj['status']['top_n']), (200, 90, 30))
        self.assertEqual(post(s, {'interval': 5}), wiring.INVALID)

    def test_degraded_does_not_turn_off_batch_mode(self):
        """降级时 status()['batch_mode'] 是 False；只改 max_watch 不能把用户的批量模式关掉。"""
        s = self.make(batch_mode=True)
        s._degraded = True
        self.assertFalse(s.status()['batch_mode'])
        code, _ = post(s, {'max_watch': 800})
        self.assertEqual(code, 200)
        self.assertTrue(s.batch_mode)
        self.assertEqual(s.max_watch, 800)

    def test_status_has_provider(self):
        self.assertEqual(self.make(batch_mode=True).status()['provider'], 'easy')
        self.assertEqual(self.make(batch_mode=False).status()['provider'], 'tencent')

    def test_easy_available_false_not_treated_as_missing(self):
        s = self.make(batch_mode=True, easy=False)    # 显式传 False 必须生效（退回单只），不能回退去探测安装
        self.assertEqual((s.batch_mode, s.batch_requested), (False, True))


APP = (ROOT / 'dashboard/app.js').read_text()
I18N = (ROOT / 'dashboard/i18n.js').read_text()
MON_START = APP.index('function monitorBuild()')
MON_BLOCK = APP[MON_START:APP.index('const PAGES =')]
MON_TR = I18N[I18N.index('const monitorTranslations'):I18N.index('window.MONITOR_I18N')]
PAIR = r'"(mon\.[\w.]+)": \["((?:[^"\\]|\\.)+)", "((?:[^"\\]|\\.)+)"\]'


class ProviderFrontendStaticTests(unittest.TestCase):
    """T016 S4：盯盘页配置区静态检查（不启动浏览器、不请求服务）。"""

    def test_controls_exist(self):
        for needle in ('id = "monProvider"', 'id = "monBatch"', '"monMaxWatch"', '"mon.cfg.max_watch"',
                       '["tencent", "mon.cfg.provider.tencent"]', '["easy", "mon.cfg.provider.easy"]',
                       'batch.type = "checkbox"'):
            self.assertIn(needle, MON_BLOCK, needle)
        # max_watch 输入默认 500，范围 1–5500；top_n 上界不再写死 60
        self.assertIn('field("monMaxWatch", "mon.cfg.max_watch", "500", 1, 5500)', MON_BLOCK)
        self.assertNotIn('"mon.cfg.top_n", "20", 0, 60)', MON_BLOCK)

    def test_provider_and_batch_linked(self):
        self.assertIn('batch.checked = prov.value === "easy"', MON_BLOCK)
        self.assertIn('prov.value = batch.checked ? "easy" : "tencent"', MON_BLOCK)

    def test_only_changed_fields_submitted(self):
        save = MON_BLOCK[MON_BLOCK.index('async function monitorSaveConfig'):MON_BLOCK.index('async function loadMonitor')]
        self.assertIn('if (form.provider !== monitorRequestedProvider(s))', APP)
        self.assertNotIn('provider !== s.provider', save)
        self.assertIn('if (form.maxWatch !== s.max_watch) body.max_watch = form.maxWatch;', APP)
        self.assertIn('hRequest("/api/market_monitor/config", body)', save)
        # 提交的键都在后端白名单里
        for key in ('provider', 'batch_mode', 'max_watch', 'interval', 'top_n'):
            self.assertIn(key, wiring.CONFIG_KEYS)

    def test_text_nodes_only(self):
        for bad in ('innerHTML', 'outerHTML', 'insertAdjacentHTML', 'document.write', 'DOMParser', 'eval(', 'new Function',
                    'localStorage', 'sessionStorage'):
            self.assertNotIn(bad, MON_BLOCK, bad)
        self.assertIn('textContent', APP)

    def test_i18n_complete_for_mon_keys(self):
        keys = {k: (zh, en) for k, zh, en in re.findall(PAIR, MON_TR)}
        for k in ('mon.cfg.provider', 'mon.cfg.provider.tencent', 'mon.cfg.provider.easy', 'mon.cfg.max_watch',
                  'mon.cfg.batch_mode', 'mon.cfg.hint_provider', 'mon.status.degraded', 'mon.status.easy_missing', 'mon.cfg.err_easy', 'mon.cfg.interval', 'mon.cfg.top_n', 'mon.err.INVALID_INPUT'):
            self.assertIn(k, keys, k)
        for k, (zh, en) in keys.items():
            self.assertTrue(re.search(r'[一-鿿]', zh), k)
            self.assertFalse(re.search(r'[一-鿿]', en), k)
            self.assertEqual(sorted(re.findall(r'\{\w+\}', zh)), sorted(re.findall(r'\{\w+\}', en)), k)
        used = set(re.findall(r'"(mon\.(?:cfg|status)\.[\w.]+)"', MON_BLOCK))
        self.assertFalse(sorted(used - set(keys)), '代码里用到但 i18n 里没有的键')

    def test_howto_doc_covers_required_topics(self):
        doc = (ROOT / 'docs/providers_upgrade_howto.md').read_text()
        for word in ('tencent', 'easy', 'max_watch', 'batch_mode', '10 只', '被封 IP', '许可', '回滚', 'S1', 'S3',
                     '重启', '老用户', '先把采样间隔', '重新开始'):
            self.assertIn(word, doc, word)


NODE = shutil.which('node')


def js_plan(status, form):
    """用 node 执行 app.js 里真实的 monitorConfigPlan（不加载页面、不联网），返回要依次提交的请求体。"""
    start = APP.index('const MON_SINGLE_MIN_INTERVAL')
    end = APP.index('async function monitorSaveConfig')
    src = APP[start:end] + '\nprocess.stdout.write(JSON.stringify(monitorConfigPlan(%s, %s)));' % (
        json.dumps(status), json.dumps(form))
    out = subprocess.run([NODE, '-e', src], capture_output=True, text=True, timeout=20, check=True)
    return json.loads(out.stdout)


@unittest.skipUnless(NODE, '没有 node，跳过前端逻辑测试')
class RollbackBehaviorTests(unittest.TestCase):
    """前端提交逻辑 + 真实调度器：回滚到 tencent 的两种难走情形。"""

    def make(self, batch_mode=True):
        root = Path(tempfile.gettempdir()) / 'mm_unused_never_created'
        return MonitorScheduler(store=Store(root=root), batch_mode=batch_mode, easy_available=lambda: True)

    def submit(self, s, form):
        """模拟页面保存：用 status 算计划，逐个交给 handle_config；返回每次的状态码。"""
        return [post(s, body)[0] for body in js_plan(s.status(), form)]

    def test_degraded_then_choose_tencent_sticks_after_restart(self):
        s = self.make(batch_mode=True)
        s._degraded = True
        st = s.status()
        self.assertEqual((st['provider'], st['batch_requested'], st['degraded']), ('tencent', True, True))
        form = {'interval': 60, 'topN': 20, 'provider': 'tencent', 'maxWatch': 500}
        self.assertEqual(js_plan(st, form)[-1].get('provider'), 'tencent')   # 降级后选 tencent 必须真的提交
        self.assertEqual(self.submit(s, form), [200])
        s._reset_run()                                                       # 等价于下次点“开始”
        self.assertEqual((s.status()['provider'], s.status()['batch_requested']), ('tencent', False))

    def test_degraded_keeping_easy_does_not_submit_provider(self):
        s = self.make(batch_mode=True)
        s._degraded = True
        form = {'interval': 60, 'topN': 20, 'provider': 'easy', 'maxWatch': 800}
        self.assertEqual(js_plan(s.status(), form), [{'interval': 60, 'top_n': 20, 'max_watch': 800}])
        self.assertEqual(self.submit(s, form), [200])
        self.assertTrue(s.batch_mode)                                        # 用户的批量设置没被悄悄关掉

    def test_rollback_with_interval_raised_from_batch_floor(self):
        s = self.make(batch_mode=True)
        self.assertEqual(post(s, {'interval': 10})[0], 200)                  # 批量模式允许 10 秒
        direct = self.make(batch_mode=True)
        post(direct, {'interval': 10})
        # 一个请求同时切回 tencent 并改间隔，会被后端拒绝（先用旧间隔 10 按逐只下限校验）
        self.assertEqual(post(direct, {'interval': 60, 'provider': 'tencent', 'batch_mode': False})[0], 400)
        # 前端拆成两步：先保存间隔，再切数据源
        form = {'interval': 60, 'topN': 20, 'provider': 'tencent', 'maxWatch': 500}
        plan = js_plan(s.status(), form)
        self.assertEqual(plan[0], {'interval': 60})
        self.assertEqual(len(plan), 2)
        self.assertEqual(self.submit(s, form), [200, 200])
        self.assertEqual((s.status()['provider'], s.status()['interval']), ('tencent', 60))

    def test_no_extra_step_when_not_needed(self):
        s = self.make(batch_mode=True)
        post(s, {'interval': 60})
        self.assertEqual(len(js_plan(s.status(), {'interval': 60, 'topN': 20, 'provider': 'tencent', 'maxWatch': 500})), 1)
        post(s, {'interval': 10})
        self.assertEqual(len(js_plan(s.status(), {'interval': 10, 'topN': 20, 'provider': 'easy', 'maxWatch': 500})), 1)

    def test_easy_missing_choose_tencent_not_resubmitted(self):
        # 没装 easyquotation：batch_requested=True、batch_mode=False、provider=tencent；页面显示 tencent，不重复提交 provider
        root = Path(tempfile.gettempdir()) / 'mm_unused_never_created'
        s = MonitorScheduler(store=Store(root=root), batch_mode=True, easy_available=lambda: False)
        st = s.status()
        self.assertEqual((st['provider'], st['batch_requested'], st['degraded']), ('tencent', True, False))
        plan = js_plan(st, {'interval': 60, 'topN': 20, 'provider': 'tencent', 'maxWatch': 500})
        self.assertEqual(plan, [{'interval': 60, 'top_n': 20}])

    def test_frontend_interval_floors_match_backend(self):
        from src.market_monitor import scheduler as sched
        self.assertIn('const MON_BATCH_MIN_INTERVAL = %d;' % sched.BATCH_MIN_INTERVAL, APP)
        self.assertIn('const MON_SINGLE_MIN_INTERVAL = %d;' % sched.MIN_INTERVAL, APP)
        self.assertIn('"mon.cfg.interval", "60", MON_BATCH_MIN_INTERVAL, 3600)', APP)
        for low, name in ((sched.BATCH_MIN_INTERVAL, 'batch'), (sched.MIN_INTERVAL, 'single')):
            self.assertIn('%d–3600' % low, MON_TR, name)
        # 后端确实拒绝比前端下限小 1 秒的批量间隔
        s = self.make(batch_mode=True)
        self.assertEqual(post(s, {'interval': sched.BATCH_MIN_INTERVAL - 1})[0], 400)
        self.assertEqual(post(s, {'interval': sched.BATCH_MIN_INTERVAL})[0], 200)

    def test_unchanged_form_submits_no_runtime_fields(self):
        s = self.make(batch_mode=False)
        plan = js_plan(s.status(), {'interval': 60, 'topN': 20, 'provider': 'tencent', 'maxWatch': 500})
        self.assertEqual(plan, [{'interval': 60, 'top_n': 20}])


if __name__ == '__main__':
    unittest.main()
