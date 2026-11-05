"""A股显示价格：把后复权价格换成"以最新一天为基准的前复权"，让最后一天等于真实成交价。

只用于显示。整条价格乘同一个系数 k，涨跌幅、信号、收益都不变；磁盘上的数据仍是后复权
（每日快照追加依赖它）。真实价格取自腾讯，结果缓存在 engine_price_scale.json（与引擎共用）。
"""
from __future__ import annotations
import json
import math
import numbers
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
_SCALE_FILE = ROOT / "data" / "daily" / "engine_price_scale.json"
_fail_until = 0.0
stats = {"snapshot": 0, "tencent": 0, "failed": 0}   # D62：本进程里真实价来源计数（今日买卖清单生成后写进日志）
_snap = {"stamp": None, "closes": {}}


def _snapshot_close(code: str, day: str) -> float | None:
    """D62：当天全市场快照的不复权收盘价（turnover_cn.parquet 的 close_raw，每日任务第 2 步写入）。
    和腾讯接口取的是同一个数：2745 个已缓存的 (股票, 日期) 对比全部相同（perf.md §R3-perf-1）。
    文件按修改时间+大小缓存，只读 date/code/close_raw 三列；读不到、没有这一天或价格不合法时返回 None，调用方再去腾讯取。"""
    try:
        from . import turnover
        st = turnover.OUT.stat()
        stamp = (st.st_mtime_ns, st.st_size)
        if _snap["stamp"] != stamp:
            import pandas as pd
            t = pd.read_parquet(turnover.OUT, columns=["date", "code", "close_raw"])
            _snap["closes"] = {(str(c), str(d)): v for c, d, v in zip(t["code"], t["date"], t["close_raw"])}
            _snap["stamp"] = stamp
    except Exception:
        return None
    v = _snap["closes"].get((code, day))
    return float(v) if _good_price(v) else None


def scale_for(market: str, code: str, df) -> tuple[float, str]:
    """返回 (k, 说明)。取不到真实价格时 k=1，说明里写明。"""
    global _fail_until
    if market != "cn" or df is None or len(df) == 0:
        return 1.0, ""
    last_day = df.index[-1].strftime("%Y-%m-%d")
    last = float(df["Close"].iloc[-1])
    cache = {}
    try:
        cache = json.loads(_SCALE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    hit = cache.get(code)
    if hit and hit.get("day") == last_day and hit.get("raw"):
        # 同一只股票在不同数据文件里后复权基准可能不同，所以只缓存真实价，k 按当前数据重算
        return float(hit["raw"]) / last, "前复权"
    raw = _snapshot_close(code, last_day)          # D62：先用当天全市场快照里已有的真实收盘价，不联网
    if raw is not None:
        stats["snapshot"] += 1
    else:
        if time.time() < _fail_until:
            stats["failed"] += 1
            return 1.0, "未取到真实价格，显示为后复权"
        from engines.common import _raw_close_tencent
        raw = _raw_close_tencent(code, last_day)
        if not raw:
            _fail_until = time.time() + 120
            stats["failed"] += 1
            return 1.0, "未取到真实价格，显示为后复权"
        stats["tencent"] += 1
    k = raw / last
    cache[code] = {"day": last_day, "k": k, "raw": raw}
    try:
        tmp = _SCALE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=0), encoding="utf-8")
        tmp.replace(_SCALE_FILE)
    except Exception:
        pass
    return k, "前复权"


def _good_price(x) -> bool:
    """价格必须是真正的实数（int/float/NumPy 实数标量；不能是 bool/np.bool_、字符串、Decimal），有限、为正。
    必须在 float() 之前检查：float(True)=1.0、float("75.694")=75.694 会把坏数据悄悄变成"好价格"。"""
    return (isinstance(x, numbers.Real) and not isinstance(x, (bool, np.bool_))
            and math.isfinite(x) and x > 0)


def cached_scale(market: str, code: str, df) -> tuple[float, str]:
    """只读缓存版的 scale_for：绝不联网。给"持仓不发起网络请求"的页面用。

    缓存里有这只股票、且日期正好是这根 K 线的那一天的真实收盘价，就返回 (k, "前复权")；
    否则返回 (1.0, "无缓存")，调用者应当显示"无现价"，而不是把后复权价当真实价。
    缓存里的 raw、K 线的最后一个 Close、以及算出来的 k 和还原后的价格，都必须是有限的正数且类型正确
    （bool、字符串、Infinity、NaN、负数、0 一律当作没有）。
    """
    if market != "cn" or df is None or len(df) == 0:
        return 1.0, "无缓存"
    try:
        hit = json.loads(_SCALE_FILE.read_text(encoding="utf-8")).get(code)
        last_day = df.index[-1].strftime("%Y-%m-%d")
        last_raw = df["Close"].iloc[-1]          # 先留原始类型，校验通过后才转 float
        if isinstance(hit, dict) and hit.get("day") == last_day:
            raw = hit.get("raw")
            if _good_price(raw) and _good_price(last_raw):
                last = float(last_raw)
                k = float(raw) / last
                if _good_price(k) and _good_price(last * k):         # 除法/乘法溢出或下溢也不要
                    return k, "前复权"
    except Exception:       # 缓存文件缺失/损坏/格式不对，都当作没有
        pass
    return 1.0, "无缓存"


def apply(market: str, code: str, df):
    k, note = scale_for(market, code, df)
    if k == 1.0:
        return df, k, note
    out = df.copy()
    out[["Open", "High", "Low", "Close"]] *= k
    return out, k, note
