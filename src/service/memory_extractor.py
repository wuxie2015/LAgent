from __future__ import annotations

import json
import time
from pathlib import Path

from ..domain.memory import MemoryError
from ..common.logging_config import get_logger


class ModelMemoryExtractor:
    def __init__(self, client, max_chars: int = 24000):
        self.client = client
        self.max_chars = max_chars

    def _extract(self, name: str, batch: dict, field: str, maximum: int) -> list[dict]:
        prompt = (Path(__file__).resolve().parents[2] / "resource" / "prompt" / name).read_text(encoding="utf-8")
        turn = batch.get("turn", {})
        sources = [{"turn_id": turn.get("id"), "message_id": m["id"]} for m in turn.get("messages", [])]
        contract = {"schema_version": 1, "source_turn_ids": batch["source_turn_ids"], "allowed_evidence": sources,
                    "user_message_ids": [m["id"] for m in turn.get("messages", []) if m["role"] == "user"]}
        prompt += "\n本次唯一合法的响应头和来源清单（ID必须逐字复制，不得从参考属性复制旧来源）：\n" + json.dumps(contract, ensure_ascii=False)
        payload = json.dumps(batch, ensure_ascii=False)
        if len(payload) + len(prompt) > self.max_chars:
            raise MemoryError("提取输入超出预算，需要继续分片")
        started = time.monotonic()
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
            # Normalize only redundant vendor formatting with independently verified IDs/roles.
            actual = {(turn.get("id"), m["id"]): m["role"] for m in turn.get("messages", [])}
            for value in values:
                owners = [value] + (value.get("steps", []) if isinstance(value.get("steps", []), list) else [])
                for owner in owners:
                    if not isinstance(owner, dict):
                        continue
                    for key in ("evidence", "completion_evidence"):
                        refs = owner.get(key, [])
                        if not isinstance(refs, list):
                            continue
                        for index, ref in enumerate(refs):
                            if isinstance(ref, str):
                                if (turn.get("id"), ref) not in actual:
                                    raise ValueError("证据消息ID不存在")
                                ref = {"turn_id": turn["id"], "message_id": ref}
                                refs[index] = ref
                            if isinstance(ref, dict) and "role" in ref:
                                if actual.get((ref.get("turn_id"), ref.get("message_id"))) != ref["role"]:
                                    raise ValueError("证据角色与实际消息不符")
                                del ref["role"]
            get_logger().info("memory_extraction kind=%s turn_ids=%s count=%d elapsed_seconds=%.3f", field, batch["source_turn_ids"], len(values), time.monotonic() - started)
            return values
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise MemoryError(f"记忆提取响应无效：{type(exc).__name__}") from exc

    def extract_facts(self, batch: dict) -> list[dict]:
        return self._extract("memory_extract.md", batch, "facts", 20)

    def propose_task_changes(self, batch: dict) -> list[dict]:
        return self._extract("task_update.md", batch, "changes", 10)
