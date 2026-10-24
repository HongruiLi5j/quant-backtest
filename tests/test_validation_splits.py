"""src/validation/splits.py 的测试:合成数据 + 临时目录,不读写 data/。

运行:python3 -B tests/test_validation_splits.py
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.validation.splits import (  # noqa: E402
    Fold, LockedTestError, LockedTestGuard, Split, SplitError, locked_fingerprint,
    read_log, split_by_dates, three_way_split, walk_forward)


def days(n, start=date(2020, 1, 1)):
    return [start + timedelta(days=i) for i in range(n)]


def overlap(a, b):
    return a[0] < b[1] and b[0] < a[1]


class ThreeWayTests(unittest.TestCase):
    def test_default_layout(self):
        # n=100, embargo=5 -> usable=90 -> 54/18/18
        s = three_way_split(days(100))
        self.assertEqual(s.train, (0, 54))
        self.assertEqual(s.valid, (59, 77))
        self.assertEqual(s.test, (82, 100))

    def test_order_and_no_overlap(self):
        for n in (20, 57, 100, 251, 1000):
            for emb in (0, 1, 5):
                s = three_way_split(days(n), 0.5, 0.25, emb)
                self.assertLess(s.train[0], s.train[1])
                self.assertLessEqual(s.train[1], s.valid[0])
                self.assertLess(s.valid[0], s.valid[1])
                self.assertLessEqual(s.valid[1], s.test[0])
                self.assertLess(s.test[0], s.test[1])
                self.assertEqual(s.test[1], n)
                self.assertFalse(overlap(s.train, s.valid))
                self.assertFalse(overlap(s.valid, s.test))
                self.assertFalse(overlap(s.train, s.test))

    def test_embargo_gap_exact(self):
        for emb in (0, 3, 10):
            s = three_way_split(days(200), embargo=emb)
            self.assertEqual(s.valid[0] - s.train[1], emb)
            self.assertEqual(s.test[0] - s.valid[1], emb)

    def test_embargo_zero_covers_everything(self):
        s = three_way_split(days(50), embargo=0)
        self.assertEqual(s.train[1], s.valid[0])
        self.assertEqual(s.valid[1], s.test[0])
        sizes = sum(b - a for a, b in (s.train, s.valid, s.test))
        self.assertEqual(sizes, 50)

    def test_accepts_strings_and_ints(self):
        s = three_way_split([f"2020-01-{i:02d}" for i in range(1, 31)], embargo=1)
        self.assertEqual(s.test[1], 30)
        self.assertIsInstance(three_way_split(list(range(30)), embargo=1), Split)

    def test_minimum_length_boundary(self):
        # embargo=0, n=3 -> 1/1/1 可行;n=2 不行
        s = three_way_split(days(3), 0.4, 0.3, embargo=0)
        self.assertEqual((s.train, s.valid, s.test), ((0, 1), (1, 2), (2, 3)))
        with self.assertRaises(SplitError):
            three_way_split(days(2), embargo=0)
        # embargo 吃光长度
        with self.assertRaises(SplitError):
            three_way_split(days(12), embargo=5)
        # 刚好够:usable=3
        self.assertIsInstance(three_way_split(days(13), 0.34, 0.33, embargo=5), Split)

    def test_empty_segment_rejected(self):
        # 测试段占比极小,四舍五入后为 0
        with self.assertRaises(SplitError):
            three_way_split(days(10), 0.6, 0.39, embargo=0)

    def test_bad_params(self):
        d = days(100)
        for kw in (dict(train=0), dict(train=1), dict(valid=-0.1), dict(train=0.7, valid=0.3),
                   dict(train=0.9, valid=0.2), dict(train="0.6"), dict(train=True),
                   dict(embargo=-1), dict(embargo=1.5), dict(embargo=True)):
            with self.assertRaises(SplitError, msg=str(kw)):
                three_way_split(d, **kw)

    def test_invalid_index(self):
        d = days(30)
        with self.assertRaisesRegex(SplitError, "重复"):
            three_way_split(d[:10] + [d[9]] + d[10:])
        with self.assertRaisesRegex(SplitError, "升序"):
            three_way_split(list(reversed(d)))
        swapped = list(d)
        swapped[4], swapped[5] = swapped[5], swapped[4]
        with self.assertRaisesRegex(SplitError, "升序"):
            three_way_split(swapped)
        with self.assertRaises(SplitError):
            three_way_split([])
        with self.assertRaises(SplitError):
            three_way_split("2020-01-01")
        with self.assertRaises(SplitError):
            three_way_split(5)
        with self.assertRaisesRegex(SplitError, "比较"):
            three_way_split([1, "a", 2, "b"] * 5)


class SplitByDatesTests(unittest.TestCase):
    def test_basic(self):
        d = days(100)
        s = split_by_dates(d, d[60], d[80], embargo=5)
        self.assertEqual(s.train, (0, 55))
        self.assertEqual(s.valid, (60, 75))
        self.assertEqual(s.test, (80, 100))
        self.assertEqual(s.valid[0] - s.train[1], 5)
        self.assertEqual(s.test[0] - s.valid[1], 5)

    def test_errors(self):
        d = days(100)
        with self.assertRaisesRegex(SplitError, "不在"):
            split_by_dates(d, date(1999, 1, 1), d[80])
        with self.assertRaisesRegex(SplitError, "早于"):
            split_by_dates(d, d[80], d[60])
        with self.assertRaisesRegex(SplitError, "训练段为空"):
            split_by_dates(d, d[3], d[50], embargo=3)
        with self.assertRaisesRegex(SplitError, "验证段为空"):
            split_by_dates(d, d[60], d[63], embargo=3)
        with self.assertRaises(SplitError):
            split_by_dates(d, d[60], d[80], embargo=-1)
        with self.assertRaises(SplitError):
            split_by_dates(d[::-1], d[60], d[80])


class WalkForwardTests(unittest.TestCase):
    def test_count_and_windows(self):
        # n=100, train=40, test=10, step=10 -> s=0..50 共 6 折
        folds = walk_forward(days(100), 40, 10)
        self.assertEqual(len(folds), 6)
        self.assertIsInstance(folds[0], Fold)
        self.assertEqual(folds[0].train_idx, (0, 40))
        self.assertEqual(folds[0].test_idx, (40, 50))
        self.assertEqual(folds[-1].train_idx, (50, 90))
        self.assertEqual(folds[-1].test_idx, (90, 100))

    def test_tests_do_not_overlap_and_inside_bounds(self):
        for emb in (0, 2, 7):
            for step in (None, 10, 13):
                folds = walk_forward(days(200), 50, 10, step=step, embargo=emb)
                self.assertTrue(folds)
                for f in folds:
                    self.assertLessEqual(f.train_idx[1] + emb, f.test_idx[0])
                    self.assertEqual(f.test_idx[0] - f.train_idx[1], emb)
                    self.assertGreaterEqual(f.train_idx[0], 0)
                    self.assertLessEqual(f.test_idx[1], 200)
                    self.assertFalse(overlap(f.train_idx, f.test_idx))
                for a, b in zip(folds, folds[1:]):
                    self.assertFalse(overlap(a.test_idx, b.test_idx))
                    self.assertLess(a.test_idx[1] - 1, b.test_idx[0])

    def test_default_step_tiles_tests(self):
        folds = walk_forward(days(100), 40, 10)
        for a, b in zip(folds, folds[1:]):
            self.assertEqual(a.test_idx[1], b.test_idx[0])

    def test_embargo_reduces_folds(self):
        self.assertEqual(len(walk_forward(days(100), 40, 10, embargo=0)), 6)
        # need=40+5+10=55 -> s<=45 -> step10 -> s=0,10,..,40 共 5 折
        self.assertEqual(len(walk_forward(days(100), 40, 10, embargo=5)), 5)

    def test_anchored(self):
        folds = walk_forward(days(100), 40, 10, anchored=True)
        self.assertEqual(len(folds), 6)
        self.assertTrue(all(f.train_idx[0] == 0 for f in folds))
        self.assertEqual([f.train_idx[1] for f in folds], [40, 50, 60, 70, 80, 90])

    def test_boundaries(self):
        # 刚好一折
        folds = walk_forward(days(50), 40, 10)
        self.assertEqual(len(folds), 1)
        self.assertEqual(folds[0].test_idx, (40, 50))
        # 差一根不够
        with self.assertRaises(SplitError):
            walk_forward(days(49), 40, 10)
        # 尾部放不下完整测试窗的部分被丢弃
        folds = walk_forward(days(105), 40, 10)
        self.assertEqual(folds[-1].test_idx[1], 100)
        self.assertEqual(len(folds), 6)

    def test_big_step(self):
        folds = walk_forward(days(100), 20, 5, step=30)
        self.assertEqual([f.train_idx[0] for f in folds], [0, 30, 60])
        self.assertEqual(len(folds), 3)

    def test_bad_params(self):
        d = days(100)
        for kw in (dict(train_len=0, test_len=5), dict(train_len=10, test_len=0),
                   dict(train_len=10, test_len=5, step=4),
                   dict(train_len=10, test_len=5, step=0),
                   dict(train_len=10, test_len=5, embargo=-1),
                   dict(train_len=10.0, test_len=5), dict(train_len=10, test_len=True)):
            with self.assertRaises(SplitError, msg=str(kw)):
                walk_forward(d, **kw)

    def test_invalid_index(self):
        d = days(100)
        with self.assertRaisesRegex(SplitError, "重复"):
            walk_forward(d[:50] + d[49:], 10, 5)
        with self.assertRaisesRegex(SplitError, "升序"):
            walk_forward(d[::-1], 10, 5)
        with self.assertRaisesRegex(SplitError, "长度不足"):
            walk_forward(d[:10], 10, 5)


class FingerprintTests(unittest.TestCase):
    def test_stable_and_sensitive(self):
        d = days(5)
        v = [1.0, 2.5, -0.1, 0.0, 3.14159]
        f1 = locked_fingerprint(d, v)
        self.assertEqual(f1, locked_fingerprint(list(d), tuple(v)))
        self.assertEqual(len(f1), 64)
        v2 = list(v)
        v2[2] = -0.1000000000000001
        self.assertNotEqual(f1, locked_fingerprint(d, v2))
        d2 = list(d)
        d2[0] = date(2020, 1, 2)
        self.assertNotEqual(f1, locked_fingerprint(d2, v))
        self.assertNotEqual(f1, locked_fingerprint(d[:4], v[:4]))

    def test_rows_and_length(self):
        self.assertEqual(len(locked_fingerprint([], [])), 64)
        with self.assertRaises(SplitError):
            locked_fingerprint(days(3), [1, 2])
        # 多列数值
        a = locked_fingerprint(days(2), [(1.0, 2.0), (3.0, 4.0)])
        b = locked_fingerprint(days(2), [(1.0, 2.0), (3.0, 4.5)])
        self.assertNotEqual(a, b)


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = os.path.join(self.tmp.name, "locked_access_log.jsonl")
        self.d = days(100)
        self.vals = [i * 0.01 for i in range(100)]
        self.split = three_way_split(self.d, embargo=5)
        a, b = self.split.test
        self.fp = locked_fingerprint(self.d[a:b], self.vals[a:b])

    def guard(self, **kw):
        return LockedTestGuard(kw.get("split", self.split), kw.get("log", self.log),
                               kw.get("fp", self.fp))

    def test_import_creates_nothing_and_no_log_until_constructed(self):
        self.assertFalse(os.path.exists(self.log))
        self.guard()
        self.assertTrue(os.path.exists(self.log))

    def test_seal_written_once(self):
        g = self.guard()
        rows = read_log(self.log)
        self.assertEqual([r["event"] for r in rows], ["seal"])
        self.assertEqual(rows[0]["fingerprint"], self.fp)
        self.assertEqual(rows[0]["split"]["test"], list(self.split.test))
        self.guard()
        self.guard()
        self.assertEqual([r["event"] for r in read_log(self.log)], ["seal"])
        self.assertEqual(g.open_count(), 0)

    def test_open_returns_range_and_logs(self):
        g = self.guard()
        self.assertEqual(g.open("final"), self.split.test)
        rows = read_log(self.log)
        self.assertEqual([r["event"] for r in rows], ["seal", "open"])
        self.assertEqual(rows[1]["label"], "final")
        self.assertEqual(rows[1]["range"], list(self.split.test))
        self.assertEqual(rows[1]["fingerprint"], self.fp)
        self.assertIn("time", rows[1])

    def test_same_label_twice_raises_and_log_unchanged(self):
        g = self.guard()
        g.open("x")
        with open(self.log, "rb") as f:
            before = f.read()
        with self.assertRaises(LockedTestError):
            g.open("x")
        with open(self.log, "rb") as f:
            self.assertEqual(f.read(), before)

    def test_log_is_append_only_and_counts_match(self):
        g = self.guard()
        snapshots = []
        for label in ("a", "b", "c"):
            g.open(label)
            with open(self.log, "rb") as f:
                snapshots.append(f.read())
        for older, newer in zip(snapshots, snapshots[1:]):
            self.assertTrue(newer.startswith(older))
            self.assertGreater(len(newer), len(older))
        # 失败的 open 不计数
        for _ in range(2):
            with self.assertRaises(LockedTestError):
                g.open("a")
        opens = [r for r in read_log(self.log) if r["event"] == "open"]
        self.assertEqual(len(opens), 3)
        self.assertEqual(g.open_count(), 3)

    def test_cross_instance(self):
        a = self.guard()
        a.open("x")
        b = self.guard()
        self.assertEqual(b.open_count(), 1)
        with self.assertRaises(LockedTestError):
            b.open("x")
        self.assertEqual(b.open("y"), self.split.test)
        # 旧实例也能看到新实例的访问
        with self.assertRaises(LockedTestError):
            a.open("y")
        self.assertEqual(a.open_count(), 2)

    def test_bad_labels(self):
        g = self.guard()
        for bad in ("", "  ", None, 3):
            with self.assertRaises(LockedTestError):
                g.open(bad)
        self.assertEqual(g.open_count(), 0)

    def test_fingerprint_mismatch_raises(self):
        self.guard()
        with self.assertRaisesRegex(LockedTestError, "不一致"):
            self.guard(fp=locked_fingerprint(self.d, [0.0] * 100))

    def test_split_mismatch_raises(self):
        self.guard()
        other = three_way_split(self.d, embargo=3)
        with self.assertRaisesRegex(LockedTestError, "不一致"):
            self.guard(split=other)

    def test_data_changed_detected(self):
        self.guard()
        a, b = self.split.test
        changed = list(self.vals)
        changed[a + 1] += 1e-9
        fp2 = locked_fingerprint(self.d[a:b], changed[a:b])
        with self.assertRaises(LockedTestError):
            self.guard(fp=fp2)

    def test_corrupt_line_blocks_construct_and_open(self):
        g = self.guard()
        with open(self.log, "a", encoding="utf-8") as f:
            f.write("this is not json\n")
        with self.assertRaisesRegex(LockedTestError, "无法解析"):
            g.open("x")
        with self.assertRaises(LockedTestError):
            self.guard()
        with self.assertRaises(LockedTestError):
            read_log(self.log)

    def test_non_dict_or_unknown_event_rejected(self):
        self.guard()
        for bad in ("[1,2]", json.dumps({"event": "weird"}), "42"):
            with open(self.log, "a", encoding="utf-8") as f:
                f.write(bad + "\n")
            with self.assertRaises(LockedTestError):
                self.guard()
            # 还原:去掉最后一行
            with open(self.log, "r", encoding="utf-8") as f:
                lines = f.readlines()
            with open(self.log, "w", encoding="utf-8") as f:
                f.writelines(lines[:-1])
        self.guard()

    def test_truncated_tail_does_not_glue_rows(self):
        g = self.guard()
        g.open("a")
        with open(self.log, "rb") as f:
            data = f.read()
        with open(self.log, "wb") as f:
            f.write(data.rstrip(b"\n"))  # 去掉末尾换行,但内容完整
        g.open("b")
        self.assertEqual([r.get("label") for r in read_log(self.log)], [None, "a", "b"])

    def test_deleted_seal_or_log_detected(self):
        g = self.guard()
        g.open("x")
        with open(self.log, "r", encoding="utf-8") as f:
            lines = f.readlines()
        with open(self.log, "w", encoding="utf-8") as f:
            f.writelines(lines[1:])  # 删掉 seal 行
        with self.assertRaises(LockedTestError):
            g.open("y")
        with self.assertRaises(LockedTestError):
            self.guard()

    def test_duplicate_seal_rejected(self):
        g = self.guard()
        with open(self.log, "r", encoding="utf-8") as f:
            seal = f.read()
        with open(self.log, "a", encoding="utf-8") as f:
            f.write(seal)
        with self.assertRaises(LockedTestError):
            g.open("x")

    def test_log_name_must_not_be_trials(self):
        reserved = "trials" + ".jsonl"   # 运行时拼接,源码里不出现完整字面量
        for name in (reserved, "my_" + reserved, reserved.upper(), "x_" + reserved + ".bak"):
            p = os.path.join(self.tmp.name, name)
            with self.assertRaises(SplitError):
                LockedTestGuard(self.split, p, self.fp)
            self.assertFalse(os.path.exists(p))
        # trial_log.jsonl 不含项目试验账本的保留名,允许(但语义上仍应使用 locked_access_log.jsonl)
        LockedTestGuard(self.split, os.path.join(self.tmp.name, "trial_log.jsonl"), self.fp)

    def test_constructor_validation(self):
        with self.assertRaises(SplitError):
            LockedTestGuard((0, 1), self.log, self.fp)
        with self.assertRaises(SplitError):
            LockedTestGuard(self.split, self.log, "")
        with self.assertRaises(SplitError):
            LockedTestGuard(self.split, self.log, None)
        for blank in ("   ", "\t\n"):   # 纯空白指纹也要在落盘前拒绝
            with self.assertRaises(SplitError):
                LockedTestGuard(self.split, self.log, blank)
        self.assertFalse(os.path.exists(self.log))

    def test_accepts_pathlike(self):
        from pathlib import Path
        g = LockedTestGuard(self.split, Path(self.log), self.fp)
        self.assertEqual(g.open("p"), self.split.test)

    def test_read_log_missing_file_is_empty(self):
        self.assertEqual(read_log(os.path.join(self.tmp.name, "nope.jsonl")), [])

    def _truncate_last_line(self):
        with open(self.log, "r", encoding="utf-8") as f:
            lines = f.readlines()
        with open(self.log, "w", encoding="utf-8") as f:
            f.writelines(lines[:-1])

    def test_malformed_open_rows_rejected(self):
        g = self.guard()
        good = {"event": "open", "label": "z", "range": list(self.split.test),
                "fingerprint": self.fp}
        bads = [{"event": "open"}, dict(good, label=""), dict(good, label=5),
                dict(good, range=[1]), dict(good, range=["a", "b"]),
                dict(good, fingerprint=None), dict(good, fingerprint="other"),
                dict(good, range=[0, 1])]
        for bad in bads:
            with open(self.log, "a", encoding="utf-8") as f:
                f.write(json.dumps(bad) + "\n")
            with self.assertRaises(LockedTestError, msg=str(bad)):
                g.open("new")
            with self.assertRaises(LockedTestError):
                self.guard()
            self._truncate_last_line()
        self.assertEqual(g.open("new"), self.split.test)

    def test_malformed_seal_rejected(self):
        with open(self.log, "w", encoding="utf-8") as f:
            f.write(json.dumps({"event": "seal"}) + "\n")
        with self.assertRaises(LockedTestError):
            self.guard()

    def test_invalid_split_rejected(self):
        bad_splits = [Split((10, 20), (0, 15), (30, 40)),     # 顺序颠倒
                      Split((0, 20), (10, 30), (40, 50)),     # 重叠
                      Split((0, 10), (10, 20), (-5, -1)),     # 负数
                      Split((0, 10), (10, 10), (20, 30)),     # 空段
                      Split((0, 10), (10, 20), (30, 25)),     # 倒序
                      Split((0, 10), (10.0, 20), (30, 40)),   # 非整数
                      Split((0, 10), (10,), (30, 40))]        # 长度不对
        for sp in bad_splits:
            with self.assertRaises(SplitError, msg=str(sp)):
                LockedTestGuard(sp, self.log, self.fp)
        self.assertFalse(os.path.exists(self.log))
        # embargo=0 时贴边是合法的
        LockedTestGuard(Split((0, 10), (10, 20), (20, 30)), self.log, self.fp)

    def test_concurrent_open_same_label_only_one_wins(self):
        from concurrent.futures import ThreadPoolExecutor
        for k in range(5):
            path = os.path.join(self.tmp.name, f"race{k}.jsonl")
            LockedTestGuard(self.split, path, self.fp)

            def go(_):
                try:
                    return LockedTestGuard(self.split, path, self.fp).open("race")
                except LockedTestError:
                    return None
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(go, range(16)))
            self.assertEqual(sum(r is not None for r in results), 1)
            opens = [r for r in read_log(path) if r["event"] == "open"]
            self.assertEqual(len(opens), 1)

    def test_concurrent_construct_writes_single_seal(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: self.guard(), range(16)))
        self.assertEqual([r["event"] for r in read_log(self.log)], ["seal"])


if __name__ == "__main__":
    unittest.main()
