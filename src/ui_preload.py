"""新界面页面的模块预加载清单（D65）。

为什么：/ui/ 页面是一串 ES 模块，浏览器只有下载完一个模块、读到它的 import 才知道下一个要什么，
第一次打开（冷缓存）时要来回十几趟（模拟 80ms 往返时今日页约 1.4 秒）。在 HTML 里列出整页会用到的模块
（<link rel="modulepreload">），浏览器拿到 HTML 就能同时去取，来回次数降到几趟。

做法：从页面里的 <script type="module" src> 和 <body data-page="x">（page-boot.js 会动态加载 pages/x.js）出发，
沿着静态 import（import … from '…' / import '…' / export … from '…'）找出整棵依赖树。注释里的 import 不算；
其他写在字符串里的动态 import（如 ?debug=1 才加载的 debug-fields.js）不预加载。
只是提示：漏了或多了都不影响功能，浏览器照常按 import 加载。结果按文件修改时间缓存。
"""
from __future__ import annotations

import posixpath
import re
from pathlib import Path

_IMPORT_RE = re.compile(r"""(?:^|[;\s}])(?:import|export)\s+(?:[\w*{}\s,$]+?\s+from\s+)?(['"])(\.{1,2}/[^'"\n]+?\.m?js)\1""", re.M)
_SCRIPT_RE = re.compile(r"""<script\b[^>]*\btype=["']module["'][^>]*\bsrc=["']([^"'?#]+)""", re.I)
_PAGE_RE = re.compile(r"""<body\b[^>]*\bdata-page=["']([\w-]+)["']""", re.I)
_cache: dict = {}   # 相对路径 -> (修改时间 ns, 直接依赖列表)


def _strip_comments(code: str) -> str:
    """去掉 /* */ 和 // 注释（粗略：不处理字符串里的 //，import 语句本身不会出现在那种字符串里）。"""
    code = re.sub(r"/\*.*?\*/", " ", code, flags=re.S)
    return re.sub(r"(?m)(^|[^:'\"\\])//.*$", r"\1", code)


def _deps(ui_dir: Path, rel: str) -> list[str]:
    f = ui_dir / rel
    try:
        mt = f.stat().st_mtime_ns
    except OSError:
        return []
    hit = _cache.get((str(ui_dir), rel))
    if hit and hit[0] == mt:
        return hit[1]
    code = _strip_comments(f.read_text(encoding="utf-8", errors="replace"))
    base = posixpath.dirname(rel)
    out = []
    for m in _IMPORT_RE.finditer(code):
        dep = posixpath.normpath(posixpath.join(base, m.group(2)))
        if not dep.startswith("..") and dep not in out:
            out.append(dep)
    _cache[(str(ui_dir), rel)] = (mt, out)
    return out


def modules_for_page(ui_dir: Path, page_rel: str, html: str) -> list[str]:
    """页面要用到的全部模块（相对 /ui/ 的路径，按发现顺序，不含不存在的文件）。"""
    ui_dir = Path(ui_dir)
    base = posixpath.dirname(page_rel)
    roots = [posixpath.normpath(posixpath.join(base, s)) for s in _SCRIPT_RE.findall(html)]
    m = _PAGE_RE.search(html)
    if m and (ui_dir / "pages" / f"{m.group(1)}.js").is_file():
        roots.append(f"pages/{m.group(1)}.js")      # page-boot.js 按 data-page 动态加载的页面模块
    seen, order, stack = set(), [], list(reversed(roots))
    while stack:
        rel = stack.pop()
        if rel in seen or rel.startswith("..") or not (ui_dir / rel).is_file():
            continue
        seen.add(rel)
        order.append(rel)
        stack.extend(reversed(_deps(ui_dir, rel)))
    return order
