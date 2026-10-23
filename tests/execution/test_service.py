from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

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
    LivePromotionEvidence,
    OperatorApproval,
    SimulationErrorV1,
    SimulationResultV1,
    bind_simulation_result,
    canonical_command_digest,
)
from src.execution.service import (
    ExecutionBlocked,
    ExecutionOutcomeUnknown,
    ExecutionRecord,
    ExecutionService,
    HmacApprovalVerifier,
    InMemoryExecutionStore,
    SqliteExecutionStore,
    StaticPlanResolver,
    sign_operator_approval,
)
from src.modes.models import KillSwitchPolicy

NOW = datetime(2026, 10, 18, 12, tzinfo=UTC)
PLAN_ID = UUID("30000000-0000-4000-8000-000000000001")
SECOND_PLAN_ID = UUID("30000000-0000-4000-8000-000000000002")
COMMAND_ID = UUID("10000000-0000-4000-8000-000000000001")
SECOND_COMMAND_ID = UUID("10000000-0000-4000-8000-000000000002")
APPROVAL_SECRET = b"0123456789abcdef0123456789abcdef"
APPROVAL_ID = UUID("40000000-0000-4000-8000-000000000001")


def live_promotion() -> LivePromotionEvidence:
    return LivePromotionEvidence(
        promotion_id=UUID("50000000-0000-4000-8000-000000000001"),
        ruleset_version="rules-2026-10-18",
        approval_cycles=10,
        receipts_reconciled=True,
        risk_invariants_preserved=True,
        dedicated_rpc_ready=True,
        wallet_ready=True,
        checklist_completed=True,
        operator_confirmation_id="operator-confirmation-1",
        promoted_at=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(days=1),
    )


def signed_approval(
    simulation: SimulationResultV1,
    *,
    expires_at: datetime,
    approval_id: UUID = APPROVAL_ID,
    proof: str | None = None,
) -> OperatorApproval:
    operator_id = "local-operator"
    approved_at = simulation.simulated_at
    signature = sign_operator_approval(
        APPROVAL_SECRET,
        approval_id=approval_id,
        approved_at=approved_at,
        expires_at=expires_at,
        operator_id=operator_id,
        command_digest=simulation.command_digest,
        simulation_digest=simulation.simulation_digest,
    )
    return OperatorApproval(
        approval_id=approval_id,
        approved_at=approved_at,
        expires_at=expires_at,
        operator_id=operator_id,
        command_digest=simulation.command_digest,
        simulation_digest=simulation.simulation_digest,
        proof=signature if proof is None else proof,
        comment="checked preview",
    )


def command(
    mode: TradingMode = TradingMode.APPROVAL,
    *,
    command_id: UUID = COMMAND_ID,
    idempotency_key: str = "plan-101-open-v1",
) -> DlmmCommandV1:
    return DlmmCommandV1.model_validate(
        {
            "schema_version": 1,
            "command_id": str(command_id),
            "idempotency_key": idempotency_key,
            "correlation_id": "20000000-0000-4000-8000-000000000001",
            "ruleset_version": "rules-2026-10-18",
            "mode": mode.value,
            "operation": "OPEN",
            "pool_address": "11111111111111111111111111111111",
            "wallet_address": "11111111111111111111111111111111",
            "position_address": "11111111111111111111111111111111",
            "expires_at": NOW + timedelta(minutes=10),
            "max_slippage_bps": 50,
            "strategy": "SPOT",
            "min_bin_id": -4,
            "max_bin_id": 7,
            "amount_x": "1000",
            "amount_y": "2000",
        }
    )


class KillSwitch:
    def __init__(self, active: bool = False) -> None:
        self.active = active
        self.reads = 0

    async def kill_switch_active(self) -> bool:
        self.reads += 1
        return self.active


CLEAR = KillSwitch()


class RecordingGateway:
    def __init__(self) -> None:
        self.simulate_calls = 0
        self.submit_calls = 0
        self.status_calls = 0
        self.chain_state = ChainState.CONFIRMED
        self.simulated_at = NOW
        self.last_simulation: SimulationResultV1 | None = None

    async def simulate(self, value: DlmmCommandV1) -> SimulationResultV1:
        self.simulate_calls += 1
        self.last_simulation = bind_simulation_result(
            command_id=value.command_id,
            command_digest=canonical_command_digest(value),
            success=True,
            simulated_at=self.simulated_at,
            blockhash="safe-blockhash",
            last_valid_block_height=123,
            compute_units=900,
            network_fee_lamports="5000",
            priority_fee_lamports="0",
            price_impact_bps=10,
            min_bin_id=value.min_bin_id,
            max_bin_id=value.max_bin_id,
            errors=(),
        )
        return self.last_simulation

    async def submit(
        self,
        value: DlmmCommandV1,
        idempotency_key: str,
        approval: OperatorApproval | None,
    ) -> GatewayReceiptV1:
        del approval
        self.submit_calls += 1
        assert self.last_simulation is not None
        return GatewayReceiptV1(
            command_id=value.command_id,
            command_digest=self.last_simulation.command_digest,
            simulation_digest=self.last_simulation.simulation_digest,
            idempotency_key=idempotency_key,
            state=GatewayState.UNKNOWN,
            signature="signature-1",
            signatures=("signature-1",),
            transaction_count=1,
            submitted_at=NOW,
            attempt_count=1,
        )

    async def status(self, signature: str) -> ChainReceiptV1:
        self.status_calls += 1
        return ChainReceiptV1(
            signature=signature,
            state=self.chain_state,
            checked_at=NOW,
            slot=42,
            error=None,
        )


class UncertainSubmitGateway(RecordingGateway):
    async def submit(
        self,
        value: DlmmCommandV1,
        idempotency_key: str,
        approval: OperatorApproval | None,
    ) -> GatewayReceiptV1:
        del value, idempotency_key, approval
        self.submit_calls += 1
        raise TimeoutError("transport failed after broadcast may have started")


class MultiTransactionGateway(RecordingGateway):
    async def submit(
        self,
        value: DlmmCommandV1,
        idempotency_key: str,
        approval: OperatorApproval | None,
    ) -> GatewayReceiptV1:
        del approval
        self.submit_calls += 1
        assert self.last_simulation is not None
        return GatewayReceiptV1(
            command_id=value.command_id,
            command_digest=self.last_simulation.command_digest,
            simulation_digest=self.last_simulation.simulation_digest,
            idempotency_key=idempotency_key,
            state=GatewayState.SUBMITTED,
            signature="signature-2",
            signatures=("signature-1", "signature-2"),
            transaction_count=2,
            submitted_at=NOW,
            attempt_count=1,
        )

    async def status(self, signature: str) -> ChainReceiptV1:
        self.status_calls += 1
        return ChainReceiptV1(
            signature=signature,
            state=(
                ChainState.FAILED
                if signature == "signature-1"
                else ChainState.CONFIRMED
            ),
            checked_at=NOW,
            slot=42,
            error=None,
        )


class FailedSimulationGateway(RecordingGateway):
    async def simulate(self, value: DlmmCommandV1) -> SimulationResultV1:
        self.simulate_calls += 1
        self.last_simulation = bind_simulation_result(
            command_id=value.command_id,
            command_digest=canonical_command_digest(value),
            success=False,
            simulated_at=self.simulated_at,
            blockhash=None,
            last_valid_block_height=None,
            compute_units=0,
            network_fee_lamports="0",
            priority_fee_lamports="0",
            price_impact_bps=None,
            min_bin_id=value.min_bin_id,
            max_bin_id=value.max_bin_id,
            errors=(
                SimulationErrorV1(
                    code="SIMULATION_FAILED",
                    message="execution dependency rejected the command",
                    retryable=True,
                ),
            ),
        )
        return self.last_simulation


def test_operator_approval_rejects_invalid_dates_and_blank_operator() -> None:
    with pytest.raises(ValidationError):
        OperatorApproval.model_validate(
            {
                "approval_id": str(APPROVAL_ID),
                "approved_at": "not-a-date",
                "expires_at": "also-not-a-date",
                "operator_id": "   ",
                "command_digest": "a" * 64,
                "simulation_digest": "b" * 64,
                "proof": "c" * 64,
            }
        )


def test_shadow_mode_physically_never_calls_submit() -> None:
    asyncio.run(_assert_shadow_mode_physically_never_calls_submit())


async def _assert_shadow_mode_physically_never_calls_submit() -> None:
    gateway = RecordingGateway()
    value = command(TradingMode.SHADOW)
    service = ExecutionService(
        gateway=gateway,
        store=InMemoryExecutionStore(),
        plans=StaticPlanResolver({PLAN_ID: value}),
        clock=FrozenClock(NOW),
        allowed_mode=TradingMode.SHADOW,
        kill_switch=CLEAR,
    )

    await service.prepare(PLAN_ID)
    with pytest.raises(ExecutionBlocked, match="SHADOW"):
        await service.execute(COMMAND_ID)

    assert gateway.submit_calls == 0


def test_simulation_failure_reaches_a_terminal_phase_without_submit() -> None:
    asyncio.run(_assert_simulation_failure_reaches_a_terminal_phase_without_submit())


async def _assert_simulation_failure_reaches_a_terminal_phase_without_submit() -> None:
    gateway = FailedSimulationGateway()
    store = InMemoryExecutionStore()
    service = ExecutionService(
        gateway=gateway,
        store=store,
        plans=StaticPlanResolver({PLAN_ID: command(TradingMode.LIVE)}),
        clock=FrozenClock(NOW),
        allowed_mode=TradingMode.LIVE,
        kill_switch=CLEAR,
    )

    with pytest.raises(ExecutionBlocked, match="simulation failed"):
        await service.prepare(PLAN_ID)

    record = await store.get(COMMAND_ID)
    assert record is not None
    assert record.phase is ExecutionPhase.FAILED
    assert gateway.submit_calls == 0


def test_approval_ttl_and_fresh_simulation_are_required() -> None:
    asyncio.run(_assert_approval_ttl_and_fresh_simulation_are_required())


async def _assert_approval_ttl_and_fresh_simulation_are_required() -> None:
    gateway = RecordingGateway()
    clock = FrozenClock(NOW)
    service = ExecutionService(
        gateway=gateway,
        store=InMemoryExecutionStore(),
        plans=StaticPlanResolver({PLAN_ID: command()}),
        clock=clock,
        allowed_mode=TradingMode.APPROVAL,
        kill_switch=CLEAR,
        simulation_ttl=timedelta(seconds=30),
        approval_verifier=HmacApprovalVerifier(APPROVAL_SECRET),
    )
    preview = await service.prepare(PLAN_ID)
    await service.approve(
        COMMAND_ID,
        signed_approval(
            preview.simulation,
            expires_at=NOW + timedelta(seconds=20),
        ),
    )
    clock.advance(timedelta(seconds=21))

    with pytest.raises(ExecutionBlocked, match="approval expired"):
        await service.execute(COMMAND_ID)

    assert gateway.submit_calls == 0


def test_stale_simulation_is_rejected_even_with_current_approval() -> None:
    asyncio.run(_assert_stale_simulation_is_rejected_even_with_current_approval())


async def _assert_stale_simulation_is_rejected_even_with_current_approval() -> None:
    gateway = RecordingGateway()
    clock = FrozenClock(NOW)
    service = ExecutionService(
        gateway=gateway,
        store=InMemoryExecutionStore(),
        plans=StaticPlanResolver({PLAN_ID: command()}),
        clock=clock,
        allowed_mode=TradingMode.APPROVAL,
        kill_switch=CLEAR,
        simulation_ttl=timedelta(seconds=30),
        approval_verifier=HmacApprovalVerifier(APPROVAL_SECRET),
    )
    preview = await service.prepare(PLAN_ID)
    await service.approve(
        COMMAND_ID,
        signed_approval(
            preview.simulation,
            expires_at=NOW + timedelta(seconds=30),
        ),
    )
    clock.advance(timedelta(seconds=31))

    with pytest.raises(ExecutionBlocked, match="simulation is stale"):
        await service.execute(COMMAND_ID)

    assert gateway.submit_calls == 0


def test_resimulation_invalidates_the_old_approval() -> None:
    asyncio.run(_assert_resimulation_invalidates_the_old_approval())


async def _assert_resimulation_invalidates_the_old_approval() -> None:
    gateway = RecordingGateway()
    clock = FrozenClock(NOW)
    service = ExecutionService(
        gateway=gateway,
        store=InMemoryExecutionStore(),
        plans=StaticPlanResolver({PLAN_ID: command()}),
        clock=clock,
        allowed_mode=TradingMode.APPROVAL,
        kill_switch=CLEAR,
        simulation_ttl=timedelta(seconds=30),
        approval_verifier=HmacApprovalVerifier(APPROVAL_SECRET),
    )
    preview = await service.prepare(PLAN_ID)
    await service.approve(
        COMMAND_ID,
        signed_approval(
            preview.simulation,
            expires_at=NOW + timedelta(seconds=30),
        ),
    )
    clock.advance(timedelta(seconds=31))
    gateway.simulated_at = clock.now()

    refreshed = await service.prepare(PLAN_ID)

    assert refreshed.phase is ExecutionPhase.AWAITING_APPROVAL
    assert gateway.simulate_calls == 2
    with pytest.raises(ExecutionBlocked, match="approval already used"):
        await service.approve(
            COMMAND_ID,
            signed_approval(
                refreshed.simulation,
                expires_at=clock.now() + timedelta(seconds=20),
            ),
        )
    with pytest.raises(ExecutionBlocked, match="approval required"):
        await service.execute(COMMAND_ID)
    assert gateway.submit_calls == 0


def test_changed_command_cannot_reuse_a_prepared_idempotency_key() -> None:
    asyncio.run(_assert_changed_command_cannot_reuse_a_prepared_idempotency_key())


async def _assert_changed_command_cannot_reuse_a_prepared_idempotency_key() -> None:
    gateway = RecordingGateway()
    store = InMemoryExecutionStore()
    original = command(TradingMode.LIVE)
    first = ExecutionService(
        gateway=gateway,
        store=store,
        plans=StaticPlanResolver({PLAN_ID: original}),
        clock=FrozenClock(NOW),
        allowed_mode=TradingMode.LIVE,
        kill_switch=CLEAR,
        live_promotion=live_promotion(),
    )
    await first.prepare(PLAN_ID)
    changed = original.model_copy(update={"amount_x": "1001"})
    second = ExecutionService(
        gateway=gateway,
        store=store,
        plans=StaticPlanResolver({PLAN_ID: changed}),
        clock=FrozenClock(NOW),
        allowed_mode=TradingMode.LIVE,
        kill_switch=CLEAR,
        live_promotion=live_promotion(),
    )

    with pytest.raises(ExecutionBlocked, match="another command payload"):
        await second.prepare(PLAN_ID)

    assert gateway.simulate_calls == 1


def test_changed_command_cannot_reuse_a_command_id_with_a_new_key() -> None:
    asyncio.run(_assert_changed_command_cannot_reuse_a_command_id_with_a_new_key())


async def _assert_changed_command_cannot_reuse_a_command_id_with_a_new_key() -> None:
    gateway = RecordingGateway()
    store = InMemoryExecutionStore()
    original = command(TradingMode.LIVE)
    first = ExecutionService(
        gateway=gateway,
        store=store,
        plans=StaticPlanResolver({PLAN_ID: original}),
        clock=FrozenClock(NOW),
        allowed_mode=TradingMode.LIVE,
        kill_switch=CLEAR,
        live_promotion=live_promotion(),
    )
    await first.prepare(PLAN_ID)
    changed = original.model_copy(
        update={"idempotency_key": "plan-101-changed-v2", "amount_x": "1001"}
    )
    second = ExecutionService(
        gateway=gateway,
        store=store,
        plans=StaticPlanResolver({PLAN_ID: changed}),
        clock=FrozenClock(NOW),
        allowed_mode=TradingMode.LIVE,
        kill_switch=CLEAR,
        live_promotion=live_promotion(),
    )

    with pytest.raises(ExecutionBlocked, match="command id belongs"):
        await second.prepare(PLAN_ID)

    assert gateway.simulate_calls == 1


def test_signed_approval_is_durable_and_one_shot_across_restart(
    tmp_path: Path,
) -> None:
    asyncio.run(_assert_signed_approval_is_durable_and_one_shot_across_restart(tmp_path))


async def _assert_signed_approval_is_durable_and_one_shot_across_restart(
    tmp_path: Path,
) -> None:
    gateway = RecordingGateway()
    database = tmp_path / "approval.sqlite3"
    first_store = SqliteExecutionStore(database)
    first = ExecutionService(
        gateway=gateway,
        store=first_store,
        plans=StaticPlanResolver({PLAN_ID: command()}),
        clock=FrozenClock(NOW),
        allowed_mode=TradingMode.APPROVAL,
        kill_switch=CLEAR,
        approval_verifier=HmacApprovalVerifier(APPROVAL_SECRET),
    )
    preview = await first.prepare(PLAN_ID)
    await first.approve(
        COMMAND_ID,
        signed_approval(
            preview.simulation,
            expires_at=NOW + timedelta(seconds=30),
        ),
    )
    first_store.close()

    restarted_store = SqliteExecutionStore(database)
    restarted = ExecutionService(
        gateway=gateway,
        store=restarted_store,
        plans=StaticPlanResolver({PLAN_ID: command()}),
        clock=FrozenClock(NOW),
        allowed_mode=TradingMode.APPROVAL,
        kill_switch=CLEAR,
        approval_verifier=HmacApprovalVerifier(APPROVAL_SECRET),
    )
    receipt = await restarted.execute(COMMAND_ID)

    second_value = command(
        command_id=SECOND_COMMAND_ID,
        idempotency_key="plan-102-open-v1",
    )
    second = ExecutionService(
        gateway=gateway,
        store=restarted_store,
        plans=StaticPlanResolver({SECOND_PLAN_ID: second_value}),
        clock=FrozenClock(NOW),
        allowed_mode=TradingMode.APPROVAL,
        kill_switch=CLEAR,
        approval_verifier=HmacApprovalVerifier(APPROVAL_SECRET),
    )
    second_preview = await second.prepare(SECOND_PLAN_ID)
    with pytest.raises(ExecutionBlocked, match="approval already used"):
        await second.approve(
            SECOND_COMMAND_ID,
            signed_approval(
                second_preview.simulation,
                expires_at=NOW + timedelta(seconds=30),
            ),
        )
    restarted_store.close()

    assert receipt.state is GatewayState.UNKNOWN
    assert gateway.submit_calls == 1


def test_live_promotion_requires_ten_cycles_and_completed_checklist() -> None:
    asyncio.run(_assert_live_promotion_requires_ten_cycles_and_completed_checklist())


async def _assert_live_promotion_requires_ten_cycles_and_completed_checklist() -> None:
    gateway = RecordingGateway()
    evidence = live_promotion().model_copy(
        update={"approval_cycles": 9, "checklist_completed": False}
    )
    service = ExecutionService(
        gateway=gateway,
        store=InMemoryExecutionStore(),
        plans=StaticPlanResolver({PLAN_ID: command(TradingMode.LIVE)}),
        clock=FrozenClock(NOW),
        allowed_mode=TradingMode.LIVE,
        kill_switch=CLEAR,
        live_promotion=evidence,
    )
    await service.prepare(PLAN_ID)

    with pytest.raises(ExecutionBlocked, match="LIVE promotion gate"):
        await service.execute(COMMAND_ID)

    assert gateway.submit_calls == 0


def test_live_promotion_rejects_blank_operator_confirmation() -> None:
    payload = live_promotion().model_dump(mode="python")
    payload["operator_confirmation_id"] = "   "
    with pytest.raises(ValidationError, match="operator_confirmation_id"):
        LivePromotionEvidence.model_validate(payload)

    asyncio.run(_assert_live_promotion_rechecks_blank_operator_confirmation())


async def _assert_live_promotion_rechecks_blank_operator_confirmation() -> None:
    gateway = RecordingGateway()
    unsafe_evidence = live_promotion().model_copy(
        update={"operator_confirmation_id": "   "}
    )
    service = ExecutionService(
        gateway=gateway,
        store=InMemoryExecutionStore(),
        plans=StaticPlanResolver({PLAN_ID: command(TradingMode.LIVE)}),
        clock=FrozenClock(NOW),
        allowed_mode=TradingMode.LIVE,
        kill_switch=CLEAR,
        live_promotion=unsafe_evidence,
    )
    await service.prepare(PLAN_ID)

    with pytest.raises(ExecutionBlocked, match="LIVE promotion gate"):
        await service.execute(COMMAND_ID)

    assert gateway.submit_calls == 0


def test_unknown_receipt_reconciles_without_resubmission() -> None:
    asyncio.run(_assert_unknown_receipt_reconciles_without_resubmission())


async def _assert_unknown_receipt_reconciles_without_resubmission() -> None:
    gateway = RecordingGateway()
    service = ExecutionService(
        gateway=gateway,
        store=InMemoryExecutionStore(),
        plans=StaticPlanResolver({PLAN_ID: command(TradingMode.LIVE)}),
        clock=FrozenClock(NOW),
        allowed_mode=TradingMode.LIVE,
        kill_switch=CLEAR,
        live_promotion=live_promotion(),
    )
    await service.prepare(PLAN_ID)

    first = await service.execute(COMMAND_ID)
    second = await service.execute(COMMAND_ID)

    assert first.state is GatewayState.UNKNOWN
    assert second.state is GatewayState.CONFIRMED
    assert gateway.submit_calls == 1
    assert gateway.status_calls == 1


def test_multi_transaction_receipt_fails_if_any_signature_failed() -> None:
    asyncio.run(_assert_multi_transaction_receipt_fails_if_any_signature_failed())


async def _assert_multi_transaction_receipt_fails_if_any_signature_failed() -> None:
    gateway = MultiTransactionGateway()
    service = ExecutionService(
        gateway=gateway,
        store=InMemoryExecutionStore(),
        plans=StaticPlanResolver({PLAN_ID: command(TradingMode.LIVE)}),
        clock=FrozenClock(NOW),
        allowed_mode=TradingMode.LIVE,
        kill_switch=CLEAR,
        live_promotion=live_promotion(),
    )
    await service.prepare(PLAN_ID)

    await service.execute(COMMAND_ID)
    reconciled = await service.execute(COMMAND_ID)

    assert reconciled.state is GatewayState.FAILED
    assert gateway.submit_calls == 1
    assert gateway.status_calls == 2


def test_uncertain_submit_is_terminal_until_operator_recovery() -> None:
    asyncio.run(_assert_uncertain_submit_is_terminal_until_operator_recovery())


async def _assert_uncertain_submit_is_terminal_until_operator_recovery() -> None:
    gateway = UncertainSubmitGateway()
    store = InMemoryExecutionStore()
    service = ExecutionService(
        gateway=gateway,
        store=store,
        plans=StaticPlanResolver({PLAN_ID: command(TradingMode.LIVE)}),
        clock=FrozenClock(NOW),
        allowed_mode=TradingMode.LIVE,
        kill_switch=CLEAR,
        live_promotion=live_promotion(),
    )
    await service.prepare(PLAN_ID)

    with pytest.raises(ExecutionOutcomeUnknown):
        await service.execute(COMMAND_ID)
    with pytest.raises(ExecutionOutcomeUnknown, match="outcome is unknown"):
        await service.execute(COMMAND_ID)

    record = await store.get(COMMAND_ID)
    assert record is not None
    assert record.phase is ExecutionPhase.UNKNOWN
    assert gateway.submit_calls == 1


def test_durable_receipt_reconciles_after_restart_without_rebroadcast(
    tmp_path: Path,
) -> None:
    asyncio.run(_assert_durable_receipt_reconciles_after_restart(tmp_path))


async def _assert_durable_receipt_reconciles_after_restart(tmp_path: Path) -> None:
    gateway = RecordingGateway()
    database = tmp_path / "execution.sqlite3"
    first_store = SqliteExecutionStore(database)
    first = ExecutionService(
        gateway=gateway,
        store=first_store,
        plans=StaticPlanResolver({PLAN_ID: command(TradingMode.LIVE)}),
        clock=FrozenClock(NOW),
        allowed_mode=TradingMode.LIVE,
        kill_switch=CLEAR,
        live_promotion=live_promotion(),
    )
    await first.prepare(PLAN_ID)
    initial = await first.execute(COMMAND_ID)
    first_store.close()

    restarted_store = SqliteExecutionStore(database)
    restarted = ExecutionService(
        gateway=gateway,
        store=restarted_store,
        plans=StaticPlanResolver({PLAN_ID: command(TradingMode.LIVE)}),
        clock=FrozenClock(NOW),
        allowed_mode=TradingMode.LIVE,
        kill_switch=CLEAR,
        live_promotion=live_promotion(),
    )
    reconciled = await restarted.execute(COMMAND_ID)
    restarted_store.close()

    assert initial.state is GatewayState.UNKNOWN
    assert reconciled.state is GatewayState.CONFIRMED
    assert gateway.submit_calls == 1
    assert gateway.status_calls == 1


class KillSwitchOnSubmitting(InMemoryExecutionStore):
    """The operator hits the kill switch while the SUBMITTING intent is being written."""

    def __init__(self, kill_switch: KillSwitch) -> None:
        super().__init__()
        self._kill_switch = kill_switch

    async def put(self, record: ExecutionRecord) -> None:
        await super().put(record)
        if record.phase is ExecutionPhase.SUBMITTING:
            self._kill_switch.active = True


def close_command() -> DlmmCommandV1:
    return command(TradingMode.LIVE).model_copy(
        update={
            "operation": DlmmOperation.CLOSE,
            "strategy": None,
            "amount_x": None,
            "amount_y": None,
            "remove_bps": 10_000,
        }
    )


def live_service(
    value: DlmmCommandV1,
    store: InMemoryExecutionStore,
    gateway: RecordingGateway,
    kill_switch: KillSwitch | None,
    policy: KillSwitchPolicy = KillSwitchPolicy.HOLD_AND_ALERT,
) -> ExecutionService:
    return ExecutionService(
        gateway=gateway,
        store=store,
        plans=StaticPlanResolver({PLAN_ID: value}),
        clock=FrozenClock(NOW),
        allowed_mode=TradingMode.LIVE,
        live_promotion=live_promotion(),
        kill_switch=kill_switch,
        kill_switch_policy=policy,
    )


def test_kill_switch_raised_after_the_submitting_write_stops_the_gateway_submit() -> None:
    async def scenario() -> tuple[int, ExecutionPhase]:
        gateway = RecordingGateway()
        kill_switch = KillSwitch()
        store = KillSwitchOnSubmitting(kill_switch)
        service = live_service(command(TradingMode.LIVE), store, gateway, kill_switch)
        await service.prepare(PLAN_ID)
        with pytest.raises(ExecutionBlocked, match="kill switch is active"):
            await service.execute(COMMAND_ID)
        record = await store.get(COMMAND_ID)
        assert record is not None
        return gateway.submit_calls, record.phase

    submits, phase = asyncio.run(scenario())

    assert submits == 0
    assert phase is ExecutionPhase.FAILED


@pytest.mark.parametrize("probe", [None, "failing"], ids=["not-wired", "unavailable"])
def test_unknown_kill_switch_state_never_lets_a_command_out(probe: str | None) -> None:
    class FailingKillSwitch(KillSwitch):
        async def kill_switch_active(self) -> bool:
            raise ConnectionError("mode store unreachable")

    async def scenario() -> int:
        gateway = RecordingGateway()
        kill_switch = None if probe is None else FailingKillSwitch()
        service = live_service(
            command(TradingMode.LIVE), InMemoryExecutionStore(), gateway, kill_switch
        )
        await service.prepare(PLAN_ID)
        with pytest.raises(ExecutionBlocked, match="kill switch"):
            await service.execute(COMMAND_ID)
        return gateway.submit_calls

    assert asyncio.run(scenario()) == 0


@pytest.mark.parametrize(
    ("operation", "policy", "submitted"),
    [
        ("CLOSE", KillSwitchPolicy.ORDERLY_EXIT, True),
        ("CLOSE", KillSwitchPolicy.HOLD_AND_ALERT, False),
        ("OPEN", KillSwitchPolicy.ORDERLY_EXIT, False),
    ],
)
def test_active_kill_switch_lets_only_an_orderly_exit_through(
    operation: str, policy: KillSwitchPolicy, submitted: bool
) -> None:
    value = close_command() if operation == "CLOSE" else command(TradingMode.LIVE)

    async def scenario() -> int:
        gateway = RecordingGateway()
        service = live_service(
            value, InMemoryExecutionStore(), gateway, KillSwitch(active=True), policy
        )
        await service.prepare(PLAN_ID)
        try:
            await service.execute(COMMAND_ID)
        except ExecutionBlocked:
            pass
        return gateway.submit_calls

    assert asyncio.run(scenario()) == (1 if submitted else 0)
