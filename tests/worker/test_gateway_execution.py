"""APPROVAL / LIVE through the worker: durable approved entries, gateway exits, fail-closed.

The executor is an in-process fake of contract V2 with its own durable receipts (it
survives a worker restart, as the real executor's state file does): like the real one
it creates the position of every OPEN it simulates and names it in the receipt, and it
reports the pool's active bin and mint decimals. Nothing reaches a network. A
"restart" is a new ``WorkerRuntime`` over the same durable repositories.
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
from src.domain.models.events import EventType
from src.domain.models.primitives import BaseUnits, PublicKey
from src.domain.models.trading import TradingMode
from src.domain.strategy import PositionActionKind
from src.execution.contracts import (
    ChainReceiptV1,
    ChainState,
    DlmmCommand,
    DlmmCommandV1,
    DlmmCommandV2,
    DlmmOperation,
    ExecutionPhase,
    GatewayReceiptV2,
    GatewayState,
    LivePromotionEvidence,
    OperatorApproval,
    SimulationErrorV1,
    SimulationResultV2,
    bind_simulation_result_v2,
    canonical_command_digest,
)
from src.execution.service import (
    ExecutionBlocked,
    GatewayRejected,
    HmacApprovalVerifier,
    sign_operator_approval,
)
from src.market.models import DataQuality, PoolRef, WalletState
from src.modes import KillSwitchPolicy, ModeService, PendingPlanStatus
from src.modes.models import ModeTransition
from src.observability.metrics import WorkerMetrics
from src.persistence.contracts import JobLease
from src.persistence.factory import Repositories
from src.persistence.memory import InMemoryEventSink
from src.portfolio.models import PositionStatus
from src.providers.contracts import LbPairState
from src.providers.fixtures import FixtureOnChainProvider
from src.recovery.service import RecoveryAction
from src.worker.execution import (
    AnchorSource,
    BinAnchor,
    EntryCommandUnavailable,
    PositionActionStatus,
    build_entry_command,
    entry_idempotency_key,
    entry_position_address,
    position_action_command_id,
    shadow_address,
    snapshot_anchor,
)
from src.worker.policy import DEFAULT_ACTIVE_BIN_TOLERANCE as ACTIVE_BIN_TOLERANCE
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
WALLET_ADDRESS = "SysvarC1ock11111111111111111111111111111111"


class StaticPoolStates:
    """The LB pair account as a fresh RPC read reports it right before an entry."""

    def __init__(self, active_bin_id: int | None = ANCHOR.active_bin_id, *, bin_step: int = 25):
        self.active_bin_id = active_bin_id
        self.bin_step = bin_step
        self.reads = 0

    async def get_pool_states(self, pools: Any) -> dict[str, LbPairState]:
        self.reads += 1
        if self.active_bin_id is None:
            return {}
        return {
            pool.address.root: LbPairState(
                self.active_bin_id, self.bin_step, pool.base_mint.root, pool.quote_mint.root
            )
            for pool in pools
            if isinstance(pool, PoolRef)
        }

    async def get_mint_decimals(self, mints: Any) -> dict[str, int]:
        return {}


def funded_wallet() -> WalletState:
    """Both legs available: the base token and native SOL for the wSOL quote leg."""

    return WalletState(
        wallet=PublicKey(WALLET_ADDRESS),
        native_balance=BaseUnits(10**15),
        token_balances={POOL.base_mint.root: BaseUnits(10**15)},
        observed_at=NOW,
        source_at=NOW,
        ingested_at=NOW,
        data_quality=DataQuality.COMPLETE,
    )


FUNDED_WALLET = funded_wallet()


class FakeExecutor:
    """The TS executor's contract V2 in-process: idempotent by key, receipts kept durably."""

    def __init__(self, clock: FrozenClock) -> None:
        self.clock = clock
        self.simulated: list[tuple[DlmmCommand, SimulationResultV2]] = []
        self.submitted: list[DlmmCommand] = []
        self.receipts: dict[UUID, GatewayReceiptV2] = {}
        self.submit_behavior = "confirm"
        self.reachable = True
        # (transaction_count, broadcast signatures) of a FAILED receipt, or None.
        self.failed_shape: tuple[int, int] | None = None
        # Bins the pool moved since the command's anchor was observed.
        self.active_bin_drift = 0
        self.created_positions: list[str] = []
        # Executor error code of a refused simulation, per operation.
        self.simulation_failures: dict[DlmmOperation, str] = {}
        # Quoted price impact of the swap leg, per operation (default 5 bps).
        self.price_impacts: dict[DlmmOperation, int | None] = {}

    async def simulate(self, command: DlmmCommand) -> SimulationResultV2:
        if not self.reachable:
            raise GatewayRejected("EXECUTOR_UNREACHABLE")
        assert isinstance(command, DlmmCommandV2), "the executor speaks contract V2 only"
        anchor = command.active_bin_id
        active = None if anchor is None else anchor + self.active_bin_drift
        failure = self.simulation_failures.get(command.operation)
        if (
            active is not None
            and command.active_bin_tolerance is not None
            and anchor is not None
            and abs(active - anchor) > command.active_bin_tolerance
        ):
            failure = "ACTIVE_BIN_DRIFT"
        drifted = failure is not None
        if command.operation is DlmmOperation.OPEN and not drifted:
            # Like the real executor: a fresh key per simulated OPEN, only its address leaves.
            position: str | None = shadow_address(f"executor-key:{len(self.simulated)}")
            assert position is not None
            self.created_positions.append(position)
        elif drifted:
            position = None
        else:
            position = command.position_address
        simulation = bind_simulation_result_v2(
            command_id=command.command_id,
            command_digest=canonical_command_digest(command),
            success=not drifted,
            simulated_at=self.clock.now(),
            blockhash="blockhash" if not drifted else None,
            last_valid_block_height=1 if not drifted else None,
            compute_units=1,
            network_fee_lamports="5000",
            priority_fee_lamports="0",
            price_impact_bps=self.price_impacts.get(command.operation, 5),
            min_bin_id=command.min_bin_id,
            max_bin_id=command.max_bin_id,
            position_address=position,
            active_bin_id=active if active is not None else 8_000,
            token_x_decimals=6 if command.token_x_decimals is None else command.token_x_decimals,
            token_y_decimals=6 if command.token_y_decimals is None else command.token_y_decimals,
            errors=(
                (SimulationErrorV1(code=failure, message="the executor refused the command"),)
                if failure is not None
                else ()
            ),
        )
        self.simulated.append((command, simulation))
        return simulation

    async def submit(
        self, command: DlmmCommand, idempotency_key: str, approval: OperatorApproval | None
    ) -> GatewayReceiptV2:
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
            assert simulation.position_address is not None
            receipt = GatewayReceiptV2(
                command_id=command.command_id,
                command_digest=simulation.command_digest,
                simulation_digest=simulation.simulation_digest,
                idempotency_key=idempotency_key,
                state=GatewayState.CONFIRMED if self.failed_shape is None else GatewayState.FAILED,
                position_address=simulation.position_address,
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
        if self.submit_behavior == "submitted":
            # Broadcast and later confirmed on chain; the first answer only says SUBMITTED.
            return receipt.model_copy(update={"state": GatewayState.SUBMITTED})
        return receipt

    async def lookup(self, command_id: UUID) -> GatewayReceiptV2 | None:
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
    pool_states: StaticPoolStates | None = None,
    wallet: WalletState | None = FUNDED_WALLET,
) -> WorkerRuntime:
    return WorkerRuntime(
        settings=WorkerSettings(
            metrics_port=None, owner=f"worker-{uuid4()}", shadow_equity_base_units=1_000_000
        ),
        policy=policy or harness_policy(),
        repositories=repos,
        market=None,
        onchain=FixtureOnChainProvider(
            token_risks={POOL.base_mint.root: safe_token()},
            wallet_states={} if wallet is None else {WALLET_ADDRESS: wallet},
            clock=clock,
        ),
        pool_states=pool_states or StaticPoolStates(),
        wallet_address=WALLET_ADDRESS,
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
    (position_id,) = executor.created_positions
    assert repos.mode.open_positions() == (position_id,)
    booked = repos.accounting.position_for(position_id)
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
    assert entry is not None and entry.command.position_address is None
    approve(first, entry.command.command_id, timedelta(seconds=60))
    run_job(first, "candidate")
    (position_id,) = executor.created_positions
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


def _entry(
    mode: TradingMode,
    anchor: BinAnchor | None,
    *,
    price: str = "2",
    size: int = 1_000,
) -> DlmmCommand:
    return build_entry_command(
        plan_id=UUID(int=3),
        mode=mode,
        market=snapshot(NOW, price=price),
        size_base_units=size,
        strategy_kind="SPOT",
        lower_bin_offset=-5,
        upper_bin_offset=5,
        expires_at=NOW + timedelta(minutes=5),
        correlation_id="cycle",
        ruleset_version="rules-1",
        wallet_address="SysvarC1ock11111111111111111111111111111111",
        max_slippage_bps=100,
        anchor=anchor,
    )


@pytest.mark.parametrize("mode", [TradingMode.APPROVAL, TradingMode.LIVE])
def test_a_gateway_entry_without_active_bin_and_decimals_is_refused(mode: TradingMode) -> None:
    with pytest.raises(EntryCommandUnavailable, match="ENTRY_ANCHOR_UNAVAILABLE"):
        _entry(mode, None)


def test_a_gateway_entry_is_a_v2_command_without_a_position_and_with_its_anchor() -> None:
    anchor = BinAnchor(active_bin_id=5_000, base_decimals=9, quote_decimals=6)
    command = _entry(TradingMode.LIVE, anchor)

    assert isinstance(command, DlmmCommandV2)
    assert command.position_address is None
    # 1000 quote units: 500 stay Y; 500 / price 2 = 250 X tokens = 250 * 10^9 / 10^6 base units.
    assert (command.min_bin_id, command.max_bin_id) == (4_995, 5_005)
    assert (command.amount_x, command.amount_y) == ("250000", "500")
    assert (command.active_bin_id, command.active_bin_tolerance) == (5_000, ACTIVE_BIN_TOLERANCE)
    assert (command.token_x_decimals, command.token_y_decimals) == (9, 6)


@pytest.mark.parametrize(
    ("anchor", "price", "size", "amount_x"),
    [
        # X has 3 more decimals than Y: whole accounting units only, 501/7 -> 71 -> 71 * 10^3.
        (BinAnchor(active_bin_id=1, base_decimals=9, quote_decimals=6), "7", 1_001, "71000"),
        # Y (SOL) has 3 more decimals than X: 0.5 SOL / 0.002 SOL = 250 tokens of 6 decimals.
        (
            BinAnchor(active_bin_id=1, base_decimals=6, quote_decimals=9),
            "0.002",
            1_000_000_000,
            "250000000",
        ),
    ],
    ids=["x-finer-than-y", "y-finer-than-x"],
)
def test_entry_amounts_are_exact_in_both_token_scales(
    anchor: BinAnchor, price: str, size: int, amount_x: str
) -> None:
    command = _entry(TradingMode.LIVE, anchor, price=price, size=size)

    assert command.amount_x == amount_x


def test_shadow_entries_keep_the_relative_bins_of_the_shadow_model() -> None:
    command = _entry(TradingMode.SHADOW, None)

    assert isinstance(command, DlmmCommandV1)
    assert command.position_address == entry_position_address(UUID(int=3))
    assert (command.min_bin_id, command.max_bin_id) == (-5, 5)
    assert (command.amount_x, command.amount_y) == ("250", "500")


def test_the_worker_cancels_a_gateway_entry_it_cannot_express_safely() -> None:
    clock = FrozenClock(NOW)
    repos = memory_repositories(clock)
    executor = FakeExecutor(clock)
    promote(repos, TradingMode.LIVE)
    ingest(repos, *spike_history())
    runtime = worker(repos, clock, executor, TradingMode.LIVE, anchors=snapshot_anchor)
    reason = "ENTRY_ANCHOR_UNAVAILABLE"

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


# --- contract V2: the executor creates the position, Python only reads it from the receipt ---


def test_a_live_entry_opens_the_position_the_executor_created_never_a_derived_one() -> None:
    runtime, executor, _ = live_world()

    position_id = open_live_position(runtime)

    repos = runtime.repositories
    (plan,) = repos.mode.entry_plans()
    (created,) = executor.created_positions
    entry_command, _ = executor.simulated[0]
    record = asyncio.run(
        repos.execution.get_by_idempotency_key(entry_idempotency_key(plan.plan_id))
    )
    assert record is not None and isinstance(record.receipt, GatewayReceiptV2)
    assert position_id == created == record.receipt.position_address == plan.position_id
    assert position_id != entry_position_address(plan.plan_id)
    assert isinstance(entry_command, DlmmCommandV2) and entry_command.position_address is None
    assert asyncio.run(repos.positions.get(created)) is not None
    assert repos.accounting.position_for(created) is not None


def test_an_entry_with_different_token_decimals_opens_and_is_booked_in_quote_units() -> None:
    clock = FrozenClock(NOW)
    repos = memory_repositories(clock)
    executor = FakeExecutor(clock)
    promote(repos, TradingMode.LIVE)
    ingest(repos, *spike_history())
    anchor = BinAnchor(active_bin_id=8_000, base_decimals=6, quote_decimals=9)
    runtime = worker(repos, clock, executor, TradingMode.LIVE, anchors=lambda market: anchor)

    position_id = open_live_position(runtime)

    command, simulation = executor.simulated[0]
    assert isinstance(command, DlmmCommandV2) and command.amount_x and command.amount_y
    assert (simulation.token_x_decimals, simulation.token_y_decimals) == (6, 9)
    position = repos.accounting.position_for(position_id)
    assert position is not None
    (tranche,) = position.tranches
    # X is booked in Y's scale (10^(9-6) per X base unit) at the entry price 1, plus Y.
    assert tranche.quantity_x == Decimal(command.amount_x) * 1_000
    assert position.open_cost_basis_base_units == (
        Decimal(command.amount_x) * 1_000 + Decimal(command.amount_y)
    )


def test_a_pool_that_moved_beyond_the_tolerance_fails_the_entry_without_a_submit() -> None:
    runtime, executor, clock = live_world()
    executor.active_bin_drift = ACTIVE_BIN_TOLERANCE + 1

    run_job(runtime, "candidate")

    repos = runtime.repositories
    (plan,) = repos.mode.entry_plans()
    assert plan.status is PendingPlanStatus.FAILED
    assert executor.submitted == [] and executor.created_positions == []
    assert repos.mode.committed_entry_count(clock.now()) == 0


def test_the_position_context_is_durable_before_the_operator_approves() -> None:
    first, executor, plan_id = approval_world()

    (created,) = executor.created_positions
    context = asyncio.run(first.repositories.positions.get(created))

    assert context is not None and context.plan_id == plan_id
    assert executor.submitted == []


def test_restart_recovery_confirms_a_v2_entry_by_its_receipt_without_an_operator_alert() -> None:
    runtime, executor, _ = live_world()
    executor.submit_behavior = "submitted"
    run_job(runtime, "candidate")
    plan_id = executing_plan(runtime.repositories)
    executor.submit_behavior = "confirm"

    assert isinstance(runtime.clock, FrozenClock)
    again = worker(runtime.repositories, runtime.clock, executor, TradingMode.LIVE)
    report = asyncio.run(again.recovery.reconcile(startup=True))

    repos = again.repositories
    (created,) = executor.created_positions
    assert {item.plan_id: item.action for item in report.entries} == {
        plan_id: RecoveryAction.CONFIRMED_ENTRY
    }
    assert report.operator_required == ()
    assert repos.mode.open_positions() == (created,)
    assert "EXECUTION_RECOVERY_REQUIRED" not in {
        alert.code for alert in repos.mode.alerts() if alert.open
    }
    assert len(executor.submitted) == 1


# --- review: settlement of reconciled entries, executor error codes, exits not gated ---------


def test_the_settler_confirms_a_v2_entry_reconciled_to_confirmed_before_it_ran() -> None:
    runtime, executor, _ = live_world()
    executor.submit_behavior = "submitted"
    run_job(runtime, "candidate")
    repos = runtime.repositories
    plan_id = executing_plan(repos)
    key = entry_idempotency_key(plan_id)
    executor.submit_behavior = "confirm"
    # What the periodic recovery job's network pass does while the plan is KEPT_EXECUTING.
    record = asyncio.run(repos.execution.get_by_idempotency_key(key))
    assert record is not None and runtime.execution is not None
    asyncio.run(runtime.execution.reconcile(record.command.command_id))
    assert record_phase(repos, key) is ExecutionPhase.CONFIRMED

    run_job(runtime, "candidate")

    (created,) = executor.created_positions
    assert plan_status(repos, plan_id) is PendingPlanStatus.OPENED
    assert repos.mode.open_positions() == (created,)
    assert repos.accounting.position_for(created) is not None


def test_the_executor_code_of_a_refused_entry_is_the_entry_reason() -> None:
    clock = FrozenClock(NOW)
    repos = memory_repositories(clock)
    executor = FakeExecutor(clock)
    executor.simulation_failures[DlmmOperation.OPEN] = "INSUFFICIENT_FUNDS"
    promote(repos, TradingMode.LIVE)
    ingest(repos, *spike_history())
    runtime = worker(repos, clock, executor, TradingMode.LIVE)

    async def evaluate() -> Any:
        runtime.keeper.ensure_open()
        return await runtime.pipeline.evaluate(POOL.address.root, NOW)

    outcome = asyncio.run(evaluate())

    assert outcome.entry is not None and outcome.entry.reason == "INSUFFICIENT_FUNDS"
    assert plan_status(repos, outcome.plan_id) is PendingPlanStatus.FAILED
    assert executor.submitted == []


def test_the_executor_code_of_a_refused_exit_reaches_the_operator_alert() -> None:
    runtime, executor, _ = live_world()
    position_id = open_live_position(runtime)
    manager = runtime.gateway_positions
    assert manager is not None
    executor.simulation_failures[DlmmOperation.CLOSE] = "TRANSACTION_SIMULATION_FAILED"

    (result,) = asyncio.run(manager.apply({position_id: PositionActionKind.EXIT}))

    assert (result.status, result.detail) == (
        PositionActionStatus.FAILED,
        "TRANSACTION_SIMULATION_FAILED",
    )
    (alert,) = [a for a in runtime.repositories.mode.alerts() if a.code == "POSITION_ACTION_FAILED"]
    assert "TRANSACTION_SIMULATION_FAILED" in alert.message


def test_an_exit_from_a_thin_pool_leaves_and_its_quoted_impact_is_recorded() -> None:
    runtime, executor, clock = live_world()
    position_id = open_live_position(runtime)
    # Far above the entry limit: the exit swaps nothing, so it is not stopped.
    executor.price_impacts[DlmmOperation.CLOSE] = 9_000
    decay(runtime, clock)

    run_job(runtime, "position")

    repos = runtime.repositories
    assert executor.operations() == [DlmmOperation.OPEN, DlmmOperation.CLOSE]
    assert repos.mode.open_positions() == ()
    close = asyncio.run(repos.execution.get(position_action_command_id(position_id, 1)))
    assert close is not None and close.simulation.price_impact_bps == 9_000
    confirmed = [
        event.payload
        for event in repos.accounting.outbox_events()
        if event.event_type is EventType.COMMAND_CONFIRMED
        and event.payload.get("operation") == DlmmOperation.CLOSE.value
    ]
    assert [payload["price_impact_bps"] for payload in confirmed] == [9_000]


def test_an_exit_whose_swap_cannot_be_quoted_still_leaves() -> None:
    runtime, executor, clock = live_world()
    open_live_position(runtime)
    executor.price_impacts[DlmmOperation.CLOSE] = None
    decay(runtime, clock)

    run_job(runtime, "position")

    assert executor.operations() == [DlmmOperation.OPEN, DlmmOperation.CLOSE]
    assert runtime.repositories.mode.open_positions() == ()


def test_an_entry_is_still_refused_above_the_price_impact_limit() -> None:
    runtime, executor, _ = live_world()
    executor.price_impacts[DlmmOperation.OPEN] = 9_000

    async def evaluate() -> Any:
        runtime.keeper.ensure_open()
        return await runtime.pipeline.evaluate(POOL.address.root, NOW)

    outcome = asyncio.run(evaluate())

    assert outcome.entry is not None and outcome.entry.reason == "SLIPPAGE_BREACH"
    assert executor.submitted == []
