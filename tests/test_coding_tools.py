import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.application.cli import main
from src.common import Settings
from src.tools.read_only import ProjectFilesTool
from src.tools.run_python import RunPythonTool
from src.tools.write_file import WriteFileTool


class CodingToolsTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enter_temp()).resolve()
        self.files = ProjectFilesTool(self.root, blocked_paths=(self.root / "runtime",))
        self.writer = WriteFileTool(self.files)
        self.runner = RunPythonTool(self.files)
        self.addCleanup(self.runner.close)

    def enter_temp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return directory.name

    def test_find_search_and_read(self):
        self.writer.run("pkg/demo.py", "# 演示\nprint('Hello')\n")
        self.writer.run("pkg/hello.txt", "other\n")
        self.assertEqual(self.files.run("find", query="demo.py")["paths"], ["pkg/demo.py"])
        self.assertEqual(self.files.run("find", query="*.py")["paths"], ["pkg/demo.py"])
        matches = [{"path": "pkg/demo.py", "line": 2, "text": "print('Hello')"}]
        self.assertEqual(self.files.run("search", query="HELLO")["matches"], matches)
        self.assertEqual(self.files.run("search", path="pkg/demo.py", query="hello")["matches"], matches)
        self.assertEqual(self.files.run("read", path="pkg/demo.py", line_count=1)["lines"],
                         [{"line": 1, "text": "# 演示"}])

    def test_write_preserves_other_text_and_rejects_overwrite_or_ambiguous_anchor(self):
        path = self.root / "demo.py"
        path.write_bytes(b"before\r\nline\r\nline\r\nafter\r\n")
        path.chmod(0o755)
        for old in (None, "line", "stale"):
            self.assertIn("error", self.writer.run("demo.py", "changed", old_text=old))
        self.assertEqual(path.read_bytes(), b"before\r\nline\r\nline\r\nafter\r\n")
        self.assertEqual(self.writer.run("demo.py", "changed", old_text="before\nline")["status"], "updated")
        self.assertEqual(path.read_bytes(), b"changed\r\nline\r\nafter\r\n")
        self.assertEqual(path.stat().st_mode & 0o777, 0o755)

    def test_files_reject_protected_paths_and_symlink_escapes(self):
        outside = Path(self.enter_temp()) / "outside.py"
        outside.write_text("pass\n")
        (self.root / "alias.py").symlink_to(outside)
        (self.root / "escape").symlink_to(outside.parent, target_is_directory=True)
        (self.root / ".env").write_text("hidden")
        for path in (str(outside), "../outside.py", "alias.py", "escape/new.py", ".env", ".ENV", "Runtime/log"):
            with self.subTest(path=path):
                self.assertIn("error", self.writer.run(path, "other"))
                self.assertIn("error", self.files.run("read", path=path))
        self.assertEqual(self.files.run("find", query="*")["paths"], [])
        self.assertIn("error", self.runner.run(script="alias.py"))
        self.assertEqual(outside.read_text(), "pass\n")
        self.assertFalse((outside.parent / "new.py").exists())

    def test_python_uses_same_temporary_venv_for_scripts_and_modules(self):
        code = "import os, sys\nprint(sys.prefix)\nprint(os.getenv('LIN_ENV_MARKER'))\nprint(sys.argv[1])"
        self.writer.run("example.py", code)
        with patch.dict("os.environ", {"LIN_ENV_MARKER": "host-only"}):
            script = self.runner.run(script="example.py", args=["hello; world"])
            module = self.runner.run(module="example", args=["模块"])
        self.assertEqual(script["exit_code"], 0, script)
        self.assertEqual(module["exit_code"], 0, module)
        prefix = (Path(self.runner.environment.name) / "venv").resolve()
        self.assertEqual(script["stdout"].splitlines(), [str(prefix), "None", "hello; world"])
        self.assertEqual(module["stdout"].splitlines(), [str(prefix), "None", "模块"])
        self.runner.close()
        self.assertFalse(prefix.exists())

    def test_python_timeout_and_exit_status_are_reported(self):
        self.writer.run("fail.py", "import sys\nprint('failed', file=sys.stderr)\nsys.exit(3)")
        self.writer.run("wait.py", "import time\ntime.sleep(20)")
        result = self.runner.run(script="fail.py")
        self.assertEqual((result["exit_code"], result["stderr"]), (3, "failed\n"))
        self.assertTrue(self.runner.run(script="wait.py", timeout=1)["timed_out"])

    def test_natural_language_cli_edits_then_runs_in_tool_order(self):
        self.writer.run("hello.py", "print('before')\n")
        settings = Settings("http://unused", "test-model", None, 5, "INFO", memory_enabled=False,
                            scheduler_enabled=False, subagent_enabled=False)

        def calls(*tools):
            return {"message": {"role": "assistant", "tool_calls": [
                {"function": {"name": name, "arguments": args}} for name, args in tools
            ]}}

        with (
            patch("src.application.cli._enable_line_editing"),
            patch("src.application.cli.Settings.from_env", return_value=settings),
            patch("src.application.cli.configure_logging"),
            patch("src.application.cli.Path.cwd", return_value=self.root),
            patch("src.application.cli.OllamaClient") as model,
            patch("src.application.cli.ModelMemoryExtractor") as extractor,
            patch("builtins.input", side_effect=["找到 hello.py，改成输出你好并运行", "/exit"]),
            patch("sys.stdout", new_callable=io.StringIO) as output,
        ):
            extractor.return_value.propose_task_changes.return_value = []
            model.return_value.chat.side_effect = [
                calls(("project_files", {"action": "find", "query": "hello.py"})),
                calls(("project_files", {"action": "search", "path": "hello.py", "query": "before"})),
                calls(("project_files", {"action": "read", "path": "hello.py"})),
                calls(("write_file", {"path": "hello.py", "old_text": "before", "content": "你好"}),
                      ("run_python", {"script": "hello.py"})),
                {"message": {"role": "assistant", "content": "已修改并运行。"}},
            ]
            main()
            self.assertIn("已修改并运行", output.getvalue())

        messages, definitions = model.return_value.chat.call_args.args
        run_result = json.loads(messages[-1]["content"])["result"]
        self.assertEqual((run_result["exit_code"], run_result["stdout"]), (0, "你好\n"))
        self.assertTrue({"project_files", "write_file", "run_python"} <= {d["function"]["name"] for d in definitions})
        self.assertEqual((self.root / "hello.py").read_text(), "print('你好')\n")
