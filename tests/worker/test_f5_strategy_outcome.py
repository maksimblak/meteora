"""F5 (T19 review): a candidate the strategy finds no plan for is not "accepted".

Spec 6.8: below 90% of the hour's high, or where 70 bins cannot reach 70% down, Bid-Ask has
no plan. The candidate's recorded decision says so - REJECT with the strategy's code - and
nothing is planned, sized or audited as a risk rejection.
"""

import asyncio
from decimal import Decimal
from typing import Any

import pytest

from src.domain.models.events import EventType
from src.domain.screening import CandidateStatus, RejectionCode
from src.worker.policy import GuidePolicySettings, guide_policy
from tests.domain.guide_fixtures import guide_history, guide_token
from tests.worker.harness import NOW, POOL, Harness


def evaluate(history: list[Any]) -> tuple[Harness, Any]:
    policy = guide_policy(
        GuidePolicySettings(baseline_points=6, minimum_baseline_points=6, maximum_history_points=40)
    )
    harness = Harness(policy=policy, token_risks={POOL.base_mint.root: guide_token()})
    harness.ingest(history)

    async def run() -> Any:
        harness.keeper.ensure_open()
        return await harness.pipeline.evaluate(POOL.address.root, NOW)

    return harness, asyncio.run(run())


@pytest.mark.parametrize(
    ("history", "code"),
    [
        # The hour's high is 1.25; 1.10 is 88% of it, below the 90% of guide-v1.
        (guide_history(prices=("1", "1", "1.25", "1", "1", "1", "1.1")),
         RejectionCode.NOT_NEAR_LOCAL_HIGH),
        # Bin step 100 bps: 70 bins reach 1 - 1.01^-69 = 49.7% down, short of 70%.
        (guide_history(bin_step=100), RejectionCode.RANGE_TOO_NARROW),
    ],
    ids=["below_the_high", "range_too_narrow"],
)
def test_a_candidate_without_a_plan_is_recorded_as_rejected_with_the_strategy_code(
    history: list[Any], code: RejectionCode
) -> None:
    harness, outcome = evaluate(history)

    decision = harness.decisions.decisions[outcome.decision_id].decision
    assert outcome.status is CandidateStatus.REJECT
    assert decision.status is CandidateStatus.REJECT
    assert code in decision.reasons
    assert outcome.plan_id is None and harness.decisions.plans == {}
    assert harness.modes.open_positions() == ()
    (evaluated,) = [
        event for event in harness.audit.events
        if event.event_type is EventType.CANDIDATE_EVALUATED
    ]
    assert evaluated.payload["status"] == "REJECT"
    assert code.value in evaluated.payload["reasons"]
    assert not [
        event for event in harness.audit.events if event.event_type is EventType.RISK_REJECTED
    ]


def test_the_refused_decision_keeps_its_score_and_shows_the_strategy_evidence() -> None:
    harness, outcome = evaluate(guide_history(prices=("1", "1", "1.25", "1", "1", "1", "1.1")))

    decision = harness.decisions.decisions[outcome.decision_id].decision
    found = {item.name: (item.value, item.threshold) for item in decision.evidence}
    assert decision.score is not None  # the screening passed; the strategy refused
    assert found["strategy"] == ("BID_ASK", None)
    assert found["local_high_price"][0] == "1.25"
    # 1.1 / 1.25 = 0.88 against the 0.90 of guide-v1.
    assert Decimal(found["price_to_local_high"][0]) == Decimal("0.88")
    assert found["price_to_local_high"][1] == "0.90"
