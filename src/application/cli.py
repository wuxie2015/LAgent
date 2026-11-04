from __future__ import annotations

from contextlib import ExitStack
import json
import sqlite3
from pathlib import Path

from .agent import Agent, AgentStateError, CompletedTurnStorageError
from .memory_manager import MemoryManager
from .subagent import SubagentRunner
from ..tools.delegate_task import DelegateTaskTool
from ..common import Settings, configure_logging, load_system_prompt
from ..domain import ModelExecutionEvent, ToolExecutionEvent
from ..domain.memory import MemoryError
from ..domain.memory.state import StorageError
from ..service import OllamaClient, OllamaError
from ..service.memory_store import JsonMemoryStore
from ..service.memory_extractor import ModelMemoryExtractor
from ..tools import ShellTool, WebSearchTool, WebFetchTool
from ..tools.schedule import ScheduleTool
from .scheduler import Scheduler
from ..service.notifications import NotificationDispatcher
from ..service.schedule_store import ScheduleStore


def _enable_line_editing() -> None:
    """Let readline/libedit edit whole Unicode characters before input decodes them."""
    try:
        import readline  # noqa: F401 — importing installs Python's input editor
    except ImportError:
        # Piped input and Python builds without readline keep the standard input path.
        pass


def _show_model_event(event: ModelExecutionEvent, debug: bool = False) -> None:
    print(f"[模型 · 第 {event.round_number} 轮] {'请求' if event.phase == 'requested' else '返回'}")


def _show_tool_event(event: ToolExecutionEvent, debug: bool = False) -> None:
    if event.phase == "started":
        print(f"[工具调用 · 第 {event.round_number} 轮] {event.tool_name}")
    else:
        result = event.result or {}
        status = {k: result[k] for k in ("exit_code", "timed_out", "stdout_truncated", "stderr_truncated") if k in result}
        if "results" in result:
            status["result_count"] = len(result["results"])
        if "error" in result:
            status["error"] = "工具执行失败，详情已提供给模型"
            status["error_code"] = result.get("error_code", "tool_failed")
            status["retryable"] = result.get("retryable", False)
        print(f"[工具返回] {event.tool_name} {json.dumps(status, ensure_ascii=False)}")


def _show_subagent_event(event):
    print(f"[子 Agent · {event.parent_id} / {event.task_id}] {event.phase}: {event.status}")


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


def schedule_command(text: str, tool: ScheduleTool | None) -> bool:
    parts = text.split()
    if not parts or parts[0] != "/schedule":
        return False
    if tool is None:
        print("计划任务功能未启用；设置 SCHEDULER_ENABLED=true 后重启。")
        return True
    if len(parts) == 2 and parts[1] == "list":
        result = tool.run(action="list")
    elif len(parts) == 2 and parts[1] == "now":
        result = tool.run(action="now")
    elif len(parts) == 3 and parts[1] == "get":
        result = tool.run(action="get", task_id=parts[2])
    elif len(parts) == 2 and parts[1] == "history":
        result = tool.run(action="history")
    elif len(parts) == 3 and parts[1] == "history":
        result = tool.run(action="history", task_id=parts[2])
    elif len(parts) == 3 and parts[1] in {"pause", "resume", "cancel"}:
        result = tool.run(action=parts[1], task_id=parts[2])
    else:
        print("用法：/schedule list|now|history [id]|get <id>|pause|resume|cancel <id>")
        return True
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return True


HELP = """/reset 清除短期对话（保留事实和任务，不影响计划任务）；/exit 退出。
/memory 查看记忆状态；/memory facts 查看事实；/memory tasks 查看作业。
/memory fact <id> 查看事实；/memory forget <id> 删除事实。
/memory retry 重试维护；/memory clear-all 确认后清除全部。
/task use <id> 选择作业；/task cancel <id> 取消作业。
/schedule list|now|history [id]|get <id>|pause|resume|cancel <id> 管理计划任务。"""


def main() -> None:
    _enable_line_editing()
    try:
        settings = Settings.from_env()
        logger = configure_logging(settings.log_level)
        system_prompt = load_system_prompt()
        with ExitStack() as stack:
            store = stack.enter_context(JsonMemoryStore(settings.memory_dir)) if settings.memory_enabled else None
            extractor = ModelMemoryExtractor(OllamaClient(settings.ollama_host, settings.memory_model or settings.model,
                timeout=settings.memory_policy.maintenance_timeout, json_mode=True), settings.memory_policy.extraction_max_chars)
            memory = MemoryManager(system_prompt, store, extractor, settings.memory_policy)
            schedule_tool = None
            tools = [WebSearchTool(settings.tavily_api_key, settings.search_result_limit), WebFetchTool(), ShellTool()]
            if settings.scheduler_enabled:
                settings.schedule_db.parent.mkdir(parents=True, exist_ok=True)
                schedule_store = ScheduleStore(settings.schedule_db)
                dispatcher = NotificationDispatcher(
                    console_enabled=settings.notification_console_enabled,
                    smtp_host=settings.smtp_host, smtp_port=settings.smtp_port,
                    smtp_from=settings.smtp_from, smtp_to=settings.smtp_to,
                    smtp_user=settings.smtp_user, smtp_password=settings.smtp_password,
                    smtp_security=settings.smtp_security,
                    webhook_url=settings.notification_webhook_url,
                    webhook_token=settings.notification_webhook_token,
                    timeout=settings.notification_timeout,
                )
                dispatcher.validate_channel(settings.schedule_default_channel)

                def execute_scheduled(content: str) -> str:
                    # A scheduled run has isolated, in-memory context and search only.
                    prompt = load_system_prompt() + "\n\n计划任务约束：只读执行，使用网页搜索和网页读取工具获取信息；不得调用 Shell、修改文件或声称已完成未执行的外部操作。"
                    background = Agent(
                        client=OllamaClient(settings.ollama_host, settings.model, timeout=30),
                        tools=[WebSearchTool(settings.tavily_api_key, settings.search_result_limit), WebFetchTool()],
                        system_prompt=prompt,
                    )
                    return background.ask(content)

                scheduler = Scheduler(
                    schedule_store, dispatcher, execute_scheduled,
                    poll_interval=settings.schedule_poll_interval,
                    max_notification_attempts=settings.schedule_max_notification_attempts,
                    on_error=lambda error: print(f"\n计划任务后台已停止（{type(error).__name__}）；新建和修改计划任务已暂停。"),
                )
                stack.callback(scheduler.stop)
                scheduler.start()
                schedule_tool = ScheduleTool(
                    schedule_store, dispatcher, timezone_name=settings.schedule_timezone,
                    default_channel=settings.schedule_default_channel,
                    grace_seconds=settings.schedule_grace_seconds,
                    writes_available=lambda: scheduler.fatal_error is None,
                )
                tools.append(schedule_tool)
            if settings.subagent_enabled:
                tools.append(DelegateTaskTool(SubagentRunner(
                    OllamaClient(settings.ollama_host, settings.model),
                    Path(__file__).resolve().parents[2],
                    WebSearchTool(settings.tavily_api_key, settings.search_result_limit),
                    settings.subagent_policy, _show_subagent_event, blocked_paths=(settings.memory_dir,))))
            agent = Agent(client=OllamaClient(settings.ollama_host, settings.model),
                tools=tools,
                system_prompt=system_prompt, memory=memory, execution_policy=settings.subagent_policy,
                on_tool_event=lambda e: _show_tool_event(e, settings.memory_debug_content),
                on_model_event=lambda e: _show_model_event(e, settings.memory_debug_content))
            print(f"Lin Agent 已启动（模型：{settings.model}）。输入 /help 查看命令。")
            unfinished = memory.summary()["unfinished_tasks"]
            if unfinished:
                print(f"[记忆] 已恢复 {unfinished} 个未完成任务，输入 /memory tasks 查看；不会自动执行。")
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
                    if schedule_command(text, schedule_tool):
                        continue
                    if memory_command(text, memory):
                        continue
                    if schedule_tool is not None:
                        schedule_tool.begin_request()
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
    except (MemoryError, OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        print(f"启动失败：{exc}")


if __name__ == "__main__":
    main()
