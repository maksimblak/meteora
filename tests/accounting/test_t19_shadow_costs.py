"""T19 (spec 6.8, No 49): SHADOW pays the priority fee of every transaction, and the take
profit reads the net P&L the same accounting would book for leaving now."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from src.accounting import CostModel, InMemoryAccountingRepository, LedgerKind, ShadowBroker
from src.accounting.models import ExitSaleModel
from src.core.clock import FrozenClock
from src.domain.models.trading import TradingMode
from src.execution.contracts import DlmmCommandV1, DlmmOperation, LiquidityStrategy
from src.market.models import DataQuality, Freshness, PoolRef, PoolSnapshot, WindowMetrics
from src.portfolio.models import PositionStatus

KEY = "11111111111111111111111111111111"
POOL_ADDRESS = "SysvarRent111111111111111111111111111111111"
WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
NOW = datetime(2026, 10, 24, 12, tzinfo=UTC)


def snapshot(
    price: str, *, quote: str = WSOL, at: datetime = NOW, fees_30m: str = "0"
) -> PoolSnapshot:
    windows = {
        name: WindowMetrics(volume=Decimal("1000"), fees=Decimal(0))
        for name in ("5m", "1h", "4h", "24h")
    }
    windows["30m"] = WindowMetrics(volume=Decimal("1000"), fees=Decimal(fees_30m))
    return PoolSnapshot(
        pool=PoolRef(address=POOL_ADDRESS, base_mint=KEY, quote_mint=quote),
        windows=windows,
        tvl=Decimal("100000"),
        depth=Decimal("50000"),
        price=Decimal(price),
        bin_step=10,
        created_at=NOW - timedelta(days=100),
        observed_at=at,
        source_at=at,
        ingested_at=at,
        freshness=Freshness.FRESH,
        data_quality=DataQuality.COMPLETE,
        base_fee_rate=Decimal("0.02"),
    )


def bid(operation: DlmmOperation, key: str, at: datetime = NOW) -> DlmmCommandV1:
    liquidity = operation is DlmmOperation.OPEN
    return DlmmCommandV1(
        command_id=uuid4(),
        idempotency_key=key,
        correlation_id=uuid4(),
        ruleset_version="guide-v1",
        mode=TradingMode.SHADOW,
        operation=operation,
        pool_address=POOL_ADDRESS,
        wallet_address=KEY,
        position_address=KEY,
        expires_at=at + timedelta(minutes=5),
        max_slippage_bps=100,
        strategy=LiquidityStrategy.BID_ASK if liquidity else None,
        min_bin_id=-60,
        max_bin_id=0,
        amount_x="0" if liquidity else None,
        amount_y="1000" if liquidity else None,
        remove_bps=None if liquidity else 10_000,
    )


def costs(priority_lamports: int) -> CostModel:
    return CostModel(
        slippage_bps=100,
        network_fee_base_units=Decimal(5),
        jito_tip_base_units=Decimal(0),
        priority_fee_lamports=priority_lamports,
    )


def round_trip(quote: str) -> list[tuple[LedgerKind, Decimal]]:
    repo = InMemoryAccountingRepository()
    broker = ShadowBroker(repo, FrozenClock(NOW), exit_sale=ExitSaleModel())
    broker.apply(bid(DlmmOperation.OPEN, "open"), snapshot("2", quote=quote), costs(15_000_000))
    broker.apply(bid(DlmmOperation.CLOSE, "close"), snapshot("1", quote=quote), costs(15_000_000))
    return [(entry.kind, entry.jito_base_units) for entry in repo.ledger_entries()]


def test_shadow_pays_the_priority_fee_ceiling_on_entry_exit_and_sale_in_a_sol_pool() -> None:
    # Quote token wSOL: lamports are its base units; 0.015 SOL a transaction (the ceiling).
    assert round_trip(WSOL) == [
        (LedgerKind.OPEN, Decimal(15_000_000)),
        (LedgerKind.CLOSE, Decimal(15_000_000)),
        (LedgerKind.SWAP, Decimal(15_000_000)),
    ]


def test_a_pool_not_quoted_in_sol_has_no_lamport_priced_fee_in_its_units() -> None:
    assert round_trip(USDC) == [
        (LedgerKind.OPEN, Decimal(0)),
        (LedgerKind.CLOSE, Decimal(0)),
        (LedgerKind.SWAP, Decimal(0)),
    ]


def test_the_broker_takes_the_sale_setting_from_its_caller_not_the_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # T17's tail: SWAP_* reach SHADOW through the worker's runtime only.
    monkeypatch.setenv("SWAP_AFTER_EXIT", "false")
    repo = InMemoryAccountingRepository()
    broker = ShadowBroker(repo, FrozenClock(NOW))
    broker.apply(bid(DlmmOperation.OPEN, "open"), snapshot("2"), costs(0))
    broker.apply(bid(DlmmOperation.CLOSE, "close"), snapshot("1"), costs(0))

    kinds = [entry.kind for entry in repo.ledger_entries()]
    assert kinds == [LedgerKind.OPEN, LedgerKind.CLOSE, LedgerKind.SWAP]


def test_the_close_preview_is_the_net_pnl_of_leaving_now_and_books_nothing() -> None:
    repo = InMemoryAccountingRepository()
    clock = FrozenClock(NOW)
    broker = ShadowBroker(repo, clock, exit_sale=ExitSaleModel())
    broker.apply(bid(DlmmOperation.OPEN, "open"), snapshot("2"), costs(2))
    later = NOW + timedelta(minutes=30)
    clock.advance(timedelta(minutes=30))
    position = repo.position_for(KEY)
    assert position is not None

    preview = broker.preview_close(
        position, snapshot("2", at=later, fees_30m="9000"), costs(2), bins=(-60, 0), at=later
    )

    # In range for the 30 minutes: 1000 of 100 000 TVL earns 1% of the pool's 9000 = 90.
    # Costs: entry 1% of 1000 + 5 + 2, exit the same; no base token comes back at 2.0.
    assert preview.fee_income_base_units == Decimal(90)
    assert preview.total_costs_base_units == Decimal(34)
    assert preview.net_pnl_base_units == Decimal(56)
    assert preview.invested_base_units == Decimal(1000)
    unchanged = repo.position_for(KEY)
    assert unchanged is not None and unchanged.status is PositionStatus.OPEN
    assert [entry.kind for entry in repo.ledger_entries()] == [LedgerKind.OPEN]


def test_the_close_preview_charges_the_sale_of_the_base_token_a_fall_returns() -> None:
    repo = InMemoryAccountingRepository()
    clock = FrozenClock(NOW)
    broker = ShadowBroker(repo, clock, exit_sale=ExitSaleModel())
    broker.apply(bid(DlmmOperation.OPEN, "open"), snapshot("2"), costs(2))
    position = repo.position_for(KEY)
    assert position is not None

    preview = broker.preview_close(position, snapshot("1"), costs(2), bins=(-60, 0), at=NOW)

    # At 1.0 the bid is 500 X worth 500: exit slippage 5, the sale 500 x (2% + 1%) = 15
    # and its transaction 5 + 2, on top of the entry's 17 and the exit's 7.
    assert preview.total_costs_base_units == Decimal(17 + 5 + 7 + 15 + 7)
    assert preview.net_pnl_base_units == Decimal(500 - 1000) - Decimal(51)
