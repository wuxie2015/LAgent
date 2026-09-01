# Lin Agent

本地运行的个人终端 Agent：通过 Ollama 调用 `gpt-oss:120b-cloud`，第一项能力是网页搜索。

运行环境：Python 3.10 或更新版本。

## 启动

确认 Ollama 已启动且模型可用：

```bash
ollama list
```

复制配置并启动：

```bash
cp .env.example .env
python3.10 -m src
```

不需要 OpenAI API Key。默认连接 `http://127.0.0.1:11434` 并使用 `gpt-oss:120b-cloud`。

网页搜索使用 Tavily。Agent 会自动执行搜索、将结果回传模型进行核验；若结果不足以满足原始目标，模型会继续发起更精确的工具调用，直至完成或达到安全上限。

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
- `/reset`：清除当前对话的短期上下文。
- `/exit`：退出。

## 当前安全边界

目前唯一注册的工具是只读网页搜索。项目没有文件写入、Shell 执行、发送消息或浏览器控制能力；新增这类工具时应先加入明确的用户确认步骤。
