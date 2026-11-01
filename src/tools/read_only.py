"""Bounded text access inside a project; no command execution or writes."""
import fnmatch
import os
from pathlib import Path
import stat

from ..domain.memory.privacy import redact


class ProjectFilesTool:
    name = 'project_files'
    definition = {'type': 'function', 'function': {
        'name': name, 'description': '只读项目文本：read 按行读取；search 搜索文件名和文本。返回行号，禁止越界与敏感路径。',
        'parameters': {'type': 'object', 'properties': {
            'action': {'type': 'string', 'enum': ['read', 'search']},
            'path': {'type': 'string', 'description': '项目内相对路径，默认项目根目录'},
            'query': {'type': 'string'}, 'start_line': {'type': 'integer'},
            'line_count': {'type': 'integer'}}, 'required': ['action']}}}
    denied = {'.git', '.aws', '.ssh', '.codex', '.agents', '.idea', '.venv', 'venv',
              'node_modules', '__pycache__', 'subagents'}

    def __init__(self, root, blocked_paths=()):
        self.root = Path(root).resolve()
        self.blocked_paths = tuple(Path(p).resolve() for p in blocked_paths)

    def _path(self, path):
        candidate = self.root / path
        resolved = candidate.resolve()
        if any(resolved == blocked or blocked in resolved.parents for blocked in self.blocked_paths):
            raise ValueError("禁止读取主 Agent 记忆路径")
        relative = resolved.relative_to(self.root)
        # Check lexical and resolved components to reject secret aliases and escapes.
        for parts in (candidate.relative_to(self.root).parts, relative.parts):
            if parts and parts[0] == 'memory':
                raise ValueError('禁止读取主 Agent 运行时记忆')
            for part in parts:
                if part in self.denied or part.startswith('.env') or any(
                    fnmatch.fnmatch(part.lower(), pattern)
                    for pattern in ('*.pem', '*.key', '*credentials*', '*secret*', '*.sqlite*')
                ):
                    raise ValueError('禁止读取敏感或内部路径')
        return candidate.resolve()

    def _lines(self, path):
        # O_NOFOLLOW avoids following a final symlink swapped after path validation.
        relative = Path(path).relative_to(self.root)
        directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in relative.parts[:-1]:
                next_directory = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                os.close(directory)
                directory = next_directory
            fd = os.open(relative.parts[-1] if relative.parts else '.',
                         os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory)
        finally:
            os.close(directory)
        with os.fdopen(fd, 'r', encoding='utf-8') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 1_000_000:
                raise ValueError('只支持不超过1MB的普通文本文件')
            text = stream.read(1_000_001)
        if '\x00' in text:
            raise ValueError('不支持二进制文件')
        return text.splitlines()

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
            if action != 'search' or not isinstance(query, str) or not query.strip() or len(query) > 200:
                raise ValueError('search需要1–200字符的query')
            if not target.is_dir():
                raise ValueError('search的path必须是目录')
            hits, scanned = [], 0
            for directory, dirs, files in os.walk(target, followlinks=False):
                dirs[:] = sorted(d for d in dirs if d not in self.denied and not Path(directory, d).is_symlink())
                for name in sorted(files):
                    scanned += 1
                    if scanned > 1000:
                        return {'matches': hits, 'truncated': True}
                    try:
                        file = self._path(str(Path(directory, name).relative_to(self.root)))
                        lines = self._lines(file)
                    except (OSError, ValueError, UnicodeError):
                        continue
                    relative = str(file.relative_to(self.root))
                    if query.casefold() in relative.casefold():
                        hits.append({'path': relative, 'line': 1, 'text': '[文件名匹配]'})
                        if len(hits) >= 20:
                            return {'matches': hits[:20], 'truncated': True}
                    for n, line in enumerate(lines, 1):
                        if query.casefold() in line.casefold():
                            hits.append({'path': relative, 'line': n, 'text': redact(line[:240])})
                        if len(hits) >= 20:
                            return {'matches': hits[:20], 'truncated': True}
            return {'matches': hits, 'truncated': False}
        except (OSError, ValueError, UnicodeError) as exc:
            return {'error': f'只读文件访问失败：{type(exc).__name__}'}
