from __future__ import annotations

import json
from typing import Any

from .ollama import OllamaClient
from .tools import WebSearchTool

SYSTEM_PROMPT = """你是 Lin Agent，一个谨慎、可靠的中文个人助手。
你唯一可用的外部工具是 web_search。遇到实时信息、新闻、价格、网页内容，或用户明确要求搜索时必须调用它。
搜索后只根据工具结果回答；每个重要的外部事实都在句末附上来源 URL。不能搜索或来源不足时请直说。
你目前没有文件写入、Shell、邮件或浏览器控制权限，不要声称执行了此类操作。"""


class Agent:
    def __init__(self, client: OllamaClient, search: WebSearchTool) -> None:
        self.client = client
        self.search = search
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]

    def reset(self) -> None:
        self.messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    def ask(self, text: str) -> str:
        self.messages.append({"role": "user", "content": text})
        for _ in range(4):
            response = self.client.chat(self.messages, [self.search.definition])
            message = response.get("message", {})
            self.messages.append(message)
            calls = message.get("tool_calls") or []
            if not calls:
                return message.get("content", "模型没有返回文本回复。")

            for call in calls:
                function = call.get("function", {})
                if function.get("name") != self.search.name:
                    output = {"error": "此工具未注册。"}
                else:
                    arguments = function.get("arguments", {})
                    if isinstance(arguments, str):
                        try:
                            arguments = json.loads(arguments)
                        except json.JSONDecodeError:
                            arguments = {}
                    output = self.search.run(str(arguments.get("query", "")))
                self.messages.append({"role": "tool", "content": json.dumps(output, ensure_ascii=False)})

        return "工具调用次数达到本轮上限；请缩小问题范围后重试。"
