"""策略检查：python tests/test_strategies.py

最重要的一条：没有未来函数。做法是把数据截断到第 t 天，重新算信号，
和用完整数据算出来的第 t 天信号必须一模一样。如果某个策略偷看了未来，这里就会失败。
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import data  # noqa: E402
from src import strategies as S  # noqa: E402
from src.portfolio import run_weights  # noqa: E402

DF = data.synthetic(n=1500, seed=3)
SINGLE = [S.trend_vol_target, S.donchian_breakout, S.rsi2_reversion,
          S.ma_adx_plus, S.ma_adx_original, S.trend_ensemble, S.donchian_s1]


def test_no_lookahead_single():
    for fn in SINGLE:
        full = fn(DF)
        for cut in [400, 700, 1000, 1499]:
            part = fn(DF.iloc[:cut + 1])
            assert np.isclose(part.iloc[-1], full.iloc[cut], equal_nan=True), \
                f"{fn.__name__} 在第 {cut} 天用到了未来数据"


def test_no_lookahead_dual_momentum():
    P = {s: data.synthetic(n=1500, seed=i) for i, s in enumerate(["SPY", "EFA", "IEF", "SHY"])}
    full = S.dual_momentum(P)
    for cut in [600, 900, 1499]:
        part = S.dual_momentum({s: d.iloc[:cut + 1] for s, d in P.items()})
        assert np.allclose(part.iloc[-1], full.iloc[cut]), f"dual_momentum 第 {cut} 天偷看未来"


def test_weights_in_range():
    for fn in SINGLE:
        w = fn(DF)
        assert w.min() >= 0 and w.max() <= 1.0 + 1e-9, fn.__name__


def test_engine_matches_buy_hold():
    """满仓、零成本时，引擎净值应该等于 收盘/第二天开盘 的买入持有。"""
    r = run_weights({"X": DF}, pd.Series(1.0, index=DF.index, name="X").to_frame(), cost=0)
    expected = DF.Close.iloc[-1] / DF.Open.iloc[1]
    assert np.isclose(r.equity.iloc[-1], expected)


def test_engine_executes_next_open():
    """第 t 天目标变 1，第 t+1 天才有持仓。"""
    tgt = pd.Series(0.0, index=DF.index, name="X")
    tgt.iloc[100:] = 1.0
    r = run_weights({"X": DF}, tgt.to_frame(), cost=0)
    assert r.weights.X.iloc[100] == 0 and r.weights.X.iloc[101] == 1
    assert np.isclose(r.returns.iloc[101], DF.Close.iloc[101] / DF.Open.iloc[101] - 1)


def test_cash_interest():
    """全程空仓时，净值应该等于现金复利。"""
    tgt = pd.Series(0.0, index=DF.index, name="X").to_frame()
    cash = pd.Series(0.0001, index=DF.index)
    r = run_weights({"X": DF}, tgt, cost=0, cash_ret=cash)
    assert np.isclose(r.equity.iloc[-1], 1.0001 ** len(DF))


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("ok", name)
