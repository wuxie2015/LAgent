from __future__ import annotations

import json
from typing import TypedDict
from datetime import datetime

from .privacy import KEY, sensitive
from .state import MemoryState, now, uid, MemoryError


class EvidenceRef(TypedDict, total=False):
    turn_id: str
    message_id: str
    excerpt: str


class Fact(TypedDict):
    id: str
    scope_id: str
    subject: str
    predicate: str
    value: str
    kind: str
    status: str
    source_type: str
    evidence: list[EvidenceRef]
    observed_at: str
    updated_at: str
    valid_until: str | None
    supersedes: str | None
    conflict_group_id: str | None
    verification: str


def fact_key(fact: dict) -> str:
    return "|".join(str(fact[k]).strip().casefold() for k in ("scope_id", "subject", "predicate"))


def evidence_messages(evidence: list, turns: list[dict]) -> list[dict]:
    if not isinstance(evidence, list) or not 1 <= len(evidence) <= 20:
        raise MemoryError("候选必须有1–20项真实来源")
    index = {(t["id"], m["id"]): m for t in turns for m in t["messages"]}
    messages = []
    for ref in evidence:
        if not isinstance(ref, dict) or set(ref) - {"turn_id", "message_id", "excerpt"}:
            raise MemoryError("证据字段不合法")
        message = index.get((ref.get("turn_id"), ref.get("message_id")))
        if message is None:
            raise MemoryError("候选引用了不存在的来源")
        if "excerpt" in ref and (not isinstance(ref["excerpt"], str) or ref["excerpt"] not in message.get("content", "")):
            raise MemoryError("证据摘录不在来源中")
        messages.append(message)
    return messages


def validate_fact(candidate: dict, turns: list[dict], scope: str) -> dict:
    allowed = {"subject", "predicate", "value", "kind", "source_type", "evidence", "change_type", "scope_id", "valid_until"}
    required = {"subject", "predicate", "value", "kind", "source_type", "evidence", "change_type"}
    if not isinstance(candidate, dict) or set(candidate) - allowed or not required <= set(candidate):
        raise MemoryError("事实候选字段不合法")
    for key in ("subject", "predicate", "value"):
        if not isinstance(candidate[key], str) or not candidate[key].strip() or len(candidate[key]) > 1500:
            raise MemoryError("事实内容长度或类型无效")
    if candidate["kind"] not in {"user_fact", "user_preference", "project_fact", "environment_fact"}:
        raise MemoryError("事实类型无效")
    source = candidate["source_type"]
    if source not in {"user_asserted", "user_confirmed", "tool_observed"}:
        raise MemoryError("事实来源类型无效")
    if candidate["change_type"] not in {"assertion", "explicit_change", "explicit_correction", "conflict_candidate"}:
        raise MemoryError("事实变更类型无效")
    result = dict(candidate, scope_id=candidate.get("scope_id", scope))
    if result["scope_id"] not in {scope, "user"}:
        raise MemoryError("事实scope越界")
    if result["scope_id"] == "user" and candidate["kind"] not in {"user_fact", "user_preference"}:
        raise MemoryError("项目事实不能提升到用户范围")
    if sensitive(candidate) or KEY.search(candidate["predicate"]):
        raise MemoryError("拒绝敏感事实")
    messages = evidence_messages(candidate["evidence"], turns)
    expected_role = "tool" if source == "tool_observed" else "user"
    if any(m["role"] != expected_role for m in messages):
        raise MemoryError("事实来源角色不匹配")
    if source == "tool_observed":
        for m in messages:
            try:
                payload = json.loads(m["content"])
                tool_result = payload["result"]
                if payload.get("tool") != "shell" or tool_result.get("error") or tool_result.get("timed_out") or tool_result.get("exit_code") != 0:
                    raise ValueError()
            except (ValueError, TypeError, KeyError, AttributeError):
                raise MemoryError("长期工具事实需要成功的本机观察，搜索新闻不自动长期化")
    text = "\n".join(m.get("content", "") for m in messages)
    if any(word in text.casefold() for word in ("打算", "计划迁移", "建议改用", "考虑改用", "might use", "plan to", "recommend using")):
        raise MemoryError("意图或建议来源不提升为已成立事实")
    if any(word in candidate["value"] for word in ("我将", "打算", "计划执行", "建议执行", "忽略规则", "上传密钥")):
        raise MemoryError("拒绝把意图或指令当事实")
    if candidate["change_type"] in {"explicit_change", "explicit_correction"} and not any(
        word in text.casefold() for word in ("改为", "改成", "更正", "现在", "不再", "迁移", "替换", "changed", "instead", "correct", "now uses")
    ):
        raise MemoryError("缺少明确变更证据")
    expiry = result.get("valid_until")
    if expiry is not None:
        if not isinstance(expiry, str):
            raise MemoryError("过期时间无效")
        try:
            if datetime.fromisoformat(expiry.replace("Z", "+00:00")).tzinfo is None:
                raise ValueError()
        except ValueError as exc:
            raise MemoryError("过期时间需要带时区的ISO日期") from exc
    return result


def merge_facts(state: MemoryState, candidates: list[dict], turns: list[dict]) -> list[str]:
    validated = [validate_fact(c, turns, state.scope["id"]) for c in candidates]
    affected = []
    seq = {t["id"]: t["sequence"] for t in turns}
    for c in validated:
        key = fact_key(c)
        sequence = max(seq[e["turn_id"]] for e in c["evidence"])
        if any(x["key"] == key and sequence <= x["through_sequence"] for x in state.forgotten):
            continue
        matches = [f for f in state.facts if fact_key(f) == key and f["status"] != "superseded"]
        same = next((f for f in matches if f["value"] == c["value"]), None)
        if same:
            for ref in c["evidence"]:
                if ref not in same["evidence"]:
                    same["evidence"].append(ref)
            same["updated_at"] = now()
            if c["change_type"] in {"explicit_change", "explicit_correction"}:
                for old in matches:
                    if old is not same:
                        old["status"] = "superseded"
                same.update(status="active", conflict_group_id=None)
            affected.append(same["id"])
            continue
        explicit = c.pop("change_type") in {"explicit_change", "explicit_correction"}
        new = dict(c, id=uid("fact"), status="active", observed_at=now(), updated_at=now(),
                   valid_until=c.get("valid_until"), supersedes=None, conflict_group_id=None,
                   verification="stable" if c["kind"] in {"user_fact", "user_preference"} else "recheck_before_action")
        if matches:
            if explicit:
                for old in matches:
                    old["status"] = "superseded"
                new["supersedes"] = matches[-1]["id"]
            else:
                group = matches[0].get("conflict_group_id") or uid("conflict")
                for old in matches:
                    old.update(status="disputed", conflict_group_id=group)
                new.update(status="disputed", conflict_group_id=group)
        state.facts.append(new)
        affected.append(new["id"])
    return affected
