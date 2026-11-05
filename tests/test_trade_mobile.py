"""今日买卖手机卡片布局测试：用 Node 的 vm 加载 dashboard/app.js，轻量 DOM 替身，不开浏览器、不读令牌、不联网。
运行：python3 tests/test_trade_mobile.py
"""
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PRELUDE = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert');
class Element {
  constructor() { this.value = ''; this.textContent = ''; this.innerHTML = ''; this.events = {}; this.hidden = false;
    this.style = {setProperty() {}}; this.classList = {toggle() {}}; this.dataset = {}; }
  addEventListener(name, callback) { this.events[name] = callback; }
  querySelectorAll() { return []; }
  setAttribute() {}
  // 和浏览器 Element.replaceChildren 一致：先清空原有子节点，再按参数顺序追加；非节点参数(字符串等)变成文本节点
  replaceChildren(...nodes) {
    this.childNodes = nodes.map(n => (n !== null && typeof n === 'object') ? n : {nodeType: 3, textContent: String(n)});
    this.innerHTML = '';
    this.textContent = this.childNodes.map(n => n.textContent ?? '').join('');
  }
}
function make(mobile, mine) {
  const elements = new Map();
  const get = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
  const document = {getElementById: get, querySelector: () => ({offsetHeight: 110}),
    documentElement: {getAttribute: () => 'term'}, querySelectorAll: () => []};
  const window = {addEventListener() {}, matchMedia: () => ({matches: mobile, addEventListener() {}})};
  const context = vm.createContext({document, window, console, URLSearchParams, setTimeout, clearTimeout,
    location: {hostname: '127.0.0.1'},
    fetch: async (url) => ({status: 200, json: async () => url.startsWith('/api/trade_list_mine') ? mine : {empty: true, groups: [], note: '只统计上线后信号'}})});
  const source = fs.readFileSync('dashboard/app.js', 'utf8').replace(/\ninit\(\);\s*$/, '\n');
  vm.runInContext(source, context);
  return {context, get};
}
const row = {code: '600001', name: '测试甲', last_date: '2026-10-16', action: '买入', now: 0, next: 0.25, close: 12.34,
  price_note: '前复权', d1: 1.08, liquidity: '正常', avg_turnover_20d: 90000000, lot_cost: 1234, reason: '测试原因ABC', flags: [],
  performance: {total: 5, b_total: 3, beat_bh: true, maxdd: -10, trades: 12, small_sample: false, sharpe: 0.5,
                start: '2020-01-02', end: '2026-10-16'}};
const stale = {...row, code: '002388', name: '旧数据乙', last_date: '2026-09-30', action: null, d1: 10.04,
  flags: ['发布日停牌', '数据日期异常：2026-09-30，应为 2026-10-16'], reason: '数据异常，未解释今日策略条件'};
function render(ctx, rows, id, sortable, options) {
  ctx.context.rows = rows; ctx.context.options = options;
  vm.runInContext('tradeRows(rows, $("' + id + '"), ' + sortable + ', options)', ctx.context);
  return ctx.get(id).innerHTML;
}
"""


def run_js(body):
    script = PRELUDE + "\n(async () => {\n" + body + "\nconsole.log('验证通过');\n})().catch(e => { console.error(e); process.exitCode = 1; });"
    process = subprocess.run(['node', '-e', script], cwd=ROOT, text=True, capture_output=True, timeout=30)
    return process


class TradeMobileTests(unittest.TestCase):
    def check(self, body):
        process = run_js(body)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn('验证通过', process.stdout)

    def test_desktop_width_keeps_table(self):
        self.check("""
const ctx = make(false, {});
const out = render(ctx, [row], 'tradeTable', true);
assert.ok(out.includes('<thead>') && out.includes('data-sort="code"') && out.includes('#/stock/600001'));
assert.ok(!out.includes('trade-card'), '桌面宽度不应出现卡片');
""")

    def test_mobile_width_renders_cards_not_table(self):
        self.check("""
const ctx = make(true, {});
const out = render(ctx, [row], 'tradeTable', true);
assert.ok(out.includes('trade-card') && out.includes('查看详情') && out.includes('#/stock/600001'));
assert.ok(!out.includes('<thead>') && !out.includes('data-sort'), '手机宽度不应有表头');
assert.ok(out.includes('0.0% → <strong>25.0%</strong>'), '今日 → 明日目标仓位');
assert.ok(out.includes('1.08') && out.includes('正常') && out.includes('1234.00'));
// 第一行：名称、代码、信号；其余内容在“查看详情”展开区里
assert.ok(out.indexOf('测试甲') < out.indexOf('trade-card-grid'));
assert.ok(out.indexOf('trade-card-grid') < out.indexOf('查看详情'));
assert.ok(out.indexOf('查看详情') < out.indexOf('测试原因ABC'));
assert.ok(out.indexOf('查看详情') < out.indexOf('样本外累计收益'));
assert.ok(out.indexOf('查看详情') < out.indexOf('可交易性'));
assert.ok(render(ctx, [], 'tradeTable', true).includes('无匹配记录'));
""")

    def test_exception_rows_do_not_show_old_change(self):
        self.check("""
for (const mobile of [false, true]) {
  const ctx = make(mobile, {});
  const out = render(ctx, [stale], 'tradeExceptions', false, {exception: true});
  assert.ok(out.includes('旧数据乙') && out.includes('—'), '异常行涨跌幅显示 —');
  assert.ok(!out.includes('10.04'), '不显示旧交易日的涨跌幅 mobile=' + mobile);
  const normal = render(ctx, [row], 'tradeTable', true);
  assert.ok(normal.includes('1.08'), '正常行仍显示涨跌幅');
}
""")

    def test_no_signal_shows_no_signal_text(self):
        self.check("""
const mine = {member_logged_in: false, rows: [
  {code: '600531', name: '豫光金铅', action: null, now: null, next: null, flags: [], in_watchlist: true, in_member_positions: false},
  {code: '600001', name: '测试甲', action: '卖出', now: 0.3, next: 0, flags: [], in_watchlist: true, in_member_positions: false}]};
const ctx = make(false, mine);
const text = r => { ctx.context.r = r; return vm.runInContext('tradeTargetText(r)', ctx.context); };
assert.equal(text({action: null, now: null, next: null, flags: []}), '无信号');
assert.equal(text({action: null, now: null, next: null, flags: ['扫描仓位数据不足']}), '数据不足');
assert.equal(text(row), '25.0%');
vm.runInContext('S.market = "cn";', ctx.context);
ctx.get('tradeStrategy').value = 'blend2'; ctx.get('tradeDate').value = '';
await vm.runInContext('loadTradeFollowups(TRADE.request)', ctx.context);
const chunks = ctx.get('tradeMineBody').innerHTML.split('</tr>');
const first = chunks.find(x => x.includes('豫光金铅 600531')), second = chunks.find(x => x.includes('测试甲 600001'));
assert.ok(first.includes('无信号') && !first.includes('数据不足'), '无信号股票显示“无信号”');
assert.ok(second.includes('0.0%'), '有信号的股票显示目标仓位');
""")

    def test_mobile_sort_controls_and_full_render(self):
        self.check("""
const ctx = make(true, {});
vm.runInContext('tradeInitMobile()', ctx.context);
const select = ctx.get('tradeSortMobile'), button = ctx.get('tradeSortDir');
for (const key of ['code', 'avg_turnover_20d', 'next', 'total']) assert.ok(select.innerHTML.includes('value="' + key + '"'), key);
for (const key of ['reason', 'flags']) assert.ok(!select.innerHTML.includes('value="' + key + '"'), key);
select.value = 'next'; select.events.change();
assert.equal(vm.runInContext('TRADE.sort', ctx.context), 'next');
assert.equal(vm.runInContext('TRADE.dir', ctx.context), -1);
assert.equal(vm.runInContext('TRADE.manualSort', ctx.context), true);
button.events.click();
assert.equal(vm.runInContext('TRADE.dir', ctx.context), 1);
// 整体渲染：手机上正常行和异常行都是卡片，排序按所选字段
ctx.context.payload = {rows: [{...row, code: '600002', name: '测试乙', next: 0.75}, row], exceptions: [stale], binary: false, min_target: 0.05};
vm.runInContext('TRADE.data = payload; TRADE.tab = "buy"; TRADE.sort = "next"; TRADE.dir = 1; TRADE.manualSort = true; renderTrade();', ctx.context);
const table = ctx.get('tradeTable').innerHTML;
assert.ok(table.includes('trade-card') && table.indexOf('测试甲') < table.indexOf('测试乙'), '按明日目标仓位升序');
vm.runInContext('TRADE.dir = -1; renderTrade();', ctx.context);
const desc = ctx.get('tradeTable').innerHTML;
assert.ok(desc.indexOf('测试乙') < desc.indexOf('测试甲'), '切换为降序');
assert.equal(select.value, 'next');
assert.ok(button.textContent.includes('降序'));
const exceptions = ctx.get('tradeExceptions').innerHTML;
assert.ok(exceptions.includes('旧数据乙') && exceptions.includes('trade-card-exception') && !exceptions.includes('10.04'));
""")

    def test_css_and_html_contract(self):
        css = (ROOT / 'dashboard/terminal.css').read_text(encoding='utf-8')
        html = (ROOT / 'dashboard/index.html').read_text(encoding='utf-8')
        self.assertIn('.trade-mobile-sort { display: none; }', css)
        self.assertIn('@media (max-width: 600px) {\n  .trade-mobile-sort { display: block;', css)
        self.assertIn('.trade-card-grid', css)
        self.assertIn('#tradeTracking h2, .trade-mine h2 { margin-top: 0; }', css)
        for token in ('id="tradeSortMobile"', 'id="tradeSortDir"'):
            self.assertIn(token, html)


if __name__ == '__main__':
    unittest.main(verbosity=2)
