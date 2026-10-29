"""Domain contracts. These stay independent of Ollama and search vendors."""

from __future__ import annotations

from typing import Any, Protocol


class ModelGateway(Protocol):
    def chat(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Return one model turn, optionally containing tool calls."""


class AgentTool(Protocol):
    name: str
    definition: dict[str, Any]

    def run(self, **arguments: Any) -> dict[str, Any]:
        """Execute a tool call and return JSON-serializable output."""


class MemoryStore(Protocol):
    def load(self) -> Any: ...
    def commit(self, state: Any, expected_revision: int) -> None: ...
    def archive(self, turn: dict[str, Any], epoch: str) -> dict[str, Any]: ...
    def load_archived_turn(self, ref: dict[str, Any]) -> dict[str, Any]: ...


class MemoryExtractor(Protocol):
    def extract_facts(self, batch: dict[str, Any]) -> list[dict[str, Any]]: ...
    def propose_task_changes(self, batch: dict[str, Any]) -> list[dict[str, Any]]: ...
