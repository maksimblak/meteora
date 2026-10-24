"""``SignalService``: when a token is read, and what one request is answered.

* **Cache per token.** A successful reading is served for ``cache_ttl``; afterwards it is
  read again, and while that fails the old reading is served as ``STALE``.
* **Bounded loads.** At most ``max_pages`` loads run at once and ``max_pending`` tokens wait
  for one; more are refused (``QUEUE_FULL``). One token is never loaded twice at the same
  time: concurrent requests join the load in flight.
* **Bounded waiting.** A request waits ``response_wait`` for a load; a slower load answers
  ``FETCH_PENDING`` and keeps running, so the next request finds it in the cache.
* **Backoff.** A token whose load failed is not loaded again for ``backoff_base`` seconds,
  doubled on every further failure up to ``backoff_max``.
* **Circuit breaker.** ``breaker_threshold`` failed loads in a row (any tokens) stop all
  loads for ``breaker_cooldown``; a failure that says the site refused the visitor (a
  challenge, a captcha, a block, a rate limit: ``PageUnavailable(pause=True)``) stops them at
  once, and meanwhile every token is answered with that reason. Then one probe load decides
  whether to close it.

The answer schema is ``gmgn-signals.v1`` (the headless page service) or ``gmgn-signals.v2``
(the browser bridge); the rules above are the same for both. Nothing here retries a
challenge or a captcha differently: a failure is only waited out.
"""

import asyncio
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

from gmgn_service.config import ServiceSettings
from gmgn_service.contract import Signals, SignalsV1


class PageUnavailable(Exception):
    """The source gave no metrics; ``reason`` is a stable code of the answer schema.

    ``pause``: the site refused the visitor itself - every load stops for the cooldown."""

    def __init__(self, reason: str, *, pause: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.pause = pause


class Reading(Protocol):
    """One successful reading of a token: its signals and the answers they came from."""

    @property
    def signals(self) -> Mapping[str, object]: ...

    @property
    def sources(self) -> tuple[str, ...]: ...

    @property
    def missing(self) -> tuple[str, ...]: ...

    @property
    def empty(self) -> bool: ...


class PageFetcher(Protocol):
    async def fetch(self, chain: str, mint: str) -> Reading: ...

    @property
    def ready(self) -> bool: ...


@runtime_checkable
class QueueAware(Protocol):
    """A fetcher that can serve tokens still waiting in one go (the bridge's batch)."""

    def want(self, chain: str, mint: str) -> None: ...


@dataclass(frozen=True, slots=True)
class _Reading:
    reading: Reading
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
        schema: type[Signals] = SignalsV1,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetcher = fetcher
        self._settings = settings
        self._schema = schema
        self._now = now
        self._monotonic = monotonic
        self._pages = asyncio.Semaphore(settings.max_pages)
        self._cache: OrderedDict[tuple[str, str], _Reading] = OrderedDict()
        self._failures: dict[tuple[str, str], _Failure] = {}
        self._inflight: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._failures_in_row = 0
        self._open_until: float | None = None
        self._open_reason: str | None = None
        self._probing = False

    async def signals(self, chain: str, mint: str) -> Signals:
        key = (chain, mint)
        cached = self._cache.get(key)
        if cached is not None and self._age(cached) <= self._settings.cache_ttl:
            return self._available(key, cached, reason=None)
        task = self._inflight.get(key)
        if task is None:
            refusal = self._refusal(key)
            if refusal is not None:
                return self._answer(key, cached, refusal)
            if isinstance(self._fetcher, QueueAware):
                self._fetcher.want(chain, mint)
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

    def paused(self) -> tuple[str, datetime] | None:
        """Why and until when every load is stopped; ``None`` while loads may run."""

        if self._open_until is None or not self._breaker_open():
            return None
        left = self._open_until - self._monotonic()
        return self._open_reason or "CIRCUIT_OPEN", self._now() + timedelta(seconds=left)

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
            return self._open_reason or "CIRCUIT_OPEN"
        if len(self._inflight) >= self._settings.max_pending:
            return "QUEUE_FULL"
        if self._open_until is not None:
            # Cooldown over: exactly one probe load decides whether the circuit closes.
            if self._probing:
                return self._open_reason or "CIRCUIT_OPEN"
            self._probing = True
        return None

    def _breaker_open(self) -> bool:
        return self._open_until is not None and self._monotonic() < self._open_until

    async def _load(self, key: tuple[str, str]) -> None:
        try:
            async with self._pages:
                if self._breaker_open():
                    # Queued before the pause began: answered with its reason, nothing sent.
                    self._held(key)
                    return
                try:
                    reading = await self._fetcher.fetch(*key)
                except PageUnavailable as unavailable:
                    self._failed(key, unavailable.reason, pause=unavailable.pause)
                    return
                except Exception:  # an unexpected browser fault is one more failed load
                    self._failed(key, "BROWSER_ERROR")
                    return
                if reading.empty:
                    self._failed(key, "NO_SIGNALS")
                    return
                self._succeeded(key, reading)
        finally:
            self._inflight.pop(key, None)

    def _succeeded(self, key: tuple[str, str], reading: Reading) -> None:
        self._failures.pop(key, None)
        self._failures_in_row = 0
        self._open_until = None
        self._open_reason = None
        self._probing = False
        self._cache[key] = _Reading(reading, self._now(), self._monotonic())
        self._cache.move_to_end(key)
        while len(self._cache) > self._settings.max_cached:
            self._cache.popitem(last=False)

    def _held(self, key: tuple[str, str]) -> None:
        """A load that never ran because every load is stopped: the token gets the stop's
        reason until it ends; no failure is counted and the backoff does not grow."""

        previous = self._failures.get(key)
        retry_at = self._open_until if self._open_until is not None else self._monotonic()
        self._failures.pop(key, None)
        self._failures[key] = _Failure(
            self._open_reason or "CIRCUIT_OPEN",
            previous.count if previous is not None else 0,
            retry_at,
        )

    def _failed(self, key: tuple[str, str], reason: str, *, pause: bool = False) -> None:
        settings = self._settings
        previous = self._failures.get(key)
        count = 1 if previous is None else previous.count + 1
        delay = min(settings.backoff_max, settings.backoff_base * 2 ** (count - 1))
        self._failures.pop(key, None)
        self._failures[key] = _Failure(reason, count, self._monotonic() + delay)
        while len(self._failures) > settings.max_cached:
            del self._failures[next(iter(self._failures))]
        self._failures_in_row += 1
        if pause or self._probing or self._failures_in_row >= settings.breaker_threshold:
            self._open_until = self._monotonic() + settings.breaker_cooldown
            self._open_reason = reason if pause else None
        self._probing = False

    # --- answers ---------------------------------------------------------------------

    def _age(self, reading: _Reading) -> float:
        return self._monotonic() - reading.loaded_at

    def _answer(self, key: tuple[str, str], cached: _Reading | None, reason: str) -> Signals:
        if cached is not None:
            return self._available(key, cached, reason=reason)
        chain, mint = key
        return self._schema(
            chain=chain,
            mint=mint,
            status="UNAVAILABLE",
            reason=reason,
            freshness="STALE",
            missing=list(self._schema.signal_names),
        )

    def _available(self, key: tuple[str, str], reading: _Reading, *, reason: str | None) -> Signals:
        chain, mint = key
        fresh = self._age(reading) <= self._settings.cache_ttl
        return self._schema(
            chain=chain,
            mint=mint,
            status="AVAILABLE",
            reason=reason,
            source_at=reading.source_at,
            freshness="FRESH" if fresh else "STALE",
            signals=dict(reading.reading.signals),
            missing=list(reading.reading.missing),
            sources=list(reading.reading.sources),
        )
