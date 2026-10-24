"""APPROVAL queue, preview and the operator's one-shot approval (G02, G17).

The approval itself is ``ExecutionService.approve`` (T05) with a proof signed by the
same ``EXECUTOR_APPROVAL_SECRET`` the gateway verifies; this desk only adds what the
operator must see and confirm first: irreversibility, simulation, expiry, costs, the
plan diff, decision flags (D03/G18) and the manual checklist (G17).

Order of an approval: re-authentication -> checklist -> pending phase -> readiness ->
preview digests -> audit record -> ``approve``. The audit record precedes the state
change, so an approval the audit trail does not know about cannot exist (R29i.1).
A repeated submit of the same ``approval_id`` returns the original state.

The executor swaps no tokens (D08): an entry's quoted price impact must be measured and
within the command's slippage limit (``entry_impact_rejection``, the execution service's
own rule and codes), while an exit's quote is shown and audited but never blocks it - an
exit is needed most in a thin or falling pool. For the same reason a daily stop, or a
kill switch activated with ``ORDERLY_EXIT`` (the policy recorded in the mode state), and
the demotion to SHADOW it causes do not block the exit of an APPROVAL position (spec 7.3).
"""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from pydantic import SecretStr

from src.api import labels, presenters
from src.api.context import OperatorContext
from src.api.errors import ApiError
from src.api.probes import GatewayReadiness
from src.api.schemas import (
    ApprovalPreview,
    ApprovalQueue,
    ApprovalRequest,
    ApprovalResult,
    ApprovalSummary,
    ChecklistItemView,
    CostView,
    Notice,
    PlanDiffRow,
    SimulationField,
)
from src.api.security import OperatorAuth, OperatorPrincipal
from src.domain.models.events import AuditEvent, EventType
from src.domain.models.trading import TradingMode
from src.execution.contracts import (
    DlmmCommand,
    ExecutionPhase,
    OperatorApproval,
    SimulationResult,
)
from src.execution.service import (
    ENTRY_OPERATIONS,
    EXIT_OPERATIONS,
    ExecutionBlocked,
    ExecutionRecord,
    ExecutionService,
    entry_impact_rejection,
    sign_operator_approval,
)
from src.modes.models import KillSwitchPolicy
from src.observability.logging import error_fields, get_logger
from src.persistence.contracts import DecisionRecord, PlanRecord, UniverseSummary
from src.worker.execution import entry_idempotency_key, position_action_key

DEFAULT_SIMULATION_TTL = timedelta(minutes=2)
OPERATOR_ID = "operator"
IRREVERSIBILITY_NOTE = (
    "После подтверждения команда уходит в сеть Solana: подтверждённую on-chain операцию "
    "нельзя отменить. Отменить можно только неподтверждённый план."
)
_RECENT_PHASES = (
    ExecutionPhase.APPROVED,
    ExecutionPhase.SUBMITTING,
    ExecutionPhase.SUBMITTED,
    ExecutionPhase.UNKNOWN,
    ExecutionPhase.FAILED,
    ExecutionPhase.EXPIRED,
)
# Simulation fields shown elsewhere in the preview (digests, identity) or as errors.
_HIDDEN_SIMULATION_FIELDS = frozenset(
    {"schema_version", "command_id", "command_digest", "simulation_digest", "errors"}
)


@dataclass(frozen=True, slots=True)
class ApprovalWiring:
    """The execution service of an APPROVAL deployment and its approval-signing secret."""

    service: ExecutionService
    approval_secret: SecretStr
    simulation_ttl: timedelta = DEFAULT_SIMULATION_TTL
    readiness: GatewayReadiness | None = None


@dataclass(frozen=True, slots=True)
class _Links:
    plan: PlanRecord | None
    decision: DecisionRecord | None


class ApprovalDesk:
    def __init__(
        self,
        context: OperatorContext,
        auth: OperatorAuth,
        wiring: ApprovalWiring | None,
        unavailable: Sequence[str] = (),
    ) -> None:
        self._ctx = context
        self._auth = auth
        self._wiring = wiring
        self._unavailable = tuple(unavailable) if wiring is None else ()
        self._locks: dict[UUID, asyncio.Lock] = {}
        self._log = get_logger("api.approvals")

    @property
    def simulation_ttl(self) -> timedelta:
        return DEFAULT_SIMULATION_TTL if self._wiring is None else self._wiring.simulation_ttl

    def unavailable(self) -> tuple[str, ...]:
        reasons = list(self._unavailable)
        if not self._auth.configured:
            reasons.insert(0, "OPERATOR_AUTH_NOT_CONFIGURED")
        return tuple(dict.fromkeys(reasons))

    async def global_blockers(self, *, universe: UniverseSummary | None = None) -> list[str]:
        state = await self._ctx.mode_state()
        blockers = list(self.unavailable())
        if state.mode is not TradingMode.APPROVAL:
            blockers.append("MODE_NOT_APPROVAL")
        if state.kill_switch_active:
            blockers.append("KILL_SWITCH_ACTIVE")
        if state.daily_stop_active_on(self._ctx.today()):
            blockers.append("DAILY_STOP_ACTIVE")
        if not (universe or await self._ctx.universe()).anchored:
            blockers.append("ENTRY_ANCHOR_UNAVAILABLE")
        return blockers

    async def queue(self) -> ApprovalQueue:
        store = self._ctx.repositories.execution
        pending = await self._ctx.pending_approvals()
        recent = await store.in_phases(_RECENT_PHASES)
        newest = sorted(recent, key=lambda item: item.simulation.simulated_at, reverse=True)
        return ApprovalQueue(
            pending=[self._summary(item) for item in sorted(pending, key=_expiry)],
            recent=[self._summary(item) for item in newest[:20]],
            blockers=presenters.readiness_notices(await self.global_blockers()),
        )

    async def preview(self, command_id: UUID) -> ApprovalPreview:
        record = await self._record(command_id)
        now = self._ctx.clock.now()
        links = await self._links(record)
        command, simulation = record.command, record.simulation
        simulation_expires = simulation.simulated_at + self.simulation_ttl
        approval_expires = self._approval_expiry(record, now)
        blockers = await self._blockers(record, now)
        decision = links.decision
        flags = [] if decision is None else [flag.value for flag in decision.decision.flags]
        sized = None if links.plan is None else links.plan.sized_plan
        return ApprovalPreview(
            command_id=command.command_id,
            phase=record.phase.value,
            phase_label=labels.label(labels.EXECUTION_PHASE, record.phase.value),
            irreversibility_note=IRREVERSIBILITY_NOTE,
            operation=command.operation.value,
            strategy=None if command.strategy is None else command.strategy.value,
            pool_address=command.pool_address,
            wallet_address=command.wallet_address,
            # An OPEN in APPROVAL/LIVE names no position: the executor creates it and the
            # simulation reports it (D06); older lifecycle commands carry it themselves.
            position_address=command.position_address
            or getattr(simulation, "position_address", None),
            command_digest=simulation.command_digest,
            simulation_digest=simulation.simulation_digest,
            command_expires_at=command.expires_at,
            simulation_expires_at=simulation_expires,
            approval_expires_at=approval_expires,
            seconds_left=max(int((approval_expires - now).total_seconds()), 0),
            simulation_success=simulation.success,
            simulation=[
                SimulationField(name=name, value=_text(value))
                for name, value in simulation.model_dump(mode="json").items()
                if name not in _HIDDEN_SIMULATION_FIELDS
            ],
            simulation_errors=[
                Notice(code=error.code, message=_refusal_text(error.code, error.message))
                for error in simulation.errors
            ],
            costs=CostView(
                network_fee_lamports=simulation.network_fee_lamports,
                priority_fee_lamports=simulation.priority_fee_lamports,
                compute_units=simulation.compute_units,
                price_impact_bps=simulation.price_impact_bps,
                max_slippage_bps=command.max_slippage_bps,
                estimated_loss_base_units=None
                if sized is None
                else sized.estimated_loss_base_units,
            ),
            plan_diff=_plan_diff(record, links.plan),
            checklist=[ChecklistItemView(key=key, label=text) for key, text in labels.CHECKLIST],
            notices=presenters.flag_notices(flags),
            gmgn=[] if decision is None else presenters.gmgn_metrics(decision.decision.evidence),
            decision_id=None if decision is None else decision.decision_id,
            plan_id=None if links.plan is None else links.plan.plan_id,
            blockers=presenters.readiness_notices(blockers),
            can_approve=not blockers,
        )

    async def approve(
        self, command_id: UUID, request: ApprovalRequest, principal: OperatorPrincipal
    ) -> ApprovalResult:
        self._auth.require_reauthentication(request.operator_token, request.confirm_irreversible)
        marks = request.checklist.model_dump()
        missing = [key for key, _ in labels.CHECKLIST if not marks.get(key)]
        if missing:
            raise ApiError(
                422,
                "CHECKLIST_INCOMPLETE",
                "Отметьте все пункты ручной проверки перед подтверждением",
                {"missing": missing},
            )
        # Two concurrent submits for one command are decided one after the other, so the
        # second one sees the first approval instead of racing it.
        lock = self._locks.setdefault(command_id, asyncio.Lock())
        async with lock:
            return await self._approve(command_id, request, principal)

    async def _approve(
        self, command_id: UUID, request: ApprovalRequest, principal: OperatorPrincipal
    ) -> ApprovalResult:
        record = await self._record(command_id)
        if record.approval is not None and record.approval.approval_id == request.approval_id:
            return self._result(record)
        if record.phase is not ExecutionPhase.AWAITING_APPROVAL:
            raise ApiError(
                409,
                "APPROVAL_NOT_PENDING",
                "Команда уже не ждёт подтверждения: показано её текущее состояние",
                {"phase": record.phase.value},
            )
        now = self._ctx.clock.now()
        blockers = await self._blockers(record, now)
        if blockers or self._wiring is None:
            raise ApiError(
                409,
                "APPROVAL_BLOCKED",
                "Подтверждение сейчас невозможно",
                {"blockers": blockers or list(self.unavailable())},
            )
        simulation = record.simulation
        if (
            request.command_digest != simulation.command_digest
            or request.simulation_digest != simulation.simulation_digest
        ):
            raise ApiError(
                409,
                "APPROVAL_PREVIEW_STALE",
                "Котировка или команда изменились после показа preview: обновите страницу",
            )
        expires_at = self._approval_expiry(record, now)
        approval = OperatorApproval(
            approval_id=request.approval_id,
            approved_at=now,
            expires_at=expires_at,
            operator_id=OPERATOR_ID,
            command_digest=simulation.command_digest,
            simulation_digest=simulation.simulation_digest,
            proof=sign_operator_approval(
                self._wiring.approval_secret.get_secret_value().encode("utf-8"),
                approval_id=request.approval_id,
                approved_at=now,
                expires_at=expires_at,
                operator_id=OPERATOR_ID,
                command_digest=simulation.command_digest,
                simulation_digest=simulation.simulation_digest,
            ),
            comment=request.comment,
        )
        audit = self._audit(
            record,
            now,
            {
                "stage": "approved",
                "approval_id": str(request.approval_id),
                "command_digest": simulation.command_digest,
                "simulation_digest": simulation.simulation_digest,
                # What the operator saw; for an exit it is informational and may be None.
                "price_impact_bps": simulation.price_impact_bps,
                "approved_at": now.isoformat(),
                "expires_at": expires_at.isoformat(),
                "operator": principal.confirmation_id,
                "checklist": {key: True for key, _ in labels.CHECKLIST},
                "checklist_confirmed_at": now.isoformat(),
                "comment": request.comment,
            },
        )
        try:
            await self._ctx.repositories.audit.append(audit)
        except Exception as error:
            self._log.critical("approval_audit_failed", **error_fields(error))
            raise ApiError(
                503,
                "AUDIT_UNAVAILABLE",
                "Аудит недоступен: подтверждение не выдано, команда не будет отправлена",
            ) from error
        try:
            state = await self._wiring.service.approve(command_id, approval)
        except ExecutionBlocked as error:
            await self._audit_refusal(record, audit, str(error))
            raise ApiError(
                409, "APPROVAL_REJECTED", "Подтверждение отклонено", {"reason": str(error)}
            ) from error
        self._log.info(
            "command_approved", command_id=str(command_id), approval_id=str(request.approval_id)
        )
        return ApprovalResult(
            command_id=state.command_id,
            phase=state.phase.value,
            phase_label=labels.label(labels.EXECUTION_PHASE, state.phase.value),
            approval_id=state.approval_id,
            approved_at=now,
            expires_at=expires_at,
        )

    # --- helpers ----------------------------------------------------------------------

    async def _record(self, command_id: UUID) -> ExecutionRecord:
        record = await self._ctx.repositories.execution.get(command_id)
        if record is None:
            raise ApiError(404, "COMMAND_NOT_FOUND", "Команда не найдена")
        return record

    async def _blockers(self, record: ExecutionRecord, now: datetime) -> list[str]:
        state = await self._ctx.mode_state()
        command, simulation = record.command, record.simulation
        blockers = list(self.unavailable())
        daily_stop = state.daily_stop_active_on(self._ctx.today())
        leaving = _leaves_approval_position(command)
        # Spec 7.3: a stop halts entries, not the way out of an APPROVAL position. Under an
        # active kill switch the policy chosen at its activation decides (mode state), the
        # same one the worker and ``ExecutionService`` apply; both stops demote to SHADOW.
        emergency_exit = leaving and (
            state.active_kill_switch_policy is KillSwitchPolicy.ORDERLY_EXIT
            if state.kill_switch_active
            else daily_stop
        )
        if record.phase is not ExecutionPhase.AWAITING_APPROVAL:
            blockers.append("NOT_AWAITING_APPROVAL")
        if command.mode is not TradingMode.APPROVAL:
            blockers.append("COMMAND_NOT_APPROVAL_MODE")
        if state.mode is not TradingMode.APPROVAL and not (
            emergency_exit and state.mode is TradingMode.SHADOW
        ):
            blockers.append("MODE_NOT_APPROVAL")
        if state.kill_switch_active and not emergency_exit:
            blockers.append("KILL_SWITCH_ACTIVE")
        if daily_stop and not leaving:
            blockers.append("DAILY_STOP_ACTIVE")
        if command.expires_at <= now:
            blockers.append("COMMAND_EXPIRED")
        if simulation.simulated_at + self.simulation_ttl <= now:
            blockers.append("SIMULATION_STALE")
        if not simulation.success or simulation.errors:
            blockers.append("SIMULATION_FAILED")
        impact = entry_impact_rejection(command, simulation)
        if impact is not None:
            blockers.append(impact)
        return blockers

    def _approval_expiry(self, record: ExecutionRecord, now: datetime) -> datetime:
        return min(
            now + self._ctx.settings.approval_ttl,
            record.command.expires_at,
            record.simulation.simulated_at + self.simulation_ttl,
        )

    async def _links(self, record: ExecutionRecord) -> _Links:
        repositories = self._ctx.repositories
        key = record.command.idempotency_key
        plan_id = _entry_plan_id(key)
        plan: PlanRecord | None = None
        if plan_id is not None:
            plan = await repositories.decisions.plan(plan_id)
        else:
            position_id = _position_of_action(key)
            if position_id is not None:
                context = await repositories.positions.get(position_id)
                if context is not None:
                    plan = await repositories.decisions.plan(context.plan_id)
        decision = None if plan is None else await repositories.decisions.decision(plan.decision_id)
        return _Links(plan, decision)

    def _summary(self, record: ExecutionRecord) -> ApprovalSummary:
        return ApprovalSummary(
            command_id=record.command.command_id,
            operation=record.command.operation.value,
            mode=record.command.mode,
            pool_address=record.command.pool_address,
            phase=record.phase.value,
            phase_label=labels.label(labels.EXECUTION_PHASE, record.phase.value),
            command_expires_at=record.command.expires_at,
            simulated_at=record.simulation.simulated_at,
        )

    def _result(self, record: ExecutionRecord) -> ApprovalResult:
        approval = record.approval
        return ApprovalResult(
            command_id=record.command.command_id,
            phase=record.phase.value,
            phase_label=labels.label(labels.EXECUTION_PHASE, record.phase.value),
            approval_id=None if approval is None else approval.approval_id,
            approved_at=None if approval is None else approval.approved_at,
            expires_at=None if approval is None else approval.expires_at,
        )

    def _audit(
        self, record: ExecutionRecord, now: datetime, payload: dict[str, object]
    ) -> AuditEvent:
        return AuditEvent(
            event_id=uuid4(),
            occurred_at=now,
            aggregate_id=record.command.command_id,
            correlation_id=record.command.correlation_id,
            causation_id=None,
            schema_version=1,
            event_type=EventType.APPROVAL_REQUESTED,
            payload=payload,
        )

    async def _audit_refusal(
        self, record: ExecutionRecord, granted: AuditEvent, reason: str
    ) -> None:
        refusal = self._audit(
            record,
            self._ctx.clock.now(),
            {
                "stage": "approval_rejected",
                "approval_id": granted.payload["approval_id"],
                "reason": reason,
            },
        )
        refusal = refusal.model_copy(update={"causation_id": granted.event_id})
        try:
            await self._ctx.repositories.audit.append(refusal)
        except Exception as error:
            self._log.error("approval_refusal_audit_failed", **error_fields(error))


def _leaves_approval_position(command: DlmmCommand) -> bool:
    """A REMOVE/CLOSE queued for a position opened in APPROVAL (the command's own mode,
    not the deployment's mode after a demotion)."""

    return command.operation in EXIT_OPERATIONS and command.mode is TradingMode.APPROVAL


def _entry_plan_id(key: str) -> UUID | None:
    prefix, _, rest = key.partition(":")
    if prefix != "entry":
        return None
    try:
        plan_id = UUID(rest)
    except ValueError:
        return None
    return plan_id if entry_idempotency_key(plan_id) == key else None


def _position_of_action(key: str) -> str | None:
    """The position of ``position-action:{position}:{attempt}[:{preview}]``."""

    prefix, _, rest = key.partition(":")
    position_id, *numbers = rest.split(":")
    if (
        prefix != "position-action"
        or not position_id
        or not 1 <= len(numbers) <= 2
        or not all(number.isdigit() and int(number) >= 1 for number in numbers)
    ):
        return None
    attempt, preview = int(numbers[0]), int(numbers[1]) if len(numbers) == 2 else 1
    return position_id if position_action_key(position_id, attempt, preview) == key else None


def _plan_diff(record: ExecutionRecord, plan: PlanRecord | None) -> list[PlanDiffRow]:
    command, simulation = record.command, record.simulation
    strategy = None if plan is None else plan.sized_plan.plan
    command_range = (command.min_bin_id, command.max_bin_id)
    simulated_range = (simulation.min_bin_id, simulation.max_bin_id)
    rows = [
        PlanDiffRow(
            field="strategy",
            planned=None if strategy is None else strategy.strategy.value,
            command=None if command.strategy is None else command.strategy.value,
            simulated=None,
            matches=None
            if strategy is None or command.strategy is None
            else strategy.strategy.value == command.strategy.value,
        ),
        PlanDiffRow(
            field="bin_range",
            planned=None
            if strategy is None
            else f"смещения {strategy.lower_bin_offset}..{strategy.upper_bin_offset}",
            command=f"{command.min_bin_id}..{command.max_bin_id}",
            simulated=f"{simulation.min_bin_id}..{simulation.max_bin_id}",
            matches=command_range == simulated_range
            and (
                strategy is None
                or command.max_bin_id - command.min_bin_id
                == strategy.upper_bin_offset - strategy.lower_bin_offset
            ),
        ),
        *_anchor_rows(record),
        PlanDiffRow(
            field="size",
            planned=None if plan is None else f"{plan.sized_plan.size_base_units} base units",
            command=f"x={command.amount_x}, y={command.amount_y}"
            if command.amount_x is not None
            else f"remove {command.remove_bps} bps",
            simulated=None,
            matches=None,
        ),
        _impact_row(command, simulation),
        PlanDiffRow(
            field="expires_at",
            planned=None if strategy is None else strategy.expires_at.isoformat(),
            command=command.expires_at.isoformat(),
            simulated=None,
            matches=None if strategy is None else strategy.expires_at == command.expires_at,
        ),
        PlanDiffRow(
            field="ruleset_version",
            planned=None if strategy is None else strategy.ruleset_version,
            command=command.ruleset_version,
            simulated=None,
            matches=None
            if strategy is None
            else strategy.ruleset_version == command.ruleset_version,
        ),
    ]
    return rows


def _impact_row(command: DlmmCommand, simulation: SimulationResult) -> PlanDiffRow:
    """An entry's quoted impact against its slippage limit; an exit's quote is shown only."""

    impact = simulation.price_impact_bps
    simulated = None if impact is None else str(impact)
    if command.operation not in ENTRY_OPERATIONS:
        return PlanDiffRow(
            field="price_impact_bps",
            planned="справочно: выход без обмена токенов",
            command=None,
            simulated=simulated,
            matches=None,
        )
    return PlanDiffRow(
        field="price_impact_bps",
        planned=f"≤ {command.max_slippage_bps}",
        command=str(command.max_slippage_bps),
        simulated=simulated,
        matches=entry_impact_rejection(command, simulation) is None,
    )


def _anchor_rows(record: ExecutionRecord) -> list[PlanDiffRow]:
    """Active bin the command was built on vs. what the simulation saw on chain."""

    built = getattr(record.command, "active_bin_id", None)
    seen = getattr(record.simulation, "active_bin_id", None)
    if built is None and seen is None:
        return []
    tolerance = getattr(record.command, "active_bin_tolerance", None) or 0
    return [
        PlanDiffRow(
            field="active_bin_id",
            planned=None,
            command=None if built is None else f"{built} (допуск ±{tolerance})",
            simulated=None if seen is None else str(seen),
            matches=built is not None and seen is not None and abs(built - seen) <= tolerance,
        )
    ]


def _expiry(record: ExecutionRecord) -> datetime:
    return record.command.expires_at


def _refusal_text(code: str, message: str) -> str:
    """The executor's refusal in Russian; its own (English) text only for an unknown code."""

    text = labels.executor_label(code)
    return message if text == code else text


def _text(value: object) -> str:
    return "—" if value is None else str(value)
