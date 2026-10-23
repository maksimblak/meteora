"""PostgreSQL adapter of the T04 ``ModeRepository`` contract (D01).

Every mutation is one PostgreSQL transaction that first takes the mode advisory
transaction lock, so - exactly like the SQLite ``BEGIN IMMEDIATE`` adapter - every
guard that protects money (kill switch, daily stop, position cap, one-shot evidence
and approvals) is read after all earlier writers committed and is written together
with its alert, audit and outbox rows. Events go through the T01 audit and outbox
repositories inside the same transaction.
"""

from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import ScalarResult, delete, func, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.domain.models.events import AuditEvent, EventType
from src.domain.models.trading import (
    PersistedModeAuthorization,
    TradingMode,
    restore_trading_mode,
)
from src.modes.models import (
    SYSTEM_ACTOR,
    ApprovalGrant,
    CriticalAlert,
    DailyPromotionObservation,
    KillSwitchActionKind,
    KillSwitchPolicy,
    KillSwitchPositionAction,
    KillSwitchResult,
    ModeState,
    ModeTransition,
    PendingPlan,
    PendingPlanStatus,
    PortfolioEquitySnapshot,
    PromotionEvidenceSnapshot,
    RiskDayState,
)
from src.modes.repository import DAILY_STOP_ALERT, KILL_SWITCH_ALERT, MODE_AGGREGATE_ID
from src.persistence.events import append_event, events_of_types
from src.persistence.models import (
    AlertRecord,
    EntryPlanRecord,
    KillPositionActionRecord,
    LivePromotionClaimRecord,
    ModeApprovalRecord,
    ModeStateRecord,
    ModeTransitionRecord,
    OpenPositionRecord,
    PortfolioEquityRecord,
    PromotionEvidenceRecord,
    PromotionObservationRecord,
    RiskDayRecord,
)
from src.persistence.postgres import PostgresDatabase, Work, advisory_key
from src.persistence.serialization import from_payload, to_payload

MODE_LOCK = advisory_key("meteora-volume-bot:mode-repository")
MODE_EVENT_TYPES = frozenset(
    {EventType.MODE_CHANGED, EventType.DAILY_STOP_TRIGGERED, EventType.CRITICAL_ALERT_RAISED}
)
_OPEN = "OPEN"
_RESOLVED = "RESOLVED"
_CRITICAL = "CRITICAL"


class PostgresModeRepository:
    def __init__(self, database: PostgresDatabase, *, owns_database: bool = False) -> None:
        self._db = database
        self._owns_database = owns_database
        self._db.run(_ensure_state, lock=MODE_LOCK)

    def _write[T](self, work: Work[T]) -> T:
        return self._db.run(work, lock=MODE_LOCK)

    def _read[T](self, work: Work[T]) -> T:
        return self._db.run(work)

    # --- mode -----------------------------------------------------------------

    def mode_state(self) -> ModeState:
        return self._read(_state)

    def commit_transition(
        self,
        transition: ModeTransition,
        *,
        expected_version: int,
        shadow_evidence_id: UUID | None = None,
        live_promotion_id: UUID | None = None,
    ) -> ModeState:
        async def work(session: AsyncSession) -> ModeState:
            state = await _state(session)
            if state.mode is not transition.from_mode:
                raise ValueError("mode changed concurrently")
            if transition.promotion_authorized and state.version != expected_version:
                raise ValueError("mode changed concurrently")
            if transition.promotion_authorized:
                risk_date = transition.occurred_at.astimezone(UTC).date()
                if state.kill_switch_active or state.daily_stop_active_on(risk_date):
                    raise ValueError("risk stop blocks promotion")
                if await _open_alert_count(session):
                    raise ValueError("open critical alerts block promotion")
                if shadow_evidence_id is None and live_promotion_id is None:
                    raise ValueError("promotion requires durable evidence")
            if shadow_evidence_id is not None:
                evidence = await _evidence(session, shadow_evidence_id)
                if evidence is None:
                    raise ValueError("promotion evidence is not persisted")
                if evidence.consumed_at is not None:
                    raise ValueError("promotion evidence already consumed")
                if evidence.mode_version != state.version:
                    raise ValueError("promotion evidence is stale for the current mode version")
                consumed = evidence.model_copy(update={"consumed_at": transition.occurred_at})
                await session.execute(
                    update(PromotionEvidenceRecord)
                    .where(PromotionEvidenceRecord.evidence_id == shadow_evidence_id)
                    .values(payload=to_payload(consumed))
                )
            if live_promotion_id is not None:
                claimed = await session.scalar(
                    select(LivePromotionClaimRecord.promotion_id).where(
                        LivePromotionClaimRecord.promotion_id == live_promotion_id
                    )
                )
                if claimed is not None:
                    raise ValueError("live promotion evidence already consumed")
                await session.execute(
                    insert(LivePromotionClaimRecord).values(
                        promotion_id=live_promotion_id, claimed_at=transition.occurred_at
                    )
                )
            authorized = transition.to_mode is not TradingMode.SHADOW and (
                transition.promotion_authorized or state.promotion_authorized
            )
            updated = _next_state(state, mode=transition.to_mode, promotion_authorized=authorized)
            await _write_state(session, updated)
            await _cancel_pending(session)
            await _record_transition(session, transition, uuid4())
            return updated

        return self._write(work)

    # --- approvals ------------------------------------------------------------

    def put_approval(self, grant: ApprovalGrant) -> None:
        async def work(session: AsyncSession) -> None:
            _require_approval_mode(await _state(session))
            if await _approval(session, grant.approval_id) is not None:
                raise ValueError("approval already exists")
            await session.execute(insert(ModeApprovalRecord).values(**_approval_values(grant)))

        self._write(work)

    def get_approval(self, approval_id: UUID) -> ApprovalGrant | None:
        async def work(session: AsyncSession) -> ApprovalGrant | None:
            return await _approval(session, approval_id)

        return self._read(work)

    def consume_approval(
        self,
        approval_id: UUID,
        command_digest: str,
        simulation_digest: str,
        consumed_at: datetime,
    ) -> ApprovalGrant:
        async def work(session: AsyncSession) -> ApprovalGrant:
            grant = await _approval(session, approval_id)
            if grant is None:
                raise ValueError("approval not found")
            if grant.consumed_at is not None:
                raise ValueError("approval already consumed")
            if consumed_at < grant.issued_at:
                raise ValueError("approval is not yet valid")
            if consumed_at >= grant.expires_at:
                raise ValueError("approval expired")
            if (
                grant.command_digest != command_digest
                or grant.simulation_digest != simulation_digest
            ):
                raise ValueError("approval does not match preview")
            _require_approval_mode(await _state(session))
            consumed = grant.model_copy(update={"consumed_at": consumed_at})
            await session.execute(
                update(ModeApprovalRecord)
                .where(ModeApprovalRecord.approval_id == approval_id)
                .values(consumed_at=consumed_at)
            )
            return consumed

        return self._write(work)

    # --- daily risk -----------------------------------------------------------

    def put_equity_snapshot(self, snapshot: PortfolioEquitySnapshot) -> None:
        async def work(session: AsyncSession) -> None:
            row = (
                await session.execute(
                    select(
                        PortfolioEquityRecord.observed_at,
                        PortfolioEquityRecord.equity,
                        PortfolioEquityRecord.ruleset_version,
                    ).where(PortfolioEquityRecord.observed_at == snapshot.observed_at)
                )
            ).one_or_none()
            if row is not None:
                if _equity(row) != snapshot:
                    raise ValueError("portfolio equity snapshot is immutable")
                return
            await session.execute(
                insert(PortfolioEquityRecord).values(
                    observed_at=snapshot.observed_at,
                    equity=snapshot.equity,
                    ruleset_version=snapshot.ruleset_version,
                )
            )

        self._write(work)

    def risk_day(self, risk_date: date) -> RiskDayState | None:
        async def work(session: AsyncSession) -> RiskDayState | None:
            return await _risk_day(session, risk_date)

        return self._read(work)

    def record_risk_day(
        self,
        *,
        realized_pnl: Decimal,
        conservative_unrealized_pnl: Decimal,
        ruleset_version: str,
        observed_at: datetime,
        stop_fraction: Decimal,
    ) -> RiskDayState:
        risk_date = observed_at.astimezone(UTC).date()

        async def work(session: AsyncSession) -> RiskDayState:
            existing = await _risk_day(session, risk_date)
            if existing is None:
                baseline = await _baseline_equity(session, risk_date, observed_at)
                if baseline is None:
                    raise ValueError("start-of-day portfolio equity is required")
                starting_equity = baseline.equity
                baseline_observed_at = baseline.observed_at
                day_ruleset = ruleset_version
            else:
                starting_equity = existing.starting_equity
                baseline_observed_at = existing.baseline_observed_at
                day_ruleset = existing.ruleset_version
            conservative_pnl = realized_pnl + min(conservative_unrealized_pnl, Decimal(0))
            reached = conservative_pnl <= -(starting_equity * stop_fraction)
            already = existing is not None and existing.daily_stop_triggered
            triggered_at = (
                existing.triggered_at
                if existing is not None and already
                else (observed_at if reached else None)
            )
            state = RiskDayState(
                risk_date=risk_date,
                starting_equity=starting_equity,
                baseline_observed_at=baseline_observed_at,
                realized_pnl=realized_pnl,
                conservative_unrealized_pnl=conservative_unrealized_pnl,
                daily_stop_triggered=already or reached,
                triggered_at=triggered_at,
                ruleset_version=day_ruleset,
                updated_at=observed_at,
            )
            values = _risk_day_values(state)
            await session.execute(
                pg_insert(RiskDayRecord)
                .values(id=uuid5(NAMESPACE_URL, f"risk-day:{risk_date.isoformat()}"), **values)
                .on_conflict_do_update(index_elements=[RiskDayRecord.risk_date], set_=values)
            )
            if reached and not already:
                correlation_id = uuid4()
                alert = CriticalAlert(
                    alert_id=uuid4(),
                    code=DAILY_STOP_ALERT,
                    message="daily loss limit reached; new entries are blocked for the UTC day",
                    created_at=observed_at,
                    ruleset_version=day_ruleset,
                )
                cancelled = await _cancel_pending(session)
                await _insert_alert(session, alert, correlation_id)
                await _append(
                    session,
                    EventType.DAILY_STOP_TRIGGERED,
                    observed_at,
                    correlation_id,
                    {
                        "risk_date": risk_date.isoformat(),
                        "starting_equity": str(starting_equity),
                        "conservative_pnl": str(conservative_pnl),
                        "stop_fraction": str(stop_fraction),
                        "alert_id": str(alert.alert_id),
                        "cancelled_plan_count": cancelled,
                    },
                )
                await _demote(
                    session,
                    await _state(session),
                    occurred_at=observed_at,
                    reason=DAILY_STOP_ALERT,
                    correlation_id=correlation_id,
                    daily_stop_date=risk_date,
                )
            return state

        return self._write(work)

    # --- entries --------------------------------------------------------------

    def admit_entry(self, plan: PendingPlan, maximum_open_positions: int) -> tuple[str, ...]:
        if plan.status is not PendingPlanStatus.PENDING:
            raise ValueError("only PENDING plans can be admitted")
        risk_date = plan.admitted_at.astimezone(UTC).date()

        async def work(session: AsyncSession) -> tuple[str, ...]:
            state = await _state(session)
            reasons: list[str] = []
            if state.kill_switch_active:
                reasons.append("KILL_SWITCH_ACTIVE")
            day = await _risk_day(session, risk_date)
            if day is None:
                reasons.append("RISK_DAY_NOT_OPENED")
            elif day.daily_stop_triggered or state.daily_stop_active_on(risk_date):
                reasons.append("DAILY_STOP_REACHED")
            if await _committed_entries(session, plan.admitted_at) >= maximum_open_positions:
                reasons.append("POSITION_LIMIT_REACHED")
            if reasons:
                return tuple(reasons)
            if await _plan(session, plan.plan_id) is not None:
                raise ValueError("plan already admitted")
            await session.execute(insert(EntryPlanRecord).values(**_plan_values(plan)))
            return ()

        return self._write(work)

    def claim_entry(self, plan_id: UUID, claimed_at: datetime) -> PendingPlan:
        """PENDING -> EXECUTING; after this the plan is no longer cancellable."""

        risk_date = claimed_at.astimezone(UTC).date()

        async def work(session: AsyncSession) -> PendingPlan:
            state = await _state(session)
            if state.kill_switch_active:
                raise ValueError("kill switch blocks entry execution")
            day = await _risk_day(session, risk_date)
            if day is None or day.daily_stop_triggered or state.daily_stop_active_on(risk_date):
                raise ValueError("daily stop or unopened risk day blocks entry execution")
            plan = await _require_plan(session, plan_id, PendingPlanStatus.PENDING)
            if plan.expires_at <= claimed_at:
                raise ValueError("pending plan expired")
            return await _update_plan(session, plan, status=PendingPlanStatus.EXECUTING)

        return self._write(work)

    def confirm_entry(self, plan_id: UUID, position_id: str) -> PendingPlan:
        """EXECUTING -> OPENED; a fill that happened is recorded even under kill switch."""

        async def work(session: AsyncSession) -> PendingPlan:
            plan = await _require_plan(session, plan_id, PendingPlanStatus.EXECUTING)
            exists = await session.scalar(
                select(OpenPositionRecord.position_id).where(
                    OpenPositionRecord.position_id == position_id
                )
            )
            if exists is not None:
                raise ValueError("position is already open")
            await session.execute(
                insert(OpenPositionRecord).values(position_id=position_id, plan_id=plan_id)
            )
            return await _update_plan(
                session, plan, status=PendingPlanStatus.OPENED, position_id=position_id
            )

        return self._write(work)

    def fail_entry(self, plan_id: UUID) -> PendingPlan:
        async def work(session: AsyncSession) -> PendingPlan:
            plan = await _require_plan(session, plan_id, PendingPlanStatus.EXECUTING)
            return await _update_plan(session, plan, status=PendingPlanStatus.FAILED)

        return self._write(work)

    def cancel_plan(self, plan_id: UUID) -> PendingPlan:
        async def work(session: AsyncSession) -> PendingPlan:
            plan = await _require_plan(session, plan_id, PendingPlanStatus.PENDING)
            return await _update_plan(session, plan, status=PendingPlanStatus.CANCELLED)

        return self._write(work)

    def close_position(self, position_id: str) -> None:
        async def work(session: AsyncSession) -> None:
            deleted = await session.execute(
                delete(OpenPositionRecord)
                .where(OpenPositionRecord.position_id == position_id)
                .returning(OpenPositionRecord.position_id)
            )
            if len(deleted.all()) != 1:
                raise ValueError("position is not open")

        self._write(work)

    def committed_entry_count(self, at: datetime) -> int:
        async def work(session: AsyncSession) -> int:
            return await _committed_entries(session, at)

        return self._write(work)

    def entry_plan(self, plan_id: UUID) -> PendingPlan | None:
        async def work(session: AsyncSession) -> PendingPlan | None:
            return await _plan(session, plan_id)

        return self._read(work)

    def entry_plans(self) -> tuple[PendingPlan, ...]:
        async def work(session: AsyncSession) -> tuple[PendingPlan, ...]:
            rows = await session.execute(_plan_columns().order_by(EntryPlanRecord.seq))
            return tuple(_pending_plan(row) for row in rows)

        return self._read(work)

    def pending_count(self) -> int:
        async def work(session: AsyncSession) -> int:
            count = await session.scalar(
                select(func.count())
                .select_from(EntryPlanRecord)
                .where(EntryPlanRecord.status == PendingPlanStatus.PENDING.value)
            )
            return int(count or 0)

        return self._read(work)

    def open_positions(self) -> tuple[str, ...]:
        async def work(session: AsyncSession) -> tuple[str, ...]:
            rows = await session.scalars(
                select(OpenPositionRecord.position_id).order_by(OpenPositionRecord.position_id)
            )
            return tuple(rows)

        return self._read(work)

    # --- alerts and kill switch ----------------------------------------------

    def raise_alert(self, alert: CriticalAlert) -> None:
        async def work(session: AsyncSession) -> None:
            await _insert_alert(session, alert, uuid4())

        self._write(work)

    def resolve_alert(
        self, alert_id: UUID, resolution: str, resolved_at: datetime
    ) -> CriticalAlert:
        async def work(session: AsyncSession) -> CriticalAlert:
            row = (
                await session.execute(_alert_columns().where(AlertRecord.id == alert_id))
            ).one_or_none()
            if row is None:
                raise ValueError("alert not found")
            alert = _alert(row)
            if not alert.open:
                raise ValueError("alert already resolved")
            if alert.code == KILL_SWITCH_ALERT and (await _state(session)).kill_switch_active:
                raise ValueError("release the kill switch to resolve its alert")
            resolved = _resolved(alert, resolution, resolved_at)
            await _write_alert(session, resolved)
            return resolved

        return self._write(work)

    def open_alert_count(self) -> int:
        return self._write(_open_alert_count)

    def activate_kill_switch(
        self,
        *,
        reason: str,
        policy: KillSwitchPolicy,
        occurred_at: datetime,
        ruleset_version: str,
    ) -> KillSwitchResult:
        action_kind = (
            KillSwitchActionKind.HOLD_AND_ALERT
            if policy is KillSwitchPolicy.HOLD_AND_ALERT
            else KillSwitchActionKind.ORDERLY_EXIT_REQUIRED
        )

        async def work(session: AsyncSession) -> KillSwitchResult:
            state = await _state(session)
            cancelled = await _cancel_pending(session)
            in_flight = int(
                await session.scalar(
                    select(func.count())
                    .select_from(EntryPlanRecord)
                    .where(EntryPlanRecord.status == PendingPlanStatus.EXECUTING.value)
                )
                or 0
            )
            positions = await session.scalars(
                select(OpenPositionRecord.position_id).order_by(OpenPositionRecord.position_id)
            )
            actions = tuple(
                KillSwitchPositionAction(position_id=position_id, action=action_kind)
                for position_id in positions
            )
            correlation_id = uuid4()
            alert = CriticalAlert(
                alert_id=uuid4(),
                code=KILL_SWITCH_ALERT,
                message=reason,
                created_at=occurred_at,
                ruleset_version=ruleset_version,
            )
            for action in actions:
                await session.execute(
                    insert(KillPositionActionRecord).values(
                        action_key=f"{alert.alert_id}:{action.position_id}",
                        alert_id=alert.alert_id,
                        position_id=action.position_id,
                        action=action.action.value,
                    )
                )
            await _insert_alert(
                session,
                alert,
                correlation_id,
                {
                    "policy": policy.value,
                    "cancelled_plan_count": cancelled,
                    "in_flight_entry_count": in_flight,
                    "position_action_count": len(actions),
                    "position_actions": [
                        {"position_id": action.position_id, "action": action.action.value}
                        for action in actions
                    ],
                },
            )
            await _demote(
                session,
                state,
                occurred_at=occurred_at,
                reason=KILL_SWITCH_ALERT,
                correlation_id=correlation_id,
                kill_switch_active=True,
            )
            return KillSwitchResult(
                activated_at=occurred_at,
                reason=reason,
                policy=policy,
                cancelled_plan_count=cancelled,
                position_actions=actions,
                alert_id=alert.alert_id,
            )

        return self._write(work)

    def release_kill_switch(
        self, *, reason: str, operator_confirmation_id: str, released_at: datetime
    ) -> ModeState:
        async def work(session: AsyncSession) -> ModeState:
            state = await _state(session)
            if not state.kill_switch_active:
                raise ValueError("kill switch is not active")
            rows = await session.execute(
                _alert_columns().where(
                    AlertRecord.status == _OPEN, AlertRecord.code == KILL_SWITCH_ALERT
                )
            )
            for row in rows.all():
                await _write_alert(session, _resolved(_alert(row), reason, released_at))
            updated = _next_state(state, kill_switch_active=False)
            await _write_state(session, updated)
            await _append(
                session,
                EventType.MODE_CHANGED,
                released_at,
                uuid4(),
                {
                    "change": "KILL_SWITCH_RELEASED",
                    "mode": updated.mode.value,
                    "reason": reason,
                    "operator_confirmation_id": operator_confirmation_id,
                },
            )
            return updated

        return self._write(work)

    # --- promotion evidence ---------------------------------------------------

    def put_observation(self, observation: DailyPromotionObservation) -> None:
        async def work(session: AsyncSession) -> None:
            payload = await session.scalar(
                select(PromotionObservationRecord.payload).where(
                    PromotionObservationRecord.risk_date == observation.risk_date,
                    PromotionObservationRecord.ruleset_version == observation.ruleset_version,
                    PromotionObservationRecord.mode == observation.mode.value,
                )
            )
            if payload is not None:
                if from_payload(DailyPromotionObservation, payload) != observation:
                    raise ValueError("promotion observation is immutable")
                return
            await session.execute(
                insert(PromotionObservationRecord).values(
                    risk_date=observation.risk_date,
                    ruleset_version=observation.ruleset_version,
                    mode=observation.mode.value,
                    payload=to_payload(observation),
                )
            )

        self._write(work)

    def observations(
        self, ruleset_version: str, mode: TradingMode
    ) -> tuple[DailyPromotionObservation, ...]:
        async def work(session: AsyncSession) -> tuple[DailyPromotionObservation, ...]:
            payloads: ScalarResult[dict[str, Any]] = await session.scalars(
                select(PromotionObservationRecord.payload)
                .where(
                    PromotionObservationRecord.ruleset_version == ruleset_version,
                    PromotionObservationRecord.mode == mode.value,
                )
                .order_by(PromotionObservationRecord.risk_date)
            )
            return tuple(from_payload(DailyPromotionObservation, payload) for payload in payloads)

        return self._read(work)

    def put_promotion_evidence(self, evidence: PromotionEvidenceSnapshot) -> None:
        async def work(session: AsyncSession) -> None:
            if await _evidence(session, evidence.evidence_id) is not None:
                raise ValueError("promotion evidence already exists")
            await session.execute(
                insert(PromotionEvidenceRecord).values(
                    evidence_id=evidence.evidence_id, payload=to_payload(evidence)
                )
            )

        self._write(work)

    def promotion_evidence(self, evidence_id: UUID) -> PromotionEvidenceSnapshot | None:
        async def work(session: AsyncSession) -> PromotionEvidenceSnapshot | None:
            return await _evidence(session, evidence_id)

        return self._read(work)

    # --- audit views ----------------------------------------------------------

    def transitions(self) -> tuple[ModeTransition, ...]:
        async def work(session: AsyncSession) -> tuple[ModeTransition, ...]:
            rows = await session.execute(
                select(
                    ModeTransitionRecord.id,
                    ModeTransitionRecord.from_mode,
                    ModeTransitionRecord.to_mode,
                    ModeTransitionRecord.occurred_at,
                    ModeTransitionRecord.operator_confirmation_id,
                    ModeTransitionRecord.promotion_authorized,
                    ModeTransitionRecord.reason,
                    ModeTransitionRecord.evidence_id,
                ).order_by(ModeTransitionRecord.seq)
            )
            return tuple(
                ModeTransition(
                    transition_id=row.id,
                    from_mode=TradingMode(row.from_mode),
                    to_mode=TradingMode(row.to_mode),
                    occurred_at=row.occurred_at,
                    operator_confirmation_id=row.operator_confirmation_id,
                    promotion_authorized=row.promotion_authorized,
                    reason=row.reason,
                    evidence_id=row.evidence_id,
                )
                for row in rows
            )

        return self._read(work)

    def alerts(self) -> tuple[CriticalAlert, ...]:
        async def work(session: AsyncSession) -> tuple[CriticalAlert, ...]:
            rows = await session.execute(
                _alert_columns().where(AlertRecord.severity == _CRITICAL).order_by(AlertRecord.seq)
            )
            return tuple(_alert(row) for row in rows)

        return self._read(work)

    def position_actions(self) -> tuple[KillSwitchPositionAction, ...]:
        async def work(session: AsyncSession) -> tuple[KillSwitchPositionAction, ...]:
            rows = await session.execute(
                select(
                    KillPositionActionRecord.position_id, KillPositionActionRecord.action
                ).order_by(KillPositionActionRecord.seq)
            )
            return tuple(
                KillSwitchPositionAction(
                    position_id=row.position_id, action=KillSwitchActionKind(row.action)
                )
                for row in rows
            )

        return self._read(work)

    def outbox_events(self) -> tuple[AuditEvent, ...]:
        async def work(session: AsyncSession) -> tuple[AuditEvent, ...]:
            return await events_of_types(session, MODE_EVENT_TYPES)

        return self._read(work)

    def close(self) -> None:
        if self._owns_database:
            self._db.close()


# --- helpers (all run inside the caller's transaction) --------------------------------


async def _ensure_state(session: AsyncSession) -> None:
    initial = ModeState()
    await session.execute(
        pg_insert(ModeStateRecord)
        .values(
            singleton=1,
            mode=initial.mode.value,
            promotion_authorized=initial.promotion_authorized,
            kill_switch_active=initial.kill_switch_active,
            daily_stop_date=initial.daily_stop_date,
            version=initial.version,
        )
        .on_conflict_do_nothing(index_elements=[ModeStateRecord.singleton])
    )


async def _state(session: AsyncSession) -> ModeState:
    row = (
        await session.execute(
            select(
                ModeStateRecord.mode,
                ModeStateRecord.promotion_authorized,
                ModeStateRecord.kill_switch_active,
                ModeStateRecord.daily_stop_date,
                ModeStateRecord.version,
            ).where(ModeStateRecord.singleton == 1)
        )
    ).one_or_none()
    if row is None:  # pragma: no cover - schema invariant
        return ModeState()
    state = ModeState(
        mode=TradingMode(row.mode),
        promotion_authorized=row.promotion_authorized,
        kill_switch_active=row.kill_switch_active,
        daily_stop_date=row.daily_stop_date,
        version=row.version,
    )
    restored = restore_trading_mode(
        PersistedModeAuthorization(mode=state.mode, promotion_authorized=state.promotion_authorized)
    )
    if restored is state.mode:
        return state
    return ModeState(**{**dict(state), "mode": restored, "promotion_authorized": False})


def _next_state(state: ModeState, **changes: Any) -> ModeState:
    return ModeState(**{**dict(state), **changes, "version": state.version + 1})


async def _write_state(session: AsyncSession, state: ModeState) -> None:
    await session.execute(
        update(ModeStateRecord)
        .where(ModeStateRecord.singleton == 1)
        .values(
            mode=state.mode.value,
            promotion_authorized=state.promotion_authorized,
            kill_switch_active=state.kill_switch_active,
            daily_stop_date=state.daily_stop_date,
            version=state.version,
            updated_at=func.now(),
        )
    )


async def _append(
    session: AsyncSession,
    kind: EventType,
    occurred_at: datetime,
    correlation_id: UUID,
    payload: dict[str, Any],
    aggregate_id: UUID = MODE_AGGREGATE_ID,
) -> AuditEvent:
    event = AuditEvent(
        event_id=uuid4(),
        occurred_at=occurred_at,
        aggregate_id=aggregate_id,
        correlation_id=correlation_id,
        causation_id=None,
        schema_version=1,
        event_type=kind,
        payload=payload,
    )
    await append_event(session, event)
    return event


async def _record_transition(
    session: AsyncSession, transition: ModeTransition, correlation_id: UUID
) -> None:
    await session.execute(
        insert(ModeTransitionRecord).values(
            id=transition.transition_id,
            from_mode=transition.from_mode.value,
            to_mode=transition.to_mode.value,
            promotion_authorized=transition.promotion_authorized,
            correlation_id=correlation_id,
            occurred_at=transition.occurred_at,
            operator_confirmation_id=transition.operator_confirmation_id,
            reason=transition.reason,
            evidence_id=transition.evidence_id,
        )
    )
    await _append(
        session,
        EventType.MODE_CHANGED,
        transition.occurred_at,
        correlation_id,
        {
            "transition_id": str(transition.transition_id),
            "from_mode": transition.from_mode.value,
            "to_mode": transition.to_mode.value,
            "promotion_authorized": transition.promotion_authorized,
            "operator_confirmation_id": transition.operator_confirmation_id,
            "reason": transition.reason,
            "evidence_id": transition.evidence_id,
        },
    )


async def _demote(
    session: AsyncSession,
    state: ModeState,
    *,
    occurred_at: datetime,
    reason: str,
    correlation_id: UUID,
    **flags: Any,
) -> ModeState:
    updated = _next_state(state, mode=TradingMode.SHADOW, promotion_authorized=False, **flags)
    await _write_state(session, updated)
    if state.mode is not TradingMode.SHADOW:
        await _record_transition(
            session,
            ModeTransition(
                transition_id=uuid4(),
                from_mode=state.mode,
                to_mode=TradingMode.SHADOW,
                occurred_at=occurred_at,
                operator_confirmation_id=SYSTEM_ACTOR,
                promotion_authorized=False,
                reason=reason,
            ),
            correlation_id,
        )
    return updated


def _require_approval_mode(state: ModeState) -> None:
    if state.mode is not TradingMode.APPROVAL or state.kill_switch_active:
        raise ValueError("approvals require an active APPROVAL mode without kill switch")


def _approval_values(grant: ApprovalGrant) -> dict[str, Any]:
    return {
        "approval_id": grant.approval_id,
        "command_id": grant.command_id,
        "command_digest": grant.command_digest,
        "simulation_digest": grant.simulation_digest,
        "operator_id": grant.operator_id,
        "comment": grant.comment,
        "issued_at": grant.issued_at,
        "expires_at": grant.expires_at,
        "consumed_at": grant.consumed_at,
    }


async def _approval(session: AsyncSession, approval_id: UUID) -> ApprovalGrant | None:
    row = (
        await session.execute(
            select(
                ModeApprovalRecord.approval_id,
                ModeApprovalRecord.command_id,
                ModeApprovalRecord.command_digest,
                ModeApprovalRecord.simulation_digest,
                ModeApprovalRecord.operator_id,
                ModeApprovalRecord.comment,
                ModeApprovalRecord.issued_at,
                ModeApprovalRecord.expires_at,
                ModeApprovalRecord.consumed_at,
            ).where(ModeApprovalRecord.approval_id == approval_id)
        )
    ).one_or_none()
    return None if row is None else ApprovalGrant(**row._asdict())


def _equity(row: Any) -> PortfolioEquitySnapshot:
    return PortfolioEquitySnapshot(
        observed_at=row.observed_at, equity=row.equity, ruleset_version=row.ruleset_version
    )


def _risk_day_values(state: RiskDayState) -> dict[str, Any]:
    return {
        "risk_date": state.risk_date,
        "starting_equity": state.starting_equity,
        "realized_pnl": state.realized_pnl,
        "unrealized_pnl": state.conservative_unrealized_pnl,
        "daily_stop_triggered": state.daily_stop_triggered,
        "ruleset_version": state.ruleset_version,
        "baseline_observed_at": state.baseline_observed_at,
        "triggered_at": state.triggered_at,
        "updated_at": state.updated_at,
    }


async def _risk_day(session: AsyncSession, risk_date: date) -> RiskDayState | None:
    row = (
        await session.execute(
            select(
                RiskDayRecord.risk_date,
                RiskDayRecord.starting_equity,
                RiskDayRecord.baseline_observed_at,
                RiskDayRecord.realized_pnl,
                RiskDayRecord.unrealized_pnl,
                RiskDayRecord.daily_stop_triggered,
                RiskDayRecord.triggered_at,
                RiskDayRecord.ruleset_version,
                RiskDayRecord.updated_at,
            ).where(RiskDayRecord.risk_date == risk_date)
        )
    ).one_or_none()
    if row is None:
        return None
    return RiskDayState(
        risk_date=row.risk_date,
        starting_equity=row.starting_equity,
        baseline_observed_at=row.baseline_observed_at,
        realized_pnl=row.realized_pnl,
        conservative_unrealized_pnl=row.unrealized_pnl,
        daily_stop_triggered=row.daily_stop_triggered,
        triggered_at=row.triggered_at,
        ruleset_version=row.ruleset_version,
        updated_at=row.updated_at,
    )


async def _baseline_equity(
    session: AsyncSession, risk_date: date, observed_at: datetime
) -> PortfolioEquitySnapshot | None:
    """Last equity known at the UTC boundary; the day's first mark only on a first run."""

    boundary = datetime.combine(risk_date, time.min, tzinfo=UTC)
    columns = select(
        PortfolioEquityRecord.observed_at,
        PortfolioEquityRecord.equity,
        PortfolioEquityRecord.ruleset_version,
    )
    row = (
        await session.execute(
            columns.where(PortfolioEquityRecord.observed_at <= boundary)
            .order_by(PortfolioEquityRecord.observed_at.desc())
            .limit(1)
        )
    ).one_or_none()
    if row is None:
        row = (
            await session.execute(
                columns.where(
                    PortfolioEquityRecord.observed_at > boundary,
                    PortfolioEquityRecord.observed_at <= observed_at,
                )
                .order_by(PortfolioEquityRecord.observed_at.asc())
                .limit(1)
            )
        ).one_or_none()
    return None if row is None else _equity(row)


async def _committed_entries(session: AsyncSession, at: datetime) -> int:
    open_count = await session.scalar(select(func.count()).select_from(OpenPositionRecord))
    reserved = await session.scalar(
        select(func.count())
        .select_from(EntryPlanRecord)
        .where(
            or_(
                EntryPlanRecord.status == PendingPlanStatus.EXECUTING.value,
                (EntryPlanRecord.status == PendingPlanStatus.PENDING.value)
                & (EntryPlanRecord.expires_at > at),
            )
        )
    )
    return int(open_count or 0) + int(reserved or 0)


def _plan_values(plan: PendingPlan) -> dict[str, Any]:
    return {
        "plan_id": plan.plan_id,
        "correlation_id": plan.correlation_id,
        "admitted_at": plan.admitted_at,
        "expires_at": plan.expires_at,
        "position_id": plan.position_id,
        "status": plan.status.value,
    }


def _plan_columns() -> Any:
    return select(
        EntryPlanRecord.plan_id,
        EntryPlanRecord.correlation_id,
        EntryPlanRecord.admitted_at,
        EntryPlanRecord.expires_at,
        EntryPlanRecord.position_id,
        EntryPlanRecord.status,
    )


def _pending_plan(row: Any) -> PendingPlan:
    return PendingPlan(
        plan_id=row.plan_id,
        correlation_id=row.correlation_id,
        admitted_at=row.admitted_at,
        expires_at=row.expires_at,
        position_id=row.position_id,
        status=PendingPlanStatus(row.status),
    )


async def _plan(session: AsyncSession, plan_id: UUID) -> PendingPlan | None:
    row = (
        await session.execute(_plan_columns().where(EntryPlanRecord.plan_id == plan_id))
    ).one_or_none()
    return None if row is None else _pending_plan(row)


async def _cancel_pending(session: AsyncSession) -> int:
    """Cancel every not-yet-claimed plan inside the caller's transaction."""

    cancelled = await session.execute(
        update(EntryPlanRecord)
        .where(EntryPlanRecord.status == PendingPlanStatus.PENDING.value)
        .values(status=PendingPlanStatus.CANCELLED.value)
        .returning(EntryPlanRecord.plan_id)
    )
    return len(cancelled.all())


async def _require_plan(
    session: AsyncSession, plan_id: UUID, status: PendingPlanStatus
) -> PendingPlan:
    plan = await _plan(session, plan_id)
    if plan is None:
        raise ValueError("entry plan not found")
    if plan.status is not status:
        raise ValueError(f"entry plan is {plan.status.value}, expected {status.value}")
    return plan


async def _update_plan(session: AsyncSession, plan: PendingPlan, **changes: Any) -> PendingPlan:
    updated = PendingPlan(**{**dict(plan), **changes})
    await session.execute(
        update(EntryPlanRecord)
        .where(EntryPlanRecord.plan_id == plan.plan_id)
        .values(status=updated.status.value, position_id=updated.position_id)
    )
    return updated


def _alert_columns() -> Any:
    return select(
        AlertRecord.id,
        AlertRecord.code,
        AlertRecord.message,
        AlertRecord.created_at,
        AlertRecord.ruleset_version,
        AlertRecord.status,
        AlertRecord.acknowledged_at,
        AlertRecord.resolution,
    )


def _alert(row: Any) -> CriticalAlert:
    return CriticalAlert(
        alert_id=row.id,
        code=row.code,
        message=row.message,
        created_at=row.created_at,
        ruleset_version=row.ruleset_version,
        open=row.status == _OPEN,
        resolved_at=row.acknowledged_at,
        resolution=row.resolution,
    )


async def _insert_alert(
    session: AsyncSession,
    alert: CriticalAlert,
    correlation_id: UUID,
    details: dict[str, Any] | None = None,
) -> None:
    await session.execute(
        insert(AlertRecord).values(
            id=alert.alert_id,
            severity=_CRITICAL,
            status=_OPEN if alert.open else _RESOLVED,
            message=alert.message,
            correlation_id=correlation_id,
            created_at=alert.created_at,
            acknowledged_at=alert.resolved_at,
            code=alert.code,
            ruleset_version=alert.ruleset_version,
            resolution=alert.resolution,
        )
    )
    await _append(
        session,
        EventType.CRITICAL_ALERT_RAISED,
        alert.created_at,
        correlation_id,
        {
            "alert_id": str(alert.alert_id),
            "code": alert.code,
            "message": alert.message,
            "ruleset_version": alert.ruleset_version,
            **(details or {}),
        },
        aggregate_id=alert.alert_id,
    )


async def _write_alert(session: AsyncSession, alert: CriticalAlert) -> None:
    await session.execute(
        update(AlertRecord)
        .where(AlertRecord.id == alert.alert_id)
        .values(
            status=_OPEN if alert.open else _RESOLVED,
            acknowledged_at=alert.resolved_at,
            resolution=alert.resolution,
        )
    )


def _resolved(alert: CriticalAlert, resolution: str, resolved_at: datetime) -> CriticalAlert:
    if not resolution.strip():
        raise ValueError("alert resolution requires a reason")
    return CriticalAlert(
        **{**dict(alert), "open": False, "resolved_at": resolved_at, "resolution": resolution}
    )


async def _open_alert_count(session: AsyncSession) -> int:
    """Any open critical alert blocks promotion, whatever ruleset raised it."""

    count = await session.scalar(
        select(func.count())
        .select_from(AlertRecord)
        .where(AlertRecord.status == _OPEN, AlertRecord.severity == _CRITICAL)
    )
    return int(count or 0)


async def _evidence(session: AsyncSession, evidence_id: UUID) -> PromotionEvidenceSnapshot | None:
    payload = await session.scalar(
        select(PromotionEvidenceRecord.payload).where(
            PromotionEvidenceRecord.evidence_id == evidence_id
        )
    )
    return None if payload is None else from_payload(PromotionEvidenceSnapshot, payload)
