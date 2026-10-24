"""The sale after an exit in the ledger (G22): by receipt amounts, or modeled in SHADOW."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from src.accounting import (
    AccountingService,
    CostModel,
    InMemoryAccountingRepository,
    LedgerKind,
    ShadowBroker,
)
from src.accounting.models import ExitSaleModel, SaleFill
from src.core.clock import FrozenClock
from src.domain.models.trading import TradingMode
from src.execution.contracts import DlmmCommandV1, DlmmOperation, LiquidityStrategy
from src.market.models import DataQuality, Freshness, PoolRef, PoolSnapshot, WindowMetrics
from src.portfolio.models import PositionStatus, ShadowPosition

KEY = "11111111111111111111111111111111"
NOW = datetime(2026, 10, 20, 12, tzinfo=UTC)


def closed_position() -> ShadowPosition:
    # Closed at 1.0 after the price halved: the 1000 Y bid became 500 X booked at 1.0.
    return ShadowPosition(
        position_id=KEY,
        pool_address=KEY,
        status=PositionStatus.CLOSED,
        mode=TradingMode.LIVE,
        ruleset_version="guide-v1",
        correlation_id="c-1",
        opened_at=NOW - timedelta(hours=1),
        updated_at=NOW,
        closed_at=NOW,
        tranches=(),
        realized_proceeds_base_units=Decimal(500),
        realized_hodl_proceeds_base_units=Decimal(1000),
        realized_cost_basis_base_units=Decimal(1000),
        slippage_base_units=Decimal(5),
        network_base_units=Decimal(10),
        version=2,
    )


def sale(proceeds: str, *, sold: str = "500", network: str = "5", priority: str = "2") -> SaleFill:
    return SaleFill(
        command_id=str(uuid4()),
        idempotency_key=f"exit-sale:{KEY}:1:1",
        sold_at=NOW,
        sold_base_units=Decimal(sold),
        proceeds_base_units=Decimal(proceeds),
        mark_price=Decimal("1.0"),
        network_fee_base_units=Decimal(network),
        priority_fee_base_units=Decimal(priority),
    )


def test_a_sale_below_the_exit_mark_is_a_cost_of_the_position_by_receipt_amounts() -> None:
    service = AccountingService()
    before = service.value_at(closed_position(), Decimal("1.0"), NOW)

    position, entries = service.apply_sales(closed_position(), (sale("485"),), "c-1")
    after = service.value_at(position, Decimal("1.0"), NOW)

    # 500 X booked at 1.0 sold for 485 Y: 15 below the mark, plus 5 + 2 lamports of fees.
    assert position.slippage_base_units == Decimal(20)
    assert position.network_base_units == Decimal(15)
    assert position.jito_base_units == Decimal(2)
    assert position.realized_proceeds_base_units == Decimal(500)
    assert position.version == 2
    assert after.net_pnl_base_units == before.net_pnl_base_units - Decimal(22)
    [entry] = entries
    assert entry.kind is LedgerKind.SWAP
    assert (entry.realized_pnl_base_units, entry.slippage_base_units) == (Decimal(0), Decimal(15))
    assert (entry.network_base_units, entry.jito_base_units) == (Decimal(5), Decimal(2))
    assert entry.price == Decimal("0.97")


def test_a_sale_above_the_exit_mark_adds_to_the_realized_proceeds() -> None:
    service = AccountingService()

    position, entries = service.apply_sales(
        closed_position(), (sale("510", network="0", priority="0"),), "c-1"
    )

    assert position.realized_proceeds_base_units == Decimal(510)
    assert position.slippage_base_units == Decimal(5)
    assert entries[0].realized_pnl_base_units == Decimal(10)


def snapshot(price: str, *, fee_rate: str | None = "0.02") -> PoolSnapshot:
    return PoolSnapshot(
        pool=PoolRef(address=KEY, base_mint=KEY, quote_mint=KEY),
        windows={
            name: WindowMetrics(volume=Decimal("1000"), fees=Decimal(0))
            for name in ("5m", "30m", "1h", "4h", "24h")
        },
        tvl=Decimal("100000"),
        depth=Decimal("50000"),
        price=Decimal(price),
        bin_step=10,
        created_at=NOW - timedelta(days=100),
        observed_at=NOW,
        source_at=NOW,
        ingested_at=NOW,
        freshness=Freshness.FRESH,
        data_quality=DataQuality.COMPLETE,
        base_fee_rate=None if fee_rate is None else Decimal(fee_rate),
    )


def bid(operation: DlmmOperation, key: str) -> DlmmCommandV1:
    liquidity = operation is DlmmOperation.OPEN
    return DlmmCommandV1(
        command_id=uuid4(),
        idempotency_key=key,
        correlation_id=uuid4(),
        ruleset_version="guide-v1",
        mode=TradingMode.SHADOW,
        operation=operation,
        pool_address=KEY,
        wallet_address=KEY,
        position_address=KEY,
        expires_at=NOW + timedelta(minutes=5),
        max_slippage_bps=100,
        strategy=LiquidityStrategy.BID_ASK if liquidity else None,
        min_bin_id=-60,
        max_bin_id=0,
        amount_x="0" if liquidity else None,
        amount_y="1000" if liquidity else None,
        remove_bps=None if liquidity else 10_000,
    )


COSTS = CostModel(
    slippage_bps=100, network_fee_base_units=Decimal(5), jito_tip_base_units=Decimal(0)
)


def shadow_close(price: str, model: ExitSaleModel, fee_rate: str | None = "0.02") -> ShadowPosition:
    repo = InMemoryAccountingRepository()
    broker = ShadowBroker(repo, FrozenClock(NOW), exit_sale=model)
    broker.apply(bid(DlmmOperation.OPEN, "open"), snapshot("2", fee_rate=fee_rate), COSTS)
    receipt = broker.apply(
        bid(DlmmOperation.CLOSE, "close"), snapshot(price, fee_rate=fee_rate), COSTS
    )
    entries = repo.ledger_entries()
    assert receipt.position.status is PositionStatus.CLOSED
    assert [entry.kind for entry in entries][:2] == [LedgerKind.OPEN, LedgerKind.CLOSE]
    return receipt.position


def test_shadow_models_the_sale_of_the_returned_base_token_by_the_pool_fee_and_the_quote() -> None:
    # 1000 Y bid at 2.0 closed at 1.0: the model returns 500 X worth 500. The sale costs
    # 500 * (2% pool fee + 1% modeled quote slippage) = 15 and one more transaction (5).
    sold = shadow_close("1", ExitSaleModel())
    kept = shadow_close("1", ExitSaleModel(enabled=False))

    assert sold.slippage_base_units == kept.slippage_base_units + Decimal(15)
    assert sold.network_base_units == kept.network_base_units + Decimal(5)
    assert sold.realized_proceeds_base_units == kept.realized_proceeds_base_units


def test_shadow_sale_with_an_unknown_pool_fee_assumes_the_guides_highest_base_fee() -> None:
    sold = shadow_close("1", ExitSaleModel(), fee_rate=None)
    kept = shadow_close("1", ExitSaleModel(enabled=False), fee_rate=None)

    # 500 * (5% + 1%) = 30.
    assert sold.slippage_base_units == kept.slippage_base_units + Decimal(30)


def test_shadow_exit_above_the_entry_returns_no_base_token_and_models_no_sale() -> None:
    sold = shadow_close("3", ExitSaleModel())
    kept = shadow_close("3", ExitSaleModel(enabled=False))

    # Two separate runs: only their random correlation ids differ.
    assert sold.model_dump(exclude={"correlation_id"}) == kept.model_dump(
        exclude={"correlation_id"}
    )


def test_the_shadow_sale_is_one_more_ledger_entry_of_the_close() -> None:
    repo = InMemoryAccountingRepository()
    broker = ShadowBroker(repo, FrozenClock(NOW), exit_sale=ExitSaleModel())
    broker.apply(bid(DlmmOperation.OPEN, "open"), snapshot("2"), COSTS)
    broker.apply(bid(DlmmOperation.CLOSE, "close"), snapshot("1"), COSTS)

    kinds = [entry.kind for entry in repo.ledger_entries()]
    [outcome] = repo.outcomes()

    assert kinds == [LedgerKind.OPEN, LedgerKind.CLOSE, LedgerKind.SWAP]
    assert outcome.valuation.total_costs_base_units == Decimal(10 + 5 + 15) + Decimal(5 + 5 + 5)
