"""Exact JSONB round-trip for strict domain models.

Pydantic renders ``Decimal`` and timestamps as strings in JSON mode, so JSONB keeps
them digit-for-digit; reading goes back through JSON exactly like the SQLite adapters.
"""

import json
from typing import Any

from pydantic import BaseModel


def to_payload(model: BaseModel) -> dict[str, Any]:
    value: dict[str, Any] = json.loads(model.model_dump_json())
    return value


def from_payload[ModelT: BaseModel](model: type[ModelT], payload: dict[str, Any]) -> ModelT:
    return model.model_validate_json(json.dumps(payload), strict=False)
