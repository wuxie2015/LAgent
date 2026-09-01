from __future__ import annotations

import json
import ssl
from typing import Any
from urllib.request import Request, urlopen

import certifi

from ..common.logging_config import get_logger


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
        logger = get_logger()
        if not query.strip():
            logger.warning("web_search_rejected_empty_query")
            return {"error": "搜索词不能为空。"}

        if not self.api_key:
            logger.warning("web_search_missing_api_key")
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
            logger.info("web_search_request provider=tavily query_length=%d", len(query))
            ssl_context = ssl.create_default_context(cafile=certifi.where())
            with urlopen(request, timeout=20, context=ssl_context) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # Return an error to the model rather than ending the chat.
            logger.exception("web_search_failed provider=tavily error_type=%s", type(exc).__name__)
            return {"error": f"搜索请求失败：{exc}"}

        results = [
            {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("content", ""),
            }
            for item in payload.get("results", [])[: self.result_limit]
        ]

        logger.info("web_search_response provider=tavily results=%d", len(results))
        return {"query": query, "results": results, "note": "请只基于这些结果作答，并在结论旁附 URL。"}
