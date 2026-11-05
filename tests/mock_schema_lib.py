"""A10：mock 假数据字段核对用的小工具（静态分析页面脚本，不运行 JS、不读 data/）。

做两件事：
1. 把 JS 源码里的注释和字符串内容抹成空格（长度不变，下标一一对应），方便用括号配对找函数体、调用参数；
2. 从页面脚本里找出「接口返回对象的属性访问」：
   - 根：`const r = await getJSON('/api/xxx')` 或 `const [a, b] = await Promise.all([getJSON(..), ..])`
   - 别名：`const x = r.rows`、`r.rows.map((row) => ..)`、`for (const row of r.rows)`、
     `render(r.rows)` 传给本地函数的参数
   - 读取：`r.a.b`、`row.code` 等，得到 (mock 文件名, 路径, 字段名, 是否带保护)
   「带保护」= 用了 `?.`，或后面紧跟 `??` / `||`：缺了页面也不会崩，只当提示。
只做静态近似：看不懂的写法（动态下标、解构嵌套、存进全局状态后再读）一律跳过，漏报不误报。
"""
from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path

# 页面自己判断的「业务提示」字段：成功的返回里本来就没有，不算 mock 缺字段
SENTINEL_FIELDS = {"error", "empty", "message", "missing", "mock"}
# 数组 / 字符串自带的属性和方法，不是数据字段
BUILTIN_PROPS = {
    "length", "map", "filter", "find", "findIndex", "findLast", "some", "every", "forEach", "reduce", "slice", "sort",
    "concat", "includes", "indexOf", "join", "flatMap", "at", "entries", "keys", "values", "push", "pop", "reverse",
    "toFixed", "toString", "toLocaleString", "startsWith", "endsWith", "split", "trim", "replace", "padStart",
    "hasOwnProperty", "then", "catch",
}
# 回调的第一个参数是数组元素的数组方法（reduce 的元素是第二个参数，单独处理）
ELEMENT_METHODS = ("map", "forEach", "filter", "find", "findIndex", "some", "every", "sort", "flatMap")
KEEP_METHODS = ("filter", "slice", "sort", "reverse", "concat")        # 返回值仍是同类元素数组的方法
IDENT = r"[A-Za-z_$][\w$]*"


# ---------------------------------------------------------------- 预处理

def mask_js(src: str) -> tuple[str, str]:
    """返回 (code, raw)，长度都和 src 一样。
    code：注释和字符串里的字符全换成空格，只剩真正的代码（模板字符串里 ${...} 的表达式保留）；
    raw：只去掉注释，字符串原样保留（用来读接口路径和 mock 文件名）。"""
    n = len(src)
    code, raw = list(src), list(src)

    def blank(buf, a, b):
        for k in range(a, b):
            if buf[k] != "\n":
                buf[k] = " "

    def scan_template(i):           # src[i] == '`'；返回结束的下标
        j = i + 1
        while j < n:
            c = src[j]
            if c == "\\":
                blank(code, j, j + 2); j += 2; continue
            if c == "`":
                return j
            if c == "$" and src[j + 1:j + 2] == "{":
                j = scan_code(j + 2, in_template=True)
                continue
            blank(code, j, j + 1); j += 1
        return n

    def scan_code(i, in_template=False):
        depth = 0
        prev = ""                   # 上一个非空白字符，用来判断 / 是除号还是正则
        while i < n:
            c = src[i]
            if c == "/" and src[i + 1:i + 2] == "/":
                j = src.find("\n", i); j = n if j < 0 else j
                blank(code, i, j); blank(raw, i, j); i = j; continue
            if c == "/" and src[i + 1:i + 2] == "*":
                j = src.find("*/", i + 2); j = n if j < 0 else j + 2
                blank(code, i, j); blank(raw, i, j); i = j; continue
            if c in "'\"":
                j = i + 1
                while j < n and src[j] != c and src[j] != "\n":
                    j += 2 if src[j] == "\\" else 1
                blank(code, i + 1, j); i = j + 1; prev = c; continue
            if c == "`":
                i = scan_template(i) + 1; prev = "`"; continue
            if c == "/" and (prev in "(,=:[!&|?{};" or prev == ""
                             or re.search(r"(?<![\w$])(?:return|typeof|case|in|of)\s*$", src[max(0, i - 10):i])):      # 正则字面量
                j, in_cls = i + 1, False
                while j < n and src[j] != "\n":
                    if src[j] == "\\": j += 2; continue
                    if src[j] == "[": in_cls = True
                    elif src[j] == "]": in_cls = False
                    elif src[j] == "/" and not in_cls: break
                    j += 1
                blank(code, i + 1, j); i = j + 1; prev = "/"; continue
            if in_template:
                if c == "{": depth += 1
                elif c == "}":
                    if depth == 0:
                        return i + 1
                    depth -= 1
            if not c.isspace():
                prev = c
            i += 1
        return i

    scan_code(0)
    return "".join(code), "".join(raw)


def pair_map(code: str, open_ch: str, close_ch: str) -> dict[int, int]:
    """括号配对：开括号下标 → 闭括号下标。"""
    stack, out = [], {}
    for i, c in enumerate(code):
        if c == open_ch:
            stack.append(i)
        elif c == close_ch and stack:
            out[stack.pop()] = i
    return out


def split_top(code: str, a: int, b: int) -> list[tuple[int, int]]:
    """把 code[a:b] 按最外层的逗号切开，返回每段的 (起, 止)。"""
    parts, depth, start = [], 0, a
    for i in range(a, b):
        c = code[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == "," and depth == 0:
            parts.append((start, i)); start = i + 1
    parts.append((start, b))
    return parts


# ---------------------------------------------------------------- 一个 JS 文件

@dataclass
class JsFile:
    path: Path
    code: str
    raw: str
    braces: dict[int, int]
    parens: dict[int, int]

    @classmethod
    def load(cls, path: Path) -> "JsFile":
        code, raw = mask_js(path.read_text(encoding="utf-8"))
        return cls(path, code, raw, pair_map(code, "{", "}"), pair_map(code, "(", ")"))

    def balanced(self) -> bool:
        return (len(self.braces) * 2 == self.code.count("{") + self.code.count("}")
                and len(self.parens) * 2 == self.code.count("(") + self.code.count(")"))

    def block_of(self, pos: int) -> tuple[int, int]:
        """包住 pos 的最内层 {…}；没有就是整个文件。"""
        best = (0, len(self.code))
        for a, b in self.braces.items():
            if a < pos < b and (b - a) < (best[1] - best[0]):
                best = (a, b)
        return best

    def line_of(self, pos: int) -> int:
        return self.code.count("\n", 0, pos) + 1


# ---------------------------------------------------------------- 接口 → mock 文件名

def mock_name_from_path(path: str) -> str:
    """和 shared/api.js 的 mockName 一样：去掉 /api/ 和查询串，其余 / 换成 _。"""
    clean = path.split("?")[0]
    clean = re.sub(r"^/api/", "", clean).strip("/")
    return clean.replace("/", "_")


def endpoint_of(path: str) -> str | None:
    """'/api/summary?market=${m}' → '/api/summary'；不是 /api/ 开头（或前缀是变量）返回 None。"""
    return path.split("?")[0] if path.startswith("/api/") else None


def calls_of(raw_args: str, unresolved: dict[str, list[tuple[str, str]]], mock_files: list[str]) -> list[tuple[str, str | None]]:
    """getJSON(第一个参数, { mock: '名字' }) → 这次调用在 mock 模式下会读的 [(mock 文件名, 接口路径)]（可能多个）。
    名字写死的即使文件不存在也会返回（让测试发现缺文件）；名字里带 ${变量} 的按现有文件通配。"""
    p = re.match(r"\s*(['\"`])((?:\\.|(?!\1).)*)\1", raw_args, re.S)
    path = p.group(2) if p else ""
    ep = endpoint_of(path)
    m = re.search(r"mock:\s*(['\"`])([^'\"`]*)\1", raw_args)
    if m:
        name = m.group(2)
        if "${" in name:
            return [(x, ep) for x in fnmatch.filter(mock_files, re.sub(r"\$\{[^}]*\}", "*", name))]
        return [(name, ep)]
    if not p:
        return []
    if path.startswith("${"):                       # 前缀是变量（例如盯盘页的 ${base}/quotes）
        tail = "/" + path.split("/", 1)[1].split("?")[0] if "/" in path else ""
        return list(unresolved.get(tail, []))
    return [(mock_name_from_path(re.sub(r"\$\{[^}]*\}", "X", path)), ep)]


# ---------------------------------------------------------------- 别名与读取

@dataclass(frozen=True)
class Alias:
    file: Path
    name: str
    mock: str
    path: tuple            # 路径元素：字段名、"[]"（数组元素）
    start: int             # 这个名字有效的范围（在 file 的下标）
    end: int


@dataclass(frozen=True)
class Read:
    file: Path
    line: int
    mock: str
    path: tuple            # 读取发生的位置（该对象在返回里的路径）
    key: str               # 读的字段名
    guarded: bool
    expr: str


@dataclass
class Program:
    """一个页面入口 + 它 import 的本地脚本（./xxx.js）。"""
    entry: Path
    files: dict[Path, JsFile] = field(default_factory=dict)

    @classmethod
    def load(cls, entry: Path) -> "Program":
        prog, todo = cls(entry), [entry]
        while todo:
            p = todo.pop()
            if p in prog.files or not p.exists():
                continue
            prog.files[p] = JsFile.load(p)
            for m in re.finditer(r"from\s+'(\./[\w\-]+\.js)'", prog.files[p].raw):
                todo.append(p.parent / m.group(1)[2:])
        return prog


CHAIN = re.compile(rf"(?P<root>{IDENT})(?P<chain>(?:\??\.{IDENT}|\?\.\[[^\]]*\]|\[[^\]\n]*\])*)")
DOTTED = rf"{IDENT}(?:\.{IDENT})*"


def parse_chain(chain: str) -> list[tuple[str, str]]:
    """'.a?.b[0].c' → [('a','.'), ('b','?.'), ('[]','['), ('c','.')]；变量下标记成 '*'。"""
    out = []
    for m in re.finditer(rf"(\?\.|\.)({IDENT})|\?\.\[([^\]]*)\]|\[([^\]\n]*)\]", chain):
        if m.group(2):
            out.append((m.group(2), m.group(1)))
        else:
            idx = (m.group(3) if m.group(3) is not None else m.group(4)).strip()
            out.append(("[]" if idx.isdigit() else "*", "?." if m.group(3) is not None else "["))
    return out


class Analyzer:
    def __init__(self, prog: Program, mock_files: list[str], unresolved: dict[str, list[tuple[str, str]]],
                 field_names: set | frozenset = frozenset()):
        """field_names：mock 里出现过的所有字段名。split / values 这类既是数组 / 字符串方法、又是真实字段名的，按字段处理。"""
        self.builtin = BUILTIN_PROPS - set(field_names)
        self.prog = prog
        self.mock_files = mock_files
        self.unresolved = unresolved
        self.aliases: list[Alias] = []
        self.links: dict[str, set] = {}                    # mock 文件名 → 页面里读它的接口路径
        self.extra_reads: list[Read] = []                  # 解构读取：const { a, b } = r.x
        self.wrappers: dict[str, list[tuple[str, str | None]]] = {}           # const fetchScan = (..) => getJSON(..)
        self.returners: dict[str, set] = {}                # 函数名 → 它 return 的 (mock, 路径)
        self.defs = self._collect_defs()
        self._seen: set = set()

    # ---- 本地函数定义：名字 → [(file, 参数文本列表, 函数体范围)]
    def _collect_defs(self):
        defs: dict[str, list] = {}
        for f in self.prog.files.values():
            pats = [rf"function\s+({IDENT})\s*\(", rf"(?:const|let)\s+({IDENT})\s*=\s*(?:async\s*)?\("]
            for pat in pats:
                for m in re.finditer(pat, f.code):
                    op = m.end() - 1
                    cl = f.parens.get(op)
                    if cl is None:
                        continue
                    if pat == pats[1]:
                        a = re.match(r"\s*=>\s*", f.code[cl + 1:cl + 40])
                        if not a:
                            continue
                        body_start = cl + 1 + a.end()
                    else:
                        body_start = cl + 1
                    if re.match(r"\s*\{", f.code[body_start:body_start + 5]):
                        bs = f.code.index("{", body_start)
                        rng = (bs, f.braces.get(bs, len(f.code)))
                    else:                                  # 表达式体箭头函数：到本行结束
                        e = f.code.find("\n", body_start)
                        rng = (body_start, len(f.code) if e < 0 else e)
                    params = [f.code[a:b].strip() for a, b in split_top(f.code, op + 1, cl)]
                    defs.setdefault(m.group(1), []).append((f, params, rng))
        return defs

    def _add(self, alias: Alias) -> bool:
        key = (alias.file, alias.name, alias.mock, alias.path, alias.start, alias.end)
        if key in self._seen:
            return False
        self._seen.add(key)
        self.aliases.append(alias)
        return True

    def _scope(self, f: JsFile, pos: int, declared: bool, lhs: str) -> tuple[int, int]:
        """const / let 声明的普通变量只在所在的 {…} 里有效；赋值给已有变量或对象属性（st.snap = ..）按整个文件算。"""
        return f.block_of(pos) if declared and "." not in lhs else (0, len(f.code))

    # ---- 根：getJSON 的返回值
    def _find_roots(self):
        for f in self.prog.files.values():
            # 先登记包装函数：const fetchScan = (extra) => getJSON(...)
            for m in re.finditer(rf"(?:const|let)\s+({IDENT})\s*=\s*(?:async\s*)?\([^)]*\)\s*=>\s*getJSON\(", f.code):
                op = m.end() - 1
                cl = f.parens.get(op)
                if cl is not None:
                    self.wrappers[m.group(1)] = calls_of(f.raw[op + 1:cl], self.unresolved, self.mock_files)
        for f in self.prog.files.values():
            call_names = "(?:" + "|".join(["getJSON"] + [re.escape(w) for w in self.wrappers]) + ")"

            def mocks_for(callname, op):
                cl = f.parens.get(op)
                if callname in self.wrappers:
                    return self.wrappers[callname]
                return calls_of(f.raw[op + 1:cl], self.unresolved, self.mock_files) if cl else []

            # const r = await getJSON(...)  /  r = await getJSON(...)  /  st.snap = await getJSON(...)
            for m in re.finditer(rf"((?:const|let|var)\s+)?({DOTTED})\s*=\s*await\s+({call_names})\(", f.code):
                lo, hi = self._scope(f, m.start(), bool(m.group(1)), m.group(2))
                for mk, ep in mocks_for(m.group(3), m.end() - 1):
                    self.links.setdefault(mk, set()).add(ep)
                    self._add(Alias(f.path, m.group(2), mk, (), lo, hi))
            # const [a, b] = await Promise.all([ getJSON(..), cond ? null : getJSON(..) ])
            for m in re.finditer(r"(?:const|let)\s*\[([^\]]+)\]\s*=\s*await\s+Promise\.all\(\s*\[", f.code):
                names = [x.strip() for x in m.group(1).split(",")]
                ob = m.end() - 1
                cb = pair_map(f.code[ob:], "[", "]").get(0)
                if cb is None:
                    continue
                lo, hi = f.block_of(m.start())
                for name, (a, b) in zip(names, split_top(f.code, ob + 1, ob + cb)):
                    cm = re.search(rf"\b({call_names})\(", f.code[a:b])
                    if not name or not cm:
                        continue
                    for mk, ep in mocks_for(cm.group(1), a + cm.end() - 1):
                        self.links.setdefault(mk, set()).add(ep)
                        self._add(Alias(f.path, name, mk, (), lo, hi))

    # ---- 表达式 → (alias, 路径)
    def _resolve_expr(self, f: JsFile, text: str, pos: int):
        """text 形如 `r.rows` / `r?.meta` / `st.snap.positions`：是某个别名下的路径就产出 (Alias, 路径)。"""
        text = text.strip()
        for al in self.aliases:
            if al.file != f.path or not (al.start <= pos < al.end) or not text.startswith(al.name):
                continue
            rest = text[len(al.name):]
            if rest and not re.match(r"\??\.|\[", rest):
                continue
            path = list(al.path)
            for key, _ in parse_chain(rest):
                if key in self.builtin:
                    break
                path.append(key)
            yield al, tuple(path)

    def _arg_path(self, f: JsFile, a: int, b: int):
        """函数调用的某个实参：整体是 别名链（可后接 .filter(..) 等保持元素类型的方法 / || [] / ?? []）。"""
        text = f.code[a:b].strip()
        m = CHAIN.match(text)
        if not m:
            return
        rest = text[m.end():].strip()
        ok = rest == "" or re.match(r"^(\|\||\?\?)\s*(\[\]|\{\})$", rest) or re.match(rf"^\.({'|'.join(KEEP_METHODS)})\(", rest)
        if ok:
            yield from self._resolve_expr(f, text[:m.end()], a)

    # ---- 推导别名（反复做直到没有新别名）
    def _derive_once(self) -> bool:
        grew = False
        for f in self.prog.files.values():
            code = f.code
            # const x = r.rows;  x = r.rows || [];  st.snap = snap
            for m in re.finditer(rf"((?:const|let)\s+)?({DOTTED})\s*=\s*({IDENT}(?:\??\.{IDENT})*)\s*(?=;|\n|,|\)|\|\||\?\?)", code):
                for al, path in self._resolve_expr(f, m.group(3), m.start()):
                    lo, hi = self._scope(f, m.start(), bool(m.group(1)), m.group(2))
                    grew |= self._add(Alias(f.path, m.group(2), al.mock, path, lo, hi))
            # const lead = s.strategies.find((c) => ..) ?? ..  →  lead 是 strategies 的一个元素
            for m in re.finditer(rf"((?:const|let)\s+)?({DOTTED})\s*=\s*({IDENT}(?:\??\.{IDENT})*)\??\.(?:find|findLast|at)\(", code):
                for al, path in self._resolve_expr(f, m.group(3), m.start()):
                    lo, hi = self._scope(f, m.start(), bool(m.group(1)), m.group(2))
                    grew |= self._add(Alias(f.path, m.group(2), al.mock, path + ("[]",), lo, hi))
            # const { a, b: c = 1 } = r.meta  → 读取 a、b
            for m in re.finditer(rf"(?:const|let)\s*\{{([^{{}}]*)\}}\s*=\s*({IDENT}(?:\??\.{IDENT})*)\s*(?=;|\n)", code):
                for al, path in self._resolve_expr(f, m.group(2), m.start()):
                    grew |= self._destructure(f, m.start(), m.group(1), al, path)
            # for (const row of r.rows)
            for m in re.finditer(rf"for\s*\(\s*(?:const|let)\s+({IDENT})\s+of\s+({IDENT}(?:\??\.{IDENT})*)\s*\)", code):
                lo, hi = f.block_of(m.start())
                for al, path in self._resolve_expr(f, m.group(2), m.start()):
                    grew |= self._add(Alias(f.path, m.group(1), al.mock, path + ("[]",), m.start(), hi))
            # r.rows.map((row, i) => ..) / .forEach / .filter / .find / .sort((a, b) => ..) / .reduce((acc, row) => ..)
            methods = "|".join(ELEMENT_METHODS + ("reduce",))
            for m in re.finditer(rf"({IDENT}(?:\??\.{IDENT})*)\??\.({methods})\(\s*(?:async\s*)?(?:\(\s*([^)]*)\)|({IDENT}))\s*=>", code):
                op = code.index("(", m.end(1))
                cl = f.parens.get(op)
                if cl is None:
                    continue
                plist = [x.strip() for x in (m.group(3) if m.group(3) is not None else m.group(4)).split(",") if x.strip()]
                plist = plist[1:2] if m.group(2) == "reduce" else plist[:2] if m.group(2) == "sort" else plist[:1]
                for al, path in self._resolve_expr(f, m.group(1), m.start()):
                    for p in plist:
                        if re.fullmatch(IDENT, p):
                            grew |= self._add(Alias(f.path, p, al.mock, path + ("[]",), op, cl))
            # 调用本地函数：fn(r, r.rows.filter(..)) → 函数里的形参成为别名
            for m in re.finditer(rf"\b({IDENT})\(", code):
                fname = m.group(1)
                if fname not in self.defs:
                    continue
                op = m.end() - 1
                cl = f.parens.get(op)
                if cl is None:
                    continue
                for i, (a, b) in enumerate(split_top(code, op + 1, cl)):
                    for al, path in self._arg_path(f, a, b):
                        for df, params, rng in self.defs[fname]:
                            if i >= len(params):
                                continue
                            pname = params[i].split("=")[0].strip()
                            if re.fullmatch(IDENT, pname):
                                grew |= self._add(Alias(df.path, pname, al.mock, path, *rng))
                            elif pname.startswith("{") and pname.endswith("}"):      # 形参解构：function f({ a, b })
                                grew |= self._destructure(df, rng[0], pname[1:-1], al, path)
            # 函数 return 了某个别名 → 记下来；x = await fn() 时 x 就是那个别名
            for fname, defs in self.defs.items():
                for df, _, rng in defs:
                    for m in re.finditer(rf"\breturn\s+(?:await\s+)?({IDENT}(?:\??\.{IDENT})*)\s*(?=;|\n|\))", df.code[rng[0]:rng[1]]):
                        for al, path in self._resolve_expr(df, m.group(1), rng[0] + m.start()):
                            tag = (al.mock, path)
                            if tag not in self.returners.setdefault(fname, set()):
                                self.returners[fname].add(tag)
                                grew = True
            for fname, tags in self.returners.items():
                for m in re.finditer(rf"((?:const|let)\s+)?({DOTTED})\s*=\s*(?:await\s+)?{re.escape(fname)}\(", code):
                    lo, hi = self._scope(f, m.start(), bool(m.group(1)), m.group(2))
                    for mock, path in tags:
                        grew |= self._add(Alias(f.path, m.group(2), mock, path, lo, hi))
        return grew

    def _destructure(self, f: JsFile, pos: int, names: str, al: Alias, path: tuple) -> bool:
        grew = False
        for part in split_top(names, 0, len(names)):
            item = names[part[0]:part[1]].strip()
            if not item or item.startswith("..."):
                continue
            key = re.split(r"[:=]", item)[0].strip()
            if not re.fullmatch(IDENT, key):
                continue
            tag = (f.path, pos, al.mock, path, key)
            if tag in self._seen:
                continue
            self._seen.add(tag)
            self.extra_reads.append(Read(f.path, f.line_of(pos), al.mock, path, key, "=" in item, f"{{ {key} }}"))
            grew = True
        return grew

    # ---- 读取
    def _in_sentinel_branch(self, f: JsFile, pos: int) -> bool:
        """读取是否发生在 `if (r.empty) {…}` / `if (r.error || …) {…}` 这类「没数据」分支里（这时返回里本来就不是正常字段）。"""
        for a, b in f.braces.items():
            if a < pos < b:
                m = re.search(r"\bif\s*\(([^{};]*)\)\s*$", f.code[max(0, a - 160):a])
                if m and re.search(r"(?<!!)(?<![\w$.])[\w$]+(?:\??\.[\w$]+)*\.(?:empty|error|missing)\b", m.group(1)):
                    return True
        return False

    def reads(self) -> list[Read]:
        self._find_roots()
        for _ in range(10):
            if not self._derive_once():
                break
        out, seen = list(self.extra_reads), set()
        for al in self.aliases:
            f = self.prog.files[al.file]
            code = f.code
            for m in re.finditer(rf"(?<![\w$.]){re.escape(al.name)}(?![\w$])", code[al.start:al.end]):
                pos = al.start + m.start()
                cm = CHAIN.match(code, pos)
                if not cm or not cm.group(0).startswith(al.name):
                    continue
                path = list(al.path)
                steps = parse_chain(cm.group(0)[len(al.name):])
                tail = code[cm.end():cm.end() + 12]
                before = code[max(0, pos - 16):pos]
                branch = None
                for idx, (key, sep) in enumerate(steps):
                    if key in ("*", "[]"):
                        path.append(key); continue
                    if key in self.builtin:
                        break
                    last = idx == len(steps) - 1
                    nxt_optional = not last and steps[idx + 1][1] == "?."
                    guarded = (sep == "?." and idx > 0) or nxt_optional or (last and (
                        bool(re.match(r"\s*(\?\?|\|\||&&|\?(?![.?]))", tail))
                        or bool(re.search(r"(?:!|\bif\s*\(|Array\.isArray\(|Boolean\(|typeof\s)\s*$", before))))
                    k = (al.file, pos, al.mock, tuple(path), key)
                    if k not in seen:
                        seen.add(k)
                        if branch is None:
                            branch = self._in_sentinel_branch(f, pos)
                        if not branch:
                            out.append(Read(al.file, f.line_of(pos), al.mock, tuple(path), key, guarded, cm.group(0)))
                    path.append(key)
        return out


# ---------------------------------------------------------------- 在 mock 里找位置

def all_keys(data) -> set:
    """一个 JSON 里出现过的所有对象键。"""
    out = set()
    if isinstance(data, dict):
        for k, v in data.items():
            out.add(k)
            out |= all_keys(v)
    elif isinstance(data, list):
        for v in data[:50]:
            out |= all_keys(v)
    return out


def nodes_at(data, path: tuple, limit: int = 30) -> list:
    """沿 path 走到 data 里的所有对应节点；"[]" 取数组前 limit 项，"*" 取数组元素或对象的所有值。"""
    cur = [data]
    for step in path:
        nxt = []
        for node in cur:
            if step == "[]" or step == "*":
                if isinstance(node, list):
                    nxt += node[:limit]
                elif isinstance(node, dict) and step == "*":
                    nxt += list(node.values())[:limit]
            elif isinstance(node, dict) and step in node:
                nxt.append(node[step])
        cur = nxt
    return cur


def check_reads(reads: list[Read], mocks: dict[str, object]) -> tuple[list[Read], list[Read]]:
    """返回 (缺字段且没有保护, 缺字段但有保护)。
    跳过：没有这个 mock 文件、节点不是对象、数组是空的、对象本身是 {error/empty} 这类业务提示（例如 compare 里出错的那一行）。"""
    hard, soft = [], []
    # 同一个文件里只要有一处把这个字段当可选来读（`?.`、`!x.f`、`x.f ? ..`），其余各处靠提前 return / 三元分支保护，也算有保护
    tolerant = {(r.file, r.mock, r.path, r.key) for r in reads if r.guarded}
    for r in reads:
        if r.key in SENTINEL_FIELDS or r.mock not in mocks:
            continue
        if (r.file, r.mock, r.path, r.key) in tolerant:
            r = Read(r.file, r.line, r.mock, r.path, r.key, True, r.expr)
        nodes = [n for n in nodes_at(mocks[r.mock], r.path) if isinstance(n, dict) and not ({"error", "empty", "missing"} & n.keys())]
        if not nodes:
            continue
        if r.guarded:
            if all(r.key not in n for n in nodes):
                soft.append(r)
        elif any(r.key not in n for n in nodes):
            hard.append(r)
    return hard, soft
