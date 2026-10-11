"""两个市场的配置。改这里就能换品种。"""

MARKETS = {
    "us": {
        "name": "美股",
        "source": "yahoo",
        "tz": "America/New_York",
        "close_ready": "16:30",          # 当地时间几点之后才认为今天的日线是完整的
        "start": "2005-01-01",
        "main": "SPY",                   # 主信号品种
        "cash": "SHY",                   # 空仓时的现金替代
        "universe": {                    # 代码 → 中文名
            "SPY": "标普500", "QQQ": "纳斯达克100", "IWM": "罗素2000",
            "EFA": "发达市场(除美)", "GLD": "黄金", "TLT": "20年+美债",
            "IEF": "7-10年美债", "SHY": "1-3年美债",
        },
        # 美股个股自选：免费 Yahoo Finance 日线；网页中可增删。
        "stocks": {
            "AAPL": "Apple", "MSFT": "Microsoft", "NVDA": "NVIDIA",
            "AMZN": "Amazon", "GOOGL": "Alphabet", "META": "Meta Platforms",
            "TSLA": "Tesla",
        },
        "dual_momentum": {"risky": ("SPY", "EFA"), "safe": "IEF", "cash": "SHY"},
        "cost_bp": 5,
        "stock_cost_bp": 10,             # 个股：买卖价差 + 滑点，比 ETF 大
        "notify": True,                  # 每天更新完弹 Mac 通知
        "alert_strategies": ["blend2"],  # 哪些策略的信号变化要提醒
    },
    "cn": {
        "name": "A股",
        "source": "sina",                # A 股 ETF 日线走新浪；东方财富只用于每日一次全市场快照
        "tz": "Asia/Shanghai",
        "close_ready": "15:30",
        "start": "2012-01-01",
        "main": "510300",
        "cash": "511880",
        "universe": {
            "510300": "沪深300ETF", "510500": "中证500ETF", "159915": "创业板ETF",
            "518880": "黄金ETF", "511010": "国债ETF", "511880": "银华日利(货币)",
        },
        "dual_momentum": {"risky": ("510300", "510500"), "safe": "511010", "cash": "511880"},
        "cost_bp": 5,                    # ETF 免印花税，佣金+滑点按单边万五估
        # 自选股：默认列表，之后在网页上增删（保存在 data/daily/watchlist_cn.json）
        "stocks": {"600531": "豫光金铅"},
        "stock_cost_bp": 8,              # 个股：佣金约万2.5 + 卖出印花税万5（平摊到单边约万4）+ 滑点
        "all_market": True,              # 每天回测沪深全部个股（先运行一次 python backfill.py）
        "repair_minutes": 30,            # 每天最多花多少分钟补缺失的历史数据
        "repair_max_stocks": 500,        # 单次最多修复数量，过多缺口分批续传
        "baostock_fallback_max": 50,     # 新浪和腾讯都失败后，单次最多用 BaoStock 兜底的数量
        "notify": True,                  # 每天更新完弹 Mac 通知
        "alert_strategies": ["blend2"],  # 自选股哪些策略的信号变化要提醒
    },
}

# 启用哪些市场，顺序 = 网页上的顺序，第一个是默认打开的市场
ENABLED = ["cn", "us"]

SERVER_HOST = "127.0.0.1"   # 只允许本机访问；不要改成 0.0.0.0
SERVER_PORT = 8765
