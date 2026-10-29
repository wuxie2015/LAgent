from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
import hashlib
import json
from typing import Any
from uuid import uuid4


class MemoryError(RuntimeError):
    pass


class StorageError(MemoryError):
    pass


class ContextCapacityError(MemoryError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def uid(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


@dataclass
class MemoryState:
    schema_version: int = 1
    revision: int = 0
    memory_epoch: str = field(default_factory=lambda: uid("epoch"))
    created_at: str = field(default_factory=now)
    updated_at: str = field(default_factory=now)
    scope: dict = field(default_factory=lambda: {"kind": "project", "id": "project", "root": ""})
    next_sequence: int = 1
    recent_turns: list[dict] = field(default_factory=list)
    facts: list[dict] = field(default_factory=list)
    tasks: list[dict] = field(default_factory=list)
    active_task_id: str | None = None
    compaction: dict = field(default_factory=lambda: {"jobs": [], "receipts": []})
    archives: list[dict] = field(default_factory=list)
    forgotten: list[dict] = field(default_factory=list)
    maintenance: dict = field(default_factory=lambda: {"pending_turn_ids": [], "last_error_code": None})
    migration: dict | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> MemoryState:
        try:
            if not isinstance(data, dict) or type(data.get("schema_version")) is not int or data["schema_version"] != 1:
                raise ValueError("不支持的记忆schema_version")
            state = cls(**data)
            state.validate()
            return state
        except (TypeError, ValueError, KeyError, AttributeError) as exc:
            raise MemoryError(f"记忆状态格式错误：{exc}") from exc

    def validate(self) -> None:
        from .conversation import validate_turn
        if type(self.revision) is not int or self.revision < 0 or type(self.next_sequence) is not int or self.next_sequence < 1:
            raise ValueError("revision/sequence无效")
        if not isinstance(self.memory_epoch, str) or not self.memory_epoch:
            raise ValueError("epoch无效")
        if not isinstance(self.scope, dict) or not isinstance(self.scope.get("id"), str):
            raise ValueError("scope无效")
        for name in ("recent_turns", "facts", "tasks", "archives", "forgotten"):
            if not isinstance(getattr(self, name), list) or any(not isinstance(x, dict) for x in getattr(self, name)):
                raise ValueError(f"{name}必须是对象列表")
        ids = []
        sequences = []
        running = 0
        for turn in self.recent_turns:
            validate_turn(turn)
            ids.append(turn["id"])
            sequences.append(turn["sequence"])
            running += turn["status"] == "running"
        if len(set(ids)) != len(ids) or sequences != sorted(set(sequences)) or running > 1:
            raise ValueError("轮次ID/顺序/活动轮次不合法")
        if sequences and self.next_sequence <= max(sequences):
            raise ValueError("next_sequence必须大于现有序号")
        for collection in (self.facts, self.tasks):
            seen = set()
            for item in collection:
                if not isinstance(item.get("id"), str) or item["id"] in seen:
                    raise ValueError("事实或任务ID重复/缺失")
                seen.add(item["id"])
                if not isinstance(item.get("evidence", item.get("completion_evidence", [])), list):
                    raise ValueError("证据必须为列表")
        for fact in self.facts:
            if fact.get("status") not in {"active", "disputed", "superseded"} or not all(k in fact for k in ("scope_id", "subject", "predicate", "value", "evidence")):
                raise ValueError("事实字段无效")
            if any(not isinstance(fact[k], str) or not fact[k] for k in ("scope_id", "subject", "predicate", "value")):
                raise ValueError("事实文本字段无效")
            if fact.get("kind") not in {"user_fact", "user_preference", "project_fact", "environment_fact"} or fact.get("source_type") not in {"user_asserted", "user_confirmed", "tool_observed"}:
                raise ValueError("事实类型/来源无效")
            expiry = fact.get("valid_until")
            if expiry is not None and (not isinstance(expiry, str) or datetime.fromisoformat(expiry.replace("Z", "+00:00")).tzinfo is None):
                raise ValueError("事实过期时间无效")
            if not fact["evidence"] or any(not isinstance(e, dict) or not isinstance(e.get("turn_id"), str) or not isinstance(e.get("message_id"), str) for e in fact["evidence"]):
                raise ValueError("事实证据无效")
        for task in self.tasks:
            if task.get("status") not in {"in_progress", "waiting_user", "blocked", "completed", "cancelled"} or not isinstance(task.get("goal"), str):
                raise ValueError("任务字段无效")
            if not isinstance(task.get("steps", []), list) or not isinstance(task.get("blockers", []), list):
                raise ValueError("任务步骤/阻碍无效")
        if self.active_task_id is not None and not any(t["id"] == self.active_task_id for t in self.tasks):
            raise ValueError("活动任务引用不存在")
        if not isinstance(self.compaction, dict) or any(not isinstance(self.compaction.get(k), list) for k in ("jobs", "receipts")):
            raise ValueError("压缩记录无效")
        if not isinstance(self.maintenance, dict) or not isinstance(self.maintenance.get("pending_turn_ids"), list):
            raise ValueError("维护记录无效")

        recent_ids = {t["id"] for t in self.recent_turns}
        archived_ids = set()
        for ref in self.archives:
            if not {"turn_id", "path", "hash", "epoch"} <= set(ref) or not isinstance(ref["turn_id"], str) or ref["turn_id"] in archived_ids or ref["turn_id"] in recent_ids:
                raise ValueError("归档引用重复或无效")
            archived_ids.add(ref["turn_id"])
        job_ids = set()
        for job in self.compaction["jobs"]:
            if not isinstance(job, dict) or not isinstance(job.get("job_id"), str) or job["job_id"] in job_ids or job.get("turn_id") not in recent_ids:
                raise ValueError("压缩任务引用无效")
            job_ids.add(job["job_id"])
            if type(job.get("next_fragment")) is not int or job["next_fragment"] < 0 or not isinstance(job.get("proposals"), list):
                raise ValueError("压缩检查点无效")
        receipt_ids = set()
        for receipt in self.compaction["receipts"]:
            if not isinstance(receipt, dict) or receipt.get("turn_id") not in archived_ids or receipt.get("job_id") in receipt_ids:
                raise ValueError("压缩回执无效")
            receipt_ids.add(receipt.get("job_id"))
        for item in self.forgotten:
            if not isinstance(item.get("key"), str) or type(item.get("through_sequence")) is not int:
                raise ValueError("遗忘标记无效")
