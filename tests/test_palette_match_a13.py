"""A13：命令面板股票搜索的匹配/排序（shared/palette-match.js 纯函数）。

只读 dashboard/ui 下的源文件，不启动服务、不联网、不读 data/；用的全是 tests/a13_probe.mjs 里写死的假数据。
1. 静态：palette-match.js 没有 import、不碰 DOM / 网络 / innerHTML；command-palette.js 引入并用它排序，不再直接 slice 服务端结果
2. （有 node 时）node 里真的 import 纯函数：代码前缀排最前、先排序再截断、稳定排序、全角输入、
   不丢行、不改入参、异常输入不抛错
3. 拼音首字母：接口没有拼音字段，所以这里明确断言"目前不支持"（后端补字段后要连同实现一起改，见 docs/A13_palette_search.md）
"""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "dashboard" / "ui"
MATCH_JS = (UI / "shared" / "palette-match.js").read_text(encoding="utf-8")
PALETTE_JS = (UI / "shared" / "command-palette.js").read_text(encoding="utf-8")
PROBE = ROOT / "tests" / "a13_probe.mjs"


def strip_comments(src):
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(?m)//.*$", "", src)


class A13StaticTests(unittest.TestCase):
    def test_match_module_is_pure(self):
        code = strip_comments(MATCH_JS)
        self.assertNotRegex(code, r"(?m)^\s*import\s", "纯函数模块不能 import 别的模块（否则 node 里没法单独测）")
        for bad in ("document", "window", "fetch(", "XMLHttpRequest", "innerHTML", "insertAdjacentHTML", "localStorage"):
            self.assertNotIn(bad, code, bad)

    def test_palette_uses_rank_before_truncate(self):
        self.assertIn("import { rankStocks } from './palette-match.js'", PALETTE_JS)
        self.assertIn("rankStocks(q, rows, 8)", PALETTE_JS)
        code = strip_comments(PALETTE_JS)
        self.assertNotIn("rows.slice(0, 8)", code, "先 slice 再排序会把后面的前缀命中丢掉")

    def test_request_url_unchanged(self):
        self.assertIn("/api/search?market=cn&q=${encodeURIComponent(q)}", PALETTE_JS)


@unittest.skipUnless(shutil.which("node"), "需要 node")
class A13NodeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        r = subprocess.run(["node", "--experimental-default-type=module", str(PROBE), str(UI)],
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise AssertionError(f"探针运行失败：{r.stderr[-800:]}")
        cls.out = json.loads(r.stdout.strip().splitlines()[-1])

    def test_exports(self):
        self.assertEqual(self.out["exports"], ["RANK", "normalizeQuery", "rankStocks", "stockRank"])

    def test_code_prefix_first(self):
        # 600 开头的三只（服务端顺序）排前面，其余保持原顺序排后面，一行不丢
        self.assertEqual(self.out["prefix600"], ["600531", "600519", "600036", "510300", "300750", "000001"])
        self.assertTrue(self.out["keepsAllRows"])

    def test_prefix_beats_contains(self):
        # 输入 300：300750（代码前缀）排在 510300（名称/代码里只是"包含"）前面
        self.assertEqual(self.out["contains300"][:2], ["300750", "510300"])

    def test_sort_before_truncate(self):
        self.assertEqual(self.out["prefix600Limit2"], ["600531", "600519"])
        self.assertEqual(self.out["lateHit"], "600888")        # 第 13 条的前缀命中也进了前 8
        self.assertTrue(self.out["lateHitNotDropped"])

    def test_stable_order_within_same_rank(self):
        self.assertEqual(self.out["stableOrder"], ["600003", "600001", "600002"])

    def test_fullwidth_and_whitespace_queries(self):
        self.assertEqual(self.out["fullwidth"], self.out["prefix600"])
        self.assertEqual(self.out["padded"], self.out["prefix600"])
        self.assertEqual(self.out["normalize"], ["spy", "600", "abc"])

    def test_empty_query_keeps_server_order(self):
        self.assertEqual(self.out["emptyQuery"], ["510300", "600531", "300750"])

    def test_rank_tiers(self):
        r = self.out["ranks"]
        R = self.out["RANK"]
        self.assertEqual(r["exact"], R["EXACT"])
        self.assertEqual(r["prefix"], R["PREFIX"])
        self.assertEqual(r["name"], R["NAME"])
        self.assertEqual(r["nameInEtf300"], R["NAME"])
        self.assertEqual(r["codeContains"], R["CODE_CONTAINS"])
        self.assertEqual(r["other"], R["OTHER"])
        self.assertEqual(r["upperQuery"], R["EXACT"])           # 大小写不敏感
        self.assertEqual(r["upperCode"], R["EXACT"])
        self.assertEqual(r["emptyQuery"], R["OTHER"])
        self.assertLess(R["EXACT"], R["PREFIX"])
        self.assertLess(R["PREFIX"], R["NAME"])
        self.assertLess(R["NAME"], R["CODE_CONTAINS"])
        self.assertLess(R["CODE_CONTAINS"], R["OTHER"])

    def test_input_not_mutated_and_bad_input_safe(self):
        self.assertTrue(self.out["notMutated"])
        self.assertEqual(self.out["nonArray"], [[], [], []])
        w = self.out["weird"]
        self.assertEqual(w["noCode"], self.out["RANK"]["OTHER"])
        self.assertEqual(w["nullRow"], self.out["RANK"]["OTHER"])
        self.assertEqual(w["numericCode"], self.out["RANK"]["PREFIX"])
        self.assertEqual(w["nonStringQuery"], ["", "", ""])

    def test_pinyin_not_supported_yet(self):
        # 接口行里没有拼音字段：gzmt 不会命中贵州茅台；但已经在结果里的行不会被吞掉
        p = self.out["pinyinNotSupported"]
        self.assertEqual(p["rank"], self.out["RANK"]["OTHER"])
        self.assertEqual(p["kept"], ["600519"])


if __name__ == "__main__":
    unittest.main()
