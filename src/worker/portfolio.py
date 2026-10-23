"""Shadow portfolio equity and the UTC risk-day lifecycle.

``RiskDayKeeper.ensure_open`` must run before any plan is claimed: the T04
``claim_entry`` refuses entries while the current UTC risk day is not opened.
"""

from datetime import UTC, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from typing import Protocol

from src.core.clock import Clock
from src.domain.risk.models import PortfolioState, RiskPolicy
from src.modes.models import PortfolioEquitySnapshot, RiskDayState
from src.modes.repository import ModeRepository
from src.modes.service import DailyRiskService
from src.portfolio.models import PositionOutcome, PositionStatus, ShadowPosition, TimeWindow

_EPOCH = datetime(2000, 1, 1, tzinfo=UTC)


class PositionBook(Protocol):
    def positions(self) -> tuple[ShadowPosition, ...]: ...

    def outcomes_in(self, window: TimeWindow) -> tuple[PositionOutcome, ...]: ...


class EntryCounter(Protocol):
    def committed_entry_count(self, at: datetime) -> int: ...


class ShadowPortfolio:
    """Paper capital: the configured starting equity plus realized shadow results."""

    def __init__(
        self, initial_equity_base_units: int, book: PositionBook, entries: EntryCounter
    ) -> None:
        if initial_equity_base_units <= 0:
            raise ValueError("initial shadow equity must be positive")
        self._initial = initial_equity_base_units
        self._book = book
        self._entries = entries

    def open_positions(self) -> tuple[ShadowPosition, ...]:
        return tuple(
            position
            for position in self._book.positions()
            if position.status is PositionStatus.OPEN
        )

    def realized_since(self, start: datetime, end: datetime) -> Decimal:
        if end <= start:
            return Decimal(0)
        outcomes = self._book.outcomes_in(TimeWindow(start=start, end=end))
        return sum((outcome.valuation.net_pnl_base_units for outcome in outcomes), Decimal(0))

    def equity(self, at: datetime) -> Decimal:
        return Decimal(self._initial) + self.realized_since(_EPOCH, at + timedelta(microseconds=1))

    def state(self, at: datetime) -> PortfolioState:
        equity = _floor(self.equity(at))
        invested = sum(
            (position.open_cost_basis_base_units for position in self.open_positions()),
            Decimal(0),
        )
        return PortfolioState(
            equity_base_units=max(equity, 1),
            available_balance_base_units=max(_floor(Decimal(equity) - invested), 0),
            open_position_count=self._entries.committed_entry_count(at),
        )


class RiskDayKeeper:
    def __init__(
        self,
        modes: ModeRepository,
        portfolio: ShadowPortfolio,
        clock: Clock,
        policy: RiskPolicy,
    ) -> None:
        self._modes = modes
        self._portfolio = portfolio
        self._clock = clock
        self._policy = policy
        self._daily = DailyRiskService(modes, clock, policy.daily_stop_fraction)

    def ensure_open(self) -> RiskDayState:
        """Open the current UTC risk day (fixing its starting equity) if needed."""

        now = self._clock.now()
        existing = self._modes.risk_day(now.astimezone(UTC).date())
        if existing is not None:
            return existing
        self.snapshot_equity()
        return self._daily.record(Decimal(0), Decimal(0), self._policy.ruleset_version)

    def snapshot_equity(self) -> None:
        now = self._clock.now()
        snapshot = PortfolioEquitySnapshot(
            observed_at=now,
            equity=max(self._portfolio.equity(now), Decimal(1)),
            ruleset_version=self._policy.ruleset_version,
        )
        try:
            self._modes.put_equity_snapshot(snapshot)
        except ValueError:
            # Same instant already recorded with another value: the first mark stands.
            return

    def record(self, realized: Decimal, conservative_unrealized: Decimal) -> RiskDayState:
        self.ensure_open()
        return self._daily.record(realized, conservative_unrealized, self._policy.ruleset_version)


def _floor(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_FLOOR))
