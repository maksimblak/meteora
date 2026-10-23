"""D04: the live Data API has no 5m window, so volume per minute comes from the marked one."""

from decimal import Decimal

from src.market.models import DataQuality, PoolSnapshot
from tests.domain.guide_fixtures import guide_features, guide_history


def _without_five_minutes(snapshot: PoolSnapshot, *, mark: str | None) -> PoolSnapshot:
    data = snapshot.model_dump()
    data["windows"] = {name: metric for name, metric in snapshot.windows.items() if name != "5m"}
    data.update(
        data_quality=DataQuality.PARTIAL,
        missing_fields=("windows.5m",),
        short_window_source=mark,
    )
    return PoolSnapshot.model_validate(data)


def _live_history(*, mark: str | None) -> list[PoolSnapshot]:
    history = guide_history(long="300000", hour="900000", tvl="150000")
    return [_without_five_minutes(snapshot, mark=mark) for snapshot in history]


def test_volume_per_minute_falls_back_to_the_marked_short_window() -> None:
    features = guide_features(_live_history(mark="30m"))

    # 300 000 over 30 minutes = 10 000 a minute; 50 000 per 5 minutes on 150 000 liquidity
    assert features.volume_per_minute == Decimal("10000")
    assert features.volume_to_liquidity == Decimal("50000") / Decimal("150000")
    assert features.volume_window_source == "30m"


def test_the_configured_window_wins_and_is_named_as_the_source() -> None:
    features = guide_features(guide_history(short="100000"))

    assert features.volume_per_minute == Decimal("20000")
    assert features.volume_window_source == "5m"


def test_no_mark_or_a_window_beyond_the_limit_leaves_volume_per_minute_unknown() -> None:
    unmarked = guide_features(_live_history(mark=None))
    hourly = guide_features(_live_history(mark="1h"))
    hourly_allowed = guide_features(_live_history(mark="1h"), maximum_volume_window="1h")

    assert unmarked.volume_per_minute is None
    assert unmarked.volume_window_source is None
    assert unmarked.volume_to_liquidity is None
    assert hourly.volume_per_minute is None  # a 1h average is beyond the default 30m limit
    assert hourly.volume_window_source is None
    assert hourly_allowed.volume_per_minute == Decimal("15000")  # 900 000 / 60
    assert hourly_allowed.volume_window_source == "1h"
