"""Strict primitives used at every domain boundary."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from pydantic import ConfigDict, RootModel, field_validator


class BaseUnits(RootModel[int]):
    """A non-negative integer amount in the token's smallest unit."""

    model_config = ConfigDict(frozen=True)

    @field_validator("root", mode="before")
    @classmethod
    def validate_base_units(cls, value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("base units must be an integer")
        if value < 0:
            raise ValueError("base units must not be negative")
        return int(value)


class DecimalAmount(RootModel[Decimal]):
    """An exact decimal amount; binary floating point is never accepted."""

    model_config = ConfigDict(frozen=True)

    @field_validator("root", mode="before")
    @classmethod
    def validate_decimal(cls, value: Any) -> Any:
        if isinstance(value, (float, bool)):
            raise ValueError("binary float is forbidden for monetary values")
        return value

    @field_validator("root")
    @classmethod
    def validate_finite(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("monetary value must be finite")
        return value


class UtcTimestamp(RootModel[datetime]):
    """A timezone-aware instant normalized to UTC."""

    model_config = ConfigDict(frozen=True)

    @field_validator("root")
    @classmethod
    def validate_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        return value.astimezone(UTC)


class PublicKey(RootModel[str]):
    """A canonical 32-byte base58 Solana public key."""

    model_config = ConfigDict(frozen=True)

    @field_validator("root")
    @classmethod
    def validate_public_key(cls, value: str) -> str:
        alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
        if not value or any(character not in alphabet for character in value):
            raise ValueError("public key must be base58")

        number = 0
        for character in value:
            number = number * 58 + alphabet.index(character)
        payload = number.to_bytes((number.bit_length() + 7) // 8, "big")
        decoded = (b"\x00" * (len(value) - len(value.lstrip("1")))) + payload
        if len(decoded) != 32:
            raise ValueError("public key must decode to 32 bytes")
        return value
