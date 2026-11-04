"""Opt-in Ollama acceptance with synthetic data and a temporary memory directory.
Run: venv/bin/python scripts/verify_memory_model.py --model gpt-oss:120b-cloud
No access to the user's real memory or shell tools.
"""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.application.memory_manager import MemoryManager
from src.domain.memory.fact import merge_facts
from src.service.memory_extractor import ModelMemoryExtractor
from src.service.memory_store import JsonMemoryStore
from src.service.ollama import OllamaClient


def finish(memory, user, answer):
    memory.begin_turn(user)
    memory.append_message({"role": "assistant", "content": answer})
    memory.finish_turn("completed")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="http://127.0.0.1:11434")
    parser.add_argument("--model", default="gpt-oss:120b-cloud")
    args = parser.parse_args()
    extractor = ModelMemoryExtractor(OllamaClient(args.host, args.model, timeout=30, json_mode=True))
    with tempfile.TemporaryDirectory(prefix="lin-memory-acceptance-") as directory:
        with JsonMemoryStore(Path(directory)) as store:
            memory = MemoryManager("你是助手，请基于提供的记忆回答。", store, extractor)
            finish(memory, "我明确偏好：以后默认用中文回复我。", "已了解你的语言偏好。")
            memory.maintain_safely()
            for _ in range(10):
                finish(memory, "1加1等于多少？", "2。")
                memory.maintain_safely()
            assert len(memory.state.recent_turns) == 10, memory.summary()
            assert any("中文" in f["value"] for f in memory.state.facts), "模型没有提取明确语言偏好"
            assert len(memory.state.archives) == 1
            print("PASS: 11轮窗口、真实模型事实提取、归档")
        with JsonMemoryStore(Path(directory)) as store:
            memory = MemoryManager("你是助手，请基于提供的记忆回答。", store, extractor)
            memory.begin_turn("我的默认回复语言偏好是什么？")
            result = OllamaClient(args.host, args.model, timeout=30).chat(memory.build_context([]), [])
            assert "中文" in result["message"]["content"], "恢复后回答没有使用长期事实"
            memory.append_message(result["message"])
            memory.finish_turn("completed")
            print("PASS: 重启恢复、模型读取长期事实")
            finish(memory, "更正我的偏好：以后改为英文回复。", "已了解新的语言偏好。")
            turn = memory.state.recent_turns[-1]
            proposed = extractor.extract_facts(memory._batch(turn, "facts"))
            probe = deepcopy(memory.state)
            merge_facts(probe, proposed, [turn])
            active = [f for f in probe.facts if f["status"] == "active"]
            assert any("英文" in f["value"] or "English" in f["value"] for f in active), "模型没有激活更正后的事实"
            assert not any("中文" in f["value"] for f in active), "模型未复用原事实属性，旧偏好仍处于活跃状态"
            print("PASS: 明确事实更正候选及合并验证")
            finish(memory, "请创建一份测试报告，先准备结构，内容等我提供。", "报告结构已准备；等待你提供内容后再完成正文。")
            turn = memory.state.recent_turns[-1]
            from src.domain.memory.task import apply_tasks
            proposed = extractor.propose_task_changes(memory._batch(turn, "tasks"))
            probe = deepcopy(memory.state)
            apply_tasks(probe, proposed, deepcopy(turn))
            assert probe.tasks and probe.tasks[-1]["status"] != "completed", "任务状态未正确保留待完成作业"
            print("PASS: 真实任务提取、等待状态、无虚假完成")
    print(json.dumps({"model": args.model, "fact_prompt_version": 2, "task_prompt_version": 2, "real_memory_accessed": False, "shell_enabled": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()
