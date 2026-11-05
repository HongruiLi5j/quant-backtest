# A14 静态资源体积体检脚本

`scripts/ui_asset_report.py`：只读扫描 `dashboard/ui`，回答四个问题。不联网、不装依赖、不改任何文件（只有 `--out` / `--json-out` / `--classes-csv` 指定的文件会被写入，而且拒绝写进 ui 目录里面）。

## 怎么运行

```bash
.venv/bin/python scripts/ui_asset_report.py
.venv/bin/python scripts/ui_asset_report.py --out /tmp/ui_report.md --json-out /tmp/ui_report.json --classes-csv /tmp/unused_classes.csv
```

测试：`.venv/bin/python tests/test_ui_asset_report.py`（用临时目录造小 ui 树，最后一项只读扫真实 ui）。

## 它怎么判断"页面加载了什么"

| 来源 | 规则 |
|---|---|
| `<link rel="stylesheet">` | 算这页的 css；css 里的 `@import` 继续跟 |
| `<script src>` | 入口 js；沿 `import ... from`、`export ... from`、`import '...'` 一路展开 |
| `import(\`../pages/${page}.js\`)` | `${...}` 用 `<body data-page>` 代入，得到这页的页面模块（常驻） |
| `import('./debug-fields.js')`（字面量路径） | 当作"按需加载"（`?debug=1` 才会走），单列，不计入常驻 |
| 字符串/属性里的 `xxx.svg` | 图标精灵，算"另有" |
| `new URL('../i18n/${lang}.json', import.meta.url)` | 语言词典：zh 必载，en 只在切换语言时才拉 |

注释里的 import 不算（脚本里有个小词法扫描器先抹掉注释）。

## 类名怎么判断"没用到"

- CSS 里每条规则的选择器取出类名（`:not()` / `:is()` / `:where()` / `:has()` 里的不算"必须出现"）。
- 在所有 html 的属性值、所有 js（不含 `vendor/`）的**字符串字面量**里按整词查找。
- 搜不到但 js 里有以同样前缀（如 `'tone-'`、`` `tone-${x}` ``）或后缀结尾的字符串，标"可能动态拼接"，排在清单最后。
- 逐页估算时，一条规则的每个逗号分隔选择器里只要有一个类名在这页的 html/js 里没出现，或限定了别的 `[data-page=…]`，这个选择器在这页就是死的；整条规则的选择器全死才算死规则。

## 看结果时注意

- 这是"候选清单"，不是"可以删"的结论。服务端接口返回的类名、运行时拼出来的类名搜不到。
- 脚本报"没人引用"的文件，仓库别处（python、测试、文档）可能还提到它，报告里会列出；同名文件不一定是同一个（`scripts/public_allowlist.json` 登记的是 `dashboard/mockups/` 下的同名文件）。
