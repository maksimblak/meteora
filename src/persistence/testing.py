"""PostgreSQL integration fixture: every test gets a freshly migrated private schema.

Used by the PostgreSQL contract and restart tests; it never touches the ``public``
schema, so parallel suites and the canonical check database stay untouched.
"""

import asyncio
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import pool, text
from sqlalchemy.ext.asyncio import create_async_engine

from src.persistence.postgres import PostgresDatabase

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"


def integration_database_url() -> str | None:
    return os.getenv("TEST_DATABASE_URL") or None


@dataclass(frozen=True, slots=True)
class MigratedSchema:
    database_url: str
    schema: str

    def connect(self, **options: Any) -> PostgresDatabase:
        """A new connection pool, as a separate worker/API process would open."""

        return PostgresDatabase(self.database_url, schema=self.schema, **options)

    def alembic(self, action: str, revision: str) -> None:
        asyncio.run(_alembic(self.database_url, self.schema, action, revision))


@contextmanager
def migrated_schema(database_url: str, *, revision: str = "head") -> Iterator[MigratedSchema]:
    schema = f"t07_{uuid4().hex[:16]}"
    asyncio.run(_create_schema(database_url, schema))
    try:
        asyncio.run(_alembic(database_url, schema, "upgrade", revision))
        yield MigratedSchema(database_url, schema)
    finally:
        asyncio.run(_drop_schema(database_url, schema))


async def _create_schema(database_url: str, schema: str) -> None:
    engine = create_async_engine(database_url, poolclass=pool.NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    finally:
        await engine.dispose()


async def _drop_schema(database_url: str, schema: str) -> None:
    engine = create_async_engine(database_url, poolclass=pool.NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    finally:
        await engine.dispose()


async def _alembic(database_url: str, schema: str, action: str, revision: str) -> None:
    engine = create_async_engine(
        database_url,
        poolclass=pool.NullPool,
        connect_args={"server_settings": {"search_path": schema}},
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(_run_alembic, action, revision)
    finally:
        await engine.dispose()


def _run_alembic(connection: Any, action: str, revision: str) -> None:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    config.attributes["connection"] = connection
    getattr(command, action)(config, revision)
