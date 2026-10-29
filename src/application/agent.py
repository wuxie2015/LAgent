from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from ..common.logging_config import get_logger
from ..domain import AgentTool, ModelExecutionEvent, ModelGateway, ToolExecutionEvent
from ..domain.memory import MemoryError
from ..domain.memory.state import StorageError
from .memory_manager import MemoryManager


class AgentStateError(RuntimeError):
    pass


class CompletedTurnStorageError(StorageError):
    def __init__(self, answer: str):
        super().__init__("本轮回答已保存，但记忆维护写盘失败；已停止后续执行，请重启核查")
        self.answer = answer


class Agent:
    """Application use case: automatically execute and evaluate tool calls."""

    max_tool_rounds = 8

    def __init__(
        self,
        client: ModelGateway,
        tools: list[AgentTool],
        system_prompt: str,
        on_tool_event: Callable[[ToolExecutionEvent], None] | None = None,
        on_model_event: Callable[[ModelExecutionEvent], None] | None = None,
        memory: MemoryManager | None = None,
    ) -> None:
        self.client = client
        self.tools = {tool.name: tool for tool in tools}
        self.system_prompt = system_prompt
        self.on_tool_event = on_tool_event
        self.on_model_event = on_model_event
        self.memory = memory if memory is not None else MemoryManager(system_prompt)

    @property
    def messages(self) -> list[dict[str, Any]]:
        return self.memory.messages

    def reset(self) -> None:
        self.memory.reset()

    def ask(self, text: str) -> str:
        logger = get_logger()
        self.memory.begin_turn(text)
        try:
            for round_number in range(1, self.max_tool_rounds + 1):
                definitions = [tool.definition for tool in self.tools.values()]
                messages = self.memory.build_context(definitions)
                self._emit_model_event("requested", round_number, messages)
                response = self.client.chat(messages, definitions)
                self._emit_model_event("completed", round_number, messages, response)
                message = response.get("message")
                if not isinstance(message, dict) or message.get("role") != "assistant":
                    raise AgentStateError("模型响应缺少有效assistant消息")
                message_id = self.memory.append_message(message)
                calls = message.get("tool_calls") or []
                if not calls:
                    self.memory.finish_turn("completed")
                    answer = message.get("content", "模型没有返回文本回复。")
                    try:
                        self.memory.maintain_safely()
                    except StorageError as exc:
                        raise CompletedTurnStorageError(answer) from exc
                    return answer
                tool_calls = [self._parse_tool_call(call) for call in calls]
                execution_ids = self.memory.record_tool_starts(tool_calls, message_id)
                for tool_name, arguments in tool_calls:
                    self._emit_tool_event("started", round_number, tool_name, arguments)
                with ThreadPoolExecutor(max_workers=len(tool_calls)) as executor:
                    results = list(executor.map(lambda item: self._execute_tool(*item), tool_calls))
                for execution_id, (tool_name, arguments), result in zip(execution_ids, tool_calls, results):
                    try:
                        self.memory.record_tool_result(execution_id, tool_name, arguments, result)
                    except (StorageError, OSError) as exc:
                        raise StorageError("命令可能已执行，结果未可靠写入记忆；停止后续执行，请重启核查，勿直接重试") from exc
                    self._emit_tool_event("completed", round_number, tool_name, arguments, result)
            answer = "已达到工具调用上限，仍未获得足够结果。请缩小目标或稍后重试。"
            self.memory.append_message({"role": "assistant", "content": answer})
            self.memory.finish_turn("failed", "model_step_limit")
            self.memory.maintain_safely()
            return answer
        except StorageError:
            # The disk may contain an execution checkpoint. Do not overwrite or replay it.
            raise
        except KeyboardInterrupt:
            self.memory.finish_turn("interrupted", "KeyboardInterrupt")
            raise
        except Exception as exc:
            self.memory.finish_turn("failed", type(exc).__name__)
            logger.warning("agent_turn_failed error_type=%s", type(exc).__name__)
            raise

    def _parse_tool_call(self, call: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        function = call.get("function", {})
        tool_name = function.get("name", "")
        if tool_name not in self.tools:
            raise AgentStateError(f"模型请求了未注册工具：{tool_name or '未知工具'}")
        arguments = function.get("arguments", {})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = {}
        if not isinstance(arguments, dict):
            arguments = {}
        return tool_name, arguments

    def _execute_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            return self.tools[tool_name].run(**arguments)
        except Exception as exc:
            return {"error": f"工具执行失败：{exc}"}

    def _emit_tool_event(
        self,
        phase: str,
        round_number: int,
        tool_name: str,
        arguments: dict[str, Any],
        result: dict[str, Any] | None = None,
    ) -> None:
        if self.on_tool_event:
            self.on_tool_event(ToolExecutionEvent(phase, round_number, tool_name, arguments, result))

    def _emit_model_event(
        self,
        phase: str,
        round_number: int,
        messages: list[dict[str, Any]],
        response: dict[str, Any] | None = None,
    ) -> None:
        if self.on_model_event:
            self.on_model_event(ModelExecutionEvent(phase, round_number, messages.copy(), response))
