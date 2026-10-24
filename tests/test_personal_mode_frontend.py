"""个人模式前端：源码静态约束与离线 Node 合成 DOM，不请求服务或读取业务数据。"""
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / 'dashboard/app.js').read_text()
I18N = (ROOT / 'dashboard/i18n.js').read_text()
HTML = (ROOT / 'dashboard/index.html').read_text()


class PersonalFrontendTests(unittest.TestCase):
    def test_static_navigation_and_default_off(self):
        self.assertIn('S.meta?.personal_mode === true', APP)
        nav = re.search(r'const PERSONAL_NAV = (.*);', APP).group(1)
        self.assertEqual(re.findall(r'\["(\w+)",', nav), ['today', 'trade', 'market', 'watch', 'lab', 'holdings', 'monitor', 'usmonitor'])
        self.assertEqual(len(re.findall(r'\["\w+",', nav)), 8)
        self.assertEqual(re.findall(r'\["(\w+)",', nav)[:7], ['today', 'trade', 'market', 'watch', 'lab', 'holdings', 'monitor'])
        self.assertIn('["usmonitor", "美股盯盘"]', nav)
        self.assertIn('if (!personalMode()) {\n    loadShare();', APP)
        self.assertIn('setInterval(loadShare, 15000)', APP)
        self.assertRegex(HTML, r'<nav[^>]*id="tabs"[^>]*hidden')
        for page in ('review', 'track', 'bt', 'rq', 'cmp', 'code'):
            self.assertIn('data-p="' + page + '"', HTML)
        self.assertIn('nav.tabs[hidden] { display: none; }', (ROOT / 'dashboard/terminal.css').read_text())

    def test_new_renderers_use_text_nodes(self):
        new = APP[APP.index('function personalMode()'):APP.index('const PAGES =')]
        self.assertNotIn('innerHTML', new)
        self.assertNotIn('insertAdjacentHTML', new)
        self.assertIn('link.textContent = label', new)
        self.assertIn('message.textContent = e.message', APP)
        self.assertIn('document.querySelector("main").replaceChildren(message)', APP)
        for fn in ('loadShare', 'renderShare', 'bindOfflineBtn'):
            self.assertRegex(APP, rf'function {fn}\([^)]*\) {{\s*if \(personalMode\(\)\) return;')

    def test_bilingual_labels_and_accessibility(self):
        for zh, en in [('今日', 'Today'), ('信号', 'Signals'), ('全市场', 'Market'), ('自选', 'Watchlist'), ('回测', 'Backtest')]:
            self.assertIn(f'"{zh}": "{en}"', I18N)
        self.assertIn('link.setAttribute("aria-label", label)', APP)
        self.assertIn('personalNavLabels[source]', I18N)
        self.assertIn('personalNavLabels[saved[attr]]', I18N)
        self.assertIn('dashboard-language-change', I18N)

    def test_personal_layout_css_is_scoped_and_responsive(self):
        css = (ROOT / 'dashboard/terminal.css').read_text().split('/* W4：', 1)[1].split('*/', 1)[1]
        # W4 所有规则都受个人模式约束；关闭开关不会命中这些选择器。
        clean = re.sub(r'/\*.*?\*/', '', css, flags=re.S)
        for selector in re.findall(r'([^{}]+)\{', clean):
            if selector.strip().startswith('@media'):
                continue
            for part in re.split(r',(?=\s*html)', selector):
                self.assertTrue(part.strip().startswith('html.personal-mode'), selector)
        self.assertIn('padding: 16px 16px 80px', css)
        self.assertIn('@media (max-width: 768px)', css)
        self.assertIn('@media (max-width: 600px)', css)
        self.assertIn('overflow-wrap: anywhere', css)
        self.assertIn('grid-template-columns: minmax(0, 1fr)', css)
        self.assertIn('html.personal-mode .tbl { min-width: 0; overflow-x: auto; }', css)
        self.assertIn('html.personal-mode nav.tabs { flex-wrap: wrap; overflow: visible;', css)
        self.assertIn('html.personal-mode nav.tabs > a { flex: 1 0 auto; white-space: nowrap; }', css)
        self.assertIn('html.personal-mode .results { width: min(300px, 100%); }', css)
        self.assertIn('html.personal-mode .kv { grid-template-columns: repeat(2, minmax(0, 1fr)); }', css)
        self.assertIn('html.personal-mode .fresh, html.personal-mode .refresh-progress { flex-wrap: wrap; white-space: normal;', css)
        self.assertIn(':focus-visible', css)
        self.assertIn('color: var(--text-2)', css)
        self.assertNotRegex(css, r'(?m)^html\.personal-mode(?: body)?\s*\{[^}]*overflow[^}]*hidden')

    @unittest.skipUnless(shutil.which('node'), 'Node 不可用，静态检查仍运行')
    def test_node_routes_navigation_and_init_requests(self):
        script = r'''
const fs = require('fs'), vm = require('vm'), assert = require('assert');
class El {
  constructor() { this.hidden = true; this.dataset = {}; this.children = []; this.attrs = {}; this.classes = new Set();
    this.classList = {add: x => this.classes.add(x), toggle: () => {}}; }
  replaceChildren(...items) { this.children = items; }
  setAttribute(k,v) { this.attrs[k] = v; }
  addEventListener() {}
}
(async () => {
for (const flag of [undefined, false, 'true', 1, true]) {
  const tabs = new El(), share = new El(), main = new El(), elements = {tabs, shareBar: share, main};
  tabs.children = ['original navigation'];
  const requests = [], intervals = [], loads = [];
  const location = {hash: '#/today'};
  const document = {documentElement: new El(), getElementById: id => elements[id] || new El(),
    createElement: () => new El(), querySelector: () => main, querySelectorAll: () => []};
  const ctx = vm.createContext({document, location, console, Set, URLSearchParams,
    history: {replaceState(a,b,hash) { location.hash = hash; }},
    window: {addEventListener() {}, scrollTo() {}}, localStorage: {getItem() {return null;}},
    setInterval: (fn, ms) => intervals.push(ms), setTimeout() {}, clearTimeout() {},
    fetch: async path => {requests.push(path); return {status:200, json:async () => path === '/api/meta' ?
      {personal_mode:flag, default_market:'cn', markets:{cn:{}}} : {running:false}};}});
  vm.runInContext(fs.readFileSync('dashboard/app.js','utf8').replace(/\ninit\(\);\s*$/, '\n'), ctx);
  ctx.loads = loads;
  vm.runInContext(`renderMarkets = setupSearch = setupPersonalPages = pLoadSection = () => {};
    renderShare = () => {}; loadToday = async () => loads.push('today');
    loadTrade = async () => loads.push('trade'); loadLab = async () => loads.push('lab');
    loadReview = async () => loads.push('review'); loadTrack = async () => loads.push('track');
    loadAi = async () => loads.push('ai');`, ctx);
  await vm.runInContext('init()', ctx);
  await Promise.resolve();
  const on = flag === true;
  assert.equal(tabs.hidden, false);
  assert.deepEqual(intervals, on ? [] : [15000]);
  assert.deepEqual(requests, on ? ['/api/meta'] : ['/api/meta','/api/share']);
  if (on) {
    assert.deepEqual(tabs.children.map(x=>x.dataset.p), ['today','trade','market','watch','lab','holdings','monitor','usmonitor']);
    assert.deepEqual(tabs.children.map(x=>x.textContent), ['今日','信号','全市场','自选','回测','持仓','盯盘','美股盯盘']);
    assert(tabs.children.every(x=>x.attrs['aria-label'] === x.textContent));
    assert.equal(share.hidden, true);
    for (const page of ['ai','translator','bt','rq','cmp','code','member','admin']) {
      location.hash = '#/'+page+'/test?x=1'; loads.length=0;
      vm.runInContext('route()',ctx); assert.equal(location.hash,'#/today'); assert.deepEqual(loads,['today']);
    }
    for (const page of ['review','track']) {
      location.hash = '#/'+page+'/strategy/date?x=1'; loads.length=0;
      vm.runInContext('route()',ctx);
      assert.equal(location.hash,'#/trade/strategy/date?x=1'); assert.deepEqual(loads,['trade']);
      vm.runInContext('route()',ctx); // 刷新或前进后退重复进入仍走信号页
      assert.equal(loads.at(-1),'trade');
    }
    location.hash='#/lab?strategy=demo'; vm.runInContext('route()',ctx); assert.equal(loads.at(-1),'lab');
    await vm.runInContext('loadShare()',ctx);
    assert.deepEqual(requests,['/api/meta']);
  } else {
    assert.deepEqual(tabs.children,['original navigation']);
    for (const page of ['review','track','ai']) {
      location.hash='#/'+page; vm.runInContext('route()',ctx);
      assert.equal(location.hash,'#/'+page); assert.equal(loads.at(-1),page);
    }
  }
}
console.log('PASS strict meta flag, current navigation labels/aria, off DOM/routes/polling, hidden routes, aliases/parameters, zero personal hidden requests');
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        result = subprocess.run(['node', '-e', script], cwd=ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        print(result.stdout, end='')


    @unittest.skipUnless(shutil.which('node'), 'Node 不可用')
    def test_node_integrated_pages_cache_sources_and_races(self):
        script = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert');
let fullMode=false;
class El {
  constructor(tag='div') {
    this.tagName=tag; this.children=[]; this.dataset={}; this.attrs={}; this.events={}; this.nodeType=1;
    this._text=''; this.value=''; this.hidden=false; this.style={setProperty(){}};
    const classes=new Set();
    this.classList={add:x=>classes.add(x), contains:x=>classes.has(x), toggle:(x,on)=>on?classes.add(x):classes.delete(x)};
  }
  set textContent(v) {this._text=String(v??'');this.children=[];}
  get textContent(){return this._text+this.children.map(c=>c.textContent).join(' ');}
  set innerHTML(v){if(!fullMode)throw Error('personal rendering used innerHTML: '+this.id);this.textContent=v;}
  get firstChild(){return this.children[0];}
  get options(){return this.children.filter(c=>c.tagName==='option');}
  append(...nodes){for(const n of nodes){if(n.parentElement)n.parentElement.children.splice(n.parentElement.children.indexOf(n),1);this.children.push(n);n.parentElement=this;}}
  appendChild(n){this.append(n);return n;}
  prepend(n){this.insertBefore(n,this.firstChild);}
  insertBefore(n,b){if(n.parentElement)n.parentElement.children.splice(n.parentElement.children.indexOf(n),1);const i=this.children.indexOf(b);this.children.splice(i<0?this.children.length:i,0,n);n.parentElement=this;}
  before(n){this.parentElement.insertBefore(n,this);}
  replaceChildren(...nodes){this.children.forEach(n=>n.parentElement=null);this.children=[];this._text='';this.append(...nodes);}
  setAttribute(k,v){this.attrs[k]=v;if(k==='id')this.id=v;if(k==='class')v.split(' ').forEach(x=>this.classList.add(x));}
  getAttribute(k){return this.attrs[k];}
  addEventListener(k,fn){(this.events[k] ||= []).push(fn);}
  matches(sel){if(sel.startsWith('#'))return this.id===sel.slice(1);if(sel.startsWith('.'))return this.classList.contains(sel.slice(1));if(sel==='*')return true;return this.tagName===sel;}
  querySelectorAll(sel){return this.children.flatMap(c=>[...(c.matches(sel)?[c]:[]),...c.querySelectorAll(sel)]);}
  querySelector(sel){return this.querySelectorAll(sel)[0]||null;}
  closest(sel){return this.matches(sel)?this:this.parentElement?.closest(sel);}
}
function fixture() {
  const root=new El('html'), stack=[root];
  const html=fs.readFileSync('dashboard/index.html','utf8');
  const main=html.slice(html.indexOf('<main'),html.indexOf('</main>')+7);
  for(const token of main.match(/<!--[\s\S]*?-->|<[^>]+>|[^<]+/g)) {
    if(token.startsWith('<!--'))continue;
    if(token.startsWith('</')){stack.pop();continue;}
    if(token.startsWith('<')) {
      const tag=/^<(\w+)/.exec(token)[1], el=new El(tag);
      for(const a of token.matchAll(/([\w-]+)="([^"]*)"/g)){
        el.setAttribute(a[1],a[2]);if(a[1]==='value')el.value=a[2];
        if(a[1].startsWith('data-'))el.dataset[a[1].slice(5)]=a[2];
      }
      stack.at(-1).append(el);if(!['input','br','hr','img'].includes(tag))stack.push(el);
    } else stack.at(-1)._text+=token.trim();
  }
  return root;
}
(async()=>{
const root=fixture(), requests=[], events={};
const header=new El("header"); header.offsetHeight=100;root.append(header);
const get=id=>root.querySelector('#'+id);
let me={user:null}, portfolio={total:12000,ret_pct:2,positions:[],curve:[['2026-10-01',12000]]};
let mineLogged=false, mainError=false, summaryError=false, listEmpty=false, noMain=false, delayLedger=null, ledgerCalls=0;
const attack='<img src=x onerror=bad()>'+ '超长股票名称LongName'.repeat(80), lists={};
for(const strategy of ['main','other'])lists[strategy]={strategy,label:strategy,data_date:'2000-01-01',generated_at:'2000-01-01T12:00:00Z',min_target:.07,counts_main:{'买入':13},counts_small:{'买入':4},counts:{'卖出':1,'减仓':1,'买入':1,'加仓':1},exceptions:[{code:'000005',name:attack,flags:['停牌']}],rows:['卖出','减仓','买入','加仓'].map((action,i)=>({action,code:'00000'+(i+1),name:attack,reason:attack,now:.1,next:.2,performance:{},liquidity:'流动性未知'}))};
const live={total:1,since:'2026-01-01',latest:'2026-01-02',summary:[{strategy:'main',action:'买入',signals:1,n20:0}],expected_first_5:'2026-11-02',recent:[{signal_date:'2026-10-22',code:'600531',name:'合成股票',strategy:'main',action:'买入',r5:1.2,r20:2.3,r60:3.4,r_now:12.3,x_now:4.5}]};
const replay={...live,total:999};
const serve=async path=>{
  requests.push(path);
  if(path==='/api/main_strategy')return mainError?{error:'temporary main error'}:noMain?{}:{main:'main'};
  if(path.startsWith('/api/trade_list?'))return listEmpty?{empty:true,message:attack}:lists[new URLSearchParams(path.split('?')[1]).get('strategy')];
  if(path==='/api/trade_list_dates')return {dates:['2026-01-01']};
  if(path.startsWith('/api/trade_list_tracking'))throw Error(attack);
  if(path.startsWith('/api/trade_list_mine'))return {member_logged_in:mineLogged,rows:lists.main.rows.map(r=>({...r,in_watchlist:true,in_member_positions:mineLogged}))};
  if(path.startsWith('/api/summary')){if(summaryError)throw Error('summary failed');return {health:[attack],watch:[],strategies:[]};}
  if(path.startsWith('/api/tracking'))return {eval:live,replay,notifications:[]};
  if(path==='/api/member/me')return me;
  if(path.startsWith('/api/member/portfolio'))return portfolio;
  if(path==='/api/ledger'){ledgerCalls++;if(delayLedger)return delayLedger;return {main:'main',main_pick:{main:'main',default:true},promotion:{message:attack},signal_days:['a','b','c'],evaluated_days:['a','b'],notes:[attack],precision:{main:[{action:'买入',h:5,n:2,hit_excess:.5,untradable:7,hit_excess_lo:.12,hit_excess_hi:.88,median_excess:.023,net_excess:.019,enough:false}]},recall:[{day:'2026-10-16',h:5,winners:9,recall:.4,categories:{'被过滤':3,'没有信号':6},missed:[{code:'000099',name:attack,ret:.234,category:'合成未买原因'}]}],reflection:[]};}
  throw Error('unexpected '+path);
};
const ctx=vm.createContext({document:{documentElement:root,getElementById:get,createElement:tag=>new El(tag),querySelectorAll:s=>root.querySelectorAll(s),querySelector:s=>root.querySelector(s)},
  window:{location:{assign:path=>requests.push(path)},addEventListener:(k,f)=>events[k]=f,AI_I18N:{language:'zh'},scrollTo(){}},location:{hostname:'127.0.0.1',hash:'#/trade'},
  URLSearchParams,console,setTimeout,clearTimeout,localStorage:{getItem(){return null;}},fetch:async path=>({status:200,json:()=>serve(path)})});
vm.runInContext(fs.readFileSync('dashboard/app.js','utf8').replace(/\ninit\(\);\s*$/,'\n'),ctx);
const run=code=>vm.runInContext(code,ctx);
run(`S.meta={personal_mode:false,strategies:{main:{kind:'single',label:'合成主策略'},other:{kind:'single',label:'other'}}};`);
const oldToday=[...get('p-today').children], oldReview=[...get('p-review').children];
run('setupPersonalPages()');assert.deepEqual(get('p-today').children,oldToday);assert.deepEqual(get('p-review').children,oldReview);
run('S.meta.personal_mode=true;setupPersonalPages()');
assert.equal(get('p-review').children.length,0);assert.equal(get('p-track').children.length,0);
assert.equal(get('reviewBody').closest('details').id,'personal-review');assert.equal(get('trEmpty').closest('details').id,'personal-track');
const ids=root.querySelectorAll('*').filter(x=>x.id).map(x=>x.id);assert.equal(ids.length,new Set(ids).size);
assert.deepEqual(get('p-today').children.filter(x=>x.id?.startsWith('personal')).map(x=>x.id),['personalActions','personalMonitor','personalPortfolio','personalRisks','personalReference']);
const order=get('p-trade').children.map(x=>x.id).filter(Boolean);
assert(order.indexOf('tradeMine')<order.indexOf('tradeTracking'));assert(order.indexOf('tradeTracking')<order.indexOf('personal-review'));assert(order.indexOf('personal-review')<order.indexOf('personal-track'));
assert.deepEqual(get('tradeTabs').children.filter(x=>!x.hidden).map(x=>x.dataset.tab),['sell','reduce','buy','add']);
assert.equal(ledgerCalls,0);assert.equal(requests.length,0);
assert(get('personal-review').querySelector('h2'));assert(!get('personal-review').querySelector('h1'));
assert.equal(get('p-lab').querySelector('h1').textContent,'回测');
assert.equal(get('personalMonitor').querySelector('.personal-state').getAttribute('role'),'status');
assert(!get('btOut').querySelector('details').open);
const emptyTable=run('pTable(["Code"], [])');assert(emptyTable.textContent.includes('暂无数据'));
await run('loadPersonalToday()');
assert(get('personalActions').textContent.includes('可能过旧'));assert(get('personalActions').textContent.includes(attack));
assert(get('personalPortfolio').textContent.includes('未加载'));assert(!requests.some(x=>x.includes('/portfolio')));
assert(!requests.some(x=>/monitor|\/sim|\/ai|\/share|\/eng|\/code/.test(x)));
await run('loadTrade()');
assert.equal(requests.filter(x=>x.startsWith('/api/trade_list?')).length,1);
assert.equal(requests.filter(x=>x==='/api/main_strategy').length,1);
assert(get('tradeCounts').textContent.includes('目标仓位 ≥7% 的 13 只，另有 4 只为小仓位'));
assert(get('tradeMineBody').textContent.includes('相关来源'));assert(get('tradeMineBody').textContent.includes('登录模拟交易会员'));
assert(get('tradeMineBody').textContent.includes('600531 固定第一'));assert(get('tradeMineBody').querySelector('.trade-mine-sell'));
mineLogged=true;await run('pTradeFollowups(TRADE.request)');
assert(get('tradeMineBody').textContent.includes('已包含当前会员'));assert(get('tradeMineBody').textContent.includes('自选 / 模拟持仓'));
assert(get('tradeMineBody').textContent.includes(attack));assert(get('tradeTrackingBody').textContent.includes('区块暂不可用'));
assert(get('tradeTable').textContent.includes(attack));assert(!root.querySelector('img'));
assert(get('tradeTable').classList.contains('personal-signal-table'));
assert.equal(get('tradeTable').querySelector('tbody').firstChild.children.length,17);
const cellLabels=get('tradeTable').querySelectorAll('.personal-cell-label');assert.equal(cellLabels.length,17);
assert.equal(cellLabels[0].textContent,'股票 / 代码');
ctx.window.AI_I18N.language='en';events['dashboard-language-change']();
assert.equal(cellLabels[0].textContent,'Stock / code');assert.equal(cellLabels[6].textContent,'Signal');
assert(get('tradeTable').textContent.includes('Sell'));assert(get('tradeTable').textContent.includes(attack));
assert.equal(get('tradeTable').querySelectorAll('.personal-cell-label').length,17);
ctx.window.AI_I18N.language='zh';events['dashboard-language-change']();
assert.equal(cellLabels[0].textContent,'股票 / 代码');
assert(get('tradeSortMobile').textContent.includes('20 日平均成交额（元）'));
for(const [tab,action] of [['sell','卖出'],['reduce','减仓'],['buy','买入'],['add','加仓']]) {
  run(`TRADE.tab='${tab}';renderTrade()`);
  assert.equal(run('tradeCSVData().rows.length'),1);assert.equal(run('tradeCSVData().rows[0].action'),action);
}
assert(get('tradeCoverageWarning').textContent.includes('补充中'));
assert(get('tradeExceptions').textContent.includes(attack));
run('S.personalSelection={strategy:"other",date:"2026-01-01"}');await run('loadTrade()');
assert.equal(get('tradeDate').value,'2026-01-01');assert.equal(get('tradeStrategy').value,'other');
assert(requests.some(x=>x.includes('strategy=other&date=2026-01-01')));
get('tradeExportXlsx').events.click[0]();
assert.equal(requests.at(-1),'/api/trade_list_export?strategy=other&date=2026-01-01');
run('TRADE.tab="sell";renderTrade()');assert.equal(run('tradeCSVData().rows.length'),1);
get('tradeExportXlsx').events.click[0]();assert.equal(requests.at(-1),'/api/trade_list_export?strategy=other&date=2026-01-01');
const listeners=get('tradeExport').events.click.length;await run('loadTrade()');assert.equal(get('tradeExport').events.click.length,listeners);
await run('pLoadSection("review")');assert.equal(ledgerCalls,1);assert(get('reviewBody').textContent.includes(attack));
assert(get('reviewMeta').textContent.includes('已记录 3 个信号日，已核对 2 个'));
for(const text of ['被过滤','没有信号','000099','23.4%','合成未买原因','7 条开盘即涨/跌停','95% 区间 12%–88%','中位数 2.30%','扣费后 1.90%','样本不足'])assert(get('reviewBody').textContent.includes(text),text);
assert(get('reviewBody').querySelector('tbody').children[0].classList.contains('dim'));
await run('pLoadSection("track")');assert(get('trEmpty').textContent.includes('1 · 2026'));assert(!get('trEmpty').textContent.includes('999'));
for(const text of ['合成主策略','合成股票','+12.3%','+4.5%','尚无信号完成 5 个持有交易日','2026-11-02'])assert(get('trEmpty').textContent.includes(text),text);
assert.equal(get('trEmpty').querySelector('a').href,'#/stock/600531');
await run('PERSONAL.track="replay";pLoadSection("track")');assert(get('trEmpty').textContent.includes('999'));assert(get('trEmpty').textContent.includes('历史回放'));
live.empty=true;await run('PERSONAL.track="live";pLoadSection("track")');assert(get('trEmpty').textContent.includes('不自动切换'));assert(!get('trEmpty').textContent.includes('999'));live.empty=false;
assert.equal(run('pRatio(null)'), '—');assert.equal(run('pRatio(0)'), '0.0%');
events['dashboard-language-change']();ctx.window.AI_I18N.language='en';events['dashboard-language-change']();assert(get('tradeCounts').textContent.includes('small targets: 4'));assert(get('reviewBody').textContent.includes('Hit rate 95% interval'));assert(get('tradeMineBody').textContent.includes('Paper holdings'));assert(get('personalMonitor').textContent.includes('Not connected'));
ctx.window.AI_I18N.language='zh';events['dashboard-language-change']();assert(get('personalMonitor').textContent.includes('尚未接入'));
me={user:{name:'synthetic'}};await run('loadPersonalToday()');assert(get('personalPortfolio').textContent.includes('12000.00'));assert(get('personalReference').textContent.includes('2026-10-01'));
portfolio={error:attack};await run('loadPersonalToday()');assert(get('personalPortfolio').textContent.includes('不可用'));assert(!get('personalPortfolio').textContent.includes('12000'));
ctx.location.hostname='shared.example';const memberN=requests.filter(x=>x.includes('/member/')).length;await run('loadPersonalToday()');assert.equal(memberN,requests.filter(x=>x.includes('/member/')).length);
ctx.location.hostname='127.0.0.1';summaryError=true;run('pInvalidate()');await run('loadPersonalToday()');assert(get('personalActions').textContent.includes('卖出'));assert(get('personalActions').textContent.includes('区块暂不可用'));
mainError=true;run('pInvalidate()');let errorN=requests.filter(x=>x==='/api/main_strategy').length;
await run('pRequest("/api/main_strategy")');mainError=false;await run('pRequest("/api/main_strategy")');
assert.equal(requests.filter(x=>x==='/api/main_strategy').length,errorN+2);
noMain=true;run('TRADE.inited=false;S.personalSelection={strategy:"main"};pInvalidate()');await run('loadTrade()');assert(get('personalStrategyState').textContent.includes('主策略不可用'));assert(get('tradeTable').textContent.includes(attack));
noMain=true;run('pInvalidate()');await run('loadPersonalToday()');assert(get('personalActions').textContent.includes('主策略不可用'));
noMain=false;listEmpty=true;run('pInvalidate()');await run('loadPersonalToday()');assert(get('personalActions').textContent.includes('暂无已发布动作'));
run('pInvalidate()');await run('loadTrade()');assert.equal(run('TRADE.data'),null);assert.equal(run('tradeCSVData().rows.length'),0);assert(get('tradeMessage').textContent.includes(attack));assert.equal(get('tradeTable').textContent,'');
let release;delayLedger=new Promise(r=>release=r);run('pInvalidate()');const pending=run('pLoadSection("review")');await Promise.resolve();
run('S.market="us";PERSONAL.view++');await run('pLoadSection("review")');release({notes:['STALE_CN_RESPONSE']});await pending;
assert(get('reviewBody').textContent.includes('美股'));assert(!get('reviewBody').textContent.includes('STALE_CN_RESPONSE'));
const before=requests.filter(x=>x==='/api/ledger').length;await run('pLoadSection("review")');assert.equal(requests.filter(x=>x==='/api/ledger').length,before);
// 完整模式执行原 loader：独立核对页字段与旧自动回放行为仍在。
fullMode=true;delayLedger=null;run('S.market="cn";S.meta.personal_mode=false');
await run('loadReview()');
for(const text of ['000099','合成未买原因','另有 7 条','命中率 95% 区间'])assert(get('reviewBody').textContent.includes(text),text);
assert(get('reviewMeta').textContent.includes('已记录 3 个信号日，已核对 2 个'));
await run('loadTrack()');assert(get('trStatus').textContent.includes('历史回放：999'));
replay.empty=true;await run('loadTrack()');
assert(get('trStatus').textContent.includes('预计首批统计日期 2026-11-02'));
for(const text of ['合成主策略','合成股票','+12.3%','+4.5%'])assert(get('trRecent').textContent.includes(text),text);
console.log('PASS integrated layout, unique IDs, four actions, cached published lists, archives, CSV scope and XLSX endpoint, restored review/mine/tracking fields, error-cache retry, signal empty/main states, lazy independent sections, explicit replay, bilingual text, local assets, empty/error/stale/race isolation');
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
        result = subprocess.run(['node', '-e', script], cwd=ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        print(result.stdout, end='')


if __name__ == '__main__':
    unittest.main(verbosity=2)
