"""第三方回测引擎：backtrader（国外）和 RQAlpha 米筐（国内），以及和自研引擎的对比。

为什么要用两个外部引擎再跑一遍？
  自己写的回测引擎（src/portfolio.py）最容易出的错，是"自己验证自己"：
  规则理解错了，代码和测试会一起错。拿同样的数据、同样的策略规则，
  交给两个成熟的开源引擎各跑一遍，三者结果接近，才说明回测本身可信；
  差得多，就逐笔找原因（成交价、手续费、整手、涨跌停……）。

文件分工：
  common.py        数据读取、价格换算、A 股交易成本、绩效统计、逐笔交易配对（三个引擎共用）
  bt_strategies.py backtrader 版的 7 个策略（网页「代码」页可以直接修改）
  bt_engine.py     backtrader 的运行框架：数据、手续费、账户、结果整理
  rq_strategies.py RQAlpha 版的 7 个策略（网页「代码」页可以直接修改）
  rq_engine.py     RQAlpha 的运行框架：把本地数据接进 RQAlpha（自定义数据源 Mod）
  inhouse.py       自研引擎（src/portfolio.py）按同样的接口包一层，方便对比
  optimize.py      参数优化（网格搜索 + 样本内/样本外检验）
  runner.py        网页点「运行」时在独立子进程里执行的入口
  codefiles.py     「代码」页：列出、读取、保存（带历史版本）策略代码
"""
