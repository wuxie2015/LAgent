from __future__ import annotations

import json
from typing import Any
from urllib.request import Request, urlopen


class WebSearchTool:
    name = "web_search"
    definition: dict[str, Any] = {
        "type": "function",
        "function": {
            "name": name,
            "description": "搜索互联网以获取实时、可核实的信息。返回标题、摘要和来源 URL。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "简洁、具体的搜索查询"},
                },
                "required": ["query"],
            },
        },
    }

    def __init__(self, api_key: str | None, result_limit: int = 5) -> None:
        self.api_key = api_key
        self.result_limit = result_limit

    def run(self, query: str) -> dict[str, Any]:
        if not query.strip():
            return {"error": "搜索词不能为空。"}

        if not self.api_key:
            return {
                "error": "网页搜索尚未配置。请将 Tavily API Key 写入本地 .env 的 TAVILY_API_KEY 后重试。"
            }

        payload = json.dumps(
            {
                "api_key": self.api_key,
                "query": query,
                "search_depth": "basic",
                "max_results": self.result_limit,
                "include_answer": False,
            }
        ).encode("utf-8")
        request = Request(
            "https://api.tavily.com/search",
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "LinAgent/0.1"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # Return an error to the model rather than ending the chat.
            return {"error": f"搜索请求失败：{exc}"}

        results = [
            {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("content", ""),
            }
            for item in payload.get("results", [])[: self.result_limit]
        ]

        return {"query": query, "results": results, "note": "请只基于这些结果作答，并在结论旁附 URL。"}
