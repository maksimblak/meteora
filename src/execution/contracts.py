"""Versioned, SDK-independent contracts shared with the TypeScript executor."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from src.domain.models.primitives import PublicKey
from src.domain.models.trading import TradingMode

BaseUnitString = Annotated[str, StringConstraints(pattern=r"^(0|[1-9][0-9]*)$")]
DigestString = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]


class DlmmOperation(StrEnum):
    OPEN = "OPEN"
    ADD = "ADD"
    REMOVE = "REMOVE"
    CLOSE = "CLOSE"


class LiquidityStrategy(StrEnum):
    SPOT = "SPOT"
    BID_ASK = "BID_ASK"


class ContractModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", use_enum_values=False)


class DlmmCommandV1(ContractModel):
    schema_version: Literal[1] = 1
    command_id: UUID
    idempotency_key: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    correlation_id: UUID
    ruleset_version: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    mode: TradingMode
    operation: DlmmOperation
    pool_address: str
    wallet_address: str
    position_address: str | None = None
    expires_at: datetime
    max_slippage_bps: int = Field(ge=0, le=10_000)
    strategy: LiquidityStrategy | None = None
    min_bin_id: int
    max_bin_id: int
    amount_x: BaseUnitString | None = None
    amount_y: BaseUnitString | None = None
    remove_bps: int | None = Field(default=None, ge=1, le=10_000)

    @field_validator("pool_address", "wallet_address", "position_address")
    @classmethod
    def validate_public_key(cls, value: str | None) -> str | None:
        if value is not None:
            PublicKey.model_validate(value)
        return value

    @field_validator("idempotency_key", "ruleset_version")
    @classmethod
    def validate_non_blank_identifier(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("identifier must not be blank")
        return value

    @field_validator("expires_at")
    @classmethod
    def validate_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("expires_at must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_operation_fields(self) -> DlmmCommandV1:
        if self.min_bin_id > self.max_bin_id:
            raise ValueError("min_bin_id must not exceed max_bin_id")
        if self.operation in {DlmmOperation.OPEN, DlmmOperation.ADD}:
            if self.position_address is None:
                raise ValueError("position_address is required")
            if self.strategy is None or self.amount_x is None or self.amount_y is None:
                raise ValueError("liquidity commands require strategy and both amounts")
            if self.remove_bps is not None:
                raise ValueError("liquidity commands must not set remove_bps")
        else:
            if self.position_address is None or self.remove_bps is None:
                raise ValueError("removal commands require position_address and remove_bps")
            if self.strategy is not None or self.amount_x is not None or self.amount_y is not None:
                raise ValueError("removal commands must not set strategy or amounts")
        if self.operation is DlmmOperation.CLOSE and self.remove_bps != 10_000:
            raise ValueError("close requires remove_bps=10000")
        return self


class SimulationErrorV1(ContractModel):
    code: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    message: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    retryable: bool = False


class SimulationResultV1(ContractModel):
    schema_version: Literal[1] = 1
    command_id: UUID
    command_digest: DigestString
    simulation_digest: DigestString
    success: bool
    simulated_at: datetime
    blockhash: str | None
    last_valid_block_height: int | None = Field(ge=0)
    compute_units: int = Field(ge=0)
    network_fee_lamports: BaseUnitString
    priority_fee_lamports: BaseUnitString
    price_impact_bps: int | None = Field(default=None, ge=0, le=10_000)
    min_bin_id: int
    max_bin_id: int
    errors: tuple[SimulationErrorV1, ...]

    @field_validator("simulated_at")
    @classmethod
    def validate_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("simulated_at must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_outcome(self) -> SimulationResultV1:
        if self.success and self.price_impact_bps is None:
            raise ValueError("successful simulations require measured price impact")
        if not self.success and not self.errors:
            raise ValueError("failed simulations require an error")
        return self


class GatewayState(StrEnum):
    SUBMITTED = "SUBMITTED"
    CONFIRMED = "CONFIRMED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class GatewayReceiptV1(ContractModel):
    schema_version: Literal[1] = 1
    command_id: UUID
    command_digest: DigestString
    simulation_digest: DigestString
    idempotency_key: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    state: GatewayState
    signature: Annotated[str, StringConstraints(min_length=1)] | None = None
    signatures: tuple[Annotated[str, StringConstraints(min_length=1)], ...]
    transaction_count: int = Field(gt=0)
    submitted_at: datetime
    attempt_count: int = Field(ge=0)
    error: SimulationErrorV1 | None = None

    @field_validator("submitted_at")
    @classmethod
    def validate_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("submitted_at must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator("idempotency_key")
    @classmethod
    def validate_non_blank_idempotency_key(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("idempotency_key must not be blank")
        return value

    @model_validator(mode="after")
    def validate_signatures(self) -> GatewayReceiptV1:
        if any(not signature.strip() for signature in self.signatures):
            raise ValueError("signatures must not be blank")
        primary = self.signatures[-1] if self.signatures else None
        if self.signature != primary:
            raise ValueError("signature must be the final submitted signature")
        if (
            self.state in {GatewayState.SUBMITTED, GatewayState.CONFIRMED}
            and len(self.signatures) != self.transaction_count
        ):
            raise ValueError("submitted receipts require every transaction signature")
        if len(self.signatures) > self.transaction_count:
            raise ValueError("signature count exceeds transaction count")
        return self


class ChainState(StrEnum):
    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class ChainReceiptV1(ContractModel):
    schema_version: Literal[1] = 1
    signature: Annotated[str, StringConstraints(min_length=1)]
    state: ChainState
    checked_at: datetime
    slot: int | None = Field(default=None, ge=0)
    error: str | None = None

    @field_validator("checked_at")
    @classmethod
    def validate_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("checked_at must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator("signature")
    @classmethod
    def validate_non_blank_signature(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("signature must not be blank")
        return value


class OperatorApproval(ContractModel):
    approval_id: UUID
    approved_at: datetime
    expires_at: datetime
    operator_id: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    command_digest: DigestString
    simulation_digest: DigestString
    proof: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
    comment: Annotated[str, StringConstraints(max_length=1000)] = ""

    @field_validator("approved_at", "expires_at")
    @classmethod
    def validate_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("approval timestamp must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator("operator_id")
    @classmethod
    def validate_non_blank_operator_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("operator_id must not be blank")
        return value

    @model_validator(mode="after")
    def validate_window(self) -> OperatorApproval:
        if self.expires_at <= self.approved_at:
            raise ValueError("approval expiry must be after approval time")
        return self


class LivePromotionEvidence(ContractModel):
    promotion_id: UUID
    ruleset_version: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    approval_cycles: int = Field(ge=0)
    receipts_reconciled: bool
    risk_invariants_preserved: bool
    dedicated_rpc_ready: bool
    wallet_ready: bool
    checklist_completed: bool
    operator_confirmation_id: Annotated[
        str, StringConstraints(min_length=1, max_length=128)
    ]
    promoted_at: datetime
    expires_at: datetime

    @field_validator("promoted_at", "expires_at")
    @classmethod
    def validate_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("promotion timestamp must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator("operator_confirmation_id")
    @classmethod
    def validate_non_blank_operator_confirmation_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("operator_confirmation_id must not be blank")
        return value

    @model_validator(mode="after")
    def validate_window(self) -> LivePromotionEvidence:
        if self.expires_at <= self.promoted_at:
            raise ValueError("promotion expiry must be after promotion time")
        return self


class ExecutionPhase(StrEnum):
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPROVED = "APPROVED"
    SIMULATED = "SIMULATED"
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"
    CONFIRMED = "CONFIRMED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"


class ExecutionPreview(ContractModel):
    command: DlmmCommandV1
    simulation: SimulationResultV1
    phase: ExecutionPhase
    approval_required: bool
    irreversible_after_submit: Literal[True] = True


class ExecutionState(ContractModel):
    command_id: UUID
    phase: ExecutionPhase
    approval_id: UUID | None = None
    receipt: GatewayReceiptV1 | None = None


def canonical_command_digest(command: DlmmCommandV1) -> str:
    """Return the language-neutral digest binding preview/build/submit."""

    payload = command.model_dump(mode="json", exclude_none=True)
    payload["expires_at"] = _canonical_timestamp(command.expires_at)
    return _sha256(payload)


def canonical_simulation_digest(simulation: SimulationResultV1) -> str:
    """Hash a normalized simulation while excluding its self digest."""

    payload = simulation.model_dump(mode="json")
    payload.pop("simulation_digest", None)
    payload["simulated_at"] = _canonical_timestamp(simulation.simulated_at)
    return _sha256(payload)


def bind_simulation_result(**payload: object) -> SimulationResultV1:
    """Construct a simulation with its canonical digest populated."""

    provisional = SimulationResultV1.model_validate(
        {**payload, "simulation_digest": "0" * 64}
    )
    return provisional.model_copy(
        update={"simulation_digest": canonical_simulation_digest(provisional)}
    )


def _canonical_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _sha256(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
