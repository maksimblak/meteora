"""Async SQLAlchemy engine construction."""

from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def create_engine(
    database_url: str,
    *,
    echo: bool = False,
    schema: str | None = None,
    command_timeout: float | None = None,
    pool_size: int | None = None,
) -> AsyncEngine:
    """Build the asyncpg engine; ``schema`` pins ``search_path`` (isolated test schemas)."""

    if not database_url.startswith("postgresql+asyncpg://"):
        raise ValueError("database URL must use postgresql+asyncpg")
    connect_args: dict[str, Any] = {}
    if schema is not None:
        if not schema.isidentifier():
            raise ValueError("schema must be a plain identifier")
        connect_args["server_settings"] = {"search_path": schema}
    if command_timeout is not None:
        connect_args["command_timeout"] = command_timeout
    options: dict[str, Any] = {"echo": echo, "pool_pre_ping": True}
    if pool_size is not None:
        options["pool_size"] = pool_size
    return create_async_engine(database_url, connect_args=connect_args, **options)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
