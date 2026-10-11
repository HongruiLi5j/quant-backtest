"""用 RQAlpha（米筐）跑一次 A 股回测。

RQAlpha 默认从米筐服务器下载数据包（bundle）。这里不下载，而是写了一个"自定义数据源 Mod"，
把本项目已有的本地数据（data/daily/cn_all/*.parquet）交给 RQAlpha。好处：
  1. 三个引擎用的是同一份数据，差别只来自引擎本身
  2. 不需要联网、不需要米筐账号

这个文件同时是一个 RQAlpha Mod（RQAlpha 按配置里的 lib 名导入本模块，调用 load_mod()）。
"""
from __future__ import annotations

import time

import numpy as np
import pandas as pd

from engines import common

_CURRENT: dict = {}          # 本次回测的数据，Mod 启动时从这里取


# ---------------------------------------------------------------- 自定义数据源
def _date_int(d) -> int:
    return int(pd.Timestamp(d).strftime("%Y%m%d")) * 1000000


def _ymd(d) -> int:
    """RQAlpha 传进来的日期可能是整数 20120104、date 或 datetime，统一成整数 YYYYMMDD。"""
    if isinstance(d, (int, np.integer)):
        d = int(d)
        return d // 1000000 if d > 10 ** 9 else d
    return int(pd.Timestamp(d).strftime("%Y%m%d"))


def _make_source():
    from rqalpha.data.base_data_source import BaseDataSource
    from rqalpha.data.base_data_source.storage_interface import (AbstractCalendarStore, AbstractDayBarStore)
    from rqalpha.data.base_data_source.storages import SecuritiesDayBarStore
    from rqalpha.const import INSTRUMENT_TYPE, MARKET, TRADING_CALENDAR_TYPE
    from rqalpha.model.instrument import Instrument

    class Calendar(AbstractCalendarStore):
        def __init__(self, days):
            self.days = days

        def get_trading_calendar(self):
            return self.days

    class Bars(AbstractDayBarStore):
        def __init__(self, arrays):
            self.arrays = arrays

        def get_bars(self, oid):
            return self.arrays.get(oid, np.empty(0, dtype=SecuritiesDayBarStore.DEFAULT_DTYPE))

        def get_date_range(self, oid):
            a = self.arrays[oid]
            return a["datetime"][0], a["datetime"][-1]

    class NoFactors:
        """价格已经是复权价，不需要 RQAlpha 再做除权/分红处理。"""
        def get_dividend(self, oid):
            return None

        def get_factors(self, oid):
            return None

    class Suspended:
        """某只股票在交易所交易日里没有 K 线 = 停牌。"""
        def __init__(self, have: dict):
            self.have = have

        def contains(self, oid, dates):
            s = self.have.get(oid)
            if s is None:
                return None
            return [_ymd(d) not in s for d in dates]

    class NotST:
        def contains(self, oid, dates):
            return None

    class ZeroCurve:
        """无风险利率曲线：统一按 0，这样 RQAlpha 自己算的 Sharpe 和另外两个引擎口径一致。"""
        TENORS = ["0S", "1M", "2M", "3M", "6M", "9M", "1Y", "2Y", "3Y", "5Y", "7Y", "10Y"]

        def __init__(self, days):
            self.df = pd.DataFrame(0.0, index=days, columns=self.TENORS)

        def get_yield_curve(self, start_date, end_date, tenor=None):
            df = self.df.loc[pd.Timestamp(start_date):pd.Timestamp(end_date)]
            return df[tenor] if tenor is not None else df

    class NoShareTransformation:
        def get_share_transformation(self, oid):
            return None

    class LocalDataSource(BaseDataSource):
        def __init__(self, d):   # 不调用父类 __init__（父类会去找 bundle 文件），直接注册内存里的数据
            self._market = MARKET.CN
            self._future_info_store = None
            self._yield_curve = ZeroCurve(d["calendar"])
            self._share_transformation = NoShareTransformation()
            self._suspend_days = [Suspended(d["have"])]
            self._st_stock_days = NotST()
            self._ins_id_or_sym_type_map = {}
            self._day_bar_stores, self._dividend_stores, self._split_stores = {}, {}, {}
            self._calendar_stores, self._ex_factor_stores = {}, {}
            self._id_instrument_map, self._sym_instrument_map = {}, {}
            from collections import ChainMap
            self._id_or_sym_instrument_map = ChainMap(self._id_instrument_map, self._sym_instrument_map)
            self._grouped_instruments = {}
            self.register_instruments([Instrument(i) for i in d["instruments"]])
            self.register_day_bar_store(INSTRUMENT_TYPE.CS, Bars(d["bars"]))
            nf = NoFactors()
            self.register_dividend_store(INSTRUMENT_TYPE.CS, nf)
            self.register_split_store(INSTRUMENT_TYPE.CS, nf)
            self.register_ex_factor_store(INSTRUMENT_TYPE.CS, nf)
            self.register_calendar_store(TRADING_CALENDAR_TYPE.CN_STOCK, Calendar(d["calendar"]))
            self._range = (d["calendar"][0].date(), d["calendar"][-1].date())

        def available_data_range(self, frequency):
            return self._range

    return LocalDataSource


def prepare(code: str, name: str, df: pd.DataFrame) -> dict:
    """把一只股票的 DataFrame 转成 RQAlpha 需要的格式。"""
    from rqalpha.data.base_data_source.storages import SecuritiesDayBarStore
    from src import allmarket
    oid = code + (".XSHG" if code.startswith("6") else ".XSHE")
    lim = common.limit_pct(code, name)
    prev = df["Close"].shift(1)
    # 涨跌停价：RQAlpha 判断"开盘价 ≥ 涨停价 - 1 个最小价位(0.01)"就不让买。
    # 这里把门槛设成和另外两个引擎一样：开盘涨幅 ≥ 涨停幅度 - 0.2%（复权价不是整分钱，不能用精确的涨停价）
    up = prev * (1 + lim - common.LIMIT_TOL) + 0.01
    dn = prev * (1 - lim + common.LIMIT_TOL) - 0.01
    arr = np.zeros(len(df), dtype=SecuritiesDayBarStore.DEFAULT_DTYPE)
    arr["datetime"] = [_date_int(d) for d in df.index]
    arr["open"], arr["high"], arr["low"], arr["close"] = (df[c].to_numpy() for c in ("Open", "High", "Low", "Close"))
    arr["volume"] = df["Volume"].to_numpy()
    arr["total_turnover"] = (df["Volume"] * df["Close"]).to_numpy()
    arr["limit_up"] = up.fillna(df["Close"] * 10).to_numpy()
    arr["limit_down"] = dn.fillna(0.0).to_numpy()
    cal = pd.to_datetime(pd.read_csv(allmarket.CAL_FILE)["trade_date"])
    cal = pd.DatetimeIndex(cal[(cal >= df.index[0]) & (cal <= df.index[-1])])
    cal = cal.union(df.index)                     # 保险：数据里有的日期一定是交易日
    board = "KSH" if code.startswith("68") else "GEM" if code.startswith("30") else "MainBoard"
    ins = {"order_book_id": oid, "symbol": name or code, "abbrev_symbol": code, "type": "CS",
           "listed_date": df.index[0].strftime("%Y-%m-%d"), "de_listed_date": "0000-00-00",
           "exchange": "XSHG" if code.startswith("6") else "XSHE", "round_lot": 100,
           "board_type": board, "status": "Active", "special_type": "Normal", "market_tplus": 1,
           "industry_code": "", "industry_name": "", "sector_code": "", "sector_code_name": "", "trading_hours": "09:31-11:30,13:01-15:00"}
    return {"oid": oid, "calendar": cal, "bars": {oid: arr},
            "have": {oid: set(int(d.strftime("%Y%m%d")) for d in df.index)}, "instruments": [ins]}


# ---------------------------------------------------------------- Mod：启动时把数据源装进 RQAlpha
def load_mod():
    from rqalpha.interface import AbstractMod

    class LocalDataMod(AbstractMod):
        def start_up(self, env, mod_config):
            env.set_data_source(_make_source()(_CURRENT))

        def tear_down(self, code, exception=None):
            pass

    return LocalDataMod()


# ---------------------------------------------------------------- 运行
def run(code: str, name: str, params: dict | None = None, start: str | None = None,
        cash: float = common.DEFAULT_CASH, df: pd.DataFrame | None = None, info: dict | None = None,
        light: bool = False) -> dict:
    import logging
    from rqalpha import run_func
    from engines import rq_base, rq_strategies

    t0 = time.time()
    params = common.parse_params(name, params)
    if df is None:
        raw, info = common.load_stock(code)
        df, k, note = common.to_forward_adjusted(code, raw)
    else:
        k, note = 1.0, ""
    info = info or {"name": code}
    data = prepare(code, info.get("name", ""), df)
    _CURRENT.clear()
    _CURRENT.update(data)

    strat = rq_strategies.STRATEGY_CLASSES[name](**params)
    funcs = rq_base.build_funcs(strat, data["oid"])
    ctx_holder = {}
    user_init = funcs["init"]

    def init(context):
        user_init(context)
        ctx_holder["ctx"] = context

    config = {
        "base": {"start_date": df.index[0].strftime("%Y-%m-%d"), "end_date": df.index[-1].strftime("%Y-%m-%d"),
                 "frequency": "1d", "accounts": {"stock": cash}, "market": "CN"},
        "extra": {"log_level": "error"},
        "mod": {
            "local_data": {"enabled": True, "lib": "engines.rq_engine", "priority": 1},
            "sys_analyser": {"enabled": True, "benchmark": None, "plot": False, "record": True},
            "sys_progress": {"enabled": False},
            "sys_simulation": {"enabled": True, "matching_type": "current_bar", "price_limit": True,
                               "volume_limit": False, "slippage": 0, "inactive_limit": True},
            "sys_transaction_cost": {"enabled": True, "stock_min_commission": common.MIN_COMMISSION,
                                     # RQAlpha 默认佣金万 8，乘 0.325 = 万 2.6（佣金万 2.5 + 过户费万 0.1）
                                     "stock_commission_multiplier": (common.COMMISSION + common.TRANSFER_FEE) / 0.0008,
                                     "pit_tax": True},
        },
    }
    logging.getLogger().setLevel(logging.ERROR)
    result = run_func(config=config, init=init, open_auction=funcs["open_auction"], handle_bar=funcs["handle_bar"])
    ctx = ctx_holder["ctx"]
    daily = pd.DataFrame(ctx.daily, columns=["date", "value", "exposure"])
    daily.index = pd.to_datetime(daily["date"])
    if light:
        return {"value": daily["value"], "index": df.index}

    fills = []
    an = (result or {}).get("sys_analyser", {})
    tr = an.get("trades")
    if tr is not None and len(tr):
        for _, row in tr.iterrows():
            q = int(row["last_quantity"]) * (1 if str(row["side"]).upper().endswith("BUY") else -1)
            fills.append({"date": pd.Timestamp(row["trading_datetime"] if "trading_datetime" in row else row.name).strftime("%Y-%m-%d"),
                          "qty": q, "price": float(row["last_price"]),
                          "fee": float(row.get("commission", 0.0)) + float(row.get("tax", 0.0))})
    summary = an.get("summary", {}) or {}
    native = {k2: summary.get(k2) for k2 in ("total_returns", "annualized_returns", "sharpe", "max_drawdown",
                                              "volatility", "total_value", "win_rate") if k2 in summary}
    extra = {
        "price_scale": k, "price_note": note,
        "events": [{"date": d, "msg": m} for d, m in ctx.events[-100:]],
        "native_summary": {k2: (float(v) if isinstance(v, (int, float, np.floating)) else v) for k2, v in native.items()},
        "engine_notes": [
            "撮合：收盘出信号，次日集合竞价（open_auction）下单，RQAlpha 按开盘价撮合",
            "费用：RQAlpha 内置股票费用模型，佣金调成万2.6（含过户费，每笔最低 5 元），印花税按当时真实税率（pit_tax）；无滑点",
            "整手、T+1、停牌不能交易、资金不足少买：全部由 RQAlpha 自己检查",
            "涨跌停：RQAlpha 的 price_limit 规则，门槛和另外两个引擎一致（开盘涨跌幅 ≥ 涨跌停幅度 - 0.2%）",
            "数据：用自定义数据源 Mod 把本地数据接入，没有使用米筐的数据包",
        ],
    }
    return common.pack_result("rqalpha", name, code, info, params, df, daily["value"], daily["exposure"],
                              fills, start, cash, t0, extra)
