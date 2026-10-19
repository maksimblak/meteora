import asyncio
from typing import Any

import httpx

from src.providers.resilience import (
    CircuitBreaker,
    IntervalRateLimiter,
    ProviderState,
    ResilientHttpClient,
    RetryPolicy,
)


class FakeMonotonic:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def test_retry_jitter_never_exceeds_the_configured_bound() -> None:
    policy = RetryPolicy(
        max_attempts=5,
        base_delay=1,
        max_delay=2,
        jitter_ratio=0.5,
    )

    assert policy.delay(10, random_value=1) == 2


def test_circuit_breaker_open_half_open_and_recovery_are_deterministic() -> None:
    monotonic = FakeMonotonic()
    breaker = CircuitBreaker(
        failure_threshold=1,
        recovery_timeout=5,
        monotonic=monotonic,
    )

    breaker.record_failure("timeout")
    open_state = breaker.health.state
    assert open_state is ProviderState.OPEN

    monotonic.advance(5)
    breaker.before_request()
    half_open_state = breaker.health.state
    assert half_open_state is ProviderState.HALF_OPEN

    breaker.record_success()
    recovered_state = breaker.health.state
    assert recovered_state is ProviderState.HEALTHY
    assert breaker.health.consecutive_failures == 0


def test_rate_limiter_uses_fake_monotonic_and_sleep() -> None:
    async def exercise() -> None:
        monotonic = FakeMonotonic()
        delays: list[float] = []

        async def fake_sleep(delay: float) -> None:
            delays.append(delay)
            monotonic.advance(delay)

        limiter = IntervalRateLimiter(
            2,
            monotonic=monotonic,
            sleep=fake_sleep,
        )

        await limiter.acquire()
        await limiter.acquire()
        await limiter.acquire()

        assert delays == [0.5, 0.5]

    asyncio.run(exercise())


def test_http_client_retries_429_and_timeout_with_bounded_backoff() -> None:
    async def exercise() -> None:
        attempts = 0
        delays: list[float] = []

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return httpx.Response(429, json={"error": "limited"})
            if attempts == 2:
                raise httpx.ReadTimeout("slow provider", request=request)
            return httpx.Response(200, json={"ok": True})

        async def fake_sleep(delay: float) -> None:
            delays.append(delay)

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(
            base_url="https://provider.invalid", transport=transport
        ) as http_client:
            client = ResilientHttpClient(
                http_client,
                retry_policy=RetryPolicy(
                    max_attempts=3,
                    base_delay=0.1,
                    max_delay=0.2,
                    jitter_ratio=0,
                ),
                circuit_breaker=CircuitBreaker(
                    failure_threshold=3,
                    recovery_timeout=30,
                ),
                timeout=1.5,
                sleep=fake_sleep,
            )
            payload: Any = await client.get_json("/data")

        assert payload == {"ok": True}
        assert attempts == 3
        assert delays == [0.1, 0.2]
        assert client.health.state is ProviderState.HEALTHY
        assert client.health.consecutive_failures == 0

    asyncio.run(exercise())


def test_rpc_post_uses_the_same_bounded_retry_contract() -> None:
    async def exercise() -> None:
        attempts = 0
        delays: list[float] = []

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            assert request.method == "POST"
            if attempts == 1:
                return httpx.Response(503, json={"error": "outage"})
            return httpx.Response(200, json={"result": "ok"})

        async def fake_sleep(delay: float) -> None:
            delays.append(delay)

        async with httpx.AsyncClient(
            base_url="https://rpc.invalid",
            transport=httpx.MockTransport(handler),
        ) as http_client:
            client = ResilientHttpClient(
                http_client,
                retry_policy=RetryPolicy(
                    max_attempts=2,
                    base_delay=0.1,
                    max_delay=0.1,
                    jitter_ratio=0,
                ),
                timeout=1,
                sleep=fake_sleep,
            )
            payload: Any = await client.post_json({"method": "getAccountInfo"})

        assert payload == {"result": "ok"}
        assert attempts == 2
        assert delays == [0.1]
        assert client.health.state is ProviderState.HEALTHY

    asyncio.run(exercise())
