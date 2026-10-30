import io
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from src.application.cli import main, schedule_command
from src.common.config import Settings
from src.service.schedule_store import ScheduleStore
from src.tools.schedule import ScheduleTool


class SettingsScheduleTests(unittest.TestCase):
    def test_scheduler_environment_defaults_and_db_path(self):
        with patch.dict("os.environ", {}, clear=True), patch("src.common.config._load_dotenv"):
            settings = Settings.from_env()
        self.assertTrue(settings.scheduler_enabled)
        self.assertEqual(settings.schedule_timezone, "Asia/Tokyo")
        self.assertEqual(settings.schedule_default_channel, "console")
        self.assertEqual(settings.schedule_poll_interval, 1.0)
        self.assertEqual(settings.schedule_db, Path(__file__).resolve().parents[1] / "data" / "schedules.sqlite3")

    def test_scheduler_settings_validate_timezone_interval_grace_and_boolean(self):
        for change in ({"SCHEDULE_TIMEZONE": "Mars/Olympus"}, {"SCHEDULE_POLL_INTERVAL": "nan"},
                       {"SCHEDULE_GRACE_SECONDS": "31536001"}, {"SCHEDULER_ENABLED": "sometimes"}):
            with self.subTest(change=change), patch.dict("os.environ", change, clear=True), patch("src.common.config._load_dotenv"):
                with self.assertRaises(ValueError):
                    Settings.from_env()

    def test_scheduler_zero_grace_period_is_valid(self):
        with patch.dict("os.environ", {"SCHEDULE_GRACE_SECONDS": "0"}, clear=True), patch("src.common.config._load_dotenv"):
            self.assertEqual(Settings.from_env().schedule_grace_seconds, 0)


class ScheduleCliTests(unittest.TestCase):
    def test_schedule_commands_disabled_and_enabled(self):
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertTrue(schedule_command("/schedule list", None))
            self.assertIn("未启用", output.getvalue())

        with tempfile.TemporaryDirectory() as directory:
            store = ScheduleStore(Path(directory) / "schedules.sqlite3")
            from src.service.notifications import NotificationDispatcher
            tool = ScheduleTool(store, NotificationDispatcher())
            with patch("sys.stdout", new_callable=io.StringIO) as output:
                self.assertTrue(schedule_command("/schedule list", tool))
                self.assertIn('"tasks": []', output.getvalue())
                self.assertTrue(schedule_command("/schedule get absent", tool))
                self.assertIn('"ok": false', output.getvalue())

    def test_enabled_cli_starts_scheduler_registers_tool_commands_and_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Settings("http://localhost", "model", None, 5, "INFO", memory_enabled=False)
            settings = replace(base, scheduler_enabled=True, schedule_db=Path(directory) / "data" / "tasks.sqlite3")
            captured = {}

            class FakeScheduler:
                def __init__(self, store, dispatcher, execute_agent, **kwargs):
                    captured.update(store=store, dispatcher=dispatcher, execute_agent=execute_agent)
                    self.fatal_error = None
                def start(self):
                    captured["started"] = True
                def stop(self):
                    captured["stopped"] = True

            class FakeAgent:
                def __init__(self, client, tools, system_prompt, **kwargs):
                    captured["tools"] = tools
                def ask(self, text):
                    return "done"

            with (
                patch("src.application.cli.Settings.from_env", return_value=settings),
                patch("src.application.cli.configure_logging"),
                patch("src.application.cli.load_system_prompt", return_value="system"),
                patch("src.application.cli.OllamaClient"),
                patch("src.application.cli.Agent", FakeAgent),
                patch("src.application.cli.Scheduler", FakeScheduler),
                patch("builtins.input", side_effect=["/schedule list", "/exit"]),
                patch("sys.stdout", new_callable=io.StringIO) as output,
            ):
                main()

            self.assertTrue(captured["started"])
            self.assertTrue(captured["stopped"])
            self.assertTrue(any(isinstance(tool, ScheduleTool) for tool in captured["tools"]))
            self.assertTrue(settings.schedule_db.exists())
            self.assertIn('"tasks": []', output.getvalue())

    def test_background_agent_has_only_web_search_and_fresh_memory(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Settings("http://localhost", "model", None, 5, "INFO", memory_enabled=False)
            settings = replace(base, scheduler_enabled=True, schedule_db=Path(directory) / "tasks.sqlite3")
            executions = []
            class FakeScheduler:
                def __init__(self, store, dispatcher, execute_agent, **kwargs):
                    executions.append(execute_agent)
                    self.fatal_error = None
                def start(self): pass
                def stop(self): pass

            calls = []
            class FakeAgent:
                def __init__(self, client, tools, system_prompt, **kwargs):
                    calls.append((tools, system_prompt))
                def ask(self, text): return "search result"

            with (
                patch("src.application.cli.Settings.from_env", return_value=settings),
                patch("src.application.cli.configure_logging"),
                patch("src.application.cli.load_system_prompt", return_value="system"),
                patch("src.application.cli.OllamaClient"),
                patch("src.application.cli.Agent", FakeAgent),
                patch("src.application.cli.Scheduler", FakeScheduler),
                patch("builtins.input", side_effect=["/exit"]),
                patch("sys.stdout", new_callable=io.StringIO),
            ):
                main()
                self.assertEqual(executions[0]("find something"), "search result")

            self.assertEqual(len(executions), 1)
            self.assertEqual(len(calls), 2)
            background_tools, background_prompt = calls[1]
            self.assertEqual([tool.name for tool in background_tools], ["web_search"])
            self.assertIn("只读执行", background_prompt)
            self.assertNotIn("schedule", [tool.name for tool in background_tools])

    def test_scheduler_start_failure_still_runs_registered_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Settings("http://localhost", "model", None, 5, "INFO", memory_enabled=False)
            settings = replace(base, scheduler_enabled=True, schedule_db=Path(directory) / "tasks.sqlite3")
            calls = []

            class FailingScheduler:
                def __init__(self, *args, **kwargs): pass
                def start(self):
                    raise RuntimeError("worker failed to start")
                def stop(self):
                    calls.append("stopped")

            with (
                patch("src.application.cli.Settings.from_env", return_value=settings),
                patch("src.application.cli.configure_logging"),
                patch("src.application.cli.load_system_prompt", return_value="system"),
                patch("src.application.cli.OllamaClient"),
                patch("src.application.cli.Scheduler", FailingScheduler),
                patch("builtins.input", side_effect=AssertionError("CLI should not reach input")),
                patch("sys.stdout", new_callable=io.StringIO) as output,
            ):
                main()

            self.assertEqual(calls, ["stopped"])
            self.assertIn("启动失败", output.getvalue())


if __name__ == "__main__":
    unittest.main()
