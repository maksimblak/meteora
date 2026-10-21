from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from src.domain.features import (
    SignalReadiness,
    VolumeFeatureEngine,
    VolumeFeatureRules,
    VolumeSpikeFeatures,
)
from src.domain.models.primitives import PublicKey
from src.domain.models.trading import TradingMode
from src.domain.screening import (
    CandidateStatus,
    DecisionContext,
    IntegrityStatus,
    PoolScreener,
    RejectionCode,
    ScreeningPolicy,
    assess_volume_integrity,
)
from src.market.models import (
    AuthorityStatus,
    DataQuality,
    Freshness,
    PoolRef,
    PoolSnapshot,
    RiskSignal,
    TokenRiskSnapshot,
    WindowMetrics,
)

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
POOL = PoolRef(
    address=PublicKey("SysvarRent111111111111111111111111111111111"),
    base_mint=PublicKey("11111111111111111111111111111111"),
    quote_mint=PublicKey("So11111111111111111111111111111111111111112"),
)


def snapshot(
    at: datetime,
    *,
    short: str = "100",
    long: str = "600",
    tvl: str = "1000",
    depth: str = "500",
    price: str = "1",
    transactions: int | None = 100,
    participants: int | None = 20,
) -> PoolSnapshot:
    metrics = {
        "5m": WindowMetrics(
            volume=Decimal(short),
            fees=Decimal("2"),
            transaction_count=transactions,
            unique_participants=participants,
        ),
        "30m": WindowMetrics(volume=Decimal(long), fees=Decimal("20")),
        "1h": WindowMetrics(volume=Decimal(long) * 2, fees=Decimal("40")),
        "4h": WindowMetrics(volume=Decimal(long) * 8, fees=Decimal("160")),
        "24h": WindowMetrics(volume=Decimal(long) * 48, fees=Decimal("960")),
    }
    return PoolSnapshot(
        pool=POOL,
        windows=metrics,
        tvl=Decimal(tvl),
        depth=Decimal(depth),
        price=Decimal(price),
        bin_step=25,
        created_at=NOW - timedelta(days=7),
        observed_at=at,
        source_at=at,
        ingested_at=at,
        freshness=Freshness.FRESH,
        data_quality=DataQuality.COMPLETE,
    )


def feature(
    *, transactions: int | None = 100, participants: int | None = 20
) -> tuple[PoolSnapshot, VolumeSpikeFeatures]:
    samples = [snapshot(NOW - timedelta(minutes=35 - index * 5)) for index in range(6)]
    samples.append(
        snapshot(
            NOW,
            short="400",
            long="1200",
            transactions=transactions,
            participants=participants,
        )
    )
    rules = VolumeFeatureRules(
        ruleset_version="rules-1",
        config_version="config-1",
        baseline_points=6,
        minimum_baseline_points=6,
        maximum_history_points=20,
    )
    return samples[-1], VolumeFeatureEngine().compute(samples, rules, decision_at=NOW)


def token(
    *,
    wash: RiskSignal = RiskSignal.SAFE,
    quality: DataQuality = DataQuality.COMPLETE,
    missing: tuple[str, ...] = (),
) -> TokenRiskSnapshot:
    return TokenRiskSnapshot(
        mint=POOL.base_mint,
        mint_authority=AuthorityStatus.REVOKED,
        freeze_authority=AuthorityStatus.REVOKED,
        blacklisted=RiskSignal.SAFE,
        verified=RiskSignal.SAFE,
        holder_concentration=Decimal("0.20"),
        wash_trading=wash,
        observed_at=NOW,
        source_at=NOW,
        ingested_at=NOW,
        data_quality=quality,
        missing_signals=missing,
    )


def policy(**updates: object) -> ScreeningPolicy:
    values: dict[str, object] = {
        "minimum_tvl": Decimal("1000"),
        "minimum_depth": Decimal("500"),
        "minimum_pool_age": timedelta(days=7),
        "maximum_holder_concentration": Decimal("0.20"),
        "maximum_price_impact": Decimal("0.02"),
        "minimum_short_ratio": Decimal("4"),
        "minimum_long_ratio": Decimal("2"),
        "minimum_robust_z_score": Decimal("10"),
        "minimum_fee_tvl": Decimal("0.02"),
        "minimum_score": Decimal("0"),
    }
    values.update(updates)
    return ScreeningPolicy(**values)  # type: ignore[arg-type]


def context(mode: TradingMode) -> DecisionContext:
    return DecisionContext(
        mode=mode,
        ruleset_version="rules-1",
        config_version="config-1",
        correlation_id="cycle-0001",
    )


def test_boundary_values_exactly_at_hard_limits_pass_and_record_evidence() -> None:
    pool, features = feature()

    decision = PoolScreener().evaluate(
        pool,
        token(),
        features,
        policy(),
        context=context(TradingMode.LIVE),
        estimated_price_impact=Decimal("0.02"),
    )

    assert decision.status is CandidateStatus.ACCEPT
    assert decision.reasons == ()
    assert decision.correlation_id == "cycle-0001"
    assert decision.score is not None
    assert len(decision.score_components) == 7
    assert tuple(item.name for item in decision.evidence)[:2] == ("tvl", "depth")


def test_just_below_or_above_limits_has_stable_reason_order() -> None:
    pool, features = feature()
    low_pool = snapshot(NOW, short="400", long="1200", tvl="999.99", depth="499.99")

    decision = PoolScreener().evaluate(
        low_pool,
        token(),
        features.model_copy(update={"observed_at": low_pool.observed_at}),
        policy(),
        context=context(TradingMode.LIVE),
        estimated_price_impact=Decimal("0.0200001"),
    )

    assert decision.status is CandidateStatus.REJECT
    assert decision.reasons[:2] == (RejectionCode.LOW_TVL, RejectionCode.LOW_DEPTH)
    assert RejectionCode.PRICE_IMPACT_HIGH in decision.reasons


def test_confirmed_fake_volume_rejects_but_large_spike_alone_does_not() -> None:
    pool, features = feature(transactions=200, participants=20)
    safe_integrity = assess_volume_integrity(token(), features, policy())
    confirmed_integrity = assess_volume_integrity(token(wash=RiskSignal.UNSAFE), features, policy())

    accepted = PoolScreener().evaluate(
        pool,
        token(),
        features,
        policy(),
        context=context(TradingMode.LIVE),
        estimated_price_impact=Decimal("0.01"),
    )
    rejected = PoolScreener().evaluate(
        pool,
        token(wash=RiskSignal.UNSAFE),
        features,
        policy(),
        context=context(TradingMode.LIVE),
        estimated_price_impact=Decimal("0.01"),
    )

    assert safe_integrity.status is IntegrityStatus.CLEAR
    assert confirmed_integrity.status is IntegrityStatus.CONFIRMED
    assert accepted.status is CandidateStatus.ACCEPT
    assert RejectionCode.ARTIFICIAL_VOLUME_CONFIRMED in rejected.reasons


def test_positive_volume_with_zero_activity_is_confirmed_inconsistent() -> None:
    pool, features = feature(transactions=0, participants=0)

    integrity = assess_volume_integrity(token(), features, policy())
    decision = PoolScreener().evaluate(
        pool,
        token(),
        features,
        policy(),
        context=context(TradingMode.LIVE),
        estimated_price_impact=Decimal("0.01"),
    )

    assert integrity.status is IntegrityStatus.CONFIRMED
    assert integrity.reasons == (
        "positive_volume_without_participants",
        "positive_volume_without_transactions",
    )
    assert decision.status is CandidateStatus.REJECT
    assert RejectionCode.ARTIFICIAL_VOLUME_CONFIRMED in decision.reasons


def test_volume_contradictions_are_confirmed_even_when_other_signals_are_missing() -> None:
    _, zero_transactions = feature(transactions=0, participants=None)
    _, impossible_participants = feature(transactions=1, participants=2)
    unresolved_token = token(
        wash=RiskSignal.UNKNOWN,
        quality=DataQuality.PARTIAL,
        missing=("wash_trading",),
    )

    missing_breadth = assess_volume_integrity(unresolved_token, zero_transactions, policy())
    impossible_counts = assess_volume_integrity(token(), impossible_participants, policy())

    assert missing_breadth.status is IntegrityStatus.CONFIRMED
    assert missing_breadth.reasons == ("positive_volume_without_transactions",)
    assert "unique_participants" in missing_breadth.missing_signals
    assert "wash_trading" in missing_breadth.missing_signals
    assert impossible_counts.status is IntegrityStatus.CONFIRMED
    assert "participants_exceed_transactions" in impossible_counts.reasons


def test_unknown_integrity_is_observable_in_shadow_and_fail_closed_in_live() -> None:
    pool, features = feature(transactions=None, participants=None)
    unresolved_token = token(
        wash=RiskSignal.UNKNOWN,
        quality=DataQuality.PARTIAL,
        missing=("wash_trading",),
    )

    shadow = PoolScreener().evaluate(
        pool,
        unresolved_token,
        features,
        policy(),
        context=context(TradingMode.SHADOW),
        estimated_price_impact=Decimal("0.01"),
    )
    live = PoolScreener().evaluate(
        pool,
        unresolved_token,
        features,
        policy(),
        context=context(TradingMode.LIVE),
        estimated_price_impact=Decimal("0.01"),
    )

    assert shadow.status is CandidateStatus.UNKNOWN
    assert live.status is CandidateStatus.REJECT
    assert RejectionCode.VOLUME_INTEGRITY_UNKNOWN in live.reasons
    assert "unique_participants" in live.missing_signals


def test_golden_decision_is_byte_for_byte_repeatable() -> None:
    pool, features = feature()
    screener = PoolScreener()

    first = screener.evaluate(
        pool,
        token(),
        features,
        policy(),
        context=context(TradingMode.LIVE),
        estimated_price_impact=Decimal("0.01"),
    )
    second = screener.evaluate(
        pool,
        token(),
        features,
        policy(),
        context=context(TradingMode.LIVE),
        estimated_price_impact=Decimal("0.01"),
    )

    assert first.model_dump_json() == second.model_dump_json()


def test_warming_up_is_distinct_but_does_not_mask_confirmed_danger() -> None:
    pool, features = feature()
    warming = features.model_copy(
        update={
            "readiness": SignalReadiness.WARMING_UP,
            "data_quality": DataQuality.PARTIAL,
            "missing_signals": ("baseline_history",),
        }
    )

    waiting = PoolScreener().evaluate(
        pool,
        token(),
        warming,
        policy(),
        context=context(TradingMode.SHADOW),
        estimated_price_impact=Decimal("0.01"),
    )
    dangerous = PoolScreener().evaluate(
        pool,
        token(wash=RiskSignal.UNSAFE),
        warming,
        policy(),
        context=context(TradingMode.SHADOW),
        estimated_price_impact=Decimal("0.01"),
    )

    assert waiting.status is CandidateStatus.WARMING_UP
    assert waiting.reasons == (RejectionCode.HISTORY_WARMING_UP,)
    assert dangerous.status is CandidateStatus.REJECT
    assert RejectionCode.ARTIFICIAL_VOLUME_CONFIRMED in dangerous.reasons


def test_mixed_pool_and_token_identity_fail_closed_with_decision_record() -> None:
    pool, features = feature()
    mixed = features.model_copy(update={"pool_address": POOL.quote_mint})

    decision = PoolScreener().evaluate(
        pool,
        token(),
        mixed,
        policy(),
        context=context(TradingMode.LIVE),
        estimated_price_impact=Decimal("0.01"),
    )

    assert decision.status is CandidateStatus.REJECT
    assert RejectionCode.INPUT_IDENTITY_MISMATCH in decision.reasons


@pytest.mark.parametrize("field", ["source_at", "ingested_at"])
def test_market_and_feature_provenance_must_align_exactly(field: str) -> None:
    pool, features = feature()
    shifted = NOW - timedelta(seconds=1)
    features = features.model_copy(update={field: shifted})

    decision = PoolScreener().evaluate(
        pool,
        token(),
        features,
        policy(),
        context=context(TradingMode.LIVE),
        estimated_price_impact=Decimal("0.01"),
    )

    assert decision.status is CandidateStatus.REJECT
    assert RejectionCode.SNAPSHOT_ALIGNMENT_MISMATCH in decision.reasons


def test_combined_identity_and_provenance_mismatch_records_both_reasons() -> None:
    pool, features = feature()
    features = features.model_copy(
        update={
            "base_mint": POOL.quote_mint,
            "source_at": NOW - timedelta(seconds=1),
        }
    )

    decision = PoolScreener().evaluate(
        pool,
        token(),
        features,
        policy(),
        context=context(TradingMode.LIVE),
        estimated_price_impact=Decimal("0.01"),
    )

    assert decision.status is CandidateStatus.REJECT
    assert RejectionCode.INPUT_IDENTITY_MISMATCH in decision.reasons
    assert RejectionCode.SNAPSHOT_ALIGNMENT_MISMATCH in decision.reasons


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("market", RejectionCode.FUTURE_MARKET_DATA),
        ("token", RejectionCode.FUTURE_TOKEN_RISK_DATA),
        ("features", RejectionCode.FUTURE_FEATURE_DATA),
    ],
)
def test_future_dated_inputs_fail_closed(source: str, expected: RejectionCode) -> None:
    pool, features = feature()
    risk = token()
    future = NOW + timedelta(seconds=1)
    if source == "market":
        pool = pool.model_copy(update={"source_at": future, "ingested_at": future})
    elif source == "token":
        risk = risk.model_copy(update={"source_at": future, "ingested_at": future})
    else:
        features = features.model_copy(update={"source_at": future, "ingested_at": future})

    decision = PoolScreener().evaluate(
        pool,
        risk,
        features,
        policy(),
        context=context(TradingMode.LIVE),
        estimated_price_impact=Decimal("0.01"),
    )

    assert decision.status is CandidateStatus.REJECT
    assert expected in decision.reasons
