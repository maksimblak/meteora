"""guide-v1 hard filters: author thresholds, fee organicity and fail-closed UNKNOWN."""

from datetime import timedelta
from decimal import Decimal

import pytest

from src.domain.models.trading import TradingMode
from src.domain.screening import (
    CandidateDecision,
    CandidateStatus,
    DecisionContext,
    DecisionFlag,
    GuideScreeningRules,
    PoolScreener,
    RejectionCode,
    ScreeningPolicy,
)
from src.market.models import PoolSnapshot, TokenRiskSnapshot
from tests.domain.guide_fixtures import (
    CONFIG,
    NOW,
    RULESET,
    guide_features,
    guide_history,
    guide_token,
)


def screening_policy(*, guide: GuideScreeningRules | None = None) -> ScreeningPolicy:
    return ScreeningPolicy(
        minimum_tvl=Decimal("10000"),
        minimum_depth=Decimal("5000"),
        minimum_pool_age=timedelta(minutes=30),
        maximum_holder_concentration=Decimal("0.30"),
        maximum_price_impact=Decimal("0.01"),
        minimum_short_ratio=Decimal("3"),
        minimum_long_ratio=Decimal("1.5"),
        minimum_robust_z_score=Decimal("4"),
        minimum_fee_tvl=Decimal("0.002"),
        minimum_score=Decimal("0.5"),
        guide=guide,
    )


GUIDE = screening_policy(guide=GuideScreeningRules())
LEGACY = screening_policy()


def evaluate(
    history: list[PoolSnapshot],
    token: TokenRiskSnapshot | None = None,
    *,
    policy: ScreeningPolicy = GUIDE,
    mode: TradingMode = TradingMode.LIVE,
) -> CandidateDecision:
    return PoolScreener().evaluate(
        history[-1],
        token or guide_token(),
        guide_features(history),
        policy,
        context=DecisionContext(
            mode=mode, ruleset_version=RULESET, config_version=CONFIG, correlation_id="cycle-1"
        ),
        estimated_price_impact=Decimal("0.001"),
    )


def evidence(decision: CandidateDecision) -> dict[str, tuple[str, str | None]]:
    return {item.name: (item.value, item.threshold) for item in decision.evidence}


def test_guide_thresholds_default_to_the_author_values_of_spec_6_6() -> None:
    rules = GuideScreeningRules()

    assert rules.minimum_market_cap == Decimal("500000")
    assert rules.preferred_market_cap == Decimal("1000000")
    assert rules.minimum_token_age == timedelta(hours=2)
    assert rules.minimum_volume_per_minute == Decimal("14000")
    assert rules.overvolume_volume_per_minute == Decimal("100000")
    assert rules.minimum_volume_to_liquidity == Decimal("0.5")
    assert rules.minimum_total_fees_sol == Decimal("30")


def test_organic_working_volume_passes_and_records_every_guide_threshold() -> None:
    decision = evaluate(guide_history())
    recorded = evidence(decision)

    assert decision.status is CandidateStatus.ACCEPT
    assert decision.reasons == ()
    assert recorded["volume_regime"][0] == "WORKING"
    assert recorded["volume_per_minute"] == ("20000", "14000")
    assert recorded["market_cap"] == ("2000000", "500000")
    assert recorded["market_cap_preferred"][0] == "true"
    # G24: the hour's average must be sustained too (900 000 / 60); organicity by base fee
    # is information only (>= 1 by construction in DLMM), so it carries no threshold.
    assert recorded["hourly_volume_per_minute"] == ("15000", "14000")
    assert recorded["fee_efficiency"][1] is None
    assert recorded["total_fees_sol"] == ("45", "30")
    assert recorded["token_age_seconds"] == ("21600", "7200")  # 6h pool age bounds the token


def test_overvolume_is_distinguished_from_working_volume() -> None:
    decision = evaluate(guide_history(short="600000", tvl="1000000"))

    assert decision.status is CandidateStatus.ACCEPT
    assert evidence(decision)["volume_regime"] == (
        "OVERVOLUME",
        "working>=14000; overvolume>=100000",
    )


def test_savash_inorganic_volume_is_rejected_as_artificial_in_every_mode() -> None:
    """980k/h on 87k liquidity (11x) with total fees of 45 SOL where a 2M cap needs 120 (G24:
    the author's "fees too low for the cap"; the base-fee organicity is only recorded)."""

    history = guide_history(
        short="81000", hour="980000", hour_fees="900", tvl="87000", base_fee_rate="0.01"
    )

    live = evaluate(history)
    shadow = evaluate(history, mode=TradingMode.SHADOW)

    assert live.status is CandidateStatus.REJECT
    assert shadow.status is CandidateStatus.REJECT
    assert RejectionCode.ARTIFICIAL_VOLUME_SUSPECTED in live.reasons
    assert evidence(live)["volume_regime"][0] == "WORKING"  # the volume alone looked attractive
    assert evidence(live)["fee_efficiency"][0].startswith("0.0918")


@pytest.mark.parametrize(
    ("current", "token", "expected"),
    [
        ({"market_cap": "499999"}, guide_token(), RejectionCode.MARKET_CAP_BELOW_THRESHOLD),
        (
            {"short": "65000", "tvl": "100000"},
            guide_token(),
            RejectionCode.VOLUME_PER_MINUTE_BELOW_THRESHOLD,
        ),
        ({"tvl": "250000"}, guide_token(), RejectionCode.VOLUME_TO_LIQUIDITY_BELOW_THRESHOLD),
        ({}, guide_token(total_fees_sol="29.99"), RejectionCode.TOTAL_FEES_BELOW_THRESHOLD),
        ({}, guide_token(holder_concentration="0.31"), RejectionCode.HOLDER_CONCENTRATION_HIGH),
    ],
)
def test_each_author_threshold_rejects_with_its_own_reason(
    current: dict[str, str], token: TokenRiskSnapshot, expected: RejectionCode
) -> None:
    decision = evaluate(guide_history(**current), token)

    assert decision.status is CandidateStatus.REJECT
    assert expected in decision.reasons


def test_a_top10_share_of_exactly_30_percent_fails_the_guide() -> None:
    """Spec 6.6 asks for a top-10 share below 30%: the maximum itself already rejects, as
    for the GMGN top-10 threshold. ``shadow-hypothesis-v1`` keeps its inclusive maximum."""

    at_maximum = guide_token(holder_concentration="0.30")

    guide = evaluate(guide_history(), at_maximum)
    legacy = evaluate(guide_history(), at_maximum, policy=LEGACY)
    below = evaluate(guide_history(), guide_token(holder_concentration="0.2999"))

    assert guide.status is CandidateStatus.REJECT
    assert RejectionCode.HOLDER_CONCENTRATION_HIGH in guide.reasons
    assert RejectionCode.HOLDER_CONCENTRATION_HIGH not in legacy.reasons
    assert RejectionCode.HOLDER_CONCENTRATION_HIGH not in below.reasons


def test_longer_volume_window_does_not_weaken_the_volume_to_liquidity_threshold() -> None:
    history = guide_history(long="600000", tvl="250000")

    decision = PoolScreener().evaluate(
        history[-1],
        guide_token(),
        guide_features(history, volume_window="30m"),
        GUIDE,
        context=DecisionContext(
            mode=TradingMode.LIVE,
            ruleset_version=RULESET,
            config_version=CONFIG,
            correlation_id="cycle-1",
        ),
        estimated_price_impact=Decimal("0.001"),
    )

    assert decision.status is CandidateStatus.REJECT
    assert decision.reasons == (RejectionCode.VOLUME_TO_LIQUIDITY_BELOW_THRESHOLD,)
    assert evidence(decision)["volume_to_liquidity"] == ("0.4", "0.5")


def test_token_age_uses_pool_age_or_mint_activity_as_lower_bound() -> None:
    young_pool = NOW - timedelta(minutes=90)
    exact_young = guide_token(earliest_activity_at=young_pool, complete=True)
    old_mint_bound = guide_token(earliest_activity_at=NOW - timedelta(hours=3))

    too_young = evaluate(guide_history(created_at=young_pool), exact_young)
    unknown = evaluate(guide_history(created_at=young_pool), mode=TradingMode.SHADOW)
    proven = evaluate(guide_history(created_at=young_pool), old_mint_bound)

    assert RejectionCode.TOKEN_TOO_YOUNG in too_young.reasons
    assert unknown.status is CandidateStatus.UNKNOWN
    assert "token_age" in unknown.missing_signals
    assert proven.status is CandidateStatus.ACCEPT


def test_unavailable_guide_signal_is_flagged_in_shadow_and_closes_approval_and_live() -> None:
    history = guide_history(market_cap=None, base_fee_rate=None)

    shadow = evaluate(history, mode=TradingMode.SHADOW)
    approval = evaluate(history, mode=TradingMode.APPROVAL)
    live = evaluate(history, mode=TradingMode.LIVE)
    without_fees = evaluate(history, guide_token(total_fees_sol=None), mode=TradingMode.SHADOW)

    assert shadow.status is CandidateStatus.UNKNOWN
    assert shadow.reasons == (RejectionCode.GUIDE_SIGNAL_UNKNOWN,)
    assert "market_cap" in shadow.missing_signals
    # G24: organicity by base fee no longer decides anything, so its absence is no gap.
    assert "fee_efficiency" not in shadow.missing_signals
    assert approval.status is CandidateStatus.REJECT
    assert live.status is CandidateStatus.REJECT
    assert RejectionCode.GUIDE_SIGNAL_UNKNOWN in live.reasons
    # Absent total fees add their flag but never rescue a missing required signal.
    assert without_fees.status is CandidateStatus.UNKNOWN
    assert DecisionFlag.TOTAL_FEES_UNAVAILABLE in without_fees.flags


# G24 flags every fixture decision carries: no GMGN, so the token volume is the DLMM pool's
# own, and the fixture's 5m window spikes over its calm baseline.
SELECTION_FLAGS = {DecisionFlag.TOKEN_VOLUME_POOL_ONLY, DecisionFlag.VOLUME_SPIKE}


def test_absent_total_fees_pass_with_a_flag_instead_of_unknown() -> None:
    """D03: no source reports GMGN total fees; that alone must not close the entry."""

    token = guide_token(total_fees_sol=None)

    decisions = {
        mode: evaluate(guide_history(), token, mode=mode)
        for mode in (TradingMode.SHADOW, TradingMode.APPROVAL, TradingMode.LIVE)
    }

    for decision in decisions.values():
        assert decision.status is CandidateStatus.ACCEPT
        assert decision.reasons == ()
        assert set(decision.flags) == {DecisionFlag.TOTAL_FEES_UNAVAILABLE, *SELECTION_FLAGS}
        assert "total_fees_sol" not in decision.missing_signals
        assert evidence(decision)["total_fees_sol"] == ("UNAVAILABLE", "30")
    assert set(evaluate(guide_history()).flags) == SELECTION_FLAGS


def test_reported_total_fees_below_30_sol_reject_at_a_qualifying_market_cap() -> None:
    decision = evaluate(guide_history(market_cap="500000"), guide_token(total_fees_sol="29.99"))

    assert decision.status is CandidateStatus.REJECT
    assert decision.reasons == (RejectionCode.TOTAL_FEES_BELOW_THRESHOLD,)
    assert set(decision.flags) == SELECTION_FLAGS


def test_same_inputs_change_outcome_only_through_the_ruleset_version() -> None:
    """Golden pair: shadow-hypothesis-v1 ignores guide signals, guide-v1 fails closed on them."""

    history = guide_history(market_cap=None)
    token = guide_token(total_fees_sol=None)

    legacy = evaluate(history, token, policy=LEGACY)
    repeated = evaluate(history, token, policy=LEGACY)
    guided = evaluate(history, token, policy=GUIDE)

    assert legacy.status is CandidateStatus.ACCEPT
    assert legacy.model_dump_json() == repeated.model_dump_json()
    assert "volume_regime" not in evidence(legacy)
    assert guided.status is CandidateStatus.REJECT
    assert RejectionCode.GUIDE_SIGNAL_UNKNOWN in guided.reasons
