"""Injectable UTC clock used by time-sensitive domain and persistence code."""

from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FrozenClock:
    def __init__(self, instant: datetime) -> None:
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("frozen clock instant must be timezone-aware")
        self._instant = instant.astimezone(UTC)

    def now(self) -> datetime:
        return self._instant

    def advance(self, delta: timedelta) -> None:
        self._instant += delta
