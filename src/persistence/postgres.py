"""Thread-hosted asyncpg engine serving both synchronous and asynchronous callers.

The T04 ``ModeRepository`` / ``AccountingRepository`` protocols are synchronous while
the only PostgreSQL driver in the lock is asyncpg. ``PostgresDatabase`` owns one
event loop on a private thread; every unit of work runs there as one PostgreSQL
transaction. Synchronous callers block on the result, asynchronous callers await it
from their own loop. A writer that needs ``BEGIN IMMEDIATE`` semantics passes a
``lock`` key: the transaction first takes ``pg_advisory_xact_lock`` so every guard it
reads afterwards is serialized with every other writer of that key, in any process.
"""

import asyncio
import hashlib
import threading
from collections.abc import Awaitable, Callable, Coroutine
from concurrent.futures import Future
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from src.persistence.database import create_engine, create_session_factory

type Work[T] = Callable[[AsyncSession], Awaitable[T]]


def advisory_key(name: str) -> int:
    """Stable signed 64-bit advisory lock key for a lock name."""

    digest = hashlib.sha256(name.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


class AdvisoryLockHandle:
    """A session-level advisory lock held on its own autocommit connection.

    PostgreSQL releases it automatically when the owning connection dies, so a
    crashed worker never keeps a job locked past its connection.
    """

    def __init__(self, database: "PostgresDatabase", connection: AsyncConnection, key: int) -> None:
        self._database = database
        self._connection = connection
        self._key = key
        self._released = False

    @property
    def key(self) -> int:
        return self._key

    async def is_held(self) -> bool:
        if self._released:
            return False
        return await self._database.submit_async(self._probe())

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        await self._database.submit_async(self._unlock())

    async def _release_on_loop(self) -> None:
        if not self._released:
            self._released = True
            await self._unlock()

    async def _probe(self) -> bool:
        try:
            await self._connection.execute(text("SELECT 1"))
        except Exception:
            return False
        return True

    async def _unlock(self) -> None:
        self._database.forget(self)
        try:
            await self._connection.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": self._key}
            )
        except Exception:
            await self._connection.invalidate()
        finally:
            await self._connection.close()


class PostgresDatabase:
    def __init__(
        self,
        database_url: str,
        *,
        schema: str | None = None,
        command_timeout: float = 30.0,
        pool_size: int = 5,
    ) -> None:
        self._loop = asyncio.new_event_loop()
        started = threading.Event()
        self._thread = threading.Thread(
            target=self._serve, args=(started,), name="postgres-bridge", daemon=True
        )
        self._thread.start()
        started.wait()
        self._engine = create_engine(
            database_url, schema=schema, command_timeout=command_timeout, pool_size=pool_size
        )
        self._sessions = create_session_factory(self._engine)
        self._locks: set[AdvisoryLockHandle] = set()
        self._closed = False

    def _serve(self, started: threading.Event) -> None:
        asyncio.set_event_loop(self._loop)
        started.set()
        self._loop.run_forever()

    # --- execution ----------------------------------------------------------------

    def _submit[T](self, coroutine: Coroutine[Any, Any, T]) -> Future[T]:
        if self._closed:
            coroutine.close()
            raise RuntimeError("database bridge is closed")
        if threading.get_ident() == self._thread.ident:
            coroutine.close()
            raise RuntimeError("re-entrant call from the database bridge loop")
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop)

    def submit[T](self, coroutine: Coroutine[Any, Any, T]) -> T:
        return self._submit(coroutine).result()

    async def submit_async[T](self, coroutine: Coroutine[Any, Any, T]) -> T:
        return await asyncio.wrap_future(self._submit(coroutine))

    def run[T](self, work: Work[T], *, lock: int | None = None) -> T:
        """Run ``work`` in one transaction and block until it commits or fails."""

        return self.submit(self._transaction(work, lock))

    async def run_async[T](self, work: Work[T], *, lock: int | None = None) -> T:
        return await self.submit_async(self._transaction(work, lock))

    async def _transaction[T](self, work: Work[T], lock: int | None) -> T:
        async with self._sessions() as session, session.begin():
            if lock is not None:
                await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock})
            return await work(session)

    # --- coordination ---------------------------------------------------------------

    async def try_advisory_lock(self, key: int) -> AdvisoryLockHandle | None:
        return await self.submit_async(self._acquire_session_lock(key))

    async def _acquire_session_lock(self, key: int) -> AdvisoryLockHandle | None:
        connection = await self._engine.connect()
        try:
            await connection.execution_options(isolation_level="AUTOCOMMIT")
            acquired: bool = (
                await connection.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": key})
            ).scalar_one()
        except BaseException:
            await connection.close()
            raise
        if not acquired:
            await connection.close()
            return None
        handle = AdvisoryLockHandle(self, connection, key)
        self._locks.add(handle)
        return handle

    def forget(self, handle: AdvisoryLockHandle) -> None:
        self._locks.discard(handle)

    async def _shutdown(self) -> None:
        for handle in list(self._locks):
            await handle._release_on_loop()
        await self._engine.dispose()

    async def ping(self) -> bool:
        try:
            return await self.submit_async(self._ping())
        except Exception:
            return False

    async def _ping(self) -> bool:
        async with self._engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return True

    # --- lifecycle --------------------------------------------------------------------

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        if self._closed:
            return
        future = asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop)
        self._closed = True
        try:
            future.result(timeout=10)
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=10)
            self._loop.close()
