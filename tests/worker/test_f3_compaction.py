"""F3 (C1, R29i.2): records grow with changes, not with pools x cycles.

A pool whose outcome did not change since its last decision gets no new decision record,
no ``CandidateEvaluated`` audit event and no outbox row; the panel still sees it as
evaluated in the current cycle.
"""

import asyncio
from datetime import timedelta
from typing import Any

from src.domain.models.events import EventType
from src.domain.screening import CandidateStatus
from src.market.models import PoolRef
from tests.api.support import POOL_B, POOL_C
from tests.worker.harness import NOW, POOL, Harness, snapshot, spike_history

CYCLE = timedelta(seconds=5)


def ingest(harness: Harness, pools: list[PoolRef], *, tvl: str = "999") -> None:
    history = spike_history(tvl=tvl)
    harness.ingest([item.model_copy(update={"pool": pool}) for pool in pools for item in history])


def cycles(harness: Harness, pools: list[PoolRef], count: int) -> list[Any]:
    async def run() -> list[Any]:
        harness.keeper.ensure_open()
        outcomes = []
        for index in range(count):
            at = NOW + CYCLE * index
            for pool in pools:
                outcomes.append(await harness.pipeline.evaluate(pool.address.root, at))
        return outcomes

    return asyncio.run(run())


def evaluated_events(harness: Harness) -> int:
    events = harness.audit.events
    return sum(1 for item in events if item.event_type is EventType.CANDIDATE_EVALUATED)


def test_ten_cycles_of_an_unchanged_universe_add_at_most_one_record_per_pool() -> None:
    harness = Harness()
    pools = [POOL, POOL_B, POOL_C]
    ingest(harness, pools)

    outcomes = cycles(harness, pools, 10)

    assert {item.status for item in outcomes} == {CandidateStatus.REJECT}
    assert len(harness.decisions.decisions) <= len(pools)
    assert evaluated_events(harness) <= len(pools)
    # Every cycle still names the decision that stands for the pool.
    by_pool = {}
    for item in outcomes:
        by_pool.setdefault(item.pool_address, set()).add(item.decision_id)
    assert all(len(ids) == 1 for ids in by_pool.values())
    assert all(next(iter(ids)) in harness.decisions.decisions for ids in by_pool.values())


def test_a_changed_outcome_is_recorded_and_audited_again() -> None:
    harness = Harness()
    ingest(harness, [POOL])
    (first,) = cycles(harness, [POOL], 1)
    # The pool's TVL recovers: the reasons change, so does the outcome.
    harness.ingest([snapshot(NOW + timedelta(minutes=1), tvl="5000")])
    harness.clock.advance(timedelta(minutes=1))

    async def later() -> Any:
        return await harness.pipeline.evaluate(POOL.address.root, NOW + timedelta(minutes=1))

    second = asyncio.run(later())

    assert first.decision_id != second.decision_id
    assert len(harness.decisions.decisions) == 2
    assert evaluated_events(harness) == 2


def test_an_accepted_candidate_is_always_recorded_before_its_money_step() -> None:
    harness = Harness()
    harness.ingest(spike_history())

    (accepted,) = cycles(harness, [POOL], 1)

    assert accepted.status is CandidateStatus.ACCEPT
    assert accepted.decision_id in harness.decisions.decisions
    assert evaluated_events(harness) == 1
