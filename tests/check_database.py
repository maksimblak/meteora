"""The canonical check's own database (F6).

``docker compose --profile test run check`` may run next to a live stack in the same Compose
project. It must not write a row into the working database nor contend with the running
worker for its leases (PostgreSQL advisory locks are scoped to one database), so the check
works in a database of its own on the same server. This module creates that database when
it is missing; the check then migrates it (``alembic upgrade head``) and pytest receives it
as ``TEST_DATABASE_URL``.

Run: ``python -m tests.check_database`` (reads ``TEST_DATABASE_URL``).
"""

import asyncio
import os
import re
import sys

from sqlalchemy import pool, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

# The server's maintenance database: CREATE DATABASE is issued from it, never from the
# working database.
MAINTENANCE_DATABASE = "postgres"
_PLAIN_NAME = re.compile(r"[a-z_][a-z0-9_]{0,62}")


async def ensure_database(database_url: str) -> bool:
    """Create the database the URL names unless it exists; True when it was created."""

    url = make_url(database_url)
    name = url.database
    if name is None or not _PLAIN_NAME.fullmatch(name):
        raise ValueError(f"unsupported database name {name!r}")
    engine = create_async_engine(
        url.set(database=MAINTENANCE_DATABASE),
        poolclass=pool.NullPool,
        isolation_level="AUTOCOMMIT",
    )
    try:
        async with engine.connect() as connection:
            exists = await connection.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": name}
            )
            if exists:
                return False
            await connection.execute(text(f'CREATE DATABASE "{name}"'))
            return True
    finally:
        await engine.dispose()


def main() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        sys.exit("TEST_DATABASE_URL is required")
    created = asyncio.run(ensure_database(database_url))
    state = "created" if created else "exists"
    print(f"check database {make_url(database_url).database}: {state}")


if __name__ == "__main__":
    main()
