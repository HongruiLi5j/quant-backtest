# vendor · 本地第三方库

真实站的 CSP 是 `script-src 'self'`，不能引 CDN，所以这些库固定版本放在本目录。**不要手改**；升级时重新下载并更新下面的 sha256。

| 库 | 版本 | 本地文件 | 来源 | 用在哪 | sha256 |
|---|---|---|---|---|---|
| lightweight-charts | 5.2.1 | `lightweight-charts.standalone.production.js` | https://cdn.jsdelivr.net/npm/lightweight-charts@5.2.1/dist/lightweight-charts.standalone.production.js | 回测、持仓的 K 线 / 权益曲线（`<script defer src="../vendor/...">`，提供全局 `LightweightCharts`） | `e21cc5caa0226ef30bd8549c50b9ef926615f2a4ee6b4e486353477a55f598cf` |
| marked | 12.0.2 | `marked.esm.js` | https://cdn.jsdelivr.net/npm/marked@12.0.2/lib/marked.esm.js | 报告页 markdown 渲染（`import('../vendor/marked.esm.js')`），输出必须再过 DOMPurify | `288515814e901ea6535521d9fb35a1329455d3d3973de8723db998536f979cf2` |
| DOMPurify | 3.1.6 | `purify.es.mjs` | https://cdn.jsdelivr.net/npm/dompurify@3.1.6/dist/purify.es.mjs | 清洗 marked 输出 | `541c3817132bd3328ae1e585cbef5a9cb14a966276e0c4a0443d4bb3061c9f6d` |
| qrcode-generator | 1.4.4 | `qrcode-generator.esm.js` | https://cdn.jsdelivr.net/npm/qrcode-generator@1.4.4/+esm（jsDelivr 打包成 ESM 的版本） | 分享二维码（目前没有页面用到，留备用） | `28752f5b02662b35c7c6bf31320831ad11392516fe776a42b3cc2347ab754d57` |

说明：

- **字体**：mockup 的 `fonts.css` 只用系统字体栈，不下载 webfont；只有落地页（不复制）用到 Geist Mono，所以没有字体文件。
- 本目录文件是第三方原样代码（已压缩，行很长），不受「单文件 ≤500 行」和「不含 https:// 字符串」两条检查约束；`redline_check` 也要排除 vendor。
- DOMPurify 要用 `DOMPurify.sanitize(html, { RETURN_DOM_FRAGMENT: true })`，再 `append` 进页面，不经过 innerHTML。
