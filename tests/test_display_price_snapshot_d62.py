"""D62：显示价格先用当天快照的真实收盘价，取不到再问腾讯。临时目录 + 假腾讯接口，不联网、不碰 data/。
python tests/test_display_price_snapshot_d62.py"""
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pandas as pd  # noqa: E402

import engines.common as ec  # noqa: E402
from src import display_price as dp, turnover  # noqa: E402


def df_last(day, close):
    idx = pd.DatetimeIndex([pd.Timestamp(day)])
    return pd.DataFrame({"Open": close, "High": close, "Low": close, "Close": close, "Volume": 1.0}, index=idx)


class Env:
    def __init__(self, rows, tencent):
        self.rows, self.tencent = rows, tencent

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.saved = (turnover.OUT, dp._SCALE_FILE, ec._raw_close_tencent, dp._fail_until, dict(dp.stats), dict(dp._snap))
        turnover.OUT = base / "turnover_cn.parquet"
        pd.DataFrame(self.rows, columns=["date", "code", "close_raw"]).to_parquet(turnover.OUT)
        dp._SCALE_FILE = base / "engine_price_scale.json"
        self.calls = []
        ec._raw_close_tencent = lambda code, day: (self.calls.append(code), self.tencent.get(code))[1]
        dp._fail_until = 0.0
        dp._snap.update(stamp=None, closes={})
        for k in dp.stats:
            dp.stats[k] = 0
        return self

    def __exit__(self, *a):
        turnover.OUT, dp._SCALE_FILE, ec._raw_close_tencent, dp._fail_until, st, snap = self.saved
        dp.stats.clear(); dp.stats.update(st)
        dp._snap.clear(); dp._snap.update(snap)
        self.tmp.cleanup()


def test_snapshot_first_no_network():
    with Env([("2026-11-04", "600531", 10.18)], {"600531": 99.0}) as e:
        k, note = dp.scale_for("cn", "600531", df_last("2026-11-04", 50.9))
        assert note == "前复权" and abs(k - 10.18 / 50.9) < 1e-12
        assert e.calls == [] and dp.stats == {"snapshot": 1, "tencent": 0, "failed": 0}


def test_fallback_to_tencent_when_missing_or_bad():
    rows = [("2026-11-03", "600531", 10.0), ("2026-11-04", "600000", float("nan")), ("2026-11-04", "000001", -1.0)]
    with Env(rows, {"600531": 10.18, "600000": 8.0, "000001": 11.0}) as e:
        for code in ("600531", "600000", "000001"):             # 没有这一天 / NaN / 负数 → 都去腾讯
            k, note = dp.scale_for("cn", code, df_last("2026-11-04", 20.0))
            assert note == "前复权"
        assert e.calls == ["600531", "600000", "000001"] and dp.stats["tencent"] == 3


def test_both_fail_counts_and_cooldown_unchanged():
    with Env([], {}) as e:
        assert dp.scale_for("cn", "600531", df_last("2026-11-04", 20.0)) == (1.0, "未取到真实价格，显示为后复权")
        assert dp._fail_until > 0 and dp.stats["failed"] == 1
        dp.scale_for("cn", "600000", df_last("2026-11-04", 20.0))   # 冷却期内不再请求腾讯
        assert e.calls == ["600531"] and dp.stats["failed"] == 2


def test_snapshot_file_change_reloads():
    with Env([("2026-11-04", "600531", 10.0)], {}) as e:
        dp.scale_for("cn", "600531", df_last("2026-11-04", 20.0))
        pd.DataFrame([("2026-11-04", "600531", 10.0), ("2026-11-05", "600531", 11.0)],
                     columns=["date", "code", "close_raw"]).to_parquet(turnover.OUT)
        k, _ = dp.scale_for("cn", "600531", df_last("2026-11-05", 22.0))
        assert abs(k - 0.5) < 1e-12 and e.calls == []


def test_non_cn_untouched():
    assert dp.scale_for("us", "SPY", df_last("2026-11-04", 500.0)) == (1.0, "")


if __name__ == "__main__":
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
    print(f"ok: {n} 项通过")
