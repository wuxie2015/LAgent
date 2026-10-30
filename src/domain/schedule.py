"""Pure time rules for persistent schedules.

All timestamps returned by this module are canonical UTC ISO-8601 strings.
Weekly weekdays use Python's convention: Monday is 0 and Sunday is 6.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _zone(name: str) -> ZoneInfo:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("timezone must be a non-empty IANA name")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown IANA timezone: {name}") from exc


def _parse(value: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("timestamp must be a non-empty ISO-8601 string")
    try:
        # datetime.fromisoformat on 3.10 does not accept the Z suffix.
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("timestamp must be ISO-8601") from exc


def utc_iso(value: datetime | str) -> str:
    dt = _parse(value) if isinstance(value, str) else value
    if not isinstance(dt, datetime) or dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _valid_local_candidates(local: datetime, zone: ZoneInfo) -> list[datetime]:
    candidates = []
    for fold in (0, 1):
        aware = local.replace(tzinfo=zone, fold=fold)
        roundtrip = aware.astimezone(timezone.utc).astimezone(zone)
        if roundtrip.replace(tzinfo=None) == local and roundtrip.fold == fold:
            if all(aware.utcoffset() != item.utcoffset() for item in candidates):
                candidates.append(aware)
    return candidates


def normalize_scheduled_time(value: str, timezone_name: str) -> str:
    """Resolve an ISO timestamp in a selected zone and return canonical UTC.

    Naive local times reject DST gaps and folds. An explicit offset resolves a
    fold; its resulting instant must still have that offset in the named zone.
    """
    zone = _zone(timezone_name)
    dt = _parse(value)
    if dt.tzinfo is not None and dt.utcoffset() is not None:
        local = dt.astimezone(zone)
        if local.utcoffset() != dt.utcoffset():
            raise ValueError("explicit offset does not match the selected timezone")
        return utc_iso(dt)
    candidates = _valid_local_candidates(dt, zone)
    if not candidates:
        raise ValueError("local time does not exist in the selected timezone")
    if len(candidates) > 1:
        raise ValueError("ambiguous local time requires an explicit UTC offset")
    return utc_iso(candidates[0])


def _repeat_value(repeat: str) -> str:
    if repeat not in {"once", "daily", "weekly"}:
        raise ValueError("repeat must be once, daily or weekly")
    return repeat


def _weekdays(values) -> tuple[int, ...]:
    if values is None:
        raise ValueError("weekly schedules require weekdays")
    if isinstance(values, (str, bytes)):
        raise ValueError("weekdays must be a sequence of integers from 0 to 6")
    try:
        result = tuple(values)
    except TypeError as exc:
        raise ValueError("weekdays must be a sequence of integers from 0 to 6") from exc
    if not result or any(type(day) is not int or day < 0 or day > 6 for day in result):
        raise ValueError("weekdays must contain integers from 0 to 6")
    if len(set(result)) != len(result):
        raise ValueError("weekdays must not contain duplicates")
    return tuple(sorted(result))


def validate_rule(repeat: str, weekdays=None) -> tuple[str, tuple[int, ...]]:
    if not isinstance(repeat, str):
        raise ValueError("repeat must be once, daily or weekly")
    repeat = _repeat_value(repeat)
    if repeat == "weekly":
        return repeat, _weekdays(weekdays)
    if weekdays not in (None, (), []):
        raise ValueError("weekdays are only valid for weekly schedules")
    return repeat, ()


def validate_recurrence(scheduled_at: str, timezone_name: str, repeat: str,
                        weekdays=None) -> tuple[str, tuple[int, ...]]:
    """Validate rule shape and that its first occurrence matches the rule."""
    repeat, days = validate_rule(repeat, weekdays)
    instant = _parse(scheduled_at)
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("scheduled_at must be timezone-aware")
    zone = _zone(timezone_name)
    if repeat == "weekly" and instant.astimezone(zone).weekday() not in days:
        raise ValueError("initial scheduled date must be included in weekly weekdays")
    return repeat, days


def next_occurrence(scheduled_at: str, timezone_name: str, repeat: str,
                    weekdays=None, after: str | datetime | None = None) -> str | None:
    """Return the next wall-clock occurrence strictly after ``after``.

    With no ``after`` argument, return the occurrence after the supplied one.
    DST gaps are skipped and DST folds select the earlier instant.
    """
    repeat, days = validate_recurrence(scheduled_at, timezone_name, repeat, weekdays)
    instant = _parse(scheduled_at)
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("scheduled_at must be timezone-aware")
    if repeat == "once":
        return None
    zone = _zone(timezone_name)
    initial_local = instant.astimezone(zone)
    threshold = (utc_iso(after) if isinstance(after, str) else utc_iso(after)) if after is not None else utc_iso(instant)
    threshold_dt = _parse(threshold)
    threshold_local = threshold_dt.astimezone(zone)
    local_date = max(initial_local.date(), threshold_local.date())
    wall = initial_local.timetz().replace(tzinfo=None)
    for offset in range(0, 15):
        candidate_date = local_date + timedelta(days=offset)
        if repeat == "weekly" and candidate_date.weekday() not in days:
            continue
        naive = datetime.combine(candidate_date, wall)
        candidates = _valid_local_candidates(naive, zone)
        if not candidates:
            continue
        # A fold's first instant is deterministic, preventing duplicate wall times.
        candidate = min(candidates, key=lambda item: item.astimezone(timezone.utc))
        if candidate.astimezone(timezone.utc) > threshold_dt.astimezone(timezone.utc):
            return utc_iso(candidate)
    raise ValueError("could not calculate next occurrence")
