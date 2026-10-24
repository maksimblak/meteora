"""D07 hard filters on live signals: quote depth, participant gaps and Data API token flags."""

from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest

from src.domain.models.trading import TradingMode
from src.domain.screening import (
    CandidateDecision,
    CandidateStatus,
    DecisionContext,
    DecisionFlag,
    DepthEvidence,
    DepthSource,
    GuideScreeningRules,
    PoolScreener,
    RejectionCode,
    ScreeningPolicy,
    VolumeIntegrityMode,
    merge_pool_token_signals,
)
from src.market.models import (
    AuthorityStatus,
    DataQuality,
    PoolSnapshot,
    RiskSignal,
    TokenRiskSnapshot,
)
from tests.domain.guide_fixtures import (
    CONFIG,
    NOW,
    RULESET,
    guide_features,
    guide_history,
    guide_token,
)

LIVE_SIGNALS = ScreeningPolicy(
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
    guide=GuideScreeningRules(),
    volume_integrity=VolumeIntegrityMode.AVAILABLE_SIGNALS,
)


def _rebuilt(snapshot: PoolSnapshot, **changes: Any) -> PoolSnapshot:
    data = snapshot.model_dump()
    data.update(changes)
    return PoolSnapshot.model_validate(data)


def without_counts(history: list[PoolSnapshot]) -> list[PoolSnapshot]:
    """The live Data API: no transaction or participant counts on any window."""

    rebuilt = []
    for snapshot in history:
        windows = snapshot.model_dump()["windows"]
        for window in windows.values():
            window.update(transaction_count=None, unique_participants=None)
        rebuilt.append(_rebuilt(snapshot, windows=windows))
    return rebuilt


def wash_unknown() -> TokenRiskSnapshot:
    return TokenRiskSnapshot.model_validate(
        {
            **guide_token().model_dump(),
            "wash_trading": RiskSignal.UNKNOWN,
            "missing_signals": ("wash_trading",),
            "data_quality": DataQuality.PARTIAL,
        }
    )


def evaluate(
    history: list[PoolSnapshot],
    token: TokenRiskSnapshot,
    *,
    mode: TradingMode,
    depth: DepthEvidence | None = None,
    policy: ScreeningPolicy = LIVE_SIGNALS,
) -> CandidateDecision:
    return PoolScreener().evaluate(
        history[-1],
        token,
        guide_features(history),
        policy,
        context=DecisionContext(
            mode=mode, ruleset_version=RULESET, config_version=CONFIG, correlation_id="cycle-1"
        ),
        estimated_price_impact=Decimal("0.001"),
        depth=depth,
    )


@pytest.mark.parametrize("mode", [TradingMode.SHADOW, TradingMode.APPROVAL])
def test_without_participant_signals_shadow_and_approval_go_on_with_a_flag(
    mode: TradingMode,
) -> None:
    decision = evaluate(without_counts(guide_history()), wash_unknown(), mode=mode)

    assert decision.status is CandidateStatus.ACCEPT
    assert DecisionFlag.VOLUME_PARTICIPANTS_UNAVAILABLE in decision.flags
    assert RejectionCode.VOLUME_INTEGRITY_UNKNOWN not in decision.reasons
    assert RejectionCode.TOKEN_RISK_UNKNOWN not in decision.reasons


def test_live_stays_closed_until_a_provider_reports_participants() -> None:
    blind = evaluate(without_counts(guide_history()), wash_unknown(), mode=TradingMode.LIVE)
    enhanced = evaluate(guide_history(), guide_token(), mode=TradingMode.LIVE)

    assert blind.status is CandidateStatus.REJECT
    assert RejectionCode.VOLUME_PARTICIPANTS_UNAVAILABLE in blind.reasons
    assert enhanced.status is CandidateStatus.ACCEPT
    assert DecisionFlag.VOLUME_PARTICIPANTS_UNAVAILABLE not in enhanced.flags


def test_the_same_burst_repeated_is_artificial_volume_on_available_signals() -> None:
    history = guide_history()
    # The two windows before the spike already carried the same 100 000 burst.
    repeated = [
        *history[:-3],
        *(_rebuilt(item, windows={**item.model_dump()["windows"],
                                  "5m": history[-1].model_dump()["windows"]["5m"]})
          for item in history[-3:-1]),
        history[-1],
    ]

    decision = evaluate(without_counts(repeated), wash_unknown(), mode=TradingMode.SHADOW)

    assert decision.status is CandidateStatus.REJECT
    assert RejectionCode.ARTIFICIAL_VOLUME_CONFIRMED in decision.reasons


def test_heavy_turnover_with_too_few_total_fees_for_the_cap_is_suspected_artificial() -> None:
    """G24 replaced "the hour paid a tenth of the base fee" (fees over volume x base fee is
    at least one in DLMM, so it never fires on real swaps) with the author's signal: the
    hour turned the pool over 40x (6 000 000 on 150 000) while the token's total fees, 45
    SOL, are below the 30 x 2 000 000 / 500 000 = 120 SOL its cap calls for."""

    history = guide_history(long="3600000", hour="6000000", hour_fees="6000")

    decision = evaluate(without_counts(history), wash_unknown(), mode=TradingMode.SHADOW)

    assert decision.status is CandidateStatus.REJECT
    assert RejectionCode.ARTIFICIAL_VOLUME_SUSPECTED in decision.reasons
    assert RejectionCode.ARTIFICIAL_VOLUME_CONFIRMED not in decision.reasons
    assert ("volume_integrity", "CLEAR") in {
        (item.name, item.value) for item in decision.evidence
    }


@pytest.mark.parametrize(
    ("flag", "value", "reason"),
    [
        ("pool_blacklisted", True, RejectionCode.BLACKLISTED_POOL),
        ("base_token_verified", False, RejectionCode.UNVERIFIED_TOKEN),
        ("quote_token_verified", False, RejectionCode.UNVERIFIED_TOKEN),
        ("base_freeze_authority_disabled", False, RejectionCode.FREEZE_AUTHORITY_PRESENT),
    ],
)
def test_data_api_pool_and_token_flags_are_hard_filters(
    flag: str, value: bool, reason: RejectionCode
) -> None:
    history = guide_history()
    flagged = [*history[:-1], _rebuilt(history[-1], **{flag: value})]

    decision = evaluate(flagged, guide_token(), mode=TradingMode.SHADOW)

    assert decision.status is CandidateStatus.REJECT
    assert decision.reasons.count(reason) == 1


def test_data_api_flags_resolve_what_rpc_alone_leaves_unknown() -> None:
    unknown = TokenRiskSnapshot.model_validate(
        {
            **guide_token().model_dump(),
            "freeze_authority": AuthorityStatus.UNKNOWN,
            "blacklisted": RiskSignal.UNKNOWN,
            "verified": RiskSignal.UNKNOWN,
            "wash_trading": RiskSignal.UNKNOWN,
            "missing_signals": ("blacklisted", "freeze_authority", "verified", "wash_trading"),
            "data_quality": DataQuality.PARTIAL,
            "provider": "solana_rpc",
        }
    )
    pool = _rebuilt(
        guide_history()[-1],
        provider="meteora_data_api",
        pool_blacklisted=False,
        base_token_verified=True,
        base_freeze_authority_disabled=True,
    )
    dangerous = _rebuilt(pool, pool_blacklisted=True, base_token_verified=False,
                         base_freeze_authority_disabled=False)

    merged = merge_pool_token_signals(unknown, pool)
    unsafe = merge_pool_token_signals(guide_token(), dangerous)

    assert (merged.blacklisted, merged.verified) == (RiskSignal.SAFE, RiskSignal.SAFE)
    assert merged.freeze_authority is AuthorityStatus.REVOKED
    assert merged.missing_signals == ("wash_trading",)
    assert merged.provider == "solana_rpc+meteora_data_api"
    # A danger reported by either source wins over the other's all-clear.
    assert (unsafe.blacklisted, unsafe.verified) == (RiskSignal.UNSAFE, RiskSignal.UNSAFE)
    assert unsafe.freeze_authority is AuthorityStatus.PRESENT
    assert merge_pool_token_signals(guide_token(), guide_history()[-1]) == guide_token()


def _live_pool(history: list[PoolSnapshot]) -> list[PoolSnapshot]:
    return [*history[:-1], _rebuilt(history[-1], depth=None)]


def test_without_a_quote_shadow_goes_on_with_the_depth_flag() -> None:
    unavailable = DepthEvidence(source=DepthSource.UNAVAILABLE, detail="EXECUTOR_UNREACHABLE")

    decision = evaluate(
        _live_pool(guide_history()), guide_token(), mode=TradingMode.SHADOW, depth=unavailable
    )

    assert decision.status is CandidateStatus.ACCEPT
    assert DecisionFlag.DEPTH_UNAVAILABLE in decision.flags
    assert RejectionCode.LOW_DEPTH not in decision.reasons
    assert RejectionCode.PRICE_IMPACT_HIGH not in decision.reasons
    assert decision.depth == unavailable


@pytest.mark.parametrize("mode", [TradingMode.APPROVAL, TradingMode.LIVE])
def test_without_a_quote_approval_and_live_entries_are_closed(mode: TradingMode) -> None:
    decision = evaluate(
        _live_pool(guide_history()),
        guide_token(),
        mode=mode,
        depth=DepthEvidence(source=DepthSource.UNAVAILABLE, detail="QUOTES_NOT_CONFIGURED"),
    )

    assert decision.status is CandidateStatus.REJECT
    assert RejectionCode.DEPTH_UNAVAILABLE in decision.reasons


def test_quoted_depth_and_impact_replace_the_missing_snapshot_depth() -> None:
    deep = DepthEvidence(
        source=DepthSource.QUOTE,
        depth=Decimal("50000"),
        price_impact=Decimal("0.002"),
        reference_notional=Decimal("100"),
        measured_at=NOW,
    )
    thin = DepthEvidence(
        source=DepthSource.QUOTE,
        reference_notional=Decimal("100"),
        detail="INSUFFICIENT_LIQUIDITY",
        measured_at=NOW - timedelta(seconds=1),
    )

    accepted = evaluate(
        _live_pool(guide_history()), guide_token(), mode=TradingMode.LIVE, depth=deep
    )
    rejected = evaluate(
        _live_pool(guide_history()), guide_token(), mode=TradingMode.SHADOW, depth=thin
    )

    assert accepted.status is CandidateStatus.ACCEPT
    assert DecisionFlag.DEPTH_UNAVAILABLE not in accepted.flags
    assert {RejectionCode.LOW_DEPTH, RejectionCode.PRICE_IMPACT_HIGH} <= set(rejected.reasons)
    assert rejected.status is CandidateStatus.REJECT
