"""T22: RugCheck's summary report - danger risks apart, reports held, never an error.

The recorded answers are the live ones of 2026-10-25: a clean memecoin (Chubby), one with
three ``danger`` risks (FO) and BONK with only a warning.
"""

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from src.core.clock import FrozenClock
from src.domain.models.primitives import PublicKey
from src.market.models import Freshness, ProviderState, RugCheckReading
from src.providers.rugcheck import RUGCHECK_URL, RugCheckProvider
from tests.gmgn.test_gmgn_openapi import Sleeper

NOW = datetime(2026, 10, 25, 10, 30, tzinfo=UTC)
FIXTURES = Path(__file__).parent / "fixtures" / "rugcheck"
CLEAN = PublicKey("2LVYAfg3ZtgFLp2B9QrTZy18sVzxqmocNmAgBS9Vpump")
DANGER = PublicKey("JDzPbXboQYWVmdxXS3LbvjM52RtsV1QaSv2AzoCiai2o")
WARN = PublicKey("DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263")
RECORDED = {CLEAN.root: "clean", DANGER.root: "danger", WARN.root: "warn"}

Handler = Callable[[httpx.Request], httpx.Response]


def recorded(request: httpx.Request) -> httpx.Response:
    mint = request.url.path.split("/")[3]
    name = RECORDED.get(mint)
    if name is None:
        return httpx.Response(404, json={"error": "not found"})
    body = (FIXTURES / f"summary_{name}.json").read_text(encoding="utf-8")
    return httpx.Response(200, content=body, headers={"Content-Type": "application/json"})


def scenario(
    handler: Handler,
    steps: Callable[[RugCheckProvider, FrozenClock], Any],
    **options: Any,
) -> tuple[Any, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    async def run() -> Any:
        clock = FrozenClock(NOW)
        async with httpx.AsyncClient(
            base_url=RUGCHECK_URL, transport=httpx.MockTransport(answer)
        ) as client:
            provider = RugCheckProvider(client, clock=clock, sleep=Sleeper(clock), **options)
            return await steps(provider, clock)

    return asyncio.run(run()), requests


def test_danger_risks_are_kept_apart_from_warnings() -> None:
    async def steps(provider: RugCheckProvider, clock: FrozenClock) -> list[RugCheckReading]:
        return [await provider.report(mint) for mint in (CLEAN, DANGER, WARN)]

    (clean, danger, warn), requests = scenario(recorded, steps)

    assert [item.url.path for item in requests] == [
        f"/v1/tokens/{mint.root}/report/summary" for mint in (CLEAN, DANGER, WARN)
    ]
    assert (clean.freshness, clean.danger, clean.warnings, clean.score) == (
        Freshness.FRESH,
        (),
        (),
        1,
    )
    assert danger.danger == (
        "Top 10 holders high ownership",
        "Single holder ownership",
        "High ownership",
    )
    assert danger.warnings == ("Mutable metadata",)
    assert danger.score == 57
    # lpLockedPct 0.0009144395780156977 (percent), as a fraction.
    assert danger.lp_locked_share == Decimal("0.0009144395780156977") / 100
    assert (warn.danger, warn.warnings, warn.score) == ((), ("Mutable metadata",), 7)


def test_a_report_is_held_for_its_ttl_and_a_failure_for_less() -> None:
    answers = [httpx.Response(500)]

    def handler(request: httpx.Request) -> httpx.Response:
        return answers.pop(0) if answers else recorded(request)

    async def steps(provider: RugCheckProvider, clock: FrozenClock) -> list[str | None]:
        details = [(await provider.report(DANGER)).detail]
        clock.advance(timedelta(minutes=1))
        details.append((await provider.report(DANGER)).detail)  # the failure, held
        clock.advance(timedelta(minutes=1))
        details.append((await provider.report(DANGER)).detail)  # asked again: good
        clock.advance(timedelta(minutes=29))
        details.append((await provider.report(DANGER)).detail)  # the good one, held
        return details

    details, requests = scenario(handler, steps)

    assert details == ["API_UNAVAILABLE", "API_UNAVAILABLE", None, None]
    assert len(requests) == 2


def test_an_unknown_token_a_429_or_a_new_format_is_a_report_never_an_error() -> None:
    answers: list[httpx.Response | Exception] = [
        httpx.Response(404),
        httpx.Response(429),
        httpx.Response(200, json={"risks": "changed"}),
        httpx.ReadTimeout("slow"),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def steps(provider: RugCheckProvider, clock: FrozenClock) -> list[Any]:
        found: list[Any] = [(await provider.report(CLEAN)).detail]
        clock.advance(timedelta(minutes=2))
        found.append((await provider.report(CLEAN)).detail)
        found.append(provider.health.state)
        clock.advance(timedelta(minutes=2))
        found.append((await provider.report(CLEAN)).detail)
        clock.advance(timedelta(minutes=2))
        found.append((await provider.report(CLEAN)).detail)
        return found

    found, requests = scenario(handler, steps, failure_threshold=10)

    assert found == [
        "NOT_LISTED",
        "RATE_LIMITED",
        ProviderState.OPEN,  # paused a minute after the 429
        "API_PAYLOAD_INVALID",
        "API_TIMEOUT",
    ]
    assert len(requests) == 4


def test_reports_are_asked_at_most_one_a_second() -> None:
    mints = [CLEAN, DANGER, WARN]

    async def steps(provider: RugCheckProvider, clock: FrozenClock) -> timedelta:
        start = clock.now()
        for mint in mints:
            await provider.report(mint)
        return clock.now() - start

    waited, _ = scenario(recorded, steps)

    assert waited == timedelta(seconds=2)


def test_the_recorded_answer_is_the_live_shape() -> None:
    body = json.loads((FIXTURES / "summary_danger.json").read_text(encoding="utf-8"))

    assert {risk["level"] for risk in body["risks"]} == {"danger", "warn"}
    assert isinstance(body["score_normalised"], int)
