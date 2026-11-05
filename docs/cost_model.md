# 交易成本口径说明（B03）

项目里同时存在**三套用途不同的成本口径**，外加外部引擎（backtrader / RQAlpha）自己的一套。它们的数值本来就不同，
但此前没有一份文档把"用在哪、数值多少、为什么不同、混用会怎样"写在一起。本文件补上这件事，并配套一个
只搬常量、不改数值的补丁：`src/costs.py`（单一来源）。

> 所有 bp 数字来自程序实际输出（见文末"怎么复现"），不是估算。行号是打补丁**之后**的行号。
> **费率本身（佣金万 2.5、过户费 0.001% 等）从未和真实账户核对过**（`src/simtrade.py:27` `FEE_VERIFIED = False`），
> 本补丁只统一"数值写在哪"，不是核实费率。

## 1. 一页速览

| 口径 | 用在哪 | 成本形态 | 个股来回成本（单笔 100 万元） |
|---|---|---|---|
| ① 主回测 | 全市场扫描、网页回测、今日买卖清单的历史绩效 | 对称"单边 bp × 换手"，一个数包含佣金/印花税/滑点 | A 股 16.00 bp；美股 20.00 bp |
| ② T008 回放 | `src/replay/` 的选股回放、淘汰判定 | 分项费用 + **显式 5bp 滑点（买卖各一次）** | 20.20 bp（不含滑点 10.20 bp） |
| ③ 模拟盘 / 持仓 | `src/simtrade.py` AI 账户；`src/holdings/` 手工记账的费用估算 | 分项费用，无滑点 | 10.20 bp |
| ④ 外部引擎 | `engines/` 的 backtrader、RQAlpha | 分项费用，印花税**按成交日分段**，无滑点 | 税前（2023-08-28 前）15.20 bp；税后 10.20 bp |

"来回成本" = 买入一次 + 卖出一次的费用占成交额的 bp，由真实费用函数算出（`outputs/B03/roundtrip_after.md`）。

## 2. 逐套说明

### ① 主回测（对称单边 bp）

| 项 | 数值 | 位置 |
|---|---|---|
| A 股个股 | 单边 8 bp | `config.py:47` `stock_cost_bp`；`src/scan.py:35` `COST` |
| A 股 ETF | 单边 5 bp | `config.py:44` `cost_bp` |
| 美股个股 | 单边 10 bp | `config.py:27` `stock_cost_bp`；`src/scan_us.py:28` `COST` |
| 美股 ETF | 单边 5 bp | `config.py:26` `cost_bp` |
| 引擎默认 | 0.0005 | `src/portfolio.py:58` `run_weights(cost=…)`；`src/validation/adapter.py:333,374`（B14 起读 `costs.ENGINE_DEFAULT_ONE_WAY`） |
| 与 AmiBroker 对照 | 0.0005 | `src/backtest.py:21` `commission_pct`，必须等于 `amibroker/ma_cross_adx_atr.afl:16` 的 0.05% |
| 来回 16 bp | `2 × 8` | `src/signal_ledger.py:34` `ROUND_TRIP_BP`，用于信号账本的"净超额" |

扣费方式（`src/portfolio.py:101`）：`ret = (1+隔夜) × (1 − cost × 换手) × (1+日内) − 1`，换手 = Σ|目标仓位变化|，
买和卖**同一个费率**，没有最低 5 元，没有单独的印花税，没有显式滑点。

使用点：`src/scan.py:96,107`、`src/scan_us.py:57,65`、`src/trade_list.py:143,144`（用 `scan.COST`）、
`src/registry.py:141`（网页单股回测，按个股/ETF 取不同档）、`src/detail.py:118`。README（`README.md:19`）只写了 A 股的 8 bp / 5 bp，
**没有写美股的 10 bp / 5 bp**。

为什么是 8 bp：`config.py:47` 注释说"佣金约 2.5bp + 卖出印花税 5bp（每边约 4bp）+ 滑点"。按本项目 `engines/common.py` 的常量做算术：

- 税前（印花税千分之一）：佣金+过户 2.60 bp + 印花税摊到每边 5.00 bp = 7.60 bp，8 bp 只剩 0.40 bp 给滑点；
- 税后（印花税万分之五）：2.60 + 2.50 = 5.10 bp，8 bp 剩 2.90 bp 给滑点。

即 8 bp 大致相当于"税前印花税、几乎没有滑点"，或"税后印花税、约 3 bp 滑点"。注释里"每边约 4bp"与算术对不上（5 bp 只在卖出收，平摊是 2.5 bp），未改注释，列入"需站主决定"。

### ② T008 回放（分项 + 显式滑点）

| 项 | 数值 | 位置 |
|---|---|---|
| 佣金率 | 0.00025（万 2.5），最低 5 元 | `src/validation/costs.py:43-44`（默认值，来自 `src/costs.py`） |
| 过户费率 | 0.00001 | `src/validation/costs.py:45` |
| 卖出印花税 | 0.0005（固定，不分日期） | `src/validation/costs.py:46` |
| 滑点 | 5 bp，买卖各一次；涨跌停附近另加 20 bp（仅逐笔场景） | `src/validation/costs.py:47-48`；回放 Engine 把滑点折成费用 `src/replay/engine.py:380,389` |
| 回放继承 | `CostParams(_BaseCostParams)` | `src/replay/engine.py:81`；`COST_SOURCE`（`:55`）标注是否走 validation.costs |
| 内置简版 | 同样的默认值，只在 `src.validation.costs` 导入失败时用 | `src/replay/engine.py:58-63`（在红线目录，未改，由测试核对不漂移） |
| 组合层成本 | 同样的费率，但**忽略最低佣金** | `src/replay/ensemble.py:142-146` `_rates`；`docs/replay_howto.md:101` 已说明 |

回放区间是 2024-01-01～2026-03-31（`scripts/replay_run.py:4-5` 用法示例、`项目交接.md` 里训练段 2024-01-01～2025-06-30），整段都在印花税减半之后，所以固定 0.0005 在这个区间是对的；
**但代码里没有任何"开始日期早于 2023-08-28 就报错/提示"的保护**。

### ③ 模拟盘 / 持仓

| 项 | 股票 | ETF | 位置 |
|---|---|---|---|
| 佣金率 | 万 2.5 | 万 1.5 | `src/simtrade.py:28-33` `FEE` |
| 最低佣金 | 5 元 | 5 元 | 同上 |
| 过户费 | 0.001%（沪深都收） | 0 | 同上 |
| 卖出印花税 | 0.05% | 0 | 同上 |

- 计费函数 `src/simtrade.py:49-54` `fees()`：每项四舍五入到分；没有日期参数；成交价取次日开盘价，**无滑点**。
- 持仓页网页路径用的就是这个函数：`app.py:868` 把 `simtrade.fees` 注入为 `fee_fn`。
- 持仓模块自己还有一个**回退**估算 `src/holdings/calc.py:68-82` `default_fee_estimate`（`Decimal`）：佣金万 2.5 最低 5 元；过户费只对 5/6 开头代码收；卖出印花税对非 ETF 收。网页路径不走它（因为注入了 `simtrade.fees`），但其他调用者和测试会走。

两者对同一笔交易的估算并不相同（单笔 100 万元，来回 bp）：

| 品种 | simtrade | holdings 回退 |
|---|---|---|
| 沪市个股 600531 | 10.20 | 10.20 |
| 深市个股 000001 | 10.20 | 10.00（回退不收深市过户费） |
| ETF 510300 | 3.00（佣金万 1.5、无过户费） | 5.20（佣金万 2.5 + 沪市过户费） |

### ④ 外部引擎（backtrader / RQAlpha）

- 常量：`engines/common.py:31-34` 佣金 0.00025、过户费 0.00001、最低 5 元、`STAMP_CHANGE = 2023-08-28`；`stamp_duty()`（`:37-40`）按成交日返回 0.001（之前）或 0.0005（当天起）。
- backtrader：`engines/bt_base.py:29-34` 手写 `_getcommission`；RQAlpha：`engines/rq_engine.py:212-215` 用佣金乘数 + `pit_tax: True`（按时点税率）。
- 自研引擎的包装 `engines/inhouse.py:32`、`engines/custom_engine.py:49`：用的是**主回测的 8 bp**（不是分项费用）。

也就是说，网页"引擎对照"里的三个引擎，**自研用 8 bp 对称，backtrader/RQAlpha 用分项费用**，三者成本本身就不同；这是已知差异（`docs/backtest_audit.md` 第 160-161 行），但"三个引擎规则要一致"的约定在成本这一项上没有做到。
此外 `engines/common.py:263` 给分析页传入 2.6 bp，而 `src/analytics.py:122` 的文案固定写"已包含佣金、印花税和滑点的估计"——对 backtrader/RQAlpha 来说这句话不成立（不含滑点，印花税是另算的）。

## 3. 同一笔交易，各口径怎么算（程序输出）

单笔 1,000,000 元（最低佣金不起作用）：

| 口径 | 买入 bp | 卖出 bp | 来回 bp |
|---|---|---|---|
| 主回测 个股（A 股） | 8.00 | 8.00 | 16.00 |
| 主回测 ETF（A 股） | 5.00 | 5.00 | 10.00 |
| 主回测 个股（美股） | 10.00 | 10.00 | 20.00 |
| 模拟盘 simtrade 个股 | 2.60 | 7.60 | 10.20 |
| 模拟盘 simtrade ETF | 1.50 | 1.50 | 3.00 |
| T008 回放（含 5bp 滑点） | 7.60 | 12.60 | 20.20 |
| T008 回放（不含滑点） | 2.60 | 7.60 | 10.20 |
| 外部引擎，成交日 2023-08-25（税前） | 2.60 | 12.60 | 15.20 |
| 外部引擎，成交日 2023-08-28（税后） | 2.60 | 7.60 | 10.20 |

单笔 10,000 元（最低 5 元起作用，把 5 bp 抬到 5.00～5.10 bp）：simtrade 个股来回 15.20，回放 25.20（不含滑点 15.20），外部引擎税前 20.00/税后 15.00，主回测不变（没有最低佣金）。
完整表见 `outputs/B03/roundtrip_after.md`。

## 4. 为什么不同

1. **目的不同。** 主回测要在全市场 ×7 个策略 ×十几年上批量跑，用一个对称的 bp 最简单、可向量化；回放要给"淘汰/入池"做判定，需要把成本拆开，才能做"成本翻倍""扣成本前后"的分解；模拟盘/持仓要贴近真实账单（整手、最低 5 元、分项）。
2. **滑点处理不同。** 只有回放把滑点显式建模（5 bp，买卖各一次）；主回测把它折进 8 bp；模拟盘和外部引擎不含滑点。
3. **印花税处理不同。** 见 §5。
4. **形成时间不同。** 主回测的数值最早（扫描时代），后来的模块各自从 `simtrade.FEE` 复制，复制时没有统一管理，所以才有 `validation/costs.py` 注释里"来源 simtrade，未核对"这种链条。

## 5. 印花税 2023-08-28 起减半：代码里有没有按日期区分？

（卖出印花税从千分之一降到万分之五、生效日 2023-08-28，是 `engines/common.py:34-40` 的写法；本任务没有联网，这个日期和税率本身**需站主按官方公告核实**，其他年份的历史税率更没有核实，不在本文范围。）

| 位置 | 印花税处理 | 按日期区分？ |
|---|---|---|
| `src/portfolio.py`（主回测）、`src/scan.py`、`src/backtest.py` | 没有单独的印花税，折进 8 bp / 5 bp | **否**（全历史同一个数） |
| `engines/common.py:37-40`、`bt_base.py:33`、`rq_engine.py:215` | 前 0.1% / 后 0.05% | **是** |
| `engines/inhouse.py`、`custom_engine.py` | 固定 8 bp | 否 |
| `src/simtrade.py:53` | 固定 0.0005 | **否**（`fees()` 没有日期参数） |
| `src/validation/costs.py:136`、`src/replay/engine.py`（`fee`、`_rates`） | 固定 0.0005 | **否** |
| `src/holdings/calc.py:81` | 固定 0.0005 | **否** |

结论：**只有外部引擎按日期区分，`src/` 下所有模块都没有。** 影响：

- 模拟盘只撮合"今天之后"的纸面成交，影响为零；回放区间整段在 2023-08-28 之后，影响为零（但没有保护，换成更早的 `start` 会低估卖出成本约 5 bp）。
- 持仓模块允许录入过去日期的成交（只拒绝未来日期），旧成交如果让系统估算费用，会少估卖出成交额的 0.05%。
- 主回测的 2013～2023-08 段，印花税实际是现在的两倍，8 bp 恰好与"税前税率、几乎无滑点"吻合；2023-08 以后段则隐含约 3 bp 滑点。所以**样本内（2013～2019）和样本外（2020 起，`src/scan.py:33`）的实际成本并不一致**，对高换手策略尤其如此。
- 本补丁没有改任何一处的行为（只搬常量）。要加日期分段需要改 `portfolio.run_weights` 的计费方式，会改变回测数字，必须单独做、单独验收。

## 6. 混用风险（按严重程度）

1. **把回放的净收益和主回测的净收益直接比较。** 同一个策略，回放来回 20.20 bp、主回测 16.00 bp，差异来自显式滑点、印花税摊法和最低佣金，不是策略差异。
2. **"成本翻倍"不是同一个翻倍。** 网页回测 `src/registry.py:223` 是把 8 bp 乘 k；回放 `src/replay/selection.py:267-271` 是把回放自己的 `cost`（含滑点）乘倍数。两边"翻倍后仍为正"不能互相引用。
3. **三引擎对照里成本不一致。** 自研 8 bp 对称 vs backtrader/RQAlpha 分项费用（§2④），差异里混入了成本模型差异，不全是实现差异。
4. **`engines/common.py:263` 的文案与实际不符**（见 §2④），页面会让人以为已含印花税和滑点。
5. **同一笔交易，simtrade 与 holdings 回退估算不同**（§2③ 表）。网页走 simtrade，不走回退，但换一个调用者就会出现 ETF 5.20 vs 3.00 的差。
6. **信号账本 16 bp 与 `scripts/signal_backfill.py:36` 的默认 30 bp。** 同样是"买入类扣往返费用"，默认值不同；`src/paper_review/weekly.py:35` 的"16bp"是硬编码文字，不会随 `ROUND_TRIP_BP` 变化。
7. **最低 5 元只在部分口径生效。** 主回测、回放组合层（`ensemble._rates`）忽略；simtrade、回放逐笔、外部引擎计入。小账户（单笔低于 2 万元时 5 元高于万 2.5）差别明显，现在 AI 账户 5000 万影响小。
8. **所有费率都是占位值。** `FEE_VERIFIED=False`，美股 10 bp、回放 5 bp 滑点均非实测。拿这些数当结论会过于自信。
9. **README 只写了 A 股成本**，未写美股的 10 bp / 5 bp。

## 7. 统一建议

已做（补丁，**数值不变**）：

- 新增 `src/costs.py`：只放常量，不 import 项目内任何模块（`tests/test_costs.py` 检查这一点，避免 `config.py` 引用它时循环导入）。
- 改为读取它的位置：`config.py`、`src/scan.py`、`src/scan_us.py`、`src/simtrade.py`、`src/validation/costs.py`、`src/holdings/calc.py`、`src/signal_ledger.py`、`src/portfolio.py`（默认参数）、`src/backtest.py`。
- `src/replay/engine.py` 的内置简版没改（`src/replay/` 在红线目录里），`engines/common.py` 没改（`engines/` 不在本任务允许范围），`src/validation/adapter.py` 当时没改（B14 已改，见下）。前两处由 `tests/test_costs.py` 核对与 `src/costs.py` 不漂移。

B14 补充（数值仍不变）：

- `src/validation/adapter.py` 的 `from_strategy`、`rerun_with_held` 两个 `cost=0.0005` 默认值改读 `costs.ENGINE_DEFAULT_ONE_WAY`。B03 担心的"屏蔽 `src.portfolio` 也要能降级"不受影响：那套降级只屏蔽 `src.portfolio` / `src.strategies`，而 `src/costs.py` 是不 import 任何项目模块的叶子模块，导入它不会失败。
- 新增 `tests/test_costs_single_source.py`：用 AST 扫描 `src/`、`scripts/`、`config.py`、`app.py`、`daily.py`，凡是出现等于已知费率（0.00025、0.00015、0.00001、0.0008、0.0005，含 `Decimal("0.00025")` 这类字符串）的字面量就失败；白名单只有三个文件并写明原因：`src/costs.py` 自己、`src/replay/engine.py`（红线目录的降级简版）、`scripts/audit_recompute.py`（独立复算脚本，设计上不 import 项目计算代码，值必须等于 afl 的 0.05%）。
- 持仓页（`holdings.calc.default_fee_estimate`）、模拟盘（`simtrade.FEE`）、会员模拟盘（`app.py` 取 `MARKETS[...]["stock_cost_bp"]` 传给 `members.order` 的 `fee_bp`）三处读的都是 `src/costs.py`（经 `config.py` 间接），没有各自手写一份。
- **仍然存在的"第二份"**（本任务不能或不该改）：`engines/common.py` 的四个常量与 `src/replay/engine.py` 内置简版（路径不在允许范围）；前端 `dashboard/engines.js` 里写死的"万2.6、最低 5 元"等**文字**（展示文案，不参与计算）。

建议后续（每一项都会或可能改变数值，需要站主批准、单独验收）：

1. `engines/common.py` 的四个常量改为从 `src/costs.py` 引用（或反过来），消除唯一的第二份拷贝。
2. 给印花税加日期：`costs.py` 增加 `stamp_duty_rate(date)`，主回测、simtrade、holdings 的费用函数带日期参数；主回测这么做会改变 2023-08 之前的历史数字。
3. 核对真实费率后把 `FEE_VERIFIED` 改 True，并在 `costs.py` 注明核对日期与来源（账户/券商费率表）。
4. 把 `analytics.assumptions` 的成本文案按口径区分，并让 README 写全美股成本。
5. 报告任何净收益数字时，同时写明用的是哪一套口径（①②③④）。

## 8. 怎么复现文中数字

```
.venv/bin/python tests/test_costs.py
```
来回 bp 表由真实函数算出：`simtrade.fees`、`calc.default_fee_estimate`、回放 `Engine.fee`、`engines.common` 常量；表和改前改后对照的指纹文件在 `outputs/B03/`（`roundtrip_*.md`、`fingerprint_*.json`）。

## 9. 局限

- 本文只描述各口径的**设定**，没有评价哪套更接近真实成交；没有用实盘成交验证。
- 费率、税率日期没有联网核对，只引用了代码里的说法。
- 来回 bp 以单笔成交额计，没有考虑持仓天数、换手频率；高换手策略对成本更敏感，不要把"单笔差几个 bp"当成"策略收益差几个 bp"。
