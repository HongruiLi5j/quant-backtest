"""上线后的清单信号单独记账；不写入或回放原 signals_history。"""
from __future__ import annotations

import copy
import fcntl
import json
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from config import MARKETS
from .datasource import CACHE
from . import market_clock, resultcache, turnover

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
    return (market_clock.close_ready_at(release),
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


_EVAL_MEMO = resultcache.Memo(maxsize=8, ttl=600)    # B07：每次请求要读约 2000 个 parquet（约 1.6 秒），输入没变就别重算


_stamp = resultcache.file_stamp


def _price_stamps(codes):
    """这些代码对应的行情文件指纹。tracking._prices 会读 ETF 的 csv 或全市场 parquet，
    这里两个候选文件都盖章，只会多失效、不会漏失效。"""
    from . import allmarket, datasource
    return tuple((c, _stamp(datasource.cache_path("cn", c)), _stamp(allmarket.path(c))) for c in sorted(codes))


def _clock_state():
    """行情读取时会按北京时间 15:30 丢掉当天的半天 K 线（allmarket._drop_unfinished），
    所以「今天的日期」和「是否已过 15:30」也是输入；缓存跨过 15:30 必须失效。"""
    from . import allmarket
    return market_clock.clock_state(allmarket._now_bj())


_HIST_MEMO = {}     # D23：(路径, 文件指纹, 策略) -> (筛好的信号表, 代码集合)；最多留 8 份


def _read_hist(path, strategy):
    """读信号表并筛出本策略的清单信号。按文件指纹缓存（文件一变指纹就变，见 resultcache.file_stamp）。
    D23：原来每次请求都先读整张 parquet（约 1.4 万行、约 32ms）再去查结果缓存；命中缓存时这一步是白读。
    返回的表只读，调用方不要修改。"""
    key = (str(path), _stamp(path), strategy)
    hit = _HIST_MEMO.get(key)
    if hit is not None:
        return hit
    hist = pd.read_parquet(path)
    hist = hist[(hist.market == "cn") & (hist.strategy == strategy) & (hist.source == "trade_list")]
    out = (hist, frozenset(hist.code))
    if len(_HIST_MEMO) >= 8:
        _HIST_MEMO.clear()
    _HIST_MEMO[key] = out
    return out


def evaluate(strategy="blend2", *, cache=None, prices_loader=None):
    """只读上线后的独立信号；不调用 build_replay，不混入旧跟踪样本。

    B07：默认读行情（prices_loader 为空）时，结果按「信号表指纹 + 各行情文件指纹 + 15:30 状态」缓存，
    输入完全没变就直接返回上次的结果；传入自定义 prices_loader（测试用）时不缓存。"""
    from . import tracking
    base = Path(cache) if cache is not None else CACHE
    path = base / "trade_signals_history.parquet"
    empty = {"empty": True, "strategy": strategy, "note": NOTE, "groups": []}
    if not path.exists():
        return empty
    hist, codes = _read_hist(path, strategy)
    if hist.empty:
        return empty
    loader = prices_loader or tracking._prices
    cfg = MARKETS["cn"]
    memo_key = None
    if prices_loader is None:
        from . import signal_ledger
        memo_key = (str(path), _stamp(path), strategy, id(loader), _clock_state(),
                    _price_stamps(codes | {cfg["main"]}), _stamp(signal_ledger.DB))     # D16：等权基准读账本
        hit = _EVAL_MEMO.get(memo_key)
        if hit is not None:
            return copy.deepcopy(hit)
    result = _evaluate_rows(strategy, hist, loader, cfg, tracking)
    if memo_key is not None:
        _EVAL_MEMO.put(memo_key, copy.deepcopy(result), keepalive=loader)
    return result


def _ledger_baseline(db=None):
    """D16：从信号核对账本只读取「当天全市场个股等权平均」和随机基线，让这张表和信号核对用同一个基准。
    返回 ({(day, h): 平均收益}, {(day, h): (跑赢占比, 跑输占比)})；账本不存在或读失败就返回两个空字典（表里显示“数据不足”）。"""
    from . import signal_ledger as L
    p = Path(db) if db is not None else L.DB
    if not p.exists():
        return {}, {}
    try:
        import sqlite3
        c = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5)
        c.row_factory = sqlite3.Row
        try:
            return L.baseline(c), L.random_share(c)
        finally:
            c.close()
    except Exception:
        return {}, {}


def _evaluate_rows(strategy, hist, loader, cfg, tracking, ledger_db=None):
    bench = loader("cn", cfg["main"], cfg)
    ew, rnd = _ledger_baseline(ledger_db)
    cached, rows = {}, []
    for signal in hist.itertuples():
        if signal.code not in cached:
            cached[signal.code] = loader("cn", signal.code, cfg)
        frame = cached[signal.code]
        if frame is None or frame.empty:
            continue
        row = {"action": signal.action, "day": str(signal.signal_date)[:10]}
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
            # D16：和信号核对同一个基准（全市场个股等权平均），卖出/减仓按方向翻转；附随机挑股的基线。单位 %。
            buy = action in ("买入", "加仓")
            ew_rows = [(row[horizon][0], ew[(row["day"], horizon)], rnd[(row["day"], horizon)][0 if buy else 1])
                       for row in selected if row[horizon][1] and row[horizon][0] is not None and (row["day"], horizon) in ew]
            group[f"ew_n{horizon}"] = len(ew_rows)
            group[f"ew_dir{horizon}"] = (sum((r > m) if buy else (r < m) for r, m, _ in ew_rows) / len(ew_rows) * 100) if ew_rows else None
            group[f"ew_random{horizon}"] = (sum(x for _, _, x in ew_rows) / len(ew_rows) * 100) if ew_rows else None
            group[f"ew_edge{horizon}"] = (group[f"ew_dir{horizon}"] - group[f"ew_random{horizon}"]) if ew_rows else None
        groups.append(group)
    return {"empty": False, "strategy": strategy, "note": NOTE, "groups": groups,
            "ew_benchmark": "全市场个股等权平均（与信号核对同一基准）；ew_dir=方向正确率，ew_random=随机挑股的同口径基线，ew_edge=两者之差（百分点）",
            "beat_benchmark": f"{cfg['main']}（沪深300ETF，大盘股）",
            "since": hist.signal_date.min(), "recorded_signals": len(hist),
            "latest_price_date": str(bench.index[-1].date()) if bench is not None and not bench.empty else None}
