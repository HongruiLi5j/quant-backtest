"""真实成交额旁表：统一元/股，独立于原 OHLCV，失败不影响日线更新。"""
from __future__ import annotations

import fcntl
import logging
import math
import re
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from .datasource import CACHE

OUT = CACHE / "turnover_cn.parquet"
COLUMNS = ["date", "code", "turnover_yuan", "volume_shares", "close_raw", "low_raw", "high_raw", "source"]
SAMPLES = ("600531", "600000", "600519", "601318", "000001", "000858", "300750", "688981")


def cached_calendar(cache=None):
    """只读已有交易日历；缺失或无效时返回空，不触发联网或猜测节假日。"""
    base = Path(cache) if cache is not None else OUT.parent
    try:
        values = pd.to_datetime(pd.read_csv(base / "cn_trade_calendar.csv")["trade_date"], errors="raise")
        return sorted({value.date() for value in values if not pd.isna(value)})
    except Exception as exc:
        logging.warning("本地成交额交易日历不可用：%s", exc)
        return []


def _finite(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def normalize(code, day, amount, volume, close, low, high, *, source, amount_unit, volume_unit):
    """成交均价应落在当日低高价附近；单位不明确、缺失或异常时不入库。"""
    if not re.fullmatch(r"(?:60|68|00|30)\d{4}", str(code)):
        return None
    if amount_unit not in ("元", "万元") or volume_unit not in ("股", "手"):
        return None
    values = [_finite(x) for x in (amount, volume, close, low, high)]
    if any(x is None or x <= 0 for x in values):
        return None
    amount, volume, close, low, high = values
    amount *= 10000 if amount_unit == "万元" else 1
    volume *= 100 if volume_unit == "手" else 1
    vwap = amount / volume
    if not (low <= high and low * .98 <= vwap <= high * 1.02 and .5 <= vwap / close <= 2):
        return None
    return {"date": pd.Timestamp(day).strftime("%Y-%m-%d"), "code": code,
            "turnover_yuan": amount, "volume_shares": volume, "close_raw": close,
            "low_raw": low, "high_raw": high, "source": source}


def load(path=None):
    path = Path(path) if path is not None else OUT
    if not path.exists():
        return pd.DataFrame(columns=COLUMNS)
    frame = pd.read_parquet(path)
    if not set(COLUMNS).issubset(frame.columns) or frame.duplicated(["date", "code"]).any():
        raise ValueError("成交额旁表结构或唯一键异常")
    return frame[COLUMNS]


def save(rows, path=None):
    """锁内合并，临时 parquet 回读校验后替换；不触碰原行情文件。"""
    if not rows:
        return 0
    path = Path(path) if path is not None else OUT
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        old = load(path)
        new = pd.DataFrame(rows, columns=COLUMNS)
        combined = (pd.concat([old, new], ignore_index=True) if len(old) else new.copy()).drop_duplicates(["date", "code"], keep="last")
        combined = combined.sort_values(["date", "code"]).reset_index(drop=True)
        tmp = path.with_suffix(".tmp.parquet")
        combined.to_parquet(tmp, index=False)
        pd.testing.assert_frame_equal(combined, pd.read_parquet(tmp), check_dtype=False)
        tmp.replace(path)
    return len(new)


def capture_snapshot(raw, source, now=None, *, path=None):
    """只使用调用方已有快照；无网络请求，盘中不保存成交额。"""
    now = (now or datetime.now(ZoneInfo("Asia/Shanghai"))).astimezone(ZoneInfo("Asia/Shanghai"))
    if now.time() < time(15, 30):
        return 0
    base = Path(path).parent if path is not None else OUT.parent
    if now.date() not in cached_calendar(base):
        logging.info("跳过成交额快照：%s 非缓存交易日或日历不可用", now.date())
        return 0
    units = {"东方财富": ("元", "手"), "新浪": ("元", "股")}
    if source not in units:
        raise ValueError("未知快照来源，不能推断成交额单位")
    required = {"代码", "成交额", "成交量", "最新价", "最低", "最高"}
    if not required.issubset(raw.columns):
        raise ValueError("原始快照缺少成交额校验字段")
    rows = []
    for record in raw[list(required)].to_dict("records"):
        code = re.sub(r"\D", "", str(record["代码"]))[-6:]
        normalized = normalize(code, now.date(), record["成交额"], record["成交量"],
                               record["最新价"], record["最低"], record["最高"], source=source,
                               amount_unit=units[source][0], volume_unit=units[source][1])
        if normalized:
            rows.append(normalized)
    return save(rows, path)


def average_20d(code, day, calendar, table, suspended, minimum):
    """严格按发布日以前最近 20 个交易日；停牌来自既有记录，缺失不当零。"""
    days = sorted({pd.Timestamp(d).strftime("%Y-%m-%d") for d in calendar if pd.Timestamp(d).date() <= pd.Timestamp(day).date()})[-20:]
    unknown = {"avg_turnover_20d": None, "liquidity": "流动性未知", "turnover_days": 0}
    if len(days) < 20:
        return unknown
    current = table[(table.code == code) & table.date.isin(days)] if len(table) else table
    amounts = dict(zip(current.date, current.turnover_yuan)) if len(current) else {}
    values = []
    for d in days:
        if code in suspended.get(d, []):
            values.append(0.)
        elif d in amounts and _finite(amounts[d]) is not None and amounts[d] >= 0:
            values.append(float(amounts[d]))
        else:
            return {**unknown, "turnover_days": len(values)}
    average = sum(values) / 20
    return {"avg_turnover_20d": average, "liquidity": "正常" if average >= minimum else "流动性差", "turnover_days": 20}


def lot_cost(close, calibrated):
    close = _finite(close)
    return close * 100 if calibrated and close is not None and close > 0 else None
