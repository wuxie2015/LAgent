from copy import deepcopy
from dataclasses import replace
import io
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.application.agent import Agent
from src.application.cli import memory_command, _show_model_event
from src.application.context_builder import ContextBuilder
from src.application.memory_manager import MemoryManager
from src.domain.memory import MemoryError, MemoryPolicy, MemoryState, ContextCapacityError
from src.domain.memory.state import StorageError, digest
from src.domain.memory.fact import merge_facts
from src.domain.memory.task import apply_tasks
from src.domain.memory.privacy import redact
from src.service.memory_store import JsonMemoryStore
from src.service.memory_extractor import ModelMemoryExtractor
from src.domain import ModelExecutionEvent


class Extractor:
    def __init__(self):
        self.fact_calls = []
        self.task_calls = []
        self.facts = []
        self.changes = []
        self.fail = False

    def extract_facts(self, batch):
        self.fact_calls.append(batch)
        if self.fail:
            raise MemoryError("invalid extraction")
        return deepcopy(self.facts)

    def propose_task_changes(self, batch):
        self.task_calls.append(batch)
        if self.fail:
            raise MemoryError("invalid extraction")
        return deepcopy(self.changes)


def finish(memory, text="你好", answer="收到"):
    memory.begin_turn(text)
    memory.append_message({"role": "assistant", "content": answer})
    memory.finish_turn("completed")
    return memory.state.recent_turns[-1]


def fact(turn, value="中文", change="assertion", scope="user"):
    return {"subject": "user", "predicate": "preferred_language", "value": value,
            "kind": "user_preference", "source_type": "user_asserted", "scope_id": scope,
            "change_type": change, "evidence": [{"turn_id": turn["id"], "message_id": turn["user_message_id"]}]}


def task(turn, status="in_progress", task_id=None):
    value = {"goal": "实现记忆", "status": status, "evidence": [{"turn_id": turn["id"], "message_id": turn["user_message_id"]}],
             "next_step": "实现存储", "blockers": [], "completion_evidence": []}
    if task_id:
        value["task_id"] = task_id
    return value


def try_lock(directory, queue):
    try:
        with JsonMemoryStore(Path(directory)):
            queue.put("locked")
    except MemoryError:
        queue.put("rejected")


class LayeredMemoryTests(unittest.TestCase):
    def test_ten_turns_then_eleventh_running_and_compacted(self):
        extractor = Extractor()
        with tempfile.TemporaryDirectory() as directory, JsonMemoryStore(Path(directory)) as store:
            memory = MemoryManager("system", store, extractor)
            for _ in range(10):
                finish(memory)
                memory.maintain_safely()
            first = memory.state.recent_turns[0]
            self.assertEqual(len(extractor.fact_calls), 0)
            memory.begin_turn("第十一轮")
            self.assertEqual(len(memory.state.recent_turns), 11)
            with self.assertRaises(MemoryError):
                memory.maintain()
            memory.append_message({"role": "assistant", "content": "回答"})
            memory.finish_turn("completed")
            extractor.facts = [fact(first)]
            memory.maintain_safely()
            self.assertEqual(len(memory.state.recent_turns), 10)
            self.assertEqual(memory.state.facts[0]["value"], "中文")
            self.assertEqual(store.load_archived_turn(memory.state.archives[0])["id"], first["id"])
            revisions = memory.state.revision
            memory.maintain_safely()
            self.assertEqual(memory.state.revision, revisions)

    def test_no_facts_still_archives_with_receipt(self):
        memory = MemoryManager("system", extractor=Extractor(), policy=MemoryPolicy(recent_turns=1))
        finish(memory); finish(memory)
        memory.maintain_safely()
        self.assertEqual(len(memory.state.recent_turns), 1)
        self.assertEqual(len(memory.state.compaction["receipts"]), 1)
        self.assertEqual(memory.state.facts, [])

    def test_failed_extraction_preserves_history_and_retry(self):
        extractor = Extractor(); extractor.fail = True
        memory = MemoryManager("system", extractor=extractor, policy=MemoryPolicy(recent_turns=1))
        first = finish(memory); finish(memory)
        memory.maintain_safely()
        self.assertEqual(len(memory.state.recent_turns), 2)
        self.assertFalse(memory.state.compaction["receipts"])
        extractor.fail = False
        extractor.facts = [fact(first)]
        memory.maintain_safely()
        self.assertEqual(len(memory.state.recent_turns), 1)
        self.assertEqual(len(memory.state.facts), 1)

    def test_fact_dedup_conflict_correction_and_scope(self):
        memory = MemoryManager("system")
        first = finish(memory, "我喜欢中文")
        merge_facts(memory.state, [fact(first), fact(first)], [first])
        self.assertEqual(len(memory.state.facts), 1)
        second = finish(memory, "喜欢英文")
        merge_facts(memory.state, [fact(second, "英文")], [second])
        self.assertTrue(all(f["status"] == "disputed" for f in memory.state.facts))
        third = finish(memory, "更正，改为日文")
        merge_facts(memory.state, [fact(third, "日文", "explicit_correction")], [third])
        self.assertEqual([f["value"] for f in memory.state.facts if f["status"] == "active"], ["日文"])
        merge_facts(memory.state, [fact(third, "中文", scope=memory.state.scope["id"])], [third])
        self.assertEqual(sum(f["status"] == "active" for f in memory.state.facts), 2)

    def test_fake_sources_secret_role_and_unsupported_change_rejected(self):
        memory = MemoryManager("system"); turn = finish(memory)
        candidates = []
        bad = fact(turn); bad["evidence"][0]["message_id"] = "invented"; candidates.append(bad)
        bad = fact(turn); bad["evidence"][0]["message_id"] = turn["final_message_id"]; candidates.append(bad)
        bad = fact(turn); bad["predicate"] = "api_key"; candidates.append(bad)
        bad = fact(turn, "打算迁移服务"); candidates.append(bad)
        candidates.append(fact(turn, "英文", "explicit_change"))
        for candidate in candidates:
            with self.subTest(candidate=candidate), self.assertRaises(MemoryError):
                merge_facts(deepcopy(memory.state), [candidate], [turn])

    def test_task_transitions_evidence_and_interleaved_question(self):
        memory = MemoryManager("system", extractor=Extractor())
        turn = finish(memory, "实现记忆")
        change = task(turn)
        apply_tasks(memory.state, [change], turn)
        tid = memory.state.tasks[0]["id"]
        finish(memory, "什么是Python")
        memory.maintain_safely()
        self.assertEqual(memory.state.tasks[0]["status"], "in_progress")
        memory.cancel_task(tid)
        unrelated = memory.state.recent_turns[-1]
        with self.assertRaises(MemoryError):
            apply_tasks(deepcopy(memory.state), [task(unrelated, task_id=tid)], unrelated)
        turn = finish(memory, "继续实现记忆")
        apply_tasks(memory.state, [task(turn, task_id=tid)], turn)
        with self.assertRaises(MemoryError):
            apply_tasks(deepcopy(memory.state), [task(turn, "completed", tid)], turn)
        completion = task(turn, "completed", tid)
        completion["completion_evidence"] = [{"turn_id": turn["id"], "message_id": turn["final_message_id"]}]
        apply_tasks(memory.state, [completion], turn)
        self.assertEqual(memory.state.tasks[0]["status"], "completed")

    def test_reset_preserves_facts_tasks_but_blocks_continuation(self):
        memory = MemoryManager("system")
        turn = finish(memory, "实现记忆")
        merge_facts(memory.state, [fact(turn)], [turn]); apply_tasks(memory.state, [task(turn)], turn)
        memory.reset()
        self.assertEqual(memory.state.recent_turns, [])
        self.assertEqual(len(memory.state.facts), 1)
        self.assertEqual(memory.state.tasks[0]["status"], "blocked")
        self.assertIsNone(memory.state.active_task_id)

    def test_forget_prevents_old_reextraction_allows_new(self):
        memory = MemoryManager("system")
        first = finish(memory)
        merge_facts(memory.state, [fact(first)], [first])
        memory.forget_fact(memory.state.facts[0]["id"])
        merge_facts(memory.state, [fact(first)], [first])
        self.assertFalse(memory.state.facts)
        second = finish(memory)
        merge_facts(memory.state, [fact(second)], [second])
        self.assertEqual(len(memory.state.facts), 1)

    def test_context_truncation_preserves_tool_pairs_and_current_input(self):
        memory = MemoryManager("system", policy=MemoryPolicy(context_max_chars=4000, tool_context_max_chars=300))
        finish(memory, "旧问题", "x" * 10000)
        memory.begin_turn("当前问题不能截断")
        mid = memory.append_message({"role": "assistant", "tool_calls": [{"function": {"name": "shell", "arguments": {"command": "pwd"}}}]})
        ids = memory.record_tool_starts([("shell", {"command": "pwd"})], mid)
        memory.record_tool_result(ids[0], "shell", {}, {"exit_code": 0, "stdout": "x" * 20000})
        context = memory.build_context([])
        self.assertLess(len(json.dumps(context, ensure_ascii=False)), 4000)
        self.assertTrue(any(m.get("content") == "当前问题不能截断" for m in context))
        self.assertEqual(context[-1]["role"], "tool")
        self.assertIn("exit_code", context[-1]["content"])
        self.assertNotIn("旧问题", json.dumps(context, ensure_ascii=False))

    def test_capacity_error_never_silently_truncates_user(self):
        memory = MemoryManager("system", policy=MemoryPolicy(context_max_chars=1000))
        memory.begin_turn("用户" * 10000)
        with self.assertRaises(ContextCapacityError):
            memory.build_context([])

    def test_interrupted_unmatched_tool_not_sent_for_reexecution(self):
        memory = MemoryManager("system")
        memory.begin_turn("执行")
        memory.append_message({"role": "assistant", "tool_calls": [{"function": {"name": "shell", "arguments": {"command": "pwd"}}}]})
        memory.finish_turn("interrupted")
        memory.begin_turn("继续")
        context = memory.build_context([])
        self.assertFalse(any(m.get("tool_calls") for m in context))
        self.assertIn("不重放", json.dumps(context, ensure_ascii=False))

    def test_recovery_marks_execution_unknown_without_running_it(self):
        with tempfile.TemporaryDirectory() as directory:
            with JsonMemoryStore(Path(directory)) as store:
                memory = MemoryManager("system", store)
                memory.begin_turn("执行")
                mid = memory.append_message({"role": "assistant", "tool_calls": [{"function": {"name": "shell", "arguments": {"command": "pwd"}}}]})
                memory.record_tool_starts([("shell", {"command": "pwd"})], mid)
            with JsonMemoryStore(Path(directory)) as store:
                restored = MemoryManager("system", store)
                turn = restored.state.recent_turns[0]
                self.assertEqual(turn["status"], "interrupted")
                self.assertEqual(turn["tool_executions"][0]["status"], "unknown")
                self.assertTrue(restored.notices)

    def test_commit_failure_does_not_change_live_state(self):
        with tempfile.TemporaryDirectory() as directory, JsonMemoryStore(Path(directory)) as store:
            memory = MemoryManager("system", store)
            revision = memory.state.revision
            with patch("src.service.memory_store.os.replace", side_effect=OSError("disk full")):
                with self.assertRaises(StorageError):
                    memory.begin_turn("你好")
            self.assertEqual(memory.state.revision, revision)
            self.assertIsNone(memory.current)

    def test_directory_sync_failure_poison_store(self):
        with tempfile.TemporaryDirectory() as directory, JsonMemoryStore(Path(directory)) as store:
            memory = MemoryManager("system", store)
            with patch("src.service.memory_store.os.fsync", side_effect=[None, OSError("sync failed")]):
                with self.assertRaisesRegex(StorageError, "durability_uncertain"):
                    memory.begin_turn("你好")
            self.assertTrue(store.poisoned)
            self.assertEqual(store.load().revision, 1)
            with self.assertRaises(StorageError):
                memory.begin_turn("重试")

    def test_checkpoint_failure_means_tool_not_run(self):
        class Model:
            def chat(self, messages, tools):
                return {"message": {"role": "assistant", "tool_calls": [{"function": {"name": "fake", "arguments": {}}}]}}
        class Tool:
            name = "fake"; definition = {}
            called = False
            def run(self):
                self.called = True
                return {}
        memory = MemoryManager("system"); tool = Tool()
        with patch.object(memory, "record_tool_starts", side_effect=StorageError("disk full")):
            with self.assertRaises(StorageError):
                Agent(Model(), [tool], "system", memory=memory).ask("执行")
        self.assertFalse(tool.called)

    def test_result_save_failure_does_not_replay_tool(self):
        class Model:
            def chat(self, messages, tools):
                return {"message": {"role": "assistant", "tool_calls": [{"function": {"name": "fake", "arguments": {}}}]}}
        class Tool:
            name = "fake"; definition = {}
            calls = 0
            def run(self):
                self.calls += 1
                return {}
        memory = MemoryManager("system"); tool = Tool()
        with patch.object(memory, "record_tool_result", side_effect=StorageError("disk full")):
            with self.assertRaisesRegex(StorageError, "命令可能已执行"):
                Agent(Model(), [tool], "system", memory=memory).ask("执行")
        self.assertEqual(tool.calls, 1)
        self.assertEqual(memory.current["tool_executions"][0]["status"], "running")

    def test_single_writer_lock_between_processes(self):
        with tempfile.TemporaryDirectory() as directory:
            context = multiprocessing.get_context("spawn")
            with JsonMemoryStore(Path(directory)):
                queue = context.Queue()
                process = context.Process(target=try_lock, args=(directory, queue))
                process.start(); process.join(10)
                self.assertEqual(queue.get(timeout=2), "rejected")
                self.assertEqual(process.exitcode, 0)
            with JsonMemoryStore(Path(directory)):
                pass

    def test_revision_conflict_unknown_version_and_archive_integrity(self):
        with tempfile.TemporaryDirectory() as directory, JsonMemoryStore(Path(directory)) as store:
            memory = MemoryManager("system", store)
            turn = finish(memory)
            with self.assertRaises(StorageError):
                store.commit(deepcopy(memory.state), 0)
            ref = store.archive(turn, memory.state.memory_epoch)
            self.assertEqual(store.load_archived_turn(ref)["id"], turn["id"])
            (Path(directory) / ref["path"]).write_text("{}")
            with self.assertRaises(MemoryError):
                store.load_archived_turn(ref)
            path = Path(directory) / "memory.json"
            data = json.loads(path.read_text()); data["schema_version"] = 99; path.write_text(json.dumps(data))
            with self.assertRaises(MemoryError):
                store.load()
            self.assertEqual(json.loads(path.read_text())["schema_version"], 99)

    def test_migration_orphans_unfinished_and_no_reimport(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.json"
            path.write_text(json.dumps([
                {"role": "tool", "content": "orphan"},
                {"role": "user", "content": "你好"}, {"role": "assistant", "content": "回答"},
                {"role": "user", "content": "未完成"},
            ]))
            with JsonMemoryStore(Path(directory)) as store:
                memory = MemoryManager("system", store)
                self.assertEqual(len(memory.state.recent_turns), 2)
                self.assertEqual(memory.state.recent_turns[-1]["status"], "interrupted")
                self.assertEqual(len(memory.state.migration["orphaned"]), 1)
                self.assertTrue((Path(directory) / memory.state.migration["backup"]).exists())
                path.write_text("[]")
                reloaded = MemoryManager("system", store)
                self.assertEqual(len(reloaded.state.recent_turns), 2)
                old_epoch = reloaded.state.memory_epoch
                reloaded.clear_all()
                self.assertNotEqual(reloaded.state.memory_epoch, old_epoch)
                self.assertFalse(path.exists())
                self.assertFalse((Path(directory) / "migration").exists())

    def test_archive_written_but_commit_failed_is_reused(self):
        with tempfile.TemporaryDirectory() as directory, JsonMemoryStore(Path(directory)) as store:
            extractor = Extractor()
            memory = MemoryManager("system", store, extractor, MemoryPolicy(recent_turns=1))
            finish(memory); finish(memory)
            original = store.commit
            def commit(candidate, expected):
                if candidate.archives:
                    raise StorageError("simulate crash after archive")
                original(candidate, expected)
            with patch.object(store, "commit", side_effect=commit):
                with self.assertRaises(StorageError):
                    memory.maintain_safely()
            self.assertEqual(len(memory.state.recent_turns), 2)
            self.assertTrue(list((Path(directory) / "archive").rglob("*.json")))
            memory.maintain_safely()
            self.assertEqual(len(memory.state.recent_turns), 1)
            self.assertEqual(len(memory.state.compaction["receipts"]), 1)

    def test_redaction_and_default_debug_do_not_print_content(self):
        private = {"api_key": "value", "command": "token=abcdef", "text": "Bearer xyzxyz"}
        self.assertNotIn("abcdef", json.dumps(redact(private)))
        with io.StringIO() as output, patch("sys.stdout", output):
            _show_model_event(ModelExecutionEvent("requested", 1, [{"content": "private-message"}]))
            self.assertNotIn("private-message", output.getvalue())

    def test_cli_clear_confirmation_and_memory_commands(self):
        memory = MemoryManager("system")
        turn = finish(memory); merge_facts(memory.state, [fact(turn)], [turn])
        with patch("sys.stdout", new_callable=io.StringIO), patch("builtins.input", return_value="no"):
            memory_command("/memory clear-all", memory)
            self.assertEqual(len(memory.state.facts), 1)
        with patch("sys.stdout", new_callable=io.StringIO), patch("builtins.input", return_value="CLEAR"):
            memory_command("/memory clear-all", memory)
            self.assertFalse(memory.state.facts)
            self.assertTrue(memory_command("/memory retry", memory))
            self.assertTrue(memory_command("/memory", memory))

    def test_extractor_isolated_tools_schema_and_invalid_output(self):
        class Model:
            def chat(self, messages, tools):
                self.tools = tools; self.messages = messages
                return {"message": {"content": json.dumps({"schema_version": 1, "source_turn_ids": ["t"], "facts": []})}}
        model = Model(); extractor = ModelMemoryExtractor(model)
        self.assertEqual(extractor.extract_facts({"source_turn_ids": ["t"]}), [])
        self.assertEqual(model.tools, [])
        self.assertIn("事实提取器", model.messages[0]["content"])
        with patch.object(model, "chat", return_value={"message": {"content": "invalid"}}):
            with self.assertRaises(MemoryError):
                extractor.extract_facts({"source_turn_ids": ["t"]})

    def test_fragmented_compaction_is_bounded_and_resumable(self):
        extractor = Extractor()
        policy = MemoryPolicy(recent_turns=1, extraction_max_chars=4000, maintenance_calls=2)
        memory = MemoryManager("system", extractor=extractor, policy=policy)
        finish(memory, "长内容" * 4000); finish(memory)
        for _ in range(30):
            before = len(extractor.fact_calls)
            memory.maintain_safely()
            self.assertLessEqual(len(extractor.fact_calls) - before, 2)
            if len(memory.state.recent_turns) == 1:
                break
        self.assertEqual(len(memory.state.recent_turns), 1)
        self.assertGreater(len(extractor.fact_calls), 2)

    def test_main_loop_limit_and_failures_are_terminal_turns(self):
        class Model:
            def chat(self, messages, tools):
                raise RuntimeError("model offline")
        memory = MemoryManager("system")
        with self.assertRaises(RuntimeError):
            Agent(Model(), [], "system", memory=memory).ask("你好")
        self.assertEqual(memory.state.recent_turns[-1]["status"], "failed")
        class Loop:
            def chat(self, messages, tools):
                return {"message": {"role": "assistant", "tool_calls": [{"function": {"name": "fake", "arguments": {}}}]}}
        class Tool:
            name = "fake"; definition = {}
            def run(self): return {"ok": True}
        agent = Agent(Loop(), [Tool()], "system", memory=memory)
        agent.max_tool_rounds = 2
        self.assertIn("上限", agent.ask("执行任务"))
        self.assertEqual(memory.state.recent_turns[-1]["status"], "failed")
        self.assertEqual(len(memory.state.recent_turns[-1]["tool_executions"]), 2)

    def test_keyboard_interrupt_persisted(self):
        class Model:
            def chat(self, messages, tools): raise KeyboardInterrupt()
        memory = MemoryManager("system")
        with self.assertRaises(KeyboardInterrupt):
            Agent(Model(), [], "system", memory=memory).ask("你好")
        self.assertEqual(memory.state.recent_turns[-1]["status"], "interrupted")

    def test_pending_purge_blocks_new_turn_and_restart_retries(self):
        with tempfile.TemporaryDirectory() as directory:
            with JsonMemoryStore(Path(directory)) as store:
                memory = MemoryManager("system", store)
                finish(memory)
                with patch.object(store, "purge", return_value=["migration"]):
                    self.assertEqual(memory.clear_all(), ["migration"])
                with self.assertRaises(StorageError):
                    memory.begin_turn("新任务")
            with JsonMemoryStore(Path(directory)) as store:
                memory = MemoryManager("system", store)
                self.assertFalse(memory.state.maintenance["purge_pending"])
                self.assertFalse(memory.state.recent_turns)
                memory.begin_turn("新任务")

    def test_redacted_storage_keeps_live_user_input(self):
        memory = MemoryManager("system")
        memory.begin_turn("本次使用 token=abcdefgh")
        self.assertIn("abcdefgh", memory.build_context([])[-1]["content"])
        self.assertNotIn("abcdefgh", json.dumps(memory.state.to_dict()))
        memory.append_message({"role": "assistant", "content": "收到"})
        memory.finish_turn("completed")
        self.assertFalse(memory._runtime_messages)

    def test_task_failure_is_visible_and_retried(self):
        extractor = Extractor(); extractor.fail = True
        memory = MemoryManager("system", extractor=extractor)
        turn = finish(memory, "实现存储")
        memory.maintain_safely()
        self.assertEqual(memory.state.recent_turns[0]["task_update_status"], "failed")
        self.assertIn(turn["id"], memory.summary()["maintenance"]["pending_turn_ids"])
        extractor.fail = False
        extractor.changes = [task(turn)]
        memory.maintain_safely()
        self.assertEqual(memory.state.tasks[0]["status"], "in_progress")
        self.assertIsNone(memory.state.maintenance["last_error_code"])

    def test_task_projection_fits_large_output_without_losing_source_ids(self):
        memory = MemoryManager("system")
        turn = finish(memory, "实现报告", "x" * 80000)
        batch = memory._batch(turn, "tasks")
        self.assertLess(len(json.dumps(batch)), memory.policy.extraction_max_chars - 2000)
        self.assertEqual(batch["turn"]["messages"][-1]["id"], turn["final_message_id"])
        self.assertIn("截断", batch["turn"]["messages"][-1]["content"])

    def test_fake_file_completion_rejected_without_tool(self):
        memory = MemoryManager("system")
        turn = finish(memory, "创建一个文件", "已经创建了")
        change = task(turn, "completed")
        change["completion_evidence"] = [{"turn_id": turn["id"], "message_id": turn["final_message_id"]}]
        with self.assertRaises(MemoryError):
            apply_tasks(memory.state, [change], turn)

    def test_path_traversal_and_symlinks_rejected(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            with JsonMemoryStore(Path(directory)) as store:
                (Path(directory) / "archive").symlink_to(outside)
                with self.assertRaises(MemoryError):
                    store.load_archived_turn({"path": "../outside.json", "turn_id": "x", "hash": "x"})
                with self.assertRaises(MemoryError):
                    store.load_archived_turn({"path": "archive/outside.json", "turn_id": "x", "hash": "x"})

    def test_expired_facts_and_disputed_status_in_context(self):
        memory = MemoryManager("system")
        turn = finish(memory)
        merge_facts(memory.state, [fact(turn)], [turn])
        memory.state.facts[0]["valid_until"] = "2000-01-01T00:00:00Z"
        memory.begin_turn("记忆是什么")
        context = json.dumps(memory.build_context([]), ensure_ascii=False)
        self.assertNotIn("preferred_language", context)
        memory.state.facts[0]["valid_until"] = None
        memory.state.facts[0]["status"] = "disputed"
        self.assertIn("disputed", json.dumps(memory.build_context([])))

    def test_complete_current_exchange_compacted_as_data_when_budget_tight(self):
        memory = MemoryManager("system", policy=MemoryPolicy(context_max_chars=3000))
        memory.begin_turn("当前输入必须保留")
        for _ in range(10):
            mid = memory.append_message({"role": "assistant", "tool_calls": [{"function": {"name": "shell", "arguments": {"command": "pwd"}}}]})
            eid = memory.record_tool_starts([("shell", {})], mid)[0]
            memory.record_tool_result(eid, "shell", {}, {"exit_code": 0, "stdout": "x" * 1000})
        context = memory.build_context([])
        self.assertTrue(any(m.get("content") == "当前输入必须保留" for m in context))
        self.assertLess(len(json.dumps(context, ensure_ascii=False)), 3000)
        self.assertFalse(any(m["role"] == "tool" for m in context))

    def test_correction_can_resolve_conflict_by_restating_old_value(self):
        memory = MemoryManager("system")
        turn = finish(memory)
        merge_facts(memory.state, [fact(turn), fact(turn, "英文")], [turn])
        changed = finish(memory, "更正，改为中文")
        merge_facts(memory.state, [fact(changed, "中文", "explicit_correction")], [changed])
        active = [f for f in memory.state.facts if f["status"] == "active"]
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["value"], "中文")

    def test_plan_source_cannot_be_rephrased_as_current_fact(self):
        memory = MemoryManager("system")
        turn = finish(memory, "我计划迁移到PostgreSQL")
        candidate = fact(turn, "PostgreSQL")
        with self.assertRaises(MemoryError):
            merge_facts(memory.state, [candidate], [turn])

    def test_configuration_resolves_relative_paths_and_validates(self):
        from src.common import Settings
        with patch.dict(os.environ, {"MEMORY_DIR": "test-memory", "MEMORY_ENABLED": "false"}, clear=True), patch("src.common.config._load_dotenv"):
            settings = Settings.from_env()
            self.assertEqual(settings.memory_dir, Path(__file__).resolve().parents[1] / "test-memory")
            self.assertFalse(settings.memory_enabled)
        for changes in ({"MEMORY_ENABLED": "maybe"}, {"MEMORY_RECENT_TURNS": "0"}, {"MEMORY_CONTEXT_MAX_CHARS": "oops"}):
            with patch.dict(os.environ, changes, clear=True), patch("src.common.config._load_dotenv"), self.assertRaises(ValueError):
                Settings.from_env()

    def test_cli_subprocess_start_reset_and_reload(self):
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ, MEMORY_DIR=directory, MEMORY_DEBUG_CONTENT="false", MEMORY_ENABLED="true")
            result = subprocess.run([sys.executable, "-m", "src"], input="/memory\n/reset\n/exit\n", text=True, capture_output=True, env=env, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("recent_turns", result.stdout)
            self.assertIn("短期对话已清除", result.stdout)
            state = json.loads((Path(directory) / "memory.json").read_text())
            self.assertEqual(state["recent_turns"], [])
            result = subprocess.run([sys.executable, "-m", "src"], input="/memory\n/exit\n", text=True, capture_output=True, env=env, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("启动失败", result.stdout)

    def test_gateway_uses_maintenance_timeout_json_mode_and_no_tools(self):
        from src.service.ollama import OllamaClient
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_args): pass
            def read(self): return b'{"message":{"role":"assistant","content":"{}"}}'
        with patch("src.service.ollama.urlopen", return_value=Response()) as request:
            OllamaClient("http://local", "test-model", timeout=7, json_mode=True).chat([], [])
        args, kwargs = request.call_args
        payload = json.loads(args[0].data)
        self.assertEqual(kwargs["timeout"], 7)
        self.assertEqual(payload["format"], "json")
        self.assertEqual(payload["tools"], [])

    def test_completed_answer_survives_maintenance_storage_error(self):
        from src.application.agent import CompletedTurnStorageError
        class Model:
            def chat(self, messages, tools):
                return {"message": {"role": "assistant", "content": "已经生成答案"}}
        memory = MemoryManager("system")
        with patch.object(memory, "maintain_safely", side_effect=StorageError("disk full")):
            with self.assertRaises(CompletedTurnStorageError) as raised:
                Agent(Model(), [], "system", memory=memory).ask("解释一个概念")
        self.assertEqual(raised.exception.answer, "已经生成答案")
        self.assertEqual(memory.state.recent_turns[-1]["status"], "completed")

    def test_archive_orphans_are_diagnostic_only(self):
        with tempfile.TemporaryDirectory() as directory, JsonMemoryStore(Path(directory)) as store:
            memory = MemoryManager("system", store)
            turn = finish(memory)
            store.archive(turn, memory.state.memory_epoch)
            self.assertEqual(memory.summary()["orphan_archives"], 1)
            self.assertEqual(memory.state.archives, [])
            self.assertEqual(store.load().facts, [])

    def test_nonterminal_task_states_and_scope_validation(self):
        memory = MemoryManager("system")
        turn = finish(memory, "实现报告")
        candidate = task(turn, "waiting_user"); candidate["waiting_for"] = "内容"
        apply_tasks(memory.state, [candidate], turn)
        tid = memory.state.tasks[0]["id"]
        candidate = task(turn, "completed", tid)
        with self.assertRaises(MemoryError):
            apply_tasks(deepcopy(memory.state), [candidate], turn)
        next_turn = finish(memory, "继续，内容如下")
        apply_tasks(memory.state, [task(next_turn, task_id=tid)], next_turn)
        block = task(next_turn, "blocked", tid); block["blockers"] = ["缺少服务"]
        apply_tasks(memory.state, [block], next_turn)
        self.assertEqual(memory.state.tasks[0]["status"], "blocked")
        candidate = fact(next_turn, scope="another-project")
        with self.assertRaises(MemoryError):
            merge_facts(memory.state, [candidate], [next_turn])

    def test_migration_large_history_and_missing_tool_result(self):
        with tempfile.TemporaryDirectory() as directory, JsonMemoryStore(Path(directory)) as store:
            history = []
            for i in range(15):
                history += [{"role": "user", "content": str(i)}, {"role": "assistant", "content": "收到"}]
            history += [{"role": "user", "content": "执行"}, {"role": "assistant", "tool_calls": [{"function": {"name": "shell", "arguments": {"command": "pwd"}}}]}]
            (Path(directory) / "history.json").write_text(json.dumps(history))
            memory = MemoryManager("system", store)
            self.assertEqual(len(memory.state.recent_turns), 16)
            self.assertEqual(memory.state.recent_turns[-1]["tool_executions"][0]["status"], "unknown")
            memory.begin_turn("新问题")
            context = memory.build_context([])
            self.assertFalse(any(m.get("tool_calls") for m in context))
            self.assertLess(sum(m["role"] == "user" for m in context), 16)

    def test_empty_proposal_for_explicit_waiting_task_is_not_silent_success(self):
        memory = MemoryManager("system", extractor=Extractor())
        finish(memory, "请创建一份报告，内容等我提供")
        memory.maintain_safely()
        self.assertEqual(memory.state.recent_turns[0]["task_update_status"], "failed")
        self.assertTrue(memory.state.maintenance["pending_turn_ids"])
        self.assertTrue(memory.notices)
