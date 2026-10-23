import asyncio
from collections import Counter
from datetime import UTC, datetime, timedelta

from prometheus_client import CollectorRegistry, generate_latest

from src.core.clock import FrozenClock
from src.observability.metrics import WorkerMetrics
from src.persistence.memory import InMemoryJobLeases
from src.worker.scheduler import JobContext, JobOutcome, JobSpec, Scheduler

NOW = datetime(2026, 10, 20, 12, tzinfo=UTC)


def recorder(calls: list[str], name: str):  # type: ignore[no-untyped-def]
    async def handler(context: JobContext) -> None:
        assert context.name == name
        calls.append(name)

    return handler


def three_jobs(calls: list[str]) -> list[JobSpec]:
    return [
        JobSpec(
            "universe_sync", timedelta(seconds=60), recorder(calls, "universe_sync"), priority=2
        ),
        JobSpec(
            "position_cycle", timedelta(seconds=10), recorder(calls, "position_cycle"), priority=0
        ),
        JobSpec(
            "candidate_pipeline",
            timedelta(seconds=30),
            recorder(calls, "candidate_pipeline"),
            priority=1,
        ),
    ]


def test_jobs_run_on_their_own_intervals() -> None:
    async def scenario() -> Counter[str]:
        clock = FrozenClock(NOW)
        calls: list[str] = []
        scheduler = Scheduler(
            three_jobs(calls), InMemoryJobLeases(clock), owner="w1", clock=clock, max_concurrency=3
        )
        for _ in range(7):
            await scheduler.tick()
            await scheduler.drain()
            clock.advance(timedelta(seconds=10))
        return Counter(calls)

    counts = asyncio.run(scenario())
    assert counts == {"position_cycle": 7, "candidate_pipeline": 3, "universe_sync": 2}


def test_bounded_concurrency_starts_open_positions_first() -> None:
    async def scenario() -> list[tuple[str, ...]]:
        clock = FrozenClock(NOW)
        gate = asyncio.Event()
        calls: list[str] = []

        async def slow_positions(context: JobContext) -> None:
            calls.append(context.name)
            await gate.wait()

        jobs = three_jobs(calls)
        jobs[1] = JobSpec("position_cycle", timedelta(seconds=10), slow_positions, priority=0)
        scheduler = Scheduler(jobs, InMemoryJobLeases(clock), owner="w1", clock=clock)
        scheduler_one = Scheduler(
            jobs, InMemoryJobLeases(clock), owner="w2", clock=clock, max_concurrency=1
        )
        started = [await scheduler_one.tick()]
        await asyncio.sleep(0)
        started.append(await scheduler_one.tick())
        gate.set()
        await scheduler_one.drain()
        started.append(await scheduler_one.tick())
        await scheduler_one.drain()
        started.append(await scheduler_one.tick())
        await scheduler_one.drain()
        both = await scheduler.tick()
        await scheduler.drain()
        started.append(both)
        return started

    first, while_busy, second, third, two_slots = asyncio.run(scenario())
    assert first == ("position_cycle",)
    assert while_busy == ()
    assert second == ("candidate_pipeline",)
    assert third == ("universe_sync",)
    assert two_slots == ("position_cycle", "candidate_pipeline")


def test_two_workers_never_run_the_same_job_at_once() -> None:
    async def scenario() -> tuple[tuple[str, ...], tuple[str, ...], int, str]:
        clock = FrozenClock(NOW)
        leases = InMemoryJobLeases(clock)
        gate = asyncio.Event()
        active: list[str] = []

        async def exclusive(context: JobContext) -> None:
            active.append(context.lease.owner)
            assert len(active) == 1
            await gate.wait()
            active.pop()

        job = [JobSpec("universe_sync", timedelta(seconds=60), exclusive)]
        registry = CollectorRegistry()
        first = Scheduler(job, leases, owner="worker-a", clock=clock)
        second = Scheduler(
            job, leases, owner="worker-b", clock=clock, metrics=WorkerMetrics(registry)
        )
        started_a = await first.tick()
        started_b = await second.tick()
        gate.set()
        runs = await first.drain()
        clock.advance(timedelta(seconds=5))
        later_b = await second.tick()
        await second.drain()
        return started_a + started_b, later_b, len(runs), generate_latest(registry).decode()

    started, later, runs, metrics = asyncio.run(scenario())
    assert started == ("universe_sync",)
    assert later == ("universe_sync",)
    assert runs == 1
    assert 'meteora_job_lease_contention_total{job="universe_sync"} 1.0' in metrics


def test_expired_lease_of_a_crashed_worker_is_taken_over() -> None:
    async def scenario() -> tuple[tuple[str, ...], tuple[str, ...]]:
        clock = FrozenClock(NOW)
        leases = InMemoryJobLeases(clock)
        crashed = await leases.acquire("universe_sync", "crashed-worker", timedelta(seconds=30))
        assert crashed is not None
        calls: list[str] = []
        survivor = Scheduler(
            [
                JobSpec(
                    "universe_sync",
                    timedelta(seconds=60),
                    recorder(calls, "universe_sync"),
                    lease_ttl=timedelta(seconds=30),
                )
            ],
            leases,
            owner="survivor",
            clock=clock,
            contention_retry=timedelta(seconds=5),
        )
        blocked = await survivor.tick()
        clock.advance(timedelta(seconds=30))
        recovered = await survivor.tick()
        await survivor.drain()
        assert leases.holder("universe_sync") is None
        return blocked, recovered

    blocked, recovered = asyncio.run(scenario())
    assert blocked == ()
    assert recovered == ("universe_sync",)


def test_failures_timeouts_and_lost_leases_are_isolated_and_recorded() -> None:
    async def scenario() -> dict[str, JobOutcome]:
        clock = FrozenClock(NOW)
        leases = InMemoryJobLeases(clock)

        async def broken(context: JobContext) -> None:
            raise RuntimeError("provider exploded")

        async def hangs(context: JobContext) -> None:
            await asyncio.sleep(10)

        async def outlived(context: JobContext) -> None:
            clock.advance(timedelta(seconds=1))
            await leases.acquire("outlived", "intruder", timedelta(seconds=30))
            await asyncio.sleep(10)

        calls: list[str] = []
        jobs = [
            JobSpec("broken", timedelta(seconds=60), broken, priority=0),
            JobSpec(
                "hangs",
                timedelta(seconds=60),
                hangs,
                priority=1,
                timeout=timedelta(milliseconds=50),
            ),
            JobSpec(
                "outlived",
                timedelta(seconds=60),
                outlived,
                priority=2,
                lease_ttl=timedelta(milliseconds=600),
            ),
            JobSpec("healthy", timedelta(seconds=60), recorder(calls, "healthy"), priority=3),
        ]
        scheduler = Scheduler(jobs, leases, owner="w1", clock=clock, max_concurrency=4)
        await scheduler.tick()
        runs = await scheduler.drain()
        assert calls == ["healthy"]
        assert all(run.correlation_id for run in runs)
        assert leases.holder("broken") is None and leases.holder("hangs") is None
        return {run.name: run.outcome for run in runs}

    outcomes = asyncio.run(scenario())
    assert outcomes == {
        "broken": JobOutcome.FAILED,
        "hangs": JobOutcome.TIMED_OUT,
        "outlived": JobOutcome.LEASE_LOST,
        "healthy": JobOutcome.SUCCEEDED,
    }
