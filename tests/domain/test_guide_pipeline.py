"""guide-v1 through the worker's candidate pipeline and position cycle (shadow, no network)."""

import asyncio
from typing import Any

from src.domain.screening import CandidateStatus, DecisionFlag, RejectionCode
from src.domain.strategy import PositionActionKind, StrategyKind, StrategySelectionReason
from src.market.models import TokenRiskSnapshot
from src.providers.fixtures import FixtureOnChainProvider
from src.worker.pipeline import CandidatePipeline
from src.worker.policy import GuidePolicySettings, guide_policy
from tests.domain.guide_fixtures import NOW, POOL, guide_history, guide_token
from tests.worker.harness import Harness


def guide_harness(token: TokenRiskSnapshot) -> tuple[Harness, CandidatePipeline]:
    policy = guide_policy(
        GuidePolicySettings(baseline_points=6, minimum_baseline_points=6, maximum_history_points=20)
    )
    harness = Harness(policy=policy)
    harness.ingest(guide_history())
    pipeline = CandidatePipeline(
        history=harness.history,
        onchain=FixtureOnChainProvider(
            token_risks={POOL.base_mint.root: token}, clock=harness.clock
        ),
        decisions=harness.decisions,
        modes=harness.modes,
        entries=harness.entries,
        portfolio=harness.portfolio,
        audit=harness.audit,
        alerts=harness.alerts,
        policy=policy,
        clock=harness.clock,
        metrics=harness.metrics,
    )
    return harness, pipeline


def evaluate(harness: Harness, pipeline: CandidatePipeline) -> Any:
    async def run() -> Any:
        harness.keeper.ensure_open()
        return await pipeline.evaluate(POOL.address.root, NOW)

    return asyncio.run(run())


def test_guide_v1_records_thresholds_and_plans_bid_ask_from_the_local_high() -> None:
    harness, pipeline = guide_harness(guide_token())

    outcome = evaluate(harness, pipeline)
    record = harness.decisions.decisions[outcome.decision_id]
    plan = harness.decisions.plans[outcome.plan_id].sized_plan.plan

    assert outcome.status is CandidateStatus.ACCEPT
    assert record.decision.ruleset_version == "guide-v1"
    assert record.thresholds.guide is not None
    assert {"volume_regime", "fee_efficiency", "market_cap"} <= {
        item.name for item in record.decision.evidence
    }
    assert plan.strategy is StrategyKind.BID_ASK
    assert plan.selection_reason is StrategySelectionReason.BID_ASK_FROM_LOCAL_HIGH
    assert plan.ruleset_version == "guide-v1"


def test_fresh_bid_ask_position_is_held_not_reduced_on_the_next_cycle() -> None:
    harness, pipeline = guide_harness(guide_token())
    outcome = evaluate(harness, pipeline)
    assert outcome.entry is not None and outcome.entry.position_id is not None

    report = asyncio.run(harness.positions.run_all())

    assert [review.action for review in report.reviews] == [PositionActionKind.HOLD]


def test_missing_total_fees_is_flagged_in_the_record_and_still_planned() -> None:
    harness, pipeline = guide_harness(guide_token(total_fees_sol=None))

    outcome = evaluate(harness, pipeline)
    record = harness.decisions.decisions[outcome.decision_id]

    assert outcome.status is CandidateStatus.ACCEPT
    assert outcome.plan_id is not None
    # G24: without GMGN the token volume is its DLMM pools'; the fixture's 5m window spikes.
    assert set(record.decision.flags) == {
        DecisionFlag.TOTAL_FEES_UNAVAILABLE,
        DecisionFlag.TOKEN_VOLUME_POOL_ONLY,
        DecisionFlag.VOLUME_SPIKE,
    }
    assert RejectionCode.GUIDE_SIGNAL_UNKNOWN not in record.decision.reasons


def test_other_missing_required_signal_still_stops_at_unknown_without_a_plan() -> None:
    harness, pipeline = guide_harness(guide_token(holder_concentration=None))

    outcome = evaluate(harness, pipeline)

    assert outcome.status is CandidateStatus.UNKNOWN
    assert outcome.plan_id is None
