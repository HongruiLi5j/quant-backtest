# 价值 + 基本面模块使用说明(T014)

> 状态:骨架与打分已完成;S5 接入了 BaoStock 真实数据(见 §3.1),目前只跑过 20 只股票的冒烟,全市场尚未下载。
> 本文里关于数据源的说法(免费积分门槛、接口字段、披露规则)都凭记忆,**[需核实]**,下载前请先到官网/文档核对。

## 1. 这个模块是什么

- 一套**月度**、**独立于技术面**的价值 + 基本面排名:12 个因子(`src/fundamental/factors.py`,id 都以 `fv_` 开头),
  每个因子在行业内做 Z-score(截尾 ±3),方向统一后**等权**平均,得到综合分(`src/fundamental/ranker.py`)。
- 报告由 `src/fundamental/report.py` 的 `build_monthly_report(ranking, universe_info, prev_ranking=None)` 生成 Markdown:
  前后 N 名、与上月变化、各因子贡献拆解、前组行业分布、免责声明与局限。
- 它是"打分函数",**不是经过验证的 Alpha**:没做样本外检验、没扣成本,有幸存者和重述偏差。

## 2. 与短中期技术面系统的关系:两套独立

| | 技术面(`strategies.py`、`market_monitor`、回放) | 价值/基本面(本模块) |
|---|---|---|
| 问题 | 未来几天~几周价格怎么走 | 公司相对盈利/净资产/分红是否便宜、质量如何 |
| 频率 | 每日 | 月度出排名,财报季后刷新数据 |
| 数据 | 后复权日线 | 财报、未复权价、股本、行业 |

- 不混进同一个打分:两者信号半衰期差一个数量级,硬加权的权重没有经济含义,只会多出自由度(多重检验);分开才能分别证伪。
- 不导入 `market_monitor`、不写 `signal_ledger`、不改任何已有策略/网页。
- 因子本身用的是**未复权价与市值**(估值分子分母口径一致),不能直接拿本地后复权价算 PE/PB。

## 3. 怎么把真实数据下载进 `fundamental.db`(下载阶段由所有者批准后另开)

存储在 `src/fundamental/store.py`(`FundamentalStore`,默认路径 `data/fundamental.db`,`data/` 不上传)。要点:

- **按公告日保存,多版本**:每行有 `report_date`(报告期)、`announce_date`(公告日)、`source`、`inserted_at`;重述不覆盖旧版本。
  回测/排名只用 `announce_date <= 排名日` 的版本(`get_metrics_asof`)。
- 外部数据只能通过**调用者注入的 `provider_fn`** 进入:`ingest_reports(store, codes, report_dates, provider_fn)`,
  `provider_fn(code, report_date) -> list[dict]`,每个 dict 含 `metric / value / source / announce_date`。默认 provider 抛 `NotImplementedError`,不会意外联网。
- 候选数据源(**[需核实]**):
  1. **Tushare**(免费积分档位可能够用于利润表/资产负债表/现金流量表/财务指标,有 `ann_date`、`f_ann_date`、`update_flag`,可保留重述多版本;需个人 token,放 `.secret/`,**不要写进代码或仓库**);
  2. **巨潮资讯公开披露页**(官方原始披露,公告日最可靠;结构化程度低,需要自己解析,且要遵守其使用条款与访问频率);
  3. BaoStock(项目已用,免登录;财务接口一次一个"股票×年×季",全市场全历史请求量极大,需分级起步,例如先沪深 300 / 中证 500)。
- 下载守则:限速、断点续传、原始返回连同抓取时间存档(只增不改,将来可检验数据源是否悄悄重述);
  东方财富接口只用于每天一次的全市场快照,**不用于历史回补**。
- 排名需要的字段(TTM 口径、归母净利润、营业成本、经营现金流、分红等)见 `factors.py` 顶部文档;
  TTM 由累计值差分得到(Q1/H1/Q3/FY 累计口径),这一步属于下载阶段的数据整理,不在本模块里猜。

## 3.1 真实数据接入(T014 S5:BaoStock)

数据源由所有者定为 **BaoStock**(免费公开,不需要 key;项目已在 `src/allmarket.py` 用它拉行业分类)。不用 akshare、不用 Tushare。

### 组成

| 文件 | 作用 |
|---|---|
| `src/fundamental/fetch_baostock.py` | provider:`fetch_quarterly(code, year, quarter)` 调 `query_profit_data / query_balance_data / query_cash_flow_data`,`fetch_dividends(code, year)` 调 `query_dividend_data`,`fetch_close_raw(code, asof)` 取未复权收盘价;`assemble_asof(store, code, asof)` 按排名日组装字段 |
| `scripts/fetch_fundamental.py` | 命令行:拉取 → 经 `ingest_reports`(provider_fn 注入)写入 `FundamentalStore` → 按 `--asof` 导出两份 CSV |
| `tests/test_fundamental_fetch.py` | 离线测试(假 baostock 模块 + 临时目录) |

### 防未来函数的规则

- 每条财报记录 `announce_date = pubDate`(公告日)、`report_date = statDate`(报告期)。
  **pubDate 缺失、格式不对、早于报告期末或晚于今天的整行丢弃并计数**,绝不拿报告期冒充公告日。
- 导出时只用 `announce_date <= asof` 的版本(`store.get_report_asof / get_metrics_asof` 同一规则);同一报告期后来被重述,
  重述公告日之前看到的仍是旧值。
- 分红只计"已实施"的:`announce_date = 除权除息日`,过去 12 个月 = `(asof − 1 年, asof]`(左开右闭,避免同一天的两次年度分红都算进来);
  分红年份没查询过时 `dps_ttm` 为 NaN(没查过 ≠ 没分红)。
- `--asof` 是今天且北京时间未到 15:30 时拒绝运行(当天 K 线是盘中数据)。
- [可再保守] 公告当天就算可见。A 股定期报告多在开盘前或前一晚披露,但若想更保守,可以把 `--asof` 往前挪一个交易日。

### 字段怎么来(BaoStock 季频接口多数只给比率)

| 导出字段 | 计算 | 说明 |
|---|---|---|
| `net_profit_ttm` / `_prev` | `netProfit` 累计值差分:TTM = 本期累计 + 上年年报 − 上年同期累计(年报本身即 TTM) | 缺任一期 → NaN,不插值。`_prev` 是上年同期的 TTM,所以至少要往前 2 年的数据 |
| `revenue_ttm` / `_prev` | 每期 营业收入累计 = `netProfit / npMargin`,再差分 | `MBRevenue`(主营收入)季报常为空,只存档不用,避免口径混用 |
| `operating_cost_ttm` | 每期 营业收入累计 × (1 − `gpMargin`),再差分 | 银行等无毛利率 → NaN |
| `cfo_ttm` | 每期 `CFOToNP × netProfit`,再差分 | |
| `dps_ttm` | 过去 12 个月已实施的 `dividCashPsBeforeTax` 合计 | |
| `close_raw` | baostock 日线 `adjustflag=3`(未复权)排名日收盘 | **本地 `data/daily/cn_all` 是后复权价**(例如 600519 约 11800,未复权约 1400 多),拿来算市值会放大几倍到几十倍,所以不能用 |
| `market_cap` | `close_raw × totalShare`(截至 asof 最新已公告的总股本) | 两期财报之间的送转/增发会让股本滞后 |
| `equity_parent` / `total_assets` / `total_liabilities` / `operating_profit_ttm` | **NaN** | BaoStock 季频接口没有这些绝对金额(只有资产负债率、权益乘数等比率),不猜。受影响的因子:fv_bp、fv_roe、fv_roa、fv_debt_ratio、fv_op_margin;其余 7 个因子可用(排名脚本默认 `--min-factors 6`) |
| `available_date` | 本行用到的记录里最晚的公告日(必然 ≤ asof) | |

**[需核实]**:`netProfit` 是"净利润"还是"归母净利润"(若含少数股东损益,EP/ROE 对子公司多的公司偏高);
`npMargin`/`gpMargin` 是小数(0.25)还是百分数(25)——冒烟结果看是小数,但应抽几只和年报核对。

### 命令

```
.venv/bin/python scripts/fetch_fundamental.py --asof 2026-10-22 --limit 20
```

全部参数:`--codes-file`(默认 `data/daily/cn_all_list.csv`)、`--start`(最早报告期,默认 2023-01-01)、
`--db`(默认 `data/fundamental.db`)、`--export-fundamental`(默认 `data/exports/fundamental_latest.csv`)、
`--export-prices`(默认 `data/exports/prices_latest.csv`)、`--asof`(必填)、`--limit N`(调试只取前 N 只)、
`--skip-fetch`(不拉财报,只用库里已有数据导出;仍会为排名日有行情的股票各取一次未复权价)。

- **断点续跑**:已入库的 (代码, 报告期) 直接跳过;中断后原命令再跑一遍即可。还没披露的报告期(返回空)下次会再试。
  往年分红查过就不再查,今年的分红每次都重查。
- **限速与停止**:请求间隔至少 0.2 秒;连续失败 5 次停下,退出码 3,已入库数据保留。单季失败只计数,不停。
- 导出 CSV 每次覆盖(原子替换);拒绝写入 `data/daily/`。

### 耗时估计

- 所有者的估算:5200 只 × 4 季 × 3 接口 = 6.2 万次,按 0.2 秒间隔约 3.5 小时。
- **实际要多得多**,两个原因:① 默认 `--start 2023-01-01` 到 2026-10 是 15 个报告期,不是 4 个(TTM 和"上年同期 TTM"至少要往前两年);
  每只股票约 15×3 + 2(分红)+ 1(未复权价)= 48 次请求;② 冒烟实测平均每次请求约 0.7 秒(间隔 0.2 秒 + 网络往返)。
  → 全市场首次约 5200 × 48 × 0.7 秒 ≈ **48 小时**。之后续跑每季只需取新的一期(约 5200 × 3 次,约 3 小时)加分红和价格。
- 建议:**夜间分批跑**(例如 `--limit` 配合拆分后的 `--codes-file`,或直接跑、中断后续跑);想快一点可以把 `--start` 设成 2024-01-01
  (11 个报告期,约 36 次/只,约 36 小时),代价是 2026 年中报的上年同期 TTM 仍可算,但更早的排名日会缺数据。

### 接着跑排名

```
.venv/bin/python scripts/run_fundamental_rank.py --date 2026-10-22 --top 50 --out reports/value_2026-10-22.md --fundamental data/exports/fundamental_latest.csv --prices data/exports/prices_latest.csv
```

`--date` 要和 `fetch_fundamental.py` 的 `--asof` 一致;输出已存在时默认拒绝覆盖,确需覆盖加 `--force`。
注意:5 个因子因缺绝对金额为 NaN,报告"数据备注"里的补值数会偏高,排名可信度相应降低。

## 4. 每月手动跑一次

1. 等到财报披露较集中之后再跑(见 §6 的滞后问题);确认排名日是已收盘的交易日(北京时间 15:30 后才能用当天数据)。
2. 准备两份本地 CSV:基本面表(`code`,可选 `name / industry / available_date`,加标准字段)和行情表(`code,date,close`,只用来剔除排名日停牌/无行情的股票)。
3. 运行(不联网,不给数据路径会拒绝):

```
.venv/bin/python scripts/run_fundamental_rank.py --date 2026-10-30 --top 50 --out reports/value_2026-10.md --fundamental fund.csv --prices prices.csv
```

   该脚本默认**拒绝覆盖**已有输出,确需覆盖加 `--force`。

4. 在 Python 里用 `report.py` 生成带"与上月对比"和"贡献拆解"的完整月报:

```
from src.fundamental.ranker import build_ranking
from src.fundamental.report import build_monthly_report, write_report

res = build_ranking(fund_df, price_df, "2026-10-30", top_n=50)
prev = build_ranking(prev_fund_df, prev_price_df, "2026-09-30", top_n=50)
text = build_monthly_report(res, {"top_n": 50, "generated_at": "2026-10-30 16:00", "data_source": "..."}, prev)
write_report("reports/value_2026-10.md", text)
```

   `write_report` 默认不覆盖(已存在抛 `FileExistsError`),`overwrite=True` 才覆盖。
   上月排名可以用上月同样的函数重算,或把上月 `RankResult.table`(含 `code, rank` 两列的 DataFrame)存下来再传入。
5. 看报告时先看"数据备注":`industry_fallback`(行业样本不足回退全市场)和 `imputed`(补值)的数量大,说明这一期数据质量差,排名可信度低。

## 5. 如何读报告

- **综合分** = 各因子 z 的加权平均;因子贡献 = 权重占比 × z,所以"前组平均贡献"最大的因子,就是把这批股票推到前面的主要原因。
  如果贡献几乎全来自一两个因子(如只靠股息率),要警惕。
- **行业分布**:前组若高度集中在个别行业,说明行业中性化后仍有行业暴露(例如行业内样本不足时回退全市场)。
- **与上月对比**:换手大说明排名对数据噪声敏感,实际交易成本会更高。

## 6. 已知局限

- **财报滞后 1~3 个月**:报告期末到公告日之间,价格已经反映了新信息;因子是慢变量,不能当短期择时。
  模块用 `available_date`/`announce_date` 屏蔽未公告数据;若数据源缺公告日,需按法定披露截止日保守估计(下载阶段决定)。
- **业绩变脸不可预测**:排名只反映已公告数据,无法预见造假、会计差错更正、突发亏损。
- **价值陷阱**:低 PE/高股息常是市场预期恶化的结果。
- **重述与幸存者偏差**:免费数据多为最新值,退市公司常缺失,历史看起来更乐观。
- **金融/地产行业可比性**:负债率、毛利率对银行/保险/券商/地产意义不同,目前只靠行业内比较,未单独处理。
- **多重检验与样本量**:12 个因子等权未经样本外验证;任何改权重/换因子都应当作新一次尝试登记(可复用 `src/validation` 的试验账本思路,账本独立)。
- 回测好看不代表有效;真实数据阶段之后,需要先做样本外与成本检验,才能谈"有没有用"。

## 7. 怎样接入网站(建议,等所有者批准后另开阶段)

- 建议做成**单独的"价值"页面**,与现有技术面页面并列,不合并分数。
- 只读展示最近一期月报(Markdown 或 JSON),不在网页里触发下载或计算;网页服务仍只监听 127.0.0.1。
- 需要改 `app.py` 和 `dashboard/*`,不在本任务范围,须先获批。
- 页面顶部固定显示免责声明与"未经验证"提示。

## 8. 测试

```
.venv/bin/python tests/test_fundamental_fetch.py
.venv/bin/python tests/test_fundamental_report.py
.venv/bin/python tests/test_fundamental_ranker.py
.venv/bin/python tests/test_fundamental_factors.py
.venv/bin/python tests/test_fundamental_store.py
```

全部使用合成数据与临时目录,不联网。
