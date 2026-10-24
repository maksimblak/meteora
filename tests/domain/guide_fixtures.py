"""Point-in-time inputs for the guide-v1 (LexDollar) rules, shared by domain tests."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from src.domain.features import GuideFeatureRules, VolumeFeatureEngine, VolumeFeatureRules
from src.domain.features.models import VolumeSpikeFeatures
from src.domain.models.primitives import PublicKey
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
RULESET = "guide-test"
CONFIG = "guide-config"


def guide_snapshot(
    at: datetime,
    *,
    short: str = "100000",
    long: str = "300000",
    hour: str = "900000",
    hour_fees: str = "8100",
    tvl: str = "150000",
    price: str = "1",
    base_fee_rate: str | None = "0.01",
    market_cap: str | None = "2000000",
    bin_step: int = 250,
    created_at: datetime | None = None,
    name: str | None = "Guide Coin",
    symbol: str | None = "GUIDE",
) -> PoolSnapshot:
    """A memecoin pool: 5m volume ``short``, 30m ``long``, 1h ``hour`` with ``hour_fees``.

    The default hour, 900 000 (15 000 a minute), is a sustained volume above the guide-v1
    minimum of 14 000 a minute (G24); the default name and ticker name no excluded theme."""

    windows = {
        "5m": WindowMetrics(
            volume=Decimal(short),
            fees=Decimal(short) / Decimal(100),
            transaction_count=400,
            unique_participants=150,
        ),
        "30m": WindowMetrics(volume=Decimal(long), fees=Decimal(long) / Decimal(100)),
        "1h": WindowMetrics(volume=Decimal(hour), fees=Decimal(hour_fees)),
        "4h": WindowMetrics(volume=Decimal(hour) * 3, fees=Decimal(hour_fees) * 3),
        "24h": WindowMetrics(volume=Decimal(hour) * 10, fees=Decimal(hour_fees) * 10),
    }
    return PoolSnapshot(
        pool=POOL,
        windows=windows,
        tvl=Decimal(tvl),
        depth=Decimal(tvl) / Decimal(2),
        price=Decimal(price),
        bin_step=bin_step,
        created_at=created_at or NOW - timedelta(hours=6),
        observed_at=at,
        source_at=at,
        ingested_at=at,
        freshness=Freshness.FRESH,
        data_quality=DataQuality.COMPLETE,
        base_fee_rate=None if base_fee_rate is None else Decimal(base_fee_rate),
        base_token_market_cap=None if market_cap is None else Decimal(market_cap),
        base_token_name=name,
        base_token_symbol=symbol,
    )


def guide_history(
    *,
    prices: tuple[str, ...] = ("1",) * 7,
    created_at: datetime | None = None,
    bin_step: int = 250,
    **current: str | None,
) -> list[PoolSnapshot]:
    """Calm 5-minute observations at ``prices[:-1]`` and a volume spike at ``NOW``."""

    calm = [
        guide_snapshot(
            NOW - timedelta(minutes=5 * (len(prices) - 1 - index)),
            short="20000",
            long="120000",
            price=price,
            created_at=created_at,
            bin_step=bin_step,
        )
        for index, price in enumerate(prices[:-1])
    ]
    latest = guide_snapshot(
        NOW,
        price=prices[-1],
        created_at=created_at,
        bin_step=bin_step,
        **current,  # type: ignore[arg-type]
    )
    return [*calm, latest]


def guide_feature_rules(**guide: object) -> VolumeFeatureRules:
    return VolumeFeatureRules(
        ruleset_version=RULESET,
        config_version=CONFIG,
        baseline_points=6,
        minimum_baseline_points=6,
        maximum_history_points=100,
        guide=GuideFeatureRules(**guide),  # type: ignore[arg-type]
    )


def guide_features(history: list[PoolSnapshot], **guide: object) -> VolumeSpikeFeatures:
    return VolumeFeatureEngine().compute(history, guide_feature_rules(**guide), decision_at=NOW)


def guide_token(
    *,
    holder_concentration: str | None = "0.20",
    total_fees_sol: str | None = "45",
    earliest_activity_at: datetime | None = None,
    complete: bool = False,
) -> TokenRiskSnapshot:
    holders = None if holder_concentration is None else Decimal(holder_concentration)
    return TokenRiskSnapshot(
        mint=POOL.base_mint,
        mint_authority=AuthorityStatus.REVOKED,
        freeze_authority=AuthorityStatus.REVOKED,
        blacklisted=RiskSignal.SAFE,
        verified=RiskSignal.SAFE,
        holder_concentration=holders,
        wash_trading=RiskSignal.SAFE,
        observed_at=NOW,
        source_at=NOW,
        ingested_at=NOW,
        data_quality=(
            DataQuality.COMPLETE if holder_concentration is not None else DataQuality.PARTIAL
        ),
        missing_signals=() if holder_concentration is not None else ("holder_concentration",),
        earliest_activity_at=earliest_activity_at,
        activity_history_complete=complete,
        total_fees_sol=None if total_fees_sol is None else Decimal(total_fees_sol),
    )
