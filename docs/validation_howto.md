# 验证与模拟盘工具包使用说明(T007)

> 状态:S1–S8 已补全。设计见 handoff/reports/T007_plan.md(该目录不进公开仓库)。
> 这是离线工具:不接网站、不联网、不读写 data/,不构成投资建议。

## 1. 它解决什么问题
回测好看不等于有效。本工具包把"防过拟合"流程化:三段切分、滚动前进、A 股成本压力测试、试验次数与多重检验折扣、模拟盘对比、统一报告。

## 2. 快速开始
见第 10 节(统一报告与命令行)。想先看完整演示,读第 11 节并运行 `python3 -B tests/test_validation_e2e.py`。

## 3. 模块说明
| 模块 | 作用 | 阶段 |
|---|---|---|
| src/validation/splits.py | 训练/验证/锁定测试切分、滚动前进 | S1 |
| src/validation/costs.py | A 股成本、涨跌停、T+1、整手压力测试 | S2 |
| src/validation/trials.py | 试验记录(只追加)、夏普折扣、PSR/DSR、多重检验、bootstrap | S3 |
| src/validation/stats.py | 绩效统计、样本内外对比 | S4 |
| src/validation/paper.py | 模拟盘日志(SQLite)与回测对比 | S5 |
| src/validation/adapter.py | 只读适配现有回测或外部收益序列 | S6 |
| src/validation/report.py + scripts/validate_strategy.py | 统一报告、命令行 | S7 |

## 4. 怎么读结果(统计概念)
Deflated Sharpe、Bonferroni/Holm/BH、块 bootstrap 见 4.3;读整份报告的顺序见第 12 节。

### 4.1 切分与锁定测试段(S1,`src/validation/splits.py`)
- **三段切分**:`three_way_split(index, train=0.6, valid=0.2, embargo=5)`。训练段用来选参数,验证段用来选策略,锁定测试段只在最后看一次。区间是位置区间 `(start, end)`,左闭右开,即 `index[start:end]`。也可按日期切:`split_by_dates(index, valid_start, test_start, embargo)`。
- **embargo(隔离带)**:相邻两段之间丢弃的交易日根数。持仓或标签跨好几天时,训练段末尾的样本其实用到了后面几天的信息,紧挨着切会把信息泄漏进验证段,所以中间空几根。embargo 一般不小于最长持仓/标签跨度。
- **滚动前进**:`walk_forward(index, train_len, test_len, step=None, embargo=0, anchored=False)` 返回若干 `Fold(train_idx, test_idx)`。`step` 默认等于 `test_len`,各折测试窗首尾相接、不重叠(`step < test_len` 会报错,否则样本外收益会重复计入)。`anchored=True` 时训练段起点固定,越滚越长。放不下完整测试窗的尾部会丢弃。窗口长度本身也算一次试验,应登记。
- **锁定段守卫**:`locked_fingerprint(dates, values)` 对锁定段数据取 sha256;`LockedTestGuard(split, log_path, fingerprint)` 在日志首行写 seal(封存指纹),之后只能用 `guard.open(label)` 取得锁定段区间,每次访问追加一行 JSONL;同一 label 不能第二次 open,换进程/新建实例同样有效。数据或切分被改会因指纹/区间不一致而报错;日志损坏、seal 缺失或重复也会拒绝放行。日志路径由调用者指定,文件名不得含项目试验账本的保留名,建议 `locked_access_log.jsonl`。
- **并发与校验**:构造和 `open` 在同一把文件独占锁(`fcntl.flock`,加在日志文件自身上)内完成"读日志→查重→追加",所以多线程/多进程同时 open 同一 label 只有一个成功;无 `fcntl` 的平台(Windows)退化为无锁。日志每行的字段(label、区间、指纹)都校验类型,open 记录的指纹/区间必须与 seal 一致;守卫只接受整数、非负、非空、顺序且不重叠的切分。
- **局限**:这是"留痕与检出改动",不是访问控制——调用者手里的数组谁都能直接读。它防的是自己无意中反复偷看。新 label 仍会放行,只是留痕;报告里应展示 `open_count()`。
- 日期序列必须严格升序、无重复、够长,否则抛 `SplitError`(带位置信息)。

### 4.2 A 股成本与成交约束(S2,`src/validation/costs.py`)
- **逐笔场景**:`apply_costs(trades, params)` 输入逐笔委托(`Trade` 或 dict:date、side、price、shares,可选 code、limit_up、limit_down、suspended、near_limit),返回 `CostResult`:每笔的成交价(含滑点)、佣金/过户费/印花税、滑点成本、是否被拦截及原因,以及已实现的毛盈亏与净盈亏(移动平均成本法,未平仓部分不计)。
- **规则**:佣金双边、有最低收费;印花税只在卖出;过户费双边;滑点按比例(买价上移、卖价下移),`near_limit` 的成交额外加大;T+1(当日买入的股数当日不可卖);涨停不可买、跌停不可卖;停牌不可成交;买入按 100 股向下取整,卖出超出可卖持仓会被截断,非清仓时也取整到整手(清仓允许零股尾数)。被拦截的单不产生费用、不改变持仓,也不会顺延到下一天。
- **默认值不是事实**:`CostParams` 的费率(佣金 0.025%、最低 5 元、过户费 0.001%、印花税 0.05%)复制自 `src/simtrade.py` 的 `FEE["stock"]`,那里 `FEE_VERIFIED=False`;滑点 5bp、涨跌停附近额外 20bp 是占位假设。**需所有者核对当前费率**后再用于任何结论。
- **敏感表**:`stress_table(trades, {"slippage_bps": [0, 5, 20], "commission_rate": [0.00025, 0.001]})` 逐格重算净收益;参数越大净收益不会更高。
- **局限**:这里是"逐笔、有价格有股数"的口径,只用于有成交列表的场景。仅有收益序列时的"换手 × 成本倍数"压力测试是另一条路径(见 5.1),需要换手、基准成本和含费声明。拦截只是丢弃该笔,并非模拟"挂单顺延";滑点是固定比例,不随成交量变化,不含冲击成本。

### 4.3 试验次数与多重检验折扣(S3,`src/validation/trials.py`)
- **为什么**:同一份数据上试了 N 组参数,"最好那组"的夏普天然偏高。必须记录试了多少次,并按次数折扣显著性。
- **账本**:`TrialLog(path).record(strategy, params, metric_name, value, n_obs, tag="")` 追加一行 JSONL(id、时间、策略名、参数哈希、指标),返回 id;`count(strategy=None)`、`summary()`、`load()`。路径由调用者指定,实例化不创建文件;文件名不得使用项目试验账本的保留名,建议 `trial_log.jsonl`。只追加、不覆盖;同一(策略, 参数)重复登记仍计数并标 `duplicate`(防"偷偷重跑");显式给的 id 重复会抛 `TrialError`;损坏行跳过并在 `summary()["bad_lines"]` 报告。参数哈希对键顺序不敏感(`params_hash`)。
- **多重检验校正**:`bonferroni(p, m)=min(1, m·p)`;`sidak(p, m)=1−(1−p)^m`(假设独立);`holm(ps)` 逐步法,不比 Bonferroni 差;`bh_adjusted/bh_fdr` 控制错误发现率 FDR(假设独立或正相关)。`bonferroni_threshold(alpha, m)=alpha/m`,试验越多门槛越严。
- **夏普的不确定性**:`sharpe_se` 用 Mertens 修正 `SE=√((1−γ₃·SR+(γ₄−1)/4·SR²)/(n−1))`(γ₃ 偏度、γ₄ 非超额峰度,由 `sample_moments` 样本估计,SR 为单周期未年化);`prob_sharpe_gt` 是 PSR=Φ((SR−SR*)/SE)。
- **Deflated Sharpe**:`deflated_sharpe(sr, n, skew, kurt, n_trials, var_sr_trials)` = 以 `expected_max_sharpe(N, V)≈√V·((1−γ)Φ⁻¹(1−1/N)+γΦ⁻¹(1−1/(Ne)))`(γ 为欧拉常数)为基准的 PSR,V 是各试验夏普的方差(可从账本里同一指标的 `var` 取得,注意口径要与 SR 同周期)。N 越大 DSR 越低。这是近似:假设试验近似独立、夏普近似正态;参数相近的试验高度相关,实际有效 N 比记录数小,所以偏保守。
- **bootstrap**:`sharpe_ci / mean_ci(r, block, n_boot, seed, alpha)` 用循环块重采样(块保留短程自相关),固定 seed 可复现,不动全局随机状态。区间只反映抽样波动,不含选择偏差,仍需另做试验折扣。
- **小样本**:n < 30 时 `sharpe_se / PSR / DSR / bootstrap 区间` 返回 NaN 并发警告,不给假精确的数字。
- **`min_trades_hint(mean, std)`**:按 `n≈(z·σ/μ)²` 与下限 30 取大,仅为经验提示;不是统计保证,达到该笔数也不代表策略有效。

### 4.4 绩效统计与样本内外对比(S4,`src/validation/stats.py`)
- **`perf_stats(returns, rf=0.0, trades=None, dates=None, nan_policy="error")`**:输入逐日**简单收益**(小数,必须 > -1;误传百分比会报错)。pandas Series 的索引自动当日期。
- **日级指标**(只需收益):`cum_return` 累计收益;`ann_return` = (1+累计)^(252/n)−1(几何年化);`ann_vol` = 样本标准差 × √252;`sharpe` = 日均超额收益 / 日标准差 × √252,`rf` 为年化无风险利率,日 rf = rf/252;`max_drawdown`(≤0)及 `max_dd_peak_date`(峰值日)/`max_dd_trough_date`(谷底日)/`max_dd_recovery_date`(首次回到峰值,None 表示至今未恢复);`calmar` = 年化收益 / |最大回撤|;`day_win_rate`、`day_profit_factor`(盈利日之和 / 亏损日绝对值之和)、`day_payoff_ratio`(平均盈利 / 平均亏损)、`day_expectancy`(日均收益)、`max_losing_days`(最大连续亏损日数)。胜率与盈亏比只统计有盈亏的日子,收益恰为 0 的日子单列 `n_zero_days`,免得长期空仓的策略被稀释。
- **交易级指标**(必须传 `trades`,含逐笔 `ret`):`trade_count`、`trade_win_rate`、`trade_profit_factor`、`trade_expectancy`、`max_losing_trades`。逐日收益恢复不出逐笔交易,`trades=None` 时这些键为 None 且 `trade_status="unavailable"`,不拿日级数字顶替。日胜率与笔胜率是两件事,不要混用。连续亏损按 `exit_date` 排序后数;所有交易都没有 `exit_date` 时按传入顺序(`trade_order="input_order"`);只有部分有、或日期类型无法比较时抛 `StatsError`,不会静默改用传入顺序。
- **最大回撤区间**:净值从 1 起算;净值回到或超过历史峰值就重置峰值位置,所以区间从"最近一次触及峰值"起算,不会跨越已经恢复的前一次回撤。
- **日期与数值稳定性**:传入的日期(或 Series 索引)必须无 NaN/NaT、类型可比较且严格升序,否则抛 `StatsError`(倒序日期会让回撤起止日失去意义);交易的 `exit_date` 含 NaN 同样报错。最大回撤用对数净值计算,极长或极大收益序列下净值连乘溢出时,回撤不会被静默算成 0;此时累计收益溢出降级为 None 并在 `warnings` 提示。
- **盈亏比边界**:全是亏损日时 `day_profit_factor` = 0(与 `trade_profit_factor` 一致),`day_payoff_ratio` 为 None(没有平均盈利);全是盈利日时二者都是 None。
- **稳健性**:空序列、非数值、inf、≤ -100% 的收益抛 `StatsError`;含 NaN 默认也抛错,`nan_policy="drop"` 剔除并记入 `n_dropped_nan`;单点、全零(标准差为 0)、没有回撤、没有亏损日这些情形对应指标返回 None,原因写进 `warnings`,不给无穷大或假精确;样本 < 30 天也会提醒。
- **`compare_segments(train, validate, test, rf=0.0, decay_warn=0.5)`**:三段各算一份 `perf_stats`,再对夏普、年化收益、卡玛算**衰减** = (训练 − 本段) / 训练:0.5 表示掉了一半,1 表示掉到 0,>1 表示变成负数,负数表示反而更好。训练指标 ≤ 0 时衰减没有意义,返回 None。`hints` 是带数值的提示字符串(衰减超过阈值、符号翻转、段太短),**只是提示,不是"过拟合/有效"的结论**;`validate`/`test` 传 None 记为 unavailable。
- **怎么读**:训练段是选参数的那段,天然偏乐观,衰减大不稀奇;但衰减小也不代表没问题,试验次数、成本、样本量要另看(见 4.2、4.3)。年化收益在短样本上会被外推放大,建议和 `cum_return`、`n_obs` 一起看。

## 5. 常见误区与警告
过拟合、多重检验、样本量不足、交易成本低估;锁定测试段只能看一次。

> **警示:自动批量生成/搜索策略之前,必须先做两件事。** ① 建好 `TrialLog`,每一次尝试(每组参数、每个策略、每个窗口长度)都登记,不登记的尝试等于偷偷多试;② 先用 `three_way_split` + `LockedTestGuard` 封存锁定测试段,搜索只许用训练/验证段。试得越多,"最好的那个"越可能只是运气(第 11 节演示:300 个噪声策略里挑最好的,原始 p 值约 0.007,看着显著,Bonferroni 校正后接近 1)。本工具包不做批量生成,也不会替你登记;`adapter.run_grid` 传了 `ledger` 才登记。

## 5.1 输入不足时的行为(S2/S5/S7 补全)
- 只给收益 CSV(date, ret)时:切分、滚动前进、试验折扣、统计照常;成本压力与成交约束章节写"无法检验"及原因(缺逐日换手/基准成本/含费声明),并给强警告,不当作通过。想检验成本,请额外提供三项:turnover 列、base_cost(1x 单边成本率),以及 `--cost-included`(收益已含费)或 `--gross`(税前收益)之一;缺任何一项都是"无法检验",不会默认当作已含费。这些输入经 `RunRecord` 传给 `build_report`。
- 模拟盘对比需要调用者提供每日估值价格(marks):无成交日也按估值价重估净值;缺价默认报错,可选 ffill 但会标注陈旧估值日。

## 6. 不做什么
不接网站、不碰 app.py、不批量生成策略、不实盘、不联网。

## 7. 运行测试
- S1:`python3 -B tests/test_validation_splits.py`(合成数据 + 临时目录,不读写 data/)
- S2:`python3 -B tests/test_validation_costs.py`(纯合成数据,不读写文件)
- S3:`python3 -B tests/test_validation_trials.py`(合成数据 + 临时目录,账本只写临时目录)
- S4:`python3 -B tests/test_validation_stats.py`(纯合成数据,不读写文件;有 pandas 时多跑一项与 `src/metrics.py` 的交叉校验,没有则跳过)
- S5:`python3 -B tests/test_validation_paper.py`(合成数据 + 临时目录,数据库只建在临时目录)
- S6:`python3 -B tests/test_validation_adapter.py`(合成数据 + 假回测函数 + 临时目录;真实 `portfolio.run_weights` 一致性测试需要 pandas,没有则跳过)
- S7:`python3 -B tests/test_validation_report.py`(合成 csv + 临时目录;含命令行入口的子进程测试)
- S8:`python3 -B tests/test_validation_e2e.py`(端到端:确定性合成数据 + 临时目录 + 命令行子进程,约 1 秒)
- 全部测试:对 `tests/test_validation_*.py` 逐个执行,每个都以退出码表示成败。

## 8. 模拟盘日志 `src/validation/paper.py`(S5)
独立的 SQLite 日志,不接网站。`PaperJournal(db_path)`:路径必须由调用者传入(没有默认值;拒绝 `simtrade.db`、`signal_ledger.db`;建议 `data/paper_journal.db`),导入模块不创建文件,实例化才创建;目录不存在会报错,不会自动建目录。文件名按不区分大小写比较,并检查符号链接指向的真实路径;库里已有表时,表名和列必须与本模块完全一致,否则拒绝且不做任何写入(不会往别人的库里建表)。事务的提交失败也会回滚,连接可继续使用。
- 写入:`add_signal(date, code, side, theory_price, strategy_id)`、`add_fill(signal_id, price, shares, ts, fee=0, note="")`、`add_miss(signal_id, reason, note="")`(reason ∈ limit/suspended/too_late/other)、`add_mark(date, code, price, source="")`(每日估值价,由调用者提供)。全部参数化查询 + 单事务,校验失败整体回滚。
- 幂等:信号按 (date, code, side, strategy_id)、成交按 (signal_id, ts, price, shares)、漏单按 signal_id、估值价按 (date, code) 去重;重复提交且内容一致返回已有 id / 忽略,内容冲突(理论价、费用、原因、估值价不同)抛 `PaperError`,不覆盖。一个信号不能既有成交又有漏单。
- 查询与删除:`get_signal/list_signals/list_fills/list_misses/list_marks`;`delete_signal`(级联删成交与漏单)、`delete_fill`、`delete_miss`。
- `report(start, end, initial_cash, bt_returns, calendar, missing_mark="error")`:
  - 滑点(正 = 对自己不利):买入 (成交价−理论价)/理论价,卖出 (理论价−成交价)/理论价;给逐笔平均与成交额加权两种。
  - 漏单率 = 漏单信号 / (有成交信号 + 漏单信号),无成交也无漏单的信号算"待定",不进分母;附原因分布。
  - 净值逐日 = 现金 + Σ 股数×估值价,持仓期间无成交的日子也重估;缺估值价默认抛错,`ffill` 时沿用并列入 `stale_mark_days`。窗口起点假设无持仓:`start` 之前若已有成交,`report` 直接抛 `PaperError`(不输出错误净值),请从第一笔成交日起算;净值 ≤ 0 同样报错。
  - 报告截止日 `end` 之后才发生的成交不计入滑点/漏单统计(与净值口径一致,对应信号算"待定")。成交时间 `ts` 会规范成 `YYYY-MM-DD HH:MM:SS`(`T` 与空格写法等价,不接受带时区的时间)。
  - 跟踪误差 = (模拟盘日收益 − 回测日收益)的样本标准差 × √252,只比较日历内两边都有值的日子(缺的列入 `bt_missing_days`);另给累计偏差与逐日归因(滑点成本、费用、漏单数、陈旧估值)。
  - 提醒:模拟盘天数少(< 20)时跟踪误差和滑点都只是粗估;理论价取法不同(收盘价还是次日开盘价)会直接改变"滑点"的含义,需自己统一口径。

## 9. 适配器 `src/validation/adapter.py`(S6)
把"逐日收益 + 交易列表"统一成与引擎无关的 `RunRecord(dates, returns, trades, turnover, meta)`(不可变;日期为 `datetime.date`,收益为逐日简单收益小数)。后续模块只吃 `RunRecord`。适配器不创建、不写任何文件。
- 降级入口(显式,不依赖 src、不依赖 pandas):`from_returns(dates, returns, trades=None, turnover=None, base_cost=None, cost_included=None, nan_policy="error")`、`from_csv(path, date_col="date", ret_col="ret", turnover_col=None, ...)`、`from_trades(trades)`、`trades_from_csv(path)`。校验:长度一致、日期严格升序且无重复、收益有限且 > -1、turnover 非负;含 NaN 默认报错,`nan_policy="drop"` 剔除并把个数写进 `meta["n_dropped_nan"]`。`cost_included` 默认 None(未声明),不会替你猜。
- 仅交易列表:`has_daily=False`,`record.require_daily("夏普")` 会抛 `AdapterError`(无法恢复逐日净值,不做假精确);只能用 `trade_returns()` 做交易级指标。
- 主路径:`from_strategy(df, "trend_vol_target", {"sma_len": 200}, code="X", cost=0.0005, band=0.0, limit=0.10)` 在函数内延迟导入 `src.strategies` 与 `src.portfolio.run_weights`,只读调用(不改 src、不碰 data/、不联网)。`df` 由调用者提供。策略名必须在 `KNOWN_STRATEGIES`;导入失败会抛 `AdapterError` 并提示改用降级入口,**不会静默切换**。真实路径的收益已由 `run_weights` 扣费,所以 `meta["cost_included"]=True`、`meta["base_cost"]=cost`,`meta["source"]="portfolio.run_weights"`。测试可注入 `strategy_fn` / `run_fn`:来源只看有没有注入 `run_fn`——注入了 `run_fn` 才是 `source="injected_runner"`(这是设计文档之外的第三种取值,S7 需认可;此时 `cost_included` 由调用者声明,默认 None);只注入 `strategy_fn` 时仍调用真实 `run_weights`,标注为 `portfolio.run_weights` 且 `cost_included=True`。
- `meta` 保留键:`source`、`base_cost`、`cost_included`、`n_dropped_nan` 由适配器自己校验写入,调用者传的 `meta` 含这些键会抛 `AdapterError`(防止伪造来源或含费声明);其余键照常保留。`from_returns` 与 `from_trades` 的 `base_cost` 都不能为负;日期字符串严格解析(`2020-01-01xx` 报错)。直接构造 `RunRecord(...)` 不做校验,请通过上述工厂函数获得。
- `run_grid(df, strategy, grid, ledger=None, tag="")`:`grid` 由调用者显式给出,按参数名排序做笛卡尔积,上限 500 组。传 `TrialLog` 就**每组都登记**(指标 `sharpe_daily` = 日均值/日标准差,未年化,与 trials 同口径);所有回测成功后才统一登记,中途报错则一组都不登记。不传 ledger 时返回 `untracked=True` 与警告"这批试验未计入试验次数"。某组夏普无法计算(零波动)时仍登记(value 记 0.0,tag 含 degenerate),因为试验确实做过。
- `rerun_with_held(df, held, ...)`:用受限后的持仓路径重算收益。内部传 `target = held.shift(-1)`(末行补 0)、`band=0`、不传 limits,保证重跑后 `weights` 逐日等于 `held`。`held` 日期必须与 `df.index` 一致。**限制**:`run_weights` 内部 `shift(1)`,第 0 天持仓恒为 0,所以 `held[0]` 必须为 0,否则抛 `AdapterError`(不悄悄丢掉第一天的仓位)。
- 对齐工具:`check_calendar(record, calendar)` 只比对日历(缺日/多日),不猜交易日历;`align_pair(a, b)` 取两条记录的日期交集(回测 vs 模拟盘)。
- 提醒:回测收益好看不等于有效;`run_grid` 每多试一组都应登记,否则多重检验折扣会被低估。

## 10. 统一报告与命令行(S7,`src/validation/report.py`、`scripts/validate_strategy.py`)
把前面各模块串成一份 Markdown 报告,固定章节:数据与切分、样本内/验证/锁定测试、滚动前进、成本压力、试验次数与折扣、模拟盘对比(可选)、自检清单、警告;文末写明"仅供研究,不构成投资建议,回测与模拟结果不代表未来"。

命令行(只读你给的文件,只写 `--out`,默认拒绝覆盖,不碰项目数据目录、不联网):

    python3 -B scripts/validate_strategy.py --returns ret.csv --out report.md

完整参数:`--trades trades.csv`(列 entry_date, exit_date, ret)、`--turnover-col turnover --base-cost 0.0005 (--cost-included | --gross)`、`--trial-log trial_log.jsonl [--strategy 名]`、`--train/--valid/--embargo`、`--wf-train/--wf-test`、`--locked-open-count N`、`--force`。退出码 0 成功,2 为输入或输出路径有问题(原因输出到 stderr)。`--out` 不能与输入文件相同,即使加了 `--force`。

Python 接口:`build_report(record, params=None, trial_log=None, cost_inputs=None) -> (text, summary)`;`build_report_from_arrays(dates, returns, ...)`;`warnings_for(summary)`;`write_report(path, text, force=False)`;`cost_stress(...)`。params 里写了不认识的键会报错(防拼写错误)。

要点:
- **成本压力**需要三项:逐日换手、1x 单边成本率、含费声明。缺任何一项都写"无法检验"并列出缺哪项,同时给强警告;不会当成通过。公式是乘性的:`cost_included=True` 时 `r'=(1+r)(1−c'T)/(1−base·T)−1`(先还原已扣费用),`False` 时 `r'=(1+r)(1−c'T)−1`,`c'=base×倍数+滑点bp/1e4`。这条路径不含涨跌停、T+1、整手(需逐笔数据,见 4.2)。
- **滚动前进**默认窗口 252/63 根只是占位,不是推荐值;窗口长度本身也算一次试验。该序列是已跑完的策略,所以这里看的是各时间段是否稳定,不是重新调参。
- **试验折扣**:给了账本才有。N 来自账本记录数(可按 `strategy` 过滤);Bonferroni/Šidák 作用在"夏普>0"的单边 p 值上;DSR 需要账本里至少 2 条 `sharpe_daily` 指标来估计各试验夏普的方差,否则写明无法计算。
- **自检清单**:是否留 embargo、锁定测试段是否已被访问(需自行传入 `locked_open_count`,不传为"未知")、交易笔数、样本年数、是否登记试验、成本是否检验。
- **警告**:成本未检验、2x 成本后夏普转负、试验数 ≥ 阈值(默认 20)或未登记、样本 < 3 年、交易 < 30 笔、验证/测试衰减 > 50%、模拟盘 < 20 天、单日收益 > 100%(疑似百分数口径)。这些是提示不是结论;没触发警告也不等于没有问题。
- 局限:报告不给"策略有效"的结论;成本费率是调用者假设(项目默认值未与真实账户核对);多次试验、窗口选择、参数选择都会让结果偏乐观。

## 11. 端到端演示(S8,`tests/test_validation_e2e.py`)
全部使用固定随机种子的合成数据和临时目录,不读写 data/,不联网,可重复。
- **纯噪声策略被大量试验后挑出最好者**:生成 300 条零均值噪声"策略"收益(各 1000 天),全部登记进 `TrialLog`,取日夏普最高的一条。单看它,单边 p 值约 0.007(像是显著);按 N=300 做 Bonferroni 校正后为 1.0、Šidák 约 0.87,Deflated Sharpe 约 0.37(< 0.95),折扣后不再显著。这是"挑最好的"陷阱的直观演示。
- **有真实漂移的序列**:日均收益 0.12%、日波动 1%、1500 天,只登记了 5 次试验,折扣后 Bonferroni p < 0.01 且 DSR > 0.95。这是人为构造的强信号,真实策略很少这么干净;即便"通过",报告也只写"没能证伪",常驻警告照旧。
- **完整流程**:三段切分 → `LockedTestGuard` 封存指纹 → 报告(含滚动前进、成本压力)→ 打开锁定段(同标签第二次被拒、改数据被检出)→ 模拟盘日志与回测对比(无成交日按估值价重估净值)→ 命令行生成报告(拒绝覆盖)。
- 这些断言是工具行为的回归测试,不是对任何真实策略的结论。

## 12. 怎么读一份报告
1. 先看第 8 节警告和第 7 节自检清单:哪些项是"否/未知",哪些检验"无法检验"。无法检验不等于通过。
2. 第 1–2 节:三段是否够长;验证/测试相对训练的衰减。训练段天然偏乐观;测试段只有一次机会,看过就不再干净(`locked_open_count`)。
3. 第 3 节滚动前进:看各折测试夏普的符号是否稳定、拼接样本外的夏普,而不是挑最好的一折。窗口默认值只是占位。
4. 第 4 节成本压力:2x/3x 成本和额外滑点后夏普、年化是否还为正。缺换手/基准成本/含费声明时整节是"无法检验"。
5. 第 5 节试验折扣:看 N、Bonferroni/Šidák 调整后的 p 和 DSR,而不是原始 p。没给账本就没有折扣,等于假设只试了一次。
6. 第 6 节模拟盘(可选):天数少于 20 时只是粗估。

## 13. 已知局限
- 锁定段守卫是"留痕与检错",不是访问控制:调用者手里的数组谁都能直接读。
- 成本压力走"换手 × 倍数"的乘性公式,不含涨跌停、T+1、整手、冲击成本;逐笔口径见 `costs.apply_costs`。默认费率复制自 `src/simtrade.py`,那里 `FEE_VERIFIED=False`,需所有者核对。
- 多重检验假设试验近似独立;参数相近的试验高度相关,有效 N 小于记录数,折扣偏保守。DSR 的方差来自账本里的 `sharpe_daily`,口径需与被检策略一致。
- 样本只有几年时 PSR/DSR/bootstrap 区间很宽,这是正确现象,不是工具缺陷;n < 30 返回 NaN。
- 滚动前进检验的是已跑完策略的稳定性,不重新调参;窗口长度本身也算一次试验。
- 统一报告的模拟盘章节目前只带覆盖天数;完整对比请直接用 `PaperJournal.report` 的返回值。
- 账本、守卫日志是追加式文本文件,文件锁依赖 `fcntl`(Windows 退化为无锁);不防人直接编辑文件。
- 合成数据演示证明的是代码行为,不能证明任何真实策略有效。

## 14. 与模拟盘日志配合
1. 回测侧:用 `adapter.from_strategy`(或 `from_returns`)得到回测日收益,整理成 `{日期: 日收益}`。
2. 模拟盘侧:每天用 `PaperJournal` 登记信号 `add_signal`、成交 `add_fill`、漏单 `add_miss`,并登记每个持仓标的的收盘估值价 `add_mark`(调用者自己取价,模块不联网)。
3. 事后 `journal.report(start, end, initial_cash, bt_returns, calendar)`:得到滑点、漏单率与原因、逐日净值、跟踪误差、累计偏差与逐日归因。`start` 取第一笔成交日。
4. 把覆盖天数传给 `build_report` 的 `paper_days`,少于 20 天会触发警告。
5. 解读:跟踪误差大,先查是成交价(滑点)、成本、漏单(涨跌停/停牌)还是陈旧估值价;理论价取法(收盘价或次日开盘价)必须与回测成交口径一致。
6. 数据库路径由你指定,建议 `data/paper_journal.db`(data/ 不进仓库);不要指向 `simtrade.db`、`signal_ledger.db`。

## 15. 收尾自查(S8)
- 新增文件无密钥、无个人绝对路径、无被禁字符串。
- 所有 `tests/test_validation_*.py` 只用合成数据与临时目录,可用 `python3 -B` 直接运行。
- `python3 -m py_compile src/validation/*.py scripts/validate_strategy.py tests/test_validation_*.py` 全部通过。
