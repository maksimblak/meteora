"""Periodic recovery running in parallel with a live entry must not touch that entry."""

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from src.accounting import InMemoryAccountingRepository
from src.core.clock import FrozenClock
from src.core.config import AppConfig
from src.modes import InMemoryModeRepository, PendingPlanStatus
from src.persistence.factory import open_repositories
from src.persistence.memory import InMemoryJobLeases, InMemoryPositionContexts
from src.persistence.testing import integration_database_url, migrated_schema
from src.portfolio.models import PositionStatus
from src.recovery.service import ExecutorLease, RecoveryAction, RecoveryReport, RecoveryService
from src.worker.execution import EntryStatus, ShadowSubmitter
from tests.worker.harness import NOW, POOL, Harness, spike_history

CANDIDATE_JOB = "candidate_pipeline"
TTL = timedelta(minutes=2)


@dataclass
class Stores:
    modes: Any
    accounting: Any
    contexts: Any
    leases: Any


@pytest.fixture(params=["memory", "sqlite", "postgres"])
def stores(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[tuple[Stores, FrozenClock]]:
    clock = FrozenClock(NOW)
    if request.param == "memory":
        yield (
            Stores(
                InMemoryModeRepository(),
                InMemoryAccountingRepository(),
                InMemoryPositionContexts(),
                InMemoryJobLeases(clock),
            ),
            clock,
        )
        return
    if request.param == "sqlite":
        repositories = open_repositories(
            AppConfig.load(environ={"APP_ENV": "development"}),
            environ={"LOCAL_SQLITE_PATH": str(tmp_path / "race.sqlite3")},
            clock=clock,
        )
        try:
            yield (
                Stores(
                    repositories.mode,
                    repositories.accounting,
                    repositories.positions,
                    repositories.leases,
                ),
                clock,
            )
        finally:
            repositories.close()
        return
    url = integration_database_url()
    if url is None:
        pytest.skip("PostgreSQL integration URL is not configured")
    with migrated_schema(url) as schema:
        repositories = open_repositories(
            AppConfig.load(environ={"DATABASE_URL": url}),
            environ={},
            clock=clock,
            schema=schema.schema,
        )
        try:
            yield (
                Stores(
                    repositories.mode,
                    repositories.accounting,
                    repositories.positions,
                    repositories.leases,
                ),
                clock,
            )
        finally:
            repositories.close()


@pytest.mark.parametrize("guard", ["executor_lease", "claim_age"])
def test_periodic_recovery_in_parallel_with_an_entry_leaves_it_to_its_executor(
    stores: tuple[Stores, FrozenClock], guard: str
) -> None:
    store, clock = stores
    reports: list[RecoveryReport] = []
    recovery: list[RecoveryService] = []

    class ReconcileDuringFill:
        """Recovery (another job or process) runs between claim_entry and the fill."""

        def __init__(self, inner: ShadowSubmitter) -> None:
            self._inner = inner

        async def submit(self, command: Any, market: Any, guard_call: Any) -> Any:
            reports.append(await recovery[0].reconcile())
            return await self._inner.submit(command, market, guard_call)

    harness = Harness(
        clock=clock,
        modes=store.modes,
        accounting=store.accounting,
        contexts=store.contexts,
        wrap_shadow_submitter=ReconcileDuringFill,
    )
    recovery.append(
        RecoveryService(
            modes=store.modes,
            plans=store.modes,
            accounting=store.accounting,
            alerts=harness.alerts,
            clock=clock,
            metrics=harness.metrics,
            executor_lease=ExecutorLease(store.leases, CANDIDATE_JOB, "worker-b:recovery", TTL)
            if guard == "executor_lease"
            else None,
            stale_after=TTL,
        )
    )
    harness.ingest(spike_history())

    async def enter_while_holding_the_candidate_lease() -> Any:
        lease = await store.leases.acquire(CANDIDATE_JOB, "worker-a", TTL)
        assert lease is not None
        try:
            harness.keeper.ensure_open()
            return await harness.pipeline.evaluate(POOL.address.root, NOW)
        finally:
            await store.leases.release(lease)

    outcome = asyncio.run(enter_while_holding_the_candidate_lease())

    (report,) = reports
    assert [item.action for item in report.entries] == [RecoveryAction.KEPT_EXECUTING]
    assert outcome.entry is not None and outcome.entry.status is EntryStatus.OPENED
    position_id = outcome.entry.position_id
    assert store.modes.open_positions() == (position_id,)
    assert store.modes.entry_plan(outcome.plan_id).status is PendingPlanStatus.OPENED
    open_in_accounting = [
        item.position_id
        for item in store.accounting.positions()
        if item.status is PositionStatus.OPEN
    ]
    assert open_in_accounting == [position_id]

    clock.advance(TTL)
    later = asyncio.run(recovery[0].reconcile())
    assert later.entries == ()
    assert store.modes.open_positions() == (position_id,)


def test_an_abandoned_claim_is_settled_once_older_than_the_lease_ttl() -> None:
    clock = FrozenClock(NOW)
    harness = Harness(clock=clock)
    harness.ingest(spike_history())

    class DiesBeforeFill:
        def __init__(self, inner: ShadowSubmitter) -> None:
            del inner

        async def submit(self, command: Any, market: Any, guard_call: Any) -> Any:
            raise RuntimeError("worker died before the fill")

    dying = Harness(
        clock=clock,
        modes=harness.modes,
        accounting=harness.accounting,
        contexts=harness.contexts,
        universe=harness.universe,
        wrap_shadow_submitter=DiesBeforeFill,
    )
    dying.keeper.ensure_open()
    outcome = asyncio.run(dying.pipeline.evaluate(POOL.address.root, NOW))
    recovery = RecoveryService(
        modes=harness.modes,
        plans=harness.modes,
        accounting=harness.accounting,
        alerts=harness.alerts,
        clock=clock,
        metrics=harness.metrics,
        stale_after=TTL,
    )

    young = asyncio.run(recovery.reconcile())
    clock.advance(TTL)
    stale = asyncio.run(recovery.reconcile())

    assert [item.action for item in young.entries] == [RecoveryAction.KEPT_EXECUTING]
    assert [item.action for item in stale.entries] == [RecoveryAction.FAILED_ENTRY]
    assert harness.modes.entry_plan(outcome.plan_id).status is PendingPlanStatus.FAILED
    assert harness.accounting.positions() == ()
