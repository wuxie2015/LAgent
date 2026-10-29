from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from ..domain.memory import MemoryPolicy


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
    log_level: str
    memory_enabled: bool = True
    memory_dir: Path = Path(__file__).resolve().parents[2] / "memory"
    memory_model: str = ""
    memory_debug_content: bool = False
    memory_policy: MemoryPolicy = field(default_factory=MemoryPolicy)

    @classmethod
    def from_env(cls) -> "Settings":
        _load_dotenv()
        return cls(
            ollama_host=os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/"),
            model=os.getenv("OLLAMA_MODEL", "gpt-oss:120b-cloud"),
            tavily_api_key=os.getenv("TAVILY_API_KEY") or None,
            search_result_limit=int(os.getenv("SEARCH_RESULT_LIMIT", "5")),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            memory_enabled=_boolean("MEMORY_ENABLED", True),
            memory_dir=_memory_directory(),
            memory_model=os.getenv("MEMORY_MODEL", ""),
            memory_debug_content=_boolean("MEMORY_DEBUG_CONTENT", False),
            memory_policy=MemoryPolicy(**{
                name: int(os.getenv("MEMORY_" + name.upper(), str(default)))
                for name, default in (("recent_turns", 10), ("context_max_chars", 48000),
                    ("facts_max_chars", 8000), ("tasks_max_chars", 6000),
                    ("tool_context_max_chars", 4000), ("extraction_max_chars", 24000),
                    ("maintenance_calls", 2), ("maintenance_timeout", 30),
                    ("backlog_warning_turns", 30))
            }),
        )


def _boolean(name: str, default: bool) -> bool:
    value = os.getenv(name, str(default)).lower()
    if value not in {"true", "false", "1", "0"}:
        raise ValueError(f"{name}必须是true或false")
    return value in {"true", "1"}


def _memory_directory() -> Path:
    root = Path(__file__).resolve().parents[2]
    configured = Path(os.getenv("MEMORY_DIR", "memory")).expanduser()
    return (configured if configured.is_absolute() else root / configured).resolve()
