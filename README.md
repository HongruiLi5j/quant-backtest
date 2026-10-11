# quant-backtest

MA5/MA20 均线交叉 + ADX 趋势过滤 + ATR 止损，Python 和 AmiBroker 两套实现，逐笔对照。

## 第一次运行（Mac 终端）

```bash
cd ~/Documents/Projects/quant-backtest
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python tests/test_basic.py          # 自检
python run_backtest.py SPY          # 下载 SPY 数据并回测
python run_backtest.py SPY --scan   # 参数扫描
```

结果在 `results/`：`SPY_backtest.png`（价格+买卖点、资金曲线、回撤）、`SPY_trades.csv`（每笔交易）。
下载的数据在 `data/SPY.csv`，同时会生成 `data/SPY_amibroker.csv` 给 AmiBroker 导入。

## 和 AmiBroker 对比

目的：两个独立实现得到一样的交易 → 说明你对策略规则的理解和代码都是对的。

1. **用同一份数据**：在 Windows 虚拟机的 AmiBroker 里，File → Import Wizard，导入
   `data/SPY_amibroker.csv`（Parallels 下 Mac 文件夹可以直接访问）。
   字段顺序：Date(YMD), Ticker, Open, High, Low, Close, Volume，跳过第一行表头。
   ⚠️ 不要用 AmiBroker 自带的数据源，复权方式不同，结果会对不上。
2. 打开 `amibroker/ma_cross_adx_atr.afl`，Analysis 窗口里 Apply to = 当前代码 SPY，Range = All quotes。
3. 点 **Backtest**，在结果里切到 Trade list，File → Export → 存成 `amibroker/SPY_ab_trades.csv`。
4. （可选）点 **Explore**，导出成 `amibroker/SPY_ab_explore.csv`。
5. 回到 Mac：
   ```bash
   python compare_amibroker.py SPY amibroker/SPY_ab_trades.csv --explore amibroker/SPY_ab_explore.csv
   ```

对不上很正常，查原因本身就是最好的学习。常见原因：
- 数据不同（复权、日期范围）
- 指标初始值（Wilder 平滑的种子）→ 前几十根 K 线的 ADX/ATR 有差异，之后应该收敛
- 成交规则（开盘价 vs 收盘价、止损入场当天是否生效、跳空处理）
- 仓位与手续费（整数股、100% 仓位时手续费让股数少 1 股）→ 只影响盈亏金额，不影响交易日期

## 项目结构

```
src/indicators.py   SMA、Wilder 平滑、ATR、ADX、Cross（对应 AFL 同名函数）
src/strategy.py     策略参数与信号
src/backtest.py     逐根 K 线的回测引擎
src/metrics.py      收益、Sharpe、最大回撤、胜率、盈亏比
run_backtest.py     命令行入口
compare_amibroker.py
amibroker/          AFL 代码，AmiBroker 的导出文件放这里
tests/
```

## 接下来可以做的练习（自己写，卡住再问 Claude）

1. **样本外检验**：用 2010–2019 扫参数挑最优，看 2020 之后的表现。落差多大？
2. **Walk-forward**：每年用过去 5 年选参数，跑下一年，拼起来。
3. **多品种**：SPY、QQQ、IWM、TLT、GLD 一起跑，这个策略在哪类资产上有效？
4. **移动止损**：把固定 ATR 止损改成跟踪止损（Chandelier exit），比较结果。
5. **交易成本敏感性**：手续费从 0 到 0.2%，Sharpe 怎么变？
6. **统计显著性**：把每笔交易的收益 bootstrap 1000 次，Sharpe 的置信区间有多宽？（和 Exam P 的内容直接相关）

做完 1、2、6 就是一个能写进简历、面试能讲清楚的项目。

## 每日自动更新 + 网页仪表盘

一键安装（只需一次）：

```bash
cd ~/Documents/Projects/quant-backtest
bash scripts/install_mac.sh
bash scripts/open_dashboard.sh
```

- **每天自动运行**：01:30（A 股收盘后）和 14:30（美股收盘后），加州时间。电脑要开着；合盖睡眠的话，唤醒后会补跑。
- **网页**：明日开盘建议仓位、信号变化、自动点评、观察列表；「回测实验室」可以换策略、换品种、改参数、改成本、改开始日期。
- **数据**：美股来自 Yahoo Finance，A 股 ETF 来自东方财富（通过 AKShare，后复权），都不需要账号。以后开通广发 QMT，把 `config.py` 里 A 股的 `source` 改成 `"qmt"` 并实现 `src/datasource.py` 里的 `_fetch_qmt`。
- **加新策略**：在 `src/strategies.py` 写函数，在 `src/registry.py` 的 `STRATEGIES` 里登记，网页会自动出现。
- **停止自动运行**：`bash scripts/uninstall_mac.sh`

安全：
- 网页服务只监听 127.0.0.1，同一 Wi-Fi 下的其他设备也连不上
- 需要访问令牌（`.secret/token`，权限 600），浏览器用 HttpOnly cookie 记住
- 检查 Host 头防 DNS rebinding；只有固定接口，参数全部白名单校验
- 数据、日志、令牌目录权限 700；不保存任何账号密码；不连接交易接口，不会下单
- 不要把 `.secret/` 发给别人或传到 GitHub（已写进 .gitignore）
