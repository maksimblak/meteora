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
