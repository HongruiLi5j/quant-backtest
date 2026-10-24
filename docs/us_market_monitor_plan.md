# 美股盯盘方案（T015 S0，仅设计，未写产品代码）

状态：S0 只读盘点与方案。镜像 T006 A 股盯盘的架构，新建独立模块 `src/us_market_monitor/`，不改动 `src/market_monitor/`。
默认关闭、仅个人模式、仅可信本机、只记录不下单、不构成投资建议。

## 0. 盘点结论（T006 现状，S0 读过的文件）

| T006 文件 | 作用 | 与市场相关的硬编码（美股必须替换） |
|---|---|---|
| core.py | Quote 数据类、validate_quote、alert_step、continuous_session | `valid_symbol` 只认 sh/sz 加六位代码，且限定号段前缀（sh 600/601/603/605/688，sz 000/001/002/003/300/301，`core.py:12`）；`continuous_session` 写死北京 9:30–11:30/13:00–15:00；`BEIJING` 时区；源时间 14 位字符串 |
| providers.py | 腾讯 HTTP 适配（GBK、`~` 分隔、SIGALRM 时限、不跟随重定向） | 全部是腾讯私有格式，美股不可复用，需要新的 yfinance 适配器 |
| service.py | S2 的 5 只会话；`process_request` 在独立 spawn 进程里跑 Provider，父进程硬截止 | `symbol_for` 六位代码；`process_request` 的子进程/硬截止机制**可原样借鉴**，它与市场无关 |
| store.py | 目录 0700/文件 0600、逐层 O_NOFOLLOW、独占创建 | `STORE_ROOT=data/market_monitor`；机制可直接复用 |
| scheduler.py | 后台线程、预算、间隔、暂停、告警、落盘 | `_eligible` 用 A 股时段；`symbol_for`；`WATCH_STORE_ROOT`；MAX_WATCH=60、间隔 30–3600 |
| watchlist.py | 持仓+自选+信号合并，上限 60，降级为空 | `_valid_code` 六位数字；三个来源函数都是 A 股的 |
| signals_readonly.py | 读 `data/daily/trade_list_cn.json` | 只有 cn；`trade_list.py` 的 `_market()` 对非 cn 直接拒绝 |
| intraday.py | 执行提醒 / 信号追踪 / 新机会 | `limit_pct/limit_state/distance_to_limits`（涨跌停）、手→股×100、开盘 5 分钟窗口按北京时间 |
| wiring.py（`Ctx`、`_gate`、`handle_*`）与 api.py | 纯函数 + `Ctx` 四重门禁（可信本机/已登录/dashboard 头/写操作 Origin），失败统一 403；`api.py` 与 `wiring.py` 各有一份 `_pairs` | 与市场无关，**从 `market_monitor.wiring` 导入 `Ctx`、`_gate`、`FORBIDDEN/INVALID`、`_pairs`**（不要从 `api.py` 导入，避免两份 `_pairs` 混用） |
| app.py 里的接线 | `MONITOR_ROUTES`、`market_monitor_scheduler()` 懒构造、个人模式关闭时 404 | 需要另加一组 `/api/us_market_monitor/*` |

另外盘点到的两个关键事实（决定了美股 watchlist 的来源）：
1. `src/trade_list.py` 的 `_market()` 对非 `cn` 抛错，只发布 `trade_list_cn.json`，**当前没有美股"今日买卖清单"**。
2. `src/holdings/store.py` 用 `\d{6}` 校验代码，**T013 持仓台账不支持美股代码**（也没有币种）。
3. 项目里已有美股侧：`config.py` 的 `us` 市场、`datasource.load_watchlist('us')`、`src/usdata.py`（本地 Parquet 日线）、`src/scan_us.py`、`data/*.csv` 的 ETF 日线。美股自选可用，日线均量可读本地缓存。
4. `.venv` 里已装 `yfinance 1.7.0`（含 pytz/tzdata），不需要新增依赖；S0 没有 import、没有联网。

## 1. 架构草图

```
src/us_market_monitor/            (新建，全部 US_* 语义，互不改动 src/market_monitor/)
  core.py        USQuote、us_symbol 校验、us_session()（美东时区）、validate_us_quote
  providers.py   YFinanceProvider（首选）、PolygonProvider（备选，S 阶段后期可选）
  worker.py      spawn 子进程入口：子进程内 import yfinance，只返回白名单字段（价格/昨收/开盘/成交量/时间）
  store.py       复用 market_monitor.store 的 directory()/StorageError，仅改根目录
  watchlist.py   build_us_watchlist()
  scheduler.py   USMonitorScheduler（结构照搬 MonitorScheduler，换时段/代码/提供者）
  intraday.py    美股版执行提醒/信号追踪/新机会
  wiring.py      复用 market_monitor.wiring 的 Ctx/_gate，只写美股 handle_*
scripts/validate_us_feed.py       所有者手动运行的联网验证脚本（S 阶段后期，本阶段不写）
```

**直接复用（import，不复制）**：`market_monitor.wiring.Ctx/_gate/FORBIDDEN/INVALID/_pairs`、`market_monitor.store.directory/StorageError`、`alert_step/AlertState/Condition`（价格穿越逻辑与市场无关）、`service.process_request` 的"独立 spawn 进程 + 父进程硬截止 + 取消"模式（写成美股自己的函数，不改原函数）。
**新写（US_*）**：代码校验、时段、提供者、watchlist、intraday 三块、路由、前端页。
**不复用**：`valid_symbol`、`continuous_session`、`limit_*`、`TencentProvider`、`signals_readonly`。

为什么新建而不是给 `market_monitor` 加参数：A 股盯盘已通过 S0–S3d 审阅，改动会让已验收代码重新进入风险面；两个市场的时段、代码、数据源、规则差异大，分开更易审、可独立开关。代价是约 30% 代码相似，接受。

### 1.1 接口草案（签名 + 最小测试清单，S1–S4 照此写）

下面是草案，不是已实现代码。所有函数无副作用、不联网；时间参数必须带时区，否则 `ValueError`（与 `continuous_session` 一致）。

**S1 `core.py`**

| 接口 | 签名 / 语义 | 最小测试 |
|---|---|---|
| `valid_us_symbol` | `(s: object) -> bool`；正则 `[A-Z]{1,5}([.-][A-Z]{1,2})?`，非 str 返回 False，不抛错 | 通过：`AAPL`、`BRK-B`、`BF.B`、`A`。拒绝：`^GSPC`、`ES=F`、`aapl`、`AAPL `、`''`、`TOOLONGX`、`AAPL-ABC`、`ＡＡＰＬ`（全角）、`None`、`123` |
| `us_session` | `(at: datetime) -> bool`；转 `America/New_York` 后周一至周五且 `09:30 <= t <= 16:00` | 周一 09:30:00 True、09:29:59 False、16:00:00 True、16:00:01 False；周六/周日 False；无时区抛 `ValueError`；**夏令时切换日**：2026-03-08（周日，休市，False）、2026-03-09 周一（EDT）用 UTC 13:30 为 True；2026-11-01 周日 False、2026-11-02 周一（EST）用 UTC 14:30 为 True 而 13:30 为 False（证明没有手写 UTC 偏移） |
| `us_trading_date` | `(at: datetime) -> date`；美东日期 | 北京 2026-10-23 02:00（美东 10-22 14:00）返回 10-22 |
| `USQuote` | frozen dataclass：`symbol: str`、`price: float\|None`、`previous_close: float\|None`、`opening: float\|None`、`volume: int\|None`（股）、`source_time: datetime\|None`（带时区）、`received_at: datetime`、`quality: str`、`reasons: tuple[str,...]` | 构造后不可变；`volume` 是 bool/负数/小数 → 校验时标坏 |
| `validate_us_quote` | `(q: USQuote, now: datetime, max_age: float = 1200.0) -> USQuote`；返回带 `quality` 与 `reasons` 的新对象。`quality ∈ {'ok','stale','after_hours','bad'}` | 价格 0/负/NaN/inf/bool → `bad`；昨收缺失 → `bad`；`source_time` 晚于 16:00 美东 → `after_hours`；`now - source_time > max_age`（默认 20 分钟，覆盖 15 分钟延迟）→ `stale`；`source_time` 在 `now` 之后超过 5 秒 → `bad` |

**S2 `providers.py` / `worker.py`**

- Provider 调用约定：`fetch(symbols: tuple[str,...], timeout: float) -> list[USQuote] | ProviderError`。第一版 `len(symbols)==1`；多代码需 `multi_code` 开关且经真实验证才允许（见 6.1）。
- 失败分类（枚举，固定集合，不回传上游异常文本）：`timeout`、`rate_limited`（HTTP 429 或 yfinance 的限流异常）、`empty`（空 DataFrame / 全 NaN）、`bad_response`（字段缺失、类型不符）、`network`、`service_error`。
- 子进程回传白名单字段，且只有数值/ISO 时间：`symbol`（回显并与请求核对）、`price`、`previous_close`、`opening`、`volume`、`source_time_epoch`（秒，UTC）。名称、异常字符串、原始 JSON 一律不回传。
- 最小测试（全用注入的假 Provider / 假 worker，不 import yfinance、不联网）：429 → `rate_limited`；空结果 → `empty`；子进程 sleep 超过 timeout+2 秒 → 被终止并返回 `timeout`，且测试内用 `os.kill(pid, 0)` 断言进程已消失；回传多余字段被丢弃；回显代码与请求不符 → `bad_response`；字段含 `NaN`/字符串价格 → 被 `validate_us_quote` 判 `bad`。

**S3 `watchlist.py` / `scheduler.py` / `store.py`**

- `build_us_watchlist(top_n: int = 0, *, holdings_fn=None, watch_fn=None, signals_fn=None) -> dict`，返回 `{'codes': list[str], 'sources': dict[str,str], 'truncated': bool}`；顺序 持仓 > 自选 > 信号，去重，截断到 `MAX_US_WATCH = 30`。来源函数抛错 → 该来源降级为空，不影响其他来源。测试：三源重复代码只留一份且保留最高优先级来源；31 个自选被截断且 `truncated=True`；非法代码（`^GSPC`）被丢弃；`top_n` 为 bool/负数/超过 30 → `ValueError`。
- `USMonitorScheduler`：与 `MonitorScheduler` 同构（`start/stop/status/quotes`，注入 `clock`、`provider`、`store`）。测试：非 `us_session` 时间不发请求；连续 3 次失败暂停；`rate_limited` 立即暂停并退避；预算用尽后 `budget_exhausted`；进程重启后状态为已停止。
- `store`：根目录 `data/us_market_monitor/`，复用 `market_monitor.store.directory()`。测试（临时目录）：目录 0700、文件 0600、符号链接拒绝、独占创建。

**S4 `intraday.py`**

- `execution_alerts(rows, now) -> list[dict]`、`signal_tracking(signals, quotes, now) -> list[dict]`、`new_opportunities(quotes, daily_stats, now) -> list[dict]`，均为纯函数。测试：阈值严格大于（刚好 3% 不触发，3.0001% 触发）；成交量不乘 100；价格 < 5 美元或 20 日均成交额 < 1000 万美元只展示不提示；均量缺失 → "数据不足"；开盘 5 分钟窗口 09:30:00–09:35:00 美东含端点，以 UTC 构造跨夏令时两天验证。

## 2. 美股开盘时段检测

- 使用 `zoneinfo.ZoneInfo('America/New_York')`（标准库，自动处理夏令时；tzdata 已装），**不手写 UTC 偏移**。
- 常规时段：周一至周五 09:30–16:00（美东）。**边界定死（S1 测试按此写）**：`us_session` 与 A 股 `continuous_session`（`core.py:23`，`9:30 <= t <= 11:30`、`13:00 <= t <= 15:00`）一样**两端都含**，即 09:30:00 与 16:00:00 返回 True，09:29:59 与 16:00:01 返回 False。它只是钟表提示，决定"是否发请求"。另有一条独立的数据规则：行情自带时间戳（美东）晚于 16:00:00 的视为盘后数据，标为 `after_hours`、不触发提醒。两条规则不矛盾：请求窗口含 16:00，是为了拿到收盘那一笔；盘后时间戳的行情不用。盘前/盘后不采集（用户指令：本阶段只做常规时段）。
- 对照：夏令时（约 3 月第 2 个周日至 11 月第 1 个周日）= 洛杉矶 06:30–13:00 = 北京 21:30–04:00；标准时间 = 北京 22:30–05:00。**任务书写的"北京 22:30–05:00"只在冬令时成立**，夏令时少一小时，文档与页面一律以美东时间为准并显示北京时间换算。今日（2026-10-22）仍是夏令时（2026-11-01 结束），所以明晚实际是北京 21:30 开盘。
- 节假日：沿用 T006 的做法——不内置交易日历，靠"行情时间是否过旧"拦截（周末/休市日 yfinance 返回上一交易日时间，会被判过旧）。半日市（感恩节次日、平安夜 13:00 收盘）同理。S 阶段可选加一份手写的当年休市日常量表，但必须标注"手工维护"，默认不依赖。
- 跨日问题：北京时间的"日期"在美股时段内会跨零点。所有落盘文件名、`_today_samples`、"当日"判断一律用**美东日期**，避免 jsonl 被切成两天。

## 3. watchlist 来源

优先级沿用 持仓 > 自选 > 信号，总数硬上限仍为 60（美股默认更小，建议 30，见需决定事项）。

| 来源 | 现状 | 方案 |
|---|---|---|
| 美股持仓 | T013 台账只支持六位 A 股代码 | **第一版返回空集**；不改 `src/holdings/*`（不在 T015 范围）。如要支持，需另开任务扩展台账 |
| 美股自选 | `datasource.load_watchlist('us')` 已存在 | 读取，逐个校验代码格式，失败降级为空 |
| 美股信号 | 无 `trade_list_us.json` | **第一版返回空集**；接口留好（`signals_fn` 可注入），将来有美股清单再接 |

代码校验：`^[A-Z]{1,5}$`，可选一个 `.` 或 `-` 后接 1–2 个大写字母（BRK-B、BF.B）。严格拒绝 `^`、`=`（指数/期货/外汇）、空格、小写、超长、非 ASCII，防止把任意字符串拼进上游请求。内部统一用 Yahoo 写法（`BRK-B`）。

初始 watchlist 实际上就是自选，因此"没有持仓、没有信号"时页面要明确显示"仅自选"，不要显示成故障。

## 4. 三块实时分析的美股差异

### A. 执行提醒
- **没有 A 股式涨跌停**：删除"涨停不可买/跌停不可卖"。取而代之：
  - LULD 熔断（个股 5 分钟内偏离参考价 5%/10%/20%/…的暂停，随价格档位而异）无法从 yfinance 可靠得知，**不做识别**，只在文档说明；若行情长时间不变则由"数据过旧"提示。
  - 保留"持仓异常波动"思路但阈值改为可配置，默认单日跌幅 > 7%（美股个股波动比 A 股主板 10% 限制下更自由）。第一版持仓为空，该规则默认无输出。
  - "买入窗口打开"：开盘后前 5 分钟（美东 09:30–09:35），仅在有买入信号时输出；第一版无信号，输出为空。
- 可做空：T+1 限制不存在，**不做** A 股"当日买入不可卖"类提示；本盯盘不下单，不输出做空建议。
- 盘前盘后：不采集；开盘第一笔报价与昨收的缺口只作为"跳空 %"信息展示，不当信号。

### B. 信号追踪
- 美股暂无信号来源，**第一版只实现框架，输出恒为空表**（"暂无美股信号"）。
- 框架口径与 A 股一致：开盘价（行情开盘字段，缺失退回当日首笔）、现价、相对开盘涨跌%、相对信号日收盘 `return_since_signal`。美股无"距涨跌停%"一列，替换为"距当日高/低点%"。
- 样本量警告同 T006：样本极少，只供观察，不是胜率。

### C. 新机会
- 动量（现价/开盘价-1 > 3%）、放量（当日累计成交量 / 20 日均量 > 2）、突破（相对"此前"当日最高/最低价超过 1%）三条规则沿用，阈值严格大于。
- 差异：
  - 成交量单位：美股是**股**，不乘 100（A 股"手"需 ×100）。
  - 流动性：小盘/低价股价差大，1% 突破可能只是买卖价差抖动；加一个最低价格与最低日均成交额过滤（建议价格 ≥ 5 美元、20 日均成交额 ≥ 1000 万美元，可在代码配置），不满足的只展示不提示。
  - 均量来源：本地 `usdata`/`data/*.csv` 日线缓存；缺失则放量项不判定（显示"数据不足"），不用盘中数据自行估。
  - 开盘 5 分钟窗口和"当日"都按美东时间。

## 5. 数据存储路径与权限

- `data/us_market_monitor/`：告警、冷却计数、质量计数（对应 `market_monitor_watch`）；目录 0700、文件 0600、独占创建、逐层 O_NOFOLLOW，拒绝符号链接，复用 `market_monitor.store.directory`。
- `data/us_market_monitor_intraday/<美东日期>.jsonl`：追踪/触发记录追加写，约每分钟一批。
- 两个目录与 A 股的 `data/market_monitor*` 分开；`data/` 本来就不进 git。不读写 `data/` 下任何已有真实文件；测试一律用临时目录。
- 报价只在内存；不进离线导出、分享页、浏览器存储和日志（日志只写轮次计数）。
- 注意：磁盘上追加 jsonl 目前没有自动清理，长期使用需评估容量（与 T006 相同）。

## 6. 数据源与"稳定不中断"

### 6.1 yfinance（首选）
- 版本 1.7.0，Apache-2.0 许可（任务书写 MIT；**本机 `.venv` 的 `yfinance-1.7.0.dist-info/METADATA` 实际标注 `License: Apache-2.0`**，已核实，以此为准）。它包装 Yahoo 的非官方公开接口：接口随时可能改变，无 SLA。关于使用限制，以下是 yfinance 自述的原话（`METADATA` 第 78、80、82 行，**这是 yfinance 作者的说法，以 Yahoo 条款原文为准，本方案未联网核对条款本身**）：
  - "It's an open-source tool that uses Yahoo's publicly available APIs, and is intended for research and educational purposes."
  - "You should refer to Yahoo!'s terms of use ... for details on your rights to use the actual data downloaded."（条款链接：`https://policies.yahoo.com/us/en/yahoo/terms/product-atos/apiforydn/index.htm`、`https://legal.yahoo.com/us/en/yahoo/terms/otos/index.html`、`https://policies.yahoo.com/us/en/yahoo/terms/index.htm`）
  - "Remember - the Yahoo! finance API is intended for personal use only."
  结论：仅个人少量自用，不分享、不再分发。
- 取数方式：`Ticker(sym).fast_info`（现价/昨收/开盘/成交量/最近成交时间）或 `yf.download(..., period='1d', interval='1m')` 的最后一根。每只一次请求较重；多代码用 `yf.download(tickers=[...])` 一次拿一批（美股一次批量比逐只请求更友好，但 A 股"多代码未验证"的原则照搬：**默认逐只，真实验证 `multi_code` 后才打开**）。
- 延迟：Yahoo 免费行情可能有延迟，具体多久、因交易所而异，**本方案未核实**（一般说法是部分标的约 15 分钟，仅作假设，不作依据）。**页面必须显示数据自身的时间戳，且不能假设实时**；`max_age` 默认 20 分钟只是保守的占位值，S2 真实验证后再定。
- 隔离：沿用 T006 的"独立 spawn 子进程 + 父进程 timeout+2 秒硬截止 + terminate/kill"。yfinance 内部用 requests/curl_cffi，可能阻塞在 DNS 或 TLS；SIGALRM 在子进程主线程可用。子进程只回传白名单数值字段，不回传异常文本或原始 JSON。
- 禁止：不使用其缓存目录写入项目目录（`yf.set_tz_cache_location` 指到临时目录或禁用）、不使用 yfinance 的 cookie/crumb 之外的账号功能、不传任何凭据、不开代理。

### 6.2 Polygon.io（备选）
- 免费层 5 请求/分钟、数据延迟（15 分钟）、需注册 API key。**API key 属于凭据**：CLAUDE.md 规定不保存账号密码，因此 key 只能经环境变量临时传入，不写文件、不进日志。默认不启用，仅在 yfinance 长期不可用且所有者明确批准后再做；本方案 S 阶段不实现 Polygon，只留 `Provider` 接口。

### 6.3 稳定性设计（"不中断"的合理含义）
目标是**"出错不崩溃、能自愈、能说清楚"**，而不是"永不出错"——上游不稳定时不可能保证不中断。
- 单次失败只计数，连续 3 次失败暂停并显示原因，**与 T006 相同**，不无限重试（防止被 Yahoo 限流甚至封 IP）。
- 增加指数退避 + 抖动：失败后的下一次请求间隔翻倍，上限 5 分钟；成功后恢复。
- 速率与预算（**必须算账，T006 的数字直接搬过来会和"稳定不中断"冲突**）：
  - T006 现值（`scheduler.py:26-27`）：相邻请求 `REQUEST_GAP=10` 秒、同一代码 `SYMBOL_GAP=60` 秒、`MAX_BUDGET=600` 次/次 start。上一版草稿写"放宽到 3 秒"没有任何来源，**撤回**；相邻请求间隔先维持 10 秒，不比 T006 更激进。
  - 直接搬用的后果：30 只 × 每只每 60 秒 = 约 30 次/分钟；600 次预算约 **20 分钟**就耗尽，触发 `budget_exhausted` 暂停。常规时段 6.5 小时 = 390 分钟，需要约 30 × 390 = **11,700 次**请求。
  - 此外 30 只 × 每只间隔 10 秒 = 单轮至少 300 秒，所以 60 秒的每只间隔本来就排不满，实际节奏由 10 秒相邻间隔决定（最多 6 次/分钟）。
  - 备选方案（需所有者选，见第 10 节第 6 条）：

    | 方案 | 做法 | 全时段请求数（30 只） | 代价 |
    |---|---|---|---|
    | A（建议） | 每只间隔 300 秒、相邻 10 秒、美股版预算上限 `US_MAX_BUDGET=2500`（独立常量，不改 T006） | 约 6 次/分钟 × 390 ≈ 2,340 | 每只 5 分钟更新一次；已远低于 Yahoo 常见限流，但"常见"未经核实 |
    | B | 只改预算：保持每只 60 秒、预算调到约 12,000 | 约 11,700 | 请求量最大、被限流风险最高，不建议 |
    | C | 经 S7 真实验证 `multi_code` 后批量请求（一次 30 只，每 60 秒一批） | 约 390 | 依赖 `yf.download` 批量行为，可能部分代码缺失，需要逐代码核对；验证前不可用 |

  - 无论选哪个，预算耗尽不是故障：页面显示"本次预算用完，可手动再开始"，不自动续期，这是为了防止后台悄悄无限打 Yahoo；"稳定"指的是不崩溃、状态说得清，不是无限制地发请求。
  - `429` 单独分类 `rate_limited`，立即进入退避并暂停 10 分钟（退避与暂停时长是设计值，未经 Yahoo 实测）。
- 监督线程异常被捕获并转为 `service_error` 暂停；后台线程不死循环、不吞 KeyboardInterrupt。
- 数据质量：价格 ≤0/非有限、昨收缺失、时间戳早于上一交易日收盘 + 老化阈值、同价同时间戳重复过久，标为异常并不触发提醒；yfinance 返回空 DataFrame 视为失败。
- 重启进程始终停止（不自动恢复），避免"后台悄悄一直打 Yahoo"。

## 7. 页面接入提案

- 个人模式下导航新增第 8 项“美股盯盘”（任务书原话"第 8 项"）。已核对：`dashboard/app.js:415` 的 `PERSONAL_NAV` 现有 7 项（今日/信号/全市场/自选/回测/持仓/盯盘），新增即第 8 项；`PERSONAL_PAGES` 与 `personalHash` 白名单需同步加入 `usmonitor`。
- 页面结构镜像 A 股盯盘页：运行状态、请求次数/预算、监控只数、暂停原因；报价表（代码、名称若有、现价、相对昨收涨跌、数据时间（美东 + 北京时间）、告警状态、来源、质量）；三栏实时分析（空集时显示"暂无美股信号/持仓，仅自选"）；开始/停止按钮；配置只接受 `interval` 与 `top_n`。
- 路由：`GET /api/us_market_monitor/{status,quotes,alerts}`，`POST /api/us_market_monitor/{start,stop,config}`；个人模式关闭时 404，不创建调度器、不启动线程。门禁与 T006 完全相同（可信本机 + 已登录 + `X-Requested-With: dashboard`，写操作再加 Origin 校验；失败统一 403）。
- 全部使用 `textContent`，上游返回的名称只当文本；页面每 10 秒取一次本机快照，离开页面清空报价；中英文 i18n 新增键。
- 不进离线导出（`/api/offline` 的清单不含新路由）、分享页、代理；分享/代理访问一律拒绝（沿用 `trusted_local` 判断）。
- 需要改的文件（超出 S0 许可，留待后续阶段）：`app.py`（路由接线）、`dashboard/{index.html,app.js,i18n.js,terminal.css}`，均已在 TASKS.md T015 行登记。

## 8. 风险

| 风险 | 严重度 | 应对 |
|---|---|---|
| yfinance 为非官方接口，Yahoo 改版即失效；1.x 更换过 HTTP 库（curl_cffi） | 高 | 提供者抽象；子进程隔离；字段白名单；失败分类；文档写明可能随时失效；测试全部用注入的假提供者 |
| 速率限制 / IP 被临时封禁（HTTP 429、空数据） | 高 | 保守速率、退避、429 立即暂停、预算上限、默认逐只；不并发 |
| 许可与条款：yfinance 自述"personal use only"（原话见 6.1，以 Yahoo 条款原文为准）；yfinance 仅是抓取封装，库的开源许可不等于数据授权 | 中 | 仅个人少量自用；不分享、不再分发、不进导出；文档明确 |
| 数据延迟（15 分钟）与"收盘价非最终价" | 中 | 展示数据时间戳；不用盘中价重算策略信号；提醒不构成建议 |
| 时区/夏令时算错、周末判断错 | 中 | `zoneinfo` + 美东；测试覆盖夏令时切换日（3 月/11 月）、周末、16:00 边界 |
| 子进程/线程残留、Mac 睡眠中断采集 | 中 | 沿用 T006 的 spawn + terminate/kill；睡眠中断写入文档 |
| 美股样本小、信号为空，实时分析几乎无输出 | 低 | 页面明确说明"仅自选/暂无信号"，不是故障 |
| 低价/低流动性股票价差抖动造成假突破 | 低 | 价格与成交额过滤，仅展示 |
| 把盯盘当成交易建议 | 中 | 页面与文档固定声明；本模块无下单接口 |
| 回测/盯盘结果好 ≠ 策略有效（过拟合、多重检验、样本量、成本） | — | 盯盘不产生可验证的统计结论；追踪表样本通常 <10，仅观察；任何"赢率"不得拿来当策略证据 |

## 9. 建议的阶段切分（供所有者在 plan_T015.json 之外参考，不是本阶段承诺）

S1 core+时段+代码校验+测试 → S2 YFinanceProvider+子进程+白名单+限流/退避+测试（全 mock） → S3 store+watchlist+scheduler+测试 → S4 intraday 美股版+测试 → S5 wiring+app.py 路由+个人模式门禁+测试 → S6 前端第 8 项导航+i18n+页面+测试 → S7 `scripts/validate_us_feed.py`（所有者手动联网，上限 5 次请求）+ howto 文档。

## 10. 需要所有者决定的事

1. yfinance 许可：任务书写 MIT，本机 METADATA 为 Apache-2.0（已核实）。是否接受以 METADATA 为准？建议：接受。
2. 美股持仓：T013 台账不支持美股，第一版持仓来源为空。是否另开任务扩展台账？建议：暂不，先用自选。
3. 美股信号：无 `trade_list_us.json`，第一版信号为空。是否需要之后给美股生成今日清单？建议：另开任务，不在 T015。
4. 名单上限：建议美股上限 30（默认 top_n=0，因无信号），比 A 股 60 更保守以减轻 Yahoo 限流风险。
5. 北京时间说明：夏令时到 2026-11-01，之后开盘变为北京 22:30。页面以美东时间为准。是否接受？建议：接受。
6. **请求预算与"不中断"的冲突**（见 6.3）：选方案 A（每只 300 秒、预算 2500）、B 还是 C？建议：A，S7 真实验证后再评估 C。
7. 16:00 边界：请求窗口两端都含（与 A 股一致），盘后时间戳的行情不用。是否接受？建议：接受。
8. 并行占用提示（不是本阶段问题）：TASKS.md 中 T015 与 T009、T013 都登记了 `app.py` 和 `dashboard/*`。S5/S6 开工前须确认没有其他任务同时占用这些文件。
