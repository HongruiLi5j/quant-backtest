"""src/costs.py 单一来源的单测(B03):只做常量与纯函数检查，不读写 data/ 下任何文件，不联网。
运行:.venv/bin/python tests/test_costs.py

两类检查:
  1. 钉死数值:src/costs.py 里每个数都等于抽取之前各处的字面量(本次抽取不改数值)。以后要改费率，
     请同时改这里并在 docs/cost_model.md 写明原因，这样不会无声无息地改变回测结果。
  2. 一致性:每个使用方读到的都是 src/costs.py 的值;engines/common.py 与 src/replay 的内置简版不在
     本补丁范围(不能改)，这里只核对它们和单一来源没有漂移。
"""
import ast
import sys
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from src import backtest, costs, portfolio, scan, scan_us, signal_ledger, simtrade  # noqa: E402
from src.holdings import calc  # noqa: E402
from src.replay import engine as replay_engine  # noqa: E402
from src.validation import costs as vcosts  # noqa: E402
from engines import common as engines_common  # noqa: E402


class TestPinnedValues(unittest.TestCase):
    """第 1 类:数值钉死。"""

    def test_one_way_bp(self):
        self.assertEqual((costs.CN_STOCK_ONE_WAY_BP, costs.CN_ETF_ONE_WAY_BP,
                          costs.US_STOCK_ONE_WAY_BP, costs.US_ETF_ONE_WAY_BP), (8, 5, 10, 5))

    def test_one_way_fraction_equals_old_literals(self):
        # bp / 1e4 与原来直接写的小数字面量必须是同一个 float(逐位相等，不是近似)
        self.assertEqual(costs.CN_STOCK_ONE_WAY, 0.0008)
        self.assertEqual(costs.CN_ETF_ONE_WAY, 0.0005)
        self.assertEqual(costs.US_STOCK_ONE_WAY, 0.0010)
        self.assertEqual(costs.ENGINE_DEFAULT_ONE_WAY, 0.0005)
        self.assertEqual(costs.CN_STOCK_ROUND_TRIP_BP, 16)

    def test_itemized_fees(self):
        self.assertEqual(costs.COMMISSION_RATE, 0.00025)
        self.assertEqual(costs.ETF_COMMISSION_RATE, 0.00015)
        self.assertEqual(costs.MIN_COMMISSION, 5.0)
        self.assertEqual(costs.TRANSFER_RATE, 0.00001)
        self.assertEqual(costs.STAMP_DUTY_RATE, 0.0005)
        self.assertEqual(costs.STAMP_DUTY_RATE_BEFORE_CHANGE, 0.001)
        self.assertEqual(costs.STAMP_DUTY_CHANGE_DATE, date(2023, 8, 28))
        self.assertEqual((costs.SLIPPAGE_BPS, costs.LIMIT_EXTRA_SLIPPAGE_BPS), (5.0, 20.0))
        self.assertEqual(costs.AMIBROKER_COMMISSION_PCT, 0.0005)


class TestConsumersReadTheSource(unittest.TestCase):
    """第 2 类:使用方读到的就是单一来源的值。"""

    def test_config_markets(self):
        cn, us = config.MARKETS["cn"], config.MARKETS["us"]
        self.assertEqual((cn["cost_bp"], cn["stock_cost_bp"]), (5, 8))
        self.assertEqual((us["cost_bp"], us["stock_cost_bp"]), (5, 10))
        self.assertIs(type(cn["stock_cost_bp"]), int)    # 仍是 int(下游 f"{cost_bp:g}" 之类的输出不变)

    def test_scan_constants(self):
        self.assertEqual(scan.COST, 0.0008)
        self.assertEqual(scan_us.COST, 0.0010)
        self.assertEqual(scan.COST, config.MARKETS["cn"]["stock_cost_bp"] / 1e4)    # 与 config 口径一致
        self.assertEqual(scan_us.COST, config.MARKETS["us"]["stock_cost_bp"] / 1e4)

    def test_signal_ledger_round_trip_is_twice_one_way(self):
        self.assertEqual(signal_ledger.ROUND_TRIP_BP, 2 * config.MARKETS["cn"]["stock_cost_bp"])

    def test_default_args(self):
        import inspect
        self.assertEqual(inspect.signature(portfolio.run_weights).parameters["cost"].default, 0.0005)
        self.assertEqual(backtest.Config().commission_pct, 0.0005)

    def test_simtrade_fee_table(self):
        self.assertEqual(simtrade.FEE, {
            "stock": {"commission_rate": 0.00025, "min_commission": 5.0, "transfer_rate": 0.00001, "stamp_rate": 0.0005},
            "etf": {"commission_rate": 0.00015, "min_commission": 5.0, "transfer_rate": 0.0, "stamp_rate": 0.0},
        })
        self.assertFalse(simtrade.FEE_VERIFIED)      # 抽常量不等于核实费率

    def test_validation_cost_params_defaults(self):
        p = vcosts.CostParams()
        self.assertEqual((p.commission_rate, p.min_commission, p.transfer_rate, p.stamp_rate),
                         (0.00025, 5.0, 0.00001, 0.0005))
        self.assertEqual((p.slippage_bps, p.limit_extra_slippage_bps), (5.0, 20.0))

    def test_replay_cost_params_inherit_same_defaults(self):
        self.assertTrue(replay_engine.COST_SOURCE.startswith("validation.costs"))   # 走的是 validation.costs，不是内置简版
        a, b = replay_engine.CostParams(), vcosts.CostParams()
        for f in ("commission_rate", "min_commission", "transfer_rate", "stamp_rate", "slippage_bps"):
            self.assertEqual(getattr(a, f), getattr(b, f), f)

    def test_replay_fallback_literals_have_not_drifted(self):
        """src/replay/engine.py 的内置简版(T007 缺失时才用)在红线目录里不能改，这里用 ast 读出它的默认值核对。"""
        tree = ast.parse((ROOT / "src" / "replay" / "engine.py").read_text(encoding="utf-8"))
        fallback = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "_BaseCostParams"]
        self.assertEqual(len(fallback), 1)
        vals = {s.target.id: ast.literal_eval(s.value) for s in fallback[0].body
                if isinstance(s, ast.AnnAssign) and isinstance(s.value, ast.Constant)}
        self.assertEqual(vals["commission_rate"], costs.COMMISSION_RATE)
        self.assertEqual(vals["min_commission"], costs.MIN_COMMISSION)
        self.assertEqual(vals["transfer_rate"], costs.TRANSFER_RATE)
        self.assertEqual(vals["stamp_rate"], costs.STAMP_DUTY_RATE)
        self.assertEqual(vals["slippage_bps"], costs.SLIPPAGE_BPS)

    def test_engines_common_constants_equal_source(self):
        self.assertEqual(engines_common.COMMISSION, costs.COMMISSION_RATE)
        self.assertEqual(engines_common.TRANSFER_FEE, costs.TRANSFER_RATE)
        self.assertEqual(engines_common.MIN_COMMISSION, costs.MIN_COMMISSION)
        self.assertEqual(engines_common.STAMP_CHANGE, costs.STAMP_DUTY_CHANGE_DATE)
        # 外部引擎按成交日分段:前一天 0.1%，当天起 0.05%;src 侧各模块固定用 0.05%
        self.assertEqual(engines_common.stamp_duty(date(2023, 8, 27)), costs.STAMP_DUTY_RATE_BEFORE_CHANGE)
        self.assertEqual(engines_common.stamp_duty(date(2023, 8, 28)), costs.STAMP_DUTY_RATE)


class TestHoldingsDecimalEquivalence(unittest.TestCase):
    """holdings/calc 改成 Decimal(str(float)) 之后，与原来的字符串字面量逐值相等。"""

    @staticmethod
    def old_formula(code, side, amount):
        amount = Decimal(amount)
        fee = max(amount * Decimal("0.00025"), Decimal(5))
        if code[:1] in ("5", "6"):
            fee += amount * Decimal("0.00001")
        is_etf = code[:2] in ("51", "56", "58") or code.startswith("159")
        if side == "sell" and not is_etf:
            fee += amount * Decimal("0.0005")
        return calc.q2(fee)

    def test_same_as_original_formula(self):
        n = 0
        for code in ("600531", "000001", "300750", "688981", "510300", "159915", "511880", "588000"):
            for side in ("buy", "sell"):
                for amt in ("0.01", "100", "1000", "19999.99", "20000", "20000.01", "123456.78", "1000000", "50000000.55"):
                    self.assertEqual(calc.default_fee_estimate(code, side, amt), self.old_formula(code, side, amt),
                                     (code, side, amt))
                    n += 1
        self.assertEqual(n, 8 * 2 * 9)

    def test_decimal_str_of_constants_is_exact(self):
        self.assertEqual(Decimal(str(costs.COMMISSION_RATE)), Decimal("0.00025"))
        self.assertEqual(Decimal(str(costs.TRANSFER_RATE)), Decimal("0.00001"))
        self.assertEqual(Decimal(str(costs.STAMP_DUTY_RATE)), Decimal("0.0005"))
        self.assertEqual(Decimal(str(costs.MIN_COMMISSION)), Decimal(5))


class TestSourceModuleIsLeaf(unittest.TestCase):
    def test_costs_imports_nothing_from_project(self):
        """src/costs.py 不能 import 项目内模块，否则 config.py 引用它会形成循环导入。"""
        tree = ast.parse((ROOT / "src" / "costs.py").read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom):
                self.assertEqual(n.level, 0)
                self.assertIn(n.module, ("__future__", "datetime"))
            elif isinstance(n, ast.Import):
                self.assertTrue(all(a.name in ("datetime",) for a in n.names))


if __name__ == "__main__":
    unittest.main()
