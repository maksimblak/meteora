"""Mode, risk-day, entry and kill-switch store with one transactional SQLite adapter.

Every guard that protects money (kill switch, daily stop, position cap, one-shot
evidence and approvals) is re-read inside the same ``BEGIN IMMEDIATE``
transaction that writes the outcome, together with its alert and outbox rows.
"""

import sqlite3
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

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
from src.portfolio.sqlite import MEMORY, SqliteDatabase, sortable_instant

MODE_AGGREGATE_ID = uuid5(NAMESPACE_URL, "meteora-volume-bot:mode")
KILL_SWITCH_ALERT = "KILL_SWITCH_ACTIVE"
DAILY_STOP_ALERT = "DAILY_STOP_TRIGGERED"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS mode_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mode_transitions (
    transition_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mode_approvals (
    approval_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS risk_days (
    risk_date TEXT PRIMARY KEY,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS portfolio_equity (
    observed_at TEXT PRIMARY KEY,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entry_plans (
    plan_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS open_positions (
    position_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS critical_alerts (
    alert_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS promotion_observations (
    observation_key TEXT PRIMARY KEY,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS promotion_evidence (
    evidence_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS live_promotion_claims (
    promotion_id TEXT PRIMARY KEY,
    claimed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kill_position_actions (
    action_key TEXT PRIMARY KEY,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mode_outbox (
    event_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL
)
"""


class ModeRepository(Protocol):
    def mode_state(self) -> ModeState: ...

    def commit_transition(
        self,
        transition: ModeTransition,
        *,
        expected_version: int,
        shadow_evidence_id: UUID | None = None,
        live_promotion_id: UUID | None = None,
    ) -> ModeState: ...

    def put_approval(self, grant: ApprovalGrant) -> None: ...

    def get_approval(self, approval_id: UUID) -> ApprovalGrant | None: ...

    def consume_approval(
        self,
        approval_id: UUID,
        command_digest: str,
        simulation_digest: str,
        consumed_at: datetime,
    ) -> ApprovalGrant: ...

    def put_equity_snapshot(self, snapshot: PortfolioEquitySnapshot) -> None: ...

    def risk_day(self, risk_date: date) -> RiskDayState | None: ...

    def record_risk_day(
        self,
        *,
        realized_pnl: Decimal,
        conservative_unrealized_pnl: Decimal,
        ruleset_version: str,
        observed_at: datetime,
        stop_fraction: Decimal,
    ) -> RiskDayState: ...

    def admit_entry(self, plan: PendingPlan, maximum_open_positions: int) -> tuple[str, ...]: ...

    def claim_entry(self, plan_id: UUID, claimed_at: datetime) -> PendingPlan: ...

    def confirm_entry(self, plan_id: UUID, position_id: str) -> PendingPlan: ...

    def fail_entry(self, plan_id: UUID) -> PendingPlan: ...

    def cancel_plan(self, plan_id: UUID) -> PendingPlan: ...

    def close_position(self, position_id: str) -> None: ...

    def committed_entry_count(self, at: datetime) -> int: ...

    def open_positions(self) -> tuple[str, ...]: ...

    def raise_alert(self, alert: CriticalAlert) -> None: ...

    def resolve_alert(
        self, alert_id: UUID, resolution: str, resolved_at: datetime
    ) -> CriticalAlert: ...

    def open_alert_count(self) -> int: ...

    def activate_kill_switch(
        self,
        *,
        reason: str,
        policy: KillSwitchPolicy,
        occurred_at: datetime,
        ruleset_version: str,
    ) -> KillSwitchResult: ...

    def release_kill_switch(
        self, *, reason: str, operator_confirmation_id: str, released_at: datetime
    ) -> ModeState: ...

    def put_observation(self, observation: DailyPromotionObservation) -> None: ...

    def observations(
        self, ruleset_version: str, mode: TradingMode
    ) -> tuple[DailyPromotionObservation, ...]: ...

    def put_promotion_evidence(self, evidence: PromotionEvidenceSnapshot) -> None: ...

    def promotion_evidence(self, evidence_id: UUID) -> PromotionEvidenceSnapshot | None: ...


class SqliteModeRepository:
    def __init__(self, path: Path | str) -> None:
        self._db = SqliteDatabase(path, _SCHEMA)
        with self._db.transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO mode_state VALUES (1, ?)",
                (ModeState().model_dump_json(),),
            )

    # --- mode -----------------------------------------------------------------

    def mode_state(self) -> ModeState:
        row = self._db.fetch_one("SELECT payload FROM mode_state WHERE singleton = 1")
        return _restore(row)

    def commit_transition(
        self,
        transition: ModeTransition,
        *,
        expected_version: int,
        shadow_evidence_id: UUID | None = None,
        live_promotion_id: UUID | None = None,
    ) -> ModeState:
        with self._db.transaction() as connection:
            state = _state(connection)
            if state.mode is not transition.from_mode:
                raise ValueError("mode changed concurrently")
            # Downgrades only need the source mode to still hold; promotions are
            # compare-and-set on the whole mode version their evidence was checked against.
            if transition.promotion_authorized and state.version != expected_version:
                raise ValueError("mode changed concurrently")
            if transition.promotion_authorized:
                risk_date = transition.occurred_at.astimezone(UTC).date()
                if state.kill_switch_active or state.daily_stop_active_on(risk_date):
                    raise ValueError("risk stop blocks promotion")
                if _open_alert_count(connection):
                    raise ValueError("open critical alerts block promotion")
                if shadow_evidence_id is None and live_promotion_id is None:
                    raise ValueError("promotion requires durable evidence")
            if shadow_evidence_id is not None:
                row = connection.execute(
                    "SELECT payload FROM promotion_evidence WHERE evidence_id = ?",
                    (str(shadow_evidence_id),),
                ).fetchone()
                if row is None:
                    raise ValueError("promotion evidence is not persisted")
                evidence = PromotionEvidenceSnapshot.model_validate_json(row[0], strict=False)
                if evidence.consumed_at is not None:
                    raise ValueError("promotion evidence already consumed")
                if evidence.mode_version != state.version:
                    raise ValueError("promotion evidence is stale for the current mode version")
                consumed = evidence.model_copy(update={"consumed_at": transition.occurred_at})
                connection.execute(
                    "UPDATE promotion_evidence SET payload = ? WHERE evidence_id = ?",
                    (consumed.model_dump_json(), str(shadow_evidence_id)),
                )
            if live_promotion_id is not None:
                try:
                    connection.execute(
                        "INSERT INTO live_promotion_claims VALUES (?, ?)",
                        (str(live_promotion_id), sortable_instant(transition.occurred_at)),
                    )
                except sqlite3.IntegrityError as error:
                    raise ValueError("live promotion evidence already consumed") from error
            authorized = transition.to_mode is not TradingMode.SHADOW and (
                transition.promotion_authorized or state.promotion_authorized
            )
            updated = _next_state(
                state, mode=transition.to_mode, promotion_authorized=authorized
            )
            _write_state(connection, updated)
            # Plans were admitted and risk-checked under the previous mode.
            _cancel_pending(connection)
            _record_transition(connection, transition, uuid4())
            return updated

    # --- approvals ------------------------------------------------------------

    def put_approval(self, grant: ApprovalGrant) -> None:
        with self._db.transaction() as connection:
            _require_approval_mode(_state(connection))
            try:
                connection.execute(
                    "INSERT INTO mode_approvals VALUES (?, ?)",
                    (str(grant.approval_id), grant.model_dump_json()),
                )
            except sqlite3.IntegrityError as error:
                raise ValueError("approval already exists") from error

    def get_approval(self, approval_id: UUID) -> ApprovalGrant | None:
        row = self._db.fetch_one(
            "SELECT payload FROM mode_approvals WHERE approval_id = ?", (str(approval_id),)
        )
        return None if row is None else ApprovalGrant.model_validate_json(row[0])

    def consume_approval(
        self,
        approval_id: UUID,
        command_digest: str,
        simulation_digest: str,
        consumed_at: datetime,
    ) -> ApprovalGrant:
        with self._db.transaction() as connection:
            row = connection.execute(
                "SELECT payload FROM mode_approvals WHERE approval_id = ?", (str(approval_id),)
            ).fetchone()
            if row is None:
                raise ValueError("approval not found")
            grant = ApprovalGrant.model_validate_json(row[0])
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
            _require_approval_mode(_state(connection))
            consumed = grant.model_copy(update={"consumed_at": consumed_at})
            connection.execute(
                "UPDATE mode_approvals SET payload = ? WHERE approval_id = ?",
                (consumed.model_dump_json(), str(approval_id)),
            )
            return consumed

    # --- daily risk -----------------------------------------------------------

    def put_equity_snapshot(self, snapshot: PortfolioEquitySnapshot) -> None:
        key = sortable_instant(snapshot.observed_at)
        with self._db.transaction() as connection:
            row = connection.execute(
                "SELECT payload FROM portfolio_equity WHERE observed_at = ?", (key,)
            ).fetchone()
            if row is not None:
                existing = PortfolioEquitySnapshot.model_validate_json(row[0], strict=False)
                if existing != snapshot:
                    raise ValueError("portfolio equity snapshot is immutable")
                return
            connection.execute(
                "INSERT INTO portfolio_equity VALUES (?, ?)", (key, snapshot.model_dump_json())
            )

    def risk_day(self, risk_date: date) -> RiskDayState | None:
        row = self._db.fetch_one(
            "SELECT payload FROM risk_days WHERE risk_date = ?", (risk_date.isoformat(),)
        )
        return None if row is None else RiskDayState.model_validate_json(row[0], strict=False)

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
        with self._db.transaction() as connection:
            existing = _risk_day(connection, risk_date)
            if existing is None:
                baseline = _baseline_equity(connection, risk_date, observed_at)
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
            connection.execute(
                "INSERT INTO risk_days VALUES (?, ?) "
                "ON CONFLICT(risk_date) DO UPDATE SET payload = excluded.payload",
                (risk_date.isoformat(), state.model_dump_json()),
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
                cancelled = _cancel_pending(connection)
                _insert_alert(connection, alert, correlation_id)
                _append(
                    connection,
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
                _demote(
                    connection,
                    _state(connection),
                    occurred_at=observed_at,
                    reason=DAILY_STOP_ALERT,
                    correlation_id=correlation_id,
                    daily_stop_date=risk_date,
                )
            return state

    # --- entries --------------------------------------------------------------

    def admit_entry(self, plan: PendingPlan, maximum_open_positions: int) -> tuple[str, ...]:
        if plan.status is not PendingPlanStatus.PENDING:
            raise ValueError("only PENDING plans can be admitted")
        risk_date = plan.admitted_at.astimezone(UTC).date()
        with self._db.transaction() as connection:
            state = _state(connection)
            reasons: list[str] = []
            if state.kill_switch_active:
                reasons.append("KILL_SWITCH_ACTIVE")
            day = _risk_day(connection, risk_date)
            if day is None:
                reasons.append("RISK_DAY_NOT_OPENED")
            elif day.daily_stop_triggered or state.daily_stop_active_on(risk_date):
                reasons.append("DAILY_STOP_REACHED")
            if _committed_entries(connection, plan.admitted_at) >= maximum_open_positions:
                reasons.append("POSITION_LIMIT_REACHED")
            if reasons:
                return tuple(reasons)
            try:
                _insert_plan(connection, plan)
            except sqlite3.IntegrityError as error:
                raise ValueError("plan already admitted") from error
            return ()

    def claim_entry(self, plan_id: UUID, claimed_at: datetime) -> PendingPlan:
        """PENDING -> EXECUTING; after this the plan is no longer cancellable."""

        risk_date = claimed_at.astimezone(UTC).date()
        with self._db.transaction() as connection:
            state = _state(connection)
            if state.kill_switch_active:
                raise ValueError("kill switch blocks entry execution")
            day = _risk_day(connection, risk_date)
            if (
                day is None
                or day.daily_stop_triggered
                or state.daily_stop_active_on(risk_date)
            ):
                raise ValueError("daily stop or unopened risk day blocks entry execution")
            plan = _require_plan(connection, plan_id, PendingPlanStatus.PENDING)
            if plan.expires_at <= claimed_at:
                raise ValueError("pending plan expired")
            return _update_plan(connection, plan, status=PendingPlanStatus.EXECUTING)

    def confirm_entry(self, plan_id: UUID, position_id: str) -> PendingPlan:
        """EXECUTING -> OPENED; a fill that happened is recorded even under kill switch."""

        with self._db.transaction() as connection:
            plan = _require_plan(connection, plan_id, PendingPlanStatus.EXECUTING)
            try:
                connection.execute(
                    "INSERT INTO open_positions VALUES (?, ?)", (position_id, str(plan_id))
                )
            except sqlite3.IntegrityError as error:
                raise ValueError("position is already open") from error
            return _update_plan(
                connection, plan, status=PendingPlanStatus.OPENED, position_id=position_id
            )

    def fail_entry(self, plan_id: UUID) -> PendingPlan:
        with self._db.transaction() as connection:
            plan = _require_plan(connection, plan_id, PendingPlanStatus.EXECUTING)
            return _update_plan(connection, plan, status=PendingPlanStatus.FAILED)

    def cancel_plan(self, plan_id: UUID) -> PendingPlan:
        with self._db.transaction() as connection:
            plan = _require_plan(connection, plan_id, PendingPlanStatus.PENDING)
            return _update_plan(connection, plan, status=PendingPlanStatus.CANCELLED)

    def close_position(self, position_id: str) -> None:
        with self._db.transaction() as connection:
            deleted = connection.execute(
                "DELETE FROM open_positions WHERE position_id = ?", (position_id,)
            ).rowcount
            if deleted != 1:
                raise ValueError("position is not open")

    def committed_entry_count(self, at: datetime) -> int:
        with self._db.transaction() as connection:
            return _committed_entries(connection, at)

    def entry_plan(self, plan_id: UUID) -> PendingPlan | None:
        row = self._db.fetch_one(
            "SELECT payload FROM entry_plans WHERE plan_id = ?", (str(plan_id),)
        )
        return None if row is None else PendingPlan.model_validate_json(row[0], strict=False)

    def entry_plans(self) -> tuple[PendingPlan, ...]:
        rows = self._db.fetch_all("SELECT payload FROM entry_plans ORDER BY rowid")
        return tuple(PendingPlan.model_validate_json(row[0], strict=False) for row in rows)

    def pending_count(self) -> int:
        row = self._db.fetch_one(
            "SELECT COUNT(*) FROM entry_plans WHERE status = ?",
            (PendingPlanStatus.PENDING.value,),
        )
        return int(row[0])

    def open_positions(self) -> tuple[str, ...]:
        rows = self._db.fetch_all("SELECT position_id FROM open_positions ORDER BY position_id")
        return tuple(str(row[0]) for row in rows)

    # --- alerts and kill switch ----------------------------------------------

    def raise_alert(self, alert: CriticalAlert) -> None:
        with self._db.transaction() as connection:
            _insert_alert(connection, alert, uuid4())

    def resolve_alert(
        self, alert_id: UUID, resolution: str, resolved_at: datetime
    ) -> CriticalAlert:
        with self._db.transaction() as connection:
            row = connection.execute(
                "SELECT payload FROM critical_alerts WHERE alert_id = ?", (str(alert_id),)
            ).fetchone()
            if row is None:
                raise ValueError("alert not found")
            alert = CriticalAlert.model_validate_json(row[0])
            if not alert.open:
                raise ValueError("alert already resolved")
            if alert.code == KILL_SWITCH_ALERT and _state(connection).kill_switch_active:
                raise ValueError("release the kill switch to resolve its alert")
            resolved = _resolved(alert, resolution, resolved_at)
            _write_alert(connection, resolved)
            return resolved

    def open_alert_count(self) -> int:
        with self._db.transaction() as connection:
            return _open_alert_count(connection)

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
        with self._db.transaction() as connection:
            state = _state(connection)
            cancelled = _cancel_pending(connection)
            in_flight = int(
                connection.execute(
                    "SELECT COUNT(*) FROM entry_plans WHERE status = ?",
                    (PendingPlanStatus.EXECUTING.value,),
                ).fetchone()[0]
            )
            actions = tuple(
                KillSwitchPositionAction(position_id=str(row[0]), action=action_kind)
                for row in connection.execute(
                    "SELECT position_id FROM open_positions ORDER BY position_id"
                ).fetchall()
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
                connection.execute(
                    "INSERT INTO kill_position_actions VALUES (?, ?)",
                    (f"{alert.alert_id}:{action.position_id}", action.model_dump_json()),
                )
            _insert_alert(
                connection,
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
            _demote(
                connection,
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

    def release_kill_switch(
        self, *, reason: str, operator_confirmation_id: str, released_at: datetime
    ) -> ModeState:
        with self._db.transaction() as connection:
            state = _state(connection)
            if not state.kill_switch_active:
                raise ValueError("kill switch is not active")
            for row in connection.execute("SELECT payload FROM critical_alerts").fetchall():
                alert = CriticalAlert.model_validate_json(row[0])
                if alert.open and alert.code == KILL_SWITCH_ALERT:
                    _write_alert(connection, _resolved(alert, reason, released_at))
            updated = _next_state(state, kill_switch_active=False)
            _write_state(connection, updated)
            _append(
                connection,
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

    # --- promotion evidence ---------------------------------------------------

    def put_observation(self, observation: DailyPromotionObservation) -> None:
        key = (
            f"{observation.risk_date.isoformat()}:{observation.ruleset_version}:"
            f"{observation.mode.value}"
        )
        with self._db.transaction() as connection:
            row = connection.execute(
                "SELECT payload FROM promotion_observations WHERE observation_key = ?", (key,)
            ).fetchone()
            if row is not None:
                existing = DailyPromotionObservation.model_validate_json(row[0], strict=False)
                if existing != observation:
                    raise ValueError("promotion observation is immutable")
                return
            connection.execute(
                "INSERT INTO promotion_observations VALUES (?, ?)",
                (key, observation.model_dump_json()),
            )

    def observations(
        self, ruleset_version: str, mode: TradingMode
    ) -> tuple[DailyPromotionObservation, ...]:
        rows = self._db.fetch_all("SELECT payload FROM promotion_observations")
        values = (
            DailyPromotionObservation.model_validate_json(row[0], strict=False) for row in rows
        )
        return tuple(
            sorted(
                (
                    item
                    for item in values
                    if item.ruleset_version == ruleset_version and item.mode is mode
                ),
                key=lambda item: item.risk_date,
            )
        )

    def put_promotion_evidence(self, evidence: PromotionEvidenceSnapshot) -> None:
        with self._db.transaction() as connection:
            try:
                connection.execute(
                    "INSERT INTO promotion_evidence VALUES (?, ?)",
                    (str(evidence.evidence_id), evidence.model_dump_json()),
                )
            except sqlite3.IntegrityError as error:
                raise ValueError("promotion evidence already exists") from error

    def promotion_evidence(self, evidence_id: UUID) -> PromotionEvidenceSnapshot | None:
        row = self._db.fetch_one(
            "SELECT payload FROM promotion_evidence WHERE evidence_id = ?", (str(evidence_id),)
        )
        return (
            None
            if row is None
            else PromotionEvidenceSnapshot.model_validate_json(row[0], strict=False)
        )

    # --- audit views ----------------------------------------------------------

    def transitions(self) -> tuple[ModeTransition, ...]:
        rows = self._db.fetch_all("SELECT payload FROM mode_transitions ORDER BY rowid")
        return tuple(ModeTransition.model_validate_json(row[0]) for row in rows)

    def alerts(self) -> tuple[CriticalAlert, ...]:
        rows = self._db.fetch_all("SELECT payload FROM critical_alerts ORDER BY rowid")
        return tuple(CriticalAlert.model_validate_json(row[0]) for row in rows)

    def position_actions(self) -> tuple[KillSwitchPositionAction, ...]:
        rows = self._db.fetch_all("SELECT payload FROM kill_position_actions ORDER BY rowid")
        return tuple(KillSwitchPositionAction.model_validate_json(row[0]) for row in rows)

    def outbox_events(self) -> tuple[AuditEvent, ...]:
        rows = self._db.fetch_all("SELECT payload FROM mode_outbox ORDER BY rowid")
        return tuple(AuditEvent.model_validate_json(row[0]) for row in rows)

    def close(self) -> None:
        self._db.close()


class InMemoryModeRepository(SqliteModeRepository):
    """Same transactional adapter on a private in-memory database."""

    def __init__(self) -> None:
        super().__init__(MEMORY)


def _restore(row: Any) -> ModeState:
    if row is None:  # pragma: no cover - schema invariant
        return ModeState()
    state = ModeState.model_validate_json(row[0], strict=False)
    restored = restore_trading_mode(
        PersistedModeAuthorization(
            mode=state.mode, promotion_authorized=state.promotion_authorized
        )
    )
    if restored is state.mode:
        return state
    return ModeState(**{**dict(state), "mode": restored, "promotion_authorized": False})


def _state(connection: sqlite3.Connection) -> ModeState:
    return _restore(
        connection.execute("SELECT payload FROM mode_state WHERE singleton = 1").fetchone()
    )


def _next_state(state: ModeState, **changes: Any) -> ModeState:
    return ModeState(**{**dict(state), **changes, "version": state.version + 1})


def _write_state(connection: sqlite3.Connection, state: ModeState) -> None:
    connection.execute(
        "UPDATE mode_state SET payload = ? WHERE singleton = 1", (state.model_dump_json(),)
    )


def _append(
    connection: sqlite3.Connection,
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
    connection.execute(
        "INSERT INTO mode_outbox VALUES (?, ?)", (str(event.event_id), event.model_dump_json())
    )
    return event


def _record_transition(
    connection: sqlite3.Connection, transition: ModeTransition, correlation_id: UUID
) -> None:
    connection.execute(
        "INSERT INTO mode_transitions VALUES (?, ?)",
        (str(transition.transition_id), transition.model_dump_json()),
    )
    _append(
        connection,
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


def _demote(
    connection: sqlite3.Connection,
    state: ModeState,
    *,
    occurred_at: datetime,
    reason: str,
    correlation_id: UUID,
    **flags: Any,
) -> ModeState:
    """Force SHADOW and set safety flags; the transition is recorded when the mode changes."""

    updated = _next_state(
        state, mode=TradingMode.SHADOW, promotion_authorized=False, **flags
    )
    _write_state(connection, updated)
    if state.mode is not TradingMode.SHADOW:
        _record_transition(
            connection,
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


def _risk_day(connection: sqlite3.Connection, risk_date: date) -> RiskDayState | None:
    row = connection.execute(
        "SELECT payload FROM risk_days WHERE risk_date = ?", (risk_date.isoformat(),)
    ).fetchone()
    return None if row is None else RiskDayState.model_validate_json(row[0], strict=False)


def _baseline_equity(
    connection: sqlite3.Connection, risk_date: date, observed_at: datetime
) -> PortfolioEquitySnapshot | None:
    """Last equity known at the UTC boundary; the day's first mark only on a first run."""

    boundary = sortable_instant(datetime.combine(risk_date, time.min, tzinfo=UTC))
    row = connection.execute(
        "SELECT payload FROM portfolio_equity WHERE observed_at <= ? "
        "ORDER BY observed_at DESC LIMIT 1",
        (boundary,),
    ).fetchone()
    if row is None:
        row = connection.execute(
            "SELECT payload FROM portfolio_equity WHERE observed_at > ? AND observed_at <= ? "
            "ORDER BY observed_at ASC LIMIT 1",
            (boundary, sortable_instant(observed_at)),
        ).fetchone()
    return (
        None if row is None else PortfolioEquitySnapshot.model_validate_json(row[0], strict=False)
    )


def _committed_entries(connection: sqlite3.Connection, at: datetime) -> int:
    open_count = connection.execute("SELECT COUNT(*) FROM open_positions").fetchone()[0]
    reserved = connection.execute(
        "SELECT COUNT(*) FROM entry_plans WHERE status = ? OR (status = ? AND expires_at > ?)",
        (
            PendingPlanStatus.EXECUTING.value,
            PendingPlanStatus.PENDING.value,
            sortable_instant(at),
        ),
    ).fetchone()[0]
    return int(open_count) + int(reserved)


def _insert_plan(connection: sqlite3.Connection, plan: PendingPlan) -> None:
    connection.execute(
        "INSERT INTO entry_plans VALUES (?, ?, ?, ?)",
        (
            str(plan.plan_id),
            plan.status.value,
            sortable_instant(plan.expires_at),
            plan.model_dump_json(),
        ),
    )


def _cancel_pending(connection: sqlite3.Connection) -> int:
    """Cancel every not-yet-claimed plan inside the caller's transaction."""

    rows = connection.execute(
        "SELECT payload FROM entry_plans WHERE status = ?", (PendingPlanStatus.PENDING.value,)
    ).fetchall()
    for row in rows:
        plan = PendingPlan.model_validate_json(row[0], strict=False)
        _update_plan(connection, plan, status=PendingPlanStatus.CANCELLED)
    return len(rows)


def _require_plan(
    connection: sqlite3.Connection, plan_id: UUID, status: PendingPlanStatus
) -> PendingPlan:
    row = connection.execute(
        "SELECT payload FROM entry_plans WHERE plan_id = ?", (str(plan_id),)
    ).fetchone()
    if row is None:
        raise ValueError("entry plan not found")
    plan = PendingPlan.model_validate_json(row[0], strict=False)
    if plan.status is not status:
        raise ValueError(f"entry plan is {plan.status.value}, expected {status.value}")
    return plan


def _update_plan(connection: sqlite3.Connection, plan: PendingPlan, **changes: Any) -> PendingPlan:
    updated = PendingPlan(**{**dict(plan), **changes})
    connection.execute(
        "UPDATE entry_plans SET status = ?, payload = ? WHERE plan_id = ?",
        (updated.status.value, updated.model_dump_json(), str(plan.plan_id)),
    )
    return updated


def _insert_alert(
    connection: sqlite3.Connection,
    alert: CriticalAlert,
    correlation_id: UUID,
    details: dict[str, Any] | None = None,
) -> None:
    connection.execute(
        "INSERT INTO critical_alerts VALUES (?, ?)", (str(alert.alert_id), alert.model_dump_json())
    )
    _append(
        connection,
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


def _write_alert(connection: sqlite3.Connection, alert: CriticalAlert) -> None:
    connection.execute(
        "UPDATE critical_alerts SET payload = ? WHERE alert_id = ?",
        (alert.model_dump_json(), str(alert.alert_id)),
    )


def _resolved(alert: CriticalAlert, resolution: str, resolved_at: datetime) -> CriticalAlert:
    if not resolution.strip():
        raise ValueError("alert resolution requires a reason")
    return CriticalAlert(
        **{**dict(alert), "open": False, "resolved_at": resolved_at, "resolution": resolution}
    )


def _open_alert_count(connection: sqlite3.Connection) -> int:
    """Any open critical alert blocks promotion, whatever ruleset raised it."""

    rows = connection.execute("SELECT payload FROM critical_alerts").fetchall()
    return sum(1 for row in rows if CriticalAlert.model_validate_json(row[0]).open)
