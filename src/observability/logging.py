"""Correlation-aware structured logging for worker cycles.

Records go through the T01 JSON formatter and its secret redactor; this module only
binds ``correlation_id`` / ``causation_id`` (and other cycle fields) to every record
emitted inside a cycle and never logs raw exceptions or tracebacks.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import UUID

import structlog


@contextmanager
def cycle_context(
    *, correlation_id: UUID, causation_id: UUID | None = None, **fields: Any
) -> Iterator[None]:
    values: dict[str, Any] = {"correlation_id": str(correlation_id), **fields}
    if causation_id is not None:
        values["causation_id"] = str(causation_id)
    with structlog.contextvars.bound_contextvars(**values):
        yield


def get_logger(name: str) -> Any:
    return structlog.get_logger(name)


def error_fields(error: BaseException) -> dict[str, str]:
    """Loggable description of a failure; values still pass the secret redactor."""

    return {"error_type": type(error).__name__, "error": str(error)}
