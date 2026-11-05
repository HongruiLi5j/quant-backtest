"""D15：盯盘「只看异动」视图和参数可行性估算。
1) 后端 status()['estimate']：逐只模式一轮 ≥ 只数 × REQUEST_GAP、每轮请求数 = 只数；批量模式按 _batch_cost / effective_interval；
   剩余预算够几轮 = (预算 − 已用) // 每轮请求数。只读，不启动采集、不联网（不存在的临时目录，从不写盘）。
2) 前端纯函数 isMover / viewRows（用 node 从 monitor.js 截出来执行）：默认只看异动、搜索查全部、每页 100、排序。"""
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
from src.market_monitor.providers_easy import effective_interval  # noqa: E402


class EstimateTests(unittest.TestCase):
    def make(self, batch):
        root = Path(tempfile.gettempdir()) / 'mm_unused_never_created_d15'
        return sched_mod.MonitorScheduler(store=Store(root=root), batch_mode=batch, easy_available=lambda: batch)

    def test_per_stock_mode(self):
        s = self.make(False)
        e = s.status()['estimate']
        n = s.max_watch
        self.assertEqual(e['basis'], 'max_watch')
        self.assertEqual(e['requests_per_round'], n)
        self.assertEqual(e['round_seconds'], max(s.interval, n * sched_mod.REQUEST_GAP))
        self.assertEqual(e['rounds_affordable'], s.budget // n)

    def test_batch_mode(self):
        s = self.make(True)
        e = s.status()['estimate']
        n = s.max_watch
        self.assertEqual(e['requests_per_round'], sched_mod._batch_cost(n))
        self.assertEqual(e['round_seconds'], max(s.interval, effective_interval(n)))


class RulesTests(unittest.TestCase):
    """D68：status()['rules'] 就是盘中分析实际用的阈值，名单为空时目标价数为 0"""
    def test_rules_are_the_real_thresholds(self):
        from src.market_monitor import intraday
        root = Path(tempfile.gettempdir()) / 'mm_unused_never_created_d68'
        s = sched_mod.MonitorScheduler(store=Store(root=root), batch_mode=False, easy_available=lambda: False)
        r = s.status()['rules']
        self.assertEqual(r['thresholds'], intraday.DEFAULT_THRESHOLDS)
        self.assertEqual(r['targets'], 0)


NODE = shutil.which('node') or '/opt/homebrew/bin/node'
JS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
const body = src.slice(src.indexOf('const PAGE = 100;'), src.indexOf('/** 建一次控件和表头'));
const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
const f = new Function('isNum', body.replace(/export /g, '') + '; return { isMover, viewRows };');
const { isMover, viewRows } = f(isNum);
const qs = [];
for (let i = 0; i < 250; i++) qs.push({ code: String(600000 + i), name: 'N' + i, pct: (i % 7) - 3, sources: i === 5 ? ['holding'] : ['signal'], alert: 'none', quality: 'valid' });
qs.push({ code: '000001', name: 'Bad', pct: 0, sources: ['signal'], alert: 'none', quality: 'stale' });
const out = {
  movers: qs.filter(isMover).length,
  def: viewRows(qs, { all: false, q: '', sort: 'abs', page: 0 }),
  all2: viewRows(qs, { all: true, q: '', sort: 'code', page: 1 }),
  search: viewRows(qs, { all: false, q: '600010', sort: 'abs', page: 0 }),
  clamp: viewRows(qs, { all: true, q: '', sort: 'abs', page: 99 }),
};
out.def = { total: out.def.total, n: out.def.rows.length, first: Math.abs(out.def.rows[0].pct) };
out.all2 = { total: out.all2.total, n: out.all2.rows.length, first: out.all2.rows[0].code, pages: out.all2.pages };
out.search = { total: out.search.total, code: out.search.rows[0].code };
out.clamp = { page: out.clamp.page };
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(Path(NODE).exists(), 'node 不可用')
class ViewTests(unittest.TestCase):
    def test_view(self):
        r = subprocess.run([NODE, '-e', JS, str(ROOT / 'dashboard/ui/pages/monitor.js')], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        o = json.loads(r.stdout)
        # pct 取 -3..3：|pct|=3 的 2/7 + 持仓 1 只（pct=2，不重复计）+ 数据过旧 1 只
        expected = sum(1 for i in range(250) if abs((i % 7) - 3) >= 3 or i == 5) + 1
        self.assertEqual(o['movers'], expected)
        self.assertEqual(o['def']['total'], expected)
        self.assertEqual(o['def']['first'], 3)
        self.assertEqual(o['all2'], {'total': 251, 'n': 100, 'first': '600099', 'pages': 3})
        self.assertEqual(o['search'], {'total': 1, 'code': '600010'})   # 搜索查全部名单（600010 不是异动）
        self.assertEqual(o['clamp']['page'], 2)


if __name__ == '__main__':
    unittest.main()
