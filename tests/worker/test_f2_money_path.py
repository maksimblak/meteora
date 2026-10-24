"""F2: the money path makes no second entry into a busy pool, spends exit attempts only on
real tries, applies the kill-switch policy the operator chose, closes the alerts the
worker cleared itself and proves every fail-closed branch.

Same in-process executor (contract V2) and durable in-memory stores as
``test_gateway_execution``; a "restart" is a new ``WorkerRuntime`` over the same stores.
"""

import asyncio
import dataclasses
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest
from prometheus_client import CollectorRegistry

from src.core.clock import FrozenClock
from src.domain.models.trading import TradingMode
from src.domain.strategy import PositionActionKind
from src.execution.contracts import DlmmCommand, DlmmCommandV2, DlmmOperation, ExecutionPhase
from src.execution.http_gateway import HttpExecutionGateway
from src.execution.service import ExecutionRecord
from src.modes import KillSwitchPolicy, ModeService, PendingPlanStatus
from src.observability.metrics import WorkerMetrics
from src.persistence.factory import Repositories
from src.providers.fixtures import FixtureOnChainProvider
from src.worker.execution import PositionActionStatus, entry_idempotency_key
from src.worker.runtime import WorkerRuntime, WorkerSettings
from tests.worker.harness import (
    NOW,
    POOL,
    Harness,
    harness_policy,
    memory_repositories,
    safe_token,
    snapshot,
    spike_history,
)
from tests.worker.test_gateway_execution import (
    FakeExecutor,
    approval_world,
    approve,
    decay,
    executing_plan,
    ingest,
    live_world,
    open_live_position,
    plan_status,
    promote,
    record_phase,
    restart,
    run_job,
    worker,
)

ORDERLY_RULES = dataclasses.replace(
    harness_policy(), kill_switch_policy=KillSwitchPolicy.ORDERLY_EXIT
)


def open_alerts(repos: Repositories) -> list[str]:
    return [alert.code for alert in repos.mode.alerts() if alert.open]


def simulated(executor: FakeExecutor, operation: DlmmOperation) -> list[DlmmCommand]:
    return [command for command, _ in executor.simulated if command.operation is operation]


def activate(runtime: WorkerRuntime, policy: KillSwitchPolicy) -> None:
    ModeService(runtime.repositories.mode, runtime.clock, "rules-1").activate_kill_switch(
        "operator stop", policy
    )


def evaluate_pool(runtime: WorkerRuntime) -> Any:
    async def run() -> Any:
        runtime.keeper.ensure_open()
        return await runtime.pipeline.evaluate(POOL.address.root, runtime.clock.now())

    return asyncio.run(run())


def live_runtime(executor: FakeExecutor, clock: FrozenClock) -> WorkerRuntime:
    repos = memory_repositories(clock)
    promote(repos, TradingMode.LIVE)
    ingest(repos, *spike_history())
    return worker(repos, clock, executor, TradingMode.LIVE)


class ForgetfulExecutor(FakeExecutor):
    """Its command lookup never finds a submission: an unknown outcome stays unknown."""

    async def lookup(self, command_id: UUID) -> Any:
        return None


class LateLookupExecutor(FakeExecutor):
    """The command lookup answers only once ``lookups`` is switched on."""

    lookups = False

    async def lookup(self, command_id: UUID) -> Any:
        return await super().lookup(command_id) if self.lookups else None


# --- B1: a pool with an entry in flight gets no second plan ---------------------------------


def test_a_pool_whose_entry_awaits_approval_gets_no_second_plan_or_simulation() -> None:
    first, executor, plan_id = approval_world()
    first.clock.advance(timedelta(seconds=30))

    run_job(first, "candidate")

    plans = first.repositories.mode.entry_plans()
    assert [(plan.plan_id, plan.status) for plan in plans] == [
        (plan_id, PendingPlanStatus.EXECUTING)
    ]
    assert len(simulated(executor, DlmmOperation.OPEN)) == 1
    assert first.repositories.mode.committed_entry_count(first.clock.now()) == 1


def test_an_entry_with_an_unknown_outcome_blocks_a_second_entry_into_its_pool() -> None:
    clock = FrozenClock(NOW)
    executor = ForgetfulExecutor(clock)
    runtime = live_runtime(executor, clock)
    executor.submit_behavior = "timeout"
    run_job(runtime, "candidate")
    plan_id = executing_plan(runtime.repositories)

    clock.advance(timedelta(seconds=30))
    executor.submit_behavior = "confirm"
    run_job(runtime, "candidate")

    assert len(executor.submitted) == 1
    assert [(plan.plan_id, plan.status) for plan in runtime.repositories.mode.entry_plans()] == [
        (plan_id, PendingPlanStatus.EXECUTING)
    ]


# --- B2: an exit nobody approved spends no attempt ----------------------------------------------


def opened_approval_position() -> tuple[WorkerRuntime, FakeExecutor, str]:
    first, executor, plan_id = approval_world()
    entry = asyncio.run(
        first.repositories.execution.get_by_idempotency_key(entry_idempotency_key(plan_id))
    )
    assert entry is not None
    approve(first, entry.command.command_id, timedelta(seconds=60))
    run_job(first, "candidate")
    (position_id,) = executor.created_positions
    assert first.repositories.mode.open_positions() == (position_id,)
    return first, executor, position_id


def live_approval_request(repos: Repositories, now: datetime) -> ExecutionRecord:
    async def pending() -> tuple[ExecutionRecord, ...]:
        return await repos.execution.in_phases((ExecutionPhase.AWAITING_APPROVAL,))  # type: ignore[attr-defined,no-any-return]

    (record,) = [item for item in asyncio.run(pending()) if item.command.expires_at > now]
    return record


def test_exits_that_expired_without_an_approval_spend_no_attempt_even_across_a_restart() -> None:
    runtime, executor, position_id = opened_approval_position()
    clock = runtime.clock
    assert isinstance(clock, FrozenClock)
    statuses = []
    for cycle in range(6):
        if cycle == 3:
            runtime = restart(runtime, executor, TradingMode.APPROVAL)
        manager = runtime.gateway_positions
        assert manager is not None
        (result,) = asyncio.run(manager.apply({position_id: PositionActionKind.EXIT}))
        statuses.append(result.status)
        clock.advance(timedelta(minutes=3))  # past the 2-minute exit TTL, nobody approved

    assert statuses == [PositionActionStatus.AWAITING_APPROVAL] * 6
    manager = runtime.gateway_positions
    assert manager is not None
    (seventh,) = asyncio.run(manager.apply({position_id: PositionActionKind.EXIT}))
    assert seventh.status is PositionActionStatus.AWAITING_APPROVAL
    request = live_approval_request(runtime.repositories, clock.now())
    approve(runtime, request.command.command_id, timedelta(seconds=60))
    (done,) = asyncio.run(manager.apply({position_id: PositionActionKind.EXIT}))

    assert done.status is PositionActionStatus.EXITED
    assert executor.operations() == [DlmmOperation.OPEN, DlmmOperation.CLOSE]
    assert runtime.repositories.mode.open_positions() == ()


def test_failed_exit_attempts_still_stop_at_five() -> None:
    runtime, executor, _ = live_world()
    position_id = open_live_position(runtime)
    manager = runtime.gateway_positions
    assert manager is not None
    executor.simulation_failures[DlmmOperation.CLOSE] = "TRANSACTION_SIMULATION_FAILED"

    results = [
        asyncio.run(manager.apply({position_id: PositionActionKind.EXIT}))[0].status
        for _ in range(6)
    ]

    assert results == [PositionActionStatus.FAILED] * 5 + [PositionActionStatus.EXHAUSTED]
    assert len(simulated(executor, DlmmOperation.CLOSE)) == 5
    assert "POSITION_ACTION_ATTEMPTS_EXHAUSTED" in open_alerts(runtime.repositories)


# --- B3: the policy chosen at activation is what the worker applies ------------------------


def test_an_orderly_exit_chosen_at_activation_closes_a_position_with_no_other_reason() -> None:
    runtime, executor, clock = live_world()  # the rules keep the default HOLD_AND_ALERT
    position_id = open_live_position(runtime)
    clock.advance(timedelta(seconds=30))
    activate(runtime, KillSwitchPolicy.ORDERLY_EXIT)

    run_job(runtime, "position")

    assert executor.operations() == [DlmmOperation.OPEN, DlmmOperation.CLOSE]
    close_command, close_simulation = executor.simulated[-1]
    assert close_command.operation is DlmmOperation.CLOSE
    assert close_command.position_address == position_id
    assert close_simulation.simulated_at == clock.now()
    assert runtime.repositories.mode.open_positions() == ()


def test_hold_and_alert_chosen_at_activation_holds_even_when_the_rules_say_orderly() -> None:
    runtime, executor, clock = live_world(policy=ORDERLY_RULES)
    position_id = open_live_position(runtime)
    activate(runtime, KillSwitchPolicy.HOLD_AND_ALERT)
    decay(runtime, clock)

    run_job(runtime, "position")

    assert executor.operations() == [DlmmOperation.OPEN]
    assert runtime.repositories.mode.open_positions() == (position_id,)


def test_an_orderly_exit_closes_a_shadow_position_with_no_other_reason() -> None:
    harness = Harness()
    harness.ingest(spike_history())

    async def enter() -> Any:
        harness.keeper.ensure_open()
        return await harness.pipeline.evaluate(POOL.address.root, NOW)

    outcome = asyncio.run(enter())
    assert outcome.entry is not None and outcome.entry.position_id is not None
    ModeService(harness.modes, harness.clock, "rules-1").activate_kill_switch(
        "operator stop", KillSwitchPolicy.ORDERLY_EXIT
    )
    harness.clock.advance(timedelta(minutes=1))
    harness.ingest([snapshot(harness.clock.now(), short="400", long="1200")])

    report = asyncio.run(harness.positions.run_all())

    (review,) = report.reviews
    assert review.action is PositionActionKind.EXIT and review.closed is True
    assert "KILL_SWITCH_ACTIVE" in review.reasons
    assert harness.modes.open_positions() == ()


# --- B4: an exit through a connected gateway raises no gateway alert ------------------------


def test_an_exit_through_the_gateway_leaves_no_open_alert() -> None:
    runtime, executor, clock = live_world()
    open_live_position(runtime)
    decay(runtime, clock)

    run_job(runtime, "position")

    assert executor.operations() == [DlmmOperation.OPEN, DlmmOperation.CLOSE]
    assert open_alerts(runtime.repositories) == []


def test_a_position_without_a_gateway_for_its_mode_still_alerts() -> None:
    runtime, executor, clock = live_world()
    position_id = open_live_position(runtime)
    decay(runtime, clock)
    shadow_only = WorkerRuntime(
        settings=WorkerSettings(metrics_port=None, owner="worker-without-gateway"),
        policy=harness_policy(),
        repositories=runtime.repositories,
        market=None,
        onchain=FixtureOnChainProvider(
            token_risks={POOL.base_mint.root: safe_token()}, clock=clock
        ),
        clock=clock,
        metrics=WorkerMetrics(CollectorRegistry()),
    )

    run_job(shadow_only, "position")

    assert executor.operations() == [DlmmOperation.OPEN]
    assert runtime.repositories.mode.open_positions() == (position_id,)
    assert "EXIT_REQUIRES_EXECUTION_GATEWAY" in open_alerts(runtime.repositories)


# --- B5: network first, and alerts the worker cleared itself are closed --------------------


def test_a_submit_timeout_confirmed_later_leaves_no_open_alert() -> None:
    runtime, executor, _ = live_world()
    executor.submit_behavior = "timeout"
    run_job(runtime, "candidate")
    plan_id = executing_plan(runtime.repositories)

    asyncio.run(runtime.recovery.reconcile())  # the periodic job, before the next cycle
    executor.submit_behavior = "confirm"
    run_job(runtime, "candidate")

    assert plan_status(runtime.repositories, plan_id) is PendingPlanStatus.OPENED
    assert open_alerts(runtime.repositories) == []
    assert len(executor.submitted) == 1


def test_a_recovery_alert_is_closed_by_the_system_once_the_outcome_is_known() -> None:
    clock = FrozenClock(NOW)
    executor = LateLookupExecutor(clock)
    runtime = live_runtime(executor, clock)
    executor.submit_behavior = "timeout"
    run_job(runtime, "candidate")
    plan_id = executing_plan(runtime.repositories)
    asyncio.run(runtime.recovery.reconcile())
    assert open_alerts(runtime.repositories) == ["EXECUTION_RECOVERY_REQUIRED"]

    executor.lookups = True
    run_job(runtime, "candidate")
    asyncio.run(runtime.recovery.reconcile())

    assert plan_status(runtime.repositories, plan_id) is PendingPlanStatus.OPENED
    assert open_alerts(runtime.repositories) == []
    (alert,) = runtime.repositories.mode.alerts()
    assert alert.resolution.startswith("system:")
    assert len(executor.submitted) == 1


def test_exit_failure_alerts_are_closed_by_the_system_after_a_successful_exit() -> None:
    runtime, executor, clock = live_world()
    open_live_position(runtime)
    decay(runtime, clock)
    executor.reachable = False
    run_job(runtime, "position")
    executor.reachable = True
    executor.simulation_failures[DlmmOperation.CLOSE] = "TRANSACTION_SIMULATION_FAILED"
    decay(runtime, clock)
    run_job(runtime, "position")
    assert {"EXIT_GATEWAY_UNAVAILABLE", "POSITION_ACTION_FAILED"} <= set(
        open_alerts(runtime.repositories)
    )

    del executor.simulation_failures[DlmmOperation.CLOSE]
    decay(runtime, clock)
    run_job(runtime, "position")

    repos = runtime.repositories
    assert repos.mode.open_positions() == ()
    assert open_alerts(repos) == []
    closed = {alert.code: alert.resolution for alert in repos.mode.alerts()}
    assert closed.keys() == {"EXIT_GATEWAY_UNAVAILABLE", "POSITION_ACTION_FAILED"}
    assert all(resolution.startswith("system:") for resolution in closed.values())


class MismatchedSimulation(FakeExecutor):
    """Answers CLOSE simulations that do not match the command: prepare refuses them
    before anything is written (``ExecutionBlocked``, no execution record)."""

    async def simulate(self, command: DlmmCommand) -> Any:
        result = await super().simulate(command)
        if command.operation is DlmmOperation.CLOSE:
            return result.model_copy(update={"min_bin_id": result.min_bin_id - 1})
        return result


@pytest.mark.parametrize(
    ("failure", "code"),
    [("outage", "EXIT_GATEWAY_UNAVAILABLE"), ("refused", "POSITION_ACTION_FAILED")],
)
def test_an_exit_failure_after_a_confirmed_reduction_stays_open(failure: str, code: str) -> None:
    """The system closes alerts once, when an attempt turns CONFIRMED, and only those
    raised before that success - never on the next cycles that re-read it."""

    clock = FrozenClock(NOW)
    executor = MismatchedSimulation(clock) if failure == "refused" else FakeExecutor(clock)
    runtime = live_runtime(executor, clock)
    position_id = open_live_position(runtime)
    manager = runtime.gateway_positions
    assert manager is not None
    (reduced,) = asyncio.run(manager.apply({position_id: PositionActionKind.REDUCE}))
    assert reduced.status is PositionActionStatus.REDUCED
    executor.reachable = failure != "outage"

    for _ in range(2):
        (result,) = asyncio.run(manager.apply({position_id: PositionActionKind.EXIT}))
        assert result.status is PositionActionStatus.FAILED
        clock.advance(timedelta(seconds=30))

    assert runtime.repositories.mode.open_positions() == (position_id,)
    assert open_alerts(runtime.repositories) == [code]


# --- B6: an executor conflict means "already submitted", never "nothing sent" --------------


def http_executor(fake: FakeExecutor, conflict: str) -> HttpExecutionGateway:
    """The real HTTP adapter over an executor that simulates like ``fake`` and answers
    every ``POST /v1/commands`` with HTTP 409 ``conflict`` (engine.ts 543, 677, 778)."""

    async def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/simulations":
            command = DlmmCommandV2.model_validate_json(request.content)
            simulation = await fake.simulate(command)
            return httpx.Response(200, json=simulation.model_dump(mode="json"))
        if (request.method, request.url.path) == ("POST", "/v1/commands"):
            return httpx.Response(409, json={"error": conflict})
        return httpx.Response(404, json={"error": "command_not_found"})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(answer), base_url="http://executor.internal"
    )
    return HttpExecutionGateway(client, internal_token="internal-test-value")  # noqa: S106


@pytest.mark.parametrize("conflict", ["idempotency_conflict", "approval_already_used"])
def test_an_executor_conflict_keeps_the_entry_executing_and_its_capacity(conflict: str) -> None:
    clock = FrozenClock(NOW)
    repos = memory_repositories(clock)
    promote(repos, TradingMode.LIVE)
    ingest(repos, *spike_history())
    gateway = http_executor(FakeExecutor(clock), conflict)
    runtime = worker(repos, clock, gateway, TradingMode.LIVE)  # type: ignore[arg-type]

    run_job(runtime, "candidate")

    plan_id = executing_plan(repos)
    assert record_phase(repos, entry_idempotency_key(plan_id)) is ExecutionPhase.UNKNOWN
    assert repos.mode.committed_entry_count(clock.now()) == 1


# --- B8: every fail-closed branch of the money path is exercised ----------------------------


def test_an_entry_without_a_measured_price_impact_is_refused_before_any_submit() -> None:
    runtime, executor, clock = live_world()
    executor.price_impacts[DlmmOperation.OPEN] = None

    outcome = evaluate_pool(runtime)

    assert outcome.entry is not None and outcome.entry.reason == "PRICE_IMPACT_UNAVAILABLE"
    assert plan_status(runtime.repositories, outcome.plan_id) is PendingPlanStatus.FAILED
    assert executor.submitted == []
    assert runtime.repositories.mode.committed_entry_count(clock.now()) == 0


class PlanSettledDuring(FakeExecutor):
    """Something else (a concurrent recovery) fails the EXECUTING plan while the executor
    simulates (``stage="simulate"``) or right after it broadcast (``stage="submit"``)."""

    def __init__(self, clock: FrozenClock, stage: str) -> None:
        super().__init__(clock)
        self.stage = stage
        self.repos: Repositories | None = None

    def _settle_elsewhere(self) -> None:
        assert self.repos is not None
        for plan in self.repos.mode.entry_plans():
            if plan.status is PendingPlanStatus.EXECUTING:
                self.repos.mode.fail_entry(plan.plan_id)

    async def simulate(self, command: DlmmCommand) -> Any:
        result = await super().simulate(command)
        if self.stage == "simulate" and command.operation is DlmmOperation.OPEN:
            self._settle_elsewhere()
        return result

    async def submit(self, command: DlmmCommand, idempotency_key: str, approval: Any) -> Any:
        receipt = await super().submit(command, idempotency_key, approval)
        if self.stage == "submit":
            self._settle_elsewhere()
        return receipt


def racing_runtime(stage: str) -> tuple[WorkerRuntime, PlanSettledDuring]:
    clock = FrozenClock(NOW)
    executor = PlanSettledDuring(clock, stage)
    runtime = live_runtime(executor, clock)
    executor.repos = runtime.repositories
    return runtime, executor


def test_an_entry_whose_plan_stopped_executing_is_never_submitted() -> None:
    runtime, executor = racing_runtime("simulate")

    outcome = evaluate_pool(runtime)

    assert outcome.entry is not None and outcome.entry.reason == "PLAN_NOT_EXECUTING"
    assert executor.submitted == []
    assert plan_status(runtime.repositories, outcome.plan_id) is PendingPlanStatus.FAILED
    assert runtime.repositories.mode.open_positions() == ()


def test_a_fill_the_bookkeeping_refuses_raises_an_untracked_fill_alert() -> None:
    runtime, executor = racing_runtime("submit")

    outcome = evaluate_pool(runtime)

    (created,) = executor.created_positions
    assert outcome.entry is not None and outcome.entry.reason == "ENTRY_FILL_UNTRACKED"
    assert outcome.entry.position_id == created
    assert len(executor.submitted) == 1
    (alert,) = [a for a in runtime.repositories.mode.alerts() if a.open]
    assert alert.code == "ENTRY_FILL_UNTRACKED" and str(outcome.plan_id) in alert.message


def test_a_confirmed_entry_without_a_known_position_keeps_its_plan_and_alerts() -> None:
    runtime, executor, clock = live_world()
    executor.submit_behavior = "submitted"
    run_job(runtime, "candidate")
    repos = runtime.repositories
    plan_id = executing_plan(repos)
    key = entry_idempotency_key(plan_id)
    record = asyncio.run(repos.execution.get_by_idempotency_key(key))
    assert record is not None and record.receipt is not None
    # A durable record that says CONFIRMED but names no position (a V1-shaped receipt).
    record.receipt = record.receipt.lifecycle()  # type: ignore[union-attr]
    record.phase = ExecutionPhase.CONFIRMED
    asyncio.run(repos.execution.put(record))

    run_job(runtime, "candidate")

    assert plan_status(repos, plan_id) is PendingPlanStatus.EXECUTING
    assert repos.mode.committed_entry_count(clock.now()) == 1
    assert "ENTRY_POSITION_UNKNOWN" in open_alerts(repos)
    assert len(executor.submitted) == 1
