"""D108：盯盘接上持仓止损线（quant 的 stops.stop_lines → watchlist plan['stops']）。
1) 后端：_apply_plan 为每条线建一个"下穿"条件；quotes 行带 stop（不含成本）；告警记录带 line；status.rules 有 stops / stop_pct；通知写明"以收盘为准"。
2) 前端纯函数 stopInfo（用 node 从 monitor.js 截出来执行）：取最高的线、算现价离它多远、不高于时标出。
不联网、不启动盯盘、不写 data/。"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.market_monitor import scheduler as sched_mod  # noqa: E402
from src.market_monitor.store import Store  # noqa: E402

STOPS = {'600000': {'cost': 10.0, 'nearest': {'kind': 'donchian_s1', 'price': 9.1},
                    'lines': [{'kind': 'fixed', 'price': 8.5, 'rule': '成本 10.00 × …'},
                              {'kind': 'donchian_s1', 'price': 9.1, 'as_of': '2026-11-04'},
                              {'kind': 'weird', 'price': 9.9}, {'kind': 'fixed', 'price': float('nan')}]}}


def _sched():
    root = Path(tempfile.gettempdir()) / 'mm_unused_never_created_d108'
    return sched_mod.MonitorScheduler(store=Store(root=root), batch_mode=False, easy_available=lambda: False)


class SchedulerStopTests(unittest.TestCase):
    def setUp(self):
        self.s = _sched()
        plan = {'codes': ['600000', '600001'], 'sources': {'600000': ['holding'], '600001': ['watchlist']},
                'targets': {}, 'signals': {}, 'stops': STOPS, 'truncated': False}
        with self.s._lock:
            self.s._apply_plan(plan)

    def test_conditions_per_line(self):
        conds = self.s._conditions
        self.assertEqual(set(conds), {'600000:stop:fixed', '600000:stop:donchian_s1'})
        self.assertTrue(all(c.direction == 'down' for c in conds.values()))
        self.assertEqual(conds['600000:stop:fixed'].threshold, 8.5)

    def test_quotes_carry_stop_without_cost(self):
        rows = {r['code']: r for r in self.s.quotes()}
        self.assertEqual([x['price'] for x in rows['600000']['stop']], [9.1, 8.5])     # 高的在前；坏线丢掉
        self.assertNotIn('cost', json.dumps(rows['600000']['stop']))
        self.assertNotIn('rule', json.dumps(rows['600000']['stop']))
        self.assertEqual(rows['600000']['alert'], 'armed')
        self.assertNotIn('stop', rows['600001'])

    def test_rules(self):
        r = self.s.status()['rules']
        self.assertEqual(r['stops'], 1)
        self.assertAlmostEqual(r['stop_pct'], 0.15)

    def test_notify_wording(self):
        got = []
        self.s.notify_fn = lambda title, body: got.append((title, body))
        self.s._notify({'code': '600000', 'direction': 'down', 'threshold': 9.1, 'price': 9.05, 'line': 'donchian_s1'})
        self.s._notify({'code': '600000', 'direction': 'down', 'threshold': 8.0, 'price': 7.9})
        self.assertEqual(got[0][0], '盯盘止损提醒')
        self.assertIn('跌破海龟出场线', got[0][1])
        self.assertIn('以收盘价为准', got[0][1])
        self.assertEqual(got[1][0], '盯盘到价提醒')

    def test_empty_plan_has_no_stops(self):
        s = _sched()
        self.assertEqual(s.status()['rules']['stops'], 0)
        self.assertEqual(s.quotes(), [])


class StopCrossEndToEnd(unittest.TestCase):
    """用 test_market_monitor_scheduler 的假时钟 / 假行情：现价从线上跌到线下，记一条带 line 的告警、发一次通知。"""
    def test_cross_alerts_once(self):
        sys.path.insert(0, str(ROOT / 'tests'))
        from test_market_monitor_scheduler import Harness, until
        h = Harness(self, prices={'600000': [9.5, 9.4, 9.0, 8.9, 8.8]})
        h.plan.update(sources={'600000': ['holding']},
                      stops={'600000': {'lines': [{'kind': 'donchian_s1', 'price': 9.1}, {'kind': 'fixed', 'price': 8.5}]}})
        h.s.start()
        self.assertTrue(until(lambda: len(h.calls) >= 6))
        h.s.stop()
        alerts = h.s.alerts()
        self.assertEqual([(a['line'], a['threshold'], a['previous_price'], a['price']) for a in alerts], [('donchian_s1', 9.1, 9.4, 9.0)])
        self.assertEqual(len(h.notified), 1)
        self.assertEqual(h.notified[0][0], '盯盘止损提醒')


NODE = shutil.which('node') or '/opt/homebrew/bin/node'
JS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
const body = src.slice(src.indexOf('export function stopInfo'), src.indexOf('function stopNote'));
const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
const stopInfo = new Function('isNum', body.replace(/export /g, '') + '; return stopInfo;')(isNum);
const s = (q) => { const r = stopInfo(q); return r && { top: r.top.price, gap: r.gap === null ? null : Math.round(r.gap * 10) / 10, below: r.below }; };
console.log(JSON.stringify([
  s({ price: 10, stop: [{ kind: 'fixed', price: 8.5 }, { kind: 'donchian_s1', price: 9.1 }] }),
  s({ price: 9.0, stop: [{ kind: 'donchian_s1', price: 9.1 }] }),
  s({ price: null, stop: [{ kind: 'fixed', price: 8.5 }] }),
  s({ price: 10 }),
  s({ price: 10, stop: [{ kind: 'fixed', price: 0 }] }),
]));
"""


class StopInfoTests(unittest.TestCase):
    def test_stop_info(self):
        out = subprocess.run([NODE, '-e', JS, str(ROOT / 'dashboard/ui/pages/monitor.js')], capture_output=True, text=True, check=True)
        a, b, c, d, e = json.loads(out.stdout)
        self.assertEqual(a, {'top': 9.1, 'gap': 9.9, 'below': False})
        self.assertEqual(b, {'top': 9.1, 'gap': -1.1, 'below': True})
        self.assertEqual(c, {'top': 8.5, 'gap': None, 'below': False})
        self.assertIsNone(d)
        self.assertIsNone(e)

    def test_dictionary_keys(self):
        for lang in ('zh', 'en'):
            d = json.loads((ROOT / f'dashboard/ui/i18n/{lang}.json').read_text(encoding='utf-8'))
            for k in ('c108.kind.fixed', 'c108.kind.donchian_s1', 'c108.line', 'c108.below', 'c108.basis',
                      'c108.dir.fixed', 'c108.dir.donchian_s1', 'c108.rule', 'c108.count', 'c108.none'):
                self.assertIn(k, d, f'{lang} {k}')


if __name__ == '__main__':
    unittest.main()
