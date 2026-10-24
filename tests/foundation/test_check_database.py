"""The canonical check creates its own database when it is missing (F6).

Runs only with ``TEST_DATABASE_URL`` (the Compose ``check`` service); skipped otherwise.
The creation path runs in a real check only on a fresh volume, so it is exercised here on
a throwaway database of the same server that the test drops afterwards.
"""

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import pool, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import create_async_engine

from src.persistence.testing import integration_database_url
from tests.check_database import ensure_database


def test_a_missing_database_is_created_once_and_then_left_as_it_is() -> None:
    url = integration_database_url()
    if url is None:
        pytest.skip("PostgreSQL integration URL is not configured")
    probe = make_url(url).set(database=f"f6_probe_{uuid4().hex[:12]}")
    probe_url = probe.render_as_string(hide_password=False)
    try:
        assert asyncio.run(ensure_database(probe_url)) is True
        assert asyncio.run(ensure_database(probe_url)) is False
        assert asyncio.run(_current_database(probe)) == probe.database
    finally:
        asyncio.run(_drop_database(probe))


async def _current_database(url: URL) -> str | None:
    engine = create_async_engine(url, poolclass=pool.NullPool)
    try:
        async with engine.connect() as connection:
            return await connection.scalar(text("SELECT current_database()"))
    finally:
        await engine.dispose()


async def _drop_database(url: URL) -> None:
    engine = create_async_engine(
        url.set(database="postgres"), poolclass=pool.NullPool, isolation_level="AUTOCOMMIT"
    )
    try:
        async with engine.connect() as connection:
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{url.database}"'))
    finally:
        await engine.dispose()
