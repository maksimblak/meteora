"""Alembic runtime configured for the async PostgreSQL driver."""

from asyncio import run

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from src.core.config import AppConfig
from src.core.logging import configure_logging
from src.persistence.models import Base

config = context.config
target_metadata = Base.metadata
settings = AppConfig.load()
configure_logging(settings)


def database_url() -> str:
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


def do_run_migrations(connection: object) -> None:
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


if context.is_offline_mode():
    run_migrations_offline()
else:
    run(run_async_migrations())
