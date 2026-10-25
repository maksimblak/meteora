"""T22: what a fresh Jupiter reading and a RugCheck report decide in guide-v1 screening.

Volumes in the comments: a 5m figure over 5 minutes, an hourly one over 60, against the
guide-v1 minimum of 14 000 a minute for both.
"""

from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest

from src.domain.models.trading import TradingMode
from src.domain.screening import (
    CandidateDecision,
    CandidateStatus,
    DecisionFlag,
    GmgnScreeningRules,
    GuideScreeningRules,
    RejectionCode,
    TokenVolumeSource,
    VolumeIntegrityMode,
    merge_enhanced_signals,
    merge_token_sources,
)
from src.market.models import (
    AuthorityStatus,
    DataQuality,
    EnhancedTokenSignals,
    Freshness,
    JupiterTokenReading,
    OrganicScoreLabel,
    PoolSnapshot,
    RiskSignal,
    RugCheckReading,
    TokenRiskSnapshot,
)
from tests.domain.guide_fixtures import NOW, POOL, guide_history, guide_token
from tests.domain.test_live_screening import LIVE_SIGNALS, evaluate, wash_unknown, without_counts

WITH_GMGN = LIVE_SIGNALS.model_copy(
    update={"guide": GuideScreeningRules(gmgn=GmgnScreeningRules(required_for_live=False))}
)


def thin_pool() -> list[PoolSnapshot]:
    """DLMM alone: 300 000 in the hour, 5 000 a minute - not sustained (the 5m spikes)."""

    return without_counts(guide_history(hour="300000"))


def jupiter(**fields: Any) -> JupiterTokenReading:
    values: dict[str, Any] = {
        "mint": POOL.base_mint,
        "source_at": NOW - timedelta(seconds=30),
        "freshness": Freshness.FRESH,
        # 100 000 in 5m and 1 080 000 in the hour: 20 000 and 18 000 a minute.
        "volume_5m_usd": Decimal("100000"),
        "volume_1h_usd": Decimal("1080000"),
        "trades_5m": 400,
        "traders_5m": 150,
        "organic_score": Decimal("81.5"),
        "organic_score_label": OrganicScoreLabel.HIGH,
        "mint_authority": AuthorityStatus.REVOKED,
        "freeze_authority": AuthorityStatus.REVOKED,
        "top_holders_share": Decimal("0.18"),
    }
    values.update(fields)
    return JupiterTokenReading.model_validate(values)


def rugcheck(*danger: str, **fields: Any) -> RugCheckReading:
    values: dict[str, Any] = {
        "mint": POOL.base_mint,
        "source_at": NOW - timedelta(seconds=10),
        "freshness": Freshness.FRESH,
        "score": 1,
        "danger": danger,
        "warnings": ("Mutable metadata",),
    }
    values.update(fields)
    return RugCheckReading.model_validate(values)


def with_sources(
    token: TokenRiskSnapshot | None = None,
    *,
    reading: JupiterTokenReading | None = None,
    report: RugCheckReading | None = None,
) -> TokenRiskSnapshot:
    return merge_token_sources(token or wash_unknown(), jupiter=reading, rugcheck=report)


def evidence(decision: CandidateDecision) -> dict[str, str]:
    return {item.name: item.value for item in decision.evidence}


# --- token volume and market cap ----------------------------------------------------------


def test_jupiters_token_volume_decides_where_the_dlmm_pools_alone_fall_short() -> None:
    history = thin_pool()

    pools_only = evaluate(history, wash_unknown(), mode=TradingMode.SHADOW)
    read = evaluate(history, with_sources(reading=jupiter()), mode=TradingMode.SHADOW)

    assert RejectionCode.HOURLY_VOLUME_PER_MINUTE_BELOW_THRESHOLD in pools_only.reasons
    assert DecisionFlag.TOKEN_VOLUME_POOL_ONLY in pools_only.flags
    assert read.status is CandidateStatus.ACCEPT, read.reasons
    found = evidence(read)
    assert found["token_volume_source"] == TokenVolumeSource.JUPITER
    assert (found["volume_per_minute"], found["hourly_volume_per_minute"]) == ("20000", "18000")
    assert DecisionFlag.TOKEN_VOLUME_POOL_ONLY not in read.flags


def test_a_fresh_gmgn_figure_still_comes_before_jupiters() -> None:
    gmgn = EnhancedTokenSignals(
        provider="gmgn-openapi",
        source_at=NOW - timedelta(minutes=1),
        freshness=Freshness.FRESH,
        # 50 000 in 5m and 600 000 in the hour: 10 000 a minute, below the minimum.
        volume_5m_usd=Decimal("50000"),
        volume_1h_usd=Decimal("600000"),
        market_cap_usd=Decimal("3000000"),
    )
    token = with_sources(
        merge_enhanced_signals(wash_unknown(), gmgn),
        reading=jupiter(market_cap_usd=Decimal("400000")),
    )

    decision = evaluate(
        thin_pool(), token, mode=TradingMode.SHADOW, policy=WITH_GMGN
    )

    found = evidence(decision)
    assert (found["token_volume_source"], found["market_cap_source"]) == ("GMGN", "GMGN")
    assert RejectionCode.VOLUME_PER_MINUTE_BELOW_THRESHOLD in decision.reasons
    assert RejectionCode.MARKET_CAP_BELOW_THRESHOLD not in decision.reasons


def test_jupiters_cap_replaces_the_data_apis() -> None:
    # The Data API says 2 000 000; Jupiter, by circulating supply, 400 000 - below 500 000.
    token = with_sources(reading=jupiter(market_cap_usd=Decimal("400000")))

    decision = evaluate(without_counts(guide_history()), token, mode=TradingMode.SHADOW)

    assert evidence(decision)["market_cap_source"] == "JUPITER"
    assert RejectionCode.MARKET_CAP_BELOW_THRESHOLD in decision.reasons


def test_a_stale_reading_decides_nothing_and_says_why() -> None:
    stale = JupiterTokenReading(
        mint=POOL.base_mint,
        source_at=NOW - timedelta(seconds=5),
        freshness=Freshness.STALE,
        detail="RATE_LIMITED",
    )

    decision = evaluate(
        thin_pool(),
        with_sources(reading=stale),
        mode=TradingMode.SHADOW,
    )

    found = evidence(decision)
    assert found["token_volume_source"] == TokenVolumeSource.DLMM_POOLS
    assert found["jupiter_status"] == "UNAVAILABLE:RATE_LIMITED"
    assert DecisionFlag.TOKEN_VOLUME_POOL_ONLY in decision.flags
    assert DecisionFlag.VOLUME_PARTICIPANTS_UNAVAILABLE in decision.flags


def test_the_first_pool_jupiter_names_bounds_the_tokens_age_from_below() -> None:
    # The pool is 6 hours old; Jupiter's first pool of the token, 3 days.
    token = with_sources(reading=jupiter(first_pool_at=NOW - timedelta(days=3)))

    decision = evaluate(without_counts(guide_history()), token, mode=TradingMode.SHADOW)

    assert evidence(decision)["token_age_seconds"] == str(3 * 24 * 3600)


# --- artificial volume ---------------------------------------------------------------------


@pytest.mark.parametrize("mode", list(TradingMode))
def test_a_low_organic_grade_is_suspected_artificial_volume_in_every_mode(
    mode: TradingMode,
) -> None:
    token = with_sources(
        reading=jupiter(organic_score=Decimal(0), organic_score_label=OrganicScoreLabel.LOW)
    )

    decision = evaluate(without_counts(guide_history()), token, mode=mode)

    assert decision.status is CandidateStatus.REJECT
    assert RejectionCode.ARTIFICIAL_VOLUME_SUSPECTED in decision.reasons
    assert evidence(decision)["jupiter_organic_score_label"] == "low"


def test_jupiters_trades_and_traders_stand_in_for_the_pools_missing_counts() -> None:
    history = without_counts(guide_history())

    blind = evaluate(history, wash_unknown(), mode=TradingMode.LIVE)
    counted = evaluate(history, with_sources(reading=jupiter()), mode=TradingMode.LIVE)

    assert RejectionCode.VOLUME_PARTICIPANTS_UNAVAILABLE in blind.reasons
    assert DecisionFlag.VOLUME_PARTICIPANTS_UNAVAILABLE not in counted.flags
    assert counted.status is CandidateStatus.ACCEPT, counted.reasons
    assert (evidence(counted)["jupiter_trades_5m"], evidence(counted)["jupiter_traders_5m"]) == (
        "400",
        "150",
    )


def test_a_handful_of_wallets_making_the_whole_volume_is_artificial() -> None:
    # 300 trades by 3 wallets in 5 minutes: 100 each, over the 20 allowed below 5 wallets.
    token = with_sources(reading=jupiter(trades_5m=300, traders_5m=3))

    decision = evaluate(without_counts(guide_history()), token, mode=TradingMode.SHADOW)

    assert RejectionCode.ARTIFICIAL_VOLUME_CONFIRMED in decision.reasons


def test_the_pools_own_counts_still_come_first() -> None:
    # The pool reports 400 trades by 150 wallets; Jupiter's figures are not used then.
    token = with_sources(guide_token(), reading=jupiter(trades_5m=300, traders_5m=3))

    decision = evaluate(guide_history(), token, mode=TradingMode.SHADOW)

    assert RejectionCode.ARTIFICIAL_VOLUME_CONFIRMED not in decision.reasons


# --- authorities and the developer ---------------------------------------------------------


def test_jupiter_fills_unknown_authorities_and_a_live_one_rejects_at_once() -> None:
    unknown = TokenRiskSnapshot.model_validate(
        {
            **wash_unknown().model_dump(),
            "mint_authority": AuthorityStatus.UNKNOWN,
            "freeze_authority": AuthorityStatus.UNKNOWN,
            "missing_signals": ("wash_trading", "mint_authority", "freeze_authority"),
        }
    )

    filled = with_sources(unknown, reading=jupiter())
    live = with_sources(unknown, reading=jupiter(mint_authority=AuthorityStatus.PRESENT))

    assert (filled.mint_authority, filled.freeze_authority) == (
        AuthorityStatus.REVOKED,
        AuthorityStatus.REVOKED,
    )
    assert filled.missing_signals == ("wash_trading",)
    assert filled.provider == "solana_rpc+jupiter"
    decision = evaluate(without_counts(guide_history()), live, mode=TradingMode.SHADOW)
    assert RejectionCode.MINT_AUTHORITY_PRESENT in decision.reasons


def test_the_more_dangerous_authority_reading_wins() -> None:
    # RPC: revoked; Jupiter: present. Danger wins, as with the Data API's own flags.
    token = with_sources(guide_token(), reading=jupiter(freeze_authority=AuthorityStatus.PRESENT))

    assert token.freeze_authority is AuthorityStatus.PRESENT


def test_a_reading_of_another_mint_is_not_attached() -> None:
    other = jupiter(mint=POOL.quote_mint)

    assert with_sources(reading=other).jupiter is None


@pytest.mark.parametrize(
    ("mode", "rejects"),
    [(TradingMode.SHADOW, False), (TradingMode.APPROVAL, False), (TradingMode.LIVE, True)],
)
def test_jupiters_developer_share_flags_without_gmgn_and_closes_live(
    mode: TradingMode, rejects: bool
) -> None:
    token = with_sources(reading=jupiter(developer_share=Decimal("0.0015")))

    decision = evaluate(without_counts(guide_history()), token, mode=mode)

    assert DecisionFlag.DEVELOPER_HOLDS_TOKENS in decision.flags
    assert (RejectionCode.DEVELOPER_HOLDS_TOKENS in decision.reasons) is rejects


def test_gmgns_developer_share_decides_when_gmgn_reports_one() -> None:
    gmgn = EnhancedTokenSignals(
        provider="gmgn-openapi",
        source_at=NOW - timedelta(minutes=1),
        freshness=Freshness.FRESH,
        developer_share=Decimal("0"),
    )
    token = with_sources(
        merge_enhanced_signals(wash_unknown(), gmgn),
        reading=jupiter(developer_share=Decimal("0.0015")),
    )

    decision = evaluate(
        without_counts(guide_history()), token, mode=TradingMode.SHADOW, policy=WITH_GMGN
    )

    assert DecisionFlag.DEVELOPER_HOLDS_TOKENS not in decision.flags


# --- RugCheck -----------------------------------------------------------------------------


@pytest.mark.parametrize("mode", list(TradingMode))
def test_a_rugcheck_danger_rejects_in_every_mode(mode: TradingMode) -> None:
    token = with_sources(
        reading=jupiter(),
        report=rugcheck("Single holder ownership", "Large Amount of LP Unlocked"),
    )

    decision = evaluate(without_counts(guide_history()), token, mode=mode)

    assert decision.status is CandidateStatus.REJECT
    assert RejectionCode.RUGCHECK_DANGER in decision.reasons
    assert evidence(decision)["rugcheck_danger"] == (
        "Single holder ownership; Large Amount of LP Unlocked"
    )


def test_warnings_alone_pass() -> None:
    token = with_sources(reading=jupiter(), report=rugcheck())

    decision = evaluate(without_counts(guide_history()), token, mode=TradingMode.LIVE)

    assert decision.status is CandidateStatus.ACCEPT, decision.reasons
    found = evidence(decision)
    assert (found["rugcheck_status"], found["rugcheck_danger"]) == ("FRESH", "none")
    assert found["rugcheck_warnings"] == "Mutable metadata"


def test_an_unavailable_report_is_a_flag_that_closes_nothing() -> None:
    report = RugCheckReading(
        mint=POOL.base_mint,
        source_at=NOW - timedelta(seconds=5),
        freshness=Freshness.STALE,
        detail="API_TIMEOUT",
    )
    token = with_sources(reading=jupiter(), report=report)

    decision = evaluate(without_counts(guide_history()), token, mode=TradingMode.LIVE)

    assert DecisionFlag.RUGCHECK_UNAVAILABLE in decision.flags
    assert decision.status is CandidateStatus.ACCEPT, decision.reasons
    assert evidence(decision)["rugcheck_status"] == "UNAVAILABLE:API_TIMEOUT"


def test_neither_source_asked_is_shown_and_changes_nothing() -> None:
    decision = evaluate(guide_history(), guide_token(), mode=TradingMode.LIVE)

    found = evidence(decision)
    assert (found["jupiter_status"], found["rugcheck_status"]) == ("NOT_READ", "NOT_READ")
    assert decision.status is CandidateStatus.ACCEPT, decision.reasons


def test_the_sources_move_the_tokens_ingestion_up_to_their_readings() -> None:
    minute_ago = NOW - timedelta(minutes=1)
    early = guide_token().model_copy(
        update={"observed_at": minute_ago, "source_at": minute_ago, "ingested_at": minute_ago}
    )

    token = with_sources(early, reading=jupiter(), report=rugcheck())

    assert token.ingested_at == NOW - timedelta(seconds=10)
    assert token.data_quality is DataQuality.COMPLETE
    assert token.wash_trading is RiskSignal.SAFE


def test_the_hypothesis_ruleset_ignores_the_sources() -> None:
    # shadow-hypothesis-v1: no guide rules, participant counts of the pool mandatory.
    frozen = LIVE_SIGNALS.model_copy(
        update={"guide": None, "volume_integrity": VolumeIntegrityMode.PARTICIPANTS}
    )
    token = with_sources(
        guide_token(),
        reading=jupiter(organic_score_label=OrganicScoreLabel.LOW, trades_5m=300, traders_5m=3),
        report=rugcheck("Single holder ownership"),
    )

    decision = evaluate(guide_history(), token, mode=TradingMode.SHADOW, policy=frozen)

    assert {
        RejectionCode.RUGCHECK_DANGER,
        RejectionCode.ARTIFICIAL_VOLUME_SUSPECTED,
        RejectionCode.ARTIFICIAL_VOLUME_CONFIRMED,
    }.isdisjoint(decision.reasons)
    assert "jupiter_status" not in evidence(decision)
