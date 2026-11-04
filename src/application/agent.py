from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from ..common.logging_config import get_logger
from ..domain import AgentTool, ModelExecutionEvent, ModelGateway, ToolExecutionEvent
from ..domain.delegation import RequestBudget, SubagentPolicy
from ..domain.memory import MemoryError
from ..domain.memory.state import StorageError
from .memory_manager import MemoryManager
from .tool_execution import ToolExecutor


class AgentStateError(RuntimeError):
    pass


class CompletedTurnStorageError(StorageError):
    def __init__(self, answer: str):
        super().__init__("本轮回答已保存，但记忆维护写盘失败；已停止后续执行，请重启核查")
        self.answer = answer


class Agent:
    """Application use case: automatically execute and evaluate tool calls."""

    max_tool_rounds = 8

    def __init__(
        self,
        client: ModelGateway,
        tools: list[AgentTool],
        system_prompt: str,
        on_tool_event: Callable[[ToolExecutionEvent], None] | None = None,
        on_model_event: Callable[[ModelExecutionEvent], None] | None = None,
        memory: MemoryManager | None = None,
        execution_policy: SubagentPolicy | None = None,
    ) -> None:
        self.client = client
        self.tools = {tool.name: tool for tool in tools}
        self.system_prompt = system_prompt
        self.on_tool_event = on_tool_event
        self.on_model_event = on_model_event
        self.memory = memory if memory is not None else MemoryManager(system_prompt)
        delegated = self.tools.get("delegate_task")
        self.execution_policy = execution_policy or (delegated.runner.policy if delegated else SubagentPolicy())
        self.last_run_metrics = {}

    @property
    def messages(self) -> list[dict[str, Any]]:
        return self.memory.messages

    def reset(self) -> None:
        self.memory.reset()

    def ask(self, text: str) -> str:
        logger = get_logger()
        self.memory.begin_turn(text)
        budget = RequestBudget(self.execution_policy)
        delegate = self.tools.get("delegate_task")
        if delegate is not None:
            delegate.begin_request(budget)
        invalid_tool_retry = False
        forced_final = False
        executor = ToolExecutor(self.tools)
        try:
            for round_number in range(1, self.max_tool_rounds + 1):
                final_only = forced_final or budget.remaining == 1 or round_number == self.max_tool_rounds
                definitions = [] if final_only else [tool.definition for tool in self.tools.values()]
                notice = "已到本轮最终回答阶段：工具已关闭。请基于已获得的成功结果回答，给出来源，并明确无法确认的部分；不得再请求工具，也不要仅回复达到上限。搜索摘要、社区文章或issue提案不能证明官方当前实现。404只说明该URL读取失败，不能推出源码不存在或提示词未公开；未取得提示词源码时只说“未取得完整system prompt原文”，不要编造文件路径、CLI选项或运行时事件格式。" if final_only else ""
                messages = self.memory.build_context(definitions, extra_system=notice)
                budget.consume_parent()
                self._emit_model_event("requested", round_number, messages)
                started = time.monotonic()
                response = self.client.chat(messages, definitions)
                metric = {"agent": "parent", "round": round_number,
                          "context_chars": len(json.dumps(messages, ensure_ascii=False)) + len(json.dumps(definitions, ensure_ascii=False)),
                          "elapsed_seconds": round(time.monotonic() - started, 6)}
                for key in ("prompt_eval_count", "eval_count"):
                    if type(response.get(key)) is int:
                        metric[key] = response[key]
                budget.metrics.append(metric)
                self._emit_model_event("completed", round_number, messages, response)
                message = response.get("message")
                if not isinstance(message, dict) or message.get("role") != "assistant":
                    raise AgentStateError("模型响应缺少有效assistant消息")
                calls = message.get("tool_calls") or []
                if not isinstance(calls, list) or len(calls) > 8 or any(not isinstance(call, dict) or not isinstance(call.get("function"), dict) or not isinstance(call["function"].get("name"), str) for call in calls):
                    raise AgentStateError("模型返回无效工具调用，单轮最多8项")
                if final_only and (calls or not str(message.get("content") or "").strip()):
                    answer = self._partial_answer("相同工具调用重复失败，工具阶段已停止" if forced_final else "已到本轮调用上限")
                    self.memory.append_message({"role": "assistant", "content": answer})
                    return self._finish_answer(answer, "failed", "model_step_limit")
                unknown = any(call.get("function", {}).get("name") not in self.tools for call in calls)
                if unknown:
                    if invalid_tool_retry or final_only:
                        raise AgentStateError("模型重复请求未注册工具，已阻止执行")
                    invalid_tool_retry = True
                    self.memory.append_message({"role": "assistant", "content":
                        "[本次模型请求了不可用工具，调用已被阻止，未执行任何操作。请仅使用实际提供的工具；无工具时直接基于现有资料回答，不得虚构调用。]"})
                    continue
                message_id = self.memory.append_message(message)
                if not calls:
                    return self._finish_answer(message.get("content", "模型没有返回文本回复。"))
                tool_calls = [self._parse_tool_call(call) for call in calls]
                execution_ids = self.memory.record_tool_starts(tool_calls, message_id)
                for tool_name, arguments in tool_calls:
                    self._emit_tool_event("started", round_number, tool_name, arguments)
                # Delegate synchronously, without forking or spawning from an executor thread.
                # Mixed batches are serialized to avoid reading while main tools write.
                if any(name == "delegate_task" for name, _ in tool_calls):
                    results = [executor.execute(*item) for item in tool_calls]
                else:
                    with ThreadPoolExecutor(max_workers=len(tool_calls)) as pool:
                        results = list(pool.map(lambda item: executor.execute(*item), tool_calls))
                if any(result.get("error_code") == "repeat_limit" for result in results):
                    forced_final = True
                for execution_id, (tool_name, arguments), result in zip(execution_ids, tool_calls, results):
                    try:
                        self.memory.record_tool_result(execution_id, tool_name, arguments, result)
                    except (StorageError, OSError) as exc:
                        raise StorageError("命令可能已执行，结果未可靠写入记忆；停止后续执行，请重启核查，勿直接重试") from exc
                    self._emit_tool_event("completed", round_number, tool_name, arguments, result)
            answer = self._partial_answer()
            self.memory.append_message({"role": "assistant", "content": answer})
            return self._finish_answer(answer, "failed", "model_step_limit")
        except StorageError:
            # The disk may contain an execution checkpoint. Do not overwrite or replay it.
            raise
        except KeyboardInterrupt:
            self.memory.finish_turn("interrupted", "KeyboardInterrupt")
            raise
        except Exception as exc:
            self.memory.finish_turn("failed", type(exc).__name__)
            logger.warning("agent_turn_failed error_type=%s", type(exc).__name__)
            raise
        finally:
            self.last_run_metrics = budget.snapshot()
            logger.info("agent_request_metrics %s", json.dumps(self.last_run_metrics, ensure_ascii=False))

    def _parse_tool_call(self, call: dict[str, Any]) -> tuple[str, Any]:
        function = call.get("function", {})
        tool_name = function.get("name", "")
        if tool_name not in self.tools:
            raise AgentStateError(f"模型请求了未注册工具：{tool_name or '未知工具'}")
        return tool_name, function.get("arguments", {})

    def _finish_answer(self, answer, status="completed", failure=None):
        self.memory.finish_turn(status, failure)
        try:
            self.memory.maintain_safely()
        except StorageError as exc:
            raise CompletedTurnStorageError(answer) from exc
        return answer

    def _partial_answer(self, reason="已到本轮调用上限"):
        parts = [reason + "，未能完整回答。以下是已获得的资料（不代表已完成全部核验）："]
        evidence = {}
        for message in (self.memory.current or {}).get("messages", []):
            if message["role"] != "tool":
                continue
            payload = json.loads(message["content"])
            result = payload.get("result", {})
            if result.get("error"):
                continue
            if payload.get("tool") == "web_search":
                for item in result.get("results", [])[:2]:
                    url = item.get("url", "")
                    evidence.setdefault(url, f"- {item.get('title', '搜索结果')}：{item.get('snippet', '')[:300]}\n  来源：{url}")
            elif payload.get("tool") == "web_fetch":
                url = result.get("url", "")
                evidence[url] = f"- 已读取：{url}\n  正文片段：{result.get('content', '')[:600]}"
        parts.extend(list(evidence.values())[:6])
        if len(parts) == 1:
            parts.append("尚无可展示的网页证据；已执行操作及错误保存在本轮工具记录中，不会自动重放。")
        parts.append("未完成部分：仍需核实原始问题中尚未被上述来源支持的内容。最终阶段的额外工具请求已阻止。")
        return "\n\n".join(parts)

    def _emit_tool_event(
        self,
        phase: str,
        round_number: int,
        tool_name: str,
        arguments: Any,
        result: dict[str, Any] | None = None,
    ) -> None:
        if self.on_tool_event:
            self.on_tool_event(ToolExecutionEvent(phase, round_number, tool_name, arguments, result))

    def _emit_model_event(
        self,
        phase: str,
        round_number: int,
        messages: list[dict[str, Any]],
        response: dict[str, Any] | None = None,
    ) -> None:
        if self.on_model_event:
            self.on_model_event(ModelExecutionEvent(phase, round_number, messages.copy(), response))
