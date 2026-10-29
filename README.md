# Lin Agent

本地运行的个人终端 Agent：通过 Ollama 调用 `gpt-oss:120b-cloud`，第一项能力是网页搜索。

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

交互过程中，终端会显示当前工具调用、参数和返回摘要，例如搜索结果数量与前三条标题；不会显示 Tavily Key 或完整原始响应。

## DDD 分层

```text
src/
├── application/  # 对话用例与 CLI 输入输出
├── domain/       # 模型与工具的业务契约，不依赖具体供应商
├── service/      # Ollama 等基础设施适配器
├── common/       # 配置与通用能力
└── tools/        # 可被 Agent 调用的外部工具（当前为 Tavily 搜索）
```

系统提示词仅保存在 `resource/prompt/system.md`。

## 命令

- 输入问题：模型按需自动调用工具、检查结果并生成最终回答。
- `/help`：显示可用命令。
- `/reset`：清除当前对话及已保存的历史。
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

每个用户请求最多进行 8 轮工具调用，避免因不可靠的外部结果陷入无限循环。

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

输入 `/reset`。这会清除当前对话，并将 `memory/history.json` 重置为空历史，不会删除其他项目文件或 `.env`。

## 当前安全边界

目前注册了 `web_search` 和 `shell` 两个工具。`shell` 使用 `/bin/sh` 执行命令，支持 `command`、`cwd`、`timeout` 参数。默认工作目录为启动目录，默认超时 30 秒，最大 300 秒；返回退出码、标准输出、标准错误及超时标记，每个输出流最多返回 16000 字节，超出时标记截断。超时会终止命令进程组。

Shell 命令自动执行，具有启动 Agent 的系统用户权限，没有独立沙箱或逐次确认机制，可以修改文件。各次调用不保留工作目录或环境变量修改。系统提示词要求重要删除、覆盖及其他不可逆操作先获得明确授权，这属于模型行为约束。项目尚无发送消息或浏览器控制工具。

## 对话记忆

`src/domain/memory.py` 负责管理消息历史。CLI 将用户消息、模型回复和工具结果以 UTF-8 JSON 保存到项目根目录的 `memory/history.json`，每次追加消息后自动保存，重启后自动恢复。系统提示词不写入历史，启动时重新加载最新版本。`/reset` 同时清除内存和磁盘中的历史。

记忆目录已加入 Git 忽略规则。历史为本地明文，可能包含命令输出等敏感内容。当前使用单个会话文件，不支持多个进程同时写入，也尚无历史压缩或检索。文件损坏时启动会报错并保留原文件；修复或移走该文件后可以重启。直接创建 Agent 时默认使用内存记忆，可通过 memory 参数注入持久化 Memory。
