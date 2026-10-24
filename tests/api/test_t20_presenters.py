"""T20 in the panel: the evidence row that really decided an "unknown" is the one marked.

A fresh GMGN reading that lacks only the developer's share is ``GMGN_UNAVAILABLE`` (G24):
the operator must see ``developer_share=UNAVAILABLE`` marked, not ``gmgn_status=FRESH``.
A token without a name or ticker has its ``theme`` row marked, both in SHADOW (the flag
``THEME_UNKNOWN``) and in APPROVAL/LIVE (the reason ``GUIDE_SIGNAL_UNKNOWN``).
"""

from src.api.presenters import evidence_views
from src.domain.models.trading import TradingMode
from src.domain.screening.models import EvidenceItem
from tests.domain.guide_fixtures import guide_history
from tests.domain.test_t18_selection import GMGN, reading, screen, with_gmgn

FRESH_WITHOUT_DEVELOPER = (
    EvidenceItem(name="gmgn_status", value="FRESH"),
    EvidenceItem(name="insiders_share", value="0.02", threshold="0.10"),
    EvidenceItem(name="bundling_share", value="0.10", threshold="0.70"),
    EvidenceItem(name="phishing_share", value="0.05", threshold="0.30"),
    EvidenceItem(name="gmgn_top10_holder_share", value="0.15", threshold="0.30"),
    EvidenceItem(name="developer_share", value="UNAVAILABLE", threshold="0"),
)


def marked(
    items: tuple[EvidenceItem, ...], reasons: tuple[str, ...], flags: tuple[str, ...] = ()
) -> set[str]:
    return {row.name for row in evidence_views(items, reasons, flags) if row.triggered}


def test_an_unknown_developer_share_marks_its_own_row_not_the_fresh_status() -> None:
    assert marked(FRESH_WITHOUT_DEVELOPER, ("GMGN_UNAVAILABLE",)) == {"developer_share"}


def test_a_fresh_reading_missing_a_threshold_metric_marks_that_metric() -> None:
    rows = (
        *FRESH_WITHOUT_DEVELOPER[:1],
        EvidenceItem(name="insiders_share", value="UNAVAILABLE", threshold="0.10"),
        EvidenceItem(name="developer_share", value="0", threshold="0"),
    )

    assert marked(rows, ("GMGN_UNAVAILABLE",)) == {"insiders_share"}


def test_a_reading_that_is_not_fresh_still_marks_the_status_row() -> None:
    rows = (
        EvidenceItem(name="gmgn_status", value="UNAVAILABLE:API_TIMEOUT"),
        EvidenceItem(name="insiders_share", value="UNAVAILABLE", threshold="0.10"),
        EvidenceItem(name="developer_share", value="UNAVAILABLE", threshold="0"),
    )

    assert marked(rows, ("GMGN_UNAVAILABLE",)) == {"gmgn_status"}


def test_a_live_decision_without_the_developer_share_is_marked_on_that_row() -> None:
    decision = screen(guide_history(), with_gmgn(reading(developer_share=None)), rules=GMGN)
    reasons = tuple(reason.value for reason in decision.reasons)

    assert "GMGN_UNAVAILABLE" in reasons
    assert marked(decision.evidence, reasons) == {"developer_share"}


def test_an_unknown_theme_is_marked_by_the_missing_signal_reason_or_its_flag() -> None:
    unknown = (EvidenceItem(name="theme", value="UNKNOWN"),)
    checked = (EvidenceItem(name="theme", value="none"),)

    assert marked(unknown, ("GUIDE_SIGNAL_UNKNOWN",)) == {"theme"}
    assert marked(unknown, (), ("THEME_UNKNOWN",)) == {"theme"}
    # GUIDE_SIGNAL_UNKNOWN for another signal (a missing market cap) leaves a checked theme.
    assert marked(checked, ("GUIDE_SIGNAL_UNKNOWN",)) == set()


def test_a_nameless_token_has_its_theme_row_marked_in_every_mode() -> None:
    nameless = guide_history(name=None, symbol=None)

    for mode in (TradingMode.SHADOW, TradingMode.APPROVAL):
        decision = screen(nameless, mode=mode)
        reasons = tuple(reason.value for reason in decision.reasons)
        flags = tuple(flag.value for flag in decision.flags)

        assert "theme" in marked(decision.evidence, reasons, flags), mode
