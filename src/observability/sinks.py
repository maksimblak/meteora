"""Default outbox publication target: a structured log line per distinct event.

External publication is optional (spec "События"); the transactional outbox and
the audit trail are the durable record. Repeated deliveries of one ``event_id``
(at-least-once relay) are logged once per process.
"""

from collections import OrderedDict
from uuid import UUID

from src.domain.models.events import AuditEvent
from src.observability.logging import get_logger


class LoggingEventSink:
    def __init__(self, *, remember: int = 10_000) -> None:
        self._seen: OrderedDict[UUID, None] = OrderedDict()
        self._remember = remember
        self._log = get_logger("worker.events")

    async def publish(self, event: AuditEvent) -> None:
        if event.event_id in self._seen:
            return
        self._seen[event.event_id] = None
        if len(self._seen) > self._remember:
            self._seen.popitem(last=False)
        self._log.info(
            "event_published",
            event_id=str(event.event_id),
            event_type=event.event_type.value,
            aggregate_id=str(event.aggregate_id),
            correlation_id=str(event.correlation_id),
            causation_id=None if event.causation_id is None else str(event.causation_id),
            schema_version=event.schema_version,
        )
