from __future__ import annotations

from copy import deepcopy
import json
import re
import time

from ..domain.memory import MemoryError, MemoryPolicy, MemoryState
from ..domain.memory.conversation import new_turn, message_record
from ..domain.memory.fact import merge_facts, fact_key
from ..domain.memory.task import apply_tasks
from ..domain.memory.privacy import redact
from ..domain.memory.state import digest, now, uid, StorageError
from .context_builder import ContextBuilder, compact


class MemoryManager:
    def __init__(self, system_prompt: str, store=None, extractor=None, policy: MemoryPolicy | None = None):
        self.system_prompt = system_prompt
        self.store = store
        self.extractor = extractor
        self.policy = policy or MemoryPolicy()
        self.builder = ContextBuilder(self.policy)
        self.state = store.load() if store else MemoryState()
        self.notices = []
        self._runtime_messages = {}
        if store:
            migrated = store.migrate(deepcopy(self.state))
            if migrated.migration and not self.state.migration:
                self.notices.append("旧history.json已迁移；旧轮次将有界压缩，不会全部发送模型。")
            self.state = migrated
        self._recover()

    def _commit(self, candidate: MemoryState):
        expected = self.state.revision
        candidate.revision = expected + 1
        candidate.updated_at = now()
        overflow = max(0, len(candidate.recent_turns) - self.policy.recent_turns)
        candidate.maintenance["pending_turn_ids"] = [t["id"] for i, t in enumerate(candidate.recent_turns)
            if t["status"] != "running" and (t["task_update_status"] != "applied" or i < overflow)]
        candidate.validate()
        if self.store:
            self.store.commit(candidate, expected)
        self.state = candidate

    def _recover(self):
        candidate = deepcopy(self.state)
        changed = False
        for turn in candidate.recent_turns:
            if turn["status"] == "running":
                turn.update(status="interrupted", ended_at=now(), failure={"type": "process_interrupted"})
                changed = True
                self.notices.append("上次请求被中断，工具可能已执行；不会自动重放，请先检查实际状态。")
            for execution in turn["tool_executions"]:
                if execution["status"] in {"running", "pending", "unknown"}:
                    execution["status"] = "unknown"
                    changed = True
                    for task in candidate.tasks:
                        if task["id"] in turn["task_ids"] or task["id"] == candidate.active_task_id:
                            task.update(status="blocked", blockers=["上次工具结果未知，需核查实际状态"], updated_at=now())
        if candidate.maintenance.get("purge_pending") and self.store:
            failures = self.store.purge()
            candidate.maintenance["purge_pending"] = bool(failures)
            candidate.maintenance["purge_residuals"] = failures
            changed = True
            if failures:
                self.notices.append("逻辑清除完成，物理清理未完成：" + ", ".join(failures))
        if changed:
            self._commit(candidate)

    @property
    def messages(self):
        messages = [{"role": "system", "content": self.system_prompt}]
        for turn in self.state.recent_turns:
            for record in turn["messages"]:
                messages.append({k: deepcopy(v) for k, v in record.items() if k in {"role", "content", "tool_calls"}})
        return messages

    @property
    def current(self):
        return next((t for t in self.state.recent_turns if t["status"] == "running"), None)

    def begin_turn(self, text: str):
        if self.state.maintenance.get("purge_pending"):
            raise StorageError("物理清理尚未完成，不能开始新任务；请重启重试清理")
        if self.current:
            raise MemoryError("已有进行中的轮次")
        candidate = deepcopy(self.state)
        turn = new_turn(redact(text), candidate.next_sequence)
        candidate.next_sequence += 1
        if candidate.active_task_id:
            turn["task_ids"] = [candidate.active_task_id]
        candidate.recent_turns.append(turn)
        self._commit(candidate)
        self._runtime_messages[turn["user_message_id"]] = {"role": "user", "content": text}
        return turn["id"]

    def append_message(self, message: dict):
        if not self.current:
            raise MemoryError("没有活动轮次")
        candidate = deepcopy(self.state)
        turn = candidate.recent_turns[-1]
        try:
            record = message_record(redact(message))
        except ValueError as exc:
            raise MemoryError(str(exc)) from exc
        turn["messages"].append(record)
        self._commit(candidate)
        self._runtime_messages[record["id"]] = deepcopy(message)
        return record["id"]

    def record_tool_starts(self, requests: list[tuple[str, dict]], message_id: str) -> list[str]:
        candidate = deepcopy(self.state)
        turn = candidate.recent_turns[-1]
        ids = []
        for name, arguments in requests:
            execution_id = uid("exec")
            turn["tool_executions"].append({"execution_id": execution_id, "message_id": message_id,
                "name": name, "arguments": redact(arguments), "status": "running", "started_at": now(), "ended_at": None, "result_message_id": None})
            ids.append(execution_id)
        self._commit(candidate)
        return ids

    def record_tool_result(self, execution_id: str, name: str, arguments: dict, result: dict):
        candidate = deepcopy(self.state)
        turn = candidate.recent_turns[-1]
        record = message_record({"role": "tool", "tool_call_id": execution_id,
            "content": json.dumps(redact({"tool": name, "arguments": arguments, "result": result}), ensure_ascii=False)})
        turn["messages"].append(record)
        execution = next(e for e in turn["tool_executions"] if e["execution_id"] == execution_id)
        failed = bool(result.get("error") or result.get("timed_out") or result.get("exit_code", 0) != 0)
        execution.update(status="failed" if failed else "succeeded", ended_at=now(), result_message_id=record["id"])
        self._commit(candidate)

    def finish_turn(self, status: str, failure: str | None = None):
        if self.current is None:
            return
        candidate = deepcopy(self.state)
        turn = candidate.recent_turns[-1]
        turn.update(status=status, ended_at=now(), failure={"type": failure} if failure else None)
        if status == "completed":
            turn["final_message_id"] = turn["messages"][-1]["id"]
        for execution in turn["tool_executions"]:
            if execution["status"] in {"pending", "running"}:
                execution["status"] = "unknown"
        if status in {"failed", "interrupted"} and any(e["status"] == "unknown" for e in turn["tool_executions"]):
            for task in candidate.tasks:
                if task["id"] in turn["task_ids"]:
                    task.update(status="blocked", blockers=["工具结果未知，继续前需核查实际状态"], updated_at=now())
        self._commit(candidate)
        self._runtime_messages.clear()

    def build_context(self, tools: list[dict]):
        # Credentials needed for this live request stay in memory only, never on disk.
        state = deepcopy(self.state)
        if self.current:
            for message in state.recent_turns[-1]["messages"]:
                message.update(self._runtime_messages.get(message["id"], {}))
        return self.builder.build(state, self.system_prompt, tools)

    def _batch(self, turn: dict, kind: str, payload: dict | None = None):
        related = [f for f in self.state.facts if f["status"] != "superseded" and f["scope_id"] in {self.state.scope["id"], "user"}]
        related.sort(key=lambda f: f.get("updated_at", ""), reverse=True)
        selected = []
        for fact in related:
            if len(json.dumps(selected + [fact], ensure_ascii=False)) < self.policy.facts_max_chars // 2:
                selected.append(fact)
        batch = {"schema_version": 1, "scope_id": self.state.scope["id"], "source_turn_ids": [turn["id"]],
                 "turn": payload or turn, "related_facts": selected if kind == "facts" else [],
                 "tasks": [] , "forgotten": []}
        if kind == "tasks":
            available = max(500, self.policy.extraction_max_chars // 4)
            for task in sorted(self.state.tasks, key=lambda t: t["id"] == self.state.active_task_id, reverse=True):
                if task["status"] in {"completed", "cancelled"} and task["id"] not in turn["task_ids"]:
                    continue
                projection = {k: task[k] for k in ("id", "goal", "status", "next_step", "blockers") if k in task}
                if len(json.dumps(batch["tasks"] + [projection], ensure_ascii=False)) < available:
                    batch["tasks"].append(projection)
            slim = {k: deepcopy(turn[k]) for k in ("id", "sequence", "status", "user_message_id", "final_message_id", "task_ids")}
            slim["tool_executions"] = [{k: e[k] for k in ("execution_id", "name", "status", "result_message_id")} for e in turn["tool_executions"]]
            message_limit = max(128, available // max(1, len(turn["messages"])))
            slim["messages"] = [{"id": m["id"], "role": m["role"], "content": compact(m.get("content", ""), message_limit)} for m in turn["messages"]]
            batch["turn"] = slim
        return batch

    def _fragments(self, turn: dict) -> list[dict]:
        # Extract chunks with stable original message IDs. Applying waits for every chunk.
        limit = self.policy.extraction_max_chars // 2
        chunks = []
        records = []
        for message in turn["messages"]:
            text = message.get("content", "")
            if len(json.dumps(message, ensure_ascii=False)) <= limit:
                records.append(message)
                continue
            if message.get("tool_calls"):
                raise MemoryError("单个工具调用参数超出提取预算；保留原始轮次")
            step = max(128, limit // 2)
            for offset in range(0, len(text), step):
                records.append(dict(message, content=text[offset:offset + step], fragment_offset=offset))
        current = []
        for record in records:
            if current and len(json.dumps(current + [record], ensure_ascii=False)) > limit:
                chunks.append(current)
                current = []
            current.append(record)
        if current:
            chunks.append(current)
        metadata = {k: turn[k] for k in ("id", "status", "sequence", "user_message_id")}
        return [dict(metadata, messages=messages, fragment_index=i, fragment_count=len(chunks)) for i, messages in enumerate(chunks)]

    def _error(self, code: str, turn_id: str | None = None):
        candidate = deepcopy(self.state)
        candidate.maintenance["last_error_code"] = code
        target = next((t for t in candidate.recent_turns if t["id"] == turn_id), None)
        if target and target["task_update_status"] != "applied":
            target["task_update_status"] = "failed"
        if turn_id and turn_id not in candidate.maintenance["pending_turn_ids"]:
            candidate.maintenance["pending_turn_ids"].append(turn_id)
        self._commit(candidate)
        self.notices.append("记忆维护暂未完成，原始历史仍保留：" + code)

    def maintain(self):
        if self.current:
            raise MemoryError("活动轮次不能压缩")
        calls = 0
        deadline = time.monotonic() + self.policy.maintenance_timeout * self.policy.maintenance_calls
        try:
            # Old failed updates first. Current turn is also eligible; ordinary conversation can skip.
            pending = [t for t in self.state.recent_turns if t["task_update_status"] != "applied"]
            for turn in pending:
                if calls >= self.policy.maintenance_calls or time.monotonic() >= deadline:
                    break
                text = turn["messages"][0]["content"]
                relevant = bool(any(t["status"] not in {"completed", "cancelled"} for t in self.state.tasks) or turn["tool_executions"] or re.search(r"实现|修改|创建|开发|排查|修复|整理|调研|制作|完成|执行|继续|取消|停止|build|create|implement|fix|continue|cancel", text, re.I))
                changes = []
                if self.extractor and relevant:
                    calls += 1
                    changes = self.extractor.propose_task_changes(self._batch(turn, "tasks"))
                    if not changes and re.search(r"创建|实现|修改|制作|create|implement", text, re.I) and re.search(r"等我|待我|等待我|wait for my", text, re.I):
                        raise MemoryError("明确待完成任务未产生状态，保留轮次待重试")
                candidate = deepcopy(self.state)
                target = next(t for t in candidate.recent_turns if t["id"] == turn["id"])
                apply_tasks(candidate, changes, target)
                target["task_update_status"] = "applied"
                self._commit(candidate)
            while len(self.state.recent_turns) > self.policy.recent_turns:
                if self.extractor is None or calls >= self.policy.maintenance_calls or time.monotonic() >= deadline:
                    break
                turn = self.state.recent_turns[0]
                if turn["task_update_status"] != "applied":
                    break
                checksum = digest(turn)
                job_id = digest({"turn": turn["id"], "hash": checksum, "policy": 1})
                candidate = deepcopy(self.state)
                job = next((j for j in candidate.compaction["jobs"] if j["job_id"] == job_id), None)
                if job is None:
                    job = {"job_id": job_id, "turn_id": turn["id"], "source_hash": checksum, "policy_version": 1, "status": "pending", "attempts": 0, "next_fragment": 0, "proposals": []}
                    candidate.compaction["jobs"].append(job)
                job["attempts"] += 1
                self._commit(candidate)
                fragments = self._fragments(turn)
                job = next(j for j in self.state.compaction["jobs"] if j["job_id"] == job_id)
                while job["next_fragment"] < len(fragments) and calls < self.policy.maintenance_calls and time.monotonic() < deadline:
                    fragment = fragments[job["next_fragment"]]
                    calls += 1
                    proposals = self.extractor.extract_facts(self._batch(turn, "facts", fragment))
                    # Validate now, before saving proposals or accepting the next fragment.
                    probe = deepcopy(self.state)
                    merge_facts(probe, proposals, [turn])
                    candidate = deepcopy(self.state)
                    update = next(j for j in candidate.compaction["jobs"] if j["job_id"] == job_id)
                    update["proposals"].extend(proposals)
                    update["next_fragment"] += 1
                    self._commit(candidate)
                    job = update
                if job["next_fragment"] < len(fragments):
                    break
                candidate = deepcopy(self.state)
                fact_ids = merge_facts(candidate, job["proposals"], [turn])
                ref = self.store.archive(turn, candidate.memory_epoch) if self.store else {"turn_id": turn["id"], "hash": checksum, "epoch": candidate.memory_epoch, "path": None}
                candidate.archives.append(ref)
                candidate.compaction["receipts"].append({"job_id": job_id, "turn_id": turn["id"], "source_hash": checksum, "applied_revision": candidate.revision + 1, "fact_ids": fact_ids, "archive": ref})
                candidate.compaction["jobs"] = [j for j in candidate.compaction["jobs"] if j["job_id"] != job_id]
                candidate.recent_turns.pop(0)
                candidate.maintenance["pending_turn_ids"] = [x for x in candidate.maintenance["pending_turn_ids"] if x != turn["id"]]
                self._commit(candidate)
            if self.state.maintenance["last_error_code"] and all(t["task_update_status"] == "applied" for t in self.state.recent_turns) and len(self.state.recent_turns) <= self.policy.recent_turns:
                candidate = deepcopy(self.state)
                candidate.maintenance.update(last_error_code=None, pending_turn_ids=[])
                self._commit(candidate)
        except MemoryError:
            # Storage failures must stop the agent; extractor/validation failures are recoverable.
            raise
        except Exception as exc:
            raise MemoryError("模型维护失败：" + type(exc).__name__) from exc
        if len(self.state.recent_turns) >= self.policy.backlog_warning_turns:
            self.notices.append("记忆积压达到阈值，本次只加载容量内的最近对话；可用 /memory retry 重试。")

    def maintain_safely(self):
        try:
            self.maintain()
        except StorageError:
            raise
        except MemoryError as exc:
            # Save the failure classification only; no raw potentially private model output in logs.
            target = next((t for t in self.state.recent_turns if t["task_update_status"] != "applied"), None)
            if target is None and len(self.state.recent_turns) > self.policy.recent_turns:
                target = self.state.recent_turns[0]
            self._error(type(exc.__cause__ or exc).__name__, target["id"] if target else None)

    def reset(self):
        candidate = deepcopy(self.state)
        candidate.recent_turns = []
        candidate.compaction["jobs"] = []
        candidate.active_task_id = None
        candidate.maintenance = {"pending_turn_ids": [], "last_error_code": None}
        for task in candidate.tasks:
            if task["status"] not in {"completed", "cancelled"}:
                task.update(status="blocked", blockers=["对话已重置，继续前需核查状态和授权"], updated_at=now())
        self._commit(candidate)
        self._runtime_messages.clear()

    def forget_fact(self, fact_id: str):
        candidate = deepcopy(self.state)
        fact = next((f for f in candidate.facts if f["id"] == fact_id), None)
        if fact is None:
            raise MemoryError("事实ID不存在")
        key = fact_key(fact)
        # Remove all versions of this attribute; no hidden superseded value should reactivate.
        candidate.facts = [f for f in candidate.facts if fact_key(f) != key]
        candidate.forgotten.append({"key": key, "through_sequence": candidate.next_sequence - 1, "deleted_at": now()})
        # Pending proposals must not retain the forgotten value.
        for job in candidate.compaction["jobs"]:
            job["proposals"] = [p for p in job["proposals"] if fact_key(dict(p, scope_id=p.get("scope_id", candidate.scope["id"]))) != key]
        self._commit(candidate)

    def clear_all(self):
        self._runtime_messages.clear()
        candidate = MemoryState(scope=deepcopy(self.state.scope))
        candidate.maintenance["purge_pending"] = True
        self._commit(candidate)
        failures = self.store.purge() if self.store else []
        candidate = deepcopy(self.state)
        candidate.maintenance.update(purge_pending=bool(failures), purge_residuals=failures)
        self._commit(candidate)
        return failures

    def use_task(self, task_id: str):
        candidate = deepcopy(self.state)
        task = next((t for t in candidate.tasks if t["id"] == task_id and t["status"] not in {"completed", "cancelled"}), None)
        if task is None:
            raise MemoryError("未完成任务ID不存在")
        candidate.active_task_id = task_id
        self._commit(candidate)

    def cancel_task(self, task_id: str):
        candidate = deepcopy(self.state)
        task = next((t for t in candidate.tasks if t["id"] == task_id), None)
        if task is None:
            raise MemoryError("任务ID不存在")
        task.update(status="cancelled", updated_at=now())
        if candidate.active_task_id == task_id:
            candidate.active_task_id = None
        self._commit(candidate)

    def summary(self):
        return {"recent_turns": len(self.state.recent_turns), "facts": len(self.state.facts), "unfinished_tasks": sum(t["status"] not in {"completed", "cancelled"} for t in self.state.tasks), "active_task_id": self.state.active_task_id, "maintenance": self.state.maintenance, "revision": self.state.revision, "archives": len(self.state.archives), "orphan_archives": self.store.orphan_count(self.state) if self.store else 0}
