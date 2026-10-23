"""The money path of an admitted entry plan.

Order of guarantees (each step fails closed and never re-orders):

1. ``claim_entry`` - PENDING -> EXECUTING under the kill-switch / daily-stop guards;
2. *guard* - the durable mode state is re-read; kill switch, daily stop or a mode
   change since the claim blocks the command (closes the claim -> submit seam);
3. *audit* - position context and a ``CommandSubmitted`` record are made durable;
   an unaudited command is never sent and the failure raises a critical alert;
4. *submit* - the submitter calls the guard again immediately before the one
   irreversible step (shadow fill or gateway submit, after any simulation);
5. *settle* - CONFIRMED -> ``confirm_entry``; FAILED/BLOCKED -> ``fail_entry``;
   an unknown outcome stays EXECUTING for restart recovery and is never resent.
"""

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_FLOOR, Decimal
from enum import StrEnum
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from src.accounting.models import CostModel
from src.accounting.repository import AccountingRepository
from src.accounting.service import ShadowBroker
from src.core.clock import Clock
from src.domain.models.events import AuditEvent, EventType
from src.domain.models.trading import TradingMode
from src.domain.risk.models import SizedPlan
from src.execution.contracts import (
    DlmmCommandV1,
    DlmmOperation,
    GatewayState,
    LiquidityStrategy,
    canonical_command_digest,
)
from src.execution.service import ExecutionBlocked, ExecutionService
from src.market.models import PoolSnapshot
from src.modes.models import PendingPlan, PendingPlanStatus
from src.modes.repository import ModeRepository
from src.observability.alerts import CriticalAlertReporter
from src.observability.logging import error_fields, get_logger
from src.observability.metrics import WorkerMetrics
from src.persistence.contracts import AuditLog, PositionContext, PositionContextStore
from src.worker.policy import PipelinePolicy

_BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_BPS = Decimal(10_000)


def shadow_address(seed: str) -> str:
    """Deterministic, valid 32-byte base58 key used for shadow wallets and positions."""

    data = hashlib.sha256(seed.encode("utf-8")).digest()
    number = int.from_bytes(data, "big")
    encoded = ""
    while number:
        number, remainder = divmod(number, 58)
        encoded = _BASE58[remainder] + encoded
    padding = len(data) - len(data.lstrip(b"\x00"))
    return "1" * padding + encoded


def entry_command_id(plan_id: UUID) -> UUID:
    return uuid5(NAMESPACE_URL, f"meteora-volume-bot:entry-command:{plan_id}")


def entry_idempotency_key(plan_id: UUID) -> str:
    return f"entry:{plan_id}"


def entry_position_address(plan_id: UUID) -> str:
    return shadow_address(f"position:{plan_id}")


def build_entry_command(
    *,
    plan_id: UUID,
    mode: TradingMode,
    market: PoolSnapshot,
    size_base_units: int,
    strategy_kind: str,
    lower_bin_offset: int,
    upper_bin_offset: int,
    expires_at: datetime,
    correlation_id: str,
    ruleset_version: str,
    wallet_address: str,
    position_address: str,
    max_slippage_bps: int,
) -> DlmmCommandV1:
    """Split the sized capital evenly between Y and X at the observed price."""

    amount_y = size_base_units // 2
    amount_x = int(
        (Decimal(size_base_units - amount_y) / market.price).to_integral_value(rounding=ROUND_FLOOR)
    )
    try:
        correlation = UUID(correlation_id)
    except ValueError:
        correlation = uuid5(NAMESPACE_URL, f"correlation:{correlation_id}")
    return DlmmCommandV1(
        command_id=entry_command_id(plan_id),
        idempotency_key=entry_idempotency_key(plan_id),
        correlation_id=correlation,
        ruleset_version=ruleset_version,
        mode=mode,
        operation=DlmmOperation.OPEN,
        pool_address=market.pool.address.root,
        wallet_address=wallet_address,
        position_address=position_address,
        expires_at=expires_at,
        max_slippage_bps=max_slippage_bps,
        strategy=LiquidityStrategy(strategy_kind),
        min_bin_id=lower_bin_offset,
        max_bin_id=upper_bin_offset,
        amount_x=str(amount_x),
        amount_y=str(amount_y),
    )


# --- submitters --------------------------------------------------------------------------


class SubmitStatus(StrEnum):
    CONFIRMED = "CONFIRMED"
    PENDING = "PENDING"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True, slots=True)
class SubmitResult:
    status: SubmitStatus
    position_id: str | None = None
    detail: str | None = None


type SubmitGuard = Callable[[], None]


class CommandSubmitter(Protocol):
    async def submit(
        self, command: DlmmCommandV1, market: PoolSnapshot, guard: SubmitGuard
    ) -> SubmitResult: ...


class ShadowSubmitter:
    """SHADOW: a modeled fill through the T04 shadow broker, no network at all."""

    def __init__(self, accounting: AccountingRepository, clock: Clock, costs: CostModel) -> None:
        self._broker = ShadowBroker(accounting, clock)
        self._costs = costs

    async def submit(
        self, command: DlmmCommandV1, market: PoolSnapshot, guard: SubmitGuard
    ) -> SubmitResult:
        try:
            guard()
        except Exception as error:
            return SubmitResult(SubmitStatus.BLOCKED, detail=str(error))
        try:
            receipt = self._broker.apply(command, market, self._costs)
        except ValueError as error:
            return SubmitResult(SubmitStatus.FAILED, detail=str(error))
        return SubmitResult(SubmitStatus.CONFIRMED, position_id=receipt.position.position_id)


class RegisteredPlanResolver:
    """``PlanResolver`` for ``ExecutionService``: the command is keyed by its own id."""

    def __init__(self) -> None:
        self._commands: dict[UUID, DlmmCommandV1] = {}

    def register(self, command: DlmmCommandV1) -> UUID:
        self._commands[command.command_id] = command
        return command.command_id

    async def resolve(self, plan_id: UUID) -> DlmmCommandV1:
        try:
            return self._commands[plan_id]
        except KeyError as error:
            raise ExecutionBlocked("plan not found") from error


class GatewaySubmitter:
    """APPROVAL / LIVE through the T05 execution service and the internal gateway.

    APPROVAL stops after the simulated preview (an operator approval is the manual
    step of that stage); LIVE submits only after the guard re-checks the durable
    kill switch *after* simulation, immediately before the irreversible call.
    """

    def __init__(self, service: ExecutionService, resolver: RegisteredPlanResolver) -> None:
        self._service = service
        self._resolver = resolver

    async def submit(
        self, command: DlmmCommandV1, market: PoolSnapshot, guard: SubmitGuard
    ) -> SubmitResult:
        del market
        key = self._resolver.register(command)
        try:
            guard()
        except Exception as error:
            return SubmitResult(SubmitStatus.BLOCKED, detail=str(error))
        try:
            await self._service.prepare(key)
        except ExecutionBlocked as error:
            return SubmitResult(SubmitStatus.FAILED, detail=str(error))
        if command.mode is TradingMode.APPROVAL:
            return SubmitResult(SubmitStatus.PENDING, detail="AWAITING_APPROVAL")
        try:
            guard()
        except Exception as error:
            return SubmitResult(SubmitStatus.BLOCKED, detail=str(error))
        try:
            receipt = await self._service.execute(command.command_id)
        except ExecutionBlocked as error:
            return SubmitResult(SubmitStatus.FAILED, detail=str(error))
        except Exception:
            return SubmitResult(SubmitStatus.PENDING, detail="SUBMIT_OUTCOME_UNKNOWN")
        if receipt.state is GatewayState.CONFIRMED:
            return SubmitResult(SubmitStatus.CONFIRMED, position_id=command.position_address)
        if receipt.state is GatewayState.FAILED:
            return SubmitResult(SubmitStatus.FAILED, detail="CHAIN_FAILED")
        return SubmitResult(SubmitStatus.PENDING, detail=receipt.state.value)


# --- entry executor ----------------------------------------------------------------------


class EntryStatus(StrEnum):
    OPENED = "OPENED"
    PENDING = "PENDING"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    NOT_CONFIGURED = "NOT_CONFIGURED"


@dataclass(frozen=True, slots=True)
class EntryResult:
    status: EntryStatus
    plan_id: UUID
    reason: str | None = None
    position_id: str | None = None
    command_id: UUID | None = None


class EntryStore(ModeRepository, Protocol):
    """T04 entry contract plus the plan read the pre-fill guard needs."""

    def entry_plan(self, plan_id: UUID) -> PendingPlan | None: ...


class EntryBlocked(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class EntryExecutor:
    def __init__(
        self,
        *,
        modes: EntryStore,
        audit: AuditLog,
        submitters: Mapping[TradingMode, CommandSubmitter],
        contexts: PositionContextStore,
        alerts: CriticalAlertReporter,
        clock: Clock,
        metrics: WorkerMetrics,
        policy: PipelinePolicy,
        wallet_address: str,
    ) -> None:
        self._modes = modes
        self._audit = audit
        self._submitters = dict(submitters)
        self._contexts = contexts
        self._alerts = alerts
        self._clock = clock
        self._metrics = metrics
        self._policy = policy
        self._wallet = wallet_address
        self._log = get_logger("worker.entries")

    async def execute(
        self,
        *,
        plan_id: UUID,
        decision_id: UUID,
        sized: SizedPlan,
        market: PoolSnapshot,
        correlation_id: UUID,
        causation_id: UUID | None,
    ) -> EntryResult:
        mode = self._modes.mode_state().mode
        submitter = self._submitters.get(mode)
        if submitter is None:
            self._cancel(plan_id)
            return self._finish(
                mode,
                EntryResult(
                    EntryStatus.NOT_CONFIGURED, plan_id, "EXECUTION_GATEWAY_NOT_CONFIGURED"
                ),
            )
        try:
            self._modes.claim_entry(plan_id, self._clock.now())
        except ValueError as error:
            self._cancel(plan_id)
            return self._finish(
                mode, EntryResult(EntryStatus.BLOCKED, plan_id, _claim_reason(error))
            )

        plan = sized.plan
        position_address = entry_position_address(plan_id)
        command = build_entry_command(
            plan_id=plan_id,
            mode=mode,
            market=market,
            size_base_units=sized.size_base_units,
            strategy_kind=plan.strategy.value,
            lower_bin_offset=plan.lower_bin_offset,
            upper_bin_offset=plan.upper_bin_offset,
            expires_at=plan.expires_at,
            correlation_id=str(correlation_id),
            ruleset_version=plan.ruleset_version,
            wallet_address=self._wallet,
            position_address=position_address,
            max_slippage_bps=self._policy.max_slippage_bps,
        )
        guard = self._guard(mode, plan_id)
        try:
            guard()
        except EntryBlocked as blocked:
            return await self._blocked(mode, plan_id, command, blocked.reason, correlation_id)

        context = _position_context(plan_id, decision_id, sized, market, mode, command)
        try:
            await self._contexts.put(context)
            await self._audit.append(
                audit_event(
                    EventType.COMMAND_SUBMITTED,
                    self._clock.now(),
                    command.command_id,
                    correlation_id,
                    causation_id,
                    {
                        "stage": "authorized",
                        "plan_id": str(plan_id),
                        "mode": mode.value,
                        "command_digest": canonical_command_digest(command),
                        "idempotency_key": command.idempotency_key,
                        "size_base_units": sized.size_base_units,
                    },
                )
            )
        except Exception as error:
            self._fail(plan_id)
            self._log.critical("audit_persistence_failed", **error_fields(error))
            self._alerts.raise_alert(
                "AUDIT_PERSISTENCE_FAILED",
                "audit record could not be persisted; money execution was blocked",
            )
            return self._finish(
                mode, EntryResult(EntryStatus.BLOCKED, plan_id, "AUDIT_PERSISTENCE_FAILED")
            )

        try:
            result = await submitter.submit(command, market, guard)
        except Exception as error:
            # The outcome is unknown: keep the plan EXECUTING (capacity stays reserved)
            # so restart recovery reconciles it; never retry the submit here.
            self._log.error("submit_outcome_unknown", plan_id=str(plan_id), **error_fields(error))
            result = SubmitResult(SubmitStatus.PENDING, detail="SUBMIT_OUTCOME_UNKNOWN")
        if result.status is SubmitStatus.CONFIRMED and result.position_id is not None:
            try:
                self._modes.confirm_entry(plan_id, result.position_id)
            except ValueError as error:
                # A fill exists that the entry bookkeeping refuses: never hide it.
                self._log.critical("fill_not_tracked", plan_id=str(plan_id), **error_fields(error))
                self._alerts.raise_alert(
                    "ENTRY_FILL_UNTRACKED",
                    f"position {result.position_id} was filled but plan {plan_id} "
                    "could not be confirmed; reconcile before trading continues",
                    dedupe_key=f"untracked:{plan_id}",
                )
                return self._finish(
                    mode,
                    EntryResult(
                        EntryStatus.FAILED,
                        plan_id,
                        "ENTRY_FILL_UNTRACKED",
                        position_id=result.position_id,
                        command_id=command.command_id,
                    ),
                )
            return self._finish(
                mode,
                EntryResult(
                    EntryStatus.OPENED,
                    plan_id,
                    position_id=result.position_id,
                    command_id=command.command_id,
                ),
            )
        if result.status is SubmitStatus.PENDING:
            return self._finish(
                mode,
                EntryResult(
                    EntryStatus.PENDING, plan_id, result.detail, command_id=command.command_id
                ),
            )
        if result.status is SubmitStatus.BLOCKED:
            return await self._blocked(
                mode, plan_id, command, result.detail or "BLOCKED", correlation_id
            )
        self._fail(plan_id)
        return self._finish(
            mode,
            EntryResult(EntryStatus.FAILED, plan_id, result.detail, command_id=command.command_id),
        )

    def _guard(self, mode: TradingMode, plan_id: UUID) -> SubmitGuard:
        def guard() -> None:
            try:
                state = self._modes.mode_state()
                plan = self._modes.entry_plan(plan_id)
            except Exception as error:
                raise EntryBlocked("MODE_STATE_UNAVAILABLE") from error
            if plan is None or plan.status is not PendingPlanStatus.EXECUTING:
                raise EntryBlocked("PLAN_NOT_EXECUTING")
            today = self._clock.now().astimezone(UTC).date()
            if state.kill_switch_active:
                raise EntryBlocked("KILL_SWITCH_ACTIVE")
            if state.daily_stop_active_on(today):
                raise EntryBlocked("DAILY_STOP_REACHED")
            if state.mode is not mode:
                raise EntryBlocked("MODE_CHANGED")

        return guard

    async def _blocked(
        self,
        mode: TradingMode,
        plan_id: UUID,
        command: DlmmCommandV1,
        reason: str,
        correlation_id: UUID,
    ) -> EntryResult:
        self._fail(plan_id)
        try:
            await self._audit.append(
                audit_event(
                    EventType.RISK_REJECTED,
                    self._clock.now(),
                    command.command_id,
                    correlation_id,
                    None,
                    {"stage": "execution", "plan_id": str(plan_id), "reason": reason},
                )
            )
        except Exception as error:
            self._log.error("blocked_entry_not_audited", **error_fields(error))
        return self._finish(
            mode, EntryResult(EntryStatus.BLOCKED, plan_id, reason, command_id=command.command_id)
        )

    def _cancel(self, plan_id: UUID) -> None:
        try:
            self._modes.cancel_plan(plan_id)
        except (ValueError, OSError) as error:
            self._log.warning("plan_cancel_skipped", plan_id=str(plan_id), **error_fields(error))

    def _fail(self, plan_id: UUID) -> None:
        try:
            self._modes.fail_entry(plan_id)
        except Exception as error:
            # Left EXECUTING: it keeps its capacity reserved until recovery settles it.
            self._log.error("entry_fail_not_recorded", plan_id=str(plan_id), **error_fields(error))

    def _finish(self, mode: TradingMode, result: EntryResult) -> EntryResult:
        self._metrics.execution(mode, result.status.value)
        self._log.info(
            "entry_finished",
            plan_id=str(result.plan_id),
            status=result.status.value,
            reason=result.reason,
        )
        return result


def _claim_reason(error: ValueError) -> str:
    message = str(error)
    if "kill switch" in message:
        return "KILL_SWITCH_ACTIVE"
    if "daily stop" in message:
        return "DAILY_STOP_OR_RISK_DAY"
    if "expired" in message:
        return "PLAN_EXPIRED"
    return "CLAIM_REJECTED"


def _position_context(
    plan_id: UUID,
    decision_id: UUID,
    sized: SizedPlan,
    market: PoolSnapshot,
    mode: TradingMode,
    command: DlmmCommandV1,
) -> PositionContext:
    plan = sized.plan
    step = Decimal(1) + Decimal(market.bin_step) / _BPS
    short = market.windows.get("5m")
    long = market.windows.get("30m")
    fees = long.fees if long is not None and long.fees is not None else Decimal(0)
    return PositionContext(
        position_id=command.position_address or entry_position_address(plan_id),
        plan_id=plan_id,
        decision_id=decision_id,
        pool_address=market.pool.address.root,
        base_mint=market.pool.base_mint.root,
        entry_price=market.price,
        lower_price=market.price * step**plan.lower_bin_offset,
        upper_price=market.price * step**plan.upper_bin_offset,
        entry_short_volume=short.volume if short is not None else Decimal(0),
        baseline_fee_tvl=fees / market.tvl if market.tvl > 0 else Decimal(0),
        entry_tvl=market.tvl,
        maximum_holding_period=plan.exit_policy.maximum_holding_period,
        opened_at=market.observed_at,
        mode=mode,
        ruleset_version=plan.ruleset_version,
        correlation_id=str(command.correlation_id),
        wallet_address=command.wallet_address,
        max_slippage_bps=command.max_slippage_bps,
        min_bin_id=command.min_bin_id,
        max_bin_id=command.max_bin_id,
    )


def audit_event(
    kind: EventType,
    occurred_at: datetime,
    aggregate_id: UUID,
    correlation_id: UUID,
    causation_id: UUID | None,
    payload: dict[str, object],
) -> AuditEvent:
    return AuditEvent(
        event_id=uuid4(),
        occurred_at=occurred_at,
        aggregate_id=aggregate_id,
        correlation_id=correlation_id,
        causation_id=causation_id,
        schema_version=1,
        event_type=kind,
        payload=payload,
    )
