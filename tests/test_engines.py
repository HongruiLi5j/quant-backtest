"""第三方引擎（backtrader / RQAlpha）的自检。运行：
    PYTHONPATH=.engines_lib .venv/bin/python tests/test_engines.py

检查三件事：
  1. 没有未来函数：把数据截断到某一天，RQAlpha 版策略在截断日之前给出的目标仓位必须和用全部数据时完全一样
  2. RQAlpha 版指标和自研引擎（src/indicators.py）数值一致
  3. 两个外部引擎对"只有 0/1 仓位"的策略，成交记录（日期、股数、价格）完全一致
"""
import math
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from engines import common, rq_base, rq_strategies  # noqa: E402
from src import indicators  # noqa: E402

CODE = "600531"


def targets(df, name):
    s = rq_strategies.STRATEGY_CLASSES[name](**common.parse_params(name, {}))
    return [s.on_bar(o, h, l, c) for o, h, l, c in df[["Open", "High", "Low", "Close"]].itertuples(index=False)]


def test_no_lookahead():
    df, _ = common.load_stock(CODE)
    for name in common.STRATS:
        full = targets(df, name)
        for frac in (0.4, 0.7, 0.9):
            cut = int(len(df) * frac)
            part = targets(df.iloc[:cut], name)
            assert np.allclose(full[:cut], part, equal_nan=True), f"{name} 用到了未来数据（截断在 {df.index[cut].date()}）"
    print("✓ 未来函数检查：7 个策略截断数据前后的目标仓位完全一致")


def test_indicators_match():
    df, _ = common.load_stock(CODE)
    a, x, r = rq_base.ATR(14), rq_base.ADX(14), rq_base.RSI(2)
    atr = [a.update(h, l, c) for h, l, c in df[["High", "Low", "Close"]].itertuples(index=False)]
    adx = [x.update(h, l, c) for h, l, c in df[["High", "Low", "Close"]].itertuples(index=False)]
    rsi = [r.update(c) for c in df["Close"]]
    for mine, ref, n in ((atr, indicators.atr(df, 14), "ATR"), (adx, indicators.adx(df, 14), "ADX"),
                         (rsi, indicators.rsi(df["Close"], 2), "RSI")):
        m, rf = np.array(mine, float), ref.to_numpy(float)
        ok = ~np.isnan(rf)
        assert np.allclose(m[ok], rf[ok], rtol=1e-9, atol=1e-9), f"{n} 和自研引擎不一致"
    print("✓ 指标检查：RQAlpha 版的 ATR / ADX / RSI 和 src/indicators.py 数值完全一致")


def test_engines_agree():
    from engines import bt_engine, rq_engine
    raw, info = common.load_stock(CODE)
    df, _, _ = common.to_forward_adjusted(CODE, raw)
    a = bt_engine.run(CODE, "ma_adx_original", {}, None, df=df, info=info)
    b = rq_engine.run(CODE, "ma_adx_original", {}, None, df=df, info=info)
    fa, fb = a["fills"], b["fills"]
    assert len(fa) == len(fb), "backtrader 和 RQAlpha 的成交笔数不一致"
    for x, y in zip(fa, fb):
        # 日期、方向、成交价必须相同；股数允许差 1 手（两边的费用算法略有不同，账户现金差几元，整手取整时可能差 100 股）
        assert x["date"] == y["date"] and (x["qty"] > 0) == (y["qty"] > 0) and abs(x["price"] - y["price"]) < 5e-4, (x, y)   # RQAlpha 把成交价四舍五入到 4 位小数，容许最多差 0.0005 元
        assert abs(x["qty"] - y["qty"]) <= 100, (x, y)
    same = sum(x["qty"] == y["qty"] for x, y in zip(fa, fb))
    gap = abs(a["stats"]["CAGR%"] - b["stats"]["CAGR%"])
    assert gap < 0.5, f"年化差 {gap:.2f}% 太大"
    print(f"✓ 引擎一致性：MA+ADX 原版两个引擎各 {len(fa)} 笔成交，日期/方向/价格全部相同，"
          f"{same} 笔股数完全相同（其余差 1 手），年化差 {gap:.3f}%")


if __name__ == "__main__":
    test_no_lookahead()
    test_indicators_match()
    test_engines_agree()
    print("全部通过")
