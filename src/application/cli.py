from __future__ import annotations

import json

from .agent import Agent, AgentStateError
from ..common import Settings, configure_logging, load_system_prompt
from ..domain import DEFAULT_MEMORY_PATH, Memory, MemoryError, ModelExecutionEvent, ToolExecutionEvent


def _show_model_event(event: ModelExecutionEvent) -> None:
    if event.phase == "requested":
        print(
            f"\n[模型调用 · 第 {event.round_number} 轮]\n"
            f"{json.dumps(event.messages, ensure_ascii=False, indent=2)}"
        )
        return

    print(
        f"[模型返回 · 第 {event.round_number} 轮]\n"
        f"{json.dumps(event.response, ensure_ascii=False, indent=2)}"
    )
from ..service import OllamaClient, OllamaError
from ..tools import ShellTool, WebSearchTool


def _show_tool_event(event: ToolExecutionEvent) -> None:
    arguments = json.dumps(event.arguments, ensure_ascii=False)
    if event.phase == "started":
        print(f"\n[工具调用 · 第 {event.round_number} 轮] {event.tool_name} {arguments}")
        return

    print(
        f"[工具返回] {event.tool_name}\n"
        f"{json.dumps(event.result or {}, ensure_ascii=False, indent=2)}"
    )


def main() -> None:
    settings = Settings.from_env()
    logger = configure_logging(settings.log_level)
    system_prompt = load_system_prompt()
    try:
        memory = Memory(system_prompt, DEFAULT_MEMORY_PATH)
    except MemoryError as exc:
        print(f"错误：{exc}")
        return
    agent = Agent(
        client=OllamaClient(settings.ollama_host, settings.model),
        tools=[WebSearchTool(settings.tavily_api_key, settings.search_result_limit), ShellTool()],
        system_prompt=system_prompt,
        memory=memory,
        on_tool_event=_show_tool_event,
        on_model_event=_show_model_event,
    )
    print(f"Lin Agent 已启动（模型：{settings.model}）。输入 /help 查看命令。")
    logger.info("agent_started model=%s", settings.model)

    while True:
        try:
            text = input("\n你：").strip()
        except UnicodeDecodeError:
            print("\n输入解码失败：终端收到的内容不是有效的 UTF-8。请切换终端编码为 UTF-8 后重新输入。")
            continue
        except (EOFError, KeyboardInterrupt):
            print("\n再见。")
            return

        if not text:
            continue
        if text == "/exit":
            print("再见。")
            return
        if text == "/reset":
            try:
                agent.reset()
                print("当前会话及已保存的历史已清除。")
            except MemoryError as exc:
                print(f"错误：{exc}")
            continue
        if text == "/help":
            print("/reset 清除会话；/exit 退出。工具会自动执行并由 Agent 验证结果。")
            continue

        try:
            print(f"\nAgent：{agent.ask(text)}")
        except (AgentStateError, OllamaError, MemoryError) as exc:
            print(f"\n错误：{exc}")


if __name__ == "__main__":
    main()
