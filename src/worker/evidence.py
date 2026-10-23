"""Daily promotion evidence: one observation per complete UTC day the worker watched (§7.1).

The worker tallies each UTC day as it runs - candidate cycles, whether the universe was
fresh, decisions, the candidates among them and how many had every signal, and risk
invariant breaches - and, on the first cycle of the next day, hands the day to
``PromotionEvidenceService.record_daily``. Only a day the worker watched from its start to
its end without a long gap is recorded: a day it joined late, left early or skipped for a
while is no full shadow day, so ``build_shadow`` restarts the count of consecutive days
after it (fail-closed). The tally lives in this process; a restart costs that day.

``data_coverage``: the share of the day's candidates (pools past the cheap filters) whose
decision had no data gap; on a day without candidates, the share of cycles that ran on a
fresh universe.
"""

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from src.accounting.repository import AccountingRepository
from src.accounting.service import PerformanceService
from src.core.clock import Clock
from src.domain.models.trading import TradingMode
from src.modes.models import DailyPromotionObservation
from src.modes.repository import ModeRepository
from src.modes.service import PromotionEvidenceService
from src.observability.logging import error_fields, get_logger
from src.worker.portfolio import ShadowPortfolio

_DAY = timedelta(days=1)
_MICROSECOND = timedelta(microseconds=1)


@dataclass(slots=True)
class _DayTally:
    first_cycle: datetime
    last_cycle: datetime
    largest_gap: timedelta = timedelta(0)
    cycles: int = 0
    fresh_cycles: int = 0
    decisions: int = 0
    candidates: int = 0
    covered: int = 0
    breaches: int = 0
    modes: set[TradingMode] = field(default_factory=set)


class DailyEvidence:
    def __init__(
        self,
        *,
        modes: ModeRepository,
        accounting: AccountingRepository,
        portfolio: ShadowPortfolio,
        clock: Clock,
        ruleset_version: str,
        cycle_gap: timedelta,
    ) -> None:
        if cycle_gap <= timedelta(0):
            raise ValueError("cycle_gap must be positive")
        self._modes = modes
        self._performance = PerformanceService(accounting)
        self._promotion = PromotionEvidenceService(modes, clock)
        self._portfolio = portfolio
        self._clock = clock
        self._ruleset = ruleset_version
        self._gap = cycle_gap
        self._days: dict[date, _DayTally] = {}
        self._log = get_logger("worker.evidence")

    def cycle(self, at: datetime, *, fresh: bool, mode: TradingMode) -> None:
        """One candidate cycle; ``fresh``: it evaluated a universe of current data."""

        day = _utc_date(at)
        tally = self._days.get(day)
        if tally is None:
            tally = self._days[day] = _DayTally(first_cycle=at, last_cycle=at)
        else:
            tally.largest_gap = max(tally.largest_gap, at - tally.last_cycle)
            tally.last_cycle = max(tally.last_cycle, at)
        tally.cycles += 1
        tally.fresh_cycles += 1 if fresh else 0
        tally.modes.add(mode)

    def decision(self, at: datetime, *, candidate: bool, covered: bool) -> None:
        tally = self._days.get(_utc_date(at))
        if tally is None:
            return  # outside any watched cycle: it cannot vouch for the day
        tally.decisions += 1
        if candidate:
            tally.candidates += 1
            tally.covered += 1 if covered else 0

    def breach(self, at: datetime) -> None:
        tally = self._days.get(_utc_date(at))
        if tally is not None:
            tally.breaches += 1

    def close_finished_days(self) -> tuple[DailyPromotionObservation, ...]:
        """Record every complete, fully watched day before today; forget the others."""

        today = _utc_date(self._clock.now())
        recorded: list[DailyPromotionObservation] = []
        for day in sorted(item for item in self._days if item < today):
            tally = self._days.pop(day)
            try:
                observation = self._observation(day, tally)
                if observation is not None:
                    self._promotion.record_daily(observation)
                    recorded.append(observation)
            except Exception as error:  # evidence never stops trading cycles
                self._log.error("daily_evidence_failed", day=day.isoformat(), **error_fields(error))
        return tuple(recorded)

    def _observation(self, day: date, tally: _DayTally) -> DailyPromotionObservation | None:
        start = datetime.combine(day, time.min, tzinfo=UTC)
        end = start + _DAY
        watched = (
            tally.first_cycle - start <= self._gap
            and end - tally.last_cycle <= self._gap
            and tally.largest_gap <= self._gap
        )
        if not watched or len(tally.modes) != 1:
            self._log.info(
                "daily_evidence_skipped",
                day=day.isoformat(),
                reason="NOT_WATCHED_IN_FULL" if not watched else "MODE_CHANGED",
            )
            return None
        (mode,) = tally.modes
        if any(
            item.risk_date == day for item in self._modes.observations(self._ruleset, mode)
        ):
            return None
        risk_day = self._modes.risk_day(day)
        if risk_day is None:
            self._log.info("daily_evidence_skipped", day=day.isoformat(), reason="NO_RISK_DAY")
            return None
        start_equity = risk_day.starting_equity
        end_equity = max(self._portfolio.equity(end - _MICROSECOND), Decimal(1))
        trough = max(start_equity + risk_day.conservative_pnl, Decimal(1))
        if tally.candidates:
            coverage = Decimal(tally.covered) / Decimal(tally.candidates)
        else:
            coverage = Decimal(tally.fresh_cycles) / Decimal(tally.cycles)
        return self._performance.daily_observation(
            day,
            ruleset_version=self._ruleset,
            mode=mode,
            start_equity=start_equity,
            end_equity=end_equity,
            conservative_open_equity=trough,
            data_coverage=coverage,
            decision_count=tally.decisions,
            risk_invariant_breaches=tally.breaches,
        )


def _utc_date(instant: datetime) -> date:
    return instant.astimezone(UTC).date()
