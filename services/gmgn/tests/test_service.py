"""``SignalService``: cache per token, bounded pages, backoff and circuit breaker."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from gmgn_service.config import ServiceSettings
from gmgn_service.extract import SIGNAL_NAMES, Extraction
from gmgn_service.service import PageUnavailable, SignalService

START = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
MINTS = [
    "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm",
    "CSocnzcRpgSCr1naKcNTieWZUxb6rABe45j844m7pump",
    "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
    "6p6xgHyF7AeE6TZkSmFsko444wqoP15icUSqi2jfGiPN",
]


def full(**values: str | None) -> Extraction:
    signals: dict[str, Decimal | None] = {
        "top10_holder_share": Decimal("0.3217"),
        "insiders_share": Decimal("0.0001"),
        "bundling_share": Decimal("0.0028"),
        "phishing_share": Decimal("0.738"),
        "developer_share": Decimal("0"),
        "total_fees_sol": Decimal("1222.42"),
    }
    for name, value in values.items():
        signals[name] = None if value is None else Decimal(value)
    return Extraction(signals=signals, sources=("mutil_window_token_info", "token_stat"))


@dataclass
class Clock:
    wall: datetime = START
    seconds: float = 1000.0

    def now(self) -> datetime:
        return self.wall

    def monotonic(self) -> float:
        return self.seconds

    def advance(self, seconds: float) -> None:
        self.wall += timedelta(seconds=seconds)
        self.seconds += seconds


@dataclass
class Fetcher:
    """Answers page loads from a script; ``gate`` holds loads until released."""

    outcomes: list[Extraction | PageUnavailable] = field(default_factory=lambda: [full()])
    gate: asyncio.Event | None = None
    calls: list[str] = field(default_factory=list)
    running: int = 0
    peak: int = 0

    async def fetch(self, chain: str, mint: str) -> Extraction:
        self.calls.append(mint)
        self.running += 1
        self.peak = max(self.peak, self.running)
        try:
            if self.gate is not None:
                await self.gate.wait()
            outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
            if isinstance(outcome, PageUnavailable):
                raise outcome
            return outcome
        finally:
            self.running -= 1

    @property
    def ready(self) -> bool:
        return True


def settings(**changes: Any) -> ServiceSettings:
    values: dict[str, Any] = {
        "cache_ttl": 600.0,
        "max_pages": 2,
        "max_pending": 8,
        "response_wait": 1.0,
        "backoff_base": 60.0,
        "backoff_max": 240.0,
        "breaker_threshold": 3,
        "breaker_cooldown": 900.0,
    }
    values.update(changes)
    return ServiceSettings(**values)


def run(work: Callable[[], Awaitable[Any]]) -> Any:
    return asyncio.run(work())


def service(fetcher: Fetcher, clock: Clock, **changes: Any) -> SignalService:
    return SignalService(
        fetcher, settings=settings(**changes), now=clock.now, monotonic=clock.monotonic
    )


def test_a_token_is_read_once_and_served_from_the_cache_while_fresh() -> None:
    fetcher, clock = Fetcher(), Clock()
    signals = service(fetcher, clock)

    async def work() -> list[Any]:
        first = await signals.signals("sol", MINTS[0])
        clock.advance(599)
        second = await signals.signals("sol", MINTS[0])
        clock.advance(2)
        third = await signals.signals("sol", MINTS[0])
        return [first, second, third]

    first, second, third = run(work)

    assert fetcher.calls == [MINTS[0], MINTS[0]]
    assert first.status == "AVAILABLE" and first.freshness == "FRESH"
    assert first.source_at == START
    assert first.signals.phishing_share == Decimal("0.738")
    assert second.source_at == START and second.freshness == "FRESH"
    assert third.source_at == START + timedelta(seconds=601)


def test_page_loads_are_bounded_and_one_token_is_loaded_once_at_a_time() -> None:
    fetcher, clock = Fetcher(gate=None), Clock()

    async def work() -> list[Any]:
        fetcher.gate = asyncio.Event()
        signals = service(fetcher, clock, response_wait=5.0)
        asked = [signals.signals("sol", mint) for mint in MINTS] + [
            signals.signals("sol", MINTS[0]) for _ in range(3)
        ]
        tasks = [asyncio.create_task(item) for item in asked]
        await asyncio.sleep(0.05)
        peak_while_held = fetcher.peak
        fetcher.gate.set()
        answers = await asyncio.gather(*tasks)
        return [peak_while_held, answers]

    peak, answers = run(work)

    assert peak == 2
    assert sorted(fetcher.calls) == sorted(MINTS)
    assert all(answer.status == "AVAILABLE" for answer in answers)


def test_a_slow_page_answers_pending_and_the_load_finishes_for_the_next_request() -> None:
    fetcher, clock = Fetcher(), Clock()

    async def work() -> list[Any]:
        fetcher.gate = asyncio.Event()
        signals = service(fetcher, clock, response_wait=0.05)
        pending = await signals.signals("sol", MINTS[0])
        fetcher.gate.set()
        await signals.drain()
        ready = await signals.signals("sol", MINTS[0])
        return [pending, ready]

    pending, ready = run(work)

    assert (pending.status, pending.reason, pending.source_at) == (
        "UNAVAILABLE",
        "FETCH_PENDING",
        None,
    )
    assert ready.status == "AVAILABLE"
    assert fetcher.calls == [MINTS[0]]


def test_a_failed_token_backs_off_exponentially_without_new_page_loads() -> None:
    blocked = PageUnavailable("CHALLENGE_TIMEOUT")
    fetcher, clock = Fetcher(outcomes=[blocked, blocked, full()]), Clock()
    signals = service(fetcher, clock, breaker_threshold=10)

    async def work() -> list[Any]:
        answers = [await signals.signals("sol", MINTS[0])]
        clock.advance(59)
        answers.append(await signals.signals("sol", MINTS[0]))  # within 60 s: no load
        clock.advance(2)
        answers.append(await signals.signals("sol", MINTS[0]))  # second failure: 120 s
        clock.advance(119)
        answers.append(await signals.signals("sol", MINTS[0]))
        clock.advance(2)
        answers.append(await signals.signals("sol", MINTS[0]))
        return answers

    answers = run(work)

    assert fetcher.calls == [MINTS[0]] * 3
    assert [answer.reason for answer in answers[:4]] == ["CHALLENGE_TIMEOUT"] * 4
    assert all(answer.status == "UNAVAILABLE" for answer in answers[:4])
    assert answers[4].status == "AVAILABLE"


def test_repeated_page_failures_open_the_circuit_for_every_token() -> None:
    captcha = PageUnavailable("CAPTCHA")
    fetcher, clock = Fetcher(outcomes=[captcha, captcha, captcha, full()]), Clock()
    signals = service(fetcher, clock, breaker_threshold=3, breaker_cooldown=900.0)

    async def work() -> list[Any]:
        for mint in MINTS[:3]:
            await signals.signals("sol", mint)
        opened = await signals.signals("sol", MINTS[3])
        ready_while_open = signals.readiness()
        clock.advance(901)
        probe = await signals.signals("sol", MINTS[3])
        return [opened, ready_while_open, probe, signals.readiness()]

    opened, ready_while_open, probe, ready_after = run(work)

    assert fetcher.calls == [*MINTS[:3], MINTS[3]]
    assert (opened.status, opened.reason) == ("UNAVAILABLE", "CIRCUIT_OPEN")
    assert ready_while_open == (False, ["CIRCUIT_OPEN"])
    assert probe.status == "AVAILABLE"
    assert ready_after == (True, [])


def test_old_data_is_served_as_stale_while_the_page_cannot_be_read() -> None:
    fetcher, clock = Fetcher(outcomes=[full(), PageUnavailable("ACCESS_BLOCKED")]), Clock()
    signals = service(fetcher, clock)

    async def work() -> Any:
        await signals.signals("sol", MINTS[0])
        clock.advance(700)
        return await signals.signals("sol", MINTS[0])

    stale = run(work)

    assert stale.status == "AVAILABLE"
    assert stale.freshness == "STALE"
    assert stale.reason == "ACCESS_BLOCKED"
    assert stale.source_at == START


def test_a_partial_page_is_available_with_its_missing_metrics_named() -> None:
    fetcher, clock = Fetcher(outcomes=[full(bundling_share=None, total_fees_sol=None)]), Clock()

    answer = run(lambda: service(fetcher, clock).signals("sol", MINTS[0]))

    assert answer.status == "AVAILABLE"
    assert answer.missing == ["bundling_share", "total_fees_sol"]
    assert answer.signals.bundling_share is None


def test_too_many_waiting_tokens_are_refused_instead_of_queued_without_bound() -> None:
    fetcher, clock = Fetcher(), Clock()

    async def work() -> list[Any]:
        fetcher.gate = asyncio.Event()
        signals = service(fetcher, clock, max_pages=1, max_pending=2, response_wait=0.01)
        answers = [await signals.signals("sol", mint) for mint in MINTS[:3]]
        fetcher.gate.set()
        await signals.drain()
        return answers

    answers = run(work)

    assert [answer.reason for answer in answers] == ["FETCH_PENDING", "FETCH_PENDING", "QUEUE_FULL"]
    assert len(fetcher.calls) == 2


def test_the_answer_lists_every_signal_name_of_the_contract() -> None:
    answer = run(lambda: service(Fetcher(), Clock()).signals("sol", MINTS[0]))

    assert set(answer.signals.model_dump()) == set(SIGNAL_NAMES)
    assert answer.schema_version == "gmgn-signals.v1"
    assert (answer.chain, answer.mint) == ("sol", MINTS[0])
