"""上线后的清单信号单独记账；不写入或回放原 signals_history。"""
from __future__ import annotations

import fcntl
import json
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from config import MARKETS
from .datasource import CACHE
from . import turnover

BASE_COLUMNS = ["market", "signal_date", "code", "name", "strategy", "action", "next", "recorded_at"]
COLUMNS = BASE_COLUMNS + ["source", "now", "close", "reason", "snapshot_json"]
NOTE = ("2020 年以后的回测我已经看过，不算干净的样本外；只有上线后当时记录、未事后改写的信号跟踪才是真正的样本外检验。"
        "这里假设下一交易日开盘成交，统计股票价格收益，不含费用、滑点和涨跌停造成的无法成交；卖出信号后的股票收益不等于卖出策略收益。")


def _now(now=None):
    return (now or datetime.now(ZoneInfo("Asia/Shanghai"))).astimezone(ZoneInfo("Asia/Shanghai"))


def recording_window(day, base):
    """发布日收盘至下一交易日开盘；缓存未覆盖时仅退化为下一个工作日。"""
    release = pd.Timestamp(day).date()
    calendar = turnover.cached_calendar(base)
    following = [value for value in calendar if value > release]
    if release in calendar and following:
        next_day = following[0]
    else:
        next_day = release + timedelta(days=1)
        while next_day.weekday() >= 5:
            next_day += timedelta(days=1)
    zone = ZoneInfo("Asia/Shanghai")
    return (datetime.combine(release, time(15, 30), zone),
            datetime.combine(next_day, time(9, 30), zone))


def initialize(*, cache=None, now=None):
    base = Path(cache) if cache is not None else CACHE
    path = base / "trade_signals_launch.json"
    if not path.exists():
        base.mkdir(parents=True, exist_ok=True)
        # 排他创建，只初始化一次，不回溯上线日期。
        try:
            with path.open("x", encoding="utf-8") as handle:
                json.dump({"started_at": _now(now).isoformat(timespec="seconds")}, handle)
        except FileExistsError:
            pass
    return json.loads(path.read_text())


def record_publication(payload, *, cache=None, now=None):
    base = Path(cache) if cache is not None else CACHE
    current = _now(now)
    launch = initialize(cache=base, now=current)
    day = payload["data_date"]
    start, end = recording_window(day, base)
    generated = datetime.fromisoformat(payload["generated_at"])
    launched = datetime.fromisoformat(launch["started_at"])
    # 跨午夜仍可记录；下一开盘起不补记，生成时间也必须处于上线后的本次窗口内。
    if (not start <= current < end or generated.tzinfo is None or launched.tzinfo is None
            or not max(start, launched) <= generated <= current):
        return 0
    rows = []
    for strategy, pack in payload["strategies"].items():
        for row in pack["rows"]:
            rows.append({"market": payload["market"], "signal_date": day, "code": row["code"],
                         "name": row["name"], "strategy": strategy, "action": row["action"], "next": row["next"],
                         "recorded_at": current.isoformat(timespec="seconds"), "source": "trade_list",
                         "now": row["now"], "close": row["close"], "reason": row["reason"],
                         "snapshot_json": json.dumps(row, ensure_ascii=False, allow_nan=False)})
    if not rows:
        return 0
    path = base / "trade_signals_history.parquet"
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        old = pd.read_parquet(path) if path.exists() else pd.DataFrame(columns=COLUMNS)
        keys = ["market", "signal_date", "code", "strategy"]
        existing = set(old[keys].itertuples(index=False, name=None)) if len(old) else set()
        fresh = [row for row in rows if tuple(row[key] for key in keys) not in existing]
        if not fresh:
            return 0
        new = pd.DataFrame(fresh, columns=COLUMNS)
        for column in ("next", "now", "close"):
            new[column] = pd.to_numeric(new[column], errors="coerce").astype("Float64")
            if len(old):
                old[column] = pd.to_numeric(old[column], errors="coerce").astype("Float64")
        combined = pd.concat([old, new], ignore_index=True) if len(old) else new
        combined = combined.drop_duplicates(keys, keep="first").reset_index(drop=True)
        tmp = path.with_suffix(".tmp.parquet")
        combined.to_parquet(tmp, index=False)
        pd.testing.assert_frame_equal(combined, pd.read_parquet(tmp), check_dtype=False)
        tmp.replace(path)
    return len(combined) - len(old)


def evaluate(strategy="blend2", *, cache=None, prices_loader=None):
    """只读上线后的独立信号；不调用 build_replay，不混入旧跟踪样本。"""
    from . import tracking
    base = Path(cache) if cache is not None else CACHE
    path = base / "trade_signals_history.parquet"
    empty = {"empty": True, "strategy": strategy, "note": NOTE, "groups": []}
    if not path.exists():
        return empty
    hist = pd.read_parquet(path)
    hist = hist[(hist.market == "cn") & (hist.strategy == strategy) & (hist.source == "trade_list")]
    if hist.empty:
        return empty
    loader = prices_loader or tracking._prices
    cfg = MARKETS["cn"]
    bench = loader("cn", cfg["main"], cfg)
    cached, rows = {}, []
    for signal in hist.itertuples():
        if signal.code not in cached:
            cached[signal.code] = loader("cn", signal.code, cfg)
        frame = cached[signal.code]
        if frame is None or frame.empty:
            continue
        row = {"action": signal.action}
        for horizon in tracking.HORIZONS:
            ret, done, _ = tracking._fwd(frame, signal.signal_date, horizon)
            benchmark, benchmark_done, _ = tracking._fwd(bench, signal.signal_date, horizon) if bench is not None else (None, False, None)
            row[horizon] = (ret, done, benchmark, benchmark_done)
        rows.append(row)
    groups = []
    for action in ("买入", "卖出", "加仓", "减仓"):
        selected = [row for row in rows if row["action"] == action]
        group = {"action": action, "signals": len(selected)}
        for horizon in tracking.HORIZONS:
            completed = [row[horizon] for row in selected if row[horizon][1] and row[horizon][0] is not None]
            comparable = [value for value in completed if value[3] and value[2] is not None]
            group[f"n{horizon}"] = len(completed)
            group[f"avg{horizon}"] = sum(value[0] for value in completed) / len(completed) * 100 if completed else None
            group[f"bench_n{horizon}"] = len(comparable)
            group[f"beat{horizon}"] = sum(value[0] > value[2] for value in comparable) / len(comparable) * 100 if comparable else None
        groups.append(group)
    return {"empty": False, "strategy": strategy, "note": NOTE, "groups": groups,
            "since": hist.signal_date.min(), "recorded_signals": len(hist),
            "latest_price_date": str(bench.index[-1].date()) if bench is not None and not bench.empty else None}
