"""Versioned, SDK-independent contracts shared with the TypeScript executor.

The executor wire speaks contract **V2** only (``DlmmCommandV2``, ``SimulationResultV2``,
``GatewayReceiptV2``): the executor creates the key of a new position itself, so an OPEN
carries no position address and the receipt returns it; an entry carries the LB-pair
anchor the executor checks on chain; a simulation returns the observed market facts.

**V1** is retired from the wire and rejected there. It stays the *lifecycle shape* the
SHADOW broker and the T04 ledger record (a position address on every command) and the
shape of records stored before V2; ``command_from_payload`` and friends read a stored
record by its own version and refuse any other. ``ChainReceiptV1`` is unchanged.

``QuoteRequestV1`` / ``QuoteResultV1`` are the separate read-only quote contract.

**V2 extension (G22, compatible).** Every V2 payload valid before stays valid with the
same digest; the additions are optional and omitted when absent, on the wire and in the
digests: the ``SWAP`` operation (exact-in sale of the pool's X for its Y; it names the
exited position whose withdrawn tokens it sells, carries ``in_amount`` and its own
``max_price_impact_bps``, uses ``max_slippage_bps`` as the tolerance of its on-chain
minimum output and has no bin range), the simulation's ``swap_quote`` (null bins for a
swap) and the confirmed receipt's ``settlement`` read from the chain.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Final, Literal
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
PositiveBaseUnitString = Annotated[str, StringConstraints(pattern=r"^[1-9][0-9]*$")]
# A signed change of a balance: no plus sign, no negative zero.
SignedBaseUnitString = Annotated[str, StringConstraints(pattern=r"^(0|-?[1-9][0-9]*)$")]
DigestString = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
# SPL mint decimals are a u8.
MintDecimals = Annotated[int, Field(ge=0, le=255)]
EXECUTOR_CONTRACT_VERSION: Final = 2


class DlmmOperation(StrEnum):
    OPEN = "OPEN"
    ADD = "ADD"
    REMOVE = "REMOVE"
    CLOSE = "CLOSE"
    # Contract V2 only: the sale of the base token an exit returned (G22).
    SWAP = "SWAP"


class LiquidityStrategy(StrEnum):
    SPOT = "SPOT"
    BID_ASK = "BID_ASK"


class SwapDirection(StrEnum):
    """Only the sale of the base token (X) for the quote token (Y): buying was removed."""

    X_TO_Y = "X_TO_Y"


class ContractModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", use_enum_values=False)


def _public_key(value: str | None) -> str | None:
    if value is not None:
        PublicKey.model_validate(value)
    return value


def _non_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("identifier must not be blank")
    return value


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC)


class DlmmCommandV1(ContractModel):
    """Lifecycle command: SHADOW fills, the T04 ledger and records stored before V2."""

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
        if self.operation is DlmmOperation.SWAP:
            raise ValueError("the V1 lifecycle shape has no SWAP")
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


_LIQUIDITY_OPERATIONS = frozenset({DlmmOperation.OPEN, DlmmOperation.ADD})


class DlmmCommandV2(ContractModel):
    """Executor wire command.

    OPEN carries no ``position_address``: the executor creates the position's key and
    the receipt names the address. OPEN and ADD carry the anchor the bin range and the
    amounts were derived from (``active_bin_id`` +- ``active_bin_tolerance``, mint
    decimals); the executor refuses the command when the chain disagrees.

    SWAP sells ``in_amount`` of the pool's X for Y (exact in) in the same pool. It names
    the exited position whose withdrawn tokens it sells (the executor does not act on
    it), has no bin range, refuses a quote above ``max_price_impact_bps`` and sends the
    quote's output less ``max_slippage_bps`` as the on-chain minimum output.
    """

    schema_version: Literal[2] = 2
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
    # Required by every operation but SWAP, which must not carry them.
    min_bin_id: int | None = None
    max_bin_id: int | None = None
    amount_x: BaseUnitString | None = None
    amount_y: BaseUnitString | None = None
    remove_bps: int | None = Field(default=None, ge=1, le=10_000)
    active_bin_id: int | None = None
    active_bin_tolerance: int | None = Field(default=None, ge=0, le=1_000)
    token_x_decimals: MintDecimals | None = None
    token_y_decimals: MintDecimals | None = None
    swap_direction: SwapDirection | None = None
    in_amount: PositiveBaseUnitString | None = None
    max_price_impact_bps: int | None = Field(default=None, ge=0, le=10_000)

    _keys = field_validator("pool_address", "wallet_address", "position_address")(
        _public_key
    )
    _identifiers = field_validator("idempotency_key", "ruleset_version")(_non_blank)
    _expiry = field_validator("expires_at")(_utc)

    @model_validator(mode="after")
    def validate_operation_fields(self) -> DlmmCommandV2:
        anchor = (
            self.active_bin_id,
            self.active_bin_tolerance,
            self.token_x_decimals,
            self.token_y_decimals,
        )
        swap = (self.swap_direction, self.in_amount, self.max_price_impact_bps)
        if self.operation is DlmmOperation.SWAP:
            if self.position_address is None:
                raise ValueError("a swap names the exited position it settles")
            if any(value is None for value in swap):
                raise ValueError("a swap requires its direction, amount and impact limit")
            if self.min_bin_id is not None or self.max_bin_id is not None:
                raise ValueError("a swap has no bin range")
            if (
                self.strategy is not None
                or self.amount_x is not None
                or self.amount_y is not None
                or self.remove_bps is not None
                or any(value is not None for value in anchor)
            ):
                raise ValueError("a swap carries no liquidity, removal or anchor fields")
            return self
        if any(value is not None for value in swap):
            raise ValueError("only a swap carries swap fields")
        if self.min_bin_id is None or self.max_bin_id is None:
            raise ValueError("liquidity commands require a bin range")
        if self.min_bin_id > self.max_bin_id:
            raise ValueError("min_bin_id must not exceed max_bin_id")
        if self.operation is DlmmOperation.OPEN and self.position_address is not None:
            raise ValueError("the executor creates the position of an OPEN")
        if self.operation is not DlmmOperation.OPEN and self.position_address is None:
            raise ValueError("position_address is required")
        if self.operation in _LIQUIDITY_OPERATIONS:
            if self.strategy is None or self.amount_x is None or self.amount_y is None:
                raise ValueError("liquidity commands require strategy and both amounts")
            if any(value is None for value in anchor):
                raise ValueError("liquidity commands require the active bin anchor and decimals")
            if self.remove_bps is not None:
                raise ValueError("liquidity commands must not set remove_bps")
            return self
        if self.remove_bps is None:
            raise ValueError("removal commands require remove_bps")
        if self.strategy is not None or self.amount_x is not None or self.amount_y is not None:
            raise ValueError("removal commands must not set strategy or amounts")
        if any(value is not None for value in anchor):
            raise ValueError("removal commands must not carry an entry anchor")
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


class SwapQuoteV2(ContractModel):
    """The official SDK's exact-in quote a SWAP was built with (base units).

    ``min_out_amount`` is the minimum output written into the transaction: the quoted
    output less the command's ``max_slippage_bps``; below it the swap fails on chain."""

    in_amount: PositiveBaseUnitString
    out_amount: BaseUnitString
    min_out_amount: BaseUnitString
    fee_amount: BaseUnitString

    @model_validator(mode="after")
    def validate_minimum(self) -> SwapQuoteV2:
        if int(self.min_out_amount) > int(self.out_amount):
            raise ValueError("the minimum output cannot exceed the quoted output")
        return self


class SimulationResultV2(ContractModel):
    """Executor simulation: costs, the SDK-quoted price impact of the swap leg, the
    position the command acts on (for an OPEN the one the executor created) and what
    the chain says right now: the LB pair's active bin and the mint decimals.

    ``price_impact_bps`` may be ``None`` on a successful exit: an exit swaps nothing,
    its quote is informational and a thin pool must never keep a position open. An
    entry without a measured impact is refused by the executor and by the service.

    A SWAP has no bin range (``None``) and reports its ``swap_quote``; the extension
    field is absent for every other operation."""

    schema_version: Literal[2] = 2
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
    price_impact_bps: int | None = Field(ge=0, le=10_000)
    min_bin_id: int | None
    max_bin_id: int | None
    position_address: str | None
    active_bin_id: int | None
    token_x_decimals: MintDecimals | None
    token_y_decimals: MintDecimals | None
    errors: tuple[SimulationErrorV1, ...]
    swap_quote: SwapQuoteV2 | None = None

    _key = field_validator("position_address")(_public_key)
    _timestamp = field_validator("simulated_at")(_utc)

    @model_validator(mode="after")
    def validate_outcome(self) -> SimulationResultV2:
        if self.success and None in (
            self.position_address,
            self.active_bin_id,
            self.token_x_decimals,
            self.token_y_decimals,
        ):
            raise ValueError("successful simulations require the position and market facts")
        if self.success and self.swap_quote is None and (
            self.min_bin_id is None or self.max_bin_id is None
        ):
            raise ValueError("a successful simulation carries a bin range or a swap quote")
        if self.success and self.swap_quote is not None and int(self.swap_quote.out_amount) == 0:
            raise ValueError("a successful swap quote has an output")
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


class SettlementV2(ContractModel):
    """What the chain says a confirmed command did to the wallet (all its transactions).

    ``fee_lamports`` is the fee the wallet paid, ``priority_fee_lamports`` its part above
    the base signature fee. ``delta_x`` / ``delta_y`` are the signed changes
    of the wallet's X and Y in base units; for wrapped SOL the native lamports the wallet
    received net of the fee and of token-account rent count as the token. ``mint_x`` /
    ``mint_y`` name X and Y, so the receipt alone tells whether its lamport fees are quote
    units (Y is wrapped SOL)."""

    fee_lamports: BaseUnitString
    priority_fee_lamports: BaseUnitString
    delta_x: SignedBaseUnitString
    delta_y: SignedBaseUnitString
    mint_x: str
    mint_y: str

    _mints = field_validator("mint_x", "mint_y")(_public_key)

    @model_validator(mode="after")
    def validate_fees(self) -> SettlementV2:
        if int(self.priority_fee_lamports) > int(self.fee_lamports):
            raise ValueError("the priority fee is part of the paid fee")
        return self


class GatewayReceiptV2(ContractModel):
    """Executor receipt; ``position_address`` is the only source of a new position's address.

    ``settlement`` (extension) is the chain's account of a CONFIRMED command, ``None``
    until the executor could read it. Deliberately not a subclass of
    ``GatewayReceiptV1``: a V2 receipt must never pass where the V1 lifecycle shape is
    expected.
    """

    schema_version: Literal[2] = 2
    command_id: UUID
    command_digest: DigestString
    simulation_digest: DigestString
    idempotency_key: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    state: GatewayState
    position_address: str
    signature: Annotated[str, StringConstraints(min_length=1)] | None = None
    signatures: tuple[Annotated[str, StringConstraints(min_length=1)], ...]
    transaction_count: int = Field(gt=0)
    submitted_at: datetime
    attempt_count: int = Field(ge=0)
    error: SimulationErrorV1 | None = None
    settlement: SettlementV2 | None = None

    _key = field_validator("position_address")(_public_key)
    _identifier = field_validator("idempotency_key")(_non_blank)
    _timestamp = field_validator("submitted_at")(_utc)

    @model_validator(mode="after")
    def validate_signatures(self) -> GatewayReceiptV2:
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
        if self.settlement is not None and self.state is not GatewayState.CONFIRMED:
            raise ValueError("only a confirmed command has a settlement")
        return self

    def lifecycle(self) -> GatewayReceiptV1:
        """The same receipt in the V1 lifecycle shape the T04 ledger records."""

        return GatewayReceiptV1.model_validate(
            self.model_dump(exclude={"schema_version", "position_address", "settlement"})
        )


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


# A record of either version; the executor wire itself accepts V2 only.
type DlmmCommand = DlmmCommandV1 | DlmmCommandV2
type SimulationResult = SimulationResultV1 | SimulationResultV2
type GatewayReceipt = GatewayReceiptV1 | GatewayReceiptV2

_COMMAND = Annotated[DlmmCommandV1 | DlmmCommandV2, Field(discriminator="schema_version")]
_SIMULATION = Annotated[
    SimulationResultV1 | SimulationResultV2, Field(discriminator="schema_version")
]
_RECEIPT = Annotated[GatewayReceiptV1 | GatewayReceiptV2, Field(discriminator="schema_version")]


def command_from_payload(payload: Mapping[str, Any]) -> DlmmCommand:
    """A stored command by its own ``schema_version``; any other version is refused."""

    if payload.get("schema_version") == 1:
        return DlmmCommandV1.model_validate(payload)
    return DlmmCommandV2.model_validate(payload)


def simulation_from_payload(payload: Mapping[str, Any]) -> SimulationResult:
    if payload.get("schema_version") == 1:
        return SimulationResultV1.model_validate(payload)
    return SimulationResultV2.model_validate(payload)


def receipt_from_payload(payload: Mapping[str, Any]) -> GatewayReceipt:
    if payload.get("schema_version") == 1:
        return GatewayReceiptV1.model_validate(payload)
    return GatewayReceiptV2.model_validate(payload)


class QuoteDirection(StrEnum):
    X_TO_Y = "X_TO_Y"
    Y_TO_X = "Y_TO_X"


class QuoteRequestV1(ContractModel):
    """Read-only quote of a reference size (``in_amount`` base units of the input token)."""

    schema_version: Literal[1] = 1
    pool_address: str
    direction: QuoteDirection
    in_amount: PositiveBaseUnitString

    _pool = field_validator("pool_address")(_public_key)


class QuoteResultV1(ContractModel):
    """Price impact and expected output by the official SDK quote, or why there is none."""

    schema_version: Literal[1] = 1
    pool_address: str
    direction: QuoteDirection
    in_amount: PositiveBaseUnitString
    quoted_at: datetime
    success: bool
    consumed_in_amount: BaseUnitString | None
    out_amount: BaseUnitString | None
    fee_amount: BaseUnitString | None
    price_impact_bps: int | None = Field(ge=0, le=10_000)
    active_bin_id: int | None
    token_x_decimals: MintDecimals | None
    token_y_decimals: MintDecimals | None
    error: SimulationErrorV1 | None

    _pool = field_validator("pool_address")(_public_key)
    _timestamp = field_validator("quoted_at")(_utc)

    @model_validator(mode="after")
    def validate_outcome(self) -> QuoteResultV1:
        measured = (
            self.consumed_in_amount,
            self.out_amount,
            self.fee_amount,
            self.price_impact_bps,
            self.active_bin_id,
            self.token_x_decimals,
            self.token_y_decimals,
        )
        if self.success and (self.error is not None or None in measured):
            raise ValueError("a successful quote carries every measured value and no error")
        if not self.success and self.error is None:
            raise ValueError("a failed quote needs an error")
        return self


class ExecutionPreview(ContractModel):
    command: _COMMAND
    simulation: _SIMULATION
    phase: ExecutionPhase
    approval_required: bool
    irreversible_after_submit: Literal[True] = True


class ExecutionState(ContractModel):
    command_id: UUID
    phase: ExecutionPhase
    approval_id: UUID | None = None
    receipt: _RECEIPT | None = None


_SIMULATION_EXTENSIONS: Final = ("swap_quote",)


def canonical_command_digest(command: DlmmCommand) -> str:
    """Return the language-neutral digest binding preview/build/submit."""

    payload = command.model_dump(mode="json", exclude_none=True)
    payload["expires_at"] = _canonical_timestamp(command.expires_at)
    return _sha256(payload)


def canonical_simulation_digest(simulation: SimulationResult) -> str:
    """Hash a normalized simulation while excluding its self digest.

    An absent extension field (``swap_quote``) is omitted, as the executor omits it, so
    every simulation digested before the extension keeps its digest."""

    payload = simulation.model_dump(mode="json")
    payload.pop("simulation_digest", None)
    for name in _SIMULATION_EXTENSIONS:
        if payload.get(name, ...) is None:
            del payload[name]
    payload["simulated_at"] = _canonical_timestamp(simulation.simulated_at)
    return _sha256(payload)


def bind_simulation_result(**payload: object) -> SimulationResultV1:
    """Construct a V1 (lifecycle) simulation with its canonical digest populated."""

    provisional = SimulationResultV1.model_validate(
        {**payload, "simulation_digest": "0" * 64}
    )
    return provisional.model_copy(
        update={"simulation_digest": canonical_simulation_digest(provisional)}
    )


def bind_simulation_result_v2(**payload: object) -> SimulationResultV2:
    """Construct a V2 simulation with its canonical digest populated."""

    provisional = SimulationResultV2.model_validate(
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
