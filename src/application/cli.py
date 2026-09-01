from __future__ import annotations

import json

from .agent import Agent, AgentStateError
from ..common import Settings, configure_logging, load_system_prompt
from ..domain import ToolExecutionEvent
from ..service import OllamaClient, OllamaError
from ..tools import WebSearchTool


def _show_tool_event(event: ToolExecutionEvent) -> None:
    arguments = json.dumps(event.arguments, ensure_ascii=False)
    if event.phase == "started":
        print(f"\n[工具调用 · 第 {event.round_number} 轮] {event.tool_name} {arguments}")
        return

    result = event.result or {}
    if error := result.get("error"):
        print(f"[工具返回] {event.tool_name}：失败（{error}）")
        return

    results = result.get("results")
    if isinstance(results, list):
        titles = [item.get("title", "") for item in results[:3] if isinstance(item, dict)]
        title_summary = "；".join(title for title in titles if title)
        suffix = f"；示例：{title_summary}" if title_summary else ""
        print(f"[工具返回] {event.tool_name}：获得 {len(results)} 条结果{suffix}")
        return

    print(f"[工具返回] {event.tool_name}：已完成")


def main() -> None:
    settings = Settings.from_env()
    logger = configure_logging(settings.log_level)
    agent = Agent(
        client=OllamaClient(settings.ollama_host, settings.model),
        tools=[WebSearchTool(settings.tavily_api_key, settings.search_result_limit)],
        system_prompt=load_system_prompt(),
        on_tool_event=_show_tool_event,
    )
    print(f"Lin Agent 已启动（模型：{settings.model}）。输入 /help 查看命令。")
    logger.info("agent_started model=%s", settings.model)

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
