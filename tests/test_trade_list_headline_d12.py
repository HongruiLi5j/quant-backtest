"""D12：信号表一句话触发原因 headline()。
1) 性质测试（合成随机游走价格，多个截断点）：单策略的 headline 说「入场 / 退出」当且仅当既有策略函数的状态昨天→今天真的变了，
   而且入场 / 退出方向和状态变化方向一致——保证这句话不会和策略本身对不上。
2) 组合策略：用替身子策略检查只列出变化的子策略、加权结果和「都没变」时的说法。只用内存数据，不读写 data/。"""
import sys
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src import trade_list  # noqa: E402

EPS = trade_list.EPS


def walk(seed, n=700):
    rng = np.random.default_rng(seed)
    close = 20 * np.exp(np.cumsum(rng.normal(0, 0.025, n)))
    idx = pd.bdate_range("2020-01-01", periods=n)
    hi, lo = close * (1 + rng.uniform(0, 0.02, n)), close * (1 - rng.uniform(0, 0.02, n))
    return pd.DataFrame({"Open": close, "High": hi, "Low": lo, "Close": close, "Volume": 1e6}, index=idx)


def state(strategy, df):
    p = trade_list._parameters(strategy, None)
    if strategy == "rsi2":
        return trade_list.strategies.rsi2_reversion(df, **p)
    if strategy == "donchian_s1":
        return trade_list.strategies.donchian_s1(df, **{**p, "target_vol": None})
    return trade_list.strategies.ma_adx_original(df, **p)


class HeadlineMatchesStateTests(unittest.TestCase):
    def test_single_strategies(self):
        checked = {"入场": 0, "退出": 0}
        for seed in range(3):
            full = walk(seed)
            for end in range(300, len(full), 7):
                df = full.iloc[:end]
                for strategy in ("rsi2", "donchian_s1", "ma_adx_original"):
                    st = state(strategy, df)
                    was, now = bool(st.iloc[-2] > EPS), bool(st.iloc[-1] > EPS)
                    text = trade_list.headline(strategy, df)
                    self.assertEqual(text.startswith("入场"), (not was) and now, (strategy, end, text))
                    self.assertEqual(text.startswith("退出"), was and not now, (strategy, end, text))
                    for k in checked:
                        checked[k] += text.startswith(k)
        self.assertGreater(checked["入场"], 0)   # 合成数据里确实出现过入场和退出，测试不是空转
        self.assertGreater(checked["退出"], 0)


class BlendHeadlineTests(unittest.TestCase):
    def fake(self, prev, now):
        return lambda df: pd.Series([prev, now], index=df.index[-2:])

    def run_blend(self, comps):
        df = walk(0, 300)
        funcs = dict(trade_list.STRATEGY_FUNCTIONS)
        funcs.update({k: self.fake(*v) for k, v in comps.items()})
        with mock.patch.object(trade_list, "STRATEGY_FUNCTIONS", funcs), \
                mock.patch.object(trade_list, "_trigger", return_value="退出：示例条件"):
            return trade_list.headline("blend2", df)

    def test_only_changed_components_listed(self):
        text = self.run_blend({"trend_ens": (0.0, 0.0), "rsi2": (1.0, 0.0), "donchian_s1": (0.6, 0.6)})
        self.assertIn("RSI2 100.00%→0.00%：退出：示例条件", text)
        self.assertNotIn("趋势集成", text)
        self.assertNotIn("海龟 60", text)
        self.assertIn("加权目标 20.00%", text)   # (0 + 0 + 0.6) / 3

    def test_nothing_changed(self):
        text = self.run_blend({"trend_ens": (0.5, 0.5), "rsi2": (0.0, 0.0), "donchian_s1": (0.0, 0.0)})
        self.assertTrue(text.startswith("子策略目标与昨日相同"), text)


if __name__ == "__main__":
    unittest.main()
