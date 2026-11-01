"""Request-scoped delegation limits, independent of model and persistence vendors."""
from dataclasses import dataclass
from threading import Lock
import math
import time
from uuid import uuid4
from typing import Literal, TypedDict


class SubtaskFinding(TypedDict):
    claim: str
    evidence: str


class SubtaskResult(TypedDict):
    status: Literal['completed', 'partial', 'failed']
    summary: str
    findings: list[SubtaskFinding]
    artifacts: list[str]
    unresolved: list[str]


@dataclass(frozen=True)
class SubagentPolicy:
    max_tasks: int = 2
    max_rounds: int = 5
    max_calls: int = 18
    timeout: float = 120
    input_chars: int = 24000
    context_chars: int = 48000
    result_chars: int = 3000

    def __post_init__(self):
        for name in ('max_tasks', 'max_rounds', 'max_calls', 'input_chars', 'context_chars', 'result_chars'):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f'{name}必须是正整数')
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError('子任务超时必须是正数')
        if self.max_calls < 2 or self.result_chars < 1000 or self.context_chars < self.input_chars:
            raise ValueError('调用预算至少2次、返回预算至少1000字符、上下文预算不得小于输入预算')


@dataclass(frozen=True)
class SubtaskRequest:
    goal: str
    context: str
    expected_output: str

    def validate(self):
        if any(not isinstance(v, str) for v in (self.goal, self.context, self.expected_output)):
            raise ValueError('子任务参数必须是字符串')
        if not self.goal.strip() or not self.expected_output.strip():
            raise ValueError('目标和验收条件不能为空')


class RequestBudget:
    """Reserve child calls before launch; never spend the final parent call."""
    def __init__(self, policy: SubagentPolicy):
        self.policy = policy
        self.request_id = 'request_' + uuid4().hex
        self.remaining = policy.max_calls
        self.tasks = 0
        self.lock = Lock()
        self.started = time.monotonic()
        self.metrics = []

    def consume_parent(self):
        with self.lock:
            if self.remaining < 1:
                raise RuntimeError('已达到主子 Agent 共享模型调用预算')
            self.remaining -= 1

    def reserve_child(self):
        with self.lock:
            if self.tasks >= self.policy.max_tasks:
                raise ValueError('已达到本次请求子任务数量上限')
            allocation = min(self.policy.max_rounds, self.remaining - 1)
            if allocation < 1:
                raise ValueError('预算不足，已为主 Agent 保留最终回答调用')
            self.remaining -= allocation
            self.tasks += 1
            return allocation

    def refund(self, count):
        with self.lock:
            self.remaining += count

    def snapshot(self):
        return {'request_id': self.request_id, 'model_calls_charged': self.policy.max_calls - self.remaining,
                'subtasks': self.tasks, 'elapsed_seconds': round(time.monotonic() - self.started, 6),
                'calls': list(self.metrics)}
