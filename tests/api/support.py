"""Operator-app test wiring: factory repositories (SQLite), the worker harness as the data
source, a fake execution gateway and a cookie-keeping ASGI client.

Values below are test-only placeholders, not credentials of any real deployment.
"""

import asyncio
import re
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import httpx
from fastapi import FastAPI
from pydantic import SecretStr

from src.api.app import ApprovalSetup, create_operator_app
from src.api.approvals import ApprovalWiring
from src.api.probes import ServiceProbe
from src.api.settings import OperatorSettings
from src.core.clock import FrozenClock
from src.core.config import AppConfig
from src.domain.models.primitives import PublicKey
from src.domain.models.trading import TradingMode
from src.domain.screening.models import DecisionFlag
from src.execution import contracts
from src.execution.contracts import (
    ChainReceiptV1,
    ChainState,
    GatewayReceiptV1,
    OperatorApproval,
    bind_simulation_result,
    canonical_command_digest,
)
from src.execution.service import ExecutionService, HmacApprovalVerifier, StaticPlanResolver
from src.market.models import PoolRef, PoolSnapshot
from src.modes.models import DailyPromotionObservation
from src.modes.service import ModeService, PromotionEvidenceService
from src.persistence.factory import Repositories, open_repositories
from src.worker.execution import (
    BinAnchor,
    ModeKillSwitch,
    build_entry_command,
    position_action_command_id,
    position_action_key,
)
from src.worker.policy import PipelinePolicy
from tests.worker.harness import (
    NOW,
    POOL,
    WALLET,
    Harness,
    harness_policy,
    snapshot,
    spike_history,
)

OPERATOR_TOKEN = "test-operator-token-not-a-real-secret"  # noqa: S105 - test placeholder
APPROVAL_SECRET = "test-approval-secret-not-a-real-one-0123456789"  # noqa: S105
SESSION_KEY = b"test-session-key-not-a-real-secret-0123"
NEW_POSITION = "SysvarRecentB1ockHashes11111111111111111111"
EXIT_POSITION = "Stake11111111111111111111111111111111111111"
# Same base token as POOL, so the fixture token risk applies: B warms up, C (low TVL) rejects.
POOL_B = PoolRef(
    address=PublicKey("Vote111111111111111111111111111111111111111"),
    base_mint=POOL.base_mint,
    quote_mint=POOL.quote_mint,
)
POOL_C = PoolRef(
    address=PublicKey("Config1111111111111111111111111111111111111"),
    base_mint=POOL.base_mint,
    quote_mint=POOL.quote_mint,
)


def factory_repositories(tmp_path: Path) -> Repositories:
    return open_repositories(
        AppConfig.load(environ={"APP_ENV": "test"}),
        environ={"LOCAL_SQLITE_PATH": str(tmp_path / "operator.sqlite3")},
    )


def harness_over(
    repositories: Repositories, clock: FrozenClock, *, policy: PipelinePolicy | None = None
) -> Harness:
    return Harness(
        clock=clock,
        modes=repositories.mode,
        accounting=repositories.accounting,
        universe=repositories.universe,  # type: ignore[arg-type]
        decisions=repositories.decisions,  # type: ignore[arg-type]
        audit=repositories.audit,  # type: ignore[arg-type]
        contexts=repositories.positions,  # type: ignore[arg-type]
        policy=policy or harness_policy(),
    )


def ingest_universe(harness: Harness, snapshots: list[PoolSnapshot]) -> None:
    async def run() -> None:
        stage = await harness.universe.begin(uuid4(), snapshots[0].observed_at)
        await stage.stage(tuple(snapshots))
        await stage.commit()

    asyncio.run(run())


def other_pool(pool: PoolRef, *, tvl: str = "5000") -> PoolSnapshot:
    return snapshot(NOW, tvl=tvl).model_copy(update={"pool": pool})


def evaluate(harness: Harness, address: str) -> Any:
    async def run() -> Any:
        harness.keeper.ensure_open()
        return await harness.pipeline.evaluate(address, NOW)

    return asyncio.run(run())


@dataclass
class Scenario:
    harness: Harness
    accepted: Any
    others: list[Any]


def populated(harness: Harness) -> Scenario:
    """POOL: calm history + spike (ACCEPT, shadow position); POOL_B warms up; POOL_C has
    a TVL below the minimum (REJECT)."""

    history = spike_history()
    harness.ingest(history[:-1])
    ingest_universe(harness, [history[-1], other_pool(POOL_B), other_pool(POOL_C, tvl="999")])
    accepted = evaluate(harness, POOL.address.root)
    others = [evaluate(harness, POOL_B.address.root), evaluate(harness, POOL_C.address.root)]
    return Scenario(harness, accepted, others)


def flag_decision(harness: Harness, decision_id: UUID, *flags: DecisionFlag) -> None:
    store = harness.decisions
    record = store.decisions[decision_id]
    store.decisions[decision_id] = record.model_copy(
        update={"decision": record.decision.model_copy(update={"flags": flags})}
    )


def promote_to_approval(harness: Harness) -> None:
    clock = harness.clock
    service = PromotionEvidenceService(harness.modes, clock)
    for days_ago in range(7, 0, -1):
        service.record_daily(
            DailyPromotionObservation(
                risk_date=(clock.now() - timedelta(days=days_ago)).date(),
                ruleset_version=harness.policy.ruleset_version,
                mode=TradingMode.SHADOW,
                completed_positions=3,
                net_pnl=Decimal("1"),
                start_equity=Decimal("1000"),
                end_equity=Decimal("1001"),
                conservative_open_equity=Decimal("1000"),
                data_coverage=Decimal("0.99"),
                decision_count=10,
            )
        )
    evidence = service.build_shadow(harness.policy.ruleset_version)
    ModeService(harness.modes, clock, harness.policy.ruleset_version).transition(
        TradingMode.APPROVAL, evidence, "operator-test-confirmation"
    )


def record_observations(harness: Harness, days: int) -> None:
    clock = harness.clock
    service = PromotionEvidenceService(harness.modes, clock)
    for days_ago in range(days, 0, -1):
        service.record_daily(
            DailyPromotionObservation(
                risk_date=(clock.now() - timedelta(days=days_ago)).date(),
                ruleset_version=harness.policy.ruleset_version,
                mode=TradingMode.SHADOW,
                completed_positions=3,
                net_pnl=Decimal("1"),
                start_equity=Decimal("1000"),
                end_equity=Decimal("1001"),
                conservative_open_equity=Decimal("1000"),
                data_coverage=Decimal("0.99"),
                decision_count=10,
            )
        )


class FakeGateway:
    """Simulates at the current clock; never submits (approvals are not executions)."""

    def __init__(self, clock: FrozenClock, *, price_impact_bps: int | None = 5) -> None:
        self.clock = clock
        self.price_impact_bps = price_impact_bps
        self.submits = 0

    async def simulate(self, command: Any) -> Any:
        payload: dict[str, Any] = {
            "command_id": command.command_id,
            "command_digest": canonical_command_digest(command),
            "success": True,
            "simulated_at": self.clock.now(),
            "blockhash": "blockhash-test",
            "last_valid_block_height": 100,
            "compute_units": 120_000,
            "network_fee_lamports": "5000",
            "priority_fee_lamports": "1000",
            "price_impact_bps": self.price_impact_bps,
            "min_bin_id": command.min_bin_id,
            "max_bin_id": command.max_bin_id,
            "errors": (),
        }
        if command.schema_version == 1:
            return bind_simulation_result(**payload)
        # V2: the executor reports the position it acts on (for an OPEN the one it created)
        # and what the chain says now - for an entry, the anchor it was built on.
        return contracts.bind_simulation_result_v2(
            **payload,
            position_address=command.position_address or NEW_POSITION,
            active_bin_id=_or(command.active_bin_id, 8_000),
            token_x_decimals=_or(command.token_x_decimals, 6),
            token_y_decimals=_or(command.token_y_decimals, 6),
        )

    async def submit(
        self, command: Any, idempotency_key: str, approval: OperatorApproval | None
    ) -> GatewayReceiptV1:
        self.submits += 1
        raise AssertionError("the operator app never submits")

    async def status(self, signature: str) -> ChainReceiptV1:
        return ChainReceiptV1(
            signature=signature, state=ChainState.CONFIRMED, checked_at=self.clock.now()
        )


@dataclass
class ApprovalCase:
    command: Any
    service: ExecutionService
    setup: ApprovalSetup
    plan_id: UUID
    decision_id: UUID


def awaiting_approval(scenario: Scenario, repositories: Repositories) -> ApprovalCase:
    """An APPROVAL entry for the accepted plan, simulated and waiting for the operator."""

    harness = scenario.harness
    plan_id = scenario.accepted.plan_id
    sized = harness.decisions.plans[plan_id].sized_plan
    plan = sized.plan
    command = build_entry_command(
        plan_id=plan_id,
        mode=TradingMode.APPROVAL,
        market=spike_history()[-1],
        size_base_units=sized.size_base_units,
        strategy_kind=plan.strategy.value,
        lower_bin_offset=plan.lower_bin_offset,
        upper_bin_offset=plan.upper_bin_offset,
        expires_at=plan.expires_at,
        correlation_id=str(uuid5(NAMESPACE_URL, "approval-case")),
        ruleset_version=plan.ruleset_version,
        wallet_address=WALLET,
        max_slippage_bps=100,
        anchor=BinAnchor(active_bin_id=8_000, base_decimals=6, quote_decimals=6),
    )
    service = ExecutionService(
        gateway=FakeGateway(harness.clock),
        store=repositories.execution,
        plans=StaticPlanResolver({command.command_id: command}),
        clock=harness.clock,
        allowed_mode=TradingMode.APPROVAL,
        approval_verifier=HmacApprovalVerifier(APPROVAL_SECRET.encode("utf-8")),
        # The service and the desk read one kill-switch policy: the mode state's.
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
    return ApprovalCase(command, service, setup, plan_id, scenario.accepted.decision_id)


def awaiting_exit(
    scenario: Scenario,
    repositories: Repositories,
    *,
    operation: contracts.DlmmOperation,
    price_impact_bps: int | None,
    position_id: str = EXIT_POSITION,
    preview: int = 1,
) -> ApprovalCase:
    """An APPROVAL exit of an open position, built as the position cycle builds it (no
    anchor, no amounts), simulated by an executor quoting ``price_impact_bps`` for the swap
    leg it does not perform (D08) and waiting for the operator. ``preview`` > 1: a renewed
    preview of the first attempt (the earlier ones expired unapproved)."""

    harness = scenario.harness
    attempt = 1
    command = contracts.DlmmCommandV2(
        command_id=position_action_command_id(position_id, attempt, preview),
        idempotency_key=position_action_key(position_id, attempt, preview),
        correlation_id=uuid5(NAMESPACE_URL, "approval-exit"),
        ruleset_version=harness.policy.ruleset_version,
        mode=TradingMode.APPROVAL,
        operation=operation,
        pool_address=POOL.address.root,
        wallet_address=WALLET,
        position_address=position_id,
        expires_at=harness.clock.now() + timedelta(minutes=5),
        max_slippage_bps=100,
        min_bin_id=7_990,
        max_bin_id=8_010,
        remove_bps=10_000 if operation is contracts.DlmmOperation.CLOSE else 5_000,
    )
    service = ExecutionService(
        gateway=FakeGateway(harness.clock, price_impact_bps=price_impact_bps),
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


def requote(repositories: Repositories, case: ApprovalCase, price_impact_bps: int | None) -> None:
    """Store the pending command with another quote, as a record written before the
    executor refused unmeasured or over-limit entries would look (the desk's own guard)."""

    async def run() -> None:
        record = await repositories.execution.get(case.command.command_id)
        assert record is not None
        payload = record.simulation.model_dump(exclude={"simulation_digest"})
        record.simulation = contracts.bind_simulation_result_v2(
            **{**payload, "price_impact_bps": price_impact_bps}
        )
        await repositories.execution.put(record)

    asyncio.run(run())


def _or(value: int | None, default: int) -> int:
    return default if value is None else value


def operator_settings(**overrides: Any) -> OperatorSettings:
    values: dict[str, Any] = {
        "operator_token": SecretStr(OPERATOR_TOKEN),
        "session_key": SESSION_KEY,
        "stream_max_events": 1,
        "stream_interval": timedelta(0),
    }
    values.update(overrides)
    return OperatorSettings(**values)


@dataclass
class Panel:
    app: FastAPI
    repositories: Repositories
    harness: Harness
    cookies: httpx.Cookies = field(default_factory=httpx.Cookies)

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        async def run() -> httpx.Response:
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver", cookies=self.cookies
            ) as client:
                response = await client.request(method, path, **kwargs)
                self.cookies = client.cookies
                return response

        return asyncio.run(run())

    def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("POST", path, **kwargs)

    def bearer(self, path: str, body: dict[str, Any] | None = None) -> httpx.Response:
        return self.post(
            path, json=body or {}, headers={"Authorization": f"Bearer {OPERATOR_TOKEN}"}
        )

    def login(self) -> str:
        response = self.post("/login", data={"token": OPERATOR_TOKEN, "next": "/"})
        assert response.status_code == 303, response.text
        page = self.get("/").text
        match = re.search(r'<meta name="csrf-token" content="([0-9a-f]{64})">', page)
        assert match is not None, "logged-in pages carry the session CSRF token"
        return match.group(1)


def make_panel(
    tmp_path: Path,
    *,
    environ: dict[str, str] | None = None,
    approvals: ApprovalSetup | None = None,
    probes: tuple[ServiceProbe, ...] = (),
    settings: OperatorSettings | None = None,
    repositories: Repositories | None = None,
    harness: Harness | None = None,
) -> Panel:
    clock = harness.clock if harness is not None else FrozenClock(NOW)
    stores = repositories or factory_repositories(tmp_path)
    worker = harness or harness_over(stores, clock)
    environment = {"APP_ENV": "test", "APP_OPERATOR_TOKEN": OPERATOR_TOKEN, **(environ or {})}
    app = create_operator_app(
        config=AppConfig.load(environ=environment),
        environ=environment,
        repositories=stores,
        clock=clock,
        policy=worker.policy,
        settings=settings or operator_settings(),
        approvals=approvals or ApprovalSetup(None, ("TRADING_MODE_SHADOW",)),
        probes=probes,
    )
    return Panel(app, stores, worker)
