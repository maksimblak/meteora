"""Finding T06: the worker hands every fully watched UTC day to the promotion evidence."""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from prometheus_client import CollectorRegistry

from src.core.clock import FrozenClock
from src.domain.models.trading import TradingMode
from src.modes.service import PromotionEvidenceService
from src.observability.metrics import WorkerMetrics
from src.persistence.contracts import JobLease
from src.persistence.memory import InMemoryEventSink
from src.providers.fixtures import FixtureOnChainProvider
from src.worker.evidence import DailyEvidence
from src.worker.runtime import WorkerRuntime, WorkerSettings
from src.worker.scheduler import JobContext
from tests.worker.harness import Harness, harness_policy, memory_repositories

DAY = datetime(2026, 10, 20, tzinfo=UTC)
CYCLE = timedelta(minutes=10)


def watch(evidence: DailyEvidence, clock: FrozenClock, until: datetime, **decision: bool) -> None:
    while clock.now() < until:
        evidence.cycle(clock.now(), fresh=True, mode=TradingMode.SHADOW)
        if decision:
            evidence.decision(clock.now(), **decision)
        clock.advance(CYCLE)


def evidence_for(harness: Harness) -> DailyEvidence:
    return DailyEvidence(
        modes=harness.modes,
        accounting=harness.accounting,
        portfolio=harness.portfolio,
        clock=harness.clock,
        ruleset_version=harness.policy.ruleset_version,
        cycle_gap=timedelta(minutes=30),
    )


def test_a_fully_watched_day_becomes_one_shadow_observation_on_the_next_day() -> None:
    harness = Harness(clock=FrozenClock(DAY))
    harness.keeper.ensure_open()
    evidence = evidence_for(harness)

    watch(evidence, harness.clock, DAY + timedelta(days=1), candidate=True, covered=True)
    assert harness.clock.now() == DAY + timedelta(days=1)
    recorded = evidence.close_finished_days()

    (observation,) = harness.modes.observations("rules-1", TradingMode.SHADOW)
    assert recorded == (observation,)
    assert observation.risk_date == DAY.date()
    assert observation.decision_count == 144
    assert observation.data_coverage == Decimal(1)
    assert observation.start_equity == Decimal(1_000_000)
    snapshot = PromotionEvidenceService(harness.modes, harness.clock).build_shadow("rules-1")
    assert snapshot.full_utc_days == 1
    # A second close of the same day records nothing twice.
    assert evidence.close_finished_days() == ()


def test_a_day_watched_only_in_part_is_not_a_full_shadow_day() -> None:
    harness = Harness(clock=FrozenClock(DAY + timedelta(hours=12)))
    harness.keeper.ensure_open()
    evidence = evidence_for(harness)

    watch(evidence, harness.clock, DAY + timedelta(days=1))
    evidence.close_finished_days()

    assert harness.modes.observations("rules-1", TradingMode.SHADOW) == ()


def test_coverage_counts_candidates_whose_decision_had_every_signal() -> None:
    harness = Harness(clock=FrozenClock(DAY))
    harness.keeper.ensure_open()
    evidence = evidence_for(harness)
    watch(evidence, harness.clock, DAY + timedelta(hours=12), candidate=True, covered=True)
    watch(evidence, harness.clock, DAY + timedelta(days=1), candidate=True, covered=False)

    (observation,) = evidence.close_finished_days()

    assert observation.data_coverage == Decimal("0.5")


def test_the_worker_records_the_day_on_its_first_cycle_after_midnight() -> None:
    clock = FrozenClock(DAY)
    repos = memory_repositories(clock)
    runtime = WorkerRuntime(
        settings=WorkerSettings(metrics_port=None, owner="worker-1", candidate_interval=CYCLE),
        policy=harness_policy(),
        repositories=repos,
        market=None,
        onchain=FixtureOnChainProvider(token_risks={}, clock=clock),
        clock=clock,
        metrics=WorkerMetrics(CollectorRegistry()),
        sink=InMemoryEventSink(),
    )

    def candidate_cycle() -> None:
        now = clock.now()
        lease = JobLease(
            name="candidate_pipeline", owner="worker-1", token=1, acquired_at=now,
            expires_at=now + timedelta(minutes=2),
        )
        context = JobContext(
            name="candidate_pipeline", correlation_id=uuid4(), started_at=now, lease=lease
        )
        asyncio.run(runtime.candidate_job(context))

    while clock.now() < DAY + timedelta(days=1):
        candidate_cycle()
        clock.advance(CYCLE)
    assert repos.mode.observations("rules-1", TradingMode.SHADOW) == ()

    candidate_cycle()  # 00:00 of the next UTC day

    (observation,) = repos.mode.observations("rules-1", TradingMode.SHADOW)
    assert observation.risk_date == DAY.date()
    # No universe was ever synced: the day had no fresh data, so it covers nothing.
    assert observation.data_coverage == Decimal(0)
