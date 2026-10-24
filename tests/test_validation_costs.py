"""costs.py 单测(T007 / S2):只用合成数据,不读写任何文件。
运行:python3 -B tests/test_validation_costs.py
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.validation.costs import (CostParams, Trade, apply_costs, stress_table, lot_round,
                                  trade_fees, BLOCK_SUSPENDED, BLOCK_LIMIT_UP, BLOCK_LIMIT_DOWN,
                                  BLOCK_T1, BLOCK_LOT, BLOCK_NO_POSITION)

# 零滑点参数,便于手算
P0 = CostParams(slippage_bps=0.0, limit_extra_slippage_bps=0.0)


def tr(date, side, price, shares, **kw):
    return {"date": date, "side": side, "price": price, "shares": shares, "code": "600000", **kw}


class TestCosts(unittest.TestCase):
    def test_min_commission(self):
        # 1000 元 × 0.00025 = 0.25 < 5 → 取 5
        self.assertEqual(trade_fees(P0, "buy", 1000.0)["commission"], 5.0)
        # 大额:1,000,000 × 0.00025 = 250 > 5
        self.assertAlmostEqual(trade_fees(P0, "buy", 1e6)["commission"], 250.0)

    def test_stamp_only_on_sell(self):
        self.assertEqual(trade_fees(P0, "buy", 1e5)["stamp"], 0.0)
        self.assertAlmostEqual(trade_fees(P0, "sell", 1e5)["stamp"], 50.0)
        r = apply_costs([tr("2024-01-02", "buy", 10, 1000), tr("2024-01-03", "sell", 10, 1000)], P0)
        b, s = r.fills
        self.assertEqual(b.stamp, 0.0)
        self.assertAlmostEqual(s.stamp, 5.0)   # 10000 × 0.0005

    def test_hand_calc_round_trip(self):
        r = apply_costs([tr("2024-01-02", "buy", 10, 1000), tr("2024-01-03", "sell", 11, 1000)], P0)
        buy_fee = 5.0 + 10000 * 1e-5                      # 最低佣金 + 过户
        sell_fee = 5.0 + 11000 * 1e-5 + 11000 * 0.0005    # 最低佣金 + 过户 + 印花
        self.assertAlmostEqual(r.gross_pnl, 1000.0)
        self.assertAlmostEqual(r.net_pnl, 1000.0 - buy_fee - sell_fee)
        self.assertAlmostEqual(r.total_fees, buy_fee + sell_fee)

    def test_slippage_direction(self):
        p = CostParams(slippage_bps=10.0)
        r = apply_costs([tr("2024-01-02", "buy", 10, 100), tr("2024-01-03", "sell", 10, 100)], p)
        self.assertAlmostEqual(r.fills[0].exec_price, 10.01)
        self.assertAlmostEqual(r.fills[1].exec_price, 9.99)
        self.assertAlmostEqual(r.total_slippage, 0.01 * 100 * 2)

    def test_near_limit_extra_slippage(self):
        p = CostParams(slippage_bps=5.0, limit_extra_slippage_bps=20.0)
        r = apply_costs([tr("2024-01-02", "buy", 10, 100, near_limit=True)], p)
        self.assertAlmostEqual(r.fills[0].exec_price, 10 * (1 + 25e-4))

    def test_t1_blocks_same_day_sell(self):
        r = apply_costs([tr("2024-01-02", "buy", 10, 1000), tr("2024-01-02", "sell", 10, 1000)], P0)
        self.assertTrue(r.fills[0].executed)
        self.assertFalse(r.fills[1].executed)
        self.assertEqual(r.fills[1].blocked_reason, BLOCK_T1)
        # 关闭 T+1 后可成交
        r2 = apply_costs([tr("2024-01-02", "buy", 10, 1000), tr("2024-01-02", "sell", 10, 1000)],
                         CostParams(slippage_bps=0, enforce_t1=False))
        self.assertTrue(r2.fills[1].executed)
        # 次日可卖
        r3 = apply_costs([tr("2024-01-02", "buy", 10, 1000), tr("2024-01-03", "sell", 10, 1000)], P0)
        self.assertTrue(r3.fills[1].executed)

    def test_t1_partial_only_settled(self):
        # 周一买 1000,周二加买 500 再想卖 1500:只能卖周一那 1000
        r = apply_costs([tr("2024-01-02", "buy", 10, 1000), tr("2024-01-03", "buy", 10, 500),
                         tr("2024-01-03", "sell", 10, 1500)], P0)
        self.assertEqual(r.fills[2].shares, 1000)

    def test_sell_without_position(self):
        r = apply_costs([tr("2024-01-02", "sell", 10, 100)], P0)
        self.assertEqual(r.fills[0].blocked_reason, BLOCK_NO_POSITION)

    def test_limit_blocks(self):
        r = apply_costs([tr("2024-01-02", "buy", 10, 100, limit_up=True),
                         tr("2024-01-02", "buy", 10, 100),
                         tr("2024-01-03", "sell", 10, 100, limit_down=True),
                         tr("2024-01-04", "sell", 10, 100)], P0)
        self.assertEqual(r.fills[0].blocked_reason, BLOCK_LIMIT_UP)
        self.assertTrue(r.fills[1].executed)
        self.assertEqual(r.fills[2].blocked_reason, BLOCK_LIMIT_DOWN)
        self.assertTrue(r.fills[3].executed)   # 持仓未被被拦截的卖单改变
        # 涨停不拦卖、跌停不拦买
        r2 = apply_costs([tr("2024-01-02", "buy", 10, 100, limit_down=True),
                          tr("2024-01-03", "sell", 10, 100, limit_up=True)], P0)
        self.assertTrue(all(f.executed for f in r2.fills))

    def test_suspension_blocks_both_sides(self):
        r = apply_costs([tr("2024-01-02", "buy", 10, 100, suspended=True),
                         tr("2024-01-03", "buy", 10, 100),
                         tr("2024-01-04", "sell", 10, 100, suspended=True)], P0)
        self.assertEqual(r.fills[0].blocked_reason, BLOCK_SUSPENDED)
        self.assertEqual(r.fills[2].blocked_reason, BLOCK_SUSPENDED)
        self.assertEqual(r.n_blocked, 2)

    def test_lot_rounding(self):
        self.assertEqual(lot_round(250), 200)
        self.assertEqual(lot_round(99), 0)
        self.assertEqual(lot_round(-5), 0)
        self.assertEqual(lot_round(250, 10), 250)
        r = apply_costs([tr("2024-01-02", "buy", 10, 250), tr("2024-01-02", "buy", 10, 50)], P0)
        self.assertEqual(r.fills[0].shares, 200)
        self.assertEqual(r.fills[1].blocked_reason, BLOCK_LOT)
        self.assertEqual(r.fills[1].total_fees, 0.0)

    def test_odd_lot_allowed_when_liquidating(self):
        r = apply_costs([tr("2024-01-02", "buy", 10, 200), tr("2024-01-03", "buy", 10, 30),
                         tr("2024-01-04", "sell", 10, 230)], P0)
        self.assertEqual(r.fills[1].blocked_reason, BLOCK_LOT)   # 买入零股被拒
        self.assertEqual(r.fills[2].shares, 200)                 # 持仓 200,全部卖出
        r2 = apply_costs([tr("2024-01-02", "buy", 10, 300), tr("2024-01-03", "sell", 10, 150)], P0)
        self.assertEqual(r2.fills[1].shares, 100)                # 非清仓取整到整手

    def test_costs_monotone(self):
        trades = [tr("2024-01-02", "buy", 10, 1000), tr("2024-01-03", "sell", 11, 1000),
                  tr("2024-01-04", "buy", 12, 500), tr("2024-01-05", "sell", 11.5, 500)]
        prev = None
        for s in (0, 5, 10, 30, 100):
            n = apply_costs(trades, CostParams(slippage_bps=s)).net_pnl
            if prev is not None:
                self.assertLess(n, prev)
            prev = n
        prev = None
        for c in (0.0, 0.00025, 0.001, 0.003):
            n = apply_costs(trades, CostParams(commission_rate=c)).net_pnl
            if prev is not None:
                self.assertLessEqual(n, prev)
            prev = n
        rows = stress_table(trades, {"slippage_bps": [0, 5, 20], "commission_rate": [0.00025, 0.001]})
        self.assertEqual(len(rows), 6)
        for c in (0.00025, 0.001):
            nets = [r["net_pnl"] for r in rows if r["commission_rate"] == c]
            self.assertEqual(nets, sorted(nets, reverse=True))
        for s in (0, 5, 20):
            nets = [r["net_pnl"] for r in rows if r["slippage_bps"] == s]
            self.assertGreaterEqual(nets[0], nets[1])
        # 表中格点与直接计算一致
        direct = apply_costs(trades, CostParams(commission_rate=0.001, slippage_bps=20)).net_pnl
        self.assertAlmostEqual(rows[-1]["net_pnl"], direct)

    def test_empty(self):
        r = apply_costs([])
        self.assertEqual((r.net_pnl, r.gross_pnl, r.n_executed, r.n_blocked, r.total_cost),
                         (0.0, 0.0, 0, 0, 0.0))
        rows = stress_table([], {"slippage_bps": [0, 10]})
        self.assertEqual([x["net_pnl"] for x in rows], [0.0, 0.0])

    def test_dataclass_input_and_order(self):
        # 输入乱序也按日期处理;Trade 与 dict 等价
        a = [Trade("2024-01-03", "sell", 11, 1000, "X"), Trade("2024-01-02", "buy", 10, 1000, "X")]
        r = apply_costs(a, P0)
        self.assertTrue(all(f.executed for f in r.fills))
        self.assertAlmostEqual(r.gross_pnl, 1000.0)

    def test_invalid_inputs(self):
        bad = [tr("2024-01-02", "hold", 10, 100), tr("2024-01-02", "buy", 0, 100),
               tr("2024-01-02", "buy", 10, 0), {"date": "2024-01-02"}, 5,
               dict(tr("2024-01-02", "buy", 10, 100), foo=1)]
        for b in bad:
            with self.assertRaises(ValueError):
                apply_costs([b])
        with self.assertRaises(ValueError):
            CostParams(slippage_bps=-1)
        with self.assertRaises(ValueError):
            CostParams(lot=0)
        with self.assertRaises(ValueError):
            stress_table([], {"bogus": [1]})


if __name__ == "__main__":
    unittest.main()
