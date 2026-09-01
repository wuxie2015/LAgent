import unittest

from src.application.agent import Agent
from src.common import load_system_prompt
from src.tools import WebSearchTool


class FakeSearchTool:
    name = "web_search"
    definition = WebSearchTool.definition

    def __init__(self):
        self.queries = []

    def run(self, query):
        self.queries.append(query)
        return {"results": [{"url": "https://python.org", "title": "Welcome to Python.org"}]}


class FakeModel:
    def __init__(self):
        self.calls = []

    def chat(self, messages, tools):
        self.calls.append([message.copy() for message in messages])
        if len(self.calls) == 1:
            return {
                "message": {
                    "role": "assistant",
                    "content": "我需要先搜索。",
                    "tool_calls": [{"function": {"name": "web_search", "arguments": {"query": "Python"}}}],
                }
            }
        return {"message": {"role": "assistant", "content": "结果满足要求：https://python.org"}}


class AgentTests(unittest.TestCase):
    def test_automatically_executes_and_evaluates_tool_result(self):
        model = FakeModel()
        search = FakeSearchTool()
        agent = Agent(model, [search], load_system_prompt())

        answer = agent.ask("找到 Python 官方网站")

        self.assertIn("满足要求", answer)
        self.assertEqual(search.queries, ["Python"])
        self.assertEqual(model.calls[1][-1]["role"], "tool")


if __name__ == "__main__":
    unittest.main()
