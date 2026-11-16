"""Run workspace Python code in a session-local dependency environment."""
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import venv

from .read_only import ProjectFilesTool
from .shell import run_process


class RunPythonTool:
    name = "run_python"
    definition = {"type": "function", "function": {
        "name": name,
        "description": "在临时 Python venv 中运行工作区脚本或模块（如 unittest）。隔离依赖，不是系统沙箱。",
        "parameters": {"type": "object", "properties": {
            "script": {"type": "string", "description": "工作区内 .py 文件的相对路径"},
            "module": {"type": "string", "description": "Python 模块名；与 script 二选一"},
            "args": {"type": "array", "items": {"type": "string"}, "description": "传给脚本或模块的参数"},
            "timeout": {"type": "integer", "minimum": 1, "maximum": 300},
        }}}}

    def __init__(self, files: ProjectFilesTool):
        self.files = files
        self.environment = None

    def close(self):
        if self.environment is not None:
            self.environment.cleanup()
            self.environment = None

    def run(self, script: str | None = None, module: str | None = None,
            args: list[str] | None = None, timeout: int = 30) -> dict:
        try:
            if (script is None) == (module is None):
                raise ValueError("必须且只能提供 script 或 module 之一")
            if type(timeout) is not int or not 1 <= timeout <= 300:
                raise ValueError("timeout 必须是 1–300 之间的整数")
            if script is not None:
                path = self.files._path(script)
                if path.suffix != ".py" or not path.is_file():
                    raise ValueError("script 必须是工作区内存在的 .py 文件")
                target = [str(path)]
            else:
                if not all(part.isidentifier() for part in module.split(".")):
                    raise ValueError("module 必须是有效的 Python 模块名")
                target = ["-m", module]
            if self.environment is None:
                self.environment = TemporaryDirectory(prefix="lin-python-")
                try:
                    venv.EnvBuilder(symlinks=True).create(Path(self.environment.name) / "venv")
                except BaseException:
                    self.close()
                    raise
            home = Path(self.environment.name)
            env_dir = home / "venv"
            bin_dir = env_dir / "bin"
            env = {"PATH": str(bin_dir) + os.pathsep + os.defpath, "HOME": str(home),
                   "TMPDIR": str(home), "VIRTUAL_ENV": str(env_dir)}
            return run_process([str(bin_dir / "python"), "-E", "-s", "-B", "-X", "utf8",
                                *target, *(args or [])], self.files.root, timeout, env=env)
        except ValueError as exc:
            return {"error": str(exc)}
        except OSError as exc:
            return {"error": f"虚拟环境运行失败：{type(exc).__name__}"}
