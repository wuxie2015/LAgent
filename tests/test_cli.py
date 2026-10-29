import io
import unittest
from unittest.mock import patch

from src.application.cli import main
from src.common import Settings
from src.domain.memory import MemoryPolicy
from pathlib import Path


class CliTests(unittest.TestCase):
    def test_invalid_utf8_keeps_session_and_accepts_next_question(self):
        question = "朝鲜的首都最近发生了什么新闻"
        error = UnicodeDecodeError("utf-8", b"\xe6\x9c\xff", 0, 2, "invalid continuation byte")
        with (
            patch("src.application.cli.Settings") as settings,
            patch("src.application.cli.configure_logging"),
            patch("src.application.cli.load_system_prompt", return_value="system"),
            patch("src.application.cli.OllamaClient"),
            patch("src.application.cli.WebSearchTool"),
            patch("src.application.cli.Agent") as agent_class,
            patch("builtins.input", side_effect=[error, question, "/exit"]),
            patch("sys.stdout", new_callable=io.StringIO) as output,
        ):
            settings.from_env.return_value = Settings("http://unused", "test-model", None, 5, "INFO", memory_enabled=False)
            agent_class.return_value.ask.return_value = "搜索结果"

            main()

            agent_class.assert_called_once()
            agent_class.return_value.ask.assert_called_once_with(question)
            self.assertIn("输入解码失败", output.getvalue())
            self.assertIn("搜索结果", output.getvalue())
            self.assertIn("再见", output.getvalue())


if __name__ == "__main__":
    unittest.main()
