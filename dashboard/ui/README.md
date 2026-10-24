# dashboard/ui · 新界面（quant-ai-dark · Workspace）

旧界面原样保留在 `/`，新界面放在 `/ui/`（上线前用预览服务器看）。设计源在 `dashboard/mockups/`，这里只复制、不改 mockup。

## 目录

| 路径 | 内容 |
|---|---|
| `index.html` | 入口，meta refresh 跳到 `w/today.html` |
| `w/` | 页面 html（只写骨架）。`_template.html` 是样板 |
| `pages/` | 页面逻辑：`pages/<id>.js` 导出 `async function render(root)` |
| `nav.js` | 顶栏和导航清单（每项有新页面地址 `href` 和旧页面地址 `classic`；`BUILT` 清单决定点了去新还是旧） |
| `shared/` | 通用模块：`api.js`（数据层）、`page-boot.js`（页面启动）、`dom.js`、`i18n.js`、`toast.js`、`chart-helpers.js`、`chart-heatmap.js`、`command-palette.js`、`theme-boot.js`、`theme-switcher.js`、`number-ticker.js`、`quota-bar.js`、`custom-cursor.js`；以及通用 css 和 `icons.svg` |
| `theme/` | `tokens.css`（颜色等变量）、`workspace.css`（密度）、`theme.css`（落地页光晕，Workspace 不引用） |
| `vendor/` | 本地第三方库（固定版本，见 `vendor/VERSIONS.md`） |
| `i18n/` | `zh.json` / `en.json` 基础字典；`parts/<终端号>.<语言>.json` 各终端的增量词条 |
| `mock-data/` | `?mock=1` 预览用的假数据，规则见 `mock-data/README.md` |
| `API_MAP.md` | 旧页面 ↔ 新页面 ↔ 接口对照 |

## 预览

    cd <项目目录>/dashboard/ui
    python3 -m http.server 8767 --bind 127.0.0.1

打开 `http://127.0.0.1:8767/w/_template.html?mock=1`。`?mock=1` 读 `mock-data/`；不带 `mock=1` 会请求 `/api/...`，静态预览服务器上会 404，页面显示错误卡片（这是预期行为）。
还可以加 `?state=loading|error|empty` 强制看三种状态。

## 怎么加一个新页面（以 `holdings` 为例）

1. 复制 `w/_template.html` → `w/holdings.html`，改 `<title>`、`<body class="ws" data-page="holdings">`、页头标题的 `data-i18n` 键。
2. 新建 `pages/holdings.js`，写 `export async function render(root) { ... }`：
   - 数据用 `import { getJSON, postJSON } from '../shared/api.js'`，路径写 `/api/...`。
   - 内容用 `el()`（`shared/dom.js`）或 `textContent` 构建，不拼 HTML 字符串；markdown 只能 marked → DOMPurify。
   - `getJSON` 抛的 `ApiError` 不用接，直接让它抛：页面启动器会显示错误卡片，401/403 显示「登录已过期，请从首页重新进入」。
   - 没数据时 `return 'empty'`，会显示空状态。
3. 文案：`t('key')`；新词条写进自己的 `i18n/parts/<终端号>.zh.json` 和 `.en.json`（两边键必须完全一致）。
4. 假数据：`mock-data/<名字>.json`（字段和真实接口一致）。
5. 页面做好后，需要在 `nav.js` 的 `BUILT` 里加上这个 id，才会让导航点进新页面。这是共用文件：**不要自己改**，把需求追加到 `~/Documents/AQ-ui-status/requests.md`，由 U5 统一处理。开发预览（`?mock=1`）时所有导航项都直接指向新页面，不受 `BUILT` 影响。

## 规则（摘要，完整版见 `~/Documents/AQ-ui-migration/COMMON.md`）

- 服务器 CSP：`script-src 'self'`、`connect-src 'self'`。不写行内脚本、行内事件、CDN 地址、meta CSP。
- 单个 js/css 文件 ≤ 500 行（`vendor/` 的第三方文件除外）。
- 快捷键：1–9 切到导航前 9 项，R 刷新当前页，/ 搜索，? 看快捷键。
- 顶栏不要设 `data-mode="workspace"`（那套居中布局是给 10 项导航写的，13 项会压到搜索框）。

## 检查

交付前在项目根目录 grep `dashboard/ui`（排除 `vendor/`）：行内脚本、行内事件、HTML 字符串拼接相关的写法、外部网址、红线字符串都必须为 0。具体的词表见 `~/Documents/AQ-ui-migration/COMMON.md` 的「硬约束」和「红线」；检查时连注释和 md 文件里的字样也会命中，所以文档里也不要写出这些词。
