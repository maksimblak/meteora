"""The execution service with SWAP commands (G22): limits, kill switch and settlements."""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest

from src.core.clock import FrozenClock
from src.domain.models.trading import TradingMode
from src.execution.contracts import (
    ChainReceiptV1,
    DlmmCommandV2,
    DlmmOperation,
    ExecutionPhase,
    GatewayReceiptV2,
    GatewayState,
    LivePromotionEvidence,
    OperatorApproval,
    SettlementV2,
    SimulationResultV2,
    SwapDirection,
    SwapQuoteV2,
    bind_simulation_result_v2,
    canonical_command_digest,
)
from src.execution.service import (
    ExecutionBlocked,
    ExecutionOutcomeUnknown,
    ExecutionService,
    InMemoryExecutionStore,
    SimulationRejected,
    StaticPlanResolver,
)
from src.modes.models import KillSwitchPolicy

NOW = datetime(2026, 10, 18, 12, tzinfo=UTC)
COMMAND_ID = UUID("10000000-0000-4000-8000-000000000501")
POOL = "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo"
WALLET = "SysvarC1ock11111111111111111111111111111111"
POSITION = "SysvarRent111111111111111111111111111111111"
WSOL = "So11111111111111111111111111111111111111112"


def synchronously[**P](
    function: Callable[P, Coroutine[Any, Any, None]],
) -> Callable[P, None]:
    @functools.wraps(function)
    def run(*args: P.args, **kwargs: P.kwargs) -> None:
        asyncio.run(function(*args, **kwargs))

    return run


def swap(
    *, in_amount: str = "1000", impact_limit: int = 300, slippage: int = 100
) -> DlmmCommandV2:
    return DlmmCommandV2(
        command_id=COMMAND_ID,
        idempotency_key=f"exit-sale:{POSITION}:1:1",
        correlation_id=UUID("20000000-0000-4000-8000-000000000501"),
        ruleset_version="guide-v1",
        mode=TradingMode.LIVE,
        operation=DlmmOperation.SWAP,
        pool_address=POOL,
        wallet_address=WALLET,
        position_address=POSITION,
        expires_at=NOW + timedelta(minutes=2),
        max_slippage_bps=slippage,
        swap_direction=SwapDirection.X_TO_Y,
        in_amount=in_amount,
        max_price_impact_bps=impact_limit,
    )


def promotion() -> LivePromotionEvidence:
    return LivePromotionEvidence(
        promotion_id=UUID("50000000-0000-4000-8000-000000000001"),
        ruleset_version="guide-v1",
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


class KillSwitch:
    def __init__(self, active: bool, policy: KillSwitchPolicy | None) -> None:
        self.active = active
        self.policy = policy

    async def kill_switch_active(self) -> bool:
        return self.active

    async def kill_switch_policy(self) -> KillSwitchPolicy | None:
        return self.policy if self.active else None


class SwapGateway:
    """Answers like the executor would, with a configurable quote."""

    def __init__(self, *, impact: int = 120, out: str = "5000", minimum: str | None = None) -> None:
        self.impact = impact
        self.out = out
        self.minimum = minimum
        self.submits = 0
        self.fail_submit = False
        self.receipt: GatewayReceiptV2 | None = None
        self.lookups: list[UUID] = []
        self.lookup_answer: GatewayReceiptV2 | None = None
        self.simulation: SimulationResultV2 | None = None

    async def simulate(self, command: DlmmCommandV2) -> SimulationResultV2:
        assert command.in_amount is not None
        out = int(self.out)
        minimum = self.minimum or str(out * (10_000 - command.max_slippage_bps) // 10_000)
        self.simulation = bind_simulation_result_v2(
            command_id=command.command_id,
            command_digest=canonical_command_digest(command),
            success=True,
            simulated_at=NOW,
            blockhash="safe-blockhash",
            last_valid_block_height=9,
            compute_units=60_000,
            network_fee_lamports="5000",
            priority_fee_lamports="1200",
            price_impact_bps=self.impact,
            min_bin_id=None,
            max_bin_id=None,
            position_address=command.position_address,
            active_bin_id=8000,
            token_x_decimals=6,
            token_y_decimals=9,
            errors=(),
            swap_quote=SwapQuoteV2(
                in_amount=command.in_amount,
                out_amount=self.out,
                min_out_amount=minimum,
                fee_amount="10",
            ),
        )
        return self.simulation

    async def submit(
        self, command: DlmmCommandV2, idempotency_key: str, approval: OperatorApproval | None
    ) -> GatewayReceiptV2:
        del approval
        self.submits += 1
        if self.fail_submit:
            raise TimeoutError("the executor did not answer after the request left")
        assert self.simulation is not None
        self.receipt = GatewayReceiptV2(
            command_id=command.command_id,
            command_digest=self.simulation.command_digest,
            simulation_digest=self.simulation.simulation_digest,
            idempotency_key=idempotency_key,
            state=GatewayState.CONFIRMED,
            position_address=POSITION,
            signature="sale-1",
            signatures=("sale-1",),
            transaction_count=1,
            submitted_at=NOW,
            attempt_count=1,
        )
        return self.receipt

    async def status(self, signature: str) -> ChainReceiptV1:  # pragma: no cover - unused
        raise AssertionError(signature)

    async def lookup(self, command_id: UUID) -> GatewayReceiptV2 | None:
        self.lookups.append(command_id)
        return self.lookup_answer


def service(
    gateway: SwapGateway, command: DlmmCommandV2, kill_switch: KillSwitch | None = None
) -> tuple[ExecutionService, InMemoryExecutionStore]:
    store = InMemoryExecutionStore()
    return (
        ExecutionService(
            gateway=gateway,
            store=store,
            plans=StaticPlanResolver({command.command_id: command}),
            clock=FrozenClock(NOW),
            allowed_mode=TradingMode.LIVE,
            live_promotion=promotion(),
            receipts=gateway,
            kill_switch=kill_switch or KillSwitch(False, None),
        ),
        store,
    )


@synchronously
async def test_a_swap_quoted_above_its_own_impact_limit_is_never_sent() -> None:
    command = swap(impact_limit=300)
    gateway = SwapGateway(impact=301)
    execution, store = service(gateway, command)

    with pytest.raises(SimulationRejected) as refused:
        await execution.prepare(command.command_id)

    assert refused.value.code == "PRICE_IMPACT_BREACH"
    record = await store.get(command.command_id)
    assert record is not None and record.phase is ExecutionPhase.FAILED
    with pytest.raises(ExecutionBlocked):
        await execution.execute(command.command_id)
    assert gateway.submits == 0


@synchronously
async def test_a_swap_whose_on_chain_minimum_is_looser_than_its_slippage_is_refused() -> None:
    # 5000 out with 100 bps slippage tolerates at least 4950; the executor offered 4949.
    command = swap(slippage=100)
    gateway = SwapGateway(out="5000", minimum="4949")
    execution, _ = service(gateway, command)

    with pytest.raises(SimulationRejected) as refused:
        await execution.prepare(command.command_id)

    assert refused.value.code == "SWAP_QUOTE_INCOMPLETE"
    assert gateway.submits == 0


@synchronously
@pytest.mark.parametrize(
    "policy", [KillSwitchPolicy.HOLD_AND_ALERT, KillSwitchPolicy.ORDERLY_EXIT]
)
async def test_the_kill_switch_never_blocks_the_sale_after_an_exit(
    policy: KillSwitchPolicy,
) -> None:
    command = swap()
    gateway = SwapGateway()
    execution, _ = service(gateway, command, KillSwitch(True, policy))

    await execution.prepare(command.command_id)
    receipt = await execution.execute(command.command_id)

    assert receipt.state is GatewayState.CONFIRMED
    assert gateway.submits == 1


@synchronously
async def test_an_unknown_swap_outcome_is_reconciled_by_command_id_and_never_resent() -> None:
    command = swap()
    gateway = SwapGateway()
    gateway.fail_submit = True
    execution, _ = service(gateway, command)
    await execution.prepare(command.command_id)

    with pytest.raises(ExecutionOutcomeUnknown):
        await execution.execute(command.command_id)
    gateway.fail_submit = False
    with pytest.raises(ExecutionOutcomeUnknown):
        await execution.execute(command.command_id)

    assert gateway.submits == 1
    assert gateway.lookups == [command.command_id]


@synchronously
async def test_the_settlement_of_a_confirmed_command_is_adopted_when_the_executor_has_read_it() -> (
    None
):
    command = swap()
    gateway = SwapGateway()
    execution, store = service(gateway, command)
    await execution.prepare(command.command_id)
    await execution.execute(command.command_id)
    assert gateway.receipt is not None
    settlement = SettlementV2(
        fee_lamports="6200",
        priority_fee_lamports="1200",
        delta_x="-1000",
        delta_y="4990",
        mint_x=WALLET,
        mint_y=WSOL,
    )

    assert await execution.refresh_settlement(command.command_id) is None
    gateway.lookup_answer = gateway.receipt.model_copy(update={"settlement": settlement})
    adopted = await execution.refresh_settlement(command.command_id)
    gateway.lookup_answer = None
    kept = await execution.refresh_settlement(command.command_id)

    assert adopted == settlement
    assert kept == settlement
    record = await store.get(command.command_id)
    assert record is not None and isinstance(record.receipt, GatewayReceiptV2)
    assert record.receipt.settlement == settlement
    assert gateway.lookups == [command.command_id, command.command_id]


@synchronously
async def test_a_settlement_for_another_binding_is_never_adopted() -> None:
    command = swap()
    gateway = SwapGateway()
    execution, store = service(gateway, command)
    await execution.prepare(command.command_id)
    await execution.execute(command.command_id)
    assert gateway.receipt is not None
    gateway.lookup_answer = gateway.receipt.model_copy(
        update={
            "simulation_digest": "f" * 64,
            "settlement": SettlementV2(
                fee_lamports="1",
                priority_fee_lamports="0",
                delta_x="-1",
                delta_y="1",
                mint_x=WALLET,
                mint_y=WSOL,
            ),
        }
    )

    with pytest.raises(ExecutionBlocked):
        await execution.refresh_settlement(command.command_id)

    record = await store.get(command.command_id)
    assert record is not None and record.receipt is not None
    assert getattr(record.receipt, "settlement", None) is None
