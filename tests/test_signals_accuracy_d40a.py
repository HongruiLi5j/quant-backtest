"""D40 方案 A（含 revquant C-D40 约束）：信号页「信号准不准」的一句话结论。
- 满 12 个完整交易周之前一律中性（即使有行的区间不含 0）；周数取自数据。
- 满了以后只用「买入 5 日」「卖出 5 日」两行的后端 verdict，不看别的行。
用 node 从 accuracy-headline.js 截出 headline、从 signals-extra.js 截出 excludesZero 执行，t() 用替身直接返回键名 + 参数。"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which('node') or '/opt/homebrew/bin/node'
JS = r"""
const fs = require('fs');
const h = fs.readFileSync(process.argv[1].replace('signals-extra.js', 'accuracy-headline.js'), 'utf8');
const src = fs.readFileSync(process.argv[1], 'utf8');
const body = h.slice(h.indexOf('export function headline(')).replace('export ', '') + '\n' + src.slice(src.indexOf('const excludesZero'), src.indexOf('/** 信号页的「信号准不准」'));
const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
const t = (k, v) => k + (v ? JSON.stringify(v) : '');
const { headline, excludesZero } = new Function('isNum', 't', body + '; return { headline, excludesZero };')(isNum, t);
const row = (action, h, verdict, lo, hi, weeks) => ({ action, h, verdict, enough: true, edge_lo: lo, edge_hi: hi, weeks, weeks_needed: 12 });
const early = [row('买入', 1, '积累中', 0.01, 0.1, 4), row('买入', 5, '积累中', null, null, 3), row('卖出', 5, '积累中', -0.2, -0.01, 3)];   // 第一行是 4 周，结论要用买入 5 日的 3 周
const late = [row('买入', 1, '有效', 0.01, 0.1, 12), row('买入', 5, '看不出差别', -0.1, 0.1, 12), row('卖出', 5, '反向', -0.2, -0.01, 12), row('加仓', 5, '有效', 0.02, 0.2, 12)];
console.log(JSON.stringify({ early: headline(early, 'blend2'), late: headline(late, 'blend2'), ex: excludesZero(early) }));
"""


@unittest.skipUnless(Path(NODE).exists(), 'node 不可用')
class HeadlineTests(unittest.TestCase):
    def run_js(self):
        r = subprocess.run([NODE, '-e', JS, str(ROOT / 'dashboard/ui/pages/signals-extra.js')], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_neutral_before_12_weeks_even_if_some_row_excludes_zero(self):
        o = self.run_js()
        self.assertTrue(o['early'].startswith('c40a.headline_wait'), o['early'])
        self.assertIn('"w":3', o['early'])
        self.assertIn('"n":12', o['early'])
        self.assertEqual(o['ex'], 2)

    def test_after_12_weeks_only_prespecified_rows(self):
        o = self.run_js()
        self.assertTrue(o['late'].startswith('c40a.headline_final'), o['late'])
        self.assertIn('"b":"看不出差别"', o['late'])   # 买入 1 日、加仓 5 日的「有效」不被拿来当结论
        self.assertIn('"s":"反向"', o['late'])

    def test_keys_exist_in_both_languages(self):
        for lang in ('zh', 'en'):
            d = json.loads((ROOT / f'dashboard/ui/i18n/{lang}.json').read_text(encoding='utf-8'))
            for k in ('c40a.headline_wait', 'c40a.headline_final', 'c40a.many_rows', 'c40a.old_ledger', 'c40a.title'):
                self.assertIn(k, d, (lang, k))
            self.assertNotIn('c40a.headline_better', d)


if __name__ == '__main__':
    unittest.main()
