import json
import threading
import unittest

from src.application.agent import Agent
from src.common import load_system_prompt
from src.domain import ModelExecutionEvent, ToolExecutionEvent
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


class ParallelModel:
    def __init__(self):
        self.calls = []

    def chat(self, messages, tools):
        self.calls.append([message.copy() for message in messages])
        if len(self.calls) == 1:
            return {
                "message": {
                    "role": "assistant",
                    "tool_calls": [
                        {"function": {"name": "first_search", "arguments": {"query": "first"}}},
                        {"function": {"name": "second_search", "arguments": {"query": "second"}}},
                    ],
                }
            }
        return {"message": {"role": "assistant", "content": "完成"}}


class BarrierSearchTool:
    definition = WebSearchTool.definition

    def __init__(self, name, barrier):
        self.name = name
        self.barrier = barrier

    def run(self, query):
        self.barrier.wait(timeout=1)
        return {"query": query}


class AgentTests(unittest.TestCase):
    def test_automatically_executes_and_evaluates_tool_result(self):
        model = FakeModel()
        search = FakeSearchTool()
        agent = Agent(model, [search], load_system_prompt())

        answer = agent.ask("找到 Python 官方网站")

        self.assertIn("满足要求", answer)
        self.assertEqual(search.queries, ["Python"])
        self.assertEqual(model.calls[1][-1]["role"], "tool")

    def test_reports_tool_execution_events(self):
        events: list[ToolExecutionEvent] = []
        agent = Agent(FakeModel(), [FakeSearchTool()], load_system_prompt(), on_tool_event=events.append)

        agent.ask("找到 Python 官方网站")

        self.assertEqual([event.phase for event in events], ["started", "completed"])
        self.assertEqual(events[0].tool_name, "web_search")
        self.assertEqual(events[1].result["results"][0]["url"], "https://python.org")

    def test_reports_model_execution_events(self):
        events: list[ModelExecutionEvent] = []
        agent = Agent(FakeModel(), [FakeSearchTool()], load_system_prompt(), on_model_event=events.append)

        agent.ask("找到 Python 官方网站")

        self.assertEqual([event.phase for event in events], ["requested", "completed", "requested", "completed"])
        self.assertEqual(events[0].messages[-1], {"role": "user", "content": "找到 Python 官方网站"})
        self.assertEqual(events[1].response["message"]["role"], "assistant")
        self.assertEqual(events[2].messages[-1]["role"], "tool")

    def test_executes_same_round_tool_calls_concurrently_and_preserves_order(self):
        barrier = threading.Barrier(2)
        model = ParallelModel()
        agent = Agent(
            model,
            [BarrierSearchTool("first_search", barrier), BarrierSearchTool("second_search", barrier)],
            load_system_prompt(),
        )

        self.assertEqual(agent.ask("同时搜索"), "完成")

        tool_messages = model.calls[1][-2:]
        self.assertEqual([message["role"] for message in tool_messages], ["tool", "tool"])
        self.assertEqual([json.loads(message["content"])["tool"] for message in tool_messages], ["first_search", "second_search"])


if __name__ == "__main__":
    unittest.main()
