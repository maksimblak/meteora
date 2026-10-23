"""Restart recovery: reconcile with the network first, only then settle entry plans."""

import asyncio
from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from src.accounting import InMemoryAccountingRepository, ShadowBroker
from src.core.clock import FrozenClock
from src.domain.models.trading import TradingMode
from src.execution.contracts import (
    ChainReceiptV1,
    ChainState,
    DlmmCommandV1,
    DlmmOperation,
    ExecutionPhase,
    GatewayReceiptV1,
    GatewayState,
    LiquidityStrategy,
    OperatorApproval,
    SimulationResultV1,
    bind_simulation_result,
    canonical_command_digest,
)
from src.execution.service import ExecutionRecord, ExecutionService
from src.modes import InMemoryModeRepository, PendingPlan, PendingPlanStatus
from src.persistence.memory import InMemoryRecoverableExecutionStore
from src.recovery.service import RecoveryAction, RecoveryService
from src.worker.execution import (
    EntryStatus,
    ShadowSubmitter,
    entry_command_id,
    entry_idempotency_key,
    entry_position_address,
)
from tests.worker.harness import NOW, POOL, WALLET, Harness, snapshot, spike_history

KEY = "11111111111111111111111111111111"


class ChainGateway:
    def __init__(self, log: list[str], states: dict[str, ChainState]) -> None:
        self._log = log
        self._states = states
        self.submits = 0

    async def simulate(self, command: DlmmCommandV1) -> SimulationResultV1:
        raise AssertionError("recovery must not simulate")

    async def submit(
        self, command: DlmmCommandV1, idempotency_key: str, approval: OperatorApproval | None
    ) -> GatewayReceiptV1:
        self.submits += 1
        raise AssertionError("recovery must never resubmit")

    async def status(self, signature: str) -> ChainReceiptV1:
        self._log.append(f"status:{signature}")
        return ChainReceiptV1(signature=signature, state=self._states[signature], checked_at=NOW)


class LoggingModes:
    def __init__(self, base: InMemoryModeRepository, log: list[str]) -> None:
        self._base = base
        self._log = log

    def __getattr__(self, name: str) -> Any:
        return getattr(self._base, name)

    def confirm_entry(self, plan_id: UUID, position_id: str) -> Any:
        self._log.append(f"confirm:{plan_id}")
        return self._base.confirm_entry(plan_id, position_id)

    def fail_entry(self, plan_id: UUID) -> Any:
        self._log.append(f"fail:{plan_id}")
        return self._base.fail_entry(plan_id)


def live_record(plan_id: UUID, phase: ExecutionPhase, signature: str | None) -> ExecutionRecord:
    command = DlmmCommandV1(
        command_id=entry_command_id(plan_id),
        idempotency_key=entry_idempotency_key(plan_id),
        correlation_id=uuid4(),
        ruleset_version="rules-1",
        mode=TradingMode.LIVE,
        operation=DlmmOperation.OPEN,
        pool_address=KEY,
        wallet_address=KEY,
        position_address=entry_position_address(plan_id),
        expires_at=NOW + timedelta(minutes=5),
        max_slippage_bps=100,
        strategy=LiquidityStrategy.SPOT,
        min_bin_id=-2,
        max_bin_id=2,
        amount_x="10",
        amount_y="10",
    )
    simulation = bind_simulation_result(
        command_id=command.command_id,
        command_digest=canonical_command_digest(command),
        success=True,
        simulated_at=NOW,
        blockhash="hash",
        last_valid_block_height=1,
        compute_units=1,
        network_fee_lamports="5000",
        priority_fee_lamports="0",
        price_impact_bps=5,
        min_bin_id=-2,
        max_bin_id=2,
        errors=(),
    )
    receipt = (
        None
        if signature is None
        else GatewayReceiptV1(
            command_id=command.command_id,
            command_digest=simulation.command_digest,
            simulation_digest=simulation.simulation_digest,
            idempotency_key=command.idempotency_key,
            state=GatewayState.UNKNOWN,
            signature=signature,
            signatures=(signature,),
            transaction_count=1,
            submitted_at=NOW,
            attempt_count=1,
        )
    )
    return ExecutionRecord(command=command, simulation=simulation, phase=phase, receipt=receipt)


def executing(base: InMemoryModeRepository, clock: FrozenClock) -> UUID:
    plan = PendingPlan(
        plan_id=uuid4(),
        correlation_id="cycle",
        admitted_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
    )
    assert base.admit_entry(plan, 10) == ()
    base.claim_entry(plan.plan_id, NOW)
    return plan.plan_id


def test_recovery_checks_the_network_before_settling_any_entry() -> None:
    async def scenario() -> tuple[Any, list[str], InMemoryModeRepository, dict[str, UUID], int]:
        clock = FrozenClock(NOW)
        log: list[str] = []
        base = InMemoryModeRepository()
        harness = Harness(clock=clock, modes=base)
        harness.keeper.ensure_open()
        plans = {name: executing(base, clock) for name in ("a", "b", "c", "d", "e")}
        store = InMemoryRecoverableExecutionStore()
        await store.put(live_record(plans["a"], ExecutionPhase.UNKNOWN, "sig-a"))
        await store.put(live_record(plans["b"], ExecutionPhase.SUBMITTED, "sig-b"))
        await store.put(live_record(plans["c"], ExecutionPhase.SUBMITTING, None))
        shadow = live_record(plans["d"], ExecutionPhase.SIMULATED, None).command.model_copy(
            update={"mode": TradingMode.SHADOW, "pool_address": POOL.address.root}
        )
        accounting = InMemoryAccountingRepository()
        ShadowBroker(accounting, clock).apply(shadow, snapshot(NOW), harness.policy.shadow_costs)
        gateway = ChainGateway(log, {"sig-a": ChainState.CONFIRMED, "sig-b": ChainState.FAILED})
        service = ExecutionService(
            gateway=gateway,
            store=store,
            plans=None,  # type: ignore[arg-type]
            clock=clock,
            allowed_mode=TradingMode.LIVE,
        )
        recovery = RecoveryService(
            modes=LoggingModes(base, log),  # type: ignore[arg-type]
            plans=base,
            accounting=accounting,
            alerts=harness.alerts,
            clock=clock,
            metrics=harness.metrics,
            executions=store,
            execution_service=service,
        )
        report = await recovery.reconcile(startup=True)
        return report, log, base, plans, gateway.submits

    report, log, modes, plans, submits = asyncio.run(scenario())

    first_settlement = next(i for i, entry in enumerate(log) if not entry.startswith("status:"))
    assert set(log[:first_settlement]) == {"status:sig-a", "status:sig-b"}
    assert all(not entry.startswith("status:") for entry in log[first_settlement:])
    actions = {item.plan_id: item.action for item in report.entries}
    assert actions == {
        plans["a"]: RecoveryAction.CONFIRMED_ENTRY,
        plans["b"]: RecoveryAction.FAILED_ENTRY,
        plans["c"]: RecoveryAction.OPERATOR_REQUIRED,
        plans["d"]: RecoveryAction.CONFIRMED_ENTRY,
        plans["e"]: RecoveryAction.FAILED_ENTRY,
    }
    status = {plan.plan_id: plan.status for plan in modes.entry_plans()}
    assert status[plans["c"]] is PendingPlanStatus.EXECUTING
    assert set(modes.open_positions()) == {
        entry_position_address(plans["a"]),
        entry_position_address(plans["d"]),
    }
    assert report.operator_required == (entry_command_id(plans["c"]),)
    assert [alert.code for alert in modes.alerts()] == ["EXECUTION_RECOVERY_REQUIRED"]
    assert submits == 0


def test_interrupted_shadow_entry_resumes_after_restart_without_duplicates() -> None:
    class CrashAfterFill:
        """The fill commits, then the process dies before ``confirm_entry``."""

        def __init__(self, inner: ShadowSubmitter) -> None:
            self._inner = inner

        async def submit(self, command: DlmmCommandV1, market: Any, guard: Any) -> Any:
            await self._inner.submit(command, market, guard)
            raise RuntimeError("worker process crashed")

    harness = Harness(wrap_shadow_submitter=CrashAfterFill)
    harness.ingest(spike_history())

    async def crash_then_recover() -> tuple[Any, Any, Any]:
        harness.keeper.ensure_open()
        interrupted = await harness.pipeline.evaluate(POOL.address.root, NOW)
        recovery = RecoveryService(
            modes=harness.modes,
            plans=harness.modes,
            accounting=harness.accounting,
            alerts=harness.alerts,
            clock=harness.clock,
            metrics=harness.metrics,
        )
        report = await recovery.reconcile(startup=True)
        replay = await harness.pipeline.evaluate(POOL.address.root, NOW)
        return interrupted, report, replay

    interrupted, report, replay = asyncio.run(crash_then_recover())

    assert interrupted.entry is not None and interrupted.entry.status is EntryStatus.PENDING
    assert [item.action for item in report.entries] == [RecoveryAction.CONFIRMED_ENTRY]
    assert len(harness.modes.open_positions()) == 1
    assert len(harness.accounting.positions()) == 1
    assert len(harness.accounting.ledger_entries()) == 1
    assert "PLAN_ALREADY_ADMITTED" in replay.reasons
    assert harness.accounting.positions()[0].invested_base_units > Decimal(0)
    assert WALLET
