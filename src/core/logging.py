"""Structured JSON logging with value- and key-based secret redaction."""

import logging
import sys
from collections.abc import Mapping, Sequence
from typing import Any, TextIO

import structlog
from pydantic import SecretStr
from structlog.typing import EventDict, Processor, WrappedLogger

from src.core.config import AppConfig

_REDACTED = "[REDACTED]"
_SECRET_FIELDS = (
    "database_url",
    "wallet_keypair_file",
    "operator_token",
    "session_secret",
    "helius_api_key",
    "executor_internal_token",
    "telegram_bot_token",
)
_SENSITIVE_KEY_PARTS = (
    "api_key",
    "database_url",
    "dsn",
    "keypair",
    "password",
    "private_key",
    "secret",
    "token",
)


class _SecretRedactor:
    def __init__(self, secrets: Sequence[str]) -> None:
        self._secrets = tuple(secret for secret in secrets if secret)

    def __call__(
        self,
        _: WrappedLogger,
        __: str,
        event_dict: EventDict,
    ) -> EventDict:
        return self._redact_mapping(event_dict)

    def _redact_mapping(self, value: Mapping[str, Any]) -> EventDict:
        output: EventDict = {}
        for key, item in value.items():
            normalized_key = key.lower()
            if any(part in normalized_key for part in _SENSITIVE_KEY_PARTS):
                output[key] = _REDACTED
            else:
                output[key] = self._redact_value(item)
        return output

    def _redact_value(self, value: Any) -> Any:
        if isinstance(value, str):
            for secret in self._secrets:
                value = value.replace(secret, _REDACTED)
            return value
        if isinstance(value, Mapping):
            return self._redact_mapping(value)
        if isinstance(value, list):
            return [self._redact_value(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self._redact_value(item) for item in value)
        return value


def _secret_values(settings: AppConfig) -> tuple[str, ...]:
    values: list[str] = []
    for field_name in _SECRET_FIELDS:
        value = getattr(settings, field_name)
        if isinstance(value, SecretStr):
            values.append(value.get_secret_value())
    return tuple(values)


def configure_logging(settings: AppConfig, *, stream: TextIO | None = None) -> None:
    """Route structlog, stdlib and uvicorn records through one redacting JSON formatter."""

    redactor = _SecretRedactor(_secret_values(settings))
    timestamp = structlog.processors.TimeStamper(fmt="iso", utc=True)
    foreign_processors: list[Processor] = [
        structlog.stdlib.add_log_level,
        structlog.stdlib.ExtraAdder(),
        timestamp,
        redactor,
    ]
    formatter = structlog.stdlib.ProcessorFormatter(
        processor=structlog.processors.JSONRenderer(),
        foreign_pre_chain=foreign_processors,
    )
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(settings.app_log_level)

    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(logger_name)
        logger.handlers.clear()
        logger.propagate = True

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            timestamp,
            redactor,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=False,
    )
