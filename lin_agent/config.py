from __future__ import annotations

import os
from dataclasses import dataclass


def _load_dotenv(path: str = ".env") -> None:
    """Load a small .env file without making a runtime dependency mandatory."""
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    except FileNotFoundError:
        return

    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Settings:
    ollama_host: str
    model: str
    tavily_api_key: str | None
    search_result_limit: int

    @classmethod
    def from_env(cls) -> "Settings":
        _load_dotenv()
        return cls(
            ollama_host=os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/"),
            model=os.getenv("OLLAMA_MODEL", "gpt-oss:120b-cloud"),
            tavily_api_key=os.getenv("TAVILY_API_KEY") or None,
            search_result_limit=int(os.getenv("SEARCH_RESULT_LIMIT", "5")),
        )
