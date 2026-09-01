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

    def run(self, query: str) -> dict[str, Any]:
        """Execute a read-only tool call and return JSON-serializable output."""
