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


def is_task_request(text: str) -> bool:
    """State-changing work, rather than a factual correction or status question."""
    if re.search(r"明确更正|补充.*(?:事实|偏好)|默认.*(?:回复|标注)|时区.*偏好", text) and not re.search(r"修改.*(?:文件|代码)|实现|开发", text):
        return False
    if re.search(r"(?:是否|有哪|为什么|什么是|解释|列出.*(?:任务|作业)|现在.*吗|完成了吗|缺少什么|提供什么|状态是什么|已经.*吗)", text) and not re.search(r"请.*(?:创建|实现|修改|写完|取消|停止|部署|安装|运行|删除|写入|生成)", text):
        return False
    return bool(re.search(r"实现|修改|创建|开发|排查|修复|整理|调研|制作|完成|执行|继续|取消|停止|写完|等待|等我|返工|恢复|部署|安装|运行|删除|写入|撰写|生成|build|create|implement|fix|continue|cancel|resume|deploy|install|run|delete|write|generate", text, re.I))


def _goal_key(goal: str) -> str:
    return re.sub(r"[\W_]+", "", re.sub(r"一[份项个]", "", goal).casefold())


def explicit_waiting_change(turn: dict) -> list[dict]:
    """Recover an empty model proposal only from an explicit creation + wait request."""
    text = turn["messages"][0]["content"]
    clauses = re.split(r"[，。；,;]", text)
    goal = re.search(r"(?:创建|制作|实现|修改|create|implement).+?(?=[，。；,!]|$)", text, re.I)
    waiting = next((c for c in clauses if re.search(r"等我|待我|等待我|wait for my", c, re.I)), None)
    if not goal or not waiting or not is_task_request(text):
        return []
    change = {"goal": goal[0], "status": "waiting_user", "waiting_for": waiting.strip(),
              "next_step": "等待用户补充必需资料后继续", "blockers": [], "completion_evidence": [],
              "evidence": [{"turn_id": turn["id"], "message_id": turn["user_message_id"]}]}
    return [change]


def _resolve_task(state, change, turn):
    if change.get("task_id"):
        task = next((t for t in state.tasks if t["id"] == change["task_id"]), None)
        if task is None:
            raise MemoryError("任务ID不存在")
        return task
    matches = [t for t in state.tasks if _goal_key(t["goal"]) == _goal_key(change["goal"])]
    text = turn["messages"][0]["content"]
    new_request = bool(re.search(r"另一|另外|新建|新的一|another|new task", text, re.I))
    if matches and not new_request:
        if len(matches) != 1:
            raise MemoryError("已有多个同目标任务，必须明确task_id")
        return matches[0]
    # An update lacking identity must not silently become another deliverable.
    if not new_request and not re.search(r"创建|制作|实现|开发|build|create|implement", text, re.I):
        related = [t for t in state.tasks if t["id"] in turn["task_ids"]]
        if len(related) == 1 and any(word in text and word in change["goal"] and word in related[0]["goal"]
                                    for word in ("报告", "清单", "文件", "功能", "report", "checklist")):
            return related[0]
        if state.tasks:
            raise MemoryError("任务更新缺少明确task_id")
    return None


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
        if not is_task_request(turn["messages"][0]["content"]):
            raise MemoryError("事实确认、偏好和普通问答不创建或更新作业")
        task = _resolve_task(state, c, turn)
        if task and turn["sequence"] < task.get("last_sequence", 0):
            continue
        status = c["status"]
        if status not in TRANSITIONS or (task and status not in TRANSITIONS[task["status"]] and not (status == "completed" and task["status"] in {"waiting_user", "blocked"})):
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
            task = {"id": uid("task"), "goal": c["goal"], "scope_id": state.scope["id"], "origin_turn_id": turn["id"], "created_at": now(), "steps": []}
            state.tasks.append(task)
        task.update({k: v for k, v in c.items() if k not in {"task_id", "evidence", "steps", "goal"}})
        task.update(last_turn_id=turn["id"], last_sequence=turn["sequence"], updated_at=now())
        if status in {"completed", "cancelled"}:
            task.update(waiting_for=None, blockers=[], next_step=None)
        if "steps" in c:
            task["steps"] = checked_steps
        if task["id"] not in turn["task_ids"]:
            turn["task_ids"].append(task["id"])
        if status not in {"completed", "cancelled"}:
            state.active_task_id = task["id"]
        elif state.active_task_id == task["id"]:
            state.active_task_id = None
