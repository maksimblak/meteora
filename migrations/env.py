"""Alembic runtime configured for the async PostgreSQL driver.

A caller may pass an already open synchronous connection through
``config.attributes["connection"]`` (used by the isolated-schema test fixture);
otherwise the DSN comes from the typed application configuration.
"""

from asyncio import run
from typing import Any

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from src.core.config import AppConfig
from src.core.logging import configure_logging
from src.persistence.models import Base

config = context.config
target_metadata = Base.metadata


def database_url() -> str:
    settings = AppConfig.load()
    configure_logging(settings)
    value = settings.database_dsn()
    if not value:
        raise RuntimeError("DATABASE_URL is required for migrations")
    if not value.startswith("postgresql+asyncpg://"):
        raise RuntimeError("DATABASE_URL must use postgresql+asyncpg")
    return value


def run_migrations_offline() -> None:
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Any) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = database_url()
    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


provided_connection = config.attributes.get("connection")
if provided_connection is not None:
    do_run_migrations(provided_connection)
elif context.is_offline_mode():
    run_migrations_offline()
else:
    run(run_async_migrations())
