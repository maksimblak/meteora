"""F5 (T18 review): one position per token cannot be bypassed through a pool where the coin
stands second (SOL-MEME next to MEME-SOL).

The worker runs its real candidate cycle (``WorkerRuntime.candidate_job``) on in-memory
stores. guide-v1 skips SOL-MEME with its reason and holds it with the coin; the legacy
ruleset, which may still enter such a pool, holds the coin's other pools after it.
"""

import asyncio
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from prometheus_client import CollectorRegistry

from src.core.clock import FrozenClock
from src.domain.models.primitives import PublicKey
from src.domain.screening import CandidateStatus, RejectionCode
from src.market.models import PoolRef, PoolSnapshot
from src.observability.metrics import WorkerMetrics
from src.persistence.contracts import DecisionRecord, JobLease
from src.persistence.memory import InMemoryEventSink
from src.providers.fixtures import FixtureOnChainProvider
from src.worker.execution import EntryResult, EntryStatus
from src.worker.runtime import WorkerRuntime, WorkerSettings
from src.worker.scheduler import JobContext
from tests.domain.guide_fixtures import guide_history
from tests.worker.harness import (
    NOW,
    harness_policy,
    memory_repositories,
    safe_token,
    spike_history,
)
from tests.worker.test_t18_cycle import World

WSOL = PublicKey("So11111111111111111111111111111111111111112")
MEME = PublicKey("11111111111111111111111111111111")
# The address order breaks the tie of two identical pools: SOL-MEME is tried first.
SOL_MEME = PublicKey("SysvarC1ock11111111111111111111111111111111")
MEME_SOL = PublicKey("Vote111111111111111111111111111111111111111")


def repooled(history: list[PoolSnapshot], address: PublicKey, x: PublicKey, y: PublicKey) -> Any:
    ref = PoolRef(address=address, base_mint=x, quote_mint=y)
    return [item.model_copy(update={"pool": ref}) for item in history]


def latest(records: list[DecisionRecord]) -> DecisionRecord:
    return max(records, key=lambda record: record.decision.decision_at)


def test_guide_v1_enters_the_coin_first_pool_and_holds_the_sol_meme_pool_with_it() -> None:
    world = World(
        [
            # SOL-MEME took more LP fees in the hour (300 > 100): it is the main pool.
            repooled(guide_history(hour_fees="300"), SOL_MEME, WSOL, MEME),
            repooled(guide_history(hour_fees="100"), MEME_SOL, MEME, WSOL),
        ]
    )

    world.cycle()

    (skipped,) = world.records(SOL_MEME)
    (entered,) = world.records(MEME_SOL)
    assert skipped.decision.status is CandidateStatus.REJECT
    assert RejectionCode.TOKEN_SECOND_IN_PAIR in skipped.decision.reasons
    assert entered.decision.status is CandidateStatus.ACCEPT, entered.decision.reasons
    (position,) = world.repos.mode.open_positions()

    world.clock.advance(timedelta(minutes=1))
    world.cycle()

    held = latest(world.records(SOL_MEME))
    assert RejectionCode.TOKEN_ALREADY_HELD in held.decision.reasons
    assert world.repos.mode.open_positions() == (position,)
    assert len(world.repos.decisions.plans) == 1


class LegacyWorld:
    """The legacy ruleset (``shadow-hypothesis-v1``) on two pools of MEME, SOL-MEME first."""

    def __init__(self) -> None:
        self.clock = FrozenClock(NOW)
        self.repos = memory_repositories(self.clock)
        tokens = {
            mint.root: safe_token().model_copy(update={"mint": mint}) for mint in (WSOL, MEME)
        }
        self.runtime = WorkerRuntime(
            settings=WorkerSettings(metrics_port=None, owner="worker-1"),
            policy=harness_policy(),
            repositories=self.repos,
            market=None,
            onchain=FixtureOnChainProvider(token_risks=tokens, clock=self.clock),
            clock=self.clock,
            metrics=WorkerMetrics(CollectorRegistry()),
            sink=InMemoryEventSink(),
        )
        pools = [
            *repooled(spike_history(), SOL_MEME, WSOL, MEME),
            *repooled(spike_history(), MEME_SOL, MEME, WSOL),
        ]
        asyncio.run(self._ingest(pools))

    async def _ingest(self, snapshots: list[PoolSnapshot]) -> None:
        for at in sorted({item.observed_at for item in snapshots}):
            stage = await self.repos.universe.begin(uuid4(), at)
            await stage.stage(tuple(item for item in snapshots if item.observed_at == at))
            await stage.commit()

    def cycle(self) -> None:
        now = self.clock.now()
        lease = JobLease(
            name="candidate_pipeline",
            owner="worker-1",
            token=1,
            acquired_at=now,
            expires_at=now + timedelta(minutes=2),
        )
        context = JobContext(
            name="candidate_pipeline", correlation_id=uuid4(), started_at=now, lease=lease
        )
        asyncio.run(self.runtime.candidate_job(context))

    def records(self, address: PublicKey) -> list[DecisionRecord]:
        return [
            record
            for record in self.repos.decisions.decisions.values()
            if record.decision.pool_address == address
        ]


@pytest.mark.parametrize("entry", ["open_position", "plan_in_flight"])
def test_an_entry_in_a_sol_meme_pool_holds_meme_in_its_meme_sol_pool(entry: str) -> None:
    world = LegacyWorld()
    if entry == "plan_in_flight":

        async def left_pending(**request: Any) -> EntryResult:
            return EntryResult(status=EntryStatus.PENDING, plan_id=request["plan_id"])

        world.runtime.entries.execute = left_pending  # type: ignore[method-assign]
    world.cycle()
    (first,) = world.records(SOL_MEME)
    assert first.decision.status is CandidateStatus.ACCEPT, first.decision.reasons
    assert world.records(MEME_SOL) == []  # the coin's other pool waited
    assert len(world.repos.decisions.plans) == 1

    world.clock.advance(timedelta(minutes=1))
    world.cycle()

    (other,) = world.records(MEME_SOL)
    assert RejectionCode.TOKEN_ALREADY_HELD in other.decision.reasons
    assert len(world.repos.decisions.plans) == 1
