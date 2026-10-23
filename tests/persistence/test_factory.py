import asyncio
from pathlib import Path

import pytest

from src.accounting import SqliteAccountingRepository
from src.core.config import AppConfig
from src.domain.models.trading import TradingMode
from src.modes import SqliteModeRepository
from src.persistence.factory import (
    PersistenceBackend,
    PersistenceConfigurationError,
    open_repositories,
)
from src.persistence.testing import MigratedSchema
from tests.persistence.test_repository_contracts import promote_to_approval


def test_api_and_worker_get_postgresql_repositories_sharing_one_state(
    postgres_schema: MigratedSchema,
) -> None:
    from src.persistence.accounting_repository import PostgresAccountingRepository
    from src.persistence.execution_store import PostgresExecutionStore
    from src.persistence.leases import PostgresJobLeases
    from src.persistence.mode_repository import PostgresModeRepository

    config = AppConfig.load(environ={"DATABASE_URL": postgres_schema.database_url})
    api = open_repositories(config, environ={}, schema=postgres_schema.schema)
    worker = open_repositories(config, environ={}, schema=postgres_schema.schema)
    try:
        assert api.backend is PersistenceBackend.POSTGRESQL
        assert isinstance(api.mode, PostgresModeRepository)
        assert isinstance(api.accounting, PostgresAccountingRepository)
        assert isinstance(worker.execution, PostgresExecutionStore)
        assert isinstance(worker.leases, PostgresJobLeases)
        assert worker.retention() is not None
        assert asyncio.run(worker.ping()) is True

        promote_to_approval(api.mode)
        assert worker.mode.mode_state().mode is TradingMode.APPROVAL
    finally:
        api.close()
        worker.close()


def test_the_factory_refuses_to_start_without_a_database() -> None:
    with pytest.raises(PersistenceConfigurationError, match="DATABASE_URL"):
        open_repositories(AppConfig.load(environ={}), environ={})


def test_sqlite_is_only_a_local_debugging_backend(tmp_path: Path) -> None:
    path = str(tmp_path / "debug.sqlite3")
    local = open_repositories(
        AppConfig.load(environ={"APP_ENV": "development"}),
        environ={"LOCAL_SQLITE_PATH": path},
    )
    try:
        assert local.backend is PersistenceBackend.SQLITE
        assert isinstance(local.mode, SqliteModeRepository)
        assert isinstance(local.accounting, SqliteAccountingRepository)
        assert local.retention() is None
    finally:
        local.close()

    with pytest.raises(PersistenceConfigurationError, match="production"):
        open_repositories(
            AppConfig.load(environ={"APP_ENV": "production"}),
            environ={"LOCAL_SQLITE_PATH": path},
        )
