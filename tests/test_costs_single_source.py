"""费率常量"只保留一份"的防回归测试(B14):只读源码、做 AST 扫描与签名检查，不读写 data/ 下任何文件，不联网。
运行:.venv/bin/python tests/test_costs_single_source.py

B03 把费率搬进了 src/costs.py;这里防止以后有人又在别处手写一份(手写的数字改一处漏一处，
回测、模拟盘、持仓页的费用就会悄悄不一致)。

扫描对象:src/、scripts/、config.py、app.py、daily.py 里所有"等于已知费率"的数字字面量
(包括 Decimal("0.00025") 这种写成字符串的)。允许出现的只有下面两类，且都写明了原因。
"""
import ast
import inspect
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import costs  # noqa: E402
from src.validation import adapter  # noqa: E402

# 已知费率:佣金万 2.5、ETF 佣金万 1.5、过户费十万分之一、个股单边 8bp、ETF/引擎默认 0.0005(印花税现行税率也是它)
WATCHED = {0.00025, 0.00015, 0.00001, 0.0008, 0.0005}

# 允许保留自己一份字面量的文件(路径相对项目根),原因:
ALLOWED = {
    "src/costs.py": "单一来源本身",
    "src/replay/engine.py": "T008 回放内置简版(仅 import 失败时降级用);红线目录不能改,由 tests/test_costs.py 核对没有漂移",
    "scripts/audit_recompute.py": "独立复算脚本,设计上不 import 引擎/项目计算代码以保持独立;值必须等于 afl 的 0.05%",
    "scripts/engine_rule_diff.py": "C09 合成行情的日均漂移 0.0008,不是费率(数值碰巧相同)",
}


def _literals(path: Path):
    """返回文件中等于 WATCHED 的字面量 [(行号, 原值)]。字符串只在能转成 float 时才算。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Constant):
            continue
        v = n.value
        if isinstance(v, str):
            try:
                v = float(v)
            except ValueError:
                continue
        if isinstance(v, float) and v in WATCHED:
            hits.append((n.lineno, n.value))
    return hits


def _scan_files():
    files = [*ROOT.glob("src/**/*.py"), *ROOT.glob("scripts/**/*.py"),
             ROOT / "config.py", ROOT / "app.py", ROOT / "daily.py"]
    return sorted(f for f in files if f.is_file())


class TestNoDuplicatedFeeLiterals(unittest.TestCase):
    def test_no_fee_literals_outside_allowlist(self):
        bad = []
        for f in _scan_files():
            rel = f.relative_to(ROOT).as_posix()
            if rel in ALLOWED:
                continue
            for line, val in _literals(f):
                bad.append(f"{rel}:{line} {val!r}")
        self.assertEqual(bad, [], "费率请从 src/costs.py 引用，不要再手写一份:\n" + "\n".join(bad))

    def test_allowlist_entries_are_real(self):
        # 白名单里的文件如果被删了或不再有字面量，就该把它从白名单去掉，避免白名单越攒越多
        for rel in ALLOWED:
            f = ROOT / rel
            self.assertTrue(f.is_file(), rel)
            self.assertTrue(_literals(f), f"{rel} 已没有费率字面量，请从 ALLOWED 去掉")


class TestAdapterDefaults(unittest.TestCase):
    """B14 把 validation.adapter 两个函数 cost=0.0005 的默认值改成读 costs.ENGINE_DEFAULT_ONE_WAY。"""

    def test_from_strategy_default_cost(self):
        d = inspect.signature(adapter.from_strategy).parameters["cost"].default
        self.assertEqual(d, costs.ENGINE_DEFAULT_ONE_WAY)
        self.assertEqual(d, 0.0005)          # 数值没变

    def test_rerun_with_held_default_cost(self):
        d = inspect.signature(adapter.rerun_with_held).parameters["cost"].default
        self.assertEqual(d, costs.ENGINE_DEFAULT_ONE_WAY)
        self.assertEqual(d, 0.0005)

    def test_default_matches_portfolio_run_weights(self):
        from src import portfolio
        d = inspect.signature(portfolio.run_weights).parameters["cost"].default
        self.assertEqual(d, inspect.signature(adapter.from_strategy).parameters["cost"].default)


class TestSimAndHoldingsShareSource(unittest.TestCase):
    """模拟盘(simtrade)、持仓默认估算(holdings.calc)、会员模拟盘(经 config 的 bp)读的是同一份。"""

    def test_simtrade_fee_table_is_from_costs(self):
        from src import simtrade
        s = simtrade.FEE["stock"]
        self.assertEqual((s["commission_rate"], s["min_commission"], s["transfer_rate"], s["stamp_rate"]),
                         (costs.COMMISSION_RATE, costs.MIN_COMMISSION, costs.TRANSFER_RATE, costs.STAMP_DUTY_RATE))
        self.assertEqual(simtrade.FEE["etf"]["commission_rate"], costs.ETF_COMMISSION_RATE)

    def test_holdings_default_estimate_uses_costs_for_a_sell(self):
        from decimal import Decimal
        from src.holdings import calc
        # 沪市个股卖出 100 万:佣金 250 + 过户 10 + 印花 500 = 760(与 costs 的费率逐项对得上)
        amount = Decimal(1_000_000)
        expect = (amount * Decimal(str(costs.COMMISSION_RATE)) + amount * Decimal(str(costs.TRANSFER_RATE))
                  + amount * Decimal(str(costs.STAMP_DUTY_RATE)))
        self.assertEqual(calc.default_fee_estimate("600000", "sell", amount), calc.q2(expect))
        self.assertEqual(calc.default_fee_estimate("600000", "sell", amount), Decimal("760.00"))

    def test_member_account_fee_bp_comes_from_config(self):
        import config
        self.assertEqual(config.MARKETS["cn"]["stock_cost_bp"], costs.CN_STOCK_ONE_WAY_BP)
        self.assertEqual(config.MARKETS["cn"]["cost_bp"], costs.CN_ETF_ONE_WAY_BP)
        self.assertEqual(config.MARKETS["us"]["stock_cost_bp"], costs.US_STOCK_ONE_WAY_BP)
        self.assertEqual(config.MARKETS["us"]["cost_bp"], costs.US_ETF_ONE_WAY_BP)
        # app.py 给会员下单的 fee_bp 来自 MARKETS,不是自己写的数:源码里要有这个取法
        src = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn('mcfg.get("stock_cost_bp", mcfg["cost_bp"])', src)


if __name__ == "__main__":
    unittest.main()
