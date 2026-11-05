"""D69 第二部分：页面 ↔ 接口字段契约（静态检查，不启动服务、不读 data/、不导入 app.py）。

为什么要有：今晚的问题大多出在「前端读的字段」和「后端写的字段」对不上（例如 D12 的 trigger 只在新清单里有）。
这个测试把新界面脚本里读到的行字段抽出来，要求后端生成这些行的代码里确实写了同名键；
有人在后端改名 / 删字段而没改前端（或反过来），这里会直接报出是哪个字段。

检查方式：前端用 `r.<字段>` / `r.performance?.<字段>` 读行；后端用 `"字段": …`、`["字段"] = …` 或 `字段=…`（row.update 关键字）写。
运行：.venv/bin/python tests/test_page_api_contract.py
"""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "dashboard" / "ui"

# 前端脚本 → (行变量名, 后端写这些行的源文件, 不是数据字段的名字)
CONTRACTS = {
    "信号表 /api/trade_list 每行": (
        UI / "pages" / "signals-table.js", "r",
        [ROOT / "src" / "trade_list.py", ROOT / "src" / "turnover.py"],
        {"tr"},                     # r.tr 是表格行 DOM 节点，不是接口字段
    ),
}
# 行里嵌套的 performance 对象（样本外 / 2020 年起的统计）
NESTED = {
    "信号表 performance": (UI / "pages" / "signals-table.js", r"performance\?*\.", [ROOT / "src" / "trade_list.py"]),
}


def js_fields(js: Path, var: str) -> set[str]:
    text = js.read_text(encoding="utf-8")
    text = re.sub(r"//[^\n]*", "", text)                     # 去掉注释里的 r.xxx
    return set(re.findall(rf"(?<![\w.$]){re.escape(var)}\.([a-zA-Z_][a-zA-Z0-9_]*)", text))


def written_in(field: str, sources: list[Path]) -> bool:
    pat = re.compile(rf"""["']{re.escape(field)}["']\s*[:\]]|\b{re.escape(field)}\s*=(?!=)""")
    return any(pat.search(p.read_text(encoding="utf-8")) for p in sources)


class PageApiContract(unittest.TestCase):
    def test_row_fields_read_by_pages_are_written_by_backend(self):
        for name, (js, var, sources, ignore) in CONTRACTS.items():
            fields = js_fields(js, var) - ignore
            self.assertGreaterEqual(len(fields), 10, f"{name}: 前端字段抽取太少，正则可能要跟着改")
            missing = sorted(f for f in fields if not written_in(f, sources))
            self.assertEqual(missing, [], f"{name}: 前端读了、后端没写的字段 {missing}")

    def test_nested_performance_fields(self):
        for name, (js, prefix, sources) in NESTED.items():
            text = re.sub(r"//[^\n]*", "", js.read_text(encoding="utf-8"))
            fields = set(re.findall(prefix + r"([a-zA-Z_][a-zA-Z0-9_]*)", text))
            self.assertGreaterEqual(len(fields), 3, f"{name}: 抽取太少")
            missing = sorted(f for f in fields if not written_in(f, sources))
            self.assertEqual(missing, [], f"{name}: 前端读了、后端没写的字段 {missing}")

    def test_extractor_catches_a_renamed_field(self):
        """自检：后端把一个字段整体改名后，契约必须报出来（防止正则永远匹配成功）。"""
        import tempfile
        src = (ROOT / "src" / "trade_list.py").read_text(encoding="utf-8")
        renamed = re.sub(r"\bprice_note\b", "price_note_renamed", src)
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d) / "trade_list_renamed.py"
            tmp.write_text(renamed, encoding="utf-8")
            self.assertTrue(written_in("price_note", [ROOT / "src" / "trade_list.py"]))
            self.assertFalse(written_in("price_note", [tmp]))
            self.assertTrue(written_in("price_note_renamed", [tmp]))

if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False, verbosity=2).result.wasSuccessful() else 1)
