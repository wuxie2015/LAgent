from __future__ import annotations

import html
import re
from typing import Any
from urllib.parse import quote_plus, unquote, urlparse
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

    def __init__(self, result_limit: int = 5) -> None:
        self.result_limit = result_limit

    def run(self, query: str) -> dict[str, Any]:
        if not query.strip():
            return {"error": "搜索词不能为空。"}

        request = Request(
            f"https://html.duckduckgo.com/html/?q={quote_plus(query)}",
            headers={"User-Agent": "LinAgent/0.1 (+local personal agent)"},
        )
        try:
            with urlopen(request, timeout=20) as response:
                page = response.read().decode("utf-8", errors="replace")
        except Exception as exc:  # Return an error to the model rather than ending the chat.
            return {"error": f"搜索请求失败：{exc}"}

        links = re.findall(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', page, re.S)
        snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</(?:a|span)>', page, re.S)
        results = []
        for index, (link, title) in enumerate(links[: self.result_limit]):
            # DuckDuckGo sometimes wraps destination URLs in a redirect parameter.
            parsed = urlparse(html.unescape(link))
            destination = unquote(parsed.query.split("uddg=", 1)[-1].split("&", 1)[0]) if "uddg=" in parsed.query else link
            results.append(
                {
                    "title": re.sub(r"<[^>]+>", "", html.unescape(title)).strip(),
                    "url": destination,
                    "snippet": re.sub(r"<[^>]+>", "", html.unescape(snippets[index])).strip() if index < len(snippets) else "",
                }
            )

        return {"query": query, "results": results, "note": "请只基于这些结果作答，并在结论旁附 URL。"}
