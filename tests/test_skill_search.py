import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from src.application.agent import Agent
from src.domain.delegation import RequestBudget, SubagentPolicy
from src.domain.memory.state import MemoryValidationError
from src.domain.memory.task import apply_tasks
from src.tools.skill_search import SkillSearchTool


def reply(content="", calls=None):
    message = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = [{"function": {"name": name, "arguments": args}} for name, args in calls]
    return {"message": message}


class SkillSearchTests(unittest.TestCase):
    root = Path(__file__).resolve().parents[1] / "resource" / "skills"

    def test_matches_then_executes_with_existing_tool_and_drops_skill_next_turn(self):
        marker = "从变更的入口追到受影响的调用者与测试"
        matcher = Mock()
        matcher.chat.return_value = reply('{"skill":"code-review"}')
        reader = Mock()
        reader.name = "read_workspace"
        reader.definition = {"function": {"name": reader.name, "parameters": {"type": "object"}}}
        reader.run.return_value = {"observed": "review evidence"}
        main = Mock()
        main.chat.side_effect = [
            reply(calls=[("skill_search", {"need": "审查 Git 差异，输出问题和行号"})]),
            reply(calls=[("read_workspace", {})]),
            reply("已审查"), reply("新一轮"),
        ]
        agent = Agent(main, [SkillSearchTool(matcher, self.root), reader], "original rules")
        self.assertEqual(agent.ask("请审查本地改动。原始会话细节不需要发送给匹配模型。"), "已审查")
        matcher.chat.assert_called_once()
        messages, tools = matcher.chat.call_args.args
        self.assertEqual(tools, [])
        self.assertNotIn("原始会话细节", str(messages))
        self.assertNotIn(marker, str(messages))
        payload = json.loads(messages[-1]["content"])
        self.assertEqual(set(payload), {"need", "skills"})
        self.assertEqual({entry["name"] for entry in payload["skills"]}, {"code-review", "text-summary"})
        self.assertIn(marker, main.chat.call_args_list[1].args[0][0]["content"])
        self.assertIn(marker, main.chat.call_args_list[2].args[0][0]["content"])
        self.assertIn("review evidence", str(main.chat.call_args_list[2].args[0]))
        reader.run.assert_called_once_with()
        self.assertEqual(agent.last_run_metrics["model_calls_charged"], 4)
        self.assertNotIn(marker, json.dumps(agent.memory.state.recent_turns, ensure_ascii=False))
        self.assertEqual(agent.ask("普通问题"), "新一轮")
        self.assertNotIn(marker, str(main.chat.call_args.args[0]))

    def test_no_match_and_invalid_or_executable_selections(self):
        matcher = Mock()
        tool = SkillSearchTool(matcher, self.root)
        for response, expected in (
            (reply('{"skill":null}'), {"status": "no_match"}),
            (reply('{"skill":"../../other"}'), {"error_code": "invalid_skill_selection"}),
            (reply('{"skill":"text-summary"}', [("shell", {})]), {"error_code": "invalid_skill_selection"}),
            (reply('{"skill":"text-summary","extra":true}'), {"error_code": "invalid_skill_selection"}),
        ):
            with self.subTest(response=response):
                matcher.chat.return_value = response
                tool.begin_request(RequestBudget(SubagentPolicy()))
                result = tool.run("整理会议纪要")
                for key, value in expected.items():
                    self.assertEqual(result[key], value)
                self.assertNotIn("instructions", result)

    def test_catalog_is_reloaded_and_ignores_links(self):
        matcher = Mock()
        matcher.chat.return_value = reply('{"skill":"new-skill"}')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "linked").symlink_to(self.root / "code-review", target_is_directory=True)
            folder = root / "new-skill"
            folder.mkdir()
            path = folder / "SKILL.md"
            path.symlink_to(self.root / "code-review" / "SKILL.md")
            tool = SkillSearchTool(matcher, root)
            tool.begin_request(RequestBudget(SubagentPolicy()))
            self.assertEqual(tool.run("需要新技能"), {"status": "no_match"})
            matcher.chat.assert_not_called()
            path.unlink()
            path.write_text("---\nname: new-skill\ndescription: 新流程\n---\n\n执行流程", encoding="utf-8")
            self.assertEqual(tool.run("需要新技能")["instructions"], "执行流程")

    def test_matcher_leaves_final_model_call_available(self):
        matcher = Mock()
        main = Mock()
        main.chat.side_effect = [reply(calls=[("skill_search", {"need": "摘要会议"})]), reply("直接总结")]
        agent = Agent(main, [SkillSearchTool(matcher, self.root)], "rules", execution_policy=SubagentPolicy(max_calls=2))
        self.assertEqual(agent.ask("整理会议纪要"), "直接总结")
        matcher.chat.assert_not_called()
        self.assertEqual(main.chat.call_args.args[1], [])
        self.assertIn("skill_budget_exhausted", str(main.chat.call_args.args[0]))

    def test_skill_match_is_not_evidence_of_real_execution(self):
        matcher = Mock()
        matcher.chat.return_value = reply('{"skill":"text-summary"}')
        main = Mock()
        main.chat.side_effect = [reply(calls=[("skill_search", {"need": "整理文本"})]), reply("摘要正文")]
        agent = Agent(main, [SkillSearchTool(matcher, self.root)], "rules")
        agent.ask("请创建文件保存会议纪要")
        turn = agent.memory.state.recent_turns[-1]
        source = {"turn_id": turn["id"], "message_id": turn["user_message_id"]}
        match = {"turn_id": turn["id"], "message_id": turn["tool_executions"][0]["result_message_id"]}
        change = {"goal": "整理会议纪要并保存", "status": "completed", "evidence": [source],
                  "completion_evidence": [match]}
        with self.assertRaisesRegex(MemoryValidationError, "不能作为任务完成证据"):
            apply_tasks(agent.memory.state, [change], turn)
        change["completion_evidence"].append({"turn_id": turn["id"], "message_id": turn["final_message_id"]})
        with self.assertRaisesRegex(MemoryValidationError, "缺少实际执行证据"):
            apply_tasks(agent.memory.state, [change], turn)
