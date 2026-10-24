"""T19: the SHADOW cost model pays the executor's priority fee ceiling (spec 6.8, No 49)."""

from decimal import Decimal

import pytest

from src.worker.policy import (
    LEGACY_RULESET_VERSION,
    PRIORITY_FEE_MAX_ENV,
    RULESET_VERSION_ENV,
    load_pipeline_policy,
)


def test_shadow_costs_pay_the_priority_fee_ceiling_of_the_executor() -> None:
    default = load_pipeline_policy({})
    tuned = load_pipeline_policy({PRIORITY_FEE_MAX_ENV: "5000000"})
    legacy = load_pipeline_policy({RULESET_VERSION_ENV: LEGACY_RULESET_VERSION})

    # PRIORITY_FEE_MAX_LAMPORTS defaults to 0.015 SOL a transaction, as in the executor.
    assert default.shadow_costs.priority_fee_lamports == 15_000_000
    assert default.conservative_costs.priority_fee_lamports == 15_000_000
    assert tuned.shadow_costs.priority_fee_lamports == 5_000_000
    assert tuned.conservative_costs.priority_fee_lamports == 5_000_000
    # A cost of the network, not of a ruleset.
    assert legacy.shadow_costs.priority_fee_lamports == 15_000_000
    assert default.shadow_costs.slippage_bps == 50


@pytest.mark.parametrize("raw", ["abc", "-1", "1.5"])
def test_an_unreadable_priority_fee_ceiling_stops_the_start(raw: str) -> None:
    with pytest.raises(ValueError, match=PRIORITY_FEE_MAX_ENV):
        load_pipeline_policy({PRIORITY_FEE_MAX_ENV: raw})


def test_guide_v1_carries_the_liquidity_slippage_share() -> None:
    policy = load_pipeline_policy({})

    assert policy.liquidity_slippage == Decimal("0.30")
    # 70 bins: 21 may move; a two-bin range still tolerates one.
    assert policy.active_bin_tolerance_for(-69, 0) == 21
    assert policy.active_bin_tolerance_for(-1, 0) == 1
    legacy = load_pipeline_policy({RULESET_VERSION_ENV: LEGACY_RULESET_VERSION})
    assert legacy.active_bin_tolerance_for(-69, 0) == legacy.active_bin_tolerance == 3
