"""Incremental, exact market features derived from immutable snapshots."""

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from src.market.models import DataQuality, PoolSnapshot, WindowMetrics


class FeatureReadiness(StrEnum):
    WARMING_UP = "WARMING_UP"
    READY = "READY"


@dataclass(frozen=True, slots=True)
class FeatureRules:
    required_history_points: int = 12
    history_limit: int = 288

    def __post_init__(self) -> None:
        if self.required_history_points < 1:
            raise ValueError("required_history_points must be at least one")
        if self.history_limit < self.required_history_points:
            raise ValueError("history_limit must cover required_history_points")


class FeatureVector(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    observed_at: datetime
    history_points: int
    readiness: FeatureReadiness
    volume_acceleration: Decimal | None
    turnover_24h: Decimal | None
    fee_tvl_24h: Decimal | None
    price_volatility: Decimal | None
    data_quality: DataQuality
    missing_signals: tuple[str, ...] = ()


class FeatureEngine:
    """Compute features from only the observations supplied by the caller."""

    def __init__(self) -> None:
        self._states: dict[tuple[str, FeatureRules], _FeatureState] = {}

    def compute(
        self,
        history: Sequence[PoolSnapshot],
        rules: FeatureRules,
    ) -> FeatureVector:
        if not history:
            raise ValueError("feature history must not be empty")

        ordered = sorted(history, key=lambda item: item.observed_at)[-rules.history_limit :]
        latest = ordered[-1]
        if any(item.pool.address != latest.pool.address for item in ordered):
            raise ValueError("feature history must belong to one pool")

        missing: list[str] = []
        acceleration = _volume_acceleration(latest, missing)
        window_24h = latest.windows.get("24h")
        turnover = _turnover(window_24h, latest.tvl, missing)
        fee_tvl = _fee_tvl(window_24h, latest.tvl, missing)
        volatility = _price_volatility(ordered, missing)
        readiness = (
            FeatureReadiness.READY
            if len(ordered) >= rules.required_history_points
            else FeatureReadiness.WARMING_UP
        )
        quality = (
            DataQuality.COMPLETE
            if not missing and all(item.data_quality is DataQuality.COMPLETE for item in ordered)
            else DataQuality.PARTIAL
        )
        return FeatureVector(
            observed_at=latest.observed_at,
            history_points=len(ordered),
            readiness=readiness,
            volume_acceleration=acceleration,
            turnover_24h=turnover,
            fee_tvl_24h=fee_tvl,
            price_volatility=volatility,
            data_quality=quality,
            missing_signals=tuple(sorted(set(missing))),
        )

    def update(self, snapshot: PoolSnapshot, rules: FeatureRules) -> FeatureVector:
        """Increment one pool's bounded feature state without rescanning its history."""

        key = (snapshot.pool.address.root, rules)
        state = self._states.setdefault(key, _FeatureState())
        if state.snapshots:
            latest = state.snapshots[-1]
            if snapshot.observed_at < latest.observed_at:
                raise ValueError("incremental snapshots must be ordered by observed_at")
            if snapshot.observed_at == latest.observed_at:
                if snapshot != latest:
                    raise ValueError("observation timestamp already has different snapshot data")
                if state.vector is None:
                    raise AssertionError("incremental state has no feature vector")
                return state.vector

            move = _price_move(latest, snapshot)
            state.price_moves.append((snapshot.observed_at, move))
            if move is None:
                state.invalid_price_moves += 1
            else:
                while state.maximum_price_moves and state.maximum_price_moves[-1][1] <= move:
                    state.maximum_price_moves.pop()
                state.maximum_price_moves.append((snapshot.observed_at, move))

        state.snapshots.append(snapshot)
        if snapshot.data_quality is not DataQuality.COMPLETE:
            state.incomplete_snapshots += 1

        if len(state.snapshots) > rules.history_limit:
            evicted = state.snapshots.popleft()
            if evicted.data_quality is not DataQuality.COMPLETE:
                state.incomplete_snapshots -= 1
            removed_at, removed_move = state.price_moves.popleft()
            if removed_move is None:
                state.invalid_price_moves -= 1
            elif state.maximum_price_moves and state.maximum_price_moves[0][0] == removed_at:
                state.maximum_price_moves.popleft()

        missing: list[str] = []
        acceleration = _volume_acceleration(snapshot, missing)
        window_24h = snapshot.windows.get("24h")
        turnover = _turnover(window_24h, snapshot.tvl, missing)
        fee_tvl = _fee_tvl(window_24h, snapshot.tvl, missing)
        if len(state.snapshots) < 2 or state.invalid_price_moves:
            missing.append("price_volatility")
            volatility = None
        else:
            volatility = state.maximum_price_moves[0][1]
        readiness = (
            FeatureReadiness.READY
            if len(state.snapshots) >= rules.required_history_points
            else FeatureReadiness.WARMING_UP
        )
        quality = (
            DataQuality.COMPLETE
            if not missing and state.incomplete_snapshots == 0
            else DataQuality.PARTIAL
        )
        state.vector = FeatureVector(
            observed_at=snapshot.observed_at,
            history_points=len(state.snapshots),
            readiness=readiness,
            volume_acceleration=acceleration,
            turnover_24h=turnover,
            fee_tvl_24h=fee_tvl,
            price_volatility=volatility,
            data_quality=quality,
            missing_signals=tuple(sorted(set(missing))),
        )
        return state.vector


@dataclass(slots=True)
class _FeatureState:
    snapshots: deque[PoolSnapshot] = field(default_factory=deque)
    price_moves: deque[tuple[datetime, Decimal | None]] = field(default_factory=deque)
    maximum_price_moves: deque[tuple[datetime, Decimal]] = field(default_factory=deque)
    invalid_price_moves: int = 0
    incomplete_snapshots: int = 0
    vector: FeatureVector | None = None


def _volume_acceleration(snapshot: PoolSnapshot, missing: list[str]) -> Decimal | None:
    short = snapshot.windows.get("5m")
    baseline = snapshot.windows.get("30m")
    if short is None or baseline is None or baseline.volume == 0:
        missing.append("volume_acceleration")
        return None
    short_rate = short.volume / Decimal(5)
    baseline_rate = baseline.volume / Decimal(30)
    if baseline_rate == 0:
        missing.append("volume_acceleration")
        return None
    return short_rate / baseline_rate


def _turnover(
    window: WindowMetrics | None, denominator: Decimal, missing: list[str]
) -> Decimal | None:
    if window is None or denominator == 0:
        missing.append("turnover_24h")
        return None
    return window.volume / denominator


def _fee_tvl(
    window: WindowMetrics | None, denominator: Decimal, missing: list[str]
) -> Decimal | None:
    if window is None or window.fees is None or denominator == 0:
        missing.append("fee_tvl_24h")
        return None
    return window.fees / denominator


def _price_volatility(history: Sequence[PoolSnapshot], missing: list[str]) -> Decimal | None:
    if len(history) < 2:
        missing.append("price_volatility")
        return None
    moves: list[Decimal] = []
    for previous, current in zip(history, history[1:], strict=False):
        if previous.price == 0:
            missing.append("price_volatility")
            return None
        moves.append(abs(current.price - previous.price) / previous.price)
    return max(moves)


def _price_move(previous: PoolSnapshot, current: PoolSnapshot) -> Decimal | None:
    if previous.price == 0:
        return None
    return abs(current.price - previous.price) / previous.price
