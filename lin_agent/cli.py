from __future__ import annotations

from .agent import Agent
from .config import Settings
from .ollama import OllamaClient, OllamaError
from .tools import WebSearchTool


def main() -> None:
    settings = Settings.from_env()
    agent = Agent(
        OllamaClient(settings.ollama_host, settings.model),
        WebSearchTool(settings.search_result_limit),
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
            print("/reset 清除会话；/exit 退出。实时信息会自动使用网页搜索。")
            continue

        try:
            print(f"\nAgent：{agent.ask(text)}")
        except OllamaError as exc:
            print(f"\n错误：{exc}")


if __name__ == "__main__":
    main()
