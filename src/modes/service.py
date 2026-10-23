"""Fail-closed staged promotions, one-shot approvals, daily stop, entries and kill switch."""

from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_FLOOR, Decimal
from uuid import UUID, uuid4

from src.core.clock import Clock
from src.domain.models.trading import TradingMode
from src.domain.risk.engine import RiskEngine
from src.domain.risk.models import RiskDecisionStatus, RiskPolicy, RiskState, SizedPlan
from src.execution.contracts import LivePromotionEvidence
from src.modes.models import (
    MAXIMUM_PROMOTION_DRAWDOWN,
    MINIMUM_COMPLETED_POSITIONS,
    MINIMUM_DATA_COVERAGE,
    MINIMUM_SHADOW_DAYS,
    RECOMMENDED_SHADOW_DAYS,
    ApprovalGrant,
    CriticalAlert,
    DailyPromotionObservation,
    EntryDecision,
    KillSwitchPolicy,
    KillSwitchResult,
    ModeState,
    ModeTransition,
    PendingPlan,
    PromotionEvidenceSnapshot,
    RiskDayState,
    ShadowReadiness,
)
from src.modes.repository import ModeRepository

_RANK = {TradingMode.SHADOW: 0, TradingMode.APPROVAL: 1, TradingMode.LIVE: 2}


class ModeTransitionRejected(ValueError):
    pass


def _utc_date(instant: datetime) -> date:
    return instant.astimezone(UTC).date()


class ModeService:
    def __init__(self, repository: ModeRepository, clock: Clock, ruleset_version: str) -> None:
        if not ruleset_version.strip():
            raise ValueError("ruleset_version is required")
        self._repository = repository
        self._clock = clock
        self._ruleset_version = ruleset_version

    def mode_state(self) -> ModeState:
        return self._repository.mode_state()

    def shadow_readiness(self, evidence: PromotionEvidenceSnapshot) -> ShadowReadiness:
        yesterday = _utc_date(self._clock.now()) - timedelta(days=1)
        reasons: list[str] = []
        if evidence.from_mode is not TradingMode.SHADOW:
            reasons.append("EVIDENCE_NOT_FROM_SHADOW")
        if evidence.through_date != yesterday:
            reasons.append("EVIDENCE_STALE")
        if evidence.full_utc_days < MINIMUM_SHADOW_DAYS:
            reasons.append("SHADOW_DAYS_BELOW_7")
        insufficient = evidence.completed_positions < MINIMUM_COMPLETED_POSITIONS
        if insufficient:
            reasons.append("COMPLETED_POSITIONS_BELOW_20")
        if evidence.net_expectancy <= 0:
            reasons.append("NET_EXPECTANCY_NOT_POSITIVE")
        if evidence.maximum_drawdown > MAXIMUM_PROMOTION_DRAWDOWN:
            reasons.append("DRAWDOWN_ABOVE_4_PERCENT")
        if evidence.data_coverage < MINIMUM_DATA_COVERAGE:
            reasons.append("DATA_COVERAGE_BELOW_99_PERCENT")
        if evidence.risk_invariant_breaches:
            reasons.append("RISK_INVARIANT_BREACHED")
        if evidence.open_critical_alerts:
            reasons.append("CRITICAL_ALERTS_OPEN")
        if evidence.consumed_at is not None:
            reasons.append("EVIDENCE_ALREADY_CONSUMED")
        return ShadowReadiness(
            allowed=not reasons,
            reasons=tuple(reasons),
            full_shadow_days=evidence.full_utc_days,
            minimum_days_remaining=max(MINIMUM_SHADOW_DAYS - evidence.full_utc_days, 0),
            recommended_days_remaining=max(RECOMMENDED_SHADOW_DAYS - evidence.full_utc_days, 0),
            completed_positions=evidence.completed_positions,
            decision_count=evidence.decision_count,
            insufficient_statistics=insufficient,
            net_expectancy=evidence.net_expectancy,
            net_expectancy_positive=evidence.net_expectancy > 0,
            maximum_drawdown=evidence.maximum_drawdown,
            data_coverage=evidence.data_coverage,
            risk_invariant_breaches=evidence.risk_invariant_breaches,
            open_critical_alerts=evidence.open_critical_alerts,
        )

    def transition(
        self,
        target: TradingMode,
        evidence: PromotionEvidenceSnapshot | LivePromotionEvidence | None,
        operator_confirmation_id: str,
        reason: str = "",
    ) -> ModeTransition:
        now = self._clock.now()
        state = self._repository.mode_state()
        current = state.mode
        if target is current:
            raise ModeTransitionRejected("mode is already active")
        promotion = _RANK[target] > _RANK[current]
        shadow_evidence_id: UUID | None = None
        live_promotion_id: UUID | None = None
        if promotion:
            if current is TradingMode.SHADOW and target is TradingMode.LIVE:
                raise ModeTransitionRejected("direct SHADOW to LIVE transition is forbidden")
            if state.kill_switch_active or state.daily_stop_active_on(_utc_date(now)):
                raise ModeTransitionRejected("risk stop blocks promotion")
            if not operator_confirmation_id.strip():
                raise ModeTransitionRejected("operator confirmation is required")
            if target is TradingMode.APPROVAL:
                shadow_evidence_id = self._validate_shadow_evidence(evidence, state)
            else:
                live_promotion_id = self._validate_live_evidence(
                    evidence, operator_confirmation_id, now
                )
        evidence_id = shadow_evidence_id or live_promotion_id
        transition = ModeTransition(
            transition_id=uuid4(),
            from_mode=current,
            to_mode=target,
            occurred_at=now,
            operator_confirmation_id=operator_confirmation_id,
            promotion_authorized=promotion,
            reason=reason or ("operator promotion" if promotion else "operator downgrade"),
            evidence_id=None if evidence_id is None else str(evidence_id),
        )
        try:
            self._repository.commit_transition(
                transition,
                expected_version=state.version,
                shadow_evidence_id=shadow_evidence_id,
                live_promotion_id=live_promotion_id,
            )
        except ValueError as error:
            raise ModeTransitionRejected(str(error)) from error
        return transition

    def activate_kill_switch(self, reason: str, policy: KillSwitchPolicy) -> KillSwitchResult:
        if not reason.strip():
            raise ValueError("kill switch reason is required")
        return self._repository.activate_kill_switch(
            reason=reason,
            policy=policy,
            occurred_at=self._clock.now(),
            ruleset_version=self._ruleset_version,
        )

    def release_kill_switch(self, reason: str, operator_confirmation_id: str) -> ModeState:
        if not reason.strip() or not operator_confirmation_id.strip():
            raise ValueError("release requires reason and operator confirmation")
        return self._repository.release_kill_switch(
            reason=reason,
            operator_confirmation_id=operator_confirmation_id,
            released_at=self._clock.now(),
        )

    def resolve_alert(
        self, alert_id: UUID, resolution: str, operator_confirmation_id: str
    ) -> CriticalAlert:
        if not resolution.strip() or not operator_confirmation_id.strip():
            raise ValueError("alert resolution requires reason and operator confirmation")
        return self._repository.resolve_alert(
            alert_id, f"{operator_confirmation_id}: {resolution}", self._clock.now()
        )

    def _validate_shadow_evidence(
        self,
        evidence: PromotionEvidenceSnapshot | LivePromotionEvidence | None,
        state: ModeState,
    ) -> UUID:
        if not isinstance(evidence, PromotionEvidenceSnapshot):
            raise ModeTransitionRejected("shadow promotion evidence is required")
        persisted = self._repository.promotion_evidence(evidence.evidence_id)
        if persisted is None:
            raise ModeTransitionRejected("promotion evidence is not persisted")
        if persisted.consumed_at is not None:
            raise ModeTransitionRejected("promotion evidence already consumed")
        if persisted != evidence:
            raise ModeTransitionRejected("promotion evidence payload changed")
        if evidence.ruleset_version != self._ruleset_version:
            raise ModeTransitionRejected("promotion evidence ruleset is stale")
        if evidence.mode_version != state.version:
            raise ModeTransitionRejected("promotion evidence is stale for the current mode version")
        readiness = self.shadow_readiness(persisted)
        if not readiness.allowed:
            raise ModeTransitionRejected(
                "shadow promotion gate failed: " + ",".join(readiness.reasons)
            )
        return evidence.evidence_id

    def _validate_live_evidence(
        self,
        evidence: PromotionEvidenceSnapshot | LivePromotionEvidence | None,
        operator_confirmation_id: str,
        now: datetime,
    ) -> UUID:
        if not isinstance(evidence, LivePromotionEvidence):
            raise ModeTransitionRejected("live promotion evidence is required")
        if evidence.ruleset_version != self._ruleset_version:
            raise ModeTransitionRejected("live evidence ruleset is stale")
        if evidence.approval_cycles < 10:
            raise ModeTransitionRejected("approval cycles below 10")
        if not all(
            (
                evidence.receipts_reconciled,
                evidence.risk_invariants_preserved,
                evidence.dedicated_rpc_ready,
                evidence.wallet_ready,
                evidence.checklist_completed,
            )
        ):
            raise ModeTransitionRejected("live readiness is incomplete")
        if evidence.operator_confirmation_id != operator_confirmation_id:
            raise ModeTransitionRejected("operator confirmation does not match evidence")
        if evidence.promoted_at > now or evidence.expires_at <= now:
            raise ModeTransitionRejected("live promotion evidence is stale")
        return evidence.promotion_id


class ApprovalService:
    """One-shot, TTL-bound, preview-bound grants usable only in APPROVAL mode."""

    def __init__(self, repository: ModeRepository, clock: Clock) -> None:
        self._repository = repository
        self._clock = clock

    def issue(
        self,
        command_id: UUID,
        command_digest: str,
        simulation_digest: str,
        ttl: timedelta,
        operator_id: str,
        comment: str = "",
    ) -> ApprovalGrant:
        if ttl <= timedelta(0):
            raise ValueError("approval TTL must be positive")
        now = self._clock.now()
        grant = ApprovalGrant(
            approval_id=uuid4(),
            command_id=command_id,
            command_digest=command_digest,
            simulation_digest=simulation_digest,
            operator_id=operator_id,
            comment=comment,
            issued_at=now,
            expires_at=now + ttl,
        )
        self._repository.put_approval(grant)
        return grant

    def consume(
        self, approval_id: UUID, command_digest: str, simulation_digest: str
    ) -> ApprovalGrant:
        return self._repository.consume_approval(
            approval_id, command_digest, simulation_digest, self._clock.now()
        )


class DailyRiskService:
    """4% stop against equity at the start of the UTC risk day, sticky until the next day."""

    def __init__(
        self,
        repository: ModeRepository,
        clock: Clock,
        stop_fraction: Decimal = Decimal("0.04"),
    ) -> None:
        if isinstance(stop_fraction, float) or not Decimal(0) < stop_fraction <= Decimal("0.04"):
            raise ValueError("daily stop fraction must be in (0, 0.04]")
        self._repository = repository
        self._clock = clock
        self._stop_fraction = stop_fraction

    def record(
        self,
        realized_pnl: Decimal,
        conservative_unrealized_pnl: Decimal,
        ruleset_version: str,
    ) -> RiskDayState:
        if any(
            not isinstance(value, Decimal) for value in (realized_pnl, conservative_unrealized_pnl)
        ):
            raise ValueError("daily risk values must use Decimal")
        return self._repository.record_risk_day(
            realized_pnl=realized_pnl,
            conservative_unrealized_pnl=conservative_unrealized_pnl,
            ruleset_version=ruleset_version,
            observed_at=self._clock.now(),
            stop_fraction=self._stop_fraction,
        )

    def entries_allowed(self) -> bool:
        """Fail closed: an unopened risk day, a stop or the kill switch blocks entries."""

        risk_date = _utc_date(self._clock.now())
        day = self._repository.risk_day(risk_date)
        mode = self._repository.mode_state()
        return not (
            day is None
            or day.daily_stop_triggered
            or mode.daily_stop_active_on(risk_date)
            or mode.kill_switch_active
        )

    def risk_state(self) -> RiskState | None:
        now = self._clock.now()
        risk_date = _utc_date(now)
        day = self._repository.risk_day(risk_date)
        if day is None:
            return None
        mode = self._repository.mode_state()
        return RiskState(
            as_of=now,
            risk_day_start=datetime.combine(risk_date, time.min, tzinfo=UTC),
            start_of_day_equity_base_units=_floor(day.starting_equity),
            realized_pnl_base_units=_floor(day.realized_pnl),
            conservative_unrealized_pnl_base_units=_floor(
                min(day.conservative_unrealized_pnl, Decimal(0))
            ),
            open_position_count=self._repository.committed_entry_count(now),
            kill_switch_active=mode.kill_switch_active,
        )


class EntryGate:
    """Runs the pure pre-trade risk engine on durable state, then admits atomically."""

    def __init__(self, repository: ModeRepository, clock: Clock, policy: RiskPolicy) -> None:
        self._repository = repository
        self._clock = clock
        self._policy = policy
        self._daily = DailyRiskService(repository, clock, policy.daily_stop_fraction)

    def admit(self, plan: SizedPlan, plan_id: UUID) -> EntryDecision:
        state = self._daily.risk_state()
        if state is None:
            return self._decision(False, plan_id, plan, ("RISK_DAY_NOT_OPENED",))
        decision = RiskEngine(self._policy).pre_trade(plan, state)
        if decision.status is RiskDecisionStatus.REJECT:
            return self._decision(
                False, plan_id, plan, tuple(reason.value for reason in decision.reasons)
            )
        reasons = self._repository.admit_entry(
            PendingPlan(
                plan_id=plan_id,
                correlation_id=plan.correlation_id,
                admitted_at=state.as_of,
                expires_at=plan.plan.expires_at,
            ),
            self._policy.maximum_open_positions,
        )
        return self._decision(not reasons, plan_id, plan, reasons)

    @staticmethod
    def _decision(
        admitted: bool, plan_id: UUID, plan: SizedPlan, reasons: tuple[str, ...]
    ) -> EntryDecision:
        return EntryDecision(
            admitted=admitted,
            plan_id=plan_id,
            reasons=reasons,
            correlation_id=plan.correlation_id,
        )


class PromotionEvidenceService:
    def __init__(self, repository: ModeRepository, clock: Clock) -> None:
        self._repository = repository
        self._clock = clock

    def record_daily(self, observation: DailyPromotionObservation) -> None:
        if observation.risk_date >= _utc_date(self._clock.now()):
            raise ValueError("promotion observations must be complete UTC days")
        self._repository.put_observation(observation)

    def build_shadow(self, ruleset_version: str) -> PromotionEvidenceSnapshot:
        """Persist versioned evidence over the consecutive full UTC days ending yesterday."""

        now = self._clock.now()
        mode = self._repository.mode_state()
        if mode.mode is not TradingMode.SHADOW:
            raise ValueError("shadow evidence is built only while in SHADOW mode")
        yesterday = _utc_date(now) - timedelta(days=1)
        by_date = {
            item.risk_date: item
            for item in self._repository.observations(ruleset_version, TradingMode.SHADOW)
        }
        selected: list[DailyPromotionObservation] = []
        cursor = yesterday
        while cursor in by_date:
            selected.append(by_date[cursor])
            cursor -= timedelta(days=1)
        selected.reverse()
        completed = sum(item.completed_positions for item in selected)
        net = sum((item.net_pnl for item in selected), Decimal(0))
        evidence = PromotionEvidenceSnapshot(
            evidence_id=uuid4(),
            ruleset_version=ruleset_version,
            from_mode=TradingMode.SHADOW,
            mode_version=mode.version,
            generated_at=now,
            first_date=selected[0].risk_date if selected else None,
            through_date=yesterday,
            full_utc_days=len(selected),
            completed_positions=completed,
            decision_count=sum(item.decision_count for item in selected),
            net_expectancy=net / Decimal(completed) if completed else Decimal(0),
            maximum_drawdown=_maximum_drawdown(selected),
            data_coverage=min((item.data_coverage for item in selected), default=Decimal(0)),
            risk_invariant_breaches=sum(item.risk_invariant_breaches for item in selected),
            open_critical_alerts=self._repository.open_alert_count(),
        )
        self._repository.put_promotion_evidence(evidence)
        return evidence


def _maximum_drawdown(days: list[DailyPromotionObservation]) -> Decimal:
    """Peak-to-trough drawdown including each day's conservative open-position trough."""

    high_water = Decimal(0)
    drawdown = Decimal(0)
    for item in days:
        high_water = max(high_water, item.start_equity)
        trough = min(item.end_equity, item.conservative_open_equity)
        drawdown = max(drawdown, (high_water - trough) / high_water)
        high_water = max(high_water, item.end_equity)
    return drawdown


def _floor(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_FLOOR))
