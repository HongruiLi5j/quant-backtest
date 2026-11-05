"""D65：页面模块预加载清单（静态 import 依赖树）。临时目录里的假页面，python tests/test_ui_preload_d65.py"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.ui_preload import modules_for_page  # noqa: E402

FILES = {
    "shared/boot.js": "import { a } from './a.js';\nimport './side.js';\n// import { ghost } from './ghost.js';\n"
                      "/* import x from './ghost2.js' */\nexport async function go(p) { return import(`../pages/${p}.js`); }\n"
                      "if (location.search) import('./debug.js');\n",
    "shared/a.js": "export { b } from './b.js';\nexport const a = 1;\n",
    "shared/b.js": "import { a } from './a.js';\nexport const b = 2;\n",          # 循环依赖
    "shared/side.js": "export const s = 1;\n",
    "shared/debug.js": "export const d = 1;\n",
    "shared/ghost.js": "", "shared/ghost2.js": "",
    "pages/today.js": "import { a } from '../shared/a.js';\nimport { m } from './missing.js';\n",
}
HTML = ('<html><head><script src="../shared/classic.js"></script>'
        '<script type="module" src="../shared/boot.js"></script></head><body class="ws" data-page="today"></body></html>')


def test_graph():
    with tempfile.TemporaryDirectory() as t:
        ui = Path(t)
        for rel, code in FILES.items():
            (ui / rel).parent.mkdir(parents=True, exist_ok=True)
            (ui / rel).write_text(code, encoding="utf-8")
        mods = modules_for_page(ui, "w/today.html", HTML)
        assert mods[0] == "shared/boot.js"
        assert set(mods) == {"shared/boot.js", "shared/a.js", "shared/b.js", "shared/side.js", "pages/today.js"}, mods
        assert len(mods) == len(set(mods))                       # 循环依赖不重复
        (ui / "shared/side.js").unlink()                          # 文件删了：清单里也没有
        assert "shared/side.js" not in modules_for_page(ui, "w/today.html", HTML)
        assert modules_for_page(ui, "w/x.html", "<html><body></body></html>") == []


def test_real_pages_cover_page_module_and_boot():
    ui = ROOT / "dashboard" / "ui"
    for p in ("today", "signals", "backtest", "monitor"):
        mods = modules_for_page(ui, f"w/{p}.html", (ui / "w" / f"{p}.html").read_text(encoding="utf-8"))
        assert "shared/page-boot.js" in mods and f"pages/{p}.js" in mods and "shared/i18n.js" in mods, (p, mods)
        assert not any(m.startswith("vendor/lightweight") for m in mods)   # 普通 <script> 不进模块预加载


if __name__ == "__main__":
    test_graph()
    test_real_pages_cover_page_module_and_boot()
    print("ok: 2 项通过")
