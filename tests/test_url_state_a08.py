"""A08：表格的排序 / 筛选状态写进网址（刷新和分享后保留）的检查。

只读 dashboard/ui 下的源文件和 mock-data，不启动服务、不读 data/；node 探针（tests/a08_probe.mjs）用假 DOM + 假 fetch 真的渲染页面。
1. 静态：三个页面都接了 shared/url-state.js；只用 replaceState（没有 pushState）；不用 innerHTML 等；网址参数前缀互不相同
2. url-state.js 纯函数：合并参数时保留 mock / 其他页面的参数和 #hash、空值不写、特殊字符被编码、日期校验、写入失败不抛错
3. 三个页面：
   - 从网址恢复排序 / 筛选，接口收到的参数、页面表头的排序标记都对
   - 点表头 / 改下拉 / 搜索之后，网址跟着变；回到默认值时对应参数消失；一路只调用 replaceState（不产生历史记录）
   - 网址里的垃圾值（不在白名单的策略 / 排序列、负页码、不存在的日期等）一律退回默认，不会发给接口
   - 全市场页：页码越界 / 不存在的板块 → 纠正后重取一次；切换市场时网址里的市场跟着变
"""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "dashboard" / "ui"
PAGES = UI / "pages"
PROBE = ROOT / "tests" / "a08_probe.mjs"
FILES = [UI / "shared" / "url-state.js", PAGES / "market.js", PAGES / "watch.js", PAGES / "signals.js", PAGES / "u4-util.js"]
FORBIDDEN = ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write")
PREFIX = {"market.js": "mk", "watch.js": "wl", "signals.js": "sg"}


def strip_comments(src):
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(?<!:)//[^\n]*", "", src)


class StaticTests(unittest.TestCase):
    def test_pages_use_url_state_module(self):
        for name, prefix in PREFIX.items():
            src = (PAGES / name).read_text(encoding="utf-8")
            self.assertIn("from '../shared/url-state.js'", src, name)
            self.assertIn(f"const NS = '{prefix}'", src, name)
            self.assertIn("writeState(NS", src, name)
            self.assertIn("pickState(NS", src, name)

    def test_prefixes_are_distinct(self):
        self.assertEqual(len(set(PREFIX.values())), len(PREFIX))

    def test_no_history_entries_and_no_html_strings(self):
        for f in FILES:
            code = strip_comments(f.read_text(encoding="utf-8"))
            self.assertNotIn("pushState", code, f.name)
            self.assertNotIn("location.assign", code, f.name)
            for bad in FORBIDDEN:
                self.assertNotIn(bad, code, f"{f.name} 用了 {bad}")
        self.assertIn("replaceState", strip_comments((UI / "shared" / "url-state.js").read_text(encoding="utf-8")))

    def test_no_external_urls_or_cjk_in_new_module_code(self):
        code = strip_comments((UI / "shared" / "url-state.js").read_text(encoding="utf-8"))
        self.assertNotRegex(code, r"https?://")
        self.assertIsNone(re.search(r"[㐀-鿿]", code), "url-state.js 不含界面文字")

    def test_file_sizes_within_limit(self):
        for f in FILES:
            self.assertLessEqual(len(f.read_text(encoding="utf-8").splitlines()), 500, f.name)

    def test_no_new_visible_text(self):
        """A08 不新增界面文字，所以不需要 A08 字典；如果以后加了文字，必须中英两份键一致"""
        zh, en = UI / "i18n" / "parts" / "A08.zh.json", UI / "i18n" / "parts" / "A08.en.json"
        self.assertEqual(zh.exists(), en.exists())
        if zh.exists():
            self.assertEqual(set(json.loads(zh.read_text(encoding="utf-8"))), set(json.loads(en.read_text(encoding="utf-8"))))


@unittest.skipUnless(shutil.which("node"), "需要 node")
class NodeRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        r = subprocess.run(["node", "--experimental-default-type=module", str(PROBE), str(UI)], capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            raise AssertionError(r.stderr)
        cls.o = json.loads(r.stdout.strip().splitlines()[-1])

    # ---------- shared/url-state.js ----------
    def test_merge_keeps_other_params(self):
        u = self.o["us"]
        self.assertEqual(u["merge_keep_other"], "?mock=1&foo=bar&mk_s=cagr&mk_o=d")      # mock / foo 保留；旧的 mk_ 参数被整体替换；空值 q 不写
        self.assertEqual(u["merge_clear_all"], "?wl_s=keep")                              # 清空 mk_ 时别的页面前缀不动
        self.assertEqual(u["merge_empty"], "")                                            # 没有任何参数时不留孤零零的 ?
        self.assertEqual(u["merge_encode"], "?mk_q=%E9%93%B6+%E8%A1%8C%26%3D%23")        # 特殊字符被编码，不会截断其他参数
        self.assertEqual(u["pick"], {"s": "cagr", "q": "银"})
        self.assertTrue(u["has_yes"])
        self.assertFalse(u["has_no"])

    def test_helpers(self):
        u = self.o["us"]
        self.assertTrue(u["date_ok"])
        self.assertFalse(u["date_feb30"])
        self.assertFalse(u["date_fmt"])
        self.assertFalse(u["date_null"])
        self.assertEqual((u["nondef_same"], u["nondef_diff"]), ("", "b"))
        self.assertEqual(u["dir_to"], ["a", "d"])
        self.assertEqual(u["dir_from"], ["asc", "desc", None, None])

    def test_write_state_keeps_hash_and_adds_no_history(self):
        w = self.o["us"]["write"]
        self.assertEqual(w, {"search": "?mock=0&foo=1&mk_s=cagr&mk_o=a", "hash": "#top", "replaced": 1, "pushed": 0})
        self.assertEqual(self.o["us"]["write_same_replaced"], 1)       # 没变化就不再调用 replaceState
        self.assertTrue(self.o["us"]["write_throw_swallowed"])          # replaceState 抛错（Safari 频率限制 / 沙箱）时页面照常工作

    # ---------- market.js ----------
    def test_market_state_parsing(self):
        m = self.o["market_unit"]
        self.assertEqual(m["from_ok"], {"strategy": "rsi2", "q": "银行", "board": "主板", "industry": "银行", "signal": "买入", "sort": "cagr", "dir": "asc", "page": 2})
        for k in ("from_bad", "from_page_zero", "from_page_big", "from_page_float", "from_buy_hold"):
            self.assertEqual(m[k], {}, k)                                # 垃圾值一个都不放行
        self.assertEqual(m["from_raw"], {"strategy": "raw", "sort": "mcap"})
        self.assertEqual(m["long_q"], 20)                               # 搜索词截到服务端的 20 字
        self.assertEqual({k: v for k, v in m["to_default"].items() if v != ""}, {"m": "cn"})   # 默认状态只剩市场一个参数
        self.assertEqual(m["to_full"], {"m": "us", "t": "rsi2", "q": "银", "b": "主板", "i": "银行", "g": "买入", "s": "cagr", "o": "a", "p": 3})
        self.assertEqual((m["to_dir_only"]["s"], m["to_dir_only"]["o"]), ("sharpe", "a"))   # 只改方向也要把列一起写

    def test_market_restore_from_url(self):
        r = self.o["market_restore"]
        self.assertEqual(r["scan"], {"market": "cn", "strategy": "rsi2", "q": "银", "board": "主板", "industry": "银行", "signal": "买入",
                                     "sort": "cagr", "dir": "asc", "page": "1"})
        self.assertEqual(r["sortTh"], "ascending")                      # 表头的排序标记也恢复了
        self.assertEqual(r["url"]["foo"], "1")                          # 不认识的参数保留
        self.assertEqual(r["hash"], "#h")
        self.assertEqual(r["pushed"], 0)

    def test_market_click_updates_url(self):
        o = self.o
        self.assertEqual((o["market_click1"]["url"]["mk_s"], o["market_click1"]["url"]["mk_o"]), ("d1", "d"))     # 换列：先降序
        self.assertNotIn("mk_p", o["market_click1"]["url"])                                                     # 换排序回到第 1 页
        self.assertEqual((o["market_click1"]["scan"]["sort"], o["market_click1"]["scan"]["dir"], o["market_click1"]["scan"]["page"]), ("d1", "desc", "0"))
        self.assertEqual(o["market_click2"]["url"]["mk_o"], "a")                                                 # 同列再点：翻转
        c3 = o["market_click3"]
        self.assertNotIn("mk_s", c3["url"])                                                                      # 点回默认（sharpe 降序）：排序参数消失
        self.assertNotIn("mk_o", c3["url"])
        self.assertEqual(c3["url"]["mk_t"], "rsi2")                                                              # 其他筛选还在
        self.assertEqual((c3["hash"], c3["pushed"]), ("#h", 0))
        self.assertTrue(c3["replaced_gt0"])

    def test_market_rerender_keeps_state(self):
        r = self.o["market_rerender"]
        self.assertEqual((r["scan"]["strategy"], r["scan"]["q"], r["scan"]["board"], r["scan"]["signal"]), ("rsi2", "银", "主板", "买入"))
        self.assertEqual((r["scan"]["sort"], r["scan"]["dir"]), ("sharpe", "desc"))     # 没有被网址里的旧排序（cagr 升序）覆盖

    def test_market_garbage_url_falls_back_to_defaults(self):
        b = self.o["market_bad"]
        self.assertEqual(b["scan"], {"market": "cn", "strategy": "blend2", "q": "", "board": "", "industry": "", "signal": "", "sort": "sharpe", "dir": "desc", "page": "0"})
        self.assertEqual(b["url"], {"mk_m": "cn"})                      # 垃圾参数被清掉，只剩合法的市场

    def test_market_plain_load_does_not_touch_url(self):
        p = self.o["market_plain_load"]
        self.assertEqual((p["search"], p["replaced"]), ("?mock=0", 0))
        c = self.o["market_plain_click"]
        self.assertEqual(c["url"], {"mock": "0", "mk_m": "cn", "mk_s": "cagr", "mk_o": "d"})
        s = self.o["market_plain_search"]
        self.assertEqual(s["url"]["mk_q"], "平安")                       # 搜索词防抖后写进网址
        self.assertEqual(s["scan"]["q"], "平安")

    def test_market_out_of_range_page_and_unknown_board_are_fixed(self):
        c = self.o["market_clamp"]
        self.assertEqual(c["scan_calls"], 2)                            # 第一次发现越界，纠正后只重取一次
        self.assertEqual((c["scan"]["page"], c["scan"]["board"], c["scan"]["industry"]), ("0", "", ""))
        self.assertEqual(c["url"], {"mk_m": "cn"})

    def test_market_switch_updates_url_and_rerender_uses_it(self):
        self.assertEqual(self.o["market_switch"]["url"]["mk_m"], "us")
        self.assertEqual(self.o["market_switch_render"]["scan"]["market"], "us")
        self.assertEqual(self.o["market_us"]["scan"]["market"], "us")    # 分享的链接自带市场

    # ---------- watch.js ----------
    def test_watch_state_parsing(self):
        w = self.o["watch_unit"]
        self.assertEqual(w["from_ok"], {"strategy": "rsi2", "start": "2024-01-31", "sort": "cagr", "dir": 1})
        self.assertEqual(w["from_sort_no_dir"], {"sort": "sharpe", "dir": -1})
        self.assertEqual(w["from_bad"], {})                              # 2026-02-31 不是真实日期；symbol 不是表头列
        self.assertEqual(w["to_default"], {"m": "cn", "t": "", "d": "", "s": "", "o": ""})
        self.assertEqual(w["to_full"], {"m": "us", "t": "rsi2", "d": "2024-01-31", "s": "cagr", "o": "a"})

    def test_watch_restore_sorts_table(self):
        r = self.o["watch_restore"]
        self.assertEqual(r["table"], {"market": "cn", "strategy": "rsi2", "start": "2024-01-31"})
        rows = json.loads((UI / "mock-data" / "watch-table.json").read_text(encoding="utf-8"))["rows"]
        expect = [x["name"] for x in sorted(rows, key=lambda x: x.get("cagr", -1e9))]     # 缺值（还在下载的行）和页面一样当成最小
        self.assertEqual(r["order"], expect)                             # 表格真的按 cagr 升序排好了
        self.assertEqual(r["sortTh"], "ascending")
        self.assertEqual(r["replaced"], 0)                               # 光恢复不改地址栏

    def test_watch_controls_update_url(self):
        o = self.o
        self.assertEqual((o["watch_click1"]["url"]["wl_s"], o["watch_click1"]["url"]["wl_o"]), ("sharpe", "d"))
        self.assertEqual(o["watch_click1"]["pushed"], 0)
        self.assertEqual(o["watch_click2"]["url"]["wl_o"], "a")
        self.assertEqual((o["watch_date"]["url"]["wl_d"], o["watch_date"]["table"]["start"]), ("2023-05-06", "2023-05-06"))
        self.assertEqual((o["watch_strategy"]["url"]["wl_t"], o["watch_strategy"]["table"]["strategy"]), ("trend_ens", "trend_ens"))
        self.assertNotIn("wl_d", o["watch_date_cleared"]["url"])         # 清空日期：参数消失
        self.assertEqual(o["watch_date_cleared"]["url"]["foo"], "1")
        self.assertEqual(o["watch_switch"]["url"]["wl_m"], "us")
        self.assertEqual(o["watch_switch_render"]["table"]["market"], "us")

    def test_watch_garbage_url_falls_back_to_defaults(self):
        b = self.o["watch_bad"]
        self.assertEqual(b["table"], {"market": "cn", "strategy": "blend2", "start": ""})
        self.assertTrue(b["sortTh"])                                     # 没有任何表头被标成排序列

    # ---------- signals.js ----------
    def test_signals_state_parsing(self):
        s = self.o["signals_unit"]
        self.assertEqual(s["from_ok"], {"strategy": "rsi2", "date": "2026-10-22", "tab": "sell", "q": "平安", "liq": "正常",
                                        "view": {"sort": "close", "dir": 1, "manual": True}})
        self.assertEqual(s["from_code_default_dir"], {"sort": "code", "dir": 1, "manual": True})     # 只给列不给方向：代码升序、其余降序（和手机下拉框一致）
        self.assertEqual(s["from_other_default_dir"], {"sort": "d1", "dir": -1, "manual": True})
        self.assertEqual(s["from_bad"], {})
        self.assertEqual(s["from_unsortable_flags"], {})                  # reason / flags 列本来就不能排序
        self.assertEqual(s["long_q"], 20)
        self.assertEqual(s["to_auto"], {"t": "blend2", "d": "", "k": "buy", "q": "平安", "l": "", "s": "", "o": ""})   # 自动排序不钉死
        self.assertEqual(s["to_manual"], {"t": "rsi2", "d": "2026-10-22", "k": "sell", "q": "", "l": "正常", "s": "close", "o": "d"})

    def test_signals_restore_from_url(self):
        r = self.o["signals_restore"]
        self.assertEqual(r["list"], {"strategy": "trend_ens", "date": "2026-10-22"})
        self.assertEqual(r["pressed"], ["sell"])
        self.assertEqual(r["search"], "平安")
        self.assertEqual(r["sortTh"], "ascending")
        self.assertEqual(r["replaced"], 0)

    def test_signals_controls_update_url(self):
        o = self.o
        t = o["signals_tab"]["url"]
        self.assertEqual(t["sg_k"], "buy")
        self.assertNotIn("sg_s", t)                                      # 换标签会重置手动排序 → 排序参数跟着消失
        self.assertEqual(o["signals_tab"]["pushed"], 0)
        self.assertEqual((o["signals_sort1"]["url"]["sg_s"], o["signals_sort1"]["url"]["sg_o"]), ("close", "a"))
        self.assertEqual(o["signals_sort2"]["url"]["sg_o"], "d")
        self.assertEqual((o["signals_sort3"]["url"]["sg_s"], o["signals_sort3"]["url"]["sg_o"]), ("code", "a"))
        self.assertEqual(o["signals_search_immediate"], "平安")           # 搜索框防抖：刚输入时网址还是旧的
        self.assertEqual(o["signals_search_later"], "600")
        self.assertEqual(o["signals_liq"]["url"]["sg_l"], "流动性差")
        d = o["signals_date"]
        self.assertEqual((d["url"]["sg_d"], d["reloaded"], d["list"]["date"]), ("2026-10-21", 1, "2026-10-21"))
        self.assertNotIn("sg_s", d["url"])                               # 换日期同样重置手动排序
        self.assertEqual(d["url"]["foo"], "1")

    def test_signals_garbage_url_falls_back_to_defaults(self):
        self.assertEqual(self.o["signals_bad"]["list"], {"strategy": "blend2", "date": ""})
        self.assertEqual(self.o["signals_bad"]["replaced"], 0)
        self.assertEqual(self.o["signals_unknown_date"]["list"]["date"], "")   # 存档里没有的日期 → 最新


if __name__ == "__main__":
    unittest.main()
