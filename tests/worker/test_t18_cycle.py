"""T18 (G24) in the worker's candidate cycle: one pool per token - the main one, tokens
ranked by their hourly volume with only the top N checked in depth, one position per token.

The worker runs as in production (``WorkerRuntime.candidate_job``) on in-memory stores, the
guide-v1 rules and recorded-style guide snapshots; token risk is a counting fake.
"""

import asyncio
from collections import Counter
from datetime import timedelta
from typing import Any
from uuid import uuid4

from prometheus_client import CollectorRegistry

from src.core.clock import FrozenClock
from src.domain.models.primitives import PublicKey
from src.domain.screening import CandidateStatus, RejectionCode
from src.market.models import PoolRef, PoolSnapshot, TokenRiskSnapshot
from src.modes.models import PendingPlanStatus
from src.observability.metrics import WorkerMetrics
from src.persistence.contracts import DecisionRecord, JobLease
from src.persistence.memory import InMemoryEventSink
from src.providers.fixtures import FixtureOnChainProvider
from src.worker.execution import EntryResult, EntryStatus
from src.worker.policy import GuidePolicySettings, guide_policy
from src.worker.runtime import WorkerRuntime, WorkerSettings
from src.worker.scheduler import JobContext
from tests.domain.guide_fixtures import NOW, POOL, guide_history, guide_token
from tests.gmgn.test_gmgn_openapi import mint_number
from tests.worker.harness import memory_repositories


class CountingOnChain:
    """Token risk per mint, counting every read (each read is a request in production)."""

    def __init__(self, tokens: dict[str, TokenRiskSnapshot], clock: FrozenClock) -> None:
        self.fixture = FixtureOnChainProvider(token_risks=tokens, clock=clock)
        self.reads: Counter[str] = Counter()

    @property
    def health(self) -> Any:
        return self.fixture.health

    async def get_token_risk(self, mint: PublicKey) -> TokenRiskSnapshot:
        self.reads[mint.root] += 1
        return await self.fixture.get_token_risk(mint)

    async def get_wallet_state(self, wallet: PublicKey) -> Any:
        return await self.fixture.get_wallet_state(wallet)


def pool_history(address: PublicKey, mint: PublicKey, **current: Any) -> list[PoolSnapshot]:
    ref = PoolRef(address=address, base_mint=mint, quote_mint=POOL.quote_mint)
    return [item.model_copy(update={"pool": ref}) for item in guide_history(**current)]


class World:
    def __init__(self, pools: list[list[PoolSnapshot]], *, deep: int = 50) -> None:
        self.clock = FrozenClock(NOW)
        self.repos = memory_repositories(self.clock)
        mints = {history[-1].pool.base_mint.root for history in pools}
        self.onchain = CountingOnChain(
            {mint: guide_token().model_copy(update={"mint": PublicKey(mint)}) for mint in mints},
            self.clock,
        )
        policy = guide_policy(
            GuidePolicySettings(
                baseline_points=6,
                minimum_baseline_points=6,
                maximum_history_points=20,
                maximum_deep_candidates=deep,
            )
        )
        self.runtime = WorkerRuntime(
            settings=WorkerSettings(metrics_port=None, owner="worker-1"),
            policy=policy,
            repositories=self.repos,
            market=None,
            onchain=self.onchain,
            clock=self.clock,
            metrics=WorkerMetrics(CollectorRegistry()),
            sink=InMemoryEventSink(),
        )
        asyncio.run(self._ingest([item for history in pools for item in history]))

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


MINT_A = mint_number(1)
P1, P2, P3 = mint_number(11), mint_number(12), mint_number(13)


def three_pools(**main: Any) -> list[list[PoolSnapshot]]:
    """Token A in three pools: 100 / 300 / 300 of LP fees in the hour; P3 has more TVL."""

    return [
        pool_history(P1, MINT_A, hour_fees="100", tvl="150000"),
        pool_history(P2, MINT_A, hour_fees="300", tvl="150000"),
        pool_history(P3, MINT_A, **{"hour_fees": "300", "tvl": "160000", **main}),
    ]


def test_of_three_pools_of_a_token_only_the_main_one_is_evaluated_and_entered() -> None:
    world = World(three_pools())

    world.cycle()

    (main,) = world.records(P3)
    assert main.decision.status is CandidateStatus.ACCEPT, main.decision.reasons
    assert world.records(P1) == [] and world.records(P2) == []
    assert world.onchain.reads == Counter({MINT_A.root: 1})
    assert len(world.repos.mode.open_positions()) == 1
    found = {item.name: item.value for item in main.decision.evidence}
    assert (found["pool_bin_step"], found["pool_base_fee_rate"]) == ("250", "0.01")
    assert found["token_pool_count"] == "3"  # noqa: S105 - an evidence row


def test_a_main_pool_failing_a_cheap_filter_hands_over_to_the_next_one() -> None:
    world = World(three_pools(market_cap="100000"))  # below the 500 000 market-cap minimum

    world.cycle()

    (rejected,) = world.records(P3)
    (main,) = world.records(P2)
    assert RejectionCode.MARKET_CAP_BELOW_THRESHOLD in rejected.decision.reasons
    assert main.decision.status is CandidateStatus.ACCEPT, main.decision.reasons
    assert world.records(P1) == []
    assert world.onchain.reads == Counter({MINT_A.root: 1})


def test_a_main_pool_too_narrow_for_bid_ask_hands_over_to_a_pool_that_holds_the_range() -> None:
    """D10: 70 bins of bin step 100 reach 49.7% down, short of the 70% of Bid-Ask; the
    token's next pool whose bin step holds it is entered instead of missing the token."""

    world = World(
        [
            pool_history(P1, MINT_A, hour_fees="100", tvl="150000"),  # bin step 250
            pool_history(P2, MINT_A, hour_fees="300", tvl="150000", bin_step=80),
            pool_history(P3, MINT_A, hour_fees="300", tvl="160000", bin_step=100),
        ]
    )

    world.cycle()

    (main,) = world.records(P3)
    assert main.decision.reasons == (RejectionCode.RANGE_TOO_NARROW,)
    assert world.records(P2) == []  # bin step 80 cannot hold the range either: not tried
    (entered,) = world.records(P1)
    assert entered.decision.status is CandidateStatus.ACCEPT, entered.decision.reasons
    (position_id,) = world.repos.mode.open_positions()
    context = asyncio.run(world.repos.positions.get(position_id))
    assert context is not None and context.pool_address == P1.root
    assert world.onchain.reads == Counter({MINT_A.root: 2})  # the hand-over is checked in depth


def test_the_hand_over_skips_a_wide_pool_failing_a_cheap_filter_and_records_its_reject() -> None:
    world = World(
        [
            pool_history(P1, MINT_A, hour_fees="100", tvl="150000"),
            # Wide enough, but below the 500 000 market-cap minimum.
            pool_history(P2, MINT_A, hour_fees="300", tvl="150000", market_cap="100000"),
            pool_history(P3, MINT_A, hour_fees="300", tvl="160000", bin_step=100),
        ]
    )

    world.cycle()

    (main,) = world.records(P3)
    (skipped,) = world.records(P2)
    (entered,) = world.records(P1)
    assert main.decision.reasons == (RejectionCode.RANGE_TOO_NARROW,)
    assert RejectionCode.MARKET_CAP_BELOW_THRESHOLD in skipped.decision.reasons
    assert entered.decision.status is CandidateStatus.ACCEPT, entered.decision.reasons
    assert len(world.repos.mode.open_positions()) == 1


def test_no_hand_over_when_no_pool_holds_the_range_or_the_strategy_refused_more() -> None:
    narrow = World(
        [
            pool_history(P1, MINT_A, hour_fees="100", tvl="150000", bin_step=80),
            pool_history(P3, MINT_A, hour_fees="300", tvl="160000", bin_step=100),
        ]
    )
    # Below 90% of the hour's high as well: the token is not "from the high" in any pool.
    below_high = World(
        [
            pool_history(P1, MINT_A, hour_fees="100", tvl="150000"),
            pool_history(
                P3,
                MINT_A,
                prices=("1", "1", "1.25", "1", "1", "1", "1.1"),
                hour_fees="300",
                tvl="160000",
                bin_step=100,
            ),
        ]
    )

    for world in (narrow, below_high):
        world.cycle()

        (main,) = world.records(P3)
        assert RejectionCode.RANGE_TOO_NARROW in main.decision.reasons
        assert world.records(P1) == []
        assert world.onchain.reads == Counter({MINT_A.root: 1})
        assert world.repos.mode.open_positions() == ()


def test_only_the_top_tokens_by_hourly_volume_are_checked_in_depth() -> None:
    hours = ["1400000", "1200000", "1000000", "900000"]
    pools = [mint_number(21 + index) for index in range(4)]
    mints = [mint_number(31 + index) for index in range(4)]
    world = World(
        [
            pool_history(address, mint, hour=hour)
            for address, mint, hour in zip(pools, mints, hours, strict=True)
        ],
        deep=2,
    )

    world.cycle()

    assert world.onchain.reads == Counter({mints[0].root: 1, mints[1].root: 1})
    for address in pools[:2]:
        (record,) = world.records(address)
        assert record.decision.status is CandidateStatus.ACCEPT
    for rank, address in enumerate(pools[2:], start=3):
        (record,) = world.records(address)
        assert record.decision.status is CandidateStatus.REJECT
        assert RejectionCode.CANDIDATE_RANK_BELOW_LIMIT in record.decision.reasons
        found = {item.name: (item.value, item.threshold) for item in record.decision.evidence}
        assert found["token_volume_rank"] == (str(rank), "2")


def test_a_token_held_in_one_pool_is_never_entered_again_through_another() -> None:
    world = World(three_pools())
    world.cycle()
    (position,) = world.repos.mode.open_positions()

    world.clock.advance(timedelta(minutes=1))
    world.cycle()

    # P3 holds the position and is not evaluated; P2 is the next pool of the same token.
    (held,) = world.records(P2)
    assert held.decision.status is CandidateStatus.REJECT
    assert RejectionCode.TOKEN_ALREADY_HELD in held.decision.reasons
    assert world.repos.mode.open_positions() == (position,)
    assert world.onchain.reads == Counter({MINT_A.root: 1})  # no request for a held token
    assert len(world.repos.decisions.plans) == 1


def test_a_token_with_an_entry_still_in_flight_is_held_too() -> None:
    """An admitted plan left PENDING (e.g. awaiting the operator) holds its token."""

    world = World(three_pools())

    async def left_pending(**entry: Any) -> EntryResult:
        return EntryResult(status=EntryStatus.PENDING, plan_id=entry["plan_id"])

    world.runtime.entries.execute = left_pending  # type: ignore[method-assign]
    world.cycle()
    (plan,) = world.repos.mode.entry_plans()
    assert plan.status is PendingPlanStatus.PENDING and world.repos.mode.open_positions() == ()

    world.clock.advance(timedelta(minutes=1))
    world.cycle()

    (held,) = world.records(P2)
    assert RejectionCode.TOKEN_ALREADY_HELD in held.decision.reasons
    assert len(world.repos.decisions.plans) == 1
