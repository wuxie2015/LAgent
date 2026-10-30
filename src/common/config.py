from __future__ import annotations

import os
import math
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
    scheduler_enabled: bool = False
    schedule_db: Path = Path(__file__).resolve().parents[2] / "data" / "schedules.sqlite3"
    schedule_timezone: str = "Asia/Tokyo"
    schedule_default_channel: str = "console"
    schedule_poll_interval: float = 1.0
    schedule_grace_seconds: int = 300
    schedule_max_notification_attempts: int = 5
    notification_console_enabled: bool = True
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_from: str = ""
    smtp_to: str = ""
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_security: str = "starttls"
    notification_webhook_url: str = ""
    notification_webhook_token: str = ""
    notification_timeout: float = 15.0

    @classmethod
    def from_env(cls) -> "Settings":
        _load_dotenv()
        root = Path(__file__).resolve().parents[2]
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
            scheduler_enabled=_boolean("SCHEDULER_ENABLED", True),
            schedule_db=_path_from_root("SCHEDULE_DB", "data/schedules.sqlite3", root),
            schedule_timezone=_timezone("SCHEDULE_TIMEZONE", "Asia/Tokyo"),
            schedule_default_channel=_nonempty("SCHEDULE_DEFAULT_CHANNEL", "console"),
            schedule_poll_interval=_positive_float("SCHEDULE_POLL_INTERVAL", 1.0),
            schedule_grace_seconds=_bounded_int("SCHEDULE_GRACE_SECONDS", 300, 0, 31_536_000),
            schedule_max_notification_attempts=_positive_int("SCHEDULE_MAX_NOTIFICATION_ATTEMPTS", 5),
            notification_console_enabled=_boolean("NOTIFICATION_CONSOLE_ENABLED", True),
            smtp_host=os.getenv("SMTP_HOST", ""),
            smtp_port=_bounded_int("SMTP_PORT", 587, 1, 65535),
            smtp_from=os.getenv("SMTP_FROM", ""),
            smtp_to=os.getenv("SMTP_TO", ""),
            smtp_user=os.getenv("SMTP_USER", ""),
            smtp_password=os.getenv("SMTP_PASSWORD", ""),
            smtp_security=os.getenv("SMTP_SECURITY", "starttls"),
            notification_webhook_url=os.getenv("NOTIFICATION_WEBHOOK_URL", ""),
            notification_webhook_token=os.getenv("NOTIFICATION_WEBHOOK_TOKEN", ""),
            notification_timeout=_bounded_float("NOTIFICATION_TIMEOUT", 15.0, 0.01, 300),
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


def _path_from_root(name: str, default: str, root: Path) -> Path:
    configured = Path(os.getenv(name, default)).expanduser()
    return (configured if configured.is_absolute() else root / configured).resolve()


def _positive_float(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name}必须是正数") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name}必须是正数")
    return value


def _positive_int(name: str, default: int, allow_zero: bool = False) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name}必须是{'非负' if allow_zero else '正'}整数") from exc
    if value < (0 if allow_zero else 1):
        raise ValueError(f"{name}必须是{'非负' if allow_zero else '正'}整数")
    return value


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    value = _positive_int(name, default, allow_zero=minimum == 0)
    if not minimum <= value <= maximum:
        raise ValueError(f"{name}必须在{minimum}到{maximum}之间")
    return value


def _bounded_float(name: str, default: float, minimum: float, maximum: float) -> float:
    value = _positive_float(name, default)
    if not minimum <= value <= maximum:
        raise ValueError(f"{name}必须在{minimum}到{maximum}之间")
    return value


def _timezone(name: str, default: str) -> str:
    value = os.getenv(name, default)
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"{name}必须是有效的IANA时区") from exc
    return value


def _nonempty(name: str, default: str) -> str:
    value = os.getenv(name, default).strip()
    if not value:
        raise ValueError(f"{name}不能为空")
    return value
