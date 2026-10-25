"""Server-sent status events for open operator pages (spec 8.1).

Each event is ``event: status`` with a ``StatusPulse`` JSON body. A connection is
recycled after ``stream_max_events`` ticks; ``EventSource`` reconnects by itself. The
panel's overview stream sends the same status feed plus its own ``event: overview``
(``src/web/routes.py``) over the one connection.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from fastapi import Request
from fastapi.responses import StreamingResponse

from src.api import metrics, presenters
from src.api.context import OperatorContext
from src.api.schemas import StatusPulse

_RETRY_MILLISECONDS = 5000


async def status_pulse(ctx: OperatorContext) -> StatusPulse:
    state = await ctx.mode_state()
    alerts = await ctx.sync(ctx.repositories.mode.open_alert_count)
    pending = await ctx.pending_approvals()
    last_sync = await ctx.repositories.history.latest_sync_at()
    now = ctx.clock.now()
    universe: Literal["NOT_SYNCED", "STALE", "FRESH"]
    if last_sync is None:
        universe = "NOT_SYNCED"
    elif now - last_sync > ctx.settings.maximum_sync_age:
        universe = "STALE"
    else:
        universe = "FRESH"
    metrics.UNIVERSE_AGE_SECONDS.set(
        -1 if last_sync is None else max((now - last_sync).total_seconds(), 0)
    )
    metrics.APPROVALS_PENDING.set(len(pending))
    metrics.OPEN_CRITICAL_ALERTS.set(alerts)
    return StatusPulse(
        generated_at=now,
        mode=presenters.mode_view(state),
        open_alerts=alerts,
        approvals_pending=len(pending),
        universe_state=universe,
        last_sync_at=last_sync,
    )


@dataclass(frozen=True, slots=True)
class Feed:
    """One kind of event in a stream: ``payload()`` (one line) on every ``every``-th tick."""

    event: str
    payload: Callable[[], Awaitable[str]]
    every: int = 1

    def __post_init__(self) -> None:
        if self.every < 1:
            raise ValueError("a feed is sent at least once per tick")


def status_feed(ctx: OperatorContext) -> Feed:
    async def pulse() -> str:
        return (await status_pulse(ctx)).model_dump_json()

    return Feed("status", pulse)


def status_stream(ctx: OperatorContext, request: Request) -> StreamingResponse:
    return event_stream(ctx, request, [status_feed(ctx)])


def event_stream(
    ctx: OperatorContext, request: Request, feeds: Sequence[Feed]
) -> StreamingResponse:
    """A page's single stream: every ``stream_interval`` tick sends each feed that is due.

    One ``EventSource`` per page: over HTTP/1.1 a browser keeps at most six connections
    per host, so a page that opened a stream per feed would starve its own other tabs.
    """

    settings = ctx.settings

    async def events() -> AsyncIterator[str]:
        tick = 0
        yield f"retry: {_RETRY_MILLISECONDS}\n\n"
        while settings.stream_max_events is None or tick < settings.stream_max_events:
            if await request.is_disconnected():
                return
            for feed in feeds:
                if tick % feed.every == 0:
                    yield f"event: {feed.event}\ndata: {await feed.payload()}\n\n"
            tick += 1
            if settings.stream_max_events is not None and tick >= settings.stream_max_events:
                return
            await asyncio.sleep(settings.stream_interval.total_seconds())

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
