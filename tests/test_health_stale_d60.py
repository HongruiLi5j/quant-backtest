"""D60：健康提示和观察列表按「应有的最新交易日」判断过期（revquant_stability I2）。
合成行情、固定时间、假交易日历、临时目录（state.json 不写进 data/），不联网。python tests/test_health_stale_d60.py"""
import copy
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from config import MARKETS  # noqa: E402
from src import allmarket, daily_report as dr  # noqa: E402

BJ = ZoneInfo("Asia/Shanghai")
CAL = [d.date() for d in pd.bdate_range("2024-01-01", "2026-12-31")]


def series(last, n=400, seed=0):
    idx = pd.bdate_range(end=last, periods=n)
    rng = np.random.default_rng(seed)
    c = pd.Series(10 * np.exp(np.cumsum(rng.normal(0, 0.01, n))), index=idx)
    return pd.DataFrame({"Open": c, "High": c, "Low": c, "Close": c, "Volume": 1e6}, index=idx)


def build(now, last_by_sym, errors=None):
    cfg = copy.deepcopy(MARKETS["cn"])
    main = cfg["main"]
    other = next(s for s in cfg["universe"] if s != main)
    watch = "600531"
    syms = {main: last_by_sym.get("main", "2026-11-04"), other: last_by_sym.get("other", "2026-11-04"),
            watch: last_by_sym.get("watch", "2026-11-04")}
    prices = {s: series(d, seed=i) for i, (s, d) in enumerate(syms.items())}

    class FixedDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz else now
    saved = (dr.datetime, allmarket.trade_calendar, dr.CACHE, dr.load_watchlist)
    with tempfile.TemporaryDirectory() as t:
        dr.datetime, allmarket.trade_calendar, dr.CACHE = FixedDT, (lambda: CAL), Path(t)
        dr.load_watchlist = lambda m: {watch: "豫光金铅"}
        try:
            out = dr.build("cn", cfg, prices, errors or {})
        finally:
            dr.datetime, allmarket.trade_calendar, dr.CACHE, dr.load_watchlist = saved
    return out, main, other, watch


AFTER = datetime(2026, 11, 4, 17, 0, tzinfo=BJ)      # 周三 17:00：应有 11-04
BEFORE = datetime(2026, 11, 4, 14, 0, tzinfo=BJ)     # 盘中：应有 11-03


def health(out):
    return "\n".join(out["health"])


def test_fresh_no_warning():
    out, *_ = build(AFTER, {})
    assert not any("落后" in h for h in out["health"]), out["health"]
    assert all(w.get("behind") == 0 for w in out["watch"])


def test_main_one_day_behind_is_flagged_as_affecting_signals():
    out, main, *_ = build(AFTER, {"main": "2026-11-03"})
    h = health(out)
    assert f"（{main}）最新数据停在 2026-11-03，比应有的 2026-11-04 落后 1 个交易日" in h, h
    assert "今天的策略信号用到了它" in h
    assert "仍是最近的交易日" not in h


def test_before_close_yesterday_is_not_stale():
    out, *_ = build(BEFORE, {"main": "2026-11-03", "other": "2026-11-03", "watch": "2026-11-03"})
    assert not any("落后" in h for h in out["health"]), out["health"]


def test_watch_stock_behind_only_affects_display_and_row_marked():
    out, main, other, watch = build(AFTER, {"watch": "2026-10-30"}, errors={"600531": "sina 返回空；tencent 超时"})
    h = health(out)
    assert "落后 3 个交易日；只影响观察列表的显示" in h and "原因：sina 返回空" in h, h
    row = next(w for w in out["watch"] if w["symbol"] == watch)
    assert row["behind"] == 3 and row["last_date"] == "2026-10-30"


def test_soft_error_wording_when_data_is_current():
    other = next(s for s in MARKETS["cn"]["universe"] if s != MARKETS["cn"]["main"])
    out, *_ = build(AFTER, {}, errors={other: "ConnectionError"})
    h = health(out)
    assert "本地已是应有的最新交易日" in h and "仍是最近的交易日" not in h, h


if __name__ == "__main__":
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
    print(f"ok: {n} 项通过")
