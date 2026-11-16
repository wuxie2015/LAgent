"""Create a workspace text file or replace one exact fragment."""
import os
from pathlib import Path
import stat
from uuid import uuid4

from .read_only import ProjectFilesTool


class WriteFileTool:
    name = "write_file"
    definition = {"type": "function", "function": {
        "name": name,
        "description": "写工作区 UTF-8 文件。新文件只需 path/content；修改已有文件时，先读取并提供唯一匹配的 old_text。",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "工作区内相对路径"},
            "content": {"type": "string", "description": "新文件内容或用于替换 old_text 的文本"},
            "old_text": {"type": "string", "description": "读取到的唯一原文片段；省略则只允许新建"},
        }, "required": ["path", "content"]}}}

    def __init__(self, files: ProjectFilesTool):
        self.files = files

    def run(self, path: str, content: str, old_text: str | None = None) -> dict:
        try:
            self.files._path(path)
            relative = Path(path)
            if not relative.name:
                raise ValueError("目标必须是文件路径")
            data = content.encode("utf-8")
            if len(data) > self.files.max_bytes or b"\0" in data:
                raise ValueError("只支持不超过 1MB 的 UTF-8 文本")
            with self.files._directory(relative.parts[:-1], create=old_text is None) as directory:
                self._write(directory, relative.name, data, old_text)
            return {"path": relative.as_posix(), "status": "created" if old_text is None else "updated"}
        except (OSError, UnicodeError) as exc:
            if isinstance(exc, FileExistsError):
                return {"error": "目标已存在；读取文件后用 old_text 修改"}
            return {"error": f"文件写入失败：{type(exc).__name__}"}
        except ValueError as exc:
            return {"error": str(exc)}

    def _write(self, directory, name, data, old_text):
        info = None
        if old_text is not None:
            text, info = self.files._text(directory, name)
            # Line reads omit terminators. Keep the file's CRLF convention for edits.
            replacement = data.decode("utf-8")
            if "\r\n" in text and "\n" not in text.replace("\r\n", ""):
                old_text = old_text.replace("\r\n", "\n").replace("\n", "\r\n")
                replacement = replacement.replace("\r\n", "\n").replace("\n", "\r\n")
            start = text.find(old_text)
            if start < 0 or text.find(old_text, start + 1) >= 0:
                raise ValueError("old_text 必须唯一匹配；请重新读取并选择更精确的原文")
            data = (text[:start] + replacement + text[start + len(old_text):]).encode("utf-8")
            if len(data) > self.files.max_bytes:
                raise ValueError("修改后的文件超过 1MB")
        temp = ".lin-write-" + uuid4().hex
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666, dir_fd=directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                if info is not None:
                    os.fchmod(stream.fileno(), stat.S_IMODE(info.st_mode) & 0o777)
            if info is None:
                os.link(temp, name, src_dir_fd=directory, dst_dir_fd=directory)
            else:
                current = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if (current.st_ino, current.st_mtime_ns, current.st_ctime_ns) != (
                    info.st_ino, info.st_mtime_ns, info.st_ctime_ns
                ):
                    raise ValueError("写入前目标已变化；请重新读取")
                os.replace(temp, name, src_dir_fd=directory, dst_dir_fd=directory)
        finally:
            try:
                os.unlink(temp, dir_fd=directory)
            except FileNotFoundError:
                pass
