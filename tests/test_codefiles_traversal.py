"""E01：网页「代码」页「我的策略」的路径穿越。

以前 key = "custom_../../app" 会被原样拼成 engines/custom/../../app.py，save() 就能写项目根目录的 app.py，
绕过 FILES 白名单。现在 _slug 用字符白名单拒绝、拼好路径后再核对 resolve().parent，历史目录也一样。

全部在临时目录里跑：ROOT / HIST / CUSTOM_DIR 都指向 tempfile 建的目录，不读写 engines/custom/ 和 data/。
不 import app（import 会读 .secret）：接口那一层用 ast 把 Handler._post_engine 抽出来在桩对象上跑。
"""
import ast
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from engines import codefiles  # noqa: E402

GOOD = "def strategy(df):\n    return df['Close'] * 0\n"
EVIL = "print('pwned')\n"

BAD_KEYS = [
    "custom_../../app",          # 经典 ../ 穿越到项目根目录
    "custom_..%2F..",            # URL 编码的 /（parse_qs 只解一次码，这里是二次编码、解码后仍带 % 的情况）
    "custom_..%2F..%2Fapp",
    "custom_..",
    "custom_a/b",
    "custom_/etc/passwd",        # 绝对路径：Path("/x") / "/etc/passwd" 会直接变成 /etc/passwd
    "custom_..\\..\\app",        # Windows 风格反斜杠
    "custom_",                   # 空 slug：会拼成 engines/custom/.py
    "custom_ ",
    "custom_a.b",
    "custom_a\x00b",
    "custom_ａｐｐ",              # 全角字母，不在白名单里
]


class TempDirs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "proj"
        self.custom = self.root / "engines" / "custom"
        self.hist = self.root / "data" / "daily" / "code_history"
        self.custom.mkdir(parents=True)
        self.app = self.root / "app.py"
        self.app.write_text("# 真 app.py\n", encoding="utf-8")
        for name, val in (("ROOT", self.root), ("CUSTOM_DIR", self.custom), ("HIST", self.hist)):
            p = mock.patch.object(codefiles, name, val)
            p.start()
            self.addCleanup(p.stop)

    def all_files(self):
        return sorted(str(p.relative_to(self.tmp.name)) for p in Path(self.tmp.name).rglob("*"))


class TraversalRejected(TempDirs):
    def test_save_rejects_bad_keys_and_writes_nothing(self):
        before = self.all_files()
        for key in BAD_KEYS:
            with self.assertRaises(KeyError, msg=repr(key)):
                codefiles.save(key, EVIL)
        self.assertEqual(self.all_files(), before)                     # 一个文件、一个目录都没多出来
        self.assertEqual(self.app.read_text(encoding="utf-8"), "# 真 app.py\n")

    def test_read_history_version_reject_bad_keys(self):
        for key in BAD_KEYS:
            for call in (lambda: codefiles.read(key), lambda: codefiles.history(key),
                         lambda: codefiles.version(key, "original")):
                with self.assertRaises(KeyError, msg=repr(key)):
                    call()
        self.assertFalse(self.hist.exists())                          # 历史目录也没被建出来

    def test_history_dir_outside_hist_rejected(self):
        # 即使将来有人放宽了 _slug，_hist_dir 拼好路径后还有 resolve 这道检查
        with mock.patch.object(codefiles, "_slug", lambda k: "x"), \
                mock.patch.object(codefiles, "_is_custom", lambda k: True):
            with self.assertRaises(KeyError):
                codefiles._hist_dir("custom_../../../evil")

    def test_custom_path_resolve_check_without_regex(self):
        # 直接绕开 _slug 调 _custom_path：拼出来跑到 CUSTOM_DIR 外面的一律拒绝
        for slug in ("../../app", "../x", "/etc/passwd", "a/b"):
            with self.assertRaises(KeyError, msg=slug):
                codefiles._custom_path(slug)

    def test_symlink_pointing_outside_rejected(self):
        # engines/custom/link.py 是指向外面 app.py 的符号链接：resolve 后父目录不对，拒绝读写
        link = self.custom / "link.py"
        try:
            os.symlink(self.app, link)
        except (OSError, NotImplementedError):
            self.skipTest("这个系统不能建符号链接")
        with self.assertRaises(KeyError):
            codefiles.save("custom_link", EVIL)
        with self.assertRaises(KeyError):
            codefiles.read("custom_link")
        self.assertEqual(self.app.read_text(encoding="utf-8"), "# 真 app.py\n")

    def test_version_name_traversal_rejected(self):
        codefiles.save("custom_ok", GOOD)
        codefiles.save("custom_ok", GOOD + "# 2\n")
        for v in ("../../../app", "..", "a/b", "original/../original"):
            with self.assertRaises(KeyError, msg=v):
                codefiles.version("custom_ok", v)


class NormalUseStillWorks(TempDirs):
    def test_create_save_history_version_restore(self):
        r = codefiles.create("我的 均线-策略")
        self.assertEqual(r["key"], "custom_我的_均线-策略")
        key = r["key"]
        self.assertTrue((self.custom / "我的_均线-策略.py").exists())

        self.assertEqual(codefiles.save(key, GOOD), {"ok": True})
        self.assertEqual(codefiles.read(key)["content"], GOOD)
        self.assertIn("original", [v["v"] for v in codefiles.history(key)])
        self.assertTrue((self.hist / key / "original.py").exists())

        self.assertEqual(codefiles.save(key, GOOD + "# v2\n"), {"ok": True})
        before = [v["v"] for v in codefiles.history(key) if v["v"] != "original"]
        self.assertTrue(before)
        # 同一秒内两次保存，备份文件名相同会互相覆盖，所以只要求读回来的是保存过的某一版
        self.assertIn(codefiles.version(key, before[0]), (GOOD, codefiles.CUSTOM_TEMPLATE))
        self.assertEqual(codefiles.version(key, "original"), codefiles.CUSTOM_TEMPLATE)
        self.assertEqual(codefiles.save(key, GOOD), {"ok": True})     # 「恢复」= version + save
        self.assertEqual(codefiles.read(key)["content"], GOOD)

    def test_plain_names(self):
        for key in ("custom_ma20_example", "custom_A-b_1", "custom_策略123456"):
            self.assertEqual(codefiles.save(key, GOOD), {"ok": True}, key)
            self.assertTrue((self.custom / f"{key[len('custom_'):]}.py").exists(), key)
        listed = {f["key"] for f in codefiles.listing() if f["custom"]}
        self.assertEqual(listed, {"custom_ma20_example", "custom_A-b_1", "custom_策略123456"})

    def test_create_with_hostile_name_stays_inside(self):
        r = codefiles.create("../../app")
        self.assertEqual(r["key"], "custom_app")
        self.assertTrue((self.custom / "app.py").exists())
        self.assertEqual(self.app.read_text(encoding="utf-8"), "# 真 app.py\n")

    def test_builtin_whitelist_unchanged(self):
        self.assertEqual(codefiles.history("strategies"), [])
        with self.assertRaises(KeyError):
            codefiles.save("app", GOOD)
        with self.assertRaises(PermissionError):
            codefiles.save("portfolio", GOOD)


class SameWhitelistAsRunner(unittest.TestCase):
    def test_custom_engine_uses_same_regex(self):
        src = (ROOT / "engines" / "custom_engine.py").read_text(encoding="utf-8")
        self.assertIn("from engines.codefiles import SLUG_BAD_CHARS", src)
        self.assertIn("re.sub(SLUG_BAD_CHARS,", src)
        self.assertEqual(re.findall(r'r"\[\^A-Za-z0-9_一-鿿-\]', src), [])   # 不再各写一份


def _app_method(tree, name):
    handler = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "Handler")
    return next(f for f in handler.body if isinstance(f, ast.FunctionDef) and f.name == name)


class ApiLayer(TempDirs):
    """把 app.py 真实的 Handler._post_engine 抽进桩类跑；并确认 do_POST 把 /api/code/ 的异常统一转成 400。"""

    def setUp(self):
        super().setUp()
        self.tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
        cls = ast.ClassDef(name="H", bases=[], keywords=[], decorator_list=[],
                           body=[_app_method(self.tree, "_post_engine")])
        ns = {"parse_qs": parse_qs, "codefiles": codefiles}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[])), "app.py", "exec"), ns)
        self.H = ns["H"]

    def call(self, path, body=None):
        h = self.H()
        h._body = lambda: body or {}
        return h._post_engine(urlparse(path))

    def test_save_and_restore_reject(self):
        with self.assertRaises(KeyError):
            self.call("/api/code/save", {"f": "custom_../../app", "content": EVIL})
        with self.assertRaises(KeyError):
            self.call("/api/code/restore?f=custom_../../app&v=original")
        self.assertEqual(self.app.read_text(encoding="utf-8"), "# 真 app.py\n")
        self.assertEqual(self.call("/api/code/save", {"f": "custom_ok", "content": GOOD}), {"ok": True})

    def test_do_post_maps_code_errors_to_400(self):
        src = ast.get_source_segment((ROOT / "app.py").read_text(encoding="utf-8"), _app_method(self.tree, "do_POST"))
        i = src.index('u.path.startswith("/api/code/")')
        seg = src[i:i + 600]
        self.assertIn("self._post_engine(u)", seg)
        self.assertRegex(seg, r'except Exception as e:\s*\n\s*return self\._json\(\{"error": f"\{e\}"\}, 400\)')


if __name__ == "__main__":
    unittest.main(verbosity=2)
