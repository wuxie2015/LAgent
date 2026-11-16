import io
import unittest
from unittest.mock import patch

from src.application.agent import Agent
from src.application.cli import main, _show_model_event, _show_tool_event, _show_subagent_event
from src.domain import ModelExecutionEvent, ToolExecutionEvent
from types import SimpleNamespace
from src.common import Settings
from src.domain.memory import MemoryPolicy
from pathlib import Path
from src.tools.ask_user import AskUserTool
from src.tools.shell import ShellTool


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
            self.assertIn("skill_search", {tool.name for tool in agent_class.call_args.kwargs["tools"]})
            agent_class.return_value.ask.assert_called_once_with(question)
            self.assertIn("输入解码失败", output.getvalue())
            self.assertIn("搜索结果", output.getvalue())
            self.assertIn("再见", output.getvalue())

    def test_cli_waits_for_reply_and_passes_it_to_model_in_same_turn(self):
        settings = Settings("http://unused", "test-model", None, 5, "INFO", memory_enabled=False,
                            scheduler_enabled=False, subagent_enabled=False)
        question = "你想选择哪一天？"
        call = {"function": {"name": "ask_user", "arguments": {"message": question}}}
        shell = {"function": {"name": "shell", "arguments": {"command": "echo should-not-run"}}}
        with (
            patch("src.application.cli._enable_line_editing"),
            patch("src.application.cli.Settings.from_env", return_value=settings),
            patch("src.application.cli.configure_logging"),
            patch("src.application.cli.OllamaClient") as model,
            patch("src.application.cli.ModelMemoryExtractor") as extractor,
            patch.object(ShellTool, "run") as run_shell,
            patch("sys.stdout", new_callable=io.StringIO) as output,
        ):
            extractor.return_value.propose_task_changes.return_value = []
            chat = model.return_value.chat
            chat.side_effect = [
                {"message": {"role": "assistant", "tool_calls": [call, shell]}},
                {"message": {"role": "assistant", "tool_calls": [call]}},
                {"message": {"role": "assistant", "content": "好的，星期五。"}},
            ]
            inputs = iter(["请先问我日期，再复述", "", "星期五", "/exit"])

            def read(prompt):
                if prompt == "你（回复）：":
                    self.assertIn(question, output.getvalue())
                    self.assertEqual(chat.call_count, 2)
                return next(inputs)

            with patch("builtins.input", side_effect=read):
                main()

        run_shell.assert_not_called()
        self.assertEqual(chat.call_count, 3)
        context = chat.call_args.args[0]
        self.assertEqual(context[-1], {"role": "user", "content": "星期五"})
        self.assertEqual(context[-2]["role"], "tool")
        self.assertIn("好的，星期五。", output.getvalue())

    def test_interrupt_or_eof_stops_turn_before_another_model_call(self):
        for interruption, expected in [(KeyboardInterrupt, "user_input_cancelled"), (EOFError, "user_input_closed")]:
            with self.subTest(interruption=interruption), patch("src.application.cli.OllamaClient") as model:
                model.return_value.chat.return_value = {
                    "message": {"role": "assistant", "tool_calls": [
                        {"function": {"name": "ask_user", "arguments": {"message": "选哪一天？"}}}
                    ]}
                }

                def read(_message):
                    raise interruption

                agent = Agent(model.return_value, [AskUserTool(read)], "system")
                with self.assertRaises(interruption):
                    agent.ask("请给我安排时间")

                model.return_value.chat.assert_called_once()
                turn = agent.memory.state.recent_turns[-1]
                self.assertEqual(turn["status"], "interrupted")
                self.assertEqual(turn["tool_executions"][0]["status"], "failed")
                self.assertIn(expected, turn["messages"][-1]["content"])


if __name__ == "__main__":
    unittest.main()
