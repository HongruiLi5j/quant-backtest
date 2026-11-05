# Python 版 MA+ADX+ATR 与 AmiBroker AFL 规则对照表（C10）

对照对象：`amibroker/ma_cross_adx_atr.afl`（92 行）↔ `src/strategy.py`（信号）+ `src/backtest.py`（逐根回测）+ `src/indicators.py`（指标）。
对照方式：逐行读代码（静态）+ `scripts/afl_python_consistency.py` 把 AFL 的 for 循环逐行翻译成 Python 后在合成行情和本地 8 只 ETF 上实测（动态）。
回归护栏：`.venv/bin/python tests/test_afl_consistency.py`。

**重要限制：这台机器没有 AmiBroker，仓库里 `amibroker/` 下也没有任何导出文件（只有 .afl），所以 `compare_amibroker.py` 没有历史对比结果可引用。**
下面「动态」那一列证明的是「按我对 AFL 语义的理解，两边循环等价」，不是 AmiBroker 真的跑出来的结果。
AmiBroker 内部指标函数（`ADX()`、`ATR()`、`MA()`）的数值细节无法在这里验证，见第 3 节。

## 1. 逐条对照

| # | 项目 | AFL（行号） | Python（位置） | 是否一致 |
|---|---|---|---|---|
| 1 | 均线类型和周期 | `MA(C, Fast)`、`MA(C, Slow)`，默认 5 / 20（L24-25, L31-32） | `sma()` = 收盘价 `rolling(n).mean()`，`Params` 默认 5 / 20（`strategy.py:20-21`、`indicators.py:11-13`） | 一致（简单均线、收盘价） |
| 2 | ADX 周期 / 下限 | `ADX(ADXLen)`，14；`ADXv > ADXMin`，20（L26-27, L33, L36） | `adx(df, 14)`，`adx > 20`（`strategy.py:22-23`、`:34`） | 参数和比较方向一致（严格大于）；指标数值未验证（第 3 节） |
| 3 | ADX 平滑 | AmiBroker 内置 `ADX()`，文档称 Wilder 平滑，实现细节看不到 | `wilder()`：前 n 个简单平均作种子，之后 `w=w+(x-w)/n`；DM、TR 各平滑一次，DX 再平滑一次（`indicators.py:16-33`、`:52-67`） | 公式层面是 Wilder；种子和预热期是否和 AmiBroker 相同**未验证** |
| 4 | ATR 周期 / 平滑 | `ATR(ATRLen)`，14（L28, L34） | `wilder(true_range, 14)`，首根 TR = High−Low（`indicators.py:35-49`） | 同上：公式一致，种子**未验证** |
| 5 | 买入信号 | `Cross(MAf, MAs) AND ADXv > ADXMin`（L36） | `cross(fast, slow) & (adx > adx_min)`（`strategy.py:34`） | 一致；`cross` = 今天 a>b 且昨天 a<=b，与 AFL `Cross` 同义（`indicators.py:85-87`） |
| 6 | 卖出信号 | `Cross(MAs, MAf)`（L37） | `cross(slow, fast)`（`strategy.py:35`） | 一致 |
| 7 | 入场时点和价格 | 用 `BuySig[i-1]`，在第 i 根开盘价买（L68-70） | `buy_sig[i-1]`，`px = o[i]`（`backtest.py:75-77`） | 一致（信号在收盘，次日开盘成交，无未来函数） |
| 8 | 入场前提 | 空仓，且 `ATRv[i-1]` 非 Null（L68） | 空仓，且 `not isnan(atr_[i-1])`（`backtest.py:75`） | 一致 |
| 9 | 止损价 | `O[i] - ATRMult * ATRv[i-1]`，入场后固定（L72） | `px - atr_mult * atr_[i-1]`，入场后固定（`backtest.py:82`） | 一致（用信号当天 ATR，不是入场当天） |
| 10 | 止损倍数 | 默认 2，范围 0.5~5（L29） | `atr_mult = 2.0`（`strategy.py:23`） | 一致 |
| 11 | 出场优先级 | 先判 `SellSig[i-1]`，再判止损（L51-65） | 先判 `sell_sig[i-1]`，`elif` 再判止损（`backtest.py:67-74`） | 一致（同一根上两者都触发时按信号价 = 开盘价出，且原因记为 signal） |
| 12 | 信号出场价 | 第 i 根开盘价（L53） | `o[i]`（`backtest.py:68`，同一根上信号优先） | 一致 |
| 13 | 止损出场价 | `O[i] <= 止损` → 开盘价；否则 `L[i] <= 止损` → 止损价（L57-64） | `o[i] <= stop` → 开盘价；否则 `l[i] <= stop` → 止损价（`backtest.py:70-74`） | 一致（跳空低开按开盘价，盘中触及按止损价） |
| 14 | 入场当根是否查止损 | 不查（入场在 `else if` 分支，下一根才进 `if (inPos)`）；`i > entryBar` 判断实际上恒真 | 默认不查（`stop_on_entry_bar=False`）；该开关打开时才查（`backtest.py:21`、`:70`、`:83`） | 默认一致；Python 多一个 AFL 没有的开关，且默认关 |
| 15 | 同根卖出后能否再买 | 不能（`if / else if`，同一次迭代只走一个分支）（L49, L68） | 不能（`if shares>0 ... elif buy_sig`）（`backtest.py:66-75`） | 一致：最早下一根才能再入场 |
| 16 | 循环起点 | `i = 1`（L47） | `if i > 0`（`backtest.py:64`） | 一致 |
| 17 | 手续费 | `CommissionMode=1`（按成交金额百分比），`CommissionAmount=0.05`（L15-16） | `commission_pct = 0.0005`，买卖各收一次（`backtest.py:20`） | 一致（0.05% = 0.0005，单边） |
| 18 | 初始资金 | 100000（L14） | `initial_equity = 100_000`（`backtest.py:19`） | 一致 |
| 19 | 仓位 | `SetPositionSize(100, spsPercentOfEquity)` + `RoundLotSize=1` | `qty = floor(cash / (px*(1+commission_pct)))`（`backtest.py:78`） | 近似一致：Python 把手续费算进买入成本，AmiBroker 是否这样取整未验证（可能差 1 股，只影响盈亏金额，不影响日期） |
| 20 | 数据末尾仍持仓 | 不平仓 | 以最后收盘价强制平仓，记为 `end_of_data`（`backtest.py:88-89`） | 有差异，但 `compare_amibroker.compare_trades` 已把 `end_of_data` 先去掉（`compare_amibroker.py:43`） |

**规则层结论：20 项里没有发现规则写法不同。** 默认参数、比较方向、信号用前一根、成交用当根开盘价、止损价与跳空处理、出场优先级全部一致。

## 2. 实测（动态）结果

数字来自 `outputs/C10/afl_python_consistency.json`（脚本真实输出）。

| 检查 | 范围 | 结果 |
|---|---|---|
| 静态：AFL 参数 / 选项 vs `Params` / `Config` 默认值 | 6 个 Param + InitialEquity + 手续费 | 8 / 8 一致 |
| 静态：16 条关键语句是否存在于 AFL 文本 | 见第 1 节 | 16 / 16 存在 |
| 循环翻译 vs `backtest.run`，**未取整**合成行情 | 4 组参数 × 30 个种子 × 2000 根；每边 5019 笔 | 信号 120 / 120 次运行完全一致；交易逐笔 0 笔不一致；三种出场分支（signal / stop / stop_gap）都走到 |
| 同上，价格**取整到分**（会出现 SMA5 = SMA20 精确并列） | 同上 | 信号一致的运行 111 / 120；其余 9 次的差异 K 线（共 18 根）全部紧邻一个并列 K 线（共 21 根并列，4 组参数累计） |
| 循环翻译 vs `backtest.run`，**本地真实 ETF**（EFA、GLD、IEF、IWM、QQQ、SHY、SPY、TLT，2005-01-03 ~ 2026-03-31） | 每只 5344 根；共 491 笔 | 0 笔不一致，信号差异 0 根（EFA 有 1 根并列，未造成差异） |

## 3. 已发现的差异（都不是规则写法不同，是实现 / 数据层面）

| 编号 | 差异 | 实测影响 | 建议 |
|---|---|---|---|
| D1 | **导出给 AmiBroker 的 CSV 取 4 位小数，Python 读全精度**（`src/data.py:40-47`，`float_format="%.4f"`；`compare_amibroker.py:89` 的 `data.load` 读的是全精度文件） | 同一份 8 只 ETF，全精度 vs 取 4 位小数、各跑 Python 版：**4 / 8 只有信号差异**。SPY 信号差 2 根、1 笔交易日期不同、价格最大差 1.52；QQQ 交易数 69 → 68；SHY 53 → 54；EFA 信号差 6 根、1 笔日期不同。机理有两种：均线差只有 1e-6 量级的「近似并列」被取整推过 0（上穿提前或推后一天）；SHY 这类日内波幅极小的品种，ADX 被取整改变 3~4 个点（16.78 → 20.17）从而跨过 20 的下限 | 对比时让 Python 也读取整后的数据（或导出时多留小数），否则 `compare_amibroker.py` 会报「不一致」，但根因是数据不是规则。改 `src/data.py` 超出本队列范围，只报告 |
| D2 | **均线精确并列时 `Cross` 对浮点舍入敏感** | 取整到分的合成数据上，并列根附近的上穿 / 下穿会差一天，进而交易日期、之后的状态整体错位（同一对种子上最多 20 笔「不一致」是错位连带）。真实 ETF 8 只 × 5344 根里并列只有 1 根（EFA） | 属性质限制；AmiBroker 的 `MA()` 浮点运算精度与 pandas 不同（我记得 AmiBroker 数组是 32 位浮点，**本机无法验证**），并列根上两边可能判定不同 |
| D3 | **Wilder 平滑种子 / 预热期是否与 AmiBroker 相同未验证** | 只量化了 ATR：把种子从「前 14 个平均」换成「第 1 个值」，ATR 最大相对差 30 根后 2.46%、50 根后 0.60%、100 根后 0.016%、200 根后 8e-8（理论 (13/14)^100 = 0.060%）。ADX 是二次平滑，收敛更慢，没单独量化。`compare_amibroker.py` 跳过前 100 根、容差绝对值 1e-3，对 SPY 这类 ATR 约 6 的品种，0.016% 的相对差就接近 1e-3，可能报假 ⚠️ | 站主跑一次 Explore 导出，用 `--explore` 看真实差异；容差可改成相对误差（超出本队列范围，只建议） |
| D4 | 买不起 1 股时状态不同 | 价格 > 本金（合成：最低开盘价 451,200；11 次买入信号）：Python 因 `qty==0` 不开仓（0 笔），AFL 脚本自己的 `inPos` 仍置 True（脚本状态 11 次入场），之后会按它自己的信号 / 止损「卖出」 | 本地 8 只 ETF 都不会触发；只在股价高于本金时有意义 |
| D5 | Python 多了 `stop_on_entry_bar` 开关 | 默认 False，与 AFL 一致 | 保持默认关；若打开就和 AFL 不一致 |
| D6 | 预热期 Null 语义 | AFL 里 Null 参与比较时的行为（我记得 AmiBroker 把 Null 当作 −1e10）本机无法验证；若如此，慢线第一根有效 K 线上可能出现一个伪「卖出信号」，Python 不会（NaN 比较为假）。但 ADX 要 27 根才有值（实测首个有效 ADX 在第 27 根），此时不可能持仓，所以不影响交易 | 无需处理 |

## 4. 边界：这份 AFL 对应哪份 Python

AFL 文件头写明对应 `src/strategy.py + src/backtest.py`，上面的核对针对这两份。网站和三引擎实际用的 `src/strategies.py::ma_adx_original`（及 backtrader / RQAlpha 的对应策略）**没有 ATR 止损**，
信号和 AFL 的买卖信号相同，但出场只有「下穿」，仓位用组合引擎的目标仓位——它们和 AFL 是「信号同源、执行不同」，不是同一个回测。三引擎之间的一致性见 `docs/engine_rules_compare.md`（C09）。

## 5. 一个被证伪的假设（留档，免得以后重复担心）

我最初怀疑：价格整段不动（停牌、一字板）会使 TR、DM 平滑值为 0，`PDI/MDI` 变成 0/0 = NaN，而 `wilder()` 遇到 NaN 后会一直是 NaN，ADX 永久丢失。实测：
- 序列中段出现 30 根不动的 K 线：ADX 有效 273 / 300 根，flat 之后 0 根 NaN，没有永久丢失。原因：此前平滑值非零，递推只会衰减、不会精确变成 0。
- 序列开头就有 30 根不动的 K 线：首个有效 ADX 在第 43 根（正常是第 27 根），之后恢复，没有永久丢失。
