"""A15：持仓录入表单的数字格式检查、整手提示、费用留空说明。

只读 dashboard/ui 下的源文件，不启动服务、不读 data/；node 探针（tests/a15_probe.mjs）用假 DOM 真跑 pages/holdings-form.js 和 pages/holdings-rules.js。
1. A15 两份字典：合法 JSON、键完全一致、占位符一致、英文里没有中文、不和基础字典 / 其他 parts 撞键、代码里用到的键都有词条、词条都被用到
2. 静态：新文件不用 innerHTML 之类；数量 / 价格 / 费用不再是 type=number，有 aria-describedby 指向提示；规则常量和后端 store.py 一致
3. （有 node 时）规则函数、表单行为（合并词条 / 未合并词条两种状态，中英文各一遍）
"""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "dashboard" / "ui"
PARTS = UI / "i18n" / "parts"
ZH = json.loads((PARTS / "A15.zh.json").read_text(encoding="utf-8"))
EN = json.loads((PARTS / "A15.en.json").read_text(encoding="utf-8"))
CJK = re.compile(r"[㐀-鿿　-〿＀-￯]")
PLACEHOLDER = re.compile(r"\{(\w+)\}")
FORM_JS = (UI / "pages" / "holdings-form.js").read_text(encoding="utf-8")
RULES_JS = (UI / "pages" / "holdings-rules.js").read_text(encoding="utf-8")
STORE_PY = (ROOT / "src" / "holdings" / "store.py").read_text(encoding="utf-8")
PROBE = ROOT / "tests" / "a15_probe.mjs"


class A15DictTests(unittest.TestCase):
    def test_keys_identical_and_prefixed(self):
        self.assertEqual(set(ZH), set(EN))
        self.assertTrue(ZH)
        for k in ZH:
            self.assertTrue(k.startswith("a15."), k)

    def test_placeholders_match(self):
        for k in ZH:
            self.assertEqual(sorted(PLACEHOLDER.findall(ZH[k])), sorted(PLACEHOLDER.findall(EN[k])), k)

    def test_english_has_no_chinese_and_chinese_is_chinese(self):
        for k, v in EN.items():
            self.assertTrue(v.strip(), k)
            self.assertIsNone(CJK.search(v), f"{k}: {v}")
        for k, v in ZH.items():
            self.assertIsNotNone(CJK.search(v), k)

    def test_no_collision_with_base_or_other_parts(self):
        for lang, mine in (("zh", ZH), ("en", EN)):
            base = json.loads((UI / "i18n" / f"{lang}.json").read_text(encoding="utf-8"))
            self.assertFalse(set(mine) & set(base), lang)
            for p in PARTS.glob(f"*.{lang}.json"):
                if p.name.startswith("A15."):
                    continue
                self.assertFalse(set(mine) & set(json.loads(p.read_text(encoding="utf-8"))), p.name)

    def test_used_keys_exist_and_all_keys_used(self):
        used = set(re.findall(r"""['"`](a15\.[a-z_.0-9]+)['"`]""", FORM_JS + RULES_JS))
        # 错误词条按 `a15.err.${name}` 拼键，name 是下面五个字面量之一（逐个确认它们真的出现在规则文件里）
        self.assertIn("a15.err.${name}", RULES_JS)
        for n in ("'qty'", "'qty_max'", "'price'", "'fee'", "'fee_over'"):
            self.assertIn(n, RULES_JS)
        used |= {f"a15.err.{n}" for n in ("qty", "qty_max", "price", "fee", "fee_over")}
        self.assertEqual(used - set(ZH), set(), "代码里用了字典里没有的键")
        self.assertEqual(set(ZH) - used, set(), "字典里有没被用到的键")


class StaticTests(unittest.TestCase):
    def test_no_html_string_building(self):
        for name, src in (("holdings-form.js", FORM_JS), ("holdings-rules.js", RULES_JS)):
            for bad in ("innerHTML", "insertAdjacentHTML", "document.write", "outerHTML"):
                self.assertNotIn(bad, src, f"{name} 用了 {bad}")

    def test_files_stay_small(self):
        self.assertLessEqual(len(FORM_JS.splitlines()), 500)

    def test_inputs_not_type_number_and_described_by_hints(self):
        for ident, hint in (("holdQty", "holdQtyHint"), ("holdPrice", "holdPriceHint"), ("holdFee", "holdFeeHint")):
            line = next(l for l in FORM_JS.splitlines() if f"id: '{ident}'" in l and "el('input'" in l)
            self.assertIn("type: 'text'", line, ident)
            self.assertIn("inputmode:", line, ident)
            self.assertIn(f"'aria-describedby': '{hint}'", line, ident)
            self.assertIn(f"id: '{hint}'", FORM_JS, hint)

    def test_fee_placeholder_comes_from_a15_with_safe_fallback(self):
        self.assertIn("tMaybe('a15.fee.placeholder')", FORM_JS)
        self.assertIn("?? t('hold.form.fee_auto')", FORM_JS)     # 词条没合并时退回原来的「按费率估算」，不露键名

    def test_rule_constants_match_backend(self):
        self.assertIn("MAX_QTY = 100_000_000", RULES_JS)
        self.assertIn("MAX_QTY = 100_000_000", STORE_PY)
        self.assertIn("MAX_PRICE = 100000", RULES_JS)
        self.assertIn("d > 100000", STORE_PY)
        self.assertIn("NUM_RE = /^\\d+(\\.\\d+)?$/", RULES_JS)
        self.assertIn('_NUM_RE = re.compile(r"^\\d+(\\.\\d+)?$")', STORE_PY)
        # 科创板 688 / 689 起买 200 股、其余买入 100 的整数倍 —— 前后端同一规则
        self.assertIn('code.startswith(("688", "689"))', STORE_PY)
        self.assertIn("value < 200", STORE_PY)
        self.assertIn("value % 100 != 0", STORE_PY)
        self.assertIn("/^68[89]/", RULES_JS)
        self.assertIn("n < 200", RULES_JS)
        self.assertIn("n % 100 !== 0", RULES_JS)

    def test_dividend_blank_fee_is_zero_like_backend(self):
        # 后端：分红且费用留空 → 0 元、不标估算；页面不能再说「按费率估算」
        self.assertRegex(STORE_PY, r'elif side == "dividend":\s*\n\s*fee_d, estimated = Decimal\(0\), 0')
        self.assertIn("body.side === 'dividend' ? t('hold.form.fee_value', { fee: '0' })", FORM_JS)


@unittest.skipUnless(shutil.which("node"), "需要 node")
class NodeRuntimeTests(unittest.TestCase):
    @staticmethod
    def run_probe(merge):
        r = subprocess.run(["node", "--experimental-default-type=module", str(PROBE), str(UI), merge],
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise AssertionError(r.stderr)
        return json.loads(r.stdout.strip().splitlines()[-1])

    @classmethod
    def setUpClass(cls):
        cls.merged = cls.run_probe("1")
        cls.unmerged = cls.run_probe("0")

    # ---------- 纯函数 ----------
    def test_clean_number_only_strips_unambiguous_thousands_commas(self):
        got = dict(map(tuple, self.merged["clean"]))
        self.assertEqual(got["1,000"], "1000")
        self.assertEqual(got["1,234,567.89"], "1234567.89")
        self.assertEqual(got[" 100 "], "100")
        for s in ("12,34", "1,00", "-1,000"):   # 不像千分位的逗号原样保留，后面的格式检查会报错
            self.assertEqual(got[s], s)

    def test_check_inputs_accepts_valid(self):
        c = self.merged["check"]
        for k in ("ok", "okFee", "okFeeZero", "okTrailingZeros", "qtyMax", "priceMaxOk", "feeEqAmount", "sellNoLotCheck"):
            self.assertEqual(c[k], [], k)

    def test_check_inputs_rejects_non_positive_and_malformed(self):
        c = self.merged["check"]
        for k in ("qtyZero", "qtyZeros", "qtyNeg", "qtyFrac", "qtyLetters", "qtySci"):
            self.assertEqual(c[k], ["qty:a15.err.qty"], k)
        for k in ("qtyOverMax", "qtyHuge"):
            self.assertEqual(c[k], ["qty:a15.err.qty_max"], k)
        for k in ("priceZero", "priceZeroDec", "priceNeg", "priceSci", "priceDec4", "priceDot", "priceDotFirst", "priceBig", "priceLetters"):
            self.assertEqual(c[k], ["price:a15.err.price"], k)
        for k in ("feeNeg", "feeDec3", "feeLetters"):
            self.assertEqual(c[k], ["fee:a15.err.fee"], k)
        self.assertEqual(c["feeOver"], ["fee:a15.err.fee_over"])
        self.assertEqual(c["feeOverButQtyBad"], ["qty:a15.err.qty"])    # 数量本身不合法时不拿它算成交金额
        self.assertEqual(c["multi"], ["qty:a15.err.qty", "price:a15.err.price", "fee:a15.err.fee"])
        self.assertEqual(c["fallbacks"], ["hold.err.HOLDINGS_BAD_QTY", "hold.err.HOLDINGS_BAD_PRICE", "hold.err.HOLDINGS_BAD_FEE"])

    def test_check_inputs_by_side(self):
        c = self.merged["check"]
        self.assertEqual(c["bonusIgnoresPriceFee"], [])        # 送转没有价格和费用
        self.assertEqual(c["bonusQtyStillChecked"], ["qty"])
        self.assertEqual(c["dividendPriceZero"], ["price"])    # 分红的每股现金同样须大于 0

    def test_lot_hint_rules(self):
        lot = self.merged["lot"]
        self.assertEqual(lot["sell"], "a15.lot.sell|false")
        self.assertEqual(lot["sellEmpty"], "a15.lot.sell|false")
        self.assertIsNone(lot["dividend"])
        self.assertIsNone(lot["bonus"])
        self.assertEqual(lot["buyNoCode"], "a15.lot.generic|false")
        self.assertEqual(lot["buyPartialCode"], "a15.lot.generic|false")   # 代码没输完不下结论
        for k in ("buyOk", "buyEmptyQty", "buyEtfOk", "buyEtf159", "buyGem", "etf588"):
            self.assertEqual(lot[k], "a15.lot.buy|false", k)
        for k in ("buyWarn", "buyEtfWarn", "buyShenzhen", "etf588Warn"):    # 588 开头是科创板 ETF，按 100 份一手，不是 200
            self.assertEqual(lot[k], "a15.lot.warn_buy|true", k)
        for k in ("star200", "star201", "starEmpty"):
            self.assertEqual(lot[k], "a15.lot.star|false", k)               # 200 起买之后任意整数股
        for k in ("star100", "star199", "star689", "buyPartialStar"):
            self.assertEqual(lot[k], "a15.lot.warn_star|true", k)

    # ---------- 表单 ----------
    def test_static_structure(self):
        for out in (self.merged, self.unmerged):
            for lang in ("zh", "en"):
                s = out[f"{lang}.static"]
                self.assertEqual((s["qtyType"], s["qtyMode"]), ("text", "numeric"))
                self.assertEqual((s["qtyDesc"], s["priceDesc"], s["feeDesc"]), ("holdQtyHint", "holdPriceHint", "holdFeeHint"))
                self.assertEqual(s["hintIds"], [True, True, True])

    def test_fee_placeholder_and_hint_after_merge(self):
        for lang, d in (("zh", ZH), ("en", EN)):
            m = self.merged
            self.assertEqual(m[f"{lang}.initial"]["feePlaceholder"], d["a15.fee.placeholder"])
            self.assertEqual(m[f"{lang}.dividend"]["feePlaceholder"], d["a15.fee.placeholder_div"])
            self.assertEqual(m[f"{lang}.dividend"]["feeHint"], d["a15.fee.hint_div"])
            self.assertEqual(m[f"{lang}.initial"]["priceHint"], d["a15.price.hint"])
            self.assertEqual(m[f"{lang}.backToBuy"]["feePlaceholder"], d["a15.fee.placeholder"])
            self.assertEqual(m[f"{lang}.backToBuy"]["feeHint"], m[f"{lang}.initial"]["feeHint"])    # 切回买入：恢复原来的估算说明
        self.assertIn("留空", self.merged["zh.initial"]["feePlaceholder"])
        self.assertIn("估算", self.merged["zh.initial"]["feePlaceholder"])

    def test_lot_hint_in_form(self):
        for lang, d in (("zh", ZH), ("en", EN)):
            m = self.merged
            self.assertEqual(m[f"{lang}.initial"]["qtyHint"], d["a15.lot.generic"])
            self.assertEqual((m[f"{lang}.etf150"]["qtyHint"], m[f"{lang}.etf150"]["qtyWarn"]), (d["a15.lot.warn_buy"], "1"))
            self.assertEqual((m[f"{lang}.etf300"]["qtyHint"], m[f"{lang}.etf300"]["qtyWarn"]), (d["a15.lot.buy"], ""))
            self.assertEqual((m[f"{lang}.star100"]["qtyHint"], m[f"{lang}.star100"]["qtyWarn"]), (d["a15.lot.warn_star"], "1"))
            self.assertEqual((m[f"{lang}.star200"]["qtyHint"], m[f"{lang}.star200"]["qtyWarn"]), (d["a15.lot.star"], ""))
            self.assertEqual(m[f"{lang}.sell"]["qtyHint"], d["a15.lot.sell"])
            self.assertEqual(m[f"{lang}.dividend"]["qtyHint"], "")
            self.assertEqual(m[f"{lang}.bonus"]["qtyHint"], "")
            self.assertEqual(m[f"{lang}.restored"]["qtyHint"], d["a15.lot.warn_star"])     # 切换语言重画后放回输入值，提示跟着更新

    def test_lot_warning_never_blocks_submit(self):
        for out in (self.merged, self.unmerged):
            for lang in ("zh", "en"):
                r = out[f"{lang}.lot_not_blocking"]
                self.assertTrue(r["confirmOpen"])
                self.assertIn("150", r["confirmText"])

    def test_invalid_input_blocks_confirm_marks_and_focuses(self):
        expect = {"bad_qty0": (["holdQty"], 1), "bad_qty_frac": (["holdQty"], 1), "bad_price_neg": (["holdPrice"], 1),
                  "bad_price_zero": (["holdPrice"], 1), "bad_price_dec": (["holdPrice"], 1), "bad_fee_neg": (["holdFee"], 1),
                  "bad_fee_over": (["holdFee"], 1), "bad_multi": (["holdQty", "holdPrice", "holdFee"], 3)}
        for out in (self.merged, self.unmerged):
            for lang in ("zh", "en"):
                for k, (invalid, count) in expect.items():
                    r = out[f"{lang}.{k}"]
                    self.assertFalse(r["confirmOpen"], k)
                    self.assertEqual((r["invalid"], r["msgCount"]), (invalid, count), k)
                    self.assertEqual(r["focus"], f"input#{invalid[0]}", k)
                    self.assertTrue(r["msg"].strip(), k)

    def test_error_text_by_language_and_no_key_names_before_merge(self):
        m, u = self.merged, self.unmerged
        self.assertEqual(m["zh.bad_qty0"]["msg"], ZH["a15.err.qty"])
        self.assertEqual(m["en.bad_qty0"]["msg"], EN["a15.err.qty"])
        self.assertEqual(m["zh.bad_fee_over"]["msg"], ZH["a15.err.fee_over"])
        self.assertEqual(m["en.bad_price_neg"]["msg"], EN["a15.err.price"])
        # 词条没合并：退回基础字典里已有的「数量无效」「价格无效」「费用无效」，不露键名
        base_zh = json.loads((UI / "i18n" / "zh.json").read_text(encoding="utf-8"))
        base_en = json.loads((UI / "i18n" / "en.json").read_text(encoding="utf-8"))
        self.assertEqual(u["zh.bad_qty0"]["msg"], base_zh["hold.err.HOLDINGS_BAD_QTY"])
        self.assertEqual(u["en.bad_price_neg"]["msg"], base_en["hold.err.HOLDINGS_BAD_PRICE"])
        for out in (m, u):
            for key, val in out.items():
                if key[:3] in ('zh.', 'en.') and isinstance(val, dict):   # 只扫表单输出（纯函数结果里本来就有键名）
                    for v in val.values():
                        self.assertNotRegex(str(v), r"a15\.", key)
        for lang in ("zh", "en"):
            i = u[f"{lang}.initial"]
            self.assertEqual((i["qtyHint"], i["priceHint"]), ("", ""))       # 未合并：不显示提示，也不露键名
        self.assertEqual(u["zh.initial"]["feePlaceholder"], base_zh["hold.form.fee_auto"])
        self.assertEqual(u["en.initial"]["feePlaceholder"], base_en["hold.form.fee_auto"])

    def test_aria_invalid_cleared_only_for_edited_field(self):
        for lang in ("zh", "en"):
            # bad_multi 之后只改数量框：数量的标红清掉，价格 / 费用的还在
            self.assertEqual(self.merged[f"{lang}.invalid_cleared"], ["holdPrice", "holdFee"])

    def test_incomplete_message_unchanged(self):
        base_zh = json.loads((UI / "i18n" / "zh.json").read_text(encoding="utf-8"))
        r = self.merged["zh.incomplete"]
        self.assertFalse(r["confirmOpen"])
        self.assertEqual(r["msg"], base_zh["hold.form.incomplete"])
        self.assertEqual(r["invalid"], [])

    def test_valid_inputs_open_confirm(self):
        for out in (self.merged, self.unmerged):
            for lang in ("zh", "en"):
                r = out[f"{lang}.ok_commas"]
                self.assertTrue(r["confirmOpen"])
                self.assertIn("1000", r["confirmText"])         # 「1,000」按 1000 提交
                self.assertNotIn("1,000", r["confirmText"])
                self.assertEqual((r["msgCount"], r["invalid"]), (0, []))
                self.assertTrue(out[f"{lang}.ok_fee_zero"]["confirmOpen"])   # 明确填 0 是合法的（与留空不同：留空 = 估算）
                self.assertTrue(out[f"{lang}.bonus_confirm"]["confirmOpen"])

    def test_confirm_fee_wording_buy_vs_dividend(self):
        for out in (self.merged, self.unmerged):
            self.assertIn("按费率估算", out["zh.buy_confirm"]["confirmText"])
            self.assertIn("estimated from rates", out["en.buy_confirm"]["confirmText"])
            self.assertIn("费用 0 元", out["zh.dividend_confirm"]["confirmText"])     # 分红留空 = 0，不写成估算
            self.assertNotIn("估算", out["zh.dividend_confirm"]["confirmText"])
            self.assertNotIn("estimated", out["en.dividend_confirm"]["confirmText"])


if __name__ == "__main__":
    unittest.main()
