"""The author's wait after an exit in the worker's real cycles: enter, leave on falling
volumes, wait while the volumes stay below the entry's, enter again on new volumes.

``guide_history`` enters MEME at ``NOW`` on 100 000 in the 5m window - 20 000 a minute.
"""

import asyncio
from collections import Counter
from datetime import datetime, timedelta
from uuid import uuid4

from src.domain.screening import CandidateStatus, RejectionCode
from src.market.models import PoolSnapshot
from src.persistence.contracts import DecisionRecord, JobLease
from src.worker.scheduler import JobContext
from tests.domain.guide_fixtures import NOW, guide_snapshot
from tests.worker.test_t18_cycle import MINT_A, P1, World, pool_history


def later(at: datetime, *, short: str, price: str = "1") -> PoolSnapshot:
    snapshot = guide_snapshot(at, short=short, price=price)
    return snapshot.model_copy(update={"pool": pool_history(P1, MINT_A)[-1].pool})


def run_positions(world: World) -> None:
    now = world.clock.now()
    lease = JobLease(
        name="position_cycle",
        owner="worker-1",
        token=1,
        acquired_at=now,
        expires_at=now + timedelta(minutes=2),
    )
    context = JobContext(name="position_cycle", correlation_id=uuid4(), started_at=now, lease=lease)
    asyncio.run(world.runtime.position_job(context))


def decision_at(world: World, at: datetime) -> DecisionRecord:
    (record,) = [item for item in world.records(P1) if item.decision.decision_at == at]
    return record


def step(world: World, minutes: int, *, short: str, price: str = "1") -> datetime:
    at = NOW + timedelta(minutes=minutes)
    world.clock.advance(at - world.clock.now())
    asyncio.run(world._ingest([later(at, short=short, price=price)]))
    return at


def test_after_an_exit_the_token_waits_for_new_volumes_and_then_enters_again() -> None:
    world = World([pool_history(P1, MINT_A)])
    world.cycle()
    (position,) = world.repos.mode.open_positions()

    # 40 000 in 5m is below half of the entry's 100 000: the position leaves (VOLUME_DECAY).
    step(world, 5, short="40000")
    run_positions(world)
    assert world.repos.mode.open_positions() == ()

    # 90 000 in 5m - 18 000 a minute - is above the guide minimum but below the entry's.
    waited = step(world, 10, short="90000")
    reads = Counter(world.onchain.reads)
    world.cycle()
    record = decision_at(world, waited)
    assert record.decision.status is CandidateStatus.REJECT
    assert RejectionCode.NO_NEW_VOLUME_OR_HIGH in record.decision.reasons
    found = {item.name: (item.value, item.threshold) for item in record.decision.evidence}
    assert found["reentry_volume_per_minute"] == ("18000", ">20000 (DLMM_POOLS)")
    assert found["reentry_price"] == ("1", ">1")  # the entry snapshot's price is the high
    assert world.onchain.reads == reads  # a cheap filter: the wait costs no request

    # 110 000 in 5m - 22 000 a minute - is more than at the entry: new volumes.
    entered = step(world, 15, short="110000")
    world.cycle()
    assert decision_at(world, entered).decision.status is CandidateStatus.ACCEPT
    (again,) = world.repos.mode.open_positions()
    assert again != position


def test_a_new_high_since_the_entry_opens_the_token_without_new_volumes() -> None:
    world = World([pool_history(P1, MINT_A)])
    world.cycle()
    step(world, 5, short="40000")
    run_positions(world)
    assert world.repos.mode.open_positions() == ()

    # Volumes still below the entry's, but the price 1.05 is above every price since it.
    high = step(world, 10, short="90000", price="1.05")
    world.cycle()

    assert decision_at(world, high).decision.status is CandidateStatus.ACCEPT
    assert len(world.repos.mode.open_positions()) == 1
