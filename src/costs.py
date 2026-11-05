"""交易成本常量的单一来源：只放数值，不放逻辑，不 import 项目里的任何模块（避免循环导入）。

为什么要有这个文件：项目里有三套不同用途的成本口径（详见 docs/cost_model.md），以前每套各写各的字面量，
改一个数很容易漏掉另一处。现在数值只在这里定义，各模块 import 使用；本次抽取【没有改任何数值】
（tests/test_costs.py 把每个数钉死，并核对各模块读到的就是这里的值）。

三套口径（含义不同，数值本来就不同，不要混用）：
  1. 主回测（src/portfolio.py::run_weights，扫描、网页回测、今日买卖清单）：
     对称的"单边 bp × 换手"，没有最低佣金、没有单独的印花税、没有显式滑点（折在这个 bp 里）。
  2. T008 回放（src/replay/）：分项费用 + 显式 5bp 滑点，印花税用固定的现行税率（回放区间 2024-01 起，全在税率减半之后）。
  3. 模拟盘 / 持仓（src/simtrade.py、src/holdings/calc.py）：分项费用（佣金带最低 5 元、过户费、卖出印花税），无滑点。
  外部引擎（engines/common.py）：分项费用 + 印花税按成交日分段。engines/ 不在本补丁范围，
  它自己的常量由 tests/test_costs.py 核对必须等于这里的值。

注意：FEE_VERIFIED 仍是 False（src/simtrade.py），分项费率没有和真实账户核对过，这里只是搬家，不是核实。
"""
from __future__ import annotations

from datetime import date

# ---------------------------------------------------------------- 1. 主回测：单边 bp（每单位换手）
CN_STOCK_ONE_WAY_BP = 8     # A 股个股；README 写的 8bp；config.MARKETS["cn"]["stock_cost_bp"]、src/scan.py
CN_ETF_ONE_WAY_BP = 5       # A 股 ETF；免印花税；config.MARKETS["cn"]["cost_bp"]
US_STOCK_ONE_WAY_BP = 10    # 美股个股（买卖价差 + 滑点）；config.MARKETS["us"]["stock_cost_bp"]、src/scan_us.py
US_ETF_ONE_WAY_BP = 5       # 美股 ETF；config.MARKETS["us"]["cost_bp"]

# 换算成小数（run_weights 的 cost 参数用小数）；bp / 1e4 与直接写字面量得到同一个 float
CN_STOCK_ONE_WAY = CN_STOCK_ONE_WAY_BP / 1e4     # 0.0008
CN_ETF_ONE_WAY = CN_ETF_ONE_WAY_BP / 1e4         # 0.0005
US_STOCK_ONE_WAY = US_STOCK_ONE_WAY_BP / 1e4     # 0.0010

# run_weights / validation.adapter 的 cost 参数默认值：沿用历史上的 0.0005（即 ETF 档），数值不变
ENGINE_DEFAULT_ONE_WAY = CN_ETF_ONE_WAY

# 信号账本"扣来回费用"用的来回 bp = 2 × 个股单边（src/signal_ledger.py）
CN_STOCK_ROUND_TRIP_BP = 2 * CN_STOCK_ONE_WAY_BP     # 16

# ---------------------------------------------------------------- 2/3. 分项费用（A 股）
COMMISSION_RATE = 0.00025        # 股票佣金率（双边）万 2.5；engines.common.COMMISSION 同值
ETF_COMMISSION_RATE = 0.00015    # ETF 佣金率（双边）万 1.5；只有 src/simtrade.py 使用
MIN_COMMISSION = 5.0             # 每笔最低佣金（元）；engines.common.MIN_COMMISSION 同值
TRANSFER_RATE = 0.00001          # 过户费率（双边）十万分之一；engines.common.TRANSFER_FEE 同值

# 卖出印花税（只卖出收取）：2023-08-28 起从千分之一降为万分之五
STAMP_DUTY_CHANGE_DATE = date(2023, 8, 28)
STAMP_DUTY_RATE_BEFORE_CHANGE = 0.001    # 2023-08-28 之前
STAMP_DUTY_RATE = 0.0005                 # 2023-08-28 起（现行）；simtrade / validation / replay / holdings 固定用这个

# T008 回放 / validation.costs 的滑点假设（占位，非实测）
SLIPPAGE_BPS = 5.0
LIMIT_EXTRA_SLIPPAGE_BPS = 20.0

# ---------------------------------------------------------------- 与 AmiBroker 对照
# src/backtest.py 只用来和 amibroker/ma_cross_adx_atr.afl 对照；afl 里 CommissionAmount = 0.05%，
# 两边必须同值。amibroker/ 不在本次改动范围，改这里时要提醒同步 afl。
AMIBROKER_COMMISSION_PCT = 0.0005
