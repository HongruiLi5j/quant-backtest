# 权威因子库（24 个）

代码：`src/market_monitor/factors.py`。只用日线 OHLCV + 名称/行业；全部是 pandas 向量化的宽表函数（行=交易日，列=代码）。
这是**当日监控/复盘用的打分函数，不是被验证过的 Alpha**：没有样本外检验、没有扣成本、24 个同时看有多重检验问题（碰巧"显著"的会混进来）。

## 通用约定
- 方向：+1 表示值越大越好；-1 表示值越小越好。score 因子的"触发集" = 方向调整后截面排名**严格高于**建议分位（默认 0.9，即最靠前的 10%）。flag 因子 True 即触发；filter 因子 True 为通过。
- 数据不足：前 `min_bars-1` 行强制为 NaN（score）或 False（flag/filter），不用伪值。
- 未来函数：第 t 行只用 ≤t 的数据。`check_no_lookahead(id, panel, cut)` 把面板截到前 cut 行重算，第 cut-1 行必须与全量一致；测试对 24 个因子全部执行（含一个故意偷看明天的反例）。
- 数据局限（影响所有因子）：本地日线是**后复权价**，`close×volume` 只是成交额的近似（复权倍数会放大它）；沪深300 不在本地，"大盘"默认用全市场等权收益；ST 状态来自当前名称快照；没有流通股本，换手率用量比代理。

## 来源的写法（"精确可查"原则）
- 只有 **wq12、wq54** 是 WorldQuant 101 Alphas（Kakushadze 2016）里的原式，来源写 `Alpha#12` / `Alpha#54`，原公式贴在 `factors.py` 的函数注释里：
  Alpha#12 = `sign(delta(volume, 1)) * (-1 * delta(close, 1))`；Alpha#54 = `(-1 * ((low - close) * (open^5))) / ((low - high) * (close^5))`。
- 有明确文献的经典因子写作者+年份+期刊（Amihud 2002、Jegadeesh 1990、Jegadeesh-Titman 1993、Harvey-Siddique 2000、Ang et al. 2006、Blitz et al. 2011）。
- 来源写"自行实现，思路参考国泰君安 Alpha191 / MSCI Barra China (CNE5) 方法论"的，是**简化版**：我没有逐条核对原报告的编号和页码，所以不写编号，也不是官方公式。
- 技术分析教材里的经典指标（Chaikin 资金流、Close Location Value、Donchian 通道、均线排列）写"技术分析教材"，不挂 WQ101 编号。
- 早先版本把 close_pos 写成"WQ #28/#44 同类"、pv_corr5 写成"WQ #6/#13 同类"是不准确的（#28、#44 是量相关类公式，#6 是 -corr(open, volume, 10)），已更正。

## 流动性（4）
| id | 公式 | 来源 | 方向 | A 股适用性与局限 |
|---|---|---|---|---|
| amihud | mean(\|r_t\|/amount_t, 20)×1e8 | Amihud (2002) | -1 | 适用；amount 是近似，复权倍数会扭曲横截面比较 |
| turnover | volume_t / mean(volume, 60) | 自行实现，参考 MSCI Barra Liquidity（量比近似） | -1 | 不是真换手率，只反映"相对自己放量" |
| avg_amount | mean(close×volume, 20) | 自行实现，参考 MSCI Size/Liquidity | +1 | 偏大市值，等于一个规模暴露 |
| big_order | sum(((2C-H-L)/(H-L))×V,20)/sum(V,20) | Chaikin Money Flow | +1 | 只是日线上的"资金流"代理，不是逐笔大单；一字板为 NaN |

## 价量短期（8）
| id | 公式 | 来源 | 方向 | 局限 |
|---|---|---|---|---|
| wq12 | sign(Δvolume,1)×(-Δclose,1) | WQ 101 Alpha#12（原式） | +1 | 量纲是价格差，横截面比较偏向高价股（原文如此） |
| wq54 | -(low-close)×open⁵ / ((low-high)×close⁵) | WQ 101 Alpha#54（与原式数学等价，只改了数值写法） | +1 | 一字板除零为 NaN |
| pv_corr5 | corr(close, volume, 5) | 改编自量价相关类思路（非 WQ101 原式） | -1 | 5 日窗口噪声很大 |
| intraday_skew | skew(close/open-1, 20) | Harvey-Siddique (2000)；自行实现，参考 GTJA 同类因子 | -1 | 真正的日内偏度需要分钟数据，这里是日线近似 |
| gap_open | open_t/close_{t-1}-1 | 技术分析教材的跳空缺口；自行实现，参考 GTJA | -1 | 方向是假设（跳空回补），未验证 |
| hl_range | max(high,20)/min(low,20)-1 | 技术分析教材的区间振幅；自行实现，参考 GTJA | -1 | 与波动率高度相关 |
| close_pos | (close-low)/(high-low) | Close Location Value 的单日形式（技术分析教材，非 WQ101 原式） | +1 | 涨跌停日恒为 0 或 1，信息被限价截断 |
| reversal1 | -(close_t/close_{t-1}-1) | Jegadeesh (1990) | +1 | 跌停股得分最高但买不到（T+1/跌停不可买卖） |

## 动量/反转（4）
| id | 公式 | 来源 | 方向 | 局限 |
|---|---|---|---|---|
| mom252 | close_{t-20}/close_{t-252}-1 | Jegadeesh-Titman (1993) | +1 | 需 253 根；A 股长期动量证据弱于美股 |
| mom20 | close_t/close_{t-20}-1 | 改编自 Jegadeesh (1990) 月度反转；自行实现，参考 GTJA | -1 | 方向取反转，A 股 1 个月内常见反转，但未验证 |
| resid_mom | ret20 - 同行业 ret20 均值 | Blitz et al. (2011) | +1 | 行业来自 `cn_all_list.csv`；行业只有自己时为 0 |
| rel_strength | ret20 - 大盘 20 日累计 | 自行实现，参考 MSCI Barra 方法论 | +1 | 大盘缺省为等权，不是沪深300 |

## 波动率（2）
| id | 公式 | 来源 | 方向 | 局限 |
|---|---|---|---|---|
| low_vol | std(r, 60) | 自行实现，参考 MSCI Volatility；Ang et al. (2006) | -1 | 低波股往往也是大盘价值股，是风格暴露 |
| idio_vol | sqrt(Var(r,60) - β²Var(m,60)) | Ang-Hodrick-Xing-Zhang (2006) | -1 | β 与方差用同一窗口估计，偏乐观 |

## 趋势（3）
| id | 公式 | 来源 | 局限 |
|---|---|---|---|
| ma_bull | MA5 > MA20 > MA60 | 经典均线 | 滞后；震荡市假信号多 |
| breakout20 | close_t > 此前 20 日最高收盘 | Donchian / Turtle | 涨停连板股会连续触发但买不进 |
| vol_breakout | breakout20 且 volume_t > 1.5×此前 20 日均量 | Donchian + 量能确认 | 1.5 倍是约定值，未调参 |

## 风险过滤（3，True=通过）
| id | 规则 | 局限 |
|---|---|---|
| liq_filter | mean(close×volume,20) > 1 亿 | 复权价使成交额偏高，门槛偏宽松 |
| st_filter | 名称不含 ST | 名称是当前快照，历史 ST 状态不精确 |
| susp_filter | 当日 volume>0 且收盘价存在 | 停牌日在数据里没有 K 线，靠"当日缺失"识别 |

## 使用时请记住
- 因子触发 ≠ 买入信号。T+1、涨跌停、手数、成本都没有在这里处理。
- 想把任何因子升级为策略，必须走 `docs/validation_howto.md` 的流程（样本外、成本翻倍、多重检验校正），本库本身不提供这些证据。
