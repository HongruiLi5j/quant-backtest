"""A12：个股 K 线「数据量大时首屏只显示最近约 1 年」的检查。

只读 dashboard/ui 下的源文件，不启动服务、不读 data/；node 运行时检查直接读 ui 目录（只读，不写）。
1. A12 两份字典：合法 JSON、键完全一致、占位符一致、英文里没有中文、不和基础字典 / 其他 parts 撞键、代码里用到的键都有词条、词条都被用到
2. （有 node 时）假 DOM + 假图表库里真的跑 pages/stock-chart.js 的 kChart：
   - 根数 ≤300：画 K 线、fitContent 铺满，没有 setVisibleLogicalRange（行为和改动前一样）
   - 根数 >300：画面积线，默认 setVisibleLogicalRange 到最后 252 根；view='all' 时 fitContent
   - 各序列喂入的点数 = 全部根数（完整数据仍然加载），买卖点标记数量在两种视图下一致、等于「日期在数据里」的买卖点数
   - showRange 来回切换不重新喂数据、不重设标记
3. stock.js 把视图状态接到 kChart、开关只在数据量大时出现；两个文件里没有 innerHTML 之类的拼 HTML 写法
"""
import json
import sys
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "dashboard" / "ui"
sys.path.insert(0, str(Path(__file__).resolve().parent))
import i18n_parts_compat as parts_compat  # noqa: E402  部件（parts/）已退役，键名单来自快照、值来自基础字典
ZH = parts_compat.part("A12", "zh")
EN = parts_compat.part("A12", "en")
CJK = re.compile(r"[㐀-鿿　-〿＀-￯]")
PLACEHOLDER = re.compile(r"\{(\w+)\}")
STOCK_JS = (UI / "pages" / "stock.js").read_text(encoding="utf-8")
CHART_JS = (UI / "pages" / "stock-chart.js").read_text(encoding="utf-8")
PROBE = ROOT / "tests" / "a12_probe.mjs"


class A12DictTests(unittest.TestCase):
    def test_keys_identical_and_prefixed(self):
        self.assertEqual(set(ZH), set(EN))
        self.assertTrue(ZH)
        for k in ZH:
            self.assertTrue(k.startswith("a12."), k)

    def test_placeholders_match(self):
        for k in ZH:
            self.assertEqual(sorted(PLACEHOLDER.findall(ZH[k])), sorted(PLACEHOLDER.findall(EN[k])), k)

    def test_english_has_no_chinese(self):
        for k, v in EN.items():
            self.assertIsNone(CJK.search(v), f"{k}: {v}")

    def test_all_keys_in_base_dictionary(self):
        """部件已合并进基础字典并退役（parts/ 移到 _to_delete/）：本任务的每个键都必须在基础字典里。"""
        for lang in ("zh", "en"):
            self.assertEqual(parts_compat.missing_in_base("A12", lang), [], lang)

    def test_used_keys_exist_and_all_keys_used(self):
        used = set(re.findall(r"""t\(\s*['"](a12\.[a-z_.0-9]+)['"]""", STOCK_JS + CHART_JS))
        # 开关按钮的文字按 VIEWS 拼键：t(`a12.range.${v}`)
        views = re.search(r"const VIEWS = \[([^\]]*)\]", STOCK_JS)
        self.assertTrue(views, "stock.js 里找不到 VIEWS")
        self.assertIn("t(`a12.range.${v}`)", STOCK_JS)
        used |= {f"a12.range.{v}" for v in re.findall(r"'(\w+)'", views.group(1))}
        self.assertEqual(used - set(ZH), set(), "代码里用了字典里没有的键")
        self.assertEqual(set(ZH) - used, set(), "字典里有没被用到的键")


class StaticWiringTests(unittest.TestCase):
    def test_stock_js_passes_view_to_both_kchart_calls(self):
        self.assertIn("kChart(box, d, st.ind, st.view)", STOCK_JS)
        self.assertIn("kChart(box, data, value, st.view)", STOCK_JS)

    def test_toggle_only_for_large_data(self):
        self.assertIn("canZoomRecent(d.dates.length)", STOCK_JS)
        self.assertRegex(STOCK_JS, r"const view = canZoomRecent\(d\.dates\.length\)\s*\?")

    def test_view_is_validated_and_stored(self):
        self.assertIn("view: 'qb-a12-stock-view'", STOCK_JS)
        self.assertIn("if (!VIEWS.includes(st.view)) st.view = 'recent'", STOCK_JS)

    def test_no_html_string_building(self):
        for name, src in (("stock.js", STOCK_JS), ("stock-chart.js", CHART_JS)):
            for bad in ("innerHTML", "insertAdjacentHTML", "document.write", "outerHTML"):
                self.assertNotIn(bad, src, f"{name} 用了 {bad}")


@unittest.skipUnless(shutil.which("node"), "需要 node")
class KChartRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        r = subprocess.run(["node", "--experimental-default-type=module", str(PROBE), str(UI)],
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        cls.out = json.loads(r.stdout.strip().splitlines()[-1])

    def test_threshold(self):
        self.assertEqual({n: z for n, z in self.out["canZoom"]}, {252: False, 300: False, 301: True, 756: True})

    def test_small_data_unchanged_behaviour(self):
        for n in (252, 300):
            for view in ("recent", "all"):
                o = self.out[f"{n}.{view}"]
                self.assertEqual((o["price"], o["fit"], o["range"]), ("candle", 1, []), f"{n}.{view}")

    def test_large_data_recent_window(self):
        for n in (301, 756, 3500):
            o = self.out[f"{n}.recent"]
            self.assertEqual(o["price"], "area", n)
            self.assertEqual(o["fit"], 0, n)
            self.assertEqual(o["range"], [{"from": n - 252, "to": n - 1}], n)

    def test_large_data_all_view_fits_everything(self):
        for n in (301, 756, 3500):
            o = self.out[f"{n}.all"]
            self.assertEqual((o["fit"], o["range"]), (1, []), n)

    def test_default_view_is_recent(self):
        self.assertEqual(self.out["default_view"], {"range": [{"from": 504, "to": 755}], "fit": 0})

    def test_full_data_is_still_loaded(self):
        for n in (301, 756, 3500):
            rec, full = self.out[f"{n}.recent"], self.out[f"{n}.all"]
            self.assertEqual(rec["setData"], full["setData"], "两种视图喂给各序列的数据必须一样")
            for item in rec["setData"].split(","):
                self.assertEqual(int(item.split(":")[1]), n, item)

    def test_marker_count_unchanged_by_view(self):
        for n in (252, 300, 301, 756, 3500):
            rec, full = self.out[f"{n}.recent"], self.out[f"{n}.all"]
            self.assertEqual(rec["expectedMarkers"], 5)
            self.assertEqual((rec["markers"], full["markers"]), (5, 5), n)
            self.assertEqual((rec["markerCalls"], full["markerCalls"]), (1, 1), n)

    def test_toggle_moves_window_without_redrawing(self):
        o = self.out["toggle"]
        self.assertTrue(o["hasShowRange"])
        self.assertEqual(o["afterAll"], {"fit": 1, "range": 1})
        self.assertEqual(o["afterRecent"], {"fit": 1, "range": 2})
        self.assertTrue(o["unchanged"])


if __name__ == "__main__":
    unittest.main()
