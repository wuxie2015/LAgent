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
python3 -m lin_agent
```

不需要 OpenAI API Key。默认连接 `http://127.0.0.1:11434` 并使用 `gpt-oss:120b-cloud`。

网页搜索通过 Tavily 的搜索 API 提供。将 Tavily Key 填入本地 `.env` 的 `TAVILY_API_KEY`；它不会提交到 Git。若未配置，普通对话仍可用，但 Agent 会说明网页搜索尚未配置。

## 命令

- 输入问题：正常对话；模型会在需要实时资料时调用网页搜索。
- `/reset`：清除当前对话的短期上下文。
- `/exit`：退出。

## 当前安全边界

目前唯一注册的工具是只读网页搜索。项目没有文件写入、Shell 执行、发送消息或浏览器控制能力；新增这类工具时应先加入明确的用户确认步骤。
