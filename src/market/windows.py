"""Short windows from the cumulative counters of adjacent snapshots (spec 6.1, D07).

The live Data API reports no 5-minute window, only lifetime ``cumulative_metrics`` of
volume and fees, so the short window of a snapshot is the counters' increase since an
earlier snapshot of the same pool, rescaled to the window length. Only the *previous*
snapshot is ever read: a snapshot whose predecessor is not older than it gets no window
at all, so no figure can leak from the future.

Each derivation carries, per counter, a *mark*: the highest reading so far, when it was
seen, and the older highs still close enough to start a window. A window starts at the
newest high that is at least ``minimum_interval`` (the window less its tolerance) old,
and that start must be within ``maximum_interval`` (one cadence step): an increase over
a shorter interval is never stretched to the window (after a restart one minute of
trading is not five), and sweeps that come sooner than the window are summed until they
cover it. After 1M@t0 -> 900k@t5, the reading 1.01M@t10 is 10 000 over ten minutes -
no 5m window - and moves the mark, so 1.02M@t15 is 10 000 in five minutes.

The counters are not monotonic at noise level (a -1.1 move within seconds was recorded on
a 2.6e10 counter): a drop within ``max(absolute, relative * mark)`` counts as zero. A
larger drop is ``NEGATIVE_DELTA``; readings that stay below the mark without falling for
``counter_reset_after`` are a reset of the counter (``COUNTER_RESET``): the mark moves to
the new counter, remembering the highest reading before the reset. A counter that climbs
back to that reading is the old counter going on, so it is measured from it, never from
the counter after the reset.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from src.market.models import (
    CounterMark,
    CounterReading,
    PoolSnapshot,
    ShortWindowDerivation,
    ShortWindowStatus,
    WindowMetrics,
    WindowOrigin,
)

_MICROSECOND = timedelta(microseconds=1)
WINDOW_MINUTES: dict[str, int] = {"5m": 5, "30m": 30, "1h": 60, "4h": 240, "24h": 1_440}
TOO_SHORT = "INTERVAL_TOO_SHORT"
TOO_LONG = "INTERVAL_TOO_LONG"


@dataclass(frozen=True, slots=True)
class ShortWindowRules:
    """Tolerances of the derivation; ``maximum_interval`` follows the universe cadence.

    ``interval_tolerance``: how much shorter than the window an interval may be and still
    make it (scheduling jitter of the sweeps), so ``minimum_interval`` is the window less
    it; a shorter increase is summed with the next ones, never stretched to the window.
    """

    window: str = "5m"
    interval_tolerance: timedelta = timedelta(seconds=30)
    maximum_interval: timedelta = timedelta(minutes=5, seconds=30)
    negative_noise_absolute: Decimal = Decimal("10")
    negative_noise_relative: Decimal = Decimal("0.0000001")
    counter_reset_after: timedelta = timedelta(minutes=30)

    def __post_init__(self) -> None:
        if self.window not in WINDOW_MINUTES:
            raise ValueError("unsupported short window")
        if not timedelta(0) <= self.interval_tolerance < self.window_length:
            raise ValueError("interval_tolerance must be in [0, window length)")
        if self.maximum_interval < self.minimum_interval:
            raise ValueError("maximum_interval must not be below the window less its tolerance")
        if any(
            isinstance(value, float) or value < 0
            for value in (self.negative_noise_absolute, self.negative_noise_relative)
        ):
            raise ValueError("negative-noise tolerances must be non-negative Decimals")
        if self.counter_reset_after <= timedelta(0):
            raise ValueError("counter_reset_after must be positive")

    @property
    def window_length(self) -> timedelta:
        return timedelta(minutes=WINDOW_MINUTES[self.window])

    @property
    def minimum_interval(self) -> timedelta:
        """The shortest interval whose increase is a window (the window less the tolerance)."""

        return self.window_length - self.interval_tolerance


class _Move(StrEnum):
    INCREASE = "INCREASE"
    NOISE = "NOISE"
    DROP = "DROP"
    RESET = "RESET"
    MISSING = "MISSING"


@dataclass(frozen=True, slots=True)
class _Step:
    """One counter's reading against its mark.

    ``delta``/``interval``: for an increase (or noise) the increase since the window's start
    and the time since it, with ``timing`` naming why they make no window; for a drop the
    fall below the mark and the time since the mark.
    """

    move: _Move
    mark: CounterMark | None
    delta: Decimal | None = None
    interval: timedelta | None = None
    timing: str | None = None


def derive_short_window(
    current: PoolSnapshot, previous: PoolSnapshot | None, rules: ShortWindowRules
) -> PoolSnapshot:
    """``current`` with its short window derived from ``previous``, or marked ``UNKNOWN``."""

    if rules.window in current.windows:
        return current  # the source reported it; nothing to derive
    if previous is None or previous.pool != current.pool:
        marks = (
            _first_mark(current.cumulative_volume, current.source_at),
            _first_mark(current.cumulative_fees, current.source_at),
        )
        return _unknown(current, rules, "NO_PREVIOUS", None, marks)
    base_volume, base_fees = _marks(previous)
    if current.source_at <= previous.source_at:
        # Not a later observation: nothing of it may move the marks.
        return _unknown(current, rules, TOO_SHORT, previous.source_at, (base_volume, base_fees))
    volume = _advance(base_volume, current.cumulative_volume, current.source_at, rules)
    fees = _advance(base_fees, current.cumulative_fees, current.source_at, rules)
    marks = (volume.mark, fees.mark)
    moves = {volume.move, fees.move}
    reason: str | None = None
    if _Move.MISSING in moves:
        reason = "COUNTER_MISSING"
    elif _Move.RESET in moves:
        reason = "COUNTER_RESET"
    elif _Move.DROP in moves:
        reason = "NEGATIVE_DELTA"
    elif TOO_SHORT in (volume.timing, fees.timing):
        reason = TOO_SHORT
    elif TOO_LONG in (volume.timing, fees.timing):
        reason = TOO_LONG
    if reason is not None:
        return _unknown(current, rules, reason, previous.source_at, marks, volume, fees)
    assert volume.interval is not None and fees.interval is not None
    assert volume.delta is not None and fees.delta is not None
    window = Decimal(WINDOW_MINUTES[rules.window] * 60)
    derivation = ShortWindowDerivation(
        window=rules.window,
        origin=WindowOrigin.CUMULATIVE_DELTA,
        status=ShortWindowStatus.DERIVED,
        previous_source_at=previous.source_at,
        interval_seconds=_seconds(volume.interval),
        volume_delta=volume.delta,
        fees_delta=fees.delta,
        zeroed_noise=_Move.NOISE in moves,
        volume_mark=volume.mark,
        fees_mark=fees.mark,
    )
    windows = dict(current.windows)
    windows[rules.window] = WindowMetrics(
        volume=volume.delta * window / _seconds(volume.interval),
        fees=fees.delta * window / _seconds(fees.interval),
    )
    return _replace(current, windows=windows, short_window=derivation)


def _advance(
    mark: CounterMark | None, counter: Decimal | None, at: datetime, rules: ShortWindowRules
) -> _Step:
    """Move one counter's mark by a new reading and say what the reading was."""

    if counter is None or mark is None:
        return _Step(_Move.MISSING, mark if counter is None else _first_mark(counter, at))
    previous = mark.reset_from
    if previous is not None and counter >= previous.value - _noise(previous.value, rules):
        # Back at the level it had before the reset: the old counter goes on (A2).
        mark = CounterMark(value=previous.value, at=previous.at)
    if counter >= mark.value - _noise(mark.value, rules):
        return _increase(mark, counter, at, rules)
    steady = mark.low_last is not None and counter >= mark.low_last
    low_since = mark.low_since if steady and mark.low_since is not None else at
    if at - low_since >= rules.counter_reset_after:
        highest = CounterReading(value=mark.value, at=mark.at)
        if mark.reset_from is not None and mark.reset_from.value > highest.value:
            highest = mark.reset_from
        reset = CounterMark(value=counter, at=at, reset_from=highest)
        return _Step(_Move.RESET, reset, counter - mark.value, at - mark.at)
    lowered = CounterMark(
        value=mark.value,
        at=mark.at,
        low_since=low_since,
        low_last=counter,
        earlier=_within_reach(mark.earlier, at, rules),
        reset_from=mark.reset_from,
    )
    return _Step(_Move.DROP, lowered, counter - mark.value, at - mark.at)


def _increase(
    mark: CounterMark, counter: Decimal, at: datetime, rules: ShortWindowRules
) -> _Step:
    """An increase (or a dip within noise, which counts as no change) since the window's
    start: the newest high at least ``minimum_interval`` old."""

    reading = max(counter, mark.value)
    starts = (*mark.earlier, CounterReading(value=mark.value, at=mark.at))
    due = [start for start in starts if at - start.at >= rules.minimum_interval]
    start = due[-1] if due else starts[0]
    interval = at - start.at
    timing: str | None = None
    if not due:
        timing = TOO_SHORT
    elif interval > rules.maximum_interval:
        timing = TOO_LONG
    moved = CounterMark(
        value=reading,
        at=at,
        earlier=_within_reach(starts, at, rules),
        reset_from=mark.reset_from,
    )
    move = _Move.INCREASE if counter >= mark.value else _Move.NOISE
    return _Step(move, moved, reading - start.value, interval, timing)


def _within_reach(
    starts: tuple[CounterReading, ...], at: datetime, rules: ShortWindowRules
) -> tuple[CounterReading, ...]:
    """The highs a later reading may still start its window at (no older than one step)."""

    return tuple(start for start in starts if at - start.at <= rules.maximum_interval)


def _noise(value: Decimal, rules: ShortWindowRules) -> Decimal:
    return max(rules.negative_noise_absolute, value * rules.negative_noise_relative)


def _marks(previous: PoolSnapshot) -> tuple[CounterMark | None, CounterMark | None]:
    """The predecessor's marks; its own readings if it carries none (a first snapshot)."""

    derivation = previous.short_window
    volume = derivation.volume_mark if derivation is not None else None
    fees = derivation.fees_mark if derivation is not None else None
    return (
        volume or _first_mark(previous.cumulative_volume, previous.source_at),
        fees or _first_mark(previous.cumulative_fees, previous.source_at),
    )


def _first_mark(counter: Decimal | None, at: datetime) -> CounterMark | None:
    return None if counter is None else CounterMark(value=counter, at=at)


def _seconds(interval: timedelta) -> Decimal:
    return Decimal(interval // _MICROSECOND) / Decimal(1_000_000)


def _unknown(
    current: PoolSnapshot,
    rules: ShortWindowRules,
    reason: str,
    previous_source_at: datetime | None,
    marks: tuple[CounterMark | None, CounterMark | None],
    volume: _Step | None = None,
    fees: _Step | None = None,
) -> PoolSnapshot:
    """No window; the marks carried to the next snapshot and what was measured, if any."""

    interval = volume.interval if volume is not None else None
    derivation = ShortWindowDerivation(
        window=rules.window,
        origin=WindowOrigin.CUMULATIVE_DELTA,
        status=ShortWindowStatus.UNKNOWN,
        reason=reason,
        previous_source_at=previous_source_at,
        interval_seconds=None if interval is None else _seconds(interval),
        volume_delta=None if volume is None else volume.delta,
        fees_delta=None if fees is None else fees.delta,
        volume_mark=marks[0],
        fees_mark=marks[1],
    )
    return _replace(current, short_window=derivation)


def _replace(snapshot: PoolSnapshot, **changes: object) -> PoolSnapshot:
    """A fully re-validated copy (``model_copy`` would skip the snapshot invariants)."""

    values: dict[str, object] = {
        name: getattr(snapshot, name) for name in PoolSnapshot.model_fields
    }
    values["windows"] = dict(snapshot.windows)
    values.update(changes)
    return PoolSnapshot(**values)  # type: ignore[arg-type]
