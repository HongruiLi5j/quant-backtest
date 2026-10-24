"use strict";
// English UI with a one-click Chinese fallback. Market names, company names, and security codes stay as source data.
(() => {
  // 翻译页使用稳定的键；中英文成对保存，避免自动替换不可信文本。
  const aiTranslations = {
    "ai.title": [
        "AI 想法翻译(实验)",
        "AI Idea Translator (experimental)"
    ],
    "ai.notice": [
        "实验功能,结果需确认。模型可能理解错你的意思,请逐项核对后再确认。",
        "Experimental feature — results must be confirmed. The model can misread your idea; check every field before confirming."
    ],
    "ai.badge.exp": [
        "实验版本",
        "Experimental version"
    ],
    "ai.input.ph": [
        "用中文或英文描述一个回测想法,例如:60 日动量,每月调仓",
        "Describe a backtest idea in Chinese or English, e.g. 60-day momentum, rebalance monthly"
    ],
    "ai.btn.translate": [
        "翻译",
        "Translate"
    ],
    "ai.busy": [
        "正在翻译…(首次需加载模型,可能要几十秒)",
        "Translating… (the first request loads the model and may take tens of seconds)"
    ],
    "ai.btn.confirm": [
        "确认",
        "Confirm"
    ],
    "ai.btn.cancel": [
        "取消",
        "Cancel"
    ],
    "ai.btn.wrong": [
        "翻译错了",
        "Translation is wrong"
    ],
    "ai.btn.copy": [
        "复制配置 JSON",
        "Copy config JSON"
    ],
    "ai.ack": [
        "我已看到上面的不一致提示",
        "I have seen the mismatch warnings above"
    ],
    "ai.confirmed.note": [
        "已确认。本版不会自动运行回测。",
        "Confirmed. This version does not run any backtest automatically."
    ],
    "ai.col.field": [
        "字段",
        "Field"
    ],
    "ai.col.value": [
        "取值",
        "Value"
    ],
    "ai.col.source": [
        "来源",
        "Source"
    ],
    "ai.src.idea": [
        "你写的",
        "From your idea"
    ],
    "ai.src.default": [
        "默认值(你没提)",
        "Default (not in your idea)"
    ],
    "ai.src.mismatch": [
        "与你的想法不一致",
        "Differs from your idea"
    ],
    "ai.src.unchecked": [
        "无法自动核对",
        "Could not be checked automatically"
    ],
    "ai.flag.mismatch": [
        "你写的是「{idea}」,配置里是「{config}」",
        "You wrote \"{idea}\", the config has \"{config}\""
    ],
    "ai.flag.block": [
        "此项无法确认,请修改想法后重试",
        "This cannot be confirmed; edit your idea and try again"
    ],
    "ai.note.limits": [
        "自动核对只能发现一部分不一致,没有提示不代表配置一定正确。",
        "The automatic check finds only some mismatches; no warning does not prove the config is right."
    ],
    "ai.off": [
        "AI 翻译未开启。可在 data/ai_translator.json 中打开(仅本机)。",
        "AI translation is off. It can be turned on in data/ai_translator.json (local machine only)."
    ],
    "ai.cfg.bad": [
        "AI 翻译配置有误,已自动关闭,请检查 data/ai_translator.json。",
        "The AI translator config is invalid and the feature was switched off; check data/ai_translator.json."
    ],
    "ai.err.TRANSLATOR_BUSY": [
        "正在处理上一个请求,请稍后再试。",
        "Another request is being processed; please try again shortly."
    ],
    "ai.err.TRANSLATOR_TIMEOUT": [
        "翻译超时,本次请求已结束。",
        "The translation timed out. This request has ended."
    ],
    "ai.err.TRANSLATOR_UNAVAILABLE": [
        "翻译服务暂不可用。",
        "The translator is temporarily unavailable."
    ],
    "ai.err.TRANSLATOR_DAILY_LIMIT": [
        "今日次数已用完。",
        "Today's limit has been reached."
    ],
    "ai.err.TRANSLATOR_BUDGET_EXCEEDED": [
        "翻译预算或今日请求次数已到上限，暂不能发送请求。",
        "The translation budget or daily request limit has been reached. No request can be sent."
    ],
    "ai.err.TRANSLATOR_BAD_OUTPUT": [
        "模型输出格式不正确：需要单一配置、拒绝或追问 JSON；消息须为 1–200 字，不能有未知字段。",
        "Invalid model output: expected one config, rejection or clarification JSON, no unknown fields, and a 1–200 character message."
    ],
    "ai.clarify.title": [
        "请补充你的想法",
        "Please add a little detail"
    ],
    "ai.example.equal_weight": [
        "美股等权，其余使用默认值。",
        "US equal-weight, using defaults elsewhere."
    ],
    "ai.example.momentum": [
        "美股动量，回看 252 日，跳过 21 日，每日调仓，前后 10%。",
        "US momentum, 252-day lookback, skip 21 days, rebalance daily, top/bottom 10%."
    ],
    "ai.refusal.fields": [
        "请检查字段：{fields}",
        "Check these fields: {fields}"
    ],
    "ai.refusal.SEALED_PERIOD": [
        "评估结束日期不能晚于 2023-12-31;2024-01-01 起的数据为封存期,不能使用。",
        "The evaluation end date cannot be after 2023-12-31; data from 2024-01-01 onward is sealed."
    ],
    "ai.refusal.OUT_OF_RANGE": [
        "请求的参数超出本项目实际使用过的取值范围,无法生成配置。",
        "A requested value is outside the ranges this project has actually used, so no config can be produced."
    ],
    "ai.refusal.EMBARGO_VIOLATION": [
        "embargo 不得小于预测周期加 1 天(h+1),否则训练标签会泄漏。",
        "The embargo must be at least h+1 days, otherwise training labels leak."
    ],
    "ai.refusal.UNSUPPORTED_REQUEST": [
        "该请求不在本项目支持的策略与参数范围内,无法生成配置。",
        "This request is outside the strategies and parameter combinations this project supports."
    ],
    "ai.refusal.MISSING_REQUIRED": [
        "请求没有指明要测试的策略类型,请说明信号(如残差反转、动量、ML 模型)。",
        "The request does not say which strategy to test; please name a signal (e.g. residual reversal, momentum, an ML model)."
    ],
    "ai.flag.conflict": [
        "你的想法里对「{field}」写了不同的值,无法自动核对",
        "Your idea gives different values for \"{field}\"; it cannot be checked automatically"
    ],
    "ai.flag.missing": [
        "你提到了「{idea}」,但配置里没有「{field}」这一项",
        "You mentioned \"{idea}\", but the config has no \"{field}\""
    ],
    "ai.input.label": [
        "回测想法",
        "Backtest idea"
    ],
    "ai.feedback.note": [
        "备注(可选,最多 200 字)",
        "Note (optional, up to 200 characters)"
    ],
    "ai.btn.feedback": [
        "提交反馈",
        "Send feedback"
    ],
    "ai.feedback.sent": [
        "反馈已记录。",
        "Feedback recorded."
    ],
    "ai.copy.ok": [
        "配置 JSON 已复制。",
        "Config JSON copied."
    ],
    "ai.copy.failed": [
        "复制失败,请手动复制下方 JSON。",
        "Copy failed; select and copy the JSON below."
    ],
    "ai.loading": [
        "正在读取翻译状态…",
        "Loading translator status…"
    ],
    "ai.saving": [
        "正在提交…",
        "Submitting…"
    ],
    "ai.note.coverage": [
        "自动核对不覆盖中文数字、英文数字词、口语化或相对时间、语义替换、否定与假设、模糊或冲突意图、拼写及刻意绕过;也不判断策略是否合理,可能漏报或误报。默认值仅表示未在想法中识别到,不表示你一定没提。",
        "Automatic checks do not cover number words, informal or relative dates, semantic substitutions, negation or hypotheticals, ambiguous or conflicting intent, spelling or deliberate evasion. They do not judge strategy quality and may miss issues or raise false alarms. Default means nothing was extracted, not necessarily that you never mentioned it."
    ],
    "ai.err.TRANSLATOR_RATE_LIMITED": [
        "请求过于频繁,请稍后再试。",
        "Too many requests; please try again later."
    ],
    "ai.err.TRANSLATOR_BACKEND_FAILED": [
        "翻译后端处理失败,请稍后重试。",
        "The translation backend failed; please try again later."
    ],
    "ai.err.TRANSLATOR_INPUT_INVALID": [
        "请输入有效的回测想法或反馈。",
        "Enter a valid backtest idea or feedback."
    ],
    "ai.err.TRANSLATOR_INPUT_TOO_LONG": [
        "想法超过允许的字数,请缩短后重试。",
        "The idea exceeds the input limit; shorten it and try again."
    ],
    "ai.err.TRANSLATOR_DISABLED": [
        "AI 翻译已关闭。",
        "AI translation has been switched off."
    ],
    "ai.err.TRANSLATOR_BAD_ORIGIN": [
        "请求来源校验失败,请从本机仪表盘重试。",
        "Request origin check failed; retry from the local dashboard."
    ],
    "ai.err.TRANSLATOR_NOT_CONFIRMABLE": [
        "此结果无法确认,请修改想法后重试。",
        "This result cannot be confirmed; edit the idea and try again."
    ],
    "ai.err.TRANSLATOR_ACK_REQUIRED": [
        "请先勾选已看到不一致提示。",
        "Acknowledge the mismatch warnings first."
    ],
    "ai.err.TRANSLATOR_ALREADY_CONFIRMED": [
        "此结果已确认。",
        "This result has already been confirmed."
    ],
    "ai.err.TRANSLATOR_REQUEST_EXPIRED": [
        "结果已过期,请重新翻译。",
        "This result has expired; translate again."
    ],
    "ai.err.HTTP_401": [
        "登录已失效,请从本机重新打开仪表盘。",
        "Your session has expired; reopen the local dashboard."
    ],
    "ai.err.HTTP_403": [
        "请求未通过访问校验。",
        "The request failed access checks."
    ],
    "ai.err.HTTP_404": [
        "翻译页仅限本机访问,或服务尚不可用。",
        "The translator requires local access, or the service is unavailable."
    ],
    "ai.err.HTTP_429": [
        "请求过于频繁,请稍后再试。",
        "Too many requests; please try again later."
    ]
};
  const dictionary = {
    "关闭": "Off",
    "技术指标": "Indicators",
    "全部（布林带 + MACD + RSI + KDJ）": "All (Bollinger + MACD + RSI + KDJ)",
    "布林带 + MACD": "Bollinger + MACD",
    "布林带 + RSI": "Bollinger + RSI",
    "布林带 + KDJ": "Bollinger + KDJ",
    "MACD (12, 26, 9)　DIF · DEA · 柱": "MACD (12, 26, 9)  DIF · DEA · Hist",
    "KDJ (9, 3, 3)　K · D · J": "KDJ (9, 3, 3)  K · D · J",
    "原本数据（不回测）": "Raw data (no backtest)",
    "近1月": "1M",
    "近1年": "1Y",
    "排名靠前的个股（Blend 夏普）": "Top-ranked stocks (blend Sharpe)",
    "数据偏旧，建议更新": "data is stale, consider refreshing",
    "距今": "age",
    "数据日期": "Data date",
    "假设与局限": "Assumptions & Limitations",
    "看结果前先读这里": "read this before the results",
    "执行时点": "Execution timing",
    "手续费与滑点": "Fees & slippage",
    "仓位口径": "Position sizing",
    "整手与最低佣金": "Lot size & minimum commission",
    "涨跌停": "Price limits",
    "涨跌停与停牌": "Price limits & halts",
    "复权方式": "Price adjustment",
    "幸存者偏差": "Survivorship bias",
    "数据来源": "Data source",
    "不构成建议": "Not advice",
    "策略在第 t 天收盘后产生信号，第 t+1 天开盘价成交；止损也按收盘判断、次日开盘执行。不用当天未来数据。": "Signals form after the close on day t and fill at the open on day t+1; stops are judged at the close and executed at the next open. No same-day future data is used.",
    "已包含佣金、印花税和滑点的估计，按每次换手扣除。真实成交会因流动性和行情而不同。": "includes estimated commission, stamp duty and slippage, charged on every turnover. Real fills vary with liquidity and market conditions.",
    "按目标权重（比例）计算，允许零股，不做整手取整；资金量很小时与真实成交会有差异。": "Calculated on target weights, fractional shares allowed, no lot rounding; results differ from real fills when capital is very small.",
    "按 100 股一手和每笔最低 5 元佣金成交（A 股）；资金不足一手时无法买入。": "Fills in 100-share lots with a 5 CNY minimum commission per order (A-shares); no purchase if cash is below one lot.",
    "按股数成交，不足一股的部分取整。": "Fills in whole shares; fractions are rounded down.",
    "一字涨停买不进、一字跌停卖不出；非一字板按正常成交处理，盘中涨停排队等真实情况未模拟。": "Cannot buy at a one-price limit-up or sell at a one-price limit-down; other days fill normally. Intraday queueing at limits is not simulated.",
    "使用前复权价格计算；价格和行情软件显示的不同，但收益率是连续可比的。": "Uses forward-adjusted prices; levels differ from quote software, but returns are continuous and comparable.",
    "股票池以当前可交易股票为主，已退市的公司大多不在其中，结果会偏乐观；全市场扫描页会单独列出含退市的对比。": "The universe is mostly currently listed stocks; delisted companies are largely missing, so results skew optimistic. The Market scan page shows a comparison including delisted names.",
    "美股没有涨跌停；熔断、停牌、盘前盘后价格未模拟。": "US stocks have no daily price limits; circuit breakers, halts and pre/after-hours prices are not simulated.",
    "使用 Yahoo 的 Adj Close（含拆股和分红）计算收益。": "Returns use Yahoo's Adj Close (adjusted for splits and dividends).",
    "股票池来自今天的上市名单，已退市公司不在其中，历史回测会偏乐观。": "The universe comes from today's listings; delisted companies are not included, so historical backtests skew optimistic.",
    "免费行情源，偶有缺日或坏数据；缺失和异常已尽量检查，但不保证与付费数据完全一致。": "Free data source with occasional gaps or bad prints; checked as far as possible, but not guaranteed to match paid data.",
    "历史回测不代表未来；参数是事后选的，存在过拟合风险。请重点看样本外表现。": "Past results do not predict the future; parameters were chosen in hindsight and may be overfit. Focus on out-of-sample results.",
    "核心指标": "Key Metrics",
    "Sortino（只算下跌波动）": "Sortino (downside volatility only)",
    "Calmar（年化÷最大回撤）": "Calmar (CAGR ÷ max drawdown)",
    "最好的一天": "Best day",
    "最差的一天": "Worst day",
    "单日 VaR(95%)：最差的 5% 天数至少亏": "Daily VaR (95%): worst 5% of days lose at least",
    "偏度（负数 = 容易出大跌）": "Skewness (negative = prone to big drops)",
    "峰度（越大 = 极端行情越多）": "Kurtosis (higher = more extreme days)",
    "上涨月份占比": "Share of up months",
    "样本内 / 样本外": "In-sample / Out-of-sample",
    "时间段": "Period",
    "策略Sharpe": "Strategy Sharpe",
    "样本内（": "In-sample (",
    "样本外（": "Out-of-sample (",
    "前 70%）": "first 70%)",
    "后 30%）": "last 30%)",
    "样本外 Sharpe 没有明显低于样本内，参数相对稳健。": "Out-of-sample Sharpe is not much lower than in-sample, so the parameters look relatively robust.",
    "样本外 Sharpe 明显低于样本内，可能有过拟合，请谨慎。": "Out-of-sample Sharpe is clearly lower than in-sample; possible overfitting, be careful.",
    "注意：策略规则和默认参数是事后看过全部历史才定的，这里的拆分只是粗略的参考，不是严格的样本外检验。": "Note: the rules and default parameters were set after seeing all history, so this split is a rough reference, not a strict out-of-sample test.",
    "区间太短，无法拆分。": "The period is too short to split.",
    "月度收益": "Monthly Returns",
    "绿涨红跌": "green up, red down",
    "策略全年": "Strategy (year)",
    "持有全年": "Hold (year)",
    "滚动 Sharpe": "Rolling Sharpe",
    "近 126 个交易日": "last 126 trading days",
    "仓位占用": "Exposure",
    "100% = 满仓": "100% = fully invested",
    "和其他指数比": "Compared with other indices",
    "起点都设为 1": "all start at 1",
    "交易分布": "Trade Distribution",
    "交易笔数": "Trades",
    "平均盈利": "Average win",
    "平均亏损": "Average loss",
    "盈亏比（平均盈利÷平均亏损）": "Payoff ratio (avg win ÷ avg loss)",
    "每笔期望收益": "Expectancy per trade",
    "最好一笔": "Best trade",
    "最差一笔": "Worst trade",
    "平均持仓天数": "Average holding days",
    "最长连续亏损笔数": "Longest losing streak",
    "没有逐笔交易（一直持仓或多品种轮动），不显示交易分布。": "No individual trades (always invested or multi-asset rotation), so no trade distribution is shown.",
    "每笔收益分布": "Distribution of returns per trade",
    "持仓天数分布": "Distribution of holding days",
    "样本太少": "Too few samples",
    "手续费敏感性": "Fee Sensitivity",
    "把成本放大后，策略还赚钱吗": "does the strategy still make money when costs rise?",
    "手续费水平": "Fee level",
    "零成本": "Zero cost",
    "当前设置": "Current setting",
    " 倍": "x",
    "成本涨到 4 倍后仍然有正的 Sharpe，对手续费不算敏感。": "Sharpe stays positive even at 4x costs, so the strategy is not very fee sensitive.",
    "成本涨到 4 倍后 Sharpe 已很低，策略对手续费敏感。": "Sharpe is very low at 4x costs; the strategy is fee sensitive.",
    "导出净值 CSV": "Export equity CSV",
    "导出交易明细 CSV": "Export trades CSV",
    "策略净值": "Strategy equity",
    "持有净值": "Hold equity",
    "回撤%": "Drawdown %",
    "卖出日": "Exit date",
    "持仓天数": "Holding days",
    "收益%": "Return %",
    "盈亏(元)": "P&L (CNY)",
    "量化日报": "Quant Dashboard",
    "切换语言": "Switch language",
    "搜索代码或名称": "Search symbol or name",
    "立即更新数据": "Refresh market data",
    "主导航": "Main navigation",
    "股票页面": "Stock pages",
    "今日": "Today",
    "全市场": "Market scan",
    "自选": "Watchlist",
    "回测实验室": "Backtest Lab",
    "信号跟踪": "Signal Review",
    "引擎对比": "Engine Comparison",
    "代码": "Code",
    "自选股": "Watchlist",
    "A 股市场概览": "A-Share Market Overview",
    "策略信号": "Strategy Signals",
    "点击组合建议可查看标的详情。": "Select the portfolio recommendation to view the underlying asset.",
    "组合建议": "Portfolio Recommendation",
    "下一交易日目标仓位": "Target exposure for next session",
    "当前仓位": "Current exposure",
    "目标仓位": "Target exposure",
    "组合年化": "Annualized return",
    "超额 Sharpe": "Excess Sharpe",
    "最大回撤": "Max drawdown",
    "年化波动": "Annualized volatility",
    "信号": "Signal",
    "目标仓位": "Target exposure",
    "距 MA300": "Distance to MA300",
    "20 日年化波动率": "20-day annualized volatility",
    "20日波动率": "20-day volatility",
    "距 52 周高点": "Drawdown from 52-week high",
    "距52周高点": "Drawdown from 52-week high",
    "RSI(14)": "RSI (14)",
    "收盘价": "Close",
    "今日涨跌": "Daily change",
    "今年以来": "Year to date",
    "数据日期": "Data as of",
    "更新于": "Updated",
    "启动中": "Starting",
    "正在启动": "Starting",
    "已有更新正在运行": "An update is already running",
    "正在读取当前进度…": "Reading current progress…",
    "准备更新…": "Preparing update…",
    "启动失败": "Could not start update",
    "状态读取失败": "Could not read update status",
    "已完成": "Complete",
    "正在收尾": "Finishing up",
    "全市场回测完成": "Market scan complete",
    "全市场回测中": "Running market scan",
    "正在补齐全市场数据": "Filling market data gaps",
    "正在整理全市场数据": "Finalizing market data",
    "行情下载": "Downloading prices",
    "更新失败": "Update failed",
    "更新中": "Updating",
    "观察列表": "Watchlist",
    "点一行查看详情": "Select a row for details",
    "自动点评": "Market Notes",
    "最近提醒": "Recent Alerts",
    "Mac 通知的记录": "macOS notification history",
    "品种": "Asset",
    "股票代码": "股票代码 / Symbol",
    "代码 / 名称": "代码 / Name",
    "策略": "Strategy",
    "策略年化": "Strategy CAGR",
    "策略回撤": "Strategy drawdown",
    "胜率": "Win rate",
    "交易": "Trades",
    "年化收益": "Annualized return",
    "总收益": "Total return",
    "年化": "CAGR",
    "最大回撤": "Max drawdown",
    "回撤": "Drawdown",
    "波动率": "Volatility",
    "夏普": "Sharpe",
    "年化波动": "Annualized volatility",
    "Sharpe": "Sharpe",
    "超额Sharpe": "Excess Sharpe",
    "回测实验室": "Backtest Lab",
    "换策略、换品种、改参数，实时回测": "Change the strategy, asset, or parameters and run a backtest.",
    "股票、ETF 或基准": "Stock, ETF, or benchmark",
    "开始日期": "Start date",
    "单边成本 bp": "One-way cost (bp)",
    "运行回测": "Run backtest",
    "恢复默认": "Reset defaults",
    "买入持有": "Buy and hold",
    "买入持有（基准）": "Buy and Hold (Benchmark)",
    "回撤不代表未来": "Past performance does not predict future results",
    "最近收益": "Recent Returns",
    "最近交易": "Recent Trades",
    "净值": "Equity Curve",
    "策略 vs 一直持有": "Strategy vs. Buy and Hold",
    "交易记录": "Trade History",
    "导出 Excel": "Export to Excel",
    "导出 CSV": "Export CSV",
    "添加": "Add",
    "添加自选股": "Add to watchlist",
    "请输入代码": "Enter a symbol",
    "搜索": "Search",
    "板块": "Board",
    "行业": "Industry",
    "筛选": "Filter",
    "全部": "All",
    "全部股票": "All stocks",
    "今天买入信号": "Buy signals today",
    "今天卖出信号": "Sell signals today",
    "策略当前持仓": "Currently held by strategy",
    "策略当前空仓": "Currently flat",
    "策略比持有好": "Strategy beats buy-and-hold",
    "已退市股票": "Delisted stocks",
    "上一页": "Previous",
    "下一页": "Next",
    "逐只结果": "Results by symbol",
    "策略在全市场有没有用？": "Does the strategy work across the market?",
    "年化、回撤、超额 Sharpe 在同一时间段计算；\"持有\"指同一只股票一直拿着不动。个股按单边 8bp 成本（含印花税），考虑一字涨跌停；价格为后复权。": "Annualized return, drawdown, and excess Sharpe use the same period. “Buy and hold” means holding the same stock throughout. Stocks use an 8 bp one-way cost including stamp duty; limit-up/limit-down sessions are accounted for, and prices are adjusted.",
    "当时说买的，后来涨了没有？这是最诚实的检验": "Did the stocks flagged as buys actually rise? This is the reality check.",
    "区间": "Period",
    "在回测实验室调参数": "Tune parameters in Backtest Lab",
    "这只股票，哪个策略好？": "Which strategy works best for this stock?",
    "回测": "Backtest",
    "引擎对比": "Engine Comparison",
    "同一只股票、同一个策略、同一组参数：自研引擎 vs backtrader vs RQAlpha 米筐": "Same stock, strategy, and parameters: compare the in-house engine with Backtrader and RQAlpha.",
    "三个引擎一起跑": "Run all three engines",
    "一致性": "Consistency",
    "两两比较": "Pairwise comparison",
    "逐笔对照": "Trade-by-Trade Comparison",
    "指标": "Metric",
    "买入日": "Entry date",
    "年化收益率": "Annualized return",
    "样本内": "In-sample",
    "样本外": "Out-of-sample",
    "默认参数": "Default parameters",
    "最佳参数": "Best parameters",
    "用最佳参数运行回测": "Run with best parameters",
    "信号跟踪": "Signal Review",
    "最近的信号": "Recent Signals",
    "显示全部": "Show all",
    "只读": "Read-only",
    "历史版本": "Version history",
    "恢复这个版本": "Restore this version",
    "放弃修改": "Discard changes",
    "保存并自检": "Save and validate",
    "保存": "Save",
    "保存中…": "Saving…",
    "运行中…": "Running…",
    "扫描中": "Scanning",
    "已完成": "Complete",
    "已暂停": "Paused",
    "开始扫描全部 A 股": "Scan all A-shares",
    "重新扫描": "Scan again",
    "继续扫描": "Resume scan",
    "新建": "New",
    "我的策略": "My Strategies",
    "内置策略": "Built-in Strategies",
    "自研引擎": "In-house engine",
    "持仓中": "Open",
    "空仓": "Flat",
    "买入": "Buy",
    "卖出": "Sell",
    "加仓": "Add",
    "减仓": "Reduce",
    "换仓": "Rebalance",
    "持有": "Hold",
    "空仓观望": "Stay in cash",
    "退市": "Delisted",
    "完成": "Done",
    "通过": "Passed",
    "出错": "Error",
    "全部通过": "All passed",
    "保存失败": "Save failed",
    "没有改动": "No changes",
    "已保存": "Saved",
    "恢复失败": "Restore failed",
    "取消": "Cancel",
    "仅供研究学习，不构成投资建议；回测不代表未来 · 颜色：红涨绿跌 · 只在本机运行（127.0.0.1），不连接交易接口": "For research and education only; not investment advice. Backtests do not predict future results · Red = up, green = down · Runs locally (127.0.0.1); no brokerage connection",
    "加载失败": "Failed to load",
    "数据不足": "Insufficient data",
    "没有数据": "No data",
    "暂时没有": "Nothing here yet",
    "还没有": "None yet",
    "小时": "hours",
    "分钟": "minutes",
    "组参数": "parameter sets",
    "个策略": "strategies",
    "只股票": "stocks",
    "A 股": "A-shares",
    "A股": "A-shares",
    "沪深300ETF": "CSI 300 ETF",
    "米筐": "Ricequant",
    "回测统计": "Backtest Statistics",
    "最大连续亏损": "Max consecutive losses",
    "收益风险比": "Profit/loss ratio",
    "信息比率": "Information ratio",
    "Sortino": "Sortino",
    "胜率%": "Win rate (%)",
    "交易次数": "Trade count",
    "平均持有天数": "Average holding period (days)",
    "手续费+税（元）": "Fees + tax (CNY)",
    "百分比": "Percent",
    "股票名称": "股票名称 / Company",
    "证券名称": "证券名称 / Security",
    "只在本机运行": "Runs locally only",
    "三个引擎是否一致": "Do all three engines agree?",
    "三引擎对比": "Three-Engine Comparison",
    "当时说买的，后来涨了没有？": "Did the stocks flagged as buys actually rise?",
    "这是最诚实的检验": "This is the reality check.",
    "开始优化": "Start optimization",
    "开始统计日期": "Statistics start date",
    "样本外从哪天开始": "Out-of-sample start date",
    "网格搜索": "Grid search",
    "优化目标": "Optimization metric",
    "参数平原": "Parameter plateau",
    "全部成交记录": "All fills",
    "成交笔数": "Number of fills",
    "成交价": "Fill price",
    "盈亏（元）": "P&L (CNY)",
    "回测规则": "Backtest rules",
    "引擎规则": "Engine rules",
    "运行失败": "Run failed",
    "结果展示（单个引擎）": "Results (single engine)",
    "全市场三引擎扫描": "Full-market, three-engine scan",
    "当前文件还没保存，确定要离开吗": "This file has unsaved changes. Leave anyway?",
    "下一次运行回测就会用新代码。": "The next backtest will use the updated code.",
    "下载历史数据，之后每天会自动更新。": "Download historical data; it will update automatically each day.",
    "还没有全市场数据": "No full-market data yet",
    "没有逐笔交易（一直持仓或多品种轮动）": "No individual trades (buy-and-hold or multi-asset rotation).",
    "三个引擎吃同一份数据，差别只来自引擎本身。": "All three engines use the same data, so differences come from the engines themselves.",
    "保存失败": "Save failed",
    "登录已失效：在终端运行": "Session expired. In Terminal, run ",
    "样本内挑出来的最佳参数，到了样本外往往就不灵了": "The best in-sample parameters often fail out of sample.",
    "样本内好的参数，样本外大体也好，比较可信": "Parameters that work in and out of sample are more credible.",
    "样本内排名和样本外几乎没关系": "In-sample and out-of-sample rankings are barely related.",
    "可以关掉网页，扫描在后台继续": "You can close this page while the scan runs in the background.",
    "每只股票 × 7 个策略 × 3 个引擎，默认参数。全部跑完大约": "Each stock × 7 strategies × 3 engines, using default parameters. A full run takes about ",
    "每天收盘后发出的买入/卖出信号都会原样记录下来、不再修改。": "Buy and sell signals are recorded after each close and never edited.",
    "还没有信号记录。每天收盘更新后会自动记录，过几天就能看到统计。": "No signal history yet. Signals are recorded after each daily update.",
    "当时说买的，后来涨了没有": "Did the stocks flagged as buys actually rise?",
    "计算收益，并和同期的沪深300ETF（美股为 SPY）比较。": "Returns are compared with the CSI 300 ETF over the same period (SPY for U.S. stocks).",
    "只有从上线那天起的记录才算真正的样本外": "Only records since launch count as genuinely out-of-sample.",
    "买入持有（基准）": "Buy and Hold (Benchmark)",
    "持仓状态一致": "Position agreement",
    "每天持仓状态一致": "Daily position agreement",
    "日收益相关系数": "Daily return correlation",
    "期末净值差": "Ending equity difference",
    "年化中位数": "Median annualized return",
    "年化差（绝对值中位数）": "Median absolute CAGR difference",
    "跑赢持有的股票": "Stocks beating buy-and-hold",
    "代码页": "Code Editor",
    "只读（引擎框架）": "Read-only (engine framework)",
    "股票代码": "股票代码 / Symbol",
    "证券名称": "证券名称 / Security",
    "公司名称": "公司名称 / Company",
    "持仓比例": "Exposure",
    "持有Sharpe": "Buy-and-hold Sharpe",
    "持有回撤": "Buy-and-hold drawdown",
    "持有年化": "Buy-and-hold CAGR",
    "持有最大回撤%": "Buy-and-hold max drawdown (%)",
    "策略最大回撤%": "Strategy max drawdown (%)",
    "样本内Sharpe": "In-sample Sharpe",
    "样本外Sharpe": "Out-of-sample Sharpe",
    "明日仓位%": "Next-session exposure (%)",
    "收盘价": "Close",
    "距1年高点": "Drawdown from 1-year high",
    "近1月": "1-month return",
    "收益风险比": "Profit/loss ratio",
    "信息比率": "Information ratio",
    "平均持有天数": "Average holding period (days)",
    "手续费+税（元）": "Fees + tax (CNY)",
    "保存并运行回测": "Save and run backtest",
    "分享给家人看": "Share with family",
    "复制链接": "Copy link",
    "停止分享": "Stop sharing",
    "分享中：": "Sharing: ",
    "扫描中": "Scanning",
    "已完成": "Complete",
    "已暂停": "Paused",
    "继续扫描": "Resume scan",
    "重新扫描": "Scan again",
    "开始扫描全部 A 股": "Scan all A-shares",
    "内置策略": "Built-in strategies",
    "我的策略": "My strategies",
    "新建": "New",
    "已保存": "Saved",
    "没有改动": "No changes",
    "已放弃修改": "Changes discarded",
    "已恢复": "Restored",
    "停止": "Stop",
    "股票拿出来": "select those stocks",
    "代码或名称": "symbol or name"
    ,"自选股和 ETF 逐只回测，同一个策略、默认参数": "Run each watchlist stock and ETF with the same strategy and default parameters."
    ,"当时说买的，后来涨了没有？这是最诚实的检验": "Did the stocks flagged as buys actually rise? This is the reality check."
    ,"每天收盘后发出的买入/卖出信号都会原样记录下来、不再修改。之后按": "Buy/sell signals are recorded after each close and never edited. Returns are calculated assuming"
    ,"下一个交易日开盘买入，持有 5 / 20 / 60 个交易日": "buying at the next open and holding for 5, 20, or 60 trading days"
    ,"只有从上线那天起的记录才算真正的样本外，所以这里的数据会越积越多。": "Only records since launch count as truly out of sample, so this history grows over time."
    ,"年化、回撤、超额 Sharpe 在同一时间段计算；": "CAGR, drawdown, and excess Sharpe use the same period; "
    ,"“持有”指同一只股票一直拿着不动。个股按单边 8bp 成本（含印花税），考虑一字涨跌停；价格为后复权。": "“Buy and hold” means holding the same stock throughout. Stocks use an 8 bp one-way cost including stamp duty; limit-up/limit-down sessions are accounted for, and prices are adjusted."
    ,"策略在全市场有没有用？": "Does the strategy work across the market?"
    ,"这只股票，哪个策略好？": "Which strategy works best for this stock?"
    ,"同一只股票、同一个策略、同一组参数：自研引擎 vs backtrader vs RQAlpha 米筐": "Same stock, strategy, and parameters: compare the in-house engine with Backtrader and RQAlpha."
    ,"信号跟踪": "Signal Review"
    ,"每只股票 × 7 个策略 × 3 个引擎，默认参数": "Each stock × 7 strategies × 3 engines, using default parameters"
    ,"通常几秒到一分钟": "Usually a few seconds to one minute"
    ,"好了会自动出现": "Results will appear automatically when ready."
    ,"还没有提醒。每天更新完会在 Mac 右上角弹出通知。": "No alerts yet. macOS notifications appear after the daily update."
    ,"最近的信号": "Recent Signals"
    ,"趋势集成 + RSI2 + 海龟": "Trend Ensemble + RSI(2) + Donchian"
    ,"趋势集成": "Trend Ensemble"
    ,"海龟系统": "Donchian System"
    ,"双动量轮动": "Dual Momentum Rotation"
    ,"买入持有（基准）": "Buy and Hold (Benchmark)"
    ,"RQAlpha 米筐": "RQAlpha"
    ,"米筐（国内）": "Ricequant (China)"
    ,"结果展示（单个引擎）": "Results (single engine)"
    ,"样本内（用来挑参数）": "In-sample (parameter selection)"
    ,"样本外（检验）": "Out-of-sample (validation)"
    ,"最佳参数周围一圈的平均值": "Average metric around the best parameters"
    ,"蓝框": "Blue outline"
    ,"橙框": "orange outline"
    ,"绿色越深越好，红色越深越差。": "Darker green is better; darker red is worse."
    ,"空白 = 这个引擎那天没有开新仓": "blank = the engine did not open a position that day"
    ,"点击组合建议可查看标的详情。": "Select the portfolio recommendation to view the underlying asset."
    ,"只读）": " (read-only)"
    ,"代码或名称": "symbol or name"
    ,"输入 6 位沪深 A 股代码（60/68/00/30 开头）": "Enter a 6-digit Shanghai/Shenzhen A-share symbol (starts with 60, 68, 00, or 30)"
    ,"还没有，点上面": "Nothing here yet. Select "
    ,"点一行查看详情": "Select a row for details"
    ,"正在开始": "Starting"
    ,"正在下载": "Downloading"
    ,"保存并运行回测": "Save and run backtest"
    ,"确定要离开吗": "Leave this page?"
    ,"自研引擎": "In-house engine"
    ,"backtrader": "Backtrader"
    ,"代码编辑": "Code Editor"
    ,"页面": "Page"
    ,"A股市场概览": "A-Share Market Overview"
    ,"美股市场概览": "U.S. Market Overview"
    ,"市场概览": "Market Overview"
    ,"暂无基准行情": "No benchmark data available"
    ,"当前没有可用的策略信号": "No strategy signals available"
    ,"当前建议空仓，原因：": "Cash is recommended. Reason: "
    ,"当前建议空仓": "Cash is currently recommended"
    ,"当前仓位": "Current position"
    ,"目标仓位": "Target position"
    ,"下一交易日目标仓位": "Target exposure for next session"
    ,"自动点评": "Market Notes"
    ,"最近提醒": "Recent Alerts"
    ,"Mac 通知的记录": "macOS notification history"
    ,"还没有提醒。每天更新完会在 Mac 右上角弹出通知。": "No alerts yet. A macOS notification appears after each daily update."
    ,"数据日期": "Data as of"
    ,"更新于": "Updated"
    ,"截至": "As of"
    ,"收盘价": "Close"
    ,"今日涨跌": "Daily change"
    ,"今年以来": "Year to date"
    ,"20 日年化波动率": "20-day annualized volatility"
    ,"观察列表": "Market Watch"
    ,"股票 / Stock · 代码 / Symbol": "Company / Symbol"
    ,"20日波动率": "20-day volatility"
    ,"距52周高点": "Drawdown from 52-week high"
    ,"当前没有全市场数据": "Full-market data is not available"
    ,"全市场扫描只支持 A 股": "Full-market scans are available for A-shares only"
    ,"还没有全市场数据": "No full-market data yet"
    ,"全市场扫描只支持A股": "Full-market scans are available for A-shares only"
    ,"比持有好的股票": "Stocks outperforming buy-and-hold"
    ,"持续性": "Persistence"
    ,"样本内前10% → 样本外": "Top 10% in-sample → out-of-sample"
    ,"今日买入": "Buy signals today"
    ,"今日卖出": "Sell signals today"
    ,"怎么看这张表：": "How to read this table: "
    ,"策略的超额 Sharpe 高于一直持有这只股票的比例，超过 50% 才说明策略整体有用。": "the share of stocks where the strategy's excess Sharpe beats buy-and-hold. Above 50% suggests broad usefulness."
    ,"前半段": "first half"
    ,"后半段": "second half"
    ,"年以前": " and earlier"
    ,"年以后": " and later"
    ,"共 ": "Total: "
    ,"只（排除 ST ": " symbols (excluding ST: "
    ,"只）": ")"
    ,"含退市 ": "including delisted: "
    ,"只做统计": " counted for statistics"
    ,"自选股和 ETF 逐只回测，同一个策略、默认参数": "Backtest each watchlist stock and ETF with the same strategy and default parameters."
    ,"输入 6 位沪深 A 股代码（60/68/00/30 开头）": "Enter a 6-digit Shanghai/Shenzhen A-share symbol (starts with 60, 68, 00, or 30)"
    ,"请输入 6 位沪深 A 股代码（60/68/00/30 开头）": "Enter a 6-digit Shanghai/Shenzhen A-share symbol (starts with 60, 68, 00, or 30)"
    ,"请输入美股代码，例如 AAPL": "Enter a U.S. ticker, such as AAPL"
    ,"6 位代码": "6-digit symbol"
    ,"美股代码，如 AAPL": "U.S. ticker, e.g. AAPL"
    ,"正在下载：": "Downloading: "
    ,"，好了会自动出现": ". It will appear when ready."
    ,"计算中…": "Calculating…"
    ,"下载中，稍等": "Downloading, please wait"
    ,"个股": "Stock"
    ,"移除": "Remove"
    ,"移出自选": "Remove from watchlist"
    ,"＋ 加入自选": "+ Add to watchlist"
    ,"加入自选": "Add to watchlist"
    ,"6个月": "6 months"
    ,"1年": "1 year"
    ,"3年": "3 years"
    ,"5年": "5 years"
    ,"明日（": "Next session ("
    ,"买入（次日开盘）": "Buy (next open)"
    ,"持仓（越深仓位越重）": "Position (darker means larger exposure)"
    ,"价格为后复权（除权除息后连续可比，数值和行情软件不同）。数据 ": "Prices are adjusted for splits and dividends, so values may differ from quote apps. Data: "
    ,"个交易日（": " trading days ("
    ,"单边成本 ": "One-way cost "
    ,"这个策略没有交易记录": "This strategy has no trade records"
    ,"开 ": "Open "
    ,"高 ": "High "
    ,"低 ": "Low "
    ,"收 ": "Close "
    ,"仓位": "Position"
    ,"成交量": "Volume"
    ,"买": "Buy"
    ,"卖": "Sell"
    ,"多品种策略：「品种」只影响对比基准": "Multi-asset strategy: the selected asset only changes the benchmark"
    ,"这个策略没有可调参数": "This strategy has no adjustable parameters"
    ,"其他": "Other"
    ,"自选股": "Watchlist stocks"
    ,"ETF": "ETFs"
    ,"交易": "Trades"
    ,"持仓时间": "Time in market"
    ,"一直持有": "Buy and hold"
    ,"区间 ": "Period "
    ,"当前持仓 ": "Current position "
    ,"，明日开盘目标 ": "; target at next open "
    ,"没有逐笔交易（一直持仓或多品种轮动）": "No individual trades (buy-and-hold or multi-asset rotation)"
    ,"还没有信号记录。每天收盘更新后会自动记录，过几天就能看到统计。": "No signal history yet. Signals are recorded after each market close. Statistics will appear once enough sessions have passed."
    ,"已记录 ": "Recorded "
    ," 条信号 · 最早 ": " signals · Earliest "
    ," · 最近 ": " · Latest "
    ," · 数据截至 ": " · Prices through "
    ,"尚无信号完成 5 个持有交易日，预计首批统计日期 ": "No signal has completed a 5-session holding period. First results are expected around "
    ,"待数据更新": "pending data update"
    ,"按工作日估算，遇市场假期顺延": "estimated using weekdays; market holidays may extend this date"
    ,"天 样本": "-session samples"
    ,"天 平均": "-session average"
    ,"跑赢大盘": "Beat benchmark"
    ,"等待样本": "Awaiting samples"
    ,"信号日": "Signal date"
    ,"至今比大盘": "To date vs. benchmark"
    ,"今日": "Today"
    ,"近1周": "1 week"
    ,"近1月": "1 month"
    ,"近3月": "3 months"
    ,"近1年": "1 year"
    ,"今年": "YTD"
    ,"当时说买的，后来涨了没有？这是最诚实的检验": "Did stocks flagged as buys rise afterward? This is the reality check."
    ,"查看 / 修改策略代码": "View / edit strategy code"
    ,"开始统计日期": "Start date"
    ,"初始资金（元）": "Initial capital (CNY)"
    ,"这个策略没有可调参数": "This strategy has no adjustable parameters"
    ,"没有找到引擎库（项目里的 .engines_lib 文件夹）。backtrader 和 RQAlpha 装在这个文件夹里，和主环境的 pandas 版本隔离。": "Engine libraries not found (.engines_lib). Backtrader and RQAlpha are isolated here from the main environment's pandas version."
    ,"没有交易": "No trades"
    ,"买入价": "Entry price"
    ,"卖出价": "Exit price"
    ,"手续费+税": "Fees + tax"
    ,"费用": "Fees"
    ,"收益": "Return"
    ,"用时 ": "Elapsed "
    ,"秒": " sec"
    ,"价格：后复权价整体乘以 ": "Prices: adjusted prices are scaled by "
    ,"，让最后一天等于真实股价（不改变任何一天的涨跌幅，只让\"一手多少钱\"符合实际）。": " so the last day matches the market price. Daily returns are unchanged; only the cash value of a board lot is aligned."
    ,"RQAlpha 自带报告：总收益 ": "RQAlpha report: total return "
    ,"，年化 ": ", annualized "
    ,"，最大回撤 ": ", max drawdown "
    ,"（从数据第一天算起，包含预热期，所以和上面的数字不同）": " (measured from the first data day, including warm-up, so it differs from the figures above)"
    ,"没有被拒的订单": "No rejected orders"
    ,"逐笔交易": "Trade Details"
    ,"从空仓买入到全部卖出算一笔；中间的加减仓算在同一笔里": "One trade runs from opening a position to fully closing it; additions and reductions in between remain part of the same trade."
    ,"全部成交记录（": "All fills ("
    ,"笔）": " fills)"
    ,"股数": "Shares"
    ,"方向": "Side"
    ,"参数优化": "Parameter Optimization"
    ,"网格搜索 + 样本内/样本外检验": "Grid search + in-sample/out-of-sample validation"
    ,"用上面选的股票和策略。统计区间切成两段：": "Uses the selected stock and strategy above. The analysis period is split into two parts: "
    ,"用来挑参数": "used for parameter selection"
    ,"是挑完之后才看的\"考试\"。样本内挑出来的最佳参数，到了样本外往往就不灵了——这就是过拟合。": "is the unseen evaluation period. Parameters that look best in-sample often fail out-of-sample; this is overfitting."
    ,"步长": "Step"
    ,"可不选": "optional"
    ,"优化目标": "Optimization metric"
    ,"年化收益": "Annualized return"
    ,"Calmar（年化/回撤）": "Calmar (annualized return / drawdown)"
    ,"最大回撤最小": "Minimize max drawdown"
    ,"不填 = 自动：统计区间的后 30%": "Leave blank = automatic: last 30% of the period"
    ,"共 ": "Total: "
    ," 组（上限 ": " sets (limit "
    ,"）太多了": ") — too many"
    ,"启动中…": "Starting…"
    ,"完成，用时 ": "Complete in "
    ,"组合太少，没法算": "Too few parameter sets to calculate"
    ,"关系比较弱，优化结果要打折扣看": "The relationship is weak; treat the optimization result cautiously."
    ,"：样本内排名和样本外几乎没关系——挑出来的\"最佳参数\"": ": in-sample and out-of-sample rankings are barely related; the selected ‘best parameters’ "
    ,"主要是运气": "are likely luck"
    ,"年化": "CAGR"
    ,"绿色越深越好，红色越深越差。": "Darker green is better; darker red is worse."
    ,"蓝框 = 样本内最佳，橙框 = 默认参数。": "Blue outline = best in-sample; orange outline = default parameters."
    ,"蓝框": "Blue outline"
    ,"橙框": "orange outline"
    ,"默认参数：": "Default parameters: "
    ,"最佳参数：": "Best parameters: "
    ," 组失败：": " parameter sets; "
    ,"运行失败": "Run failed"
    ,"出错：": " error: "
    ,"用最佳参数运行回测": "Run backtest with best parameters"
    ,"参数平原": "parameter plateau"
    ,"关系比较弱": "The relationship is weak"
    ,"扫描中": "Scanning"
    ,"全市场回测中": "Running full-market backtest"
    ,"全市场回测完成": "Full-market backtest complete"
    ,"当前文件还没保存，确定要离开吗": "This file has unsaved changes. Leave anyway?"
    ,"股票代码 / Symbol": "股票代码 / Symbol"
    ,"股票名称 / Company": "股票名称 / Company"
    ,"证券名称 / Security": "证券名称 / Security"
    ,"美股": "U.S. Stocks"
    ,"A股": "A-Shares"
    ,"A 股": "A-Shares"
    ,"组合：趋势集成 + RSI2 + 海龟": "Portfolio: Trend Ensemble + RSI(2) + Donchian"
    ,"三个策略各 1/3 仓位。赚钱时段不同，合起来回撤最小。": "One-third exposure per strategy. Their return patterns differ, reducing combined drawdown."
    ,"趋势集成（4 条均线）": "Trend Ensemble (4 Moving Averages)"
    ,"看 100/150/200/250 日均线，站上几条持几成仓，再乘以波动率仓位。": "Exposure follows how many of the 100/150/200/250-day averages price is above, then scales by volatility."
    ,"趋势 + 波动率目标（单均线）": "Trend + Volatility Target (Single Moving Average)"
    ,"收盘在均线上方持有，下方空仓；仓位按波动率调整。": "Hold above the moving average and stay in cash below it; size adjusts with volatility."
    ,"海龟系统 1（通道突破）": "Donchian System 1 (Channel Breakout)"
    ,"突破 N 日最高价进场；跌破 M 日最低价或回撤 k×ATR 出场。": "Enter on an N-day high breakout; exit below the M-day low or after a k×ATR drawdown."
    ,"RSI(2) 均值回归": "RSI(2) Mean Reversion"
    ,"长期上升趋势中买短期超跌，反弹就卖。胜率高、持仓短，怕交易成本。": "Buy short-term oversold dips in long-term uptrends and sell the rebound. High win rate and short holds, but sensitive to costs."
    ,"MA + ADX（你的原版）": "MA + ADX (Original)"
    ,"快线上穿慢线且 ADX 高于阈值买入，下穿卖出。": "Buy when the fast average crosses above the slow average and ADX exceeds its threshold; sell on a cross below."
    ,"MA + ADX 改进版": "MA + ADX (Improved)"
    ,"原版 + 200 日均线过滤 + 跟踪止损 + 波动率仓位。": "Original strategy plus a 200-day trend filter, trailing stop, and volatility-based sizing."
    ,"双动量轮动（多品种）": "Dual Momentum Rotation (Multi-Asset)"
    ,"每月末在两个股票类资产里选涨得多的；跑不赢现金就换债券。": "At each month-end, choose the stronger of two equity assets; switch to bonds if neither beats cash."
    ,"买入持有（基准）": "Buy and Hold (Benchmark)"
    ,"一直满仓，用来做比较。": "Fully invested throughout as a benchmark."
    ,"全球大盘": "Broad Market"
    ,"立即更新数据": "Refresh market data"
    ,"运行中": "Running"
    ,"运行中…": "Running…"
    ,"已完成": "Complete"
    ,"正在收尾": "Finishing up"
    ,"行情下载": "Downloading prices"
    ,"持仓比例": "Exposure"
    ,"样本内": "In-sample"
    ,"样本外": "Out-of-sample"
    ,"策略当前持仓": "Currently held by strategy"
    ,"策略当前空仓": "Currently flat"
    ,"策略比持有好": "Strategy beats buy-and-hold"
    ,"已退市股票": "Delisted stocks"
    ,"尚无": "Not available yet"
    ,"年化波动": "Annualized volatility"
    ,"超额 Sharpe": "Excess Sharpe"
    ,"超额Sharpe": "Excess Sharpe"
    ,"最大回撤": "Max drawdown"
    ,"总收益": "Total return"
    ,"胜率": "Win rate"
    ,"期末资产": "Ending value"
    ,"持仓中": "Open"
    ,"空仓观望": "Stay in cash"
    ,"持有": "Hold"
    ,"加仓": "Add"
    ,"减仓": "Reduce"
    ,"换仓": "Rebalance"
    ,"组合建议": "Portfolio Recommendation"
    ,"自动点评": "Market Notes"
    ,"下一页": "Next"
    ,"全市场": "Market Scan"
    ,"每只股票 × 7 个策略 × 3 个引擎，默认参数。全部跑完大约": "Each stock × 7 strategies × 3 engines, using default parameters. A full run takes about "
    ,"每只股票 × 7 个策略 × 3 个引擎，默认参数": "Each stock × 7 strategies × 3 engines, using default parameters"
    ,"好了会自动出现": "Results will appear automatically when ready."
    ,"现在可以关掉网页，扫描在后台继续": "You can close this page while the scan runs in the background."
    ,"通常几秒到一分钟": "Usually a few seconds to one minute"
    ,"没有找到引擎库": "Engine libraries not found"
    ,"持仓时间": "Time in market"
    ,"运行失败": "Run failed"
    ,"错误": "Error"
    ,"数据不足": "Insufficient data"
    ,"加载失败": "Failed to load"
    ,"没有数据": "No data"
    ,"还没有": "None yet"
    ,"暂时没有": "Nothing here yet"
    ,"只读（引擎框架）": "Read-only (engine framework)"
    ,"当前文件还没保存，确定要离开吗？": "This file has unsaved changes. Leave anyway?"
    ,"网址刚更新过，发给对方前请用这个最新的链接。": "The link has changed. Share this latest link."
    ,"对方打开这个链接就能看到和你现在一样的网页；网址是临时的，断线重连会换一个（这个页面会自动跟着更新）": "Anyone with this temporary link can view the same page. The link may change after a disconnection; this page will update automatically."
    ,"生成一个临时网址，对方不用在同一个网络也能打开这个网页；断线会自动重连": "Create a temporary link that works outside your network. It reconnects automatically after a disconnection."
    ,"网址是临时的，断线重连会换一个": "The temporary link may change after a disconnection."
    ,"网址刚更新过": "The link has changed"
    ,"历史数据不足": "Not enough historical data"
    ,"股票": "Stock"
    ,"名称": "Name"
    ,"代码": "Symbol"
    ,"板块": "Board"
    ,"行业": "Industry"
    ,"信号": "Signal"
    ,"日期": "Date"
    ,"类型": "Type"
    ,"股票名称": "股票名称 / Company"
    ,"证券名称": "证券名称 / Security"
    ,"开盘": "Open"
    ,"最高": "High"
    ,"最低": "Low"
    ,"收盘": "Close"
    ,"买入价": "Entry price"
    ,"卖出价": "Exit price"
    ,"样本": "Samples"
    ,"持有": "Hold"
    ,"历史回测": "Historical backtest"
    ,"运行中": "Running"
    ,"暂时没有": "Nothing here yet"
    ,"读取当前进度…": "Reading current progress…"
    ,"准备更新…": "Preparing update…"
    ,"启动失败": "Could not start update"
    ,"状态读取失败": "Could not read update status"
    ,"更新失败": "Update failed"
    ,"更新中": "Updating"
    ,"已用 ": "Elapsed "
    ," 分钟，预计还要 ": " min, estimated remaining "
    ," 分钟 · ": " min · "
    ," 个进程并行": " workers in parallel"
    ,"正在扫描": "Scanning"
    ,"已下载 ": "Downloaded "
    ,"只（已上市 ": " symbols (listed: "
    ,"只）": " symbols)"
    ,"数据日期 ": "Data as of "
    ," 只（排除 ST ": " symbols (excluding ST: "
    ," 只）": " symbols)"
    ," · 含退市 ": " · including delisted: "
    ," 只做统计": " included in statistics"
    ,"共 ": "Total: "
    ," 只": " symbols"
    ,"只。": " symbols."
    ," 策略": " Strategies"
    ,"内置策略": "Built-in Strategies"
    ,"我的策略": "My Strategies"
    ,"全市场回测": "Full-Market Backtest"
    ,"策略比一直持有好不好": "Does the strategy beat buy-and-hold?"
    ,"怎么看：": "How to read this: "
    ,"已统计 ": "Analyzed "
    ," 只股票（": " stocks ("
    ," 更新）。": " updated)."
    ,"运行回测": "Run backtest"
    ,"保存并运行回测": "Save and run backtest"
    ,"恢复默认": "Reset defaults"
    ,"开始优化": "Start optimization"
    ,"三个引擎一起跑": "Run all three engines"
    ,"当前版本": "Current version"
    ,"历史版本": "Version history"
    ,"正在查看历史版本（还没恢复）。点「恢复这个版本」才会保存成当前代码。": "Viewing a historical version (not restored). Select ‘Restore this version’ to make it current."
    ,"保存后网页服务会自动重启（约 5 秒）。": "The dashboard restarts after saving (about 5 seconds)."
    ,"注意：src/strategies.py 也被每日任务和全市场扫描使用，改错会影响每天的信号。": "Note: src/strategies.py is also used by daily jobs and full-market scans; errors can affect daily signals."
    ,"已保存 ": "Saved at "
    ,"，旧版本已存进「历史版本」。下一次运行回测就会用新代码。": ". The previous version is in Version History. The next backtest will use this code."
    ,"自检结果": "Validation Results"
    ,"出错": "Errors"
    ," 个出错": " errors"
    ,"已恢复": "Restored"
    ,"（恢复前的代码也存进了历史版本）": " (the previous code was also saved in version history)"
    ,"给这个策略起个名字（比如：突破策略、双均线）：": "Name this strategy (e.g. breakout, dual moving average):"
    ,"＋自选": "+ Watchlist"
    ,"已自选": "In watchlist"
    ,"没有交易记录": "No trade records"
    ,"全部成交记录": "All fills"
    ,"成交笔数": "Fill count"
    ,"价格涨跌": "Price return"
    ,"账户收益": "Portfolio return"
    ,"至今比大盘": "To date vs. benchmark"
    ,"最近收益": "Recent Returns"
    ,"比持有好的股票": "Stocks outperforming buy-and-hold"
    ,"策略年化%": "Strategy CAGR (%)"
    ,"策略最大回撤%": "Strategy max drawdown (%)"
    ,"持有年化%": "Buy-and-hold CAGR (%)"
    ,"明日仓位%": "Next-session exposure (%)"
    ,"样本外从哪天开始": "Out-of-sample start date"
    ,"参数 1": "Parameter 1"
    ,"参数 2（可不选）": "Parameter 2 (optional)"
    ,"不选": "None"
    ,"到": "To"
    ,"步长": "Step"
    ,"没有找到引擎库 .engines_lib": "Engine libraries (.engines_lib) not found"
    ,"· 没取到真实股价，按比例缩放": " · No quote was available; values are scaled proportionally"
    ,"点击组合建议可查看标的详情。": "Select the portfolio recommendation to view the underlying asset."
    ,"结果将自动显示": "Results will appear automatically when ready."
    ,"按工作日估算，遇市场假期顺延": "Estimated using weekdays; market holidays may extend this date."
    ,"策略年化": "Strategy CAGR"
    ,"样本内好的参数，样本外大体也好，比较可信": "Parameters that perform well in-sample and out-of-sample are more credible."
    ,"关系比较弱，优化结果要打折扣看": "The relationship is weak; treat the optimization result cautiously."
    ,"样本内排名和样本外几乎没关系": "In-sample and out-of-sample rankings are barely related."
    ,"最佳参数周围一圈的平均值：": "Average metric around the best parameters: "
    ,"和最佳值差得少 = \"参数平原\"，比较稳健；差得多 = 孤立的尖峰，多半是过拟合": "A small gap suggests a robust parameter plateau; a large gap is an isolated peak and likely overfitting."
    ,"盈亏（元）": "P&L (CNY)"
    ,"股票代码 / Symbol": "股票代码 / Symbol"
    ," 股票代码": " Symbol"
    ,"当前没有可用的策略信号": "No strategy signals are available"
    ,"持仓": "Position"
    ,"明日": "Next session"
    ,"明日仓位": "Next-session exposure"
    ,"天数": "Days"
    ,"次数": "Count"
    ,"5天": "5 days"
    ,"20天": "20 days"
    ,"60天": "60 days"
    ,"至今": "To date"
    ,"有效": "Positive edge"
    ,"策略比持有好多少": "How much the strategy beats buy-and-hold"
    ,"如 ": "e.g. "
    ,"万": "×10,000"
    ,"Backtrader（国外）": "Backtrader (International)"
    ,"backtrader（国外）": "Backtrader (International)"
    ,"RQAlpha 米筐（国内）": "RQAlpha (China)"
    ,"自定义策略": "Custom Strategy"
    ,"自定义Strategy": "Custom Strategy"
    ,"A 股代码": "A-share symbol"
    ,"国外 · 最常用的开源 Python 事件驱动回测框架": "International · Popular open-source event-driven Python backtesting framework"
    ,"国内 · 米筐科技开源回测框架，内置 A 股交易规则": "China · Open-source Ricequant backtesting framework with built-in A-share trading rules"
    ,"内置 A 股交易规则": "with built-in A-share trading rules"
    ,"独立地再算一遍": "independently reimplemented"
    ,"提示": "Notes"
    ,"涨跌停、拒单等": "Price limits, rejected orders, etc."
    ,"从": "From"
    ,"太多了": "Too many"
    ,"和最佳值差得少 = ": "A small gap from the best value = "
    ,"，比较稳健；差得多 = 孤立的尖峰，多半是过拟合": ", more robust; a large gap = an isolated peak, likely overfitting"
    ,"符合实际": "matches real market prices"
    ,"让最后一天等于真实股价（不改变任何一天的涨跌幅，只让\"一手多少钱\"": "so the last day matches the real price. Daily returns stay unchanged; only the cash cost of a board lot"
    ,"样本外是挑完之后才看的": "out-of-sample is the unseen evaluation period"
    ,"挑出来的": "selected"
    ,"只会自动更新": "updates automatically"
    ,"中途停止或电脑重启后再点「开始」会接着算。": "If stopped or restarted, select Start to resume."
    ,"全部跑完大约 ": "A full run takes about "
    ,"（M 系列 Mac），期间 Mac 不会自动睡眠（合上屏幕仍会睡眠）": " on an Apple silicon Mac. The Mac stays awake during the run; closing the lid still sleeps it."
    ,"正在用 ": "Using "
    ,"把 ": " to run "
    ," 个策略都跑一遍": " strategies"
    ,"个历史版本": " versions"
    ,"写第一个": "Create the first one"
    ,"扫描过": "scans yet"
    ,"跑赢持有": "Beat buy-and-hold"
    ,"年化差": "CAGR difference"
    ,"刷新数据": "Refresh data"
    ,"请查看更新日志": "See the update log"
    ,"已复制": "Copied"
    ,"复制失败，长按/选中链接手动复制吧": "Could not copy. Select and copy the link manually."
    ,"（通常几秒到一分钟）": " (usually a few seconds to one minute)"
    ,"预计首批统计日期": "First results expected around"
    ,"北京时间": "Beijing time"
    ,"美东时间": "U.S. Eastern Time"
    ,"信号为零": "No active signal"
    ,"信号建议空仓": "the signal recommends staying in cash"
    ,"数据截至": "Prices through"
    ,"正在生成分享链接": "Creating a share link"
    ,"重新打开": "reopen it"
    ,"在终端运行": "In Terminal, run"
    ,"。在终端运行 ": ". In Terminal, run "
    ," 元": " CNY"
    ,"，让最后一天": ", so the last day"
    ,"不改变任何一天的涨跌幅，只让": "daily returns stay unchanged; only"
    ,"是挑完之后才看的": "is the unseen evaluation period"
    ,"有 ": "There are "
    ,"（和最佳值差得少 = ": " (A small gap from the best value = "
    ,"的 7 个策略都跑一遍": "'s 7 strategies"
    ,"每日": "daily"
    ,"（按工作日估算，遇市场假期顺延）": " (estimated using weekdays; market holidays may extend this date)"
    ,"趋势集成、RSI2、海龟三个子策略加权后的目标仓位为 0%，所以当前建议空仓。": "The Trend Ensemble, RSI(2), and Donchian sub-strategies have a combined target exposure of 0%, so cash is recommended."
    ,"趋势集成、RSI2、海龟各 1/3 仓位相加": "One-third exposure each in Trend Ensemble, RSI(2), and Donchian"
    ,"价格站上 ": "Price is above "
    ," 条均线（": " moving averages ("
    ," 日）；20 日波动率 ": " days); 20-day volatility "
    ,"，波动率系数 ": ", volatility scale "
    ,"收盘 ": "Close "
    ,"，前 20 日最高 ": ", prior 20-day high "
    ,"（差 ": " (difference "
    ,"%），前 10 日最低 ": "%), prior 10-day low "
    ,"RSI(2) = ": "RSI(2) = "
    ,"（低于 10 为超卖），价格不在 ": " (below 10 is oversold); price is below "
    ,"（低于 10 为超卖），价格在 ": " (below 10 is oversold); price is above "
    ," 日均线上方": "-day moving average"
    ,"一直满仓": "Fully invested throughout"
    ,"过去 12 个月：": "Past 12 months: "
    ,"（每月末调仓）": " (rebalanced at each month-end)"
    ,"数据不足": "Insufficient data"
    ,"今天有信号变化：": "Signal changes today: "
    ,"今天所有策略都没有信号变化。": "No strategy signals changed today."
    ,"价格站上全部 4 条均线，长期趋势向上。": "Price is above all four moving averages, indicating a long-term uptrend."
    ,"价格在 4 条均线全部下方，长期趋势向下，趋势策略空仓。": "Price is below all four moving averages, indicating a long-term downtrend; trend strategies are flat."
    ,"价格站上 ": "Price is above "
    ," 条均线，趋势不明朗，趋势策略部分仓位。": " moving averages; the trend is unclear and trend strategies hold partial exposure."
    ,"近 20 日年化波动率 ": "20-day annualized volatility "
    ,"%，高于 15% 的目标，趋势类策略仓位打 ": "%, above the 15% target; trend-strategy exposure is scaled by "
    ,"，处在上升趋势中的短期超卖，均值回归策略会买入。": ", a short-term oversold reading in an uptrend; the mean-reversion strategy would buy."
    ," 距离一年内高点回撤 ": " drawdown from its one-year high: "
    ,"上线日 ": "Launch date "
    ,"：从这天起的表现是真正的样本外，比任何历史回测都可信。": ": performance from this date onward is genuinely out-of-sample and more informative than historical backtests."
    ,"：趋势集成、RSI2、海龟": ": Trend Ensemble, RSI(2), and Donchian"
    ,"策略信号为零": "strategy signals are all zero"
    ,"趋势策略信号为零": "trend strategy signals are all zero"
    ,"目标仓位为 0%": "target exposure is 0%"
    ,"所以当前建议空仓。": "so cash is currently recommended."
    ,"，让最后一天等于真实股价": ", so the final day matches the real market price"
    ,"（不改变任何一天的涨跌幅，只让\"一手多少钱\"符合实际）。": " (daily returns are unchanged; only the cash value of a board lot is aligned with the market)."
    ,"，所以和上面的数字不同）": ", so it differs from the figures above)"
    ,"撮合：收盘出信号，次日开盘价成交（cheat-on-open，相当于集合竞价下单）": "Execution: signal at close, fill at the next open (cheat-on-open, like placing an opening-auction order)"
    ,"撮合：收盘出信号，次日开盘按目标仓位成交（src/portfolio.py）": "Execution: signal at close, fill at the next open to reach target exposure (src/portfolio.py)"
    ,"撮合：收盘出信号，次日集合竞价（open_auction）下单，RQAlpha 按开盘价撮合": "Execution: signal at close, place an open_auction order for the next session; RQAlpha fills at the opening price"
    ,"费用：佣金+过户费 万2.6（每笔最低 5 元），卖出印花税 2023-08-28 前 0.1%、之后 0.05%；无滑点": "Costs: 2.6 bp commission and transfer fee (CNY 5 minimum per order); sell-side stamp duty was 0.1% before 2023-08-28 and 0.05% afterward; no slippage"
    ,"整手：100 股一手，向下取整；资金不够时少买": "Board lots: 100 shares per lot, rounded down; buys are reduced when cash is insufficient"
    ,"T+1：日线下买入最早下一个交易日才能卖，天然满足": "T+1: daily-bar purchases can be sold no earlier than the next trading day"
    ,"费用：固定单边 ": "Costs: fixed one-way "
    ,"bp（含佣金、平摊的印花税和滑点估计），没有最低 5 元": " bp (commission, allocated stamp duty, and estimated slippage); no CNY 5 minimum"
    ,"按百分比持仓，不取整到 100 股": "Fractional exposure; positions are not rounded to 100-share lots"
    ,"空仓资金不计利息（和两个外部引擎一致）": "Cash earns no interest, consistent with the two external engines"
    ,"费用：RQAlpha 内置股票费用模型，佣金调成万2.6（含过户费，每笔最低 5 元），印花税按当时真实税率（pit_tax）；无滑点": "Costs: RQAlpha's built-in equity fee model uses 2.6 bp commission (including transfer fee, CNY 5 minimum per order) and the historical stamp-duty rate (pit_tax); no slippage"
    ,"整手、T+1、停牌不能交易、资金不足少买：全部由 RQAlpha 自己检查": "Board lots, T+1, suspensions, and insufficient cash are handled by RQAlpha"
    ,"数据：用自定义数据源 Mod 把本地数据接入，没有使用米筐的数据包": "Data: a custom data-source Mod loads local data; no Ricequant data package is used"
    ,"运行的是你自己写的策略：": "Running your custom strategy: "
    ,"涨跌停：开盘涨幅 ≥ ": "Price limits: do not buy if the opening gain is at least "
    ,"% 不买、跌幅 ≥ ": "%; do not sell if the opening loss is at least "
    ,"% 不卖，第二天再试": "%; retry the next day"
    ,"% 不卖": "%; do not sell"
    ,"% 不买": "%; do not buy"
    ,"% 不卖、": "%; do not sell; "
    ,"（不影响收益率，只影响整手取整）": " (does not affect returns, only board-lot rounding)"
    ,"价格按比例缩放": "prices are scaled proportionally"
    ,"订单未成交": "Order not filled"
    ,"开盘涨停，买不进": "Opening at limit up; buy not filled"
    ,"开盘跌停，卖不出": "Opening at limit down; sell not filled"
    ,"停牌": "Suspended"
    ,"每笔最低 ": "CNY "
    ,"相当于集合竞价下单": "like placing an opening-auction order"
    ,"（明日目标 ": " (next-session target "
    ,"自选股和主要 ETF 今天没有新信号。": "No new signals today for watchlist stocks or key ETFs."
    ,"全市场（组合策略）：买入 ": "Full market (portfolio strategy): "
    ," 只，卖出 ": " buys, "
    ," 只。": " sells."
    ,"量化日报 · ": "Quant Dashboard · "
    ,"美股代码，例如 AAPL": "U.S. ticker, e.g. AAPL"
    ,"访问令牌": "access token"
    ,"请查看更新日志": "See the update log"
    ,"正在查看历史版本": "Viewing a historical version"
    ,"登录已失效：在终端运行 bash scripts/open_dashboard.sh 重新打开": "Session expired. In Terminal, run bash scripts/open_dashboard.sh to reopen the dashboard."
    ,"数据未就绪": "Data is not ready"
    ,"当前没有可用": "No data is currently available",
    "正在下载：": "Downloading: ",
    "计算中": "Calculating",
    "下载中，稍等": "Downloading, please wait",
    "好了会自动出现": "will appear automatically when ready",
    "移除": "Remove",
    "移出": "Remove",
    "加入": "Add",
    "单边成本": "One-way cost",
    "明日开盘目标": "Next-open target",
    "持仓时间": "Holding period",
    "价格涨跌": "Price change",
    "账户收益": "Account return",
    "没有可调参数": "No adjustable parameters",
    "近1周": "1 week",
    "近3月": "3 months",
    "近1年": "1 year",
    "待数据更新": "Waiting for data update",
    "等待样本": "Waiting for samples",
    "跑赢大盘": "Beat benchmark",
    "至今比大盘": "Excess vs benchmark to date",
    "至今": "To date",
    "请查看更新日志": "See the update log",
    "刷新数据": "Refresh data",
    "已复制": "Copied",
    "正在生成分享链接": "Generating share link",
    "自定义": "Custom",
    "期末资产": "Ending equity",
    "手续费": "Fees",
    "用时": "Elapsed",
    "没有被拒的订单": "No rejected orders",
    "日期": "Date",
    "方向": "Side",
    "股数": "Shares",
    "参数优化": "Parameter optimization",
    "参数": "Parameters",
    "查看": "View",
    "修改": "Edit",
    "步长": "Step",
    "最小": "Min",
    "不填": "Leave blank",
    "不选": "None",
    "组合太少，没法算": "Too few combinations to compute",
    "运行中": "Running",
    "当前版本": "Current version",
    "自检结果": "Self-check result",
    "自研": "In-house",
    "结论一致": "Same conclusion",
    "已统计": "Counted",
    "次数": "Count",
    "明日仓位": "Next-day exposure",
    "市场概览": "Market overview",
    "暂无基准行情": "No benchmark data yet",
    "成交量": "Volume",
    "重新打开": "Reopen",
    "名称": "Name",
    "持续性": "Persistence",
    "怎么看这张表：": "How to read this table: ",
    "没有找到引擎库": "Engine library not found",
    "提示": "Notes",
    "个股": "Stocks",
    "判断依据": "Why this position",
    "价格相对 MA300": "Price vs MA300",
    "组合仓位是三个子策略仓位的加权平均；三个子策略全部空仓时，组合就是空仓。": "The portfolio exposure is the weighted average of the three sub-strategies; when all three are in cash, the portfolio is in cash."
    ,"明天开盘，组合策略建议": "Next open: the portfolio strategy suggests"
    ,"近 20 日年化波动率": "20-day annualized volatility"
    ,"，高于 15% 的目标，趋势类策略仓位打": ", above the 15% target; trend strategies scale their position to"
    ,"处在上升趋势中的短期超卖，均值回归策略会买入。": "a short-term oversold reading inside an uptrend; the mean-reversion strategy will buy."
    ,"距离一年内高点回撤": "is below its 1-year high by"
    ,"上线日": "Launch date"
    ,"当前显示：历史回放": "Showing: historical replay"
    ,"。用现在的规则把过去 3 年每天会发出的信号重算了一遍，再按同样的规则计算 5、20、60 天后的收益。它不是当时记录下来的信号，规则是事后定的，结果会偏乐观，只能当参考。实盘记录已有 ": ". Signals were recomputed with today's rules for every day of the past 3 years, then returns after 5, 20 and 60 days were calculated the same way. These are not signals recorded at the time; the rules were set after the fact, so results are optimistic and for reference only. Live records so far: "
    ," 条，等累计到 30 个以上的 20 天样本，这里会自动改成实盘数据。": " signals. Once there are 30 or more 20-day samples, this view switches to live data automatically."
    ,"历史回放：": "Historical replay: "
    ," 条信号 · ": " signals · "
    ," 至 ": " to "
    ,"· 实盘记录 ": "· live records "
    ," 条，首批 5 天统计预计 ": " signals; first 5-day stats expected "
    ,"平均": "Avg"
    ,"下载离线版（发给家人）": "Download offline copy (to share with family)"
    ,"单个网页文件，对方用浏览器双击就能打开，不用联网；数据停在下载这一刻，不会自动更新。": "A single web file the recipient can open by double-clicking in a browser, no internet needed. The data is frozen at the moment of download and does not update."
    ,"正在生成…": "Generating…"
    ,"第一次生成大约需要几十秒，请稍等。": "The first build takes about half a minute, please wait."
    ,"已下载。把这个文件发给对方即可。": "Downloaded. Send this file to the recipient."
    ,"下载失败：": "Download failed: "
  };
  const pairs = Object.entries(dictionary).sort((a, b) => b[0].length - a[0].length);
  const translationPattern = new RegExp(pairs.map(([zh]) => zh.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|"), "g");
  const originals = new WeakMap();
  const originalAttrs = new WeakMap();
  const renderedText = new WeakMap();
  const renderedAttrs = new WeakMap();
  let language = "en";
  try { language = localStorage.getItem("dashboard-language-v2") || "en"; } catch {}
  window.AI_I18N = {
    get language() { return language === "en" ? "en" : "zh"; },
    has(key) { return Object.hasOwn(aiTranslations, key); },
    text(key, values = {}, lang = language) {
      const pair = aiTranslations[key];
      const template = pair ? pair[lang === "en" ? 1 : 0] : key;
      return template.replace(/\{(idea|config|field)\}/g, (_, name) => String(values[name] ?? ""));
    }
  };
  // 持仓页文案：键成对保存 [中文, 英文]，由 app.js 的 hText 取用；占位符 {name} 只做文本替换。
  const holdingsTranslations = {
    "hold.title": ["持仓", "Holdings"],
    "hold.note": ["仅记录，不连接券商、不下单；数据只存在本机 data/holdings.db。", "Record-keeping only: no broker connection, no orders; data stays in data/holdings.db on this machine."],
    "hold.loading": ["读取中…", "Loading…"],
    "hold.retry": ["重试", "Retry"],
    "hold.monitor": ["盯盘盈亏：尚未接入。", "Live-monitor P&L: not connected yet."],
    "hold.sum.title": ["汇总", "Summary"],
    "hold.sum.capital": ["总资金", "Total capital"],
    "hold.sum.capital_unset": ["未设置", "Not set"],
    "hold.sum.capital_hint": ["总资金 = 账户总资产（含现金），手动填写，不会自动调整，只用来算占比。", "Total capital = total account assets including cash. Entered by hand, never adjusted automatically; used only for weights."],
    "hold.sum.edit": ["修改", "Edit"],
    "hold.sum.save": ["保存", "Save"],
    "hold.sum.cancel": ["取消", "Cancel"],
    "hold.sum.capital_label": ["总资金（元）", "Total capital (CNY)"],
    "hold.sum.market_value": ["持仓市值", "Market value"],
    "hold.sum.invested": ["已投入占比", "Invested share"],
    "hold.sum.floating": ["浮动盈亏", "Unrealized P&L"],
    "hold.sum.realized": ["已实现盈亏", "Realized P&L"],
    "hold.sum.dividends": ["现金分红", "Cash dividends"],
    "hold.sum.missing": ["{n} 只持仓价格不可用，市值与占比未计入。", "{n} position(s) have no price; their value and weight are excluded."],
    "hold.pos.title": ["当前持仓", "Current positions"],
    "hold.pos.empty": ["还没有持仓。在下方录入第一笔成交。", "No positions yet. Record your first trade below."],
    "hold.pos.code": ["代码", "Code"],
    "hold.pos.name": ["名称", "Name"],
    "hold.pos.shares": ["数量（股）", "Shares"],
    "hold.pos.avg": ["平均成本", "Avg cost"],
    "hold.pos.price": ["现价", "Price"],
    "hold.pos.pnl": ["浮动盈亏", "Unrealized P&L"],
    "hold.pos.weight": ["占总资金", "Weight"],
    "hold.pos.first": ["首次买入日", "First bought"],
    "hold.pos.days": ["持有天数", "Days held"],
    "hold.pos.over": ["超 10%", "Over 10%"],
    "hold.pos.noprice": ["价格不可用", "No price"],
    "hold.pos.stale": ["价格偏旧", "Stale price"],
    "hold.pos.foot": ["浮动盈亏未扣预估卖出费用；现价取自本地日线收盘价，不是实时价；占比超过 10% 标红（仅展示，不限制录入）。", "Unrealized P&L excludes estimated selling costs; prices are local daily closes, not real-time; weights above 10% are shown in red (display only, entries are not blocked)."],
    "hold.form.title": ["录入成交", "Record a trade"],
    "hold.form.date": ["日期（北京时间）", "Date (Beijing)"],
    "hold.form.code": ["代码（6 位）", "Code (6 digits)"],
    "hold.form.side": ["方向", "Side"],
    "hold.form.qty": ["数量（股）", "Quantity (shares)"],
    "hold.form.qty_div": ["登记日持股数", "Shares at record date"],
    "hold.form.qty_bonus": ["送转新增股数", "New bonus shares"],
    "hold.form.price": ["价格（元）", "Price (CNY)"],
    "hold.form.price_div": ["每股税后现金（元）", "After-tax cash per share (CNY)"],
    "hold.form.fee": ["费用（元，可留空）", "Fees (CNY, optional)"],
    "hold.form.fee_hint": ["留空将按费率估算（估算值，建议填券商账单上的真实费用）。", "Leave blank to estimate from fee rates (an estimate; prefer the real fee on your broker statement)."],
    "hold.form.note": ["备注", "Note"],
    "hold.form.submit": ["提交", "Submit"],
    "hold.form.name_unknown": ["未找到名称（仍可录入）", "Name not found (you can still record it)"],
    "hold.form.incomplete": ["请填写 6 位代码、数量，以及（送转以外的）价格。", "Enter a 6-digit code, a quantity and (except for bonus shares) a price."],
    "hold.form.confirm_title": ["请确认这笔成交", "Please confirm this entry"],
    "hold.form.confirm_text": ["{side} {code} {name} {qty} 股 @{price} 元，费用 {fee}，日期 {date}", "{side} {code} {name} {qty} shares @ {price} CNY, fees {fee}, date {date}"],
    "hold.form.confirm_text_bonus": ["{side} {code} {name} 新增 {qty} 股，日期 {date}", "{side} {code} {name} +{qty} shares, date {date}"],
    "hold.form.fee_auto": ["按费率估算", "estimated from rates"],
    "hold.form.fee_value": ["{fee} 元", "{fee} CNY"],
    "hold.form.confirm_ok": ["确认入账", "Confirm entry"],
    "hold.form.back": ["返回修改", "Back to edit"],
    "hold.form.saving": ["提交中…", "Submitting…"],
    "hold.form.saved": ["已入账。", "Entry recorded."],
    "hold.form.dup": ["相同的提交已存在，没有重复入账。", "This submission already exists; it was not recorded twice."],
    "hold.side.buy": ["买入", "Buy"],
    "hold.side.sell": ["卖出", "Sell"],
    "hold.side.bonus": ["送转", "Bonus shares"],
    "hold.side.dividend": ["分红", "Dividend"],
    "hold.hist.title": ["成交历史", "Trade history"],
    "hold.hist.empty": ["暂无成交记录。", "No trades yet."],
    "hold.hist.show_voided": ["显示已撤销", "Show voided"],
    "hold.hist.date": ["日期", "Date"],
    "hold.hist.side": ["方向", "Side"],
    "hold.hist.stock": ["股票", "Stock"],
    "hold.hist.qty": ["数量", "Qty"],
    "hold.hist.price": ["价格", "Price"],
    "hold.hist.fee": ["费用", "Fees"],
    "hold.hist.note": ["备注", "Note"],
    "hold.hist.est": ["估算", "est."],
    "hold.hist.voided": ["已撤销", "Voided"],
    "hold.hist.void": ["撤销", "Void"],
    "hold.hist.void_ask": ["确认撤销这笔成交？撤销后持仓会按剩余记录整体重算。", "Void this trade? Positions will be recomputed from the remaining records."],
    "hold.hist.void_ok": ["确认撤销", "Confirm void"],
    "hold.hist.void_cancel": ["取消", "Cancel"],
    "hold.hist.void_done": ["已撤销。", "Trade voided."],
    "hold.closed.title": ["已清仓历史", "Closed positions"],
    "hold.closed.empty": ["暂无已清仓记录。", "No closed positions."],
    "hold.closed.last": ["最后卖出日", "Last sold"],
    "hold.closed.pnl": ["盈亏", "P&L"],
    "hold.capital_saved": ["总资金已保存。", "Total capital saved."],
    "hold.err.AUTH": ["登录已失效：在终端运行 bash scripts/open_dashboard.sh 重新打开。", "Session expired: run bash scripts/open_dashboard.sh in a terminal to reopen."],
    "hold.err.UNAVAILABLE": ["持仓功能不可用（仅个人模式、仅本机访问）。", "Holdings is unavailable (personal mode and this machine only)."],
    "hold.err.NETWORK": ["网络错误，请稍后重试。", "Network error; please try again."],
    "hold.err.generic": ["操作失败，请检查输入后重试。", "The operation failed; check your input and try again."],
    "hold.err.HOLDINGS_FORBIDDEN": ["请求被拒绝。", "Request refused."],
    "hold.err.HOLDINGS_BAD_JSON": ["请求格式不正确。", "Malformed request."],
    "hold.err.HOLDINGS_UNKNOWN_FIELD": ["请求包含不支持的字段。", "The request contains an unsupported field."],
    "hold.err.HOLDINGS_MISSING_FIELD": ["缺少必填字段。", "A required field is missing."],
    "hold.err.HOLDINGS_BAD_CODE": ["代码无效：只支持沪深 A 股和沪深 ETF 的 6 位代码。", "Invalid code: only 6-digit Shanghai/Shenzhen stock and ETF codes are supported."],
    "hold.err.HOLDINGS_BAD_SIDE": ["方向无效。", "Invalid side."],
    "hold.err.HOLDINGS_BAD_DATE": ["日期无效。", "Invalid date."],
    "hold.err.HOLDINGS_FUTURE_DATE": ["日期不能晚于北京时间今天。", "The date cannot be later than today in Beijing."],
    "hold.err.HOLDINGS_BAD_QTY": ["数量无效：须为正整数。", "Invalid quantity: it must be a positive integer."],
    "hold.err.HOLDINGS_BAD_LOT": ["买入数量不符合整手规则（普通 100 股整数倍；科创板至少 200 股）。", "The buy quantity breaks the lot rule (multiples of 100; at least 200 on STAR Market)."],
    "hold.err.HOLDINGS_BAD_PRICE": ["价格无效。", "Invalid price."],
    "hold.err.HOLDINGS_BAD_FEE": ["费用无效。", "Invalid fees."],
    "hold.err.HOLDINGS_BAD_NOTE": ["备注无效（最多 200 字）。", "Invalid note (200 characters at most)."],
    "hold.err.HOLDINGS_BAD_SIGNAL_REF": ["来源信号格式无效。", "Invalid signal reference."],
    "hold.err.HOLDINGS_BAD_CLIENT_ID": ["提交标识无效，请刷新页面重试。", "Invalid submission id; reload the page and retry."],
    "hold.err.HOLDINGS_BAD_CAPITAL": ["总资金无效：须大于 0，最多 2 位小数。", "Invalid total capital: it must be positive with at most 2 decimals."],
    "hold.err.HOLDINGS_BAD_ID": ["记录编号无效。", "Invalid record id."],
    "hold.err.HOLDINGS_OVERSELL": ["卖出数量超过当时的持股数。", "The sell quantity exceeds the shares held at that time."],
    "hold.err.HOLDINGS_NO_POSITION": ["送转或分红需要对应的持股。", "A bonus or dividend entry needs a matching position."],
    "hold.err.HOLDINGS_VOID_BREAKS_HISTORY": ["撤销这笔会让之后的记录不合法，请先撤销后面的卖出。", "Voiding this would invalidate later records; void the later sells first."],
    "hold.err.HOLDINGS_ALREADY_VOIDED": ["这笔已经撤销过了。", "This trade is already voided."],
    "hold.err.HOLDINGS_TRADE_NOT_FOUND": ["找不到这笔记录。", "Trade not found."],
    "hold.err.HOLDINGS_CLIENT_ID_CONFLICT": ["提交标识冲突，请刷新页面重试。", "Submission id conflict; reload the page and retry."],
    "hold.err.HOLDINGS_TOO_LARGE": ["请求体过大。", "Request too large."],
    "hold.err.HOLDINGS_RATE_LIMITED": ["操作过于频繁，请稍后再试。", "Too many requests; please wait a moment."],
    "hold.err.HOLDINGS_STORE_UNSAFE": ["持仓文件不安全（符号链接或非普通文件），已拒绝读写。", "The holdings file is unsafe (symlink or not a regular file); access refused."],
    "hold.err.HOLDINGS_INTERNAL": ["服务内部错误。", "Internal error."]
  };
  window.HOLDINGS_I18N = {
    has(key) { return Object.hasOwn(holdingsTranslations, key); },
    keys() { return Object.keys(holdingsTranslations); },
    text(key, values = {}, lang = language) {
      const pair = holdingsTranslations[key];
      const template = pair ? pair[lang === "en" ? 1 : 0] : key;
      return template.replace(/\{(\w+)\}/g, (_, name) => String(values[name] ?? ""));
    }
  };
  // 盯盘页文案（T006 S3）：键成对 [中文, 英文]，由 app.js 的 mText 取用；占位符只做文本替换。
  const monitorTranslations = {
    "mon.title": ["实时盯盘", "Live Monitor"],
    "mon.note": ["监控范围 = 我的持仓 + 自选 + 今日买卖信号前 N 只（上限 60 只）。价格来自腾讯公开接口，无许可、延迟与准确性未验证，仅限个人少量股票；只在 A 股连续竞价时段采集，收盘自动停止；默认关闭，重启后需手动开启。不构成投资建议。", "Scope = my holdings + watchlist + top N of today's signals (max 60). Prices come from an unlicensed public Tencent endpoint with unverified delay and accuracy; personal, small-scale use only. Polling runs during A-share continuous trading only and stops after the close; off by default and must be started manually after a restart. Not investment advice."],
    "mon.ctl.title": ["运行状态", "Status"],
    "mon.state.stopped": ["已停止", "Stopped"],
    "mon.state.running": ["运行中", "Running"],
    "mon.state.paused": ["已暂停", "Paused"],
    "mon.phase.idle": ["空闲", "Idle"],
    "mon.phase.waiting_session": ["等待开盘时段（北京时间 9:30–11:30、13:00–15:00）", "Waiting for trading hours (Beijing 9:30–11:30, 13:00–15:00)"],
    "mon.phase.polling": ["采集中", "Polling"],
    "mon.status.line": ["状态：{state} · {phase}", "State: {state} · {phase}"],
    "mon.status.stats": ["已请求 {used} / 预算 {budget} 次 · 监控 {count} 只（上限 {max}） · 已完成 {rounds} 轮", "{used} / {budget} requests used · {count} stocks watched (max {max}) · {rounds} rounds done"],
    "mon.status.truncated": ["名单超过上限，已截断（持仓优先，其次自选，最后信号）。", "The list exceeded the limit and was truncated (holdings first, then watchlist, then signals)."],
    "mon.reason.line": ["暂停原因：{reason}", "Pause reason: {reason}"],
    "mon.reason.budget_exhausted": ["本次启动的请求预算已用完，需手动重新启动", "The request budget for this run is used up; start again manually"],
    "mon.reason.storage_error": ["存储异常", "Storage error"],
    "mon.reason.service_error": ["服务内部异常", "Internal service error"],
    "mon.reason.quality_error": ["连续三次数据质量异常", "Three consecutive data-quality failures"],
    "mon.reason.timeout": ["连续三次请求超时", "Three consecutive request timeouts"],
    "mon.reason.network_error": ["连续三次网络错误", "Three consecutive network errors"],
    "mon.reason.worker_exit": ["连续三次采集进程异常退出", "Three consecutive worker exits"],
    "mon.reason.http_status": ["连续三次上游返回非 200", "Three consecutive non-200 upstream responses"],
    "mon.reason.redirect_refused": ["上游要求跳转，已拒绝", "Upstream redirect refused"],
    "mon.reason.invalid_record": ["连续三次返回记录无效", "Three consecutive invalid records"],
    "mon.reason.provider_error": ["连续三次采集失败", "Three consecutive fetch failures"],
    "mon.btn.start": ["开始盯盘", "Start monitoring"],
    "mon.btn.stop": ["停止盯盘", "Stop monitoring"],
    "mon.cfg.title": ["配置", "Settings"],
    "mon.cfg.interval": ["采样间隔（秒，逐只模式 30–3600，批量模式 10–3600）", "Sampling interval (seconds; 30–3600 single, 10–3600 batch)"],
    "mon.cfg.top_n": ["信号前 N 只（0–名单上限）", "Top N signals (0 to the watch limit)"],
    "mon.cfg.provider": ["数据源", "Data source"],
    "mon.cfg.provider.tencent": ["tencent（腾讯逐只，兜底）", "tencent (Tencent, one by one; fallback)"],
    "mon.cfg.provider.easy": ["easy（easyquotation 批量）", "easy (easyquotation, batch)"],
    "mon.cfg.max_watch": ["名单上限 max_watch（1–5500）", "Watch limit max_watch (1–5500)"],
    "mon.cfg.batch_mode": ["批量模式（与数据源联动）", "Batch mode (linked to data source)"],
    "mon.cfg.hint_provider": ["建议先把名单上限设为 10 只试跑，确认稳定再放开；批量模式依赖本机已安装 easyquotation，未安装时会自动退回 tencent。出问题时把数据源改回 tencent 即可回滚。", "Start with a watch limit of 10 and raise it only once it is stable. Batch mode needs easyquotation installed locally and falls back to tencent otherwise. If anything goes wrong, set the data source back to tencent to roll back."],
    "mon.cfg.save": ["保存配置", "Save settings"],
    "mon.cfg.hint": ["每只股票最短 60 秒才会再请求一次、相邻请求至少隔 10 秒，不受此处间隔影响；修改在下一轮生效。", "Each stock is requested at most once per 60 s and requests are at least 10 s apart regardless of this interval; changes apply from the next round."],
    "mon.cfg.err_easy": ["切换到 easy 失败：最常见的原因是本机没有安装 easyquotation（也可能是其他参数不合法）。数据源保持不变。", "Switching to easy failed: most often easyquotation is not installed on this machine (or another value is invalid). The data source is unchanged."],
    "mon.status.degraded": ["批量模式连续失败，已自动降级为 tencent 逐只模式。要回滚请选 tencent 并保存；要重试 easy 请先停止再重新开始。", "Batch mode failed repeatedly and fell back to tencent one-by-one mode. To roll back, choose tencent and save; to retry easy, stop and start again."],
    "mon.status.easy_missing": ["已请求批量模式，但本机没有安装 easyquotation，当前实际在用 tencent。", "Batch mode was requested but easyquotation is not installed, so tencent is in use."],
    "mon.cfg.saved": ["配置已保存。", "Settings saved."],
    "mon.q.title": ["监控股票", "Watched stocks"],
    "mon.q.empty": ["暂无监控股票（持仓、自选和信号名单都为空，或尚未开始）。", "No watched stocks (holdings, watchlist and signal lists are empty, or monitoring has not started)."],
    "mon.q.time_note": ["时间均为北京时间；价格是盘中采样价，不是收盘价，也不会用来重算策略信号。", "All times are Beijing time. Prices are intraday samples, not closing prices, and are never used to recompute strategy signals."],
    "mon.col.code": ["代码", "Code"],
    "mon.col.name": ["名称", "Name"],
    "mon.col.price": ["现价", "Price"],
    "mon.col.pct": ["涨跌", "Change"],
    "mon.col.time": ["采集时间", "Collected at"],
    "mon.col.alert": ["告警状态", "Alert"],
    "mon.col.source": ["来源", "Source"],
    "mon.col.quality": ["数据质量", "Quality"],
    "mon.src.holding": ["持仓", "Holding"],
    "mon.src.watchlist": ["自选", "Watchlist"],
    "mon.src.signal": ["信号", "Signal"],
    "mon.quality.valid": ["正常", "OK"],
    "mon.quality.stale": ["数据过旧", "Stale"],
    "mon.quality.invalid": ["数据异常", "Invalid"],
    "mon.quality.off_session": ["非交易时段", "Off session"],
    "mon.quality.pending": ["等待采集", "Pending"],
    "mon.alert.none": ["无目标价", "No target"],
    "mon.alert.armed": ["已布防", "Armed"],
    "mon.alert.triggered": ["已触发", "Triggered"],
    "mon.a.title": ["告警历史", "Alert history"],
    "mon.a.empty": ["暂无告警。", "No alerts."],
    "mon.a.col.time": ["触发时间", "Triggered at"],
    "mon.a.col.code": ["代码", "Code"],
    "mon.a.col.dir": ["方向", "Direction"],
    "mon.a.col.threshold": ["目标价", "Target"],
    "mon.a.col.price": ["前价 → 现价", "Previous → current"],
    "mon.dir.down": ["跌破买入目标价", "Fell through buy target"],
    "mon.dir.up": ["突破卖出目标价", "Rose through sell target"],
    "mon.ia.title": ["实时分析", "Live analysis"],
    "mon.ia.note": ["约每分钟更新一次；盘中价格不是收盘价，以下全部仅参考，不构成投资建议。", "Updated about once a minute; intraday prices are not closing prices. Everything below is for reference only, not investment advice."],
    "mon.ia.exec.title": ["执行提醒", "Execution hints"],
    "mon.ia.exec.empty": ["暂无执行提醒。", "No execution hints."],
    "mon.ia.track.title": ["信号追踪", "Signal tracking"],
    "mon.ia.track.empty": ["今日清单暂无可追踪的信号（或尚无报价）。", "No trackable signals in today's list yet (or no quotes)."],
    "mon.ia.opp.title": ["新机会", "New opportunities"],
    "mon.ia.opp.empty": ["暂无新信号。", "No new signals."],
    "mon.ia.summary": ["实时正确率：{total} 条买入信号，赢 {winners}、亏 {losers}，平均 {mean}%，中位数 {median}%（赢 = 现价高于开盘价；样本很小，仅供观察）", "Live hit rate: {total} buy signals, {winners} winners, {losers} losers, mean {mean}%, median {median}% (winner = price above the open; tiny sample, observation only)"],
    "mon.ia.summary_empty": ["实时正确率：暂无买入信号可统计。", "Live hit rate: no buy signals to count yet."],
    "mon.ia.write_error": ["追踪记录写入失败（不影响盯盘）。", "Writing the tracking log failed (monitoring is unaffected)."],
    "mon.ia.col.code": ["代码", "Code"],
    "mon.ia.col.action": ["动作", "Action"],
    "mon.ia.col.open": ["开盘价", "Open"],
    "mon.ia.col.price": ["现价", "Price"],
    "mon.ia.col.chg": ["相对开盘%", "vs open %"],
    "mon.ia.col.cum": ["累计收益%", "Return %"],
    "mon.ia.col.up": ["距涨停%", "To limit-up %"],
    "mon.ia.col.down": ["距跌停%", "To limit-down %"],
    "mon.ia.act.buy": ["买入", "Buy"],
    "mon.ia.act.sell": ["卖出", "Sell"],
    "mon.ia.act.add": ["加仓", "Add"],
    "mon.ia.act.trim": ["减仓", "Trim"],
    "mon.ev.buy_window": ["{code} 买入窗口打开（开盘后 {minutes} 分钟内）", "{code} buy window open (first {minutes} minutes after the open)"],
    "mon.ev.limit_up_no_buy": ["{code} 涨停不可买，追不进", "{code} is limit-up: cannot buy, do not chase"],
    "mon.ev.limit_down_no_sell": ["{code} 跌停不可卖", "{code} is limit-down: cannot sell"],
    "mon.ev.holding_drop": ["{code} 持仓异常波动，当日跌 {pct}%，考虑止损", "{code} holding fell {pct}% today; consider a stop-loss"],
    "mon.ev.watch": ["{code} 关注：{triggers}", "{code} on watch: {triggers}"],
    "mon.ev.consider_sell": ["{code} 持仓出现负向信号：{triggers}，考虑卖出", "{code} holding shows a negative signal: {triggers}; consider selling"],
    "mon.ev.trig.momentum": ["动量", "momentum"],
    "mon.ev.trig.volume": ["放量", "volume surge"],
    "mon.ev.trig.breakout": ["突破", "breakout"],
    "mon.ev.since_trigger": ["自触发 {ret}", "since trigger {ret}"],
    "mon.ev.note": ["仅参考，未经分钟级回测", "reference only, not minute-level backtested"],
    "mon.loading": ["加载中…", "Loading…"],
    "mon.err.generic": ["操作失败，请稍后重试。", "The operation failed; please try again."],
    "mon.err.FORBIDDEN": ["请求被拒绝（仅个人模式、仅本机、需登录）。", "Request refused (personal mode, this machine and login only)."],
    "mon.err.AUTH": ["未登录，请从终端脚本重新打开网页。", "Not signed in; reopen the page from the terminal script."],
    "mon.err.UNAVAILABLE": ["盯盘功能不可用（仅个人模式）。", "Monitoring is unavailable (personal mode only)."],
    "mon.err.NETWORK": ["网络错误，请稍后重试。", "Network error; please try again."],
    "mon.err.INVALID_INPUT": ["输入无效：请检查间隔（逐只 30–3600、批量 10–3600）、前 N 只（0–名单上限）、名单上限（1–5500）和数据源（tencent/easy）。", "Invalid input: check interval (30–3600 single, 10–3600 batch), top N (0 to the watch limit), watch limit (1–5500) and data source (tencent/easy)."],
    "mon.err.RATE_LIMITED": ["操作过于频繁，请稍后再试。", "Too many requests; please wait a moment."],
    "mon.err.MONITOR_INTERNAL": ["服务内部错误。", "Internal error."],
    "usmon.title": ["美股盯盘", "US Live Monitor"],
    "usmon.note": ["监控范围 = 美股自选（上限 30 只；美股持仓与买卖信号第一版为空）。价格来自 Yahoo 的非官方公开接口（yfinance），无行情许可，延迟与准确性未验证，仅限个人少量自用；每只约 5 分钟更新一次；只在美东常规时段（周一至周五 09:30–16:00，夏令时对应北京 21:30–04:00，冬令时 22:30–05:00）采集，收盘自动停止；默认关闭，重启后需手动开启。休市日没有日历，靠行情过旧判断。不构成投资建议。", "Scope = US watchlist (max 30; US holdings and signals are empty in this first version). Prices come from Yahoo's unofficial public endpoint via yfinance, with no market-data license and unverified delay and accuracy; personal, small-scale use only; each stock refreshes about every 5 minutes. Polling runs during the US regular session only (Mon-Fri 09:30-16:00 Eastern, i.e. 21:30-04:00 Beijing in summer time and 22:30-05:00 in winter time) and stops after the close; off by default and must be started manually after a restart. There is no holiday calendar; stale quotes are the only holiday signal. Not investment advice."],
    "usmon.ia.note": ["约每个采集轮次更新一次；盘中价格不是收盘价，以下全部仅参考，不构成投资建议。美股没有涨跌停，也没有 T+1 限制。", "Updated once per polling round; intraday prices are not closing prices. Everything below is for reference only, not investment advice. US stocks have no daily price limits and no T+1 rule."],
    "usmon.ia.exec.empty": ["暂无执行提醒（美股暂无信号来源；持仓为空时不会出现持仓异常波动）。", "No execution hints (no US signal source yet; holding-drop hints need holdings)."],
    "usmon.ia.opp.empty": ["暂无新信号。", "No new signals."],
    "usmon.ia.track.empty": ["暂无美股信号可追踪。", "No US signals to track yet."],
    "usmon.ia.col.high": ["距当日高点%", "To day high %"],
    "usmon.ia.col.low": ["距当日低点%", "To day low %"],
    "usmon.ev.display_only": ["流动性不足（价格过低或成交额偏小），仅展示", "low liquidity (low price or small turnover), display only"],
    "usmon.cfg.interval": ["采样轮次间隔（秒，60–3600）", "Round interval (seconds, 60–3600)"],
    "usmon.cfg.top_n": ["信号前 N 只（0–30）", "Top N signals (0–30)"],
    "usmon.cfg.hint": ["每只股票最短 300 秒才会再请求一次、相邻请求至少隔 10 秒，不受此处间隔影响；连续失败会自动拉长等待，限流后冷却 10 分钟；修改在下一轮生效。", "Each stock is requested at most once per 300 s and requests are at least 10 s apart regardless of this interval; consecutive failures lengthen the wait automatically and rate limiting triggers a 10-minute cooldown; changes apply from the next round."],
    "usmon.q.time_note": ["时间均为美东时间（夏令时自动切换）；价格是盘中采样价，不是收盘价，也不会用来重算策略信号。", "All times are US Eastern time (daylight saving handled automatically). Prices are intraday samples, not closing prices, and are never used to recompute strategy signals."],
    "usmon.q.empty": ["暂无监控股票：美股自选里没有有效代码（持仓与信号第一版为空），或尚未开始。这是正常状态，不是故障。", "No watched stocks: the US watchlist has no valid symbols (holdings and signals are empty in this version), or monitoring has not started. This is normal, not a fault."],
    "usmon.phase.idle": ["空闲", "Idle"],
    "usmon.phase.waiting_session": ["等待开盘时段（美东 09:30–16:00，周一至周五）", "Waiting for trading hours (US Eastern 09:30–16:00, Mon–Fri)"],
    "usmon.phase.polling": ["采集中", "Polling"],
    "usmon.phase.cooling_down": ["冷却等待中（失败或限流后暂缓请求）", "Cooling down (requests deferred after a failure or rate limit)"],
    "usmon.reason.budget_exhausted": ["本次启动的请求预算已用完，需手动重新启动", "The request budget for this run is used up; start again manually"],
    "usmon.reason.storage_error": ["存储异常", "Storage error"],
    "usmon.reason.service_error": ["服务内部异常", "Internal service error"],
    "usmon.reason.quality_error": ["连续三次数据质量异常（可能是休市日或行情过旧）", "Three consecutive data-quality failures (possibly a market holiday or stale quotes)"],
    "usmon.reason.timeout": ["连续三次请求超时", "Three consecutive request timeouts"],
    "usmon.reason.rate_limited": ["上游限流（429），已暂停以免加重限流", "Upstream rate limit (429); paused to avoid making it worse"],
    "usmon.reason.network": ["连续三次网络错误", "Three consecutive network errors"],
    "usmon.reason.worker_exit": ["连续三次采集进程异常退出", "Three consecutive worker exits"],
    "usmon.reason.empty": ["连续三次返回空数据", "Three consecutive empty responses"],
    "usmon.reason.symbol_missing": ["连续三次上游判定代码无数据", "Three consecutive no-data answers for the symbol"],
    "usmon.reason.bad_response": ["连续三次返回格式异常", "Three consecutive malformed responses"],
    "usmon.reason.invalid_record": ["连续三次返回记录无效", "Three consecutive invalid records"],
    "usmon.reason.provider_error": ["连续三次采集失败", "Three consecutive fetch failures"],
    "usmon.reason.yfinance_not_installed": ["未安装 yfinance，无法采集（pip install yfinance 后重新开始）", "yfinance is not installed; cannot poll (install it and start again)"],
    "usmon.quality.valid": ["正常", "OK"],
    "usmon.quality.stale": ["数据过旧", "Stale"],
    "usmon.quality.invalid": ["数据异常", "Invalid"],
    "usmon.quality.off_session": ["非交易时段", "Off session"],
    "usmon.quality.after_hours": ["盘前/盘后数据", "Pre/after-hours data"],
    "usmon.quality.pending": ["等待采集", "Pending"],
    "usmon.err.INVALID_INPUT": ["输入无效：间隔 60–3600 的整数，前 N 只 0–30 的整数。", "Invalid input: interval must be an integer 60–3600 and top N an integer 0–30."]
  };
  window.MONITOR_I18N = {
    has(key) { return Object.hasOwn(monitorTranslations, key); },
    keys() { return Object.keys(monitorTranslations); },
    text(key, values = {}, lang = language) {
      const pair = monitorTranslations[key];
      const template = pair ? pair[lang === "en" ? 1 : 0] : key;
      return template.replace(/\{(\w+)\}/g, (_, name) => String(values[name] ?? ""));
    }
  };
  const personalNavLabels = { "今日": "Today", "信号": "Signals", "全市场": "Market", "自选": "Watchlist", "回测": "Backtest", "持仓": "Holdings", "盯盘": "Monitor", "美股盯盘": "US Monitor" };
  const sourceTitle = document.title;

  function translate(value) {
    const text = String(value);
    const out = text.replace(translationPattern, (zh) => dictionary[zh]);
    if (out === text) return out;
    // 翻译过的句子里残留的全角标点换成英文标点，避免 "（Stay in cash）。" 这类中英混排
    return out.replace(/，/g, ", ").replace(/。/g, ".").replace(/（/g, " (").replace(/）/g, ")").replace(/；/g, "; ").replace(/、/g, ", ").replace(/\s{2,}/g, " ");
  }
  function visit(root) {
    if (!root) return;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const nodes = [];
    if (root.nodeType === Node.TEXT_NODE) nodes.push(root);
    while (walker.nextNode()) nodes.push(walker.currentNode);
    for (const node of nodes) {
      if (node.parentElement?.closest("script, style, .editor, .ai-translator, .ai-translator-nav, .personal-copy")) continue;
      if (!originals.has(node) || (renderedText.has(node) && node.nodeValue !== renderedText.get(node))) originals.set(node, node.nodeValue);
      const source = originals.get(node);
      const rendered = language === "en" ? (node.parentElement?.closest(".personal-nav") ? (personalNavLabels[source] || translate(source)) : translate(source)) : source;
      if (node.nodeValue !== rendered) node.nodeValue = rendered;
      renderedText.set(node, rendered);
    }
    const elements = root.nodeType === Node.ELEMENT_NODE ? [root, ...root.querySelectorAll("*")] : root.querySelectorAll?.("*") || [];
    for (const el of elements) {
      if (el.closest(".editor, .ai-translator, .ai-translator-nav, .personal-copy")) continue;
      for (const attr of ["placeholder", "title", "aria-label", "alt"]) {
        if (el.id === "languageToggle") continue;
        if (!el.hasAttribute(attr)) continue;
        let saved = originalAttrs.get(el);
        if (!saved) { saved = {}; originalAttrs.set(el, saved); }
        const current = el.getAttribute(attr);
        if (!(attr in saved) || (renderedAttrs.get(el)?.[attr] !== undefined && current !== renderedAttrs.get(el)[attr])) saved[attr] = current;
        const rendered = language === "en" ? (el.classList.contains("personal-nav") ? (personalNavLabels[saved[attr]] || translate(saved[attr])) : translate(saved[attr])) : saved[attr];
        if (current !== rendered) el.setAttribute(attr, rendered);
        const renderedForEl = renderedAttrs.get(el) || {};
        renderedForEl[attr] = rendered;
        renderedAttrs.set(el, renderedForEl);
      }
    }
  }
  function setLanguage(next) {
    language = next;
    try { localStorage.setItem("dashboard-language-v2", next); } catch {}
    document.documentElement.lang = next === "en" ? "en" : "zh-CN";
    document.title = next === "en" ? translate(sourceTitle) : sourceTitle;
    visit(document.body);
    const button = document.getElementById("languageToggle");
    if (button) button.setAttribute("aria-label", next === "en" ? "Switch language / 切换语言" : "切换语言 / Switch language");
    window.dispatchEvent(new Event("dashboard-language-change"));
  }

  setLanguage(language);
  document.addEventListener("click", (event) => {
    if (!(event.target instanceof Element) || !event.target.closest("#languageToggle")) return;
    event.preventDefault();
    setLanguage(language === "en" ? "zh" : "en");
  });
  new MutationObserver((changes) => {
    for (const change of changes) {
      if (change.type === "childList") {
        for (const node of change.addedNodes) {
          if (node.nodeType === Node.ELEMENT_NODE || node.nodeType === Node.TEXT_NODE) visit(node);
        }
      } else if (change.type === "characterData" || change.type === "attributes") {
        visit(change.target);
      }
    }
  }).observe(document.body, { childList: true, characterData: true, attributes: true, attributeFilter: ["placeholder", "title", "aria-label", "alt"], subtree: true });
})();
