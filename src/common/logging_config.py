from __future__ import annotations

import logging
from pathlib import Path


def get_logger() -> logging.Logger:
    """Return a logger that remains silent until the application configures it."""
    logger = logging.getLogger("lin_agent")
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
        logger.propagate = False
    return logger


def configure_logging(level: str = "INFO") -> logging.Logger:
    """Write operational logs without recording secrets or full conversation content."""
    logger = get_logger()
    if any(isinstance(handler, logging.FileHandler) for handler in logger.handlers):
        return logger

    project_root = Path(__file__).resolve().parents[2]
    log_dir = project_root / "data" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    handler = logging.FileHandler(log_dir / "lin-agent.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger.addHandler(handler)
    return logger
