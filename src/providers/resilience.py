"""Shared timeout, rate-limit, retry, breaker and health behavior for providers."""

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from src.market.models import ProviderHealth as ProviderHealth
from src.market.models import ProviderState as ProviderState
from src.providers.contracts import (
    ProviderPayloadError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)

Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay: float = 0.25
    max_delay: float = 2.0
    jitter_ratio: float = 0.2

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least one")
        if self.base_delay < 0 or self.max_delay < self.base_delay:
            raise ValueError("retry delays must be non-negative and bounded")
        if not 0 <= self.jitter_ratio <= 1:
            raise ValueError("jitter_ratio must be between zero and one")

    def delay(self, failed_attempt: int, *, random_value: float) -> float:
        if failed_attempt < 1:
            raise ValueError("failed_attempt must be at least one")
        if not 0 <= random_value <= 1:
            raise ValueError("random_value must be between zero and one")
        bounded = min(self.max_delay, self.base_delay * (2 ** (failed_attempt - 1)))
        jitter = bounded * self.jitter_ratio * ((2 * random_value) - 1)
        return float(min(self.max_delay, max(0.0, bounded + jitter)))


class CircuitBreaker:
    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        recovery_timeout: float = 30,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be at least one")
        if recovery_timeout <= 0:
            raise ValueError("recovery_timeout must be positive")
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._monotonic = monotonic
        self._failures = 0
        self._open_until: float | None = None
        self._half_open = False
        self._last_error: str | None = None

    def before_request(self) -> None:
        if self._open_until is None:
            return
        if self._monotonic() < self._open_until:
            raise ProviderUnavailableError("provider circuit breaker is open")
        self._half_open = True
        self._open_until = None

    def record_success(self) -> None:
        self._failures = 0
        self._open_until = None
        self._half_open = False
        self._last_error = None

    def record_failure(self, category: str) -> None:
        self._failures += 1
        self._last_error = category
        if self._half_open or self._failures >= self._failure_threshold:
            self._open_until = self._monotonic() + self._recovery_timeout
            self._half_open = False

    @property
    def health(self) -> ProviderHealth:
        if self._open_until is not None:
            state = ProviderState.OPEN
        elif self._half_open:
            state = ProviderState.HALF_OPEN
        elif self._failures:
            state = ProviderState.DEGRADED
        else:
            state = ProviderState.HEALTHY
        return ProviderHealth(state, self._failures, self._last_error, self._open_until)


class IntervalRateLimiter:
    """Serialize requests and enforce a maximum average request rate."""

    def __init__(
        self,
        requests_per_second: float,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if requests_per_second <= 0:
            raise ValueError("requests_per_second must be positive")
        self._interval = 1 / requests_per_second
        self._monotonic = monotonic
        self._sleep = sleep
        self._next_allowed = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = self._monotonic()
            delay = max(0.0, self._next_allowed - now)
            if delay:
                await self._sleep(delay)
                now = self._monotonic()
            self._next_allowed = max(now, self._next_allowed) + self._interval


class ResilientHttpClient:
    """A JSON HTTP client with identical resilience behavior for all adapters."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        retry_policy: RetryPolicy | None = None,
        circuit_breaker: CircuitBreaker | None = None,
        rate_limiter: IntervalRateLimiter | None = None,
        timeout: float = 5,
        sleep: Sleep = asyncio.sleep,
        random_source: Callable[[], float] = random.random,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._client = client
        self._retry = retry_policy or RetryPolicy()
        self._breaker = circuit_breaker or CircuitBreaker()
        self._rate_limiter = rate_limiter
        self._timeout = timeout
        self._sleep = sleep
        self._random = random_source

    @property
    def health(self) -> ProviderHealth:
        return self._breaker.health

    async def get_json(self, path: str, *, params: dict[str, str] | None = None) -> Any:
        return await self._request_json("GET", path, params=params, request_payload=None)

    async def post_json(self, payload: dict[str, Any]) -> Any:
        return await self._request_json("POST", "", params=None, request_payload=payload)

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None,
        request_payload: dict[str, Any] | None,
    ) -> Any:
        for attempt in range(1, self._retry.max_attempts + 1):
            self._breaker.before_request()
            if self._rate_limiter is not None:
                await self._rate_limiter.acquire()
            try:
                if method == "GET":
                    response = await self._client.get(path, params=params, timeout=self._timeout)
                else:
                    response = await self._client.post(
                        path, json=request_payload, timeout=self._timeout
                    )
            except httpx.TimeoutException as error:
                self._breaker.record_failure("timeout")
                if attempt == self._retry.max_attempts:
                    raise ProviderTimeoutError("provider request timed out") from error
                await self._backoff(attempt)
                continue
            except httpx.RequestError as error:
                self._breaker.record_failure("network")
                if attempt == self._retry.max_attempts:
                    raise ProviderUnavailableError("provider network request failed") from error
                await self._backoff(attempt)
                continue

            if response.status_code == 429:
                self._breaker.record_failure("rate_limit")
                if attempt == self._retry.max_attempts:
                    raise ProviderRateLimitError("provider rate limit exceeded")
                await self._backoff(attempt)
                continue
            if response.status_code >= 500:
                self._breaker.record_failure("server_error")
                if attempt == self._retry.max_attempts:
                    raise ProviderUnavailableError(f"provider returned HTTP {response.status_code}")
                await self._backoff(attempt)
                continue
            if response.status_code >= 400:
                self._breaker.record_failure("client_error")
                raise ProviderUnavailableError(f"provider returned HTTP {response.status_code}")

            try:
                response_payload: Any = response.json()
            except ValueError as error:
                self._breaker.record_failure("malformed_payload")
                raise ProviderPayloadError("provider returned malformed JSON") from error
            self._breaker.record_success()
            return response_payload
        raise AssertionError("retry loop exhausted without returning or raising")

    async def _backoff(self, failed_attempt: int) -> None:
        await self._sleep(self._retry.delay(failed_attempt, random_value=self._random()))
