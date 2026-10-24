"""B7 through the worker: a SHADOW exit books the LP fees the position earned while held."""

import asyncio
from datetime import timedelta
from decimal import Decimal
from typing import Any

from src.domain.strategy import PositionActionKind
from tests.worker.harness import NOW, POOL, Harness, snapshot, spike_history


def test_a_shadow_position_closed_by_the_cycle_books_its_share_of_the_pool_fees() -> None:
    harness = Harness()
    harness.ingest(spike_history())

    async def enter() -> Any:
        harness.keeper.ensure_open()
        return await harness.pipeline.evaluate(POOL.address.root, NOW)

    outcome = asyncio.run(enter())
    assert outcome.entry is not None and outcome.entry.position_id is not None
    position = harness.accounting.position_for(outcome.entry.position_id)
    invested = position.open_cost_basis_base_units
    # Ten minutes later the volume decayed at the same price: the cycle exits.
    harness.clock.advance(timedelta(minutes=10))
    harness.ingest([snapshot(harness.clock.now(), short="50", long="1200")])

    report = asyncio.run(harness.positions.run_all())

    (review,) = report.reviews
    assert review.action is PositionActionKind.EXIT and review.closed is True
    (closed,) = harness.accounting.outcomes()
    # The harness pool: TVL 1 000, 20 of LP fees in its 30m window. Ten minutes are a third
    # of that window: 6.67 of pool fees, of which the position takes invested / 1 000.
    expected = invested * Decimal(20) / Decimal(3) / Decimal(1000)
    assert closed.valuation.fee_income_base_units > 0
    assert abs(closed.valuation.fee_income_base_units - expected) < Decimal("1e-12")
