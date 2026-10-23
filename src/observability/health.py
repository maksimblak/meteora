"""Liveness (is the process looping?) versus readiness (is it fit for its mode?).

Readiness is mode-specific and fail-closed: SHADOW keeps observing through provider
degradation (reported as ``DEGRADED`` with reasons), while APPROVAL and LIVE are
``NOT_READY`` unless every provider is healthy, the universe is fresh, no critical
alert is open and an execution gateway is configured and ready.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from src.core.clock import Clock
from src.domain.models.trading import TradingMode
from src.market.models import ProviderHealth, ProviderState


class ReadinessStatus(StrEnum):
    READY = "READY"
    DEGRADED = "DEGRADED"
    NOT_READY = "NOT_READY"


@dataclass(frozen=True, slots=True)
class LivenessReport:
    alive: bool
    last_heartbeat_at: datetime | None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    mode: TradingMode
    status: ReadinessStatus
    reasons: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return self.status is not ReadinessStatus.NOT_READY


class HealthMonitor:
    def __init__(
        self,
        clock: Clock,
        *,
        heartbeat_timeout: timedelta = timedelta(seconds=60),
        maximum_sync_age: timedelta = timedelta(minutes=15),
    ) -> None:
        self._clock = clock
        self._heartbeat_timeout = heartbeat_timeout
        self._maximum_sync_age = maximum_sync_age
        self._heartbeat_at: datetime | None = None
        self._persistence_ok = False
        self._providers: dict[str, Callable[[], ProviderHealth]] = {}
        self._last_sync_at: datetime | None = None
        self._open_alerts = 0
        self._gateway_ready: bool | None = None

    def heartbeat(self) -> None:
        self._heartbeat_at = self._clock.now()

    def set_persistence(self, ok: bool) -> None:
        self._persistence_ok = ok

    def register_provider(self, name: str, health: Callable[[], ProviderHealth]) -> None:
        self._providers[name] = health

    def set_last_sync(self, at: datetime | None) -> None:
        self._last_sync_at = at

    def set_open_critical_alerts(self, count: int) -> None:
        self._open_alerts = count

    def set_execution_gateway(self, ready: bool | None) -> None:
        """``None`` means no gateway is configured (the default shadow-only stack)."""

        self._gateway_ready = ready

    def liveness(self) -> LivenessReport:
        if self._heartbeat_at is None:
            return LivenessReport(False, None, "NO_HEARTBEAT")
        if self._clock.now() - self._heartbeat_at > self._heartbeat_timeout:
            return LivenessReport(False, self._heartbeat_at, "HEARTBEAT_STALE")
        return LivenessReport(True, self._heartbeat_at)

    def readiness(self, mode: TradingMode) -> ReadinessReport:
        if not self._persistence_ok:
            return ReadinessReport(mode, ReadinessStatus.NOT_READY, ("PERSISTENCE_UNAVAILABLE",))
        reasons: list[str] = []
        for name, health in sorted(self._providers.items()):
            state = health().state
            if state is not ProviderState.HEALTHY:
                reasons.append(f"PROVIDER_DEGRADED:{name}")
        if not self._providers:
            reasons.append("PROVIDERS_NOT_CONFIGURED")
        now = self._clock.now()
        if self._last_sync_at is None or now - self._last_sync_at > self._maximum_sync_age:
            reasons.append("UNIVERSE_STALE")
        if self._open_alerts:
            reasons.append("CRITICAL_ALERTS_OPEN")
        if mode is not TradingMode.SHADOW:
            if self._gateway_ready is None:
                reasons.append("EXECUTION_GATEWAY_NOT_CONFIGURED")
            elif not self._gateway_ready:
                reasons.append("EXECUTION_GATEWAY_NOT_READY")
        if not reasons:
            return ReadinessReport(mode, ReadinessStatus.READY, ())
        status = (
            ReadinessStatus.DEGRADED if mode is TradingMode.SHADOW else ReadinessStatus.NOT_READY
        )
        return ReadinessReport(mode, status, tuple(reasons))
