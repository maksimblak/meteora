"""``SignalService``: when a page is loaded, and what one request is answered.

* **Cache per token.** A successful reading is served for ``cache_ttl``; afterwards it is
  read again, and while that fails the old reading is served as ``STALE``.
* **Bounded loads.** At most ``max_pages`` pages are open at once and ``max_pending``
  tokens wait for one; more are refused (``QUEUE_FULL``). One token is never loaded twice
  at the same time: concurrent requests join the load in flight.
* **Bounded waiting.** A request waits ``response_wait`` for a load; a slower load answers
  ``FETCH_PENDING`` and keeps running, so the next request finds it in the cache.
* **Backoff.** A token whose page failed is not loaded again for ``backoff_base`` seconds,
  doubled on every further failure up to ``backoff_max``.
* **Circuit breaker.** ``breaker_threshold`` failed loads in a row (any tokens) stop all
  loads for ``breaker_cooldown``; then one probe load decides whether to close it.

Nothing here retries a challenge or a captcha differently: a failure is only waited out.
"""

import asyncio
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from gmgn_service.config import ServiceSettings
from gmgn_service.contract import SignalsV1, SignalValues
from gmgn_service.extract import SIGNAL_NAMES, Extraction


class PageUnavailable(Exception):
    """The page gave no metrics; ``reason`` is a stable code of ``gmgn-signals.v1``."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class PageFetcher(Protocol):
    async def fetch(self, chain: str, mint: str) -> Extraction: ...

    @property
    def ready(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class _Reading:
    extraction: Extraction
    source_at: datetime
    loaded_at: float


@dataclass(slots=True)
class _Failure:
    reason: str
    count: int
    retry_at: float


class SignalService:
    def __init__(
        self,
        fetcher: PageFetcher,
        *,
        settings: ServiceSettings,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetcher = fetcher
        self._settings = settings
        self._now = now
        self._monotonic = monotonic
        self._pages = asyncio.Semaphore(settings.max_pages)
        self._cache: OrderedDict[tuple[str, str], _Reading] = OrderedDict()
        self._failures: dict[tuple[str, str], _Failure] = {}
        self._inflight: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._failures_in_row = 0
        self._open_until: float | None = None
        self._probing = False

    async def signals(self, chain: str, mint: str) -> SignalsV1:
        key = (chain, mint)
        cached = self._cache.get(key)
        if cached is not None and self._age(cached) <= self._settings.cache_ttl:
            return self._available(key, cached, reason=None)
        task = self._inflight.get(key)
        if task is None:
            refusal = self._refusal(key)
            if refusal is not None:
                return self._answer(key, cached, refusal)
            task = asyncio.create_task(self._load(key))
            self._inflight[key] = task
        done, _ = await asyncio.wait({task}, timeout=self._settings.response_wait)
        if not done:
            return self._answer(key, cached, "FETCH_PENDING")
        reading = self._cache.get(key)
        if reading is not None and reading is not cached:
            return self._available(key, reading, reason=None)
        failure = self._failures.get(key)
        return self._answer(key, cached, failure.reason if failure else "NO_SIGNALS")

    def readiness(self) -> tuple[bool, list[str]]:
        reasons: list[str] = []
        if not self._fetcher.ready:
            reasons.append("BROWSER_UNAVAILABLE")
        if self._breaker_open():
            reasons.append("CIRCUIT_OPEN")
        return not reasons, reasons

    async def drain(self) -> None:
        """Wait for every load in flight (shutdown and tests)."""

        while self._inflight:
            await asyncio.gather(*self._inflight.values(), return_exceptions=True)

    # --- loads -----------------------------------------------------------------------

    def _refusal(self, key: tuple[str, str]) -> str | None:
        failure = self._failures.get(key)
        if failure is not None and self._monotonic() < failure.retry_at:
            return failure.reason
        if self._breaker_open():
            return "CIRCUIT_OPEN"
        if len(self._inflight) >= self._settings.max_pending:
            return "QUEUE_FULL"
        if self._open_until is not None:
            # Cooldown over: exactly one probe load decides whether the circuit closes.
            if self._probing:
                return "CIRCUIT_OPEN"
            self._probing = True
        return None

    def _breaker_open(self) -> bool:
        return self._open_until is not None and self._monotonic() < self._open_until

    async def _load(self, key: tuple[str, str]) -> None:
        try:
            async with self._pages:
                try:
                    extraction = await self._fetcher.fetch(*key)
                except PageUnavailable as unavailable:
                    self._failed(key, unavailable.reason)
                    return
                except Exception:  # an unexpected browser fault is one more failed load
                    self._failed(key, "BROWSER_ERROR")
                    return
                if extraction.empty:
                    self._failed(key, "NO_SIGNALS")
                    return
                self._succeeded(key, extraction)
        finally:
            self._inflight.pop(key, None)

    def _succeeded(self, key: tuple[str, str], extraction: Extraction) -> None:
        self._failures.pop(key, None)
        self._failures_in_row = 0
        self._open_until = None
        self._probing = False
        self._cache[key] = _Reading(extraction, self._now(), self._monotonic())
        self._cache.move_to_end(key)
        while len(self._cache) > self._settings.max_cached:
            self._cache.popitem(last=False)

    def _failed(self, key: tuple[str, str], reason: str) -> None:
        settings = self._settings
        previous = self._failures.get(key)
        count = 1 if previous is None else previous.count + 1
        delay = min(settings.backoff_max, settings.backoff_base * 2 ** (count - 1))
        self._failures.pop(key, None)
        self._failures[key] = _Failure(reason, count, self._monotonic() + delay)
        while len(self._failures) > settings.max_cached:
            del self._failures[next(iter(self._failures))]
        self._failures_in_row += 1
        if self._probing or self._failures_in_row >= settings.breaker_threshold:
            self._open_until = self._monotonic() + settings.breaker_cooldown
        self._probing = False

    # --- answers ---------------------------------------------------------------------

    def _age(self, reading: _Reading) -> float:
        return self._monotonic() - reading.loaded_at

    def _answer(self, key: tuple[str, str], cached: _Reading | None, reason: str) -> SignalsV1:
        if cached is not None:
            return self._available(key, cached, reason=reason)
        chain, mint = key
        return SignalsV1(
            chain=chain,
            mint=mint,
            status="UNAVAILABLE",
            reason=reason,
            freshness="STALE",
            missing=list(SIGNAL_NAMES),
        )

    def _available(
        self, key: tuple[str, str], reading: _Reading, *, reason: str | None
    ) -> SignalsV1:
        chain, mint = key
        fresh = self._age(reading) <= self._settings.cache_ttl
        return SignalsV1(
            chain=chain,
            mint=mint,
            status="AVAILABLE",
            reason=reason,
            source_at=reading.source_at,
            freshness="FRESH" if fresh else "STALE",
            signals=SignalValues(**reading.extraction.signals),
            missing=list(reading.extraction.missing),
            sources=list(reading.extraction.sources),
        )
