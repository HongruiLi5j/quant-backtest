# AI 翻译功能接入网站:最小受限版设计方案

- 日期:2026-10-21
- 状态:**只是设计,尚未实现,未经你批准不要动手**
- 读者:实现者(ChatGPT / Codex)。你要能直接照着做,不要重新探索已确认的事实。
- 一句话定位:把 `ai_layer` 的「想法 → 配置 JSON」翻译模型接到网站上,**默认关闭、只限本机、只翻译不执行**,输出必须过校验器闸门和确定性的规则预检,用大白话展示后由用户确认。模型更新有登记、冒烟、切换、回滚的固定通道。

---

## 0. 先看这几条(最重要的约束)

1. **模型不能单独上线**。`ai_layer/README.md` 第 8 节写明:必须有校验器闸门 + 用户确认 + 不自动运行 + 保留日志。本方案把这四条全部做成代码,缺一不可。
2. **模型很弱**。run2_best 在 test2 上:exact 75%(达标,但 Wilson 95% 区间 59.8%–85.8%,没有余量),legal 87.5%(**未达标**,要求 90%),该拒绝的 8 条只拒绝了 1 条(**未达标**,要求 6 条),zh 80% / en 70%(达标)。预注册 4 条只达到 2 条。所以:登记状态是 experimental,页面永远显示「实验版本」。
3. **校验器抓不住「悄悄改成合法值」**(README 第 6 节 B 类 3 条)。第 7 节的预检与一致性比对是对这个缺陷的**部分**缓解,不是根治。文档和页面都不能声称能挡住所有悄悄修改。
4. **test2 已经用掉了**(`runs.jsonl` 最后一行 `test2_evaluation_once`),旧 test 也用掉了。**任何预检规则、冒烟样例、测试用例都不得参照这两份测试集的内容来写或调**;规则只依据 `SYSTEM_PROMPT`、校验器代码和第 7 节的类别描述来设计。
5. 本版**不执行回测、不改策略公式、不改 regime-statarb、不改 ai_layer 现有文件、不改预注册标准、不自动晋升、不 push**。

### 0.1 目录事实(实现者必须用这些路径)

| 对象 | 位置 |
|---|---|
| 网站仓库 | `~/Documents/Projects/quant-backtest`(网站进程用 `.venv/bin/python`,**没有 mlx**) |
| ai_layer(独立项目,不在网站仓库里) | `~/Documents/Projects/ai_layer`(`.venv/bin/python` 有 mlx 0.32.4、mlx_lm 0.32.2) |
| 基础模型 | `ai_layer/models/Qwen3-4B-Instruct-2507-4bit` |
| 适配器 | `ai_layer/adapters/run2_best` |
| 校验器(只读引用) | `~/Documents/Projects/regime-statarb/src/statarb/schema.py`,由 `ai_lib.py` 硬编码路径导入 |

因为 ai_layer 在仓库外,**网站代码不得在 import 时依赖它存在**;ai_layer 的位置由配置文件的 `ai_layer_dir` 给出,默认 `~/Documents/Projects/ai_layer`。

---

## 1. 数据流

```
浏览器输入框
   │  POST /api/ai/translate {"idea": "..."}
   ▼
[0] 访问控制:本机? 开关开? 令牌? 频率? 单飞锁?     (第 2、3 节)
   ▼
[1] 输入清洗:去控制字符、截断检查、长度上限        → 超长直接 400
   ▼
[2] 规则预检(模型之前,确定性,纯 Python)          → 命中"必拒"规则:不调用模型,直接返回拒绝(省算力)
   ▼
[3] 翻译后端 translate(idea) → {config|refusal, backend_id, latency_ms}
   │    local_lora:独立子进程里的 MLX 模型(第 4 节)
   ▼
[4] 解析:从模型输出取出 JSON 对象(见 4.6),取不出 → TRANSLATOR_BAD_OUTPUT
   ▼
[5] 校验器闸门 validate_document(doc)               → 不合法:返回校验器拒绝码,不展示配置
   ▼
[6] 一致性比对(想法 vs 配置,确定性)               → 生成 flags(block / warn)和每个字段的"来源"
   ▼
[7] 大白话展示:字段表 + 来源标记 + 高亮 + "实验功能,结果需确认"
   ▼
[8] 用户点"确认"(有 block 则不能确认;有 warn 必须先勾选"我已看到不一致提示")
   ▼
[9] 记录确认事件到日志。**到此为止,不运行任何回测。** 页面只提供"复制配置 JSON"。
```

关键点:
- 「已确认的配置」**由服务端按 request_id 从内存缓存取**,确认接口**不接受客户端回传配置**,防止客户端篡改后被当成已确认。
- 模型若自己输出拒绝文档(`{"reject":{...}}`),同样要过闸门(它自己也是被 schema 校验的文档),然后原样展示拒绝码和原因。
- 预检命中必拒规则时**不调用模型**:一来省内存和发热,二来这类请求正好是模型最容易悄悄改写的。

---

## 2. 接口设计

### 2.1 路径总表

所有路径在 `/api/ai/` 下。**不在 `_post_engine` 里加**,而是在 `do_POST` / `do_GET` 里**最前面**(紧跟 `_guard()` 和 `/api/admin/` 分支之后、`_share_login` 和 `_share_gate` **之前**)加一个分支:

```
if path.startswith("/api/ai/"):  return self._ai(method, u)
```

原因:`_share_gate` 对外网访客在设了分享密码时会返回 401 页面,和「一律 404」冲突,所以必须在它之前拦。

| 方法 | 路径 | 作用 | 是否启动模型 |
|---|---|---|---|
| GET | `/api/ai/status` | 状态查询(开关、后端、状态、版本、配置问题、今日剩余次数) | 否 |
| POST | `/api/ai/translate` | 翻译 | 是(懒启动) |
| POST | `/api/ai/confirm` | 确认某次翻译结果 | 否 |
| POST | `/api/ai/feedback` | 标记「翻译错了」 | 否 |

### 2.2 统一的入口顺序(`_ai` 方法内部)

```
1. if not admin.is_local_request(self.headers, ALLOWED_HOSTS):
       admin.audit_blocked(u.path); return 404 b"not found" (text/plain)      # 和 _admin_get 完全一致
2. if not self._authed():                                   → 401 b"unauthorized"
3. 如果是 POST:X-Requested-With != "dashboard" → 401;admin.origin_ok(headers, ALLOWED_HOSTS) 为 False → 403
4. 把 (method, path, body_bytes, is_local=True) 交给 src/ai_translator/api.py::handle(),
   它返回 (http_code, dict);app.py 只负责 self._json(obj, code)
```

注意:
- 第 1 步用 `admin.is_local_request`(Host 在白名单且**没有任何代理/隧道头**),不要用 `self._remote()`(它只看 Host 后缀,伪装 Host 的隧道请求会漏过)。
- **所有业务逻辑放在 `src/ai_translator/api.py` 的纯函数里**,`app.py` 只做胶水。理由:`app.py` 在模块加载时会读令牌等真实文件,测试不应 import 它;业务逻辑可在不碰真实数据的情况下充分测试。
- `audit_blocked` 每分钟最多记一条,沿用,不改。
- 速率限制:`members.hit("ai_translate", "local", 10, 60)`(每 60 秒最多 10 次),键固定为 `"local"`,**不记录 IP**。超限返回 429 `TRANSLATOR_RATE_LIMITED`。

### 2.3 `GET /api/ai/status`

```json
{
  "enabled": true,
  "backend": "local_lora",
  "state": "idle",
  "model": {"version": "run2_best", "grade": "experimental", "adapter_sha256_short": "ab12cd34"},
  "experimental": true,
  "problem": null,
  "limits": {"max_input_chars": 500, "remaining_today": 187},
  "notice_key": "ai.notice"
}
```

- `state` ∈ `disabled | unavailable | idle | loading | ready | busy | cooldown`。
- `problem`:配置损坏、registry 无 active、哈希不匹配、断路器打开等,给**人话原因的代码**(如 `CONFIG_INVALID`),前端按代码查 i18n 文案。
- `enabled=false` 时仍返回 200(本机访问),`state="disabled"`,前端据此隐藏输入框并说明如何开启。

### 2.4 `POST /api/ai/translate`

请求:`{"idea": "请回测:……"}`。除 `idea` 外的任何字段**忽略**(尤其不接受客户端传 max_tokens、模型版本、温度)。

成功响应(HTTP 200;"被拒绝"也是 200,因为这是正常业务结果):

```json
{
  "request_id": "a1b2c3d4e5f60718",
  "status": "ok",
  "backend_id": "local_lora:run2_best",
  "model_version": "run2_best",
  "experimental": true,
  "latency_ms": 5400,
  "config": { "...": "通过闸门的完整配置" },
  "refusal": null,
  "view": [
    {"field": "period.end", "label_zh": "评估结束日", "label_en": "Evaluation end",
     "value_text": "2023-12-31", "source": "default", "important": true}
  ],
  "flags": [
    {"rule_id": "C-NUM", "level": "warn", "field": "portfolio.q",
     "idea_says": "20%", "config_has": "10%", "message_key": "ai.flag.mismatch"}
  ],
  "can_confirm": true,
  "needs_ack": true
}
```

- `status` ∈ `ok`(有配置可展示)、`rejected`(有拒绝,无配置)。
- 拒绝时:`config=null`,`refusal={"code":"SEALED_PERIOD","message_zh":"…","message_en":"…","source":"precheck|model|gate|counterfactual"}`,`can_confirm=false`。拒绝文案**优先用校验器/`ai_lib.REJECT_MSG` 里已有的中英文**(只读复制到网站侧常量表,不要 import ai_lib);没有对应码时用上面第 2.6 节的 `TRANSLATOR_*` 文案。
- `view[].source` ∈ `idea`(想法里明确写了且一致)/ `default`(想法没提,是默认值)/ `mismatch`(想法写了但配置不同)/ `unchecked`(规则无法核对)。`important=true` 的字段是 README §8.2 列的:评估区间、成本、embargo、q、训练窗口、行业或成交额子集。
- `flags[].level`:`block`(不可确认)或 `warn`(必须勾选「我已看到不一致提示」才能确认)。

### 2.5 `POST /api/ai/confirm`

请求:`{"request_id": "...", "ack_flags": true}`。

- 服务端取回该 request_id 的结果(内存 LRU,最近 20 条,TTL 30 分钟);找不到/过期 → 410 `TRANSLATOR_REQUEST_EXPIRED`。
- `can_confirm=false` → 409 `TRANSLATOR_NOT_CONFIRMABLE`。
- `needs_ack=true` 但 `ack_flags!=true` → 409 `TRANSLATOR_ACK_REQUIRED`。
- 同一个 request_id 只能确认一次;重复 → 409 `TRANSLATOR_ALREADY_CONFIRMED`。
- 成功:`{"ok": true, "request_id": "...", "config": {...}, "note_key": "ai.confirmed.note"}`,并在日志追加一条 `confirm` 事件。**不触发任何执行。**

### 2.6 `POST /api/ai/feedback`

请求:`{"request_id": "...", "verdict": "wrong", "note": "≤200 字,可空"}`。

- 只接受 `verdict="wrong"`(本版不收"点赞")。request_id 必须仍在内存缓存或日志里能找到;否则 410。
- 追加到 `data/ai_translator/feedback.jsonl`(权限 600),状态 `pending_review`。不做任何自动处理。
- 响应 `{"ok": true}`。

### 2.7 错误码

**拒绝码(沿用校验器 / SYSTEM_PROMPT 的五个,出现在 `refusal.code`,HTTP 200):**
`SEALED_PERIOD`、`OUT_OF_RANGE`、`EMBARGO_VIOLATION`、`UNSUPPORTED_REQUEST`、`MISSING_REQUIRED`。

**接口错误码(新增,统一前缀 `TRANSLATOR_`,出现在 `{"error": "<CODE>", "message": "..."}`):**

| HTTP | 码 | 场景 |
|---|---|---|
| 404 | (纯文本 `not found`) | 非本机请求 |
| 401 | (纯文本 `unauthorized`) | 未带令牌 / 缺 `X-Requested-With` |
| 403 | `TRANSLATOR_BAD_ORIGIN` | Origin 不是本机页面 |
| 400 | `TRANSLATOR_INPUT_INVALID` / `TRANSLATOR_INPUT_TOO_LONG` | 空输入、非字符串、超过 `max_input_chars` |
| 503 | `TRANSLATOR_DISABLED` | 开关关闭,或配置损坏已回退 disabled |
| 503 | `TRANSLATOR_UNAVAILABLE` | 无 active 模型、哈希不匹配、ai_layer 缺失、断路器打开 |
| 429 | `TRANSLATOR_BUSY` | 已有请求在处理(单飞锁) |
| 429 | `TRANSLATOR_RATE_LIMITED` / `TRANSLATOR_DAILY_LIMIT` | 频率或每日上限 |
| 504 | `TRANSLATOR_TIMEOUT` | 子进程超时(已被杀并回收) |
| 502 | `TRANSLATOR_BACKEND_FAILED` | 子进程崩溃 / 协议错乱 |
| 502 | `TRANSLATOR_BAD_OUTPUT` | 模型输出里取不出 JSON 对象 |
| 409/410 | `TRANSLATOR_NOT_CONFIRMABLE` / `_ACK_REQUIRED` / `_ALREADY_CONFIRMED` / `_REQUEST_EXPIRED` | 确认流程 |

**所有错误响应不得包含:模型原始输出、异常堆栈、文件路径、令牌。** 详细原因只进日志。

---

## 3. 权限

1. **默认关闭**:配置文件不存在、或 `enabled` 不是严格的 `true`,一律视为关闭。
2. **一个开关**:`data/ai_translator.json` 的 `"enabled"` 字段。
3. **只允许本机**:第 2.2 节第 1 步。外网请求(包括分享链接访客、带任何代理头的请求)一律 404 + `audit_blocked`。
4. **分享链接访客不可用**:既被第 3 条拦在最前面,也不依赖 `do_POST` 里「`_remote()` 时除 `/api/eng/job` 外都拒」那条(那条放在 `/api/ai/` 分支之后,不应作为这里的唯一防线)。
5. **开关放哪里、怎么开关**:
   - 主入口:后台 `/admin` 页面新增一张「AI 翻译(实验)」卡片,一个开关。对应新接口 `POST /api/admin/ai_translator {"enabled": true|false}`,走现有 `_admin_post` 的认证流程(管理令牌会话、`origin_ok`、`is_local_request`),并把 `"ai_translator_enable"` 加进 `admin.CONFIRM_ACTIONS`(开启要二次确认,关闭不用)。实现者先读 `_admin_post` 看现有写法,**照现有写法加,不要另起一套**。
   - 次入口:手改 `data/ai_translator.json`。允许,但服务端每次读取配置时比较文件哈希,变化了就写一条审计 `ai_translator_config_changed`(说明"改动来源不明,可能是手改")。
6. **审计**:开关每次变化(无论哪个入口)调用 `admin.audit("ai_translator_toggle", {"enabled": ..., "via": "admin|file"}, ok=True)`。关闭时同时**立即回收子进程**。另外这些事件也要审计:`ai_translator_config_invalid`(配置损坏回退 disabled)、`ai_translator_model_switched`(由 `set_translator_model.py` 写入审计需要 admin 模块——脚本可直接调用 `admin.audit`,它只是追加文件)、`ai_translator_breaker_open`(断路器打开)。审计内容**不含想法原文、不含令牌**。

---

## 4. 翻译后端(可替换)

### 4.1 统一接口(网站只依赖它)

文件 `src/ai_translator/backend.py`:

```python
class TranslatorBackend:
    backend_id: str                      # 如 "local_lora:run2_best"
    def status(self) -> dict: ...        # 不触发加载;返回 state/problem
    def translate(self, idea_text: str, *, task: str = "idea_to_config") -> dict: ...
    def close(self) -> None: ...         # 回收资源(子进程)
```

`translate` 返回**统一结构**(网站只认这个):

```python
{
  "config": dict | None,        # 解析出的 JSON 对象(含 reject 文档);取不出则 None
  "refusal": dict | None,       # 后端层面的失败,如 {"code": "TRANSLATOR_TIMEOUT"}
  "raw_output": str,            # 仅供写日志,不返回给前端
  "parse_mode": "strict" | "lenient" | "none",
  "backend_id": str,
  "model_version": str,
  "latency_ms": int,
}
```

- `config` 和 `refusal` 二选一非空。
- 网站其余代码**不 import mlx、不 import ai_lib、不知道子进程的存在**。`api.py` 只通过 `backends.get_backend(cfg)` 取得对象。
- `task` 参数现在只有 `"idea_to_config"` 一个值,为第 12 节留位置。

### 4.2 配置文件 `data/ai_translator.json`

```json
{
  "schema": 1,
  "enabled": false,
  "backend": "disabled",
  "expect_model_version": null,
  "ai_layer_dir": "~/Documents/Projects/ai_layer",
  "limits": {
    "max_input_chars": 500,
    "daily_limit": 200
  },
  "timeouts": {
    "load_s": 120,
    "request_s": 60,
    "idle_s": 300
  },
  "shadow": {"enabled": false, "version": null, "daily_limit": 10},
  "api_llm": {
    "provider": null,
    "model": null,
    "base_url": null,
    "api_key_env": "AI_TRANSLATOR_API_KEY",
    "timeout_s": 30
  },
  "log": {"retention_days": 180}
}
```

规则:
- `backend` ∈ `disabled | local_lora | api_llm`。`api_llm` 本版只是占位:选中它时 `status.state="unavailable"`,`problem="BACKEND_NOT_IMPLEMENTED"`。
- **模型版本不在这里选**:选哪个版本由 `ai_layer/MODEL_REGISTRY.json` 的 `active_version` 决定(第 11 节)。`expect_model_version` 是可选的**钉死**:不为空且与 registry 的 active 不一致 → 拒绝启动并报 `MODEL_VERSION_MISMATCH`,防止切换后配置还指着旧版本。
- **密钥规则**:`api_llm.api_key_env` 只是**环境变量的名字**。以后实现 api_llm 时,密钥只从 `os.environ[api_key_env]` 读;配置文件里出现任何名字含 `key`/`secret`/`token`/`password` 且值不是 `api_key_env` 的字段 → 整份配置判为损坏。密钥**不写进配置、不写进日志、不进审计、不传给本地模型子进程**(见 4.4 的环境变量清洗)。本版不填任何密钥。
- **加载失败不崩**:文件缺失 → 视为 `{"enabled": false, "backend": "disabled"}`(不报错);JSON 语法错误、类型不对、未知 `backend`、数值越界(如 `request_s` ≤ 0、`max_input_chars` > 4000)→ 回退 disabled,`status.problem="CONFIG_INVALID"`,写审计 `ai_translator_config_invalid`(审计里写原因类别,不写文件内容),页面提示「AI 翻译配置有误,已自动关闭,请检查 data/ai_translator.json」。**任何异常都在 `config.load()` 内部吞掉并转成 disabled**,网站启动和请求路径都不得因此抛出。
- 配置写入一律「写临时文件 → `os.replace`」,权限 600。
- 每次请求前 `config.load()` 只做 `stat`(mtime+size)比较,变了才重新读,避免每次都读盘。

### 4.3 `local_lora` 后端:独立子进程

**为什么独立进程**:网站的 `.venv` 没有 mlx;模型 3.28GB 峰值,放进网站进程会拖累整个网站;出事时可以整体杀掉。

**文件:**
- `src/ai_translator/local_lora.py`:监督者(Supervisor),在网站进程里。
- `scripts/ai_translator_worker.py`:子进程入口,**只由 `ai_layer/.venv/bin/python` 运行**。

**启动命令(监督者拼出,不经过 shell):**

```
[ai_layer_dir/.venv/bin/python, "-I", "<repo>/scripts/ai_translator_worker.py",
 "--ai-layer-dir", ai_layer_dir, "--model", "<base_model_dir>", "--adapter", "<adapter_dir>"]
```

`subprocess.Popen(..., stdin=PIPE, stdout=PIPE, stderr=<size-capped 日志文件或 DEVNULL>, start_new_session=True, env=<清洗后的最小环境>, cwd=ai_layer_dir, close_fds=True)`。

- `start_new_session=True`:子进程独占一个**进程组**,杀的时候用 `os.killpg`,连它派生的子孙一起清掉。
- **环境变量清洗**:只传 `PATH`、`HOME`、`LANG`、`LC_ALL`,另加 `HF_HUB_OFFLINE=1`、`TRANSFORMERS_OFFLINE=1`(禁止联网下载)、`TOKENIZERS_PARALLELISM=false`。**不继承** `AI_TRANSLATOR_API_KEY` 或其他环境变量。
- **子进程降优先级**:worker 启动第一件事 `os.setpriority(os.PRIO_PROCESS, 0, 10)`,避免推理时把 Mac 的交互卡死。
- stderr:MLX 会往 stderr 打进度信息。写到 `data/ai_translator/worker.err`(权限 600,启动时若大于 1MB 就截断),**绝不能走 stdout**,否则污染协议。

**协议(JSON 行,一行一个对象,UTF-8,每行 ≤ 64KB):**

```
worker → 监督者:  {"op":"ready","model_dir":"...","adapter_dir":"...","load_s":12.3}
监督者 → worker:  {"op":"translate","id":"r1","idea":"..."}
worker → 监督者:  {"op":"result","id":"r1","ok":true,"raw_output":"...","parse_mode":"strict","obj":{...},"gen_ms":5123}
                  {"op":"result","id":"r1","ok":false,"error":"GEN_FAILED"}
监督者 → worker:  {"op":"shutdown"}
```

- worker 推理方式**严格照抄 evaluate.py**:`model, tok = mlx_lm.load(base_model_dir, adapter_path=adapter_dir)`;`prompt = tok.apply_chat_template(ai_lib.make_messages(idea), add_generation_prompt=True, tokenize=False)`;`text = mlx_lm.generate(model, tok, prompt=prompt, max_tokens=420, sampler=make_sampler(temp=0.0), verbose=False)`。**`max_tokens` 和 `temp` 写死在 worker 里,协议里没有这两个字段。**
- worker 用 `ai_lib.parse_output(text)` 得到 strict / lenient;`obj` 优先 strict,其次 lenient,都没有则 `null`。
- 监督者读取时:每行先限长再 `json.loads`;非 JSON、`id` 不匹配、行过长 → 视为协议错乱,杀进程组,返回 `TRANSLATOR_BACKEND_FAILED`。
- worker **懒加载**:进程启动后先加载模型,加载完发 `ready`;监督者等 `ready` 最多 `load_s` 秒(默认 120)。

**worker 的导入白名单(安全要点):**

`ai_lib.py` 在导入时把 `regime-statarb/src` 加进 `sys.path` 并 `from statarb import schema`。方案要求:

- worker **只允许导入** `ai_lib`(进而导入 `statarb.schema` 这个只读模块)、`mlx_lm`、`mlx.core` 和标准库。
- **绝不能导入** `evaluate`(它引用 `train.log_row`,会写 `runs.jsonl`)、`train`、`gen_data`、`gen_test2`,也不能导入 `statarb` 里的任何登记 / 试验 / 封存相关模块。
- 实现手段:worker 在 `import ai_lib` 之后立刻检查 `sys.modules`:所有以 `statarb` 开头的模块名必须属于 `{"statarb", "statarb.schema"}`;出现其他(例如 `statarb.registry`)→ 向监督者发 `{"op":"fatal","error":"IMPORT_ALLOWLIST"}` 并 `sys.exit(3)`。再检查 `sys.modules` 里没有 `train`、`evaluate`。**这是机械检查,不依赖我对 `statarb/__init__.py` 内容的猜测**(我没读它,见文末不确定项)。
- 校验器 hash:worker 在 `ready` 里附带 `schema.py` 的 sha256;监督者对照 `PREREGISTRATION.md` 记录的 `ef6322a00fc61933e34ba95e53c5c1afd3076b6e078a22e46c53e0f51f79860c`,不一致不阻止运行,但 `status` 里给 `warning: "SCHEMA_CHANGED"`,日志里记下哈希。

**监督者的生命周期规则(防发热 / 孤儿进程,逐条都要有测试):**

1. **同一时间只处理一个请求**:`threading.Lock`,用 `acquire(blocking=False)`;拿不到 → 立即 429 `TRANSLATOR_BUSY`,**不排队**(排队的 HTTP 线程会堆积,模型还在跑)。
2. **全程至多一个 worker**:启动前持有文件锁 `data/ai_translator/worker.lock`(`fcntl.flock(LOCK_EX|LOCK_NB)`),worker 活着期间一直持有(锁文件描述符由监督者持有)。拿不到锁说明别的进程(例如 `set_translator_model.py` 的冒烟测试)在跑模型 → `TRANSLATOR_UNAVAILABLE`。
3. **懒启动**:只有第一次 `translate` 才启动;`status` 永不启动。
4. **空闲回收**:每次请求结束记 `last_used`;一个守护线程每 5 秒检查,`now - last_used > idle_s`(默认 300 秒)且当前没有请求 → 回收。
5. **回收流程**(`_kill()`,所有路径共用):
   - 先发 `{"op":"shutdown"}` 并关闭 stdin,等 5 秒;
   - 仍活着 → `os.killpg(pgid, SIGTERM)`,等 3 秒;
   - 仍活着 → `os.killpg(pgid, SIGKILL)`,`wait()` 收尸;
   - 关闭 stdin/stdout 管道、释放文件锁、删除 pid 文件。
6. **超时**:单次请求超过 `request_s`(默认 60)→ `_kill()` + 返回 504。**超时后不复用该进程**(状态可能乱了)。
7. **断路器**:10 分钟内杀进程 / 崩溃累计 3 次 → 断路器打开 10 分钟,期间一律 `TRANSLATOR_UNAVAILABLE`,`status.state="cooldown"`,写审计。防止"崩溃 → 立刻重启 → 再崩"的发热循环。
8. **父进程死亡的孤儿处理(双保险)**:
   - worker 端:一个线程每秒检查 `os.getppid() != 启动时的 ppid`,或 stdin 读到 EOF,就立刻 `os._exit(0)`;
   - 网站端:启动时检查 `data/ai_translator/worker.pid`,若存在且该 pid 仍活着且命令行包含 `ai_translator_worker.py` → `os.killpg` 清理后删文件。
9. **网站退出**:`atexit` + 对 SIGTERM/SIGINT 的处理里调 `_kill()`(链接原有的信号处理,不要覆盖)。
10. **关开关立即回收**;**registry 的 active 版本变了** → 当前请求结束后回收,下次请求按新版本启动。
11. **每日上限**:`daily_limit`(默认 200)按本地日期计数,存在内存里即可(重启清零,可接受,因为还有频率限制)。
12. **模型输出长度**:`max_tokens=420` 写死,监督者读 stdout 单行上限 64KB,双重限制。
13. 监督者不用 `shell=True`,不拼接用户输入进命令行;想法文本只通过 stdin 的 JSON 传入。

**内存提示**:峰值约 3.28GB,每次只会有一个 worker。冒烟 / 影子模式都遵守同一把文件锁,**不会同时加载两个模型**。

### 4.4 `api_llm` 后端(本版只留接口,不实现)

`src/ai_translator/api_llm.py`:一个 `ApiLlmBackend(TranslatorBackend)`,`translate()` 抛 `NotImplementedError`,`status()` 返回 `unavailable / BACKEND_NOT_IMPLEMENTED`。配置位置见 4.2。以后实现时必须遵守:密钥只从环境变量读;请求体里的想法文本同样要过第 1 节全部闸门;**API 后端的输出和本地模型的输出走完全相同的解析 → 闸门 → 比对流水线**;每次调用的 `backend_id` 写入日志。不要因为"API 模型更强"就放松闸门。

### 4.5 校验器闸门 `src/ai_translator/gate.py`

接口:`validate(doc: dict) -> {"legal": bool, "errors": list[str], "code": str|None}`。

- 唯一依据是 `statarb.schema.validate_document`(`== []` 才算合法,与 `ai_lib.score` 同一口径)。
- **在哪里运行**:实现者第 0 步先读 `schema.py`,判断它是否只依赖标准库:
  - **方案 A(首选,若 schema.py 只用标准库)**:用 `importlib.util.spec_from_file_location("qb_statarb_schema", <path>)` 按文件路径加载 `schema.py` 这**一个**文件,不把 `regime-statarb/src` 加进 `sys.path`,因此无法连带导入 `statarb` 包里其他模块。
  - **方案 B(若 schema.py 依赖第三方库或相对导入)**:用 ai_layer 的 python 以 `-I` 启动一个短命校验子进程(`scripts/ai_gate_validate.py`,stdin 收 JSON、stdout 回结果,**不加载 mlx、不加载模型**),同样做 `sys.modules` 白名单检查。
  - 两种方案对外接口一致。无论哪种,**只读**,不得导入登记 / 试验 / 封存相关模块。
- **拒绝码的提取**:`validate_document` 返回的是错误列表。README 说校验器对 A 类 4 条"给出恰好正确的拒绝码",说明码在错误里以某种方式出现,但**我没读 schema.py,不知道具体格式**。实现者第 0 步必须读清楚并写进 `docs/ai_translator_notes.md`;若错误里没有稳定的码,则在 `gate.py` 里做一个**明确命名的映射层**,映射不到的统一用 `UNSUPPORTED_REQUEST`,并在 notes 里列出映射表供我审。
- 方案 A/B 的选择理由和实测结果由实现者写在 notes 里。

### 4.6 输出解析策略

worker 已给出 `obj` 和 `parse_mode`。网站侧规则:`strict` 和 `lenient` 都接受(与 `ai_lib.parse_output` 一致),但 `parse_mode="lenient"` 时:日志记录,`flags` 里加一条 `warn`(`P-LENIENT`,"模型输出带有多余文字,已提取其中的 JSON")。`obj` 为 `null` → `TRANSLATOR_BAD_OUTPUT`,**不重试**(重试会放大发热;重试策略留给第 12 节)。

---

## 5. 前端

### 5.1 文件与登记
- 新增 `dashboard/ai_translator.js`(**不能写内联脚本**,CSP 是 `script-src 'self'`)。
- 在 `app.py` 第 644 行附近的静态文件白名单元组 `("/app.js", "/engines.js", "/i18n.js", "/analysis.js", "/skin.js")` 里加 `"/ai_translator.js"`;在 `dashboard/index.html` 里加 `<script src="/ai_translator.js"></script>`(`_page()` 的正则会自动补 `?v=` 哈希)。
- 页面是 hash 路由:实现者先看 `app.js` 现有路由注册方式,**照着加一个**路由(建议 `#/translator`),并在导航里加入口。入口默认隐藏,`GET /api/ai/status` 返回 200 且 `enabled` 才显示(外网访客拿到 404 → 入口永远不出现)。
- 所有文案放 `dashboard/i18n.js`,键前缀 `ai.`,中英文各一份。
- 样式追加到 `terminal.css`,复用现有变量,不新增外部资源。
- **渲染必须用 `textContent` / `createElement`,不得用 `innerHTML` 拼接想法或模型输出**(两者都是不可信文本)。

### 5.2 界面元素
1. 顶部常驻横幅:「实验功能,结果需确认」。若 `model.grade="experimental"`,再加一个徽标「实验版本 run2_best」。
2. 输入框(多行,`maxlength` 取自 status 的 `max_input_chars`)+「翻译」按钮。翻译期间按钮禁用并显示「正在翻译…(首次需加载模型,可能要几十秒)」。
3. 结果区:
   - 拒绝:大号拒绝码 + 中/英文原因,无确认按钮;
   - 通过:字段表(列:字段、值、来源),`important` 字段加粗;来源 `mismatch` 红底、`unchecked` 灰色、`default` 浅色小字「默认值」;
   - `flags` 列表置于字段表上方;
   - 「确认」按钮(`can_confirm=false` 时隐藏);`needs_ack` 时先显示复选框「我已看到上面的不一致提示」;
   - 「取消」按钮:清空结果;
   - 「翻译错了」按钮:弹出可选备注(≤200 字),提交 `/api/ai/feedback`;
   - 确认后:显示「已确认。本版不会自动运行回测。」+「复制配置 JSON」按钮。
4. 状态非 `enabled`:显示说明而非输入框(见文案 `ai.off`)。

### 5.3 中英文文案(写进 i18n.js)

| 键 | 中文 | English |
|---|---|---|
| `ai.title` | AI 想法翻译(实验) | AI Idea Translator (experimental) |
| `ai.notice` | 实验功能,结果需确认。模型可能理解错你的意思,请逐项核对后再确认。 | Experimental feature — results must be confirmed. The model can misread your idea; check every field before confirming. |
| `ai.badge.exp` | 实验版本 | Experimental version |
| `ai.input.ph` | 用中文或英文描述一个回测想法,例如:60 日动量,每月调仓 | Describe a backtest idea in Chinese or English, e.g. 60-day momentum, rebalance monthly |
| `ai.btn.translate` | 翻译 | Translate |
| `ai.busy` | 正在翻译…(首次需加载模型,可能要几十秒) | Translating… (the first request loads the model and may take tens of seconds) |
| `ai.btn.confirm` | 确认 | Confirm |
| `ai.btn.cancel` | 取消 | Cancel |
| `ai.btn.wrong` | 翻译错了 | Translation is wrong |
| `ai.btn.copy` | 复制配置 JSON | Copy config JSON |
| `ai.ack` | 我已看到上面的不一致提示 | I have seen the mismatch warnings above |
| `ai.confirmed.note` | 已确认。本版不会自动运行回测。 | Confirmed. This version does not run any backtest automatically. |
| `ai.col.field` / `.value` / `.source` | 字段 / 取值 / 来源 | Field / Value / Source |
| `ai.src.idea` | 你写的 | From your idea |
| `ai.src.default` | 默认值(你没提) | Default (not in your idea) |
| `ai.src.mismatch` | 与你的想法不一致 | Differs from your idea |
| `ai.src.unchecked` | 无法自动核对 | Could not be checked automatically |
| `ai.flag.mismatch` | 你写的是「{idea}」,配置里是「{config}」 | You wrote "{idea}", the config has "{config}" |
| `ai.flag.block` | 此项无法确认,请修改想法后重试 | This cannot be confirmed; edit your idea and try again |
| `ai.note.limits` | 自动核对只能发现一部分不一致,没有提示不代表配置一定正确。 | The automatic check finds only some mismatches; no warning does not prove the config is right. |
| `ai.off` | AI 翻译未开启。可在后台 /admin(仅本机)打开。 | AI translation is off. It can be turned on in /admin (local machine only). |
| `ai.cfg.bad` | AI 翻译配置有误,已自动关闭,请检查 data/ai_translator.json。 | The AI translator config is invalid and the feature was switched off; check data/ai_translator.json. |
| `ai.err.TRANSLATOR_BUSY` | 正在处理上一个请求,请稍后再试。 | Another request is being processed; please try again shortly. |
| `ai.err.TRANSLATOR_TIMEOUT` | 翻译超时,已停止模型进程。 | The translation timed out and the model process was stopped. |
| `ai.err.TRANSLATOR_UNAVAILABLE` | 翻译服务暂不可用。 | The translator is temporarily unavailable. |
| `ai.err.TRANSLATOR_DAILY_LIMIT` | 今日次数已用完。 | Today's limit has been reached. |
| `ai.err.TRANSLATOR_BAD_OUTPUT` | 模型没有给出可解析的结果。 | The model did not return a parsable result. |
| `ai.refusal.SEALED_PERIOD` 等五个 | 取自 `ai_lib.REJECT_MSG`(中英文各一条,只读复制成网站侧常量) | same |

---

## 6. 日志

### 6.1 文件与格式
- 目录 `data/ai_translator/`(权限 700),按月分文件 `log_YYYYMM.jsonl`(权限 600),追加写,写入用 `fcntl.flock` 防并发交错。
- 每次 `translate` 写一条 `event="translate"`;确认写一条 `event="confirm"`;反馈写在 `feedback.jsonl`(权限 600)。**日志只追加,不改写已有行。**

`translate` 记录字段:

```json
{
  "event": "translate",
  "ts": "2026-10-21T10:15:30-07:00",
  "request_id": "a1b2c3d4e5f60718",
  "backend_id": "local_lora:run2_best",
  "model": {
    "version": "run2_best",
    "adapter_path": "adapters/run2_best",
    "adapter_sha256": "…",
    "base_model_sha256": "…",
    "schema_sha256": "…"
  },
  "idea": "(用户输入原文)",
  "precheck": {"blocked": false, "rules": []},
  "raw_output": "(模型原始输出,最多 4000 字符)",
  "parse_mode": "strict",
  "config": {"...": "解析出的对象"},
  "gate": {"legal": true, "errors": [], "code": null},
  "flags": [{"rule_id": "C-NUM", "level": "warn", "field": "portfolio.q"}],
  "status": "ok",
  "latency_ms": 5400
}
```

`confirm` 记录:`{"event":"confirm","ts":…,"request_id":…,"ack_flags":true}`。

### 6.2 必须避免写入的内容
**令牌、Cookie、`Authorization`、任何请求头、IP 地址(含 `CF-Connecting-IP`)、管理令牌、密码、`.secret` 里的任何内容、环境变量**。实现上:日志函数的签名只接受上面这些具名字段,**不接受 `headers` 或 `self`**;不要写"把整个请求 dict 序列化"的代码。测试里用一个带假令牌和假 IP 的请求跑一遍,再扫描日志文件,断言这些字符串都不出现(第 8 节)。

### 6.3 权限与保留
- 创建文件时 `os.open(path, O_WRONLY|O_APPEND|O_CREAT, 0o600)`,每次写入后 `os.chmod(path, 0o600)`(与 `admin.audit` 同做法)。
- `data/` 在 `.gitignore` 里(实现者第 0 步确认,`CLAUDE.md` 规定 `data/` 不上传;**如果 `data/ai_translator*` 没被忽略就先补**)。
- **保留期限建议(需要你决定,见文末)**:翻译日志默认 `log.retention_days=180`;反馈文件在被人工审核处理之前不删,审核完成后保留 365 天。清理由手动脚本 `scripts/prune_ai_logs.py` 执行(`--dry-run` 默认),**不自动删除**,删除前列出文件和大小。想法原文可能含用户隐私,这是需要你确认保留多久的理由。

---

## 7. 已知缺陷的处理:规则预检与一致性比对

### 7.1 背景(来自 README 第 6 节)
8 条该拒绝的请求,模型只拒绝了 1 条;另外 7 条里:4 条输出了不合法配置,**校验器能抓住并给出正确拒绝码**;3 条输出了**合法配置,但悄悄改掉了违规值**,校验器抓不住:

- 想法要求评估到封存期(2024 年及以后),配置里被写成默认的 `2023-12-31`;
- 想法要求 embargo 小于 h+1,配置里被写成合法的 h+1;
- 想法要求数值越界(如 q 取 20%),配置里被写成默认值。

### 7.2 总思路(三层,全部确定性、不调用任何模型)

1. **L1 模型前预检(precheck)**:只看想法文本。命中"必拒"规则 → 不调用模型,直接拒绝。
2. **L2 模型后一致性比对(consistency)**:想法文本里**抽取出的"事实"** 与 **配置里对应字段** 机械比较。
3. **L3 反事实校验(counterfactual)**:对 L2 发现不一致的字段,**把想法里写的值替换进配置**,再交给校验器。若校验器说非法,说明"用户要的值本身就越界/违规",直接拒绝并给出校验器的拒绝码;若合法,说明模型只是改成了另一个合法值,标 warn。

**为什么要 L3(给你面试用的解释)**:合法取值范围只存在于 `schema.py` 里。如果我们在网站里再抄一份范围表,迟早和校验器不一致,而且违背"规则取自研究代码本身,不要另抄"。反事实校验把校验器当成**判定器(oracle)**:不问"范围是多少",只问"用户要的这个值合法吗",范围始终只有一份真相来源。这是一个纯确定性的步骤,不涉及统计推断。

### 7.3 抽取"事实"的规则(中英文,正则 + 小表;**宁可抽不出来,也不要抽错**)

抽取器只在**模式明确**时产出事实 `(field_path, value, span)`;含糊就不产出,该字段在 `view` 里标 `unchecked`。核心模式(实现者按 `SYSTEM_PROMPT` 的字段清单写,不得从 test/test2 的想法文本里抄模式):

| 字段 | 想法里的写法(示例模式) | 事实 |
|---|---|---|
| `period.end` | `YYYY-MM-DD`;`YYYY年底`;`YYYY年6月底`;`end of YYYY`;`mid-YYYY`;`through/until/到/至/评估到 + 日期` | 对应日期 |
| 任意"日期语境里的年份 ≥ 2024" | 年份后接 `年` / `-月` / `/`,或前面是月份英文名、`mid-`、`end of`、`in`、`through`、`until`、`到`、`至`;**排除**年份后面紧跟 `日/天/days/d`(避免把"2048 日训练窗口"当成年份) | "提到封存期日期" |
| 相对当前时间的说法 | `至今`、`到现在`、`截至今天`、`最新数据`、`to date`、`up to now`、`until today`、`through today`、`latest data` | "提到当前时间"(评估区间必然进入封存期) |
| `signal.h` | `未来 N 日`、`预测目标 N 日`、`N-day forward`、`N-day target` | N |
| `signal.embargo` | `embargo N 天/日/days`、`不留 embargo` / `no embargo`(→0) | N |
| `signal.train_window` | `训练窗口 N 日`、`N-day training window` | N |
| `signal.refit_every` | `每 N 日重训`、`refit every N days` | N |
| `signal.alpha` | `alpha=N`、`alpha N` | N |
| `signal.fast/slow` | `N 日/M 日均线`、`N/M-day moving-average` | (N, M) |
| `signal.lookback/skip` | `回看 N 日`、`跳过 M 日`、`lookback N`、`skip M` | N, M |
| `signal.L` | `N 日反转`、`N-day reversal`(仅当 type 为 residual_reversal) | N |
| `portfolio.q` | `前后 N%`、`top/bottom N%`、`十分位`(→0.1) | N/100 |
| `portfolio.band_entry/exit` | `前/后 N% 进场`、`掉出前/后 M% 才离场`(沿用 `render()` 的措辞) | N/100, M/100 |
| `portfolio.rebalance_every` | `每日/日频`→1,`每周/周频`→5,`每月/月频`→21,`每 N 日调仓`→N | 对应值 |
| `market.cost_bp` | `单边 N bp`、`N 个基点`、`N basis points`、`N bp one-way` | N |
| `universe.subset` | 关键词:`成交额较高的一半/liquid half`→liquid_half;`较低的一半/illiquid half`→illiquid_half;`防御型/defensive`→defensive;`周期型/cyclical`→cyclical | 枚举值 |
| `signal.type` | 关键词:`动量/momentum`→momentum;`均线交叉/moving average`→ma_cross;`等权/equal-weight`→equal_weight;`岭回归/ridge`、`HGB/梯度提升`、`MLP/神经网络`→ml;`残差…反转`→residual_reversal;**`反转/reversal` 单独出现不产出事实**(raw 与 residual 容易混) | 枚举值 |
| ML 模型 / 特征 | `岭回归/ridge`、`HGB`、`MLP`;`基础8特征/basic 8`→v1、`扩展21特征/extended 21`→v2 | 枚举值 |

抽取细则:
- 数字归一化只支持阿拉伯数字(含 `5%`、`0.05`、`1,500`);**中文数字("二十""百分之二十")本版不支持**(抽不出就标 `unchecked`,见 7.6)。
- 同一字段抽到**两个不同的值**(如"周频……每月调仓") → 不产出事实,该字段 `unchecked`,并加一条 `warn`(`C-CONFLICT`,"想法里对同一项写了不同的值")。
- 否定语境(`不要`、`不加`、`without`、`no `、`instead of`)紧邻数字时 → 不产出事实,`unchecked`(例外:`no embargo`、`不留 embargo` 已作为专用模式)。

### 7.4 规则清单

| 规则 ID | 层 | 条件 | 动作 | 拒绝码 |
|---|---|---|---|---|
| **P-DATE** | L1 | 想法含"日期语境里的年份 ≥ 2024",或含"相对当前时间的说法" | **block + 不调用模型** | `SEALED_PERIOD` |
| **P-EMBARGO** | L1 | 想法同时抽到 `embargo=N` 和 `h=H`,且 `N < H+1` | **block + 不调用模型** | `EMBARGO_VIOLATION` |
| **C-EMBARGO** | L2 | 抽到 `embargo=N`,配置 `signal.h` 为 H,且 `N < H+1`(想法没写 h 时,用配置里的 h 判断) | block | `EMBARGO_VIOLATION` |
| **C-NUM** | L2 | 对 7.3 表里的数值 / 枚举字段:想法抽到的值 ≠ 配置里的值 | 先交 L3;L3 判非法 → block(码取自校验器);判合法 → **warn** | 来自校验器 |
| **C-END** | L2 | 想法抽到 `period.end ≤ 2023-12-31`,配置 `period.end` 不同 | warn(同样先交 L3) | 来自校验器 |
| **C-TYPE** | L2 | 抽到 `signal.type` 关键词事实,配置 type 不同 | warn | — |
| **C-CONFLICT** | L2 | 同一字段想法里有两个不同的值 | warn | — |
| **P-LENIENT** | L2 | `parse_mode="lenient"` | warn | — |
| **D-DEFAULT**(可选,第二阶段) | L2 | 想法**没提**的字段,配置值必须等于 `SYSTEM_PROMPT` 写明的默认值(market、stats、band 为 null、q 对 equal_weight/ma_cross 为 null 等) | warn | — |

- 每个字段在 `view` 里的 `source`:有事实且一致→`idea`;有事实不一致→`mismatch`;无事实且值等于 SYSTEM_PROMPT 默认→`default`;其余→`unchecked`。
- 所有"必拒"类规则返回的拒绝文案,用 `REJECT_MSG` 对应码的中英文,再附一句**具体定位**("你的想法里写了 2025,评估区间不能晚于 2023-12-31")。
- 规则文件 `src/ai_translator/precheck.py`、`consistency.py`;抽取器 `src/ai_translator/extract.py`;大白话 `src/ai_translator/plainview.py`。**所有规则必须是纯函数**(输入文本/字典,输出结构),可以不加载任何模型地测试。

### 7.5 这套规则能抓到的(如实写)

1. 想法里**明确写出**的封存期日期(`2024-06-30`、`2025年3月`、`mid-2024`、`end of 2024` 等规范写法)→ 在模型之前拒绝。无论模型会不会把它悄悄改成默认值。
2. 想法里**明确写出**"到现在/至今/最新数据"→ 同上。
3. 想法里**明确写出** embargo 天数且小于 h+1 → 拒绝(h 来自想法或配置)。
4. 想法里**明确写出**某个数值/枚举参数(7.3 表里的字段),而配置里是另一个值 → 高亮 `mismatch`;若该值本身被校验器判非法(越界、封存期、embargo 违规)→ 反事实校验升级为拒绝,拒绝码来自校验器,而不是网站自己编。
5. 模型编造不存在的信号类型、写出越界参数、生成没跑过的组合 → 由校验器闸门抓(README 第 6 节 A 类 4 条属于这一类)。

按这个设计,README 第 6 节 B 类的**三类**(封存期日期、embargo<h+1、越界数值)在"想法用了规范写法"时都有对应规则。**这只是按类别推演,不是实测**;没有任何测试集可以用来"证明"覆盖率(test/test2 已用掉,且不得拿来调规则)。

### 7.6 这套规则抓不到的(必须写进页面的 `ai.note.limits`、README、验收报告)

1. **措辞不在模式表里的表达**:"两年后""去年""疫情之后""最近十年""明年初""下个季度"等相对时间;口语化数量("两周调一次仓"→10 日)。
2. **中文数字 / 英文数字词**:"百分之二十""二十日""twenty days"(本版抽不出,标 `unchecked`,不会报警)。
3. **语义层面的替换**:模型把意思相近但不同的策略互换(`reversal_raw` ↔ `residual_reversal`,`pca5` ↔ `sector`),因为"反转"一词不产出事实;`C-TYPE` 只覆盖关键词明确的几种。
4. **否定、假设、对比语境**:"不要加迟滞带""比 5 日更长的回看窗口""instead of ridge"。抽取器遇到会放弃,标 `unchecked`,不报警。
5. **想法没写、模型自己填的字段**:模型填错默认值(例如把成本写成 10bp)而想法没提 → `D-DEFAULT` 只覆盖 SYSTEM_PROMPT 写明默认值的字段,而且是第二阶段的可选项。
6. **同一想法里多个冲突数字**:只会标 `C-CONFLICT`,不会判断用户真正想要哪个。
7. **用户想法本身模糊或自相矛盾**:没有"正确答案"可比对。
8. **非中英文输入、拼写错误、刻意绕过**:没有对抗性测试。
9. **配置合法、与想法一致、但想法本身有问题**:例如"想法"本身不合理但没触犯任何规则。
10. **假阳性**:规则可能把正常请求当成违规(例如日期语境判断误伤)。预检会让用户修改想法重试,这是**有意倾向于保守**的选择。

结论:**这套比对只能降低一部分静默偏离的概率,不能保证。** 它的价值是把"静默偏离"中**机械可检**的那部分变成可见,剩下的靠用户确认这一步和字段表的 `unchecked` 标记提醒。任何文案、README、简历描述都不得写"能挡住所有悄悄修改"。

### 7.7 规则开发约束
- 规则和单元测试用例由实现者**自己新写**,不得查看 `ai_layer/data/` 下的 test/test2 想法文本,不得把 README 第 6 节引用的措辞逐字当测试用例(会变成对已用掉测试集的隐性拟合)。
- 以后若要衡量预检的真实覆盖率,必须在**新封存、从未参与调规则的测试集**上做一次,并预先登记好指标。本版不做、也不声称有这个数字。

---

## 8. 测试清单

新增 `tests/test_ai_translator.py`(风格沿用 `tests/test_admin.py`:普通 `test_*` 函数 + `__main__` 汇总,**只用标准库**),并按 `realdata_guard.py` 的用法在其他导入之后、测试定义之前加一行 `from realdata_guard import ZRealDataUntouchedTests  # noqa: F401`。

### 8.1 守卫扩展(`tests/realdata_guard.py`)
1. `PROTECTED_GLOBS` 增加:`data/ai_translator.json`、`data/ai_translator/**/*`、`data/ai_translator*.lock`、`data/ai_translator*.pid`。
2. 新增 `EXTERNAL_PROTECTED`(绝对路径,不在仓库内):
   - `~/Documents/Projects/ai_layer/runs.jsonl`
   - `~/Documents/Projects/ai_layer/MODEL_REGISTRY.json`
   - `~/Documents/Projects/ai_layer/adapters/run2_best/*`(适配器文件本身)
   - `~/Documents/Projects/regime-statarb/registry/trials.jsonl`
   `fingerprint()` 同时返回这些文件的 sha256(文件不存在也记入"不存在")。**只比较字节哈希,不解析内容、不做任何读取以外的操作**。
3. 守卫自测(`test_guard_detects_changes`)补一个对外部路径的临时目录模拟,验证新增/修改/删除都能检出。

### 8.2 防护栏(tripwire)
`src/ai_translator/paths.py` 里所有真实路径函数在 `os.environ.get("QB_TESTING") == "1"` 时,**若解析后的路径落在真实 `ROOT/data` 或真实 `ai_layer_dir` 内就直接抛异常**;测试文件顶部设置 `os.environ["QB_TESTING"]="1"`,所有被测对象都用 `tempfile.mkdtemp()` 注入路径。这样即使测试写错了,也不会写到真实文件。(`QB_TESTING=1` 时才允许存在 `backend="fake"`,生产环境配置里出现 `fake` 一律判损坏。)

### 8.3 测试用例

| # | 测试 | 要点 |
|---|---|---|
| 1 | 守卫:测试前后真实文件哈希不变 | 包含 8.1 里的全部外部文件(`runs.jsonl`、`MODEL_REGISTRY.json`、`trials.jsonl` 等) |
| 2 | 闸门:合法配置通过 | 用实现者新构造的、`validate_document` 判合法的最小配置 |
| 3 | 闸门:非法配置被拒,返回码 | 构造越界值 / 封存期结束日 / embargo<h+1 / 未知 signal.type,各断言 `legal=False` 与 `code` |
| 4 | 闸门:`api.handle` 对"模型输出非法配置"的假后端,**不得**返回 `config` | 断言响应里 `config` 为空、`can_confirm=False` |
| 5 | 闸门接口只加载 schema 一个模块 | 加载后 `sys.modules` 里 `statarb.*` ⊆ `{statarb, statarb.schema}`(方案 B 在子进程里断言) |
| 6 | 开关关闭 | 配置缺失 / `enabled=false` → `translate`/`confirm`/`feedback` 全返回 503 `TRANSLATOR_DISABLED`;`status` 返回 200 且 `state="disabled"`;**断言没有启动任何子进程** |
| 7 | 配置损坏回退 | 语法错误、`backend` 未知、`request_s=-1`、含密钥字段 → `load()` 不抛异常,结果 `enabled=False`、`problem="CONFIG_INVALID"`,审计被调用一次 |
| 8 | 外网请求 404 | 参数化:Host 为 `x.trycloudflare.com`;Host 为 `127.0.0.1:8765` 但带 `CF-Connecting-IP` / `X-Forwarded-For` / `Via` 等每一个 `PROXY_HEADERS`;Host 不在白名单。四条路径(status / translate / confirm / feedback)× 各情况,全部 404;`audit_blocked` 被调用 |
| 9 | 路由顺序 | 静态检查 `app.py` 源码:`/api/ai/` 分支出现在 `_share_gate(` 调用之前(防止以后重构把顺序调换) |
| 10 | 认证 | 本机但无令牌 → 401;缺 `X-Requested-With` → 401;Origin 非本机 → 403 |
| 11 | 输入校验 | 空串、非字符串、超 `max_input_chars` → 400,**子进程未启动** |
| 12 | 预检:每条规则的正例和反例 | `P-DATE`(规范写法命中;"训练窗口 2048 日"不命中)、`P-EMBARGO`、中英文各至少 3 条;相对时间词命中 |
| 13 | 一致性:`C-NUM` / `C-END` / `C-TYPE` / `C-CONFLICT` | 配置与想法不一致 → 对应 flag 和 `source="mismatch"`;一致 → `idea`;想法没提 → `default`/`unchecked` |
| 14 | 反事实校验 | 想法写了越界值、配置被改成合法默认值 → L3 把想法值写回配置后校验器判非法 → 结果为 block 且码来自校验器;想法写的是合法值但被改 → 只是 warn |
| 15 | 规则的局限(诚实测试) | 写几条**故意抓不到**的例子(中文数字、相对时间"两年后"、否定句),断言结果是 `unchecked` 且**没有** `block`——把"抓不到"固化成文档化的行为,防止以后有人误以为已覆盖 |
| 16 | 日志内容 | 用带假令牌、假 Cookie、假 IP 的请求跑一遍完整流程,扫描日志文本,断言这些字符串、`Cookie`、`Authorization`、IP 形态正则都不出现;文件权限 `0o600`,目录 `0o700` |
| 17 | 日志字段完整 | 必含时间、模型版本、适配器路径与哈希、输入、输出、校验结果;确认后追加 `confirm` 行 |
| 18 | 确认流程 | `block` 时不可确认(409);`warn` 无 `ack_flags` 不可确认;确认接口**忽略**客户端传来的 `config` 字段;重复确认 409;过期 410 |
| 19 | 子进程:正常 | 用**假 worker 脚本**(`tests/fake_translator_worker.py`,标准库,用 `sys.executable` 启动,不加载 mlx)走完 `ready → translate → result` |
| 20 | 子进程:超时与回收 | 假 worker 的 `hang` 模式:请求超时 → 返回 504;之后断言该 pid 已不存在(`os.kill(pid, 0)` 抛 `ProcessLookupError`) |
| 21 | 进程组清理 | 假 worker 的 `spawn_child` 模式:派生孙进程并上报其 pid;`_kill()` 后孙进程也必须消失 |
| 22 | 空闲回收 | `idle_s=1`,请求后等 3 秒,断言 worker 已回收且状态 `idle`;再发请求能重新启动 |
| 23 | 单飞 | 两个线程并发请求;一个成功,另一个立刻得到 429 `TRANSLATOR_BUSY`;断言期间只存在一个 worker |
| 24 | 崩溃与断路器 | 假 worker 的 `crash` 模式连续 3 次 → 断路器打开,第 4 次不启动进程直接 503,`state="cooldown"` |
| 25 | 协议错乱 | 假 worker 输出非 JSON / 超长行 / id 不匹配 → 杀进程组,502 |
| 26 | 父进程死亡 | 启动监督者的**子 python**,杀掉它(`SIGKILL`),断言假 worker 在 ~3 秒内自行退出(worker 端 ppid 检测) |
| 27 | 单 worker 文件锁 | 第二个监督者在锁被占用时不启动进程,返回 `TRANSLATOR_UNAVAILABLE` |
| 28 | 环境清洗 | 假 worker 回报自己的环境变量名;断言不含 `AI_TRANSLATOR_API_KEY` 或测试里放进去的假密钥 |
| 29 | 整条流程(假后端) | `FakeBackend`(实现 `TranslatorBackend`,按预设返回配置 / 拒绝 / 非法配置 / 超时)驱动 `api.handle`:输入 → 预检 → 翻译 → 闸门 → 一致性 → 展示 → 确认,**全程不加载真实模型、不启动真实 mlx**;覆盖 ok / rejected / 预检拦截 / 闸门拦截 / warn 需 ack |
| 30 | registry:只加载 active | 无 `active_version`→ `UNAVAILABLE`;active 的适配器哈希被篡改 → 拒绝启动 `HASH_MISMATCH`;`expect_model_version` 不一致 → 拒绝 |
| 31 | `set_translator_model.py`(用临时 registry 和假 worker) | 哈希错 → 退出码非 0 且 registry **字节不变**;冒烟失败 → 同上;成功 → active 变化、`history` 增加;`--rollback` 回到上一个;文件锁被占用时拒绝 |
| 32 | 冒烟样例互不重叠 | 与 `data/train.jsonl`、`valid.jsonl`、旧 `test` 的想法做归一化后比较(只读 ai_layer 里这几个**非封存**文件),重叠度超过阈值则失败;**不读封存数据** |
| 33 | 导入隔离 | `import src.ai_translator`(主包)后 `sys.modules` 里不得出现 `mlx`、`mlx_lm`、`ai_lib`、`statarb`(网站进程里绝不加载它们;方案 A 的 schema 加载只发生在 gate 首次调用时,并且用的是文件路径加载) |
| 34 | 反馈 | 写入 `feedback.jsonl`(600);只接受 `wrong`;备注截断 200 字 |

运行命令(给你的,不带 `#` 注释):

```
.venv/bin/python tests/test_ai_translator.py
.venv/bin/python tests/test_admin.py
.venv/bin/python tests/test_strategies.py
.venv/bin/python tests/test_basic.py
```

---

## 9. 明确不做的事

- 不接入回测执行:`confirm` 只记录并回传配置,不调用 `jobs`、引擎或任何运行接口。
- 不改策略公式;三引擎(自研 / backtrader / RQAlpha)和 `amibroker/ma_cross_adx_atr.afl` 都不动。
- 不改 `regime-statarb` 任何文件,不导入它的登记 / 试验 / 封存相关模块,不读 `registry/`、封存期数据。
- 不改 `ai_layer` 的**现有**文件(`ai_lib.py`、`evaluate.py`、`train.py`、`PREREGISTRATION.md`、`README.md`、`runs.jsonl`、数据、适配器)。**唯一允许在 ai_layer 新建的文件是 `MODEL_REGISTRY.json`**(第 11 节)。
- 不修改预注册标准;上线门槛是**另写**的流程(11.5),不覆盖预注册。
- 不自动晋升:`experimental → recommended` 只能由人工在满足门槛后手动改;没有任何代码自动改状态。
- 不 push;不 commit(除非你另行要求)。
- 不保存任何账号密码,不接交易接口;网页服务仍然只监听 127.0.0.1。
- 不实现 `api_llm`,不填任何密钥。
- 不在网站进程里加载 mlx。
- 不读取 `.secret/`,不读取 `ai_layer/sealed/` 和任何 test2 文件。

---

## 10. 实施步骤与验收

角色约定:**实现者** = ChatGPT/Codex;**审查者** = Claude Code(读 diff、跑测试、核对守卫);**你** = 最终人工验收。每步实现者先自测,审查者复核,标"你"的步骤由你亲手验证。

| 步骤 | 内容 | 验收标准 | 谁验证 / 怎么验证 |
|---|---|---|---|
| **S0 预检(只读)** | 读 `schema.py`(是否只用标准库、`validate_document` 的返回格式与码的出现方式)、`statarb/__init__.py` 的导入内容(**只看 import 语句**)、`app.py` 的 `do_GET` 结构、`_admin_post`、`.gitignore` 是否忽略 `data/`。产出 `docs/ai_translator_notes.md`(**不得改本设计文档**) | notes 里明确回答:闸门选方案 A 还是 B 及理由;拒绝码如何提取;`/api/ai/` 分支应插入的具体行;`data/ai_translator*` 是否已被忽略 | 审查者:对照源码核对 notes 的每条结论;你:读 notes 并决定第 12 节之外的待定项 |
| **S1 骨架** | `src/ai_translator/`:`paths.py`、`config.py`(加载 / 回退 / 原子写)、`backend.py`、`api_llm.py` 占位;测试 6、7、33(配置与导入隔离部分)、tripwire;扩展 `realdata_guard.py`(8.1) | `config.load()` 对 8.3 的 7 号用例全部不抛异常;主包导入不触发 mlx;守卫测试通过 | 实现者跑 `tests/test_ai_translator.py`;审查者再跑一遍并 `git status` 确认没有写真实 `data/` |
| **S2 纯逻辑** | `extract.py`、`precheck.py`、`consistency.py`、`plainview.py`、`gate.py`;测试 2–5、12–15 | 用例全过;测试 15 证明"抓不到"的例子不被误报为已覆盖;`plainview` 对每个 schema 字段都有中英文标签 | 审查者:随机抽 10 个新写的想法手工演算规则结果;你:读第 7 节与代码里的规则表是否一致 |
| **S3 子进程监督** | `local_lora.py`(Supervisor)、`tests/fake_translator_worker.py`;测试 19–28 | 所有进程相关测试通过;**跑完后 `pgrep -fl fake_translator_worker` 无输出**;连续跑 5 次测试仍无残留 | 审查者:跑测试后 `pgrep -fl "translator_worker"` 查残留;你:Activity Monitor 里无多余 python |
| **S4 日志与 API 层** | `logstore.py`、`api.py`(`handle()`)、FakeBackend、feedback;测试 1、8(api 层)、10、11、16–18、29、34 | 完整流程(假后端)通过;日志无令牌/IP;文件权限 600 | 审查者:grep 日志目录里的假令牌;核对 `api.py` 不含 `headers`/`client_ip` 的落盘 |
| **S5 网站接线 + 后台开关** | `app.py` 加 `/api/ai/` 分支(在 `_share_gate` 之前)、静态白名单登记、`/api/admin/ai_translator`、`CONFIRM_ACTIONS`;测试 9、8(源码顺序) | `tests/test_admin.py`、`test_strategies.py`、`test_basic.py` 仍然通过;本机 `curl` 访问 `/api/ai/status`(默认关闭)返回 `disabled`;模拟 `Host: x.trycloudflare.com` 返回 404 | 实现者:给出 curl 命令与输出;**你**:亲手 curl 一次本机和伪造 Host 的两种请求,并看 `data/admin_audit` 里有 `blocked_remote` |
| **S6 前端** | `dashboard/ai_translator.js`、`i18n.js`、`terminal.css`、`index.html`、路由 | 浏览器无 CSP 报错(控制台);关闭时入口隐藏;用 `QB_TESTING=1` 的 fake 后端能走通"翻译 → 高亮 → 确认";中英文切换文案正确;`textContent` 渲染(往想法里输入 `<img onerror>` 不会执行) | **你**:浏览器手动走一遍(本机),截图给审查者;审查者:grep 确认无 `innerHTML` 拼接不可信文本 |
| **S7 真实模型 + registry + 切换脚本** | `scripts/ai_translator_worker.py`、`ai_layer/MODEL_REGISTRY.json`(登记 run2_best)、`scripts/register_translator_model.py`、`scripts/set_translator_model.py`、冒烟样例;测试 30–32 | 首次真实翻译成功;空闲 `idle_s` 后进程消失;`set_translator_model.py run2_best` 通过冒烟并切换;故意改一个字节的适配器副本能被哈希拦下(**在临时副本上演示,不动真实适配器**) | **你**接电源、关闭其他 App 后手动执行;命令示例见附录 B。审查者核对 `runs.jsonl`、`MODEL_REGISTRY.json`、适配器哈希在守卫里无意外变化(`runs.jsonl` 字节必须不变) |
| **S8 反馈导出(可选)** | `scripts/export_ai_feedback.py`;测试 34 扩展 | 导出文件在 `data/ai_translator/review/`;每条带 `reviewed_by_human:false` | 你:读一份导出样例 |
| **S9 影子模式(可选,需另行批准)** | 11.6 | 默认关闭;每日上限生效;任何时刻只有一个模型在内存 | 你:开 10 次观察内存和发热后再决定是否保留 |
| **S10 收尾** | 更新 `项目交接.md`(新增模块、开关位置、不做的事、如何回滚);`docs/ai_translator_howto.md`(附录 A 的正式版) | 交接文档能让新助手 10 分钟内知道怎么关掉、怎么回滚 | 审查者:按文档独立演练一次"关开关 / 回滚" |

每步完成后:实现者**不得 commit/push**,由你决定;每步结束都附上 `git status` 的输出,确认只改了该步声称的文件。

---

## 11. 更新与迭代通道

### 11.1 `ai_layer/MODEL_REGISTRY.json`

```json
{
  "schema": 1,
  "active_version": null,
  "history": [],
  "versions": [
    {
      "version": "run2_best",
      "adapter_path": "adapters/run2_best",
      "adapter_sha256": "(目录树哈希,见下)",
      "base_model": "models/Qwen3-4B-Instruct-2507-4bit",
      "base_model_weights_sha256": "(model.safetensors 的 sha256;预注册记录开头为 2a73c6c248601ab9…)",
      "train_run_id": "run2",
      "eval_ref": {
        "file": "runs.jsonl",
        "status": "test2_evaluation_once",
        "run_id": "run2",
        "line_sha256": "(runs.jsonl 中该行文本的 sha256,防篡改)"
      },
      "criteria_met": {"exact>=75%": true, "legal>=90%": false, "rejected>=6of8": false, "zh&en>=65%": true},
      "meets_launch_gate": false,
      "status": "experimental",
      "registered_at": "2026-10-21T00:00:00-07:00",
      "note": "预注册 4 条只达到 2/4;只能作为实验版本使用"
    }
  ]
}
```

- **`adapter_sha256`(目录树哈希)**:对适配器目录下所有文件,按相对路径排序,拼接 `相对路径 + "\0" + 文件sha256 + "\n"`,再取 sha256。这样新增 / 删除 / 修改任何文件都会变。
- **与你原要求的一处偏离(需你确认,见文末)**:你写的状态取值是 `experimental / recommended / active`。我把"当前在用"从状态里拆出来,做成顶层 `active_version` 指针,`status` 只表示质量等级 `experimental | recommended`。理由:①"active"和"experimental"不是互斥关系(run2_best 既要是 experimental 又要是当前在用);②单一指针天然保证"最多一个 active",切换和回滚是原子的(改一个字段)。**网站只加载 `active_version` 指向的版本。**
- `eval_ref` 只**引用** `runs.jsonl` 的记录(行内容哈希),**不修改**它。`criteria_met` 与 `meets_launch_gate` 由人工根据该记录填写,脚本只校验"引用行的哈希能对上"。
- registry 的写入一律 `写临时文件 → os.replace`,权限 600,只由 `register_translator_model.py` 和 `set_translator_model.py` 修改,**网站进程只读它**。
- 登记新版本脚本 `scripts/register_translator_model.py <version> <adapter_dir> <train_run_id> <eval_status>`:计算哈希、定位 runs.jsonl 对应行、追加一条 `status="experimental"`、`meets_launch_gate=false` 的记录;**不会**修改 `active_version`、**不会**把状态改成 recommended。

### 11.2 一键切换与回滚:`scripts/set_translator_model.py`

用法(ai_layer 的 python 或网站 `.venv` 均可运行监督者逻辑,冒烟的 worker 子进程固定用 ai_layer 的 python):

```
.venv/bin/python scripts/set_translator_model.py run2_best
.venv/bin/python scripts/set_translator_model.py --rollback
.venv/bin/python scripts/set_translator_model.py run2_best --dry-run
```

执行顺序(任何一步失败 → 退出码非 0,`MODEL_REGISTRY.json` **字节不变**,继续用原版本):

1. 抢 `data/ai_translator/worker.lock`(`LOCK_EX|LOCK_NB`)。抢不到说明网站的 worker 或另一次切换在跑模型 → 提示"先在后台关闭翻译开关或等空闲回收",退出。**保证不会同时加载两个模型。**
2. 读 registry,确认目标版本存在,且记录完整。
3. **哈希检查**:重新计算 `adapter_sha256`(和基础模型权重哈希,基础模型哈希按 mtime+size 缓存),与 registry 登记值比对;不一致 → 失败。
4. 校验 `eval_ref.line_sha256` 与 `runs.jsonl` 当前行一致(只读)。
5. **冒烟测试**(附录 C):启动一个 worker 加载**候选版本**,依次翻译约 10 条样例,检查"管道通、能解析、闸门被调用"(见 11.3)。最长总时限 `10×request_s + load_s`,超时即杀进程组。
6. 全部通过 → **原子地**:`history` 追加 `{from, to, at, via}`,`active_version = 目标`,写 `ai_translator_model_switched` 审计。
7. 若目标版本 `status="experimental"`,终端明确打印"该版本为实验版本,页面会显示'实验版本'"。
8. `--rollback`:把 `active_version` 设为 `history` 里上一个 `from`;**同样要过哈希检查和冒烟**(回滚目标也可能损坏);如果 `history` 为空 → 失败并提示。
9. `--dry-run`:只做 1–5,不写 registry。
10. 网站在下一次请求或 `status` 时发现 registry 的 `active_version`/文件哈希变了,当前请求结束后回收旧 worker,下次请求按新版本启动。

### 11.3 冒烟测试集

- 文件:`tests/ai_translator_smoke.json`,约 10 条,**由实现者自己新写**,不得与训练集、验证集、旧测试集、test2 任何一条重叠(见下"互不重叠的检查方式")。其中 3 条应拒绝。
- **只检查三件事**:① worker 在时限内返回结果(管道通);② 输出能解析成 JSON 对象(能解析);③ 网站的闸门函数对每条都被调用了一次(闸门被调用,用计数器断言)。**不检查内容是否正确,不统计准确率,不用它衡量质量,不得用它调 prompt、调规则或选版本。**
- 通过标准:10/10 有响应,10/10 可解析,闸门调用次数 = 10。
- **互不重叠的检查**:测试 32 用归一化文本 / 字符 n-gram 相似度比较冒烟样例与 `data/train.jsonl`、`valid.jsonl`、旧 `test`。**test2 在 `sealed/` 里,权限 000,我不可读也不应读,所以对 test2 做不了机械比对**;缓解办法是:冒烟样例由实现者独立撰写,不引用 README 第 6 节里的措辞(见附录 C 的建议样例),并在 notes 里如实声明"与 test2 的互不重叠只能靠写法保证,没有机械验证"。如果你想要机械保证,可以由你自己在 ai_layer 里运行一个只输出**哈希**的脚本(不输出文本),把"已用测试集的归一化想法哈希"放进网站可读的位置,再由测试 32 比对哈希。这件事由你决定,见文末。

### 11.4 影子模式与灰度对比(可选,默认关闭,S9)
- 配置 `shadow.enabled=false`(默认),`shadow.version` 指向 registry 里另一个版本,`shadow.daily_limit`(默认 10)。
- **不会同时加载两个模型**:一次 `translate` 的流程是:用 active 版本翻译并**先把结果返回给用户**;之后(在同一把单飞锁内)回收 active worker → 启动 shadow 版本的 worker → 翻译同一条想法 → 记录差异 → **立刻**回收 shadow worker(不等空闲);active worker 在下一次请求时再懒启动。代价是每次影子对比多两次加载,所以每日上限默认只有 10。
- 用户**只看 active 版本的输出**,影子结果只进日志:`event="shadow"`,字段:`request_id`、两个版本号和适配器哈希、字段级差异列表(自己实现一个字段 diff,不 import ai_lib)、两边的闸门结果。
- 更省资源的替代方案(**我更推荐先用它**):离线回放脚本 `scripts/shadow_replay.py <version>`:从日志里取最近 N 条想法,在机器空闲时由你手动运行,用候选版本重新翻译并输出差异报告。不占用网站运行时的内存,也没有"用户等待"的问题。
- 影子对比**只是观察**,不能用来宣称候选版本"更好"(没有标准答案),更不能用日志里的想法去调训练或测试。

### 11.5 上线门槛(写进文档,脚本只校验引用,不代替你判断)
1. **只有在新的封存测试集上达到预注册的 4 条标准,版本才可标记 `recommended`**:exact ≥ 75%;legal ≥ 90%;8 条该拒绝的至少 6 条给出合法拒绝文档且说明非空;zh 和 en 的 exact 各 ≥ 65%。
2. "新的封存测试集"的要求:训练开始**之前**生成并封存、记录哈希;与训练 / 验证 / 之前所有测试集(含旧 test、test2)配置和想法文本都不相交;只在训练结束后评估**一次**;评估结果追加进 `runs.jsonl`(沿用现有的"只追加、守卫禁止二评"机制,由 ai_layer 那边的流程完成,网站方案不碰它)。
3. 建议(**需你决定**):新测试集比 40 条更大(例如 80 条,其中拒绝类 ≥ 16 条),否则 Wilson 区间依旧很宽(run2 的 75% 的 95% 区间是 59.8%–85.8%)。另外可考虑在预注册之外**增加**一项"经过本网站预检 + 闸门后,静默改写合法值的数量"作为附加指标(只作报告,不影响原 4 条)。
4. 未达标的版本只能 `experimental`,可以被 `set_translator_model.py` 启用,页面显示「实验版本」徽标。**`recommended` 状态只能由人工修改 registry**,且要求 `meets_launch_gate=true` 与有效的 `eval_ref`;`set_translator_model.py` **不会**修改 `status`。
5. 即使达到 `recommended`,也不取消闸门、预检和用户确认。

### 11.6 训练数据回流(只设计,不自动执行)
1. 用户在结果页点「翻译错了」→ `POST /api/ai/feedback` → 追加进 `data/ai_translator/feedback.jsonl`:`request_id`、时间、想法原文、模型输出、配置、版本号与适配器哈希、备注、`status="pending_review"`。
2. 导出:`scripts/export_ai_feedback.py` 把 `pending_review` 条目导成 `data/ai_translator/review/pending_YYYYMMDD.jsonl`。导出只是复制并去重,**不改变状态**,不写入任何训练目录。
3. **进入训练集前必须人工审核**:由人逐条写出**正确的标准答案**(gold),在文件里填 `reviewed_by_human: true` 和 `reviewer_note`。**模型自己的输出不能当 gold**(否则是自我强化)。
4. **绝不能进入已用过的两份测试集**,也不能进入未来任何测试集;防线:
   - 反馈数据只能被**训练集生成流程**消费,不能被评估流程消费;
   - 进入训练集前,和已用测试集做**哈希比对**(见 11.3 末尾的哈希黑名单思路,由你在 ai_layer 里生成,只含哈希);
   - 任何由反馈衍生的数据,在新测试集生成前就必须被登记为"训练用",测试集生成时反向排除。
5. 数据合规:想法原文可能含隐私,反馈和日志都只在本机,`data/` 不上传;导出文件同样不得进入 git。

### 11.7 一页「如何更新模型」
见附录 A。

---

## 12. 扩展点(只写一小节,不展开;**你尚未批准,本版不实现**)

以后可以把统一翻译接口扩展成"多任务、多后端的路由层"。当前设计里已经留好的位置:

- `translate(idea_text, *, task="idea_to_config")` 的 `task` 参数目前只有一个取值;路由层只需要在 `backends.get_backend(cfg, task)` 里按 `task` 选后端。
- 返回结构里的 `backend_id` 和日志里的 `backend_id` / `model.version` 已经是统一字段,路由层的"备用后端、降级"可以直接写进同一个日志格式。
- 配置文件里 `backend` 现在是单个字符串,升级时改成 `{"idea_to_config": "local_lora", ...}` 并保留旧字符串的兼容读取(`schema` 版本号已经是 1,升级时 +1)。
- 备用后端(例如 local_lora 不可用时转 api_llm)只需要 `get_backend` 返回有序列表;**无论走哪个后端,都必须走同一条解析 → 闸门 → 比对流水线**。
- 月度预算上限:需要在 `log` 里增加 `cost` 字段并按月汇总;**本版不记录费用,也不实现预算**。
- 以上不属于本版范围,**需要你另行批准**才会设计细节。

---

## 附录 A:如何更新模型(操作说明)

> 前提:你在 `~/Documents/Projects/ai_layer` 里训练;网站在 `~/Documents/Projects/quant-backtest`。所有命令不带 `#` 注释。

1. **训练(ai_layer 里,按预注册流程)**
   训练前先写好预注册修订、生成并封存**新的测试集**(11.5)。训练结束后评估**一次**,结果自动追加进 `runs.jsonl`。
2. **登记**
   ```
   cd ~/Documents/Projects/quant-backtest
   .venv/bin/python scripts/register_translator_model.py run3_best adapters/run3_best run3 test3_evaluation_once
   ```
   新版本以 `experimental` 登记;检查脚本打印的哈希和引用的 `runs.jsonl` 行是否正确。若评估记录达到全部 4 条标准,**由你手动**把 `status` 改成 `recommended` 并把 `meets_launch_gate` 改为 `true`。
3. **冒烟测试(不切换)**
   ```
   .venv/bin/python scripts/set_translator_model.py run3_best --dry-run
   ```
   提示:先在后台把"AI 翻译"开关关掉或等空闲回收,否则会因文件锁被拒绝。
4. **切换**
   ```
   .venv/bin/python scripts/set_translator_model.py run3_best
   ```
   成功后页面显示新版本;若是 experimental,页面带「实验版本」徽标。
5. **观察日志**
   至少观察一周:`data/ai_translator/log_YYYYMM.jsonl` 里看闸门拒绝率、`mismatch` 的比例、用户点"翻译错了"的次数。**这些观察只用来决定是否回滚,不能用来调测试集或预检规则的"成绩"。**
6. **必要时回滚**
   ```
   .venv/bin/python scripts/set_translator_model.py --rollback
   ```
   回滚也会做哈希检查和冒烟。紧急情况下直接在后台 `/admin` 关掉开关,子进程会被立即回收。
7. **清理残留(只在怀疑有孤儿进程时)**
   ```
   pgrep -fl ai_translator_worker
   ```
   正常情况下这条命令没有输出。

---

## 附录 B:真实模型验收命令(S7,你亲手跑)

接上电源、关闭其他占内存的 App。

```
cd ~/Documents/Projects/quant-backtest
.venv/bin/python scripts/set_translator_model.py run2_best --dry-run
.venv/bin/python scripts/set_translator_model.py run2_best
pgrep -fl ai_translator_worker
```

然后在后台把开关打开,在页面里翻译一次,等待 `idle_s`(默认 5 分钟)后再次运行:

```
pgrep -fl ai_translator_worker
```

期望:翻译期间有一个 worker;空闲回收后没有输出。

---

## 附录 C:冒烟样例建议(实现者可以换措辞,但要保持"新写、不重叠")

这是 10 条建议,仅用于"管道通 / 能解析 / 闸门被调用"。**不检查内容对错。**

应接受的 7 条:
1. 帮我测一个 60 日动量,跳过最近 5 日,每周调仓
2. Test a 10-day market-residual reversal, rebalance daily, with 8 bp one-way cost
3. 用岭回归加扩展特征预测未来 10 日收益,训练窗口 756 日
4. Equal-weight baseline restricted to the more liquid half of the stocks
5. 20 日/100 日均线交叉,单边成本 3bp
6. HGB with the basic features, 5-day target, refit every 42 days, evaluate through the end of 2021
7. 对 5 因子 PCA 残差做 15 日反转,只做防御型行业

应拒绝的 3 条:
8. 评估到 2025 年 3 月
9. Ridge model, 5-day target, embargo of 3 days
10. 帮我找一个能赚钱的策略

注:这 10 条是我临时写的,没有对照训练集 / 验证集 / 旧测试集做过比对(我没有读这些文件),也**不可能**对 test2 做比对。实现者必须在测试 32 里做机械比对,有重叠的要重写。

---

## 附录 D:新增 / 修改文件清单(实现时)

新增(网站仓库):
`src/ai_translator/{__init__,paths,config,registry,backend,local_lora,api_llm,gate,extract,precheck,consistency,plainview,logstore,api}.py`、
`scripts/{ai_translator_worker,ai_gate_validate(仅方案B),register_translator_model,set_translator_model,export_ai_feedback,prune_ai_logs,shadow_replay(可选)}.py`、
`dashboard/ai_translator.js`、
`tests/{test_ai_translator.py,fake_translator_worker.py,ai_translator_smoke.json}`、
`docs/{ai_translator_notes.md,ai_translator_howto.md}`。

新增(仓库外):`~/Documents/Projects/ai_layer/MODEL_REGISTRY.json`(唯一一个)。

修改(网站仓库,各自最小改动):
`app.py`(`/api/ai/` 分支、静态白名单一项、`/api/admin/ai_translator`)、`src/admin.py`(`CONFIRM_ACTIONS` 一项,以及可能的 admin 卡片所需最小改动)、`dashboard/{index.html,i18n.js,terminal.css,admin.js}`、`tests/realdata_guard.py`(保护名单)、`项目交接.md`。

**三引擎和 AFL 同步提醒:本功能不涉及策略规则,无需同步。**

---

## 读过的文件(本次设计)

- `app.py`:第 440–530、774–860、986–1030 行;另用 `sed` 看了第 640–650 行(静态白名单位置)。其余部分没有通读。
- `src/admin.py`:`is_local_request`、`origin_ok`、`audit`、`audit_blocked`、`PROXY_HEADERS`(`grep` 取出的片段);`src/members.py`:`hit`。
- `tests/realdata_guard.py`(全文)、`tests/test_admin.py`(前 60 行)。
- `ai_layer/ai_lib.py`(全文)、`ai_layer/evaluate.py`(前 150 行,基本是全文)。
- `ai_layer/PREREGISTRATION.md`(全文)、`ai_layer/README.md`(章节标题 + 第 6–9 节;**第 6 节超出你给的 7–9 节范围**,因为它记录了那 3 条静默修改的样本)。
- `ai_layer/runs.jsonl`:`tail -n 5`,每行截断到 1000 字符。
- 目录列表(只列名字,没进入内容):`~/Documents/Projects`、`ai_layer`、`quant-backtest/docs`、`quant-backtest/scripts`。列 `ai_layer` 时能看到 `sealed`、`data` 目录名,**没有读它们的内容**。
- **没有读**:`.secret/`、`ai_layer/sealed/`、`ai_layer/data/` 下任何文件、regime-statarb 的 `registry/` 和封存期数据、`schema.py`、`statarb/__init__.py`、`_admin_post`、`do_GET`。

## 不确定的地方(需要你决定)

**A. 我没验证的事实(实现者在 S0 必须查清,结果写进 notes):**
1. `validate_document` 返回的错误里,**拒绝码以什么形式出现**?如果没有稳定的码,4.5 说的映射层就要由你批准映射表。
2. `schema.py` 是否只依赖标准库?决定闸门用方案 A(按文件路径加载)还是方案 B(ai_layer python 的短命子进程)。
3. `statarb/__init__.py` 导入了什么?`ai_lib` 的 `from statarb import schema` 可能顺带导入别的模块;worker 的 `sys.modules` 白名单检查会把这种情况直接暴露出来,但我无法预先判断。
4. `do_GET` 的结构、`_admin_post` 的认证写法、`data/` 是否被 `.gitignore` 忽略,我都没读。
5. 首次加载模型要多久我没有数据(只知道 40 条评估约 257 秒,即平均 6.4 秒/条;`load_s=120`、`request_s=60` 是留了余量的猜测)。
6. `ai_layer` 目录在 `~/Documents/Projects/ai_layer`,和你说的"quant-backtest 的 .venv"分开;它是不是 git 仓库、`MODEL_REGISTRY.json` 要不要版本管理,我不知道。

**B. 需要你拍板的决定:**
1. **日志保留期限**:我建议翻译日志 180 天、反馈在人工审核前不删审核后 365 天。想法原文可能含隐私,你要不要缩短,或只存哈希?
2. **registry 状态模型**:我把 `active` 拆成顶层 `active_version` 指针,`status` 只留 `experimental|recommended`(偏离了你原来的三值写法,理由见 11.1)。同意吗?
3. **模糊/相对时间词**("至今""最新数据")是**直接拒绝**还是只 warn?我选了直接拒绝(偏保守,会有误伤)。
4. **`C-NUM` 不一致**默认只 warn、需用户勾选确认;你是否希望对"重要字段"(评估区间、embargo、q、成本)直接 block?
5. **宽松解析**(模型输出带多余文字也取出 JSON)是否接受?我选了接受并加 warn;严格口径(与预注册一致)则直接 `BAD_OUTPUT`。
6. **同步 vs 异步**:现在是同步请求(最长 60 秒占一个 HTTP 线程,单飞锁保护)。要不要改成"提交 → 轮询"?我认为本版不必要。
7. **开关入口**:后台页面卡片(要改 `admin.js`)+ 手改配置文件并审计。是否只保留其中之一?
8. **每日上限 200、频率 10 次/分钟、空闲 300 秒、断路器 3 次/10 分钟**是否合适(尤其发热敏感,是否把空闲改成 120 秒)?
9. **影子模式**:做在线版(S9)还是先只做离线回放脚本?我推荐先离线。
10. **已用测试集的哈希黑名单**:要不要由你在 ai_layer 里生成一份"只含哈希"的名单供反馈回流和冒烟测试比对?这会是你而不是我去读封存数据,所以必须你决定。
11. **新测试集规模和附加指标**(11.5 第 3 点):是否改成 80 条?是否加"静默改写合法值数量"这一附加指标?
12. **代码放在公开仓库里的问题**:翻译模块默认关闭,但代码里会出现对兄弟项目 `ai_layer` 和 `regime-statarb` 的引用(项目会放到 GitHub 上)。要不要把这些路径全部放配置、不在代码里写死,并在 `README` 里注明可选依赖?我建议是。
13. **`confirm` 的含义**:本版只"记录 + 复制配置",没有下一步。你是否希望本版顺便预留一个"以后点这里去回测页预填"的钩子(不执行)?我建议不留,等你批准后再设计。
