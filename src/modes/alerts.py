"""Critical alerts the worker closes itself once it has cleared their condition.

An alert raised about one subject carries a subject tag in its message:
``[position:<id>]`` or ``[command:<id>]`` (``alert_subject``). When the worker later
clears that condition on its own - the exit went through the gateway, the unknown outcome
was reconciled with the network - it resolves the matching open alerts through
``ModeService.resolve_alert`` with the system as the confirming actor, so a condition
that no longer exists does not block promotion forever. Conditions that need a person
(a partly executed command, an untracked fill, exhausted attempts) are never resolved here.
"""

import re
from collections.abc import Callable, Collection
from datetime import datetime
from typing import Protocol
from uuid import UUID

from src.core.clock import Clock
from src.modes.models import SYSTEM_ACTOR, CriticalAlert
from src.modes.repository import ModeRepository
from src.modes.service import ModeService
from src.observability.logging import error_fields, get_logger


class AlertLedger(ModeRepository, Protocol):
    """The mode contract plus its alert view (every adapter has it)."""

    def alerts(self) -> tuple[CriticalAlert, ...]: ...


def alert_subject(kind: str, value: object) -> str:
    """The tag an alert message carries for the subject its condition is about."""

    return f"[{kind}:{value}]"


def alert_subjects(message: str, kind: str) -> tuple[str, ...]:
    return tuple(re.findall(rf"\[{re.escape(kind)}:([^\]\s]+)\]", message))


class SystemAlertResolver:
    def __init__(self, repository: AlertLedger, clock: Clock, ruleset_version: str) -> None:
        self._repository = repository
        self._modes = ModeService(repository, clock, ruleset_version)
        self._log = get_logger("worker.alerts")

    def resolve(
        self,
        codes: Collection[str],
        subject: str,
        reason: str,
        *,
        raised_before: datetime | None = None,
    ) -> tuple[UUID, ...]:
        """Resolve every open alert of ``codes`` whose message carries ``subject``; with
        ``raised_before`` only those raised no later than that instant (the success that
        cleared them), never one about a condition that came back afterwards."""

        def matches(alert: CriticalAlert) -> bool:
            return (
                alert.code in codes
                and subject in alert.message
                and (raised_before is None or alert.created_at <= raised_before)
            )

        return self._resolve(matches, reason)

    def resolve_except(
        self, code: str, kind: str, keep: Collection[str], reason: str
    ) -> tuple[UUID, ...]:
        """Resolve the open ``code`` alerts about ``kind`` subjects none of which is in
        ``keep``; an alert without such a tag only once ``keep`` is empty."""

        kept = set(keep)

        def matches(alert: CriticalAlert) -> bool:
            if alert.code != code:
                return False
            subjects = alert_subjects(alert.message, kind)
            return not kept.intersection(subjects) if subjects else not kept

        return self._resolve(matches, reason)

    def _resolve(
        self, matches: Callable[[CriticalAlert], bool], reason: str
    ) -> tuple[UUID, ...]:
        # Best effort: an alert left open only keeps blocking promotion (fail-safe).
        try:
            if self._repository.open_alert_count() == 0:
                return ()
            candidates = [
                alert for alert in self._repository.alerts() if alert.open and matches(alert)
            ]
        except Exception as error:
            self._log.warning("alert_resolution_skipped", **error_fields(error))
            return ()
        resolved: list[UUID] = []
        for alert in candidates:
            try:
                self._modes.resolve_alert(alert.alert_id, reason, SYSTEM_ACTOR)
            except Exception as error:
                self._log.warning(
                    "alert_not_resolved", alert_id=str(alert.alert_id), **error_fields(error)
                )
                continue
            self._log.info(
                "alert_resolved_by_system", alert_id=str(alert.alert_id), code=alert.code
            )
            resolved.append(alert.alert_id)
        return tuple(resolved)
