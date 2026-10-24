# API_MAP · 旧页面 ↔ 新页面 ↔ 接口

> 来源：`dashboard/app.js`（路由 `route()` 和各 `loadXxx()`）、`dashboard/admin.js`、`dashboard/engines.js`、`app.py`（`do_GET` / `do_POST`）。只读盘点，没有改任何旧文件。
> 字段名来自 app.py 返回值和 `src/` 里的构造代码，数字类型以服务端为准（持仓接口里的金额是字符串，见下）。

## 0. 通用约定

- 所有请求相对路径 `/api/...`，`credentials: 'same-origin'`。`ui/shared/api.js` 已统一处理。
- 写操作（POST）和持仓 / 盯盘 / 离线版必须带请求头 `X-Requested-With: dashboard`，`api.js` 每个请求都带，无副作用。
- **个人模式**：`GET /api/meta` 返回的 `personal_mode` 必须严格等于 `true`，才有持仓、盯盘、美股盯盘；否则这些接口 404。新页面进入这三页前先看 `personal_mode`。
- **鉴权失败**：持仓 / 盯盘 / 我的相关 / 管理接口无权限时返回 401 或 403（固定错误码，如 `HOLDINGS_FORBIDDEN`）。`api.js` 把 401/403 抛成 `ApiError`（`err.auth === true`），页面显示「登录已过期，请从首页重新进入」。
- **业务错误**：不少接口用 HTTP 200 + `{ "error": "文字" }` 表示「没数据 / 参数错」（例如 `/api/summary` 还没生成时）。`api.js` **不**把这种情况抛错，页面自己判断 `r.error`。
- 路由：旧版是 hash 路由（`#/today`），旧页面地址按 `app.js` 的 `route()` 写。

## 1. 旧页面 → 新页面

「新页面」栏是 `ui/w/<id>.html`；mockup 里没有对应页面的标 ⚠（需要新做）。`usmonitor` 复用 `monitor` 页的「美股」tab。

| 旧页面 | 旧地址 | 新页面 | 接口（方法 + 路径） | 返回的主要字段 |
|---|---|---|---|---|
| 今日（个人模式） | `/#/today` | `w/today.html` | GET `/api/main_strategy`；GET `/api/summary?market=cn`；GET `/api/tracking?market=cn`；（会员模拟持仓）GET `/api/member/me`、`/api/member/portfolio?market=cn` | main_strategy: `main`, `reason`。summary: `market, market_name, main, main_name, data_date, generated, timezone_label, launch, strategies[], watch[], health[], notes`；strategies[]: `key, label, action, now, next, next_total, reason, recent, since_launch, stats, cur_dd`；watch[]: `symbol, name, last_date, close, d1, m1, ytd, above, trend_w, rsi2, rsi14, vol, ma300_gap, dd1y`。tracking: `eval{empty, generated, total, since, latest, summary, recent}, replay{…}, notifications[]` |
| 今日买卖（旧名）/ 信号 | `/#/trade`（`#/review`、`#/track` 在个人模式会并入 `#/trade`） | `w/signals.html` | GET `/api/trade_list_dates`；GET `/api/trade_list?strategy=&date=&market=`；GET `/api/trade_list_tracking?strategy=`；GET `/api/trade_list_mine?strategy=&date=`（仅本机）；GET `/api/trade_list_export?…`（下载 xlsx）；GET `/api/ledger`；GET `/api/main_strategy` | trade_list_dates: `dates[], latest`。trade_list: `market, data_date, generated_at, threshold, min_target, min_turnover, default_strategy, strategy, available_strategies, label, counts, counts_main, counts_small, rows[], exceptions[], empty, disclaimer, limitations, performance_note`。trade_list_tracking: 见 `src/tracking.py: evaluate_trade_list`。mine: `rows[], member_logged_in`。ledger: `empty / message` 或账本 `latest.json` |
| 全市场 | `/#/market` | `w/market.html` ⚠ | GET `/api/scan?market=&strategy=&q=&board=&signal=&page=&size=…`；GET `/api/search?market=&q=` | scan: `rows[], total, page, size, meta, boards[], industries[]`；search: `[{code, name, kind}]` |
| 自选 | `/#/watch` | `w/watch.html` ⚠ | GET `/api/watchlist?market=`；POST `/api/watchlist?market=&action=add\|remove&code=`；GET `/api/table?market=&strategy=&start=`；GET `/api/meta`（添加后刷新） | watchlist: `stocks[], pending[]`；table: `rows[], pending[]`；meta: `markets{cn:{name, main, universe, stocks, timezone, timezone_label, stock_cost_bp, cost_bp, source}}, default_market, strategies, personal_mode` |
| 个股详情 | `/#/stock/<code>` | 无独立 mockup 页（搜索结果跳转目标，⚠ 需要决定放在 market 页里还是单独一页） | GET `/api/stock?market=&code=&strategy=&period=` | `info, strategy, period, cost_bp, dates[], open/high/low/close/volume[], lines, sub, ind, position, equity, bench, trades[], compare, next, action` |
| 回测实验室 | `/#/lab` | `w/backtest.html` | GET `/api/backtest?market=&strategy=&symbol=&params={json}&cost=&start=`；GET `/api/eng/meta`；POST/GET `/api/eng/job`（backtrader / RQAlpha，POST 提交，GET `?id=` 轮询）；引擎对比 `#/cmp`、代码 `#/code` 用 `/api/code/*` | backtest: 引擎结果（`registry.run(..., detail=True)`），出错时 `{error}`。eng/meta: `strategies[{key,label,desc,params}], lib_ok, watchlist, default_code, engines`。eng/job: 任务 `id` 和结果 |
| 持仓 | `/#/holdings` | `w/holdings.html` | GET `/api/holdings?trades_limit=200&include_voided=0\|1`；POST `/api/holdings/add`、`/void`、`/capital` | `ok, as_of, capital, summary{market_value,total_cost,floating_pnl,realized,dividends,invested_ratio,missing_price_count}, positions[{code,name,shares,avg_cost,first_buy,held_days,price,price_date,market_value,floating_pnl,floating_pct,weight,realized,dividends}], closed[{code,name,first_buy,last_sell,held_days,pnl}], trades[{id,trade_date,code,name,side,qty,price,fee,fee_estimated,signal_ref,note,voided,created_at}]`。**金额都是字符串**（Decimal 格式化），前端要 `Number()` |
| 盯盘（A 股） | `/#/monitor` | `w/monitor.html`（A 股 tab） | GET `/api/market_monitor/status`、`/quotes`、`/alerts`；POST `/start`、`/stop`、`/config`（body: `interval, top_n, provider, max_watch, batch_mode`） | status: `ok, status{state, reason, phase, requests_used, request_budget, interval, top_n, watch_count, missing_count, missing_codes, max_watch, batch_mode, provider, degraded, truncated, rounds, timezone}`。quotes: `ok, quotes[{code,name,price,pct,source_time,received_at,quality,reasons,sources,alert}], status, intraday{events[], tracking{day, rows, summary…}}`。alerts: `ok, alerts[]` |
| 美股盯盘 | `/#/usmonitor` | `w/monitor.html?market=us`（美股 tab） | GET `/api/us_market_monitor/status`、`/quotes`、`/alerts`；POST `/start`、`/stop`、`/config`（`interval, top_n`） | 结构与 A 股相同（见 `src/us_market_monitor/`） |
| 引擎页 | `/#/bt` `/#/rq` `/#/cmp` `/#/code` | 并入 `w/backtest.html`（三引擎 tab）/ `w/research.html`；代码编辑无 mockup 对应 | GET `/api/eng/meta`、`/api/eng/job`、`/api/eng/scan`、`/api/code/list`、`/api/code/file`、`/api/code/history`、`/api/code/version`；POST `/api/code/save`、`/new`、`/restore`、`/api/eng/scan?action=start\|stop` | 见 `dashboard/engines.js` |
| AI 翻译 | `/#/translator`（入口在 `#/ai`） | 无 | POST `/api/ai/<action>` | 见 `dashboard/ai_translator.js`、`src/ai_translator/` |
| 分享 / 离线版 | 旧首页底部 | `w/settings.html` 或 `w/system.html`（需要决定） | GET `/api/share`、`/api/share_visits`；POST `/api/share?action=start\|stop`；GET `/api/offline`（下载，需请求头） | share: 分享状态与链接；visits: 访问记录 |
| 立即更新 / 状态 | 旧顶栏按钮 | `w/system.html` | POST `/api/refresh?market=`；GET `/api/status` | status: `running, log, percent, phase, message, returncode` |
| 模拟交易 | `/member` | 无（会员站，新界面不做） | `/api/member/*` | — |
| 后台 | `/admin` | 导航里的「后台」直接链到 `/admin`（管理令牌登录，另一套页面） | GET `/api/admin/overview`、`/data`、`/log?name=&n=`、`/audit`、`/members`、`/member?id=`、`/visits`、`/backup/download`；POST `/api/admin/action` | overview: `now, server{pid,started,uptime,python,managed}, launchd[], daily_running, markets, disk, tasks, gap, token` + 刷新状态 |

## 2. mockup Workspace 页 ↔ 真实接口是否存在

mockup `quant-ai-dark/w/` 实际有 **10 个 html**（today、signals、monitor、holdings、backtest、research、reports、system、share-members、settings）。任务书里写的是 11 页；多出的那一页应该是「全市场」或「自选」之类还没做 mockup 的页面，这里一并列在最后。

| mockup 页 | 有真实接口？ | 对应真实接口 | 说明 |
|---|---|---|---|
| today | 有（部分） | `/api/summary`、`/api/tracking`、`/api/main_strategy`、`/api/trade_list` | mockup 的「仓位风险」「昨晚报告摘要」没有直接接口：仓位风险可用 `/api/holdings`，报告摘要依赖 Reports 接口（无） |
| signals | 有 | `/api/trade_list*`（mockup 注释写的 `/api/trade?date=` 并不存在，以 `/api/trade_list` 为准） | 「触发条件 / 历史胜率 / ATR 止损」字段需核对 `trade_list.rows[]` 是否都有 |
| monitor | 有 | `/api/market_monitor/*`、`/api/us_market_monitor/*` | 分时图数据来自 `quotes.intraday`，需核对；「数据源灯」「调度器」来自 `status` |
| holdings | 有（部分） | `/api/holdings` | 「净值 vs 基准」「行业分布」「今日盈亏」接口里没有，需前端自己算或砍掉；「信号状态与距止损」要拼 `/api/trade_list` |
| backtest | 有 | `/api/backtest`、`/api/eng/meta`、`/api/eng/job` | 成本滑块 mockup 是前端重算，真实接口有 `cost`（bp）参数，应让后端重跑 |
| research | **没有** | — | Replay 排行 / Validation / Factor Scan / Monotonic 的数据在 `data/`、`scripts/` 输出里，没有 HTTP 接口。`/api/tracking` 的 `replay` 字段只覆盖一小部分 |
| reports | **没有** | — | 读 `reports/overnight_*/` markdown，没有接口；只能等 U5 加只读接口（新增接口需要站主确认） |
| system | **部分** | `/api/admin/overview`、`/api/admin/log`、`/api/status`、`/api/market_monitor/status` | mockup 写的 `/api/owner/system` 不存在；`/api/admin/*` 需要管理令牌登录，主站登录凭据访问会 401/403。handoff 任务看板没有接口，且 handoff/ 是红线目录 |
| settings | **没有** | — | 只是前端偏好（localStorage）；费用默认值要后端保存的话没有接口，先放 localStorage |
| share-members | 管理接口有（需管理令牌） | `/api/admin/members`、`/api/admin/member` 等 | 真实站只有站主一人，不进新导航；保留旧「后台」 |
| （缺 mockup）全市场 / 自选 / 个股 / 美股盯盘 | 接口都有 | `/api/scan`、`/api/table`、`/api/watchlist`、`/api/stock`、`/api/us_market_monitor/*` | 新导航需要这几项，但 mockup 没有对应页面：全市场和自选要新做；美股盯盘用 monitor 页的美股 tab |

## 3. 接口注意事项（给 U1–U4）

1. `/api/summary`、`/api/tracking` 用 `market=cn|us` 区分；`/api/meta.markets` 的键就是可用市场。
2. `/api/holdings*`、`/api/market_monitor/*`、`/api/us_market_monitor/*` 个人模式关闭时 404，且持仓内容不写日志；mock 数据里不要放真实金额。
3. 个人模式下 `/api/trade_list_mine` 只能本机访问，从外网（me.accessiblequant.com）访问会 403，页面要优雅降级。
4. 上游文本（行情源返回的名称、告警原因）一律当纯文本，`textContent`。
5. 未列出的 `/api/*` 业务实现不要改；缺字段写进 `~/Documents/AQ-ui-status/requests.md`，等 U5 和站主确认。
