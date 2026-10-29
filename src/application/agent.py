from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from ..common.logging_config import get_logger
from ..domain import AgentTool, ModelExecutionEvent, ModelGateway, ToolExecutionEvent


class AgentStateError(RuntimeError):
    pass


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
    ) -> None:
        self.client = client
        self.tools = {tool.name: tool for tool in tools}
        self.system_prompt = system_prompt
        self.on_tool_event = on_tool_event
        self.on_model_event = on_model_event
        self.messages: list[dict[str, Any]] = []
        self.reset()

    def reset(self) -> None:
        self.messages = [{"role": "system", "content": self.system_prompt}]

    def ask(self, text: str) -> str:
        logger = get_logger()
        self.messages.append({"role": "user", "content": text})
        logger.info("agent_turn_started message_count=%d", len(self.messages))

        for round_number in range(1, self.max_tool_rounds + 1):
            self._emit_model_event("requested", round_number, self.messages)
            response = self.client.chat(self.messages, [tool.definition for tool in self.tools.values()])
            self._emit_model_event("completed", round_number, self.messages, response)
            message = response.get("message", {})
            self.messages.append(message)
            calls = message.get("tool_calls") or []
            if not calls:
                logger.info("agent_turn_completed tool_rounds=%d", round_number - 1)
                return message.get("content", "模型没有返回文本回复。")

            tool_calls = [self._parse_tool_call(call) for call in calls]
            for tool_name, arguments in tool_calls:
                logger.info("agent_tool_call round=%d tool=%s", round_number, tool_name)
                self._emit_tool_event("started", round_number, tool_name, arguments)

            with ThreadPoolExecutor(max_workers=len(tool_calls)) as executor:
                results = list(
                    executor.map(lambda item: self._execute_tool(*item), tool_calls)
                )

            for (tool_name, arguments), result in zip(tool_calls, results):
                self._emit_tool_event("completed", round_number, tool_name, arguments, result)
                self.messages.append(
                    {
                        "role": "tool",
                        "content": json.dumps(
                            {"tool": tool_name, "arguments": arguments, "result": result}, ensure_ascii=False
                        ),
                    }
                )

        logger.warning("agent_tool_round_limit_reached limit=%d", self.max_tool_rounds)
        return "已达到工具调用上限，仍未获得足够结果。请缩小目标或稍后重试。"

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
        # Current tools accept a search query. Future tools can receive their own argument adapter here.
        return self.tools[tool_name].run(str(arguments.get("query", "")))

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
