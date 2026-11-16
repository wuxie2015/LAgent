from __future__ import annotations

import json
import time
from pathlib import Path

from ..domain.memory.state import MemoryValidationError
from ..domain.memory.fact import is_successful_shell_observation
from ..common.logging_config import get_logger


class ModelMemoryExtractor:
    def __init__(self, client, max_chars: int = 24000):
        self.client = client
        self.max_chars = max_chars

    def _extract(self, name: str, batch: dict, field: str, maximum: int) -> list[dict]:
        prompt = (Path(__file__).resolve().parents[2] / "resource" / "prompt" / name).read_text(encoding="utf-8")
        turn = batch.get("turn", {})
        sources = [{"turn_id": turn.get("id"), "message_id": m["id"]} for m in turn.get("messages", [])]
        user_sources = [ref for ref, m in zip(sources, turn.get("messages", [])) if m["role"] == "user"]
        shell_sources = [ref for ref, m in zip(sources, turn.get("messages", [])) if is_successful_shell_observation(m)]
        successful_results = {e.get("result_message_id") for e in turn.get("tool_executions", []) if e["status"] == "succeeded"}
        completion_sources = [ref for ref, m in zip(sources, turn.get("messages", []))
                              if turn.get("status") == "completed" and
                              (m["id"] == turn.get("final_message_id") or m["id"] in successful_results)]
        contract = {"schema_version": 1, "source_turn_ids": batch["source_turn_ids"], "allowed_evidence": sources,
                    "user_message_ids": [ref["message_id"] for ref in user_sources]}
        if field == "facts":
            contract["allowed_evidence"] = user_sources + shell_sources
            contract["source_type_allowed_evidence"] = {"user_asserted": user_sources, "user_confirmed": user_sources,
                                                        "tool_observed": shell_sources}
        else:
            contract["required_user_evidence"] = user_sources
            contract["allowed_completion_evidence"] = completion_sources
            contract["available_task_ids"] = [task["id"] for task in batch.get("tasks", [])]
            contract["completed_requires_tool_evidence"] = bool(turn.get("tool_executions"))
            contract["successful_tool_evidence"] = [ref for ref in completion_sources if ref["message_id"] in successful_results]
        prompt += "\n本次唯一合法的响应头和来源清单（ID必须逐字复制，不得从参考属性复制旧来源）：\n" + json.dumps(contract, ensure_ascii=False)
        payload = json.dumps(batch, ensure_ascii=False)
        if len(payload) + len(prompt) > self.max_chars:
            raise MemoryValidationError("提取输入超出预算，需要继续分片")
        started = time.monotonic()
        response = self.client.chat([{"role": "system", "content": prompt}, {"role": "user", "content": payload}], [])
        response_chars = None
        try:
            message = response["message"]
            if message.get("tool_calls"):
                raise MemoryValidationError("提取器禁止调用工具")
            content = message["content"].strip()
            response_chars = len(content)
            # Some providers wrap JSON despite the prompt; accept only a single fenced JSON document.
            if content.startswith("```json\n") and content.endswith("\n```"):
                content = content[8:-4]
            result = json.loads(content)
            if not isinstance(result, dict) or set(result) != {"schema_version", "source_turn_ids", field} or type(result["schema_version"]) is not int or result["schema_version"] != 1:
                raise MemoryValidationError("提取schema无效")
            if result["source_turn_ids"] != batch["source_turn_ids"]:
                raise MemoryValidationError("提取轮次来源不匹配")
            values = result[field]
            if not isinstance(values, list) or len(values) > maximum or any(not isinstance(v, dict) for v in values):
                raise MemoryValidationError("提取数量/结构无效")
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
                                    raise MemoryValidationError("证据消息ID不存在")
                                ref = {"turn_id": turn["id"], "message_id": ref}
                                refs[index] = ref
                            if isinstance(ref, dict) and "role" in ref:
                                if actual.get((ref.get("turn_id"), ref.get("message_id"))) != ref["role"]:
                                    raise MemoryValidationError("证据角色与实际消息不符")
                                del ref["role"]
            get_logger().info("memory_extraction kind=%s turn_ids=%s count=%d elapsed_seconds=%.3f", field, batch["source_turn_ids"], len(values), time.monotonic() - started)
            return values
        except (KeyError, TypeError, ValueError, AttributeError, MemoryValidationError) as exc:
            if isinstance(exc, MemoryValidationError):
                reason = str(exc)
            elif isinstance(exc, json.JSONDecodeError):
                reason = "记忆提取响应不是有效JSON"
            else:
                reason = "记忆提取响应结构无效：" + type(exc).__name__
            get_logger().warning("memory_extraction_failed kind=%s turn_ids=%s error=%s reason=%s "
                                 "response_chars=%s json_line=%s json_column=%s elapsed_seconds=%.3f",
                                 field, batch["source_turn_ids"], type(exc).__name__, reason, response_chars,
                                 exc.lineno if isinstance(exc, json.JSONDecodeError) else None,
                                 exc.colno if isinstance(exc, json.JSONDecodeError) else None, time.monotonic() - started)
            raise MemoryValidationError(reason) from exc

    def extract_facts(self, batch: dict) -> list[dict]:
        return self._extract("memory_extract.md", batch, "facts", 20)

    def propose_task_changes(self, batch: dict) -> list[dict]:
        return self._extract("task_update.md", batch, "changes", 10)
