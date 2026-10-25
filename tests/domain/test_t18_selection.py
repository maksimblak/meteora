"""T18 (G24): selection as the author does it - sustained token volume, token metrics,
artificial volume against the cap, the theme, the developer, snipers and revivals.

Every expected number is worked out by hand from the inputs in the comment next to it.
"""

from datetime import timedelta
from decimal import Decimal

import pytest

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
    TokenSelection,
    merge_enhanced_signals,
)
from src.market.models import (
    DeveloperStatus,
    EnhancedTokenSignals,
    Freshness,
    PoolSnapshot,
    RiskSignal,
    TokenRiskSnapshot,
)
from src.worker.policy import GuidePolicySettings, guide_policy
from tests.domain.guide_fixtures import (
    CONFIG,
    NOW,
    RULESET,
    guide_features,
    guide_history,
    guide_snapshot,
    guide_token,
)


def policy(guide: GuideScreeningRules | None = None) -> ScreeningPolicy:
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
        guide=guide or GuideScreeningRules(),
    )


GUIDE = policy()
GMGN = policy(GuideScreeningRules(gmgn=GmgnScreeningRules()))


def screen(
    history: list[PoolSnapshot],
    token: TokenRiskSnapshot | None = None,
    *,
    rules: ScreeningPolicy = GUIDE,
    mode: TradingMode = TradingMode.LIVE,
    selection: TokenSelection | None = None,
) -> CandidateDecision:
    return PoolScreener().evaluate(
        history[-1],
        token or guide_token(),
        guide_features(history),
        rules,
        context=DecisionContext(
            mode=mode, ruleset_version=RULESET, config_version=CONFIG, correlation_id="cycle-1"
        ),
        estimated_price_impact=Decimal("0.001"),
        selection=selection,
    )


def evidence(decision: CandidateDecision) -> dict[str, tuple[str, str | None]]:
    return {item.name: (item.value, item.threshold) for item in decision.evidence}


def components(decision: CandidateDecision) -> dict[str, Decimal]:
    return {item.name: item.normalized_value for item in decision.score_components}


def steady_history(*, short: str, long: str, hour: str, **current: object) -> list[PoolSnapshot]:
    """Seven 5-minute observations of one unchanged runner: no new burst at ``NOW``."""

    return [
        guide_snapshot(
            NOW - timedelta(minutes=5 * (6 - index)),
            short=short,
            long=long,
            hour=hour,
            **(current if index == 6 else {}),  # type: ignore[arg-type]
        )
        for index in range(7)
    ]


def reading(**fields: object) -> EnhancedTokenSignals:
    values: dict[str, object] = {
        "insiders_share": Decimal("0.02"),
        "bundling_share": Decimal("0.10"),
        "phishing_share": Decimal("0.05"),
        "top10_holder_share": Decimal("0.15"),
        "developer_share": Decimal("0"),
        "total_fees_sol": Decimal("45"),
        "provider": "gmgn-openapi",
        "source_at": NOW - timedelta(minutes=1),
        "freshness": Freshness.FRESH,
    }
    values.update(fields)
    return EnhancedTokenSignals.model_validate(values)


def with_gmgn(signals: EnhancedTokenSignals, **token: str | None) -> TokenRiskSnapshot:
    return merge_enhanced_signals(guide_token(total_fees_sol=None, **token), signals)


# --- sustained volume, spike as a bonus (criterion 1) -----------------------------------


def test_a_steady_runner_without_a_new_burst_passes_on_its_sustained_volume() -> None:
    # 80 000 per 5m and 960 000 an hour: 16 000 a minute now and on average; no spike.
    decision = screen(steady_history(short="80000", long="480000", hour="960000"))
    found = evidence(decision)

    assert decision.status is CandidateStatus.ACCEPT, decision.reasons
    assert DecisionFlag.VOLUME_SPIKE not in decision.flags
    assert found["volume_spike"][0] == "false"
    assert found["volume_per_minute"] == ("16000", "14000")
    assert found["hourly_volume_per_minute"] == ("16000", "14000")
    assert components(decision)["volume_spike"] == 0
    # 16 000 of the 20 000 a minute the author calls good.
    assert components(decision)["sustained_volume"] == Decimal("0.8")


def test_a_momentary_burst_without_the_hours_volume_is_rejected() -> None:
    # 100 000 in the last 5m (20 000 a minute), but only 300 000 in the hour (5 000 a minute).
    decision = screen(guide_history(hour="300000"), mode=TradingMode.SHADOW)

    assert decision.status is CandidateStatus.REJECT
    assert RejectionCode.HOURLY_VOLUME_PER_MINUTE_BELOW_THRESHOLD in decision.reasons
    assert evidence(decision)["hourly_volume_per_minute"] == ("5000", "14000")


def test_a_spike_is_a_flag_and_a_score_bonus_never_a_requirement() -> None:
    # 5m 100 000 against calm 20 000 (5x), 30m 300 000 against 120 000 (2.5x).
    spiked = screen(guide_history())
    steady = screen(steady_history(short="100000", long="300000", hour="900000"))

    assert spiked.status is steady.status is CandidateStatus.ACCEPT
    assert DecisionFlag.VOLUME_SPIKE in spiked.flags
    assert evidence(spiked)["volume_spike"] == ("true", "short>=3; robust_z>=4; long>=1.5")
    assert components(spiked)["volume_spike"] == 1
    assert spiked.score is not None and steady.score is not None
    assert spiked.score - steady.score == GuideScreeningRules().weights.volume_spike


def test_a_low_fee_tvl_is_no_reason_the_author_has_no_such_minimum() -> None:
    # 30m LP fees 300 000 / 100 = 3 000 on 2 000 000 of TVL: 0.15%, below the 0.2% the bot
    # demanded until 2026-10-25. The guide selects by volume; fee/TVL stays information.
    decision = screen(
        steady_history(short="1200000", long="300000", hour="1800000", tvl="2000000"),
        mode=TradingMode.SHADOW,
    )

    assert RejectionCode.FEE_TVL_BELOW_THRESHOLD not in decision.reasons
    assert RejectionCode.VOLUME_SIGNAL_BELOW_THRESHOLD not in decision.reasons
    assert evidence(decision)["fee_tvl"] == ("0.0015", None)


def test_guide_v1_carries_no_fee_tvl_minimum() -> None:
    assert guide_policy().screening.minimum_fee_tvl is None
    with pytest.raises(ValueError, match="minimum_fee_tvl"):
        GuidePolicySettings.model_validate({"minimum_fee_tvl": "0.002"})


def test_the_legacy_ruleset_still_needs_its_fee_tvl_minimum() -> None:
    legacy = {**GUIDE.model_dump(), "guide": None, "minimum_fee_tvl": None}
    with pytest.raises(ValueError, match="minimum_fee_tvl"):
        ScreeningPolicy.model_validate(legacy)


# --- metrics by token (criterion 3) -----------------------------------------------------


def test_without_gmgn_the_token_volume_is_the_sum_of_its_dlmm_pools_with_a_flag() -> None:
    # This pool: 50 000 per 5m (10 000/min) and 600 000 an hour (10 000/min) - too little alone.
    history = guide_history(short="50000", hour="600000", tvl="90000")
    # Two more pools of the token: 25 000 per 5m and 300 000 an hour between them.
    siblings = TokenSelection(
        sibling_pools=2,
        sibling_volume_1h=Decimal("300000"),
        sibling_volume_5m=Decimal("25000"),
    )

    alone = screen(history, mode=TradingMode.SHADOW)
    summed = screen(history, mode=TradingMode.SHADOW, selection=siblings)
    found = evidence(summed)

    assert RejectionCode.VOLUME_PER_MINUTE_BELOW_THRESHOLD in alone.reasons
    assert RejectionCode.HOURLY_VOLUME_PER_MINUTE_BELOW_THRESHOLD in alone.reasons
    assert summed.status is CandidateStatus.ACCEPT, summed.reasons
    assert DecisionFlag.TOKEN_VOLUME_POOL_ONLY in summed.flags
    assert found["token_volume_source"][0] == "DLMM_POOLS"
    assert found["token_pool_count"][0] == "3"
    assert found["volume_per_minute"][0] == "15000"  # (50 000 + 25 000) / 5
    assert found["hourly_volume_per_minute"][0] == "15000"  # 900 000 / 60


def test_with_a_fresh_gmgn_reading_the_token_volume_and_cap_are_gmgns() -> None:
    history = guide_history(short="50000", hour="600000", tvl="90000")
    # GMGN over every pool: 100 000 per 5m (20 000/min), 1 200 000 an hour (20 000/min).
    busy = reading(
        volume_5m_usd=Decimal("100000"),
        volume_1h_usd=Decimal("1200000"),
        market_cap_usd=Decimal("3000000"),
    )
    # GMGN says the token barely trades, although this one pool looks busy.
    quiet = reading(volume_5m_usd=Decimal("5000"), volume_1h_usd=Decimal("60000"))

    accepted = screen(history, with_gmgn(busy), rules=GMGN)
    rejected = screen(guide_history(), with_gmgn(quiet), rules=GMGN, mode=TradingMode.SHADOW)

    assert accepted.status is CandidateStatus.ACCEPT, accepted.reasons
    assert DecisionFlag.TOKEN_VOLUME_POOL_ONLY not in accepted.flags
    assert evidence(accepted)["token_volume_source"][0] == "GMGN"
    assert evidence(accepted)["volume_per_minute"][0] == "20000"
    assert evidence(accepted)["market_cap"] == ("3000000", "500000")
    assert RejectionCode.VOLUME_PER_MINUTE_BELOW_THRESHOLD in rejected.reasons  # 1 000/min
    assert RejectionCode.HOURLY_VOLUME_PER_MINUTE_BELOW_THRESHOLD in rejected.reasons


def test_a_stale_gmgn_reading_falls_back_to_the_pools_with_the_flag() -> None:
    stale = reading(
        volume_5m_usd=Decimal("5000"),
        volume_1h_usd=Decimal("60000"),
        freshness=Freshness.STALE,
    )

    decision = screen(guide_history(), with_gmgn(stale), rules=GMGN, mode=TradingMode.SHADOW)

    assert DecisionFlag.TOKEN_VOLUME_POOL_ONLY in decision.flags
    assert RejectionCode.VOLUME_PER_MINUTE_BELOW_THRESHOLD not in decision.reasons


def test_before_gmgn_is_asked_pool_sums_never_decide_the_token_volume() -> None:
    """The cheap stage with GMGN configured: low DLMM volume is unknown, not a reject."""

    pending = TokenSelection(enhanced_pending=True)

    decision = screen(
        guide_history(short="5000", hour="60000", tvl="10000"),
        mode=TradingMode.SHADOW,
        selection=pending,
    )

    assert decision.status is CandidateStatus.UNKNOWN
    assert RejectionCode.VOLUME_PER_MINUTE_BELOW_THRESHOLD not in decision.reasons
    assert RejectionCode.HOURLY_VOLUME_PER_MINUTE_BELOW_THRESHOLD not in decision.reasons
    assert "token_volume" in decision.missing_signals


def test_the_pool_base_fee_and_bin_step_are_in_the_evidence() -> None:
    decision = screen(guide_history(base_fee_rate="0.02", bin_step=200))

    assert evidence(decision)["pool_base_fee_rate"][0] == "0.02"
    assert evidence(decision)["pool_bin_step"][0] == "200"


# --- artificial volume against the cap (criterion 6) ------------------------------------


def test_savash_honest_looking_fees_but_too_few_total_fees_for_the_cap_is_artificial() -> None:
    # 980 000 an hour on 87 000 of TVL (11.3x); the pool's fees match its 1% base fee
    # (9 800), but GMGN total fees are 45 SOL where a 2 000 000 cap needs 30 x 4 = 120.
    history = guide_history(short="81000", hour="980000", hour_fees="9800", tvl="87000")

    decision = screen(history, guide_token(total_fees_sol="45"), mode=TradingMode.SHADOW)
    found = evidence(decision)

    assert decision.status is CandidateStatus.REJECT
    assert RejectionCode.ARTIFICIAL_VOLUME_SUSPECTED in decision.reasons
    assert Decimal(found["required_total_fees_sol"][0]) == Decimal("120")
    assert Decimal(found["pool_hourly_turnover"][0]) == Decimal("980000") / Decimal("87000")


def test_low_pool_fees_alone_no_longer_reject_when_total_fees_fit_the_cap() -> None:
    # The old organicity rule: 900 / (980 000 x 1%) = 0.09 < 0.5. Total fees 150 >= 120.
    history = guide_history(short="81000", hour="980000", hour_fees="900", tvl="87000")

    decision = screen(history, guide_token(total_fees_sol="150"))

    assert decision.status is CandidateStatus.ACCEPT, decision.reasons
    assert RejectionCode.ARTIFICIAL_VOLUME_SUSPECTED not in decision.reasons


def test_too_few_total_fees_without_the_tenfold_turnover_is_not_artificial() -> None:
    # 900 000 an hour on 150 000 of TVL: 6x < 10x; 45 SOL < 120 SOL required.
    decision = screen(guide_history(), guide_token(total_fees_sol="45"))

    assert RejectionCode.ARTIFICIAL_VOLUME_SUSPECTED not in decision.reasons
    assert decision.status is CandidateStatus.ACCEPT


def test_without_total_fees_the_artificial_rule_waits_and_the_d03_flag_is_set() -> None:
    history = guide_history(short="81000", hour="980000", hour_fees="9800", tvl="87000")

    decision = screen(history, guide_token(total_fees_sol=None))

    assert RejectionCode.ARTIFICIAL_VOLUME_SUSPECTED not in decision.reasons
    assert DecisionFlag.TOTAL_FEES_UNAVAILABLE in decision.flags
    assert evidence(decision)["required_total_fees_sol"][0] == "120"


def test_a_gmgn_wash_trading_verdict_rejects() -> None:
    washed = with_gmgn(reading(wash_trading=RiskSignal.UNSAFE))
    clean = with_gmgn(reading(wash_trading=RiskSignal.SAFE))

    rejected = screen(guide_history(), washed, rules=GMGN, mode=TradingMode.SHADOW)
    accepted = screen(guide_history(), clean, rules=GMGN, mode=TradingMode.SHADOW)

    assert RejectionCode.ARTIFICIAL_VOLUME_SUSPECTED in rejected.reasons
    assert evidence(rejected)["gmgn_wash_trading"][0] == "UNSAFE"
    assert accepted.status is CandidateStatus.ACCEPT


# --- theme (criterion 7) ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "symbol"),
    [
        ("OFFICIAL TRUMP", "TRUMP"),
        ("Melania Meme", "MELANIA"),
        ("Илон Маск", "ILON"),
        ("Дональд ТРАМП", "DJT"),
        ("baby", "eLoNdOgE"),
        ("Political Cat", "PCAT"),
        ("Кот политик", "KOT"),
        ("Stream Coin", "LIVE"),
        ("Стример", "STRM"),
        ("Make America", "MAGA"),
    ],
)
def test_coins_about_elon_trump_politics_or_streams_are_excluded(name: str, symbol: str) -> None:
    decision = screen(guide_history(name=name, symbol=symbol), mode=TradingMode.SHADOW)

    assert decision.status is CandidateStatus.REJECT
    assert RejectionCode.THEME_EXCLUDED in decision.reasons


def test_the_theme_list_is_a_setting_and_an_ordinary_coin_passes() -> None:
    own = policy(GuideScreeningRules(excluded_theme_words=("doge",)))

    ordinary = screen(guide_history(name="Backpack", symbol="BP"))
    trump_allowed = screen(guide_history(name="OFFICIAL TRUMP", symbol="TRUMP"), rules=own)
    doge = screen(guide_history(name="Dogecoin", symbol="DOGE"), rules=own)

    assert ordinary.status is CandidateStatus.ACCEPT
    assert RejectionCode.THEME_EXCLUDED not in trump_allowed.reasons
    assert RejectionCode.THEME_EXCLUDED in doge.reasons
    assert evidence(doge)["theme"][0] == "doge"


# --- developer, snipers, revivals (criterion 8) -----------------------------------------


def test_a_developer_holding_tokens_closes_live_and_is_a_flag_elsewhere() -> None:
    token = with_gmgn(reading(developer_share=Decimal("0.035")))

    live = screen(guide_history(), token, rules=GMGN, mode=TradingMode.LIVE)
    approval = screen(guide_history(), token, rules=GMGN, mode=TradingMode.APPROVAL)
    shadow = screen(guide_history(), token, rules=GMGN, mode=TradingMode.SHADOW)

    assert live.status is CandidateStatus.REJECT
    assert RejectionCode.DEVELOPER_HOLDS_TOKENS in live.reasons
    for decision in (approval, shadow):
        assert decision.status is CandidateStatus.ACCEPT
        assert DecisionFlag.DEVELOPER_HOLDS_TOKENS in decision.flags
        assert RejectionCode.DEVELOPER_HOLDS_TOKENS not in decision.reasons


def test_many_snipers_are_a_flag_not_a_reject() -> None:
    many = screen(guide_history(), with_gmgn(reading(sniper_count=21)), rules=GMGN)
    usual = screen(guide_history(), with_gmgn(reading(sniper_count=20)), rules=GMGN)

    assert many.status is CandidateStatus.ACCEPT
    assert DecisionFlag.SNIPERS_HIGH in many.flags
    assert evidence(many)["gmgn_sniper_count"] == ("21", "20")
    assert DecisionFlag.SNIPERS_HIGH not in usual.flags


def test_a_token_sixty_days_old_or_more_is_flagged_as_revived() -> None:
    revived = screen(guide_history(created_at=NOW - timedelta(days=61)))
    young = screen(guide_history(created_at=NOW - timedelta(days=59)))
    by_gmgn = screen(
        guide_history(),
        with_gmgn(reading(created_at=NOW - timedelta(days=90))),
        rules=GMGN,
    )

    assert revived.status is CandidateStatus.ACCEPT
    assert DecisionFlag.TOKEN_REVIVED in revived.flags
    assert DecisionFlag.TOKEN_REVIVED not in young.flags
    assert DecisionFlag.TOKEN_REVIVED in by_gmgn.flags


def test_gmgn_ath_and_developer_status_reach_the_record() -> None:
    signals = reading(
        ath_price_usd=Decimal("0.004"),
        ath_market_cap_usd=Decimal("4000000"),
        developer_status=DeveloperStatus.SOLD,
    )

    found = evidence(screen(guide_history(), with_gmgn(signals), rules=GMGN))

    assert found["gmgn_token_ath_price"][0] == "0.004"
    assert found["gmgn_token_ath_market_cap"][0] == "4000000"
    assert found["developer_status"][0] == "SOLD"


# --- one position per token, rank (criteria 2, 5) ---------------------------------------


def test_a_token_already_held_in_any_pool_is_rejected_in_every_mode() -> None:
    held = TokenSelection(held=True)

    for mode in TradingMode:
        decision = screen(guide_history(), mode=mode, selection=held)
        assert decision.status is CandidateStatus.REJECT
        assert RejectionCode.TOKEN_ALREADY_HELD in decision.reasons


def test_a_candidate_below_the_deep_check_limit_is_recorded_as_such() -> None:
    deferred = screen(guide_history(), selection=TokenSelection(rank=51, rank_limit=50))
    checked = screen(guide_history(), selection=TokenSelection(rank=50, rank_limit=50))

    assert RejectionCode.CANDIDATE_RANK_BELOW_LIMIT in deferred.reasons
    assert evidence(deferred)["token_volume_rank"] == ("51", "50")
    assert checked.status is CandidateStatus.ACCEPT

# --- review: unknown inputs of the new rules fail closed where no operator checks ---------


GMGN_RELEASED = policy(GuideScreeningRules(gmgn=GmgnScreeningRules(required_for_live=False)))


def test_an_unknown_developer_share_closes_live_while_gmgn_is_required() -> None:
    """A fresh reading with every holder share but the developer's: LIVE cannot tell
    whether the developer still holds, so it is closed like any incomplete GMGN reading."""

    token = with_gmgn(reading(developer_share=None))

    live = screen(guide_history(), token, rules=GMGN, mode=TradingMode.LIVE)
    released = screen(guide_history(), token, rules=GMGN_RELEASED, mode=TradingMode.LIVE)
    approval = screen(guide_history(), token, rules=GMGN, mode=TradingMode.APPROVAL)
    shadow = screen(guide_history(), token, rules=GMGN, mode=TradingMode.SHADOW)

    assert live.status is CandidateStatus.REJECT
    assert RejectionCode.GMGN_UNAVAILABLE in live.reasons
    assert evidence(live)["developer_share"][0] == "UNAVAILABLE"
    assert released.status is CandidateStatus.ACCEPT
    for decision in (approval, shadow, released):
        assert DecisionFlag.GMGN_UNAVAILABLE in decision.flags
    assert approval.status is shadow.status is CandidateStatus.ACCEPT


def test_a_token_without_name_or_ticker_is_flagged_in_shadow_and_closed_beyond() -> None:
    nameless = guide_history(name=None, symbol=None)

    shadow = screen(nameless, mode=TradingMode.SHADOW)
    approval = screen(nameless, mode=TradingMode.APPROVAL)
    live = screen(nameless, mode=TradingMode.LIVE)

    assert shadow.status is CandidateStatus.ACCEPT
    assert DecisionFlag.THEME_UNKNOWN in shadow.flags
    assert "theme" not in shadow.missing_signals
    for decision in (approval, live):
        assert decision.status is CandidateStatus.REJECT
        assert RejectionCode.GUIDE_SIGNAL_UNKNOWN in decision.reasons
        assert "theme" in decision.missing_signals
        assert DecisionFlag.THEME_UNKNOWN in decision.flags
    assert evidence(live)["theme"][0] == "UNKNOWN"
