"""/ui/ 只读静态路由的测试：用 AST 从 app.py 摘出 Handler 的相关方法，起一个临时本机服务器，
目录用 tempfile 造；不导入 app，不读真实 data/，不联网。"""
import ast
import hashlib
import hmac
import http.client
import os
import sys
import tempfile
import threading
import time
import unittest
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parent.parent
TREE = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
HANDLER = next(n for n in TREE.body if isinstance(n, ast.ClassDef) and n.name == "Handler")
METHODS = {n.name: n for n in HANDLER.body if isinstance(n, ast.FunctionDef)}
NAMES = ("do_GET", "do_HEAD", "_maintenance_blocked", "_section_blocked", "_send", "_ui_static", "_authed", "_remote", "_https",
         "_owner_cfg", "_owner", "_local_or_owner", "_origin_ok")
TOKEN = "synthetic-token"
COOKIE_HDR = {"Cookie": f"qb={TOKEN}"}


def top_level(*names):
    """摘出 app.py 顶层的同名函数或赋值。"""
    out = []
    for n in TREE.body:
        if isinstance(n, ast.FunctionDef) and n.name in names:
            out.append(n)
        elif isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in names for t in n.targets):
            out.append(n)
    return out


def build_class(ui_dir):
    cls = ast.ClassDef(name="Handler", bases=[ast.Name(id="BaseHTTPRequestHandler", ctx=ast.Load())],
                       keywords=[], decorator_list=[], body=[METHODS[n] for n in NAMES])
    body = top_level("UI_TYPES", "COMPRESSIBLE", "_gzip_cached", "_gz_cache") + [cls]
    ns = dict(BaseHTTPRequestHandler=BaseHTTPRequestHandler, urlparse=urlparse, parse_qs=parse_qs,
              unquote=unquote, hashlib=hashlib, hmac=hmac, Path=Path, SimpleCookie=SimpleCookie,
              UI_DIR=ui_dir, COOKIE="qb", MEMBER_COOKIE="qm", TOKEN=TOKEN, owner_access=None, ROOT=None,
              _bad_login=[], _alive={}, time=time, _gz_cache={})
    exec(compile(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])), "<handler>", "exec"), ns)

    class Test(ns["Handler"]):
        def log_message(self, *a):
            pass

        def _guard(self):
            return True

        def _personal_blocked(self):
            return False

        def _share_gate(self, u):
            return False

        def _page(self, name):       # 首页：固定内容，用来对照 /ui/ 的未登录结果
            self._send(200, b"home", "text/html; charset=utf-8", cache="private, no-cache")

    return Test


class UiStaticTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.ui = base / "ui"
        (self.ui / "w").mkdir(parents=True)
        (self.ui / "index.html").write_text("<html>idx</html>", encoding="utf-8")
        (self.ui / "w" / "today.html").write_text("<html>today</html>", encoding="utf-8")
        (self.ui / "app.js").write_text("var a=1;", encoding="utf-8")
        (self.ui / "x.css").write_text("a{}", encoding="utf-8")
        (self.ui / "d.json").write_text("{}", encoding="utf-8")
        (self.ui / "i.svg").write_text("<svg/>", encoding="utf-8")
        (self.ui / "p.png").write_bytes(b"\x89PNG")
        (self.ui / "q.webp").write_bytes(b"RIFF")
        (self.ui / "f.woff2").write_bytes(b"wOF2")
        (self.ui / "big.js").write_text("x" * 5000, encoding="utf-8")
        for bad in (".hidden.js", "secret.py", "notes.md", "nav.js.before-N03", "app.js.before"):
            (self.ui / bad).write_text("no", encoding="utf-8")
        (self.ui / ".dot").mkdir()
        (self.ui / ".dot" / "a.js").write_text("no", encoding="utf-8")
        self.outside = base / "outside.js"
        self.outside.write_text("outside", encoding="utf-8")
        os.symlink(self.outside, self.ui / "link.js")
        os.symlink(base, self.ui / "dirlink")
        self.start(self.ui)

    def start(self, ui_dir):
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), build_class(ui_dir))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)

    def req(self, path, method="GET", headers=None, auth=True):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_port, timeout=5)
        hdr = dict(COOKIE_HDR) if auth else {}
        hdr.update(headers or {})
        c.request(method, path, headers=hdr)      # http.client 不会改写路径，../ 和 %2e%2e 原样发出
        r = c.getresponse()
        body = r.read()
        out = (r.status, r.getheader("Content-Type"), body, r)
        c.close()
        return out

    def test_normal_files_and_content_type(self):
        cases = {"/ui/": "text/html; charset=utf-8", "/ui/index.html": "text/html; charset=utf-8",
                 "/ui/w/today.html": "text/html; charset=utf-8", "/ui/app.js": "text/javascript; charset=utf-8",
                 "/ui/x.css": "text/css; charset=utf-8", "/ui/d.json": "application/json; charset=utf-8",
                 "/ui/i.svg": "image/svg+xml", "/ui/p.png": "image/png", "/ui/q.webp": "image/webp",
                 "/ui/f.woff2": "font/woff2", "/ui/app.js?x=1": "text/javascript; charset=utf-8"}
        for path, ctype in cases.items():
            code, ct, body, r = self.req(path)
            self.assertEqual((code, ct), (200, ctype), path)
            self.assertEqual(r.getheader("Cache-Control"), "private, no-cache", path)
            self.assertTrue(r.getheader("ETag"), path)
            self.assertIn("script-src 'self'", r.getheader("Content-Security-Policy"), path)   # 走 _send，安全头不变
        self.assertEqual(self.req("/ui/")[2], b"<html>idx</html>")

    def test_redirect_without_slash(self):
        code, _, _, r = self.req("/ui")
        self.assertEqual((code, r.getheader("Location")), (301, "/ui/"))

    def test_etag_304(self):
        etag = self.req("/ui/app.js")[3].getheader("ETag")
        code, _, body, _ = self.req("/ui/app.js", headers={"If-None-Match": etag})
        self.assertEqual((code, body), (304, b""))

    def test_rejected_paths_all_404(self):
        bad = ["/ui/../outside.js", "/ui/%2e%2e/outside.js", "/ui/%2E%2E/outside.js", "/ui/w/../../outside.js",
               "/ui/..%2foutside.js", "/ui/%2e%2e%2foutside.js", "/ui//etc/passwd", "/ui//outside.js",
               "/ui/" + str(self.outside), "/ui/%2Fetc%2Fpasswd.js", "/ui/.hidden.js", "/ui/.dot/a.js",
               "/ui/w/.x.js", "/ui/secret.py", "/ui/notes.md", "/ui/nav.js.before-N03", "/ui/app.js.before",
               "/ui/link.js", "/ui/dirlink/outside.js", "/ui/w/", "/ui/w", "/ui/missing.js", "/ui/app.js%00.png",
               "/ui/..\\outside.js", "/ui/app.JS.py", "/ui/%252e%252e/outside.js"]
        for path in bad:
            code, _, body, _ = self.req(path)
            self.assertEqual((code, body), (404, b"not found"), path)      # 不透露原因：正文都一样

    def test_uppercase_suffix_allowed_only_if_whitelisted(self):
        (self.ui / "U.JS").write_text("u", encoding="utf-8")
        self.assertEqual(self.req("/ui/U.JS")[0], 200)

    def test_unauthenticated_same_as_home(self):
        home = self.req("/", auth=False)
        for path in ("/ui/", "/ui", "/ui/app.js", "/ui/missing.js", "/ui/../outside.js"):
            got = self.req(path, auth=False)
            self.assertEqual((got[0], got[1], got[2]), (home[0], home[1], home[2]), path)
        self.assertEqual(home[0], 401)
        wrong = self.req("/ui/", headers={"Cookie": "qb=wrong"}, auth=False)
        self.assertEqual(wrong[0], 401)
        self.assertEqual(self.req("/", headers={"Cookie": "qb=wrong"}, auth=False)[2], wrong[2])

    def test_authed_status_matches_home(self):
        self.assertEqual(self.req("/ui/")[0], self.req("/")[0])

    def test_head_has_no_body(self):
        code, ct, body, r = self.req("/ui/index.html", method="HEAD")
        self.assertEqual((code, body, ct), (200, b"", "text/html; charset=utf-8"))
        self.assertEqual(r.getheader("Content-Length"), str(len(b"<html>idx</html>")))
        self.assertEqual(self.req("/ui/missing.js", method="HEAD")[:3:2], (404, b""))
        self.assertEqual(self.req("/ui/app.js", method="HEAD", auth=False)[0], 401)

    def test_head_other_paths_unchanged(self):
        self.assertEqual(self.req("/", method="HEAD")[0], 501)       # 非 /ui/ 的 HEAD 保持原来的 501

    def test_gzip_for_large_file(self):
        code, _, body, r = self.req("/ui/big.js", headers={"Accept-Encoding": "gzip"})
        self.assertEqual((code, r.getheader("Content-Encoding")), (200, "gzip"))

    def test_missing_ui_dir_all_404(self):
        self.srv.shutdown()
        self.start(Path(self.tmp.name) / "nope")
        for path in ("/ui/", "/ui/index.html", "/ui/app.js"):
            self.assertEqual(self.req(path)[0], 404, path)
        self.assertEqual(self.req("/")[0], 200)

    def test_post_not_routed_to_ui(self):
        src = ast.get_source_segment((ROOT / "app.py").read_text(encoding="utf-8"), METHODS_FULL["do_POST"])
        self.assertNotIn("_ui_static", src)
        self.assertNotIn("/ui", src)

    def test_dotfile_name_check_not_substring(self):
        (self.ui / "a.b.js").write_text("ok", encoding="utf-8")       # 文件名中间有点是正常的
        self.assertEqual(self.req("/ui/a.b.js")[0], 200)


METHODS_FULL = METHODS

if __name__ == "__main__":
    unittest.main()
