from __future__ import annotations

import json
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from ..common.logging_config import get_logger


class OllamaError(RuntimeError):
    pass


class OllamaClient:
    def __init__(self, host: str, model: str) -> None:
        self.host = host
        self.model = model

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        logger = get_logger()
        logger.info("ollama_request model=%s messages=%d tools=%d", self.model, len(messages), len(tools))
        payload = json.dumps(
            {"model": self.model, "messages": messages, "tools": tools, "stream": False}
        ).encode("utf-8")
        request = Request(
            f"{self.host}/api/chat",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=180) as response:
                result = json.loads(response.read().decode("utf-8"))
                logger.info("ollama_response model=%s", self.model)
                return result
        except URLError as exc:
            logger.exception("ollama_connection_failed host=%s", self.host)
            raise OllamaError(
                f"无法连接 Ollama（{self.host}）。请确认它已启动：ollama serve"
            ) from exc
        except json.JSONDecodeError as exc:
            logger.exception("ollama_invalid_response")
            raise OllamaError("Ollama 返回了无法解析的响应。") from exc
