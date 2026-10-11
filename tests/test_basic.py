"""基本检查：python -m pytest tests  （或 python tests/test_basic.py）"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import backtest, data  # noqa: E402
from src.indicators import cross, wilder  # noqa: E402
from src.strategy import Params, generate_signals  # noqa: E402


def test_wilder_seed_and_recursion():
    s = pd.Series([1, 2, 3, 4, 5, 6], dtype=float)
    w = wilder(s, 3)
    assert np.isnan(w[1]) and w[2] == 2.0            # 种子 = (1+2+3)/3
    assert np.isclose(w[3], 2 + (4 - 2) / 3)         # 递推


def test_cross():
    a = pd.Series([1, 2, 3, 2, 1], dtype=float)
    b = pd.Series([2, 2, 2, 2, 2], dtype=float)
    assert cross(a, b).tolist() == [False, False, True, False, False]
    assert cross(b, a).tolist() == [False, False, False, False, True]


def test_no_lookahead_and_stops():
    df = data.synthetic(seed=1)
    p = Params()
    res = backtest.run(generate_signals(df, p), p)
    sig, tr = res.signals, res.trades
    assert len(tr) > 0
    for _, t in tr.iterrows():
        i = sig.index.get_loc(t.entry_date)
        assert sig.buy_sig.iloc[i - 1], "入场前一天必须有买入信号"
        assert t.entry_price == sig.Open.iloc[i], "按开盘价成交"
        if t.exit_reason == "stop":
            assert np.isclose(t.exit_price, t.stop)
    # 同一时间只有一笔持仓
    assert (tr.entry_date.iloc[1:].values > tr.exit_date.iloc[:-1].values).all()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("ok", name)
