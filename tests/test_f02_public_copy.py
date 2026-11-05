"""F02：公开站文案重写的检查（只读 dashboard/ 和 scripts/ 下的源文件，不读写 data/，不联网）。

1. 公开站字典 zh/en 键完全一致；F02 部件 parts/F02.*.json 两边键一致、占位符一致，并且和公开站字典里的值逐条相同
2. 「合成数据」标注都还在（pub.synthetic、land.synthetic、首屏 story.hero.sources、榜单 story.s2.board.note），白名单要求的键都在
3. F02 文案里没有导出脚本的红线词和合规词
4. F02 文案里出现的每个数字都在白名单里（研究结论的数字逐个对过 ~/Documents/AQ-o44/run/reports/ 原文，出处写在注释里）
   以后改文案加了新数字，这个测试会提醒去核对出处
5. 不再出现「保留退市股」这类和研究报告（C11/C12：数据里没有退市股）相矛盾的说法
6. page-method.js：只用 textContent 路径（不出现 innerHTML 等）；新加的两节和第一屏数字都在
7. HTML 静态首帧文字和字典一致（landing.html 默认英文，其余页面默认中文）

运行：.venv/bin/python tests/test_f02_public_copy.py
"""
import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MOCK = ROOT / "dashboard" / "mockups"
sys.path.insert(0, str(Path(__file__).resolve().parent))
import i18n_parts_compat as parts_compat  # noqa: E402  部件（parts/）已退役，键名单来自快照、值来自基础字典
sys.path.insert(0, str(ROOT / "scripts"))
import build_public_site as B  # noqa: E402  只用它的红线词、合规词常量，不构建

# 数字白名单：数字 → 出处（C0x = ~/Documents/AQ-o44/run/reports/C0x.md；「合成」= mock 数据或演示规则自带的示意数）
ALLOWED_NUMBERS = {
    "0": "0 个留下 / 0 处违例 / 0 项不一致（C01、C03、C04、C05、C09）",
    "0.02": "IC = 0.02 的功效 52%（C01）",
    "0.05": "单边 p < 0.05（C08）",
    "0.0806": "目标仓位差异天数占比最大 0.0806%（C09）",
    "0.386": "最好的格子 DSR 0.386（C04）",
    "0.95": "DSR ≥ 0.95 的门槛（C04）",
    "1": "1 个百分点（合成演示规则）/ 1 个检验（C08）/ 第 1 节 / T+1",
    "1.16": "515 次纯噪声最大夏普约 1.16（C04）",
    "1.1": "515 次纯噪声最大夏普约 1.1–1.2（C04；revquant 复核 6–7 年样本为 1.09–1.18）",
    "1.2": "同上（1.1–1.2 的上界）",
    "2": "第 2 节 / 12.6、4.5 等小数的一部分",
    "3": "2026 年 3 月",
    "03": "2026-03-31",
    "4": "运气预期约 4 个（C08）",
    "4.5": "blend2 逐年最大回撤平均 −4.5%（C08）",
    "5": "ETF 单边 5bp（C04/C11）/ 第 5 节 / 5 折（合成）",
    "5.2": "慢变噪声通过率 5.2%（C01）",
    "6": "样本外 6 年（C08）/ 第 6 节",
    "6.2": "规则 4 通过率 6.2%（C01）",
    "7": "7 个正式策略（C04/C05/C08/C09）/ 7 处实现差异（C09）/ 样本内 7 年（C08）/ 第 7 节",
    "008": "T008 的编号",
    "8": "个股单边 8bp（C05/C11）",
    "10": "10% 调仓阈值（C04/C05）",
    "12": "12 个 ETF（C04）/ 12 个月测试段（合成）",
    "12.6": "买入持有逐年最大回撤平均 −12.6%（C08）",
    "13": "rsi2 盈亏平衡 13–24bp（C11）",
    "15": "15:30（项目规则）",
    "18": "18 个影子候选（C04）",
    "20": "84 个样本外格子里 20 个不校正显著（C04）",
    "21": "21 个影子候选（C05）/ 漏斗留下 21 个（合成）",
    "24": "rsi2 盈亏平衡 13–24bp（C11）",
    "28": "28 个信号（C05）",
    "30": "30 个通过规则 4（C01）/ 15:30",
    "31": "2026-03-31 截止日",
    "35": "35 项可比参数默认值（C09）",
    "46": "46 个月训练段（合成）",
    "50": "衰减超过 50%（合成演示规则）",
    "52": "IC = 0.02 时约 52% 能通过（C01）",
    "84": "84 个样本外格子（C04）/ 84 个检验（C08）",
    "100": "100 个策略（合成漏斗）",
    "168": "168 个格子（C04）",
    "485": "485 个组合（C01/C03）",
    "490": "T008 的 490 次试验（C04/C13）",
    "515": "m = 515（C04）",
    "2013": "样本内起点（C04/C08）",
    "2019": "样本内终点（C04/C08）",
    "2020": "样本外起点（C04/C08）",
    "2025": "逐年检验的最后一个整年（C08）",
    "2026": "截止 2026-03-31",
}
SYNTHETIC_KEYS = ("pub.synthetic", "land.synthetic", "story.hero.sources", "story.s2.board.note")


def load(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


class F02CopyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.zh = load(MOCK / "shared" / "i18n-zh.json")
        cls.en = load(MOCK / "shared" / "i18n-en.json")
        # F02 部件原是公开站字典的镜像，已退役：键名单来自快照，值直接从公开站字典取（缺键会报 KeyError）
        cls.pzh = parts_compat.part("F02", "zh", source=cls.zh)
        cls.pen = parts_compat.part("F02", "en", source=cls.en)

    def test_dict_keys_match(self):
        self.assertEqual(set(self.zh), set(self.en))
        self.assertEqual(set(self.pzh), set(self.pen))
        self.assertTrue(self.pzh, "F02 部件不能是空的")

    def test_parts_mirror_public_dict(self):
        for k in self.pzh:
            self.assertEqual(self.pzh[k], self.zh.get(k), k)
            self.assertEqual(self.pen[k], self.en.get(k), k)
            self.assertTrue(self.pzh[k].strip() and self.pen[k].strip(), f"{k} 不能为空")
            self.assertEqual(set(re.findall(r"\{\w+\}", self.pzh[k])), set(re.findall(r"\{\w+\}", self.pen[k])), f"{k} 占位符不一致")

    def test_synthetic_labels_kept(self):
        al = load(ROOT / "scripts" / "public_allowlist.json")
        for d in (self.zh, self.en):
            for k in SYNTHETIC_KEYS + tuple(al["required_keys"]):
                self.assertTrue(d.get(k), f"缺少或为空：{k}")
        self.assertIn("合成", self.zh["pub.synthetic"])
        self.assertIn("synthetic", self.en["pub.synthetic"].lower())

    def test_no_redline_or_compliance_words(self):
        for part in (self.pzh, self.pen):
            for k, v in part.items():
                self.assertIsNone(B.REDLINE.search(v), f"{k} 含红线词")
                self.assertIsNone(B.INTERNAL_API.search(v), f"{k} 含内部接口路径")
                for w in B.COMPLIANCE_WORDS:
                    self.assertNotIn(w, v, f"{k} 含合规词 {w}")

    def test_every_number_is_sourced(self):
        for part in (self.pzh, self.pen):
            for k, v in part.items():
                for n in re.findall(r"\d+(?:\.\d+)?", v):
                    self.assertIn(n, ALLOWED_NUMBERS, f"{k} 里的数字 {n} 没有登记出处")

    def test_no_contradicted_claims(self):
        """研究报告 C11/C12 写明数据里没有退市股：公开文案不能再说「保留退市股」"""
        self.assertNotIn("退市股票保留", self.zh["land.s8.m4"])
        self.assertNotIn("kept to avoid", self.en["land.s8.m4"])
        self.assertNotIn("按「当时在市」构建，退市和暂停上市的股票保留", self.zh["method.sb.p1"])
        self.assertNotIn("Our universe is built as it stood", self.en["method.sb.p1"])
        self.assertIn("没有退市股", self.zh["method.sb.p2"])
        self.assertIn("does not yet include delisted", self.en["method.sb.p2"])
        for d in (self.zh, self.en):
            for k in ("story.hero.sub", "story.s2.title", "story.s6.sub"):
                self.assertNotIn("100+", d[k], k)

    def test_method_page_renderer(self):
        js = (MOCK / "shared" / "page-method.js").read_text(encoding="utf-8")
        for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
            self.assertNotIn(bad, js)
        self.assertIn("['find', 'findings'], ['neg', 'negative']", js)
        self.assertIn("findings(), negative()", js)
        for v in ("'0 / 485'", "'0 / 168'"):
            self.assertIn(v, js)
        used = set(re.findall(r"'(method\.(?:hero|find|neg|lookahead)\.[\w.]+)'", js))
        for k in ("method.find.t008", "method.find.dsr", "method.find.oos", "method.find.bh"):
            self.assertIn(k.split(".")[-1], js)
            self.assertIn(f"{k}.t", self.zh)
            self.assertIn(f"{k}.b", self.zh)
        for k in used:
            self.assertIn(k, self.zh, f"页面用到的键 {k} 字典里没有")
        # 合成数据脚注在研究结论之前（紧跟合成内容），研究结论一节自己写了范围说明
        self.assertLess(js.index("t('pub.synthetic')"), js.index("findings(), negative()"))
        self.assertIn("method.find.scope", js)

    def test_static_first_frame_matches_dict(self):
        def check(rel, d):
            html = (MOCK / rel).read_text(encoding="utf-8")
            for k, text in re.findall(r'data-i18n="([\w.]+)">([^<]+)<', html):
                if k in self.pzh and "{" not in d[k]:
                    self.assertEqual(text, d[k], f"{rel} 的 {k} 静态文字和字典不一致")
        check("quant-ai-dark/landing.html", self.en)
        for rel in ("METHOD.html", "quant-ai-dark/validation.html", "quant-ai-dark/engines.html",
                    "quant-ai-dark/replay.html", "quant-ai-dark/backtest.html"):
            check(rel, self.zh)


if __name__ == "__main__":
    unittest.main(verbosity=2)
