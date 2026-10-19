from datetime import UTC, datetime, timedelta
from decimal import Decimal

from src.domain.models.primitives import PublicKey
from src.market.features import FeatureEngine, FeatureReadiness, FeatureRules
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
from src.market.quality import (
    HardFilterPolicy,
    HardFilterStatus,
    MarketQualityPolicy,
    MarketReasonCode,
    RiskReasonCode,
    assess_hard_filters,
    assess_market_quality,
)

NOW = datetime(2026, 10, 18, 10, 0, tzinfo=UTC)
POOL = PoolRef(
    address=PublicKey("SysvarRent111111111111111111111111111111111"),
    base_mint=PublicKey("11111111111111111111111111111111"),
    quote_mint=PublicKey("So11111111111111111111111111111111111111112"),
)


def _snapshot(
    observed_at: datetime,
    *,
    price: str,
    tvl: str = "99",
    depth: str = "9",
) -> PoolSnapshot:
    return PoolSnapshot(
        pool=POOL,
        windows={
            "5m": WindowMetrics(volume=Decimal("20"), fees=Decimal("0.2")),
            "30m": WindowMetrics(volume=Decimal("60"), fees=Decimal("0.6")),
            "1h": WindowMetrics(volume=Decimal("120"), fees=Decimal("1.2")),
            "4h": WindowMetrics(volume=Decimal("480"), fees=Decimal("4.8")),
            "24h": WindowMetrics(volume=Decimal("2400"), fees=Decimal("24")),
        },
        tvl=Decimal(tvl),
        depth=Decimal(depth),
        price=Decimal(price),
        bin_step=25,
        created_at=NOW - timedelta(hours=1),
        observed_at=observed_at,
        source_at=observed_at,
        ingested_at=observed_at,
        freshness=Freshness.FRESH,
        data_quality=DataQuality.COMPLETE,
    )


def test_feature_engine_is_exact_and_quality_reasons_are_stable() -> None:
    history = [
        _snapshot(NOW - timedelta(minutes=10), price="1"),
        _snapshot(NOW - timedelta(minutes=5), price="1.1"),
    ]
    rules = FeatureRules(required_history_points=3, history_limit=12)

    feature = FeatureEngine().compute(history, rules)
    assessment = assess_market_quality(
        history[-1],
        feature,
        MarketQualityPolicy(
            minimum_tvl=Decimal("100"),
            minimum_depth=Decimal("10"),
            minimum_pool_age=timedelta(days=1),
            required_history_points=3,
        ),
    )

    assert feature.readiness is FeatureReadiness.WARMING_UP
    assert feature.volume_acceleration == Decimal("2")
    assert feature.turnover_24h == Decimal("2400") / Decimal("99")
    assert feature.fee_tvl_24h == Decimal("24") / Decimal("99")
    assert feature.price_volatility == Decimal("0.1")
    assert assessment.eligible is False
    assert assessment.reasons == (
        MarketReasonCode.LOW_TVL,
        MarketReasonCode.LOW_DEPTH,
        MarketReasonCode.POOL_TOO_YOUNG,
        MarketReasonCode.INCOMPLETE_HISTORY,
    )


def test_hard_filters_reject_danger_and_keep_unknown_separate() -> None:
    feature = FeatureEngine().compute(
        [
            _snapshot(NOW - timedelta(minutes=5), price="1"),
            _snapshot(NOW, price="1"),
        ],
        FeatureRules(required_history_points=2, history_limit=12),
    )
    dangerous = TokenRiskSnapshot(
        mint=POOL.base_mint,
        mint_authority=AuthorityStatus.REVOKED,
        freeze_authority=AuthorityStatus.REVOKED,
        blacklisted=RiskSignal.SAFE,
        verified=RiskSignal.SAFE,
        holder_concentration=Decimal("0.1"),
        wash_trading=RiskSignal.UNSAFE,
        observed_at=NOW,
        source_at=NOW,
        ingested_at=NOW,
        data_quality=DataQuality.COMPLETE,
    )
    unknown = TokenRiskSnapshot(
        mint=POOL.base_mint,
        mint_authority=AuthorityStatus.UNKNOWN,
        freeze_authority=AuthorityStatus.UNKNOWN,
        blacklisted=RiskSignal.UNKNOWN,
        verified=RiskSignal.UNKNOWN,
        holder_concentration=None,
        wash_trading=RiskSignal.UNKNOWN,
        observed_at=NOW,
        source_at=NOW,
        ingested_at=NOW,
        data_quality=DataQuality.PARTIAL,
        missing_signals=(
            "blacklisted",
            "freeze_authority",
            "holder_concentration",
            "mint_authority",
            "verified",
            "wash_trading",
        ),
    )

    rejected = assess_hard_filters(
        dangerous,
        feature,
        HardFilterPolicy(
            maximum_holder_concentration=Decimal("0.2"),
            maximum_volume_acceleration=Decimal("1.5"),
        ),
    )
    unresolved = assess_hard_filters(
        unknown,
        feature,
        HardFilterPolicy(
            maximum_holder_concentration=Decimal("0.2"),
            maximum_volume_acceleration=Decimal("3"),
        ),
    )

    assert rejected.status is HardFilterStatus.REJECT
    assert rejected.reasons == (
        RiskReasonCode.WASH_TRADING_DETECTED,
        RiskReasonCode.ARTIFICIAL_VOLUME,
    )
    assert unresolved.status is HardFilterStatus.UNKNOWN
    assert unresolved.missing_signals == unknown.missing_signals


def test_feature_engine_updates_incrementally_and_idempotently() -> None:
    engine = FeatureEngine()
    rules = FeatureRules(required_history_points=2, history_limit=2)
    first = _snapshot(NOW - timedelta(minutes=5), price="1")
    latest = _snapshot(NOW, price="1.1")

    warming = engine.update(first, rules)
    ready = engine.update(latest, rules)
    repeated = engine.update(latest, rules)

    assert warming.history_points == 1
    assert warming.readiness is FeatureReadiness.WARMING_UP
    assert ready.history_points == 2
    assert ready.readiness is FeatureReadiness.READY
    assert ready.price_volatility == Decimal("0.1")
    assert repeated == ready
