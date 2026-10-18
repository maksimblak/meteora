from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from src.domain.models.primitives import BaseUnits, DecimalAmount, PublicKey, UtcTimestamp


def test_domain_boundary_types_reject_float_naive_time_and_invalid_public_key() -> None:
    assert BaseUnits(1).root == 1
    assert DecimalAmount("1.25").root == Decimal("1.25")
    assert UtcTimestamp(datetime(2026, 1, 1, tzinfo=UTC)).root.tzinfo is UTC

    with pytest.raises(ValidationError):
        DecimalAmount(1.25)
    with pytest.raises(ValidationError):
        UtcTimestamp(datetime(2026, 1, 1))
    with pytest.raises(ValidationError):
        PublicKey("not-a-solana-public-key")
