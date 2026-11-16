"""Source contracts, recoverable validation failures and private diagnostics."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from src.application.memory_manager import MemoryManager
from src.domain.memory import MemoryError, MemoryPolicy
from src.domain.memory.state import MemoryValidationError
from src.domain.memory.task import apply_tasks, is_task_request
from src.service.memory_extractor import ModelMemoryExtractor
from src.service.memory_store import JsonMemoryStore
from test_layered_memory import Extractor, fact, finish, task


class MemoryMaintenanceDiagnosticsTests(unittest.TestCase):
    def test_start_debate_is_a_task_but_startup_questions_are_not(self):
        request = '请启动两个subagent进行一场辩论赛，流程按照正规辩论赛，辩题是征税是否构成多数人暴政'
        self.assertTrue(is_task_request(request))
        self.assertTrue(is_task_request('请启动服务并解释启动结果'))
        self.assertFalse(is_task_request('什么是启动流程？'))
        self.assertFalse(is_task_request('为什么启动服务失败？'))
        memory = MemoryManager('system')
        old = finish(memory, '请制作一份报告')
        apply_tasks(memory.state, [task(old)], old)
        turn = finish(memory, request, '辩论正文')
        change = task(turn, 'completed')
        change['goal'] = '启动两个subagent进行辩论'
        change['completion_evidence'] = [{'turn_id': turn['id'], 'message_id': turn['final_message_id']}]
        apply_tasks(memory.state, [change], turn)
        self.assertEqual(len(memory.state.tasks), 2)
        self.assertEqual(memory.state.tasks[-1]['status'], 'completed')
        self.assertEqual(memory.state.tasks[0]['status'], 'in_progress')

    def test_task_failure_is_explained_and_repaired_after_restart(self):
        class Repairing(Extractor):
            def propose_task_changes(self, batch):
                self.task_calls.append(deepcopy(batch))
                turn = batch['turn']
                change = task(turn, 'completed')
                completion = [{'turn_id': turn['id'], 'message_id': turn['final_message_id']}]
                change['completion_evidence'] = completion
                if not batch.get('previous_validation_error'):
                    change['evidence'] = completion
                return [change]

        extractor = Repairing()
        with tempfile.TemporaryDirectory() as directory:
            with JsonMemoryStore(Path(directory)) as store:
                memory = MemoryManager('system', store, extractor)
                turn = finish(memory, '请制作一份辩论稿', '辩论稿正文')
                original = deepcopy(turn['messages'])
                with self.assertLogs('lin_agent', level='WARNING') as logs:
                    memory.maintain_safely()
                self.assertEqual(memory.state.recent_turns[0]['messages'], original)
                self.assertFalse(memory.state.tasks)
                self.assertIn('任务必须有用户目标来源', memory.notices[0])
                self.assertIn('第1轮，任务状态', memory.notices[0])
                self.assertIn('stage=tasks', logs.output[0])
                self.assertIn('reason=任务必须有用户目标来源', logs.output[0])
            with JsonMemoryStore(Path(directory)) as store:
                memory = MemoryManager('system', store, extractor)
                memory.maintain_safely()
                self.assertEqual(extractor.task_calls[-1]['previous_validation_error'], '任务必须有用户目标来源')
                self.assertEqual(memory.state.tasks[0]['status'], 'completed')
                self.assertNotIn('task_validation_feedback', memory.state.recent_turns[0])
                self.assertIsNone(memory.state.maintenance['last_error_code'])
                self.assertIsNone(memory.state.maintenance['last_error'])

    def test_rejected_fact_preserves_history_logs_fragment_and_receives_feedback(self):
        class Repairing(Extractor):
            def extract_facts(self, batch):
                self.fact_calls.append(deepcopy(batch))
                if batch.get('previous_validation_error'):
                    return []
                candidate = fact(batch['turn'])
                candidate.update(source_type='tool_observed')
                candidate['evidence'] = [{'turn_id': batch['turn']['id'], 'message_id': result_id}]
                return [candidate]

        extractor = Repairing()
        memory = MemoryManager('system', extractor=extractor, policy=MemoryPolicy(recent_turns=1))
        memory.begin_turn('请调研一个公开话题')
        mid = memory.append_message({'role': 'assistant', 'content': '查询', 'tool_calls': [
            {'function': {'name': 'web_search', 'arguments': {'query': '话题'}}}]})
        eid = memory.record_tool_starts([('web_search', {'query': '话题'})], mid)[0]
        memory.record_tool_result(eid, 'web_search', {}, {'exit_code': 0, 'results': ['临时新闻']})
        result_id = memory.state.recent_turns[-1]['messages'][-1]['id']
        memory.append_message({'role': 'assistant', 'content': '查询结果'})
        memory.finish_turn('completed')
        first = deepcopy(memory.state.recent_turns[-1])
        finish(memory)
        with self.assertLogs('lin_agent', level='WARNING') as logs:
            memory.maintain_safely()
        self.assertEqual(len(memory.state.recent_turns), 2)
        self.assertFalse(memory.state.facts)
        self.assertFalse(memory.state.archives)
        self.assertIn('stage=facts', logs.output[-1])
        self.assertIn('fragment_index=0', logs.output[-1])
        self.assertEqual(memory.state.maintenance['last_error']['sequence'], 1)
        self.assertIn('搜索新闻不自动长期化', memory.notices[-1])
        memory.maintain_safely()
        self.assertEqual(len(extractor.fact_calls), 2)
        self.assertIn('搜索新闻不自动长期化', extractor.fact_calls[-1]['previous_validation_error'])
        self.assertEqual(memory.state.archives[0]['turn_id'], first['id'])
        self.assertFalse(memory.state.facts)
        self.assertIsNone(memory.state.maintenance['last_error'])

    def test_unexpected_exception_text_never_enters_logs_feedback_or_notices(self):
        class Broken(Extractor):
            def propose_task_changes(self, batch):
                raise MemoryError('PRIVATE user text token=do-not-log')
        memory = MemoryManager('system', extractor=Broken())
        finish(memory, '请实现服务')
        with self.assertLogs('lin_agent', level='WARNING') as logs:
            memory.maintain_safely()
        diagnostics = str(logs.output) + str(memory.notices) + str(memory.state.maintenance)
        diagnostics += memory._batch(memory.state.recent_turns[0], 'tasks')['previous_validation_error']
        self.assertNotIn('PRIVATE', diagnostics)
        self.assertNotIn('do-not-log', diagnostics)

    def test_oversized_tool_call_has_a_specific_notice_and_keeps_original(self):
        memory = MemoryManager('system', extractor=Extractor(),
                               policy=MemoryPolicy(recent_turns=1, extraction_max_chars=2000))
        memory.begin_turn('你好')
        memory.append_message({'role': 'assistant', 'tool_calls': [
            {'function': {'name': 'shell', 'arguments': {'command': 'PRIVATE' * 1000}}}]})
        memory.append_message({'role': 'assistant', 'content': '保留记录'})
        memory.finish_turn('completed')
        finish(memory)
        with self.assertLogs('lin_agent', level='WARNING') as logs:
            memory.maintain_safely()
        self.assertEqual(len(memory.state.recent_turns), 2)
        self.assertFalse(memory.state.archives)
        self.assertIn('单个工具调用参数超出提取预算', memory.notices[0])
        self.assertNotIn('PRIVATE', str(logs.output) + str(memory.notices))

    def test_extractor_contract_separates_fact_task_and_completion_sources(self):
        memory = MemoryManager('system')
        memory.begin_turn('请调研测试话题')
        result_ids = {}
        for name, result in [('shell', {'exit_code': 0}), ('shell', {'exit_code': 1}),
                             ('web_search', {'exit_code': 0}), ('delegate_task', {'exit_code': 0})]:
            mid = memory.append_message({'role': 'assistant', 'tool_calls': [
                {'function': {'name': name, 'arguments': {}}}]})
            eid = memory.record_tool_starts([(name, {})], mid)[0]
            memory.record_tool_result(eid, name, {}, result)
            result_ids[(name, result['exit_code'])] = memory.state.recent_turns[-1]['messages'][-1]['id']
        memory.append_message({'role': 'assistant', 'content': '交付'})
        memory.finish_turn('completed')
        turn = memory.state.recent_turns[-1]

        class Model:
            def chat(self, messages, tools):
                self.contract = json.loads(messages[0]['content'].split('\n')[-1])
                self.tools = tools
                return {'message': {'content': json.dumps({
                    'schema_version': 1, 'source_turn_ids': [turn['id']], self.field: []})}}
        model = Model()
        extractor = ModelMemoryExtractor(model)
        model.field = 'facts'
        extractor.extract_facts(memory._batch(turn, 'facts'))
        refs = model.contract['source_type_allowed_evidence']
        self.assertEqual(refs['user_asserted'], [{'turn_id': turn['id'], 'message_id': turn['user_message_id']}])
        self.assertEqual(refs['tool_observed'], [{'turn_id': turn['id'], 'message_id': result_ids[('shell', 0)]}])
        self.assertNotIn(turn['final_message_id'], str(model.contract['allowed_evidence']))
        model.field = 'changes'
        extractor.propose_task_changes(memory._batch(turn, 'tasks'))
        self.assertEqual(model.contract['required_user_evidence'], refs['user_asserted'])
        self.assertEqual(model.contract['available_task_ids'], [])
        self.assertTrue(model.contract['completed_requires_tool_evidence'])
        self.assertIn(result_ids[('shell', 0)], str(model.contract['successful_tool_evidence']))
        completion = str(model.contract['allowed_completion_evidence'])
        self.assertIn(turn['final_message_id'], completion)
        self.assertIn(result_ids[('shell', 0)], completion)
        self.assertNotIn(result_ids[('shell', 1)], completion)
        self.assertNotIn(turn['user_message_id'], completion)
        self.assertEqual(model.tools, [])

    def test_model_response_errors_have_specific_private_diagnostics(self):
        class Model:
            def chat(self, *args):
                return {'message': {'content': self.content}}
        model = Model()
        extractor = ModelMemoryExtractor(model)
        for content, expected in [('PRIVATE token=do-not-log', '不是有效JSON'),
                                  (json.dumps({'private': 'do-not-log'}), '提取schema无效'),
                                  (json.dumps({'schema_version': 1, 'source_turn_ids': ['wrong'], 'facts': []}), '提取轮次来源不匹配')]:
            with self.subTest(expected=expected):
                model.content = content
                with self.assertLogs('lin_agent', level='WARNING') as logs:
                    with self.assertRaisesRegex(MemoryValidationError, expected):
                        extractor.extract_facts({'source_turn_ids': ['turn_test']})
                self.assertIn('kind=facts', str(logs.output))
                self.assertIn(expected, str(logs.output))
                self.assertNotIn('do-not-log', str(logs.output))
                if expected == '不是有效JSON':
                    self.assertIn('json_line=1 json_column=1', str(logs.output))


if __name__ == '__main__':
    unittest.main()
