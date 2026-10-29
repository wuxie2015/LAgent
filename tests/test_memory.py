import json
from pathlib import Path
import tempfile
import unittest

from src.application.agent import Agent
from src.application.memory_manager import MemoryManager
from src.domain.memory import MemoryError
from src.service.memory_store import JsonMemoryStore


class MemoryTests(unittest.TestCase):
    def test_restart_restores_all_messages_with_current_system_prompt(self):
        class Model:
            def chat(self, messages, tools):
                return {"message": {"role": "assistant", "content": "你好"}}
        with tempfile.TemporaryDirectory() as directory:
            with JsonMemoryStore(Path(directory)) as store:
                memory = MemoryManager("旧提示词", store)
                Agent(Model(), [], "旧提示词", memory=memory).ask("你好")
            with JsonMemoryStore(Path(directory)) as store:
                restored = MemoryManager("新提示词", store)
                self.assertEqual(restored.messages[0]["content"], "新提示词")
                self.assertEqual(restored.messages[1]["content"], "你好")
                self.assertNotIn("旧提示词", (Path(directory) / "memory.json").read_text())

    def test_agent_persists_and_reset_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            with JsonMemoryStore(Path(directory)) as store:
                memory = MemoryManager("system", store)
                memory.begin_turn("我叫张林")
                memory.append_message({"role": "assistant", "content": "你好"})
                memory.finish_turn("completed")
                memory.reset()
            with JsonMemoryStore(Path(directory)) as store:
                self.assertEqual(MemoryManager("system", store).messages, [{"role": "system", "content": "system"}])

    def test_corrupt_history_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.json"
            for content in ("broken json", "{}", '[{"role": "invalid"}]'):
                path.write_text(content)
                with JsonMemoryStore(Path(directory)) as store:
                    with self.assertRaises(MemoryError):
                        MemoryManager("system", store)
                self.assertEqual(path.read_text(), content)
