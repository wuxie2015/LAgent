from __future__ import annotations

import json
import ssl
from typing import Any
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

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
                "required": ["query"], "additionalProperties": False,
            },
        },
    }

    def __init__(self, api_key: str | None, result_limit: int = 5) -> None:
        self.api_key = api_key
        self.result_limit = result_limit

    def run(self, query: str) -> dict[str, Any]:
        logger = get_logger()
        if not isinstance(query, str) or not query.strip():
            logger.warning("web_search_rejected_empty_query")
            return {"error": "query必须是非空字符串。", "error_code": "invalid_arguments", "retryable": True}

        if not self.api_key:
            logger.warning("web_search_missing_api_key")
            return {
                "error": "网页搜索尚未配置。请将 Tavily API Key 写入本地 .env 的 TAVILY_API_KEY 后重试。",
                "error_code": "not_configured", "retryable": False
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
        except HTTPError as exc:
            retryable = exc.code in {408, 429} or exc.code >= 500
            return {"error": f"搜索服务返回HTTP {exc.code}。", "error_code": "network_error" if retryable else "http_error", "retryable": retryable}
        except (URLError, OSError, TimeoutError) as exc:
            logger.warning("web_search_failed error_type=%s", type(exc).__name__)
            return {"error": "搜索网络请求失败：" + type(exc).__name__, "error_code": "network_error", "retryable": True}
        except (ValueError, UnicodeError):
            return {"error": "搜索服务响应不是有效JSON。", "error_code": "invalid_response", "retryable": False}
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list) or any(not isinstance(item, dict) for item in payload["results"]):
            return {"error": "搜索服务响应缺少有效results。", "error_code": "invalid_response", "retryable": False}

        results = [
            {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("content", ""),
            }
            for item in payload.get("results", [])[: self.result_limit]
        ]

        logger.info("web_search_response provider=tavily results=%d", len(results))
        return {"query": query, "results": results, "note": "这里只是搜索摘要；需要全文或源码时用web_fetch(url)，不要把id/cursor/url当web_search参数。引用支持结论的URL。"}
