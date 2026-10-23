from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from src.accounting import SqliteAccountingRepository
from src.modes import SqliteModeRepository
from src.persistence.testing import MigratedSchema, integration_database_url, migrated_schema


@dataclass
class Backend:
    """Opens independent repository instances on one durable store.

    Every call is a new connection (SQLite file handle or PostgreSQL pool), which is
    how a restarted process or a second worker sees the same state.
    """

    name: str
    open_mode: Callable[[], Any]
    open_accounting: Callable[[], Any]
    schema: MigratedSchema | None = None
    opened: list[Any] = field(default_factory=list)

    def mode(self) -> Any:
        repository = self.open_mode()
        self.opened.append(repository)
        return repository

    def accounting(self) -> Any:
        repository = self.open_accounting()
        self.opened.append(repository)
        return repository

    def close(self) -> None:
        for repository in reversed(self.opened):
            repository.close()


@pytest.fixture
def postgres_schema() -> Iterator[MigratedSchema]:
    url = integration_database_url()
    if url is None:
        pytest.skip("PostgreSQL integration URL is not configured")
    with migrated_schema(url) as schema:
        yield schema


@pytest.fixture(params=["sqlite", "postgres"])
def backend(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Backend]:
    if request.param == "sqlite":
        path = tmp_path / "state.sqlite3"
        sqlite = Backend(
            "sqlite",
            lambda: SqliteModeRepository(path),
            lambda: SqliteAccountingRepository(path),
        )
        yield sqlite
        sqlite.close()
        return

    url = integration_database_url()
    if url is None:
        pytest.skip("PostgreSQL integration URL is not configured")
    from src.persistence.accounting_repository import PostgresAccountingRepository
    from src.persistence.mode_repository import PostgresModeRepository

    with migrated_schema(url) as schema:
        postgres = Backend(
            "postgres",
            lambda: PostgresModeRepository(schema.connect(), owns_database=True),
            lambda: PostgresAccountingRepository(schema.connect(), owns_database=True),
            schema=schema,
        )
        yield postgres
        postgres.close()
