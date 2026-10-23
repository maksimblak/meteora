"""Candidate pipeline and the money path: audit-first, kill-switch-safe, idempotent."""

import asyncio
from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from prometheus_client import generate_latest

from src.core.clock import FrozenClock
from src.domain.models.events import EventType
from src.domain.models.trading import TradingMode
from src.domain.screening import CandidateStatus, RejectionCode
from src.execution.contracts import (
    ChainReceiptV1,
    ChainState,
    DlmmCommandV1,
    GatewayReceiptV1,
    GatewayState,
    OperatorApproval,
    SimulationResultV1,
    bind_simulation_result,
    canonical_command_digest,
)
from src.execution.service import ExecutionService
from src.market.models import Freshness
from src.modes import InMemoryModeRepository, KillSwitchPolicy, ModeService, PendingPlanStatus
from src.persistence.memory import InMemoryRecoverableExecutionStore
from src.providers.fixtures import FixtureFailure
from src.worker.execution import (
    BinAnchor,
    CommandResolver,
    EntryStatus,
    GatewayFillBook,
    GatewaySubmitter,
    SubmitStatus,
    build_entry_command,
)
from tests.worker.harness import NOW, POOL, WALLET, Harness, snapshot, spike_history


def evaluate(harness: Harness, *, open_day: bool = True) -> Any:
    async def run() -> Any:
        if open_day:
            harness.keeper.ensure_open()
        return await harness.pipeline.evaluate(POOL.address.root, NOW)

    return asyncio.run(run())


def test_accepted_candidate_opens_a_shadow_position_through_the_whole_pipeline() -> None:
    harness = Harness()
    harness.ingest(spike_history())

    outcome = evaluate(harness)

    assert outcome.status is CandidateStatus.ACCEPT
    assert outcome.entry is not None and outcome.entry.status is EntryStatus.OPENED
    position_id = outcome.entry.position_id
    assert harness.modes.open_positions() == (position_id,)
    position = harness.accounting.position_for(position_id)
    assert position is not None and position.mode is TradingMode.SHADOW
    assert position.invested_base_units <= Decimal(100_000)
    assert asyncio.run(harness.contexts.get(position_id)) is not None
    assert harness.event_types() == [
        EventType.CANDIDATE_EVALUATED.value,
        EventType.STRATEGY_PLANNED.value,
        EventType.COMMAND_SUBMITTED.value,
    ]
    correlations = {str(event.correlation_id) for event in harness.audit.events}
    assert correlations == {outcome.correlation_id}
    text = generate_latest(harness.registry).decode()
    assert 'meteora_executions_total{mode="SHADOW",outcome="OPENED"} 1.0' in text


def test_decision_record_keeps_market_facts_thresholds_reasons_and_versions() -> None:
    harness = Harness()
    harness.ingest(spike_history(tvl="999"))

    outcome = evaluate(harness)
    record = harness.decisions.decisions[outcome.decision_id]

    assert outcome.status is CandidateStatus.REJECT
    assert RejectionCode.LOW_TVL in record.decision.reasons
    facts = record.facts
    assert (facts.volume_short, facts.volume_long) == (Decimal("400"), Decimal("1200"))
    assert facts.volume_24h == Decimal("57600")
    assert facts.tvl == Decimal("999")
    assert facts.fee_tvl == Decimal("20") / Decimal("999")
    assert facts.price == Decimal("1")
    assert facts.price_volatility == Decimal("0")
    assert facts.pool_age_seconds == 7 * 24 * 3600
    assert (facts.freshness, facts.source_age_seconds) == (Freshness.FRESH, 0)
    assert record.thresholds.minimum_tvl == Decimal("1000")
    assert any(item.name == "tvl" and item.threshold == "1000" for item in record.decision.evidence)
    assert (record.decision.ruleset_version, record.decision.config_version) == (
        "rules-1",
        "config-1",
    )
    assert record.decision.correlation_id == outcome.correlation_id
    assert harness.modes.open_positions() == ()
    assert (
        'meteora_candidate_rejections_total{reason="LOW_TVL"} 1.0'
        in generate_latest(harness.registry).decode()
    )


def test_entries_wait_for_the_utc_risk_day_to_be_opened() -> None:
    harness = Harness()
    harness.ingest(spike_history())

    blocked = evaluate(harness, open_day=False)

    assert blocked.status is CandidateStatus.ACCEPT
    assert blocked.entry is None
    assert "RISK_DAY_NOT_OPENED" in blocked.reasons
    assert harness.modes.open_positions() == ()
    harness.keeper.ensure_open()
    assert harness.modes.risk_day(NOW.date()) is not None


def test_unknown_token_risk_is_never_treated_as_safe() -> None:
    harness = Harness(token_failure=FixtureFailure.OUTAGE)
    harness.ingest(spike_history())

    outcome = evaluate(harness)

    assert outcome.status in {CandidateStatus.UNKNOWN, CandidateStatus.REJECT}
    assert (
        RejectionCode.TOKEN_RISK_UNKNOWN
        in harness.decisions.decisions[outcome.decision_id].decision.reasons
    )
    assert outcome.entry is None
    assert harness.accounting.positions() == ()


def test_kill_switch_after_claim_blocks_the_command_before_submit() -> None:
    base = InMemoryModeRepository()

    class KillRightAfterClaim:
        """An operator presses the kill switch right after the entry was claimed."""

        def __getattr__(self, name: str) -> Any:
            return getattr(base, name)

        def claim_entry(self, plan_id: UUID, claimed_at: Any) -> Any:
            claimed = base.claim_entry(plan_id, claimed_at)
            ModeService(base, FrozenClock(NOW), "rules-1").activate_kill_switch(
                "operator emergency", KillSwitchPolicy.HOLD_AND_ALERT
            )
            return claimed

    harness = Harness(modes=KillRightAfterClaim())
    harness.ingest(spike_history())

    outcome = evaluate(harness)

    assert outcome.entry is not None
    assert outcome.entry.status is EntryStatus.BLOCKED
    assert outcome.entry.reason == "KILL_SWITCH_ACTIVE"
    assert harness.accounting.positions() == ()
    assert base.entry_plan(outcome.plan_id).status is PendingPlanStatus.FAILED
    assert EventType.COMMAND_SUBMITTED.value not in harness.event_types()


def test_audit_failure_blocks_money_execution_and_raises_a_critical_alert() -> None:
    harness = Harness()
    harness.ingest(spike_history())
    harness.audit.fail_with = OSError("audit storage unavailable")
    harness.audit.fail_on = frozenset({EventType.COMMAND_SUBMITTED})

    outcome = evaluate(harness)

    assert outcome.entry is not None
    assert outcome.entry.status is EntryStatus.BLOCKED
    assert outcome.entry.reason == "AUDIT_PERSISTENCE_FAILED"
    assert harness.accounting.positions() == ()
    assert harness.modes.entry_plan(outcome.plan_id).status is PendingPlanStatus.FAILED
    assert [alert.code for alert in harness.modes.alerts() if alert.open] == [
        "AUDIT_PERSISTENCE_FAILED"
    ]


def test_decision_persistence_failure_stops_the_cycle_before_any_money_step() -> None:
    harness = Harness()
    harness.ingest(spike_history())
    harness.decisions.fail_with = OSError("database down")

    outcome = evaluate(harness)

    assert outcome.entry is None
    assert "DECISION_PERSISTENCE_FAILED" in outcome.reasons
    assert harness.modes.pending_count() == 0
    assert [alert.code for alert in harness.modes.alerts()] == ["DECISION_PERSISTENCE_FAILED"]


def test_repeating_the_same_cycle_never_duplicates_decisions_or_positions() -> None:
    harness = Harness()
    harness.ingest(spike_history())

    first = evaluate(harness)
    second = evaluate(harness)

    assert first.decision_id == second.decision_id
    assert first.plan_id == second.plan_id
    assert len(harness.decisions.decisions) == 1
    assert len(harness.modes.open_positions()) == 1
    assert len(harness.accounting.positions()) == 1
    assert second.entry is None and "PLAN_ALREADY_ADMITTED" in second.reasons


def test_a_plan_built_on_an_old_snapshot_is_never_executed() -> None:
    harness = Harness()
    harness.ingest(spike_history())
    harness.clock.advance(timedelta(minutes=6))

    outcome = asyncio.run(_evaluate_at_now(harness))

    assert outcome.entry is None
    assert "PLAN_EXPIRED" in outcome.reasons
    assert harness.accounting.positions() == ()


async def _evaluate_at_now(harness: Harness) -> Any:
    harness.keeper.ensure_open()
    return await harness.pipeline.evaluate(POOL.address.root, harness.clock.now())


# --- APPROVAL / LIVE gateway path -------------------------------------------------------


class FakeGateway:
    def __init__(self, on_simulate: Any = None) -> None:
        self.submits = 0
        self.on_simulate = on_simulate
        self.last: SimulationResultV1 | None = None

    async def simulate(self, command: DlmmCommandV1) -> SimulationResultV1:
        if self.on_simulate is not None:
            self.on_simulate()
        self.last = bind_simulation_result(
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
            min_bin_id=command.min_bin_id,
            max_bin_id=command.max_bin_id,
            errors=(),
        )
        return self.last

    async def submit(
        self, command: DlmmCommandV1, idempotency_key: str, approval: OperatorApproval | None
    ) -> GatewayReceiptV1:
        self.submits += 1
        assert self.last is not None
        return GatewayReceiptV1(
            command_id=command.command_id,
            command_digest=self.last.command_digest,
            simulation_digest=self.last.simulation_digest,
            idempotency_key=idempotency_key,
            state=GatewayState.CONFIRMED,
            signature="sig-1",
            signatures=("sig-1",),
            transaction_count=1,
            submitted_at=NOW,
            attempt_count=1,
        )

    async def status(self, signature: str) -> ChainReceiptV1:
        return ChainReceiptV1(signature=signature, state=ChainState.CONFIRMED, checked_at=NOW)


class ClearKillSwitch:
    async def kill_switch_active(self) -> bool:
        return False


def _live_submitter(gateway: FakeGateway, harness: Harness) -> GatewaySubmitter:
    from src.execution.contracts import LivePromotionEvidence

    store = InMemoryRecoverableExecutionStore()
    resolver = CommandResolver(store)
    service = ExecutionService(
        gateway=gateway,
        store=store,
        plans=resolver,
        clock=harness.clock,
        allowed_mode=TradingMode.LIVE,
        kill_switch=ClearKillSwitch(),
        live_promotion=LivePromotionEvidence(
            promotion_id=UUID(int=1),
            ruleset_version="rules-1",
            approval_cycles=10,
            receipts_reconciled=True,
            risk_invariants_preserved=True,
            dedicated_rpc_ready=True,
            wallet_ready=True,
            checklist_completed=True,
            operator_confirmation_id="operator",
            promoted_at=NOW - timedelta(hours=1),
            expires_at=NOW + timedelta(hours=1),
        ),
    )
    book = GatewayFillBook(harness.accounting, store, harness.clock, harness.policy.shadow_costs)
    return GatewaySubmitter(service, resolver, book)


def _live_command(harness: Harness) -> tuple[DlmmCommandV1, Any]:
    harness.ingest(spike_history())
    plan_id = UUID(int=7)
    market = snapshot(NOW, short="400", long="1200")
    from src.worker.execution import shadow_address

    command = build_entry_command(
        plan_id=plan_id,
        mode=TradingMode.LIVE,
        market=market,
        size_base_units=1000,
        strategy_kind="SPOT",
        lower_bin_offset=-5,
        upper_bin_offset=5,
        expires_at=NOW + timedelta(minutes=5),
        correlation_id="00000000-0000-4000-8000-000000000001",
        ruleset_version="rules-1",
        wallet_address=WALLET,
        position_address=shadow_address(f"position:{plan_id}"),
        max_slippage_bps=100,
        anchor=BinAnchor(active_bin_id=8_000, base_decimals=6, quote_decimals=6),
    )
    return command, market


def test_kill_switch_during_live_simulation_prevents_the_gateway_submit() -> None:
    harness = Harness()
    command, market = _live_command(harness)
    killed = {"active": False}
    gateway = FakeGateway(on_simulate=lambda: killed.update(active=True))

    def guard() -> None:
        if killed["active"]:
            raise RuntimeError("KILL_SWITCH_ACTIVE")

    result = asyncio.run(_live_submitter(gateway, harness).submit(command, market, guard))

    assert result.status is SubmitStatus.BLOCKED
    assert gateway.submits == 0


def test_live_submit_happens_only_after_simulation_and_guard() -> None:
    harness = Harness()
    command, market = _live_command(harness)
    gateway = FakeGateway()

    result = asyncio.run(_live_submitter(gateway, harness).submit(command, market, lambda: None))

    assert result.status is SubmitStatus.CONFIRMED
    assert result.position_id == command.position_address
    assert gateway.submits == 1
