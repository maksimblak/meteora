"""The manual smoke command, exercised offline on the recorded Data API responses."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import httpx

from src.core.clock import FrozenClock
from src.providers.meteora_smoke import format_summary, run_smoke
from src.providers.resilience import IntervalRateLimiter, ResilientHttpClient

FIXTURES = Path(__file__).parent / "fixtures" / "meteora_live"
NOW = datetime(2026, 10, 23, 14, 0, tzinfo=UTC)


def _client(pages: dict[int, str]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        body = (FIXTURES / pages[int(request.url.params["page"])]).read_bytes()
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    return httpx.AsyncClient(
        base_url="https://dlmm.datapi.meteora.ag", transport=httpx.MockTransport(handler)
    )


def test_smoke_pass_reports_pools_rejections_and_reasons() -> None:
    async def exercise() -> None:
        async with (
            _client({1: "pools_page1.json", 2: "pools_page2.json"}) as sweep_http,
            _client({1: "pools_created_at_zero.json"}) as oldest_http,
        ):
            whole = await run_smoke(
                ResilientHttpClient(sweep_http),
                clock=FrozenClock(NOW),
                page_size=4,
                filter_by="tvl>=6000000",
                rate_limiter=IntervalRateLimiter(1_000_000),
            )
            limited = await run_smoke(
                ResilientHttpClient(oldest_http),
                clock=FrozenClock(NOW),
                page_size=2,
                max_pages=1,
                rate_limiter=IntervalRateLimiter(1_000_000),
            )

        assert (whole.status, whole.pages, whole.listed, whole.accepted) == ("COMPLETED", 2, 6, 6)
        assert whole.reported_total == 6 and whole.snapshots == 6
        assert whole.short_window_sources == {"30m": 6}
        assert whole.with_decimals == 6 and whole.with_active_bin == 0
        assert (limited.listed, limited.accepted, limited.snapshots) == (2, 0, 0)
        assert limited.rejected == {"created_at_unknown": 2}
        text = format_summary(limited)
        assert "отклонено адаптером: 2" in text
        assert "created_at_unknown: 2" in text
        assert "ограничен 1 стр." in text

    asyncio.run(exercise())
