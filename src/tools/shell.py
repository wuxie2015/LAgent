from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import tempfile
from typing import Any


class ShellTool:
    name = "shell"
    definition: dict[str, Any] = {
        "type": "function",
        "function": {
            "name": name,
            "description": "在本机执行 Shell 命令，返回退出码、标准输出和标准错误。命令具有当前进程的权限。",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的 Shell 命令"},
                    "cwd": {"type": "string", "description": "工作目录，默认是启动 Agent 时的目录"},
                    "timeout": {"type": "integer", "description": "超时秒数，默认 30，范围 1–300"},
                },
                "required": ["command"],
                "additionalProperties": False,
            },
        },
    }
    output_limit = 16000

    def run(self, command: str, cwd: str | None = None, timeout: int = 30) -> dict[str, Any]:
        if not isinstance(command, str) or not command.strip():
            return {"error": "command 必须是非空字符串。"}
        if type(timeout) is not int or not 1 <= timeout <= 300:
            return {"error": "timeout 必须是 1–300 之间的整数。"}
        if cwd is not None and not isinstance(cwd, str):
            return {"error": "cwd 必须是目录路径字符串。"}
        workdir = Path(cwd).expanduser().resolve() if cwd is not None else Path.cwd()
        if not workdir.is_dir():
            return {"error": f"工作目录不存在或不是目录：{workdir}"}

        # Files avoid buffering unbounded command output in memory.
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            try:
                process = subprocess.Popen(
                    command, shell=True, executable="/bin/sh", cwd=workdir,
                    stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                    start_new_session=True,
                )
            except OSError as exc:
                return {"error": f"无法启动命令：{exc}"}
            timed_out = False
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()

            result: dict[str, Any] = {
                "cwd": str(workdir), "exit_code": process.returncode, "timed_out": timed_out,
            }
            for name, stream in (("stdout", stdout), ("stderr", stderr)):
                stream.seek(0)
                data = stream.read(self.output_limit + 1)
                result[name] = data[:self.output_limit].decode("utf-8", errors="replace")
                result[f"{name}_truncated"] = len(data) > self.output_limit
            return result
