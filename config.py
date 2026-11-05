"""Configuration for the two markets. Edit here to change the symbols."""
from src.costs import (CN_ETF_ONE_WAY_BP, CN_STOCK_ONE_WAY_BP, US_ETF_ONE_WAY_BP,  # cost numbers live in src/costs.py
                       US_STOCK_ONE_WAY_BP)

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
        "cost_bp": US_ETF_ONE_WAY_BP,
        "stock_cost_bp": US_STOCK_ONE_WAY_BP,   # single stocks: bid-ask spread + slippage, larger than for ETFs
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
        "cost_bp": CN_ETF_ONE_WAY_BP,    # ETFs are exempt from stamp duty; commission + slippage estimated at 5bp one way
        # watchlist: default list, can be edited later in the dashboard (saved in data/daily/watchlist_cn.json)
        "stocks": {"600531": "豫光金铅"},
        "stock_cost_bp": CN_STOCK_ONE_WAY_BP,   # single stocks: commission about 2.5bp + sell-side stamp duty 5bp (about 4bp averaged per side) + slippage
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

# 今日买卖：目标仓位变化严格超过 10 个百分点才标记加仓或减仓。
TRADE_LIST_THRESHOLD = 0.1

# 涨跌停价格取整到分导致实际涨跌幅略低；与现有引擎的 0.002 容差一致。
TRADE_LIST_LIMIT_TOLERANCE = 0.002
# 小仓位仅影响展示分组，不改策略信号；目标仓位直接与阈值比较。
TRADE_LIST_MIN_TARGET = 0.05

# 自我改进闭环（src/auto_tune.py）通过 data/auto_params.json 自动调这两个参数；
# 只接受 [默认值, 上限] 内的值，文件不存在或被改坏就用上面的默认值。
def _auto_override():
    import json as _j, pathlib as _p
    try:
        a = _j.loads((_p.Path(__file__).resolve().parent / "data" / "auto_params.json").read_text(encoding="utf-8"))["active"]
        thr, mt = float(a["thr"]), float(a["mt"])
        if 0.10 <= thr <= 0.30 and 0.05 <= mt <= 0.15:
            return thr, mt
    except Exception:
        pass
    return None
_ov = _auto_override()
if _ov:
    TRADE_LIST_THRESHOLD, TRADE_LIST_MIN_TARGET = _ov

# 20 个已收盘交易日的平均真实成交额，单位元；不足 20 日标为未知。
TRADE_LIST_MIN_TURNOVER = 50_000_000
