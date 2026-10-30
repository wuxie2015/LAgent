"""SQLite persistence for schedules and their execution/notification history.

Create with ``ScheduleStore(path)``. Timestamps in returned dictionaries are
UTC ISO strings (``...+00:00``); task ``next_run_at`` is the next due instant,
and run ``scheduled_for`` is the occurrence snapshot. Both task and run IDs are
available as ``id``; runs also include ``task_id``. Connections are scoped to
one operation and claims use an immediate SQLite transaction.
"""

import hashlib
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from src.domain.schedule import next_occurrence, normalize_scheduled_time, utc_iso, validate_recurrence


_TASK_FIELDS = {"kind", "content", "channel", "scheduled_at", "timezone_name",
                "repeat", "weekdays", "grace_seconds"}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _now(value=None):
    if value is None:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")
    return utc_iso(value)


def _strict_text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _strict_grace(value):
    if type(value) is not int or value < 0 or value > 31_536_000:
        raise ValueError("grace_seconds must be an integer between 0 and 31536000")
    return value


class ScheduleStore:
    def __init__(self, path):
        if not isinstance(path, (str, os.PathLike)):
            raise ValueError("path must be a string or path-like value")
        self.path = os.fspath(path)
        if not self.path:
            raise ValueError("path must be a non-empty string")
        with self._connection() as conn:
            existing = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            if existing and not {"tasks", "runs"}.issubset(existing):
                raise RuntimeError("schedule database has an incomplete or unsupported schema")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    content TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    scheduled_at TEXT NOT NULL,
                    next_run_at TEXT,
                    timezone_name TEXT NOT NULL,
                    repeat TEXT NOT NULL CHECK(repeat IN ('once','daily','weekly')),
                    weekdays TEXT NOT NULL,
                    grace_seconds INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('active','paused','cancelled','completed')),
                    request_key TEXT UNIQUE,
                    request_fingerprint TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS tasks_due_idx ON tasks(status, next_run_at);
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(id),
                    scheduled_for TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    status TEXT NOT NULL CHECK(status IN ('executing','pending_notification','delivered','failed','missed','unknown')),
                    kind TEXT NOT NULL,
                    content TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    result TEXT,
                    error TEXT,
                    notification_attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT,
                    UNIQUE(task_id, scheduled_for)
                );
                CREATE INDEX IF NOT EXISTS runs_notification_idx ON runs(status, next_attempt_at);
            """)

    @contextmanager
    def _connection(self):
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        try:
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _task(row):
        if row is None:
            return None
        result = dict(row)
        result["weekdays"] = json.loads(result["weekdays"])
        return result

    @staticmethod
    def _run(row):
        return dict(row) if row is not None else None

    def create(self, *, kind, content, channel, scheduled_at, timezone_name,
               repeat="once", weekdays=None, grace_seconds=300, request_key=None):
        kind = _strict_text(kind, "kind")
        if kind not in {"reminder", "agent"}:
            raise ValueError("kind must be reminder or agent")
        content = _strict_text(content, "content")
        channel = _strict_text(channel, "channel")
        timezone_name = _strict_text(timezone_name, "timezone_name")
        scheduled = normalize_scheduled_time(scheduled_at, timezone_name)
        repeat, weekdays_tuple = validate_recurrence(scheduled, timezone_name, repeat, weekdays)
        grace_seconds = _strict_grace(grace_seconds)
        request_key = _strict_text(request_key, "request_key") if request_key is not None else None
        payload = {"kind": kind, "content": content, "channel": channel,
                   "scheduled_at": scheduled, "timezone_name": timezone_name,
                   "repeat": repeat, "weekdays": list(weekdays_tuple), "grace_seconds": grace_seconds}
        fingerprint = hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()
        now = _now()
        task_id = str(uuid.uuid4())
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if request_key is not None:
                existing = conn.execute("SELECT * FROM tasks WHERE request_key=?", (request_key,)).fetchone()
                if existing:
                    conn.commit()
                    if existing["request_fingerprint"] != fingerprint:
                        raise ValueError("request_key already exists with a different schedule payload")
                    return self._task(existing)
            conn.execute("""INSERT INTO tasks
                (id,kind,content,channel,scheduled_at,next_run_at,timezone_name,repeat,weekdays,
                 grace_seconds,status,request_key,request_fingerprint,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?, 'active',?,?,?,?)""",
                (task_id, kind, content, channel, scheduled, scheduled, timezone_name, repeat,
                 _json(list(weekdays_tuple)), grace_seconds, request_key, fingerprint, now, now))
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            conn.commit()
        return self._task(row)

    def list(self):
        with self._connection() as conn:
            return [self._task(r) for r in conn.execute("SELECT * FROM tasks ORDER BY created_at,id")]

    def get(self, task_id):
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (_strict_text(task_id, "id"),)).fetchone()
        if row is None:
            raise KeyError(task_id)
        return self._task(row)

    def update(self, task_id, **fields):
        if not fields or set(fields) - _TASK_FIELDS:
            raise ValueError("update requires supported schedule fields")
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                conn.rollback()
                raise KeyError(task_id)
            if row["status"] != "active":
                conn.rollback()
                raise ValueError("only active schedules can be updated")
            current = dict(row)
            current["weekdays"] = json.loads(current["weekdays"])
            values = {key: current[key] for key in _TASK_FIELDS}
            values.update(fields)
            timing_changed = bool({"scheduled_at", "timezone_name", "repeat", "weekdays"} & set(fields))
            # A timezone-only change preserves the stored instant while changing
            # how future wall-clock recurrence is interpreted.
            if "scheduled_at" in fields or "timezone_name" in fields:
                candidate = values["scheduled_at"] if "scheduled_at" in fields else current["scheduled_at"]
                if "timezone_name" in fields and "scheduled_at" not in fields:
                    candidate = datetime.fromisoformat(candidate).astimezone(
                        ZoneInfo(values["timezone_name"])
                    ).isoformat(timespec="seconds")
                values["scheduled_at"] = normalize_scheduled_time(candidate, values["timezone_name"])
            values["kind"] = _strict_text(values["kind"], "kind")
            if values["kind"] not in {"reminder", "agent"}:
                conn.rollback()
                raise ValueError("kind must be reminder or agent")
            values["content"] = _strict_text(values["content"], "content")
            values["channel"] = _strict_text(values["channel"], "channel")
            values["timezone_name"] = _strict_text(values["timezone_name"], "timezone_name")
            values["repeat"], day_values = validate_recurrence(
                values["scheduled_at"], values["timezone_name"], values["repeat"], values["weekdays"]
            )
            values["weekdays"] = _json(list(day_values))
            values["grace_seconds"] = _strict_grace(values["grace_seconds"])
            now = _now()
            next_at = row["next_run_at"]
            if timing_changed:
                latest = conn.execute("SELECT MAX(scheduled_for) FROM runs WHERE task_id=?", (task_id,)).fetchone()[0]
                next_at = values["scheduled_at"]
                if values["repeat"] == "once":
                    if latest and next_at <= latest:
                        raise ValueError("one-time update would repeat an already-recorded occurrence")
                else:
                    after = datetime.fromisoformat(now)
                    if latest:
                        after = max(after, datetime.fromisoformat(latest))
                    if datetime.fromisoformat(next_at) <= after:
                        next_at = next_occurrence(values["scheduled_at"], values["timezone_name"],
                                                  values["repeat"], day_values, after=after)
            conn.execute("""UPDATE tasks SET kind=?,content=?,channel=?,scheduled_at=?,next_run_at=?,
                timezone_name=?,repeat=?,weekdays=?,grace_seconds=?,updated_at=? WHERE id=?""",
                (values["kind"], values["content"], values["channel"], values["scheduled_at"],
                 next_at, values["timezone_name"], values["repeat"], values["weekdays"],
                 values["grace_seconds"], now, task_id))
            conn.commit()
        return self.get(task_id)

    def _set_status(self, task_id, status, allowed):
        now = _now()
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                conn.rollback()
                raise KeyError(task_id)
            if row["status"] not in allowed:
                conn.rollback()
                raise ValueError(f"cannot set {row['status']} schedule to {status}")
            conn.execute("UPDATE tasks SET status=?,updated_at=? WHERE id=?", (status, now, task_id))
            if status == "cancelled":
                conn.execute("UPDATE runs SET status='failed',finished_at=?,error='cancelled' WHERE task_id=? AND status='pending_notification'",
                             (now, task_id))
            conn.commit()
        return self.get(task_id)

    def pause(self, task_id):
        return self._set_status(task_id, "paused", {"active", "completed"})

    def resume(self, task_id):
        return self._set_status(task_id, "active", {"paused"})

    def cancel(self, task_id):
        return self._set_status(task_id, "cancelled", {"active", "paused", "completed"})

    def history(self, task_id=None):
        with self._connection() as conn:
            if task_id is None:
                rows = conn.execute("SELECT * FROM runs ORDER BY scheduled_for,id")
            else:
                rows = conn.execute("SELECT * FROM runs WHERE task_id=? ORDER BY scheduled_for,id", (_strict_text(task_id, "id"),))
            return [self._run(r) for r in rows]

    def claim_due(self, now, grace_seconds=None):
        now = _now(now)
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            task = conn.execute("SELECT * FROM tasks WHERE status='active' AND next_run_at IS NOT NULL AND next_run_at<=? ORDER BY next_run_at,id LIMIT 1", (now,)).fetchone()
            if task is None:
                conn.commit()
                return None
            scheduled = task["next_run_at"]
            grace = task["grace_seconds"] if grace_seconds is None else _strict_grace(grace_seconds)
            missed = datetime.fromisoformat(now) - datetime.fromisoformat(scheduled) > timedelta(seconds=grace)
            status = "missed" if missed else "executing"
            run_id = str(uuid.uuid4())
            conn.execute("""INSERT INTO runs (id,task_id,scheduled_for,started_at,finished_at,status,kind,content,channel)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (run_id, task["id"], scheduled, None if missed else now, now if missed else None,
                 status, task["kind"], task["content"], task["channel"]))
            if task["repeat"] == "once":
                conn.execute("UPDATE tasks SET next_run_at=NULL,status='completed',updated_at=? WHERE id=?", (now, task["id"]))
            else:
                next_at = next_occurrence(scheduled, task["timezone_name"], task["repeat"],
                                          json.loads(task["weekdays"]), after=now)
                conn.execute("UPDATE tasks SET next_run_at=?,updated_at=? WHERE id=?", (next_at, now, task["id"]))
            conn.commit()
            row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return self._run(row)

    def save_result(self, run_id, result, now=None):
        if not isinstance(result, str):
            raise ValueError("result must be a string")
        now = _now(now)
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT status FROM runs WHERE id=?", (_strict_text(run_id, "run_id"),)).fetchone()
            if row is None:
                conn.rollback()
                raise KeyError(run_id)
            if row["status"] != "executing":
                conn.rollback()
                raise ValueError("only executing runs can save results")
            conn.execute("UPDATE runs SET result=?,finished_at=?,status='pending_notification',next_attempt_at=? WHERE id=?",
                         (result, now, now, run_id))
            conn.commit()
        return next((r for r in self.history() if r["id"] == run_id), None)

    def notification_due(self, now):
        now = _now(now)
        with self._connection() as conn:
            return [self._run(r) for r in conn.execute("""SELECT runs.* FROM runs JOIN tasks ON tasks.id=runs.task_id
                WHERE runs.status='pending_notification' AND runs.next_attempt_at<=?
                AND tasks.status IN ('active','completed') ORDER BY runs.next_attempt_at,runs.id""", (now,))]

    def mark_delivered(self, run_id):
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT tasks.status,runs.status,runs.error FROM runs JOIN tasks ON tasks.id=runs.task_id WHERE runs.id=?", (run_id,)).fetchone()
            if row is None:
                conn.rollback()
                raise KeyError(run_id)
            # A delivery can finish after the task is paused or cancelled.
            # Record that external side effect to prevent a duplicate on resume.
            cancelled_run = row[1] == "failed" and row[2] == "cancelled"
            if row[1] != "pending_notification" and not cancelled_run:
                conn.rollback()
                raise ValueError("run has no pending notification")
            conn.execute("UPDATE runs SET status='delivered',next_attempt_at=NULL,error=NULL WHERE id=?", (run_id,))
            conn.commit()
        return next(r for r in self.history() if r["id"] == run_id)

    def notification_failed(self, run_id, now=None, error=None, max_attempts=5):
        if type(max_attempts) is not int or max_attempts < 1:
            raise ValueError("max_attempts must be a positive integer")
        now = _now(now)
        # Persist only a generic error marker; callers must not pass secrets or
        # raw transport exceptions into durable history.
        safe_error = "notification failed" if error else None
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT notification_attempts,status,error FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                conn.rollback()
                raise KeyError(run_id)
            # Cancellation already made this run terminal. An in-flight
            # transport failure must not turn it into a retry or a fatal error.
            if row["status"] == "failed" and row["error"] == "cancelled":
                conn.commit()
                return next(r for r in self.history() if r["id"] == run_id)
            if row["status"] != "pending_notification":
                conn.rollback()
                raise ValueError("run has no pending notification")
            attempts = row["notification_attempts"] + 1
            if attempts >= max_attempts:
                conn.execute("UPDATE runs SET status='failed',notification_attempts=?,next_attempt_at=NULL,error=? WHERE id=?",
                             (attempts, safe_error, run_id))
            else:
                delay = min(3600, 30 * (2 ** (attempts - 1)))
                due = (datetime.fromisoformat(now) + timedelta(seconds=delay)).isoformat(timespec="seconds")
                conn.execute("UPDATE runs SET notification_attempts=?,next_attempt_at=?,error=? WHERE id=?",
                             (attempts, due, safe_error, run_id))
            conn.commit()
        return next(r for r in self.history() if r["id"] == run_id)

    def mark_failed(self, run_id, now=None, error=None):
        now = _now(now)
        safe_error = "execution failed" if error else None
        with self._connection() as conn:
            cursor = conn.execute("UPDATE runs SET status='failed',finished_at=?,error=? WHERE id=? AND status='executing'",
                                  (now, safe_error, run_id))
            if cursor.rowcount != 1:
                exists = conn.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone()
                if not exists:
                    raise KeyError(run_id)
                raise ValueError("only executing runs can fail")
        return next(r for r in self.history() if r["id"] == run_id)

    def recover_interrupted(self):
        now = _now()
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute("UPDATE runs SET status='unknown',finished_at=?,error='interrupted' WHERE status='executing'", (now,))
            conn.commit()
            return cursor.rowcount
