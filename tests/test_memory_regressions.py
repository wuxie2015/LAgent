"""Regressions from the 2026-11-03 real-model 50-turn audit."""
from copy import deepcopy
import json
import unittest

from src.application.memory_manager import MemoryManager
from src.application.agent import Agent, AgentStateError
from src.domain.memory import MemoryError, MemoryPolicy, MemoryState
from src.domain.memory.fact import merge_facts
from src.domain.memory.task import apply_tasks, is_task_request
from src.service.memory_extractor import ModelMemoryExtractor
from test_layered_memory import Extractor, finish, fact, task


class MemoryRegressionTests(unittest.TestCase):
    def test_unregistered_tool_retry_never_executes_or_replays_call(self):
        class Model:
            def __init__(self): self.calls = 0
            def chat(self, messages, tools):
                self.calls += 1
                if self.calls == 1:
                    return {"message": {"role": "assistant", "tool_calls": [{"function": {"name": "container.exec", "arguments": {}}}]}}
                assert not any(m.get("tool_calls") for m in messages)
                return {"message": {"role": "assistant", "content": "直接回答"}}
        model=Model();agent=Agent(model, [], 'system')
        self.assertEqual(agent.ask('项目负责人是谁？'), '直接回答')
        self.assertEqual(model.calls,2)
        self.assertEqual(agent.memory.state.recent_turns[-1]['tool_executions'],[])

    def test_repeated_unregistered_tool_request_stops_after_one_retry(self):
        class Model:
            def __init__(self): self.calls=0
            def chat(self,*args):
                self.calls += 1
                return {"message": {"role": "assistant", "tool_calls": [{"function": {"name": "container.exec", "arguments": {}}}]}}
        model=Model();agent=Agent(model, [], 'system')
        with self.assertRaises(AgentStateError): agent.ask('查询')
        self.assertEqual(model.calls,2)
        self.assertEqual(agent.memory.state.recent_turns[-1]['status'],'failed')

    def test_empty_waiting_proposal_creates_task_on_the_same_turn(self):
        memory=MemoryManager('system', extractor=Extractor())
        turn=finish(memory,'请创建一份季度回顾报告，先准备结构，具体业务数据等我提供')
        memory.maintain_safely()
        self.assertEqual(len(memory.state.tasks),1)
        self.assertEqual(memory.state.tasks[0]['status'],'waiting_user')
        self.assertEqual(memory.state.tasks[0]['goal'],'创建一份季度回顾报告')
        self.assertEqual(memory.state.tasks[0]['origin_turn_id'],turn['id'])

    def test_task_summary_includes_recent_completed_deliverable(self):
        memory=MemoryManager('system',policy=MemoryPolicy(recent_turns=1))
        first=finish(memory,'请创建季度报告','完整报告')
        completed=task(first,'completed');completed['goal']='季度回顾报告'
        completed['completion_evidence']=[{'turn_id':first['id'],'message_id':first['final_message_id']}]
        apply_tasks(memory.state,[completed],first)
        finish(memory,'闲聊')
        memory.begin_turn('现在的任务状态是什么？')
        self.assertIn(memory.state.tasks[0]['id'],json.dumps(memory.build_context([])))

    def test_evidence_format_normalization_still_rejects_forged_roles_and_ids(self):
        memory=MemoryManager('system');turn=finish(memory)
        batch=memory._batch(turn,'facts')
        candidate=fact(turn)
        class Model:
            def chat(self,*args):
                return {'message':{'content':json.dumps({'schema_version':1,'source_turn_ids':batch['source_turn_ids'],'facts':[candidate]})}}
        extractor=ModelMemoryExtractor(Model())
        candidate['evidence'][0]['role']='user'
        value=extractor.extract_facts(batch)[0]
        self.assertNotIn('role',value['evidence'][0])
        candidate['evidence']=[turn['user_message_id']]
        self.assertEqual(extractor.extract_facts(batch)[0]['evidence'][0]['turn_id'],turn['id'])
        candidate['evidence']=[{'turn_id':turn['id'],'message_id':turn['user_message_id'],'role':'tool'}]
        with self.assertRaises(MemoryError):extractor.extract_facts(batch)
        candidate['evidence']=['invented']
        with self.assertRaises(MemoryError):extractor.extract_facts(batch)

    def test_long_old_tool_preview_shrinks_before_dropping_turn(self):
        memory=MemoryManager('system',policy=MemoryPolicy(context_max_chars=2800, tool_context_max_chars=4000))
        memory.begin_turn('旧轮次需要保留')
        mid=memory.append_message({'role':'assistant','tool_calls':[{'function':{'name':'shell','arguments':{'command':'read'}}}]})
        eid=memory.record_tool_starts([('shell',{'command':'read'})],mid)[0]
        memory.record_tool_result(eid,'shell',{}, {'exit_code':0,'stdout':'x'*18000})
        memory.append_message({'role':'assistant','content':'旧结果'})
        memory.finish_turn('completed')
        memory.begin_turn('新问题')
        context=memory.build_context([])
        self.assertIn('旧轮次需要保留',json.dumps(context,ensure_ascii=False))
        self.assertIn('context_truncated',json.dumps(context,ensure_ascii=False))
        self.assertLessEqual(len(json.dumps(context,ensure_ascii=False))+514,2800)

    def test_waiting_for_data_is_task_state_not_long_term_fact(self):
        memory=MemoryManager('system')
        turn=finish(memory,'回到报告：数据还没给你，请继续保持等待')
        for predicate in ('wait_for_data','data_provided','task_status'):
            candidate=fact(turn,'false');candidate['predicate']=predicate
            with self.subTest(predicate=predicate), self.assertRaises(MemoryError):
                merge_facts(memory.state,[candidate],[turn])

    def test_context_projects_repeated_evidence_without_hiding_project_facts(self):
        memory=MemoryManager('system')
        turn=finish(memory,'已确定的项目代号银杉')
        for i in range(18):
            candidate=fact(turn,str(i));candidate['predicate']=f'preference_{i}'
            merge_facts(memory.state,[candidate],[turn])
            memory.state.facts[-1]['evidence'] *= 10
        candidate=fact(turn,'银杉',scope=memory.state.scope['id'])
        candidate.update(subject='project',predicate='code',kind='project_fact')
        merge_facts(memory.state,[candidate],[turn])
        memory.begin_turn('请给协作记忆摘要，列出项目代号和偏好')
        context=json.dumps(memory.build_context([]),ensure_ascii=False)
        self.assertIn('银杉',context)
        self.assertIn(memory.state.facts[-1]['id'],context)
        self.assertIn(turn['user_message_id'],context)
        self.assertLess(len(context)+514,memory.policy.context_max_chars)

    def test_fact_retry_receives_validation_feedback_without_extra_calls(self):
        class Repairing(Extractor):
            def extract_facts(self,batch):
                self.fact_calls.append(batch)
                if not batch.get('previous_validation_error'):
                    raise MemoryError('意图属性不能存为长期事实')
                return []
        extractor=Repairing();memory=MemoryManager('system',extractor=extractor,policy=MemoryPolicy(recent_turns=1))
        finish(memory);finish(memory)
        memory.maintain_safely()
        self.assertEqual(len(memory.state.recent_turns),2)
        memory.maintain_safely()
        self.assertEqual(len(memory.state.recent_turns),1)
        self.assertEqual(len(extractor.fact_calls),2)
        self.assertEqual(extractor.fact_calls[-1]['previous_validation_error'],'意图属性不能存为长期事实')

    def test_task_identity_preserves_parenthesized_environment(self):
        memory=MemoryManager('system')
        first=finish(memory,'请实现服务（生产环境）')
        change=task(first);change['goal']='实现服务（生产环境）'
        apply_tasks(memory.state,[change],first)
        second=finish(memory,'请实现服务（测试环境）')
        change=task(second);change['goal']='实现服务（测试环境）'
        apply_tasks(memory.state,[change],second)
        self.assertEqual(len(memory.state.tasks),2)

    def test_task_detection_covers_operations_without_treating_queries_as_work(self):
        for text in ('请部署服务','请安装依赖','请运行程序','请删除文件','请写入文件'):
            with self.subTest(text=text):self.assertTrue(is_task_request(text))
        for text in ('为什么发布前要核对版本号？','请列出未完成的任务','什么是部署？'):
            with self.subTest(text=text):self.assertFalse(is_task_request(text))

    def test_considering_migration_cannot_be_stored_as_fact(self):
        memory = MemoryManager('system')
        turn = finish(memory, '我只是考虑将数据库迁移到 PostgreSQL，尚未决定，也没有执行。')
        candidate = fact(turn, 'PostgreSQL')
        candidate.update(predicate='considering_database_migration_to', kind='user_fact')
        with self.assertRaises(MemoryError):
            merge_facts(memory.state, [candidate], [turn])

    def test_task_without_id_updates_existing_goal_without_duplicate(self):
        memory = MemoryManager('system')
        first = finish(memory, '请创建季度报告，内容等我提供')
        waiting = task(first, 'waiting_user'); waiting['waiting_for'] = '业务数据'
        apply_tasks(memory.state, [waiting], first)
        second = finish(memory, '数据已提供，请写完报告', '报告正文')
        complete = task(second, 'completed')
        complete['completion_evidence'] = [{'turn_id':second['id'], 'message_id':second['final_message_id']}]
        apply_tasks(memory.state, [complete], second)
        self.assertEqual(len(memory.state.tasks), 1)
        self.assertEqual(memory.state.tasks[0]['status'], 'completed')
        self.assertIsNone(memory.state.tasks[0]['waiting_for'])

    def test_fact_confirmation_does_not_create_task(self):
        memory = MemoryManager('system')
        turn = finish(memory, '明确更正：项目代号现在改为银杉。请确认更新。')
        with self.assertRaises(MemoryError):
            apply_tasks(memory.state, [task(turn)], turn)
        self.assertFalse(memory.state.tasks)

    def test_older_task_retry_does_not_overwrite_new_completion(self):
        memory = MemoryManager('system')
        first = finish(memory, '请实现记忆')
        apply_tasks(memory.state, [task(first)], first)
        tid = memory.state.tasks[0]['id']
        second = finish(memory, '请继续实现记忆', '实现交付')
        complete = task(second, 'completed', tid)
        complete['completion_evidence'] = [{'turn_id':second['id'], 'message_id':second['final_message_id']}]
        apply_tasks(memory.state, [complete], second)
        apply_tasks(memory.state, [task(first, task_id=tid)], first)
        self.assertEqual(memory.state.tasks[0]['status'], 'completed')

    def test_completed_report_is_available_when_asked_after_window(self):
        memory = MemoryManager('system', policy=MemoryPolicy(recent_turns=1))
        first = finish(memory, '请创建季度报告', '季度报告正文')
        completed = task(first, 'completed'); completed['goal'] = '创建季度报告'
        completed['completion_evidence'] = [{'turn_id':first['id'], 'message_id':first['final_message_id']}]
        apply_tasks(memory.state, [completed], first)
        finish(memory, '闲聊', '好的')
        memory.begin_turn('季度报告是否已经交付？')
        context = json.dumps(memory.build_context([]), ensure_ascii=False)
        self.assertIn('completed', context)
        self.assertIn(memory.state.tasks[0]['id'], context)

    def test_current_task_failure_does_not_block_older_fact_compaction(self):
        class Selective(Extractor):
            def propose_task_changes(self, batch):
                raise MemoryError('bad task response')
        extractor = Selective()
        memory = MemoryManager('system', extractor=extractor, policy=MemoryPolicy(recent_turns=1))
        first = finish(memory, '你好'); memory.maintain_safely()
        finish(memory, '请实现存储'); memory.maintain_safely()
        self.assertTrue(any(r['turn_id']==first['id'] for r in memory.state.archives))
        self.assertEqual(memory.state.recent_turns[-1]['task_update_status'], 'failed')

    def test_failed_old_fact_does_not_block_other_overflow_turn(self):
        class Selective(Extractor):
            def extract_facts(self, batch):
                self.fact_calls.append(batch)
                if batch['turn']['sequence']==1:
                    raise MemoryError('bad old response')
                return []
        memory = MemoryManager('system', extractor=Selective(), policy=MemoryPolicy(recent_turns=1))
        first=finish(memory); second=finish(memory); finish(memory)
        memory.maintain_safely()
        self.assertTrue(any(t['id']==first['id'] for t in memory.state.recent_turns))
        self.assertTrue(any(r['turn_id']==second['id'] for r in memory.state.archives))
        self.assertTrue(memory.state.maintenance['last_error_code'])

    def test_reference_facts_have_no_old_evidence_and_choose_relevant_attribute(self):
        memory=MemoryManager('system')
        first=finish(memory, '我默认用中文')
        merge_facts(memory.state, [fact(first)], [first])
        for i in range(8):
            f=deepcopy(memory.state.facts[0]);f.update(id=f'noise{i}', subject=f'noise{i}', predicate='noise', value='x'*700, updated_at='2099')
            memory.state.facts.append(f)
        correction=finish(memory, '更正：默认回复语言改为英文，不再使用中文')
        batch=memory._batch(correction,'facts')
        self.assertTrue(any(f['predicate']=='preferred_language' for f in batch['related_facts']))
        self.assertTrue(all('evidence' not in f for f in batch['related_facts']))

    def test_uncompacted_user_correction_is_available_beyond_window(self):
        memory=MemoryManager('system', policy=MemoryPolicy(recent_turns=1))
        first=finish(memory,'默认回复语言为中文')
        merge_facts(memory.state,[fact(first)],[first])
        finish(memory,'更正：默认回复语言改为英文')
        finish(memory,'闲聊')
        memory.begin_turn('我更正后的默认回复语言是什么？')
        context=json.dumps(memory.build_context([]),ensure_ascii=False)
        self.assertIn('更正：默认回复语言改为英文',context)
        self.assertIn('uncompacted_user_statements',context)

    def test_old_fact_cannot_revive_after_newer_correction_compacted_first(self):
        memory=MemoryManager('system')
        old=finish(memory,'默认回复语言中文')
        newer=finish(memory,'更正：改为英文')
        merge_facts(memory.state,[fact(newer,'英文','explicit_correction')],[newer])
        merge_facts(memory.state,[fact(old,'中文')],[old])
        self.assertEqual([f['value'] for f in memory.state.facts if f['status']=='active'],['英文'])

    def test_dynamic_extraction_contract_uses_only_actual_source_ids(self):
        memory=MemoryManager('system');turn=finish(memory)
        batch=memory._batch(turn,'facts')
        class Model:
            def chat(self,messages,tools):
                self.messages=messages
                return {'message':{'content':json.dumps({'schema_version':1,'source_turn_ids':batch['source_turn_ids'],'facts':[]})}}
        model=Model();ModelMemoryExtractor(model).extract_facts(batch)
        self.assertIn(turn['id'],model.messages[0]['content'])
        self.assertIn(turn['user_message_id'],model.messages[0]['content'])
        self.assertNotIn('输入轮次ID',model.messages[0]['content'])


if __name__=='__main__':
    unittest.main()
