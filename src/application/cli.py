from __future__ import annotations

from .agent import Agent, AgentStateError
from ..common import Settings, load_system_prompt
from ..service import OllamaClient, OllamaError
from ..tools import WebSearchTool


def main() -> None:
    settings = Settings.from_env()
    agent = Agent(
        client=OllamaClient(settings.ollama_host, settings.model),
        tools=[WebSearchTool(settings.tavily_api_key, settings.search_result_limit)],
        system_prompt=load_system_prompt(),
    )
    print(f"Lin Agent 已启动（模型：{settings.model}）。输入 /help 查看命令。")

    while True:
        try:
            text = input("\n你：").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见。")
            return

        if not text:
            continue
        if text == "/exit":
            print("再见。")
            return
        if text == "/reset":
            agent.reset()
            print("当前会话已清除。")
            continue
        if text == "/help":
            print("/reset 清除会话；/exit 退出。工具会自动执行并由 Agent 验证结果。")
            continue

        try:
            print(f"\nAgent：{agent.ask(text)}")
        except (AgentStateError, OllamaError) as exc:
            print(f"\n错误：{exc}")


if __name__ == "__main__":
    main()
