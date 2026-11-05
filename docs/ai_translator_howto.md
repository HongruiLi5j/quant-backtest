# AI 想法翻译（实验）操作手册

更新日期：2026-10-21，T001 S11。本文按当前脚本实现整理，是设计文档附录 A 的正式操作版；设计中的后台卡片、影子模式和日志清理工具尚未实现。已批准的闸门白名单与其他实现细节见 [核查笔记](ai_translator_notes.md) 第 9–15 节。

功能只在本机把中文或英文想法翻译成配置，经过预检、校验器闸门和一致性比对，供人逐项确认、复制 JSON。确认只记录事件，不运行回测。没有提示也不保证配置符合原意。

## 1. 目录与当前验收边界

所有命令都在 `quant-backtest` 目录执行，使用网站的 `.venv/bin/python`，代码块没有 shell 注释。进入目录：

```bash
cd ~/Documents/Projects/quant-backtest
```

| 对象 | 位置或含义 |
|---|---|
| 网站模块 / 页面 | `src/ai_translator/` / `dashboard/ai_translator.js`，本机路由 `http://127.0.0.1:8765/#/translator` |
| 开关和运行配置 | `data/ai_translator.json` |
| 独立模型项目 | `~/Documents/Projects/ai_layer`，worker 固定使用该项目的 `.venv/bin/python`，网站进程不加载 MLX |
| 模型登记与当前指针 | 模型项目的 `MODEL_REGISTRY.json`，`versions` 保存登记，`active_version` 决定在用版本，`history` 保存切换历史 |
| 校验器源码目录 | `~/Documents/Projects/regime-statarb/src`，通过 `statarb_src_dir` 配置，只读使用 |

缺少配置时默认关闭；`ai_layer_dir`、`statarb_src_dir` 默认为空，不会自动猜外部目录。现有本机配置可能已经开启：S7b 所有者验收时是开启状态，当前状态以查询结果为准。S10 只写文档，不改变开关。

S7b 已验收 `run2_best` 的登记、真实模型冒烟、切换、浏览器拒绝路径，以及空闲 120 秒后 worker 回收。浏览器两次真实翻译都被闸门拒绝，**合法配置的“翻译→确认→复制”浏览器成功路径尚未走通**，不能把管道通过当成模型质量通过。

## 2. 一键 on / off / status

首次开启时显式配置两个外部项目的位置：

```bash
.venv/bin/python -B scripts/ai_translator_switch.py on --ai-layer-dir "$HOME/Documents/Projects/ai_layer" --statarb-src-dir "$HOME/Documents/Projects/regime-statarb/src"
```

路径已经保存后，再次开启：

```bash
.venv/bin/python -B scripts/ai_translator_switch.py on
```

关闭和查看配置开关：

```bash
.venv/bin/python -B scripts/ai_translator_switch.py off
.venv/bin/python -B scripts/ai_translator_switch.py status
```

`on` 和 `off` 原子保存配置并写开关审计，未指定 `--backend` 时后端设为 `local_lora`（API 模式见第 12 节）。`status` 只读取配置并打印 `enabled`、`backend`、`problem`，不会加载模型，也不检查模型是否可用。`enabled=true` 只说明开关开着。缺配置时显示关闭；配置损坏时显示 `CONFIG_INVALID`，`on` 拒绝覆盖坏配置，先检查配置；`off` 可以保存关闭配置。

开关只有上述脚本和配置文件入口，**没有 `/admin` 的 AI 卡片**。手动编辑配置也会被运行中的服务检测并审计。不要在配置中放密钥。关闭会触发 worker 回收：运行中的监督者最多约 0.2 秒检测到变化，回收还可能需要后文的退出等待时间。

## 3. 怎么看实际运行状态

在已经登录主站的本机浏览器打开翻译页；开关变化后刷新整个页面，页面不会持续轮询状态。也可在同一浏览器访问 `http://127.0.0.1:8765/api/ai/status`，或在开发者工具 Network 中查看翻译页加载时的这个响应。无需复制、打印或手动拼接登录令牌。

状态接口不加载模型。除 `enabled`、`backend`、`problem` 外，查看 `state`、`model.version`、`model.grade`、`model.adapter_sha256_short`、`limits.max_input_chars` 和 `limits.remaining_today`。当前 API 固定标注实验功能和实验等级；不是上线质量认证。

| state | 意义 |
|---|---|
| `disabled` | 功能关闭，包括配置损坏时的安全回退 |
| `unavailable` | 开关开着，但模型登记、路径、哈希或后端等不可用，查看 `problem` |
| `idle` | 可以接收请求，目前没有已加载 worker；下一次翻译懒加载 |
| `loading` / `ready` / `busy` | 正在加载 / 已加载待用 / 正在处理一个请求 |
| `cooldown` | 断路器打开，暂停启动模型，查看 `BREAKER_OPEN` |

请求只允许无代理头的本机主站登录会话，分享链接不可用。401 先检查本机主站会话；隧道或带代理头的请求返回 404，不能拿分享入口判断本机服务故障。入口有每 60 秒最多 10 次的限制，状态查询也计入，避免反复刷新。默认输入上限 500 字、每日翻译上限 200 次，每日计数在内存，服务重启会清零。

## 4. 切换模型：先关闭，再试运行，再正式切换

以下是对**已经登记**的 `run2_best` 进行操作的完整命令。切换脚本必须显式传 `--ai-layer-dir`，不会从网站配置中替你补这个必填参数；显式传校验器源码位置可避免遗漏闸门配置。

```bash
.venv/bin/python -B scripts/ai_translator_switch.py off
.venv/bin/python -B scripts/set_translator_model.py run2_best --ai-layer-dir "$HOME/Documents/Projects/ai_layer" --statarb-src-dir "$HOME/Documents/Projects/regime-statarb/src" --dry-run
.venv/bin/python -B scripts/set_translator_model.py run2_best --ai-layer-dir "$HOME/Documents/Projects/ai_layer" --statarb-src-dir "$HOME/Documents/Projects/regime-statarb/src"
```

等待关闭后的 worker 回收完再运行切换；若报告 `LOCKED`，稍候重试，不能删除锁文件绕过。切换先拿共享 worker 锁和登记写锁，校验模型资产哈希、评估引用行哈希，再加载候选版本运行 10 条冒烟，最后重新核对资产和登记字节。

通过时应看到 `responses`、`parsed`、`gate_calls` 各为 10。冒烟只检查响应、严格 JSON 解析及调用闸门，**不检查输出正确率，也不要求每条配置被闸门判合法**。`--dry-run` 同样会真实加载模型和跑冒烟，但不修改登记文件；它可能生成 worker 状态文件和 stderr 日志，不等于完全不写磁盘。

正式切换成功才原子更新 `active_version`、追加 `history` 并写切换审计；前置校验或冒烟失败时登记字节保持不变。切换脚本不保存网站配置，也不会替你重新打开开关。若配置里的 `expect_model_version` 非空，重新开启前检查它：设为目标版本，或按需要设为 `null` 取消钉死；留着旧值会报 `MODEL_VERSION_MISMATCH`。

确认目标和配置后重新开启、刷新页面：

```bash
.venv/bin/python -B scripts/ai_translator_switch.py on
.venv/bin/python -B scripts/ai_translator_switch.py status
```

再查看运行状态接口的 `model.version`。无需重启网站；版本变化时空闲 worker 被回收，忙碌 worker 在当前请求结束后回收，下次请求加载新版本。切换也可等自然空闲回收后操作，但关闭再切换更容易确认锁的归属。

## 5. 回滚版本

紧急止损先执行 `off`，不必等回滚冒烟完成。回滚也要校验哈希、评估引用并加载模型做冒烟：

```bash
.venv/bin/python -B scripts/ai_translator_switch.py off
.venv/bin/python -B scripts/set_translator_model.py --rollback --dry-run --ai-layer-dir "$HOME/Documents/Projects/ai_layer" --statarb-src-dir "$HOME/Documents/Projects/regime-statarb/src"
.venv/bin/python -B scripts/set_translator_model.py --rollback --ai-layer-dir "$HOME/Documents/Projects/ai_layer" --statarb-src-dir "$HOME/Documents/Projects/regime-statarb/src"
```

`--rollback` 的目标是最后一条 `history` 的 `from`，不是搜索“最近质量更好的版本”。没有历史、或最后一个 `from` 为空时返回 `NO_ROLLBACK_TARGET`。初次从空指针启用唯一版本不构成可回滚的旧版本；重复切换同一版本也可能让回滚目标仍是它。需要指定某个已登记版本时，直接用第 4 节的显式版本命令。成功回滚也追加历史，反复回滚可能在两个版本间往返。

回滚完成后同样检查 `expect_model_version`，决定是否重新 `on` 并核对页面版本。如果回滚不通过，保持关闭，保留日志排查；不要手改哈希让损坏版本通过。

## 6. 登记新版本

训练和一次评估在独立模型项目按其流程完成；本网站不训练、不重新评估已用测试材料，也不修改预注册标准。新版本登记前须已有适配器、基础权重和唯一可定位的评估记录。

下面以未来的 `run3_best` 为示例，**不表示这些资产已经存在**。仅在模型项目确有 `adapters/run3_best`、训练 run_id 为 `run3`、评估 status 为 `run3_evaluation_once` 的记录后复制执行；实际标识不同则先替换相应值。命令在网站目录运行，必填参数全部列出：

```bash
.venv/bin/python -B scripts/register_translator_model.py run3_best adapters/run3_best run3 run3_evaluation_once --ai-layer-dir "$HOME/Documents/Projects/ai_layer" --base-model models/Qwen3-4B-Instruct-2507-4bit --eval-file runs.jsonl
```

四个位置参数依次是版本名、相对模型项目的适配器目录、训练 run_id、评估 status；`--base-model` 和 `--eval-file` 也必须是相对模型项目的路径。登记默认写模型项目的 `MODEL_REGISTRY.json`；重复版本、路径越界或匹配到零条/多条评估行会失败。登记计算适配器目录树、基础 `model.safetensors` 和评估行原始字节（含换行）的 SHA256，只保存评估引用，不修改评估文件。

新增记录固定 `status=experimental`、`meets_launch_gate=false`、`criteria_met={}`，不改变 `active_version`，不自动晋升。质量指标由所有者依据有效的新评估记录判断；登记脚本不会替人判断。只有新测试集上的四项标准均满足，才能由人工决定推荐等级：exact ≥75%、legal ≥90%、8 条该拒绝的至少 6 条合法拒绝且说明非空、中英文 exact 各 ≥65%。这些标准不是冒烟标准，推荐等级也不能取消闸门与用户确认。

登记后先关闭、试运行，再正式切换：

```bash
.venv/bin/python -B scripts/ai_translator_switch.py off
.venv/bin/python -B scripts/set_translator_model.py run3_best --ai-layer-dir "$HOME/Documents/Projects/ai_layer" --statarb-src-dir "$HOME/Documents/Projects/regime-statarb/src" --dry-run
.venv/bin/python -B scripts/set_translator_model.py run3_best --ai-layer-dir "$HOME/Documents/Projects/ai_layer" --statarb-src-dir "$HOME/Documents/Projects/regime-statarb/src"
```

后续按第 4 节检查钉死版本、重新开启、刷新并查看日志。当前没有第二个已验收版本，不在 S10 演练登记或切换新版本。

## 7. 导出人工反馈（S8）

页面点“翻译错了”可附最多 200 字备注，源反馈记为 `pending_review`。导出只在手动运行时发生，与翻译开关无关：

```bash
.venv/bin/python -B scripts/export_ai_feedback.py
```

默认只读 `data/ai_translator/feedback.jsonl` 及同目录 `log_*.jsonl`，输出 `data/ai_translator/review/pending_YYYYMMDD.jsonl`，日期取本机当天。要从另存的数据副本导出可使用 `--data-dir`，该参数替换输入目录和输出目录的共同根，不是指定单个输出文件。

只选待审核反馈，按 `request_id` 保留首次记录及备注，再关联首次翻译日志；不改源字节、权限或反馈状态，不写训练目录。首行是 `export_metadata` 元数据，不计反馈条数。后续每条含想法、原始输出、配置、模型版本和哈希等日志上下文，初始 `reviewed_by_human=false`、`gold=null`、`reviewer_note=""`。缺少翻译上下文也会导出，标记 `missing_context=true`，相关字段为 null；没有待审反馈则只有元数据。

**同一天重跑会原子替换当天导出，并重置人工审核字段，审核前先另存副本。** 人必须独立填写正确 gold 和审核说明，模型输出不能直接当 gold；导出文本可能已脱敏或截断，无法恢复原文。反馈只能经后续人工流程进入训练用途，不能进入任何测试集，当前未实现自动回流。不要把源日志或导出提交到 git。

## 8. 日志位置、权限与保留

| 文件 | 内容与保留行为 |
|---|---|
| `data/ai_translator/log_YYYYMM.jsonl` | 按本机月份追加翻译、确认事件；含 request_id、模型版本/哈希、脱敏想法、输出、闸门与一致性结果 |
| `data/ai_translator/feedback.jsonl` | 追加待审核反馈和备注；不自动删除，不自动改变审核状态 |
| `data/ai_translator/review/pending_YYYYMMDD.jsonl` | S8 人工导出；同日重跑会覆盖，不自动清理 |
| `data/ai_translator/worker.err` | worker 的 stderr；启动时若已有文件大于 1 MiB 则截断，运行期间不是持续限长或按天轮转 |
| `data/admin_audit.jsonl` | 开关、配置变化、模型切换等管理审计；AI 监督者的断路器事件通过审计回调记录 |
| `data/ai_translator/worker.lock` / `worker.pid` | 单 worker 的文件锁 / PID 状态记录，不是反馈或训练材料 |

翻译状态目录和审核目录权限 700，业务日志、反馈、导出和 worker stderr 权限 600；配置原子写入、权限 600。日志只在本机保留，`data/` 不上传。

当前 `log.retention_days` **默认 90 天，但只是配置值，没有实现按期限清理的程序**；日志不会在第 90 天自动删除。反馈与导出也没有自动保留期限或清理机制。设计中的 180 天默认值、人工审核后 365 天和清理工具不能当作已实现功能；实际整理留待所有者另行安排，本手册不提供删除命令。

读取 worker 的最后 80 行：

```bash
tail -n 80 data/ai_translator/worker.err
```

文件尚未创建时说明可能还没有启动过 worker，不代表网站一定故障；文件为空也不能证明加载成功，worker 的通用 fatal 码走协议 stdout，不保证写入 stderr。不要把可能含隐私的完整日志贴到公开位置。

翻译原始模型输出最多保留 4000 字符，反馈备注最多 200 字，另有自由文本脱敏。B12 起脱敏只遮住疑似密钥的那一小段：一是「`token` / `secret` / `password` / `cookie` / `authorization` / `api_key` + 冒号或等号 + 值」的赋值写法（关键词连同值一起遮），二是想法、模型原文、反馈备注里长度 ≥16 的连续字母数字串（含 `_`、`-`）；正常文字里单独出现的这些词和其余内容原样保留。局限：没有冒号/等号、也不足 16 位的短口令（如"我的口令是 abc12"）不会被遮；值以中文开头的赋值不会被遮；16 位以上的长英文单词会被误遮（只影响日志可读性）。`request_id`、哈希等结构化字段不经过长串规则。导出按日志原样复制，不能用于恢复。

## 9. worker 的启动和回收规则

- 仅翻译请求懒启动；状态查询不开模型。每次只处理一个请求，不排队；网站与切换冒烟共用 `worker.lock`，防止并行加载两个模型。
- 默认加载超时 `load_s=120` 秒、单次翻译超时 `request_s=60` 秒、空闲回收 `idle_s=120` 秒，以现有配置为准。模型固定最多生成 420 tokens、温度 0，页面不能改这些参数。
- 空闲超过配置期限会回收，下次翻译重新加载。关闭开关或配置变为不可用时触发回收；active 版本变化时，忙碌请求结束后回收旧 worker。监督者后台检查配置最多约 0.2 秒一次，模型巡检最多约 5 秒一次，不是等下一次 HTTP 请求才处理。
- 所有回收共用流程：发送 shutdown、关闭 stdin、等待最多 5 秒；必要时进程组 SIGTERM 再等最多 3 秒；最后 SIGKILL 并 wait，关闭管道、释放锁并移除 PID 文件。超时、崩溃、协议错误会回收，不复用出错进程。正常空闲回收不算失败。
- 10 分钟内累计三次后端失败会打开 10 分钟断路器，状态为 `cooldown / BREAKER_OPEN`，期间不反复重启模型。应先排查故障，等冷却结束。
- 网站正常退出及 SIGTERM/SIGINT 会回收；父进程死亡或 stdin EOF 时 worker 自行退出。监督者启动时会对遗留 PID 核对脚本身份和独立进程组，确认后清理；无法验证时不盲杀。

`worker.lock` 文件存在不表示锁仍被持有；不能删除锁文件或仅凭 PID 杀进程。关闭后可检查 PID 文件是否被移除，并由所有者在活动监视器核对 worker；PID 文件缺失本身不是“绝无残留”的证明。

## 10. 故障排查

先查配置 `status`，再在本机登录浏览器查看运行状态的 `problem`，最后读取 `worker.err` 和对应月份业务日志。不要为排障跳过闸门、修改模型哈希或读取禁止材料。

| 现象或代码 | 含义与处理 |
|---|---|
| `CONFIG_INVALID` | 配置语法、类型、未知键或数值等不合法，安全回退关闭。按已实现配置结构修复，勿复制设计里的 `shadow` 配置或填密钥；需要紧急关闭可用 `off` |
| `MODEL_UNAVAILABLE` / `REGISTRY_INVALID` | 缺模型位置、登记、active 指针、资产，或登记结构不合法；核对配置路径和正式登记步骤 |
| `MODEL_VERSION_MISMATCH` | `expect_model_version` 和 active 不同；关闭后检查钉死值，不能误以为重新 `on` 就会更新版本钉死 |
| `HASH_MISMATCH` | 登记的适配器目录树或基础 `model.safetensors` 与实际 SHA256 不同（或计算期间权重改变）。拒绝加载是保护行为；保持关闭，核对资产来源，恢复可信资产或通过正规流程切回已登记的可信版本，不能直接改哈希消掉告警 |
| `EVAL_HASH_MISMATCH` | 切换时引用评估行的字节哈希不符；停止切换，查引用与记录，不覆盖原评估记录 |
| `IMPORT_OR_LOAD_FAILED` | worker 协议的通用启动失败码，可能是 ai_lib/MLX/校验器依赖导入、导入白名单/文件访问保护、模型加载失败。网站通常只返回 `TRANSLATOR_BACKEND_FAILED`，切换可能报冒烟失败；此码不会告诉具体异常，先看 stderr，再核对路径、模型项目解释器和依赖，勿认为空 stderr 就加载成功 |
| `LOCKED` / `WORKER_LOCKED_OR_STALE` | 已有 worker/切换持锁，或遗留 PID 身份无法安全确认；先关闭、等回收，不删除锁/PID 记录绕过验证 |
| `TRANSLATOR_TIMEOUT` | 加载或生成超过配置期限，worker 已进入回收流程；查看 stderr 和机器资源，避免连续重试导致发热 |
| `TRANSLATOR_BAD_OUTPUT` | 模型输出不是严格 JSON 对象；多余包装文字、重复键、NaN/Infinity 都不接受，不会自动重试 |
| `OUT_OF_RANGE` 等拒绝码 | 正常业务拒绝，可能来自预检、模型、闸门或一致性检查；查看日志里的 gate errors 与 path，页面当前不展示具体出错字段 |
| `TRANSLATOR_BUSY` / 限速或每日上限 | 一个请求仍在处理或次数已到上限；稍候、减少请求或次日再用，不能通过绕锁并发启动 |
| `TRANSLATOR_BUDGET_EXCEEDED` | API 预算或次数已达上限、剩余额度不足预留本次费用，或账本损坏；见第 12 节，保持限制并核查 |
| 真实数据守卫报告 `data/ai_translator/worker.pid` 变化 | 守卫比较测试开始与结束的文件指纹；运行中的网站 worker 启动、空闲回收或关停也会改变 PID 文件。单凭差异无法确定写入者，不能直接认定是测试写了真实目录，也不能忽略失败；按下述隔离步骤复核 |

运行回归测试前，由所有者或接力器安排无真实翻译请求的维护窗口：先按第 11 节执行 `off`、核对关闭并等待 worker 回收完成，**然后才启动测试进程**。在测试开始后再关开关或等待既有 worker 回收，同样可能改变守卫已记录的基线。测试期间保持关闭，不执行模型切换、登记或反馈导出；其他会写真实数据的定时任务也应由运行者预先安排避开。测试本身仍只使用临时目录；真实文件的前后指纹比较由既有守卫负责。

不要删除 PID 文件、排除该路径、刷新守卫基线或关闭守卫来消除报错。若隔离后仍有差异，应保留完整失败输出并调查写入来源；沙箱内不能核实的进程问题交由接力器在沙箱外复跑，不能声称全套通过。上述步骤是测试运行前提，不需要修改产品代码或测试断言。

当前闸门用模型项目的 Python 启动短命校验子进程，只调用 `validate_document`，不加载模型。批准的导入白名单是 `statarb`、`statarb.schema`、`statarb.ml`、`statarb.trials`；不调用试验登记函数，不读登记数据。不要按设计初稿缩回两模块白名单，否则会误判启动失败。

模型会给未使用的参数填值、编造固定取值之外的数字，已在 S7b 真实验收中遇到，API 提示词明确列出了固定值以降低这类错误，但不能保证语义正确。规则比对也会漏掉中文/英文数字词、相对时间、否定/假设、含糊语义；`default` 只表示没抽取到事实，`unchecked` 表示无法自动核对。必须逐项检查，不能把“无警告”理解为正确。

## 11. 一页关停清单

1. 在网站目录执行关闭并核对配置：

   ```bash
   .venv/bin/python -B scripts/ai_translator_switch.py off
   .venv/bin/python -B scripts/ai_translator_switch.py status
   ```

2. 确认输出 `enabled=false`；在已登录本机浏览器刷新状态接口，确认 `state=disabled`，翻译页刷新后入口隐藏。停止发翻译、确认和反馈请求。
3. 给回收留出检测和 shutdown/TERM 等待时间，核对 PID 记录已移除；由所有者在活动监视器确认没有遗留 worker。不要删除锁文件，不盲杀复用的 PID。若接着运行回归测试，必须等本步完成后再启动测试进程，并保持关闭直至测试结束。
4. 保留当前配置、登记历史、审计和错误日志供排查。只需停用时保持关闭即可；需要版本恢复时走第 5 节，回滚通过后也先检查钉死版本，再决定是否开启。
5. S8 没有自动任务，停用反馈导出就是不再运行导出命令。S9 影子模式未实现，不需要寻找其运行开关。
6. 不 commit、不 push。本地检查点提交含已跟踪的 `app.py.bak_progress`、`daily.py.bak_progress` 等备份，公开前须另行审查、清理检查点中的 `*.bak_progress` 内容；仅写 `.gitignore` 不会从已有提交中移除它们。S10 不清理、不改写提交。

## 12. API 后端（S11）

API 模式仍只向本机开放页面，但**想法文字会发送给 Anthropic**。每次请求只发送固定系统提示词（包括公开 schema 规则与新写示例）和当前想法，以及模型、生成长度和关闭 thinking 的参数；不附行情、日志、模型文件、历史对话或其他本机数据。不要在想法中输入凭据或不愿发送的隐私。输出仍依次经过严格 JSON 解析、同一个校验器闸门、一致性核对及日志，最后由人确认；不会执行回测。

### 准备密钥与控制台上限（由所有者操作）

1. 自行登录 [Anthropic Console](https://console.anthropic.com/)，选择用于此项目的组织/工作区；在 API Keys 页面创建项目专用 API 密钥。不要将值粘贴到聊天、项目配置、脚本、plist、报告或日志。
2. 在控制台的 Settings / Billing / Limits（名称可能随控制台调整）查看可用的月度支出限制，把该工作区的月度支出上限设置为 **$5/月**，保存并重新查看确认。余额充值、自动充值阈值、用量提醒不等于支出上限。若账户界面不提供可设为 $5 的硬上限，先保持功能关闭，由所有者核实可用的支出控制后再开启。
3. 本阶段没有真实联网，未验证账户界面、模型可用性或实际价格，也没有替所有者创建密钥、设置账单或更改运行配置。配置默认模型 ID 按本阶段批准使用 `claude-haiku-5-5`；实际账户不支持时请求会返回通用后端错误，不自动改用其他模型。

在项目终端（zsh）用隐藏输入读入密钥，避免值出现在命令历史或任何文件中；再交给当前登录用户的 launchd 环境并重启网站服务：

```bash
read -r -s 'qb_api_value?Anthropic API credential: '
printf '\n'
export AI_TRANSLATOR_API_KEY="$qb_api_value"
launchctl setenv AI_TRANSLATOR_API_KEY "$qb_api_value"
unset qb_api_value
launchctl kickstart -k gui/$(id -u)/com.quantdaily.server
```

不要把以上变量替换成写有密钥的命令文本，不要运行 `printenv` 或 `launchctl getenv` 打印密钥。`launchctl setenv` 针对当前用户登录会话，已有服务需要重启才读取到；重登或重启机器后需重新隐藏输入。无需在项目或服务 plist 中存密钥。若服务使用另一个用户或 bootstrap 域，应在该服务所属会话由所有者设置，不把凭据复制到文件。

### 切换与配置

```bash
.venv/bin/python -B scripts/ai_translator_switch.py on --backend api_llm --statarb-src-dir "$HOME/Documents/Projects/regime-statarb/src"
.venv/bin/python -B scripts/ai_translator_switch.py status
```

API 模式不要求 `--ai-layer-dir` 或模型登记；校验器子进程使用网站当前 Python，因此网站环境必须有 schema 所依赖的 numpy/pandas。依赖缺失、源码未配置或校验失败时关闭放行，不会绕过闸门。页面实验徽标显示 `api_llm` 与配置的模型名。切换后刷新页面。

`data/ai_translator.json` 的 `api_llm` 配置段支持如下非敏感字段；可以在关闭状态下修改这些字段，**密钥值只能通过环境变量提供**：

```json
{
  "model": "claude-haiku-5-5",
  "max_monthly_usd": 2.00,
  "max_daily_requests": 100,
  "price_input_per_mtok": 0.10,
  "price_output_per_mtok": 0.50,
  "base_url": "https://api.anthropic.com",
  "api_key_env": "AI_TRANSLATOR_API_KEY"
}
```

`api_key_env` 是环境变量名称。月上限范围 $0–$50、日次数 0–10000，零表示禁止请求；单价为每百万 token 的美元估算值，不是已核实的供应商报价，应由所有者按实际账单更新。保留的 `timeout_s` 默认 30 秒，发送时取它与 `timeouts.request_s` 的较小值；输出上限 600 tokens，无自动重试。Haiku 5.5 请求省略 `temperature/top_p/top_k`，显式发送 `thinking: {"type": "disabled"}` 和 `output_config: {"effort": "high"}`，避免依赖默认 thinking 或默认 effort。返回非文本块或 `stop_reason=max_tokens` 仍按坏输出拒绝，不保证重复请求得到相同结果。正式目的地只能是 `https://api.anthropic.com`，禁用代理和重定向；本机 HTTP 地址只为 `QB_TESTING=1` 的测试使用。

查看开关、后端、用量与密钥是否可见：

```bash
.venv/bin/python -B scripts/ai_translator_switch.py status
```

输出的 `api_key_env_set` 只有 `yes/no`，不打印名称对应的值。终端与 launchd 服务环境不同，终端显示 yes 不证明服务已有密钥；服务的 `/api/ai/status` 应显示 `ready`，缺密钥为 `unavailable / TRANSLATOR_UNAVAILABLE`。状态接口新增 `budget`，其中 `used_usd` / `max_monthly_usd` 为本月估算用量/上限，`requests_today` / `max_daily_requests` 为今日请求数/上限，不含密钥。日志每次 API 调用包含 `usage` 的输入、输出 token 数和估算费用；预检直接拒绝不发请求，也不计 API 次数。

用量存储在 `data/ai_translator/usage.json`，权限 600；独立 `usage.lock` 保护跨进程计数，同目录临时文件 fsync 后原子替换。发出前先预记次数与保守最大费用，响应有合法 usage 时按配置单价结算；缺 usage、超时或 HTTP 错误按预记最大值保留。最大估算按提示词与想法 UTF-8 字节数加 4096 输入 token 余量、600 输出 tokens 计。剩余额度不足以覆盖本次最大估算时也会提前拒绝。中断后可能多计，避免漏计；这不是供应商账单。状态查询只读原子快照，在途请求可能显示尚未结算的预留费用，不会因为请求持锁而显示不可用；发请求的预算检查与计数仍持独占锁。

达到预算或 API 日次数上限返回 HTTP 429 / `TRANSLATOR_BUDGET_EXCEEDED`，不发请求；原有入口限速与默认每日 200 次翻译限制仍然生效。账本跨日清零次数、跨月清零费用与次数（按服务本机日期），损坏或无法写入时拒绝，绝不当作零。此时 `budget` 可能为 null，检查状态中的错误码；保留损坏账本由所有者核查，不以删除文件的方式绕过限制。预算账本是新增的本地估算防线，控制台 $5 支出限制仍应单独配置。

关闭 API 或切回本地：

```bash
.venv/bin/python -B scripts/ai_translator_switch.py off --backend api_llm
.venv/bin/python -B scripts/ai_translator_switch.py on --backend local_lora
```

未传 `--backend` 时仍默认 `local_lora`，沿用以前行为；单纯查看 `status` 不改变后端。关闭不清除用量，切换或重启服务也不会清零账本。可在关闭后撤去当前会话凭据：

```bash
launchctl unsetenv AI_TRANSLATOR_API_KEY
unset AI_TRANSLATOR_API_KEY
launchctl kickstart -k gui/$(id -u)/com.quantdaily.server
```

## 13. 运行准确率评测（S12，由所有者手动运行）

S12 只交付脚本与隔离测试，没有运行真实评测。使用第 12 节的网站 `api_llm` 配置；先确认翻译开关已开启、后端为 `api_llm`、校验器路径已配置。**运行期间不要同时使用翻译页面，也不要启动另一份评测**。脚本保留网站入口的限速，两条用例之间等待至少 6.1 秒，40 条用例约需数分钟。

在项目目录运行以下一条命令。密钥只临时传给该命令的进程，脚本自己不调用 `launchctl`，不显示或保存密钥；不要把变量替换成真实密钥值，不要把值粘贴到聊天或文件。如果第 12 节修改了 `api_key_env`，应相应改为该环境变量名。

```bash
AI_TRANSLATOR_API_KEY="$(launchctl getenv AI_TRANSLATOR_API_KEY)" .venv/bin/python -B scripts/run_translator_eval.py
```

默认读取受保护的 `tests/ai_translator_eval_cases.json`，只读且不补写用例。`ambiguous` 项从评分和用例数量检查中剔除，报告单列其 id；剩余用例超过调用上限时整次拒绝。**硬上限最多 50 次调用**，只能通过 `--max-calls N` 调低（1–50）；可用 `--cases 路径` 选择其他公开用例文件。预检拒绝不会请求模型，也不计 API 调用。预算拒绝立即停止全部后续用例；达到硬上限也停止，无自动重试。

按已有用量约 **$0.0003/次**，50 次约 $0.015，保守预计共约 **$0.02**；实际报告费用取自后端返回的 `usage`，按当前配置单价计算，是估算值。没有 usage 或通信失败时，后端可能保留较高的预留费用，报告会标记 `estimated_usage_rows`。评测与网站共用 `data/ai_translator/usage.json`，月度/每日限制与发送前预留均照常生效，不清空或另建预算。

终端显示简洁汇总及失败/未评测清单；完整 JSON 在 `data/ai_translator/eval/eval_YYYYMMDD_HHMMSS.json`（目录 700、文件 600，同秒文件已存在时拒绝覆盖）。评测日志接收器只保留指标需要的用量、闸门结果和规则标记，不保存原始模型输出全文，不写网站的 `log_*.jsonl` 或 `feedback.jsonl`，不会混入反馈导出。

报告包括按 category/language 分组的数据和逐条结果。`false_accept` 是该拒绝却最终返回合法配置；`silent_alteration` 是该接受且返回合法配置，但指定字段不一致，逐字段列出期望值与实际值。反事实一致性拦截单独标成 `counterfactual`/`consistency`，`gate_caught` 只统计最初的非法配置被闸门兜住。接受通过率与字段全对率均以已评测 accept 用例数为分母，正确拒绝率与拒绝码匹配率均以已评测 reject 用例数为分母；基础设施错误不算正确拒绝，分母为零显示 N/A。中止后的剩余用例标记 `not_evaluated`，不会混入分母。缺密钥在发请求前退出，错误不含任何值；完成退出码 0，中途因预算/调用上限停止为 2，启动或写文件失败为 1。

本阶段隔离回归命令如下；只使用临时目录、临时 schema 和本机假 HTTP 服务器，不需真实密钥。若沙箱禁止绑定本机端口，需由接力器在沙箱外重跑，不能据内存传输替身宣称 HTTP 集成已通过。

```bash
.venv/bin/python -B tests/test_ai_translator.py --s12-only
```

## S13：拒绝与友好追问

翻译页现在有三种结果：可核对并确认的配置、带原因码的拒绝、需要补充信息的追问。问候或“买还是卖”等意图不明的输入可收到追问；只要求生成配置、给了市场/日期/成本却没说信号，应收到 `MISSING_REQUIRED`。请补充策略或信号后手动再次翻译，页面不会自动调用模型。追问页提供两个固定示例按钮，点击只填入想法，不自动翻译；追问与拒绝都不能确认，也不会执行回测。

拒绝原因保留中英文说明，并可附上模型用你的语言给出的简短原因；闸门或反事实校验能定位时显示具体字段，例如 `market.cost_bp`；拒绝结果里的 `fields` 是字段名列表，`rules` 是与之一一对应的规则码（如 `OUT_OF_RANGE`），只来自固定白名单，不回显校验器自由文本和你的原文。模型给当前策略用不到的参数填了值（如等权策略的 `portfolio.q`、非 ridge 模型的 `signal.alpha`）时，会在交给闸门前自动清除，并出现 `V-UNUSED` 警告，确认时需要勾选已看到提示。消息里的 HTML 只显示为普通文字。`TRANSLATOR_BAD_OUTPUT` 表示输出不是严格的单一配置/拒绝/追问 JSON，或包含未知字段、空消息、超过 200 字的消息等格式问题，不算正确拒绝。原来的配置预检、闸门、一致性核对和确认步骤仍全部保留。

没有新增开关，沿用 `scripts/ai_translator_switch.py on/off`，缺配置仍默认关闭；本阶段不改变本机开关状态、预算或账本，也不自动重试。控制结果写入现有日志格式，保留解析后的控制文档与状态，`raw_output` 留空。

离线回归（临时目录、本机假 HTTP、不需要真实密钥、不访问外网）：

```bash
QB_TESTING=1 .venv/bin/python -B tests/test_ai_translator.py --s13-only
```

若沙箱禁止本机端口绑定，请让接力器在沙箱外重跑上面的命令。可先补做无 socket 诊断，但其通过不代表真实 HTTP 集成已通过：

```bash
QB_TESTING=1 .venv/bin/python -B tests/test_ai_translator.py --s13-no-socket
```

本阶段没有重跑真实评测。真实拒绝码匹配率与 token 用量仍由所有者之后手动评测，沿用最多 50 次调用的硬上限。
