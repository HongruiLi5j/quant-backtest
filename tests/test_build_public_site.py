"""公开站导出脚本 scripts/build_public_site.py 的测试（T020 步骤 1，v2）。

- 单元测试全部用临时目录造一棵假的 mockups 和模板，不联网、不读 .secret、不读写 data/。
- 最后的集成测试读的是仓库里的 dashboard/mockups 源码和 scripts/public_allowlist.json（都是代码，不是数据），
  构建结果写到临时目录。

python -m pytest tests/test_build_public_site.py
"""
import ast
import contextlib
import hashlib
import io
import json
import os
import shlex
import shutil
import sys
import tempfile
import unicodedata
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import build_public_site as B  # noqa: E402


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


ICO, PNG = b"\x00\x00\x01\x00ico", b"\x89PNG\r\n\x1a\nxx"
HTML = ('<!doctype html><html><head><link rel="stylesheet" href="theme.css">'
        '<script type="module" src="public.js"></script></head><body data-page="landing">hi</body></html>')
SVG = '<svg xmlns="http://www.w3.org/2000/svg"><symbol id="i-a" viewBox="0 0 1 1"><path d="M0 0"/></symbol></svg>'
MOCK = json.dumps({"_meta": {"note": "合成数据"}, "rows": [1, 2]}, ensure_ascii=False)
DICT_ZH = {"pub.synthetic": "演示数据", "pub.derived": "衍生结果", "landing.footer.note": "演示站点", "adm.row.x": "后台开关",
           "ws.nav.admin": "后台", "land.foot.admin": "管理", "ok.key": "正常", "contact.email": "owner@accessible-demo.test"}


class Env:
    """造一套假的源码树 + 模板 + 白名单；已钉死字典/mock/SVG 的内容哈希；默认满足发布条件。"""

    def __init__(self, tmp: Path):
        self.root = tmp
        self.src, self.tpl, self.out = tmp / "mockups", tmp / "tpl", tmp / "build"
        self.trash, self.alp = tmp / "trash", tmp / "allow.json"
        files = {
            "quant-ai-dark/landing.html": HTML, "quant-ai-dark/theme.css": "body{color:red}",
            "quant-ai-dark/public.js": "import './x.js'; // 加载 ../shared/page-landing.js",
            "quant-ai-dark/x.js": "export const x = 1;",
            "shared/page-landing.js": "import { y } from './dom.js'; loadMock('demo'); console.log(y);",
            "shared/dom.js": "export const y = 2;", "shared/icons.svg": SVG,
            "shared/i18n-zh.json": json.dumps(DICT_ZH, ensure_ascii=False),
            "shared/i18n-en.json": json.dumps({**{k: k for k in DICT_ZH}, "contact.email": "owner@accessible-demo.test"}),
            "mock-data/demo.json": MOCK,
            # 下面这些是"不该发布"的东西：存在于源码树里，但不在白名单
            "shared/page-ws-holdings.js": "internal workspace", "shared/dom.js.before-N06": "backup",
            "mock-data/holdings.json": "{}",
        }
        for rel, text in files.items():
            p = self.src / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
        (self.tpl / "assets").mkdir(parents=True)
        (self.tpl / "assets" / "favicon.ico").write_bytes(ICO)
        (self.tpl / "assets" / "favicon-32.png").write_bytes(PNG)
        (self.tpl / "root-index.html").write_text("<html><body>root</body></html>", encoding="utf-8")
        self.al = {
            "version": 1,
            "src_files": ["quant-ai-dark/landing.html", "quant-ai-dark/theme.css", "quant-ai-dark/public.js",
                          "quant-ai-dark/x.js", "shared/page-landing.js", "shared/dom.js", "shared/icons.svg",
                          "shared/i18n-zh.json", "shared/i18n-en.json", "mock-data/demo.json"],
            "template": {"root-index.html": "index.html", "assets/favicon.ico": "favicon.ico",
                         "assets/favicon-32.png": "assets/favicon-32.png"},
            "i18n": {"drop_prefixes": ["adm."], "drop_keys": ["ws.nav.admin"], "allow_keys": ["land.foot.admin"]},
            "required_keys": ["pub.synthetic", "landing.footer.note"],
            "binary_sha256": {"favicon.ico": sha(ICO), "assets/favicon-32.png": sha(PNG)},
            "content_pins": {},
        }
        self.write_al()
        self.pin()

    def write_al(self):
        self.alp.write_text(json.dumps(self.al, ensure_ascii=False), encoding="utf-8")

    def pin(self):
        """把当前（改写之后的）字典/mock/SVG 内容钉死，相当于所有者审阅后更新 content_pins。"""
        al = B.load_allowlist(self.alp)
        with tempfile.TemporaryDirectory() as t:
            site = Path(t) / "site"
            site.mkdir()
            B.copy_allowed(self.src, self.tpl, al, site)
            B.apply_transforms(site, al)
            self.al["content_pins"] = B.pin_report(site)
        self.write_al()

    def build(self, keep=5):
        return B.build(self.src, self.tpl, self.alp, self.out, self.trash, keep)

    def edit(self, rel, text):
        (self.src / rel).write_text(text, encoding="utf-8")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = Env(Path(self.tmp.name))
        self.counter = iter(range(1, 1000))
        p = patch.object(B, "utc_stamp", lambda: f"20260101T0000{next(self.counter):02d}Z")
        p.start()
        self.addCleanup(p.stop)

    def site(self, rep):
        return Path(rep["dir"]) / "site"

    def errors(self, rep):
        return "\n".join(rep["errors"])

    def new_env(self):
        return Env(Path(tempfile.mkdtemp(dir=self.tmp.name)))


# ================================================================ 基本构建

class BuildTests(Base):
    def test_build_ok_and_internal_files_not_exported(self):
        rep = self.env.build()
        self.assertTrue(rep["ok"], rep["errors"])
        self.assertTrue(rep["release_ready"], rep["blockers"])
        site = self.site(rep)
        self.assertFalse((site / "shared" / "page-ws-holdings.js").exists())
        self.assertFalse((site / "shared" / "dom.js.before-N06").exists())
        self.assertFalse((site / "mock-data" / "holdings.json").exists())
        self.assertTrue((site / "index.html").is_file() and (site / "favicon.ico").is_file())

    def test_dictionary_internal_keys_removed_allowed_key_kept(self):
        site = self.site(self.env.build())
        for lang in ("zh", "en"):
            d = json.loads((site / "shared" / f"i18n-{lang}.json").read_text(encoding="utf-8"))
            self.assertNotIn("adm.row.x", d)
            self.assertNotIn("ws.nav.admin", d)
            self.assertIn("land.foot.admin", d)
            self.assertIn("pub.synthetic", d)

    def test_manifest_sorted_excludes_itself_and_diff_baseline(self):
        rep = self.env.build()
        man = json.loads((Path(rep["dir"]) / "manifest.json").read_text(encoding="utf-8"))
        paths = [f["path"] for f in man["files"]]
        self.assertEqual(paths, sorted(paths, key=lambda x: x.encode("utf-8")))
        self.assertNotIn("manifest.json", paths)
        self.assertFalse(rep["diff_vs_published"]["baseline"])
        self.assertEqual(sorted(rep["diff_vs_published"]["added"]), paths)

    def test_each_build_goes_to_new_dir_and_never_touches_old(self):
        r1 = self.env.build()
        before = B.make_manifest(self.site(r1))
        r2 = self.env.build()
        self.assertNotEqual(r1["dir"], r2["dir"])
        self.assertEqual(B.make_manifest(self.site(r1)), before)

    def test_failed_build_keeps_previous_and_leaves_evidence(self):
        r1 = self.env.build()
        (self.env.src / "shared" / "dom.js").unlink()
        r2 = self.env.build()
        self.assertFalse(r2["ok"])
        self.assertTrue(r2["dir"].endswith(".FAILED"))
        self.assertTrue((Path(r2["dir"]) / "report.json").is_file())
        self.assertTrue(self.site(r1).is_dir())
        self.assertTrue(B.verify(Path(r1["dir"]).name, self.env.out, self.env.alp)["ok"])

    def test_output_overlapping_source_refused(self):
        with self.assertRaises(B.BuildError):
            B.build(self.env.src, self.env.tpl, self.env.alp, self.env.src / "build", self.env.trash)

    def test_trash_overlapping_build_root_refused(self):
        with self.assertRaises(B.BuildError):
            B.build(self.env.src, self.env.tpl, self.env.alp, self.env.out, self.env.out / "trash")

    def test_allowlist_inside_build_root_refused(self):
        self.env.out.mkdir()
        inside = self.env.out / "allow.json"
        inside.write_text(self.env.alp.read_text(encoding="utf-8"), encoding="utf-8")
        with self.assertRaises(B.BuildError):
            B.build(self.env.src, self.env.tpl, inside, self.env.out, self.env.trash)

    def test_concurrent_build_refused_by_lock(self):
        self.env.out.mkdir(parents=True)
        (self.env.out / ".build.lock").write_text("busy")
        with self.assertRaises(B.BuildError):
            self.env.build()
        self.assertTrue((self.env.out / ".build.lock").is_file())

    def test_lock_released_after_build(self):
        self.env.build()
        self.assertFalse((self.env.out / ".build.lock").exists())


# ================================================================ F1：白名单路径校验

class AllowlistValidationTests(Base):
    def load_with(self, **patch_al):
        al = json.loads(json.dumps(self.env.al))
        for k, v in patch_al.items():
            al[k] = v
        self.env.alp.write_text(json.dumps(al, ensure_ascii=False), encoding="utf-8")
        return B.load_allowlist(self.env.alp)

    def test_template_destination_escape_refused_and_nothing_written_outside(self):
        for bad in ("../escape.html", "/abs.html", "a/./b.html", "a//b.html", "a\\b.html", ".hidden.html", "dir/", "", "x/../y.html"):
            with self.subTest(dst=bad):
                with self.assertRaises(B.BuildError):
                    self.load_with(template={"root-index.html": bad})
        self.assertFalse((Path(self.tmp.name) / "escape.html").exists())
        self.assertFalse(Path("/abs.html").exists())

    def test_template_destination_escape_never_reaches_copy(self):
        self.env.al["template"]["root-index.html"] = "../escape.html"
        self.env.write_al()
        with self.assertRaises(B.BuildError):
            self.env.build()
        self.assertFalse((self.env.out.parent / "escape.html").exists())
        self.assertFalse((self.env.out / "escape.html").exists())

    def test_copy_allowed_itself_refuses_escape_even_if_validation_is_bypassed(self):
        """纵深防御：就算有人绕过 load_allowlist 直接把越界目标交给 copy_allowed，也不会写到 site 外面。"""
        al = json.loads(json.dumps(self.env.al))
        al["template"] = {"root-index.html": "../escape.html"}
        site = Path(self.tmp.name) / "s" / "site"
        site.mkdir(parents=True)
        with self.assertRaises(B.BuildError):
            B.copy_allowed(self.env.src, self.env.tpl, al, site)
        self.assertFalse((site.parent / "escape.html").exists())

    def test_bad_source_paths_refused(self):
        for bad in ("../x.js", "/etc/passwd", "shared//dom.js", "shared/./dom.js", "shared\\dom.js", "shared/.hid.js", "", 123, None):
            with self.subTest(src=bad):
                with self.assertRaises(B.BuildError):
                    self.load_with(src_files=self.env.al["src_files"] + [bad])

    def test_non_nfc_and_control_characters_refused(self):
        nfd = unicodedata.normalize("NFD", "shared/é.js")
        self.assertNotEqual(nfd, unicodedata.normalize("NFC", nfd))
        for bad in (nfd, "shared/a\x00b.js", "shared/a\nb.js"):
            with self.subTest(p=repr(bad)):
                with self.assertRaises(B.BuildError):
                    self.load_with(src_files=self.env.al["src_files"] + [bad])

    def test_case_and_unicode_equivalent_duplicates_refused(self):
        with self.assertRaises(B.BuildError):
            self.load_with(src_files=self.env.al["src_files"] + ["Shared/DOM.js"])
        with self.assertRaises(B.BuildError):
            self.load_with(template={**self.env.al["template"], "assets/favicon-32.png": "shared/dom.js"})
        with self.assertRaises(B.BuildError):
            self.load_with(src_files=self.env.al["src_files"] + ["shared/dom.js"])
        with self.assertRaises(B.BuildError):
            self.load_with(template={**self.env.al["template"], "assets/favicon-32.png": "favicon.ico"})

    def test_pin_and_binary_hash_fields_validated(self):
        with self.assertRaises(B.BuildError):
            self.load_with(content_pins={"shared/i18n-zh.json": "not-a-hash"})
        with self.assertRaises(B.BuildError):
            self.load_with(binary_sha256={"../x.png": "0" * 64})
        with self.assertRaises(B.BuildError):
            self.load_with(content_pins=[])

    def test_missing_fields_and_wrong_types_refused(self):
        for k in ("src_files", "template", "i18n", "required_keys", "binary_sha256", "content_pins", "version"):
            with self.subTest(missing=k):
                al = json.loads(json.dumps(self.env.al))
                del al[k]
                self.env.alp.write_text(json.dumps(al, ensure_ascii=False), encoding="utf-8")
                with self.assertRaises(B.BuildError):
                    B.load_allowlist(self.env.alp)
        self.env.alp.write_text("[]", encoding="utf-8")
        with self.assertRaises(B.BuildError):
            B.load_allowlist(self.env.alp)


# ================================================================ 来源安全

class SourceSafetyTests(Base):
    def failed(self, needle):
        rep = self.env.build()
        self.assertFalse(rep["ok"])
        self.assertIn(needle, self.errors(rep))
        self.assertTrue(rep["dir"].endswith(".FAILED"), rep["dir"])
        ok_dirs = [p for p in self.env.out.iterdir() if p.is_dir() and ".FAILED" not in p.name]
        self.assertEqual(ok_dirs, [])
        return rep

    def test_symlink_source_refused(self):
        target = Path(self.tmp.name) / "outside.js"
        target.write_text("TOP-SECRET-OUTSIDE", encoding="utf-8")
        p = self.env.src / "shared" / "dom.js"
        p.unlink()
        os.symlink(target, p)
        rep = self.failed("符号链接")
        leaked = [f for f in Path(rep["dir"]).rglob("*") if f.is_file() and b"TOP-SECRET-OUTSIDE" in f.read_bytes()]
        self.assertEqual(leaked, [])

    def test_symlinked_source_directory_refused(self):
        real = Path(self.tmp.name) / "real_shared"
        (self.env.src / "shared").rename(real)
        os.symlink(real, self.env.src / "shared")
        self.failed("符号链接")

    def test_binary_hash_mismatch_refused(self):
        (self.env.tpl / "assets" / "favicon-32.png").write_bytes(PNG + b"tampered")
        self.failed("sha256 不符")

    def test_binary_without_pinned_hash_refused(self):
        del self.env.al["binary_sha256"]["favicon.ico"]
        self.env.write_al()
        self.failed("没有登记 sha256")

    def test_unknown_suffix_in_allowlist_refused(self):
        (self.env.src / "shared" / "evil.exe").write_text("x", encoding="utf-8")
        self.env.al["src_files"].append("shared/evil.exe")
        self.env.write_al()
        self.failed("未知后缀")

    def test_allowlisting_a_backup_file_is_still_rejected_by_name_rule(self):
        env = self.new_env()
        (env.src / "shared/dom.before-N06.js").write_text("export const z = 1;", encoding="utf-8")
        env.al["src_files"].append("shared/dom.before-N06.js")
        env.write_al()
        rep = env.build()
        self.assertFalse(rep["ok"])
        self.assertIn("隐藏/备份/临时/sourcemap 文件", self.errors(rep))

    def test_missing_allowlisted_file_refused(self):
        self.env.al["src_files"].append("shared/nope.js")
        self.env.write_al()
        self.failed("不存在")


# ================================================================ F4：异常与失败证据

class BuildFailureTests(Base):
    def assert_failed_with_evidence(self, rep):
        self.assertFalse(rep["ok"])
        d = Path(rep["dir"])
        self.assertTrue(d.name.endswith(".FAILED") and (d / "report.json").is_file() and (d / "manifest.json").is_file())
        self.assertEqual([p for p in self.env.out.iterdir() if p.name.endswith(".partial")], [], "不能留下孤零零的 .partial")

    def test_invalid_utf8_in_source_html_leaves_failed_evidence(self):
        (self.env.src / "quant-ai-dark" / "landing.html").write_bytes(b"<html>\xff\xfe</html>")
        rep = self.env.build()
        self.assert_failed_with_evidence(rep)
        self.assertIn("构建异常", self.errors(rep))

    def test_corrupt_dictionary_json_leaves_failed_evidence(self):
        self.env.edit("shared/i18n-zh.json", "{not json")
        rep = self.env.build()
        self.assert_failed_with_evidence(rep)

    def test_failed_dirs_are_not_counted_or_moved_by_prune(self):
        ok = self.env.build()
        (self.env.src / "shared" / "dom.js").unlink()
        f1, f2 = self.env.build(), self.env.build()
        self.assertTrue(f1["dir"].endswith(".FAILED") and f2["dir"].endswith(".FAILED"))
        moved = B.prune(self.env.out, self.env.trash, 0)
        self.assertEqual(moved, [Path(ok["dir"]).name])
        self.assertTrue(Path(f1["dir"]).is_dir() and Path(f2["dir"]).is_dir())

    def test_same_stamp_failed_builds_get_distinct_names_not_classified_ok(self):
        with patch.object(B, "utc_stamp", lambda: "20260101T000000Z"):
            (self.env.src / "shared" / "dom.js").unlink()
            f1, f2 = self.env.build(), self.env.build()
        self.assertNotEqual(f1["dir"], f2["dir"])
        self.assertFalse(B._is_ok_build(Path(f2["dir"])))
        self.assertTrue(Path(f2["dir"]).name.endswith(".FAILED"))

    def test_prune_trash_collisions_get_unique_destinations_and_dont_merge(self):
        reps = [self.env.build(keep=100) for _ in range(3)]
        oldest = Path(reps[0]["dir"]).name
        (self.env.trash / oldest).mkdir(parents=True)
        (self.env.trash / oldest / "keep.txt").write_text("existing", encoding="utf-8")
        (self.env.trash / f"{oldest}.dup2").mkdir()
        moved = B.prune(self.env.out, self.env.trash, 2)
        self.assertEqual(moved, [oldest])
        self.assertTrue((self.env.trash / oldest / "keep.txt").is_file())
        self.assertTrue((self.env.trash / f"{oldest}.dup3" / "site").is_dir())

    def test_a_directory_named_failed_is_never_treated_as_a_good_build_even_if_its_report_says_ok(self):
        """名字规则要单独成立：就算 .FAILED 目录里的 report.json 被改成 ok=true，也不能当成功构建。"""
        ok = self.env.build()
        fake = self.env.out / (Path(ok["dir"]).name + ".FAILED-2")
        shutil.copytree(ok["dir"], fake)
        self.assertTrue(json.loads((fake / "report.json").read_text(encoding="utf-8"))["ok"])
        self.assertFalse(B._is_ok_build(fake))
        fake2 = self.env.out / (Path(ok["dir"]).name + ".FAILED")
        shutil.copytree(ok["dir"], fake2)
        self.assertFalse(B._is_ok_build(fake2))
        self.assertEqual(B.prune(self.env.out, self.env.trash, 0), [Path(ok["dir"]).name])        # 只有真的成功构建被移走
        self.assertTrue(fake.is_dir() and fake2.is_dir())

    def test_prune_refuses_overlapping_dirs(self):
        self.env.build()
        with self.assertRaises(B.BuildError):
            B.prune(self.env.out, self.env.out / "trash", 1)
        with self.assertRaises(B.BuildError):
            B.prune(self.env.out, self.env.out.parent, 1)


# ================================================================ F2：build_id 与目录一致

class BuildIdTests(Base):
    def test_same_second_same_content_builds_have_matching_ids_and_dirs(self):
        with patch.object(B, "utc_stamp", lambda: "20260101T000000Z"):
            r1, r2, r3 = self.env.build(), self.env.build(), self.env.build()
        ids = [r["build_id"] for r in (r1, r2, r3)]
        self.assertEqual(len(set(ids)), 3)
        for r in (r1, r2, r3):
            self.assertEqual(Path(r["dir"]).name, r["build_id"])
            on_disk = json.loads((Path(r["dir"]) / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(on_disk["build_id"], r["build_id"])
            self.assertTrue(B.verify(r["build_id"], self.env.out, self.env.alp)["ok"])
        self.assertTrue(ids[1].endswith("-2") and ids[2].endswith("-3"))
        self.assertEqual(r1["manifest_sha256"], r2["manifest_sha256"])

    def test_failed_report_id_matches_dir_name_without_suffix(self):
        (self.env.src / "shared" / "dom.js").unlink()
        r = self.env.build()
        self.assertEqual(Path(r["dir"]).name, r["build_id"] + ".FAILED")
        self.assertEqual(json.loads((Path(r["dir"]) / "report.json").read_text(encoding="utf-8"))["build_id"], r["build_id"])

    def test_cli_prints_out_root_and_expected_digest_in_next_commands(self):
        buf = io.StringIO()
        with patch.object(B, "TRASH_DEFAULT", self.env.trash), contextlib.redirect_stdout(buf):
            rc = B.main(["--src", str(self.env.src), "--template", str(self.env.tpl), "--allowlist", str(self.env.alp),
                         "--out-root", str(self.env.out), "--allow-outside-root"])
        out = buf.getvalue()
        self.assertEqual(rc, 0, out)
        self.assertIn(f"--out-root {self.env.out}", out)
        self.assertIn(f"--allowlist {self.env.alp}", out)
        self.assertIn("--expect-manifest-sha256", out)
        self.assertIn("--mark-published", out)


# ================================================================ 检查本身

class CheckTests(Base):
    def setUp(self):
        super().setUp()
        self.rep = self.env.build()
        self.site_dir = self.site(self.rep)

    def check(self):
        return B.check_site(self.site_dir, B.load_allowlist(self.env.alp))

    def errs(self):
        return "\n".join(self.check()["errors"])

    def test_clean_site_has_no_errors_or_blockers(self):
        c = self.check()
        self.assertEqual((c["errors"], c["blockers"]), ([], []))

    def test_extra_file_not_in_allowlist(self):
        (self.site_dir / "shared" / "extra.js").write_text("x", encoding="utf-8")
        self.assertIn("不在白名单里的文件：shared/extra.js", self.errs())

    def test_backup_hidden_temp_and_sourcemap_files(self):
        for name in ("a.js.before-N06", ".DS_Store", "a.js.map", "a.js.bak", "a.js~"):
            (self.site_dir / "shared" / name).write_text("x", encoding="utf-8")
        e = self.errs()
        for name in ("a.js.before-N06", ".DS_Store", "a.js.map", "a.js.bak", "a.js~"):
            self.assertIn(name, e)

    def test_unknown_suffix_in_site(self):
        (self.site_dir / "shared" / "x.exe").write_bytes(b"MZ")
        self.assertIn("未知后缀", self.errs())

    def test_symlink_in_site(self):
        os.symlink(self.site_dir / "shared" / "dom.js", self.site_dir / "shared" / "link.js")
        self.assertIn("符号链接", self.errs())

    def test_undecodable_text_is_error_not_ignored(self):
        (self.site_dir / "shared" / "dom.js").write_bytes(b"export const a = '\xff\xfe';")
        self.assertIn("无法按 UTF-8 解码：shared/dom.js", self.errs())

    def test_redline_strings(self):
        (self.site_dir / "shared" / "dom.js").write_text("fetch('http://127.0.0.1:8765/api'); // /Users/me/.secret", encoding="utf-8")
        e = self.errs()
        for w in ("127.0.0.1", "/Users/", ".secret"):
            self.assertIn(w, e)

    def test_compliance_words(self):
        (self.site_dir / "shared" / "dom.js").write_text("const a = '低风险高收益稳赚';", encoding="utf-8")
        e = self.errs()
        for w in ("低风险", "高收益", "稳赚"):
            self.assertIn(w, e)

    def test_sourcemap_comment(self):
        (self.site_dir / "shared" / "dom.js").write_text("a\n//# sourceMappingURL=a.js.map", encoding="utf-8")
        self.assertIn("sourceMappingURL", self.errs())

    def test_binary_asset_hash_mismatch_in_site(self):
        (self.site_dir / "favicon.ico").write_bytes(b"other")
        self.assertIn("二进制资产哈希不符：favicon.ico", self.errs())

    def test_allowlisted_file_missing_in_site(self):
        (self.site_dir / "quant-ai-dark" / "x.js").unlink()
        e = self.errs()
        self.assertIn("白名单里的文件没有导出：quant-ai-dark/x.js", e)
        self.assertIn("引用不到文件", e)

    def test_required_dictionary_keys(self):
        d = self.site_dir / "shared" / "i18n-zh.json"
        j = json.loads(d.read_text(encoding="utf-8"))
        del j["pub.synthetic"]
        d.write_text(json.dumps(j, ensure_ascii=False), encoding="utf-8")
        self.assertIn("缺少必须词条：pub.synthetic", self.errs())

    def test_internal_dictionary_keys_are_errors(self):
        d = self.site_dir / "shared" / "i18n-en.json"
        j = json.loads(d.read_text(encoding="utf-8"))
        j.update({"adm.leak": "x", "ws.nav.admin": "y", "foo.admin.bar": "z"})
        d.write_text(json.dumps(j), encoding="utf-8")
        self.assertIn("含内部键", self.errs())

    def test_third_party_hosts_listed_with_careful_wording(self):
        (self.site_dir / "shared" / "dom.js").write_text("import('https://cdn.example.org/a.js')", encoding="utf-8")
        w = "\n".join(self.check()["warnings"])
        self.assertIn("cdn.example.org", w)
        self.assertIn("不等于一定会请求", w)


# ================================================================ 引用语义

class RefSemanticsTests(Base):
    def setUp(self):
        super().setUp()
        self.site_dir = self.site(self.env.build())

    def check(self):
        return B.check_site(self.site_dir, B.load_allowlist(self.env.alp))

    def html(self, extra_head):
        (self.site_dir / "quant-ai-dark" / "landing.html").write_text(HTML.replace("</head>", extra_head + "</head>"), encoding="utf-8")

    def test_broken_references_are_errors_external_fragments_and_directories_are_not(self):
        self.html('<script src="missing.js"></script><link href="https://fonts.example.org/x.css" rel="stylesheet"><a href="#top">t</a>')
        e = "\n".join(self.check()["errors"])
        self.assertIn("missing.js", e)
        self.assertNotIn("fonts.example.org", e)
        self.assertNotIn("#top", e)

    def test_non_anchor_href_is_a_resource_so_missing_stylesheet_is_an_error(self):
        """<link rel=stylesheet href=...>、<link rel=icon href=...> 的 href 是资源，不是页面链接：缺了页面会坏，必须报错。"""
        self.html('<link rel="stylesheet" href="missing.css"><link rel="icon" href="missing.ico">')
        e = "\n".join(self.check()["errors"])
        self.assertIn("missing.css", e)
        self.assertIn("missing.ico", e)
        self.html('<link rel="canonical" href="missing-canon.html"><link rel="alternate" href="alt.html">')
        c = self.check()                                           # canonical / alternate 只是元数据，缺失只告警
        self.assertNotIn("missing-canon.html", "\n".join(c["errors"]))

    def test_script_src_pointing_at_missing_html_is_hard_error_but_anchor_is_warning(self):
        self.html('<script src="missing.html"></script>')
        self.assertIn("missing.html", "\n".join(self.check()["errors"]))
        self.html('<a href="missing-page.html">x</a>')
        c = self.check()
        self.assertNotIn("missing-page.html", "\n".join(c["errors"]))
        self.assertIn("missing-page.html", "\n".join(c["warnings"]))

    def test_relative_reference_is_resolved_like_a_browser_no_fallback_directories(self):
        """quant-ai-dark/landing.html 里写 src="dom.js"：浏览器会找 quant-ai-dark/dom.js，不会去 shared/ 找。"""
        self.html('<script src="dom.js"></script>')
        self.assertIn("dom.js", "\n".join(self.check()["errors"]))
        self.html('<script src="../shared/dom.js"></script>')
        self.assertNotIn("../shared/dom.js", "\n".join(self.check()["errors"]))

    def test_srcset_inline_style_and_img_src_checked(self):
        self.html('<img src="nope.png" srcset="nope2.png 2x"><style>.a{background:url(nope3.png)}</style><i style="background:url(nope4.png)"></i>')
        e = "\n".join(self.check()["errors"])
        for n in ("nope.png", "nope2.png", "nope3.png", "nope4.png"):
            self.assertIn(n, e)

    def test_data_page_module_must_exist_when_public_js_is_used(self):
        (self.site_dir / "shared" / "page-landing.js").unlink()
        self.assertIn("__page__:landing", "\n".join(self.check()["errors"]))

    def test_mock_loaded_by_loadMock_must_exist(self):
        (self.site_dir / "mock-data" / "demo.json").unlink()
        self.assertIn("__mock__:demo.json", "\n".join(self.check()["errors"]))


# ================================================================ 内容边界（字典/mock/SVG）

class ContentBoundaryTests(Base):
    def test_changed_dictionary_content_fails_until_repinned(self):
        d = dict(DICT_ZH)
        d["new.key"] = "新增的内容"
        self.env.edit("shared/i18n-zh.json", json.dumps(d, ensure_ascii=False))
        rep = self.env.build()
        self.assertFalse(rep["ok"])
        self.assertIn("内容与钉死的哈希不符", self.errors(rep))
        self.env.pin()
        self.assertTrue(self.env.build()["ok"])

    def test_pinned_file_without_pin_fails(self):
        for rel in ("shared/i18n-en.json", "mock-data/demo.json", "shared/icons.svg"):
            with self.subTest(rel=rel):
                env = self.new_env()
                del env.al["content_pins"][rel]
                env.write_al()
                rep = env.build()
                self.assertFalse(rep["ok"])
                self.assertIn(f"内容未钉死哈希（字典/mock/SVG 必须逐个登记 content_pins）：{rel}", self.errors(rep))

    def test_new_mock_file_needs_synthetic_marker_and_pin(self):
        self.env.edit("mock-data/extra.json", json.dumps({"_meta": {"note": "真实账户"}, "positions": []}, ensure_ascii=False))
        self.env.al["src_files"].append("mock-data/extra.json")
        self.env.edit("shared/page-landing.js", "import './dom.js'; loadMock('demo'); loadMock('extra');")
        self.env.write_al()
        self.env.pin()
        rep = self.env.build()
        self.assertFalse(rep["ok"])
        self.assertIn("没有『合成数据』来源标记", self.errors(rep))

    def test_mock_marker_accepts_synthetic_english(self):
        self.env.edit("mock-data/demo.json", json.dumps({"_meta": {"note": "Synthetic data"}}))
        self.env.pin()
        self.assertTrue(self.env.build()["ok"])

    def test_svg_active_content_rejected(self):
        for bad in ('<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
                    '<svg xmlns="http://www.w3.org/2000/svg" onload="x()"></svg>',
                    '<svg xmlns="http://www.w3.org/2000/svg"><image href="https://evil.example/x.png"/></svg>',
                    '<svg xmlns="http://www.w3.org/2000/svg"><foreignObject/></svg>',
                    '<svg xmlns="http://www.w3.org/2000/svg"><a href="javascript:alert(1)"/></svg>'):
            with self.subTest(svg=bad[:60]):
                env = self.new_env()
                env.edit("shared/icons.svg", bad)
                env.pin()
                rep = env.build()
                self.assertFalse(rep["ok"])
                self.assertIn("SVG 含脚本", self.errors(rep))

    def test_internal_api_paths_in_public_data_are_warnings_not_errors(self):
        self.env.edit("mock-data/demo.json", json.dumps({"_meta": {"note": "合成数据", "schema": "GET /api/member/me 的返回"}}, ensure_ascii=False))
        self.env.pin()
        rep = self.env.build()
        self.assertTrue(rep["ok"])
        self.assertIn("内部接口路径", "\n".join(rep["warnings"]))


# ================================================================ 发布门槛（草稿 vs 可发布）

class ReleaseGateTests(Base):
    def rebuild_with_zh(self, env=None, **changes):
        env = env or self.env
        d = dict(DICT_ZH)
        d.update(changes)
        for k in [k for k, v in changes.items() if v is None]:
            del d[k]
        env.edit("shared/i18n-zh.json", json.dumps(d, ensure_ascii=False))
        env.pin()
        return env.build()

    def test_either_language_missing_contact_blocks_release(self):
        env = self.new_env()
        en = {k: k for k in DICT_ZH}                      # 英文字典没有合法邮箱
        env.edit("shared/i18n-en.json", json.dumps(en))
        env.pin()
        rep = env.build()
        self.assertTrue(rep["ok"])
        self.assertFalse(rep["release_ready"])
        self.assertIn("字典 en", "\n".join(rep["blockers"]))

    def test_missing_contact_email_blocks_release_but_not_draft(self):
        rep = self.rebuild_with_zh(**{"contact.email": None})
        self.assertTrue(rep["ok"])
        self.assertFalse(rep["release_ready"])
        self.assertIn("contact.email", "\n".join(rep["blockers"]))

    def test_placeholder_or_malformed_contact_email_blocks_release(self):
        for v in ("contact@example.invalid", "me@example.com", "TODO", "no-at-sign", "待填@x.cn"):
            with self.subTest(v=v):
                rep = self.rebuild_with_zh(self.new_env(), **{"contact.email": v})
                self.assertTrue(rep["ok"])
                self.assertFalse(rep["release_ready"], v)

    def test_unresolved_data_claim_blocks_release(self):
        rep = self.rebuild_with_zh(**{"pub.derived": "Data sources: SINA · Tencent"})
        self.assertTrue(rep["ok"])
        self.assertFalse(rep["release_ready"])
        self.assertIn("Data sources: SINA", "\n".join(rep["blockers"]))

    def test_mark_published_refuses_when_not_release_ready_and_verify_reports_it(self):
        rep = self.rebuild_with_zh(**{"contact.email": None})
        bid = rep["build_id"]
        v = B.verify(bid, self.env.out, self.env.alp)
        self.assertTrue(v["ok"])
        self.assertFalse(v["release_ready"])
        with self.assertRaises(B.BuildError):
            B.mark_published(bid, self.env.out, self.env.alp)
        self.assertFalse((self.env.out / "published.json").exists())

    def test_cli_says_cannot_publish_and_does_not_print_upload_steps(self):
        self.rebuild_with_zh(**{"contact.email": None})
        buf = io.StringIO()
        with patch.object(B, "TRASH_DEFAULT", self.env.trash), contextlib.redirect_stdout(buf):
            B.main(["--src", str(self.env.src), "--template", str(self.env.tpl), "--allowlist", str(self.env.alp),
                    "--out-root", str(self.env.out), "--allow-outside-root"])
        out = buf.getvalue()
        self.assertIn("不能发布", out)
        self.assertNotIn("下一步（由你手动做", out)                    # 不给出上传步骤
        self.assertNotIn("--expect-manifest-sha256", out)


# ================================================================ F3：verify / mark-published

class VerifyHardeningTests(Base):
    def setUp(self):
        super().setUp()
        self.rep = self.env.build()
        self.bid = self.rep["build_id"]
        self.dir = Path(self.rep["dir"])

    def verify(self, **kw):
        return B.verify(self.bid, self.env.out, self.env.alp, **kw)

    def test_verify_detects_tampering_and_additions(self):
        self.assertTrue(self.verify()["ok"])
        (self.dir / "site" / "shared" / "dom.js").write_text("export const y = 3;", encoding="utf-8")
        r = self.verify()
        self.assertFalse(r["ok"])
        self.assertIn("清单不一致", "\n".join(r["errors"]))
        (self.dir / "site" / "shared" / "dom.js").write_text("export const y = 2;", encoding="utf-8")
        (self.dir / "site" / "extra.txt").write_text("x", encoding="utf-8")
        self.assertFalse(self.verify()["ok"])

    def test_build_id_must_be_standard_format(self):
        for bad in ("../x", "/abs", f"{self.bid}/..", "latest", f"{self.bid}.FAILED", "", None, 123, f"{self.bid}\n", "20260101T000001Z-ZZZZZZZZ"):
            with self.subTest(id=bad):
                with self.assertRaises(B.BuildError):
                    B.verify(bad, self.env.out, self.env.alp)
                with self.assertRaises(B.BuildError):
                    B.mark_published(bad, self.env.out, self.env.alp)

    def test_symlinked_root_manifest_or_site_refused(self):
        link_root = Path(self.tmp.name) / "linkroot"
        os.symlink(self.env.out, link_root)
        with self.assertRaises(B.BuildError):
            B.verify(self.bid, link_root, self.env.alp)
        man = self.dir / "manifest.json"
        saved = man.read_text(encoding="utf-8")
        man.unlink()
        other = Path(self.tmp.name) / "m.json"
        other.write_text(saved, encoding="utf-8")
        os.symlink(other, man)
        with self.assertRaises(B.BuildError):
            self.verify()
        man.unlink()
        man.write_text(saved, encoding="utf-8")
        site = self.dir / "site"
        moved = Path(self.tmp.name) / "site_moved"
        site.rename(moved)
        os.symlink(moved, site)
        with self.assertRaises(B.BuildError):
            self.verify()

    def test_manifest_files_tampered_but_digest_kept_is_detected(self):
        man = self.dir / "manifest.json"
        m = json.loads(man.read_text(encoding="utf-8"))
        m["files"][0]["sha256"] = "0" * 64
        man.write_text(json.dumps(m), encoding="utf-8")
        with self.assertRaises(B.BuildError) as cm:
            self.verify()
        self.assertIn("不自洽", str(cm.exception))
        with self.assertRaises(B.BuildError):
            B.mark_published(self.bid, self.env.out, self.env.alp)
        self.assertFalse((self.env.out / "published.json").exists())

    def test_manifest_schema_violations_refused(self):
        man = self.dir / "manifest.json"
        good = json.loads(man.read_text(encoding="utf-8"))
        cases = {
            "重复路径": lambda m: m["files"].append(dict(m["files"][0])),
            "大小写等价重复": lambda m: m["files"].append({**m["files"][0], "path": m["files"][0]["path"].upper()}),
            "路径穿越": lambda m: m["files"][0].update(path="../evil.js"),
            "哈希格式": lambda m: m["files"][0].update(sha256="xyz"),
            "负数大小": lambda m: m["files"][0].update(size=-1),
            "多余字段": lambda m: m["files"][0].update(extra=1),
            "files 不是列表": lambda m: m.update(files={}),
        }
        for name, mutate in cases.items():
            with self.subTest(case=name):
                m = json.loads(json.dumps(good))
                mutate(m)
                if isinstance(m["files"], list):
                    m["manifest_sha256"] = hashlib.sha256(B.canon(m["files"]).encode()).hexdigest()
                man.write_text(json.dumps(m), encoding="utf-8")
                with self.assertRaises(B.BuildError):
                    self.verify()
        man.write_text(json.dumps(good), encoding="utf-8")
        self.assertTrue(self.verify()["ok"])

    def test_expected_digest_from_build_time_is_enforced(self):
        self.assertTrue(self.verify(expect_sha256=self.rep["manifest_sha256"])["ok"])
        r = self.verify(expect_sha256="0" * 64)
        self.assertFalse(r["ok"])
        self.assertIn("摘要不一致", "\n".join(r["errors"]))
        with self.assertRaises(B.BuildError):
            B.mark_published(self.bid, self.env.out, self.env.alp, expect_sha256="0" * 64)

    def test_rewriting_site_and_manifest_together_is_caught_only_by_expected_digest(self):
        """同时改正文和清单并重新算摘要：自洽，所以不带 expect 的 verify 发现不了——这是已知、写在文档里的信任边界；
        带构建时记下的摘要就能发现。"""
        (self.dir / "site" / "shared" / "dom.js").write_text("export const y = 'evil';", encoding="utf-8")
        new = B.make_manifest(self.dir / "site")
        (self.dir / "manifest.json").write_text(json.dumps(new), encoding="utf-8")
        self.assertTrue(self.verify()["ok"])
        self.assertFalse(self.verify(expect_sha256=self.rep["manifest_sha256"])["ok"])

    def test_mark_published_takes_the_lock_and_cleans_up(self):
        (self.env.out / ".build.lock").write_text("busy")
        with self.assertRaises(B.BuildError):
            B.mark_published(self.bid, self.env.out, self.env.alp)
        (self.env.out / ".build.lock").unlink()
        B.mark_published(self.bid, self.env.out, self.env.alp)
        self.assertFalse((self.env.out / ".build.lock").exists())
        self.assertEqual([p.name for p in self.env.out.glob("published.json*")], ["published.json"])

    def test_mark_published_then_next_build_diffs_against_it(self):
        B.mark_published(self.bid, self.env.out, self.env.alp)
        self.env.edit("quant-ai-dark/x.js", "export const x = 99;")
        d = self.env.build()["diff_vs_published"]
        self.assertTrue(d["baseline"])
        self.assertEqual((d["added"], d["removed"], d["changed"]), ([], [], ["quant-ai-dark/x.js"]))

    def test_mark_published_unknown_build_refused(self):
        with self.assertRaises(B.BuildError):
            B.mark_published("20990101T000000Z-deadbeef", self.env.out, self.env.alp)

    def test_prune_keeps_published_in_addition_to_latest_n(self):
        reps = [self.env.build(keep=100) for _ in range(6)]
        names = [self.bid] + [Path(r["dir"]).name for r in reps]
        B.mark_published(names[0], self.env.out, self.env.alp)
        moved = B.prune(self.env.out, self.env.trash, 5)
        kept = sorted(p.name for p in self.env.out.iterdir() if p.is_dir())
        self.assertEqual(len(kept), 6)
        self.assertIn(names[0], kept)
        self.assertEqual(sorted(moved), sorted(names[1:2]))
        for m in moved:
            self.assertTrue((self.env.trash / m / "site").is_dir())



# ================================================================ 第三轮复审：命令生成 / 特殊引用越界 / 加固

class PrintedCommandTests(Base):
    def odd_env(self):
        root = Path(self.tmp.name) / "dir with space; $(touch pwned) 'q' \"d\""
        root.mkdir()
        return Env(root)

    def run_build_cli(self, env):
        buf = io.StringIO()
        with patch.object(B, "TRASH_DEFAULT", env.trash), contextlib.redirect_stdout(buf):
            rc = B.main(["--src", str(env.src), "--template", str(env.tpl), "--allowlist", str(env.alp),
                         "--out-root", str(env.out), "--allow-outside-root"])
        return rc, buf.getvalue()

    def commands(self, out):
        return [l.split("：", 1)[1].strip() for l in out.splitlines() if l.strip().startswith(("1. 复核", "3. 核对线上内容后"))]

    def test_commands_are_built_from_argv_so_odd_paths_survive_and_flag_is_kept(self):
        env = self.odd_env()
        rc, out = self.run_build_cli(env)
        self.assertEqual(rc, 0, out)
        cmds = self.commands(out)
        self.assertEqual(len(cmds), 2)
        self.assertIn("--release", shlex.split(cmds[0]))                  # 复核命令必须带 --release，才会按发布条件给退出码
        self.assertIn("--verify", shlex.split(cmds[0]))
        self.assertNotIn("--release", shlex.split(cmds[1]))
        self.assertIn("--mark-published", shlex.split(cmds[1]))
        for c in cmds:
            argv = shlex.split(c)                                           # 按 shell 规则切开：参数必须逐个还原，不能被空格/引号/$() 拆开
            self.assertEqual(argv[:2], [".venv/bin/python", "scripts/build_public_site.py"])
            self.assertIn("--allow-outside-root", argv)                    # 外根构建 → 后续命令必须带同样的放行开关
            self.assertEqual(argv[argv.index("--out-root") + 1], str(env.out))
            self.assertEqual(argv[argv.index("--allowlist") + 1], str(env.alp))
            self.assertIn(shlex.quote(str(env.out)), c)                    # 打印出来的文本里路径被正确引用
        upload = out.split("2. 上传这份快照：")[1].split("（项目")[0].strip()
        site_path = shlex.split(upload)[0]                                   # 上传路径也必须是被正确引用的单个参数
        self.assertTrue(site_path.endswith("/site") and Path(site_path).is_dir(), site_path)

    def test_printed_commands_can_be_replayed_verbatim(self):
        env = self.odd_env()
        rc, out = self.run_build_cli(env)
        verify_argv, mark_argv = (shlex.split(c)[2:] for c in self.commands(out))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(B.main(verify_argv), 0)                        # 带 --release 的 verify
            self.assertEqual(B.main(mark_argv), 0)
        self.assertTrue((env.out / "published.json").is_file())

    def test_replaying_without_the_allow_flag_would_be_refused(self):
        """证明开关确实有用：去掉 --allow-outside-root，同样的命令会被项目内守卫拒绝。"""
        env = self.odd_env()
        _, out = self.run_build_cli(env)
        argv = [a for a in shlex.split(self.commands(out)[0])[2:] if a != "--allow-outside-root"]
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(B.main(argv), 2)
        self.assertIn("项目目录之外", err.getvalue())

    def test_verify_release_flag_sets_exit_code(self):
        env = self.new_env()
        d = dict(DICT_ZH)
        del d["contact.email"]
        env.edit("shared/i18n-zh.json", json.dumps(d, ensure_ascii=False))
        env.pin()
        rep = env.build()
        args = ["--verify", rep["build_id"], "--out-root", str(env.out), "--allowlist", str(env.alp), "--allow-outside-root"]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(B.main(args), 0)                               # 完整性通过
            self.assertEqual(B.main(args + ["--release"]), 1)              # 但不满足发布条件
        ready = self.new_env().build()
        e2 = Path(ready["dir"]).parent.parent
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(B.main(["--verify", ready["build_id"], "--release", "--out-root", str(e2 / "build"),
                                     "--allowlist", str(e2 / "allow.json"), "--allow-outside-root"]), 0)


class SpecialRefBoundaryTests(Base):
    def test_loadmock_cannot_escape_mock_dir_on_site_check(self):
        rep = self.env.build()
        site = self.site(rep)
        (site.parent / "outside.json").write_text('{"_meta":{"note":"合成"}}', encoding="utf-8")       # 位于 site 外、但 ../../outside 能指到
        (site / "shared" / "page-landing.js").write_text("loadMock('../../outside');", encoding="utf-8")
        errs = "\n".join(B.check_site(site, B.load_allowlist(self.env.alp))["errors"])
        self.assertIn("__mock__:../../outside.json", errs)                  # 被当成"引用不到"，没有把站外文件认作闭包

    def test_loadmock_is_confined_to_mock_data_even_for_files_inside_the_site(self):
        rep = self.env.build()
        site = self.site(rep)
        (site / "shared" / "other.json").write_text("{}", encoding="utf-8")                # 在站内、但不在 mock-data 里的真实文件
        (site / "shared" / "page-landing.js").write_text("loadMock('../shared/other');", encoding="utf-8")
        errs = "\n".join(B.check_site(site, B.load_allowlist(self.env.alp))["errors"])
        self.assertIn("__mock__:../shared/other.json", errs)                               # 文件存在也不能被当成 mock 依赖

    def test_discover_does_not_crash_or_accept_outside_files(self):
        (Path(self.tmp.name) / "outside.json").write_text("{}", encoding="utf-8")
        self.env.edit("shared/page-landing.js", "import './dom.js'; loadMock('demo'); loadMock('../../outside');")
        d = B.discover(self.env.src, B.load_allowlist(self.env.alp))
        self.assertIn("__mock__:../../outside.json", json.dumps(d["unresolved_hard_refs"]))
        self.assertNotIn("outside.json", json.dumps(d["in_closure_not_in_allowlist"]))

    def test_symlinked_mock_is_not_accepted_as_a_dependency(self):
        rep = self.env.build()
        site = self.site(rep)
        real = site / "mock-data" / "real.json"
        real.write_text((site / "mock-data" / "demo.json").read_text(encoding="utf-8"), encoding="utf-8")
        (site / "mock-data" / "demo.json").unlink()
        os.symlink(real, site / "mock-data" / "demo.json")
        errs = "\n".join(B.check_site(site, B.load_allowlist(self.env.alp))["errors"])
        self.assertIn("符号链接", errs)
        self.assertIn("__mock__:demo.json", errs)


class Round3HardeningTests(Base):
    def test_copy_allowed_validates_before_it_creates_any_directory(self):
        al = json.loads(json.dumps(self.env.al))
        al["template"] = {"root-index.html": "../newdir/x.html"}              # 绕过 load_allowlist 直接交给 copy_allowed
        site = Path(self.tmp.name) / "s" / "site"
        site.mkdir(parents=True)
        with self.assertRaises(B.BuildError):
            B.copy_allowed(self.env.src, self.env.tpl, al, site)
        self.assertFalse((site.parent / "newdir").exists())                   # 站外连目录都没有被创建

    def test_build_id_is_rejected_at_the_format_layer_not_by_missing_directory(self):
        rep = self.env.build()
        for bad in (rep["build_id"] + "\n", "../x", "/abs", rep["build_id"] + ".FAILED", "", None, 7):
            with self.subTest(bad=bad):
                with self.assertRaises(B.BuildError) as cm:
                    B.verify(bad, self.env.out, self.env.alp)
                self.assertIn("格式不对", str(cm.exception))

    def test_windows_style_and_overlong_names_refused(self):
        base = self.env.al["src_files"]
        for bad in ("C:/x.js", "shared/a:b.js", "shared/CON.js", "shared/aux.txt", "shared/nul", "shared/Com1.js",
                    "shared/a.js.", "shared/a.js ", "shared/" + "x" * 130 + ".js", "a/" * 250 + "x.js"):
            with self.subTest(bad=bad[:40]):
                al = json.loads(json.dumps(self.env.al))
                al["src_files"] = base + [bad]
                self.env.alp.write_text(json.dumps(al, ensure_ascii=False), encoding="utf-8")
                with self.assertRaises(B.BuildError):
                    B.load_allowlist(self.env.alp)

    def test_corrupt_or_malformed_published_json_is_ignored_with_a_visible_warning(self):
        self.env.out.mkdir()
        for content in ("{not json", json.dumps({"files": "x"}), json.dumps({"files": [{"path": 1}]})):
            with self.subTest(content=content[:20]):
                (self.env.out / "published.json").write_text(content, encoding="utf-8")
                rep = self.env.build()
                self.assertTrue(rep["ok"])
                self.assertIn("published.json 存在但读不了", "\n".join(rep["warnings"]))
                self.assertFalse(rep["diff_vs_published"]["baseline"])

    def test_missing_published_json_gives_no_such_warning(self):
        rep = self.env.build()
        self.assertNotIn("published.json", "\n".join(rep["warnings"]))


# ================================================================ 命令行路径守卫

class CliGuardTests(Base):
    def run_main(self, argv):
        """守卫失效时 build 绝不能真的跑起来(会往真实 build/ 写东西)：换成"被调用就报错"的桩，让测试只是失败、没有副作用。"""
        out, err = io.StringIO(), io.StringIO()
        boom = AssertionError("路径守卫失效：build() 被调用了")
        with patch.object(B, "build", side_effect=boom), patch.object(B, "fresh_pin_report", side_effect=boom), \
                patch.object(B, "discover", side_effect=boom), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = B.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_paths_outside_project_refused_by_default(self):
        outside = tempfile.mkdtemp(prefix="outside-root-")
        self.addCleanup(lambda: shutil.rmtree(outside, ignore_errors=True))
        for flag in ("--src", "--template", "--allowlist", "--out-root"):
            with self.subTest(flag=flag):
                rc, _, err = self.run_main([flag, outside])
                self.assertEqual(rc, 2)
                self.assertIn("项目目录之外", err)
        self.assertEqual(os.listdir(outside), [])

    def test_default_paths_are_inside_project(self):
        for p in (B.SRC_DEFAULT, B.TEMPLATE_DEFAULT, B.ALLOWLIST_DEFAULT, B.BUILD_ROOT_DEFAULT):
            self.assertTrue(B._inside_root(p), p)


# ================================================================ 脚本卫生 + 真实仓库

class ScriptHygieneTests(unittest.TestCase):
    def test_script_has_no_network_or_subprocess_capability(self):
        tree = ast.parse((ROOT / "scripts" / "build_public_site.py").read_text(encoding="utf-8"))
        mods = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                mods.update(a.name.split(".")[0] for a in n.names)
            elif isinstance(n, ast.ImportFrom):
                mods.add((n.module or "").split(".")[0])
        for bad in ("subprocess", "socket", "urllib", "http", "requests", "ssl", "ftplib", "smtplib", "asyncio"):
            self.assertNotIn(bad, mods, bad)

    def test_script_does_not_read_secret_or_data_or_delete_dirs(self):
        src = (ROOT / "scripts" / "build_public_site.py").read_text(encoding="utf-8")
        self.assertNotIn('".secret"', src)
        self.assertNotIn('/ "data"', src)                                    # 不拼出 ROOT / "data" 之类的路径
        self.assertNotIn("shutil.rmtree", src)
        self.assertNotIn("os.remove", src)
        self.assertNotIn("os.rmdir", src)


class RealRepoTests(unittest.TestCase):
    """读真实的 dashboard/mockups 源码和白名单（代码，不是数据），构建结果写临时目录。"""

    def test_real_allowlist_covers_every_file_public_pages_need(self):
        al = B.load_allowlist(B.ALLOWLIST_DEFAULT)
        d = B.discover(B.SRC_DEFAULT, al)
        self.assertEqual(d["in_closure_not_in_allowlist"], [], "公开页面用到了白名单里没有的文件（页面会坏）：补进白名单前先人工审阅")
        self.assertEqual(d["unresolved_hard_refs"], {}, d["unresolved_hard_refs"])

    def test_real_allowlist_has_no_internal_looking_paths(self):
        al = B.load_allowlist(B.ALLOWLIST_DEFAULT)
        for rel in al["src_files"]:
            self.assertNotIn(".before-", rel)
            self.assertNotIn("page-ws-", rel)
            self.assertNotIn("/w/", "/" + rel)
            self.assertFalse(any(x in rel for x in ("admin", "holdings", "monitor", "workspace")) and "mock-data" not in rel and "i18n" not in rel, rel)

    def test_real_allowlist_pins_every_dictionary_mock_and_svg(self):
        al = B.load_allowlist(B.ALLOWLIST_DEFAULT)
        need = {d for _, _, d in B.plan_files(al) if B.needs_pin(d)}
        self.assertEqual(need, set(al["content_pins"]), "每个字典/mock/SVG 都必须钉死哈希，且不能有多余的钉子")

    def test_real_build_passes_checks_and_is_draft_only_until_owner_decisions(self):
        with tempfile.TemporaryDirectory() as t:
            rep = B.build(B.SRC_DEFAULT, B.TEMPLATE_DEFAULT, B.ALLOWLIST_DEFAULT, Path(t) / "b", Path(t) / "trash", 5)
            self.assertTrue(rep["ok"], rep["errors"])
            self.assertGreater(rep["stats"]["files"], 50)
            files = json.loads((Path(rep["dir"]) / "manifest.json").read_text(encoding="utf-8"))["files"]
            self.assertFalse(any("page-ws-" in f["path"] for f in files))
            if rep["blockers"]:                      # 所有者的发布条件（联系邮箱、数据口径）没解决之前，真实构建只是"草稿可检查"
                self.assertFalse(rep["release_ready"])


if __name__ == "__main__":
    unittest.main()
