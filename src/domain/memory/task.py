from __future__ import annotations

import re
from typing import TypedDict
from .fact import EvidenceRef

from .fact import evidence_messages
from .privacy import sensitive
from .state import MemoryState, MemoryError, now, uid


class TaskStep(TypedDict):
    id: str
    description: str
    status: str
    evidence: list[EvidenceRef]


class Task(TypedDict, total=False):
    id: str
    scope_id: str
    goal: str
    status: str
    origin_turn_id: str
    last_turn_id: str
    acceptance_criteria: list[str]
    steps: list[TaskStep]
    next_step: str | None
    blockers: list[str]
    waiting_for: str | None
    completion_evidence: list[EvidenceRef]
    created_at: str
    updated_at: str

TRANSITIONS = {
    "in_progress": {"in_progress", "waiting_user", "blocked", "completed", "cancelled"},
    "waiting_user": {"waiting_user", "in_progress", "cancelled"},
    "blocked": {"blocked", "in_progress", "cancelled"},
    "completed": {"completed", "in_progress"},
    "cancelled": {"cancelled", "in_progress"},
}


def apply_tasks(state: MemoryState, changes: list[dict], turn: dict) -> None:
    if len(changes) > 10:
        raise MemoryError("单轮任务变更过多")
    for c in changes:
        allowed = {"task_id", "goal", "status", "evidence", "completion_evidence", "acceptance_criteria", "steps", "next_step", "blockers", "waiting_for"}
        if not isinstance(c, dict) or set(c) - allowed or not {"goal", "status", "evidence"} <= set(c) or sensitive(c):
            raise MemoryError("任务候选字段无效或包含敏感信息")
        if not isinstance(c["goal"], str) or not 1 <= len(c["goal"]) <= 1000:
            raise MemoryError("任务目标无效")
        source = evidence_messages(c["evidence"], [turn])
        if not any(m["role"] == "user" for m in source):
            raise MemoryError("任务必须有用户目标来源")
        task = next((t for t in state.tasks if t["id"] == c.get("task_id")), None)
        if c.get("task_id") and task is None:
            raise MemoryError("任务ID不存在")
        status = c["status"]
        if status not in TRANSITIONS or (task and status not in TRANSITIONS[task["status"]]):
            raise MemoryError("非法任务状态转换")
        user_text = turn["messages"][0]["content"]
        if task and task["status"] in {"completed", "cancelled"} and status == "in_progress" and not any(
            w in user_text.casefold() for w in ("继续", "恢复", "返工", "重做", "未完成", "resume", "continue", "reopen")
        ):
            raise MemoryError("恢复已结束任务需要用户明确要求")
        if status == "cancelled" and not any(w in user_text.casefold() for w in ("取消", "停止", "不用做", "cancel", "stop")):
            raise MemoryError("取消任务需要用户来源")
        for name in ("acceptance_criteria", "blockers"):
            value = c.get(name, [])
            if not isinstance(value, list) or len(value) > 20 or any(not isinstance(x, str) or len(x) > 1000 for x in value):
                raise MemoryError("任务列表字段无效")
        for name in ("next_step", "waiting_for"):
            if c.get(name) is not None and (not isinstance(c[name], str) or len(c[name]) > 2000):
                raise MemoryError("任务文本无效")
        if status == "blocked" and not c.get("blockers"):
            raise MemoryError("阻塞任务需要原因")
        if status == "waiting_user" and not c.get("waiting_for"):
            raise MemoryError("等待用户需要说明缺少什么")
        completion = c.get("completion_evidence", [])
        if status == "completed":
            refs = evidence_messages(completion, [turn])
            if turn["status"] != "completed" or not any(m["role"] in {"assistant", "tool"} for m in refs):
                raise MemoryError("完成任务缺少成功轮次证据")
            executions = turn["tool_executions"]
            requires_execution = bool(re.search(r"(?:创建|修改|写入|删除|运行|执行|部署|安装).{0,15}(?:文件|目录|代码|命令|程序|服务)|(?:create|edit|write|delete|run|deploy|install).{0,25}(?:file|directory|code|command|service)", user_text, re.I))
            if requires_execution and not executions:
                raise MemoryError("本机操作任务缺少实际执行证据")
            if executions and (any(e["status"] != "succeeded" for e in executions) or not any(m["role"] == "tool" for m in refs)):
                raise MemoryError("执行任务需要成功工具结果证据")
        steps = c.get("steps", [])
        if not isinstance(steps, list) or len(steps) > 30:
            raise MemoryError("任务步骤无效")
        checked_steps = []
        for step in steps:
            if not isinstance(step, dict) or set(step) - {"description", "status", "evidence"} or not isinstance(step.get("description"), str) or step.get("status") not in {"pending", "in_progress", "completed", "failed"}:
                raise MemoryError("步骤结构无效")
            if step["status"] == "completed":
                refs = evidence_messages(step.get("evidence", []), [turn])
                if not any(m["role"] in {"assistant", "tool"} for m in refs):
                    raise MemoryError("完成步骤不能仅引用用户请求")
            checked_steps.append(dict(step, id=uid("step")))
        if task is None:
            task = {"id": uid("task"), "scope_id": state.scope["id"], "origin_turn_id": turn["id"], "created_at": now(), "steps": []}
            state.tasks.append(task)
        task.update({k: v for k, v in c.items() if k not in {"task_id", "evidence", "steps"}})
        task.update(last_turn_id=turn["id"], updated_at=now())
        if "steps" in c:
            task["steps"] = checked_steps
        if task["id"] not in turn["task_ids"]:
            turn["task_ids"].append(task["id"])
        if status not in {"completed", "cancelled"}:
            state.active_task_id = task["id"]
        elif state.active_task_id == task["id"]:
            state.active_task_id = None
