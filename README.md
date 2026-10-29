# Lin Agent

本地运行的个人终端 Agent：通过 Ollama 调用 `gpt-oss:120b-cloud`，支持网页搜索、本机 Shell 和分层持久化记忆。

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

因此不需要手动复制搜索结果，也不需要输入工具命令。最终回答应包含所用来源的 URL。

交互过程中，终端默认显示调用状态和结果摘要，不打印完整对话、事实、参数或工具输出。设置 `MEMORY_DEBUG_CONTENT=true` 可显示经过尽力脱敏的调试内容，仍可能包含私人信息。

## DDD 分层

```text
src/
├── application/  # 对话用例与 CLI 输入输出
├── domain/       # 模型与工具的业务契约，不依赖具体供应商
├── service/      # Ollama 等基础设施适配器
├── common/       # 配置与通用能力
└── tools/        # 可被 Agent 调用的外部工具（Tavily 搜索与 Shell）
```

主助手提示词位于 `resource/prompt/system.md`；事实与任务提取分别使用 `memory_extract.md`、`task_update.md`。

## 命令

- 输入问题：模型按需自动调用工具、检查结果并生成最终回答。
- `/help`：显示可用命令。
- `/reset`：清除短期对话，保留已提取事实和任务；未提取信息不会保留。
- `/exit`：退出。

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

目前注册了 `web_search` 和 `shell` 两个工具。`shell` 使用 `/bin/sh` 执行命令，支持 `command`、`cwd`、`timeout` 参数。默认工作目录为启动目录，默认超时 30 秒，最大 300 秒；返回退出码、标准输出、标准错误及超时标记，每个输出流最多返回 16000 字节，超出时标记截断。超时会终止命令进程组。

Shell 命令自动执行，具有启动 Agent 的系统用户权限，没有独立沙箱或逐次确认机制，可以修改文件。各次调用不保留工作目录或环境变量修改。系统提示词要求重要删除、覆盖及其他不可逆操作先获得明确授权，这属于模型行为约束。项目尚无发送消息或浏览器控制工具。

## 分层记忆

CLI 使用项目根目录的 `memory/memory.json`，不随 Shell 工作目录变化。领域模型位于 `src/domain/memory/`，`application/memory_manager.py` 协调用例，`service/memory_store.py` 负责文件读写。

- **最近对话**：常态保留最近10个终结轮次，加当前活动轮次；一轮包含一次用户输入、所有模型/工具交换和最终结果。失败与中断也计入轮次。
- **长期事实**：旧轮次退出窗口前由独立模型请求提取事实，只保留有来源的稳定信息和明确偏好。相同事实合并，明确变更取代旧值，无法确认的冲突标记为 disputed。
- **任务状态**：每轮结束后维护明确作业的目标、进展、下一步、等待项和阻碍。记录不等于执行许可；重启不自动重放命令。
- **容量**：10轮还有独立字符软预算。优先裁剪无关事实和旧轮次，超长工具结果提供标记预览；当前输入不静默截断。字符预算不保证精确token数，需按实际模型容量调整。
- **维护**：每次请求默认最多2次额外模型调用，每次30秒，与主模型8步限制分离。失败保留旧轮次并显示待维护状态；原始磁盘数据可以暂时超过10轮。

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

设计基线及实际交付说明见 `设计文档/2026-10-30-记忆系统设计方案.md` 和 `设计文档/2026-10-30-分层记忆系统实施与验收报告.md`。
