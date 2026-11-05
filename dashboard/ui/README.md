# dashboard/ui · 新界面（quant-ai-dark · Workspace）

旧界面原样保留在 `/`，新界面放在 `/ui/`（上线前用预览服务器看）。设计源在 `dashboard/mockups/`，这里只复制、不改 mockup。

## 目录

| 路径 | 内容 |
|---|---|
| `index.html` | 入口，meta refresh 跳到 `w/today.html` |
| `w/` | 页面 html（只写骨架）。`_template.html` 是样板 |
| `pages/` | 页面逻辑：`pages/<id>.js` 导出 `async function render(root)` |
| `nav.js` | 顶栏和导航清单（每项有新页面地址 `href` 和旧页面地址 `classic`；`BUILT` 清单决定点了去新还是旧） |
| `shared/` | 通用模块：`api.js`（数据层）、`page-boot.js`（页面启动）、`dom.js`、`i18n.js`、`toast.js`、`chart-helpers.js`、`chart-heatmap.js`、`command-palette.js`（股票结果的排序抽成纯函数 `palette-match.js`，见 `docs/A13_palette_search.md`）、`theme-boot.js`、`theme-switcher.js`、`number-ticker.js`、`quota-bar.js`、`custom-cursor.js`；以及通用 css 和 `icons.svg` |
| `theme/` | `tokens.css`（颜色等变量）、`workspace.css`（密度）、`theme.css`（落地页光晕，Workspace 不引用） |
| `vendor/` | 本地第三方库（固定版本，见 `vendor/VERSIONS.md`） |
| `i18n/` | `zh.json` / `en.json` 基础字典；`parts/<终端号>.<语言>.json` 各终端的增量词条 |
| `mock-data/` | `?mock=1` 预览用的假数据，规则见 `mock-data/README.md` |
| `API_MAP.md` | 旧页面 ↔ 新页面 ↔ 接口对照 |

## 预览

    cd <项目目录>/dashboard/ui
    python3 -m http.server 8767 --bind 127.0.0.1

打开 `http://127.0.0.1:8767/w/_template.html?mock=1`。`?mock=1` 读 `mock-data/`；不带 `mock=1` 会请求 `/api/...`，静态预览服务器上会 404，页面显示错误卡片（这是预期行为）。
还可以加 `?state=loading|error|empty` 强制看三种状态。
出错 / 空数据卡片标题下面的说明行（「原因：HTTP 404」之类）由 `shared/state-card.js` 统一填写，词条在 `i18n/parts/A03.*.json`；词条合并进基础字典之前说明行不显示（不会露出键名）。出错卡片里「回到首页」「重试」靠 `hidden` 二选一，`components-data.css` 里的 `.state [hidden]` 保证它真的隐藏。

## 手机宽度（375px）检查

`shared/reset.css` 给 `html, body` 设了 `overflow-x: clip`，越界内容会被**直接裁掉**而不是出现滚动条，所以 `document.documentElement.scrollWidth > 375` 永远是 false，测不出问题。要遍历元素：找出「右边缘超过 375、且祖先里没有 `overflow-x` 非 visible 的容器」的元素，以及「右边缘超出自己父元素」的元素。
几条规律（A04 修过的都是这几类）：grid / flex 容器的子项默认 `min-width: auto`，不会比内容窄，要给列写 `minmax(0, 1fr)` 或给子项 `min-width: 0`；宽表格必须包在 `.table-wrap`（注意有连字符）里并带 `overflow-x: auto`；`.field__label` 是绝对定位的浮动标签，只能配 `.field__input` 用；KPI 数字在两列卡片里内容区只有约 100px。

## 怎么加一个新页面（以 `holdings` 为例）

1. 复制 `w/_template.html` → `w/holdings.html`，改 `<title>`、`<body class="ws" data-page="holdings">`、页头标题的 `data-i18n` 键。
2. 新建 `pages/holdings.js`，写 `export async function render(root) { ... }`：
   - 数据用 `import { getJSON, postJSON } from '../shared/api.js'`，路径写 `/api/...`。
   - 内容用 `el()`（`shared/dom.js`）或 `textContent` 构建，不拼 HTML 字符串；markdown 只能 marked → DOMPurify。
   - `getJSON` 抛的 `ApiError` 不用接，直接让它抛：页面启动器会显示错误卡片，401/403 显示「登录已过期，请从首页重新进入」。
   - 没数据时 `return 'empty'`，会显示空状态。
3. 文案：`t('key')`；新词条写进自己的 `i18n/parts/<终端号>.zh.json` 和 `.en.json`（两边键必须完全一致）。
4. 假数据：`mock-data/<名字>.json`（字段和真实接口一致）。
5. 页面做好后，需要在 `nav.js` 的 `BUILT` 里加上这个 id，才会让导航点进新页面。这是共用文件：**不要自己改**，把需求追加到 `~/Documents/AQ-ui-status/requests.md`，由 U5 统一处理。开发预览（`?mock=1`）时所有导航项都直接指向新页面，不受 `BUILT` 影响。

## 规则（摘要，完整版见 `~/Documents/AQ-ui-migration/COMMON.md`）

- 服务器 CSP：`script-src 'self'`、`connect-src 'self'`。不写行内脚本、行内事件、CDN 地址、meta CSP。
- 单个 js/css 文件 ≤ 500 行（`vendor/` 的第三方文件除外）。
- 快捷键：1–9 切到导航前 9 项，R 刷新当前页，/ 搜索，? 看快捷键。
- 顶栏不要设 `data-mode="workspace"`（那套居中布局是给 10 项导航写的，13 项会压到搜索框）。

## 检查

交付前在项目根目录 grep `dashboard/ui`（排除 `vendor/`）：行内脚本、行内事件、HTML 字符串拼接相关的写法、外部网址、红线字符串都必须为 0。具体的词表见 `~/Documents/AQ-ui-migration/COMMON.md` 的「硬约束」和「红线」；检查时连注释和 md 文件里的字样也会命中，所以文档里也不要写出这些词。

## 今日页的数据日期与过期提示（A07）

`pages/today-date.js` 在今日页顶部显示「数据日期：YYYY-MM-DD（北京时间 15:30 收盘后更新）」，日期来自 `/api/summary` 的 `data_date`；个人模式的「今日动作」卡片再显示 `/api/trade_list` 自己的 `data_date`。今天按 `Asia/Shanghai` 算（不是电脑本地时区）。日期早于今天时出提示条：「这是 X 收盘后的清单，不是盘中信号」，再按周末 / 15:30 前 / 15:30 后补一句原因；数据日期就是今天但还没到 15:30 时提示可能是盘中半天数据。没有交易日历，所以不判断节假日。只对 A 股（`market=cn`）出提示条。词条在 `i18n/parts/A07.*.json`，合并进基础字典之前提示条不显示（不露键名），日期行退化成只显示日期。静态 + node 检查见 `tests/test_today_date_banner.py`。

## 今日页首屏的信息层级（G03）

今日页从上到下：数据日期（A07）→ 首屏三张卡 → 「详细数据」（市场概览、组合建议、其他策略、说明、提醒、关注股票，内容没改）。三张卡在 `pages/today-brief.js`（今日动作卡在 `pages/today-personal.js`）：

- **今日动作**（只在个人模式）：主策略已发布清单里卖出 / 减仓 / 买入 / 加仓 / 异常各几只，前四个格子直达信号页对应标签（G01）。清单只列仓位变化超过阈值（`trade_list.threshold`，现在 0.1 = 10 个百分点）或开 / 清仓的股票，**接口里没有「持有」的数量**，卡里写明这一点，不自己编。清单日期和页顶相同时不再重复日期行，落后时才补日期行 + 提示条。
- **持仓变化**：组合策略（`summary.strategies` 里的 blend2）在基准品种上「当前仓位 → 目标仓位」+ 动作徽章（这里能看到「持有」）；个人模式再把模拟账户持仓逐只对上今天清单：卖出 → 减仓 → 异常 → 加仓 → 买入 → 清单无信号，前 5 只直接列出，其余折叠。清单读不到时不显示徽章（不猜）。
- **需要注意**：`summary.health`、清单过旧（清单日期起超过 3 天）、异常 / 停牌数、`price_unavailable`、模拟持仓里的卖出 / 减仓 / 异常；有条目时琥珀色描边、`role=status`；最后固定一句通用风险提示。只列接口里真有的情况，不自己设阈值评判行情。

不新增接口请求：个人模式仍是 `/api/main_strategy`、`/api/trade_list`、`/api/member/me`、`/api/member/portfolio`（各取一次，两张卡共用）。布局：≥1280px 三列、701–1279px 两列（今日动作占满一行）、≤700px 一列；手机上隐藏页头的快捷键提示。词条在 `i18n/parts/G03.*.json`，合并进基础字典之前标题退回已有词条、说明句不显示（不露键名）。检查见 `tests/test_today_brief_g03.py`（含 `tests/g03_probe.mjs` 假 DOM 探针，按个人 / 非个人、非本机、清单报错、401、无提示、清单落后、美股、清单过旧共 9 个场景渲染整页，词条合并前后各跑一遍）。

## 表格的排序 / 筛选写进网址（A08）

全市场、自选、今日买卖三页把当前的排序列、方向和筛选值同步到 `location.search`，刷新或把链接发给别人都能还原。公共代码在 `shared/url-state.js`：用 `URLSearchParams` 读写、`history.replaceState` 改地址（只替换当前这条历史，不产生新的历史记录，「后退」不会被塞满）。只改前端，没改接口。

参数名 = 页面前缀 + `_` + 一个字母，前缀把各页隔开，也不会碰 `mock` / `state` / `debug` / `market` 这些已有参数；本页以外的参数和 `#hash` 原样保留。

| 页面 | 前缀 | 参数 |
|---|---|---|
| 全市场 `market.html` | `mk_` | `m` 市场(cn/us)　`t` 策略　`q` 搜索词　`b` 板块　`i` 行业　`g` 信号　`s` 排序列　`o` 方向(`a` 升 `d` 降)　`p` 页码(从 1 数) |
| 自选 `watch.html` | `wl_` | `m` 市场　`t` 策略　`d` 开始日期(YYYY-MM-DD)　`s` 排序列　`o` 方向 |
| 今日买卖 `signals.html` | `sg_` | `t` 策略　`d` 清单日期　`k` 标签(buy/sell/adjust/reduce/add)　`q` 搜索词　`l` 流动性　`s` 排序列　`o` 方向 |

规则：
- 只在用户动了控件之后才写网址；光打开页面不改地址栏（网址本来就带本页参数时例外，会把不合法的值清掉）。等于默认值的参数不写；市场（全市场、自选）和策略、标签（今日买卖）是视图的一部分，写一次就一直写。
- 网址里的值都是不可信输入：策略、排序列、信号、标签、流动性对照白名单，日期必须是真实存在的 YYYY-MM-DD，页码必须是正整数，搜索词截到 20 字；不合法的丢掉用默认值，不会原样发给接口。全市场页的板块 / 行业要等接口返回才知道有哪些，页码要等总数，不合法时丢掉并重取一次。
- 今日买卖页只有手动排序过才写 `s` / `o`；换标签、策略、日期会重置手动排序，参数也跟着消失（保持原来「按成交额自动排」的行为）。只给 `s` 不给 `o` 时，代码列升序，其余降序。
- 搜索框的网址写入延迟 250ms（浏览器对 `replaceState` 有调用频率限制，Safari 约 30 秒 100 次）；写入失败（沙箱 / 频率限制）静默放弃，不影响页面。
- 新加带排序 / 筛选的表格时：起一个没用过的两字母前缀，读出的值先过白名单再用，用 `tests/a08_probe.mjs`（假 DOM + 假接口）加一个场景。检查见 `tests/test_url_state_a08.py`。
- 没做的：持仓页（固定排序、没有筛选）、研究页（固定排序）、前向账本页（没有排序也没有筛选）；报告页用 `#hash` 记路径，不在本任务范围。

## 回测实验室的输入校验（A09）

`pages/backtest-validate.js`（纯函数，不碰 DOM）+ `pages/backtest.js`（把结果画在输入框下面、有错就禁用「运行回测」）。出错在输入框旁边就地提示，不用 alert。

规则照后端写（`app.py` 的 `_backtest`、`src/registry.py` 的 `clean_params`）。后端对越界数字不报错，而是悄悄夹到边界、整数参数悄悄四舍五入——结果就不是用户填的那个数，所以前端的原则是：**后端会改写的输入就报错，后端原样接受的输入不挡**。

| 输入 | 错误（挡住运行） | 不挡 |
|---|---|---|
| 策略参数 | 留空 / 不是数字 / 超出 registry 的 min~max / step 为整数的参数填了小数 | 非整数步长的参数不要求是步长整数倍（后端不要求） |
| 单边成本 bp | 不是数字 / 不在 0~100 | 留空（后端用默认成本，旁边显示默认值） |
| 开始日期 | 不是真实日期 / 半截日期 / 不早于最新数据日 | 留空（预热期之后开始）；比数据开头早（后端夹到预热期之后） |
| 品种 | 空 / 不在已知列表且格式不对（A 股 6 位 60/68/00/30 开头；美股字母开头 ≤10 位） | 列表里的任何品种 |
| 提醒（黄字，不挡） | 开始日离最新数据日不足 175 个自然日（约 120 个交易日，蒙特卡洛不出结果）；快线 ≥ 慢线 | |

回测没有单独的结束日期输入（接口也没有 `end` 参数），结束日永远是该品种最新数据日，所以「开始早于结束」和「结束不晚于最新数据日」合并成「开始日必须早于最新数据日」。最新数据日取自本页上一次回测返回的 `end`（按市场+品种记在 `dataEnds`），还没跑过的品种不做这项比较。词条在 `i18n/parts/A09.*.json`，合并进基础字典之前文字位是空的（不露键名），红框和禁用照常。检查见 `tests/test_backtest_validate_a09.py`（含 `tests/a09_probe.mjs` 假 DOM 探针）。

## 按钮 / 图标 / 可点击元素的无障碍与键盘可达（A11）

公共工具在 `shared/a11y.js`，只改行为不改样式：

- `activateOnKey(node, handler)`：带 `tabindex` 的表格行（全市场汇总表、个股页策略对比表）Enter 和空格都能触发；只响应焦点就在行本身的按键，行内的链接 / 按钮自己处理自己的；空格会 `preventDefault`，页面不跟着滚。
- `setAriaLabel(node, key, vars)` / `tMaybe(key)`：给每行都叫「添加」「移除」「撤销」的按钮补带对象的朗读标签（词条 `a11y.*`，见 `i18n/parts/A11.*.json`）。词条还没合并进基础字典时 `t()` 会原样返回键名，这时不加标签（按钮仍有自己的可见文字），不把键名读出来，和 A03 / A07 一致。
- `restoreFocusOnClose(dialog)`：原生 `<dialog>`（快捷键表、全局搜索、后台确认框）关闭后把焦点还给打开前的元素。`showModal()` 本身已提供 Esc 关闭和焦点陷阱。
- `closeOnEscape(node, handler)`：页内展开的确认区（持仓录入确认、撤销确认、总资金编辑）按 Esc 等于点「取消」。确认区打开时焦点落在不会造成后果的按钮上（「返回修改」「取消」）。

新加图标按钮的规则：只有图标没有文字的 `<button>` 必须有 `aria-label`（js 里走 `t()`，不写死中文；html 里用 `data-i18n-attr="aria-label:key"`）；可点击的东西优先用 `<button>` / `<a>`，不要用 `div` / `span`。检查见 `tests/test_ui_a11y_static.py`（静态扫描 + `tests/a11_probe.mjs` 假 DOM 探针）。

## 持仓录入表单的数字格式与费用提示（A15）

`pages/holdings-form.js` 的数量 / 价格 / 费用输入规则抽成纯函数放在 `pages/holdings-rules.js`（不碰 DOM，可单独测）：

- 三个框都是 `type=text` + `inputmode`（手机弹数字键盘；`type=number` 会把粘贴来的「1,000」悄悄清空）。提交前 `cleanNumber` 只去掉明确的千分位逗号，其他写法原样交给格式检查报错。
- 前端拦截的只有后端本来就会拒绝的：数量须为正整数且 ≤ 1 亿；价格 > 0、≤ 100000、最多 3 位小数；费用 ≥ 0、最多 2 位小数、不超过成交金额。不合法时不出确认摘要，逐项说明、`aria-invalid`、焦点移到第一个出问题的框。常量与 `src/holdings/store.py` 一致，由测试逐条对照。
- 整手只提示、不拦截：买入普通 A 股 / ETF 按 100 股（份）一手，科创板股票（688 / 689 开头）至少 200 股、之后 1 股递增，卖出不受整手限制；数量框下面的提示在不符合时换成「注意：…」，是否拒绝由后端（`HOLDINGS_BAD_LOT`）决定。
- 费用留空：买入 / 卖出由后端估算，占位文字「留空自动按费率估算」；**分红**留空记 0 元（后端不估算），占位、说明、确认摘要都按这个写，不再说「按费率估算」。
- 词条在 `i18n/parts/A15.*.json`，合并进基础字典之前提示不显示（不露键名），错误提示退回基础字典已有的「数量无效」等。检查见 `tests/test_holdings_form_a15.py`（含 `tests/a15_probe.mjs` 假 DOM 探针）。
