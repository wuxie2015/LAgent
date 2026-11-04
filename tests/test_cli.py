import io
import unittest
from unittest.mock import patch

from src.application.cli import main, _show_model_event, _show_tool_event, _show_subagent_event
from src.domain import ModelExecutionEvent, ToolExecutionEvent
from types import SimpleNamespace
from src.common import Settings
from src.domain.memory import MemoryPolicy
from pathlib import Path


class CliTests(unittest.TestCase):
    def test_execution_events_only_show_summaries_even_in_debug_mode(self):
        events = [
            (_show_model_event, ModelExecutionEvent("requested", 1, [{"role": "user", "content": "private-content"}])),
            (_show_model_event, ModelExecutionEvent("completed", 1, [], {"message": {"content": "private-content"}})),
            (_show_tool_event, ToolExecutionEvent("started", 1, "web_search", {"query": "private-content"})),
            (_show_tool_event, ToolExecutionEvent("completed", 1, "web_search", {}, {"results": [{"content": "private-content"}]})),
            (_show_subagent_event, SimpleNamespace(parent_id="parent", task_id="child", phase="completed", status="completed")),
        ]
        for show, event in events:
            for debug in (False, True):
                with self.subTest(event=event, debug=debug), patch("sys.stdout", new_callable=io.StringIO) as output:
                    if show is _show_subagent_event:
                        show(event)
                    else:
                        show(event, debug=debug)
                    self.assertEqual(len(output.getvalue().splitlines()), 1)
                    self.assertNotIn("private-content", output.getvalue())
                    self.assertIn("第 1 轮" if show is _show_model_event else "web_search" if show is _show_tool_event else "child", output.getvalue())

    def test_invalid_utf8_keeps_session_and_accepts_next_question(self):
        question = "朝鲜的首都最近发生了什么新闻"
        error = UnicodeDecodeError("utf-8", b"\xe6\x9c\xff", 0, 2, "invalid continuation byte")
        with (
            patch("src.application.cli._enable_line_editing") as enable_editing,
            patch("src.application.cli.Settings") as settings,
            patch("src.application.cli.configure_logging"),
            patch("src.application.cli.load_system_prompt", return_value="system"),
            patch("src.application.cli.OllamaClient"),
            patch("src.application.cli.WebSearchTool"),
            patch("src.application.cli.SubagentRunner"),
            patch("src.application.cli.Agent") as agent_class,
            patch("builtins.input", side_effect=[error, question, "/exit"]),
            patch("sys.stdout", new_callable=io.StringIO) as output,
        ):
            settings.from_env.return_value = Settings("http://unused", "test-model", None, 5, "INFO", memory_enabled=False)
            agent_class.return_value.ask.return_value = "搜索结果"

            main()

            enable_editing.assert_called_once_with()
            agent_class.assert_called_once()
            agent_class.return_value.ask.assert_called_once_with(question)
            self.assertIn("输入解码失败", output.getvalue())
            self.assertIn("搜索结果", output.getvalue())
            self.assertIn("再见", output.getvalue())


if __name__ == "__main__":
    unittest.main()
