"""T008 S0:策略清单校验与哈希测试。只读包内清单,临时文件放临时目录,不碰 data/。"""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.replay import registry as R  # noqa: E402


def fresh():
    return copy.deepcopy(R.load_catalog())


class TestRealCatalog(unittest.TestCase):
    def test_passes_validation(self):
        cat, h = R.load_validated()
        self.assertEqual(len(cat["strategies"]), 100)
        self.assertEqual(len(h), 64)

    def test_category_counts(self):
        cat = fresh()
        got = {}
        for s in cat["strategies"]:
            got[s["category"]] = got.get(s["category"], 0) + 1
        self.assertEqual(got, R.EXPECTED_COUNTS)

    def test_controls_fixed_seeds(self):
        ctrl = [s for s in fresh()["strategies"] if s["category"] == "随机对照"]
        self.assertEqual(len(ctrl), 5)
        self.assertEqual(len({s["seed"] for s in ctrl}), 5)
        self.assertTrue(all(s["id"].startswith("ctrl_") for s in ctrl))

    def test_hash_stable(self):
        self.assertEqual(R.catalog_hash(fresh()), R.catalog_hash(fresh()))

    def test_segments_locked(self):
        # 三段日期写死:改日期必须同时改这里(有意识地升版本)
        self.assertEqual(fresh()["segments"], {
            "train": ["2024-01-01", "2025-06-30"],
            "valid": ["2025-07-01", "2026-03-31"],
            "holdout": ["2026-04-01", None],
        })

    def test_hash_locked_for_v1_1(self):
        # 锁定 1.1 版哈希:任何清单改动都会让这条失败,强制同步改测试并升版本号
        cat, h = R.load_validated()
        self.assertEqual(cat["catalog_version"], "1.1")
        # 1.0 版哈希:1befe4e7…(无 fixed_params)、276c02d5…(加 fixed_params 但未升版本);1.1 起版本号与之同步
        self.assertEqual(h, "d7c0d3b4992e8b96d055c173d1f82b161a2920cf7e93cd9f30668c8af9d54d48")

    def test_directions(self):
        for s in fresh()["strategies"]:
            want = "random" if s["category"] == "随机对照" else "higher_better"
            self.assertEqual(s["direction"], want, s["id"])

    def test_no_degenerate_ma_grid(self):
        ma02 = next(s for s in fresh()["strategies"] if s["id"] == "ma_02")
        self.assertLess(max(ma02["params"]["short"]), min(ma02["params"]["long"]))

    def test_total_trials_positive(self):
        cat = fresh()
        self.assertGreater(R.total_trials(cat), 100)
        self.assertEqual(R.grid_size(cat["strategies"][-1]), 1)


class TestValidationRules(unittest.TestCase):
    def bad(self, mutate, needle):
        cat = fresh()
        mutate(cat)
        errs = R.check_catalog(cat)
        self.assertTrue(any(needle in e for e in errs), errs)
        with self.assertRaises(R.CatalogError):
            R.validate_catalog(cat)

    def test_duplicate_id(self):
        def m(c):
            c["strategies"][1]["id"] = c["strategies"][0]["id"]
        self.bad(m, "ID 重复")

    def test_too_many_params(self):
        def m(c):
            c["strategies"][0]["params"] = {k: [1, 2, 3] for k in "abcd"}
        self.bad(m, "超过上限")

    def test_grid_too_long_and_short(self):
        def long(c):
            c["strategies"][0]["params"] = {"n": [1, 2, 3, 4, 5, 6]}
        self.bad(long, "3~5 档")

        def short(c):
            c["strategies"][0]["params"] = {"n": [1, 2]}
        self.bad(short, "3~5 档")

    def test_grid_duplicates_and_order(self):
        def dup(c):
            c["strategies"][0]["params"] = {"n": [1, 1, 2]}
        self.bad(dup, "重复")

        def order(c):
            c["strategies"][0]["params"] = {"n": [3, 2, 1]}
        self.bad(order, "升序")

    def test_total_count(self):
        self.bad(lambda c: c["strategies"].pop(), "应为 100")

    def test_category_count(self):
        def m(c):
            c["strategies"][0]["category"] = "反转"
        self.bad(m, "类别")

    def test_control_without_seed(self):
        def m(c):
            del c["strategies"][-1]["seed"]
        self.bad(m, "固定种子")

    def test_control_duplicate_seed(self):
        def m(c):
            c["strategies"][-1]["seed"] = c["strategies"][-2]["seed"]
        self.bad(m, "种子必须互不相同")

    def test_non_control_with_seed(self):
        def m(c):
            c["strategies"][0]["seed"] = 1
        self.bad(m, "不应有 seed")

    def test_missing_point_in_time(self):
        def m(c):
            c["strategies"][3]["point_in_time"] = ""
        self.bad(m, "T 日收盘")

    def test_bad_version(self):
        def m(c):
            c["strategies"][3]["version"] = "v1"
        self.bad(m, "版本号")

    def test_segments_order(self):
        def m(c):
            c["segments"]["valid"] = ["2023-01-01", "2023-06-30"]
        self.bad(m, "顺序")

    def test_segments_missing_or_malformed(self):
        self.bad(lambda c: c.pop("segments"), "segments")
        self.bad(lambda c: c["segments"].pop("holdout"), "segments")
        self.bad(lambda c: c["segments"].__setitem__("train", ["2024-01-01"]), "格式")

    def test_segments_impossible_date(self):
        self.bad(lambda c: c["segments"]["train"].__setitem__(0, "2024-13-45"), "格式")

    def test_direction_flip_rejected(self):
        self.bad(lambda c: c["strategies"][0].__setitem__("direction", "lower_better"), "direction")
        self.bad(lambda c: c["strategies"][0].pop("direction"), "缺字段")
        self.bad(lambda c: c["strategies"][-1].__setitem__("direction", "higher_better"), "direction")

    def test_unknown_category(self):
        self.bad(lambda c: c["strategies"][0].__setitem__("category", "神秘"), "未知类别")

    def test_bad_id_format(self):
        self.bad(lambda c: c["strategies"][0].__setitem__("id", "Bad-ID"), "ID 须为")

    def test_ctrl_prefix_misuse(self):
        self.bad(lambda c: c["strategies"][0].__setitem__("id", "ctrl_mom_01"), "前缀")
        self.bad(lambda c: c["strategies"][-1].__setitem__("id", "rand_05"), "前缀")

    def test_empty_name(self):
        self.bad(lambda c: c["strategies"][0].__setitem__("name", "  "), "name")

    def test_bad_expected_total(self):
        self.bad(lambda c: c.__setitem__("expected_total", 99), "expected_total")

    def test_not_dict(self):
        self.assertTrue(R.check_catalog([]))
        self.assertTrue(R.check_catalog({"strategies": 3}))


class TestHash(unittest.TestCase):
    def test_any_change_changes_hash(self):
        base = R.catalog_hash(fresh())
        edits = [
            lambda c: c["strategies"][0].__setitem__("description", "x"),
            lambda c: c["strategies"][0].__setitem__("version", "1.1"),
            lambda c: c["strategies"][0]["params"]["n"].__setitem__(0, 4),
            lambda c: c["strategies"][-1].__setitem__("seed", 1),
            lambda c: c["segments"]["train"].__setitem__(0, "2024-01-02"),
        ]
        for e in edits:
            c = fresh()
            e(c)
            self.assertNotEqual(R.catalog_hash(c), base)

    def test_key_order_and_indent_irrelevant(self):
        c = fresh()
        reordered = {k: c[k] for k in sorted(c, reverse=True)}
        self.assertEqual(R.catalog_hash(reordered), R.catalog_hash(c))
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.json"
            p.write_text(json.dumps(c, ensure_ascii=False, indent=4), encoding="utf-8")
            self.assertEqual(R.catalog_hash(R.load_catalog(p)), R.catalog_hash(c))

    def test_load_validated_rejects_bad_file(self):
        c = fresh()
        c["strategies"].pop()
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.json"
            p.write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(R.CatalogError):
                R.load_validated(p)


def _by_id(c, sid):
    return next(s for s in c["strategies"] if s["id"] == sid)


class TestFixedParams(unittest.TestCase):
    EXPECT = {
        "sq_05": {"SQUEEZE_Q": 0.2},
        "cmb_07": {"CMB07_REV_N": 5},
        "brk_05": {"breakout_mode": "last"},
        "ext_05": {"LIMIT_TOUCH": 0.095},
        "sq_06": {"YEAR": 250},
        "h52_02": {"YEAR": 250},
        "cmb_04": {"equal_weight": 0.5},
        "cmb_05": {"equal_weight": 0.5},
        "cmb_06": {"equal_weight": 0.5},
        "cmb_09": {"equal_weight": 0.5},
    }

    def test_catalog_registers_fixed_params(self):
        c = fresh()
        for sid, want in self.EXPECT.items():
            self.assertEqual(_by_id(c, sid).get("fixed_params"), want, sid)

    def test_fixed_params_change_hash_not_trials(self):
        c = fresh()
        h0, t0 = R.catalog_hash(c), R.total_trials(c)
        _by_id(c, "sq_05")["fixed_params"]["SQUEEZE_Q"] = 0.3
        self.assertNotEqual(R.catalog_hash(c), h0)
        self.assertEqual(R.total_trials(c), t0)
        self.assertEqual(R.grid_size(_by_id(c, "sq_05")), R.grid_size(_by_id(fresh(), "sq_05")))
        R.validate_catalog(c)

    def test_params_change_hash_and_trials_fixed_independent(self):
        # 改 params:哈希变且试验次数变(多一档网格);改 fixed_params:只有哈希变
        c = fresh()
        h0, t0 = R.catalog_hash(c), R.total_trials(c)
        _by_id(c, "mom_01")["params"]["n"].append(250)
        h1 = R.catalog_hash(c)
        self.assertNotEqual(h1, h0)
        self.assertEqual(R.total_trials(c), t0 + 1 * 1)
        _by_id(c, "cmb_07")["fixed_params"]["CMB07_REV_N"] = 10
        self.assertNotEqual(R.catalog_hash(c), h1)
        self.assertEqual(R.total_trials(c), t0 + 1)

    def test_hash_differs_from_old_catalog_without_fixed_params(self):
        c = fresh()
        old = fresh()
        for s in old["strategies"]:
            s.pop("fixed_params", None)
        self.assertNotEqual(R.catalog_hash(c), R.catalog_hash(old))
        self.assertEqual(R.total_trials(c), R.total_trials(old))
        R.validate_catalog(old)          # 没有该字段的旧格式仍合规(字段可选)

    def test_rejects_bad_fixed_params(self):
        for bad in ({}, [], "x", {"a": None}, {"a": [1]}, {"a": True}, {"a": ""}, {"n": 1}):
            c = fresh()
            _by_id(c, "mom_01")["fixed_params"] = bad
            self.assertTrue(R.check_catalog(c), repr(bad))


if __name__ == "__main__":
    unittest.main()
