import json
import io
from dataclasses import replace
import multiprocessing
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from src.application.agent import Agent
from src.application.subagent import SubagentRunner, validate_result
from src.common.config import Settings
from src.domain.delegation import RequestBudget, SubagentPolicy, SubtaskRequest
from src.tools.delegate_task import DelegateTaskTool
from src.tools.web_search import WebSearchTool
from src.tools.read_only import ProjectFilesTool
from src.service.ollama import OllamaClient


def answer(text):
    return {'message': {'role': 'assistant', 'content': text}, 'prompt_eval_count': 20, 'eval_count': 5}


def call(name, arguments):
    return {'message': {'role': 'assistant', 'content': '',
                        'tool_calls': [{'function': {'name': name, 'arguments': arguments}}]}}


def result(summary='文件包含样本数据', status='completed'):
    return json.dumps({'status': status, 'summary': summary,
        'findings': [{'claim': '文件包含样本数据', 'evidence': 'sample.txt:1'}],
        'artifacts': [], 'unresolved': []}, ensure_ascii=False)


class SequenceModel:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def chat(self, messages, tools):
        self.calls.append({'messages': messages, 'tools': tools})
        return self.responses.pop(0)


class SlowModel:
    def chat(self, messages, tools):
        time.sleep(10)
        return answer(result())


class SlowSearch(WebSearchTool):
    def run(self, query):
        time.sleep(10)
        return {'results': []}


class CrashModel:
    def chat(self, messages, tools):
        import os
        os._exit(7)


class ContextCheckModel:
    def chat(self, messages, tools):
        serialized = json.dumps(messages)
        if 'PRIVATE_OLD_CONVERSATION' in serialized:
            raise RuntimeError('历史泄漏')
        names = [t['function']['name'] for t in tools]
        if names != ['project_files']:
            raise RuntimeError('权限泄漏')
        return answer(result('独立上下文正常'))


class SubagentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'sample.txt').write_text('样本数据' * 1000 + '\n第二行\n', encoding='utf-8')
        self.policy = SubagentPolicy(timeout=5)

    def runner(self, model, policy=None, events=None):
        runner = SubagentRunner(model, self.root, policy=policy or self.policy,
                                on_event=events.append if events is not None else None)
        runner.begin_request(RequestBudget(runner.policy))
        return runner

    def run_task(self, runner):
        return runner.run(SubtaskRequest('分析样本', '只读sample.txt', '给出证据'))

    def test_independent_context_and_memory_and_events(self):
        events = []
        runner = self.runner(ContextCheckModel(), events=events)
        parent = SequenceModel([answer('PRIVATE_OLD_CONVERSATION'),
            call('delegate_task', {'goal': '分析', 'context': 'sample.txt', 'expected_output': '证据'}), answer('完成')])
        agent = Agent(parent, [DelegateTaskTool(runner)], '主规则')
        agent.ask('旧话题')
        agent.ask('新任务')
        self.assertEqual([e.phase for e in events], ['started', 'completed'])
        self.assertEqual(events[0].parent_id, events[1].parent_id)
        self.assertEqual(runner.budget.tasks, 1)
        self.assertNotIn('子 Agent，负责', json.dumps(agent.messages, ensure_ascii=False))
        self.assertFalse((self.root / 'memory').exists())
        record = next((self.root / 'data/subagents').glob('*.jsonl'))
        self.assertNotIn('PRIVATE_OLD_CONVERSATION', record.read_text())
        self.assertEqual(record.stat().st_mode & 0o777, 0o600)
        self.assertEqual(agent.last_run_metrics['model_calls_charged'], 3)
        self.assertEqual(len(agent.last_run_metrics['calls']), 3)

    def test_large_process_stays_outside_parent_context_and_comparison(self):
        request = {'goal': '分析样本', 'context': 'sample.txt', 'expected_output': '给出证据'}
        read = call('project_files', {'action': 'read', 'path': 'sample.txt'})
        child = SequenceModel([read, answer(result())])
        runner = self.runner(child)
        parent = SequenceModel([call('delegate_task', request), answer('文件包含样本数据 sample.txt:1')])
        delegated = Agent(parent, [DelegateTaskTool(runner)], '主规则')
        delegated.ask('分析样本')
        direct_model = SequenceModel([read, answer('文件包含样本数据 sample.txt:1')])
        direct = Agent(direct_model, [ProjectFilesTool(self.root)], '主规则')
        direct.ask('分析样本')
        indirect_size = len(json.dumps(parent.calls[-1]['messages'], ensure_ascii=False))
        direct_size = len(json.dumps(direct_model.calls[-1]['messages'], ensure_ascii=False))
        self.assertLess(indirect_size, direct_size)
        for agent in (delegated, direct):
            self.assertGreater(agent.last_run_metrics['elapsed_seconds'], 0)
            self.assertTrue(all(m['context_chars'] > 0 for m in agent.last_run_metrics['calls']))
        self.assertEqual(delegated.last_run_metrics['model_calls_charged'], 4)
        self.assertEqual(direct.last_run_metrics['model_calls_charged'], 2)
        self.assertIn('样本数据', next((self.root / 'data/subagents').glob('*.jsonl')).read_text())
        if os.getenv('SUBAGENT_BENCHMARK_OUTPUT'):
            Path(os.environ['SUBAGENT_BENCHMARK_OUTPUT']).write_text(json.dumps({
                'fixture': 'same 4000-character local text; deterministic models; identical final evidence',
                'direct_parent_final_message_chars': direct_size,
                'delegated_parent_final_message_chars': indirect_size,
                'direct': direct.last_run_metrics, 'delegated': delegated.last_run_metrics,
                'quality': 'both final answers include sample.txt:1; semantic model quality not measured'
            }, ensure_ascii=False, indent=2))

    def test_task_limit_reset_and_input_limit(self):
        runner = self.runner(SequenceModel([answer(result())]), SubagentPolicy(max_tasks=1, timeout=5))
        tool = DelegateTaskTool(runner)
        self.assertEqual(self.run_task(runner)['status'], 'completed')
        self.assertEqual(tool.run('再做', '', '证据')['status'], 'failed')
        runner.begin_request(RequestBudget(runner.policy))
        self.assertEqual(self.run_task(runner)['status'], 'completed')
        huge = tool.run('x' * 24001, '', '证据')
        self.assertEqual(huge['status'], 'failed')
        self.assertIn('input_limit', str(huge))

    def test_shared_budget_and_reserved_parent_answer(self):
        policy = SubagentPolicy(max_calls=3, max_rounds=5, timeout=5)
        runner = self.runner(SequenceModel([answer(result())]), policy)
        model = SequenceModel([call('delegate_task', {'goal': '任务', 'context': '', 'expected_output': '证据'}), answer('最终回答')])
        agent = Agent(model, [DelegateTaskTool(runner)], '规则')
        self.assertEqual(agent.ask('执行'), '最终回答')
        self.assertEqual(agent.last_run_metrics['model_calls_charged'], 3)
        self.assertEqual(model.calls[-1]['tools'], [])
        self.assertEqual(runner.budget.remaining, 0)

    def test_batch_delegations_are_serial_and_quota_is_enforced(self):
        events = []
        runner = self.runner(SequenceModel([answer(result())]), events=events)
        entry = call('delegate_task', {'goal': '任务', 'context': '', 'expected_output': '证据'})
        batch = entry['message']['tool_calls'] * 3
        parent = SequenceModel([{'message': {'role': 'assistant', 'content': '', 'tool_calls': batch}}, answer('完成')])
        agent = Agent(parent, [DelegateTaskTool(runner)], '规则')
        agent.ask('分析')
        self.assertEqual(agent.last_run_metrics['subtasks'], 2)
        self.assertEqual([e.phase for e in events], ['started', 'completed', 'started', 'completed'])
        values = [json.loads(m['content'])['result'] for m in parent.calls[-1]['messages'] if m['role'] == 'tool']
        self.assertEqual([v['status'] for v in values], ['completed', 'completed', 'failed'])

    def test_hard_timeout_covers_tool_execution(self):
        runner = SubagentRunner(SequenceModel([call('web_search', {'query': 'test'})]), self.root,
                                SlowSearch(None), SubagentPolicy(timeout=0.5))
        runner.begin_request(RequestBudget(runner.policy))
        value = self.run_task(runner)
        self.assertIn('timeout', str(value))
        self.assertFalse(multiprocessing.active_children())
        self.assertIn('web_search', Path(value['details_ref']).read_text())

    def test_timeout_stops_process_and_does_not_retry(self):
        runner = self.runner(SlowModel(), SubagentPolicy(timeout=0.2))
        before = {p.pid for p in multiprocessing.active_children()}
        started = time.monotonic()
        value = self.run_task(runner)
        self.assertLess(time.monotonic() - started, 3)
        self.assertIn('timeout', str(value))
        self.assertEqual(value['status'], 'partial')
        self.assertEqual(before, {p.pid for p in multiprocessing.active_children()})
        self.assertEqual(runner.budget.remaining, 13)  # unknown in-flight calls conservatively charged
        self.assertTrue(Path(value['details_ref']).exists())

    def test_invalid_result_crash_and_failure_do_not_end_parent(self):
        for child in (SequenceModel([answer('不是JSON')]), CrashModel(),
                      SequenceModel([call('delegate_task', {})]), SequenceModel([call('shell', {'command': 'touch bad'})])):
            with self.subTest(child=type(child).__name__):
                runner = self.runner(child)
                parent = SequenceModel([call('delegate_task', {'goal': '任务', 'context': '', 'expected_output': '证据'}), answer('部分结果，子任务失败')])
                agent = Agent(parent, [DelegateTaskTool(runner)], '规则')
                self.assertIn('失败', agent.ask('执行'))
                self.assertNotIn('"status": "completed"', json.dumps(parent.calls[-1], ensure_ascii=False))
        self.assertFalse((self.root / 'bad').exists())

    def test_round_limit_context_limit_and_large_result(self):
        loop = call('project_files', {'action': 'read', 'path': 'sample.txt'})
        runner = self.runner(SequenceModel([loop] * 6), SubagentPolicy(max_rounds=2, timeout=5))
        self.assertIn('model_step_limit', str(self.run_task(runner)))
        runner = self.runner(SequenceModel([loop, answer(result())]),
                             SubagentPolicy(input_chars=3000, context_chars=3500, timeout=5))
        self.assertIn('context_limit', str(self.run_task(runner)))
        runner = self.runner(SequenceModel([answer(result('很长' * 4000))]))
        value = self.run_task(runner)
        self.assertLessEqual(len(json.dumps(value, ensure_ascii=False)), runner.policy.result_chars)
        self.assertEqual(value['status'], 'partial')
        self.assertIn('result_limit', str(value))
        self.assertIn('很长' * 4000, Path(value['details_ref']).read_text())

    def test_schema_redaction_and_evidence(self):
        self.assertEqual(validate_result(result())['status'], 'completed')
        value = json.loads(result()); value['findings'] = []
        self.assertEqual(validate_result(json.dumps(value))['status'], 'partial')
        value = json.loads(result()); value['unresolved'] = ['证据待核验']
        self.assertEqual(validate_result(json.dumps(value))['status'], 'partial')
        value = validate_result(result('api_key=sk-123456789012345'))
        self.assertNotIn('sk-123456789012345', str(value))
        for raw in ('[]', '{}', '{"status":"completed"}'):
            with self.assertRaises(ValueError):
                validate_result(raw)

    def test_simple_task_never_forces_delegation(self):
        runner = self.runner(SequenceModel([]))
        agent = Agent(SequenceModel([answer('直接回答')]), [DelegateTaskTool(runner)], '规则')
        self.assertEqual(agent.ask('你好'), '直接回答')
        self.assertEqual(agent.last_run_metrics['subtasks'], 0)
        self.assertFalse((self.root / 'data').exists())

    def test_final_call_cannot_execute_more_tools(self):
        policy = SubagentPolicy(max_calls=2)
        runner = self.runner(SequenceModel([]), policy)
        model = SequenceModel([call('delegate_task', {'goal': '任务', 'context': '', 'expected_output': '证据'}),
                               call('delegate_task', {'goal': '任务', 'context': '', 'expected_output': '证据'})])
        agent = Agent(model, [DelegateTaskTool(runner)], '规则')
        self.assertIn('未完成', agent.ask('执行'))
        self.assertEqual(agent.last_run_metrics['subtasks'], 0)
        self.assertFalse((self.root / 'data').exists())

    def test_project_constraints_and_actual_spawned_http_gateway(self):
        (self.root / 'AGENTS.md').write_text('PROJECT_CONSTRAINT_MARKER：必须只读')
        payloads = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                payloads.append(data)
                if '只读子 Agent' in data['messages'][0]['content']:
                    if 'PROJECT_CONSTRAINT_MARKER' not in data['messages'][0]['content']:
                        response = answer('缺少项目规则')
                    elif data['messages'][-1]['role'] == 'tool':
                        response = answer(result())
                    else:
                        response = call('project_files', {'action': 'read', 'path': 'sample.txt'})
                elif data['messages'][-1]['role'] == 'tool':
                    response = answer('已核验 sample.txt:1')
                else:
                    response = call('delegate_task', {'goal': '分析样本', 'context': 'sample.txt', 'expected_output': '证据'})
                body = json.dumps(response).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = OllamaClient('http://127.0.0.1:' + str(server.server_port), 'test', timeout=3)
            runner = self.runner(client)
            agent = Agent(client, [DelegateTaskTool(runner)], '主规则')
            self.assertIn('已核验', agent.ask('分析样本'))
            self.assertEqual(agent.last_run_metrics['model_calls_charged'], 4)
            self.assertEqual(len(payloads), 4)
            child_payloads = [p for p in payloads if '只读子 Agent' in p['messages'][0]['content']]
            self.assertEqual([t['function']['name'] for t in child_payloads[0]['tools']], ['project_files'])
            self.assertNotIn('样本数据' * 100, json.dumps(payloads[-1], ensure_ascii=False))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_settings_and_policy_validation(self):
        with patch.dict('os.environ', {'SUBAGENT_ENABLED': 'false', 'SUBAGENT_MAX_TASKS': '1'}, clear=True):
            with patch('src.common.config._load_dotenv'):
                settings = Settings.from_env()
                self.assertFalse(settings.subagent_enabled)
                self.assertEqual(settings.subagent_policy.max_tasks, 1)
        for kwargs in ({'max_rounds': 0}, {'max_calls': 1}, {'timeout': float('nan')}, {'result_chars': 10}):
            with self.assertRaises(ValueError):
                SubagentPolicy(**kwargs)


class ReadOnlyTests(unittest.TestCase):
    def test_boundaries_search_limits_symlinks_and_redaction(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root = Path(directory)
            tool = ProjectFilesTool(root)
            (root / 'sample.txt').write_text('hello\napi_key=sk-123456789012345\n')
            (root / '.env').write_text('SECRET=hidden')
            (root / 'memory').mkdir(); (root / 'memory/memory.json').write_text('private')
            (root / 'escape').symlink_to(outside, target_is_directory=True)
            (root / 'alias').symlink_to(root / '.env')
            for path in ('../', str(Path(outside)), '.env', 'alias', 'memory/memory.json', 'escape/file'):
                self.assertIn('error', tool.run('read', path))
            self.assertNotIn('sk-123456789012345', str(tool.run('read', 'sample.txt')))
            self.assertEqual(tool.run('search', query='hello')['matches'][0]['line'], 1)
            self.assertEqual(tool.run('search', query='SECRET')['matches'], [])
            self.assertIn('error', tool.run('read', 'sample.txt', line_count=201))
            (root / 'binary').write_bytes(b'a\x00b')
            self.assertIn('error', tool.run('read', 'binary'))
            self.assertIn('error', tool.run('write', 'sample.txt'))
            (root / 'src/domain/memory').mkdir(parents=True)
            (root / 'src/domain/memory/state.py').write_text('class MemoryState: pass')
            self.assertNotIn('error', tool.run('read', 'src/domain/memory/state.py'))
            private = root / 'custom-memory'
            private.mkdir()
            (private / 'state.json').write_text('私人记忆')
            self.assertIn('error', ProjectFilesTool(root, [private]).run('read', 'custom-memory/state.json'))
            for index in range(25):
                (root / f'empty-match-{index}').touch()
            search = tool.run('search', query='empty-match')
            self.assertEqual(len(search['matches']), 20)
            self.assertTrue(search['truncated'])


class SubagentCliTests(unittest.TestCase):
    def test_cli_registers_delegation_only_when_enabled(self):
        from src.application.cli import main
        for enabled in (True, False):
            settings = replace(Settings('http://unused', 'model', None, 5, 'INFO', memory_enabled=False),
                               subagent_enabled=enabled)
            with patch('src.application.cli.Settings.from_env', return_value=settings), \
                 patch('src.application.cli.configure_logging'), \
                 patch('src.application.cli.Agent') as agent, \
                 patch('builtins.input', return_value='/exit'), \
                 patch('sys.stdout', new_callable=io.StringIO):
                main()
            names = [tool.name for tool in agent.call_args.kwargs['tools']]
            self.assertEqual('delegate_task' in names, enabled)


if __name__ == '__main__':
    unittest.main()
