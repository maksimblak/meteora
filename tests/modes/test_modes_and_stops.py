import sqlite3
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Barrier, BrokenBarrierError
from typing import Any
from uuid import UUID, uuid4

import pytest

from src.core.clock import FrozenClock
from src.domain.models.events import EventType
from src.domain.models.trading import TradingMode
from src.domain.risk.models import LiquidityState, PortfolioState, RiskPolicy, SizedPlan
from src.domain.risk.sizing import PositionSizer
from src.domain.strategy.models import (
    Direction,
    ExitPolicy,
    InvalidationPolicy,
    StrategyKind,
    StrategyPlan,
)
from src.execution.contracts import LivePromotionEvidence
from src.modes import (
    ApprovalService,
    CriticalAlert,
    DailyPromotionObservation,
    DailyRiskService,
    EntryGate,
    InMemoryModeRepository,
    KillSwitchPolicy,
    ModeService,
    ModeTransitionRejected,
    PendingPlan,
    PendingPlanStatus,
    PortfolioEquitySnapshot,
    PromotionEvidenceService,
    PromotionEvidenceSnapshot,
    SqliteModeRepository,
)

NOW = datetime(2026, 10, 20, 12, tzinfo=UTC)
RULES = "rules-v1"
CONFIRMATION = "operator-confirmation"
POLICY = RiskPolicy(ruleset_version=RULES, config_version="config-1")
Repo = InMemoryModeRepository | SqliteModeRepository


def boundary(instant: datetime) -> datetime:
    return datetime.combine(instant.date(), datetime.min.time(), tzinfo=UTC)


def snapshot_at(repo: Repo, observed_at: datetime, equity: str) -> None:
    repo.put_equity_snapshot(
        PortfolioEquitySnapshot(
            observed_at=observed_at, equity=Decimal(equity), ruleset_version=RULES
        )
    )


def open_day(repo: Repo, clock: FrozenClock, equity: str = "1000") -> None:
    snapshot_at(repo, boundary(clock.now()), equity)
    DailyRiskService(repo, clock).record(Decimal(0), Decimal(0), RULES)


def observation(days_ago: int, **changes: Any) -> DailyPromotionObservation:
    values: dict[str, Any] = {
        "risk_date": (NOW - timedelta(days=days_ago)).date(),
        "ruleset_version": RULES,
        "mode": TradingMode.SHADOW,
        "completed_positions": 3,
        "net_pnl": Decimal("1"),
        "start_equity": Decimal("1000"),
        "end_equity": Decimal("1001"),
        "conservative_open_equity": Decimal("1000"),
        "data_coverage": Decimal("0.99"),
        "decision_count": 10,
    }
    values.update(changes)
    return DailyPromotionObservation(**values)


def seed_shadow_evidence(
    repo: Repo, clock: FrozenClock | None = None, **changes: Any
) -> PromotionEvidenceSnapshot:
    service = PromotionEvidenceService(repo, clock or FrozenClock(NOW))
    for days_ago in range(7, 0, -1):
        service.record_daily(observation(days_ago, **changes))
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


def promote_to_approval(repo: Repo, clock: FrozenClock | None = None) -> ModeService:
    clock = clock or FrozenClock(NOW)
    service = ModeService(repo, clock, RULES)
    service.transition(TradingMode.APPROVAL, seed_shadow_evidence(repo, clock), CONFIRMATION)
    return service


def sized_plan(correlation_id: str = "cycle-1") -> SizedPlan:
    plan = StrategyPlan(
        strategy=StrategyKind.SPOT,
        direction=Direction.NEUTRAL,
        lower_bin_offset=-5,
        upper_bin_offset=5,
        holding_horizon=timedelta(hours=4),
        range_exit_probability=Decimal("0.10"),
        invalidation=InvalidationPolicy(
            maximum_adverse_price_move=Decimal("0.10"),
            volume_decay_ratio=Decimal("0.5"),
            expires_at=NOW + timedelta(minutes=5),
        ),
        exit_policy=ExitPolicy(maximum_holding_period=timedelta(hours=4)),
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
        loss_fraction_at_invalidation=Decimal("0.10"),
        expected_cost_rate=Decimal("0.002"),
        ruleset_version=RULES,
        config_version="config-1",
        correlation_id=correlation_id,
    )
    result = PositionSizer().size(
        plan,
        PortfolioState(
            equity_base_units=1000, available_balance_base_units=1000, open_position_count=0
        ),
        LiquidityState(depth_base_units=1_000_000, price_impact_cap_base_units=100_000),
        POLICY,
    )
    assert isinstance(result, SizedPlan)
    return result


def pending(plan_id: UUID | None = None) -> PendingPlan:
    return PendingPlan(
        plan_id=plan_id or uuid4(),
        correlation_id="cycle-1",
        admitted_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
    )


def open_position(repo: Repo, position_id: str) -> None:
    plan = pending()
    assert repo.admit_entry(plan, 3) == ()
    repo.claim_entry(plan.plan_id, NOW)
    repo.confirm_entry(plan.plan_id, position_id)


# --- staged modes -------------------------------------------------------------


def test_initial_and_restored_mode_is_shadow_until_explicit_promotion(tmp_path: Path) -> None:
    path = tmp_path / "modes.sqlite3"
    repo = SqliteModeRepository(path)
    assert repo.mode_state().mode is TradingMode.SHADOW
    promote_to_approval(repo)
    repo.close()

    restarted = SqliteModeRepository(path)
    assert restarted.mode_state().mode is TradingMode.APPROVAL
    ModeService(restarted, FrozenClock(NOW), RULES).activate_kill_switch(
        "operator", KillSwitchPolicy.HOLD_AND_ALERT
    )
    restarted.close()

    again = SqliteModeRepository(path)
    assert again.mode_state().mode is TradingMode.SHADOW
    assert again.mode_state().promotion_authorized is False
    again.close()


def test_mode_machine_rejects_direct_shadow_to_live() -> None:
    repo = InMemoryModeRepository()
    service = ModeService(repo, FrozenClock(NOW), RULES)

    with pytest.raises(ModeTransitionRejected, match="direct"):
        service.transition(TradingMode.LIVE, live_evidence(), CONFIRMATION)
    assert repo.mode_state().mode is TradingMode.SHADOW


def test_shadow_promotion_accepts_exact_boundaries_and_reports_14_day_recommendation() -> None:
    repo = InMemoryModeRepository()
    service = ModeService(repo, FrozenClock(NOW), RULES)
    evidence = seed_shadow_evidence(repo)

    readiness = service.shadow_readiness(evidence)
    transition = service.transition(TradingMode.APPROVAL, evidence, CONFIRMATION)

    assert evidence.full_utc_days == 7
    assert evidence.completed_positions == 21
    assert evidence.decision_count == 70
    assert readiness.allowed
    assert readiness.minimum_days_remaining == 0
    assert readiness.recommended_days == 14
    assert readiness.recommended_days_remaining == 7
    assert readiness.insufficient_statistics is False
    assert transition.to_mode is TradingMode.APPROVAL
    assert repo.transitions()[-1].evidence_id == str(evidence.evidence_id)


def test_promotion_fails_closed_without_evidence_or_operator_confirmation() -> None:
    repo = InMemoryModeRepository()
    service = ModeService(repo, FrozenClock(NOW), RULES)

    with pytest.raises(ModeTransitionRejected, match="evidence"):
        service.transition(TradingMode.APPROVAL, None, CONFIRMATION)
    with pytest.raises(ModeTransitionRejected, match="confirmation"):
        service.transition(TradingMode.APPROVAL, seed_shadow_evidence(repo), " ")
    assert repo.mode_state().mode is TradingMode.SHADOW


def test_upgrade_requires_each_stage_but_downgrade_is_always_available() -> None:
    repo = InMemoryModeRepository()
    service = promote_to_approval(repo)
    with pytest.raises(ModeTransitionRejected, match="live promotion evidence"):
        service.transition(TradingMode.LIVE, None, CONFIRMATION)
    service.transition(TradingMode.LIVE, live_evidence(), CONFIRMATION)
    repo.raise_alert(
        CriticalAlert(alert_id=uuid4(), code="RPC_DOWN", message="rpc", created_at=NOW)
    )

    to_approval = service.transition(TradingMode.APPROVAL, None, "")
    to_shadow = service.transition(TradingMode.SHADOW, None, "")

    assert (to_approval.from_mode, to_approval.to_mode) == (TradingMode.LIVE, TradingMode.APPROVAL)
    assert (to_shadow.from_mode, to_shadow.to_mode) == (TradingMode.APPROVAL, TradingMode.SHADOW)
    assert [event.event_type for event in repo.outbox_events()].count(
        EventType.MODE_CHANGED
    ) == 4


def test_promotion_evidence_and_live_evidence_are_one_shot() -> None:
    repo = InMemoryModeRepository()
    service = ModeService(repo, FrozenClock(NOW), RULES)
    evidence = seed_shadow_evidence(repo)
    service.transition(TradingMode.APPROVAL, evidence, CONFIRMATION)
    live = live_evidence()
    service.transition(TradingMode.LIVE, live, CONFIRMATION)
    service.transition(TradingMode.APPROVAL, None, "")

    with pytest.raises(ModeTransitionRejected, match="already consumed"):
        service.transition(TradingMode.LIVE, live, CONFIRMATION)
    service.transition(TradingMode.SHADOW, None, "")
    with pytest.raises(ModeTransitionRejected, match="already consumed"):
        service.transition(TradingMode.APPROVAL, evidence, CONFIRMATION)
    assert repo.mode_state().mode is TradingMode.SHADOW


def test_concurrent_promotions_with_one_evidence_commit_once() -> None:
    base = InMemoryModeRepository()
    evidence = seed_shadow_evidence(base)
    barrier = Barrier(2, timeout=2)

    class Coordinated:
        def __getattr__(self, name: str) -> Any:
            return getattr(base, name)

        def mode_state(self) -> Any:
            state = base.mode_state()
            try:
                barrier.wait()
            except BrokenBarrierError:
                pass
            return state

    service = ModeService(Coordinated(), FrozenClock(NOW), RULES)  # type: ignore[arg-type]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(service.transition, TradingMode.APPROVAL, evidence, CONFIRMATION)
            for _ in range(2)
        ]
    outcomes = []
    for future in futures:
        try:
            outcomes.append(future.result().to_mode)
        except ModeTransitionRejected:
            outcomes.append(None)

    assert sorted(outcomes, key=str) == [TradingMode.APPROVAL, None]
    assert len(base.transitions()) == 1


def test_promotion_evidence_is_versioned_against_mode_state_and_day() -> None:
    repo = InMemoryModeRepository()
    clock = FrozenClock(NOW)
    service = ModeService(repo, clock, RULES)
    evidence = seed_shadow_evidence(repo, clock)
    service.activate_kill_switch("drill", KillSwitchPolicy.HOLD_AND_ALERT)
    service.release_kill_switch("drill finished", CONFIRMATION)

    with pytest.raises(ModeTransitionRejected, match="stale"):
        service.transition(TradingMode.APPROVAL, evidence, CONFIRMATION)

    fresh = PromotionEvidenceService(repo, clock).build_shadow(RULES)
    clock.advance(timedelta(days=1))
    readiness = service.shadow_readiness(fresh)
    assert "EVIDENCE_STALE" in readiness.reasons
    with pytest.raises(ModeTransitionRejected, match="EVIDENCE_STALE"):
        service.transition(TradingMode.APPROVAL, fresh, CONFIRMATION)


def test_promotion_is_blocked_by_kill_switch_daily_stop_and_open_alerts() -> None:
    killed = InMemoryModeRepository()
    evidence = seed_shadow_evidence(killed)
    ModeService(killed, FrozenClock(NOW), RULES).activate_kill_switch(
        "risk", KillSwitchPolicy.HOLD_AND_ALERT
    )
    with pytest.raises(ModeTransitionRejected, match="risk stop"):
        ModeService(killed, FrozenClock(NOW), RULES).transition(
            TradingMode.APPROVAL, evidence, CONFIRMATION
        )

    stopped = InMemoryModeRepository()
    clock = FrozenClock(NOW)
    open_day(stopped, clock)
    DailyRiskService(stopped, clock).record(Decimal("-40"), Decimal(0), RULES)
    stopped_evidence = seed_shadow_evidence(stopped, clock)
    with pytest.raises(ModeTransitionRejected, match="risk stop"):
        ModeService(stopped, clock, RULES).transition(
            TradingMode.APPROVAL, stopped_evidence, CONFIRMATION
        )

    alerted = InMemoryModeRepository()
    alerted_evidence = seed_shadow_evidence(alerted)
    alerted.raise_alert(
        CriticalAlert(alert_id=uuid4(), code="PROVIDER_DOWN", message="down", created_at=NOW)
    )
    with pytest.raises(ModeTransitionRejected, match="critical alerts"):
        ModeService(alerted, FrozenClock(NOW), RULES).transition(
            TradingMode.APPROVAL, alerted_evidence, CONFIRMATION
        )
    for repo in (killed, stopped, alerted):
        assert repo.mode_state().mode is TradingMode.SHADOW


def test_promotion_counts_only_consecutive_full_utc_days_and_open_drawdown() -> None:
    repo = InMemoryModeRepository()
    evidence_service = PromotionEvidenceService(repo, FrozenClock(NOW))
    for days_ago in (8, 7, 6, 5, 3, 2, 1):
        evidence_service.record_daily(
            observation(
                days_ago,
                end_equity=Decimal("1000"),
                conservative_open_equity=Decimal("950") if days_ago == 1 else Decimal("1000"),
            )
        )
    with pytest.raises(ValueError, match="complete UTC days"):
        evidence_service.record_daily(observation(0))

    evidence = evidence_service.build_shadow(RULES)
    readiness = ModeService(repo, FrozenClock(NOW), RULES).shadow_readiness(evidence)

    assert evidence.full_utc_days == 3
    assert evidence.first_date == (NOW - timedelta(days=3)).date()
    assert evidence.maximum_drawdown == Decimal("0.05")
    assert readiness.minimum_days_remaining == 4
    assert {"SHADOW_DAYS_BELOW_7", "DRAWDOWN_ABOVE_4_PERCENT"} <= set(readiness.reasons)
    assert not readiness.allowed


def test_insufficient_statistics_and_risk_breaches_are_marked_and_block_promotion() -> None:
    repo = InMemoryModeRepository()
    evidence = seed_shadow_evidence(repo, completed_positions=2, risk_invariant_breaches=1)
    service = ModeService(repo, FrozenClock(NOW), RULES)

    readiness = service.shadow_readiness(evidence)

    assert readiness.insufficient_statistics is True
    assert {"COMPLETED_POSITIONS_BELOW_20", "RISK_INVARIANT_BREACHED"} <= set(readiness.reasons)
    with pytest.raises(ModeTransitionRejected, match="COMPLETED_POSITIONS_BELOW_20"):
        service.transition(TradingMode.APPROVAL, evidence, CONFIRMATION)


# --- approvals ----------------------------------------------------------------


def test_approval_is_one_shot_ttl_bound_and_survives_process_store_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "approvals.sqlite3"
    clock = FrozenClock(NOW)
    first_repo = SqliteModeRepository(path)
    promote_to_approval(first_repo, clock)
    grant = ApprovalService(first_repo, clock).issue(
        uuid4(), "a" * 64, "b" * 64, timedelta(minutes=5), "operator", "checked preview"
    )
    first_repo.close()

    restarted = SqliteModeRepository(path)
    clock.advance(timedelta(minutes=1))
    consumed = ApprovalService(restarted, clock).consume(grant.approval_id, "a" * 64, "b" * 64)
    clock.advance(timedelta(minutes=1))
    with pytest.raises(ValueError, match="already consumed"):
        ApprovalService(restarted, clock).consume(grant.approval_id, "a" * 64, "b" * 64)

    assert consumed.consumed_at == NOW + timedelta(minutes=1)
    assert restarted.get_approval(grant.approval_id) == consumed
    assert consumed.comment == "checked preview"
    restarted.close()


def test_expired_or_mismatched_approval_does_not_change_state() -> None:
    repo = InMemoryModeRepository()
    clock = FrozenClock(NOW)
    promote_to_approval(repo, clock)
    service = ApprovalService(repo, clock)
    short = service.issue(uuid4(), "a" * 64, "b" * 64, timedelta(seconds=1), "operator")
    other = service.issue(uuid4(), "a" * 64, "b" * 64, timedelta(minutes=5), "operator")
    clock.advance(timedelta(seconds=1))

    with pytest.raises(ValueError, match="expired"):
        service.consume(short.approval_id, "a" * 64, "b" * 64)
    with pytest.raises(ValueError, match="does not match"):
        service.consume(other.approval_id, "c" * 64, "b" * 64)
    assert repo.get_approval(short.approval_id) == short
    assert repo.get_approval(other.approval_id) == other


def test_approval_requires_active_approval_mode_without_kill_switch() -> None:
    shadow = InMemoryModeRepository()
    with pytest.raises(ValueError, match="APPROVAL mode"):
        ApprovalService(shadow, FrozenClock(NOW)).issue(
            uuid4(), "a" * 64, "b" * 64, timedelta(minutes=5), "operator"
        )

    repo = InMemoryModeRepository()
    modes = promote_to_approval(repo)
    service = ApprovalService(repo, FrozenClock(NOW))
    grant = service.issue(uuid4(), "a" * 64, "b" * 64, timedelta(minutes=5), "operator")
    modes.activate_kill_switch("operator", KillSwitchPolicy.HOLD_AND_ALERT)

    with pytest.raises(ValueError, match="APPROVAL mode"):
        service.consume(grant.approval_id, "a" * 64, "b" * 64)
    assert repo.get_approval(grant.approval_id) == grant


# --- daily stop ---------------------------------------------------------------


def test_daily_stop_triggers_at_exact_four_percent_and_survives_restart(tmp_path: Path) -> None:
    path = tmp_path / "risk.sqlite3"
    clock = FrozenClock(NOW)
    repo = SqliteModeRepository(path)
    open_day(repo, clock)
    service = DailyRiskService(repo, clock)

    before = service.record(Decimal("-39.99"), Decimal("0"), RULES)
    stopped = service.record(Decimal("-30"), Decimal("-10"), RULES)
    repo.close()
    restarted = SqliteModeRepository(path)
    recovered = DailyRiskService(restarted, clock).record(Decimal("5"), Decimal("0"), RULES)

    assert before.daily_stop_triggered is False
    assert stopped.daily_stop_triggered is True
    assert recovered.daily_stop_triggered is True
    assert recovered.starting_equity == Decimal("1000")
    assert DailyRiskService(restarted, clock).entries_allowed() is False
    restarted.close()


def test_positive_unrealized_never_offsets_realized_loss() -> None:
    repo = InMemoryModeRepository()
    clock = FrozenClock(NOW)
    open_day(repo, clock)

    state = DailyRiskService(repo, clock).record(Decimal("-40"), Decimal("500"), RULES)

    assert state.daily_stop_triggered is True


def test_daily_stop_uses_equity_known_at_the_start_of_the_utc_day() -> None:
    repo = InMemoryModeRepository()
    clock = FrozenClock(NOW)
    snapshot_at(repo, boundary(NOW) - timedelta(hours=1), "1000")
    snapshot_at(repo, boundary(NOW) + timedelta(hours=6), "900")
    service = DailyRiskService(repo, clock)

    first = service.record(Decimal("-36"), Decimal("0"), RULES)
    snapshot_at(repo, NOW + timedelta(minutes=1), "500")
    clock.advance(timedelta(minutes=2))
    later = service.record(Decimal("-36"), Decimal("0"), RULES)

    assert first.starting_equity == Decimal("1000")
    assert first.baseline_observed_at == boundary(NOW) - timedelta(hours=1)
    assert later.starting_equity == Decimal("1000")
    assert later.daily_stop_triggered is False


def test_entries_fail_closed_until_the_risk_day_is_opened() -> None:
    repo = InMemoryModeRepository()
    clock = FrozenClock(NOW)

    with pytest.raises(ValueError, match="start-of-day"):
        DailyRiskService(repo, clock).record(Decimal(0), Decimal(0), RULES)
    decision = EntryGate(repo, clock, POLICY).admit(sized_plan(), uuid4())

    assert DailyRiskService(repo, clock).entries_allowed() is False
    assert decision.admitted is False
    assert decision.reasons == ("RISK_DAY_NOT_OPENED",)
    assert repo.entry_plans() == ()


def test_next_utc_day_lifts_daily_stop() -> None:
    repo = InMemoryModeRepository()
    clock = FrozenClock(NOW)
    open_day(repo, clock)
    service = DailyRiskService(repo, clock)
    service.record(Decimal("-40"), Decimal("0"), RULES)
    assert service.entries_allowed() is False

    clock.advance(timedelta(days=1))
    assert service.entries_allowed() is False
    snapshot_at(repo, boundary(clock.now()), "960")
    state = service.record(Decimal("0"), Decimal("0"), RULES)

    assert state.daily_stop_triggered is False
    assert state.starting_equity == Decimal("960")
    assert service.entries_allowed() is True
    assert repo.mode_state().mode is TradingMode.SHADOW


def test_daily_stop_atomically_demotes_mode_with_alert_and_outbox(tmp_path: Path) -> None:
    path = tmp_path / "stop.sqlite3"
    clock = FrozenClock(NOW)
    repo = SqliteModeRepository(path)
    promote_to_approval(repo, clock)
    open_day(repo, clock)
    DailyRiskService(repo, clock).record(Decimal("-41"), Decimal("0"), RULES)
    repo.close()

    restarted = SqliteModeRepository(path)
    state = restarted.mode_state()
    assert state.mode is TradingMode.SHADOW
    assert state.daily_stop_date == NOW.date()
    assert [alert.code for alert in restarted.alerts()] == ["DAILY_STOP_TRIGGERED"]
    assert [event.event_type for event in restarted.outbox_events()][-3:] == [
        EventType.CRITICAL_ALERT_RAISED,
        EventType.DAILY_STOP_TRIGGERED,
        EventType.MODE_CHANGED,
    ]
    last = restarted.transitions()[-1]
    assert (last.from_mode, last.to_mode, last.reason) == (
        TradingMode.APPROVAL,
        TradingMode.SHADOW,
        "DAILY_STOP_TRIGGERED",
    )
    restarted.close()


def test_concurrent_first_daily_stop_triggers_raise_one_alert_and_event() -> None:
    base = InMemoryModeRepository()
    clock = FrozenClock(NOW)
    open_day(base, clock)
    barrier = Barrier(2, timeout=1)

    class Coordinated:
        def __getattr__(self, name: str) -> Any:
            return getattr(base, name)

        def risk_day(self, risk_date: Any) -> Any:
            value = base.risk_day(risk_date)
            try:
                barrier.wait()
            except BrokenBarrierError:
                pass
            return value

    service = DailyRiskService(Coordinated(), clock)  # type: ignore[arg-type]
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(
            pool.map(
                lambda loss: service.record(loss, Decimal("0"), RULES),
                (Decimal("-40"), Decimal("-41")),
            )
        )

    assert [alert.code for alert in base.alerts()] == ["DAILY_STOP_TRIGGERED"]
    stops = [
        event
        for event in base.outbox_events()
        if event.event_type is EventType.DAILY_STOP_TRIGGERED
    ]
    assert len(stops) == 1


def test_daily_stop_is_sticky_under_concurrent_lower_update() -> None:
    repo = InMemoryModeRepository()
    clock = FrozenClock(NOW)
    open_day(repo, clock)
    service = DailyRiskService(repo, clock)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(
            pool.map(
                lambda loss: service.record(loss, Decimal("0"), RULES),
                (Decimal("-40"), Decimal("-1")),
            )
        )

    assert repo.risk_day(NOW.date()).daily_stop_triggered is True  # type: ignore[union-attr]


# --- entries ------------------------------------------------------------------


def test_entry_gate_blocks_the_fourth_entry_with_an_explainable_reason() -> None:
    repo = InMemoryModeRepository()
    clock = FrozenClock(NOW)
    open_day(repo, clock)
    gate = EntryGate(repo, clock, POLICY)

    decisions = [gate.admit(sized_plan(f"cycle-{index}"), uuid4()) for index in range(4)]

    assert [decision.admitted for decision in decisions] == [True, True, True, False]
    assert decisions[3].reasons == ("POSITION_LIMIT_REACHED",)
    assert decisions[3].correlation_id == "cycle-3"
    assert repo.pending_count() == 3


def test_concurrent_admissions_from_separate_connections_never_exceed_three(
    tmp_path: Path,
) -> None:
    path = tmp_path / "entries.sqlite3"
    clock = FrozenClock(NOW)
    open_day(SqliteModeRepository(path), clock)
    barrier = Barrier(6, timeout=5)

    class SameSnapshotReader:
        def __init__(self) -> None:
            self._repo = SqliteModeRepository(path)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._repo, name)

        def committed_entry_count(self, at: datetime) -> int:
            count = self._repo.committed_entry_count(at)
            barrier.wait()
            return count

    gates = [EntryGate(SameSnapshotReader(), clock, POLICY) for _ in range(6)]  # type: ignore[arg-type]
    with ThreadPoolExecutor(max_workers=6) as pool:
        decisions = list(pool.map(lambda gate: gate.admit(sized_plan(), uuid4()), gates))

    assert sum(decision.admitted for decision in decisions) == 3
    assert {reason for decision in decisions for reason in decision.reasons} == {
        "POSITION_LIMIT_REACHED"
    }
    assert SqliteModeRepository(path).pending_count() == 3


def test_entry_gate_rejects_after_daily_stop_and_kill_switch() -> None:
    repo = InMemoryModeRepository()
    clock = FrozenClock(NOW)
    open_day(repo, clock)
    DailyRiskService(repo, clock).record(Decimal("-40"), Decimal("0"), RULES)
    stopped = EntryGate(repo, clock, POLICY).admit(sized_plan(), uuid4())

    killed_repo = InMemoryModeRepository()
    open_day(killed_repo, clock)
    ModeService(killed_repo, clock, RULES).activate_kill_switch(
        "operator", KillSwitchPolicy.HOLD_AND_ALERT
    )
    killed = EntryGate(killed_repo, clock, POLICY).admit(sized_plan(), uuid4())

    assert stopped.reasons == ("DAILY_STOP_REACHED",)
    assert killed.reasons == ("KILL_SWITCH_ACTIVE",)
    assert repo.entry_plans() == () and killed_repo.entry_plans() == ()


def test_sqlite_entry_admission_is_atomic_with_kill_switch_across_connections(
    tmp_path: Path,
) -> None:
    path = tmp_path / "race.sqlite3"
    clock = FrozenClock(NOW)
    killer = SqliteModeRepository(path)
    admitter = SqliteModeRepository(path)
    open_day(killer, clock)
    plan = pending()
    lock = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    lock.execute("BEGIN IMMEDIATE")
    with ThreadPoolExecutor(max_workers=2) as pool:
        admission = pool.submit(admitter.admit_entry, plan, 3)
        time.sleep(0.5)
        kill = pool.submit(
            ModeService(killer, clock, RULES).activate_kill_switch,
            "operator",
            KillSwitchPolicy.HOLD_AND_ALERT,
        )
        lock.execute("ROLLBACK")
    kill.result()
    reasons = admission.result()
    lock.close()

    assert killer.pending_count() == 0
    stored = killer.entry_plan(plan.plan_id)
    if reasons:
        assert reasons == ("KILL_SWITCH_ACTIVE",) and stored is None
    else:
        assert stored is not None and stored.status is PendingPlanStatus.CANCELLED


@pytest.fixture(params=["memory", "sqlite"])
def any_repo(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Repo]:
    repo: Repo = (
        InMemoryModeRepository()
        if request.param == "memory"
        else SqliteModeRepository(tmp_path / "claims.sqlite3")
    )
    yield repo
    repo.close()


def test_pending_plan_cannot_be_claimed_after_daily_stop(any_repo: Repo) -> None:
    clock = FrozenClock(NOW)
    open_day(any_repo, clock)
    plan = pending()
    assert any_repo.admit_entry(plan, 3) == ()

    DailyRiskService(any_repo, clock).record(Decimal("-40"), Decimal("0"), RULES)

    with pytest.raises(ValueError):
        any_repo.claim_entry(plan.plan_id, NOW)
    assert any_repo.entry_plan(plan.plan_id).status is PendingPlanStatus.CANCELLED  # type: ignore[union-attr]
    assert any_repo.pending_count() == 0


def test_pending_plan_cannot_be_claimed_after_mode_downgrade(any_repo: Repo) -> None:
    clock = FrozenClock(NOW)
    service = promote_to_approval(any_repo, clock)
    open_day(any_repo, clock)
    plan = pending()
    assert any_repo.admit_entry(plan, 3) == ()

    service.transition(TradingMode.SHADOW, None, "")

    with pytest.raises(ValueError):
        any_repo.claim_entry(plan.plan_id, NOW)
    assert any_repo.entry_plan(plan.plan_id).status is PendingPlanStatus.CANCELLED  # type: ignore[union-attr]
    assert any_repo.pending_count() == 0


def test_claimed_entry_survives_kill_switch_and_its_fill_is_still_recorded() -> None:
    repo = InMemoryModeRepository()
    clock = FrozenClock(NOW)
    open_day(repo, clock)
    executing = pending()
    waiting = pending()
    assert repo.admit_entry(executing, 3) == ()
    assert repo.admit_entry(waiting, 3) == ()
    repo.claim_entry(executing.plan_id, NOW)

    result = ModeService(repo, clock, RULES).activate_kill_switch(
        "operator", KillSwitchPolicy.ORDERLY_EXIT
    )
    repo.confirm_entry(executing.plan_id, "position-1")

    assert result.cancelled_plan_count == 1
    assert repo.entry_plan(waiting.plan_id).status is PendingPlanStatus.CANCELLED  # type: ignore[union-attr]
    assert repo.open_positions() == ("position-1",)
    with pytest.raises(ValueError, match="kill switch"):
        repo.claim_entry(waiting.plan_id, NOW)
    alert_event = [
        event
        for event in repo.outbox_events()
        if event.event_type is EventType.CRITICAL_ALERT_RAISED
    ][-1]
    assert alert_event.payload["in_flight_entry_count"] == 1


# --- kill switch --------------------------------------------------------------


@pytest.mark.parametrize(
    ("policy", "expected"),
    [
        (KillSwitchPolicy.HOLD_AND_ALERT, "HOLD_AND_ALERT"),
        (KillSwitchPolicy.ORDERLY_EXIT, "ORDERLY_EXIT_REQUIRED"),
    ],
)
def test_kill_switch_cancels_pending_without_panic_transaction(
    policy: KillSwitchPolicy, expected: str
) -> None:
    repo = InMemoryModeRepository()
    clock = FrozenClock(NOW)
    service = promote_to_approval(repo, clock)
    open_day(repo, clock)
    open_position(repo, "position-1")
    assert repo.admit_entry(pending(), 3) == ()

    result = service.activate_kill_switch("operator", policy)

    assert result.cancelled_plan_count == 1
    assert [action.position_id for action in result.position_actions] == ["position-1"]
    assert result.position_actions[0].action.value == expected
    assert result.position_actions[0].transaction_submitted is False
    assert repo.mode_state().mode is TradingMode.SHADOW
    assert repo.pending_count() == 0
    assert repo.admit_entry(pending(), 3) == ("KILL_SWITCH_ACTIVE",)


def test_sqlite_kill_switch_persists_actions_alert_and_outbox_atomically(tmp_path: Path) -> None:
    path = tmp_path / "kill.sqlite3"
    clock = FrozenClock(NOW)
    repo = SqliteModeRepository(path)
    open_day(repo, clock)
    open_position(repo, "position-1")
    assert repo.admit_entry(pending(), 3) == ()
    ModeService(repo, clock, RULES).activate_kill_switch("operator", KillSwitchPolicy.ORDERLY_EXIT)
    repo.close()

    restarted = SqliteModeRepository(path)
    assert restarted.mode_state().kill_switch_active
    assert len(restarted.position_actions()) == 1
    assert [alert.code for alert in restarted.alerts()] == ["KILL_SWITCH_ACTIVE"]
    event = restarted.outbox_events()[-1]
    assert event.event_type is EventType.CRITICAL_ALERT_RAISED
    assert event.payload["cancelled_plan_count"] == 1
    assert event.payload["position_action_count"] == 1
    assert restarted.admit_entry(pending(), 3) == ("KILL_SWITCH_ACTIVE",)
    restarted.close()


def test_kill_outbox_counts_are_derived_inside_the_transaction(tmp_path: Path) -> None:
    path = tmp_path / "late.sqlite3"
    clock = FrozenClock(NOW)
    repo = SqliteModeRepository(path)
    other = SqliteModeRepository(path)
    open_day(repo, clock)
    assert repo.admit_entry(pending(), 3) == ()

    class LateWriter:
        def __getattr__(self, name: str) -> Any:
            return getattr(repo, name)

        def activate_kill_switch(self, *args: Any, **kwargs: Any) -> Any:
            assert other.admit_entry(pending(), 3) == ()
            return repo.activate_kill_switch(*args, **kwargs)

    result = ModeService(LateWriter(), clock, RULES).activate_kill_switch(  # type: ignore[arg-type]
        "operator", KillSwitchPolicy.HOLD_AND_ALERT
    )

    assert result.cancelled_plan_count == 2
    assert repo.outbox_events()[-1].payload["cancelled_plan_count"] == 2
    assert repo.pending_count() == 0


def test_kill_switch_release_requires_reason_and_resolves_its_alert() -> None:
    repo = InMemoryModeRepository()
    service = ModeService(repo, FrozenClock(NOW), RULES)
    result = service.activate_kill_switch("operator", KillSwitchPolicy.HOLD_AND_ALERT)

    with pytest.raises(ValueError, match="reason"):
        service.release_kill_switch("", CONFIRMATION)
    with pytest.raises(ValueError, match="release the kill switch"):
        service.resolve_alert(result.alert_id, "acknowledged", CONFIRMATION)
    released = service.release_kill_switch("incident closed", CONFIRMATION)

    assert released.kill_switch_active is False
    assert released.mode is TradingMode.SHADOW
    assert repo.open_alert_count() == 0
    assert repo.outbox_events()[-1].payload["change"] == "KILL_SWITCH_RELEASED"
    with pytest.raises(ValueError, match="not active"):
        service.release_kill_switch("again", CONFIRMATION)
