"""D108：持仓自动止损线。合成行情 + 注入成本 / 系数，不读写 data/、不联网。
- 固定止损 = 成本 × (1 − simtrade.STOP_LOSS)。
- 海龟线的状态与 strategies.donchian_s1 的仓位逐日一致（持有 ⇔ 仓位 > 0）；参数与注册表默认一致。
- 出场线的含义：用真实的明天 ATR 时，“明天收盘 < 线 ⇔ 策略明天出场”逐日成立；用今天 ATR 的近似线误差有界。
- 后复权换真实价：乘系数；系数取不到就不给策略线。只给名单里的持仓股。
运行：.venv/bin/python tests/test_stops_d108.py"""
import math
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import registry, simtrade, strategies  # noqa: E402
from src.market_monitor import stops as ST, watchlist as W  # noqa: E402


def synth(seed, n=600, drift=0.0006):
    rng = np.random.default_rng(seed)
    c = 20 * np.exp(np.cumsum(rng.normal(drift, 0.022, n)))
    o = c * np.exp(rng.normal(0, 0.005, n))
    return pd.DataFrame({"Open": o, "High": np.maximum(o, c) * (1 + rng.uniform(0, 0.02, n)),
                         "Low": np.minimum(o, c) * (1 - rng.uniform(0, 0.02, n)), "Close": c, "Volume": 1e6},
                        index=pd.bdate_range("2023-01-02", periods=n))


class Params(unittest.TestCase):
    def test_same_as_registry_and_simtrade(self):
        dflt = registry.clean_params("donchian_s1", {})
        for k in ("entry_len", "exit_len", "atr_mult"):
            self.assertEqual(dflt[k], ST.DONCHIAN[k], k)
        self.assertEqual(ST.fixed_stop_pct(), simtrade.STOP_LOSS)


class DonchianLine(unittest.TestCase):
    def test_state_matches_strategy(self):
        for seed in range(5):
            df = synth(seed)
            on, *_ = ST.donchian_state(df, **ST.DONCHIAN)
            w = strategies.donchian_s1(df, target_vol=None)
            self.assertEqual([bool(x) for x in on], list(w.to_numpy() > 0), seed)

    def test_line_is_exit_trigger(self):
        exits = stays = 0
        max_gap = 0.0
        for seed in range(8):
            df = synth(seed)
            on, peak, lo, a = ST.donchian_state(df, **ST.DONCHIAN)
            c = df["Close"].to_numpy()
            for t in range(60, len(df) - 1):
                if not on[t]:
                    continue
                d = ST.donchian_line(df.iloc[:t + 1], **ST.DONCHIAN)
                self.assertIsNotNone(d)
                self.assertAlmostEqual(d["channel_low"], lo[t + 1], places=10)          # 明天的通道下沿今天就确定
                exact = max(lo[t + 1], peak[t] - 2.0 * a[t + 1])                        # 用明天真实 ATR 的线
                self.assertEqual(c[t + 1] < exact, not on[t + 1], (seed, t))           # 跌破 ⇔ 出场
                max_gap = max(max_gap, abs(d["line"] - exact) / c[t])
                exits += not on[t + 1]; stays += on[t + 1]
            self.assertIsNone(ST.donchian_line(df.iloc[:15], **ST.DONCHIAN))          # 数据不够
        self.assertGreater(exits, 10); self.assertGreater(stays, 100)
        self.assertLess(max_gap, 0.01)                                                  # 近似线与精确线相差 < 1% 股价

    def test_not_holding_no_line(self):
        df = synth(1, drift=-0.003)
        on, *_ = ST.donchian_state(df, **ST.DONCHIAN)
        if not on[-1]:
            self.assertIsNone(ST.donchian_line(df, **ST.DONCHIAN))


class StopLines(unittest.TestCase):
    def find_holding(self):
        for seed in range(40):
            df = synth(seed)
            if ST.donchian_state(df, **ST.DONCHIAN)[0][-1]:
                return df
        self.skipTest("no holding series")

    def test_lines_and_units(self):
        df = self.find_holding()
        d = ST.donchian_line(df, **ST.DONCHIAN)
        out = ST.stop_lines(costs_fn=lambda: {"600001": 10.0, "600002": 8.0}, prices_fn=lambda code: df,
                            scale_fn=lambda code, x: 0.5 if code == "600001" else None)
        a = out["600001"]
        kinds = {x["kind"]: x for x in a["lines"]}
        self.assertEqual(kinds["fixed"]["price"], round(10.0 * (1 - simtrade.STOP_LOSS), 2))
        self.assertEqual(kinds["donchian_s1"]["price"], round(d["line"] * 0.5, 2))     # 后复权 × 系数 = 真实价
        self.assertEqual(a["nearest"]["price"], max(x["price"] for x in a["lines"]))
        self.assertEqual([x["kind"] for x in out["600002"]["lines"]], ["fixed"])       # 系数取不到：不给策略线
        for x in a["lines"]:
            self.assertEqual(x["basis"], "close")
            self.assertTrue(x["rule_en"].isascii() and x["action_en"].isascii())

    def test_errors_degrade(self):
        def boom(code):
            raise RuntimeError("x")
        out = ST.stop_lines(costs_fn=lambda: {"600001": 10.0, "bad": float("nan")}, prices_fn=boom, scale_fn=lambda c, d: 1.0)
        self.assertEqual(list(out), ["600001"])
        self.assertEqual(len(out["600001"]["lines"]), 1)
        self.assertEqual(ST.stop_lines(costs_fn=lambda: 1 / 0), {})

    def test_watchlist_only_holdings(self):
        fake = {"600000": {"lines": [], "nearest": None}, "000001": {"lines": [], "nearest": None}, "600519": {"lines": []}}
        plan = W.build_watchlist(5, holdings_fn=lambda: ["600000"], watch_fn=lambda: ["000001"], signals_fn=lambda: [],
                                 stops_fn=lambda: fake)
        self.assertEqual(list(plan["stops"]), ["600000"])                                 # 自选股、不在名单的都不给
        plan = W.build_watchlist(5, holdings_fn=lambda: ["600000"], watch_fn=lambda: [], signals_fn=lambda: [])
        self.assertEqual(plan["stops"], {})                                               # 注入假持仓时不读真实台账
        plan = W.build_watchlist(5, holdings_fn=lambda: ["600000"], watch_fn=lambda: [], signals_fn=lambda: [],
                                 stops_fn=lambda: 1 / 0)
        self.assertEqual(plan["stops"], {})


if __name__ == "__main__":
    unittest.main()
