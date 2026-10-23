"""D07 candidate pipeline: quote depth, DEPTH_UNAVAILABLE by mode, cheap filters first."""

import asyncio
from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from src.domain.models.trading import TradingMode
from src.domain.screening import CandidateStatus, DecisionFlag, DepthSource, RejectionCode
from src.domain.strategy import PositionActionKind, PositionReason
from src.execution.contracts import QuoteDirection, QuoteRequestV1, QuoteResultV1
from src.market.models import PoolSnapshot
from src.modes.models import ModeTransition
from src.persistence.contracts import PlanRecord
from src.worker.policy import GuidePolicySettings, guide_policy
from tests.domain.guide_fixtures import guide_history, guide_snapshot, guide_token
from tests.worker.harness import NOW, POOL, Harness, safe_token, snapshot, spike_history


def live(history: list[PoolSnapshot]) -> list[PoolSnapshot]:
    """The same pool as the Data API reports it: no depth, quote mint decimals known."""

    rebuilt = []
    for item in history:
        data = item.model_dump()
        data.update(depth=None, base_decimals=9, quote_decimals=9)
        rebuilt.append(PoolSnapshot.model_validate(data))
    return rebuilt


class Quotes:
    def __init__(self, *, impact_bps: int = 20, fail: Exception | None = None) -> None:
        self.impact_bps = impact_bps
        self.fail = fail
        self.requests: list[QuoteRequestV1] = []

    async def quote(self, request: QuoteRequestV1) -> QuoteResultV1:
        self.requests.append(request)
        if self.fail is not None:
            raise self.fail
        return QuoteResultV1(
            pool_address=request.pool_address,
            direction=request.direction,
            in_amount=request.in_amount,
            quoted_at=NOW,
            success=True,
            consumed_in_amount=request.in_amount,
            out_amount="1",
            fee_amount="1",
            price_impact_bps=self.impact_bps,
            active_bin_id=1,
            token_x_decimals=9,
            token_y_decimals=9,
            error=None,
        )


class QuoteRefused(RuntimeError):
    code = "EXECUTOR_UNREACHABLE"


def evaluate(harness: Harness) -> Any:
    async def run() -> Any:
        harness.keeper.ensure_open()
        return await harness.pipeline.evaluate(POOL.address.root, NOW)

    outcome = asyncio.run(run())
    return outcome, harness.decisions.decisions[outcome.decision_id].decision


def test_a_candidate_is_quoted_for_the_reference_size_and_the_depth_is_recorded() -> None:
    quotes = Quotes(impact_bps=20)
    harness = Harness(quotes=quotes)
    harness.ingest(live(spike_history()))

    outcome, decision = evaluate(harness)

    (request,) = quotes.requests
    # 10 quote units of a 9-decimal quote token, spent on the base token.
    assert (request.direction, request.in_amount) == (QuoteDirection.Y_TO_X, "10000000000")
    assert decision.depth is not None and decision.depth.source is DepthSource.QUOTE
    assert decision.depth.price_impact == Decimal("0.002")
    assert decision.depth.depth == Decimal("1000")  # 10 / 0.002 = 5000, capped at the TVL
    assert decision.estimated_price_impact == Decimal("0.002")
    assert outcome.status is CandidateStatus.ACCEPT
    assert DecisionFlag.DEPTH_UNAVAILABLE not in decision.flags


@pytest.mark.parametrize("quotes", [None, Quotes(fail=QuoteRefused())], ids=["none", "refused"])
def test_without_a_quote_shadow_enters_on_the_conservative_model(quotes: Quotes | None) -> None:
    harness = Harness(quotes=quotes)
    harness.ingest(live(spike_history()))

    outcome, decision = evaluate(harness)

    assert outcome.status is CandidateStatus.ACCEPT
    assert DecisionFlag.DEPTH_UNAVAILABLE in decision.flags
    assert decision.depth is not None and decision.depth.source is DepthSource.UNAVAILABLE
    assert decision.depth.detail == (
        "QUOTES_NOT_CONFIGURED" if quotes is None else "EXECUTOR_UNREACHABLE"
    )
    assert decision.estimated_price_impact == harness.policy.screening.maximum_price_impact
    assert outcome.entry is not None and outcome.entry.position_id is not None
    position = harness.accounting.position_for(outcome.entry.position_id)
    # The fill paid the whole slippage tolerance, not the modeled shadow slippage.
    expected = position.open_cost_basis_base_units * Decimal(
        harness.policy.conservative_costs.slippage_bps
    ) / Decimal(10_000)
    assert position.slippage_base_units == expected


def test_without_a_quote_an_approval_entry_is_closed() -> None:
    harness = Harness()
    state = harness.modes.mode_state()
    harness.modes.commit_transition(
        ModeTransition(
            transition_id=uuid4(),
            from_mode=state.mode,
            to_mode=TradingMode.APPROVAL,
            occurred_at=NOW,
            operator_confirmation_id="operator",
            promotion_authorized=True,
        ),
        expected_version=state.version,
        live_promotion_id=uuid4(),
    )
    harness.ingest(live(spike_history()))

    outcome, decision = evaluate(harness)

    assert outcome.status is CandidateStatus.REJECT
    assert RejectionCode.DEPTH_UNAVAILABLE in decision.reasons
    assert outcome.plan_id is None


def test_a_pool_failing_a_cheap_filter_costs_no_token_read_and_no_quote() -> None:
    class CountingOnChain:
        def __init__(self) -> None:
            self.reads = 0

        async def get_token_risk(self, mint: Any) -> Any:
            self.reads += 1
            return safe_token()

    quotes = Quotes()
    harness = Harness(quotes=quotes)
    counting = CountingOnChain()
    harness.pipeline._onchain = counting  # type: ignore[assignment]
    harness.ingest(live(spike_history(tvl="999")))

    outcome, decision = evaluate(harness)

    assert outcome.status is CandidateStatus.REJECT
    assert RejectionCode.LOW_TVL in decision.reasons
    assert counting.reads == 0 and quotes.requests == []


def test_a_token_read_finishing_after_the_cycle_instant_is_not_future_data() -> None:
    late = safe_token(NOW + timedelta(seconds=3))
    harness = Harness(token_risks={POOL.base_mint.root: late})
    harness.ingest(spike_history())

    outcome, decision = evaluate(harness)

    assert RejectionCode.FUTURE_TOKEN_RISK_DATA not in decision.reasons
    assert outcome.status is CandidateStatus.ACCEPT
    assert decision.decision_at == NOW + timedelta(seconds=3)


def test_a_bid_only_position_rides_upticks_and_leaves_only_when_the_price_stays_above() -> None:
    """Seam plan -> position context -> PositionMonitor for the guide-v1 Bid-Ask (D08)."""

    policy = guide_policy(
        GuidePolicySettings(baseline_points=6, minimum_baseline_points=6, maximum_history_points=40)
    )
    harness = Harness(policy=policy, token_risks={POOL.base_mint.root: guide_token()})
    harness.ingest(guide_history())
    outcome, _ = evaluate(harness)
    assert outcome.entry is not None and outcome.entry.position_id is not None
    plan = harness.decisions.plans[outcome.plan_id].sized_plan.plan
    assert plan.upper_bin_offset == 0  # bin step 250 bps: the entry bin ends at 1.025

    def review(minutes: int, price: str) -> Any:
        harness.clock.advance(NOW + timedelta(minutes=minutes) - harness.clock.now())
        harness.ingest([guide_snapshot(harness.clock.now(), price=price)])
        (item,) = asyncio.run(harness.positions.run_all()).reviews
        return item

    tick = review(1, "1.00001")
    one_bin = review(2, "1.026")
    holding = [review(minutes, "1.026") for minutes in (7, 12, 17, 22, 27)]
    stayed = review(32, "1.026")

    assert tick.action is PositionActionKind.HOLD
    assert one_bin.action is PositionActionKind.HOLD
    assert {item.action for item in holding} == {PositionActionKind.HOLD}
    assert stayed.action is PositionActionKind.EXIT
    assert PositionReason.RANGE_BREACH.value in stayed.reasons
    assert harness.modes.open_positions() == ()


def test_the_position_exits_by_the_decay_ratio_of_its_own_plan() -> None:
    harness = Harness()
    harness.ingest(spike_history())
    outcome, _ = evaluate(harness)
    assert outcome.entry is not None and outcome.entry.position_id is not None
    # The plan asked for an exit at the first drop (an overvolume Spot's ratio 1).
    record = harness.decisions.plans[outcome.plan_id]
    plan = record.sized_plan.plan
    invalidation = plan.invalidation.model_copy(update={"volume_decay_ratio": Decimal(1)})
    first_drop = plan.model_copy(update={"invalidation": invalidation})
    harness.decisions.plans[outcome.plan_id] = PlanRecord.model_validate(
        {
            **record.model_dump(),
            "sized_plan": record.sized_plan.model_copy(update={"plan": first_drop}),
        }
    )
    harness.clock.advance(timedelta(minutes=1))
    harness.ingest([snapshot(harness.clock.now(), short="390", long="1200")])

    report = asyncio.run(harness.positions.run_all())

    (review,) = report.reviews
    assert review.action is PositionActionKind.EXIT
    assert PositionReason.VOLUME_DECAY.value in review.reasons
