"""Structured JSON logging with value- and key-based secret redaction.

Every record - structlog's own and foreign stdlib ones (uvicorn's "Exception in ASGI
application" included) - passes one chain: a traceback is first formatted into the
``exception`` text, then the redactor removes every secret value of the registry
(``AppConfig.secret_values``) from all strings. Nothing is rendered before that.
"""

import logging
import sys
from collections.abc import Mapping, Sequence
from typing import Any, TextIO

import structlog
from structlog.typing import EventDict, Processor, WrappedLogger

from src.core.config import AppConfig

_REDACTED = "[REDACTED]"
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
        if isinstance(value, BaseException):
            # An exception object outside ``exc_info`` is rendered by its text, redacted.
            return self._redact_value(f"{type(value).__name__}: {value}")
        return value


def configure_logging(settings: AppConfig, *, stream: TextIO | None = None) -> None:
    """Route structlog, stdlib and uvicorn records through one redacting JSON formatter."""

    redactor = _SecretRedactor(settings.secret_values())
    timestamp = structlog.processors.TimeStamper(fmt="iso", utc=True)
    # ``exc_info`` becomes the ``exception`` text here, so the redactor sees the traceback;
    # a raw exception tuple would otherwise be rendered with ``repr`` past it.
    traceback = structlog.processors.format_exc_info
    foreign_processors: list[Processor] = [
        structlog.stdlib.add_log_level,
        structlog.stdlib.ExtraAdder(),
        timestamp,
        traceback,
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
            traceback,
            redactor,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=False,
    )
