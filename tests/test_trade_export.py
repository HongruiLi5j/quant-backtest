"""「今日买卖」完整 Excel 导出测试：python3 tests/test_trade_export.py（需要 openpyxl）"""
import io, sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from openpyxl import load_workbook
from src import trade_export as X


def row(code, action, now, nxt, **kw):
    d = {"code": code, "name": "测" + code, "action": action, "now": now, "next": nxt, "close": 10.0, "d1": 1.5,
         "avg_turnover_20d": 2e8, "liquidity": "正常", "lot_cost": 1000.0, "flags": [], "reason": "原因",
         "performance": {"total": 12.0, "b_total": 30.0, "maxdd": -10.0, "trades": 12, "sharpe": 0.1, "small_sample": False}}
    d.update(kw)
    return d


PACK = {"strategy": "blend2", "label": "测试组合", "data_date": "2026-10-19", "generated_at": "2026-10-19T19:00:00+08:00",
        "counts": {"买入": 1, "卖出": 1, "加仓": 1, "减仓": 1}, "counts_main": {"买入": 1, "卖出": 1, "加仓": 1, "减仓": 1},
        "counts_small": {"买入": 0, "卖出": 0, "加仓": 0, "减仓": 0}, "limitations": ["局限一"], "disclaimer": "非投资建议", "performance_note": "口径",
        "price_unavailable": 0,
        "rows": [row("000001", "买入", 0.0, 0.4), row("000002", "卖出", 0.3, 0.0, avg_turnover_20d=None, liquidity="流动性未知"),
                 row("000003", "加仓", 0.2, 0.5, flags=["今日一字涨停，明天可能仍买不进"]), row("000004", "减仓", 0.5, 0.2, small_target=False),
                 {"code": "000005", "name": "无信号", "action": None, "now": 0.1, "next": 0.1}],
        "exceptions": [{"code": "000009", "name": "异常", "last_date": "2026-10-12", "now": 0.1, "next": 0.1, "flags": ["发布日停牌"], "reason": "数据异常"}]}


class T(unittest.TestCase):
    def setUp(self):
        self.wb = load_workbook(io.BytesIO(X.build_xlsx(PACK)))

    def test_all_four_actions_exported(self):
        ws = self.wb["全部信号"]
        acts = [ws.cell(row=r, column=3).value for r in range(4, ws.max_row + 1)]
        self.assertEqual(sorted(acts), ["买入", "减仓", "加仓", "卖出"])            # 不只是买入；没有信号的行不导出
        self.assertEqual(acts, ["卖出", "减仓", "买入", "加仓"])                       # 固定排序

    def test_sheets(self):
        self.assertEqual(self.wb.sheetnames, ["概览", "全部信号", "卖出", "减仓", "买入", "加仓", "数据异常", "说明与局限"])
        self.assertEqual(self.wb["买入"].max_row, 4)

    def test_values_and_units(self):
        ws = self.wb["全部信号"]
        sell = [r for r in range(4, 8) if ws.cell(row=r, column=1).value == "000002"][0]
        self.assertIsNone(ws.cell(row=sell, column=9).value)                           # 成交额缺失=空白，不是 0
        self.assertEqual(ws.cell(row=sell, column=10).value, "流动性未知")
        buy = [r for r in range(4, 8) if ws.cell(row=r, column=1).value == "000001"][0]
        self.assertAlmostEqual(ws.cell(row=buy, column=9).value, 2.0)                   # 2 亿元
        self.assertAlmostEqual(ws.cell(row=buy, column=8).value, 0.015)                # 1.5% 存为小数
        self.assertAlmostEqual(ws.cell(row=buy, column=6).value, 40.0)                 # 仓位变动 40 个百分点
        self.assertEqual(ws.cell(row=buy, column=3).fill.fgColor.rgb[-6:], "FCE4E4")

    def test_flags_and_filter(self):
        ws = self.wb["全部信号"]
        add = [r for r in range(4, 8) if ws.cell(row=r, column=1).value == "000003"][0]
        self.assertIn("一字涨停", ws.cell(row=add, column=13).value)
        self.assertTrue(ws.auto_filter.ref.startswith("A3"))

    def test_exceptions_and_notes(self):
        self.assertEqual(self.wb["数据异常"].cell(row=4, column=1).value, "000009")
        texts = [self.wb["说明与局限"].cell(row=r, column=2).value for r in range(3, 20)]
        self.assertTrue(any(t and "局限一" in t for t in texts))

    def test_empty_pack_does_not_crash(self):
        X.build_xlsx({"strategy": "x", "rows": [], "exceptions": []})


if __name__ == "__main__":
    unittest.main(verbosity=1)
