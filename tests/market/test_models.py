from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

import pytest
from pydantic import ValidationError

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

MINT_A = PublicKey("11111111111111111111111111111111")
MINT_B = PublicKey("So11111111111111111111111111111111111111112")
POOL_ADDRESS = PublicKey("SysvarRent111111111111111111111111111111111")
NOW = datetime(2026, 10, 18, 10, 0, tzinfo=UTC)


def _windows() -> dict[str, WindowMetrics]:
    return {
        "5m": WindowMetrics(volume=Decimal("10"), fees=Decimal("0.01")),
        "30m": WindowMetrics(volume=Decimal("50"), fees=Decimal("0.05")),
        "1h": WindowMetrics(volume=Decimal("100"), fees=Decimal("0.1")),
        "4h": WindowMetrics(volume=Decimal("400"), fees=Decimal("0.4")),
        "24h": WindowMetrics(volume=Decimal("2400"), fees=Decimal("2.4")),
    }


def test_market_models_preserve_exact_values_and_unknown_risk() -> None:
    pool = PoolRef(address=POOL_ADDRESS, base_mint=MINT_A, quote_mint=MINT_B)
    snapshot = PoolSnapshot(
        pool=pool,
        windows=_windows(),
        tvl=Decimal("1234.567890123456789"),
        depth=Decimal("900.01"),
        price=Decimal("0.00000000123456789"),
        bin_step=25,
        created_at=datetime(2026, 10, 1, tzinfo=UTC),
        observed_at=NOW,
        source_at=NOW,
        ingested_at=NOW,
        freshness=Freshness.FRESH,
        data_quality=DataQuality.COMPLETE,
    )
    risk = TokenRiskSnapshot(
        mint=MINT_A,
        mint_authority=AuthorityStatus.REVOKED,
        freeze_authority=AuthorityStatus.UNKNOWN,
        blacklisted=RiskSignal.UNKNOWN,
        verified=RiskSignal.UNKNOWN,
        holder_concentration=None,
        observed_at=NOW,
        source_at=NOW,
        ingested_at=NOW,
        data_quality=DataQuality.PARTIAL,
        missing_signals=(
            "blacklisted",
            "freeze_authority",
            "holder_concentration",
            "verified",
            "wash_trading",
        ),
    )

    assert snapshot.tvl == Decimal("1234.567890123456789")
    assert snapshot.pool_age.total_seconds() == 17 * 24 * 60 * 60 + 10 * 60 * 60
    assert risk.freeze_authority is AuthorityStatus.UNKNOWN
    assert risk.blacklisted is RiskSignal.UNKNOWN

    with pytest.raises(TypeError):
        cast(Any, snapshot.windows)["5m"] = WindowMetrics(volume=Decimal("999"))

    with pytest.raises(ValidationError, match="binary float"):
        snapshot.model_copy(update={"tvl": 1.1}, deep=True).model_validate(
            {**snapshot.model_dump(), "tvl": 1.1}
        )

    with pytest.raises(ValidationError, match="missing fields"):
        PoolSnapshot.model_validate({**snapshot.model_dump(), "missing_fields": ("depth",)})

    with pytest.raises(ValidationError, match="unknown risk signals"):
        TokenRiskSnapshot(
            mint=MINT_A,
            mint_authority=AuthorityStatus.UNKNOWN,
            freeze_authority=AuthorityStatus.UNKNOWN,
            blacklisted=RiskSignal.UNKNOWN,
            verified=RiskSignal.UNKNOWN,
            holder_concentration=None,
            observed_at=NOW,
            source_at=NOW,
            ingested_at=NOW,
            data_quality=DataQuality.COMPLETE,
        )


def _live_snapshot(**extra: Any) -> PoolSnapshot:
    """A Data API snapshot: no 5m window and no depth, so it is honestly partial."""

    windows = {name: metric for name, metric in _windows().items() if name != "5m"}
    payload: dict[str, Any] = {
        "pool": PoolRef(address=POOL_ADDRESS, base_mint=MINT_A, quote_mint=MINT_B),
        "windows": windows,
        "tvl": Decimal("100"),
        "depth": None,
        "price": Decimal("1"),
        "bin_step": 20,
        "created_at": datetime(2026, 10, 1, tzinfo=UTC),
        "observed_at": NOW,
        "source_at": NOW,
        "ingested_at": NOW,
        "freshness": Freshness.FRESH,
        "data_quality": DataQuality.PARTIAL,
        "missing_fields": ("depth", "windows.5m"),
        **extra,
    }
    return PoolSnapshot(**payload)


def test_snapshot_carries_execution_anchor_and_short_window_source() -> None:
    """D06 anchor (active bin, both decimals) and the D04 short-window mark are optional."""

    anchored = _live_snapshot(
        active_bin_id=-1387, base_decimals=9, quote_decimals=6, short_window_source="30m"
    )
    bare = _live_snapshot()

    assert (anchored.active_bin_id, anchored.base_decimals, anchored.quote_decimals) == (
        -1387,
        9,
        6,
    )
    assert anchored.short_window_source == "30m"
    assert (bare.active_bin_id, bare.base_decimals, bare.quote_decimals) == (None, None, None)
    assert bare.short_window_source is None
    for invalid in (
        {"base_decimals": -1},
        {"quote_decimals": 256},
        {"active_bin_id": 2**31},
        {"short_window_source": "5m"},  # the mark must name a window the snapshot has
        {"short_window_source": "2h"},
    ):
        with pytest.raises(ValidationError):
            _live_snapshot(**invalid)


def test_snapshot_carries_the_quote_token_usd_price_exactly() -> None:
    """A3: the quote token's USD price (``token_y.price``) converts USD into its units."""

    priced = _live_snapshot(quote_decimals=9, quote_price_usd=Decimal("104.852744278943"))

    assert priced.quote_price_usd == Decimal("104.852744278943")
    assert _live_snapshot().quote_price_usd is None
    for invalid in ({"quote_price_usd": 104.85}, {"quote_price_usd": Decimal("-1")}):
        with pytest.raises(ValidationError):
            _live_snapshot(**invalid)
