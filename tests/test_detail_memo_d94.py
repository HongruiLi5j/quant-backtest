"""D94：个股详情的 8 策略对比按数据指纹缓存；命中缓存时的输出与空缓存现算完全相同，缓存有上限、数据一变就重算。
用沙盒已有行情（只读），在同一份当前代码里比较（不和 git 旧版本比，免得以后别的提交改了文字也让测试失败）。
提交时另外对 git 093b8b0 的旧实现核对过一次，逐字节相同（见提交说明）。python tests/test_detail_memo_d94.py"""
import json
import sys
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
warnings.filterwarnings("ignore")
import pandas as pd  # noqa: E402

from config import MARKETS  # noqa: E402
from src import detail as D  # noqa: E402

CFG = MARKETS["cn"]


def codes(n=12):
    from src import allmarket
    have = [c for c in sorted(allmarket.cached_codes()) if c.startswith(("60", "00", "30"))]
    step = max(1, len(have) // n)
    return list(CFG["universe"])[:2] + have[::step][:n - 2]


def dump(x):
    return json.dumps(x, ensure_ascii=False, sort_keys=True, default=str)


def test_memo_hit_identical_to_fresh():
    for code in codes():
        for strategy in ("blend2", "rsi2"):
            for period in ("6m", "1y", "3y"):
                D._COMPARE_MEMO = None
                fresh = D.build("cn", code, CFG, strategy, period)      # 空缓存：现算
                D.build("cn", code, CFG, "trend_ens", "3y")             # 用同一份对比表算别的区间 / 策略
                hit = D.build("cn", code, CFG, strategy, period)        # 命中缓存
                assert dump(fresh) == dump(hit), (code, strategy, period)
    D._COMPARE_MEMO = None


def test_returned_compare_is_a_copy():
    D._COMPARE_MEMO = None
    code = codes(3)[-1]
    a = D.build("cn", code, CFG, "blend2", "1y")
    a["compare"][0]["cagr"] = 12345.0                                     # 调用方改了返回值
    b = D.build("cn", code, CFG, "blend2", "1y")
    assert b["compare"][0].get("cagr") != 12345.0


def test_bounded_and_invalidated_by_data_change():
    D._COMPARE_MEMO = None
    idx = pd.bdate_range("2015-01-01", periods=600)
    c = pd.Series(range(600), index=idx, dtype=float) * 0.01 + 10
    df = pd.DataFrame({"Open": c, "High": c * 1.01, "Low": c * 0.99, "Close": c, "Volume": 1e6})
    t0 = idx[250]
    rf = pd.Series(0.0, index=idx)
    for i in range(D._COMPARE_MAX + 5):
        D._compare_all("cn", f"9{i:05d}", {f"9{i:05d}": df}, CFG, 0.001, None, None, t0, rf)
    assert len(D._COMPARE_MEMO) == D._COMPARE_MAX
    calls = []
    real = D._run
    D._run = lambda *a, **k: (calls.append(1), real(*a, **k))[1]
    try:
        D._compare_all("cn", "900000", {"900000": df}, CFG, 0.001, None, None, t0, rf)   # 已被挤出：重算
        n1 = len(calls)
        D._compare_all("cn", "900000", {"900000": df}, CFG, 0.001, None, None, t0, rf)   # 命中：不算
        assert len(calls) == n1 and n1 == len(D.SINGLE)
        df2 = df.copy(); df2.iloc[-1, df2.columns.get_loc("Close")] += 0.01              # 数据变了一点：重算
        D._compare_all("cn", "900000", {"900000": df2}, CFG, 0.001, None, None, t0, rf)
        D._compare_all("cn", "900000", {"900000": df}, CFG, 0.002, None, None, t0, rf)    # 费用变了：重算
        assert len(calls) == 3 * len(D.SINGLE)
    finally:
        D._run = real
        D._COMPARE_MEMO = None


if __name__ == "__main__":
    test_memo_hit_identical_to_fresh()
    test_returned_compare_is_a_copy()
    test_bounded_and_invalidated_by_data_change()
    print("ok: 3 项通过")
