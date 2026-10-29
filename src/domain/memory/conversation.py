from __future__ import annotations

from typing import Any, TypedDict

from .state import now, uid


class MessageRecord(TypedDict, total=False):
    id: str
    role: str
    created_at: str
    content: str
    tool_calls: list[dict[str, Any]]
    tool_call_id: str


class ToolExecution(TypedDict):
    execution_id: str
    message_id: str
    name: str
    arguments: dict
    status: str
    started_at: str
    ended_at: str | None
    result_message_id: str | None


class ConversationTurn(TypedDict):
    id: str
    sequence: int
    status: str
    started_at: str
    ended_at: str | None
    user_message_id: str
    task_ids: list[str]
    messages: list[MessageRecord]
    tool_executions: list[ToolExecution]
    final_message_id: str | None
    failure: dict | None
    task_update_status: str


def message_record(message: dict) -> MessageRecord:
    if not isinstance(message, dict) or message.get("role") not in {"user", "assistant", "tool"}:
        raise ValueError("消息角色无效")
    if not isinstance(message.get("content", ""), str):
        raise ValueError("消息内容必须是字符串")
    record = {k: message[k] for k in ("role", "content", "tool_calls", "tool_call_id") if k in message}
    record.update(id=uid("msg"), created_at=now())
    return record


def new_turn(text: str, sequence: int) -> ConversationTurn:
    message = message_record({"role": "user", "content": text})
    return {
        "id": uid("turn"), "sequence": sequence, "status": "running",
        "started_at": now(), "ended_at": None, "user_message_id": message["id"],
        "task_ids": [], "messages": [message], "tool_executions": [],
        "final_message_id": None, "failure": None, "task_update_status": "pending",
    }


def validate_turn(turn: dict) -> None:
    if not isinstance(turn.get("id"), str) or type(turn.get("sequence")) is not int:
        raise ValueError("轮次标识无效")
    if turn.get("status") not in {"running", "completed", "failed", "interrupted"}:
        raise ValueError("轮次状态无效")
    if turn.get("task_update_status") not in {"pending", "applied", "failed"}:
        raise ValueError("任务更新状态无效")
    messages = turn.get("messages")
    if not isinstance(messages, list) or not messages or messages[0].get("role") != "user":
        raise ValueError("轮次必须由用户消息开始")
    ids = set()
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in {"user", "assistant", "tool"}:
            raise ValueError("历史消息角色无效")
        if not isinstance(message.get("id"), str) or message["id"] in ids or not isinstance(message.get("content", ""), str):
            raise ValueError("消息ID或内容无效")
        ids.add(message["id"])
        calls = message.get("tool_calls", [])
        if not isinstance(calls, list) or any(not isinstance(c, dict) or not isinstance(c.get("function"), dict) for c in calls):
            raise ValueError("工具调用格式无效")
    if turn.get("user_message_id") != messages[0]["id"] or turn.get("final_message_id") not in ids | {None}:
        raise ValueError("轮次消息引用无效")
    if not isinstance(turn.get("tool_executions"), list):
        raise ValueError("工具执行列表无效")
    for execution in turn["tool_executions"]:
        if execution.get("status") not in {"pending", "running", "succeeded", "failed", "unknown"} or execution.get("message_id") not in ids:
            raise ValueError("工具执行状态或引用无效")
