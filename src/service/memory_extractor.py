from __future__ import annotations

import json
from pathlib import Path

from ..domain.memory import MemoryError


class ModelMemoryExtractor:
    def __init__(self, client, max_chars: int = 24000):
        self.client = client
        self.max_chars = max_chars

    def _extract(self, name: str, batch: dict, field: str, maximum: int) -> list[dict]:
        prompt = (Path(__file__).resolve().parents[2] / "resource" / "prompt" / name).read_text(encoding="utf-8")
        payload = json.dumps(batch, ensure_ascii=False)
        if len(payload) + len(prompt) > self.max_chars:
            raise MemoryError("提取输入超出预算，需要继续分片")
        response = self.client.chat([{"role": "system", "content": prompt}, {"role": "user", "content": payload}], [])
        try:
            message = response["message"]
            if message.get("tool_calls"):
                raise ValueError("提取器禁止调用工具")
            content = message["content"].strip()
            # Some providers wrap JSON despite the prompt; accept only a single fenced JSON document.
            if content.startswith("```json\n") and content.endswith("\n```"):
                content = content[8:-4]
            result = json.loads(content)
            if not isinstance(result, dict) or set(result) != {"schema_version", "source_turn_ids", field} or type(result["schema_version"]) is not int or result["schema_version"] != 1:
                raise ValueError("提取schema无效")
            if result["source_turn_ids"] != batch["source_turn_ids"]:
                raise ValueError("提取轮次来源不匹配")
            values = result[field]
            if not isinstance(values, list) or len(values) > maximum or any(not isinstance(v, dict) for v in values):
                raise ValueError("提取数量/结构无效")
            return values
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise MemoryError(f"记忆提取响应无效：{type(exc).__name__}") from exc

    def extract_facts(self, batch: dict) -> list[dict]:
        return self._extract("memory_extract.md", batch, "facts", 20)

    def propose_task_changes(self, batch: dict) -> list[dict]:
        return self._extract("task_update.md", batch, "changes", 10)
