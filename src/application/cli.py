from __future__ import annotations

from contextlib import ExitStack
import json

from .agent import Agent, AgentStateError, CompletedTurnStorageError
from .memory_manager import MemoryManager
from ..common import Settings, configure_logging, load_system_prompt
from ..domain import ModelExecutionEvent, ToolExecutionEvent
from ..domain.memory import MemoryError
from ..domain.memory.state import StorageError
from ..domain.memory.privacy import redact
from ..service import OllamaClient, OllamaError
from ..service.memory_store import JsonMemoryStore
from ..service.memory_extractor import ModelMemoryExtractor
from ..tools import ShellTool, WebSearchTool


def _show_model_event(event: ModelExecutionEvent, debug: bool = False) -> None:
    if debug:
        print(json.dumps(redact(event.messages if event.phase == "requested" else event.response), ensure_ascii=False, indent=2))
    else:
        print(f"[模型 · 第 {event.round_number} 轮] {'请求' if event.phase == 'requested' else '返回'}")


def _show_tool_event(event: ToolExecutionEvent, debug: bool = False) -> None:
    if debug:
        print(json.dumps(redact({"tool": event.tool_name, "arguments": event.arguments, "result": event.result}), ensure_ascii=False, indent=2))
    elif event.phase == "started":
        print(f"[工具调用 · 第 {event.round_number} 轮] {event.tool_name}")
    else:
        result = event.result or {}
        status = {k: result[k] for k in ("exit_code", "timed_out", "stdout_truncated", "stderr_truncated") if k in result}
        if "results" in result:
            status["result_count"] = len(result["results"])
        if "error" in result:
            status["error"] = "工具执行失败，详情已提供给模型"
        print(f"[工具返回] {event.tool_name} {json.dumps(status, ensure_ascii=False)}")


def memory_command(text: str, memory: MemoryManager) -> bool:
    parts = text.split()
    if not parts or parts[0] not in {"/memory", "/task", "/reset"}:
        return False
    if parts == ["/reset"]:
        print("清除短期对话，未提取的事实不会保留；长期事实和任务记录保留。")
        memory.reset()
        print("短期对话已清除，继续旧任务前需核查状态和授权。")
    elif parts == ["/memory"]:
        print(json.dumps(memory.summary(), ensure_ascii=False, indent=2))
    elif parts == ["/memory", "facts"]:
        print(json.dumps(memory.state.facts, ensure_ascii=False, indent=2))
    elif parts == ["/memory", "tasks"]:
        print(json.dumps([t for t in memory.state.tasks if t["status"] not in {"completed", "cancelled"}], ensure_ascii=False, indent=2))
    elif len(parts) == 3 and parts[:2] == ["/memory", "fact"]:
        fact = next((f for f in memory.state.facts if f["id"] == parts[2]), None)
        if fact is None:
            raise MemoryError("事实ID不存在")
        print(json.dumps(fact, ensure_ascii=False, indent=2))
    elif len(parts) == 3 and parts[:2] == ["/memory", "forget"]:
        memory.forget_fact(parts[2])
        print("已删除该事实及其旧版本，并阻止旧轮次再次提取；短期对话和归档原文未物理擦除。")
    elif parts == ["/memory", "retry"]:
        memory.maintain_safely()
        print(json.dumps(memory.summary(), ensure_ascii=False, indent=2))
    elif parts == ["/memory", "clear-all"]:
        print("将清除短期对话、长期事实、任务、归档及迁移备份；不清除系统备份或外部模型数据。")
        if input("输入 CLEAR 确认：").strip() == "CLEAR":
            failures = memory.clear_all()
            print("逻辑清除完成，物理清理未完成：" + ", ".join(failures) if failures else "全部记忆及受控副本已清除。")
        else:
            print("已取消清除。")
    elif len(parts) == 3 and parts[:2] == ["/task", "use"]:
        memory.use_task(parts[2])
        print("已选择任务；执行前仍需检查实际状态。")
    elif len(parts) == 3 and parts[:2] == ["/task", "cancel"]:
        memory.cancel_task(parts[2])
        print("任务已取消。")
    else:
        print("未知记忆命令，请输入 /help。")
    return True


HELP = """/reset 清除短期对话（保留事实和任务）；/exit 退出。
/memory 查看记忆状态；/memory facts 查看事实；/memory tasks 查看作业。
/memory fact <id> 查看事实；/memory forget <id> 删除事实。
/memory retry 重试维护；/memory clear-all 确认后清除全部。
/task use <id> 选择作业；/task cancel <id> 取消作业。"""


def main() -> None:
    try:
        settings = Settings.from_env()
        logger = configure_logging(settings.log_level)
        system_prompt = load_system_prompt()
        with ExitStack() as stack:
            store = stack.enter_context(JsonMemoryStore(settings.memory_dir)) if settings.memory_enabled else None
            extractor = ModelMemoryExtractor(OllamaClient(settings.ollama_host, settings.memory_model or settings.model,
                timeout=settings.memory_policy.maintenance_timeout, json_mode=True), settings.memory_policy.extraction_max_chars)
            memory = MemoryManager(system_prompt, store, extractor, settings.memory_policy)
            agent = Agent(client=OllamaClient(settings.ollama_host, settings.model),
                tools=[WebSearchTool(settings.tavily_api_key, settings.search_result_limit), ShellTool()],
                system_prompt=system_prompt, memory=memory,
                on_tool_event=lambda e: _show_tool_event(e, settings.memory_debug_content),
                on_model_event=lambda e: _show_model_event(e, settings.memory_debug_content))
            print(f"Lin Agent 已启动（模型：{settings.model}）。输入 /help 查看命令。")
            if settings.memory_debug_content:
                print("[调试] 将显示经过尽力脱敏的完整内容，仍可能包含私人信息。")
            logger.info("agent_started model=%s", settings.model)
            while True:
                for notice in memory.notices:
                    print("[记忆] " + notice)
                memory.notices.clear()
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
                if text == "/help":
                    print(HELP)
                    continue
                try:
                    if memory_command(text, memory):
                        continue
                    print(f"\nAgent：{agent.ask(text)}")
                except CompletedTurnStorageError as exc:
                    print(f"\nAgent：{exc.answer}")
                    print(f"[记忆] {exc}")
                    return
                except StorageError as exc:
                    print(f"\n存储错误：{exc}。已停止，请重启核查，勿盲目重试命令。")
                    return
                except KeyboardInterrupt:
                    print("\n本轮已中断，不会自动重放工具调用。")
                except (AgentStateError, OllamaError, MemoryError, OSError, ValueError) as exc:
                    print(f"\n错误：{exc}")
    except (MemoryError, OSError, ValueError) as exc:
        print(f"启动失败：{exc}")


if __name__ == "__main__":
    main()
