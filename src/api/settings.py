"""Operator-surface settings: auth material, approval TTL, status probes and streaming.

Secrets (``APP_OPERATOR_TOKEN``, ``APP_SESSION_SECRET``) arrive through ``AppConfig``;
this module only keeps them as ``SecretStr``/bytes and never renders them.
"""

import hashlib
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta

from pydantic import SecretStr

from src.core.config import AppConfig

WORKER_HEALTH_URL = "WORKER_HEALTH_URL"
OPERATOR_APPROVAL_TTL_SECONDS = "OPERATOR_APPROVAL_TTL_SECONDS"
OPERATOR_SESSION_TTL_SECONDS = "OPERATOR_SESSION_TTL_SECONDS"

_MAXIMUM_APPROVAL_TTL = timedelta(minutes=10)


@dataclass(frozen=True, slots=True)
class OperatorSettings:
    operator_token: SecretStr | None
    session_key: bytes
    session_ttl: timedelta = timedelta(hours=12)
    approval_ttl: timedelta = timedelta(seconds=90)
    worker_health_url: str | None = None
    probe_timeout: timedelta = timedelta(seconds=2)
    stream_interval: timedelta = timedelta(seconds=5)
    # The overview's "what is happening now" re-reads the full status (~0.4 s), and its
    # numbers move once per worker cycle: it goes on every 3rd tick (15 s), not every one.
    overview_every_ticks: int = 3
    # A stream is recycled after this many ticks (the browser reconnects), so no
    # connection lives forever; ``None`` keeps it open until the client leaves.
    stream_max_events: int | None = 120
    maximum_sync_age: timedelta = timedelta(minutes=15)

    def __post_init__(self) -> None:
        if len(self.session_key) < 32:
            raise ValueError("session key must contain at least 32 bytes")
        if not timedelta(0) < self.approval_ttl <= _MAXIMUM_APPROVAL_TTL:
            raise ValueError("approval TTL must be in (0, 10 minutes]")
        if self.session_ttl <= timedelta(0):
            raise ValueError("session TTL must be positive")
        if self.stream_max_events is not None and self.stream_max_events < 1:
            raise ValueError("stream_max_events must be positive")
        if self.overview_every_ticks < 1:
            raise ValueError("overview_every_ticks must be positive")

    @classmethod
    def from_environment(cls, config: AppConfig, environ: Mapping[str, str]) -> "OperatorSettings":
        session = config.session_secret
        # Without APP_SESSION_SECRET web sessions live only as long as this process.
        key = (
            hashlib.sha256(
                b"meteora-operator-session:" + session.get_secret_value().encode("utf-8")
            ).digest()
            if session is not None
            else secrets.token_bytes(32)
        )
        return cls(
            operator_token=config.operator_token,
            session_key=key,
            session_ttl=_seconds(environ, OPERATOR_SESSION_TTL_SECONDS, 12 * 3600),
            approval_ttl=_seconds(environ, OPERATOR_APPROVAL_TTL_SECONDS, 90),
            worker_health_url=environ.get(WORKER_HEALTH_URL) or None,
        )

    @property
    def auth_configured(self) -> bool:
        return self.operator_token is not None and bool(self.operator_token.get_secret_value())


def _seconds(environ: Mapping[str, str], name: str, default: int) -> timedelta:
    raw = environ.get(name)
    if not raw:
        return timedelta(seconds=default)
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer number of seconds") from error
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return timedelta(seconds=value)
