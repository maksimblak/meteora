"""One contract, two adapters: SQLite (T04) and PostgreSQL (D01) must behave the same.

Every repository instance comes from ``backend.mode()`` / ``backend.accounting()``,
which opens a new connection to the same durable store, so "restart" and "second
process" are exercised on both adapters.
"""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Barrier
from typing import Any
from uuid import UUID, uuid4

import pytest

from src.accounting import (
    ConcurrentPositionUpdate,
    CostModel,
    LedgerKind,
    PerformanceService,
    ShadowBroker,
    TimeWindow,
)
from src.core.clock import FrozenClock
from src.domain.models.events import EventType
from src.domain.models.trading import TradingMode
from src.execution.contracts import DlmmCommandV1, DlmmOperation, LivePromotionEvidence
from src.execution.contracts import LiquidityStrategy as Liquidity
from src.market.models import DataQuality, Freshness, PoolRef, PoolSnapshot, WindowMetrics
from src.modes import (
    ApprovalService,
    CriticalAlert,
    DailyPromotionObservation,
    DailyRiskService,
    KillSwitchPolicy,
    ModeService,
    ModeState,
    ModeTransitionRejected,
    PendingPlan,
    PendingPlanStatus,
    PortfolioEquitySnapshot,
    PromotionEvidenceService,
    PromotionEvidenceSnapshot,
)
from tests.persistence.conftest import Backend

NOW = datetime(2026, 10, 20, 12, tzinfo=UTC)
RULES = "rules-v1"
CONFIRMATION = "operator-confirmation"
KEY = "11111111111111111111111111111111"
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


# --- builders -------------------------------------------------------------------------


def boundary(instant: datetime) -> datetime:
    return datetime.combine(instant.date(), datetime.min.time(), tzinfo=UTC)


def open_day(repo: Any, clock: FrozenClock, equity: str = "1000") -> None:
    repo.put_equity_snapshot(
        PortfolioEquitySnapshot(
            observed_at=boundary(clock.now()), equity=Decimal(equity), ruleset_version=RULES
        )
    )
    DailyRiskService(repo, clock).record(Decimal(0), Decimal(0), RULES)


def observation(days_ago: int) -> DailyPromotionObservation:
    return DailyPromotionObservation(
        risk_date=(NOW - timedelta(days=days_ago)).date(),
        ruleset_version=RULES,
        mode=TradingMode.SHADOW,
        completed_positions=3,
        net_pnl=Decimal("1"),
        start_equity=Decimal("1000"),
        end_equity=Decimal("1001"),
        conservative_open_equity=Decimal("1000"),
        data_coverage=Decimal("0.99"),
        decision_count=10,
    )


def seed_shadow_evidence(repo: Any, clock: FrozenClock | None = None) -> PromotionEvidenceSnapshot:
    service = PromotionEvidenceService(repo, clock or FrozenClock(NOW))
    for days_ago in range(7, 0, -1):
        service.record_daily(observation(days_ago))
    return service.build_shadow(RULES)


def live_evidence() -> LivePromotionEvidence:
    return LivePromotionEvidence(
        promotion_id=uuid4(),
        ruleset_version=RULES,
        approval_cycles=10,
        receipts_reconciled=True,
        risk_invariants_preserved=True,
        dedicated_rpc_ready=True,
        wallet_ready=True,
        checklist_completed=True,
        operator_confirmation_id=CONFIRMATION,
        promoted_at=NOW,
        expires_at=NOW + timedelta(hours=1),
    )


def promote_to_approval(repo: Any, clock: FrozenClock | None = None) -> ModeService:
    clock = clock or FrozenClock(NOW)
    service = ModeService(repo, clock, RULES)
    service.transition(TradingMode.APPROVAL, seed_shadow_evidence(repo, clock), CONFIRMATION)
    return service


def pending(plan_id: UUID | None = None, *, minutes: int = 5) -> PendingPlan:
    return PendingPlan(
        plan_id=plan_id or uuid4(),
        correlation_id="cycle-1",
        admitted_at=NOW,
        expires_at=NOW + timedelta(minutes=minutes),
    )


def open_position(repo: Any, position_id: str) -> None:
    plan = pending()
    assert repo.admit_entry(plan, 3) == ()
    repo.claim_entry(plan.plan_id, NOW)
    repo.confirm_entry(plan.plan_id, position_id)


def snapshot(price: str) -> PoolSnapshot:
    windows = {
        name: WindowMetrics(volume=Decimal("1000"), fees=Decimal("10"))
        for name in ("5m", "30m", "1h", "4h", "24h")
    }
    return PoolSnapshot(
        pool=PoolRef(address=KEY, base_mint=KEY, quote_mint=KEY),
        windows=windows,
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
    )


def command(operation: DlmmOperation, key: str, *, amount: str = "100") -> DlmmCommandV1:
    liquidity = operation in {DlmmOperation.OPEN, DlmmOperation.ADD}
    return DlmmCommandV1(
        command_id=uuid4(),
        idempotency_key=key,
        correlation_id=uuid4(),
        ruleset_version=RULES,
        mode=TradingMode.SHADOW,
        operation=operation,
        pool_address=KEY,
        wallet_address=KEY,
        position_address=KEY,
        expires_at=NOW + timedelta(minutes=5),
        max_slippage_bps=100,
        strategy=Liquidity.SPOT if liquidity else None,
        min_bin_id=-2,
        max_bin_id=2,
        amount_x=amount if liquidity else None,
        amount_y=amount if liquidity else None,
        remove_bps=None if liquidity else (10_000 if operation is DlmmOperation.CLOSE else 5_000),
    )


def costs(fees: str = "0") -> CostModel:
    return CostModel(
        slippage_bps=100,
        network_fee_base_units=Decimal("2"),
        jito_tip_base_units=Decimal("3"),
        fee_income_base_units=Decimal(fees),
    )


def run_concurrently(calls: list[Callable[[], Any]]) -> list[Any]:
    barrier = Barrier(len(calls))

    def gated(call: Callable[[], Any]) -> Any:
        barrier.wait(timeout=10)
        try:
            return call()
        except (ValueError, ModeTransitionRejected) as error:
            return error

    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        return [future.result() for future in [pool.submit(gated, call) for call in calls]]


# --- staged modes -----------------------------------------------------------------------


def test_initial_mode_is_shadow_and_a_promotion_survives_restart(backend: Backend) -> None:
    repo = backend.mode()
    assert repo.mode_state() == ModeState()

    promote_to_approval(repo)
    restarted = backend.mode().mode_state()

    assert restarted.mode is TradingMode.APPROVAL
    assert restarted.promotion_authorized is True
    assert [item.to_mode for item in backend.mode().transitions()] == [TradingMode.APPROVAL]


def test_promotion_is_compare_and_set_and_evidence_is_one_shot(backend: Backend) -> None:
    repo = backend.mode()
    clock = FrozenClock(NOW)
    evidence = seed_shadow_evidence(repo, clock)
    service = ModeService(repo, clock, RULES)

    service.transition(TradingMode.APPROVAL, evidence, CONFIRMATION)
    service.transition(TradingMode.SHADOW, None, CONFIRMATION)
    with pytest.raises(ModeTransitionRejected, match="consumed"):
        ModeService(backend.mode(), clock, RULES).transition(
            TradingMode.APPROVAL, evidence, CONFIRMATION
        )

    fresh = seed_shadow_evidence(repo, clock)
    service.transition(TradingMode.APPROVAL, fresh, CONFIRMATION)
    live = live_evidence()
    service.transition(TradingMode.LIVE, live, CONFIRMATION)
    service.transition(TradingMode.APPROVAL, None, CONFIRMATION)
    with pytest.raises(ModeTransitionRejected, match="consumed"):
        ModeService(backend.mode(), clock, RULES).transition(TradingMode.LIVE, live, CONFIRMATION)
    assert backend.mode().mode_state().mode is TradingMode.APPROVAL


def test_stale_promotion_version_is_rejected(backend: Backend) -> None:
    repo = backend.mode()
    evidence = seed_shadow_evidence(repo)
    from src.modes import ModeTransition

    transition = ModeTransition(
        transition_id=uuid4(),
        from_mode=TradingMode.SHADOW,
        to_mode=TradingMode.APPROVAL,
        occurred_at=NOW,
        operator_confirmation_id=CONFIRMATION,
        promotion_authorized=True,
        evidence_id=str(evidence.evidence_id),
    )
    with pytest.raises(ValueError, match="concurrently"):
        backend.mode().commit_transition(
            transition,
            expected_version=evidence.mode_version + 1,
            shadow_evidence_id=evidence.evidence_id,
        )
    assert backend.mode().mode_state().mode is TradingMode.SHADOW


def test_concurrent_promotions_with_one_evidence_commit_once(backend: Backend) -> None:
    clock = FrozenClock(NOW)
    evidence = seed_shadow_evidence(backend.mode(), clock)
    services = [ModeService(backend.mode(), clock, RULES) for _ in range(2)]

    results = run_concurrently(
        [
            lambda service=service: service.transition(  # type: ignore[misc]
                TradingMode.APPROVAL, evidence, CONFIRMATION
            )
            for service in services
        ]
    )

    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert len(backend.mode().transitions()) == 1


# --- approvals ----------------------------------------------------------------------------


def test_approval_is_one_shot_ttl_bound_and_needs_approval_mode(backend: Backend) -> None:
    shadow = backend.mode()
    clock = FrozenClock(NOW)
    with pytest.raises(ValueError, match="APPROVAL"):
        ApprovalService(shadow, clock).issue(
            uuid4(), DIGEST_A, DIGEST_B, timedelta(minutes=2), "operator"
        )

    promote_to_approval(shadow, clock)
    grant = ApprovalService(shadow, clock).issue(
        uuid4(), DIGEST_A, DIGEST_B, timedelta(minutes=2), "operator", "проверено"
    )
    restarted = backend.mode()
    assert restarted.get_approval(grant.approval_id) == grant
    with pytest.raises(ValueError, match="preview"):
        ApprovalService(restarted, clock).consume(grant.approval_id, DIGEST_B, DIGEST_B)

    consumed = ApprovalService(restarted, clock).consume(grant.approval_id, DIGEST_A, DIGEST_B)
    assert consumed.consumed_at == NOW
    with pytest.raises(ValueError, match="already consumed"):
        ApprovalService(backend.mode(), clock).consume(grant.approval_id, DIGEST_A, DIGEST_B)

    late = ApprovalService(restarted, clock).issue(
        uuid4(), DIGEST_A, DIGEST_B, timedelta(minutes=2), "operator"
    )
    clock.advance(timedelta(minutes=2))
    with pytest.raises(ValueError, match="expired"):
        ApprovalService(restarted, clock).consume(late.approval_id, DIGEST_A, DIGEST_B)
    assert backend.mode().get_approval(late.approval_id).consumed_at is None


# --- daily risk -----------------------------------------------------------------------------


def test_daily_stop_at_exact_four_percent_demotes_alerts_and_survives_restart(
    backend: Backend,
) -> None:
    clock = FrozenClock(NOW)
    repo = backend.mode()
    promote_to_approval(repo, clock)
    open_day(repo, clock)
    admitted = pending()
    assert repo.admit_entry(admitted, 3) == ()

    almost = DailyRiskService(repo, clock).record(Decimal("-39.99"), Decimal(0), RULES)
    assert almost.daily_stop_triggered is False
    state = DailyRiskService(repo, clock).record(Decimal("-40"), Decimal("25"), RULES)

    assert state.daily_stop_triggered is True
    assert state.starting_equity == Decimal("1000")
    restarted = backend.mode()
    assert restarted.mode_state().mode is TradingMode.SHADOW
    assert restarted.mode_state().daily_stop_date == NOW.date()
    assert restarted.risk_day(NOW.date()) == state
    assert restarted.entry_plan(admitted.plan_id).status is PendingPlanStatus.CANCELLED
    assert [alert.code for alert in restarted.alerts() if alert.open] == ["DAILY_STOP_TRIGGERED"]
    types = [event.event_type for event in restarted.outbox_events()]
    assert types[-3:] == [
        EventType.CRITICAL_ALERT_RAISED,
        EventType.DAILY_STOP_TRIGGERED,
        EventType.MODE_CHANGED,
    ]
    assert restarted.admit_entry(pending(), 3) == ("DAILY_STOP_REACHED",)

    lower = DailyRiskService(restarted, clock).record(Decimal("-1"), Decimal(0), RULES)
    assert lower.daily_stop_triggered is True
    assert lower.triggered_at == state.triggered_at

    clock.advance(timedelta(days=1))
    open_day(restarted, clock)
    assert DailyRiskService(backend.mode(), clock).entries_allowed() is True


def test_concurrent_first_daily_stop_triggers_raise_one_alert(backend: Backend) -> None:
    clock = FrozenClock(NOW)
    open_day(backend.mode(), clock)
    services = [DailyRiskService(backend.mode(), clock) for _ in range(3)]

    run_concurrently(
        [
            lambda service=service: service.record(  # type: ignore[misc]
                Decimal("-50"), Decimal(0), RULES
            )
            for service in services
        ]
    )

    events = [event.event_type for event in backend.mode().outbox_events()]
    assert events.count(EventType.DAILY_STOP_TRIGGERED) == 1
    assert [alert.code for alert in backend.mode().alerts()] == ["DAILY_STOP_TRIGGERED"]


def test_equity_snapshots_and_observations_are_immutable(backend: Backend) -> None:
    repo = backend.mode()
    snap = PortfolioEquitySnapshot(
        observed_at=NOW, equity=Decimal("1000.50"), ruleset_version=RULES
    )
    repo.put_equity_snapshot(snap)
    backend.mode().put_equity_snapshot(snap)
    with pytest.raises(ValueError, match="immutable"):
        backend.mode().put_equity_snapshot(snap.model_copy(update={"equity": Decimal("999")}))

    item = observation(1)
    repo.put_observation(item)
    backend.mode().put_observation(item)
    with pytest.raises(ValueError, match="immutable"):
        backend.mode().put_observation(item.model_copy(update={"decision_count": 11}))
    assert backend.mode().observations(RULES, TradingMode.SHADOW) == (item,)


# --- entries and kill switch ----------------------------------------------------------------


def test_entry_admission_never_exceeds_three_across_connections(backend: Backend) -> None:
    clock = FrozenClock(NOW)
    open_day(backend.mode(), clock)
    repositories = [backend.mode() for _ in range(6)]

    results = run_concurrently(
        [lambda repo=repo: repo.admit_entry(pending(), 3) for repo in repositories]  # type: ignore[misc]
    )

    assert results.count(()) == 3
    assert all(result in {(), ("POSITION_LIMIT_REACHED",)} for result in results)
    assert backend.mode().pending_count() == 3
    assert backend.mode().committed_entry_count(NOW) == 3


def test_entries_fail_closed_until_the_risk_day_is_opened(backend: Backend) -> None:
    repo = backend.mode()
    plan = pending()

    assert repo.admit_entry(plan, 3) == ("RISK_DAY_NOT_OPENED",)
    open_day(repo, FrozenClock(NOW))
    assert backend.mode().admit_entry(plan, 3) == ()
    with pytest.raises(ValueError, match="already admitted"):
        backend.mode().admit_entry(plan, 3)


def test_kill_switch_blocks_claims_but_records_an_in_flight_fill(backend: Backend) -> None:
    clock = FrozenClock(NOW)
    repo = backend.mode()
    open_day(repo, clock)
    in_flight, waiting = pending(), pending()
    assert repo.admit_entry(in_flight, 3) == ()
    assert repo.admit_entry(waiting, 3) == ()
    repo.claim_entry(in_flight.plan_id, NOW)

    result = ModeService(backend.mode(), clock, RULES).activate_kill_switch(
        "operator stop", KillSwitchPolicy.HOLD_AND_ALERT
    )

    assert result.cancelled_plan_count == 1
    other = backend.mode()
    with pytest.raises(ValueError):
        other.claim_entry(waiting.plan_id, NOW)
    other.confirm_entry(in_flight.plan_id, "position-1")
    assert backend.mode().open_positions() == ("position-1",)
    assert backend.mode().admit_entry(pending(), 3) == ("KILL_SWITCH_ACTIVE",)
    with pytest.raises(ValueError, match="expired"):
        expired = pending(minutes=1)
        fresh = backend.mode()
        ModeService(fresh, clock, RULES).release_kill_switch("resolved", CONFIRMATION)
        assert fresh.admit_entry(expired, 3) == ()
        fresh.claim_entry(expired.plan_id, NOW + timedelta(minutes=1))


def test_kill_switch_persists_actions_alert_outbox_and_release(backend: Backend) -> None:
    clock = FrozenClock(NOW)
    repo = backend.mode()
    promote_to_approval(repo, clock)
    open_day(repo, clock)
    open_position(repo, "position-a")
    assert repo.admit_entry(pending(), 3) == ()

    result = ModeService(repo, clock, RULES).activate_kill_switch(
        "manual", KillSwitchPolicy.ORDERLY_EXIT
    )

    restarted = backend.mode()
    state = restarted.mode_state()
    assert state.kill_switch_active is True
    assert state.mode is TradingMode.SHADOW
    assert [action.position_id for action in restarted.position_actions()] == ["position-a"]
    assert all(not action.transaction_submitted for action in restarted.position_actions())
    alert_events = [
        event
        for event in restarted.outbox_events()
        if event.event_type is EventType.CRITICAL_ALERT_RAISED
    ]
    assert alert_events[-1].payload["cancelled_plan_count"] == 1
    assert alert_events[-1].payload["position_action_count"] == 1
    assert result.alert_id == alert_events[-1].aggregate_id
    with pytest.raises(ValueError, match="release the kill switch"):
        restarted.resolve_alert(result.alert_id, "too early", NOW)

    released = ModeService(restarted, clock, RULES).release_kill_switch("ok", CONFIRMATION)
    assert released.kill_switch_active is False
    after = backend.mode()
    assert after.open_alert_count() == 0
    assert after.outbox_events()[-1].payload["change"] == "KILL_SWITCH_RELEASED"
    after.close_position("position-a")
    assert backend.mode().open_positions() == ()
    with pytest.raises(ValueError, match="not open"):
        backend.mode().close_position("position-a")


def test_alert_resolution_requires_reason_and_is_one_shot(backend: Backend) -> None:
    repo = backend.mode()
    alert = CriticalAlert(
        alert_id=uuid4(), code="AUDIT_PERSISTENCE_FAILED", message="audit down", created_at=NOW
    )
    repo.raise_alert(alert)
    assert backend.mode().open_alert_count() == 1
    with pytest.raises(ValueError, match="reason"):
        backend.mode().resolve_alert(alert.alert_id, " ", NOW)

    resolved = backend.mode().resolve_alert(alert.alert_id, "fixed", NOW + timedelta(minutes=1))
    assert resolved.open is False
    assert backend.mode().alerts() == (resolved,)
    with pytest.raises(ValueError, match="already resolved"):
        backend.mode().resolve_alert(alert.alert_id, "again", NOW)


# --- accounting -------------------------------------------------------------------------------


def test_shadow_receipt_is_idempotent_and_survives_restart(backend: Backend) -> None:
    issued = command(DlmmOperation.OPEN, "durable-open")
    first = ShadowBroker(backend.accounting(), FrozenClock(NOW)).apply(
        issued, snapshot("2"), costs()
    )

    restarted = backend.accounting()
    replay = ShadowBroker(restarted, FrozenClock(NOW)).apply(issued, snapshot("5"), costs())

    assert replay == first
    assert restarted.position_for(KEY) == first.position
    assert restarted.receipt_for("durable-open") == first
    with pytest.raises(ValueError, match="idempotency"):
        ShadowBroker(backend.accounting(), FrozenClock(NOW)).apply(
            command(DlmmOperation.OPEN, "durable-open", amount="200"), snapshot("2"), costs()
        )
    rebound = command(DlmmOperation.ADD, "other-key").model_copy(
        update={"command_id": issued.command_id}
    )
    with pytest.raises(ValueError, match="command id"):
        ShadowBroker(backend.accounting(), FrozenClock(NOW)).apply(rebound, snapshot("2"), costs())
    final = backend.accounting()
    assert len(final.ledger_entries()) == 1
    assert [event.event_type for event in final.outbox_events()] == [
        EventType.COMMAND_CONFIRMED,
        EventType.POSITION_OPENED,
    ]


def test_accounting_cas_rejects_a_stale_writer_from_another_connection(backend: Backend) -> None:
    writer = backend.accounting()
    stale = backend.accounting()
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

    current = backend.accounting().position_for(KEY)
    assert current is not None
    assert current.status.value == "OPEN"
    assert current.version == 2
    assert backend.accounting().outcomes() == ()
    assert backend.accounting().receipt_for("close") is None


def test_lifecycle_outcomes_window_and_event_order(backend: Backend) -> None:
    repo = backend.accounting()
    broker = ShadowBroker(repo, FrozenClock(NOW))
    commands = (
        command(DlmmOperation.OPEN, "open"),
        command(DlmmOperation.ADD, "add", amount="10"),
        command(DlmmOperation.REMOVE, "remove"),
        command(DlmmOperation.CLOSE, "close"),
    )
    receipts = [broker.apply(item, snapshot("2"), costs("10")) for item in commands]

    restarted = backend.accounting()
    assert [entry.kind for entry in restarted.ledger_entries()] == [
        LedgerKind.OPEN,
        LedgerKind.ADD,
        LedgerKind.REMOVE,
        LedgerKind.CLOSE,
    ]
    assert [event.event_type for event in restarted.outbox_events()] == [
        EventType.COMMAND_CONFIRMED,
        EventType.POSITION_OPENED,
        EventType.COMMAND_CONFIRMED,
        EventType.COMMAND_CONFIRMED,
        EventType.COMMAND_CONFIRMED,
        EventType.POSITION_CLOSED,
    ]
    window = TimeWindow(start=NOW - timedelta(hours=1), end=NOW + timedelta(hours=1))
    outcomes = restarted.outcomes_in(window)
    assert [outcome.valuation for outcome in outcomes] == [receipts[-1].valuation]
    assert (
        restarted.outcomes_in(
            TimeWindow(start=NOW + timedelta(seconds=1), end=NOW + timedelta(hours=1))
        )
        == ()
    )
    summary = PerformanceService(restarted).summary(window)
    assert summary.groups[0].net_pnl_base_units == receipts[-1].valuation.net_pnl_base_units
    assert [position.status.value for position in restarted.positions()] == ["CLOSED"]
