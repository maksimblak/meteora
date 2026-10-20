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
    DlmmCommandV1,
    ExecutionPhase,
    ExecutionPreview,
    ExecutionState,
    GatewayReceiptV1,
    GatewayState,
    LivePromotionEvidence,
    OperatorApproval,
    SimulationResultV1,
    canonical_command_digest,
    canonical_simulation_digest,
)


class ExecutionBlocked(RuntimeError):
    """The command failed a fail-closed execution invariant."""


class ExecutionGateway(Protocol):
    async def simulate(self, command: DlmmCommandV1) -> SimulationResultV1: ...

    async def submit(
        self,
        command: DlmmCommandV1,
        idempotency_key: str,
        approval: OperatorApproval | None,
    ) -> GatewayReceiptV1: ...

    async def status(self, signature: str) -> ChainReceiptV1: ...


class PlanResolver(Protocol):
    async def resolve(self, plan_id: UUID) -> DlmmCommandV1: ...


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
    command: DlmmCommandV1
    simulation: SimulationResultV1
    phase: ExecutionPhase
    approval: OperatorApproval | None = None
    approval_consumed: bool = False
    receipt: GatewayReceiptV1 | None = None


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
    def __init__(self, commands: Mapping[UUID, DlmmCommandV1]) -> None:
        self._commands = dict(commands)

    async def resolve(self, plan_id: UUID) -> DlmmCommandV1:
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
    ) -> None:
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
        if not simulation.success or simulation.errors:
            record.phase = ExecutionPhase.FAILED
            await self._store.put(record)
            raise ExecutionBlocked("simulation failed")
        if (
            simulation.price_impact_bps is None
            or simulation.price_impact_bps > command.max_slippage_bps
        ):
            record.phase = ExecutionPhase.FAILED
            await self._store.put(record)
            raise ExecutionBlocked("simulation exceeds slippage limit")
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

    async def execute(self, command_id: UUID) -> GatewayReceiptV1:
        record = await self._require_record(command_id)
        if record.receipt is not None:
            if (
                record.receipt.state in {GatewayState.SUBMITTED, GatewayState.UNKNOWN}
                and record.receipt.signature is not None
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
            raise ExecutionBlocked(
                "submit outcome is unknown; operator recovery is required"
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

        try:
            receipt = await self._gateway.submit(
                record.command,
                record.command.idempotency_key,
                record.approval,
            )
        except Exception:
            record.phase = ExecutionPhase.UNKNOWN
            await self._store.put(record)
            raise
        self._validate_receipt_binding(record, receipt)
        record.receipt = receipt
        record.phase = self._phase_from_gateway(receipt.state)
        await self._store.put(record)
        return receipt

    async def reconcile(self, command_id: UUID) -> ExecutionState:
        record = await self._require_record(command_id)
        if record.receipt is None or record.receipt.signature is None:
            raise ExecutionBlocked("command has no submitted signature")
        if record.receipt.state in {GatewayState.CONFIRMED, GatewayState.FAILED}:
            return self._state(record)

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
        if not record.simulation.success or record.simulation.errors:
            raise ExecutionBlocked("simulation failed")
        if (
            record.simulation.simulated_at > now
            or record.simulation.simulated_at + self._simulation_ttl <= now
        ):
            raise ExecutionBlocked("simulation is stale")
        if (
            record.simulation.price_impact_bps is None
            or record.simulation.price_impact_bps > command.max_slippage_bps
        ):
            raise ExecutionBlocked("simulation exceeds slippage limit")
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
        self, command: DlmmCommandV1, now: datetime
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
        command: DlmmCommandV1, simulation: SimulationResultV1
    ) -> None:
        digest = canonical_command_digest(command)
        if (
            simulation.command_id != command.command_id
            or simulation.command_digest != digest
            or simulation.simulation_digest
            != canonical_simulation_digest(simulation)
            or simulation.min_bin_id != command.min_bin_id
            or simulation.max_bin_id != command.max_bin_id
        ):
            raise ExecutionBlocked("simulation does not match the command payload")

    @staticmethod
    def _validate_receipt_binding(
        record: ExecutionRecord, receipt: GatewayReceiptV1
    ) -> None:
        if (
            receipt.command_id != record.command.command_id
            or receipt.idempotency_key != record.command.idempotency_key
            or receipt.command_digest != record.simulation.command_digest
            or receipt.simulation_digest != record.simulation.simulation_digest
        ):
            raise ExecutionBlocked("gateway receipt does not match execution binding")

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
            record.simulation.success
            and not record.simulation.errors
            and record.simulation.price_impact_bps is not None
            and record.simulation.price_impact_bps
            <= record.command.max_slippage_bps
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
        command=DlmmCommandV1.model_validate(value["command"]),
        simulation=SimulationResultV1.model_validate(value["simulation"]),
        phase=ExecutionPhase(value["phase"]),
        approval=None
        if value["approval"] is None
        else OperatorApproval.model_validate(value["approval"]),
        approval_consumed=bool(value["approval_consumed"]),
        receipt=None
        if value["receipt"] is None
        else GatewayReceiptV1.model_validate(value["receipt"]),
    )


def _canonical_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )
