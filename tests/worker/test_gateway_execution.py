"""APPROVAL / LIVE through the worker: durable approved entries, gateway exits, fail-closed.

The executor is an in-process fake with its own durable receipts (it survives a
worker restart, as the real executor's state file does); nothing reaches a network.
A "restart" is a new ``WorkerRuntime`` over the same durable repositories.
"""

import asyncio
import dataclasses
from collections.abc import Callable
from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from prometheus_client import CollectorRegistry

from src.core.clock import FrozenClock
from src.domain.models.trading import TradingMode
from src.domain.strategy import PositionActionKind
from src.execution.contracts import (
    ChainReceiptV1,
    ChainState,
    DlmmCommandV1,
    DlmmOperation,
    ExecutionPhase,
    GatewayReceiptV1,
    GatewayState,
    LivePromotionEvidence,
    OperatorApproval,
    SimulationResultV1,
    bind_simulation_result,
    canonical_command_digest,
)
from src.execution.service import (
    ExecutionBlocked,
    GatewayRejected,
    HmacApprovalVerifier,
    sign_operator_approval,
)
from src.modes import KillSwitchPolicy, ModeService, PendingPlanStatus
from src.modes.models import ModeTransition
from src.observability.metrics import WorkerMetrics
from src.persistence.contracts import JobLease
from src.persistence.factory import Repositories
from src.persistence.memory import InMemoryEventSink
from src.portfolio.models import PositionStatus
from src.providers.fixtures import FixtureOnChainProvider
from src.worker.execution import (
    AnchorSource,
    BinAnchor,
    EntryCommandUnavailable,
    PositionActionStatus,
    build_entry_command,
    entry_idempotency_key,
    position_action_command_id,
    snapshot_anchor,
)
from src.worker.policy import PipelinePolicy
from src.worker.runtime import GatewayWiring, WorkerRuntime, WorkerSettings
from src.worker.scheduler import JobContext
from tests.worker.harness import (
    NOW,
    POOL,
    harness_policy,
    memory_repositories,
    safe_token,
    snapshot,
    spike_history,
)

SECRET = b"0123456789abcdef0123456789abcdef"
ANCHOR = BinAnchor(active_bin_id=8_000, base_decimals=6, quote_decimals=6)


class FakeExecutor:
    """The TS executor's contract in-process: idempotent by key, receipts kept durably."""

    def __init__(self, clock: FrozenClock) -> None:
        self.clock = clock
        self.simulated: list[tuple[DlmmCommandV1, SimulationResultV1]] = []
        self.submitted: list[DlmmCommandV1] = []
        self.receipts: dict[UUID, GatewayReceiptV1] = {}
        self.submit_behavior = "confirm"
        self.reachable = True
        # (transaction_count, broadcast signatures) of a FAILED receipt, or None.
        self.failed_shape: tuple[int, int] | None = None

    async def simulate(self, command: DlmmCommandV1) -> SimulationResultV1:
        if not self.reachable:
            raise GatewayRejected("EXECUTOR_UNREACHABLE")
        simulation = bind_simulation_result(
            command_id=command.command_id,
            command_digest=canonical_command_digest(command),
            success=True,
            simulated_at=self.clock.now(),
            blockhash="blockhash",
            last_valid_block_height=1,
            compute_units=1,
            network_fee_lamports="5000",
            priority_fee_lamports="0",
            price_impact_bps=5,
            min_bin_id=command.min_bin_id,
            max_bin_id=command.max_bin_id,
            errors=(),
        )
        self.simulated.append((command, simulation))
        return simulation

    async def submit(
        self, command: DlmmCommandV1, idempotency_key: str, approval: OperatorApproval | None
    ) -> GatewayReceiptV1:
        if not self.reachable:
            raise GatewayRejected("EXECUTOR_UNREACHABLE")
        if self.submit_behavior == "reject":
            raise GatewayRejected("STALE_SIMULATION")
        if self.submit_behavior == "blocked":
            raise ExecutionBlocked("executor refused after the request was received")
        receipt = self.receipts.get(command.command_id)
        if receipt is None:
            simulation = next(sim for cmd, sim in reversed(self.simulated) if cmd == command)
            self.submitted.append(command)
            count, signed = (1, 1) if self.failed_shape is None else self.failed_shape
            signatures = tuple(f"sig-{len(self.submitted)}-{n}" for n in range(signed))
            receipt = GatewayReceiptV1(
                command_id=command.command_id,
                command_digest=simulation.command_digest,
                simulation_digest=simulation.simulation_digest,
                idempotency_key=idempotency_key,
                state=GatewayState.CONFIRMED if self.failed_shape is None else GatewayState.FAILED,
                signature=signatures[-1] if signatures else None,
                signatures=signatures,
                transaction_count=count,
                submitted_at=self.clock.now(),
                attempt_count=1,
            )
            self.receipts[command.command_id] = receipt
        if self.submit_behavior == "timeout":
            raise TimeoutError("the answer was lost after the broadcast")
        if self.submit_behavior == "mismatch":
            return receipt.model_copy(update={"simulation_digest": "f" * 64})
        return receipt

    async def lookup(self, command_id: UUID) -> GatewayReceiptV1 | None:
        return self.receipts.get(command_id)

    async def status(self, signature: str) -> ChainReceiptV1:
        return ChainReceiptV1(
            signature=signature, state=ChainState.CONFIRMED, checked_at=self.clock.now()
        )

    async def ready(self, mode: TradingMode) -> bool:
        return self.reachable

    def operations(self) -> list[DlmmOperation]:
        return [command.operation for command in self.submitted]


def live_promotion() -> LivePromotionEvidence:
    return LivePromotionEvidence(
        promotion_id=UUID(int=9),
        ruleset_version="rules-1",
        approval_cycles=10,
        receipts_reconciled=True,
        risk_invariants_preserved=True,
        dedicated_rpc_ready=True,
        wallet_ready=True,
        checklist_completed=True,
        operator_confirmation_id="operator",
        promoted_at=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(days=1),
    )


def worker(
    repos: Repositories,
    clock: FrozenClock,
    executor: FakeExecutor,
    mode: TradingMode,
    *,
    anchors: AnchorSource = lambda market: ANCHOR,
    policy: PipelinePolicy | None = None,
) -> WorkerRuntime:
    return WorkerRuntime(
        settings=WorkerSettings(
            metrics_port=None, owner=f"worker-{uuid4()}", shadow_equity_base_units=1_000_000
        ),
        policy=policy or harness_policy(),
        repositories=repos,
        market=None,
        onchain=FixtureOnChainProvider(
            token_risks={POOL.base_mint.root: safe_token()}, clock=clock
        ),
        clock=clock,
        metrics=WorkerMetrics(CollectorRegistry()),
        sink=InMemoryEventSink(),
        gateway=GatewayWiring(
            gateway=executor,
            allowed_mode=mode,
            receipts=executor,
            readiness=executor,
            approval_verifier=HmacApprovalVerifier(SECRET),
            live_promotion=live_promotion() if mode is TradingMode.LIVE else None,
        ),
        anchors=anchors,
    )


def promote(repos: Repositories, mode: TradingMode) -> None:
    state = repos.mode.mode_state()
    repos.mode.commit_transition(
        ModeTransition(
            transition_id=uuid4(),
            from_mode=state.mode,
            to_mode=mode,
            occurred_at=NOW,
            operator_confirmation_id="operator",
            promotion_authorized=True,
        ),
        expected_version=state.version,
        live_promotion_id=uuid4(),
    )


def ingest(repos: Repositories, *snapshots: Any) -> None:
    async def stage_all() -> None:
        for item in snapshots:
            stage = await repos.universe.begin(uuid4(), item.observed_at)
            await stage.stage((item,))
            await stage.commit()

    asyncio.run(stage_all())


def job(name: str, clock: FrozenClock) -> JobContext:
    now = clock.now()
    lease = JobLease(
        name=name, owner="test", token=1, acquired_at=now, expires_at=now + timedelta(minutes=2)
    )
    return JobContext(name=name, correlation_id=uuid4(), started_at=now, lease=lease)


def run_job(runtime: WorkerRuntime, name: str) -> None:
    handler: Callable[[JobContext], Any] = {
        "candidate": runtime.candidate_job,
        "position": runtime.position_job,
    }[name]
    asyncio.run(handler(job(name, runtime.clock)))  # type: ignore[arg-type]


def restart(runtime: WorkerRuntime, executor: FakeExecutor, mode: TradingMode) -> WorkerRuntime:
    """A new process: fresh objects, same durable stores, startup recovery first."""

    assert isinstance(runtime.clock, FrozenClock)
    again = worker(runtime.repositories, runtime.clock, executor, mode)
    asyncio.run(again.recovery.reconcile(startup=True))
    return again


def executing_plan(repos: Repositories) -> UUID:
    (plan,) = [p for p in repos.mode.entry_plans() if p.status is PendingPlanStatus.EXECUTING]
    return plan.plan_id


def plan_status(repos: Repositories, plan_id: UUID) -> PendingPlanStatus:
    plan = repos.mode.entry_plan(plan_id)
    assert plan is not None
    return plan.status


def record_phase(repos: Repositories, key: str) -> ExecutionPhase:
    record = asyncio.run(repos.execution.get_by_idempotency_key(key))
    assert record is not None
    return record.phase


def approve(runtime: WorkerRuntime, command_id: UUID, ttl: timedelta) -> None:
    async def run() -> None:
        assert runtime.execution is not None
        record = await runtime.repositories.execution.get(command_id)
        assert record is not None
        simulation = record.simulation
        approval_id = uuid4()
        approved_at = simulation.simulated_at
        proof = sign_operator_approval(
            SECRET,
            approval_id=approval_id,
            approved_at=approved_at,
            expires_at=approved_at + ttl,
            operator_id="operator",
            command_digest=simulation.command_digest,
            simulation_digest=simulation.simulation_digest,
        )
        await runtime.execution.approve(
            command_id,
            OperatorApproval(
                approval_id=approval_id,
                approved_at=approved_at,
                expires_at=approved_at + ttl,
                operator_id="operator",
                command_digest=simulation.command_digest,
                simulation_digest=simulation.simulation_digest,
                proof=proof,
            ),
        )

    asyncio.run(run())


def approval_world(
    executor_clock: FrozenClock | None = None,
) -> tuple[WorkerRuntime, FakeExecutor, UUID]:
    """APPROVAL mode with one entry simulated and awaiting the operator."""

    clock = executor_clock or FrozenClock(NOW)
    repos = memory_repositories(clock)
    executor = FakeExecutor(clock)
    promote(repos, TradingMode.APPROVAL)
    ingest(repos, *spike_history())
    first = worker(repos, clock, executor, TradingMode.APPROVAL)
    run_job(first, "candidate")
    plan_id = executing_plan(repos)
    assert record_phase(repos, entry_idempotency_key(plan_id)) is ExecutionPhase.AWAITING_APPROVAL
    return first, executor, plan_id


def live_world(
    *, policy: PipelinePolicy | None = None
) -> tuple[WorkerRuntime, FakeExecutor, FrozenClock]:
    clock = FrozenClock(NOW)
    repos = memory_repositories(clock)
    executor = FakeExecutor(clock)
    promote(repos, TradingMode.LIVE)
    ingest(repos, *spike_history())
    return worker(repos, clock, executor, TradingMode.LIVE, policy=policy), executor, clock


def open_live_position(runtime: WorkerRuntime) -> str:
    run_job(runtime, "candidate")
    (position_id,) = runtime.repositories.mode.open_positions()
    position = runtime.repositories.accounting.position_for(position_id)
    assert position is not None and position.mode is TradingMode.LIVE
    return position_id


def decay(runtime: WorkerRuntime, clock: FrozenClock) -> None:
    """One minute later the short-window volume collapses: the monitor says EXIT."""

    clock.advance(timedelta(minutes=1))
    ingest(runtime.repositories, snapshot(clock.now(), short="50", long="1200"))


# --- approved entries are executed from durable state -------------------------------------


def test_an_approved_entry_executes_exactly_once_after_restarts() -> None:
    first, executor, plan_id = approval_world()
    record = asyncio.run(first.repositories.execution.get_by_idempotency_key(
        entry_idempotency_key(plan_id)
    ))
    assert record is not None
    approve(first, record.command.command_id, timedelta(seconds=60))
    assert executor.submitted == []

    second = restart(first, executor, TradingMode.APPROVAL)
    run_job(second, "candidate")
    run_job(second, "candidate")
    third = restart(second, executor, TradingMode.APPROVAL)
    run_job(third, "candidate")

    repos = third.repositories
    assert [command.command_id for command in executor.submitted] == [record.command.command_id]
    assert plan_status(repos, plan_id) is PendingPlanStatus.OPENED
    assert repos.mode.open_positions() == (record.command.position_address,)
    booked = repos.accounting.position_for(record.command.position_address)
    assert booked is not None and booked.mode is TradingMode.APPROVAL


def test_an_expired_approval_is_never_executed_after_a_restart() -> None:
    first, executor, plan_id = approval_world()
    record = asyncio.run(first.repositories.execution.get_by_idempotency_key(
        entry_idempotency_key(plan_id)
    ))
    assert record is not None
    approve(first, record.command.command_id, timedelta(seconds=60))
    assert isinstance(first.clock, FrozenClock)
    first.clock.advance(timedelta(seconds=61))

    second = restart(first, executor, TradingMode.APPROVAL)
    run_job(second, "candidate")

    repos = second.repositories
    assert executor.submitted == []
    assert plan_status(repos, plan_id) is PendingPlanStatus.FAILED
    assert record_phase(repos, entry_idempotency_key(plan_id)) is ExecutionPhase.APPROVED


def test_an_unapproved_entry_waits_for_the_operator_and_is_never_sent() -> None:
    first, executor, plan_id = approval_world()

    run_job(first, "candidate")
    second = restart(first, executor, TradingMode.APPROVAL)
    run_job(second, "candidate")

    assert executor.submitted == []
    assert plan_status(second.repositories, plan_id) is PendingPlanStatus.EXECUTING


# --- an ExecutionBlocked after SUBMITTING keeps the plan reserved ---------------------------


@pytest.mark.parametrize("behavior", ["mismatch", "blocked", "timeout"])
def test_a_block_after_submitting_keeps_the_plan_executing_until_reconciled(
    behavior: str,
) -> None:
    runtime, executor, clock = live_world()
    executor.submit_behavior = behavior

    run_job(runtime, "candidate")

    repos = runtime.repositories
    plan_id = executing_plan(repos)
    assert record_phase(repos, entry_idempotency_key(plan_id)) is ExecutionPhase.UNKNOWN
    assert repos.mode.committed_entry_count(clock.now()) == 1
    assert repos.mode.open_positions() == ()


def test_a_reconciled_unknown_entry_opens_without_a_second_submit() -> None:
    runtime, executor, _ = live_world()
    executor.submit_behavior = "timeout"
    run_job(runtime, "candidate")
    plan_id = executing_plan(runtime.repositories)

    executor.submit_behavior = "confirm"
    again = restart(runtime, executor, TradingMode.LIVE)
    run_job(again, "candidate")

    assert len(executor.submitted) == 1
    assert plan_status(again.repositories, plan_id) is PendingPlanStatus.OPENED


def test_a_definitive_gateway_rejection_fails_the_plan_and_frees_its_capacity() -> None:
    runtime, executor, clock = live_world()
    executor.submit_behavior = "reject"

    run_job(runtime, "candidate")

    repos = runtime.repositories
    (plan,) = repos.mode.entry_plans()
    assert plan.status is PendingPlanStatus.FAILED
    assert record_phase(repos, entry_idempotency_key(plan.plan_id)) is ExecutionPhase.FAILED
    assert repos.mode.committed_entry_count(clock.now()) == 0


# --- non-SHADOW exits and reductions go through a fresh simulation and the gateway ---------


def test_a_live_exit_is_simulated_fresh_and_submitted_through_the_gateway() -> None:
    runtime, executor, clock = live_world()
    position_id = open_live_position(runtime)
    decay(runtime, clock)

    run_job(runtime, "position")

    repos = runtime.repositories
    assert executor.operations() == [DlmmOperation.OPEN, DlmmOperation.CLOSE]
    close_command, close_simulation = executor.simulated[-1]
    assert close_command.operation is DlmmOperation.CLOSE
    assert close_command.position_address == position_id
    assert close_simulation.simulated_at == clock.now()
    assert repos.mode.open_positions() == ()
    closed = repos.accounting.position_for(position_id)
    assert closed is not None and closed.status is PositionStatus.CLOSED
    assert len(repos.accounting.outcomes()) == 1


def test_an_unreachable_gateway_leaves_the_position_open_under_an_alert() -> None:
    runtime, executor, clock = live_world()
    position_id = open_live_position(runtime)
    decay(runtime, clock)
    executor.reachable = False

    run_job(runtime, "position")

    repos = runtime.repositories
    assert repos.mode.open_positions() == (position_id,)
    assert executor.operations() == [DlmmOperation.OPEN]
    open_alerts = {alert.code for alert in repos.mode.alerts() if alert.open}
    assert "EXIT_GATEWAY_UNAVAILABLE" in open_alerts

    executor.reachable = True
    decay(runtime, clock)
    run_job(runtime, "position")

    assert executor.operations() == [DlmmOperation.OPEN, DlmmOperation.CLOSE]
    assert repos.mode.open_positions() == ()


def test_an_exit_with_an_unknown_outcome_is_reconciled_and_never_sent_twice() -> None:
    runtime, executor, clock = live_world()
    position_id = open_live_position(runtime)
    decay(runtime, clock)
    executor.submit_behavior = "timeout"

    run_job(runtime, "position")
    assert runtime.repositories.mode.open_positions() == (position_id,)

    executor.submit_behavior = "confirm"
    again = restart(runtime, executor, TradingMode.LIVE)
    run_job(again, "position")

    assert executor.operations() == [DlmmOperation.OPEN, DlmmOperation.CLOSE]
    assert again.repositories.mode.open_positions() == ()


def test_a_reduction_removes_part_of_the_position_once_then_an_exit_closes_it() -> None:
    runtime, executor, _ = live_world()
    position_id = open_live_position(runtime)
    manager = runtime.gateway_positions
    assert manager is not None
    accounting = runtime.repositories.accounting
    invested = accounting.position_for(position_id).open_cost_basis_base_units

    first = asyncio.run(manager.apply({position_id: PositionActionKind.REDUCE}))
    second = asyncio.run(manager.apply({position_id: PositionActionKind.REDUCE}))
    reduced = accounting.position_for(position_id)
    final = asyncio.run(manager.apply({position_id: PositionActionKind.EXIT}))

    assert [result.status for result in first] == [PositionActionStatus.REDUCED]
    assert second == ()
    assert reduced is not None and reduced.status is PositionStatus.OPEN
    assert reduced.open_cost_basis_base_units == invested / 2
    assert [result.status for result in final] == [PositionActionStatus.EXITED]
    removal = executor.submitted[1]
    assert (removal.operation, removal.remove_bps) == (DlmmOperation.REMOVE, 5_000)
    assert executor.operations() == [
        DlmmOperation.OPEN,
        DlmmOperation.REMOVE,
        DlmmOperation.CLOSE,
    ]


def test_a_partly_broadcast_failed_removal_stops_the_position_for_the_operator() -> None:
    runtime, executor, _ = live_world()
    position_id = open_live_position(runtime)
    manager = runtime.gateway_positions
    assert manager is not None
    executor.failed_shape = (2, 1)  # the first of two transactions was broadcast

    cycles = [
        asyncio.run(manager.apply({position_id: action}))
        for action in (
            PositionActionKind.REDUCE,
            PositionActionKind.REDUCE,
            PositionActionKind.REDUCE,
            PositionActionKind.EXIT,
        )
    ]
    again = restart(runtime, executor, TradingMode.LIVE)
    assert again.gateway_positions is not None
    after_restart = asyncio.run(
        again.gateway_positions.apply({position_id: PositionActionKind.EXIT})
    )

    assert executor.operations() == [DlmmOperation.OPEN, DlmmOperation.REMOVE]
    assert [result.status for cycle in cycles for result in cycle] == [
        PositionActionStatus.OPERATOR_REQUIRED
    ] * 4
    assert [result.status for result in after_restart] == [PositionActionStatus.OPERATOR_REQUIRED]
    open_alerts = [alert.code for alert in runtime.repositories.mode.alerts() if alert.open]
    assert "POSITION_PARTIALLY_EXECUTED" in open_alerts
    assert runtime.repositories.mode.open_positions() == (position_id,)


@pytest.mark.parametrize("shape", [(2, 0), (1, 1)], ids=["nothing-broadcast", "one-atomic-tx"])
def test_a_removal_that_failed_without_partial_effect_is_retried_as_before(
    shape: tuple[int, int],
) -> None:
    runtime, executor, _ = live_world()
    position_id = open_live_position(runtime)
    manager = runtime.gateway_positions
    assert manager is not None
    executor.failed_shape = shape

    first = asyncio.run(manager.apply({position_id: PositionActionKind.REDUCE}))
    second = asyncio.run(manager.apply({position_id: PositionActionKind.REDUCE}))

    assert [result.status for result in (*first, *second)] == [PositionActionStatus.FAILED] * 2
    assert executor.operations() == [
        DlmmOperation.OPEN,
        DlmmOperation.REMOVE,
        DlmmOperation.REMOVE,
    ]


def test_an_approval_mode_exit_waits_for_its_own_one_shot_approval() -> None:
    first, executor, plan_id = approval_world()
    entry = asyncio.run(first.repositories.execution.get_by_idempotency_key(
        entry_idempotency_key(plan_id)
    ))
    assert entry is not None and entry.command.position_address is not None
    approve(first, entry.command.command_id, timedelta(seconds=60))
    run_job(first, "candidate")
    position_id = entry.command.position_address
    assert isinstance(first.clock, FrozenClock)
    decay(first, first.clock)

    run_job(first, "position")
    assert executor.operations() == [DlmmOperation.OPEN]
    exit_id = position_action_command_id(position_id, 1)
    approve(first, exit_id, timedelta(seconds=60))
    run_job(first, "position")

    assert executor.operations() == [DlmmOperation.OPEN, DlmmOperation.CLOSE]
    assert first.repositories.mode.open_positions() == ()


@pytest.mark.parametrize(
    ("policy", "exits"),
    [(KillSwitchPolicy.HOLD_AND_ALERT, False), (KillSwitchPolicy.ORDERLY_EXIT, True)],
)
def test_kill_switch_policy_decides_whether_a_live_position_exits(
    policy: KillSwitchPolicy, exits: bool
) -> None:
    runtime, executor, clock = live_world(
        policy=dataclasses.replace(harness_policy(), kill_switch_policy=policy)
    )
    position_id = open_live_position(runtime)
    ModeService(runtime.repositories.mode, clock, "rules-1").activate_kill_switch(
        "operator stop", policy
    )
    decay(runtime, clock)

    run_job(runtime, "position")

    expected = [DlmmOperation.OPEN, DlmmOperation.CLOSE] if exits else [DlmmOperation.OPEN]
    assert executor.operations() == expected
    assert runtime.repositories.mode.open_positions() == (() if exits else (position_id,))


# --- a non-SHADOW entry never sends a relative bin offset ----------------------------------


def _entry(mode: TradingMode, anchor: BinAnchor | None) -> DlmmCommandV1:
    return build_entry_command(
        plan_id=UUID(int=3),
        mode=mode,
        market=snapshot(NOW, price="2"),
        size_base_units=1_000,
        strategy_kind="SPOT",
        lower_bin_offset=-5,
        upper_bin_offset=5,
        expires_at=NOW + timedelta(minutes=5),
        correlation_id="cycle",
        ruleset_version="rules-1",
        wallet_address="SysvarC1ock11111111111111111111111111111111",
        position_address="11111111111111111111111111111111",
        max_slippage_bps=100,
        anchor=anchor,
    )


@pytest.mark.parametrize("mode", [TradingMode.APPROVAL, TradingMode.LIVE])
def test_a_gateway_entry_without_active_bin_and_decimals_is_refused(mode: TradingMode) -> None:
    with pytest.raises(EntryCommandUnavailable, match="ENTRY_ANCHOR_UNAVAILABLE"):
        _entry(mode, None)


def test_a_gateway_entry_uses_absolute_bins_and_token_base_units() -> None:
    anchor = BinAnchor(active_bin_id=5_000, base_decimals=9, quote_decimals=6)
    command = _entry(TradingMode.LIVE, anchor)

    # 1000 quote units: 500 stay Y; 500 / price 2 = 250 X tokens = 250 * 10^9 / 10^6 base units.
    assert (command.min_bin_id, command.max_bin_id) == (4_995, 5_005)
    assert (command.amount_x, command.amount_y) == ("250000", "500")


def test_shadow_entries_keep_the_relative_bins_of_the_shadow_model() -> None:
    command = _entry(TradingMode.SHADOW, None)

    assert (command.min_bin_id, command.max_bin_id) == (-5, 5)
    assert (command.amount_x, command.amount_y) == ("250", "500")


@pytest.mark.parametrize(
    ("anchors", "reason"),
    [
        (snapshot_anchor, "ENTRY_ANCHOR_UNAVAILABLE"),
        (lambda market: BinAnchor(8_000, 9, 6), "ACCOUNTING_UNITS_UNSUPPORTED"),
    ],
    ids=["no-anchor-in-snapshot", "unequal-decimals"],
)
def test_the_worker_cancels_a_gateway_entry_it_cannot_express_safely(
    anchors: AnchorSource, reason: str
) -> None:
    clock = FrozenClock(NOW)
    repos = memory_repositories(clock)
    executor = FakeExecutor(clock)
    promote(repos, TradingMode.LIVE)
    ingest(repos, *spike_history())
    runtime = worker(repos, clock, executor, TradingMode.LIVE, anchors=anchors)

    async def evaluate() -> Any:
        runtime.keeper.ensure_open()
        return await runtime.pipeline.evaluate(POOL.address.root, NOW)

    outcome = asyncio.run(evaluate())

    assert outcome.entry is not None and outcome.entry.reason == reason
    assert plan_status(repos, outcome.plan_id) is PendingPlanStatus.CANCELLED
    assert executor.simulated == [] and executor.submitted == []
    assert repos.mode.committed_entry_count(clock.now()) == 0


def test_the_booked_live_entry_matches_the_sized_capital() -> None:
    runtime, _, _ = live_world()
    position_id = open_live_position(runtime)
    position = runtime.repositories.accounting.position_for(position_id)
    context = asyncio.run(runtime.repositories.positions.get(position_id))

    assert position is not None and context is not None
    assert (context.min_bin_id, context.max_bin_id) != (-5, 5)
    assert position.open_cost_basis_base_units > Decimal(0)
