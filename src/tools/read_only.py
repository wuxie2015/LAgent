"""Bounded text access inside a project; no command execution or writes."""
from contextlib import contextmanager
import fnmatch
import os
from pathlib import Path
import stat

from ..domain.memory.privacy import redact


class ProjectFilesTool:
    name = 'project_files'
    definition = {'type': 'function', 'function': {
        'name': name, 'description': '只读项目文件：find 按文件名查找，search 按字面文本查找，read 按行读取。',
        'parameters': {'type': 'object', 'properties': {
            'action': {'type': 'string', 'enum': ['read', 'find', 'search']},
            'path': {'type': 'string', 'description': '项目内相对路径，默认项目根目录'},
            'query': {'type': 'string', 'description': 'find 用 glob（如 *.py）；search 用不区分大小写的字面文本'},
            'start_line': {'type': 'integer'},
            'line_count': {'type': 'integer'}}, 'required': ['action']}}}
    denied = {'.git', '.aws', '.ssh', '.codex', '.agents', '.idea', '.venv', 'venv',
              'node_modules', '__pycache__', 'subagents'}

    def __init__(self, root, blocked_paths=()):
        self.root = Path(root).resolve()
        self.blocked_paths = tuple(Path(p).resolve() for p in blocked_paths)

    def _path(self, path):
        relative_path = Path(path)
        if not path or relative_path.is_absolute() or '..' in relative_path.parts:
            raise ValueError('必须使用工作区内的相对路径')
        candidate = self.root / relative_path
        try:
            resolved = candidate.resolve()
        except RuntimeError as exc:
            raise ValueError('路径包含循环符号链接') from exc
        if not resolved.is_relative_to(self.root):
            raise ValueError('路径超出工作区')
        for path in (candidate, resolved):
            folded = Path(str(path).casefold())
            if any(folded.is_relative_to(Path(str(blocked).casefold())) for blocked in self.blocked_paths):
                raise ValueError('禁止访问此运行时路径')
        relative = resolved.relative_to(self.root)
        # Check lexical and resolved components to reject secret aliases and escapes.
        for parts in (candidate.relative_to(self.root).parts, relative.parts):
            if parts and parts[0].casefold() == 'memory':
                raise ValueError('禁止读取主 Agent 运行时记忆')
            for part in parts:
                part = part.casefold()
                if part in self.denied or part.startswith('.env') or any(
                    fnmatch.fnmatch(part, pattern)
                    for pattern in ('*.pem', '*.key', '*credentials*', '*secret*', '*.sqlite*')
                ):
                    raise ValueError('禁止读取敏感或内部路径')
        return resolved

    @contextmanager
    def _directory(self, parts, create=False):
        directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in parts:
                flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                try:
                    next_directory = os.open(part, flags, dir_fd=directory)
                except FileNotFoundError:
                    if not create:
                        raise
                    os.mkdir(part, dir_fd=directory)
                    next_directory = os.open(part, flags, dir_fd=directory)
                os.close(directory)
                directory = next_directory
            yield directory
        finally:
            os.close(directory)

    max_bytes = 1_000_000

    def _text(self, directory, name):
        fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > self.max_bytes:
                raise ValueError('只支持不超过1MB的普通文本文件')
            data = stream.read(self.max_bytes + 1)
        if len(data) > self.max_bytes or b'\x00' in data:
            raise ValueError('只支持不超过1MB的普通文本文件')
        return data.decode('utf-8'), info

    def _lines(self, path):
        relative = path.relative_to(self.root)
        with self._directory(relative.parts[:-1]) as directory:
            text, _ = self._text(directory, relative.name or '.')
        return text.splitlines()

    def _scan(self, target):
        if target.is_file():
            yield target
            return
        if not target.is_dir():
            raise ValueError('搜索路径不存在或不是普通文件/目录')
        for directory, dirs, files in os.walk(target, followlinks=False):
            allowed = []
            for name in sorted(dirs):
                child = Path(directory, name)
                try:
                    if not child.is_symlink():
                        self._path(child.relative_to(self.root).as_posix())
                        allowed.append(name)
                except (ValueError, OSError):
                    continue
            dirs[:] = allowed
            for name in sorted(files):
                yield Path(directory, name)

    def run(self, action, path='.', query='', start_line=1, line_count=80):
        try:
            target = self._path(path)
            if action == 'read':
                if type(start_line) is not int or type(line_count) is not int or start_line < 1 or not 1 <= line_count <= 200:
                    raise ValueError('行号至少1，读取行数范围1–200')
                lines = self._lines(target)
                selected, size = [], 0
                for n, line in enumerate(lines[start_line - 1:start_line - 1 + line_count], start_line):
                    if size + len(line) > 8000:
                        break
                    selected.append({'line': n, 'text': redact(line)})
                    size += len(line)
                return {'path': str(target.relative_to(self.root)), 'lines': selected,
                        'truncated': start_line - 1 + len(selected) < len(lines)}
            if action not in {'find', 'search'} or not isinstance(query, str) or not query.strip() or len(query) > 200:
                raise ValueError('find/search 需要 1–200 字符的 query')
            hits = []
            key = 'paths' if action == 'find' else 'matches'
            needle = query.casefold()
            for scanned, candidate in enumerate(self._scan(target), 1):
                if scanned > 1000 or len(hits) > 20:
                    return {key: hits[:20], 'truncated': True}
                relative = candidate.relative_to(self.root).as_posix()
                try:
                    file = self._path(relative)
                    if action == 'find':
                        if file.is_file() and (fnmatch.fnmatchcase(relative, query)
                                               or fnmatch.fnmatchcase(candidate.name, query)):
                            hits.append(relative)
                        continue
                    for n, line in enumerate(self._lines(file), 1):
                        if needle in line.casefold():
                            hits.append({'path': relative, 'line': n, 'text': redact(line)[:240]})
                            if len(hits) > 20:
                                break
                except (OSError, ValueError):
                    if target.is_file():
                        raise
            return {key: hits[:20], 'truncated': len(hits) > 20}
        except (OSError, UnicodeError) as exc:
            return {'error': f'只读文件访问失败：{type(exc).__name__}'}
        except ValueError as exc:
            return {'error': str(exc)}
