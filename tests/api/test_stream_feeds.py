"""One SSE stream per page: each feed goes on its own beat over the same connection."""

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest

from src.api.stream import Feed, event_stream
from tests.api.support import operator_settings


@dataclass
class _Context:
    settings: Any


class _Request:
    def __init__(self, disconnect_after: int | None = None) -> None:
        self.checks = 0
        self.disconnect_after = disconnect_after

    async def is_disconnected(self) -> bool:
        self.checks += 1
        return self.disconnect_after is not None and self.checks > self.disconnect_after


def _feed(name: str, every: int = 1) -> Feed:
    async def payload() -> str:
        return name

    return Feed(name, payload, every)


def _events(ticks: int | None, feeds: list[Feed], request: _Request) -> list[str]:
    settings = operator_settings(stream_max_events=ticks, stream_interval=timedelta(0))
    response = event_stream(_Context(settings), request, feeds)  # type: ignore[arg-type]

    async def collect() -> list[str]:
        return [
            chunk if isinstance(chunk, str) else bytes(chunk).decode()
            async for chunk in response.body_iterator
        ]

    chunks = asyncio.run(collect())
    return [
        line.removeprefix("event: ")
        for chunk in chunks
        for line in chunk.splitlines()
        if line.startswith("event: ")
    ]


def test_each_feed_goes_on_its_own_tick_and_all_go_on_the_first() -> None:
    events = _events(4, [_feed("status"), _feed("overview", every=3)], _Request())

    # Ticks 0..3: both at once, then the pulse alone, and the slow feed again on tick 3.
    assert events == ["status", "overview", "status", "status", "status", "overview"]


def test_the_stream_is_recycled_after_the_tick_limit_not_the_event_count() -> None:
    events = _events(1, [_feed("status"), _feed("overview", every=3)], _Request())

    assert events == ["status", "overview"]


def test_a_departed_client_ends_the_stream() -> None:
    events = _events(None, [_feed("status")], _Request(disconnect_after=2))

    assert events == ["status", "status"]


def test_a_feed_is_due_at_least_once_per_tick() -> None:
    with pytest.raises(ValueError, match="at least once"):
        _feed("never", every=0)
