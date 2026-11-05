#!/usr/bin/env python3
"""静态资源体积体检(只读):每个页面加载多少 JS/CSS,哪些文件没人引用,哪些 CSS 类名在 html/js 里搜不到。

做什么(都只读 dashboard/ui,不改、不删任何文件;只有 --out / --classes-csv 指定的文件会被写入):
  1. 每个 w/*.html:沿着 <script src>、<link rel=stylesheet>、JS 的 import 链、CSS 的 @import 找出
     它真正会加载的 js / css,按字节(原始 + 估算 gzip)汇总。
     页面入口 shared/page-boot.js 用 import(`../pages/${page}.js`) 按 <body data-page> 加载页面模块,脚本照这个规则展开。
     写成字面量的动态 import(如 debug-fields.js,只在 ?debug=1 时才加载)单列为"按需加载",不计入常驻总量。
  2. 同一个文件被多少页面加载、重复请求累计多大;内容完全一样的文件。
  3. 没有任何页面引用的 css / js(以及备份残留文件),并列出仓库其他地方(python/测试/文档)是否还提到它。
  4. CSS 里定义、但在所有 html / js 的字符串里都搜不到的类名(默认列前 50 个)。
  5. 每页加载的 CSS 里,估算有多少规则在这一页永远用不上(类名没用到,或限定了别的 data-page)。

限制(详见报告):类名是靠"字符串里有没有这个词"判断的,服务端接口返回的类名、运行时拼出来的类名可能漏判。
所以类名结果只是"待站主确认的候选",本脚本不会也不应该自动删除任何东西。

示例:.venv/bin/python scripts/ui_asset_report.py
      .venv/bin/python scripts/ui_asset_report.py --out /tmp/ui_report.md --json-out /tmp/ui_report.json
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import sys
from collections import OrderedDict, defaultdict
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_UI = ROOT / "dashboard" / "ui"
GZIP_MIN = 1024                     # 和 app.py 一致:小于等于 1KB 不压缩
BACKUP_RE = re.compile(r"\.(before|bak|orig)([-_.\w]*)$")
CODE_EXT = {".js", ".mjs", ".css"}
# 服务端 /ui/ 只放行这些后缀(app.py 的 UI_TYPES);不在里面的文件即使存在,浏览器也拿不到
SERVED_EXT = {".html", ".js", ".css", ".json", ".svg", ".png", ".webp", ".woff2"}


# ---------------------------------------------------------------- JS 词法:去注释 + 取字符串
_REGEX_PREV = set("(,=:[!&|?{};+-*%<>~^")
_REGEX_WORDS = {"return", "typeof", "case", "in", "of", "delete", "void", "throw", "new", "else", "do", "instanceof", "yield", "await"}


class _Lexer:
    """极简 JS 扫描器:注释抹成空格(保留行号),字符串/模板/正则字面量按语法跳过。

    不用 node、不装依赖。目的只有两个:import 语句不被注释里的同名文字骗到;
    类名只从字符串里取,不从变量名/注释里取(否则 row、card 这种常用词会让"没用到"全被淹没)。
    """

    def __init__(self, src: str):
        self.s, self.n = src, len(src)
        self.out: list[str] = []
        self.strings: list[tuple[str, str, bool]] = []   # (种类 s/t, 文本, 后面紧跟 ${ )
        self.ok = True
        self.prev, self.word = "", ""

    def run(self):
        self.code(0, False)
        return "".join(self.out), self.strings, self.ok

    def regex_end(self, i: int) -> int:
        """i 处的 / 若是正则字面量,返回结束位置(含 /),否则 -1。"""
        s, n, j, in_cls = self.s, self.n, i + 1, False
        while j < n:
            ch = s[j]
            if ch == "\n":
                return -1
            if ch == "\\":
                j += 2
                continue
            if ch == "[":
                in_cls = True
            elif ch == "]":
                in_cls = False
            elif ch == "/" and not in_cls:
                return j + 1
            j += 1
        return -1

    def template(self, i: int) -> int:
        s, n = self.s, self.n
        self.out.append("`")
        i += 1
        buf: list[str] = []
        while i < n:
            c = s[i]
            if c == "\\":
                buf.append(s[i:i + 2]); self.out.append(s[i:i + 2]); i += 2
                continue
            if c == "`":
                self.strings.append(("t", "".join(buf), False)); self.out.append("`")
                return i + 1
            if c == "$" and s[i + 1:i + 2] == "{":
                self.strings.append(("t", "".join(buf), True)); buf = []
                self.out.append("${")
                i = self.code(i + 2, True)
                self.out.append("}")
                i += 1
                continue
            buf.append(c); self.out.append(c); i += 1
        self.ok = False
        self.strings.append(("t", "".join(buf), False))
        return n

    def code(self, i: int, in_tpl: bool) -> int:
        s, n, depth = self.s, self.n, 0
        while i < n:
            c, nxt = s[i], s[i + 1:i + 2]
            if c == "/" and nxt == "/":
                j = s.find("\n", i)
                j = n if j < 0 else j
                self.out.append(" " * (j - i)); i = j
                continue
            if c == "/" and nxt == "*":
                j = s.find("*/", i + 2)
                if j < 0:
                    self.ok, j = False, n
                else:
                    j += 2
                self.out.append(re.sub(r"[^\n]", " ", s[i:j])); i = j
                continue
            if c in "\"'":
                j = i + 1
                while j < n and s[j] != c and s[j] != "\n":
                    j += 2 if s[j] == "\\" else 1
                if j >= n or s[j] != c:
                    self.ok = False
                self.strings.append(("s", s[i + 1:j], False))
                self.out.append(s[i:j + 1]); i = j + 1
                self.prev, self.word = c, ""
                continue
            if c == "`":
                i = self.template(i)
                self.prev, self.word = "`", ""
                continue
            if c == "/" and (self.prev == "" or self.prev in _REGEX_PREV or self.word in _REGEX_WORDS):
                j = self.regex_end(i)
                if j > 0:
                    self.out.append(s[i:j]); i = j
                    self.prev, self.word = "a", ""
                    continue
            if in_tpl:
                if c == "{":
                    depth += 1
                elif c == "}":
                    if depth == 0:
                        return i
                    depth -= 1
            if c.isalnum() or c in "_$":
                j = i
                while j < n and (s[j].isalnum() or s[j] in "_$"):
                    j += 1
                self.out.append(s[i:j]); self.word, self.prev = s[i:j], "a"
                i = j
                continue
            self.out.append(c)
            if not c.isspace():
                self.prev, self.word = c, ""
            i += 1
        if in_tpl:
            self.ok = False
        return n


def lex_js(src: str):
    """返回 (抹掉注释后的源码, 字符串列表, 是否扫描干净)。"""
    return _Lexer(src).run()


_STATIC_RE = re.compile(r"""^[ \t]*(?:import|export)\s+(?:[\w\s{},*$]*?\bfrom\s*)?(['"])([^'"\n]+)\1""", re.M)
_DYN_RE = re.compile(r"""\bimport\s*\(\s*(['"`])([^'"`\n]+?)\1\s*\)""")
_URL_RE = re.compile(r"""new\s+URL\(\s*([`'"])([^`'"\n]+?)\1\s*,\s*import\.meta\.url""")


class Module:
    """一个 JS 文件里和"加载哪些文件"有关的信息。"""

    def __init__(self, path: Path, text: str):
        code, strings, self.ok = lex_js(text)
        self.path = path
        self.static = [m.group(2) for m in _STATIC_RE.finditer(code)]
        self.lazy, self.template = [], []
        for m in _DYN_RE.finditer(code):
            (self.template if "${" in m.group(2) else self.lazy).append(m.group(2))
        self.urls = [m.group(2) for m in _URL_RE.finditer(code)]          # new URL('../i18n/${lang}.json', import.meta.url)
        self.strings = strings


# ---------------------------------------------------------------- HTML
class _Html(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.css: list[str] = []
        self.scripts: list[str] = []
        self.data_page = ""
        self.values: list[str] = []       # 所有属性值(类名、选择器线索都在里面)

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        for v in a.values():
            self.values.append(v)
        if tag == "link" and "stylesheet" in a.get("rel", "").split() and a.get("href"):
            self.css.append(a["href"])
        elif tag == "script" and a.get("src"):
            self.scripts.append(a["src"])
        if tag == "body" and a.get("data-page"):
            self.data_page = a["data-page"]


# ---------------------------------------------------------------- CSS
class CssRule:
    __slots__ = ("selectors", "bytes", "line", "offset")

    def __init__(self, selectors, nbytes, line, offset):
        self.selectors, self.bytes, self.line, self.offset = selectors, nbytes, line, offset


def _strip_css_comments(text: str) -> str:
    return re.sub(r"/\*.*?\*/", lambda m: re.sub(r"[^\n]", " ", m.group(0)), text, flags=re.S)


def _skip_str(t: str, i: int) -> int:
    q, i = t[i], i + 1
    while i < len(t) and t[i] != q:
        i += 2 if t[i] == "\\" else 1
    return i + 1


def _prelude_end(t: str, i: int) -> int:
    """从 i 起找下一个 { } ; (跳过引号和括号里的,比如 url(data:...;base64,...))。"""
    n = len(t)
    while i < n and t[i] not in "{};":
        if t[i] in "\"'":
            i = _skip_str(t, i)
        elif t[i] == "(":
            depth = 0
            while i < n:
                if t[i] in "\"'":
                    i = _skip_str(t, i)
                    continue
                depth += t[i] == "("
                depth -= t[i] == ")"
                i += 1
                if depth == 0:
                    break
        else:
            i += 1
    return i


def _skip_block(t: str, i: int) -> int:
    """i 在 { 之后,返回匹配的 } 之后的位置。"""
    depth, n = 1, len(t)
    while i < n and depth:
        if t[i] in "\"'":
            i = _skip_str(t, i)
            continue
        depth += t[i] == "{"
        depth -= t[i] == "}"
        i += 1
    return i


def _split_top_commas(sel: str) -> list[str]:
    parts, depth, cur = [], 0, []
    for ch in sel:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur).strip()); cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur).strip())
    return [p for p in parts if p]


def parse_css(raw: str):
    """返回 (规则列表, @import 列表)。规则 = 普通样式规则(含 @media/@supports 里面的);
    @keyframes / @font-face 等不判断死活,不列入。字节数按原文该规则所占的 UTF-8 字节算(估算,不含规则前面的注释)。"""
    t = _strip_css_comments(raw)
    n = len(t)
    rules: list[CssRule] = []
    imports: list[str] = []
    line_at = [0]
    for m in re.finditer("\n", t):
        line_at.append(m.start() + 1)

    def line_of(off: int) -> int:
        lo, hi = 0, len(line_at)
        while lo < hi - 1:
            mid = (lo + hi) // 2
            if line_at[mid] <= off:
                lo = mid
            else:
                hi = mid
        return lo + 1

    def block(i: int) -> int:
        while i < n:
            while i < n and t[i].isspace():
                i += 1
            start = i
            j = _prelude_end(t, i)
            if j >= n:
                return n
            ch, prelude = t[j], t[start:j].strip()
            if ch == "}":
                return j + 1
            if ch == ";":
                m = re.match(r"@import\s+(?:url\()?\s*['\"]?([^'\")\s]+)", prelude)
                if m:
                    imports.append(m.group(1))
                i = j + 1
                continue
            if prelude.startswith("@"):
                name = re.match(r"@([\w-]+)", prelude)
                if name and name.group(1) in ("media", "supports", "layer", "container"):
                    i = block(j + 1)
                else:
                    i = _skip_block(t, j + 1)
                continue
            k = _skip_block(t, j + 1)
            rules.append(CssRule(_split_top_commas(prelude), len(raw[start:k].encode("utf-8")), line_of(start), start))
            i = k
        return n

    block(0)
    return rules, imports


_FUNC_PSEUDO = re.compile(r":(?:not|is|where|has|matches)\([^()]*\)")


def selector_classes(sel: str) -> list[str]:
    """一个选择器里"必须出现才可能命中"的类名。:not()/:is()/:where()/:has() 里的不算(它们不要求元素带这个类)。"""
    s = re.sub(r"\[[^\]]*\]", "[]", sel)
    s = re.sub(r"""(['"]).*?\1""", "", s)
    prev = None
    while prev != s:
        prev, s = s, _FUNC_PSEUDO.sub("", s)
    return re.findall(r"\.(-?[A-Za-z_][\w-]*)", s)


def selector_scopes(sel: str) -> list[str]:
    return re.findall(r"""\[data-page\s*=\s*["']?([\w-]+)["']?\s*\]""", sel)


# ---------------------------------------------------------------- 整站模型
def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[A-Za-z0-9_-]+", text))


class Usage:
    """一组 html/js 里"出现过的类名词"和"疑似运行时拼接的前后缀"。"""

    def __init__(self):
        self.tokens: set[str] = set()
        self.prefixes: set[str] = set()
        self.suffixes: set[str] = set()

    def add_html(self, values):
        for v in values:
            self.tokens |= _tokens(v)

    def add_strings(self, strings):
        for _kind, text, interp in strings:
            toks = re.findall(r"[A-Za-z0-9_-]+", text)
            self.tokens.update(toks)
            for k, tok in enumerate(toks):
                last = k == len(toks) - 1
                if (tok[-1] in "-_" and len(tok) >= 3) or (interp and last and len(tok) >= 3 and text.endswith(tok)):
                    self.prefixes.add(tok)
                if tok[0] in "-_" and len(tok) >= 4:
                    self.suffixes.add(tok)

    def found(self, name: str) -> bool:
        return name in self.tokens

    def maybe_dynamic(self, name: str) -> bool:
        return any(name.startswith(p) and name != p for p in self.prefixes) or any(name.endswith(s) and name != s for s in self.suffixes)


def gz_size(data: bytes) -> int:
    return len(data) if len(data) <= GZIP_MIN else len(gzip.compress(data, 5, mtime=0))


class Site:
    def __init__(self, ui_root: Path):
        self.root = ui_root.resolve()
        self._mods: dict[Path, Module] = {}
        self._css: dict[Path, tuple] = {}
        self._bytes: dict[Path, bytes] = {}
        self.warnings: list[str] = []

    # --- 基础
    def rel(self, p: Path) -> str:
        return PurePosixPath(p.resolve().relative_to(self.root)).as_posix()

    def data(self, p: Path) -> bytes:
        if p not in self._bytes:
            self._bytes[p] = p.read_bytes()
        return self._bytes[p]

    def text(self, p: Path) -> str:
        return self.data(p).decode("utf-8", errors="replace")

    def asset(self, p: Path) -> dict:
        d = self.data(p)
        return {"path": self.rel(p), "bytes": len(d), "gzip": gz_size(d)}

    def resolve(self, base_dir: Path, spec: str, owner: str = "") -> Path | None:
        spec = spec.split("?", 1)[0].split("#", 1)[0]
        if re.match(r"^(?:[a-z]+:|//)", spec):
            return None                                                   # 外链 / data: 不属于本站文件
        if spec.startswith("/ui/"):
            cand = self.root / spec[len("/ui/"):]
        elif spec.startswith("/"):
            return None
        else:
            cand = base_dir / spec
        cand = Path(str(cand).replace("\\", "/"))
        try:
            cand = cand.resolve()
            cand.relative_to(self.root)
        except (OSError, ValueError):
            self.warnings.append(f"{owner}: 引用 {spec} 跑出了 ui 目录")
            return None
        if not cand.is_file():
            self.warnings.append(f"{owner}: 引用的文件不存在 {spec}")
            return None
        return cand

    def module(self, p: Path) -> Module:
        if p not in self._mods:
            m = Module(p, self.text(p))
            if not m.ok:
                self.warnings.append(f"{self.rel(p)}: JS 扫描器没有干净结束(字符串/注释/模板可能没配对),import 解析可能不全")
            self._mods[p] = m
        return self._mods[p]

    def css(self, p: Path):
        if p not in self._css:
            self._css[p] = parse_css(self.text(p))
        return self._css[p]

    # --- 页面
    def js_closure(self, entries, data_page: str, owner: str):
        """从入口出发的 JS 闭包。返回 (常驻模块有序列表, 按需加载模块有序列表)。"""
        core: "OrderedDict[Path, None]" = OrderedDict()
        todo = list(entries)
        lazy_targets: list[Path] = []
        while todo:
            p = todo.pop(0)
            if p in core:
                continue
            core[p] = None
            m = self.module(p)
            for spec in m.static:
                q = self.resolve(p.parent, spec, self.rel(p))
                if q:
                    todo.append(q)
            for spec in m.template:                                       # import(`../pages/${page}.js`)
                q = self.resolve(p.parent, re.sub(r"\$\{[^}]*\}", data_page, spec), self.rel(p))
                if q:
                    todo.append(q)
            for spec in m.lazy:
                q = self.resolve(p.parent, spec, self.rel(p))
                if q:
                    lazy_targets.append(q)
        lazy: "OrderedDict[Path, None]" = OrderedDict()
        todo = [q for q in lazy_targets if q not in core]
        while todo:
            p = todo.pop(0)
            if p in core or p in lazy:
                continue
            lazy[p] = None
            m = self.module(p)
            for spec in m.static + m.lazy:
                q = self.resolve(p.parent, spec, self.rel(p))
                if q:
                    todo.append(q)
        return list(core), list(lazy)

    def css_closure(self, links, base_dir: Path, owner: str):
        seen: "OrderedDict[Path, None]" = OrderedDict()
        todo = [q for q in (self.resolve(base_dir, h, owner) for h in links) if q]
        while todo:
            p = todo.pop(0)
            if p in seen:
                continue
            seen[p] = None
            for spec in self.css(p)[1]:
                q = self.resolve(p.parent, spec, self.rel(p))
                if q:
                    todo.append(q)
        return list(seen)

    def page(self, html_path: Path) -> dict:
        h = _Html()
        h.feed(self.text(html_path))
        name = html_path.name
        entries = [q for q in (self.resolve(html_path.parent, s, name) for s in h.scripts) if q and q.suffix in (".js", ".mjs")]
        core, lazy = self.js_closure(entries, h.data_page, name)
        if h.data_page and not (self.root / "pages" / f"{h.data_page}.js").is_file():
            self.warnings.append(f"{name}: data-page=\"{h.data_page}\" 没有对应的 pages/{h.data_page}.js")
        css = self.css_closure(h.css, html_path.parent, name)
        # 其他会请求的文件:图标精灵(字符串/属性里写了 .svg)和语言词典(new URL('../i18n/${lang}.json', import.meta.url))
        extras: "OrderedDict[Path, str]" = OrderedDict()
        svg_specs = [v for v in h.values if ".svg" in v]
        for m in core:
            svg_specs += [t for _k, t, _i in self.module(m).strings if ".svg" in t]
        for spec in svg_specs:
            q = self.resolve(html_path.parent, spec, name) if re.search(r"\.svg(?:#|$)", spec) else None
            if q:
                extras.setdefault(q, "常驻")
        for m in core:
            for spec in self.module(m).urls:
                if "${" in spec:
                    for k, lang in enumerate(("zh", "en")):               # 默认中文必载,英文只在切换语言时才拉
                        q = self.resolve(m.parent, re.sub(r"\$\{[^}]*\}", lang, spec), self.rel(m))
                        if q:
                            extras.setdefault(q, "常驻" if k == 0 else "按需")
        use = Usage()
        use.add_html(h.values)
        for m in list(core) + list(lazy):
            use.add_strings(self.module(m).strings)
        return {
            "page": name, "data_page": h.data_page, "html": self.asset(html_path),
            "js": [self.asset(p) for p in core], "lazy_js": [self.asset(p) for p in lazy],
            "css": [self.asset(p) for p in css],
            "extras": [dict(self.asset(p), when=w) for p, w in extras.items()],
            "_css_paths": css, "_usage": use,
        }

    # --- 整站
    def code_files(self) -> list[Path]:
        return sorted(p for p in self.root.rglob("*") if p.is_file() and p.suffix.lower() in CODE_EXT and not BACKUP_RE.search(p.name))

    def backup_files(self) -> list[Path]:
        return sorted(p for p in self.root.rglob("*") if p.is_file() and BACKUP_RE.search(p.name))


# ---------------------------------------------------------------- 汇总
def _rule_dead(rule: CssRule, data_page: str, usage: Usage, all_pages: set[str], strict: bool) -> bool:
    """这条规则在某一页(或全站)是否永远用不上。每个逗号分隔的选择器都用不上才算死。"""
    for sel in rule.selectors:
        scopes = selector_scopes(sel)
        if scopes:
            if data_page and data_page not in scopes:
                continue                                                  # 限定了别的页面:这个选择器在本页是死的
            if not data_page and not (set(scopes) & all_pages):
                continue                                                  # 限定的页面整站都没有
        classes = selector_classes(sel)
        if any(not usage.found(c) and (strict or not usage.maybe_dynamic(c)) for c in classes):
            continue
        return False
    return True


def build_report(ui_root: Path, repo_root: Path, top: int = 50, include_vendor: bool = False) -> dict:
    site = Site(ui_root)
    htmls = sorted((site.root / "w").glob("*.html"))
    pages = [site.page(h) for h in htmls]
    n_pages = len(pages)
    all_pages = {p["data_page"] for p in pages if p["data_page"]}

    # 1. 每页汇总
    rows = []
    for p in pages:
        js_b, css_b = sum(a["bytes"] for a in p["js"]), sum(a["bytes"] for a in p["css"])
        js_g, css_g = sum(a["gzip"] for a in p["js"]), sum(a["gzip"] for a in p["css"])
        ex = [a for a in p["extras"] if a["when"] == "常驻"]
        rows.append({
            "page": p["page"], "data_page": p["data_page"],
            "html_bytes": p["html"]["bytes"],
            "js_files": len(p["js"]), "js_bytes": js_b, "js_gzip": js_g,
            "css_files": len(p["css"]), "css_bytes": css_b, "css_gzip": css_g,
            "total_bytes": js_b + css_b, "total_gzip": js_g + css_g,
            "lazy_js_bytes": sum(a["bytes"] for a in p["lazy_js"]), "lazy_js": [a["path"] for a in p["lazy_js"]],
            "extra_bytes": sum(a["bytes"] for a in ex), "extra_gzip": sum(a["gzip"] for a in ex),
            "extras": [a["path"] + ("(按需)" if a["when"] == "按需" else "") for a in p["extras"]],
            "requests": 1 + len(p["js"]) + len(p["css"]) + len(ex),
            "js": [a["path"] for a in p["js"]], "css": [a["path"] for a in p["css"]],
        })

    # 2. 共用文件
    loaded: dict[str, dict] = {}
    for p in pages:
        for kind in ("js", "css"):
            for a in p[kind]:
                e = loaded.setdefault(a["path"], dict(a, kind=kind, pages=[]))
                e["pages"].append(p["page"])
    shared = []
    for e in loaded.values():
        k = len(e["pages"])
        shared.append({"path": e["path"], "kind": e["kind"], "bytes": e["bytes"], "gzip": e["gzip"], "pages": k,
                       "every_page": k == n_pages, "repeat_bytes": e["bytes"] * (k - 1),
                       "total_if_uncached": e["bytes"] * k})
    shared.sort(key=lambda r: (-r["total_if_uncached"], r["path"]))
    common = [r for r in shared if r["every_page"]]
    by_hash: dict[str, list[str]] = defaultdict(list)
    for p in site.code_files():
        by_hash[hashlib.sha256(site.data(p)).hexdigest()].append(site.rel(p))
    duplicates = [{"bytes": len(site.data(site.root / v[0])), "files": v} for v in by_hash.values() if len(v) > 1]

    # 3. 没人引用的文件
    referenced = set(loaded)
    for p in pages:
        referenced |= {a["path"] for a in p["lazy_js"]}
    importers: dict[str, list[str]] = defaultdict(list)
    for f in site.code_files():
        if f.suffix == ".css":
            for spec in site.css(f)[1]:
                q = site.resolve(f.parent, spec, site.rel(f))
                if q:
                    importers[site.rel(q)].append(site.rel(f))
        else:
            m = site.module(f)
            for spec in m.static + m.lazy:
                q = site.resolve(f.parent, spec, site.rel(f))
                if q:
                    importers[site.rel(q)].append(site.rel(f))
    mention_files = _mention_corpus(repo_root, site.root)
    unreferenced = []
    for f in site.code_files() + site.backup_files():
        r = site.rel(f)
        backup = bool(BACKUP_RE.search(f.name))
        if not backup and r in referenced:
            continue
        mentions = []
        self_label = _label(f, repo_root)
        for mp, text in mention_files:
            cnt = text.count(f.name)
            if cnt and mp != self_label:                                  # 文件自己提到自己不算
                mentions.append({"file": mp, "count": cnt})
        code_mentions = [m for m in mentions if not m["file"].endswith(".md")]
        served = f.suffix.lower() in SERVED_EXT
        unreferenced.append({
            "path": r, "bytes": len(site.data(f)), "backup": backup,
            "imported_by": sorted(set(importers.get(r, []))),
            "served": served, "mentions": mentions, "code_mentions": len(code_mentions),
        })
    for u in unreferenced:      # 只被"同样没人引用的文件"引用,等于孤儿链
        u["orphan_chain"] = bool(u["imported_by"]) and all(i in {x["path"] for x in unreferenced} for i in u["imported_by"])

    # 4. 类名
    corpus = Usage()
    for h in htmls:
        hp = _Html()
        hp.feed(site.text(h))
        corpus.add_html(hp.values)
    idx = site.root / "index.html"
    if idx.is_file():
        hp = _Html(); hp.feed(site.text(idx)); corpus.add_html(hp.values)
    for f in site.code_files():
        if f.suffix == ".css":
            continue
        if "vendor" in f.relative_to(site.root).parts and not include_vendor:
            continue
        corpus.add_strings(site.module(f).strings)
    loaded_css = [site.root / r for r in loaded if loaded[r]["kind"] == "css"]
    cls_info: dict[str, dict] = {}
    site_dead_bytes, site_rule_bytes = 0, 0
    for f in sorted(loaded_css):
        rules, _ = site.css(f)
        for rule in rules:
            site_rule_bytes += rule.bytes
            if _rule_dead(rule, "", corpus, all_pages, strict=False):
                site_dead_bytes += rule.bytes
            for sel in rule.selectors:
                for c in selector_classes(sel):
                    e = cls_info.setdefault(c, {"class": c, "file": site.rel(f), "line": rule.line, "rule_bytes": 0, "seen": set()})
                    if (site.rel(f), rule.offset) not in e["seen"]:
                        e["seen"].add((site.rel(f), rule.offset)); e["rule_bytes"] += rule.bytes
    missing = []
    for c, e in cls_info.items():
        if not corpus.found(c):
            missing.append({"class": c, "file": e["file"], "line": e["line"], "rule_bytes": e["rule_bytes"],
                            "maybe_dynamic": corpus.maybe_dynamic(c)})
    missing.sort(key=lambda r: (r["maybe_dynamic"], -r["rule_bytes"], r["file"], r["line"], r["class"]))

    # 5. 每页 CSS 死规则估算
    per_page_dead = []
    for p in pages:
        dead_b, total_b, dead_n, total_n = 0, 0, 0, 0
        per_file = []
        for f in p["_css_paths"]:
            fb, fd = 0, 0
            for rule in site.css(f)[0]:
                fb += rule.bytes
                if _rule_dead(rule, p["data_page"], p["_usage"], all_pages, strict=False):
                    fd += rule.bytes; dead_n += 1
                total_n += 1
            dead_b += fd; total_b += fb
            per_file.append({"path": site.rel(f), "rule_bytes": fb, "dead_bytes": fd})
        per_page_dead.append({"page": p["page"], "rule_bytes": total_b, "dead_bytes": dead_b, "rules": total_n, "dead_rules": dead_n,
                              "files": per_file})

    for p in pages:
        p.pop("_css_paths"); p.pop("_usage")
    return {
        "ui_root": str(site.root), "pages": n_pages, "rows": rows, "shared": shared,
        "common_bytes": sum(r["bytes"] for r in common), "common_files": len(common),
        "duplicates": duplicates, "unreferenced": unreferenced,
        "missing_classes_total": len(missing), "missing_classes": missing[:top], "missing_all": missing,
        "missing_certain": sum(1 for m in missing if not m["maybe_dynamic"]),
        "css_site_rule_bytes": site_rule_bytes, "css_site_dead_bytes": site_dead_bytes,
        "per_page_dead": per_page_dead,
        "code_files": len(site.code_files()), "code_bytes": sum(len(site.data(p)) for p in site.code_files()),
        "warnings": sorted(set(site.warnings)),
    }


def _mention_corpus(repo_root: Path, ui_root: Path):
    """仓库里"可能提到文件名"的文本:python / 测试 / 文档 / 旧版界面 / ui 自带说明。不读 data、handoff、.secret、虚拟环境。"""
    out = []
    me = Path(__file__).resolve()
    cands = [repo_root / "app.py", repo_root / "README.md", repo_root / "项目总览.md"]
    for sub in ("src", "scripts", "tests", "docs"):
        d = repo_root / sub
        if d.is_dir():
            cands += [p for p in d.rglob("*") if p.is_file() and p.suffix in (".py", ".js", ".mjs", ".md", ".html", ".json") and "__pycache__" not in p.parts
                      and "public_site" not in p.parts]
    dash = repo_root / "dashboard"
    if dash.is_dir():
        cands += [p for p in dash.glob("*") if p.is_file() and p.suffix in (".js", ".html", ".css", ".md")]
    cands += [p for p in ui_root.rglob("*.md")]
    cands += [p for p in ui_root.rglob("*.html")] + [p for p in ui_root.rglob("*.js") if not BACKUP_RE.search(p.name)]
    seen = set()
    for p in cands:
        rp = p.resolve()
        if rp in seen or rp == me or p.name.startswith("test_ui_asset_report") or not p.is_file():
            continue
        seen.add(rp)
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        out.append((_label(p, repo_root), text))
    return out


def _label(p: Path, repo_root: Path) -> str:
    """文件相对仓库根的路径(不在仓库里就用绝对路径),用来在报告里显示、也用来排除"自己提到自己"。"""
    try:
        return PurePosixPath(p.resolve().relative_to(repo_root.resolve())).as_posix()
    except ValueError:
        return p.as_posix()


# ---------------------------------------------------------------- 输出
def _kb(n: int) -> str:
    return f"{n / 1024:.1f}"


def render_md(rep: dict, top: int) -> str:
    L: list[str] = []
    a = L.append
    a("# 静态资源体积体检(只读)\n")
    a(f"- 统计范围:`{rep['ui_root']}`,{rep['pages']} 个页面(w/*.html);全站 css/js 共 {rep['code_files']} 个文件、{_kb(rep['code_bytes'])} KB(不含备份文件)。")
    a("- 字节是磁盘上的原始大小;gzip 列按 app.py 的规则估算(大于 1KB 才压缩,等级 5)。KB = 1024 字节。")
    a("- “JS”= 页面 `<script>` 加上 import 链,加上按 `data-page` 动态加载的页面模块;“按需”= 写成字面量的动态 import(如 `?debug=1` 才加载的模块),不计入常驻。\n")

    a("## 1. 每页加载多少 JS / CSS\n")
    a("| 页面 | JS 文件 | JS KB | CSS 文件 | CSS KB | JS+CSS KB | gzip 估算 KB | 请求数 | 按需 JS KB | 另有(图标/中文词典) KB |")
    a("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for r in sorted(rep["rows"], key=lambda r: -r["total_bytes"]):
        a(f"| {r['page']} | {r['js_files']} | {_kb(r['js_bytes'])} | {r['css_files']} | {_kb(r['css_bytes'])} | {_kb(r['total_bytes'])} | "
          f"{_kb(r['total_gzip'])} | {r['requests']} | {_kb(r['lazy_js_bytes'])} | {_kb(r['extra_bytes'])}(gzip {_kb(r['extra_gzip'])}) |")
    a("\n“请求数”= html 1 个 + 常驻 js + css + 常驻的图标/词典文件。app.py 对 /ui/ 的响应是 `no-cache` + ETag,所以每次打开页面,每个文件都要向服务器问一次(命中时返回 304,不重传正文),请求数比字节数更影响打开速度。\n")

    a("## 2. 多个页面重复加载的大文件\n")
    a(f"没有打包工具,每个页面各自引用同一批文件。**每个页面都会加载**的文件共 {rep['common_files']} 个、{_kb(rep['common_bytes'])} KB。按“体积 × 页数”排前 15:\n")
    a("| 文件 | 类型 | KB | 页数 | 页数占比 | 每页都加载 |")
    a("|---|---|---:|---:|---:|---|")
    for r in [r for r in rep["shared"] if r["pages"] >= 2][:15]:
        a(f"| {r['path']} | {r['kind']} | {_kb(r['bytes'])} | {r['pages']} | {r['pages']}/{rep['pages']} | {'是' if r['every_page'] else '否'} |")
    big_partial = [r for r in rep["shared"] if r["pages"] >= 2 and not r["every_page"] and r["bytes"] >= 4096]
    if big_partial:
        a("\n只被**部分**页面共用、体积 ≥ 4KB 的文件(这类文件里的内容只对其中一部分页面有用,最值得看是不是该拆):\n")
        a("| 文件 | KB | 页数 | 使用它的页面 |")
        a("|---|---:|---:|---|")
        pages_of = {e["path"]: [r["page"] for r in rep["rows"] if e["path"] in r["js"] + r["css"]] for e in big_partial}
        for r in big_partial:
            a(f"| {r['path']} | {_kb(r['bytes'])} | {r['pages']} | {', '.join(pages_of[r['path']])} |")
    a("\n内容完全相同的文件(按 SHA-256):")
    if rep["duplicates"]:
        for d in rep["duplicates"]:
            a(f"- {_kb(d['bytes'])} KB × {len(d['files'])}:{' = '.join('`' + f + '`' for f in d['files'])}")
    else:
        a("- 无")

    a("\n## 3. 没有任何页面引用的 css / js(只列出,不删)\n")
    un = rep["unreferenced"]
    if un:
        a("| 文件 | 字节 | 类型 | 谁 import 它 | 仓库里别处提到(代码/文档) | 服务端能否提供 |")
        a("|---|---:|---|---|---|---|")
        for u in un:
            kind = "备份残留" if u["backup"] else "无人引用"
            who = ", ".join(u["imported_by"]) or "-"
            if u["orphan_chain"]:
                who += "(上游也没人引用)"
            ment = ", ".join(f"{m['file']}×{m['count']}" for m in u["mentions"][:4]) or "-"
            a(f"| {u['path']} | {u['bytes']} | {kind} | {who} | {ment} | {'能' if u['served'] else '否(后缀不在白名单,访问会 404)'} |")
    else:
        a("无。")

    a(f"\n## 4. CSS 里定义、但所有 html/js 字符串里都搜不到的类名\n")
    a(f"被加载的 CSS 里共定义类名 {rep['missing_classes_total'] + 0} 个搜不到(其中 {rep['missing_certain']} 个看不出是运行时拼接的,其余在 js 里有以同样前/后缀结尾的字符串,可能是动态拼的)。"
      f"下面按“包含它的规则字节数”排前 {top}(看不出动态拼接的排前面),完整清单见 --classes-csv:\n")
    a("| # | 类名 | 所在文件:行 | 规则字节 | 可能动态拼接 |")
    a("|---:|---|---|---:|---|")
    for i, m in enumerate(rep["missing_classes"], 1):
        a(f"| {i} | `.{m['class']}` | {m['file']}:{m['line']} | {m['rule_bytes']} | {'是' if m['maybe_dynamic'] else ''} |")
    a(f"\n整站范围的“死规则”估算:被加载的 CSS 规则共 {_kb(rep['css_site_rule_bytes'])} KB,其中选择器里有类名全站搜不到(或限定了不存在的 data-page)的规则约 {_kb(rep['css_site_dead_bytes'])} KB(估算,看不出动态拼接的类名才算)。\n")

    a("## 5. 每页 CSS 里这一页永远用不上的规则(估算)\n")
    a("规则判死条件:选择器里有类名在**这一页**的 html 和它加载的 js 字符串里都没出现,或选择器限定了别的 `[data-page=…]`。字节是规则文本的 UTF-8 长度(不含规则间的注释)。\n")
    a("| 页面 | CSS 规则 KB | 这页用不上 KB | 占比 | 规则数(用不上/全部) |")
    a("|---|---:|---:|---:|---|")
    for r in sorted(rep["per_page_dead"], key=lambda r: -r["dead_bytes"]):
        pct = 100 * r["dead_bytes"] / r["rule_bytes"] if r["rule_bytes"] else 0
        a(f"| {r['page']} | {_kb(r['rule_bytes'])} | {_kb(r['dead_bytes'])} | {pct:.0f}% | {r['dead_rules']}/{r['rules']} |")
    a("\n每个 CSS 文件在不同页面的“用不上”程度(只列各文件最差的一页和最好的一页):\n")
    by_file: dict[str, list] = defaultdict(list)
    for r in rep["per_page_dead"]:
        for f in r["files"]:
            by_file[f["path"]].append((f["dead_bytes"], r["page"], f["rule_bytes"]))
    a("| CSS 文件 | 规则 KB | 加载页数 | 用不上 KB(最少 / 最多) | 最多的页面 |")
    a("|---|---:|---:|---|---|")
    for path, v in sorted(by_file.items(), key=lambda kv: -max(x[0] for x in kv[1])):
        lo, hi = min(v), max(v)
        a(f"| {path} | {_kb(hi[2])} | {len(v)} | {_kb(lo[0])} / {_kb(hi[0])} | {hi[1]} |")

    a("\n## 6. 脚本自检的警告\n")
    if rep["warnings"]:
        L.extend(f"- {w}" for w in rep["warnings"])
    else:
        a("无(所有 import / @import / script / link 都能解析到存在的文件,所有 JS 都扫描干净)。")
    return "\n".join(L) + "\n"


def _clean_for_json(rep: dict) -> dict:
    return {k: v for k, v in rep.items() if k != "missing_all"}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="静态资源体积体检(只读)")
    ap.add_argument("--ui-root", default=str(DEFAULT_UI), help="新界面目录,默认 dashboard/ui")
    ap.add_argument("--repo-root", default=str(ROOT), help="仓库根目录(只用来搜“别处是否提到某文件名”)")
    ap.add_argument("--top", type=int, default=50, help="类名清单列前 N 个")
    ap.add_argument("--include-vendor", action="store_true", help="类名搜索范围也包含 vendor/ 下的第三方库")
    ap.add_argument("--out", help="把 markdown 报告写到这个文件(默认打印到终端)")
    ap.add_argument("--json-out", help="把完整结果写成 json")
    ap.add_argument("--classes-csv", help="把搜不到的类名完整清单写成 csv")
    a = ap.parse_args(argv)
    ui = Path(a.ui_root).resolve()
    if not (ui / "w").is_dir():
        print(f"找不到 {ui}/w", file=sys.stderr)
        return 2
    for target in (a.out, a.json_out, a.classes_csv):
        if target:
            try:
                Path(target).resolve().relative_to(ui)
            except ValueError:
                continue
            print(f"拒绝写入 ui 目录里面的文件:{target}(本脚本只读 ui)", file=sys.stderr)
            return 2
    rep = build_report(ui, Path(a.repo_root).resolve(), top=a.top, include_vendor=a.include_vendor)
    md = render_md(rep, a.top)
    if a.out:
        Path(a.out).write_text(md, encoding="utf-8")
    else:
        sys.stdout.write(md)
    if a.json_out:
        Path(a.json_out).write_text(json.dumps(_clean_for_json(rep), ensure_ascii=False, indent=1), encoding="utf-8")
    if a.classes_csv:
        lines = ["class,file,line,rule_bytes,maybe_dynamic"]
        lines += [f"{m['class']},{m['file']},{m['line']},{m['rule_bytes']},{int(m['maybe_dynamic'])}" for m in rep["missing_all"]]
        Path(a.classes_csv).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
