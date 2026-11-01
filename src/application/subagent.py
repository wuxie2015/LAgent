"""Synchronous delegation with spawn isolation and a hard wall-clock deadline."""
from dataclasses import asdict, dataclass
import json
import multiprocessing
import os
from pathlib import Path
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from uuid import uuid4

from ..domain.delegation import SubagentPolicy, SubtaskRequest, SubtaskResult
from ..domain.memory.privacy import redact
from ..tools.read_only import ProjectFilesTool
from ..tools.web_search import WebSearchTool


def encoded(value):
    return json.dumps(value, ensure_ascii=False)


def journal(path, value):
    with Path(path).open('a', encoding='utf-8') as stream:
        stream.write(encoded(redact(value)) + '\n')
        stream.flush()


def failure(reason, status='failed') -> SubtaskResult:
    return {'status': status, 'summary': '子任务未完整完成，请检查限制与详细记录。',
            'findings': [], 'artifacts': [], 'unresolved': [reason]}


def validate_result(text) -> SubtaskResult:
    result = json.loads(text)
    if not isinstance(result, dict) or result.get('status') not in {'completed', 'partial', 'failed'}:
        raise ValueError('无效状态')
    if not isinstance(result.get('summary'), str) or not result['summary'].strip():
        raise ValueError('缺少结论')
    for key in ('artifacts', 'unresolved'):
        if not isinstance(result.get(key), list) or any(not isinstance(x, str) for x in result[key]):
            raise ValueError('无效列表')
    if not isinstance(result.get('findings'), list):
        raise ValueError('缺少证据列表')
    findings = []
    for item in result['findings']:
        if not isinstance(item, dict) or any(not isinstance(item.get(k), str) or not item[k].strip() for k in ('claim', 'evidence')):
            raise ValueError('发现缺少证据')
        findings.append({k: item[k] for k in ('claim', 'evidence')})
    clean = {k: result[k] for k in ('status', 'summary', 'artifacts', 'unresolved')}
    clean['findings'] = findings
    if clean['status'] == 'completed' and (not findings or clean['unresolved']):
        clean['status'] = 'partial'
        clean['unresolved'].append('完成状态尚缺充分证据，或仍有未解决事项')
    return redact(clean)


def _worker(client, tools, prompt, request, rounds, context_chars, record, output):
    """No main memory, callbacks, Shell or delegation tool crosses this boundary."""
    os.umask(0o077)
    messages = [{'role': 'system', 'content': prompt}, {'role': 'user', 'content': encoded(asdict(request))}]
    definitions = [t.definition for t in tools]
    registered = {t.name: t for t in tools}
    calls, metrics = 0, []
    result = failure('model_step_limit', 'partial')
    try:
        for number in range(1, rounds + 1):
            chars = len(encoded(messages)) + len(encoded(definitions))
            if chars > context_chars:
                result = failure('context_limit；完整已执行过程保留在details_ref中', 'partial')
                break
            journal(record, {'event': 'model_requested', 'round': number, 'messages': messages})
            calls += 1
            started = time.monotonic()
            response = client.chat(messages, definitions)
            metric = {'agent': 'child', 'round': number, 'context_chars': chars,
                      'elapsed_seconds': round(time.monotonic() - started, 6)}
            for key in ('prompt_eval_count', 'eval_count'):
                if type(response.get(key)) is int:
                    metric[key] = response[key]
            metrics.append(metric)
            journal(record, {'event': 'model_completed', 'response': response, 'metric': metric})
            message = response.get('message')
            if not isinstance(message, dict) or message.get('role') != 'assistant':
                raise ValueError('无效模型消息')
            messages.append(message)
            tool_calls = message.get('tool_calls') or []
            if not tool_calls:
                result = validate_result(message.get('content', ''))
                break
            if not isinstance(tool_calls, list) or len(tool_calls) > 8:
                raise ValueError('单轮工具调用上限8次')
            for call in tool_calls:
                function = call['function']
                name = function['name']
                if name not in registered:
                    raise ValueError('禁止的子任务工具：' + str(name))
                arguments = function.get('arguments', {})
                if isinstance(arguments, str):
                    arguments = json.loads(arguments)
                if not isinstance(arguments, dict):
                    raise ValueError('无效工具参数')
                try:
                    value = registered[name].run(**arguments)
                except Exception as exc:
                    value = {'error': type(exc).__name__}
                journal(record, {'event': 'tool_completed', 'tool': name, 'arguments': arguments, 'result': value})
                messages.append({'role': 'tool', 'content': encoded({'tool': name, 'result': value})})
    except Exception as exc:
        # Keep raw exception data out of the main conversation and record.
        result = failure('invalid_result_or_execution_error:' + type(exc).__name__, 'partial' if calls else 'failed')
    Path(output).write_text(encoded({'result': result, 'calls': calls, 'metrics': metrics}), encoding='utf-8')


@dataclass(frozen=True)
class SubagentEvent:
    phase: str
    parent_id: str
    task_id: str
    status: str


class SubagentRunner:
    def __init__(self, client, root, search=None, policy=None, on_event=None, blocked_paths=()):
        self.client = client
        self.root = Path(root).resolve()
        self.policy = policy or SubagentPolicy()
        # Explicit capability whitelist: callers cannot supply arbitrary executable tools.
        if search is not None and not isinstance(search, WebSearchTool):
            raise ValueError('子 Agent 只允许受限文件工具和网页搜索')
        self.tools = [ProjectFilesTool(self.root, blocked_paths)] + ([search] if search is not None else [])
        self.on_event = on_event
        self.budget = None
        self.records = self.root / 'data' / 'subagents'

    def begin_request(self, budget):
        self.budget = budget

    def _emit(self, phase, task_id, status):
        if self.on_event:
            self.on_event(SubagentEvent(phase, self.budget.request_id, task_id, status))

    def run(self, request):
        policy = self.policy
        request.validate()
        if self.budget is None:
            raise ValueError('委派必须绑定主请求预算')
        prompt_path = Path(__file__).resolve().parents[2] / 'resource/prompt/subagent.md'
        prompt = prompt_path.read_text(encoding='utf-8')
        prompt += '\n当前日期（Asia/Tokyo）：' + datetime.now(ZoneInfo('Asia/Tokyo')).date().isoformat()
        rules = self.root / 'AGENTS.md'
        if rules.is_file():
            prompt += '\n\n项目约束（不得扩大用户授权）：\n' + '\n'.join(ProjectFilesTool(self.root)._lines(ProjectFilesTool(self.root)._path('AGENTS.md')))
        if len(encoded(asdict(request))) + len(prompt) > policy.input_chars:
            return failure('input_limit；请缩小任务和必要背景')
        allocation = self.budget.reserve_child()
        task_id = 'sub_' + uuid4().hex
        self.records.resolve().relative_to(self.root)
        self.records.mkdir(parents=True, exist_ok=True, mode=0o700)
        record = self.records / (task_id + '.jsonl')
        output = self.records / (task_id + '.result.json')
        for path in (record, output):
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
        metadata = {'event': 'started', 'parent_id': self.budget.request_id, 'task_id': task_id,
                    'request': asdict(request), 'allocated_calls': allocation}
        journal(record, metadata)
        self._emit('started', task_id, 'running')
        process = multiprocessing.get_context('spawn').Process(target=_worker,
            args=(self.client, self.tools, prompt, request, allocation, policy.context_chars, str(record), str(output)))
        started = time.monotonic()
        result = failure('子任务进程未完成', 'partial')
        try:
            process.start()
            process.join(max(0, policy.timeout - (time.monotonic() - started)))
            if process.is_alive():
                result = failure('timeout；子进程已终止，已执行过程见details_ref', 'partial')
            elif process.exitcode == 0:
                payload = json.loads(output.read_text(encoding='utf-8'))
                used = payload['calls']
                if type(used) is not int or not 0 <= used <= allocation:
                    raise ValueError('无效调用计数')
                self.budget.refund(allocation - used)
                for metric in payload['metrics']:
                    self.budget.metrics.append({**metric, 'task_id': task_id})
                result = payload['result']
            else:
                result = failure('child_process_crashed；保守计入已预留调用预算', 'partial')
        except (OSError, ValueError, TypeError, RuntimeError) as exc:
            result = failure('child_process_error:' + type(exc).__name__)
        finally:
            if process.pid is not None:
                if process.is_alive():
                    process.terminate()
                    process.join(1)
                if process.is_alive():
                    process.kill()
                    process.join()
                process.close()
        result.update(details_ref=str(record), task_id=task_id, parent_id=self.budget.request_id)
        if len(encoded(result)) > policy.result_chars:
            # Never silently truncate an outcome into a successful result.
            result = {**failure('result_limit；完整结论和证据已保留在details_ref，需按段核验', 'partial'),
                      'summary': str(result.get('summary', ''))[:200],
                      'details_ref': str(record), 'task_id': task_id, 'parent_id': self.budget.request_id}
        journal(record, {'event': 'finished', 'result': result, 'elapsed_seconds': time.monotonic() - started})
        # This private file contains the original full result on normal completion.
        self._emit('completed', task_id, result['status'])
        return result
