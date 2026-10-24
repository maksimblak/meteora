"""Approval, idempotency and reconciliation state machine for on-chain commands."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol
from uuid import UUID

from src.core.clock import Clock
from src.domain.models.trading import TradingMode
from src.execution.contracts import (
    ChainReceiptV1,
    ChainState,
    DlmmCommand,
    DlmmCommandV2,
    DlmmOperation,
    ExecutionPhase,
    ExecutionPreview,
    ExecutionState,
    GatewayReceipt,
    GatewayReceiptV2,
    GatewayState,
    LivePromotionEvidence,
    OperatorApproval,
    SimulationResult,
    SimulationResultV2,
    canonical_command_digest,
    canonical_simulation_digest,
    command_from_payload,
    receipt_from_payload,
    simulation_from_payload,
)
from src.modes.models import KillSwitchPolicy

EXIT_OPERATIONS = frozenset({DlmmOperation.REMOVE, DlmmOperation.CLOSE})
# Only an entry is limited by the quoted price impact of its swap leg: an exit swaps
# nothing, so its quote is recorded but never keeps a position open.
ENTRY_OPERATIONS = frozenset({DlmmOperation.OPEN, DlmmOperation.ADD})
_UNRESOLVED = frozenset(
    {ExecutionPhase.SUBMITTING, ExecutionPhase.SUBMITTED, ExecutionPhase.UNKNOWN}
)


class ExecutionBlocked(RuntimeError):
    """The command failed a fail-closed execution invariant."""


class ExecutionOutcomeUnknown(ExecutionBlocked):
    """The command may already be broadcast: it is never resent, only reconciled."""


class SimulationRejected(ExecutionBlocked):
    """The simulation refused the command; ``code`` is the executor's error code (or
    ``SLIPPAGE_BREACH`` / ``PRICE_IMPACT_UNAVAILABLE`` for an entry over its limit)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class GatewayRejected(ExecutionBlocked):
    """The gateway provably did not execute the request: nothing was broadcast."""

    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code


class ExecutionGateway(Protocol):
    async def simulate(self, command: DlmmCommand) -> SimulationResult: ...

    async def submit(
        self,
        command: DlmmCommand,
        idempotency_key: str,
        approval: OperatorApproval | None,
    ) -> GatewayReceipt: ...

    async def status(self, signature: str) -> ChainReceiptV1: ...


class ReceiptLookup(Protocol):
    """Command-level status (``GET /v1/commands/{id}``): the gateway's reconciled receipt."""

    async def lookup(self, command_id: UUID) -> GatewayReceipt | None: ...


class KillSwitchProbe(Protocol):
    """The durable kill switch, re-read on every call."""

    async def kill_switch_active(self) -> bool: ...

    async def kill_switch_policy(self) -> KillSwitchPolicy | None:
        """The policy for open positions chosen at activation; ``None`` while inactive."""
        ...


class PlanResolver(Protocol):
    async def resolve(self, plan_id: UUID) -> DlmmCommand: ...


class ApprovalVerifier(Protocol):
    def verify(self, approval: OperatorApproval) -> bool: ...


class HmacApprovalVerifier:
    """Authenticate an approval and bind it to one command/simulation preview."""

    def __init__(self, secret: bytes) -> None:
        if len(secret) < 32:
            raise ValueError("approval secret must contain at least 32 bytes")
        self._secret = secret

    def verify(self, approval: OperatorApproval) -> bool:
        expected = sign_operator_approval(
            self._secret,
            approval_id=approval.approval_id,
            approved_at=approval.approved_at,
            expires_at=approval.expires_at,
            operator_id=approval.operator_id,
            command_digest=approval.command_digest,
            simulation_digest=approval.simulation_digest,
        )
        return hmac.compare_digest(expected, approval.proof)


def sign_operator_approval(
    secret: bytes,
    *,
    approval_id: UUID,
    approved_at: datetime,
    expires_at: datetime,
    operator_id: str,
    command_digest: str,
    simulation_digest: str,
) -> str:
    """Create the proof consumed by both Python orchestration and the gateway."""

    message = "\n".join(
        (
            str(approval_id),
            _canonical_timestamp(approved_at),
            _canonical_timestamp(expires_at),
            operator_id,
            command_digest,
            simulation_digest,
        )
    ).encode()
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


@dataclass
class ExecutionRecord:
    command: DlmmCommand
    simulation: SimulationResult
    phase: ExecutionPhase
    approval: OperatorApproval | None = None
    approval_consumed: bool = False
    receipt: GatewayReceipt | None = None

    @property
    def position_address(self) -> str | None:
        """The position this command acts on, as far as it is known.

        A V2 OPEN gets its position from the executor: only its receipt names it, so
        before the receipt it is ``None``. A V1 lifecycle command carried its own.
        """

        if isinstance(self.receipt, GatewayReceiptV2):
            return self.receipt.position_address
        return self.command.position_address


class ExecutionStore(Protocol):
    async def get(self, command_id: UUID) -> ExecutionRecord | None: ...

    async def get_by_idempotency_key(self, key: str) -> ExecutionRecord | None: ...

    async def put(self, record: ExecutionRecord) -> None: ...

    async def claim_approval(
        self, approval_id: UUID, command_digest: str, simulation_digest: str
    ) -> bool: ...


class InMemoryExecutionStore:
    """Fast test seam implementing the same uniqueness rules as durable storage."""

    def __init__(self) -> None:
        self._by_command: dict[UUID, ExecutionRecord] = {}
        self._by_key: dict[str, UUID] = {}
        self._approval_ids: set[UUID] = set()

    async def get(self, command_id: UUID) -> ExecutionRecord | None:
        return self._by_command.get(command_id)

    async def get_by_idempotency_key(self, key: str) -> ExecutionRecord | None:
        command_id = self._by_key.get(key)
        return None if command_id is None else self._by_command.get(command_id)

    async def put(self, record: ExecutionRecord) -> None:
        existing_record = self._by_command.get(record.command.command_id)
        if existing_record is not None and (
            existing_record.command.idempotency_key
            != record.command.idempotency_key
            or canonical_command_digest(existing_record.command)
            != canonical_command_digest(record.command)
        ):
            raise ExecutionBlocked("command id belongs to another command payload")
        existing_id = self._by_key.get(record.command.idempotency_key)
        if existing_id is not None and existing_id != record.command.command_id:
            raise ExecutionBlocked("idempotency key belongs to another command")
        self._by_command[record.command.command_id] = record
        self._by_key[record.command.idempotency_key] = record.command.command_id

    async def claim_approval(
        self, approval_id: UUID, command_digest: str, simulation_digest: str
    ) -> bool:
        del command_digest, simulation_digest
        if approval_id in self._approval_ids:
            return False
        self._approval_ids.add(approval_id)
        return True


class SqliteExecutionStore:
    """Durable local execution state with transactional uniqueness across restarts."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        if not str(self._path).strip():
            raise ValueError("execution store path is required")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self._path)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS execution_records (
                command_id TEXT PRIMARY KEY,
                idempotency_key TEXT NOT NULL UNIQUE,
                payload TEXT NOT NULL
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS approval_claims (
                approval_id TEXT PRIMARY KEY,
                command_digest TEXT NOT NULL,
                simulation_digest TEXT NOT NULL
            )
            """
        )
        self._connection.commit()
        self._lock = asyncio.Lock()

    async def get(self, command_id: UUID) -> ExecutionRecord | None:
        async with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM execution_records WHERE command_id = ?",
                (str(command_id),),
            ).fetchone()
        return None if row is None else _deserialize_record(str(row[0]))

    async def get_by_idempotency_key(self, key: str) -> ExecutionRecord | None:
        async with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM execution_records WHERE idempotency_key = ?",
                (key,),
            ).fetchone()
        return None if row is None else _deserialize_record(str(row[0]))

    async def put(self, record: ExecutionRecord) -> None:
        payload = _serialize_record(record)
        async with self._lock:
            try:
                with self._connection:
                    existing_row = self._connection.execute(
                        "SELECT payload FROM execution_records WHERE command_id = ?",
                        (str(record.command.command_id),),
                    ).fetchone()
                    if existing_row is not None:
                        existing_record = _deserialize_record(str(existing_row[0]))
                        if (
                            existing_record.command.idempotency_key
                            != record.command.idempotency_key
                            or canonical_command_digest(existing_record.command)
                            != canonical_command_digest(record.command)
                        ):
                            raise ExecutionBlocked(
                                "command id belongs to another command payload"
                            )
                    conflict = self._connection.execute(
                        """
                        SELECT command_id FROM execution_records
                        WHERE idempotency_key = ? AND command_id != ?
                        """,
                        (
                            record.command.idempotency_key,
                            str(record.command.command_id),
                        ),
                    ).fetchone()
                    if conflict is not None:
                        raise ExecutionBlocked(
                            "idempotency key belongs to another command"
                        )
                    self._connection.execute(
                        """
                        INSERT INTO execution_records(command_id, idempotency_key, payload)
                        VALUES (?, ?, ?)
                        ON CONFLICT(command_id) DO UPDATE SET
                            idempotency_key = excluded.idempotency_key,
                            payload = excluded.payload
                        """,
                        (
                            str(record.command.command_id),
                            record.command.idempotency_key,
                            payload,
                        ),
                    )
            except sqlite3.IntegrityError as error:
                raise ExecutionBlocked(
                    "idempotency key belongs to another command"
                ) from error

    async def claim_approval(
        self, approval_id: UUID, command_digest: str, simulation_digest: str
    ) -> bool:
        async with self._lock:
            try:
                with self._connection:
                    self._connection.execute(
                        """
                        INSERT INTO approval_claims(
                            approval_id, command_digest, simulation_digest
                        ) VALUES (?, ?, ?)
                        """,
                        (str(approval_id), command_digest, simulation_digest),
                    )
            except sqlite3.IntegrityError:
                return False
        return True

    def close(self) -> None:
        self._connection.close()


class StaticPlanResolver:
    def __init__(self, commands: Mapping[UUID, DlmmCommand]) -> None:
        self._commands = dict(commands)

    async def resolve(self, plan_id: UUID) -> DlmmCommand:
        try:
            return self._commands[plan_id]
        except KeyError as error:
            raise ExecutionBlocked("plan not found") from error


class ExecutionService:
    def __init__(
        self,
        *,
        gateway: ExecutionGateway,
        store: ExecutionStore,
        plans: PlanResolver,
        clock: Clock,
        allowed_mode: TradingMode,
        simulation_ttl: timedelta = timedelta(minutes=2),
        approval_verifier: ApprovalVerifier | None = None,
        live_promotion: LivePromotionEvidence | None = None,
        receipts: ReceiptLookup | None = None,
        kill_switch: KillSwitchProbe | None = None,
    ) -> None:
        """``kill_switch`` is re-read immediately before every submit, together with the
        policy the operator chose when activating it (spec 7.3; no configured policy can
        override it); without it nothing is ever submitted. ``receipts`` reconciles by
        command id (including a submit that timed out before any signature was known)."""

        if simulation_ttl <= timedelta(0):
            raise ValueError("simulation_ttl must be positive")
        self._gateway = gateway
        self._store = store
        self._plans = plans
        self._clock = clock
        self._allowed_mode = allowed_mode
        self._simulation_ttl = simulation_ttl
        self._approval_verifier = approval_verifier
        self._live_promotion = live_promotion
        self._receipts = receipts
        self._kill_switch = kill_switch

    async def prepare(self, plan_id: UUID) -> ExecutionPreview:
        command = await self._plans.resolve(plan_id)
        now = self._clock.now()
        if command.expires_at <= now:
            raise ExecutionBlocked("plan expired")

        existing = await self._store.get_by_idempotency_key(command.idempotency_key)
        existing_by_command = await self._store.get(command.command_id)
        if existing_by_command is not None:
            if (
                existing_by_command.command.idempotency_key
                != command.idempotency_key
                or canonical_command_digest(existing_by_command.command)
                != canonical_command_digest(command)
            ):
                raise ExecutionBlocked("command id belongs to another command payload")
            if existing is None:
                existing = existing_by_command
        if existing is not None:
            if (
                existing.command.command_id != command.command_id
                or canonical_command_digest(existing.command)
                != canonical_command_digest(command)
            ):
                raise ExecutionBlocked("idempotency key belongs to another command payload")
            if (
                existing.receipt is not None
                or existing.phase
                in {ExecutionPhase.SUBMITTING, ExecutionPhase.UNKNOWN}
                or self._simulation_is_fresh(existing, now)
            ):
                return self._preview(existing)
            if _position_simulated(existing):
                # Its position key existed only for that simulation: a new one would be
                # another position the stored context and any approval know nothing of.
                existing.phase = ExecutionPhase.EXPIRED
                await self._store.put(existing)
                raise ExecutionBlocked(
                    "the simulated position expired before submit; plan the entry again"
                )

        simulation = await self._gateway.simulate(command)
        self._validate_simulation_binding(command, simulation)
        phase = (
            ExecutionPhase.AWAITING_APPROVAL
            if command.mode is TradingMode.APPROVAL
            else ExecutionPhase.SIMULATED
        )
        if existing is None:
            record = ExecutionRecord(
                command=command,
                simulation=simulation,
                phase=phase,
            )
        else:
            record = existing
            record.simulation = simulation
            record.phase = phase
            record.approval = None
            record.approval_consumed = False
        rejection = _simulation_rejection(command, simulation)
        if rejection is not None:
            record.phase = ExecutionPhase.FAILED
            await self._store.put(record)
            raise SimulationRejected(rejection)
        await self._store.put(record)
        return self._preview(record)

    async def approve(
        self, command_id: UUID, approval: OperatorApproval
    ) -> ExecutionState:
        record = await self._require_record(command_id)
        now = self._clock.now()
        if record.command.mode is not TradingMode.APPROVAL:
            raise ExecutionBlocked("approval is only valid in APPROVAL mode")
        if record.phase is not ExecutionPhase.AWAITING_APPROVAL:
            raise ExecutionBlocked("approval already used or command is not awaiting approval")
        if self._approval_verifier is None or not self._approval_verifier.verify(
            approval
        ):
            raise ExecutionBlocked("approval proof is invalid")
        if (
            approval.command_digest != record.simulation.command_digest
            or approval.simulation_digest != record.simulation.simulation_digest
        ):
            raise ExecutionBlocked("approval does not match the current preview")
        if approval.approved_at > now or approval.approved_at < record.simulation.simulated_at:
            raise ExecutionBlocked("approval is not valid for the current simulation")
        if approval.expires_at <= now:
            raise ExecutionBlocked("approval expired or not yet valid")
        if approval.expires_at > record.command.expires_at:
            raise ExecutionBlocked("approval outlives the plan")
        if approval.expires_at > record.simulation.simulated_at + self._simulation_ttl:
            raise ExecutionBlocked("approval outlives the simulation")
        if not await self._store.claim_approval(
            approval.approval_id,
            approval.command_digest,
            approval.simulation_digest,
        ):
            raise ExecutionBlocked("approval already used")
        record.approval = approval
        record.phase = ExecutionPhase.APPROVED
        await self._store.put(record)
        return self._state(record)

    async def execute(self, command_id: UUID) -> GatewayReceipt:
        record = await self._require_record(command_id)
        if record.receipt is not None:
            if record.receipt.state in {GatewayState.SUBMITTED, GatewayState.UNKNOWN} and (
                record.receipt.signature is not None or self._receipts is not None
            ):
                reconciled = await self.reconcile(command_id)
                if reconciled.receipt is None:  # pragma: no cover - contract guard
                    raise ExecutionBlocked("receipt disappeared during reconciliation")
                return reconciled.receipt
            return record.receipt

        if record.phase in {ExecutionPhase.SUBMITTING, ExecutionPhase.UNKNOWN}:
            if record.phase is ExecutionPhase.SUBMITTING:
                record.phase = ExecutionPhase.UNKNOWN
                await self._store.put(record)
            if self._receipts is not None:
                reconciled = await self.reconcile(command_id)
                if reconciled.receipt is not None:
                    return reconciled.receipt
            raise ExecutionOutcomeUnknown(
                "submit outcome is unknown; it is reconciled, never resent"
            )
        if record.phase in {ExecutionPhase.FAILED, ExecutionPhase.EXPIRED}:
            raise ExecutionBlocked("execution is in a terminal state")

        try:
            self._validate_for_submit(record)
        except ExecutionBlocked:
            await self._store.put(record)
            raise
        if record.approval is not None:
            record.approval_consumed = True
        record.phase = ExecutionPhase.SUBMITTING
        await self._store.put(record)

        # Last check before the irreversible call: nothing was sent yet, so a block
        # here is a definitive FAILED, never an unknown outcome.
        try:
            await self._require_kill_switch_clear(record.command)
        except ExecutionBlocked:
            record.phase = ExecutionPhase.FAILED
            await self._store.put(record)
            raise
        try:
            receipt = await self._gateway.submit(
                record.command,
                record.command.idempotency_key,
                record.approval,
            )
        except GatewayRejected:
            record.phase = ExecutionPhase.FAILED
            await self._store.put(record)
            raise
        except Exception as error:
            record.phase = ExecutionPhase.UNKNOWN
            await self._store.put(record)
            raise ExecutionOutcomeUnknown(
                "submit outcome is unknown; it is reconciled, never resent"
            ) from error
        try:
            self._validate_receipt_binding(record, receipt)
        except ExecutionBlocked as error:
            record.phase = ExecutionPhase.UNKNOWN
            await self._store.put(record)
            raise ExecutionOutcomeUnknown(str(error)) from error
        record.receipt = receipt
        record.phase = self._phase_from_gateway(receipt.state)
        await self._store.put(record)
        return receipt

    async def get_state(self, command_id: UUID) -> ExecutionState | None:
        record = await self._store.get(command_id)
        return None if record is None else self._state(record)

    async def reconcile(self, command_id: UUID) -> ExecutionState:
        """Settle an unresolved command from the gateway; never resubmits.

        With ``receipts`` the gateway's own receipt for the command id is adopted
        (it works without any signature, e.g. after a submit timeout); otherwise
        every known signature is looked up with ``status``.
        """

        record = await self._require_record(command_id)
        if record.receipt is not None and record.receipt.state in {
            GatewayState.CONFIRMED,
            GatewayState.FAILED,
        }:
            return self._state(record)
        if self._receipts is not None and record.phase in _UNRESOLVED:
            found = await self._receipts.lookup(command_id)
            if found is not None:
                return await self._adopt_receipt(record, found)
            if record.receipt is None or record.receipt.signature is None:
                # The gateway holds no submission for it: the outcome stays unknown.
                return self._state(record)
        if record.receipt is None or record.receipt.signature is None:
            raise ExecutionBlocked("command has no submitted signature")

        statuses: list[ChainReceiptV1] = []
        lookup_failed = False
        for signature in record.receipt.signatures:
            try:
                statuses.append(await self._gateway.status(signature))
            except Exception:
                lookup_failed = True
        if any(receipt.state is ChainState.FAILED for receipt in statuses):
            state = GatewayState.FAILED
        elif (
            len(record.receipt.signatures) == record.receipt.transaction_count
            and statuses
            and all(receipt.state is ChainState.CONFIRMED for receipt in statuses)
        ):
            state = GatewayState.CONFIRMED
        elif (
            len(record.receipt.signatures) < record.receipt.transaction_count
            or lookup_failed
            or any(receipt.state is ChainState.UNKNOWN for receipt in statuses)
        ):
            state = GatewayState.UNKNOWN
        else:
            state = GatewayState.SUBMITTED
        record.receipt = record.receipt.model_copy(
            update={"state": state, "error": None}
        )
        record.phase = self._phase_from_gateway(state)
        await self._store.put(record)
        return self._state(record)

    async def _adopt_receipt(
        self, record: ExecutionRecord, receipt: GatewayReceipt
    ) -> ExecutionState:
        self._validate_receipt_binding(record, receipt)
        if record.receipt is not None and not set(record.receipt.signatures) <= set(
            receipt.signatures
        ):
            raise ExecutionBlocked("gateway receipt lost signatures that were already known")
        record.receipt = receipt
        record.phase = self._phase_from_gateway(receipt.state)
        await self._store.put(record)
        return self._state(record)

    async def _require_kill_switch_clear(self, command: DlmmCommand) -> None:
        """Active kill switch: no entry ever; an exit only under the ``ORDERLY_EXIT``
        policy chosen at activation."""

        probe = self._kill_switch
        if probe is None:
            raise ExecutionBlocked("kill switch state is not wired; submit refused")
        try:
            active = await probe.kill_switch_active()
            policy = await probe.kill_switch_policy() if active else None
        except Exception as error:
            raise ExecutionBlocked("kill switch state is unavailable") from error
        if active and not (
            command.operation in EXIT_OPERATIONS and policy is KillSwitchPolicy.ORDERLY_EXIT
        ):
            raise ExecutionBlocked("kill switch is active")

    def _validate_for_submit(self, record: ExecutionRecord) -> None:
        now = self._clock.now()
        command = record.command
        if self._allowed_mode is TradingMode.SHADOW or command.mode is TradingMode.SHADOW:
            raise ExecutionBlocked("SHADOW mode never submits")
        if command.mode is not self._allowed_mode:
            raise ExecutionBlocked("command mode is not currently authorized")
        if command.expires_at <= now:
            record.phase = ExecutionPhase.EXPIRED
            raise ExecutionBlocked("plan expired")
        self._validate_simulation_binding(command, record.simulation)
        rejection = _simulation_rejection(command, record.simulation)
        if rejection is not None:
            raise SimulationRejected(rejection)
        if (
            record.simulation.simulated_at > now
            or record.simulation.simulated_at + self._simulation_ttl <= now
        ):
            raise ExecutionBlocked("simulation is stale")
        if command.mode is TradingMode.APPROVAL:
            approval = record.approval
            if approval is None:
                raise ExecutionBlocked("approval required")
            if record.approval_consumed:
                raise ExecutionBlocked("approval already used")
            self._validate_approval_for_submit(record, approval, now)
        if command.mode is TradingMode.LIVE:
            self._validate_live_promotion(command, now)

    def _validate_live_promotion(
        self, command: DlmmCommand, now: datetime
    ) -> None:
        evidence = self._live_promotion
        if (
            evidence is None
            or evidence.ruleset_version != command.ruleset_version
            or evidence.approval_cycles < 10
            or not evidence.receipts_reconciled
            or not evidence.risk_invariants_preserved
            or not evidence.dedicated_rpc_ready
            or not evidence.wallet_ready
            or not evidence.checklist_completed
            or not evidence.operator_confirmation_id.strip()
            or evidence.promoted_at > now
            or evidence.expires_at <= now
        ):
            raise ExecutionBlocked("LIVE promotion gate is not satisfied")

    def _validate_approval_for_submit(
        self,
        record: ExecutionRecord,
        approval: OperatorApproval,
        now: datetime,
    ) -> None:
        if self._approval_verifier is None or not self._approval_verifier.verify(
            approval
        ):
            raise ExecutionBlocked("approval proof is invalid")
        if (
            approval.command_digest != record.simulation.command_digest
            or approval.simulation_digest != record.simulation.simulation_digest
        ):
            raise ExecutionBlocked("approval does not match the current preview")
        if approval.approved_at > now or approval.approved_at < record.simulation.simulated_at:
            raise ExecutionBlocked("approval is not valid for the current simulation")
        if approval.expires_at <= now:
            raise ExecutionBlocked("approval expired")
        if approval.expires_at > record.command.expires_at:
            raise ExecutionBlocked("approval outlives the plan")
        if approval.expires_at > record.simulation.simulated_at + self._simulation_ttl:
            raise ExecutionBlocked("approval outlives the simulation")

    @staticmethod
    def _validate_simulation_binding(
        command: DlmmCommand, simulation: SimulationResult
    ) -> None:
        digest = canonical_command_digest(command)
        if (
            simulation.schema_version != command.schema_version
            or simulation.command_id != command.command_id
            or simulation.command_digest != digest
            or simulation.simulation_digest
            != canonical_simulation_digest(simulation)
            or simulation.min_bin_id != command.min_bin_id
            or simulation.max_bin_id != command.max_bin_id
        ):
            raise ExecutionBlocked("simulation does not match the command payload")
        if (
            isinstance(command, DlmmCommandV2)
            and isinstance(simulation, SimulationResultV2)
            and simulation.success
            and not _market_bound(command, simulation)
        ):
            # The executor refuses these itself; a "successful" one is never trusted.
            raise ExecutionBlocked("simulation contradicts the command's position or anchor")

    @staticmethod
    def _validate_receipt_binding(
        record: ExecutionRecord, receipt: GatewayReceipt
    ) -> None:
        if (
            receipt.schema_version != record.command.schema_version
            or receipt.command_id != record.command.command_id
            or receipt.idempotency_key != record.command.idempotency_key
            or receipt.command_digest != record.simulation.command_digest
            or receipt.simulation_digest != record.simulation.simulation_digest
        ):
            raise ExecutionBlocked("gateway receipt does not match execution binding")
        if (
            isinstance(receipt, GatewayReceiptV2)
            and isinstance(record.simulation, SimulationResultV2)
            and receipt.position_address != record.simulation.position_address
        ):
            raise ExecutionBlocked("gateway receipt names another position than the simulated one")

    async def _require_record(self, command_id: UUID) -> ExecutionRecord:
        record = await self._store.get(command_id)
        if record is None:
            raise ExecutionBlocked("command not found")
        return record

    def _simulation_is_fresh(self, record: ExecutionRecord, now: datetime) -> bool:
        try:
            self._validate_simulation_binding(record.command, record.simulation)
        except ExecutionBlocked:
            return False
        return (
            _simulation_rejection(record.command, record.simulation) is None
            and record.simulation.simulated_at <= now
            and record.simulation.simulated_at + self._simulation_ttl > now
        )

    @staticmethod
    def _preview(record: ExecutionRecord) -> ExecutionPreview:
        return ExecutionPreview(
            command=record.command,
            simulation=record.simulation,
            phase=record.phase,
            approval_required=record.command.mode is TradingMode.APPROVAL,
        )

    @staticmethod
    def _state(record: ExecutionRecord) -> ExecutionState:
        return ExecutionState(
            command_id=record.command.command_id,
            phase=record.phase,
            approval_id=None
            if record.approval is None
            else record.approval.approval_id,
            receipt=record.receipt,
        )

    @staticmethod
    def _phase_from_gateway(state: GatewayState) -> ExecutionPhase:
        return {
            GatewayState.SUBMITTED: ExecutionPhase.SUBMITTED,
            GatewayState.CONFIRMED: ExecutionPhase.CONFIRMED,
            GatewayState.FAILED: ExecutionPhase.FAILED,
            GatewayState.UNKNOWN: ExecutionPhase.UNKNOWN,
        }[state]


def _serialize_record(record: ExecutionRecord) -> str:
    return json.dumps(
        {
            "command": record.command.model_dump(mode="json"),
            "simulation": record.simulation.model_dump(mode="json"),
            "phase": record.phase.value,
            "approval": None
            if record.approval is None
            else record.approval.model_dump(mode="json"),
            "approval_consumed": record.approval_consumed,
            "receipt": None
            if record.receipt is None
            else record.receipt.model_dump(mode="json"),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _deserialize_record(payload: str) -> ExecutionRecord:
    value = json.loads(payload)
    return ExecutionRecord(
        command=command_from_payload(value["command"]),
        simulation=simulation_from_payload(value["simulation"]),
        phase=ExecutionPhase(value["phase"]),
        approval=None
        if value["approval"] is None
        else OperatorApproval.model_validate(value["approval"]),
        approval_consumed=bool(value["approval_consumed"]),
        receipt=None if value["receipt"] is None else receipt_from_payload(value["receipt"]),
    )


def entry_impact_rejection(command: DlmmCommand, simulation: SimulationResult) -> str | None:
    """The one rule "an entry's quoted impact against its slippage limit" (D08), as the
    executor's code: ``PRICE_IMPACT_UNAVAILABLE`` without a measured impact,
    ``SLIPPAGE_BREACH`` above the limit; ``None`` within it and for every exit."""

    if command.operation not in ENTRY_OPERATIONS:
        return None
    if simulation.price_impact_bps is None:
        return "PRICE_IMPACT_UNAVAILABLE"
    if simulation.price_impact_bps > command.max_slippage_bps:
        return "SLIPPAGE_BREACH"
    return None


def _simulation_rejection(command: DlmmCommand, simulation: SimulationResult) -> str | None:
    """Why this simulation cannot be executed, as an executor error code; ``None`` if it can."""

    if not simulation.success or simulation.errors:
        return simulation.errors[0].code if simulation.errors else "SIMULATION_FAILED"
    return entry_impact_rejection(command, simulation)


def _position_simulated(record: ExecutionRecord) -> bool:
    """A V2 OPEN whose successful simulation already created (and named) a position key."""

    return (
        isinstance(record.command, DlmmCommandV2)
        and record.command.operation is DlmmOperation.OPEN
        and isinstance(record.simulation, SimulationResultV2)
        and record.simulation.success
        and record.simulation.position_address is not None
    )


def _market_bound(command: DlmmCommandV2, simulation: SimulationResultV2) -> bool:
    """The simulated position is the command's and the chain still matches its anchor."""

    if command.operation is DlmmOperation.OPEN:
        if simulation.position_address in (None, command.wallet_address):
            return False
    elif simulation.position_address != command.position_address:
        return False
    if command.operation not in {DlmmOperation.OPEN, DlmmOperation.ADD}:
        return True
    if (
        command.active_bin_id is None
        or command.active_bin_tolerance is None
        or simulation.active_bin_id is None
    ):
        return False
    return (
        abs(simulation.active_bin_id - command.active_bin_id) <= command.active_bin_tolerance
        and simulation.token_x_decimals == command.token_x_decimals
        and simulation.token_y_decimals == command.token_y_decimals
    )


def _canonical_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )
