"""Minimal FastAPI process surface used by API and container health checks."""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, status
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from src.core.config import AppConfig
from src.core.logging import configure_logging
from src.domain.models.trading import restore_trading_mode
from src.persistence.database import create_engine

ReadinessProbe = Callable[[], Awaitable[bool]]


def create_app(
    *,
    config: AppConfig | None = None,
    readiness_probe: ReadinessProbe | None = None,
) -> FastAPI:
    """Build the API shell without requiring RPC or wallet credentials."""

    settings = config or AppConfig.load()
    configure_logging(settings)
    database_engine: AsyncEngine | None = None
    if readiness_probe is None and (database_url := settings.database_dsn()) is not None:
        database_engine = create_engine(database_url)

    async def database_readiness() -> bool:
        if database_engine is None:
            return False
        try:
            async with database_engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except (OSError, SQLAlchemyError):
            return False
        return True

    probe = readiness_probe or database_readiness

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        if database_engine is not None:
            await database_engine.dispose()

    application = FastAPI(
        title="Meteora Volume Bot",
        version="0.1.0",
        lifespan=lifespan,
    )

    @application.get("/health/live", include_in_schema=False)
    async def health_live() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/health/ready", include_in_schema=False)
    async def health_ready(response: Response) -> dict[str, str]:
        if not await probe():
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return {"status": "not_ready"}
        effective_mode = restore_trading_mode(None)
        return {"status": "ready", "mode": effective_mode.value}

    @application.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return application


app = create_app()
