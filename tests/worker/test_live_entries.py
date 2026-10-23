"""D06/D08 entries on live data: snapshot anchor, fresh active bin, token legs by range."""

import asyncio
import dataclasses
from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest

from src.core.clock import FrozenClock
from src.domain.models.primitives import BaseUnits, PublicKey
from src.domain.models.trading import TradingMode
from src.domain.strategy import StrategySelectionReason
from src.execution.contracts import DlmmCommand, DlmmCommandV2
from src.market.models import DataQuality, PoolSnapshot, WalletState
from src.modes import PendingPlanStatus
from src.worker.execution import BinAnchor, build_entry_command, snapshot_anchor
from src.worker.policy import GuidePolicySettings, guide_policy
from tests.domain.guide_fixtures import guide_history
from tests.worker.harness import NOW, POOL, harness_policy, memory_repositories, snapshot
from tests.worker.test_gateway_execution import (
    ANCHOR,
    WALLET_ADDRESS,
    FakeExecutor,
    StaticPoolStates,
    funded_wallet,
    ingest,
    plan_status,
    promote,
    spike_history,
    worker,
)


def _anchored(**fields: Any) -> PoolSnapshot:
    data = snapshot(NOW).model_dump()
    data.update(fields)
    return PoolSnapshot.model_validate(data)


def test_the_snapshot_anchor_is_the_active_bin_and_decimals_the_snapshot_carries() -> None:
    anchored = _anchored(active_bin_id=-1387, base_decimals=9, quote_decimals=6)

    assert snapshot_anchor(anchored) == BinAnchor(
        active_bin_id=-1387, base_decimals=9, quote_decimals=6
    )
    assert snapshot_anchor(_anchored(active_bin_id=-1387, base_decimals=9)) is None
    assert snapshot_anchor(_anchored(base_decimals=9, quote_decimals=6)) is None
    # Decimals beyond what the execution scale supports never become an anchor.
    assert snapshot_anchor(_anchored(active_bin_id=1, base_decimals=19, quote_decimals=6)) is None


def _command(mode: TradingMode, lower: int, upper: int, *, size: int = 1_000) -> DlmmCommand:
    return build_entry_command(
        plan_id=UUID(int=11),
        mode=mode,
        market=snapshot(NOW, price="2"),
        size_base_units=size,
        strategy_kind="BID_ASK",
        lower_bin_offset=lower,
        upper_bin_offset=upper,
        expires_at=NOW + timedelta(minutes=5),
        correlation_id="cycle",
        ruleset_version="rules-1",
        wallet_address=WALLET_ADDRESS,
        max_slippage_bps=100,
        anchor=BinAnchor(active_bin_id=5_000, base_decimals=6, quote_decimals=6),
    )


@pytest.mark.parametrize("upper", [-1, 0], ids=["below-active", "ending-at-active"])
@pytest.mark.parametrize("mode", [TradingMode.SHADOW, TradingMode.LIVE])
def test_a_bid_ask_with_no_bin_above_the_active_needs_only_the_quote_token(
    mode: TradingMode, upper: int
) -> None:
    # A range that ends at the active bin funds that bin with Y only: one-sided bid.
    command = _command(mode, -50, upper)

    assert (command.amount_x, command.amount_y) == ("0", "1000")


def test_a_range_entirely_above_the_active_bin_needs_only_the_base_token() -> None:
    command = _command(TradingMode.LIVE, 1, 10)

    # 1000 quote units at price 2 are 500 base tokens.
    assert (command.amount_x, command.amount_y) == ("500", "0")


def test_a_range_through_the_active_bin_splits_by_the_bins_on_each_side() -> None:
    symmetric = _command(TradingMode.LIVE, -5, 5)
    skewed = _command(TradingMode.LIVE, -52, 3)

    assert (symmetric.amount_x, symmetric.amount_y) == ("250", "500")
    # 52 bins below, 3 above, the active bin shared: Y holds 52.5 / 56 of the capital.
    assert skewed.amount_y == "937"
    assert skewed.amount_x == str((1_000 - 937) // 2)


def live_runtime(**options: Any) -> tuple[Any, FakeExecutor, Any]:
    clock = FrozenClock(NOW)
    repos = memory_repositories(clock)
    executor = FakeExecutor(clock)
    promote(repos, TradingMode.LIVE)
    ingest(repos, *spike_history())
    runtime = worker(repos, clock, executor, TradingMode.LIVE, **options)
    return runtime, executor, repos


def evaluate(runtime: Any) -> Any:
    async def run() -> Any:
        runtime.keeper.ensure_open()
        return await runtime.pipeline.evaluate(POOL.address.root, NOW)

    return asyncio.run(run())


def test_a_live_entry_is_anchored_on_the_freshly_read_active_bin() -> None:
    states = StaticPoolStates(ANCHOR.active_bin_id + 2)
    runtime, executor, _ = live_runtime(pool_states=states)

    outcome = evaluate(runtime)

    command, _ = executor.simulated[0]
    assert outcome.entry is not None and outcome.entry.position_id is not None
    assert states.reads == 1
    assert isinstance(command, DlmmCommandV2)
    assert command.active_bin_id == ANCHOR.active_bin_id + 2


@pytest.mark.parametrize(
    ("states", "reason"),
    [
        (StaticPoolStates(ANCHOR.active_bin_id + 4), "ACTIVE_BIN_DRIFT"),
        (StaticPoolStates(None), "ACTIVE_BIN_UNAVAILABLE"),
        (StaticPoolStates(ANCHOR.active_bin_id, bin_step=50), "POOL_STATE_MISMATCH"),
    ],
    ids=["drift", "unread", "other-pair"],
)
def test_a_stale_or_unread_active_bin_cancels_the_entry_before_the_executor(
    states: StaticPoolStates, reason: str
) -> None:
    runtime, executor, repos = live_runtime(pool_states=states)

    outcome = evaluate(runtime)

    assert outcome.entry is not None and outcome.entry.reason == reason
    assert plan_status(repos, outcome.plan_id) is PendingPlanStatus.CANCELLED
    assert executor.simulated == []


def test_the_active_bin_tolerance_comes_from_the_policy() -> None:
    policy = dataclasses.replace(harness_policy(), active_bin_tolerance=5)
    runtime, executor, _ = live_runtime(
        pool_states=StaticPoolStates(ANCHOR.active_bin_id + 4), policy=policy
    )

    evaluate(runtime)

    command, _ = executor.simulated[0]
    assert isinstance(command, DlmmCommandV2) and command.active_bin_tolerance == 5


def _wallet(base: int) -> WalletState:
    return WalletState(
        wallet=PublicKey(WALLET_ADDRESS),
        native_balance=BaseUnits(10**15),
        token_balances={POOL.base_mint.root: BaseUnits(base)},
        observed_at=NOW,
        source_at=NOW,
        ingested_at=NOW,
        data_quality=DataQuality.COMPLETE,
    )


def test_an_entry_needing_a_token_the_wallet_does_not_hold_is_refused_with_its_reason() -> None:
    # The executor never swaps (D08): the base leg must already be in the wallet.
    runtime, executor, repos = live_runtime(wallet=_wallet(0))

    outcome = evaluate(runtime)

    assert outcome.entry is not None and outcome.entry.reason == "WALLET_INSUFFICIENT_BASE_TOKEN"
    assert plan_status(repos, outcome.plan_id) is PendingPlanStatus.FAILED
    assert executor.simulated == []


def test_without_a_wallet_state_a_live_entry_is_not_sent() -> None:
    runtime, executor, _ = live_runtime(wallet=None)

    outcome = evaluate(runtime)

    assert outcome.entry is not None and outcome.entry.reason == "WALLET_STATE_UNAVAILABLE"
    assert executor.simulated == []


def test_a_spot_through_the_active_bin_opens_when_the_wallet_holds_both_tokens() -> None:
    runtime, executor, _ = live_runtime(wallet=funded_wallet())

    outcome = evaluate(runtime)

    assert outcome.entry is not None and outcome.entry.position_id is not None
    assert len(executor.submitted) == 1
    assert Decimal(executor.submitted[0].amount_x or "0") > 0


def test_the_default_guide_bid_ask_enters_with_quote_only_from_a_sol_only_wallet() -> None:
    """Seam StrategyPlanner.plan -> EntryExecutor: the guide-v1 default plan (Bid-Ask from
    the local high) asks the wallet for no base token, so SOL alone opens it (D08)."""

    clock = FrozenClock(NOW)
    repos = memory_repositories(clock)
    executor = FakeExecutor(clock)
    promote(repos, TradingMode.APPROVAL)
    ingest(repos, *guide_history())
    policy = guide_policy(
        GuidePolicySettings(baseline_points=6, minimum_baseline_points=6, maximum_history_points=20)
    )
    sol_only = WalletState(
        wallet=PublicKey(WALLET_ADDRESS),
        native_balance=BaseUnits(10**15),
        token_balances={},
        observed_at=NOW,
        source_at=NOW,
        ingested_at=NOW,
        data_quality=DataQuality.COMPLETE,
    )
    runtime = worker(
        repos,
        clock,
        executor,
        TradingMode.APPROVAL,
        policy=policy,
        pool_states=StaticPoolStates(ANCHOR.active_bin_id, bin_step=250),
        wallet=sol_only,
    )

    outcome = evaluate(runtime)

    plan = repos.decisions.plans[outcome.plan_id].sized_plan.plan
    assert plan.selection_reason is StrategySelectionReason.BID_ASK_FROM_LOCAL_HIGH
    assert plan.upper_bin_offset == 0
    assert outcome.entry is not None and outcome.entry.reason == "AWAITING_APPROVAL"
    ((command, _),) = executor.simulated
    assert isinstance(command, DlmmCommandV2)
    assert command.amount_x == "0" and int(command.amount_y or "0") > 0
    assert command.max_bin_id == ANCHOR.active_bin_id
