"""Lease-guarded job scheduler with per-job intervals, priorities and bounded concurrency.

* Every run first acquires the job's lease; a held lease means another worker runs
  it, so this worker skips and retries soon (``contention_retry``).
* Due jobs start in priority order (lower first: open positions before candidates
  before universe) while free slots remain; a job never overlaps itself.
* A running job renews its lease every ``ttl / 3``; if renewal fails the lease was
  lost (e.g. taken over after this worker stalled) and the job is cancelled at once.
* Each run has its own correlation ID bound to every log record it emits.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID, uuid4

from src.core.clock import Clock
from src.observability.logging import cycle_context, error_fields, get_logger
from src.observability.metrics import WorkerMetrics
from src.persistence.contracts import JobLease, JobLeases


@dataclass(frozen=True, slots=True)
class JobContext:
    name: str
    correlation_id: UUID
    started_at: datetime
    lease: JobLease


type JobHandler = Callable[[JobContext], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class JobSpec:
    name: str
    interval: timedelta
    handler: JobHandler
    priority: int = 100
    lease_ttl: timedelta = timedelta(minutes=2)
    timeout: timedelta | None = None

    def __post_init__(self) -> None:
        if self.interval <= timedelta(0) or self.lease_ttl <= timedelta(0):
            raise ValueError("job interval and lease ttl must be positive")


class JobOutcome(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    LEASE_LOST = "LEASE_LOST"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True, slots=True)
class JobRun:
    name: str
    correlation_id: UUID
    outcome: JobOutcome
    started_at: datetime
    finished_at: datetime
    error_type: str | None = None


class Scheduler:
    def __init__(
        self,
        jobs: Sequence[JobSpec],
        leases: JobLeases,
        *,
        owner: str,
        clock: Clock,
        max_concurrency: int = 2,
        metrics: WorkerMetrics | None = None,
        contention_retry: timedelta = timedelta(seconds=5),
    ) -> None:
        names = [job.name for job in jobs]
        if len(set(names)) != len(names):
            raise ValueError("job names must be unique")
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least one")
        self._jobs = sorted(jobs, key=lambda job: (job.priority, job.name))
        self._leases = leases
        self._owner = owner
        self._clock = clock
        self._max_concurrency = max_concurrency
        self._metrics = metrics or WorkerMetrics()
        self._contention_retry = contention_retry
        self._next_due: dict[str, datetime] = {}
        self._tasks: dict[str, asyncio.Task[JobRun]] = {}
        self._lost: set[str] = set()
        self._finished: list[JobRun] = []
        self._log = get_logger("worker.scheduler")

    @property
    def owner(self) -> str:
        return self._owner

    @property
    def running(self) -> frozenset[str]:
        return frozenset(name for name, task in self._tasks.items() if not task.done())

    def due(self) -> tuple[JobSpec, ...]:
        now = self._clock.now()
        running = self.running
        return tuple(
            job
            for job in self._jobs
            if job.name not in running and self._next_due.get(job.name, now) <= now
        )

    def next_wakeup(self) -> datetime:
        now = self._clock.now()
        return min((self._next_due.get(job.name, now) for job in self._jobs), default=now)

    async def tick(self) -> tuple[str, ...]:
        self._collect()
        started: list[str] = []
        free = self._max_concurrency - len(self.running)
        acquired: list[tuple[JobSpec, JobLease, datetime]] = []
        for job in self.due():
            if free <= 0:
                break
            now = self._clock.now()
            lease = await self._leases.acquire(job.name, self._owner, job.lease_ttl)
            if lease is None:
                self._metrics.lease_contended(job.name)
                self._next_due[job.name] = now + min(job.interval, self._contention_retry)
                continue
            self._next_due[job.name] = now + job.interval
            acquired.append((job, lease, now))
            free -= 1
        # Start only after every lease of this tick is held, so a started job never
        # competes with a sibling of the same tick for that sibling's lease.
        for job, lease, now in acquired:
            self._metrics.lease(job.name, held=True)
            self._tasks[job.name] = asyncio.create_task(
                self._execute(job, lease, uuid4(), now), name=f"job:{job.name}"
            )
            started.append(job.name)
        return tuple(started)

    async def drain(self) -> tuple[JobRun, ...]:
        """Wait for every running job and return the runs finished since the last drain."""

        pending = [task for task in self._tasks.values() if not task.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._collect()
        finished, self._finished = tuple(self._finished), []
        return finished

    async def shutdown(self) -> tuple[JobRun, ...]:
        for task in self._tasks.values():
            if not task.done():
                task.cancel()
        return await self.drain()

    async def run_forever(
        self,
        stop: asyncio.Event,
        *,
        poll_interval: float = 1.0,
        on_tick: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        while not stop.is_set():
            if on_tick is not None:
                await on_tick()
            await self.tick()
            wait = (self.next_wakeup() - self._clock.now()).total_seconds()
            try:
                await asyncio.wait_for(stop.wait(), timeout=min(max(wait, 0.05), poll_interval))
            except TimeoutError:
                continue
        await self.shutdown()

    def _collect(self) -> None:
        for name, task in list(self._tasks.items()):
            if task.done():
                del self._tasks[name]
                if not task.cancelled():
                    self._finished.append(task.result())

    async def _execute(
        self, job: JobSpec, lease: JobLease, correlation_id: UUID, started_at: datetime
    ) -> JobRun:
        current = asyncio.current_task()
        renewal = asyncio.create_task(self._keep_renewed(job, lease, current))
        outcome = JobOutcome.SUCCEEDED
        error_type: str | None = None
        clock_started = time.perf_counter()
        with cycle_context(correlation_id=correlation_id, job=job.name, lease_fence=lease.token):
            self._log.info("job_started", owner=self._owner)
            try:
                context = JobContext(job.name, correlation_id, started_at, lease)
                if job.timeout is None:
                    await job.handler(context)
                else:
                    await asyncio.wait_for(job.handler(context), job.timeout.total_seconds())
            except TimeoutError:
                outcome, error_type = JobOutcome.TIMED_OUT, "TimeoutError"
            except asyncio.CancelledError:
                outcome = JobOutcome.LEASE_LOST if job.name in self._lost else JobOutcome.CANCELLED
            except Exception as error:
                outcome, error_type = JobOutcome.FAILED, type(error).__name__
                self._log.error("job_failed", **error_fields(error))
            finally:
                renewal.cancel()
                await asyncio.gather(renewal, return_exceptions=True)
            if outcome is not JobOutcome.LEASE_LOST:
                try:
                    await self._leases.release(lease)
                except Exception as error:
                    self._log.warning("lease_release_failed", **error_fields(error))
            self._lost.discard(job.name)
            self._metrics.lease(job.name, held=False)
            self._metrics.job_run(job.name, outcome.value, time.perf_counter() - clock_started)
            self._log.info("job_finished", outcome=outcome.value)
        return JobRun(
            name=job.name,
            correlation_id=correlation_id,
            outcome=outcome,
            started_at=started_at,
            finished_at=self._clock.now(),
            error_type=error_type,
        )

    async def _keep_renewed(
        self, job: JobSpec, lease: JobLease, task: "asyncio.Task[object] | None"
    ) -> None:
        interval = max(job.lease_ttl.total_seconds() / 3, 0.01)
        current = lease
        while True:
            await asyncio.sleep(interval)
            try:
                renewed = await self._leases.renew(current, job.lease_ttl)
            except Exception as error:
                self._log.warning("lease_renewal_failed", **error_fields(error))
                renewed = None
            if renewed is None:
                self._lost.add(job.name)
                self._log.error("lease_lost", job=job.name)
                if task is not None:
                    task.cancel()
                return
            current = renewed
