"""guide-v1 GMGN thresholds (spec 6.7, G18): separate reject codes, flags and LIVE closure."""

from datetime import timedelta
from decimal import Decimal

import pytest

from src.api.presenters import flag_notices, gmgn_metrics
from src.domain.models.trading import TradingMode
from src.domain.screening import (
    CandidateDecision,
    CandidateStatus,
    DecisionContext,
    DecisionFlag,
    GmgnScreeningRules,
    GuideScreeningRules,
    PoolScreener,
    RejectionCode,
    ScreeningPolicy,
    merge_enhanced_signals,
)
from src.market.models import EnhancedTokenSignals, Freshness, TokenRiskSnapshot
from tests.domain.guide_fixtures import (
    CONFIG,
    NOW,
    RULESET,
    guide_features,
    guide_history,
    guide_token,
)


def policy(*, gmgn: GmgnScreeningRules | None = None) -> ScreeningPolicy:
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
        guide=GuideScreeningRules(gmgn=gmgn),
    )


GMGN = policy(gmgn=GmgnScreeningRules())
OPTIONAL_FOR_LIVE = policy(gmgn=GmgnScreeningRules(required_for_live=False))


def reading(
    *,
    insiders: str | None = "0.02",
    bundling: str | None = "0.10",
    phishing: str | None = "0.05",
    top10: str | None = "0.15",
    developer: str | None = "0",
    total_fees: str | None = "45",
    freshness: Freshness = Freshness.FRESH,
    detail: str | None = None,
) -> EnhancedTokenSignals:
    def number(value: str | None) -> Decimal | None:
        return None if value is None else Decimal(value)

    return EnhancedTokenSignals(
        insiders_share=number(insiders),
        bundling_share=number(bundling),
        phishing_share=number(phishing),
        top10_holder_share=number(top10),
        developer_share=number(developer),
        total_fees_sol=number(total_fees),
        provider="gmgn",
        source_at=NOW - timedelta(minutes=1),
        freshness=freshness,
        detail=detail,
    )


def token_with(signals: EnhancedTokenSignals | None) -> TokenRiskSnapshot:
    base = guide_token(total_fees_sol=None)
    return base if signals is None else merge_enhanced_signals(base, signals)


def unavailable(detail: str = "CHALLENGE_TIMEOUT") -> EnhancedTokenSignals:
    return reading(
        insiders=None,
        bundling=None,
        phishing=None,
        top10=None,
        developer=None,
        total_fees=None,
        freshness=Freshness.STALE,
        detail=detail,
    )


def evaluate(
    token: TokenRiskSnapshot,
    *,
    mode: TradingMode = TradingMode.LIVE,
    rules: ScreeningPolicy = GMGN,
) -> CandidateDecision:
    history = guide_history()
    return PoolScreener().evaluate(
        history[-1],
        token,
        guide_features(history),
        rules,
        context=DecisionContext(
            mode=mode, ruleset_version=RULESET, config_version=CONFIG, correlation_id="cycle-1"
        ),
        estimated_price_impact=Decimal("0.001"),
    )


def evidence(decision: CandidateDecision) -> dict[str, tuple[str, str | None]]:
    return {item.name: (item.value, item.threshold) for item in decision.evidence}


def test_gmgn_thresholds_default_to_the_guide_checklist_of_spec_6_7() -> None:
    rules = GmgnScreeningRules()

    assert rules.maximum_insiders_share == Decimal("0.10")
    assert rules.maximum_bundling_share == Decimal("0.70")
    assert rules.maximum_phishing_share == Decimal("0.30")
    assert rules.maximum_top10_holder_share == Decimal("0.30")
    assert rules.required_for_live is True


def test_a_clean_fresh_reading_passes_live_and_every_metric_reaches_the_record() -> None:
    decision = evaluate(token_with(reading()))
    found = evidence(decision)

    assert decision.status is CandidateStatus.ACCEPT
    assert decision.reasons == ()
    assert decision.flags == ()
    assert found["gmgn_status"] == ("FRESH", None)
    assert found["gmgn_source_at"][0] == (NOW - timedelta(minutes=1)).isoformat()
    assert found["insiders_share"] == ("0.02", "0.10")
    assert found["bundling_share"] == ("0.10", "0.70")
    assert found["phishing_share"] == ("0.05", "0.30")
    assert found["gmgn_top10_holder_share"] == ("0.15", "0.30")
    assert found["developer_share"] == ("0", "0")
    # GMGN total fees feed the conditional D03 threshold (>= 30 SOL).
    assert found["total_fees_sol"] == ("45", "30")


@pytest.mark.parametrize(
    ("metric", "at_threshold", "below", "code"),
    [
        ("insiders", "0.10", "0.0999", RejectionCode.INSIDERS_SHARE_HIGH),
        ("bundling", "0.70", "0.6999", RejectionCode.BUNDLING_SHARE_HIGH),
        ("phishing", "0.30", "0.2999", RejectionCode.PHISHING_SHARE_HIGH),
        ("top10", "0.30", "0.2999", RejectionCode.GMGN_TOP10_HOLDER_SHARE_HIGH),
    ],
)
def test_each_gmgn_threshold_rejects_from_the_guide_value_with_its_own_code(
    metric: str, at_threshold: str, below: str, code: RejectionCode
) -> None:
    # The guide asks for "< X%": the threshold value itself already fails.
    failing = evaluate(token_with(reading(**{metric: at_threshold})), mode=TradingMode.SHADOW)
    passing = evaluate(token_with(reading(**{metric: below})), mode=TradingMode.SHADOW)

    assert failing.status is CandidateStatus.REJECT
    assert failing.reasons == (code,)
    assert passing.status is CandidateStatus.ACCEPT
    assert passing.reasons == ()


def test_reported_total_fees_below_30_sol_reject_through_the_d03_threshold() -> None:
    decision = evaluate(token_with(reading(total_fees="12.5")))

    assert decision.status is CandidateStatus.REJECT
    assert decision.reasons == (RejectionCode.TOTAL_FEES_BELOW_THRESHOLD,)
    assert DecisionFlag.TOTAL_FEES_UNAVAILABLE not in decision.flags


def test_a_developer_share_is_a_flag_in_the_record_never_a_reject() -> None:
    decision = evaluate(token_with(reading(developer="0.035")))

    assert decision.status is CandidateStatus.ACCEPT
    assert decision.reasons == ()
    assert decision.flags == (DecisionFlag.DEVELOPER_HOLDS_TOKENS,)
    assert evidence(decision)["developer_share"] == ("0.035", "0")


@pytest.mark.parametrize(
    "signals",
    [unavailable(), reading(freshness=Freshness.STALE)],
    ids=["challenge", "stale"],
)
def test_without_fresh_gmgn_data_shadow_and_approval_go_on_with_the_flag(
    signals: EnhancedTokenSignals,
) -> None:
    token = token_with(signals)

    for mode in (TradingMode.SHADOW, TradingMode.APPROVAL):
        decision = evaluate(token, mode=mode)
        assert decision.status is CandidateStatus.ACCEPT
        assert DecisionFlag.GMGN_UNAVAILABLE in decision.flags
        assert decision.reasons == ()
    # Stale numbers are not applied as if they were fresh.
    assert evidence(evaluate(token, mode=TradingMode.SHADOW))["insiders_share"][0] == (
        "UNAVAILABLE"
    )


def test_without_fresh_gmgn_data_live_is_closed_until_the_requirement_is_switched_off() -> None:
    token = token_with(unavailable("CAPTCHA"))

    closed = evaluate(token, mode=TradingMode.LIVE)
    opened = evaluate(token, mode=TradingMode.LIVE, rules=OPTIONAL_FOR_LIVE)

    assert closed.status is CandidateStatus.REJECT
    assert RejectionCode.GMGN_UNAVAILABLE in closed.reasons
    assert DecisionFlag.GMGN_UNAVAILABLE in closed.flags
    assert evidence(closed)["gmgn_status"][0] == "UNAVAILABLE:CAPTCHA"
    assert opened.status is CandidateStatus.ACCEPT
    assert opened.reasons == ()
    assert DecisionFlag.GMGN_UNAVAILABLE in opened.flags


def test_a_reading_missing_one_metric_still_applies_the_rest_and_is_flagged() -> None:
    """A changed GMGN format degrades the reading; it never turns into "all clear"."""

    missing_bundling = token_with(reading(bundling=None))
    insider_heavy = token_with(reading(bundling=None, insiders="0.25"))

    shadow = evaluate(missing_bundling, mode=TradingMode.SHADOW)
    live = evaluate(missing_bundling, mode=TradingMode.LIVE)
    rejected = evaluate(insider_heavy, mode=TradingMode.SHADOW)

    assert shadow.status is CandidateStatus.ACCEPT
    assert DecisionFlag.GMGN_UNAVAILABLE in shadow.flags
    assert evidence(shadow)["bundling_share"] == ("UNAVAILABLE", "0.70")
    assert live.status is CandidateStatus.REJECT
    assert live.reasons == (RejectionCode.GMGN_UNAVAILABLE,)
    assert rejected.reasons == (RejectionCode.INSIDERS_SHARE_HIGH,)


def test_a_token_gmgn_never_read_is_flagged_unavailable_in_every_mode() -> None:
    """No GMGN reading (service not wired, the compose default) is never "all clear":
    every mode carries the flag; SHADOW and APPROVAL keep their status, LIVE is closed."""

    token = token_with(None)

    for mode in (TradingMode.SHADOW, TradingMode.APPROVAL):
        without_rules = evaluate(token, mode=mode, rules=policy())
        decision = evaluate(token, mode=mode)
        assert decision.status is without_rules.status is CandidateStatus.ACCEPT
        assert decision.reasons == without_rules.reasons == ()
        assert decision.flags == (
            DecisionFlag.TOTAL_FEES_UNAVAILABLE,
            DecisionFlag.GMGN_UNAVAILABLE,
        )
        assert evidence(decision)["gmgn_status"] == ("NOT_CONFIGURED", None)
    live = evaluate(token, mode=TradingMode.LIVE)
    assert live.status is CandidateStatus.REJECT
    assert live.reasons == (RejectionCode.GMGN_UNAVAILABLE,)
    assert DecisionFlag.GMGN_UNAVAILABLE in live.flags
    assert evidence(live)["gmgn_status"] == ("NOT_CONFIGURED", None)
    released = evaluate(token, mode=TradingMode.LIVE, rules=OPTIONAL_FOR_LIVE)
    assert released.status is CandidateStatus.ACCEPT
    assert DecisionFlag.GMGN_UNAVAILABLE in released.flags


def test_the_merged_token_keeps_its_own_risk_and_carries_the_reading() -> None:
    base = guide_token(total_fees_sol=None)
    signals = reading()

    merged = merge_enhanced_signals(base, signals)

    assert merged.enhanced == signals
    assert merged.total_fees_sol == Decimal("45")
    # The RPC top-10 is cross-checked, not replaced, by the GMGN figure.
    assert merged.holder_concentration == base.holder_concentration
    assert (merged.freshness, merged.source_at, merged.missing_signals) == (
        base.freshness,
        base.source_at,
        base.missing_signals,
    )
    assert merged.provider == f"{base.provider}+gmgn"
    # A stale reading is recorded but its numbers fill nothing.
    assert merge_enhanced_signals(base, reading(freshness=Freshness.STALE)).total_fees_sol is None


def test_the_operator_panel_recognizes_every_gmgn_metric_and_the_flag_by_name() -> None:
    decision = evaluate(token_with(unavailable()), mode=TradingMode.SHADOW)

    shown = {item.name for item in gmgn_metrics(decision.evidence)}
    notices = {notice.code: notice.message for notice in flag_notices(["GMGN_UNAVAILABLE"])}

    assert {
        "gmgn_status",
        "gmgn_source_at",
        "insiders_share",
        "bundling_share",
        "phishing_share",
        "gmgn_top10_holder_share",
        "developer_share",
        "total_fees_sol",
    } <= shown
    assert "GMGN_UNAVAILABLE" in {flag.value for flag in decision.flags}
    assert notices["GMGN_UNAVAILABLE"] == "GMGN недоступен — проверь вручную"
