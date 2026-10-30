from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from ..domain.schedule import normalize_scheduled_time, utc_iso, validate_recurrence


class ScheduleTool:
    name = "schedule"
    definition = {
        "type": "function",
        "function": {
            "name": "schedule",
            "description": "Create and manage durable reminders or read-only scheduled agent tasks.",
            "parameters": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "action": {"type": "string", "enum": ["now", "create", "list", "get", "update", "pause", "resume", "cancel", "history"]},
                    "task_id": {"type": "string"},
                    "kind": {"type": "string", "enum": ["reminder", "agent"]},
                    "content": {"type": "string"},
                    "scheduled_at": {"type": "string", "description": "ISO-8601 local time or offset timestamp"},
                    "timezone_name": {"type": "string"},
                    "repeat": {"type": "string", "enum": ["once", "daily", "weekly"]},
                    "weekdays": {"type": "array", "items": {"type": "integer", "minimum": 0, "maximum": 6}},
                    "channel": {"type": "string"},
                    "grace_seconds": {"type": "integer", "minimum": 0, "maximum": 31536000},
                }, "required": ["action"]
            }
        }
    }

    _FIELDS = {"kind", "content", "scheduled_at", "timezone_name", "repeat", "weekdays", "channel", "grace_seconds"}

    def __init__(self, store, dispatcher, *, timezone_name="Asia/Tokyo", default_channel="console",
                 grace_seconds=300, clock=None, writes_available=None):
        ZoneInfo(timezone_name)
        self.store = store
        self.dispatcher = dispatcher
        self.timezone_name = timezone_name
        self.default_channel = default_channel
        self.grace_seconds = grace_seconds
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.writes_available = writes_available or (lambda: True)
        self._turn_id = str(uuid.uuid4())
        self._turn_lock = threading.Lock()

    def begin_request(self) -> None:
        """Set the idempotency scope for one user turn, before Agent.ask()."""
        with self._turn_lock:
            self._turn_id = str(uuid.uuid4())

    def run(self, action: str, task_id: str | None = None, kind: str | None = None,
            content: str | None = None, scheduled_at: str | None = None,
            timezone_name: str | None = None, repeat: str | None = None,
            weekdays: list[int] | None = None, channel: str | None = None,
            grace_seconds: int | None = None) -> dict:
        if not isinstance(action, str) or action not in {"now", "create", "list", "get", "update", "pause", "resume", "cancel", "history"}:
            return self._error("action must be now, create, list, get, update, pause, resume, cancel or history")
        try:
            if action == "now":
                now = self.clock().astimezone(ZoneInfo(self.timezone_name))
                return {"ok": True, "now": now.isoformat(timespec="seconds"), "timezone_name": self.timezone_name,
                        "default_channel": self.default_channel, "available_channels": self.dispatcher.available_channels()}
            if action == "list":
                return {"ok": True, "tasks": self.store.list()}
            if action == "history":
                return {"ok": True, "history": self.store.history(task_id)}
            if action == "get":
                return {"ok": True, "task": self.store.get(self._required(task_id, "task_id"))}
            if action in {"pause", "resume", "cancel"}:
                ident = self._required(task_id, "task_id")
                result = getattr(self.store, action)(ident)
                return {"ok": True, "task": result}
            if action == "create":
                if not self.writes_available():
                    return self._error("scheduler worker is unavailable; new schedules cannot be accepted")
                fields = self._create_fields(kind, content, scheduled_at, timezone_name, repeat, weekdays, channel, grace_seconds)
                self._validate_channel(fields["channel"])
                normalized = dict(fields)
                normalized["kind"] = fields["kind"].strip()
                normalized["content"] = fields["content"].strip()
                normalized["channel"] = fields["channel"].strip()
                normalized["timezone_name"] = fields["timezone_name"].strip()
                normalized["scheduled_at"] = normalize_scheduled_time(fields["scheduled_at"], normalized["timezone_name"])
                normalized["repeat"], weekdays_tuple = validate_recurrence(
                    normalized["scheduled_at"], normalized["timezone_name"],
                    fields["repeat"], fields["weekdays"],
                )
                normalized["weekdays"] = list(weekdays_tuple)
                fingerprint = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                with self._turn_lock:
                    request_key = hashlib.sha256((self._turn_id + fingerprint).encode()).hexdigest()
                task = self.store.create(**fields, request_key=request_key)
                local = datetime.fromisoformat(task["scheduled_at"]).astimezone(ZoneInfo(task["timezone_name"]))
                return {"ok": True, "created": True, "task_id": task["id"], "scheduled_at": task["scheduled_at"],
                        "local_time": local.isoformat(timespec="seconds"), "timezone_name": task["timezone_name"],
                        "repeat": task["repeat"], "channel": task["channel"], "task": task}
            if action == "update":
                if not self.writes_available():
                    return self._error("scheduler worker is unavailable; schedules cannot be updated")
                ident = self._required(task_id, "task_id")
                fields = {key: value for key, value in {
                    "kind": kind, "content": content, "scheduled_at": scheduled_at,
                    "timezone_name": timezone_name, "repeat": repeat, "weekdays": weekdays,
                    "channel": channel, "grace_seconds": grace_seconds,
                }.items() if value is not None}
                if not fields:
                    return self._error("update requires at least one field")
                if "channel" in fields:
                    self._validate_channel(fields["channel"])
                existing = self.store.get(ident)
                self._validate_channel(fields.get("channel", existing["channel"]))
                if existing["status"] != "active":
                    return self._error("only active schedules can be updated; resume a paused task before editing")
                if "scheduled_at" in fields:
                    tz = fields.get("timezone_name", existing["timezone_name"])
                    instant = normalize_scheduled_time(fields["scheduled_at"], tz)
                    if datetime.fromisoformat(instant) <= self.clock().astimezone(timezone.utc):
                        return self._error("scheduled_at must be in the future")
                    fields["scheduled_at"] = datetime.fromisoformat(instant).astimezone(ZoneInfo(tz)).isoformat(timespec="seconds")
                task = self.store.update(ident, **fields)
                return {"ok": True, "task": task}
        except (ValueError, KeyError, OSError, sqlite3.Error) as exc:
            return self._error(str(exc))
        return self._error("unsupported action")

    def _create_fields(self, kind, content, scheduled_at, timezone_name, repeat, weekdays, channel, grace_seconds):
        if kind is None or content is None or scheduled_at is None:
            raise ValueError("create requires kind, content, and scheduled_at")
        if not isinstance(kind, str) or kind.strip() not in {"reminder", "agent"}:
            raise ValueError("kind must be reminder or agent")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("content is required")
        zone = timezone_name or self.timezone_name
        if not isinstance(zone, str) or not zone.strip():
            raise ValueError("timezone_name is required")
        zone = zone.strip()
        normalized = normalize_scheduled_time(scheduled_at, zone)
        if datetime.fromisoformat(normalized) <= self.clock().astimezone(timezone.utc):
            raise ValueError("scheduled_at must be in the future")
        repeat_value = repeat or "once"
        validate_recurrence(normalized, zone, repeat_value, weekdays)
        selected_channel = channel or self.default_channel
        if not isinstance(selected_channel, str) or not selected_channel.strip():
            raise ValueError("channel is required")
        grace = self.grace_seconds if grace_seconds is None else grace_seconds
        if type(grace) is not int or not 0 <= grace <= 31_536_000:
            raise ValueError("grace_seconds must be an integer between 0 and 31536000")
        return {"kind": kind, "content": content, "scheduled_at": scheduled_at,
                "timezone_name": zone, "repeat": repeat_value, "weekdays": weekdays,
                "channel": selected_channel,
                "grace_seconds": grace}

    def _validate_channel(self, channel):
        if channel not in self.dispatcher.available_channels():
            raise ValueError(f"notification channel {channel!r} is not configured; available: {', '.join(self.dispatcher.available_channels()) or 'none'}")

    @staticmethod
    def _required(value, name):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} is required")
        return value.strip()

    @staticmethod
    def _error(message):
        return {"ok": False, "error": message}
