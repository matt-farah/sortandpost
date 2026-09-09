"""A single injectable clock so every sync timestamp is testable.

``Clock`` is just ``Callable[[], datetime]`` returning an aware UTC value.
Production code uses :data:`utcnow`; tests pass a fake that advances on demand.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

Clock = Callable[[], datetime]


def utcnow() -> datetime:
    return datetime.now(UTC)


def isoformat(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def parse(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
