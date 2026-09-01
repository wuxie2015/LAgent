from __future__ import annotations

from pathlib import Path


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
    return prompt
