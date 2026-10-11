"""在这里写你自己的策略。保存后，填一个股票代码，点"保存并运行回测"，
就会用真实的历史数据跑一遍（自研引擎口径：次日开盘成交，A 股手续费和涨跌停规则）。

只需要写一个函数：

    def strategy(df: pd.DataFrame) -> pd.Series

- df：这只股票的日线数据，按日期升序排列，列有 Open / High / Low / Close / Volume（都是后复权价格）
- 返回值：一个和 df.index 长度一样的 Series，表示"当天收盘后决定的目标仓位"：
    0   = 空仓
    1   = 满仓
    0.5 = 半仓（可以是 0~1 之间任意小数）
- 规则和内置的 7 个策略一样：只能用截至当天收盘的数据，不能提前用到未来的价格
  （比如用 df["Close"].shift(-1) 就是偷看未来，回测结果不可信，实盘不可能做到）
- 不用自己管成交价格、手续费、涨跌停——这些回测引擎会统一处理，你只管"信号"

下面是一个最简单的例子：站上 20 日均线就满仓，跌破就空仓。改掉它，写你自己的策略。
"""
from __future__ import annotations

import pandas as pd


def strategy(df: pd.DataFrame) -> pd.Series:
    ma20 = df["Close"].rolling(20).mean()
    return (df["Close"] > ma20).astype(float)
