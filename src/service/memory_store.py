"""Single-writer JSON snapshots. Archives are written before snapshot references."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import shutil
import tempfile

from ..domain.memory import MemoryState
from ..domain.memory.state import StorageError as MemoryError
from ..domain.memory.conversation import new_turn, message_record
from ..domain.memory.privacy import redact
from ..domain.memory.state import digest, now, uid


class JsonMemoryStore:
    def __init__(self, directory: Path):
        self.directory = Path(directory).expanduser().resolve()
        self.path = self.directory / "memory.json"
        self.lock = None
        self.poisoned = False

    def __enter__(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        self.lock = open(self._path(".writer.lock"), "a+")
        os.chmod(self.directory / ".writer.lock", 0o600)
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.lock.close()
            self.lock = None
            raise MemoryError("该项目记忆正在使用，请关闭另一个Agent进程") from exc
        return self

    def __exit__(self, *_args):
        if self.lock:
            fcntl.flock(self.lock, fcntl.LOCK_UN)
            self.lock.close()
            self.lock = None

    def _path(self, relative: str) -> Path:
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise MemoryError("记忆路径越界")
        path = self.directory / relative_path
        current = self.directory
        for part in relative_path.parts:
            current = current / part
            if current.is_symlink():
                raise MemoryError("记忆目录内不允许符号链接")
        if not path.resolve().is_relative_to(self.directory):
            raise MemoryError("记忆路径越界")
        return path

    def _write(self, path: Path, value) -> None:
        temporary = None
        replaced = False
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix=".memory-tmp-", dir=path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                os.chmod(temporary, 0o600)
                json.dump(value, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            replaced = True
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError as exc:
            if replaced:
                self.poisoned = True
                raise MemoryError("durability_uncertain：替换已完成但目录同步失败，请重启核查") from exc
            raise MemoryError(f"记忆写入失败：{type(exc).__name__}") from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _read(self, path: Path):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise MemoryError(f"无法读取记忆文件 {path.name}：{type(exc).__name__}") from exc

    def load(self) -> MemoryState:
        path = self._path("memory.json")
        if not path.exists():
            state = MemoryState()
            state.scope.update(id=digest(str(self.directory.parent))[:20], root=str(self.directory.parent))
            return state
        state = MemoryState.from_dict(self._read(path))
        for ref in state.archives:
            self.load_archived_turn(ref)
        return state

    def commit(self, state: MemoryState, expected_revision: int) -> None:
        if self.lock is None:
            raise MemoryError("写入前必须获得进程锁")
        if self.poisoned:
            raise MemoryError("记忆写入状态不确定，必须重启核查")
        current = self._read(self._path("memory.json")) if self.path.exists() else None
        if current is not None and (not isinstance(current, dict) or current.get("schema_version") != 1 or current.get("revision") != expected_revision):
            raise MemoryError("记忆revision冲突，拒绝覆盖")
        if state.revision != expected_revision + 1:
            raise MemoryError("新revision必须递增1")
        try:
            state.validate()
        except (ValueError, TypeError, KeyError) as exc:
            raise MemoryError("拒绝写入无效记忆状态") from exc
        self._write(self._path("memory.json"), state.to_dict())

    def archive(self, turn: dict, epoch: str) -> dict:
        relative = f"archive/{epoch}/{turn['id']}.json"
        path = self._path(relative)
        content = redact(turn)
        checksum = digest(content)
        if path.exists():
            if digest(self._read(path)) != checksum:
                raise MemoryError("同ID归档内容冲突")
        else:
            self._write(path, content)
        return {"turn_id": turn["id"], "path": relative, "hash": checksum, "archived_at": now(), "epoch": epoch}

    def load_archived_turn(self, ref: dict) -> dict:
        try:
            path = self._path(ref["path"])
            content = self._read(path)
            if digest(content) != ref["hash"] or content.get("id") != ref["turn_id"]:
                raise MemoryError("归档哈希或ID不匹配")
            return content
        except (KeyError, TypeError) as exc:
            raise MemoryError("归档引用无效") from exc

    def migrate(self, state: MemoryState) -> MemoryState:
        legacy = self._path("history.json")
        if self.path.exists() or not legacy.exists():
            return state
        history = self._read(legacy)
        if not isinstance(history, list) or any(not isinstance(m, dict) or m.get("role") not in {"system", "user", "assistant", "tool"} for m in history):
            raise MemoryError("旧历史格式错误，保留原文件")
        for m in history:
            if not isinstance(m.get("content", ""), str):
                raise MemoryError("旧历史消息内容无效")
        backup = self._path(f"migration/history-{digest(history)[:20]}.json")
        if not backup.exists():
            self._write(backup, redact(history))
            os.chmod(backup, 0o400)
        orphaned = []
        turn = None
        for index, raw in enumerate(history):
            if raw["role"] == "system":
                continue
            if raw["role"] == "user":
                turn = new_turn(redact(raw["content"]), state.next_sequence)
                state.next_sequence += 1
                turn.update(source_type="migrated", status="interrupted", ended_at=now())
                state.recent_turns.append(turn)
                record = turn["messages"][0]
            elif turn is None:
                orphaned.append({"index": index, "message": redact(raw)})
                continue
            else:
                try:
                    record = message_record(redact(raw))
                except ValueError as exc:
                    raise MemoryError("旧历史消息格式无效") from exc
                turn["messages"].append(record)
            record["legacy_index"] = index
            if raw["role"] == "assistant":
                if raw.get("tool_calls"):
                    turn.update(status="interrupted", final_message_id=None)
                    for call in raw["tool_calls"]:
                        if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
                            raise MemoryError("旧工具请求无效")
                        function = call["function"]
                        turn["tool_executions"].append({
                            "execution_id": uid("exec"), "message_id": record["id"], "name": function.get("name", "unknown"),
                            "arguments": redact(function.get("arguments", {})), "status": "unknown", "started_at": now(), "ended_at": None, "result_message_id": None,
                        })
                else:
                    turn.update(status="completed", final_message_id=record["id"])
            elif raw["role"] == "tool":
                pending = next((e for e in turn["tool_executions"] if e["result_message_id"] is None), None)
                if pending:
                    pending.update(result_message_id=record["id"], ended_at=now())
                    record["tool_call_id"] = pending["execution_id"]
                turn.update(status="interrupted", final_message_id=None)
        state.migration = {"source_hash": digest(history), "backup": str(backup.relative_to(self.directory)), "completed_at": now(), "orphaned": orphaned, "status_inferred": True}
        state.revision += 1
        self.commit(state, 0)
        # New state is authoritative. Preserve original only until a durable migration exists.
        try:
            legacy.unlink()
        except OSError:
            state.maintenance["legacy_cleanup_pending"] = True
        return state

    def orphan_count(self, state: MemoryState) -> int:
        referenced = {ref["path"] for ref in state.archives}
        root = self._path("archive")
        return sum(str(path.relative_to(self.directory)) not in referenced for path in root.rglob("*.json")) if root.exists() else 0

    def purge(self) -> list[str]:
        """Called only after a durable new empty epoch has been committed."""
        failed = []
        for name in ("archive", "migration", "history.json"):
            path = self.directory / name
            if not path.exists() and not path.is_symlink():
                continue
            try:
                if path.is_symlink() or path.is_file():
                    path.unlink()
                else:
                    shutil.rmtree(path)
            except OSError:
                failed.append(name)
        for path in self.directory.glob(".memory-tmp-*"):
            try:
                path.unlink()
            except OSError:
                failed.append(path.name)
        return failed
