"""After an exit the author enters the coin again only on new volumes or a new high
(video 1, 03:00 and 04:30: "Если АТХ пробьёт, то перезайдём"; video 1, 13:30: "подожду ...
нового от хая на более уверенных объёмах").

The default guide history at ``NOW``: 100 000 in the 5m window - 20 000 a minute, summed
over the token's DLMM pools - at the price 1.
"""

from datetime import timedelta
from decimal import Decimal

import pytest

from src.domain.screening import (
    CandidateStatus,
    ReentryMark,
    RejectionCode,
    TokenSelection,
    TokenVolumeSource,
)
from tests.domain.guide_fixtures import NOW, guide_history
from tests.domain.test_t18_selection import evidence, screen


def after_exit(
    volume: str | None = "20000",
    high: str | None = "1",
    *,
    source: TokenVolumeSource | None = TokenVolumeSource.DLMM_POOLS,
    pending: bool = False,
) -> TokenSelection:
    return TokenSelection(
        enhanced_pending=pending,
        reentry=ReentryMark(
            exited_at=NOW - timedelta(minutes=10),
            since=NOW - timedelta(minutes=40),
            volume_per_minute=None if volume is None else Decimal(volume),
            volume_source=source,
            price_high=None if high is None else Decimal(high),
        ),
    )


@pytest.mark.parametrize(
    "selection",
    [
        after_exit(),  # 20 000 a minute again, not more; the price 1 is the high itself
        after_exit(high=None),  # no price since the entry is known: no new high shown
        # The entry was decided on Jupiter's figure: a DLMM sum is no comparison.
        after_exit("15000", source=TokenVolumeSource.JUPITER),
        after_exit(None),  # the entry's figure is unknown: only a new high can lift it
    ],
    ids=["same_volume_and_price", "high_unknown", "other_source", "entry_volume_unknown"],
)
def test_without_new_volumes_or_a_new_high_the_token_waits(selection: TokenSelection) -> None:
    decision = screen(guide_history(), selection=selection)

    assert decision.status is CandidateStatus.REJECT
    assert RejectionCode.NO_NEW_VOLUME_OR_HIGH in decision.reasons
    found = evidence(decision)
    mark = selection.reentry
    assert mark is not None
    assert found["reentry_last_exit_at"][0] == (NOW - timedelta(minutes=10)).isoformat()
    assert found["reentry_volume_per_minute"][0] == "20000"
    assert found["reentry_price"] == ("1", f">{mark.price_high or 'UNKNOWN'}")


@pytest.mark.parametrize(
    "selection",
    [
        after_exit("15000"),  # 20 000 a minute now against 15 000 at the entry
        after_exit("30000", high="0.9"),  # the price 1 stands above 0.9, every price since
    ],
    ids=["new_volumes", "new_high"],
)
def test_new_volumes_or_a_new_high_open_the_token_again(selection: TokenSelection) -> None:
    decision = screen(guide_history(), selection=selection)

    assert decision.status is CandidateStatus.ACCEPT, decision.reasons


def test_while_an_enhanced_source_is_to_read_the_token_only_a_new_high_is_judged() -> None:
    # Cheap filters before GMGN: the token's volume is not read yet, so no reject can be
    # final on it; the deep check decides with the reading.
    decision = screen(guide_history(), selection=after_exit(pending=True))

    assert RejectionCode.NO_NEW_VOLUME_OR_HIGH not in decision.reasons
