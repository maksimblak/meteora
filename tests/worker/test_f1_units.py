"""A3: reference size, depth, liquidity and the depth/TVL thresholds share one unit (USD).

On a SOL-quoted pool the reference quote is the USD reference size converted by the quote
token's USD price and decimals, the depth is USD like ``minimum_depth``, and the liquidity
caps of the sizer are SOL base units by SOL's own decimals - not a USDC scale.
"""

import asyncio
from decimal import Decimal
from typing import Any
from uuid import uuid4

from src.domain.models.trading import TradingMode
from src.domain.screening import CandidateStatus, DecisionFlag, DepthSource, RejectionCode
from src.execution.contracts import QuoteDirection, QuoteRequestV1, QuoteResultV1
from src.market.models import PoolSnapshot
from src.modes.models import ModeTransition
from tests.worker.harness import NOW, POOL, Harness, spike_history

SOL_DECIMALS = 9
USDC_DECIMALS = 6


def quoted(
    history: list[PoolSnapshot], *, decimals: int, price: str | None
) -> list[PoolSnapshot]:
    """The pool as the Data API reports it: no depth, the quote token's decimals and price."""

    rebuilt = []
    for item in history:
        data = item.model_dump()
        data.update(
            depth=None,
            base_decimals=6,
            quote_decimals=decimals,
            quote_price_usd=None if price is None else Decimal(price),
        )
        rebuilt.append(PoolSnapshot.model_validate(data))
    return rebuilt


class Quotes:
    def __init__(self, impact_bps: int = 20) -> None:
        self.impact_bps = impact_bps
        self.requests: list[QuoteRequestV1] = []

    async def quote(self, request: QuoteRequestV1) -> QuoteResultV1:
        self.requests.append(request)
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
            token_x_decimals=6,
            token_y_decimals=9,
            error=None,
        )


def evaluate(harness: Harness) -> Any:
    async def run() -> Any:
        harness.keeper.ensure_open()
        return await harness.pipeline.evaluate(POOL.address.root, NOW)

    outcome = asyncio.run(run())
    return outcome, harness.decisions.decisions[outcome.decision_id].decision


def test_a_sol_pool_is_quoted_for_the_usd_reference_size_and_measured_in_usd() -> None:
    quotes = Quotes(impact_bps=20)
    harness = Harness(quotes=quotes)
    # The harness reference size is 10 USD; SOL at 150 USD; the pool's TVL is 1 000 USD.
    harness.ingest(quoted(spike_history(), decimals=SOL_DECIMALS, price="150"))

    outcome, decision = evaluate(harness)

    (request,) = quotes.requests
    # 10 USD / 150 USD per SOL = 0.0666... SOL = 66 666 666 lamports (rounded down).
    assert (request.direction, request.in_amount) == (QuoteDirection.Y_TO_X, "66666666")
    depth = decision.depth
    assert depth is not None and depth.source is DepthSource.QUOTE
    # 10 USD at 0.2% impact is 5 000 USD of depth, capped at the 1 000 USD TVL.
    assert depth.depth == Decimal("1000")
    assert depth.reference_notional == Decimal("10")
    assert depth.quote_price_usd == Decimal("150")
    assert RejectionCode.LOW_DEPTH not in decision.reasons
    assert outcome.status is CandidateStatus.ACCEPT
    basis = harness.decisions.plans[outcome.plan_id].sized_plan.basis
    # 1 000 USD of depth is 6.666... SOL; the 2% impact cap of it is 20 USD = 0.1333... SOL.
    assert basis.depth_base_units == 6_666_666_666
    assert basis.price_impact_cap_base_units == 133_333_333


def test_a_usdc_pool_keeps_the_usd_reference_size_in_usdc_units() -> None:
    quotes = Quotes(impact_bps=20)
    harness = Harness(quotes=quotes)
    harness.ingest(quoted(spike_history(), decimals=USDC_DECIMALS, price="1"))

    outcome, decision = evaluate(harness)

    (request,) = quotes.requests
    assert request.in_amount == "10000000"  # 10 USDC
    assert decision.depth is not None and decision.depth.depth == Decimal("1000")
    basis = harness.decisions.plans[outcome.plan_id].sized_plan.basis
    assert basis.depth_base_units == 1_000_000_000  # 1 000 USDC
    assert basis.price_impact_cap_base_units == 20_000_000  # 2% of it: 20 USDC


def test_the_conservative_shadow_model_sizes_by_the_quote_token_too() -> None:
    harness = Harness()  # no quotes: DEPTH_UNAVAILABLE, 5% of TVL at the maximum impact
    harness.ingest(quoted(spike_history(), decimals=SOL_DECIMALS, price="150"))

    outcome, decision = evaluate(harness)

    assert DecisionFlag.DEPTH_UNAVAILABLE in decision.flags
    basis = harness.decisions.plans[outcome.plan_id].sized_plan.basis
    # 5% of 1 000 USD = 50 USD = 0.333... SOL; 2% of it = 1 USD = 0.00666... SOL.
    assert basis.depth_base_units == 333_333_333
    assert basis.price_impact_cap_base_units == 6_666_666


def test_without_the_quote_token_price_nothing_is_quoted_and_nothing_is_sized() -> None:
    quotes = Quotes()
    harness = Harness(quotes=quotes)
    harness.ingest(quoted(spike_history(), decimals=SOL_DECIMALS, price=None))

    outcome, decision = evaluate(harness)

    assert quotes.requests == []
    assert decision.depth is not None and decision.depth.source is DepthSource.UNAVAILABLE
    assert decision.depth.detail == "QUOTE_PRICE_UNKNOWN"
    # SHADOW records the candidate, but no USD figure can become SOL base units: no entry.
    assert outcome.entry is None
    assert harness.modes.open_positions() == ()


def test_without_the_quote_token_price_an_approval_entry_is_closed() -> None:
    harness = Harness(quotes=Quotes())
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
    harness.ingest(quoted(spike_history(), decimals=SOL_DECIMALS, price=None))

    outcome, decision = evaluate(harness)

    assert outcome.status is CandidateStatus.REJECT
    assert RejectionCode.DEPTH_UNAVAILABLE in decision.reasons
