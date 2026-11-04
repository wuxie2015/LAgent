from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ToolRequest:
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class AgentTurn:
    content: str
    tool_requests: tuple[ToolRequest, ...] = ()

    @property
    def awaits_tool_result(self) -> bool:
        return bool(self.tool_requests)


@dataclass(frozen=True)
class ToolExecutionEvent:
    phase: str
    round_number: int
    tool_name: str
    arguments: Any
    result: dict[str, Any] | None = None


@dataclass(frozen=True)
class ModelExecutionEvent:
    phase: str
    round_number: int
    messages: list[dict[str, Any]]
    response: dict[str, Any] | None = None
