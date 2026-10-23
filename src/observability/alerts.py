"""Critical alerts that survive a storage outage.

The alert is written through ``ModeRepository.raise_alert`` (durable, with its
``CriticalAlertRaised`` outbox event). If that write fails - typically because the
same outage broke audit persistence - the alert is kept in memory, logged at
critical level and exported as a metric until a later ``flush`` persists it.
"""

from collections.abc import Iterable
from typing import Protocol
from uuid import uuid4

from src.core.clock import Clock
from src.modes.models import CriticalAlert
from src.observability.logging import error_fields, get_logger
from src.observability.metrics import WorkerMetrics


class AlertStore(Protocol):
    def raise_alert(self, alert: CriticalAlert) -> None: ...


class CriticalAlertReporter:
    def __init__(
        self,
        store: AlertStore,
        clock: Clock,
        metrics: WorkerMetrics,
        *,
        ruleset_version: str = "system",
    ) -> None:
        self._store = store
        self._clock = clock
        self._metrics = metrics
        self._ruleset_version = ruleset_version
        self._pending: list[CriticalAlert] = []
        self._raised: dict[str, CriticalAlert] = {}
        self._log = get_logger("worker.alerts")

    @property
    def pending(self) -> tuple[CriticalAlert, ...]:
        return tuple(self._pending)

    def adopt(self, alerts: Iterable[CriticalAlert]) -> None:
        self._pending.extend(alerts)
        self._metrics.buffered_alerts(len(self._pending))

    def raise_alert(
        self, code: str, message: str, *, dedupe_key: str | None = None
    ) -> CriticalAlert:
        """Raise once per condition.

        An identical alert that is still waiting to be persisted is never buffered
        twice; with ``dedupe_key`` a recurring condition (seen every cycle) raises a
        single durable alert per process instead of one per cycle.
        """

        for waiting in self._pending:
            if (waiting.code, waiting.message) == (code, message):
                return waiting
        if dedupe_key is not None and dedupe_key in self._raised:
            return self._raised[dedupe_key]
        alert = CriticalAlert(
            alert_id=uuid4(),
            code=code,
            message=message,
            created_at=self._clock.now(),
            ruleset_version=self._ruleset_version,
        )
        self._log.critical("critical_alert", alert_id=str(alert.alert_id), code=code)
        if dedupe_key is not None:
            self._raised[dedupe_key] = alert
        try:
            self._store.raise_alert(alert)
        except Exception as error:
            self._pending.append(alert)
            self._log.critical(
                "critical_alert_not_persisted", alert_id=str(alert.alert_id), **error_fields(error)
            )
        self._metrics.buffered_alerts(len(self._pending))
        return alert

    def flush(self) -> int:
        persisted = 0
        remaining: list[CriticalAlert] = []
        for alert in self._pending:
            try:
                self._store.raise_alert(alert)
            except Exception:
                remaining.append(alert)
            else:
                persisted += 1
        self._pending = remaining
        self._metrics.buffered_alerts(len(self._pending))
        return persisted
