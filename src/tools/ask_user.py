"""Ask the current interactive user for a reply."""
from typing import Callable

from ..common.tool_errors import tool_error


class AskUserTool:
    name = "ask_user"
    definition = {
        "type": "function",
        "function": {
            "name": name,
            "description": "单独调用：向当前用户显示消息并等待回复，收到回复后继续本轮。适用于澄清、请求缺少的信息或确认授权。",
            "parameters": {
                "type": "object",
                "properties": {"message": {"type": "string", "description": "直接展示给用户的消息或问题。"}},
                "required": ["message"],
                "additionalProperties": False,
            },
        },
    }

    def __init__(self, read_reply: Callable[[str], str]):
        self.read_reply = read_reply

    def run(self, message: str) -> dict:
        message = "".join(char for char in message if char.isprintable() or char in "\n\t").strip()
        if not message:
            return tool_error("invalid_arguments", "给用户的消息不能为空。", True)
        try:
            return {"reply": self.read_reply(message)}
        except KeyboardInterrupt:
            return tool_error("user_input_cancelled", "用户中断了本轮。")
        except EOFError:
            return tool_error("user_input_closed", "终端输入已关闭。")
