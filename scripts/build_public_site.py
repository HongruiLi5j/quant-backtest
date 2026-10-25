#!/usr/bin/env python3
"""公开站导出 + 检查（T020 步骤 1，v2：按 Codex 审核 F1–F4 加固）。**只导出、只检查，不上传、不联网、不读 .secret。**

和旧脚本 ~/Documents/AQ-tools/build_site.py 的区别：
  - 路径白名单（scripts/public_allowlist.json）：来源与目标路径都严格校验（相对、NFC、无 ./.. 段、无隐藏段、无大小写/Unicode 等价重复）；
  - 每次构建到新目录 build/<build_id>/site/，失败的留作证据（<id>.FAILED），**绝不删除已有产物**；
  - 导出后对"所有文件"做检查：不在白名单、符号链接、未知后缀、无法解码、红线词、合规词、缺失引用、字典/mock/SVG 内容与钉死的哈希不符；
  - 区分"草稿可检查(ok)"和"可以发布(release_ready)"：联系邮箱缺失/占位、数据口径矛盾的措辞会阻止发布校验与 mark-published；
  - manifest（相对路径 + 大小 + sha256）自洽校验，并和"上次已发布"清单对比；串行锁；
  - 没有 --deploy。上传由所有者在终端手动做：先 --verify，上传并核对后再 --mark-published。

用法（项目根目录，用 .venv/bin/python）：
  .venv/bin/python scripts/build_public_site.py                         构建 + 检查，输出到 build/
  .venv/bin/python scripts/build_public_site.py --verify <id> [--expect-manifest-sha256 <摘要>]   发布前复核快照
  .venv/bin/python scripts/build_public_site.py --mark-published <id> [--expect-manifest-sha256 <摘要>]
  .venv/bin/python scripts/build_public_site.py --discover              按公开页面算引用闭包，对比白名单
  .venv/bin/python scripts/build_public_site.py --pin-report            打印当前应钉死的内容哈希（字典/mock/SVG），供人工审阅后写入白名单
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import sys
import tempfile
import time
import unicodedata
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent
SRC_DEFAULT = ROOT / "dashboard" / "mockups"
TEMPLATE_DEFAULT = ROOT / "scripts" / "public_site"
ALLOWLIST_DEFAULT = ROOT / "scripts" / "public_allowlist.json"
BUILD_ROOT_DEFAULT = ROOT / "build"
TRASH_DEFAULT = ROOT / "_to_delete" / "builds"
BRAND = "AQ Quant"
KEEP_DEFAULT = 5
PROJECT = "accessiblequant"
MAX_MANIFEST_BYTES = 8_000_000

TEXT_SUFFIX = {".html", ".js", ".css", ".json", ".svg", ".txt"}
BINARY_SUFFIX = {".png", ".ico", ".webp", ".woff2", ".jpg"}
SCAN_SUFFIX = TEXT_SUFFIX
ID_RE = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{8}(-\d+)?$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")

# 导出里不允许出现的内部痕迹（和旧脚本一致）
REDLINE = re.compile(r"User-Agent|Cookie|Referer|add_header|bestip|sealed|test2|trials\.jsonl|runs\.jsonl|"
                     r"registry/|Documents/Projects|\.secret|127\.0\.0\.1|localhost|/Users/|lihongrui|Quant Backtest|AQ 量化")
# 合规词：对外页面不能出现的承诺性措辞（《金融产品网络营销管理办法》第十条等的辅助检查，不保证覆盖全部）
COMPLIANCE_WORDS = ("低风险", "高收益", "稳赚", "保证收益", "必涨", "保本")
# 口径待改（所有者 2026-10-24 确定"现在全是合成演示数据"）：出现就是"发布阻断项"
COPY_BLOCK = ("Data sources: SINA", "derived results exported overnight", "夜间导出的衍生结果")
CONTACT_PLACEHOLDER = re.compile(r"example\.(invalid|com|org)|\.invalid\b|placeholder|待填|TODO", re.I)
# 公开数据/字典里不许出现 "/api/" 形式的路径（不区分大小写，含 /api/123、/api?x、结尾的 /api，也含键名和第三方文档里的 /api/ 链接——最保守地一律拒绝）。
# 在【解析后的 JSON 字符串】上检查，所以 \/api\/ 和 \u002fapi\u002f 这类转义写法也绕不过。构建时会先删掉 mock 的 _meta.schema、覆盖三条词条，之后仍出现就是错误
INTERNAL_API = re.compile(r"/api(?:/|$|[?#])", re.I)
# SVG 里不允许的主动内容
SVG_ACTIVE = re.compile(r"<\s*script|<\s*foreignObject|\bon\w+\s*=|javascript:|(?:xlink:)?href\s*=\s*[\"']\s*(?:https?:)?//", re.I)

ZH = {
    "story.hero.badge": "在线演示 · 示例数据 · 未连接券商",
    "landing.badge": "在线演示 · 示例数据 · 不连接券商",
    "landing.footer.note": "演示站点 · 示例数据 · 不构成投资建议 · 作者 李泓锐",
    "state.error.body": "数据暂时没加载出来，刷新页面重试即可。",
    "s6.state.error_body": "数据暂时没加载出来，刷新页面重试即可。",
    "t5.preview_hint": "", "idx.lead": "",
    "land.cta.signup": "查看方案", "lock.signup": "暂未开放注册", "plan.cta.signup": "暂未开放注册",
    "auth.signup": "注册（暂未开放）", "auth.submit_up": "暂未开放注册",
    "lock.request": "暂未开放", "plan.cta.request": "暂未开放",
    "t7.link.auth": "登录", "idx.page.auth": "登录", "gate.signup": "目前暂未开放注册",
    "auth.demo": "演示：目前暂未开放注册和登录，表单不会发送。",
    "t7.auth.lead": "目前暂未开放注册，这一页只是演示。将来只需要用户名和密码，不收邮箱、不接第三方登录。",
    "plan.lead": "目前暂未开放注册，下面是将来的三档权益预览。Pro 测试期免费，由所有者手动开通。",
    # 以下三条原文写着具体的内部接口路径（/api/...），公开站不需要，改成泛称
    "cmp.topnav.p.identity": "后端返回的身份信息；决定哪些项渲染",
    "market.wl.added_toast": "演示：{c} 没有真的加入；真实版本由后端判断配额。",
    "translator.unknown": "演示版只认识示例里的几种写法；真实版本会调用翻译服务。",
}
EN = {
    "story.hero.badge": "Live demo · Sample data · No broker connected",
    "landing.badge": "Live demo · Sample data · No broker access",
    "landing.footer.note": "Demo site · sample data · not investment advice · by Hongrui Li",
    "t5.preview_hint": "", "idx.lead": "",
    "land.cta.signup": "See plans", "lock.signup": "Sign-ups closed", "plan.cta.signup": "Sign-ups closed",
    "auth.signup": "Sign up (closed)", "auth.submit_up": "Sign-ups closed",
    "lock.request": "Not open yet", "plan.cta.request": "Not open yet",
    "t7.link.auth": "Sign in", "idx.page.auth": "Sign in", "gate.signup": "Registration is closed for now",
    "auth.demo": "Demo: sign-up and sign-in are closed for now; nothing is sent.",
    "t7.auth.lead": "Registration is closed for now; this page is a demo. Later it will only need a username and password — no email, no third-party login.",
    "plan.lead": "Registration is closed for now. Below is a preview of the three planned tiers. Pro will be free during beta and turned on by the owner.",
    "cmp.topnav.p.identity": "The identity returned by the backend; decides which items render",
    "market.wl.added_toast": "Demo: {c} was not really added. In the real version the server enforces the quota.",
    "translator.unknown": "The demo understands only the example phrasings; the real version calls the translation service.",
}


class BuildError(Exception):
    """构建/检查中的致命错误（给人看的信息）。"""


# ---------------------------------------------------------------- 路径与哈希

def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def canon(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


_WIN_RESERVED = re.compile(r"(?i)con|prn|aux|nul|com[1-9]|lpt[1-9]")


def validate_rel(rel, what: str) -> str:
    """相对 POSIX 路径的严格校验（来源和目标都用它）。返回原值，不合格就抛 BuildError。"""
    if not isinstance(rel, str) or not rel:
        raise BuildError(f"{what}：路径必须是非空字符串（{rel!r}）")
    if "\\" in rel or any(ord(c) < 32 or ord(c) == 127 for c in rel):
        raise BuildError(f"{what}：路径含反斜杠或控制字符：{rel!r}")
    if rel != unicodedata.normalize("NFC", rel):
        raise BuildError(f"{what}：路径必须是 NFC 规范化形式：{rel!r}")
    if rel.startswith("/") or rel.endswith("/") or PurePosixPath(rel).as_posix() != rel:
        raise BuildError(f"{what}：必须是规范的相对路径（不能是绝对路径、不能以 / 结尾、不能有 //）：{rel!r}")
    if len(rel) > 400:
        raise BuildError(f"{what}：路径太长：{rel[:40]!r}…")
    for seg in rel.split("/"):
        if seg in ("", ".", ".."):
            raise BuildError(f"{what}：路径含 ./.. 或空段：{rel!r}")
        if seg.startswith("."):
            raise BuildError(f"{what}：路径含隐藏段：{rel!r}")
        if ":" in seg or seg.endswith((".", " ")) or len(seg) > 120 or _WIN_RESERVED.fullmatch(seg.split(".")[0]):
            raise BuildError(f"{what}：路径含盘符/冒号、结尾点或空格、超长段或 Windows 保留名（最保守地一律拒绝）：{rel!r}")
    return rel


def equiv_key(rel: str) -> str:
    """macOS 默认大小写不敏感且会做 Unicode 规范化：这两种等价的路径视为同一个。"""
    return unicodedata.normalize("NFC", rel).casefold()


def has_symlink_in_path(path: Path, stop: Path) -> bool:
    """path 到 stop（含）之间任何一级是符号链接就返回 True。"""
    p = path
    while True:
        if p.is_symlink():
            return True
        if p == stop or p.parent == p:
            return False
        p = p.parent


def overlaps(a: Path, b: Path) -> bool:
    a, b = Path(a).resolve(), Path(b).resolve()
    return a == b or a in b.parents or b in a.parents


def _claim(dsts: dict, dst: str, who: str) -> None:
    key = equiv_key(dst)
    if key in dsts:
        raise BuildError(f"白名单里目标路径重复或等价（大小写/Unicode）：{dst}（{who} 与 {dsts[key]}）")
    dsts[key] = who


def load_allowlist(path: Path) -> dict:
    try:
        al = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise BuildError(f"读不了白名单 {path}: {e}")
    if not isinstance(al, dict):
        raise BuildError("白名单必须是 JSON 对象")
    for k in ("version", "src_files", "template", "i18n", "required_keys", "binary_sha256", "content_pins"):
        if k not in al:
            raise BuildError(f"白名单缺字段 {k}")
    if al["version"] != 1:
        raise BuildError("白名单版本不对")
    if not isinstance(al["src_files"], list) or not isinstance(al["template"], dict) or not isinstance(al["i18n"], dict):
        raise BuildError("白名单 src_files 必须是列表、template/i18n 必须是对象")
    dsts: dict[str, str] = {}
    for rel in al["src_files"]:
        validate_rel(rel, "src_files")
        _claim(dsts, rel, f"src:{rel}")
    for rel, dst in al["template"].items():
        validate_rel(rel, "template 来源")
        validate_rel(dst, "template 目标")
        _claim(dsts, dst, f"template:{rel}")
    for field in ("binary_sha256", "content_pins"):
        if not isinstance(al[field], dict):
            raise BuildError(f"白名单 {field} 必须是对象")
        for rel, h in al[field].items():
            validate_rel(rel, field)
            if not isinstance(h, str) or not SHA_RE.fullmatch(h):
                raise BuildError(f"{field} 里的哈希格式不对：{rel}")
    for lst in ("drop_prefixes", "drop_keys", "allow_keys"):
        v = al["i18n"].get(lst, [])
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            raise BuildError(f"白名单 i18n.{lst} 必须是字符串列表")
    if not isinstance(al["required_keys"], list):
        raise BuildError("白名单 required_keys 必须是列表")
    return al


def plan_files(al: dict) -> list[tuple[str, str, str]]:
    """返回 [(来源类型 'src'|'template', 来源相对路径, 目标相对路径)]。"""
    out = [("src", rel, rel) for rel in al["src_files"]]
    out += [("template", rel, dst) for rel, dst in al["template"].items()]
    return out


def needs_pin(rel: str) -> bool:
    """字典、mock 数据、SVG：文本内容可变且最容易混进内部东西，必须有钉死的哈希。"""
    return (rel.startswith("shared/i18n-") and rel.endswith(".json")) or rel.startswith("mock-data/") or rel.endswith(".svg")


# ---------------------------------------------------------------- 引用分析

class _RefParser(HTMLParser):
    """按标签/属性语义取引用：<a href>/<area href> 是页面链接（缺失只告警），其余 src/href 等是资源（缺失是错误）。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self.hard: list[str] = []
        self._in_style = False
        self.style_text = ""

    def handle_starttag(self, tag, attrs):
        d = {k: (v or "") for k, v in attrs}
        if tag == "style":
            self._in_style = True
        rel = d.get("rel", "").lower()
        for k in ("src", "href", "poster", "data", "action", "manifest"):
            v = d.get(k)
            if not v:
                continue
            if (tag in ("a", "area") and k == "href") or (tag == "link" and k == "href" and ("canonical" in rel or "alternate" in rel)):
                self.links.append(v)
            else:
                self.hard.append(v)
        for item in d.get("srcset", "").split(","):
            u = item.strip().split(" ")[0]
            if u:
                self.hard.append(u)
        if d.get("style"):
            self.style_text += d["style"] + "\n"

    def handle_endtag(self, tag):
        if tag == "style":
            self._in_style = False

    def handle_data(self, data):
        if self._in_style:
            self.style_text += data


_JS_STATIC = re.compile(r"""(?:\bfrom\s*|\bimport\s*\(\s*|\bimport\s+)["'](\.{1,2}/[^"']+?)["']""")
_NEW_URL = re.compile(r"""new\s+URL\(\s*["']([^"']+)["']\s*,\s*import\.meta\.url""")
_CSS_URL = re.compile(r"""url\(\s*["']?([^"')\s]+)""")
_CSS_IMPORT = re.compile(r"""@import\s+["']([^"']+)["']""")
_LOAD_MOCK = re.compile(r"""loadMock\(\s*["']([\w\-./]+)["']""")
_DATA_PAGE = re.compile(r"""data-page\s*=\s*["']([\w\-]+)["']""")
_SKIP_SCHEMES = ("http:", "https:", "//", "data:", "mailto:", "javascript:", "tel:", "#", "%23", "blob:")


def _strip(ref: str) -> str:
    return ref.split("?")[0].split("#")[0]


def strict_refs(f: Path, text: str) -> tuple[set[str], set[str]]:
    """返回 (资源引用, 页面链接)。资源引用缺失 = 页面会坏；页面链接缺失只告警。"""
    hard: set[str] = set()
    links: set[str] = set()
    suf = f.suffix
    if suf == ".html":
        p = _RefParser()
        p.feed(text)
        p.close()
        hard.update(p.hard)
        links.update(p.links)
        hard.update(m.group(1) for m in _CSS_URL.finditer(p.style_text))
        hard.update(m.group(1) for m in _CSS_IMPORT.finditer(p.style_text))
        m = _DATA_PAGE.search(text)
        if m and ("public.js" in text or "interactions.js" in text):      # 这两个脚本才会按 page-<id>.js 动态加载
            hard.add(f"__page__:{m.group(1)}")
    elif suf == ".js":
        hard.update(m.group(1) for m in _JS_STATIC.finditer(text))
        hard.update(m.group(1) for m in _NEW_URL.finditer(text))
    elif suf == ".css":
        hard.update(m.group(1) for m in _CSS_URL.finditer(text))
        hard.update(m.group(1) for m in _CSS_IMPORT.finditer(text))
    for m in _LOAD_MOCK.finditer(text):
        n = m.group(1)
        hard.add("__mock__:" + (n if n.endswith(".json") else n + ".json"))
    return hard, links


def _resolve(base: Path, ref: str, root: Path, within: Path | None = None) -> Path | None:
    """有限的静态解析（相对引用从引用文件所在目录算，/ 开头从站点根算；不再尝试其它目录兜底）。
    解析结果必须是：根内、(若给了 within)限定目录内、普通文件、且从根到它的路径上没有符号链接。
    不覆盖 <base> 标签、百分号解码、import map、fetch/变量拼接的动态加载。"""
    ref = _strip(ref)
    if not ref:
        return None
    root_r = root.resolve()
    c = (root_r / ref.lstrip("/")) if ref.startswith("/") else (base.parent / ref)
    try:
        cr = c.resolve()
    except OSError:
        return None
    lim = (within or root).resolve()

    def inside(p: Path, r: Path) -> bool:
        return p == r or r in p.parents

    if not (cr.is_file() and inside(cr, root_r) and inside(cr, lim)):
        return None
    if has_symlink_in_path(Path(os.path.normpath(c)), root_r):
        return None
    return cr


def closure(root: Path, entries: list[Path]) -> tuple[set[Path], dict[str, set[str]], dict[str, set[str]]]:
    """从入口页出发算引用闭包。返回 (文件集合, 找不到的资源引用, 找不到的页面链接)。"""
    seen: set[Path] = set()
    todo = list(entries)
    miss_hard: dict[str, set[str]] = {}
    miss_link: dict[str, set[str]] = {}
    root_r = root.resolve()
    while todo:
        f = todo.pop()
        if f in seen:
            continue
        seen.add(f)
        if f.suffix not in {".html", ".js", ".css"}:
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        hard, links = strict_refs(f, text)
        for ref in sorted(hard):
            if ref.startswith(_SKIP_SCHEMES) or ref.endswith("/"):
                continue
            if ref.startswith("__page__:"):                 # 特殊分支也走同一套根内/限定目录/链接检查
                r = _resolve(root_r / "shared" / "_", f"page-{ref[9:]}.js", root_r, within=root_r / "shared")
            elif ref.startswith("__mock__:"):
                r = _resolve(root_r / "mock-data" / "_", ref[9:], root_r, within=root_r / "mock-data")
            else:
                r = _resolve(f, ref, root)
            if r is None:
                miss_hard.setdefault(str(f.relative_to(root_r)), set()).add(ref)
            elif r not in seen:
                todo.append(r)
        for ref in sorted(links):
            if ref.startswith(_SKIP_SCHEMES):
                continue
            r = _resolve(f, ref, root)
            if r is None:
                miss_link.setdefault(str(f.relative_to(root_r)), set()).add(ref)
            elif r not in seen:
                todo.append(r)
    return seen, miss_hard, miss_link


def discover(src: Path, al: dict) -> dict:
    """人工审阅用：按白名单里的页面算引用闭包，对比白名单本身。"""
    src_r = src.resolve()
    entries = [(src_r / rel) for rel in al["src_files"] if rel.endswith(".html")]
    seen, miss_hard, miss_link = closure(src_r, entries)
    seen_rel = {str(p.relative_to(src_r)) for p in seen}
    for lang in ("zh", "en"):           # i18n.js 按 `i18n-${lang}.json` 动态拼接加载，静态分析看不到，按约定算进闭包
        seen_rel.add(f"shared/i18n-{lang}.json")
    return {"in_closure_not_in_allowlist": sorted(seen_rel - set(al["src_files"])),
            "in_allowlist_not_in_closure": sorted(set(al["src_files"]) - seen_rel),
            "unresolved_hard_refs": {k: sorted(v) for k, v in sorted(miss_hard.items())},
            "dead_page_links": {k: sorted(v) for k, v in sorted(miss_link.items())}}


# ---------------------------------------------------------------- 构建

def _sub(path: Path, pairs, regex=False) -> int:
    if not path.is_file():
        return -1
    s = path.read_text(encoding="utf-8")
    n = 0
    for a, b in pairs:
        if regex:
            s, k = re.subn(a, b, s, flags=re.S)
        else:
            k = s.count(a)
            s = s.replace(a, b)
        n += k
    path.write_text(s, encoding="utf-8")
    return n


def copy_allowed(src: Path, template: Path, al: dict, site: Path) -> None:
    """按白名单复制。来源必须是根内普通文件、不是符号链接；目标必须落在 site 内；二进制必须有 sha256 且匹配。"""
    src_r, tpl_r, site_r = src.resolve(), template.resolve(), site.resolve()
    pins = al["binary_sha256"]
    for kind, rel, dst in plan_files(al):
        base, base_r = (src, src_r) if kind == "src" else (template, tpl_r)
        p = base / rel
        if not p.exists():
            raise BuildError(f"白名单里的文件不存在：{kind}:{rel}")
        if has_symlink_in_path(p, base):
            raise BuildError(f"来源路径含符号链接，拒绝：{kind}:{rel}")
        pr = p.resolve()
        if not (pr == base_r or base_r in pr.parents) or not pr.is_file():
            raise BuildError(f"来源不是根内的普通文件：{kind}:{rel}")
        suf = Path(dst).suffix
        if suf in BINARY_SUFFIX:
            want = pins.get(dst)
            if not want:
                raise BuildError(f"二进制资产没有登记 sha256：{dst}")
            if sha256_file(pr) != want:
                raise BuildError(f"二进制资产内容与白名单登记的 sha256 不符：{dst}")
        elif suf not in TEXT_SUFFIX:
            raise BuildError(f"白名单里有未知后缀：{dst}")
        out = site_r / dst
        out_parent = out.parent
        anc = out_parent                                   # 先验证、后创建：最近的已存在祖先必须在 site 内，且中间没有符号链接
        while not anc.exists():
            anc = anc.parent
        anc_r = anc.resolve()
        if not (anc_r == site_r or site_r in anc_r.parents):
            raise BuildError(f"目标路径越出导出目录，拒绝：{dst}")
        if has_symlink_in_path(Path(os.path.normpath(anc)), site_r):
            raise BuildError(f"目标路径含符号链接，拒绝：{dst}")
        out_parent.mkdir(parents=True, exist_ok=True)
        op = out_parent.resolve()
        if not (op == site_r or site_r in op.parents):
            raise BuildError(f"目标路径越出导出目录，拒绝：{dst}")
        if out.exists() or out.is_symlink():
            raise BuildError(f"目标文件已存在（可能是重复目标）：{dst}")
        shutil.copy2(pr, out)


def strip_mock_schema(site: Path) -> int:
    """公开的 mock JSON 不需要"接口说明"：删除 _meta.schema（里面写着内部接口路径、源码文件名、文档名）。
    页面代码不读 _meta（已在源码里确认）；只重写含 schema 的文件，其它文件保持字节不变。返回处理的文件数。"""
    d = site / "mock-data"
    n = 0
    for p in sorted(d.glob("*.json")) if d.is_dir() else []:
        j = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(j, dict) and isinstance(j.get("_meta"), dict) and "schema" in j["_meta"]:
            del j["_meta"]["schema"]
            p.write_text(json.dumps(j, ensure_ascii=False, indent=1), encoding="utf-8")
            n += 1
    return n


def apply_transforms(site: Path, al: dict) -> list[str]:
    """把旧脚本里的改写搬过来（品牌名、注册文案、导航裁剪、字典覆盖），并剔除字典里的内部键。
    返回告警列表（某个改写一次都没命中 = 源文件变了，需要人看一下）。"""
    warns: list[str] = []
    for p in sorted(site.rglob("*.html")):
        if p.name == "index.html" and p.parent == site:
            continue
        pre = "../" * (len(p.relative_to(site).parts) - 1) + "assets/"
        s = p.read_text(encoding="utf-8").replace("Quant Backtest", BRAND)
        s = re.sub(r'<link rel="icon" href="data:[^"]*">',
                   f'<link rel="icon" type="image/png" sizes="32x32" href="{pre}favicon-32.png">\n'
                   f'  <link rel="apple-touch-icon" href="{pre}apple-touch-icon.png">', s)
        s = s.replace("本机运行 · 127.0.0.1 · 不连接券商", ZH["landing.badge"])
        s = s.replace("本机 · 127.0.0.1 · 未连接券商", ZH["story.hero.badge"])
        s = s.replace("可能是本机服务没启动，或数据文件缺失。数据不会丢，重试即可。", ZH["state.error.body"])
        s = s.replace('href="auth.html#signup" data-magnetic data-hero-primary data-i18n="land.cta.signup">Sign up</a>',
                      'href="pricing.html" data-magnetic data-hero-primary data-i18n="land.cta.signup">See plans</a>')
        p.write_text(s, encoding="utf-8")
    for p in sorted(site.rglob("*.js")):
        _sub(p, [("'Quant Backtest'", f"'{BRAND}'")])

    sh = site / "shared"
    for name, pairs, regex in (
        ("site-nav.js", [(r"\n    \{ key: 't7\.col\.mockups', items: \[\n.*?\n    \] \},", "")], True),
        ("command-palette.js", [(r"\n  \{ key: 'nav\.(holdings|monitor|us_monitor)'[^\n]*", "")], True),
        ("landing-sections.js", [("return { href: 'auth.html#signup', key: 'land.cta.signup' };",
                                  "return { href: 'pricing.html', key: 'land.cta.signup' };")], False),
    ):
        n = _sub(sh / name, pairs, regex)
        if n == 0:
            warns.append(f"改写没有命中：shared/{name}（源文件可能变了）")
    css = sh / "site-nav.css"
    if css.is_file():
        s = css.read_text(encoding="utf-8")
        s, n = re.subn(r"(\.site-nav__mark \{)[^}]*\}", r'\1\n  width: 22px;\n  height: 22px;\n  border-radius: 6px;\n'
                       r'  background: url("../assets/logo-mark-128.png") center / cover no-repeat;\n}', s, count=1)
        css.write_text(s, encoding="utf-8")
        if n == 0:
            warns.append("改写没有命中：shared/site-nav.css")

    drop_pre = tuple(al["i18n"].get("drop_prefixes", []))
    drop_keys = set(al["i18n"].get("drop_keys", []))
    for lang, extra in (("zh", ZH), ("en", EN)):
        p = sh / f"i18n-{lang}.json"
        if not p.is_file():
            warns.append(f"没有字典 shared/i18n-{lang}.json")
            continue
        j = json.loads(p.read_text(encoding="utf-8"))
        for k, v in list(j.items()):
            if k in drop_keys or k.startswith(drop_pre):
                del j[k]
            elif isinstance(v, str):
                j[k] = v.replace("Quant Backtest", BRAND).replace("quant-backtest", BRAND)
        for k, v in extra.items():
            if k in j or k == "t7.auth.lead":
                j[k] = v
        p.write_text(json.dumps(j, ensure_ascii=False, indent=1), encoding="utf-8")
    strip_mock_schema(site)
    return warns


# ---------------------------------------------------------------- 检查

_HOST = re.compile(r"""https?://([A-Za-z0-9.\-]+)""")
_OWN_OR_NS = {"accessiblequant.com", "www.w3.org"}


def external_hosts(site: Path) -> dict[str, int]:
    """源码里出现的第三方域名 -> 出现在几个文件里。只表示"源码里写了这个地址"：可能是注释、普通链接或没走到的分支，
    不等于访客浏览器一定会请求它；是否真请求要看实际网络记录。"""
    seen: dict[str, set[str]] = {}
    for p in sorted(site.rglob("*")):
        if p.is_file() and p.suffix in SCAN_SUFFIX:
            for h in _HOST.findall(p.read_text(encoding="utf-8", errors="replace")):
                if h not in _OWN_OR_NS:
                    seen.setdefault(h, set()).add(str(p.relative_to(site)))
    return {h: len(v) for h, v in sorted(seen.items())}


def _json_strings(o):
    """JSON 里所有字符串：键名和值（含嵌套）。"""
    if isinstance(o, dict):
        for k, v in o.items():
            yield str(k)
            yield from _json_strings(v)
    elif isinstance(o, list):
        for v in o:
            yield from _json_strings(v)
    elif isinstance(o, str):
        yield o


def pin_report(site: Path) -> dict[str, str]:
    """site 里所有需要钉死哈希的文件（字典、mock 数据、SVG）当前的 sha256。"""
    out = {}
    for p in sorted(site.rglob("*")):
        if p.is_file() and not p.is_symlink():
            rel = str(p.relative_to(site))
            if needs_pin(rel):
                out[rel] = sha256_file(p)
    return out


def check_site(site: Path, al: dict) -> dict:
    """检查导出目录里**所有**文件。返回 {errors, warnings, blockers, stats}。
    errors 非空 = 草稿都不合格；blockers 非空 = 草稿可检查，但不满足发布条件（见 release_ready）。"""
    errors: list[str] = []
    warnings: list[str] = []
    blockers: list[str] = []
    expected = {d for _, _, d in plan_files(al)}
    pins, content = al["binary_sha256"], al["content_pins"]
    files: list[Path] = []
    for p in sorted(site.rglob("*")):
        rel = str(p.relative_to(site))
        if p.is_symlink():
            errors.append(f"符号链接：{rel}")
            continue
        if p.is_dir():
            continue
        if not p.is_file():
            errors.append(f"不是普通文件：{rel}")
            continue
        files.append(p)
        if rel not in expected:
            errors.append(f"不在白名单里的文件：{rel}")
        name = p.name
        if name.startswith(".") or ".before-" in name or name.endswith((".map", ".bak", "~", ".orig", ".tmp")):
            errors.append(f"隐藏/备份/临时/sourcemap 文件：{rel}")
        suf = p.suffix
        if suf in BINARY_SUFFIX:
            want = pins.get(rel)
            if not want:
                errors.append(f"二进制资产没有登记哈希：{rel}")
            elif sha256_file(p) != want:
                errors.append(f"二进制资产哈希不符：{rel}")
        elif suf in SCAN_SUFFIX:
            try:
                text = p.read_text(encoding="utf-8")        # 严格解码：解不开就失败，不再 errors="ignore"
            except UnicodeDecodeError:
                errors.append(f"无法按 UTF-8 解码：{rel}")
                continue
            for m in sorted({m.group(0) for m in REDLINE.finditer(text)}):
                errors.append(f"红线内容「{m}」：{rel}")
            for w in COMPLIANCE_WORDS:
                if w in text:
                    errors.append(f"合规词「{w}」：{rel}")
            if "sourceMappingURL" in text:
                errors.append(f"含 sourceMappingURL：{rel}")
            if suf == ".svg" and SVG_ACTIVE.search(text):
                errors.append(f"SVG 含脚本/事件处理器/外部链接：{rel}")
            if suf == ".json" and needs_pin(rel):
                try:
                    obj = json.loads(text)
                except ValueError:
                    obj = None
                    errors.append(f"公开 JSON 不是合法 JSON：{rel}")
                if obj is not None:
                    n_api = sum(1 for sv in _json_strings(obj) if INTERNAL_API.search(sv))
                    if n_api:
                        errors.append(f"公开数据/字典里出现内部接口路径 /api/…（{n_api} 处，已按解析后的字符串检查）：{rel}")
            for w in COPY_BLOCK:
                if w in text:
                    blockers.append(f"数据口径与『现在全是合成演示数据』不符：「{w}」出现在 {rel}")
            if rel.startswith("mock-data/") and suf == ".json":
                try:
                    obj = json.loads(text)
                except ValueError:
                    obj = None
                meta = obj.get("_meta") if isinstance(obj, dict) else None
                note = str(meta.get("note", "")) if isinstance(meta, dict) else ""
                if not re.search(r"合成|synthetic", note, re.I):
                    errors.append(f"mock 数据没有『合成数据』来源标记（_meta.note）：{rel}")
        else:
            errors.append(f"未知后缀，拒绝：{rel}")
        if rel.startswith("mock-data/") and rel.count("/") > 1:
            errors.append(f"mock-data 只支持直接子文件（_meta.schema 的清理不递归），嵌套路径需要先扩展清理并重新审阅：{rel}")
        if needs_pin(rel):
            want = content.get(rel)
            if not want:
                errors.append(f"内容未钉死哈希（字典/mock/SVG 必须逐个登记 content_pins）：{rel}")
            elif sha256_file(p) != want:
                errors.append(f"内容与钉死的哈希不符（改动后需要重新审阅并更新 content_pins）：{rel}")
    for e in sorted(expected - {str(p.relative_to(site)) for p in files}):
        errors.append(f"白名单里的文件没有导出：{e}")

    for lang in ("zh", "en"):
        dp = site / "shared" / f"i18n-{lang}.json"
        if not dp.is_file():
            errors.append(f"缺少字典 shared/i18n-{lang}.json")
            continue
        try:
            d = json.loads(dp.read_text(encoding="utf-8"))
        except (ValueError, UnicodeDecodeError):
            errors.append(f"字典不是合法 JSON：i18n-{lang}.json")
            continue
        for k in al["required_keys"]:
            if k not in d:
                errors.append(f"字典 {lang} 缺少必须词条：{k}")
        bad = [k for k in d if k.startswith(tuple(al["i18n"].get("drop_prefixes", []))) or k in al["i18n"].get("drop_keys", [])
               or (re.search(r"(^|[._-])admin([._-]|$)", k) and k not in al["i18n"].get("allow_keys", []))]
        if bad:
            errors.append(f"字典 {lang} 含内部键（{len(bad)} 个，例 {bad[:3]}）")
        c = d.get("contact.email")
        if c is None:
            blockers.append(f"字典 {lang} 没有联系邮箱词条 contact.email（公开页必需）")
        elif CONTACT_PLACEHOLDER.search(str(c)) or "@" not in str(c):
            blockers.append(f"字典 {lang} 的联系邮箱还是占位值或格式不对：{c}")

    for h, n in external_hosts(site).items():
        warnings.append(f"源码里出现第三方域名：{h}（{n} 个文件；不等于一定会请求，实际请求要看浏览器网络记录）")

    html_entries = [p.resolve() for p in sorted(site.rglob("*.html"))]
    _, miss_hard, miss_link = closure(site, html_entries)
    for f, refs in sorted(miss_hard.items()):
        errors.append(f"引用不到文件：{f} -> {sorted(refs)}")
    for f, refs in sorted(miss_link.items()):
        warnings.append(f"页面链接指向未导出的页面：{f} -> {sorted(refs)}")
    return {"errors": errors, "warnings": warnings, "blockers": sorted(set(blockers)), "stats": {"files": len(files)}}


# ---------------------------------------------------------------- 清单

def make_manifest(site: Path) -> dict:
    files = []
    for p in sorted(site.rglob("*"), key=lambda x: str(x.relative_to(site)).encode("utf-8")):
        if p.is_file() and not p.is_symlink():
            files.append({"path": str(p.relative_to(site)), "size": p.stat().st_size, "sha256": sha256_file(p)})
    return {"files": files, "manifest_sha256": hashlib.sha256(canon(files).encode("utf-8")).hexdigest()}


def validate_manifest(m) -> None:
    """清单的 schema 与自洽：files 的每一项合法、路径规范且不重复，摘要等于 files 的哈希。"""
    if not isinstance(m, dict) or not isinstance(m.get("files"), list):
        raise BuildError("清单格式不对")
    seen = set()
    for f in m["files"]:
        if not (isinstance(f, dict) and set(f) == {"path", "size", "sha256"}):
            raise BuildError("清单条目格式不对")
        validate_rel(f["path"], "清单路径")
        if not isinstance(f["size"], int) or isinstance(f["size"], bool) or f["size"] < 0:
            raise BuildError(f"清单大小不对：{f['path']}")
        if not isinstance(f["sha256"], str) or not SHA_RE.fullmatch(f["sha256"]):
            raise BuildError(f"清单哈希格式不对：{f['path']}")
        k = equiv_key(f["path"])
        if k in seen:
            raise BuildError(f"清单里有重复路径：{f['path']}")
        seen.add(k)
    if hashlib.sha256(canon(m["files"]).encode("utf-8")).hexdigest() != m.get("manifest_sha256"):
        raise BuildError("清单的 manifest_sha256 与 files 内容不自洽（清单被改过）")


def compare_manifest(new: dict, old: dict | None) -> dict:
    if not old:
        return {"baseline": False, "added": [f["path"] for f in new["files"]], "changed": [], "removed": []}
    a = {f["path"]: f["sha256"] for f in new["files"]}
    b = {f["path"]: f["sha256"] for f in old["files"]}
    return {"baseline": True, "added": sorted(set(a) - set(b)), "removed": sorted(set(b) - set(a)),
            "changed": sorted(k for k in set(a) & set(b) if a[k] != b[k])}


# ---------------------------------------------------------------- 构建流程

class Lock:
    """串行锁：同一时刻只允许一个构建/标记。用 O_EXCL 建文件；已存在就拒绝（不自动抢占）。
    只保护遵守同一把锁的操作，不保护任意外部进程。"""

    def __init__(self, path: Path):
        self.path = path

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise BuildError(f"已有构建在运行（锁文件 {self.path}）。确认没有构建在跑之后，再手动移走这个锁文件。")
        with os.fdopen(fd, "w") as f:
            f.write(f"pid={os.getpid()} t={time.time():.0f}\n")
        return self

    def __exit__(self, *exc):
        try:
            self.path.unlink()
        except OSError:
            pass


def load_published(build_root: Path) -> dict | None:
    p = Path(build_root) / "published.json"
    try:
        rec = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    ok = (isinstance(rec, dict) and isinstance(rec.get("files"), list)
          and all(isinstance(f, dict) and isinstance(f.get("path"), str) and isinstance(f.get("sha256"), str) for f in rec["files"]))
    return rec if ok else None


def _is_ok_build(p: Path) -> bool:
    """成功的构建目录：名字是标准 build_id、有 site/ 和 manifest.json、report.json 里 ok 为真。"""
    if not (ID_RE.fullmatch(p.name) and p.is_dir() and not p.is_symlink() and (p / "site").is_dir() and (p / "manifest.json").is_file()):
        return False
    try:
        return json.loads((p / "report.json").read_text(encoding="utf-8")).get("ok") is True
    except (OSError, ValueError):
        return False


def prune(build_root: Path, trash: Path, keep: int) -> list[str]:
    """只保留最近 keep 份成功的构建（另外保留"已发布"那份）；多出来的移动到 trash 下一个全新的唯一目录，不永久删除、不合并。"""
    build_root, trash = Path(build_root), Path(trash)
    if overlaps(build_root, trash):
        raise BuildError("build 目录和回收目录不能互相包含")
    if not build_root.is_dir() or build_root.is_symlink():
        return []
    pub = (load_published(build_root) or {}).get("build_id")
    ok = sorted(p for p in build_root.iterdir() if _is_ok_build(p))
    moved = []
    for p in (ok[:-keep] if keep > 0 else ok):
        if p.name == pub:
            continue
        trash.mkdir(parents=True, exist_ok=True)
        dst, n = trash / p.name, 1
        while dst.exists() or dst.is_symlink():
            n += 1
            dst = trash / f"{p.name}.dup{n}"
        shutil.move(str(p), str(dst))
        moved.append(p.name)
    return moved


def build(src: Path = SRC_DEFAULT, template: Path = TEMPLATE_DEFAULT, allowlist: Path = ALLOWLIST_DEFAULT,
          build_root: Path = BUILD_ROOT_DEFAULT, trash: Path = TRASH_DEFAULT, keep: int = KEEP_DEFAULT) -> dict:
    """构建 + 检查。成功：build/<id>/{site,manifest.json,report.json}；失败：build/<id>.FAILED/（证据），已有产物不动。"""
    src, template, build_root, trash = Path(src), Path(template), Path(build_root), Path(trash)
    for a, b, why in ((build_root, src, "输出目录和源目录"), (build_root, template, "输出目录和模板目录"),
                      (trash, build_root, "回收目录和输出目录"), (trash, src, "回收目录和源目录"), (trash, template, "回收目录和模板目录")):
        if overlaps(a, b):
            raise BuildError(f"{why}重叠，拒绝")
    al_path = Path(allowlist).resolve()
    if build_root.resolve() in al_path.parents:
        raise BuildError("白名单文件在输出目录里，拒绝")
    if build_root.is_symlink():
        raise BuildError("输出目录本身是符号链接，拒绝")
    al = load_allowlist(allowlist)
    with Lock(build_root / ".build.lock"):
        stamp = utc_stamp()
        tmp = build_root / f"{stamp}.partial"
        if tmp.exists():
            raise BuildError(f"临时目录已存在：{tmp}")
        site = tmp / "site"
        site.mkdir(parents=True)
        report = {"errors": [], "warnings": [], "blockers": [], "stats": {}}
        try:
            copy_allowed(src, template, al, site)
            report["warnings"] += apply_transforms(site, al)
            rep = check_site(site, al)
            for k in ("errors", "warnings", "blockers"):
                report[k] += rep[k]
            report["stats"] = rep["stats"]
        except BuildError as e:
            report["errors"].append(str(e))
        except Exception as e:                      # 源文件 UTF-8 损坏、JSON 损坏、IO 错误等：也要留下失败证据，不能留一个孤零零的 .partial
            report["errors"].append(f"构建异常（{type(e).__name__}）：{e}")
        try:
            man = make_manifest(site)
        except Exception as e:
            man = {"files": [], "manifest_sha256": ""}
            report["errors"].append(f"生成清单失败（{type(e).__name__}）：{e}")
        base = f"{stamp}-{(man['manifest_sha256'] or '0' * 8)[:8]}"
        bid, n = base, 1
        while (build_root / bid).exists() or (build_root / f"{bid}.FAILED").exists():     # 同秒同内容：加序号，标识和目录名保持一致
            n += 1
            bid = f"{base}-{n}"
        report.update(build_id=bid, manifest_sha256=man["manifest_sha256"], ok=not report["errors"])
        report["release_ready"] = report["ok"] and not report["blockers"]
        pub = load_published(build_root)
        if (build_root / "published.json").exists() and pub is None:
            report["warnings"].append("published.json 存在但读不了或格式不对：已忽略，本次对比按『没有上次已发布』处理")
        report["diff_vs_published"] = compare_manifest(man, pub)
        (tmp / "manifest.json").write_text(json.dumps(man, ensure_ascii=False, indent=1), encoding="utf-8")
        (tmp / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        final = build_root / (bid if report["ok"] else f"{bid}.FAILED")
        tmp.rename(final)
        report["dir"] = str(final)
        if report["ok"]:
            report["pruned"] = prune(build_root, trash, keep)
        return report


def _load_build(build_id: str, build_root: Path) -> tuple[Path, dict]:
    """安全地取一份已构建的快照：build_id 只能是标准格式，根/快照/清单都不能是符号链接，清单要自洽。"""
    if not isinstance(build_id, str) or not ID_RE.fullmatch(build_id):
        raise BuildError(f"build_id 格式不对：{build_id!r}")
    root = Path(build_root)
    d = root / build_id
    for q in (root, d, d / "site", d / "manifest.json"):
        if q.is_symlink():
            raise BuildError(f"快照路径含符号链接，拒绝：{q}")
    if not (d.is_dir() and (d / "site").is_dir() and (d / "manifest.json").is_file()):
        raise BuildError(f"找不到快照：{d}")
    if (d / "manifest.json").stat().st_size > MAX_MANIFEST_BYTES:
        raise BuildError("清单文件过大")
    try:
        stored = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise BuildError(f"清单读不了：{e}")
    validate_manifest(stored)
    return d, stored


def _verify_loaded(d: Path, stored: dict, allowlist: Path, expect_sha256: str | None) -> dict:
    errors = []
    now = make_manifest(d / "site")
    if now["manifest_sha256"] != stored["manifest_sha256"]:
        errors.append("快照内容与构建时的清单不一致（有文件被改动、增加或删除）：" + json.dumps(compare_manifest(now, stored), ensure_ascii=False))
    if expect_sha256 and expect_sha256 != stored["manifest_sha256"]:
        errors.append(f"清单摘要与你给定的（构建时记下的）摘要不一致：{stored['manifest_sha256'][:16]}… ≠ {expect_sha256[:16]}…")
    rep = check_site(d / "site", load_allowlist(allowlist))
    errors += rep["errors"]
    return {"ok": not errors, "release_ready": not errors and not rep["blockers"], "errors": errors,
            "blockers": rep["blockers"], "warnings": rep["warnings"], "manifest_sha256": stored["manifest_sha256"]}


def verify(build_id: str, build_root: Path = BUILD_ROOT_DEFAULT, allowlist: Path = ALLOWLIST_DEFAULT,
           expect_sha256: str | None = None) -> dict:
    """上传前对已有快照重新校验：清单自洽、内容未变、检查仍然通过。注意：这不是不可变快照的证明，
    校验之后到上传之前快照目录仍是可写的，所以要在上传前立刻跑，并用构建时记下的摘要做 expect_sha256。"""
    d, stored = _load_build(build_id, Path(build_root))
    return _verify_loaded(d, stored, Path(allowlist), expect_sha256)


def mark_published(build_id: str, build_root: Path = BUILD_ROOT_DEFAULT, allowlist: Path = ALLOWLIST_DEFAULT,
                   expect_sha256: str | None = None) -> dict:
    """所有者手动上传并核对成功之后才调用：持锁，一次读取清单，校验通过且满足发布条件才更新"上次已发布"指针。"""
    build_root = Path(build_root)
    with Lock(build_root / ".build.lock"):
        d, stored = _load_build(build_id, build_root)
        res = _verify_loaded(d, stored, Path(allowlist), expect_sha256)
        if not res["ok"]:
            raise BuildError("快照校验没通过，不记录为已发布：" + "; ".join(res["errors"][:5]))
        if not res["release_ready"]:
            raise BuildError("这份快照还不满足发布条件，不记录为已发布：" + "; ".join(res["blockers"][:5]))
        rec = {"build_id": build_id, "manifest_sha256": stored["manifest_sha256"], "files": stored["files"],
               "marked_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        p = build_root / "published.json"
        tmp = p.with_name(f"published.json.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(p)
        return rec


def fresh_pin_report(src: Path, template: Path, allowlist: Path) -> dict[str, str]:
    """在系统临时目录里走一遍 复制 + 改写，打印需要钉死的内容哈希。不写仓库。"""
    al = load_allowlist(allowlist)
    with tempfile.TemporaryDirectory(prefix="pubsite-pin-") as t:
        site = Path(t) / "site"
        site.mkdir()
        copy_allowed(Path(src), Path(template), al, site)
        apply_transforms(site, al)
        return pin_report(site)


# ---------------------------------------------------------------- 命令行

def _inside_root(p: Path) -> bool:
    p = Path(p).resolve()
    return p == ROOT or ROOT in p.parents


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="公开站导出 + 检查（不上传）")
    ap.add_argument("--src", default=str(SRC_DEFAULT))
    ap.add_argument("--template", default=str(TEMPLATE_DEFAULT))
    ap.add_argument("--allowlist", default=str(ALLOWLIST_DEFAULT))
    ap.add_argument("--out-root", default=str(BUILD_ROOT_DEFAULT))
    ap.add_argument("--keep", type=int, default=KEEP_DEFAULT)
    ap.add_argument("--allow-outside-root", action="store_true", help="允许路径参数指向项目目录之外（默认拒绝）")
    ap.add_argument("--discover", action="store_true")
    ap.add_argument("--pin-report", action="store_true")
    ap.add_argument("--verify", metavar="BUILD_ID")
    ap.add_argument("--mark-published", metavar="BUILD_ID")
    ap.add_argument("--expect-manifest-sha256", metavar="SHA256")
    ap.add_argument("--release", action="store_true", help="与 --verify 连用：快照不满足发布条件(release_ready)就以非 0 退出")
    a = ap.parse_args(argv)
    if not a.allow_outside_root:
        for name in ("src", "template", "allowlist", "out_root"):
            if not _inside_root(getattr(a, name)):
                print(f"拒绝：--{name.replace('_', '-')} 指向项目目录之外（需要时加 --allow-outside-root）", file=sys.stderr)
                return 2
    common: list[str] = ["--allow-outside-root"] if a.allow_outside_root else []      # 后续命令必须指向同一个根，并带上同样的放行开关
    if Path(a.out_root).resolve() != BUILD_ROOT_DEFAULT.resolve():
        common += ["--out-root", str(a.out_root)]
    if Path(a.allowlist).resolve() != ALLOWLIST_DEFAULT.resolve():
        common += ["--allowlist", str(a.allowlist)]
    try:
        if a.discover:
            print(json.dumps(discover(Path(a.src), load_allowlist(a.allowlist)), ensure_ascii=False, indent=1))
            return 0
        if a.pin_report:
            print(json.dumps(fresh_pin_report(Path(a.src), Path(a.template), Path(a.allowlist)), ensure_ascii=False, indent=1))
            return 0
        if a.verify:
            r = verify(a.verify, Path(a.out_root), Path(a.allowlist), a.expect_manifest_sha256)
            print(json.dumps(r, ensure_ascii=False, indent=1))
            return 0 if (r["ok"] and (r["release_ready"] or not a.release)) else 1
        if a.mark_published:
            rec = mark_published(a.mark_published, Path(a.out_root), Path(a.allowlist), a.expect_manifest_sha256)
            print(f"已记录为已发布：{rec['build_id']}（{len(rec['files'])} 个文件）。请确认你确实已经上传并核对过线上内容。")
            return 0
        rep = build(Path(a.src), Path(a.template), Path(a.allowlist), Path(a.out_root), TRASH_DEFAULT, a.keep)
    except BuildError as e:
        print(f"构建失败：{e}", file=sys.stderr)
        return 2
    d = rep["diff_vs_published"]
    print(f"构建 {'通过（草稿可检查）' if rep['ok'] else '【没通过】'}：{rep['dir']}（{rep['stats'].get('files', 0)} 个文件）")
    print(f"build_id：{rep['build_id']}    manifest_sha256：{rep['manifest_sha256']}")
    for e in rep["errors"][:40]:
        print("  错误：", e)
    for b in rep["blockers"][:40]:
        print("  发布阻断：", b)
    for w in rep["warnings"][:40]:
        print("  告警：", w)
    base = "与上次已发布对比" if d["baseline"] else "没有上次已发布的记录，全部算新增"
    print(f"{base}：新增 {len(d['added'])}，修改 {len(d['changed'])}，删除 {len(d['removed'])}")
    if rep["ok"] and not rep["blockers"]:
        base_cmd = [".venv/bin/python", "scripts/build_public_site.py", *common]
        digest = ["--expect-manifest-sha256", rep["manifest_sha256"]]
        print("下一步（由你手动做，本脚本不上传；下面的命令由参数列表生成，可以直接复制）：")
        print("  1. 复核：" + shlex.join([*base_cmd, "--verify", rep["build_id"], "--release", *digest]))
        print(f"  2. 上传这份快照：{shlex.quote(rep['dir'] + '/site')}  （项目 {PROJECT}，分支 main；wrangler 的精确版本和登录由你负责）")
        print("  3. 核对线上内容后：" + shlex.join([*base_cmd, "--mark-published", rep["build_id"], *digest]))
    elif rep["ok"]:
        print("这份快照【不能发布】：上面的『发布阻断』没有解决之前，带 --release 的 --verify 会以非 0 退出，--mark-published 会拒绝。")
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
