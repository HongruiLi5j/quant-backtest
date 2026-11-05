"""/ui/ 只读静态路由的测试：用 AST 从 app.py 摘出 Handler 的相关方法，起一个临时本机服务器，
目录用 tempfile 造；不导入 app，不读真实 data/，不联网。"""
import ast
import json
import re
import hashlib
import hmac
import http.client
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))   # D65：摘出的 _ui_versioned_html 会 from src import ui_preload；直接运行本文件时也要能找到 src
TREE = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
HANDLER = next(n for n in TREE.body if isinstance(n, ast.ClassDef) and n.name == "Handler")
METHODS = {n.name: n for n in HANDLER.body if isinstance(n, ast.FunctionDef)}
NAMES = ("do_GET", "do_HEAD", "_maintenance_blocked", "_section_blocked", "_send", "_ui_static", "_authed", "_remote", "_https",
         "_owner_cfg", "_owner", "_local_or_owner", "_origin_ok", "_wants_new_ui")
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
    body = top_level("UI_TYPES", "COMPRESSIBLE", "_gzip_cached", "_gz_cache",
                     "_ui_hash_cache", "_ui_hash", "_ui_importmap", "_ui_versioned_html", "_UI_FETCHED",
                     "_css_bundles", "_LINK_RE", "_merge_css_links", "_css_bundle_bytes") + [cls]   # D20 哈希版本号
    ns = dict(BaseHTTPRequestHandler=BaseHTTPRequestHandler, urlparse=urlparse, parse_qs=parse_qs, json=json, re=re,
              unquote=unquote, hashlib=hashlib, hmac=hmac, Path=Path, SimpleCookie=SimpleCookie,
              UI_DIR=ui_dir, COOKIE="qb", MEMBER_COOKIE="qm", TOKEN=TOKEN, owner_access=None, ROOT=None,
              _bad_login=[], _alive={}, time=time, _gz_cache={},
              admin_settings=types.SimpleNamespace(new_ui_home_on=lambda: True))      # 首页跳新界面的开关：默认开
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
        # poll_interval 默认 0.5 秒，shutdown() 会白等；改 10 毫秒，只缩短关闭延迟
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
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
        body = self.req("/ui/")[2]       # D20：页面里插了 import map，原文仍在
        self.assertTrue(body.startswith(b'<script type="importmap">') and body.endswith(b"<html>idx</html>"), body[:80])

    def test_d112_css_links_merged_into_one_bundle(self):
        """D112：连续的两个以上样式表合并成一个 /ui/_b/<id>.css，顺序不变；清单外的 id 一律 404；含 url() 的不合并。"""
        (self.ui / "y.css").write_text("b{}", encoding="utf-8")
        (self.ui / "u.css").write_text("c{background:url(z.png)}", encoding="utf-8")
        (self.ui / "w" / "m.html").write_text('<html><head><meta charset="utf-8"><link rel="stylesheet" href="../x.css">'
                                              '<link rel="stylesheet" href="../y.css"></head></html>', encoding="utf-8")
        (self.ui / "w" / "n.html").write_text('<html><head><meta charset="utf-8"><link rel="stylesheet" href="../x.css">'
                                              '<link rel="stylesheet" href="../u.css"></head></html>', encoding="utf-8")
        _, _, body, _ = self.req("/ui/w/m.html")
        m = re.search(r'href="(/ui/_b/([0-9a-f]{10})\.css\?v=([0-9a-f]{10}))"', body.decode())
        self.assertIsNotNone(m)
        code, _, css, r = self.req(m.group(1))
        self.assertEqual((code, css), (200, b"a{}\nb{}"))
        self.assertIn("immutable", r.getheader("Cache-Control"))
        self.assertEqual(self.req("/ui/_b/0000000000.css")[0], 404)
        _, _, body2, _ = self.req("/ui/w/n.html")
        self.assertNotIn("/ui/_b/", body2.decode())                        # 含 url() 的不合并

    def test_d20_versioned_assets_and_importmap(self):
        """D20：页面里的相对 js/css 加 ?v=内容哈希；import map 覆盖所有 js；CSP 只放行这张 map 的 sha256；
        带 ?v= 的静态文件长期缓存，不带的仍 no-cache，HTML 永远 no-cache。"""
        import base64
        page = ('<html><head><meta charset="utf-8"><link rel="stylesheet" href="../x.css">'
                '<script type="module" src="../app.js"></script><a href="#x"></a><svg><use href="../i.svg#i-x"></use></svg>'
                '<link href="https://cdn.example/a.css"><script src="../missing.js"></script></head></html>')
        (self.ui / "w" / "v.html").write_text(page, encoding="utf-8")
        code, _, body, r = self.req("/ui/w/v.html")
        html = body.decode()
        h_css = hashlib.sha1(b"a{}").hexdigest()[:10]
        h_js = hashlib.sha1(b"var a=1;").hexdigest()[:10]
        self.assertIn(f'href="../x.css?v={h_css}"', html)
        self.assertIn(f'src="../app.js?v={h_js}"', html)
        self.assertIn(f'<link rel="modulepreload" href="/ui/app.js?v={h_js}">', html)   # D65：模块预加载用同一个带哈希地址
        self.assertIn('href="https://cdn.example/a.css"', html)            # 外部地址不动
        self.assertIn(f'<use href="../i.svg?v={hashlib.sha1(b"<svg/>").hexdigest()[:10]}#i-x">', html)   # D39：svg 片段在版本号后面
        self.assertIn('<meta name="qb-asset-v" content="">', html)          # 测试目录里没有字典/图标：表为空
        self.assertIn('src="../missing.js"', html)                          # 不存在的文件不加版本号
        self.assertLess(html.index("<meta charset"), html.index("importmap"))
        imap_txt = re.search(r'<script type="importmap">(.*?)</script>', html).group(1)
        imap = json.loads(imap_txt)["imports"]
        self.assertEqual(imap["/ui/app.js"], f"/ui/app.js?v={h_js}")
        self.assertIn("/ui/big.js", imap)
        self.assertFalse(any("/.dot/" in k or ".hidden" in k for k in imap))  # 点开头的文件和目录不进 map
        want = "sha256-" + base64.b64encode(hashlib.sha256(imap_txt.encode()).digest()).decode()
        self.assertIn(f"script-src 'self' '{want}';", r.getheader("Content-Security-Policy"))
        self.assertEqual(r.getheader("Cache-Control"), "private, no-cache")
        self.assertEqual(self.req("/ui/app.js?v=" + h_js)[3].getheader("Cache-Control"), "private, max-age=31536000, immutable")
        self.assertEqual(self.req("/ui/w/v.html?v=1")[3].getheader("Cache-Control"), "private, no-cache")   # HTML 不长期缓存
        self.assertEqual(self.req("/ui/app.js?v=0000000000")[3].getheader("Cache-Control"), "private, no-cache")   # 哈希不符：不长期缓存
        self.assertEqual(self.req("/ui/app.js?v=")[3].getheader("Cache-Control"), "private, no-cache")
        self.assertEqual(self.req("/ui/app.js")[3].getheader("Cache-Control"), "private, no-cache")
        js_csp = self.req("/ui/app.js")[3].getheader("Content-Security-Policy")
        self.assertNotIn("sha256-", js_csp)                                  # 只有页面响应带哈希
        (self.ui / "app.js").write_text("var a=2;", encoding="utf-8")       # 内容变了：哈希跟着变
        self.assertIn(f'src="../app.js?v={hashlib.sha1(b"var a=2;").hexdigest()[:10]}"', self.req("/ui/w/v.html")[2].decode())
        self.assertEqual(self.req("/ui/app.js?v=" + h_js)[3].getheader("Cache-Control"), "private, no-cache")   # 旧哈希地址拿到新内容：不缓存一年

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
        home = self.req("/?classic=1", auth=False)      # 首页默认会跳新界面，对照旧首页要带 classic=1
        for path in ("/ui/", "/ui", "/ui/app.js", "/ui/missing.js", "/ui/../outside.js"):
            got = self.req(path, auth=False)
            self.assertEqual((got[0], got[1], got[2]), (home[0], home[1], home[2]), path)
        self.assertEqual(home[0], 401)
        wrong = self.req("/ui/", headers={"Cookie": "qb=wrong"}, auth=False)
        self.assertEqual(wrong[0], 401)
        self.assertEqual(self.req("/?classic=1", headers={"Cookie": "qb=wrong"}, auth=False)[2], wrong[2])

    def test_authed_status_matches_home(self):
        self.assertEqual(self.req("/ui/")[0], self.req("/?classic=1")[0])

    def test_home_redirects_to_new_ui_unless_classic(self):
        code, _, _, r = self.req("/")
        self.assertEqual((code, r.getheader("Location")), (302, "/ui/"))
        self.assertEqual(self.req("/?classic=1")[0], 200)                 # 明确要旧界面
        host = f"127.0.0.1:{self.srv.server_port}"
        from_ui = {"Referer": f"http://{host}/ui/w/today.html"}           # 从新界面里点出来的旧页链接
        self.assertEqual(self.req("/", headers=from_ui)[0], 200)
        other = {"Referer": "http://evil.example/ui/w/today.html"}        # 别的网站的来源页不算
        self.assertEqual(self.req("/", headers=other)[0], 302)

    def test_head_has_no_body(self):
        code, ct, body, r = self.req("/ui/index.html", method="HEAD")
        self.assertEqual((code, body, ct), (200, b"", "text/html; charset=utf-8"))
        self.assertEqual(r.getheader("Content-Length"), str(len(self.req("/ui/index.html")[2])))   # 与 GET 正文一致
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
