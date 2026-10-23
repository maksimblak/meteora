"""``GmgnProvider``: the worker's httpx client of the internal ``gmgn`` service (G18).

Every failure of the service, of the page behind it or of the payload format ends as an
unavailable reading (degradation), never as an exception in the candidate pipeline.
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from src.core.clock import FrozenClock
from src.domain.models.primitives import PublicKey
from src.gmgn import GmgnProvider
from src.market.models import EnhancedTokenSignals, Freshness, ProviderState

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
MINT = PublicKey("DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263")
CONTRACT = Path(__file__).parent / "contract"


def payload(**changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "schema_version": "gmgn-signals.v1",
        "chain": "sol",
        "mint": MINT.root,
        "status": "AVAILABLE",
        "reason": None,
        "source_at": (NOW - timedelta(minutes=2)).isoformat(),
        "freshness": "FRESH",
        "signals": {
            "top10_holder_share": "0.1532",
            "insiders_share": "0.0412",
            "bundling_share": "0.2210",
            "phishing_share": "0.0805",
            "developer_share": "0",
            "total_fees_sol": "152.37",
        },
        "missing": [],
    }
    body.update(changes)
    return body


class Service:
    """The internal service as the worker sees it: canned answers, recorded requests."""

    def __init__(self, *answers: httpx.Response | Exception) -> None:
        self.answers = list(answers)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer


def ok(body: object) -> httpx.Response:
    return httpx.Response(200, json=body)


def read(service: Service, *, clock: FrozenClock | None = None, **options: Any) -> Any:
    clock = clock or FrozenClock(NOW)

    async def run() -> tuple[list[EnhancedTokenSignals], GmgnProvider]:
        async with httpx.AsyncClient(
            base_url="http://gmgn:8080", transport=httpx.MockTransport(service)
        ) as client:
            provider = GmgnProvider(client, clock=clock, **options)
            readings = [await provider.get_token_signals(MINT)]
            return readings, provider

    return asyncio.run(run())


def test_a_fresh_service_answer_becomes_exact_enhanced_signals() -> None:
    service = Service(ok(payload()))

    (signals,), provider = read(service)

    (request,) = service.requests
    assert request.method == "GET"
    assert request.url.path == f"/v1/tokens/sol/{MINT.root}/signals"
    assert signals.provider == "gmgn"
    assert signals.freshness is Freshness.FRESH
    assert signals.source_at == NOW - timedelta(minutes=2)
    assert signals.top10_holder_share == Decimal("0.1532")
    assert signals.insiders_share == Decimal("0.0412")
    assert signals.bundling_share == Decimal("0.2210")
    assert signals.phishing_share == Decimal("0.0805")
    assert signals.developer_share == Decimal("0")
    assert signals.total_fees_sol == Decimal("152.37")
    assert signals.missing_signals == ()
    assert signals.detail is None
    # GMGN carries no blacklist, verification or wash-trading verdict.
    assert signals.holder_concentration is None
    assert provider.health.state is ProviderState.HEALTHY


def test_an_answer_older_than_the_freshness_window_is_stale() -> None:
    old = payload(source_at=(NOW - timedelta(minutes=16)).isoformat())

    (signals,), _ = read(Service(ok(old)), stale_after=timedelta(minutes=15))

    assert signals.freshness is Freshness.STALE
    assert signals.insiders_share == Decimal("0.0412")


def test_a_service_clock_ahead_of_the_worker_never_dates_the_reading_in_the_future() -> None:
    ahead = payload(source_at=(NOW + timedelta(seconds=5)).isoformat())

    (signals,), _ = read(Service(ok(ahead)))

    assert signals.source_at == NOW
    assert signals.freshness is Freshness.FRESH


@pytest.mark.parametrize("reason", ["CHALLENGE_TIMEOUT", "CAPTCHA", "CIRCUIT_OPEN"])
def test_an_unavailable_page_is_an_unavailable_reading_with_the_reason(reason: str) -> None:
    body = payload(status="UNAVAILABLE", reason=reason, source_at=None, freshness="STALE")
    body["signals"] = {name: None for name in body["signals"]}

    (signals,), provider = read(Service(ok(body)))

    assert signals.freshness is Freshness.STALE
    assert signals.detail == reason
    assert signals.source_at == NOW
    assert (signals.insiders_share, signals.bundling_share, signals.total_fees_sol) == (
        None,
        None,
        None,
    )
    assert provider.health.state is ProviderState.DEGRADED
    assert provider.health.last_error == reason


@pytest.mark.parametrize(
    ("answer", "detail"),
    [
        (httpx.Response(503, json={"error": "down"}), "SERVICE_UNAVAILABLE"),
        (httpx.ConnectError("refused"), "SERVICE_UNAVAILABLE"),
        (httpx.ReadTimeout("slow"), "SERVICE_TIMEOUT"),
        (httpx.Response(200, content=b"<html>not json</html>"), "SERVICE_PAYLOAD_INVALID"),
        (ok({"schema_version": "gmgn-signals.v9"}), "SERVICE_SCHEMA_UNKNOWN"),
        (ok(["not", "an", "object"]), "SERVICE_SCHEMA_UNKNOWN"),
        (ok(payload(source_at="yesterday")), "SERVICE_PAYLOAD_INVALID"),
        (ok(payload(mint="11111111111111111111111111111111")), "SERVICE_MINT_MISMATCH"),
    ],
    ids=["http-503", "refused", "timeout", "html", "schema", "array", "time", "mint"],
)
def test_every_service_failure_degrades_instead_of_raising(
    answer: httpx.Response | Exception, detail: str
) -> None:
    (signals,), provider = read(Service(answer))

    assert signals.freshness is Freshness.STALE
    assert signals.detail == detail
    assert signals.insiders_share is None
    assert provider.health.state is not ProviderState.HEALTHY


def test_a_changed_metric_format_drops_only_that_metric() -> None:
    body = payload()
    body["signals"] = {
        **body["signals"],
        "bundling_share": 0.22,  # a binary float: never trusted for a threshold
        "phishing_share": "1.7",  # not a share
        "insiders_share": {"value": "0.04"},  # a new shape
        "new_metric": "0.5",
    }

    (signals,), _ = read(Service(ok(body)))

    assert signals.freshness is Freshness.FRESH
    assert (signals.bundling_share, signals.phishing_share, signals.insiders_share) == (
        None,
        None,
        None,
    )
    assert signals.top10_holder_share == Decimal("0.1532")
    assert set(signals.missing_signals) == {"bundling_share", "phishing_share", "insiders_share"}


def test_repeated_service_failures_open_the_breaker_and_stop_the_requests() -> None:
    service = Service(httpx.Response(503, json={"error": "down"}))
    clock = FrozenClock(NOW)

    async def run() -> tuple[list[EnhancedTokenSignals], GmgnProvider]:
        async with httpx.AsyncClient(
            base_url="http://gmgn:8080", transport=httpx.MockTransport(service)
        ) as client:
            provider = GmgnProvider(client, clock=clock, failure_threshold=2)
            readings = [await provider.get_token_signals(MINT) for _ in range(4)]
            return readings, provider

    readings, provider = asyncio.run(run())

    assert len(service.requests) == 2
    assert [item.detail for item in readings[2:]] == ["CIRCUIT_OPEN", "CIRCUIT_OPEN"]
    assert provider.health.state is ProviderState.OPEN


def test_the_shared_contract_corpus_parses_on_the_worker_side() -> None:
    """The same JSON files the service's own tests answer byte for byte (G18 contract)."""

    files = sorted(CONTRACT.glob("signals_v1_*.json"))
    assert {path.stem for path in files} >= {"signals_v1_available", "signals_v1_unavailable"}
    for path in files:
        body = json.loads(path.read_text(encoding="utf-8"))
        clock = FrozenClock(
            datetime.fromisoformat(body["source_at"] or "2026-10-23T12:00:00+00:00")
        )
        mint = body["mint"]

        async def run(
            body: dict[str, Any] = body, mint: str = mint, clock: FrozenClock = clock
        ) -> EnhancedTokenSignals:
            async with httpx.AsyncClient(
                base_url="http://gmgn:8080",
                transport=httpx.MockTransport(lambda request: ok(body)),
            ) as client:
                return await GmgnProvider(client, clock=clock).get_token_signals(PublicKey(mint))

        signals = asyncio.run(run())
        if body["status"] == "AVAILABLE":
            assert signals.freshness is Freshness.FRESH, path.name
            assert signals.detail is None
        else:
            assert signals.freshness is Freshness.STALE, path.name
            assert signals.detail == body["reason"]
