"""The APPROVAL card of the sale after an exit (G22): one card per sale command."""

import asyncio
from collections.abc import Callable, Iterator
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest
from pydantic import SecretStr

from src.api.app import ApprovalSetup
from src.api.approvals import ApprovalWiring
from src.core.clock import FrozenClock
from src.domain.models.events import EventType
from src.domain.models.trading import TradingMode
from src.execution.contracts import (
    ChainReceiptV1,
    DlmmCommand,
    DlmmCommandV2,
    DlmmOperation,
    ExecutionPhase,
    GatewayReceiptV2,
    OperatorApproval,
    SimulationResultV2,
    SwapDirection,
    SwapQuoteV2,
    bind_simulation_result_v2,
    canonical_command_digest,
)
from src.execution.service import ExecutionService, HmacApprovalVerifier, StaticPlanResolver
from src.modes import DailyRiskService, KillSwitchPolicy
from src.persistence.factory import Repositories
from src.worker.execution import ModeKillSwitch, exit_sale_command_id, exit_sale_key
from tests.api.support import (
    APPROVAL_SECRET,
    OPERATOR_TOKEN,
    ApprovalCase,
    Panel,
    Scenario,
    factory_repositories,
    harness_over,
    make_panel,
    populated,
    promote_to_approval,
    requote,
)
from tests.worker.harness import NOW, POOL, WALLET

ALL_MARKS = {
    "twitter_hype": True,
    "developer_supply": True,
    "no_pvp_copies": True,
    "bubblemaps_clusters": True,
}


class SaleGateway:
    """Quotes the sale like the executor: 2 quote units per base unit, ``impact`` bps."""

    def __init__(self, clock: FrozenClock, impact: int) -> None:
        self.clock = clock
        self.impact = impact

    async def simulate(self, command: DlmmCommand) -> SimulationResultV2:
        assert isinstance(command, DlmmCommandV2) and command.in_amount is not None
        out = int(command.in_amount) * 2
        return bind_simulation_result_v2(
            command_id=command.command_id,
            command_digest=canonical_command_digest(command),
            success=True,
            simulated_at=self.clock.now(),
            blockhash="safe-blockhash",
            last_valid_block_height=9,
            compute_units=60_000,
            network_fee_lamports="5000",
            priority_fee_lamports="1200",
            price_impact_bps=self.impact,
            min_bin_id=None,
            max_bin_id=None,
            position_address=command.position_address,
            active_bin_id=8_000,
            token_x_decimals=6,
            token_y_decimals=9,
            errors=(),
            swap_quote=SwapQuoteV2(
                in_amount=command.in_amount,
                out_amount=str(out),
                min_out_amount=str(out * (10_000 - command.max_slippage_bps) // 10_000),
                fee_amount="10",
            ),
        )

    async def submit(
        self, command: DlmmCommand, key: str, approval: OperatorApproval | None
    ) -> GatewayReceiptV2:  # pragma: no cover - the desk never submits
        raise AssertionError((command, key, approval))

    async def status(self, signature: str) -> ChainReceiptV1:  # pragma: no cover - unused
        raise AssertionError(signature)


def awaiting_sale(scenario: Scenario, repositories: Repositories) -> ApprovalCase:
    """Part 1 of the sale after the first exit of the accepted (open) position."""

    harness = scenario.harness
    entry = scenario.accepted.entry
    assert entry is not None
    position_id = str(entry.position_id)
    command = DlmmCommandV2(
        command_id=exit_sale_command_id(position_id, 1, 1),
        idempotency_key=exit_sale_key(position_id, 1, 1),
        correlation_id=uuid5(NAMESPACE_URL, "approval-sale"),
        ruleset_version=harness.policy.ruleset_version,
        mode=TradingMode.APPROVAL,
        operation=DlmmOperation.SWAP,
        pool_address=POOL.address.root,
        wallet_address=WALLET,
        position_address=position_id,
        expires_at=harness.clock.now() + timedelta(minutes=5),
        max_slippage_bps=100,
        swap_direction=SwapDirection.X_TO_Y,
        in_amount="1000",
        max_price_impact_bps=300,
    )
    service = ExecutionService(
        gateway=SaleGateway(harness.clock, impact=120),
        store=repositories.execution,
        plans=StaticPlanResolver({command.command_id: command}),
        clock=harness.clock,
        allowed_mode=TradingMode.APPROVAL,
        approval_verifier=HmacApprovalVerifier(APPROVAL_SECRET.encode("utf-8")),
        kill_switch=ModeKillSwitch(repositories.mode),
    )
    asyncio.run(service.prepare(command.command_id))
    setup = ApprovalSetup(
        wiring=ApprovalWiring(
            service=service,
            approval_secret=SecretStr(APPROVAL_SECRET),
            simulation_ttl=timedelta(minutes=2),
        ),
        unavailable=(),
    )
    accepted = scenario.accepted
    return ApprovalCase(command, service, setup, accepted.plan_id, accepted.decision_id)


@pytest.fixture
def sale(tmp_path: Path) -> Iterator[tuple[Panel, ApprovalCase]]:
    repositories = factory_repositories(tmp_path)
    harness = harness_over(repositories, FrozenClock(NOW))
    scenario = populated(harness)
    promote_to_approval(harness)
    case = awaiting_sale(scenario, repositories)
    panel = make_panel(tmp_path, repositories=repositories, harness=harness, approvals=case.setup)
    yield panel, case
    repositories.close()


def approve_body(preview: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    return {
        "approval_id": str(uuid4()),
        "command_digest": preview["command_digest"],
        "simulation_digest": preview["simulation_digest"],
        "checklist": ALL_MARKS,
        "comment": "продаём",
        "operator_token": OPERATOR_TOKEN,
        "confirm_irreversible": True,
        **overrides,
    }


def phase(panel: Panel, case: ApprovalCase) -> ExecutionPhase:
    record = asyncio.run(panel.repositories.execution.get(case.command.command_id))
    assert record is not None
    return record.phase


def test_the_sale_card_shows_amount_expected_output_minimum_impact_and_its_limit(
    sale: tuple[Panel, ApprovalCase],
) -> None:
    panel, case = sale
    path = f"/api/v1/approvals/{case.command.command_id}"

    preview = panel.get(path).json()
    approved = panel.bearer(path, approve_body(preview))

    assert preview["operation"] == "SWAP"
    assert preview["swap"] == {
        "position_address": case.command.position_address,
        "exit_attempt": 1,
        "part": 1,
        "in_amount": "1000",
        "expected_out_amount": "2000",
        "min_out_amount": "1980",  # 2000 less the 100 bps slippage
        "fee_amount": "10",
        "price_impact_bps": 120,
        "max_price_impact_bps": 300,
        "slippage_bps": 100,
    }
    assert preview["blockers"] == [] and preview["can_approve"] is True
    # The exit's G17 checklist; the chart and theme marks of an entry (spec 6.8) are not asked.
    assert [item["key"] for item in preview["checklist"]] == list(ALL_MARKS)
    diff = {row["field"]: row for row in preview["plan_diff"]}
    assert diff["price_impact_bps"]["planned"] == "≤ 300"
    assert diff["price_impact_bps"]["matches"] is True
    assert preview["plan_id"] == str(case.plan_id)
    assert preview["decision_id"] == str(case.decision_id)
    assert approved.status_code == 200, approved.text
    assert phase(panel, case) is ExecutionPhase.APPROVED
    (event,) = [
        event
        for event in panel.harness.audit.events
        if event.event_type is EventType.APPROVAL_REQUESTED
    ]
    assert event.payload["swap"] == preview["swap"]


def test_a_sale_quoted_above_its_own_impact_limit_cannot_be_approved(
    sale: tuple[Panel, ApprovalCase],
) -> None:
    panel, case = sale
    requote(panel.repositories, case, 9_000)
    path = f"/api/v1/approvals/{case.command.command_id}"

    preview = panel.get(path).json()
    response = panel.bearer(path, approve_body(preview))

    assert [item["code"] for item in preview["blockers"]] == ["PRICE_IMPACT_BREACH"]
    diff = {row["field"]: row for row in preview["plan_diff"]}
    assert diff["price_impact_bps"]["matches"] is False
    assert response.status_code == 409
    assert response.json()["error"]["details"]["blockers"] == ["PRICE_IMPACT_BREACH"]
    assert phase(panel, case) is ExecutionPhase.AWAITING_APPROVAL


def kill_switch(panel: Panel) -> None:
    response = panel.bearer(
        "/api/v1/kill-switch/activate",
        {"reason": "авария", "policy": KillSwitchPolicy.HOLD_AND_ALERT.value},
    )
    assert response.status_code == 200, response.text


def daily_stop(panel: Panel) -> None:
    harness = panel.harness
    day = DailyRiskService(harness.modes, harness.clock).record(
        Decimal("-50000"), Decimal(0), harness.policy.ruleset_version
    )
    assert day.daily_stop_triggered is True


@pytest.mark.parametrize("stop", [kill_switch, daily_stop], ids=["kill-switch", "daily-stop"])
def test_neither_the_kill_switch_nor_the_daily_stop_blocks_the_sale_card(
    sale: tuple[Panel, ApprovalCase], stop: Callable[[Panel], None]
) -> None:
    panel, case = sale
    path = f"/api/v1/approvals/{case.command.command_id}"
    stop(panel)

    preview = panel.get(path).json()
    approved = panel.bearer(path, approve_body(preview))

    assert panel.repositories.mode.mode_state().mode is TradingMode.SHADOW  # demoted
    assert preview["blockers"] == [] and preview["can_approve"] is True
    assert approved.status_code == 200, approved.text
    assert phase(panel, case) is ExecutionPhase.APPROVED


def test_the_approval_page_shows_the_sale_card(sale: tuple[Panel, ApprovalCase]) -> None:
    panel, case = sale
    panel.login()

    page = panel.get(f"/approvals/{case.command.command_id}").text

    for fragment in (
        "Продажа монеты после выхода",
        "Продаётся: <strong>1000</strong>",
        "Ожидаемый выход: <strong>2000</strong>",
        "Минимальный выход: <strong>1980</strong>",
        "Price impact 120 bps при лимите обмена 300 bps",
    ):
        assert fragment in page
