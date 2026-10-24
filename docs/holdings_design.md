# 持仓台账设计（T013 · S0）

状态：设计稿，尚未写代码。实现分 S1–S6 阶段，见本文第 7 节与 `handoff/reports/T013_plan.md`。

## 0. 目标与边界

- 目标：在个人模式网站里直接回答三个问题——**我现在持有哪几只股票、各持有多少股、什么时候买的**，并附带成本、盈亏、占总资金比例；所有数据由所有者在网页上手动录入、手动撤销。
- **只记录，不连接券商，不下单，不自动同步成交。** 台账记的是"我实际做了什么"，不是系统建议。
- 仅个人模式可见（`personal_mode.json` 的 `enabled` 严格为 `true`）；关闭个人模式时所有 `/api/holdings*` 路径表现为未知路径（404），页面入口消失，数据文件原样保留。
- 与现有 `members.py` / `simtrade.py` 的**模拟持仓完全独立**：不读、不写、不复用它们的表和数据。唯一复用的是 `simtrade.fees()` 的费率公式（由调用者注入，用于"费用留空时的估算"）。
- 只支持沪深 A 股与沪深 ETF（人民币）。北交所、港美股、期货、融资融券不在范围内。

## 1. 数据表

文件固定为 `data/holdings.db`（sqlite）。`data/*` 已在 `.gitignore`，不会上传。**导入模块不创建文件**；读接口在文件不存在时直接返回空台账，也不创建文件；**第一次写入**时才创建目录与文件。

### 1.1 trades（成交与调整记录）

| 列 | 类型 | 说明 |
|---|---|---|
| id | INTEGER PRIMARY KEY AUTOINCREMENT | 记录编号 |
| client_id | TEXT UNIQUE，可空 | 前端为每次打开表单生成的随机串（`[A-Za-z0-9_-]{8,64}`），用于重复提交幂等 |
| trade_date | TEXT NOT NULL | 成交日期 `YYYY-MM-DD`（北京日期） |
| code | TEXT NOT NULL | 6 位证券代码 |
| side | TEXT NOT NULL | `buy` / `sell` / `bonus`（送转股、拆股调整）/ `dividend`（现金分红调整） |
| qty | INTEGER NOT NULL | 股数。`bonus` 为新增股数；`dividend` 为登记日持股数 |
| price_milli | INTEGER NOT NULL | 单价，单位 0.001 元（整数，避免浮点误差）。`bonus` 固定 0；`dividend` 为**每股税后到账现金** |
| fee_cent | INTEGER NOT NULL DEFAULT 0 | 费用，单位 0.01 元（佣金+过户费+印花税合计；`dividend` 时为红利税等扣款） |
| fee_estimated | INTEGER NOT NULL DEFAULT 0 | 1 表示费用由系统估算，不是券商账单上的真实值 |
| signal_ref | TEXT，可空 | 来源信号，格式 `YYYY-MM-DD|策略|代码`，对应 `signal_ledger.signals` 的主键 `(day, strategy, code)`（该表没有单列数字 ID，所以用这个复合引用）；可不填 |
| note | TEXT NOT NULL DEFAULT '' | 备注，≤200 字符 |
| voided | INTEGER NOT NULL DEFAULT 0 | 软删除标记，1 = 已撤销 |
| voided_at | TEXT，可空 | 撤销时间 |
| created_at | TEXT NOT NULL | 创建时间（服务端 ISO 时间） |

规则：不物理删除、不修改已入账记录。更正一笔 = 撤销旧的 + 新增新的。索引：`(code, trade_date, id)`、`client_id`。

### 1.2 settings（总资金）

`settings(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)`。目前只有 `capital_cent`（总资金，单位分，整数）。**总资金是所有者手填的"账户总资产（含现金）"，系统不会根据盈亏自动调整**——它只用来算占比。

### 1.3 audit（只记动作，不记内容）

`audit(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, action TEXT NOT NULL, trade_id INTEGER)`，`action` 只能是 `add_trade` / `void_trade` / `set_capital` / `init_db`。

- **不记**代码、数量、价格、费用、总资金、备注。`trade_id` 只是流水号。
- 审计只写在 `holdings.db` 自己里面，**不写** `data/admin_audit.jsonl`（它在管理后台备份名单里，也能被后台读取），**不调用** `members.log`、`notify`、`logging`。

### 1.4 文件安全

- 创建用 `os.open(path, O_CREAT|O_EXCL|O_WRONLY|O_NOFOLLOW, 0o600)`，再交给 sqlite；打开前 `lstat`：符号链接、非普通文件一律拒绝（`HOLDINGS_STORE_UNSAFE`）；已存在但权限宽于 0600 时收紧为 0600。
- `journal_mode=DELETE`（不产生长期存在的 -wal/-shm 侧文件）。所有写入在 `BEGIN IMMEDIATE` 事务内，进程内再加一把 `threading.Lock`。全部参数化查询。
- 路径只在 `app.py` 里写死为 `ROOT/"data"/"holdings.db"`；`store.py` 的函数接收路径参数，测试传临时目录。

## 2. 计算口径

输入：未撤销（`voided=0`）的全部记录，按 `(trade_date, id)` 升序重放。同一天内以录入顺序（id）为准。**系统不强制 T+1**：台账记录的是已经发生的真实成交，由券商规则保证合规；系统只保证"不会卖出比持有更多"。

数值全部用 `Decimal`（内部精度 28 位），只在输出时四舍五入（金额 2 位、均价 3 位、比例 4 位，`ROUND_HALF_UP`）。

### 2.1 逐只股票的状态机

状态：`shares`（股数）、`cost`（持仓总成本，含费用）、`first_buy`（本轮首次买入日）、`realized`（已实现盈亏，含历史各轮）、`dividends`（现金分红累计）。

- **buy**：`cost += qty × price + fee`；`shares += qty`。若买入前 `shares == 0`，则 `first_buy = trade_date`（新一轮开始）。
- **sell**：`avg = cost / shares`；`proceeds = qty × price − fee`；`realized += proceeds − qty × avg`；`cost −= qty × avg`；`shares −= qty`。平均成本不因卖出改变；若 `shares` 归零，`cost` 置精确 0，`first_buy` 清空，该轮写入"已清仓历史"。
- **bonus**（送转股）：`shares += qty`，`cost` 不变（均价被摊薄）。持股为 0 时拒绝。
- **dividend**（现金分红）：`dividends += qty × price − fee`；**不改 shares 和 cost**。`qty` 不得大于该日重放后的持股数。

> 分红口径选择：现金分红单独列示，不冲减成本。另一种常见做法（冲减成本）会让均价偏低、盈亏看起来更好；这里选择"成本只由真实买入费用决定"，更保守也更易核对。总盈亏 = 浮动盈亏 + 已实现盈亏 + 现金分红。

### 2.2 展示指标

| 指标 | 公式 |
|---|---|
| 平均成本（含费用） | `cost / shares` |
| 首次买入日 | 本轮 `first_buy`（卖光后再买，重新开始） |
| 持有天数 | `today_beijing − first_buy` 的自然日数（当天买入 = 0） |
| 现价 | 调用者注入的 `last_price_fn(code)` 返回的最近收盘价及其日期；取不到则为 `null` |
| 市值 | `shares × 现价` |
| 浮动盈亏 | `(现价 − 平均成本) × shares`，等于 `市值 − cost`；**不扣预估卖出费用**（页面注明） |
| 浮盈比例 | `浮动盈亏 / cost` |
| 已实现盈亏 | 该股票全部历史各轮卖出的累计（已扣买入摊销成本与卖出费用） |
| 占总资金比例 | `市值 / 总资金`；总资金未设置或现价缺失时为 `null` |
| 已投入占比 | `Σ市值 / 总资金` |

**缺失就是缺失**：现价取不到时，市值、浮盈、占比都是 `null`，页面显示"价格不可用"，不用成本价、不用 0 顶替；汇总里单独列出"缺价格的持仓数"。现价取自已有日线缓存（收盘价，非实时），响应里带 `price_date`，页面显示价格日期，旧于 3 个交易日时标注"价格偏旧"。

### 2.3 校验与重放

任何会改变历史的操作（新增一笔、撤销一笔）都在同一事务里对**全部**未撤销记录整体重放；任一时点出现卖出数量大于持股、`bonus`/`dividend` 面对零持股或超持股，整笔操作拒绝：

- 新增：补录过去日期的买入/卖出同样触发整体重放，所以"补一笔更早的卖出"如果让之后某天持股为负，会被拒。
- 撤销：撤销一笔买入如果会让之后的卖出变成超卖，拒绝（`HOLDINGS_VOID_BREAKS_HISTORY`），提示先撤销后面的卖出。
- 软删除后的所有指标（均价、首次买入日、已实现盈亏）都由重放得出，没有任何"累计缓存"，因此不会与历史不一致。

### 2.4 算例（测试用，手算可复核）

买 1000 股 @10.000，费用 5.00 → cost 10005.00，均价 10.005。
再买 500 股 @11.000，费用 5.00 → cost 15510.00，shares 1500，均价 10.340。
卖 600 股 @12.000，费用 6.50 → proceeds 7193.50；摊销成本 6204.00；已实现 989.50；剩 900 股，cost 9306.00，均价仍 10.340。
现价 11.50：市值 10350.00，浮盈 1044.00（11.22%）；总资金 100000 → 占比 10.35%（>10% 页面标红）。
首次买入 2026-10-01，今天 2026-10-22 → 持有 21 天。
10 送 5 → `bonus` qty 450：shares 1350，cost 9306 不变，均价 6.893。

## 3. 接口

全部在 `/api/holdings*` 下，**只在个人模式开启时存在**。处理函数是纯函数（S2），`app.py` 只负责鉴权与分派（S3）。

| 方法 路径 | 作用 | 请求 |
|---|---|---|
| GET `/api/holdings` | 持仓汇总 + 当前持仓 + 已清仓历史 + 成交列表 | 查询串：`trades_limit`（默认 200，最大 1000）、`include_voided`（0/1，默认 0） |
| POST `/api/holdings/add` | 新增一笔成交或调整记录 | JSON，见 3.2 |
| POST `/api/holdings/void` | 撤销一笔（软删除） | `{"id": 整数}` |
| POST `/api/holdings/capital` | 设置总资金 | `{"capital": 数字}` |

### 3.1 访问控制（与现有模式一致，且更严）

按下列顺序判断，先命中先返回：

1. 个人模式关闭 → 不分派，落到现有未知路径逻辑（经鉴权后 404）。
2. 非可信本机（`admin.is_local_request` 为假：Host 不在 `ALLOWED_HOSTS`、带任何代理/隧道头，或 `_remote()` 即 trycloudflare 域名）→ **404 `not found`**（沿用 `/api/admin`、`/api/ai` 的"装作没有"风格）。分享口令的 cookie（`qb_share`）**不能**换取访问：该判断发生在 `_share_gate` 之前，且不看分享 cookie。
3. 网站令牌 cookie 无效（`_authed()` 为假）→ 401 `unauthorized`（与现有一致）。个人模式下会员登录入口被隐藏，所以这里的"有效会话"就是网站令牌 cookie，不依赖会员账户。
4. 缺 `X-Requested-With: dashboard`（**GET 也要求**，现有 GET 多数不要求；这里更严）→ 403。
5. POST 额外要求 `admin.origin_ok`（Origin 缺省或为本机页面）→ 否则 403。
6. POST 请求体 Content-Length 必须在 `1..16384`，否则 413/400。JSON 解析失败 400。
7. 简单频率限制：写操作每分钟最多 60 次（`members.hit("holdings_write", "local", 60, 60)`，纯内存，不落盘）→ 429。

所有响应沿用 `_send` 的 `Cache-Control: no-store`；不设 CORS 头。

### 3.2 POST /add 字段与校验

严格白名单；**出现未知键 → 拒绝**；缺必填 → 拒绝；类型错 → 拒绝（不做宽松转换，数字可以是 JSON 数字或字符串，经 `Decimal` 解析，拒绝 NaN/Infinity/科学计数法之外的怪异格式）。

| 字段 | 必填 | 规则 |
|---|---|---|
| trade_date | 是 | 合法 `YYYY-MM-DD`；不得晚于北京日期的今天（服务器可能在美西，必须用 `Asia/Shanghai` 的今天）；不早于 1990-12-19 |
| code | 是 | 6 位数字；前缀必须属于沪深股票（60/68/00/30）或沪深 ETF（与 `simtrade.kind_of` 同一批前缀：510/511/512/513/515/516/517/518/56/588/589/159）；其他（含北交所 8/4/92）拒绝 |
| side | 是 | `buy`/`sell`/`bonus`/`dividend` |
| qty | 是 | 正整数，≤ 100,000,000。`buy`：普通代码须为 100 的整数倍；**科创板 688/689 须 ≥200 股**（之后任意整数股）；`sell`：任意正整数（允许零股，且不得超过持股）；`bonus`：任意正整数 |
| price | 是（`bonus` 必须为 0 或省略） | `Decimal` > 0，≤ 3 位小数，≤ 100000；`dividend` 为每股税后现金，可小至 0.001 |
| fee | 否 | `Decimal` ≥ 0，≤ 2 位小数，≤ 成交金额；`bonus` 固定 0。**留空 → 按注入的费用函数估算并置 `fee_estimated=1`**（现用 `simtrade.fees()`，其 `FEE_VERIFIED=False`，页面注明"估算，建议填券商账单上的真实费用"） |
| signal_ref | 否 | 匹配 `^\d{4}-\d{2}-\d{2}\|[A-Za-z0-9_]{1,32}\|\d{6}$`，其 code 必须等于 `code` |
| note | 否 | 字符串 ≤200，去除控制字符 |
| client_id | 否 | `^[A-Za-z0-9_-]{8,64}$`；同 client_id 且内容相同 → 返回已有记录（200，`duplicate: true`）；同 client_id 但内容不同 → 409 |

POST /void：`id` 必须是存在、未撤销的整数；重复撤销 → `HOLDINGS_ALREADY_VOIDED`。POST /capital：`Decimal`，> 0，≤ 1,000,000,000，≤ 2 位小数。

### 3.3 响应与错误码

成功：`{"ok": true, ...}`；GET 返回结构：

```
{ "as_of": "YYYY-MM-DD", "capital": 金额|null,
  "summary": {市值, 总成本, 浮动盈亏, 已实现盈亏, 现金分红, 已投入占比, 缺价格持仓数},
  "positions": [{code, name, shares, avg_cost, first_buy, held_days, price, price_date, market_value, floating_pnl, floating_pct, weight, realized, dividends}],
  "closed": [{code, name, first_buy, last_sell, held_days, pnl}],
  "trades": [{id, trade_date, code, name, side, qty, price, fee, fee_estimated, signal_ref, note, voided, created_at}] }
```

响应只含上述字段，不回显请求原文。`name` 由调用者注入的名称函数提供，取不到时等于代码；不存库。

失败：`{"error": "HOLDINGS_…", "message": "固定中文提示", "field": "字段名(可选)"}`。**`message` 来自固定码表，绝不拼接用户输入或异常文本；不含数量、价格、代码。**

| HTTP | error | 触发 |
|---|---|---|
| 404 | （纯文本 not found） | 外部/代理/分享访问；个人模式关闭 |
| 401 | （纯文本 unauthorized） | 令牌 cookie 无效 |
| 403 | HOLDINGS_FORBIDDEN | 缺 `X-Requested-With` 或 Origin 不对 |
| 400 | HOLDINGS_BAD_JSON / HOLDINGS_UNKNOWN_FIELD / HOLDINGS_MISSING_FIELD | 请求体结构 |
| 400 | HOLDINGS_BAD_CODE / _BAD_SIDE / _BAD_DATE / _FUTURE_DATE / _BAD_QTY / _BAD_LOT / _BAD_PRICE / _BAD_FEE / _BAD_NOTE / _BAD_SIGNAL_REF / _BAD_CAPITAL / _BAD_ID | 字段校验 |
| 400 | HOLDINGS_OVERSELL | 重放出现卖出大于持股 |
| 400 | HOLDINGS_NO_POSITION | `bonus`/`dividend` 无持股或超持股 |
| 400 | HOLDINGS_VOID_BREAKS_HISTORY | 撤销会让后续记录非法 |
| 400 | HOLDINGS_ALREADY_VOIDED | 重复撤销 |
| 404 | HOLDINGS_TRADE_NOT_FOUND | 撤销不存在的编号 |
| 409 | HOLDINGS_CLIENT_ID_CONFLICT | 同 client_id 不同内容 |
| 413 | HOLDINGS_TOO_LARGE | 请求体过大 |
| 429 | HOLDINGS_RATE_LIMITED | 写操作过频 |
| 500 | HOLDINGS_STORE_UNSAFE | 符号链接/非普通文件 |
| 500 | HOLDINGS_INTERNAL | 其他异常（不含异常文本） |

## 4. 前端

### 4.1 导航与页面

- `PERSONAL_NAV` 追加第 6 项 `["holdings", "持仓"]`（英文 "Holdings"）；`PERSONAL_PAGES` 同步加入。`PAGES` 与页面加载表加入 `holdings`，但 `route()` 里加保护：非个人模式访问 `#/holdings` 一律回 `#/today`，且不发任何 `/api/holdings` 请求。完整模式的导航、DOM、脚本行为不变。
- `index.html` 新增 `<section class="page" id="p-holdings">`，按以下区块排列（移动端单列、16px 边距、无横向滚动，宽表格复用现有 `personal-signal-table` 卡片化做法）：
  1. **汇总**：总资金（点"修改"就地编辑）、持仓市值、已投入占比、浮动盈亏、已实现盈亏、现金分红；缺价格时显示"n 只价格不可用"。
  2. **当前持仓表**：代码、名称、持有股数、平均成本、现价（含价格日期）、浮动盈亏及比例、占总资金比例、首次买入日、持有天数；默认按占比降序；占比 > 10% 标红（先作展示，与 T012 的单票 ≤10% 口径一致）。
  3. **录入成交**：日期（默认北京今天）、代码（输入 6 位后用已有名称接口只读提示名称；查不到也允许录入）、买/卖/送转/分红、数量、价格、费用（可留空，旁注"留空将按费率估算"）、备注、可选来源信号；点"提交"先显示**确认摘要**（"买入 600xxx 名称 1000 股 @10.000，费用估算 5.00"），再点"确认入账"才发请求；成功后清空表单并换新的 `client_id`。
  4. **成交历史**：倒序，含"显示已撤销"开关；每笔"撤销"按钮，点击后**二次确认**；已撤销的灰显删除线。
  5. **已清仓历史**（折叠）。
  6. 一行说明："仅记录，不连接券商、不下单；数据只存在本机 data/holdings.db。"
- 实现约束：所有数据以 `textContent` / `createElement` 渲染，禁止 `innerHTML` 拼接持仓数据；请求使用专用 helper，每次带 `X-Requested-With: dashboard`、`credentials: same-origin`；**不使用** `PERSONAL.cache`（该缓存按市场和路径保留 Promise，不适合私人数据）、不写 `localStorage`/`sessionStorage`/`IndexedDB`/`document.cookie`、不挂到 `window` 全局、不 `console.log` 数据；离开 `#/holdings` 页面时清空内存里的台账快照；请求失败只显示固定错误文案，不显示原始异常文本以外的用户数据。
- i18n：新增键沿用个人页的 `pCopy(zh, en)` 模式与 `i18n.js`；错误码到中英文文案的映射放前端表中，缺映射时显示通用文案。

### 4.2 今日 / 信号页的只读联动（S5）

- 仅个人模式、仅 A 股（`S.market === "cn"`）、仅在进入信号页/今日页时拉取**一次** `GET /api/holdings`，结果放在模块内存变量里（不进缓存层、不进信号数据对象）。
- **卖出/减仓信号且你持有该股**：该行在列表中置顶（只调整展示顺序，不改变数据对象），并在名称旁追加一行文字：`你持有 X 股 · 成本 Y · 持有 N 天 · 浮动盈亏 Z`（`textContent`）。
- **卖出/减仓信号但你不持有**：降低视觉权重（加 CSS 类，例如降低不透明度），标注"未持有"。
- **买入/加仓信号且已持有**：标注"已持有 X 股"，不改变排序。
- **台账不可用时（401/404/网络错误/空库错误）不得误标"未持有"**：此时只显示一次"持仓不可用"提示并保持原样。空台账（成功返回、无持仓）才是"全部未持有"，且此时不降低权重，只标注一次"台账为空，尚未录入持仓"，避免初次使用时整页变暗。
- 注释节点不进入 `tradeCSVData()` / XLSX 导出：导出函数读的是 `TRADE.data` 的数据对象，S5 不得往这些对象里写任何持仓字段；用测试断言。
- 盯盘（T006 S3）尚未接入，只在持仓页预留一行"盯盘盈亏：尚未接入"占位说明，不采集行情。

## 5. 隐私边界

持仓是私人财务信息。逐项边界：

| 出口 | 做法 | 验证 |
|---|---|---|
| 网络访问 | 仅可信本机（Host 在 `ALLOWED_HOSTS`、无任何代理/隧道头、非 trycloudflare），外部一律 404；分享口令不授予访问；不设 CORS | S3 测试：分享 Host、代理头、分享 cookie 均 404 |
| 鉴权 | 网站令牌 cookie + `X-Requested-With: dashboard`（GET/POST 都要）+ POST 的 Origin 检查 | S2/S3 鉴权矩阵 |
| 离线整站导出 | `/api/offline` 调用 `export_offline.py`，它不导入 holdings；离线页的请求桩对 `/api/holdings` 返回"不支持"；离线 meta 没有 `personal_mode` | 静态扫描 `export_offline.py`、`daily.py`、`notify.py`、`trade_notifications.py` 不含 `holdings`；S3/S5 测试断言离线桩无持仓 |
| 分享页面 | 个人模式下 `_share_gate` 已让外部请求 404；`/api/holdings*` 另有自己的外部拦截 | S3 测试 |
| 日志 | `Handler.log_message` 已静默；持仓代码不使用 `logging`/`print`；错误只用固定码，不含异常文本 | 静态扫描 `src/holdings/` 无 `print(`/`logging`；故意制造异常的测试断言响应与 stderr 不含数值 |
| 审计 | 仅 `holdings.db` 内的 `audit` 表：动作 + 流水号 + 时间；不写 `admin_audit.jsonl`、不调用 `members.log` | 测试：新增/撤销后检查 `audit` 列与 `admin.AUDIT` 文件未变化 |
| 通知文本 | `trade_notifications` 与 `trade_private.mine` 只用会员模拟持仓与自选，T013 不接入 | 静态扫描；见 S5 |
| 管理后台备份 | `admin.BACKUP_FILES/BACKUP_DIRS` 是白名单，不含 `data/holdings.db`（已核对）；备份由所有者自己复制文件 | S6 文档说明；测试断言白名单不含 holdings |
| 浏览器 | 无 localStorage/sessionStorage/IndexedDB/cookie 持久化；`Cache-Control: no-store`；不挂 `window` 全局；退出页面清内存 | S4/S5 静态检查 |
| 文件系统 | 0600、拒绝符号链接、`data/*` 已被 git 忽略；不在仓库任何文件里写示例真实数据（测试用合成数据） | S1 测试；`git status` 检查 |
| 联网 | 持仓代码不发起任何网络请求；价格由调用者从已有本地缓存注入 | 静态扫描 `src/holdings/` 不导入 `urllib/requests/http/socket` |
| 测试 | 全部用 `tempfile` 临时目录与合成数据；不读写真实 `data/` | 现有 `realdata_guard` 思路；见风险 R7 |

没有覆盖的威胁（如实写出）：已登录浏览器被他人使用、本机被入侵、磁盘镜像/Time Machine 备份会包含 `data/holdings.db`、网站令牌 cookie 有效期 1 年。需要时可在后续任务加"持仓页二次口令"，本任务不做。

## 6. 测试清单

- **S1 `tests/test_holdings_core.py`**：加权均价手算（2.4 算例）；卖出后剩余；卖光再买重置首次买入日；超卖拒绝；补录更早日期触发整体重放；软删除后重算；撤销买入致后续卖出超卖被拒；零股卖出；`bonus` 摊薄均价、`dividend` 不改成本；非法输入矩阵（代码/方向/数量/lot/价格/费用/日期/未来日期/科创板 200 股）；符号链接与目录拒绝；已有宽权限文件被收紧；导入模块与读空库不创建文件；首次写入创建 0600 文件；重复 `client_id` 幂等与冲突；审计表无金额列；Decimal 无浮点漂移（0.1+0.2 类用例）。
- **S2 `tests/test_holdings_api.py`**：鉴权矩阵（非本机/缺头/未认证/Origin 不对，各自固定响应，且不泄露台账是否存在）；校验矩阵与错误码表一一对应；响应不含多余字段、不回显输入；未知键拒绝；幂等；空库；价格缺失时的 `null`；异常路径响应不含数值。
- **S3 `tests/test_holdings_wiring.py`**：沿用 `tests/test_personal_mode.py` 从 `app.py` 抽取 `Handler` 的方式（不导入真实 `app.py` 初始化）；个人模式关/开/再开；关闭时与未知路径一致；分享 Host、代理头、分享 cookie → 404；缺头 403；离线导出不含持仓；原有 `test_basic.py`、`test_personal_mode.py` 仍通过。
- **S4 `tests/test_holdings_frontend.py`**：导航第 6 项仅个人模式；无 `innerHTML` 拼接持仓；无 localStorage 等持久化；请求头；i18n 键齐全；`node --check`；非个人模式访问 `#/holdings` 回今日。
- **S5 `tests/test_holdings_signals_link.py`**：联动逻辑（持有的卖出置顶、未持有降权、已持有标注、台账不可用不误标）；导出函数不含持仓字段。
- **S6**：全量自查（密钥、明文金额日志、默认关闭行为不变）。

## 7. 阶段、验收点与风险

| 阶段 | 内容 | 验收点 |
|---|---|---|
| S1 | `src/holdings/{__init__,store,calc}.py` + 核心测试 | 算例与手算一致；超卖/软删除/幂等测试全过；导入不建文件；0600 与符号链接；无网络导入、无 print/logging |
| S2 | `src/holdings/api.py` + 接口测试 | 鉴权矩阵与校验矩阵全过；错误码与第 3.3 节一致；响应不回显输入 |
| S3 | `app.py` 最小接入 + 接线测试 | 个人模式关时与未知路径一致；外部/分享/代理一律 404；`test_basic.py` 与 `test_personal_mode*.py` 仍过；`git diff app.py` 只含接入代码；建议所有者早上亲眼验收 |
| S4 | 持仓页 | 静态检查全过；真实浏览器上手机宽度无横向滚动（W4 同类未验证项，需所有者目验） |
| S5 | 信号页联动 | 台账不可用不误标；导出不含持仓 |
| S6 | `docs/holdings_howto.md` + 全量自查 | 全部测试通过；自查结果写入汇报 |

风险与对策见 `handoff/reports/T013_plan.md` 第 5 节。
