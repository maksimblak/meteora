"""Operator-facing texts by stable code (D03, G18) and threshold presentation (G16)."""

from src.api.presenters import evidence_views, flag_notices, gmgn_metrics
from src.domain.screening.models import EvidenceItem

GUIDE_ROWS = (
    EvidenceItem(name="market_cap", value="750000", threshold="500000"),
    EvidenceItem(name="top10_holder_share", value="0.25", threshold="0.30"),
    EvidenceItem(name="volume_per_minute", value="9000", threshold="14000"),
    EvidenceItem(name="total_fees_sol", value="UNAVAILABLE", threshold="30"),
)


def test_flag_lines_are_shown_by_code_including_the_future_gmgn_flag() -> None:
    notices = flag_notices(["TOTAL_FEES_UNAVAILABLE", "GMGN_UNAVAILABLE", "SOMETHING_NEW"])

    assert [(item.code, item.message) for item in notices] == [
        ("TOTAL_FEES_UNAVAILABLE", "total fees: нет данных — проверь в GMGN"),
        ("GMGN_UNAVAILABLE", "GMGN недоступен — проверь вручную"),
        ("SOMETHING_NEW", "SOMETHING_NEW"),
    ]


def test_gmgn_metrics_appear_next_to_thresholds_only_when_gmgn_reported_them() -> None:
    gmgn = (
        EvidenceItem(name="gmgn_insiders_share", value="0.04", threshold="0.10"),
        EvidenceItem(name="gmgn_bundling_share", value="0.75", threshold="0.70"),
        EvidenceItem(name="gmgn_phishing_share", value="0.10", threshold="0.30"),
        EvidenceItem(name="gmgn_developer_share", value="0.02", threshold=None),
    )

    shown = gmgn_metrics(GUIDE_ROWS + gmgn)

    assert gmgn_metrics(GUIDE_ROWS) == []
    assert [(item.name, item.value, item.threshold) for item in shown] == [
        ("top10_holder_share", "0.25", "0.30"),
        ("total_fees_sol", "UNAVAILABLE", "30"),
        ("gmgn_insiders_share", "0.04", "0.10"),
        ("gmgn_bundling_share", "0.75", "0.70"),
        ("gmgn_phishing_share", "0.10", "0.30"),
        ("gmgn_developer_share", "0.02", None),
    ]


def test_rows_whose_threshold_produced_a_reason_are_marked_triggered() -> None:
    rows = evidence_views(
        (
            *GUIDE_ROWS,
            EvidenceItem(name="short_ratio", value="1.5", threshold="4"),
            EvidenceItem(name="long_ratio", value="3", threshold="2"),
        ),
        ("VOLUME_PER_MINUTE_BELOW_THRESHOLD", "VOLUME_SIGNAL_BELOW_THRESHOLD"),
    )

    assert {row.name: row.triggered for row in rows} == {
        "market_cap": False,
        "top10_holder_share": False,
        "volume_per_minute": True,
        "total_fees_sol": False,
        "short_ratio": True,
        "long_ratio": False,
    }
