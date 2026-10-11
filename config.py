"""Configuration for the two markets. Edit here to change the symbols."""

MARKETS = {
    "us": {
        "name": "美股",
        "source": "yahoo",
        "tz": "America/New_York",
        "close_ready": "16:30",          # local time after which today's daily bar is considered complete
        "start": "2005-01-01",
        "main": "SPY",                   # main signal symbol
        "cash": "SHY",                   # cash substitute when out of the market
        "universe": {                    # symbol -> display name (Chinese)
            "SPY": "标普500", "QQQ": "纳斯达克100", "IWM": "罗素2000",
            "EFA": "发达市场(除美)", "GLD": "黄金", "TLT": "20年+美债",
            "IEF": "7-10年美债", "SHY": "1-3年美债",
        },
        # US stock watchlist: free Yahoo Finance daily bars; can be added/removed in the dashboard.
        "stocks": {
            "AAPL": "Apple", "MSFT": "Microsoft", "NVDA": "NVIDIA",
            "AMZN": "Amazon", "GOOGL": "Alphabet", "META": "Meta Platforms",
            "TSLA": "Tesla",
        },
        "dual_momentum": {"risky": ("SPY", "EFA"), "safe": "IEF", "cash": "SHY"},
        "cost_bp": 5,
        "stock_cost_bp": 10,             # single stocks: bid-ask spread + slippage, larger than for ETFs
        "notify": True,                  # show a Mac notification after each daily update
        "alert_strategies": ["blend2"],  # strategies whose signal changes trigger an alert
    },
    "cn": {
        "name": "A股",
        "source": "sina",                # A-share ETF daily bars come from Sina; Eastmoney is only used for the once-a-day full-market snapshot
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
        "cost_bp": 5,                    # ETFs are exempt from stamp duty; commission + slippage estimated at 5bp one way
        # watchlist: default list, can be edited later in the dashboard (saved in data/daily/watchlist_cn.json)
        "stocks": {"600531": "豫光金铅"},
        "stock_cost_bp": 8,              # single stocks: commission about 2.5bp + sell-side stamp duty 5bp (about 4bp averaged per side) + slippage
        "all_market": True,              # backtest all Shanghai/Shenzhen stocks daily (run python backfill.py once first)
        "repair_minutes": 30,            # max minutes per day spent repairing missing history
        "repair_max_stocks": 500,        # max stocks repaired per run; larger gaps are resumed in batches
        "baostock_fallback_max": 50,     # max stocks per run to fall back to BaoStock after both Sina and Tencent fail
        "notify": True,                  # show a Mac notification after each daily update
        "alert_strategies": ["blend2"],  # watchlist strategies whose signal changes trigger an alert
    },
}

# enabled markets; the order is the dashboard order and the first one opens by default
ENABLED = ["cn", "us"]

SERVER_HOST = "127.0.0.1"   # local access only; do not change to 0.0.0.0
SERVER_PORT = 8765
