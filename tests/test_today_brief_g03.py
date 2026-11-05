"""G03：今日页首屏信息层级（数据日期 → 今日动作 → 持仓变化 → 需要注意，详细数据放在下面）的检查。

只读 dashboard/ui 下的源文件和 mock-data 假数据，不启动服务、不读 data/、不联网；node 探针在临时目录里运行。
1. G03 两份字典：合法 JSON、键完全一致、g03. 前缀、占位符一致、英文里没有中文、不和基础字典或其他 parts 撞键；代码用到的键都有、词条都被用到
2. 静态：三个页面文件不用 innerHTML 等、不超过 500 行、没有外部网址；today.js 里首屏三张卡排在市场概览前面；CSS 类都在
3. （有 node 时）tests/g03_probe.mjs 用假 DOM 真的渲染今日页：个人 / 非个人模式、非本机、清单报错、401、无提示、清单落后、美股，
   以及字典合并前（不露键名、标题退回已有词条）和合并后
"""
import json
import sys
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "dashboard" / "ui"
sys.path.insert(0, str(Path(__file__).resolve().parent))
import i18n_parts_compat as parts_compat  # noqa: E402  部件（parts/）已退役，键名单来自快照、值来自基础字典
PAGES = UI / "pages"
PROBE = ROOT / "tests" / "g03_probe.mjs"
ZH = parts_compat.part("G03", "zh")
EN = parts_compat.part("G03", "en")
BASE_ZH = json.loads((UI / "i18n" / "zh.json").read_text(encoding="utf-8"))
MOCK = lambda name: json.loads((UI / "mock-data" / f"{name}.json").read_text(encoding="utf-8"))  # noqa: E731
CJK = re.compile(r"[㐀-鿿　-〿＀-￯]")
PLACEHOLDER = re.compile(r"\{(\w+)\}")
FILES = [PAGES / "today.js", PAGES / "today-brief.js", PAGES / "today-personal.js"]
FORBIDDEN = ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write")


def strip_comments(src):
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(?<!:)//[^\n]*", "", src)


class G03DictTests(unittest.TestCase):
    def test_keys_identical_and_prefixed(self):
        self.assertEqual(set(ZH), set(EN))
        self.assertTrue(ZH)
        for k in ZH:
            self.assertTrue(k.startswith("g03."), k)

    def test_placeholders_match(self):
        for k in ZH:
            self.assertEqual(sorted(PLACEHOLDER.findall(ZH[k])), sorted(PLACEHOLDER.findall(EN[k])), k)

    def test_english_has_no_chinese(self):
        for k, v in EN.items():
            self.assertIsNone(CJK.search(v), f"{k}: {v}")

    def test_all_keys_in_base_dictionary(self):
        """部件已合并进基础字典并退役（parts/ 移到 _to_delete/）：本任务的每个键都必须在基础字典里。
        原来的「不和基础字典撞键」检查针对合并前的状态，合并后必然撞键，已不适用。"""
        for lang in ("zh", "en"):
            self.assertEqual(parts_compat.missing_in_base("G03", lang), [], lang)

    def test_used_keys_exist_and_all_keys_used(self):
        used = set()
        for f in FILES:
            used |= set(re.findall(r"""['"](g03\.[a-z_.0-9]+)['"]""", f.read_text(encoding="utf-8")))
        self.assertEqual(used - set(ZH), set(), "代码里用了字典里没有的键")
        self.assertEqual(set(ZH) - used, set(), "字典里有没被用到的键")

    def test_fallback_keys_exist_in_base(self):
        # 合并前的兜底词条必须在基础字典里，否则兜底本身也会露键名
        src = "".join(f.read_text(encoding="utf-8") for f in FILES)
        for k in ("u1.today.target", "cmp.demo.warn", "u1.act.none", "u1.today.meta", "u1.today.exceptions", "u1.today.stale",
                  "u1.today.risk_note", "u1.today.paper", "u1.today.qty", "u1.today.now", "u1.today.next", "u1.today.port_target"):
            self.assertIn(f"'{k}'", src, k)
            self.assertIn(k, BASE_ZH, k)


class G03StaticTests(unittest.TestCase):
    def test_safe_small_local(self):
        for f in FILES + [PAGES / "U1.css", ROOT / "tests" / "g03_probe.mjs"]:
            src = f.read_text(encoding="utf-8")
            self.assertLessEqual(len(src.splitlines()), 500, f.name)
            self.assertIsNone(re.search(r"https?://", strip_comments(src)), f.name)
            if f.suffix in (".js", ".mjs"):
                for bad in FORBIDDEN:
                    self.assertNotIn(bad, strip_comments(src), f"{f.name}: {bad}")

    def test_brief_before_details(self):
        today = strip_comments((PAGES / "today.js").read_text(encoding="utf-8"))
        body = today[today.index("root.replaceChildren(...["):]
        order = [body.index(s) for s in ("dateNotice(s.data_date", "briefGrid([", "detailsHead()", "overview(s, bench", "watchTable(s.watch)")]
        self.assertEqual(order, sorted(order))
        self.assertNotIn("personalSections", today)                 # 旧的「个人区块一排」已拆进首屏三张卡

    def test_css_classes_exist(self):
        css = (PAGES / "U1.css").read_text(encoding="utf-8")
        for cls in (".u1-brief", ".u1-brief--3", ".u1-actions", ".u1-pos__move", ".u1-pos__list", ".u1-risk", ".u1-details"):
            self.assertIn(cls, css)
        self.assertNotIn(".u1-personal", css)                       # 已不再使用的类不留在样式里
        self.assertRegex(css, r"@media \(max-width: 700px\) \{ \.u1-brief, \.u1-brief--3 \{ grid-template-columns: minmax\(0, 1fr\); \} \}")


def run_probe(mode):
    with tempfile.TemporaryDirectory() as d:
        r = subprocess.run(["node", "--experimental-default-type=module", str(PROBE), str(UI), mode],
                           capture_output=True, text=True, timeout=60, cwd=d)
    if r.returncode != 0:
        raise AssertionError(r.stderr)
    return json.loads(r.stdout.strip().splitlines()[-1])


@unittest.skipUnless(shutil.which("node"), "需要 node")
class G03RuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.merged = run_probe("merged")
        cls.base = run_probe("base")
        trade = MOCK("trade_list")
        cls.counts = {tab: sum(1 for r in trade["rows"] if r["action"] == a)
                      for a, tab in (("卖出", "sell"), ("减仓", "reduce"), ("买入", "buy"), ("加仓", "add"))}
        cls.counts["exc"] = len(trade["exceptions"])
        cls.lead = next(c for c in MOCK("summary")["strategies"] if c["key"] == "blend2")
        cls.health = MOCK("summary")["health"]

    def test_no_render_errors(self):
        for out in (self.merged, self.base):
            for name, sc in out.items():
                if isinstance(sc, dict) and "error" in sc:
                    self.assertIsNone(sc["error"], f"{out['mode']}.{name}")

    def test_first_screen_order(self):
        p = self.merged["personal"]
        # 首屏三张卡下面多了一行「这些信号准不准」（A 股，content 加，取不到账本时隐藏但节点仍在）
        # D72：首次访问时最上面多一条「每页做什么」提示条（关掉后不再出现），其余顺序不变
        # D91：最上面多一个「答案优先」预览开关按钮（默认关，只是一个按钮），也不算进首屏顺序
        top = [c for c in p["top"] if c != "card ux-welcome" and "u1-hero-toggle" not in (c or "")]
        self.assertEqual(top[:4], ["u1-datewrap", "u1-brief u1-brief--3", "ws-note u1-acc-line", "eyebrow u1-details"])
        self.assertEqual(p["card_classes"], ["card u1-actions", "card u1-pos", "card u1-risk u1-risk--on"])
        self.assertEqual(p["titles"], [BASE_ZH["u1.today.actions"], ZH["g03.pos.title"], ZH["g03.risk.title"]])
        self.assertFalse(any("u1-note" in c for c in p["top"]), "系统提示不再单独占首屏最上面一条")

    def test_action_counts_match_list(self):
        p = self.merged["personal"]
        self.assertEqual({k: int(v) for k, v in p["counts"].items()}, self.counts)
        self.assertTrue(p["exc_warn"])
        self.assertEqual(len(p["act_links"]), 4)
        self.assertTrue(all("sg_t=blend2" in h for h in p["act_links"]))
        self.assertIn(ZH["g03.actions.hold_note"].replace("{pp}", "10"), p["act_text"])   # 阈值 0.1 → 10 个百分点
        self.assertEqual(p["act_has_dateline"], 0)                                       # 和页顶同一天：不重复日期行
        self.assertEqual(self.merged["lagging"]["act_has_dateline"], 1)                  # 清单落后：补日期行

    def test_position_change(self):
        p = self.merged["personal"]
        self.assertIn(self.lead["now"], p["pos_move"])
        self.assertIn(self.lead["next"], p["pos_move"])
        self.assertTrue(p["pos_move"].index(self.lead["now"]) < p["pos_move"].index(self.lead["next"]))
        sigs = [i["badge"] for i in p["pos_items"]]
        self.assertEqual(sigs, ["卖出", "减仓", BASE_ZH["u1.today.exceptions"], "加仓", "买入", ZH["g03.pos.no_signal"], ZH["g03.pos.no_signal"]])
        self.assertEqual(p["pos_visible"], 5)
        self.assertEqual(p["pos_fold"], [ZH["g03.pos.more"].replace("{n}", "2")])
        self.assertIn("1 · 减仓 1 · 买入 1 · 加仓 1 · 异常 1 · 清单无信号 2", p["pos_text"])

    def test_risk_items(self):
        p = self.merged["personal"]
        self.assertEqual(p["risk_items"][: len(self.health)], self.health)
        self.assertTrue(any("600074" in x and "600148" in x for x in p["risk_items"]), p["risk_items"])
        self.assertTrue(any("301314" in x for x in p["risk_items"]))
        self.assertNotIn(BASE_ZH["u1.today.stale"], p["risk_items"])
        self.assertIn(BASE_ZH["u1.today.stale"], self.merged["stale"]["risk_items"])
        self.assertIn(BASE_ZH["u1.today.risk_note"], p["risk_text"])
        self.assertEqual(p["risk_role"], "status")                  # 健康提示以前是 role=alert 的单独一条，挪进来后仍可朗读
        clean = self.merged["clean"]
        self.assertEqual(clean["risk_items"], [])
        self.assertFalse(clean["risk_warn"])
        self.assertIsNone(clean["risk_role"])
        self.assertIn(ZH["g03.risk.none"], clean["risk_text"])

    def test_modes_and_failures(self):
        m = self.merged
        self.assertEqual(m["nonpersonal"]["card_classes"], ["card u1-pos", "card u1-risk u1-risk--on"])
        self.assertFalse([c for c in m["nonpersonal"]["calls"] if c.startswith(("/api/trade_list", "/api/member"))])
        self.assertFalse([c for c in m["remote"]["calls"] if c.startswith("/api/member")])
        self.assertIn(BASE_ZH["u1.today.paper_local"], m["remote"]["pos_text"])
        self.assertIn("示例：清单还没生成", m["list_error"]["act_text"])
        self.assertIn(BASE_ZH["u1.today.no_actions"], m["list_error"]["act_text"])
        self.assertTrue(all(i["badge"] is None for i in m["list_error"]["pos_items"]))   # 没有清单就不猜持仓状态
        self.assertEqual(m["list_error"]["risk_items"], self.health)
        self.assertIn(BASE_ZH["ui.auth.expired"], m["auth"]["act_text"])
        self.assertEqual(m["auth"]["card_classes"][1:], ["card u1-pos", "card u1-risk u1-risk--on"])   # 一张卡失败不拖垮其他卡
        self.assertIn(BASE_ZH["u1.today.us_unsupported"], m["us"]["act_text"])
        self.assertNotIn("/api/trade_list", m["us"]["calls"])

    def test_unmerged_dict_never_shows_keys(self):
        for name, sc in self.base.items():
            if isinstance(sc, dict) and "all_text" in sc:
                self.assertNotIn("g03.", sc["all_text"], name)
        p = self.base["personal"]
        self.assertEqual(p["titles"], [BASE_ZH["u1.today.actions"], BASE_ZH["u1.today.target"], BASE_ZH["cmp.demo.warn"]])
        self.assertNotIn("eyebrow u1-details", p["top"])
        self.assertTrue(any("600074" in x for x in p["risk_items"]))   # 兜底文字仍然带代码
        self.assertNotIn(ZH["g03.risk.none"], self.base["clean"]["risk_text"])

    def test_pure_functions(self):
        pure = self.merged["pure"]
        self.assertEqual(pure["match_null"], [None])
        self.assertEqual([s for _, s in pure["match_order"]], ["卖出", "减仓", "exc", "加仓", "买入", "none", "none"])
        self.assertEqual(pure["match_numeric_code"], ["买入"])
        self.assertEqual([pure["stale_fresh"], pure["stale_old"], pure["stale_bad"]], [False, True, True])
        self.assertEqual(pure["usable"], [None, None, None, True])
        self.assertEqual(pure["risk_order"][0], "H")
        self.assertEqual(pure["risk_order"][1], BASE_ZH["u1.today.stale"])
        self.assertEqual(len(pure["risk_order"]), 6)
        self.assertEqual(pure["risk_empty"], [])
        self.assertEqual(pure["g3_missing"], "FB")


if __name__ == "__main__":
    unittest.main()
