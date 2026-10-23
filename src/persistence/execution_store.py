"""PostgreSQL adapter of the T05 ``ExecutionStore`` with restart-recovery listing.

Uniqueness (one command per idempotency key, one payload per command id, one-shot
approval claims) is enforced inside a transaction serialized by the execution
advisory lock, as the SQLite store does with its connection lock.
"""

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import ScalarResult, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.execution.contracts import (
    ExecutionPhase,
    OperatorApproval,
    canonical_command_digest,
    command_from_payload,
    receipt_from_payload,
    simulation_from_payload,
)
from src.execution.service import ExecutionBlocked, ExecutionRecord
from src.persistence.models import ExecutionApprovalClaimRecord, ExecutionStateRecord
from src.persistence.postgres import PostgresDatabase, advisory_key

EXECUTION_LOCK = advisory_key("meteora-volume-bot:execution-store")


def record_payload(record: ExecutionRecord) -> dict[str, Any]:
    return {
        "command": record.command.model_dump(mode="json"),
        "simulation": record.simulation.model_dump(mode="json"),
        "phase": record.phase.value,
        "approval": None if record.approval is None else record.approval.model_dump(mode="json"),
        "approval_consumed": record.approval_consumed,
        "receipt": None if record.receipt is None else record.receipt.model_dump(mode="json"),
    }


def record_from_payload(value: dict[str, Any]) -> ExecutionRecord:
    # Each part is read by its own contract version (V1 records written before V2 included).
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


class PostgresExecutionStore:
    def __init__(self, database: PostgresDatabase) -> None:
        self._db = database

    async def get(self, command_id: UUID) -> ExecutionRecord | None:
        async def work(session: AsyncSession) -> ExecutionRecord | None:
            payload = await session.scalar(
                select(ExecutionStateRecord.payload).where(
                    ExecutionStateRecord.command_id == command_id
                )
            )
            return None if payload is None else record_from_payload(payload)

        result: ExecutionRecord | None = await self._db.run_async(work)
        return result

    async def get_by_idempotency_key(self, key: str) -> ExecutionRecord | None:
        async def work(session: AsyncSession) -> ExecutionRecord | None:
            payload = await session.scalar(
                select(ExecutionStateRecord.payload).where(
                    ExecutionStateRecord.idempotency_key == key
                )
            )
            return None if payload is None else record_from_payload(payload)

        result: ExecutionRecord | None = await self._db.run_async(work)
        return result

    async def put(self, record: ExecutionRecord) -> None:
        command = record.command

        async def work(session: AsyncSession) -> None:
            existing = await session.scalar(
                select(ExecutionStateRecord.payload).where(
                    ExecutionStateRecord.command_id == command.command_id
                )
            )
            if existing is not None:
                previous = record_from_payload(existing).command
                if previous.idempotency_key != command.idempotency_key or canonical_command_digest(
                    previous
                ) != canonical_command_digest(command):
                    raise ExecutionBlocked("command id belongs to another command payload")
            bound = await session.scalar(
                select(ExecutionStateRecord.command_id).where(
                    ExecutionStateRecord.idempotency_key == command.idempotency_key,
                    ExecutionStateRecord.command_id != command.command_id,
                )
            )
            if bound is not None:
                raise ExecutionBlocked("idempotency key belongs to another command")
            values = {
                "idempotency_key": command.idempotency_key,
                "phase": record.phase.value,
                "payload": record_payload(record),
                "updated_at": func.now(),
            }
            await session.execute(
                pg_insert(ExecutionStateRecord)
                .values(command_id=command.command_id, **values)
                .on_conflict_do_update(
                    index_elements=[ExecutionStateRecord.command_id], set_=values
                )
            )

        await self._db.run_async(work, lock=EXECUTION_LOCK)

    async def claim_approval(
        self, approval_id: UUID, command_digest: str, simulation_digest: str
    ) -> bool:
        async def work(session: AsyncSession) -> bool:
            claimed = await session.execute(
                pg_insert(ExecutionApprovalClaimRecord)
                .values(
                    approval_id=approval_id,
                    command_digest=command_digest,
                    simulation_digest=simulation_digest,
                )
                .on_conflict_do_nothing(index_elements=[ExecutionApprovalClaimRecord.approval_id])
                .returning(ExecutionApprovalClaimRecord.approval_id)
            )
            return claimed.scalar_one_or_none() is not None

        result: bool = await self._db.run_async(work, lock=EXECUTION_LOCK)
        return result

    async def in_phases(self, phases: Sequence[ExecutionPhase]) -> tuple[ExecutionRecord, ...]:
        async def work(session: AsyncSession) -> tuple[ExecutionRecord, ...]:
            payloads: ScalarResult[dict[str, Any]] = await session.scalars(
                select(ExecutionStateRecord.payload)
                .where(ExecutionStateRecord.phase.in_([phase.value for phase in phases]))
                .order_by(ExecutionStateRecord.updated_at, ExecutionStateRecord.command_id)
            )
            return tuple(record_from_payload(payload) for payload in payloads)

        result: tuple[ExecutionRecord, ...] = await self._db.run_async(work)
        return result
