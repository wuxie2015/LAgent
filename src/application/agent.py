from __future__ import annotations

import json
from typing import Any

from ..domain import AgentTool, ModelGateway


class AgentStateError(RuntimeError):
    pass


class Agent:
    """Application use case: automatically execute and evaluate tool calls."""

    max_tool_rounds = 8

    def __init__(self, client: ModelGateway, tools: list[AgentTool], system_prompt: str) -> None:
        self.client = client
        self.tools = {tool.name: tool for tool in tools}
        self.system_prompt = system_prompt
        self.messages: list[dict[str, Any]] = []
        self.reset()

    def reset(self) -> None:
        self.messages = [{"role": "system", "content": self.system_prompt}]

    def ask(self, text: str) -> str:
        self.messages.append({"role": "user", "content": text})

        for _ in range(self.max_tool_rounds):
            response = self.client.chat(self.messages, [tool.definition for tool in self.tools.values()])
            message = response.get("message", {})
            self.messages.append(message)
            calls = message.get("tool_calls") or []
            if not calls:
                return message.get("content", "模型没有返回文本回复。")

            for call in calls:
                tool_name, arguments = self._parse_tool_call(call)
                result = self._execute_tool(tool_name, arguments)
                self.messages.append(
                    {
                        "role": "tool",
                        "content": json.dumps(
                            {"tool": tool_name, "arguments": arguments, "result": result}, ensure_ascii=False
                        ),
                    }
                )

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
