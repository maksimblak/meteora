from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Barrier, Event
from typing import Any
from uuid import uuid4

import pytest

from src.accounting import (
    AccountingService,
    ConcurrentPositionUpdate,
    CostModel,
    InMemoryAccountingRepository,
    LedgerKind,
    PerformanceService,
    PositionOutcome,
    ShadowBroker,
    SqliteAccountingRepository,
    TimeWindow,
)
from src.accounting.models import ExitSaleModel
from src.core.clock import FrozenClock
from src.domain.models.events import EventType
from src.domain.models.trading import TradingMode
from src.execution.contracts import (
    DlmmCommandV1,
    DlmmOperation,
    ExecutionPhase,
    GatewayReceiptV1,
    GatewayState,
    LiquidityStrategy,
    canonical_command_digest,
)
from src.market.models import (
    DataQuality,
    Freshness,
    PoolRef,
    PoolSnapshot,
    WindowMetrics,
)

KEY = "11111111111111111111111111111111"
OTHER_POOL = "So11111111111111111111111111111111111111112"
NOW = datetime(2026, 10, 20, 12, tzinfo=UTC)


def snapshot(
    price: str,
    *,
    observed_at: datetime = NOW,
    pool: str = KEY,
    freshness: Freshness = Freshness.FRESH,
) -> PoolSnapshot:
    windows = {
        name: WindowMetrics(volume=Decimal("1000"), fees=Decimal("10"))
        for name in ("5m", "30m", "1h", "4h", "24h")
    }
    return PoolSnapshot(
        pool=PoolRef(address=pool, base_mint=KEY, quote_mint=KEY),
        windows=windows,
        tvl=Decimal("100000"),
        depth=Decimal("50000"),
        price=Decimal(price),
        bin_step=10,
        created_at=observed_at - timedelta(days=100),
        observed_at=observed_at,
        source_at=observed_at,
        ingested_at=observed_at,
        freshness=freshness,
        data_quality=DataQuality.COMPLETE,
    )


def command(
    operation: DlmmOperation,
    key: str,
    *,
    amount: str = "100",
    remove_bps: int = 5_000,
    ruleset: str = "rules-v1",
) -> DlmmCommandV1:
    liquidity = operation in {DlmmOperation.OPEN, DlmmOperation.ADD}
    return DlmmCommandV1(
        command_id=uuid4(),
        idempotency_key=key,
        correlation_id=uuid4(),
        ruleset_version=ruleset,
        mode=TradingMode.SHADOW,
        operation=operation,
        pool_address=KEY,
        wallet_address=KEY,
        position_address=KEY,
        expires_at=NOW + timedelta(minutes=5),
        max_slippage_bps=100,
        strategy=LiquidityStrategy.SPOT if liquidity else None,
        min_bin_id=-2,
        max_bin_id=2,
        amount_x=amount if liquidity else None,
        amount_y=amount if liquidity else None,
        remove_bps=None
        if liquidity
        else (10_000 if operation is DlmmOperation.CLOSE else remove_bps),
    )


def costs(*, fees: str = "0", slippage_bps: int = 100) -> CostModel:
    return CostModel(
        slippage_bps=slippage_bps,
        network_fee_base_units=Decimal("2"),
        jito_tip_base_units=Decimal("3"),
        fee_income_base_units=Decimal(fees),
    )


def free() -> CostModel:
    return CostModel(
        slippage_bps=0, network_fee_base_units=Decimal(0), jito_tip_base_units=Decimal(0)
    )


def test_shadow_open_is_idempotent_across_broker_restart() -> None:
    repo = InMemoryAccountingRepository()
    first = ShadowBroker(repo, FrozenClock(NOW)).apply(
        command(DlmmOperation.OPEN, "open-1"), snapshot("2"), costs()
    )
    second = ShadowBroker(repo, FrozenClock(NOW)).apply(first.command, snapshot("3"), costs())

    assert second == first
    assert len(repo.positions()) == 1
    assert len(repo.ledger_entries()) == 1
    assert len(repo.outbox_events()) == 2


def test_shadow_idempotency_key_rejects_different_payload() -> None:
    repo = InMemoryAccountingRepository()
    broker = ShadowBroker(repo, FrozenClock(NOW))
    broker.apply(command(DlmmOperation.OPEN, "same-key"), snapshot("2"), costs())

    with pytest.raises(ValueError, match="idempotency"):
        broker.apply(command(DlmmOperation.OPEN, "same-key", amount="200"), snapshot("2"), costs())
    assert len(repo.ledger_entries()) == 1


def test_shadow_receipt_and_position_survive_process_store_restart(tmp_path: Path) -> None:
    path = tmp_path / "accounting.sqlite3"
    first_repo = SqliteAccountingRepository(path)
    issued = command(DlmmOperation.OPEN, "durable-open")
    first = ShadowBroker(first_repo, FrozenClock(NOW)).apply(issued, snapshot("2"), costs())
    first_repo.close()

    restarted_repo = SqliteAccountingRepository(path)
    replay = ShadowBroker(restarted_repo, FrozenClock(NOW)).apply(
        issued, snapshot("5"), costs()
    )

    assert replay == first
    assert restarted_repo.position_for(KEY) == first.position
    assert len(restarted_repo.ledger_entries()) == 1
    restarted_repo.close()


def test_shadow_lifecycle_matches_live_receipt_lifecycle_without_executor() -> None:
    repo = InMemoryAccountingRepository()
    broker = ShadowBroker(repo, FrozenClock(NOW))
    commands = (
        command(DlmmOperation.OPEN, "open"),
        command(DlmmOperation.ADD, "add", amount="10"),
        command(DlmmOperation.REMOVE, "remove"),
        command(DlmmOperation.CLOSE, "close"),
    )
    receipts = [broker.apply(item, snapshot("2"), costs()) for item in commands]

    for issued, receipt in zip(commands, receipts, strict=True):
        assert receipt.phase is ExecutionPhase.CONFIRMED
        live_shape = GatewayReceiptV1.model_validate(receipt.gateway_receipt.model_dump())
        assert live_shape.state is GatewayState.CONFIRMED
        assert live_shape.command_digest == canonical_command_digest(issued)
        assert live_shape.idempotency_key == issued.idempotency_key
    assert [receipt.position.version for receipt in receipts] == [1, 2, 3, 4]
    assert [entry.kind for entry in repo.ledger_entries()] == [
        LedgerKind.OPEN,
        LedgerKind.ADD,
        LedgerKind.REMOVE,
        LedgerKind.CLOSE,
    ]
    events = repo.outbox_events()
    assert [event.event_type for event in events] == [
        EventType.COMMAND_CONFIRMED,
        EventType.POSITION_OPENED,
        EventType.COMMAND_CONFIRMED,
        EventType.COMMAND_CONFIRMED,
        EventType.COMMAND_CONFIRMED,
        EventType.POSITION_CLOSED,
    ]
    correlations = {item.command_id: item.correlation_id for item in commands}
    assert all(event.correlation_id == correlations[event.causation_id] for event in events)
    assert len(repo.outcomes()) == 1
    assert receipts[-1].position.status.value == "CLOSED"


def test_mark_golden_costs_conservative_divergence_net_and_hodl() -> None:
    repo = InMemoryAccountingRepository()
    receipt = ShadowBroker(repo, FrozenClock(NOW)).apply(
        command(DlmmOperation.OPEN, "open"), snapshot("2"), costs(fees="30")
    )
    service = AccountingService()

    up = service.mark(receipt.position, snapshot("8"))
    down = service.mark(receipt.position, snapshot("1"))

    # 100 X + 100 Y at 2 = 300 invested; costs = 1% of 300 + 2 + 3 = 8.
    assert up.invested_base_units == Decimal("300")
    assert up.fee_income_base_units == Decimal("30")
    assert up.total_costs_base_units == Decimal("8")
    # Up to 8: HODL 900; worst case all X sold at entry 2 -> 300.
    assert up.hodl_value_base_units == Decimal("900")
    assert up.liquidity_value_base_units == Decimal("300")
    assert up.divergence_loss_base_units == Decimal("600")
    assert up.net_pnl_base_units == Decimal("22")
    assert up.hodl_pnl_base_units == Decimal("600")
    assert up.excess_vs_hodl_base_units == Decimal("-578")
    # Down to 1: HODL 200; worst case all Y bought X at 2 -> 150 X worth 150.
    assert down.liquidity_value_base_units == Decimal("150")
    assert down.divergence_loss_base_units == Decimal("50")
    assert down.unrealized_pnl_base_units == Decimal("-150")
    assert down.net_pnl_base_units == Decimal("-128")
    assert down.hodl_pnl_base_units == Decimal("-100")


@pytest.mark.parametrize("price", ["0.5", "1.5", "2", "2.5", "4", "16"])
def test_divergence_is_never_below_full_range_impermanent_loss(price: str) -> None:
    repo = InMemoryAccountingRepository()
    receipt = ShadowBroker(repo, FrozenClock(NOW)).apply(
        command(DlmmOperation.OPEN, "open"), snapshot("2"), free()
    )
    valuation = AccountingService().mark(receipt.position, snapshot(price))
    ratio = Decimal(price) / Decimal(2)
    full_range_loss = valuation.hodl_value_base_units * (
        Decimal(1) - Decimal(2) * ratio.sqrt() / (Decimal(1) + ratio)
    )

    assert valuation.divergence_loss_base_units >= full_range_loss.quantize(Decimal("1e-20"))


def test_add_at_new_price_measures_divergence_from_its_own_entry() -> None:
    repo = InMemoryAccountingRepository()
    broker = ShadowBroker(repo, FrozenClock(NOW))
    opened = broker.apply(command(DlmmOperation.OPEN, "open"), snapshot("2"), free())
    added = broker.apply(command(DlmmOperation.ADD, "add", amount="10"), snapshot("4"), free())

    alone = AccountingService().mark(opened.position, snapshot("4"))
    combined = AccountingService().mark(added.position, snapshot("4"))

    assert combined.divergence_loss_base_units == alone.divergence_loss_base_units
    assert combined.divergence_loss_base_units == Decimal("200")
    assert combined.hodl_value_base_units == Decimal("550")
    assert combined.invested_base_units == Decimal("350")


def test_partial_remove_realizes_pnl_and_close_does_not_double_count() -> None:
    repo = InMemoryAccountingRepository()
    # The lifecycle alone: the sale of the returned base token has its own tests (T17).
    broker = ShadowBroker(repo, FrozenClock(NOW), exit_sale=ExitSaleModel(enabled=False))
    broker.apply(command(DlmmOperation.OPEN, "open"), snapshot("2"), costs())
    removed = broker.apply(
        command(DlmmOperation.REMOVE, "remove"), snapshot("1"), costs(fees="10")
    )
    closed = broker.apply(command(DlmmOperation.CLOSE, "close"), snapshot("2"), costs(fees="10"))

    # Half removed at 1: proceeds (50 + 25) * 1 = 75 against basis 150.
    assert removed.position.realized_proceeds_base_units == Decimal("75")
    assert removed.position.realized_cost_basis_base_units == Decimal("150")
    assert removed.valuation.realized_pnl_base_units == Decimal("-78.75")
    assert removed.valuation.unrealized_pnl_base_units == Decimal("-75")
    assert removed.valuation.net_pnl_base_units == Decimal("-153.75")
    entries = {entry.kind: entry for entry in repo.ledger_entries()}
    assert entries[LedgerKind.REMOVE].realized_pnl_base_units == Decimal("-75")
    assert entries[LedgerKind.CLOSE].realized_pnl_base_units == Decimal("0")
    # Close the rest at entry price 2: no further divergence.
    assert closed.valuation.realized_pnl_base_units == Decimal("-75.25")
    assert closed.valuation.unrealized_pnl_base_units == Decimal("0")
    assert closed.valuation.net_pnl_base_units == Decimal("-75.25")
    assert closed.valuation.hodl_pnl_base_units == Decimal("-50")
    assert closed.valuation.divergence_loss_base_units == Decimal("25")
    assert closed.valuation.total_costs_base_units == Decimal("20.25")
    assert repo.outcomes()[0].valuation == closed.valuation
    window = TimeWindow(start=NOW - timedelta(hours=1), end=NOW + timedelta(hours=1))
    summary = PerformanceService(repo).summary(window)
    assert summary.groups[0].net_pnl_base_units == Decimal("-75.25")


def test_concurrent_add_close_uses_cas_and_cannot_resurrect_closed_position() -> None:
    base = InMemoryAccountingRepository()
    broker = ShadowBroker(base, FrozenClock(NOW))
    broker.apply(command(DlmmOperation.OPEN, "open"), snapshot("2"), costs())
    barrier = Barrier(2)
    close_committed = Event()

    class CoordinatedRepository:
        def __getattr__(self, name: str) -> Any:
            return getattr(base, name)

        def position_for(self, position_id: str) -> Any:
            position = base.position_for(position_id)
            barrier.wait()
            return position

        def commit(self, receipt: Any, *args: Any, **kwargs: Any) -> Any:
            if receipt.command.operation is DlmmOperation.ADD:
                close_committed.wait(timeout=2)
            try:
                return base.commit(receipt, *args, **kwargs)
            finally:
                if receipt.command.operation is DlmmOperation.CLOSE:
                    close_committed.set()

    concurrent = ShadowBroker(CoordinatedRepository(), FrozenClock(NOW))  # type: ignore[arg-type]
    add = command(DlmmOperation.ADD, "add", amount="10")
    close = command(DlmmOperation.CLOSE, "close")
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(concurrent.apply, add, snapshot("2"), costs()),
            pool.submit(concurrent.apply, close, snapshot("2"), costs()),
        ]
    with pytest.raises(ConcurrentPositionUpdate):
        futures[0].result()

    assert futures[1].result().position.status.value == "CLOSED"
    assert base.position_for(KEY).status.value == "CLOSED"  # type: ignore[union-attr]
    assert base.receipt_for("add") is None
    assert len(base.outcomes()) == 1


def test_sqlite_cas_rejects_a_stale_writer_from_another_connection(tmp_path: Path) -> None:
    path = tmp_path / "cas.sqlite3"
    writer = SqliteAccountingRepository(path)
    stale = SqliteAccountingRepository(path)
    ShadowBroker(writer, FrozenClock(NOW)).apply(
        command(DlmmOperation.OPEN, "open"), snapshot("2"), costs()
    )

    class StaleReader:
        def __getattr__(self, name: str) -> Any:
            return getattr(stale, name)

        def position_for(self, position_id: str) -> Any:
            position = stale.position_for(position_id)
            ShadowBroker(writer, FrozenClock(NOW)).apply(
                command(DlmmOperation.ADD, "add", amount="10"), snapshot("2"), costs()
            )
            return position

    with pytest.raises(ConcurrentPositionUpdate):
        ShadowBroker(StaleReader(), FrozenClock(NOW)).apply(  # type: ignore[arg-type]
            command(DlmmOperation.CLOSE, "close"), snapshot("2"), costs()
        )

    current = stale.position_for(KEY)
    assert current is not None
    assert current.status.value == "OPEN"
    assert current.version == 2
    assert stale.outcomes() == ()


def _outcome(base: PositionOutcome, name: str, net: str, hours: int, **changes: Any) -> Any:
    valuation = base.valuation.model_copy(update={"net_pnl_base_units": Decimal(net)})
    return base.model_copy(
        update={
            "position_id": name,
            "closed_at": NOW + timedelta(hours=hours),
            "valuation": valuation,
            **changes,
        }
    )


def test_performance_summary_separates_ruleset_and_mode_with_golden_statistics() -> None:
    repo = InMemoryAccountingRepository()
    broker = ShadowBroker(repo, FrozenClock(NOW))
    broker.apply(command(DlmmOperation.OPEN, "open"), snapshot("2"), costs())
    broker.apply(command(DlmmOperation.CLOSE, "close"), snapshot("3"), costs(fees="20"))
    base = repo.outcomes()[0]
    outcomes = (
        _outcome(base, "a", "10", 1),
        _outcome(base, "b", "-30", 2),
        _outcome(base, "c", "5", 3),
        _outcome(base, "d", "100", 1, ruleset_version="rules-v2"),
        _outcome(base, "e", "-7", 1, mode=TradingMode.APPROVAL),
    )

    class OutcomeSource:
        def outcomes_in(self, window: TimeWindow) -> tuple[PositionOutcome, ...]:
            return tuple(item for item in outcomes if window.contains(item.closed_at))

    window = TimeWindow(start=NOW, end=NOW + timedelta(days=1))
    summary = PerformanceService(OutcomeSource()).summary(window)  # type: ignore[arg-type]

    groups = {(group.ruleset_version, group.mode): group for group in summary.groups}
    assert set(groups) == {
        ("rules-v1", TradingMode.SHADOW),
        ("rules-v1", TradingMode.APPROVAL),
        ("rules-v2", TradingMode.SHADOW),
    }
    shadow = groups[("rules-v1", TradingMode.SHADOW)]
    assert shadow.completed_positions == 3
    assert shadow.winning_positions == 2
    assert shadow.net_pnl_base_units == Decimal("-15")
    assert shadow.expectancy_base_units == Decimal("-5")
    assert shadow.maximum_drawdown_base_units == Decimal("30")
    assert groups[("rules-v2", TradingMode.SHADOW)].net_pnl_base_units == Decimal("100")
    assert groups[("rules-v1", TradingMode.APPROVAL)].net_pnl_base_units == Decimal("-7")
    assert PerformanceService(repo).summary(
        TimeWindow(start=NOW - timedelta(days=2), end=NOW - timedelta(days=1))
    ).groups == ()


@pytest.mark.parametrize(
    ("issued", "market", "model"),
    [
        (
            command(DlmmOperation.OPEN, "live").model_copy(update={"mode": TradingMode.LIVE}),
            snapshot("2"),
            costs(),
        ),
        (
            command(DlmmOperation.OPEN, "expired").model_copy(
                update={"expires_at": NOW - timedelta(seconds=1)}
            ),
            snapshot("2"),
            costs(),
        ),
        (command(DlmmOperation.OPEN, "stale"), snapshot("2", freshness=Freshness.STALE), costs()),
        (
            command(DlmmOperation.OPEN, "future"),
            snapshot("2", observed_at=NOW + timedelta(seconds=1)),
            costs(),
        ),
        (command(DlmmOperation.OPEN, "pool"), snapshot("2", pool=OTHER_POOL), costs()),
        (command(DlmmOperation.OPEN, "slippage"), snapshot("2"), costs(slippage_bps=101)),
        (command(DlmmOperation.ADD, "no-position", amount="10"), snapshot("2"), costs()),
    ],
)
def test_shadow_broker_rejects_unsafe_fills_without_side_effects(
    issued: DlmmCommandV1, market: PoolSnapshot, model: CostModel
) -> None:
    repo = InMemoryAccountingRepository()

    with pytest.raises(ValueError):
        ShadowBroker(repo, FrozenClock(NOW)).apply(issued, market, model)
    assert repo.positions() == ()
    assert repo.ledger_entries() == ()
    assert repo.outbox_events() == ()


def test_daily_observation_counts_only_outcomes_closed_in_that_utc_day() -> None:
    repo = InMemoryAccountingRepository()
    broker = ShadowBroker(repo, FrozenClock(NOW))
    broker.apply(command(DlmmOperation.OPEN, "open"), snapshot("2"), costs())
    closed = broker.apply(command(DlmmOperation.CLOSE, "close"), snapshot("2"), costs())
    service = PerformanceService(repo)

    def observe(day: date, ruleset: str = "rules-v1") -> Any:
        return service.daily_observation(
            day,
            ruleset_version=ruleset,
            mode=TradingMode.SHADOW,
            start_equity=Decimal("1000"),
            end_equity=Decimal("990"),
            conservative_open_equity=Decimal("990"),
            data_coverage=Decimal("1"),
            decision_count=4,
            risk_invariant_breaches=0,
        )

    today = observe(NOW.date())
    assert today.completed_positions == 1
    assert today.net_pnl == closed.valuation.net_pnl_base_units
    assert observe(NOW.date() - timedelta(days=1)).completed_positions == 0
    assert observe(NOW.date(), "rules-v2").completed_positions == 0
