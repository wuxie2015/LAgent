# Lin Agent

本地运行的个人终端 Agent：通过 Ollama 调用 `gpt-oss:120b-cloud`，支持网页搜索、网页正文读取、本机 Shell、分层持久化记忆和持久化提醒/计划任务。

运行环境：Python 3.10 或更新版本。

## 使用方法

### 1. 准备环境

需要：

- Python 3.10 或更新版本
- 已启动的 Ollama 服务
- Ollama 中可用的 `gpt-oss:120b-cloud`
- Tavily API Key（网页搜索必需）

先确认本机模型：

```bash
python3.10 --version
ollama list
```

输出中应包含 `gpt-oss:120b-cloud`。若 Ollama 服务未运行，请启动 Ollama 桌面应用，或在另一个终端运行：

```bash
ollama serve
```

### 2. 配置 Tavily 搜索

从 [Tavily](https://tavily.com/) 创建 API Key。复制示例配置：

```bash
cp .env.example .env
```

然后在 `.env` 中填入实际密钥：

```env
TAVILY_API_KEY=tvly-你的实际密钥
```

`.env` 已被 Git 忽略。不要把 API Key 发到聊天、写入 `.env.example`，或提交到 GitHub。

默认情况下，Agent 连接 `http://127.0.0.1:11434`，模型为 `gpt-oss:120b-cloud`。你可以在 `.env` 中修改 `OLLAMA_HOST`、`OLLAMA_MODEL` 和 `SEARCH_RESULT_LIMIT`。

安装项目依赖（包含 Tavily HTTPS 证书校验所需的 `certifi`）：

```bash
python3.10 -m pip install -e .
```

### 3. 启动 Agent

在项目根目录执行：

```bash
python3.10 -m src
```

也可以使用启动脚本：

```bash
./run.sh
```

不需要 OpenAI API Key。

### 4. 与 Agent 对话

启动后直接输入需求，例如：

```text
你：搜索今天日本 AI 行业最重要的三条新闻，给出来源并说明它们为什么重要。
```

网页搜索使用 Tavily。Agent 会自动执行搜索、将结果回传模型进行核验；若结果不足以满足原始目标，模型会继续发起更精确的工具调用，直至完成或达到安全上限。

`web_search`只接受`query`，负责找来源；需要正文或源码时使用`web_fetch(url)`。网页读取支持`start_char`与`max_chars`分段，默认3000字符、单次下载最多1MB、最多3次重定向；会标明截断与读取内容哈希，只读取公开HTTP(S)文本，不执行页面指令。超过下载上限时应改选更小的原始文件。

工具调用会先校验必填项、类型、多余字段和声明的枚举/范围。错误返回`error_code`、`retryable`和必要的正确参数定义；不把无效JSON悄悄改成空参数。相同参数错误不重复执行，临时网络错误对同一调用最多重试一次，缺少配置时换搜索词也不会继续请求服务。

最多8轮模型调用中最后一轮用于整理已有证据，工具关闭，历史工具轨迹转换为资料。若模型仍请求工具或没有给出文本，程序返回带来源的部分结果与未完成说明；不会执行第8轮额外工具或只显示一句“达到上限”。这些规则同样用于只读子Agent，其最终输出仍为JSON。

因此不需要手动复制搜索结果，也不需要输入工具命令。最终回答应包含所用来源的 URL。

终端输入启用 Python 自带的 readline/libedit 行编辑，可按完整汉字退格删除、连续删除和移动光标修改；无需新增依赖。修改后重新启动 Agent 生效。

交互过程中，终端保留模型请求、工具调用和子 Agent 状态的简短摘要，不打印完整对话、参数或工具原始输出；即使设置 `MEMORY_DEBUG_CONTENT=true`，执行事件也只显示摘要。最终回答默认简洁说明结果，错误和必要提示仍会显示。

## DDD 分层

```text
src/
├── application/  # 对话用例与 CLI 输入输出
├── domain/       # 模型与工具的业务契约，不依赖具体供应商
├── service/      # Ollama 等基础设施适配器
├── common/       # 配置与通用能力
└── tools/        # 可被 Agent 调用的外部工具（Tavily 搜索、网页读取与 Shell）
```

主助手提示词位于 `resource/prompt/system.md`；事实与任务提取分别使用 `memory_extract.md`、`task_update.md`。

## 命令

- 输入问题：模型按需自动调用工具、检查结果并生成最终回答。
- `/help`：显示可用命令。
- `/reset`：清除短期对话，保留已提取事实和任务；未提取信息不会保留。
- `/exit`：退出。
- `/schedule list|now|history [id]|get <id>|pause|resume|cancel <id>`：查看当前时间、计划任务和执行历史，或管理任务。

### 提醒与计划任务

在对话中可以说“明天下午 3 点提醒我交报告”，或“每周一早上 9 点搜索 AI 新闻并通知我”。Agent 会先查询当前时间及时区，再把相对时间换算为明确时间后创建任务。重复规则支持一次、每天或每周（可指定星期）。任务 ID 和实际时间来自成功的创建回执。

任务默认存于项目根目录 `data/schedules.sqlite3`，时区为 `Asia/Tokyo`，通知渠道为终端 `console`。可在 `.env` 配置 `SCHEDULE_DB`、`SCHEDULE_TIMEZONE`、`SCHEDULE_DEFAULT_CHANNEL`、宽限期和轮询参数。渠道可以是终端、SMTP 邮件或 Webhook：邮件需配置 `SMTP_HOST`、`SMTP_FROM`、`SMTP_TO`，认证凭据可选；Webhook 需配置 HTTPS URL（本机开发可用 localhost HTTP），令牌可选。创建和修改时会检查渠道是否已配置。凭据只放入本机 `.env`，不要放进示例文件或日志。

计划任务由当前 Lin Agent 进程执行，必须让程序保持运行。电脑睡眠或程序退出时任务可能错过；晚于宽限期的任务记为 missed，不会补跑。通知失败会有限重试；远端已收到但确认响应丢失时，重试可能导致重复通知。无法确认的执行不会自动重放。

`reminder` 只发送用户提供的提醒文本。`agent` 会在到期时启动隔离的新对话，仅有公开网页搜索和读取工具，不读取当前记忆、不使用 Shell、不修改文件。`/reset` 和 `/memory clear-all` 不会取消计划任务；请通过 `/schedule cancel <id>` 取消。

## 工作流程

```text
输入目标
  → gpt-oss:120b-cloud 判断是否需要工具
  → 自动调用 Tavily 搜索
  → 将搜索结果回传模型
  → 检查是否满足原始目标
  ├─ 不满足：用更具体的查询继续搜索
  └─ 满足：输出最终回答和来源
```

每个用户请求最多进行 8 轮主模型调用（最终回答也占一轮），避免因不可靠的外部结果陷入无限循环。

## 常见问题

### 无法连接 Ollama

确认 `ollama list` 能正常返回模型；如果不能，启动 Ollama 服务后再运行 Agent。也可以检查 `.env` 的 `OLLAMA_HOST` 是否仍为 `http://127.0.0.1:11434`。

### 网页搜索没有结果或提示未配置

确认 `.env` 存在，并且 `TAVILY_API_KEY` 是有效的 Tavily Key。修改 `.env` 后，重启 Agent 使配置生效。

### 出现 SSL 证书验证错误

先安装或更新项目依赖，然后重启：

```bash
python3.10 -m pip install -e .
./run.sh
```

`run.sh` 会优先使用项目的 `venv/bin/python`；若缺少依赖，会显示对应的安装命令。

### 查看运行日志

模型请求、工具调用、搜索结果数量和异常会写入 `data/logs/lin-agent.log`。日志不会记录 Tavily Key 或完整对话内容：

```bash
tail -f data/logs/lin-agent.log
```

### 想重新开始一个话题

输入 `/reset`。这会清除短期对话，保留已提取事实和任务，未提取信息不会保留；旧任务需重新核查状态和授权。使用 `/memory clear-all` 并确认后才会清除全部记忆及受控归档/迁移备份。

## 当前安全边界

主对话注册了 `web_search`、`web_fetch`、`shell`，以及启用时的 `schedule` 工具。计划任务后台的 agent 只有 `web_search` 和 `web_fetch`，没有 Shell 或计划任务工具。`shell` 使用 `/bin/sh` 执行命令，支持 `command`、`cwd`、`timeout` 参数。默认工作目录为启动目录，默认超时 30 秒，最大 300 秒；返回退出码、标准输出、标准错误及超时标记，每个输出流最多返回 16000 字节，超出时标记截断。超时会终止命令进程组。

Shell 命令自动执行，具有启动 Agent 的系统用户权限，没有独立沙箱或逐次确认机制，可以修改文件。各次调用不保留工作目录或环境变量修改。系统提示词要求重要删除、覆盖及其他不可逆操作先获得明确授权，这属于模型行为约束。计划任务只按固定配置发送通知，没有通用消息发送或浏览器控制工具。

## 分层记忆

CLI 使用项目根目录的 `memory/memory.json`，不随 Shell 工作目录变化。领域模型位于 `src/domain/memory/`，`application/memory_manager.py` 协调用例，`service/memory_store.py` 负责文件读写。

- **最近对话**：常态保留最近10个终结轮次，加当前活动轮次；一轮包含一次用户输入、所有模型/工具交换和最终结果。失败与中断也计入轮次。
- **长期事实**：旧轮次退出窗口前由独立模型请求提取事实，只保留有来源的稳定信息和明确偏好。相同事实合并，明确变更取代旧值，无法确认的冲突标记为 disputed。
- **任务状态**：每轮结束后维护明确作业的目标、进展、下一步、等待项和阻碍；明确“创建并等我提供资料”直接登记等待任务。更新绑定已有 ID 或唯一目标，完成后清除等待项；普通状态查询、偏好和事实更正不另建作业。记录不等于执行许可，重启只提示恢复数量、不自动重放命令。
- **容量**：10轮还有独立字符软预算。优先裁剪无关事实和旧轮次，超长工具结果提供标记预览；当前输入不静默截断。字符预算不保证精确token数，需按实际模型容量调整。
- **维护**：每次请求默认最多2次额外模型调用，每次30秒，与主模型8步限制分离。优先更新最新任务；单项提取失败保留该轮并继续处理其他可维护轮次，不静默删除。积压时按容量补充相关用户原话，更正优先于旧事实；旧记录晚到不会撤销新更正。原始磁盘数据可以暂时超过10轮。

### 记忆命令

| 命令 | 作用 |
| --- | --- |
| `/memory` | 数量、待维护状态和revision |
| `/memory facts` / `/memory fact <id>` | 查看事实列表/详情 |
| `/memory tasks` | 查看未完成作业 |
| `/memory forget <id>` | 删除该属性的事实版本并阻止旧轮次重新提取；不擦除原文 |
| `/memory retry` | 有界重试维护，不重放工具 |
| `/task use <id>` / `/task cancel <id>` | 选择或取消作业 |
| `/reset` | 清短期，保留事实和任务 |
| `/memory clear-all` | 输入 CLEAR 确认后清除全部记忆、归档和迁移副本 |

### 存储与恢复

单进程持有操作系统文件锁，第二个Agent会被拒绝。状态通过revision校验、临时文件同步和原子替换保存。旧轮次归档到 `memory/archive/`，新状态引用其哈希；归档损坏或新版schema不会被静默覆盖。写盘失败停止后续副作用操作，结果未知的命令需先核查再决定是否执行。

首次启动自动将旧 `memory/history.json` 迁移为新状态，备份位于 `memory/migration/`。新状态有效后旧历史不再自动导入；长历史有界分批压缩。迁移备份按隐私策略处理，不承诺字节级保留所有秘密。

`memory/` 已被Git忽略，JSON仍为本地明文；常见凭证尽力脱敏，但不保证检测所有秘密。Ollama cloud模型请求可能离开设备，事实提取也会使用模型。clear-all不清除外部模型服务、系统快照或系统备份；物理清理失败会列出残留并阻止新作业。

### 记忆配置

完整示例见 `.env.example`。`MEMORY_ENABLED=false` 使用进程内记忆，不读写实际记忆文件；不会删除已有文件。`MEMORY_DIR` 相对路径相对项目根目录。

```env
MEMORY_ENABLED=true
MEMORY_DIR=memory
MEMORY_RECENT_TURNS=10
MEMORY_CONTEXT_MAX_CHARS=48000
MEMORY_FACTS_MAX_CHARS=8000
MEMORY_TASKS_MAX_CHARS=6000
MEMORY_TOOL_CONTEXT_MAX_CHARS=4000
MEMORY_EXTRACTION_MAX_CHARS=24000
MEMORY_MAINTENANCE_CALLS=2
MEMORY_MAINTENANCE_TIMEOUT=30
MEMORY_BACKLOG_WARNING_TURNS=30
MEMORY_MODEL=
MEMORY_DEBUG_CONTENT=false
```

### 验证

```bash
venv/bin/python -m unittest discover -s tests
```

可选真实模型验收（只使用合成对话和临时目录，不读取实际记忆、不开放Shell工具）：

```bash
venv/bin/python scripts/verify_memory_model.py --model gpt-oss:120b-cloud
```

50轮真实模型回归会保存逐轮快照、完整调用和最终可查看的记忆；reset/forget/clear-all检查在副本进行，不清除主测试记忆。输出路径必须不存在：

```bash
venv/bin/python scripts/verify_memory_50_turns.py --output /tmp/lin-memory-new-run
```

最终记忆位于输出目录的 `isolated-memory/memory.json`，归档位于其 `archive/`；`state-50.json` 是额外快照。机器检查未通过时返回非零退出码。事实上下文只带必要属性和一个来源引用，完整来源仍保存在记忆文件；维护日志记录 ID、数量和耗时，不记录事实正文。

设计基线及实际交付说明见 `设计文档/2026-10-30-记忆系统设计方案.md`、`设计文档/2026-10-30-分层记忆系统实施与验收报告.md`，本次修复见 `设计文档/2026-11-03-记忆系统修复与复验.md`。

## 自主 Sub-Agent 委派

默认启用 `delegate_task`：主模型在正常工具选择中决定是否将大量阅读、搜索或独立分析交给只读子 Agent。简单任务可以直接回答，无需手动启动子 Agent。

子任务使用独立进程与消息历史，不复制主对话，不连接主 Agent 的记忆存储；只获得 `project_files`、`web_search` 和 `web_fetch`，不能调用 Shell、计划任务或继续委派。`project_files` 只读取项目内有大小限制的普通文本，返回行号，支持按字面文本搜索；限制越界、敏感路径、主记忆目录和符号链接逃逸，输出尽力脱敏。此能力限制不等于操作系统沙箱，也不保证识别全部秘密；主 Agent 委派时仍应只传必要背景和用户约束。

默认每次用户请求最多 2 个子任务，每个最多 5 轮模型调用，主、子执行共享 18 次调用预算，并为主 Agent 保留一次最终回答调用。主 Agent 自身仍最多 8 轮。记忆维护沿用自己的既有预算，不计入这 18 次执行调用。子任务默认总超时 120 秒，超过后终止并回收子进程；外部模型服务可能仍在处理已经发送的远端请求，本机不会继续该子任务或自动重试。

子任务返回 `status`、`summary`、`findings`、`artifacts`、`unresolved`，并附可信的 `task_id`、`parent_id`、`details_ref`；默认完整返回 JSON 不超过 3000 字符。超限或格式不合规会明确降为未完成状态，完整已获得的输出保存在独立过程记录中。输入或配额被拒绝时不启动子任务，返回失败及原因。主 Agent 应核验关键证据，再整合回答。

完整过程保存在被 Git 忽略的 `data/subagents/<task_id>.jsonl`，模型完整结果另存于同名 `.result.json`。记录包含尽力脱敏的输入、工具结果和模型响应，文件权限为 0600；仍可能包含私人内容。主记忆清理命令不清除这些独立执行记录，可按需手动清理。主 Agent 可通过现有 Shell 按段读取 `details_ref`，避免重新加载全部过程。

终端展示子任务开始、结束及父子任务标识，不展示子任务原始上下文。`Agent.last_run_metrics` 与运行日志的 `agent_request_metrics` 提供每次请求的上下文字符数（含工具定义）、调用预算消耗、耗时，以及模型实际返回的 Token 计数。超时或崩溃时保守计入预留调用数，`model_calls_charged` 不等于已确认的远端请求次数。

可在 `.env` 中调整，重启后生效：

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `SUBAGENT_ENABLED` | `true` | 关闭后不注册委派工具 |
| `SUBAGENT_MAX_TASKS` | `2` | 每次请求最多子任务数，范围 1–8 |
| `SUBAGENT_MAX_ROUNDS` | `5` | 每个子任务最多模型调用，范围 1–6 |
| `AGENT_MAX_MODEL_CALLS` | `18` | 主子共享执行调用预算，范围 2–100 |
| `SUBAGENT_TIMEOUT` | `120` | 每个子任务墙钟超时秒数，范围 0.01–600 |
| `SUBAGENT_INPUT_CHARS` | `24000` | 系统约束与任务输入字符预算 |
| `SUBAGENT_CONTEXT_CHARS` | `48000` | 每轮子模型消息与工具定义字符预算 |
| `SUBAGENT_RESULT_CHARS` | `3000` | 完整子任务返回 JSON 字符预算，范围 1000–4000 |

计划任务后台 Agent 保持隔离的公开网页搜索/读取能力，不获得本地文件访问和委派工具。

回归测试：`python3 -m unittest discover -s tests -v`。其中子 Agent HTTP 集成测试仅绑定 `127.0.0.1` 的临时端口，使用模拟 Ollama 服务，无需真实模型或 API Key；运行环境必须允许本机回环端口。测试覆盖上下文隔离、共享预算、次数与轮次限制、硬超时回收、崩溃及无效返回、路径约束，以及直接处理与委派的上下文对比。它验证执行机制，不保证真实模型的委派选择或语义质量。
