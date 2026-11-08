from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any


def utcnow() -> datetime:
    return datetime.now(UTC)


def utcnow_iso() -> str:
    return to_iso(utcnow())


def to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).replace(microsecond=0).isoformat()


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def days_ago(days: float, now: datetime | None = None) -> str:
    base = now or utcnow()
    return to_iso(base - timedelta(days=days))


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=False)


def loads(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default
