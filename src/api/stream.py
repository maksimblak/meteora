"""Server-sent status events for open operator pages (spec 8.1).

Each event is ``event: status`` with a ``StatusPulse`` JSON body. A connection is
recycled after ``stream_max_events`` events; ``EventSource`` reconnects by itself.
"""

import asyncio
from collections.abc import AsyncIterator
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


def status_stream(ctx: OperatorContext, request: Request) -> StreamingResponse:
    settings = ctx.settings

    async def events() -> AsyncIterator[str]:
        sent = 0
        yield f"retry: {_RETRY_MILLISECONDS}\n\n"
        while settings.stream_max_events is None or sent < settings.stream_max_events:
            if await request.is_disconnected():
                return
            pulse = await status_pulse(ctx)
            sent += 1
            yield f"event: status\ndata: {pulse.model_dump_json()}\n\n"
            if settings.stream_max_events is not None and sent >= settings.stream_max_events:
                return
            await asyncio.sleep(settings.stream_interval.total_seconds())

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
