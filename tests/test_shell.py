import tempfile
import unittest

from src.application.agent import Agent
from src.tools import ShellTool


class ShellTests(unittest.TestCase):
    def test_output_exit_code_and_working_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            result = ShellTool().run("printf '中文'; printf 'failed' >&2; exit 7", cwd=directory)
        self.assertEqual(result["stdout"], "中文")
        self.assertEqual(result["stderr"], "failed")
        self.assertEqual(result["exit_code"], 7)
        self.assertFalse(result["timed_out"])

    def test_timeout(self):
        result = ShellTool().run("sleep 10", timeout=1)
        self.assertTrue(result["timed_out"])
        self.assertNotEqual(result["exit_code"], 0)

    def test_invalid_arguments(self):
        for arguments in ({"command": ""}, {"command": "pwd", "timeout": 0},
                          {"command": "pwd", "cwd": "/nonexistent-lin-agent-dir"}):
            self.assertIn("error", ShellTool().run(**arguments))

    def test_output_is_truncated(self):
        tool = ShellTool()
        tool.output_limit = 4
        result = tool.run("printf '123456'")
        self.assertEqual(result["stdout"], "1234")
        self.assertTrue(result["stdout_truncated"])

    def test_agent_dispatches_command_arguments_and_returns_result_to_model(self):
        class Model:
            calls = 0

            def chat(self, messages, tools):
                self.calls += 1
                if self.calls == 1:
                    return {"message": {"role": "assistant", "tool_calls": [
                        {"function": {"name": "shell", "arguments": {"command": "printf hello"}}}
                    ]}}
                self.result = messages[-1]["content"]
                return {"message": {"role": "assistant", "content": "完成"}}

        model = Model()
        agent = Agent(model, [ShellTool()], "system")
        self.assertEqual(agent.ask("执行命令"), "完成")
        self.assertIn('"stdout": "hello"', model.result)


if __name__ == "__main__":
    unittest.main()
