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
