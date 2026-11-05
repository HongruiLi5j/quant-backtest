"""配色对比度检查（WCAG 2.1）：纯 Python 解析 dashboard/ui/theme/tokens.css，不依赖浏览器。

用法：
  .venv/bin/python tests/test_ui_contrast.py                 跑单元测试（不达标会失败）
  .venv/bin/python tests/test_ui_contrast.py --table         打印全部「前景 × 背景」对比度表
  .venv/bin/python tests/test_ui_contrast.py --table --css 某个.css   对别的 tokens 文件出表（比如改动前的版本）
  .venv/bin/python tests/test_ui_contrast.py --suggest       对不达标的组合给出「保持色相、只调亮度」的建议色

公式（WCAG 2.1）：
  线性化：c ≤ 0.04045 → c/12.92，否则 ((c+0.055)/1.055)^2.4
  相对亮度 L = 0.2126 R + 0.7152 G + 0.0722 B
  对比度 = (L亮 + 0.05) / (L暗 + 0.05)
半透明色先按 sRGB 通道线性混合叠到下层不透明底色上（浏览器 alpha 合成也是这样做的），再算对比度。

只读 tokens.css，不读 data/，不联网。
"""
import colorsys
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOKENS = ROOT / "dashboard" / "ui" / "theme" / "tokens.css"

AA_TEXT = 4.5   # 正文（< 18.66px 粗体 / < 24px 常规）
AA_LARGE = 3.0  # 大字，以及非文字的图形/界面元素（WCAG 1.4.11）


# ---------------------------------------------------------------- 解析 tokens.css

def _strip_comments(css):
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def _blocks(css):
    """把 CSS 拆成 [(选择器, 块体)]，@media 里嵌套的规则选择器前面加 '@media ... ⟶ '。"""
    out = []

    def scan(text, prefix):
        i = 0
        while i < len(text):
            j = text.find("{", i)
            if j < 0:
                break
            sel = text[i:j].strip()
            depth, k = 1, j + 1
            while depth and k < len(text):
                depth += {"{": 1, "}": -1}.get(text[k], 0)
                k += 1
            body = text[j + 1:k - 1]
            if sel.startswith("@media"):
                scan(body, f"{sel} ⟶ ")
            else:
                out.append((prefix + sel, body))
            i = k

    scan(_strip_comments(css), "")
    return out


def _decls(body):
    return {m.group(1): m.group(2).strip() for m in re.finditer(r"(--[\w-]+)\s*:\s*([^;]+);", body)}


def parse_colors(path=TOKENS):
    """返回 (dark, light, light_media)：三个 {变量名: 值字符串}。light 只含浅色覆盖项。"""
    dark = light = media = None
    for sel, body in _blocks(Path(path).read_text(encoding="utf-8")):
        if sel.startswith(":root,") and 'data-theme="dark"' in sel:
            dark = _decls(body)
        elif sel == ':root[data-theme="light"]':
            light = _decls(body)
        elif sel.startswith("@media (prefers-color-scheme: light)"):
            media = _decls(body)
    if dark is None or light is None:
        raise ValueError("tokens.css 里找不到深色或浅色主题块")
    return dark, light, media


def parse_color(s):
    """'#rgb' / '#rrggbb' / 'rgb(a)(r, g, b[, a])' → (r, g, b, a)，r/g/b 为 0-255。"""
    s = s.strip().lower()
    m = re.fullmatch(r"#([0-9a-f]{3}|[0-9a-f]{6})", s)
    if m:
        h = m.group(1)
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 1.0)
    m = re.fullmatch(r"rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*(?:,\s*([\d.]+)\s*)?\)", s)
    if m:
        a = float(m.group(4)) if m.group(4) is not None else 1.0
        return (float(m.group(1)), float(m.group(2)), float(m.group(3)), a)
    raise ValueError(f"不认识的颜色值：{s!r}")


# ---------------------------------------------------------------- WCAG 公式

def _lin(c8):
    c = c8 / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def luminance(rgb):
    r, g, b = rgb[:3]
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def contrast(c1, c2):
    l1, l2 = luminance(c1), luminance(c2)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


def over(fg, bg):
    """半透明 fg 叠在不透明 bg 上，返回不透明 (r, g, b)。"""
    a = fg[3]
    return tuple(a * fg[i] + (1 - a) * bg[i] for i in range(3))


def mix(c1, c2, p):
    """CSS color-mix(in srgb, c1 p%, c2) 的近似：两个不透明色按比例混合。"""
    return tuple(p * c1[i] + (1 - p) * c2[i] for i in range(3))


def to_hex(rgb):
    return "#" + "".join(f"{max(0, min(255, round(v))):02x}" for v in rgb[:3])


# ---------------------------------------------------------------- 组合清单

class Theme:
    """一套主题的有效变量（浅色 = 深色打底 + 浅色覆盖，和浏览器级联一致），以及几层底色。"""

    def __init__(self, name, vars_):
        self.name = name
        self.v = {k: parse_color(x) for k, x in vars_.items() if _is_color(x)}
        base = self.v["--bg-base"][:3]
        self.layers = {
            "页面底 bg-base": base,
            "卡片 bg-surface": over(self.v["--bg-surface"], base),
            "浮层 bg-elevated": over(self.v["--bg-elevated"], base),
            "实色兜底 bg-solid": self.v["--bg-solid"][:3],
        }
        self.layers["凹槽(徽章) bg-sunken@页面"] = over(self.v["--bg-sunken"], base)
        self.layers["凹槽(徽章) bg-sunken@卡片"] = over(self.v["--bg-sunken"], self.layers["卡片 bg-surface"])
        # 悬停：surface-hover 叠在卡片上
        self.layers["悬停 surface-hover@卡片"] = over(self.v["--surface-hover"], self.layers["卡片 bg-surface"])
        # 警告横幅：amber 8% 混进卡片
        self.layers["警告横幅 amber8%@卡片"] = mix(self.v["--amber"], self.layers["卡片 bg-surface"], 0.08)

    def fg(self, token, bg):
        """文字色；带透明度的先叠到 bg 上。"""
        return over(self.v[token], bg)


def _is_color(x):
    return bool(re.fullmatch(r"#[0-9a-fA-F]{3,6}|rgba?\([^)]*\)", x.strip()))


NORMAL_BGS = ["页面底 bg-base", "卡片 bg-surface", "浮层 bg-elevated", "实色兜底 bg-solid"]
BADGE_BGS = NORMAL_BGS[:2] + ["凹槽(徽章) bg-sunken@页面", "凹槽(徽章) bg-sunken@卡片"]

# (类别, 前景变量, 要检查的底色名列表, 门槛, 说明)
TEXT_PAIRS = [
    ("正文", "--text-primary", NORMAL_BGS + BADGE_BGS[2:] + ["悬停 surface-hover@卡片"], AA_TEXT, "标题/正文"),
    ("次要文字", "--text-secondary", NORMAL_BGS + BADGE_BGS[2:] + ["悬停 surface-hover@卡片"], AA_TEXT,
     "说明、表头、默认徽章文字"),
    ("强调文字", "--accent-text", NORMAL_BGS + BADGE_BGS[2:], AA_TEXT, "链接、accent 徽章"),
    ("涨(A股红)", "--red", NORMAL_BGS + BADGE_BGS[2:], AA_TEXT, "涨跌文字 / rise 徽章(cn)；美股模式下是跌"),
    ("跌(A股绿)", "--green", NORMAL_BGS + BADGE_BGS[2:], AA_TEXT, "涨跌文字 / fall 徽章(cn)；美股模式下是涨"),
    ("警告", "--amber", NORMAL_BGS + BADGE_BGS[2:] + ["警告横幅 amber8%@卡片"], AA_TEXT, "警告文字、warn 徽章、横幅"),
    ("图表刻度字", "--chart-text", NORMAL_BGS, AA_TEXT, "SVG 坐标轴刻度，11px"),
]

# 非文字：图形/焦点环，门槛 3:1（WCAG 1.4.11）
GRAPHIC_PAIRS = [
    ("焦点环", "--focus-ring", NORMAL_BGS, AA_LARGE, "键盘焦点指示（1.4.11）"),
    ("涨跌色块(涨,cn)", "--red-fill", NORMAL_BGS, AA_LARGE, "柱/点/热力图填充"),
    ("涨跌色块(跌,cn)", "--green-fill", NORMAL_BGS, AA_LARGE, "柱/点/热力图填充"),
    ("图表线1", "--chart-1", NORMAL_BGS, AA_LARGE, "主折线"),
    ("图表线2", "--chart-2", NORMAL_BGS, AA_LARGE, "副折线"),
    ("回撤线", "--chart-dd", NORMAL_BGS, AA_LARGE, "回撤区域"),
    ("基准线", "--chart-bench", NORMAL_BGS, AA_LARGE, "基准折线"),
]

# 禁用态/纯装饰：WCAG 1.4.3 明确豁免「不可用的界面元素」和「纯装饰」，但仍设一个 3:1 的底线，
# 免得完全看不见。注意：当前有若干处把它用在了承载信息的小字上，见报告。
DIM_PAIRS = [
    ("禁用/装饰", "--text-dim", NORMAL_BGS, AA_LARGE, "禁用态；豁免 4.5，保底 3:1"),
]


def _check_rows(theme):
    rows = []
    for group in (TEXT_PAIRS, GRAPHIC_PAIRS, DIM_PAIRS):
        for cat, token, bgs, need, note in group:
            for bgname in bgs:
                bg = theme.layers[bgname]
                fg = theme.fg(token, bg)
                rows.append(dict(theme=theme.name, cat=cat, fg=token, fg_hex=to_hex(fg), bg=bgname,
                                 bg_hex=to_hex(bg), ratio=contrast(fg, bg), need=need, note=note))
    # 按钮：accent-contrast 文字压在渐变两端（accent-primary → accent-2）；头像压在 accent-glow → accent-2
    ac = theme.v["--accent-contrast"][:3]
    for label, stops in (("主按钮", ("--accent-primary", "--accent-2")),
                         ("头像", ("--accent-primary", "--accent-2")),
                         ("跳转链接", ("--accent-primary",))):
        for stop in stops:
            bg = theme.v[stop][:3]
            rows.append(dict(theme=theme.name, cat=f"{label}文字", fg="--accent-contrast", fg_hex=to_hex(ac),
                             bg=f"{label}底 {stop[2:]}", bg_hex=to_hex(bg), ratio=contrast(ac, bg), need=AA_TEXT,
                             note="14px/600 或 12px/700，都不算大字"))
    # 热力图单元格：文字(text-primary) 压在 70% 色块 + 30% 凹槽上（最深的一档）
    sunk = theme.layers["凹槽(徽章) bg-sunken@卡片"]
    for tok, nm in (("--red-fill", "热力格(红)"), ("--green-fill", "热力格(绿)")):
        bg = mix(theme.v[tok], sunk, 0.70)
        fg = theme.v["--text-primary"][:3]
        rows.append(dict(theme=theme.name, cat="热力图格内字", fg="--text-primary", fg_hex=to_hex(fg),
                         bg=f"{nm} 70%饱和", bg_hex=to_hex(bg), ratio=contrast(fg, bg), need=AA_TEXT,
                         note="最深一档(k=1)是最坏情况"))
    return rows


def load_themes(path=TOKENS):
    dark, light, _media = parse_colors(path)
    return [Theme("深色", dark), Theme("浅色", {**dark, **light})]


def all_rows(path=TOKENS):
    rows = []
    for th in load_themes(path):
        rows += _check_rows(th)
    return rows


# ---------------------------------------------------------------- 只调亮度的修复建议

def suggest(fg_hex_or_rgba, bg_rgb, need, direction):
    """保持色相和饱和度(HSL)，沿 direction(+1 变亮 / -1 变暗) 找刚好达标的最小亮度改动。
    前景若带透明度，返回的是不透明色（半透明色请直接调 alpha，见报告）。"""
    r, g, b, _a = parse_color(fg_hex_or_rgba)
    h, l, s = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
    for step in range(0, 1001):
        l2 = min(1.0, max(0.0, l + direction * step / 1000))
        rgb = tuple(round(x * 255) for x in colorsys.hls_to_rgb(h, l2, s))
        if contrast(rgb, bg_rgb) >= need + 0.05:  # 留 0.05 余量，避免四舍五入后擦边
            return to_hex(rgb)
    return None


def print_table(path=TOKENS, only_fail=False):
    print(f"| 主题 | 类别 | 前景 | 前景色 | 底色 | 底色值 | 对比度 | 门槛 | 结果 |")
    print("|---|---|---|---|---|---|---|---|---|")
    for r in all_rows(path):
        ok = r["ratio"] >= r["need"]
        if only_fail and ok:
            continue
        print(f"| {r['theme']} | {r['cat']} | {r['fg']} | {r['fg_hex']} | {r['bg']} | {r['bg_hex']} | "
              f"{r['ratio']:.2f} | {r['need']} | {'通过' if ok else '**不达标**'} |")


def print_suggestions(path=TOKENS):
    for r in all_rows(path):
        if r["ratio"] >= r["need"]:
            continue
        bg = parse_color(r["bg_hex"])
        direction = -1 if luminance(bg) > 0.5 else 1  # 浅底把字变深，深底把字变亮
        print(f"{r['theme']} {r['fg']} on {r['bg']}: {r['ratio']:.2f} < {r['need']} → 建议 "
              f"{suggest(r['fg_hex'], bg, r['need'], direction)}")


# ---------------------------------------------------------------- 测试

class ContrastFormulaTests(unittest.TestCase):
    """先确认公式本身：用 WCAG 文档里的已知值。"""

    def test_black_on_white_is_21(self):
        self.assertAlmostEqual(contrast((0, 0, 0), (255, 255, 255)), 21.0, places=6)

    def test_same_color_is_1(self):
        self.assertAlmostEqual(contrast((12, 34, 56), (12, 34, 56)), 1.0, places=9)

    def test_known_gray(self):
        # #767676 在白底上是 AA 刚好达标的经典灰：4.54:1
        self.assertAlmostEqual(contrast((0x76, 0x76, 0x76), (255, 255, 255)), 4.54, places=2)

    def test_symmetric(self):
        a, b = (10, 200, 30), (240, 20, 90)
        self.assertEqual(contrast(a, b), contrast(b, a))

    def test_alpha_over(self):
        self.assertEqual(over((255, 255, 255, 0.5), (0, 0, 0)), (127.5, 127.5, 127.5))

    def test_parse_formats(self):
        self.assertEqual(parse_color("#fff"), (255, 255, 255, 1.0))
        self.assertEqual(parse_color("#0a0820"), (10, 8, 32, 1.0))
        self.assertEqual(parse_color("rgba(20, 15, 45, 0.6)"), (20.0, 15.0, 45.0, 0.6))


class TokensParseTests(unittest.TestCase):
    def test_parse_finds_all_three_blocks(self):
        dark, light, media = parse_colors()
        self.assertIn("--text-primary", dark)
        self.assertIn("--text-primary", light)
        self.assertIsNotNone(media)

    def test_light_media_block_matches_light_theme(self):
        # 「跟随系统浅色」那份是手抄的一模一样的值，两份漂移了就会出现手动浅色和自动浅色不一致
        _dark, light, media = parse_colors()
        self.assertEqual(light, media)

    def test_light_overrides_cover_every_text_and_fill_token(self):
        # 浅色没覆盖的变量会静默继承深色值（比如白字在白底上），所以文字/填充类必须逐个覆盖
        dark, light, _ = parse_colors()
        needed = {t for grp in (TEXT_PAIRS, GRAPHIC_PAIRS, DIM_PAIRS) for _c, t, _b, _n, _d in grp}
        needed |= {"--bg-base", "--bg-surface", "--bg-elevated", "--bg-sunken", "--bg-solid", "--surface-hover"}
        self.assertEqual(sorted(t for t in needed if t not in light), [])

    def test_rise_fall_hues_not_swapped(self):
        # A 股红涨绿跌：--red 必须偏红(色相<20°或>340°)、--green 必须偏绿(90°~160°)，两套主题都是
        dark, light, _ = parse_colors()
        for name, vs in (("深色", dark), ("浅色", {**dark, **light})):
            for tok, lo, hi in (("--red", -20, 20), ("--red-fill", -20, 20), ("--green", 90, 160),
                                ("--green-fill", 90, 160)):
                r, g, b, _ = parse_color(vs[tok])
                h = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)[0] * 360
                h = h - 360 if h > 180 else h
                self.assertTrue(lo <= h <= hi, f"{name} {tok} 色相 {h:.0f}° 不在 [{lo}, {hi}]")


class ContrastTests(unittest.TestCase):
    def test_all_combinations_meet_threshold(self):
        bad = [f"{r['theme']} {r['fg']}({r['fg_hex']}) on {r['bg']}({r['bg_hex']}): {r['ratio']:.2f} < {r['need']}"
               for r in all_rows() if r["ratio"] < r["need"]]
        self.assertEqual(bad, [], "对比度不达标：\n" + "\n".join(bad))

    def test_checker_catches_a_bad_token(self):
        # 防「检查器永远绿」：造一份把 --text-secondary 换成浅灰的浅色主题，必须报不达标
        import tempfile
        css = TOKENS.read_text(encoding="utf-8").replace(
            "--text-secondary: #4a4566;", "--text-secondary: #bbbbbb;", 1)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "tokens.css"
            p.write_text(css, encoding="utf-8")
            fails = [r for r in all_rows(p) if r["ratio"] < r["need"] and r["fg"] == "--text-secondary"]
        self.assertTrue(fails)

    def test_suggest_keeps_hue_and_passes(self):
        bg = (255, 255, 255)
        fixed = suggest("#aaaaaa", bg, AA_TEXT, -1)
        self.assertGreaterEqual(contrast(parse_color(fixed), bg), AA_TEXT)
        # 灰色没有色相；再用有色相的例子验证色相不变
        fixed = suggest("#ff8a8a", bg, AA_TEXT, -1)
        r, g, b, _ = parse_color(fixed)
        h0 = colorsys.rgb_to_hls(1.0, 0x8a / 255, 0x8a / 255)[0]
        self.assertAlmostEqual(colorsys.rgb_to_hls(r / 255, g / 255, b / 255)[0], h0, delta=0.01)


if __name__ == "__main__":
    if "--table" in sys.argv or "--suggest" in sys.argv:
        css = Path(sys.argv[sys.argv.index("--css") + 1]) if "--css" in sys.argv else TOKENS
        (print_table if "--table" in sys.argv else print_suggestions)(css)
    else:
        unittest.main()
