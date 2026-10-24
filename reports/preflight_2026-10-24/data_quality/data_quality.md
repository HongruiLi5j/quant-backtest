# 数据质量日报 2026-10-24

- 运行状态:**正常**
- 数据目录:`/Users/lihongrui/Documents/Projects/quant-backtest/data/daily/cn_all`(只读)
- 耗时:2.33 秒

## 覆盖
- 目录内 parquet 文件数:5226
- **抽样**:本次只抽查 100/5226 只(--sample 100,seed=0);下列比例只代表样本,不代表全市场。
- 成功检查:100 只
- 在市清单(cn_all_list.csv)股票数:5223
- 交易日历:已使用
- 空数据(无有效行):0 只;不可读文件:0 个

## 最新交易日分布
- 最新日期:2026-10-23;最后一根停在更早日期的有 0 只(0.0%)。若日更正在进行,这是“部分已更新”的混合态。

| 最后一根日期 | 只数 |
|---|---|
| 2026-10-23 | 100 |

## 问题类型占比(含该问题的股票数 / 成功检查数)

| 类型 | 股票数 | 占比 |
|---|---|---|
| missing_trading_days | 57 | 57.0% |

问题条数(按严重级别):{'warn': 351}

## 异常 Top 20(按 error 数、warn 数排序)

| 代码 | error | warn | 示例 |
|---|---|---|---|
| 002076 | 0 | 20 | missing_trading_days@2012-04-19; missing_trading_days@2012-06-08; missing_trading_days@2014-06-11 |
| 002721 | 0 | 20 | missing_trading_days@2014-05-30; missing_trading_days@2014-12-24; missing_trading_days@2015-02-16 |
| 002571 | 0 | 17 | missing_trading_days@2011-09-02; missing_trading_days@2012-02-27; missing_trading_days@2012-04-09 |
| 002226 | 0 | 15 | missing_trading_days@2011-11-10; missing_trading_days@2011-12-02; missing_trading_days@2012-01-04 |
| 000536 | 0 | 14 | missing_trading_days@2012-01-05; missing_trading_days@2012-02-09; missing_trading_days@2012-03-29 |
| 002309 | 0 | 14 | missing_trading_days@2012-01-06; missing_trading_days@2012-03-21; missing_trading_days@2012-04-23 |
| 300197 | 0 | 13 | missing_trading_days@2011-10-12; missing_trading_days@2012-01-16; missing_trading_days@2012-04-09 |
| 002256 | 0 | 12 | missing_trading_days@2012-03-07; missing_trading_days@2012-04-18; missing_trading_days@2013-12-02 |
| 002527 | 0 | 11 | missing_trading_days@2012-02-02; missing_trading_days@2012-02-24; missing_trading_days@2012-04-10 |
| 002650 | 0 | 11 | missing_trading_days@2012-04-11; missing_trading_days@2012-05-08; missing_trading_days@2015-01-23 |
| 000885 | 0 | 10 | missing_trading_days@2012-02-24; missing_trading_days@2012-04-06; missing_trading_days@2012-04-19 |
| 002382 | 0 | 10 | missing_trading_days@2011-10-17; missing_trading_days@2012-03-16; missing_trading_days@2012-03-29 |
| 002654 | 0 | 10 | missing_trading_days@2012-03-26; missing_trading_days@2012-05-07; missing_trading_days@2013-01-28 |
| 002135 | 0 | 9 | missing_trading_days@2012-01-13; missing_trading_days@2012-03-27; missing_trading_days@2012-04-19 |
| 300062 | 0 | 7 | missing_trading_days@2012-04-17; missing_trading_days@2013-12-30; missing_trading_days@2015-06-04 |
| 300077 | 0 | 7 | missing_trading_days@2012-03-05; missing_trading_days@2012-05-04; missing_trading_days@2013-09-25 |
| 600722 | 0 | 7 | missing_trading_days@2012-03-26; missing_trading_days@2012-05-08; missing_trading_days@2012-10-25 |
| 600857 | 0 | 7 | missing_trading_days@2012-03-26; missing_trading_days@2012-04-20; missing_trading_days@2013-10-15 |
| 002035 | 0 | 6 | missing_trading_days@2012-04-09; missing_trading_days@2012-07-17; missing_trading_days@2012-11-14 |
| 002213 | 0 | 6 | missing_trading_days@2012-05-04; missing_trading_days@2013-12-26; missing_trading_days@2014-01-07 |

## 与前一次对比
没有找到更早的日报 JSON,无法对比。

---
本报告只读数据目录,未修改任何行情文件。阈值与涨跌幅制度日期为凭记忆的默认值 [需核实];异常不等于数据错误,也不构成对策略有效性的任何结论。
