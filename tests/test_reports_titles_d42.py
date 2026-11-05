"""D42：同组同名的报告在列表里补上能区分它们的路径片段；不重名的标题不变；path 不变。只用临时目录。"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src import reports_view  # noqa: E402


class TitleTests(unittest.TestCase):
    def test_duplicates_get_suffix(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for sub in ("run_r1", "run_r2"):
                (root / "g1" / sub).mkdir(parents=True)
                (root / "g1" / sub / "report.md").write_text("# 因子扫描\n", encoding="utf-8")
            (root / "g1" / "a.md").write_text("# 日报\n", encoding="utf-8")
            (root / "g1" / "b.md").write_text("# 日报\n", encoding="utf-8")
            (root / "g1" / "only.md").write_text("# 唯一\n", encoding="utf-8")
            (root / "g2").mkdir()
            (root / "g2" / "x.md").write_text("# 因子扫描\n", encoding="utf-8")   # 别的组同名：不算重复
            reports_view._cache.update(t=0.0, root=None, v=None)
            items = {i["path"]: i["title"] for i in reports_view.list_reports(root)["reports"]}
        self.assertEqual(items["g1/run_r1/report.md"], "因子扫描 · run_r1")
        self.assertEqual(items["g1/run_r2/report.md"], "因子扫描 · run_r2")
        self.assertEqual(items["g1/a.md"], "日报 · a")
        self.assertEqual(items["g1/b.md"], "日报 · b")
        self.assertEqual(items["g1/only.md"], "唯一")
        self.assertEqual(items["g2/x.md"], "因子扫描")


if __name__ == "__main__":
    unittest.main()
