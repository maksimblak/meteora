"""``RecoveryService.reconcile``: settle what an interrupted worker left behind.

Two strictly ordered phases:

1. **Network first.** Every command whose outcome is not final (SUBMITTING,
   SUBMITTED, UNKNOWN) is reconciled through the T05 execution service: by command id
   when the gateway offers the lookup (it works without any signature, e.g. after a
   submit timeout), otherwise per signature. Only a command still unknown afterwards is
   escalated to the operator, one ``EXECUTION_RECOVERY_REQUIRED`` alert per command;
   once a later pass finds its outcome, the system closes that alert. Nothing is ever
   re-submitted.
2. **Then decide.** Each EXECUTING entry plan is settled from the reconciled facts:
   a shadow receipt or a CONFIRMED command confirms it, a FAILED/EXPIRED command
   fails it, a still-unknown outcome keeps it EXECUTING (capacity stays reserved)
   and a plan that never produced a command is failed - it never left the process.

An EXECUTING plan may belong to a *live* executor (between ``claim_entry`` and its
fill), so it is only settled when no executor can be running: either recovery holds
the executor job lease itself (entries only execute inside that job), or - without
a configured lease - at startup of the only worker, or once the claim is older than
``stale_after`` (the executor job's lease TTL). Otherwise it is left untouched.
"""

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from src.accounting.repository import AccountingRepository
from src.core.clock import Clock
from src.execution.contracts import ExecutionPhase
from src.execution.service import ExecutionRecord, ExecutionService
from src.modes.alerts import SystemAlertResolver, alert_subject
from src.modes.models import PendingPlan, PendingPlanStatus
from src.modes.repository import ModeRepository
from src.observability.alerts import CriticalAlertReporter
from src.observability.logging import error_fields, get_logger
from src.observability.metrics import WorkerMetrics
from src.persistence.contracts import JobLeases
from src.worker.execution import entry_command_id, entry_idempotency_key

UNRESOLVED_PHASES = (ExecutionPhase.SUBMITTING, ExecutionPhase.SUBMITTED, ExecutionPhase.UNKNOWN)
RECOVERY_ALERT = "EXECUTION_RECOVERY_REQUIRED"
_NOT_BROADCAST = frozenset(
    {ExecutionPhase.AWAITING_APPROVAL, ExecutionPhase.APPROVED, ExecutionPhase.SIMULATED}
)


class RecoveryAction(StrEnum):
    CONFIRMED_ENTRY = "CONFIRMED_ENTRY"
    FAILED_ENTRY = "FAILED_ENTRY"
    KEPT_EXECUTING = "KEPT_EXECUTING"
    OPERATOR_REQUIRED = "OPERATOR_REQUIRED"


@dataclass(frozen=True, slots=True)
class CommandCheck:
    command_id: UUID
    before: ExecutionPhase
    after: ExecutionPhase
    checked_on_chain: bool


@dataclass(frozen=True, slots=True)
class EntryRecovery:
    plan_id: UUID
    action: RecoveryAction
    reason: str


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    commands: tuple[CommandCheck, ...]
    entries: tuple[EntryRecovery, ...]
    operator_required: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class ExecutorLease:
    """The job lease under which entries execute (``candidate_pipeline``).

    ``owner`` must differ from every scheduler owner, so recovery never takes the
    lease over from a live job of its own process.
    """

    leases: JobLeases
    job_name: str
    owner: str
    ttl: timedelta


class EntryPlanSource(Protocol):
    def entry_plans(self) -> tuple[PendingPlan, ...]: ...


class RecoveryExecutionStore(Protocol):
    async def get_by_idempotency_key(self, key: str) -> ExecutionRecord | None: ...

    async def in_phases(self, phases: Sequence[ExecutionPhase]) -> tuple[ExecutionRecord, ...]: ...


class RecoveryService:
    def __init__(
        self,
        *,
        modes: ModeRepository,
        plans: EntryPlanSource,
        accounting: AccountingRepository,
        alerts: CriticalAlertReporter,
        clock: Clock,
        metrics: WorkerMetrics,
        executions: RecoveryExecutionStore | None = None,
        execution_service: ExecutionService | None = None,
        executor_lease: ExecutorLease | None = None,
        stale_after: timedelta = timedelta(minutes=2),
        cleared: SystemAlertResolver | None = None,
    ) -> None:
        """``cleared`` closes recovery alerts of commands whose outcome became known;
        without it recovery only raises them."""

        if stale_after <= timedelta(0):
            raise ValueError("stale_after must be positive")
        self._modes = modes
        self._plans = plans
        self._accounting = accounting
        self._alerts = alerts
        self._clock = clock
        self._metrics = metrics
        self._executions = executions
        self._service = execution_service
        self._executor_lease = executor_lease
        self._stale_after = stale_after
        self._cleared = cleared
        self._log = get_logger("worker.recovery")

    async def reconcile(self, *, startup: bool = False) -> RecoveryReport:
        """``startup`` only matters without an executor lease: the caller asserts
        that no executor runs anywhere (single worker, before its scheduler)."""

        checks, operator = await self._reconcile_network()
        entries: list[EntryRecovery] = []
        async with self._executors_excluded(startup) as excluded:
            now = self._clock.now()
            for plan in self._plans.entry_plans():
                if plan.status is not PendingPlanStatus.EXECUTING:
                    continue
                if not excluded and now - plan.admitted_at < self._stale_after:
                    entries.append(
                        EntryRecovery(
                            plan.plan_id, RecoveryAction.KEPT_EXECUTING, "EXECUTOR_MAY_BE_ACTIVE"
                        )
                    )
                    continue
                entries.append(await self._settle(plan))
        for item in entries:
            command_id = entry_command_id(item.plan_id)
            if item.action is RecoveryAction.OPERATOR_REQUIRED and command_id not in operator:
                operator.append(command_id)
        for command_id in operator:
            self._alerts.raise_alert(
                RECOVERY_ALERT,
                f"command {command_id} has an unknown on-chain outcome; operator "
                "reconciliation is required before it is retried "
                + alert_subject("command", command_id),
                dedupe_key=f"recovery:{command_id}",
            )
        if self._cleared is not None and self._executions is not None:
            self._cleared.resolve_except(
                RECOVERY_ALERT,
                "command",
                {str(command_id) for command_id in operator},
                "the command outcome was reconciled with the network",
            )
        report = RecoveryReport(tuple(checks), tuple(entries), tuple(operator))
        self._log.info(
            "recovery_finished",
            commands=len(report.commands),
            entries=len(report.entries),
            operator_required=len(report.operator_required),
        )
        return report

    @asynccontextmanager
    async def _executors_excluded(self, startup: bool) -> AsyncIterator[bool]:
        """Yield True while no entry executor can be running anywhere."""

        holder = self._executor_lease
        if holder is None:
            yield startup
            return
        lease = await holder.leases.acquire(holder.job_name, holder.owner, holder.ttl)
        try:
            yield lease is not None
        finally:
            if lease is not None:
                await holder.leases.release(lease)

    async def _reconcile_network(self) -> tuple[list[CommandCheck], list[UUID]]:
        checks: list[CommandCheck] = []
        operator: list[UUID] = []
        if self._executions is None:
            return checks, operator
        for record in await self._executions.in_phases(UNRESOLVED_PHASES):
            command_id = record.command.command_id
            if self._service is None:
                checks.append(CommandCheck(command_id, record.phase, record.phase, False))
                operator.append(command_id)
                continue
            try:
                # By command id first when the gateway can look it up, else by signature;
                # without either it refuses and the command goes to the operator.
                state = await self._service.reconcile(command_id)
            except Exception as error:
                self._log.warning(
                    "reconcile_failed", command_id=str(command_id), **error_fields(error)
                )
                checks.append(CommandCheck(command_id, record.phase, record.phase, False))
                operator.append(command_id)
                continue
            checks.append(CommandCheck(command_id, record.phase, state.phase, True))
            if state.phase in UNRESOLVED_PHASES:
                operator.append(command_id)
        return checks, operator

    async def _settle(self, plan: PendingPlan) -> EntryRecovery:
        key = entry_idempotency_key(plan.plan_id)
        shadow = self._accounting.receipt_for(key)
        if shadow is not None:
            self._modes.confirm_entry(plan.plan_id, shadow.position.position_id)
            return EntryRecovery(plan.plan_id, RecoveryAction.CONFIRMED_ENTRY, "SHADOW_FILL_FOUND")
        record = None
        if self._executions is not None:
            record = await self._executions.get_by_idempotency_key(key)
        if record is None:
            self._modes.fail_entry(plan.plan_id)
            return EntryRecovery(plan.plan_id, RecoveryAction.FAILED_ENTRY, "NEVER_SUBMITTED")
        phase = record.phase
        # A V2 entry's position exists only as the executor named it in the receipt.
        position = record.position_address
        if phase is ExecutionPhase.CONFIRMED and position is not None:
            self._modes.confirm_entry(plan.plan_id, position)
            return EntryRecovery(plan.plan_id, RecoveryAction.CONFIRMED_ENTRY, "CHAIN_CONFIRMED")
        if phase in {ExecutionPhase.FAILED, ExecutionPhase.EXPIRED}:
            self._modes.fail_entry(plan.plan_id)
            return EntryRecovery(
                plan.plan_id, RecoveryAction.FAILED_ENTRY, f"COMMAND_{phase.value}"
            )
        if phase in _NOT_BROADCAST:
            if record.command.expires_at <= self._clock.now():
                self._modes.fail_entry(plan.plan_id)
                return EntryRecovery(plan.plan_id, RecoveryAction.FAILED_ENTRY, "EXPIRED_UNSENT")
            return EntryRecovery(plan.plan_id, RecoveryAction.KEPT_EXECUTING, phase.value)
        return EntryRecovery(plan.plan_id, RecoveryAction.OPERATOR_REQUIRED, phase.value)
