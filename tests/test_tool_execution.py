import json
import unittest

from src.application.agent import Agent
from src.application.tool_execution import ToolExecutor
from src.tools.web_search import WebSearchTool


def call(arguments, name='web_search'):
    return {'message': {'role': 'assistant', 'tool_calls': [{'function': {'name': name, 'arguments': arguments}}]}}


class Search:
    name = 'web_search'
    definition = WebSearchTool.definition

    def __init__(self, failure=None):
        self.calls = []
        self.failure = failure

    def run(self, query):
        self.calls.append(query)
        return self.failure or {'results': [{'title': '官方文档', 'url': 'https://example.com/design', 'snippet': '已经获得的设计资料'}]}


class ToolExecutionTests(unittest.TestCase):
    def test_invalid_arguments_never_reach_tool(self):
        for arguments in ({'cursor': 0, 'id': 0}, {'id': 'https://example.com'}, {}, {'query': 3},
                          {'query': 'ok', 'url': 'https://example.com'}, '{bad json', [], 'null'):
            with self.subTest(arguments=arguments):
                tool = Search()
                result = ToolExecutor({tool.name: tool}).execute(tool.name, arguments)
                self.assertEqual(result['error_code'], 'invalid_arguments')
                self.assertIn('query', str(result['expected_parameters']))
                self.assertEqual(tool.calls, [])

    def test_valid_json_arguments_are_used_without_dropping_fields(self):
        tool = Search(); executor = ToolExecutor({tool.name: tool})
        self.assertIn('results', executor.execute(tool.name, '{"query":"OpenCode"}'))
        self.assertEqual(tool.calls, ['OpenCode'])

    def test_same_invalid_call_blocked_but_corrected_call_allowed(self):
        tool = Search(); executor = ToolExecutor({tool.name: tool})
        executor.execute(tool.name, {'id': 0})
        self.assertEqual(executor.execute(tool.name, {'id': 0})['error_code'], 'repeat_limit')
        self.assertIn('results', executor.execute(tool.name, {'query': 'OpenCode'}))
        self.assertEqual(tool.calls, ['OpenCode'])

    def test_temporary_network_failure_has_only_one_retry(self):
        tool = Search({'error': 'timeout', 'error_code': 'network_error', 'retryable': True})
        executor = ToolExecutor({tool.name: tool})
        for _ in range(2):
            self.assertEqual(executor.execute(tool.name, {'query': 'OpenCode'})['error_code'], 'network_error')
        self.assertEqual(executor.execute(tool.name, {'query': 'OpenCode'})['error_code'], 'repeat_limit')
        self.assertEqual(len(tool.calls), 2)

    def test_non_retryable_failure_does_not_run_again(self):
        tool = Search({'error': 'missing API key', 'error_code': 'not_configured', 'retryable': False})
        executor = ToolExecutor({tool.name: tool})
        executor.execute(tool.name, {'query': 'OpenCode'})
        self.assertEqual(executor.execute(tool.name, {'query': 'OpenCode'})['error_code'], 'repeat_limit')
        self.assertEqual(executor.execute(tool.name, {'query': 'another search'})['error_code'], 'repeat_limit')
        self.assertEqual(len(tool.calls), 1)

    def test_schema_checks_nested_ranges_and_enums_before_execution(self):
        from src.tools.schedule import ScheduleTool
        class Schedule:
            definition = ScheduleTool.definition
            def __init__(self): self.calls=0
            def run(self,**arguments): self.calls+=1;return {'ok':True}
        tool=Schedule(); executor=ToolExecutor({'schedule':tool})
        for arguments in ({'action':'unknown'}, {'action':'create','weekdays':[7]},
                          {'action':'create','weekdays':[True]}, {'action':'create','grace_seconds':-1}):
            with self.subTest(arguments=arguments):
                self.assertEqual(executor.execute('schedule',arguments)['error_code'],'invalid_arguments')
        self.assertEqual(tool.calls,0)

    def test_answer_text_is_not_truncated_or_rewritten(self):
        text='中文与 English 完整内容\n'*1000
        class Model:
            def chat(self,*args):return {'message':{'role':'assistant','content':text}}
        self.assertEqual(Agent(Model(),[],'system').ask('给出内容'),text)

    def test_child_loop_uses_same_validation_and_reserves_final_json(self):
        from src.application.subagent import _worker
        from src.domain.delegation import SubtaskRequest
        from pathlib import Path
        import tempfile
        class Model:
            def __init__(self):self.calls=0
            def chat(self,messages,tools):
                self.calls+=1
                if self.calls<3:return call({'cursor':0,'id':0})
                self.definitions=tools;self.context=messages
                return {'message':{'role':'assistant','content':json.dumps({'status':'partial','summary':'无有效证据','findings':[],'artifacts':[],'unresolved':['参数错误']})}}
        model=Model();tool=Search()
        with tempfile.TemporaryDirectory() as directory:
            record=Path(directory)/'record.jsonl'; output=Path(directory)/'result.json'
            _worker(model,[tool],'system',SubtaskRequest('调查','背景','证据'),5,48000,str(record),str(output))
            result=json.loads(output.read_text())
            self.assertEqual(result['result']['status'],'partial')
            self.assertIn('repeat_limit',record.read_text())
        self.assertEqual(tool.calls,[])
        self.assertEqual(model.calls,3)
        self.assertEqual(model.definitions,[])
        self.assertFalse(any(m.get('tool_calls') or m['role']=='tool' for m in model.context))

    def test_malformed_json_feedback_does_not_poison_next_gateway_request(self):
        class Model:
            def __init__(self):self.calls=0
            def chat(self,messages,tools):
                self.calls+=1
                if self.calls==1:return call('{bad json')
                if self.calls==2:
                    self.assertions=messages
                    return call({'query':'corrected'})
                return {'message':{'role':'assistant','content':'已纠正并获得结果'}}
        model=Model();tool=Search();agent=Agent(model,[tool],'system')
        self.assertIn('已纠正',agent.ask('查询'))
        self.assertEqual(tool.calls,['corrected'])
        self.assertFalse(any(m.get('tool_calls') for m in model.assertions))
        self.assertIn('invalid_arguments',json.dumps(model.assertions,ensure_ascii=False))
        self.assertEqual(agent.memory.state.recent_turns[0]['tool_executions'][0]['arguments'],'{bad json')

    def test_valid_string_json_history_is_normalized_without_losing_original(self):
        class Model:
            def __init__(self):self.calls=0
            def chat(self,messages,tools):
                self.calls+=1
                if self.calls==1:return call('{"query":"corrected"}')
                self.history=messages
                return {'message':{'role':'assistant','content':'完成'}}
        model=Model();tool=Search();agent=Agent(model,[tool],'system');agent.ask('查询')
        request=next(m for m in model.history if m.get('tool_calls'))
        self.assertEqual(request['tool_calls'][0]['function']['arguments'],{'query':'corrected'})
        self.assertEqual(agent.memory.state.recent_turns[0]['tool_executions'][0]['arguments'],'{"query":"corrected"}')

    def test_loop_reserves_eighth_call_for_answer(self):
        class Model:
            def __init__(self): self.definitions = []
            def chat(self, messages, tools):
                self.definitions.append(tools)
                if len(self.definitions) < 8:
                    return call({'query': 'OpenCode ' + str(len(self.definitions))})
                self.final_context = messages
                return {'message': {'role': 'assistant', 'content': '根据已有来源，设计要点如下；尚未核实完整提示词。'}}
        model = Model(); tool = Search(); agent = Agent(model, [tool], 'system')
        self.assertIn('已有来源', agent.ask('OpenCode设计与提示词'))
        self.assertEqual(len(tool.calls), 7)
        self.assertEqual(model.definitions[-1], [])
        self.assertEqual(sum(m['role'] == 'system' for m in model.final_context), 1)
        self.assertIn('应用执行控制消息', model.final_context[-1]['content'])
        self.assertIn('工具已关闭', model.final_context[-1]['content'])
        self.assertFalse(any(m.get('tool_calls') or m['role']=='tool' for m in model.final_context))
        self.assertIn('官方文档', json.dumps(model.final_context, ensure_ascii=False))

    def test_final_tool_request_is_not_executed_and_keeps_successful_evidence(self):
        class Model:
            def chat(self, messages, tools): return call({'query': 'OpenCode'})
        tool = Search(); agent = Agent(Model(), [tool], 'system'); agent.max_tool_rounds = 2
        answer = agent.ask('OpenCode设计与提示词')
        self.assertEqual(len(tool.calls), 1)
        self.assertIn('https://example.com/design', answer)
        self.assertIn('已经获得的设计资料', answer)
        self.assertIn('未完成', answer)
        self.assertEqual(agent.memory.state.recent_turns[-1]['status'], 'failed')

    def test_pasted_bad_cursor_loop_stops_early_and_can_be_diagnosed(self):
        class Model:
            def __init__(self): self.calls = 0
            def chat(self, messages, tools):
                self.calls += 1
                if self.calls == 1: return call({'query': 'OpenCode design'})
                if self.calls <= 3: return call({'cursor': 0, 'id': 0})
                self.tools = tools; self.messages = messages
                return {'message': {'role': 'assistant', 'content': '已取得官方摘要；错误是web_search不支持cursor/id，尚未读取完整prompt。'}}
        model = Model(); tool = Search(); agent = Agent(model, [tool], 'system')
        self.assertIn('不支持cursor/id', agent.ask('解释OpenCode设计'))
        self.assertEqual(len(tool.calls), 1)
        self.assertEqual(model.calls, 4)
        self.assertEqual(model.tools, [])
        content = json.dumps(model.messages, ensure_ascii=False)
        self.assertIn('invalid_arguments', content)
        self.assertIn('repeat_limit', content)
        self.assertIn('官方文档', content)
        # Retry accounting belongs to this request, not the Agent's whole lifetime.
        self.assertFalse(hasattr(agent, 'failures'))
