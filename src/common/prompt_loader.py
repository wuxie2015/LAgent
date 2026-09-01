from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


class PromptLoadError(RuntimeError):
    pass


def load_system_prompt() -> str:
    """Load the single system prompt from the project resource directory."""
    project_root = Path(__file__).resolve().parents[2]
    prompt_path = project_root / "resource" / "prompt" / "system.md"
    try:
        prompt = prompt_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise PromptLoadError(f"系统提示词不存在：{prompt_path}") from exc
    if not prompt:
        raise PromptLoadError(f"系统提示词为空：{prompt_path}")
    current_date = datetime.now(ZoneInfo("Asia/Tokyo")).date().isoformat()
    return prompt.replace("{{CURRENT_DATE}}", current_date)
