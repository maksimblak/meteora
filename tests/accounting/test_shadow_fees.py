"""B7: a SHADOW position earns the pool's LP fees in proportion to its share of liquidity
while its range holds the price; out of range it earns nothing (R19.1, spec 7.1)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from src.accounting import CostModel, InMemoryAccountingRepository, ShadowBroker
from src.core.clock import FrozenClock
from src.domain.models.trading import TradingMode
from src.execution.contracts import DlmmCommandV1, DlmmOperation, LiquidityStrategy
from src.market.models import DataQuality, Freshness, PoolRef, PoolSnapshot, WindowMetrics

KEY = "11111111111111111111111111111111"
OPENED = datetime(2026, 10, 24, 12, tzinfo=UTC)
TVL = "20000"  # USD
# LP fees per window in USD; the protocol's share is not paid to liquidity providers.
FEES = {"5m": "200", "30m": "1000", "1h": "1500", "4h": "3000", "24h": "6000"}
SHADOW_COSTS = CostModel(
    slippage_bps=50, network_fee_base_units=Decimal(0), jito_tip_base_units=Decimal(0)
)


def snapshot(at: datetime, price: str = "1") -> PoolSnapshot:
    windows = {
        name: WindowMetrics(
            volume=Decimal("100000"), fees=Decimal(fees), protocol_fees=Decimal("500")
        )
        for name, fees in FEES.items()
    }
    return PoolSnapshot(
        pool=PoolRef(address=KEY, base_mint=KEY, quote_mint=KEY),
        windows=windows,
        tvl=Decimal(TVL),
        depth=None,
        price=Decimal(price),
        bin_step=10,
        created_at=at - timedelta(days=10),
        observed_at=at,
        source_at=at,
        ingested_at=at,
        freshness=Freshness.FRESH,
        data_quality=DataQuality.COMPLETE,
    )


def command(
    operation: DlmmOperation, key: str, at: datetime, *, amount_y: str = "200000000"
) -> DlmmCommandV1:
    liquidity = operation is DlmmOperation.OPEN
    return DlmmCommandV1(
        command_id=uuid4(),
        idempotency_key=key,
        correlation_id=uuid4(),
        ruleset_version="rules-v1",
        mode=TradingMode.SHADOW,
        operation=operation,
        pool_address=KEY,
        wallet_address=KEY,
        position_address=KEY,
        expires_at=at + timedelta(minutes=2),
        max_slippage_bps=100,
        strategy=LiquidityStrategy.SPOT if liquidity else None,
        min_bin_id=-2,
        max_bin_id=2,
        amount_x="0" if liquidity else None,
        amount_y=amount_y if liquidity else None,
        remove_bps=None
        if liquidity
        else (10_000 if operation is DlmmOperation.CLOSE else 5_000),
    )


def hold(
    minutes: int, *, amount_y: str = "200000000", close_price: str = "1"
) -> tuple[Decimal, Decimal]:
    """Open at ``OPENED`` at price 1, close ``minutes`` later: (fee income, net P&L)."""

    repo = InMemoryAccountingRepository()
    clock = FrozenClock(OPENED)
    broker = ShadowBroker(repo, clock)
    broker.apply(
        command(DlmmOperation.OPEN, "open", OPENED, amount_y=amount_y),
        snapshot(OPENED),
        SHADOW_COSTS,
    )
    closed_at = OPENED + timedelta(minutes=minutes)
    clock.advance(closed_at - OPENED)
    receipt = broker.apply(
        command(DlmmOperation.CLOSE, "close", closed_at),
        snapshot(closed_at, close_price),
        SHADOW_COSTS,
    )
    valuation = receipt.valuation
    assert repo.outcomes()[0].valuation == valuation
    return valuation.fee_income_base_units, valuation.net_pnl_base_units


def test_a_position_of_one_percent_of_tvl_earns_one_percent_of_the_pool_fees() -> None:
    # 200 quote units (2e8 base units) in a 20 000 USD pool: 1% of its liquidity. The pool
    # earned 1 000 USD of LP fees over the 30 minutes held: the position earns 10 units.
    fee_income, net = hold(30)

    assert fee_income == Decimal(10_000_000)
    # Same price: no divergence. Costs: 0.5% slippage in and out = 2 units. Net = +8 units.
    assert net == Decimal(8_000_000)
    assert net > 0


@pytest.mark.parametrize(
    ("minutes", "amount_y", "expected"),
    [
        # Half the 30m window: half its fees (1 000 * 15 / 30 = 500 USD), 1% of them.
        (15, "200000000", Decimal(5_000_000)),
        # Twice the liquidity (2% of TVL): twice the income.
        (30, "400000000", Decimal(20_000_000)),
        # 3 minutes: the 5m window covers it (200 * 3 / 5 = 120 USD), 1% of it.
        (3, "200000000", Decimal(1_200_000)),
        # Beyond the longest window only its fees are known: 6 000 USD, 1% of it.
        (48 * 60, "200000000", Decimal(60_000_000)),
    ],
)
def test_fee_income_follows_the_share_and_the_fees_earned_while_held(
    minutes: int, amount_y: str, expected: Decimal
) -> None:
    fee_income, _ = hold(minutes, amount_y=amount_y)

    assert fee_income == expected


def test_a_position_out_of_its_range_earns_no_fees() -> None:
    # Bins -2..2 of 10 bps around 1 span [0.998..., 1.003...); 0.99 is below the range.
    fee_income, net = hold(30, close_price="0.99")

    assert fee_income == Decimal(0)
    assert net < 0


def test_a_partial_remove_accrues_once_and_the_rest_earns_on_what_stays() -> None:
    repo = InMemoryAccountingRepository()
    clock = FrozenClock(OPENED)
    broker = ShadowBroker(repo, clock)
    broker.apply(command(DlmmOperation.OPEN, "open", OPENED), snapshot(OPENED), SHADOW_COSTS)
    removed_at = OPENED + timedelta(minutes=15)
    clock.advance(removed_at - OPENED)
    removed = broker.apply(
        command(DlmmOperation.REMOVE, "remove", removed_at), snapshot(removed_at), SHADOW_COSTS
    )
    closed_at = removed_at + timedelta(minutes=15)
    clock.advance(closed_at - removed_at)
    closed = broker.apply(
        command(DlmmOperation.CLOSE, "close", closed_at), snapshot(closed_at), SHADOW_COSTS
    )

    # 15 minutes on 200 units: 500 USD of pool fees, 1% = 5 units.
    assert removed.position.fee_income_base_units == Decimal(5_000_000)
    # Then 15 minutes on the 100 units left: 500 USD * 100 / 20 000 = 2.5 units more.
    assert closed.position.fee_income_base_units == Decimal(7_500_000)
    entries = {entry.kind.value: entry for entry in repo.ledger_entries()}
    assert entries["REMOVE"].fee_income_base_units == Decimal(5_000_000)
    assert entries["CLOSE"].fee_income_base_units == Decimal(2_500_000)
