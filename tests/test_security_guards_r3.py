"""D74（R3-test-1 安全部分）：把通宵审核时手工核对过的几条安全性质写成测试，以后改代码时自动守住。
python -m pytest tests/test_security_guards_r3.py

不启动服务、不导入 app.py（app.py 导入时会读 .secret 里的真实令牌，测试不能碰），只读源文件：
1. 新界面页面（dashboard/ui）没有内联 <script>、内联事件（onclick= 之类）、javascript: 链接——
   页面的 CSP 是 script-src 'self'（外加服务器注入的那一张 import map 的哈希），这些写法会被浏览器静默拦掉；
2. 新界面脚本不用 innerHTML / insertAdjacentHTML / document.write / eval / new Function（目前 0 处，守住这个 0）；
3. app.py 发出的 CSP：script-src 只有 'self'（加可选的单个哈希），没有 unsafe-inline / unsafe-eval / 外部域名；
   frame-ancestors 'none'、object-src 'none'、base-uri 'none' 都还在；
4. 公开站导出的页面（scripts/public_allowlist.json 里的 .html）：CSP 里没有任何外部域名；
   <script src> / <link href> 不指向外部网站；内联 <script> 只允许内容哈希写在该页 CSP 里的那几段（落地页的 import map）。
已有测试覆盖、这里不重复的：import map 的 CSP 哈希与长期缓存只给对的 ?v=（tests/test_ui_static.py，D20），
自带第三方库的哈希钉死（scripts/build_public_site.py 构建检查，6184e8b）。
"""
import base64
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "dashboard" / "ui"
MOCK = ROOT / "dashboard" / "mockups"

SCRIPT_TAG = re.compile(r"<script\b([^>]*)>(.*?)</script>", re.S | re.I)
INLINE_EVENT = re.compile(r"<[a-z][^>]*\son[a-z]+\s*=", re.I)
JS_URL = re.compile(r"""(?:href|src|action)\s*=\s*["']\s*javascript:""", re.I)
EXTERNAL = re.compile(r"https?://", re.I)


def _ui_pages():
    pages = sorted((UI / "w").glob("*.html")) + [UI / "index.html"]
    return [p for p in pages if p.is_file()]


def _ui_scripts():
    out = []
    for d in ("pages", "shared"):
        out += sorted((UI / d).rglob("*.js"))
    out.append(UI / "nav.js")
    return [p for p in out if p.is_file() and "vendor" not in p.parts]


def test_ui_pages_have_no_inline_script_or_handlers():
    bad = []
    for p in _ui_pages():
        html = p.read_text(encoding="utf-8")
        for attrs, body in SCRIPT_TAG.findall(html):
            if "src=" not in attrs:
                bad.append(f"{p.name}: inline <script>{body.strip()[:40]}")
            elif EXTERNAL.search(attrs):
                bad.append(f"{p.name}: external script {attrs.strip()[:80]}")
        if INLINE_EVENT.search(html):
            bad.append(f"{p.name}: inline event handler")
        if JS_URL.search(html):
            bad.append(f"{p.name}: javascript: URL")
    assert len(_ui_pages()) >= 10, "找不到新界面页面，测试路径要跟着改"
    assert bad == [], bad


def test_ui_scripts_never_build_html_from_strings():
    pat = re.compile(r"\.innerHTML\s*=|\.outerHTML\s*=|insertAdjacentHTML\s*\(|document\.write\s*\(|(?<![.\w])eval\s*\(|new\s+Function\s*\(")
    bad = []
    for p in _ui_scripts():
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("//", 1)[0]          # 注释里提到这些词不算
            if pat.search(code):
                bad.append(f"{p.relative_to(UI)}:{i}: {line.strip()[:80]}")
    assert len(_ui_scripts()) >= 20
    assert bad == [], "新界面用 el() / textContent 拼页面，不要用 HTML 字符串:\n" + "\n".join(bad)


def _app_csp_template() -> str:
    """把 app.py 里 send_header("Content-Security-Policy", ...) 的字符串片段拼起来（哈希那段用 HASH 占位）。"""
    src = (ROOT / "app.py").read_text(encoding="utf-8")
    m = re.search(r'send_header\(\s*"Content-Security-Policy",(.*?)\)\n', src, re.S)
    assert m, "app.py 里找不到 CSP 响应头，测试要跟着改"
    expr = m.group(1)
    expr = re.sub(r'\(f" \'\{script_hash\}\'" if script_hash else ""\)', '" HASH"', expr)
    parts = re.findall(r'"((?:[^"\\]|\\.)*)"', expr)
    return "".join(parts)


def test_app_csp_stays_strict():
    csp = _app_csp_template()
    d = {k.strip().split()[0]: k.strip() for k in csp.split(";") if k.strip()}
    script = d.get("script-src", "")
    assert script in ("script-src 'self'", "script-src 'self' HASH"), script
    for k in ("default-src", "script-src", "connect-src", "img-src"):
        assert not EXTERNAL.search(d.get(k, "")), (k, d.get(k))
    assert "'unsafe-eval'" not in csp
    assert d.get("frame-ancestors") == "frame-ancestors 'none'"
    assert d.get("object-src") == "object-src 'none'"
    assert d.get("base-uri") == "base-uri 'none'"


def _public_pages():
    allow = json.loads((ROOT / "scripts" / "public_allowlist.json").read_text(encoding="utf-8"))
    return [MOCK / p for p in allow["src_files"] if str(p).endswith(".html") and (MOCK / p).is_file()]


def test_public_pages_load_nothing_from_other_sites():
    pages = _public_pages()
    if not pages:   # 公开站源文件在真项目里被 gitignore；没有源文件的环境跳过
        import pytest
        pytest.skip("dashboard/mockups 不在（真项目里是 gitignore 的）")
    bad = []
    for p in pages:
        html = p.read_text(encoding="utf-8")
        rel = p.relative_to(MOCK)
        m = re.search(r'http-equiv="Content-Security-Policy"\s+content="([^"]*)"', html)
        if not m:
            bad.append(f"{rel}: no CSP meta")
            continue
        csp = m.group(1)
        if EXTERNAL.search(csp):
            bad.append(f"{rel}: CSP allows another site: {EXTERNAL.findall(csp)}")
        hashes = set(re.findall(r"'(sha256-[A-Za-z0-9+/=]+)'", csp))
        for attrs, body in SCRIPT_TAG.findall(html):
            if "src=" in attrs:
                if EXTERNAL.search(attrs):
                    bad.append(f"{rel}: external script {attrs.strip()[:80]}")
                continue
            h = "sha256-" + base64.b64encode(hashlib.sha256(body.encode("utf-8")).digest()).decode()
            if h not in hashes:
                bad.append(f"{rel}: inline <script> not covered by a CSP hash ({body.strip()[:40]})")
        for tag in re.findall(r"<link\b[^>]*>", html, re.I):
            if EXTERNAL.search(tag) and re.search(r'rel="(stylesheet|preload|modulepreload|preconnect)"', tag):
                bad.append(f"{rel}: external <link> {tag[:90]}")
        if INLINE_EVENT.search(html) or JS_URL.search(html):
            bad.append(f"{rel}: inline event handler / javascript: URL")
    assert bad == [], "\n".join(bad)


if __name__ == "__main__":   # 直接 python tests/test_security_guards_r3.py 也能跑（不依赖 pytest）
    import sys
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok", name)
            except Exception as e:   # noqa: BLE001
                if type(e).__name__ == "Skipped":
                    print("skip", name, e)
                else:
                    failed += 1
                    print("FAIL", name, str(e)[:300])
    sys.exit(1 if failed else 0)
