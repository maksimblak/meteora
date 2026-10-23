"""SQLite (local-debug backend) store for position context, so a restarted local
worker can still review the positions the T04 SQLite adapters kept open."""

from pathlib import Path

from src.persistence.contracts import PositionContext
from src.portfolio.sqlite import SqliteDatabase

_SCHEMA = """
CREATE TABLE IF NOT EXISTS position_contexts (
    position_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL
)
"""


class SqlitePositionContexts:
    def __init__(self, path: Path | str) -> None:
        self._db = SqliteDatabase(path, _SCHEMA)

    async def put(self, context: PositionContext) -> None:
        with self._db.transaction() as connection:
            row = connection.execute(
                "SELECT payload FROM position_contexts WHERE position_id = ?",
                (context.position_id,),
            ).fetchone()
            if row is not None:
                if PositionContext.model_validate_json(row[0], strict=False) != context:
                    raise ValueError("position context is immutable")
                return
            connection.execute(
                "INSERT INTO position_contexts VALUES (?, ?)",
                (context.position_id, context.model_dump_json()),
            )

    async def get(self, position_id: str) -> PositionContext | None:
        row = self._db.fetch_one(
            "SELECT payload FROM position_contexts WHERE position_id = ?", (position_id,)
        )
        return None if row is None else PositionContext.model_validate_json(row[0], strict=False)

    async def all(self) -> tuple[PositionContext, ...]:
        rows = self._db.fetch_all("SELECT payload FROM position_contexts ORDER BY position_id")
        return tuple(PositionContext.model_validate_json(row[0], strict=False) for row in rows)

    def close(self) -> None:
        self._db.close()
