"""D56：自选个股更新的四种情况（revquant 的语义要求）。全部用假数据源和临时缓存目录，不联网。
python tests/test_update_stock_d56.py"""
import logging
import sys
import tempfile
import types
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pandas as pd  # noqa: E402

from src import allmarket, datasource as ds, market_clock  # noqa: E402

CFG = {"source": "sina", "start": "2020-01-01", "tz": "Asia/Shanghai", "close_ready": "15:30"}
BJ = ZoneInfo("Asia/Shanghai")


def bars(dates, base=10.0):
    idx = pd.DatetimeIndex(pd.to_datetime(dates))
    c = pd.Series([base + i * 0.1 for i in range(len(idx))], index=idx)
    return pd.DataFrame({"Open": c, "High": c, "Low": c, "Close": c, "Volume": 100.0}, index=idx)


class Env:
    """临时缓存目录 + 假数据源 + 固定"现在"时间；退出时全部还原。"""

    def __init__(self, sina, tencent, now):
        self.sina, self.tencent, self.now = sina, tencent, now

    def __enter__(self):
        self.saved = (ds.CACHE, ds._fetch_stock_sina, allmarket._fetch_tencent, ds.time, ds.datetime,
                      allmarket.trade_calendar, market_clock.to_beijing)
        self.tmp = tempfile.TemporaryDirectory()
        ds.CACHE = Path(self.tmp.name)
        ds._fetch_stock_sina = self.sina
        allmarket._fetch_tencent = self.tencent
        ds.time = types.SimpleNamespace(sleep=lambda s: None)
        now = self.now

        class FixedDT(datetime):
            @classmethod
            def now(cls, tz=None):
                return now.astimezone(tz) if tz else now
        ds.datetime = FixedDT
        allmarket.trade_calendar = lambda: [d.date() for d in pd.bdate_range("2026-10-01", "2026-11-30")]
        orig = market_clock.to_beijing
        market_clock.to_beijing = lambda n=None: orig(now if n is None else n)
        return self

    def __exit__(self, *a):
        (ds.CACHE, ds._fetch_stock_sina, allmarket._fetch_tencent, ds.time, ds.datetime,
         allmarket.trade_calendar, market_clock.to_beijing) = self.saved
        self.tmp.cleanup()


OLD = bars(pd.bdate_range("2026-10-01", "2026-11-03"))           # 本地缓存到 11-03


def empty(*a):
    return pd.DataFrame(columns=ds.COLS)


def boom(*a):
    raise ConnectionError("injected")


def run(sina, tencent, now):
    with Env(sina, tencent, now):
        ds._save_cache_csv(OLD, ds.cache_path("cn", "600531"))
        return ds.update_stock("cn", "600531", CFG)


def logs_of(fn):
    rec = []
    h = logging.Handler()
    h.emit = lambda r: rec.append(r.getMessage())
    root = logging.getLogger()
    root.addHandler(h)
    old_level = root.level
    root.setLevel(logging.INFO)
    try:
        out = fn()
    finally:
        root.removeHandler(h)
        root.setLevel(old_level)
    return out, rec


AFTER_CLOSE = datetime(2026, 11, 4, 17, 0, tzinfo=BJ)          # 北京 17:00，应有 11-04 日线
BEFORE_CLOSE = datetime(2026, 11, 4, 14, 0, tzinfo=BJ)         # 北京 14:00，盘中


def test_case1_both_sources_fail_reports_both():
    for sina, tencent in ((empty, boom), (boom, boom), (empty, empty), (boom, empty)):
        df, err, used = run(sina, tencent, AFTER_CLOSE)
        assert err and used is None, (sina.__name__, tencent.__name__, err)
        assert "sina" in err and "tencent" in err, err                    # 两个源的原因都写出来
        assert len(df) == len(OLD)                                          # 旧数据保留，没有覆盖


def test_case2_only_intraday_bar_before_close_is_not_failure():
    intraday = lambda *a: bars(list(OLD.index[-3:]) + [pd.Timestamp("2026-11-04")])   # 含今天盘中半根
    (df, err, used), rec = logs_of(lambda: run(intraday, boom, BEFORE_CLOSE))
    assert err is None and used == "sina", err
    assert df.index[-1].date() == date(2026, 11, 3)                         # 半根被丢掉
    assert any("暂无新的完整日线" in m for m in rec), rec


def test_case3_source_behind_expected_day_is_notice_not_failure():
    behind = lambda *a: bars(list(OLD.index[-3:]))                           # 17:00 还只有到 11-03
    (df, err, used), rec = logs_of(lambda: run(behind, boom, AFTER_CLOSE))
    assert err is None and used == "sina"
    assert any("还没有 2026-11-04 的日线" in m for m in rec), rec


def test_case3b_new_rows_but_still_behind():
    old2 = OLD.iloc[:-1]                                                     # 本地只到 11-02
    def run2():
        with Env(lambda *a: bars(list(OLD.index[-4:]), base=OLD["Close"].iloc[-4]), boom, AFTER_CLOSE):
            ds._save_cache_csv(old2, ds.cache_path("cn", "600531"))
            return ds.update_stock("cn", "600531", CFG)
    (df, err, used), rec = logs_of(run2)
    assert err is None and df.index[-1].date() == date(2026, 11, 3)          # 补上了 11-03
    assert any("还没有 2026-11-04 的日线" in m for m in rec), rec             # 但仍提示缺 11-04


def test_case4_normal_update():
    fresh = lambda *a: bars(list(OLD.index[-3:]) + [pd.Timestamp("2026-11-04")], base=OLD["Close"].iloc[-3])
    (df, err, used), rec = logs_of(lambda: run(fresh, boom, AFTER_CLOSE))
    assert err is None and used == "sina" and df.index[-1].date() == date(2026, 11, 4)
    assert not any("还没有" in m or "暂无新的" in m for m in rec), rec


def test_tencent_backup_used_when_sina_empty():
    fresh = lambda *a: bars(list(OLD.index[-3:]) + [pd.Timestamp("2026-11-04")], base=OLD["Close"].iloc[-3])
    df, err, used = run(empty, fresh, AFTER_CLOSE)
    assert err is None and used == "tencent" and df.index[-1].date() == date(2026, 11, 4)


if __name__ == "__main__":
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
    print(f"ok: {n} 项通过")
