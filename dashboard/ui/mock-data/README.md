# mock-data（开发预览用的假数据）

地址栏带 `?mock=1` 时，`shared/api.js` 不请求 `/api/...`，改读这个目录下的 json。

## 文件名规则

去掉 `/api/` 和查询串，其余的 `/` 换成 `_`：

| 接口 | 文件 |
|---|---|
| `GET /api/meta` | `meta.json` |
| `GET /api/summary?market=cn` | `summary.json` |
| `GET /api/market_monitor/quotes` | `market_monitor_quotes.json` |
| `GET /api/holdings?...` | `holdings.json` |

页面也可以显式指定名字：`getJSON(path, { mock: 'today-summary' })`（同一接口不同参数需要不同样本时用）。
POST 在 mock 模式下：有同名文件就返回它，没有就返回 `{ "ok": true, "mock": true }`，不会改任何数据。

## 规则

- 字段名必须和真实接口返回完全一致（读 `app.py` 和 `API_MAP.md` 确认），数字用假的。
- 不放站主的真实持仓金额、代码、账号。
- 每个终端只写自己页面用到的文件，别覆盖别人的；同名文件需要共用时，写进 `~/Documents/AQ-ui-status/requests.md`。
- 预览：`cd dashboard/ui && python3 -m http.server 8767 --bind 127.0.0.1`，打开 `http://127.0.0.1:8767/w/<页面>.html?mock=1`。

现有文件：`meta.json`（给 `_template` 页面用的最小样本）。
