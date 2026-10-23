"""PostgreSQL job leases: a durable TTL row plus a session-level advisory lock.

* The advisory lock (``pg_try_advisory_lock``) is held on a dedicated connection for
  as long as the job runs; two live workers can never hold it at once, and it is
  released by PostgreSQL itself if the owner's connection dies.
* The ``system_leases`` row carries the TTL, owner and a fencing ``token``. It is
  only taken over once expired, so a crashed owner's job is recovered by another
  worker after ``ttl`` and a stale owner can no longer renew or release it.
"""

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import Clock
from src.persistence.contracts import JobLease
from src.persistence.models import SystemLeaseRecord
from src.persistence.postgres import AdvisoryLockHandle, PostgresDatabase, Work, advisory_key


@dataclass(frozen=True, slots=True)
class _HeldLease:
    lease: JobLease
    handle: AdvisoryLockHandle


class PostgresJobLeases:
    def __init__(self, database: PostgresDatabase, clock: Clock) -> None:
        self._db = database
        self._clock = clock
        self._held: dict[tuple[str, int], _HeldLease] = {}

    async def acquire(self, name: str, owner: str, ttl: timedelta) -> JobLease | None:
        if ttl <= timedelta(0):
            raise ValueError("lease ttl must be positive")
        handle = await self._db.try_advisory_lock(advisory_key(f"job:{name}"))
        if handle is None:
            return None
        try:
            lease = await self._db.run_async(self._take(name, owner, ttl))
        except BaseException:
            await handle.release()
            raise
        if lease is None:
            await handle.release()
            return None
        self._held[(name, lease.token)] = _HeldLease(lease, handle)
        return lease

    def _take(self, name: str, owner: str, ttl: timedelta) -> Work[JobLease | None]:
        now = self._clock.now()

        async def work(session: AsyncSession) -> JobLease | None:
            proposed = pg_insert(SystemLeaseRecord).values(
                name=name,
                owner=owner,
                acquired_at=now,
                expires_at=now + ttl,
                token=func.nextval("system_lease_tokens"),
            )
            statement = proposed.on_conflict_do_update(
                index_elements=[SystemLeaseRecord.name],
                set_={
                    "owner": proposed.excluded.owner,
                    "acquired_at": proposed.excluded.acquired_at,
                    "expires_at": proposed.excluded.expires_at,
                    "token": proposed.excluded.token,
                },
                where=or_(SystemLeaseRecord.expires_at <= now, SystemLeaseRecord.owner == owner),
            ).returning(
                SystemLeaseRecord.name,
                SystemLeaseRecord.owner,
                SystemLeaseRecord.token,
                SystemLeaseRecord.acquired_at,
                SystemLeaseRecord.expires_at,
            )
            row = (await session.execute(statement)).one_or_none()
            if row is None:
                return None
            return JobLease(row.name, row.owner, row.token, row.acquired_at, row.expires_at)

        return work

    async def renew(self, lease: JobLease, ttl: timedelta) -> JobLease | None:
        held = self._held.get((lease.name, lease.token))
        if held is None or not await held.handle.is_held():
            return None
        now = self._clock.now()

        async def work(session: AsyncSession) -> JobLease | None:
            row = (
                await session.execute(
                    update(SystemLeaseRecord)
                    .where(
                        SystemLeaseRecord.name == lease.name,
                        SystemLeaseRecord.owner == lease.owner,
                        SystemLeaseRecord.token == lease.token,
                        SystemLeaseRecord.expires_at > now,
                    )
                    .values(expires_at=now + ttl)
                    .returning(
                        SystemLeaseRecord.name,
                        SystemLeaseRecord.owner,
                        SystemLeaseRecord.token,
                        SystemLeaseRecord.acquired_at,
                        SystemLeaseRecord.expires_at,
                    )
                )
            ).one_or_none()
            if row is None:
                return None
            return JobLease(row.name, row.owner, row.token, row.acquired_at, row.expires_at)

        renewed = await self._db.run_async(work)
        if renewed is not None:
            self._held[(lease.name, lease.token)] = _HeldLease(renewed, held.handle)
        return renewed

    async def release(self, lease: JobLease) -> None:
        held = self._held.pop((lease.name, lease.token), None)

        async def work(session: AsyncSession) -> None:
            await session.execute(
                delete(SystemLeaseRecord).where(
                    SystemLeaseRecord.name == lease.name,
                    SystemLeaseRecord.owner == lease.owner,
                    SystemLeaseRecord.token == lease.token,
                )
            )

        try:
            await self._db.run_async(work)
        finally:
            if held is not None:
                await held.handle.release()

    async def holder(self, name: str) -> JobLease | None:
        async def work(session: AsyncSession) -> JobLease | None:
            row = (
                await session.execute(
                    select(
                        SystemLeaseRecord.name,
                        SystemLeaseRecord.owner,
                        SystemLeaseRecord.token,
                        SystemLeaseRecord.acquired_at,
                        SystemLeaseRecord.expires_at,
                    ).where(SystemLeaseRecord.name == name)
                )
            ).one_or_none()
            if row is None:
                return None
            return JobLease(row.name, row.owner, row.token, row.acquired_at, row.expires_at)

        result: JobLease | None = await self._db.run_async(work)
        return result

    async def abandon(self) -> None:
        """Drop the advisory locks without touching lease rows (crash simulation)."""

        for held in list(self._held.values()):
            await held.handle.release()
        self._held.clear()
