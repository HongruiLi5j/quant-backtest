# A 股实时行情 provider 升级方案(T016 · S0 设计稿)

状态:设计稿,尚未实现。本阶段只读盘点,没有联网、没有改任何代码。
**下面所有关于第三方接口的字段、限额、许可,都来自我对公开资料的记忆,本阶段没有联网核对。**
进入 S1 前必须先对照 easyquotation 的实际源码和官方说明核实,凡标"待核实"的都不能当事实用。

## 1. 现状(T006 实现,已读代码)

| 项 | 现状 | 位置 |
|---|---|---|
| 数据源 | 只有腾讯 `https://qt.gtimg.cn/q=sh600000,...`,用 urllib 默认请求头,无自定义头 | `providers.py` TencentProvider |
| 每次请求 | 最多 5 只;同一代码 ≥60 秒;相邻请求 ≥10 秒;每次 start 预算 ≤600 | `scheduler.py` MAX_BATCH / SYMBOL_GAP / REQUEST_GAP / MAX_BUDGET |
| 多代码 | `multi_code=False`,默认逐只请求(多代码写法"未经真实验证") | `scheduler.py` |
| 名单上限 | `MAX_WATCH = 60` 硬编码,接口/配置/来源都不能改大 | `watchlist.py` |
| 隔离 | 每次请求起一个 spawn 子进程,父进程 timeout+2 秒后 terminate/kill | `service.py` process_request |
| 校验 | `validate_quote` 要求 14 位源时间、开盘/昨收/现价为正、成交量非负、时间不倒退、成交量不倒退 | `core.py` |
| 页面 | 配置项 采样间隔 30–3600、前 N 只 0–60 | `dashboard/app.js:1271`、`wiring.py` |

**瓶颈的数量级**:逐只请求、相邻 ≥10 秒 ⇒ 60 只一轮约 10 分钟;预算 600 次 ⇒ 最多约 100 分钟就用完。
所以"60 秒一股、预算 600"是整套系统的上限,不是名单大小的问题。要放开 MAX_WATCH,必须先换请求方式(一次拉很多只)。

## 2. 候选源盘点

约定:✅ 有 / ❌ 没有 / ❓ 待核实。

| 源 | 买卖五档 | 成交额 | 调用方式 | 速率/并发限制 | 免费额度 | 许可 | Python 库 | 稳定性顾虑 |
|---|---|---|---|---|---|---|---|---|
| **easyquotation**(封装新浪/腾讯/集思录等) | ✅(新浪、腾讯源) | ✅ | REST 轮询(库内部用 requests,把全市场代码切成每组约 800 只并发拉) | 库本身不限速;限制来自上游的 IP 封禁,阈值无公开数字 | 免费,不要 key | 库是 MIT(待核实);**数据本身不受 MIT 保护**,上游条款仍适用 | `easyquotation` | 个人维护的开源库,更新不勤;内置股票代码表会过期;上游改版就坏;❓是否带自定义请求头(见 §6 风险 R3) |
| **新浪 hq.sinajs.cn 直接调用** | ✅ | ✅ | REST,`list=sh600000,sz000001,...` | 一次约 800 只(❓);无公开频率数字;被判定为过频会 403/封 IP | 免费,不要 key | 无明确公开许可,个人自用灰色地带 | 无(自己写 urllib) | **近年需要 Referer 头,否则 403(待核实)**——这与本项目"不发伪装头"的红线冲突;返回 GBK 文本 |
| **腾讯 qt.gtimg.cn**(现用) | ✅(字段 9–28,待核实) | ✅(字段 37 附近,待核实;现有代码刻意没用) | REST,`q=sh600000,sz000001` | 现有代码每次 ≤5 只、每股 ≥60 秒——**这是项目自己定的保守值,不是腾讯公布的限制**;更大批量未验证 | 免费,不要 key | 无官方文档/明确许可,仅个人自用 | 无(已有 TencentProvider) | 无官方承诺;成交量单位(手/股)T006 阶段始终未核实 |
| **Akshare** | `stock_zh_a_spot_em` 无五档;`stock_zh_a_spot`(新浪)有 | ✅ | 底层就是爬东方财富/新浪网页接口 | 取全市场需翻几十页,慢、容易触发封禁 | 免费 | MIT(库) | `akshare` | 东方财富被 CLAUDE.md 限定"只用于每天一次的全市场快照";新浪版翻页多,不如直接批量。**不适合做盘中轮询** |
| **Tushare 免费积分** | 老接口 `get_realtime_quotes` 本质是爬新浪;新接口实时行情需要更高积分/单独权限(❓) | ✅ | REST + token | 免费积分下的"500 次/分钟"❓待核实,且实时类接口常有单独门槛 | 需注册,**需要 token** | 平台服务条款 | `tushare` | **要 token = 要存凭据**,与项目"不保存任何账号密码、不读 .secret"冲突;免费档能否取实时快照存疑 |

结论:
- Akshare:不用于盘中(东方财富限制 + 翻页慢)。
- Tushare:不用(需要 token,违反安全约定)。
- 真正的候选只有"新浪 / 腾讯的批量端点",easyquotation 只是它们的封装。

## 3. 推荐方案

**链路:easyquotation 为主 → 新浪直连为备 → 腾讯 TencentProvider 兜底。** 但有两处我建议和你的原话不同,需要你决定(见 §9):

1. **新浪这一路默认关闭。** 原因:新浪据我记忆需要 Referer 头,而 T016 的红线是"不发伪装头"。除非你明确放行"发一个诚实的、固定的 Referer",否则这一路不启用。
   没有新浪时,实际链路是:easyquotation(腾讯源) → 现有 TencentProvider(逐只,慢但稳)。
2. **先核实 easyquotation 是否自带 User-Agent/Referer。** 若有,且不能关,选项是:
   (a) 只用它的解析逻辑,自己用 urllib 发请求;(b) 干脆不依赖它,自己写一个批量腾讯客户端(请求头同现有 TencentProvider)。
   我倾向 (b) 作为兜底方案:提速来自"一次拉很多只",不来自这个库;现有 `parse_response` 已能解析腾讯格式,只要放开 5 只的限制并**真实验证**大批量写法。

为什么主源选 easyquotation 而不是直接新浪:它省掉自己写解析和分组,且新浪/腾讯双源切换是现成的;代价是多一个外部依赖,而且依赖会随上游改版而坏。

## 4. BatchProvider 接口草案

```python
@dataclass(frozen=True)
class BatchResult:
    quotes: dict[str, Quote]        # symbol -> Quote(只含解析成功的)
    missing: tuple[str, ...]        # 请求了但没返回/解析失败的 symbol
    source: str                     # 'easyquotation:tencent' / 'sina' / 'tencent_single'
    requests_made: int              # 实际发出的 HTTP 请求数(含内部分组)
    elapsed_seconds: float

class BatchProvider(Protocol):
    name: str
    max_symbols: int                # 单次 fetch_many 最多接受多少只
    def start(self) -> None: ...    # 默认未启动,和 TencentProvider 一致
    def stop(self) -> None: ...
    def fetch_many(self, symbols: Sequence[str], timeout: float) -> BatchResult: ...
```

要点:
- **允许部分成功。** 现有逻辑 `response_count_mismatch` 是"数量不对整批作废",对 5000 只不现实;批量版返回 `missing`,调度器按"缺失比例"判定质量(例如缺失 >20% 才算一次失败)。
- **Quote 数据类不改字段**,只新增 `provider` 取值(已有该字段)。五档/成交额暂不进 Quote(下游没人用,避免扩大改动面);要用时另开任务。
- **成交量单位按源区分。** 新浪是股、腾讯多半是手(待核实);`volume_regression` 校验只能在同一 provider 的相邻两次之间做,**切换 provider 时必须清空 `_previous` 基线**(现有代码在出错时已这样做,切源要同样处理)。
- **源时间归一化。** 腾讯给 14 位 `YYYYMMDDHHMMSS`;新浪给日期和时间两个字段,需拼成 14 位后才能过现有 `validate_quote`。
- **隔离。** 沿用"独立 spawn 进程 + 父进程硬截止",但返回体可能达数 MB(5500 只),`providers.py` 里 `response_too_large` 的 64KB 上限要按批量大小单独设(例如 8MB),且不能因此放松单只路径。

## 5. MAX_WATCH 可配置化

- `watchlist.MAX_WATCH` 从硬编码 60 改为**读本地配置文件**的值,默认 **500**,合法范围 1–5500;非法值回落默认并写一条日志。
- **配置来源只有本机文件(例如仓库根的 `monitor_config.json`,不进仓库),本文件只是初始值来源。** max_watch 可通过 HTTP 修改(`wiring.handle_config`,同时接受 `provider`、`batch_mode`),但只允许从 60 调到 5500,不接受本机以外的请求(个人自用、本机 only,所有者 2026-10-23 拍板)。
- 单一事实来源:`intraday.py` 里 `MAX_UNIVERSE = 60`(注释写"与 MAX_WATCH 一致")、`scheduler.validate_settings` 对 `top_n` 的上界、页面 `dashboard/app.js:1271` 的 `0, 60` 都要跟着读同一个值。
  其中 `intraday.py` **不在 TASKS.md 登记的 T016 文件里**(见 §9-D4)。
- 注意 `top_n` 的含义是"信号前 N 只",不是名单总数;放大 MAX_WATCH 后 top_n 的上界可以保持在一个较小的值(建议 ≤200),否则信号股会挤掉自选。
- 5500 并不是全部 A 股:`core.valid_symbol` 目前只放行 600/601/603/605/688 与 000/001/002/003/300/301 开头,**北交所不在内**。要不要放行是单独的决定。

## 6. scheduler 批量模式

新增模式开关 `mode = 'single'(默认,现状) | 'batch'`,通过配置切换,**默认保持 single,新功能默认关闭**(AGENTS.md)。

batch 模式下:
- 每轮:构建名单 → 一次 `fetch_many(全部 codes)` → 一次 `_consume`。不再有 `SYMBOL_GAP=60`、`REQUEST_GAP=10`、`MAX_BATCH=5`。
- 采样间隔 `interval`:默认 **30 秒**,最小 **5 秒**。5 秒只在小名单时才合理,所以再加一条**有效间隔下限**:
  `interval_effective = max(interval, ceil(n / 800) × 2 秒 × 安全系数)`,防止 5000 只名单配 5 秒间隔时每轮发出 7 个并发请求、每分钟 84 个请求。
- 预算改为**按"请求次数 / 分钟"和"每次 start 总请求数"双限**,都可配置、有保守默认值;用完暂停并显示 `budget_exhausted`(沿用现有 reason)。旧的 600 次预算按"整轮算 1 次"会失去意义。
- 失败处理沿用:连续 3 次失败暂停;新增**源降级**:主源连续失败 → 切到下一个源并清基线,**不自动切回**(避免抖动),需要手动重新 start。
- `_history` 内存:现在每只最多保留 600 个样本。5500 只 × 600 ≈ 330 万条字典,内存可能到数百 MB~1GB 量级(粗估,未测)。需要对大名单**缩小 KEPT_SAMPLES 或只给持仓/信号股保留历史**。
- `intraday` 分析(新机会检测、信号追踪)继续只面向持仓+信号股(≤60 只),不扩到全名单;这与"全市场扫描"是两件事。
- 交易时段门控(`continuous_session`)、15:30 前不用当天日线的约定、`validate_quote` 的 stale/future/volume 校验**一律不放松**。

## 7. 兼容性与回滚

- `TencentProvider`、`process_request`、`MonitorService`(S2 的 5 只版本)**一行不改**,继续作为兜底。
- 切换:配置 `mode: single` 即回到现状;`provider_chain` 配置列表决定批量模式下的源顺序。
- 新代码放进新文件 `src/market_monitor/providers_easy.py`(TASKS.md 已登记);`service.py` 不在登记范围内,所以批量版的"子进程 + 取消"逻辑也放在新文件里,不碰 `service.py`。
- 不改 `core.Quote` 的字段,不改 store 的落盘格式(报价与名单本来就不落盘)。
- 回滚:删配置/改回 `single` 即可;没有迁移。

## 8. 风险

| 编号 | 风险 | 缓解 |
|---|---|---|
| R1 | 新浪/腾讯按 IP 封禁,封了整台 Mac 的这类访问 | 默认名单 500、间隔 30 秒;有效间隔下限;每分钟请求数上限;被 403/429 立刻暂停且不重试;不并发多源同时拉 |
| R2 | easyquotation 上游改版/库停更导致解析错 | 双层防线:解析后仍过 `validate_quote`;缺失比例超阈值算失败;Tencent 单只兜底;依赖版本固定 |
| R3 | 库或新浪要求 UA/Referer,违反"不发伪装头" | S1 先读库源码确认;不满足就走 §3 的 (a)/(b);**新浪默认关闭,需所有者明确放行** |
| R4 | 许可:新浪/腾讯无官方许可;批量高频更接近"爬取" | 仅个人自用、不转发不落盘上游原文(与 T006 一致);文档写明;不公开数据 |
| R5 | 内置代码表过期(新股/退市) | 不用库的"全市场代码表",由本项目名单显式传入代码 |
| R6 | 大返回体内存/解析耗时,进程隔离下管道传输大对象 | 限制返回体大小;只传必要字段;用 §4 的 `missing` 容忍部分失败 |
| R7 | 5500 只下历史样本内存膨胀、前端表格卡顿 | §6 缩样本;前端对大名单分页或只显示有告警/持仓/信号的行(S 阶段细化) |
| R8 | 单位混用(手/股)导致成交量回归校验误报或漏报 | 按 provider 分基线;切源清基线;单位在文档中标"待核实" |
| R9 | 盘中半天数据与日线混用 | 保持现有 `continuous_session` 门控;批量模式不写日线缓存 |
| R10 | 睡眠/断网后补拉造成突发请求 | 不补拉;下一轮按正常间隔 |

## 9. 需要所有者决定(非阻塞,给了默认建议)

- **D1 请求头政策。** 新浪需要 Referer(待核实),easyquotation 可能自带 UA。选项:(A)只允许无自定义头的源(新浪关闭);(B)允许一个固定、诚实的 Referer 但不伪装浏览器 UA;(C)都不碰、自写批量腾讯客户端。**建议 A,实现走 §3 的 (b)。**
- **D2 新依赖。** 是否同意把 easyquotation(及其依赖 requests 等)写进 requirements.txt 并装进 `.venv`。若 D1 选 A + (b),可以不装。**建议:先不装,S1 先真实验证自写批量腾讯是否可行。**
- **D3 默认规模。** 默认 MAX_WATCH=500,最大 5500;首次真实验证只用 ≤100 只、间隔 ≥30 秒。**建议同意。**
- **D4 范围外文件。** `intraday.py`(MAX_UNIVERSE)、`service.py` 不在 TASKS.md 登记的 T016 文件里。**建议:`service.py` 不动;`intraday.py` 的 60 保持不变(实时分析只面向持仓+信号)。** 这样 T016 不需要追加登记。
- **D5 北交所。** `valid_symbol` 不含北交所,"全市场 5500"实际约 5100+。**建议:本任务不放行北交所。**

## 10. 建议的后续阶段(供审阅,未获批准,不属于 S0)

- S1:读 easyquotation 源码并核实请求头/字段(只读);写 `providers_easy.py` 的 BatchProvider + 假数据解析器 + 单元测试(mock,不发请求)。
- S2:`watchlist` 的 MAX_WATCH 可配置化 + `scheduler` batch 模式 + 测试。
- S3:前端配置项/大名单显示 + 文档。
- S4:所有者在开盘时段手动做一次小规模(≤100 只)真实验证,成交量单位、批量上限在此一并核实。

## 11. 测试策略

全部使用假请求/假时钟/临时目录,不联网、不读写 `data/`;解析器用手写的合成文本样例(不是抓来的原文);对"部分缺失、字段错位、时间倒退、切源清基线、有效间隔下限、MAX_WATCH 配置非法值"各写用例。真实联网验证只在 S4 由所有者手动做。
