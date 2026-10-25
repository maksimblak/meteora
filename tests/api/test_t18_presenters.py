"""T18 (G24) in the panel: each new evidence row is marked by its own code, the artificial
volume rule marks the total-fees and turnover rows, not the retired fee organicity."""

from decimal import Decimal

from src.api.presenters import evidence_views
from src.domain.models.trading import TradingMode
from src.domain.screening import (
    CandidateDecision,
    DecisionContext,
    PoolScreener,
)
from src.domain.screening.models import EvidenceItem
from tests.domain.guide_fixtures import CONFIG, RULESET, guide_features, guide_history, guide_token
from tests.domain.test_t18_selection import GUIDE

SAVASH = (
    EvidenceItem(name="total_fees_sol", value="45", threshold="30"),
    EvidenceItem(name="required_total_fees_sol", value="120", threshold="30 x market_cap / 500000"),
    EvidenceItem(name="pool_hourly_turnover", value="11.26", threshold="10"),
    EvidenceItem(name="fee_efficiency", value="0.0918", threshold=None),
    EvidenceItem(name="gmgn_wash_trading", value="SAFE"),
)


def marked(items: tuple[EvidenceItem, ...], *reasons: str) -> set[str]:
    return {row.name for row in evidence_views(items, reasons) if row.triggered}


def test_savash_marks_total_fees_the_required_fees_and_the_turnover_not_organicity() -> None:
    assert marked(SAVASH, "ARTIFICIAL_VOLUME_SUSPECTED") == {
        "total_fees_sol",
        "required_total_fees_sol",
        "pool_hourly_turnover",
    }


def test_a_gmgn_wash_trading_verdict_marks_only_its_own_row() -> None:
    washed = (
        *SAVASH[:2],
        EvidenceItem(name="pool_hourly_turnover", value="6", threshold="10"),
        EvidenceItem(name="gmgn_wash_trading", value="UNSAFE"),
    )

    assert marked(washed, "ARTIFICIAL_VOLUME_SUSPECTED") == {"gmgn_wash_trading"}


def test_each_new_row_is_marked_by_its_own_code() -> None:
    rows = (
        EvidenceItem(name="hourly_volume_per_minute", value="5000", threshold="14000"),
        EvidenceItem(name="theme", value="trump"),
        EvidenceItem(name="token_volume_rank", value="51", threshold="50"),
        EvidenceItem(name="fee_tvl", value="0.0015", threshold="0.002"),
        EvidenceItem(name="developer_share", value="0.035", threshold="0"),
        EvidenceItem(name="short_ratio", value="1", threshold="3"),
    )
    codes = {
        "hourly_volume_per_minute": "HOURLY_VOLUME_PER_MINUTE_BELOW_THRESHOLD",
        "theme": "THEME_EXCLUDED",
        "token_volume_rank": "CANDIDATE_RANK_BELOW_LIMIT",
        "fee_tvl": "FEE_TVL_BELOW_THRESHOLD",
        "developer_share": "DEVELOPER_HOLDS_TOKENS",
    }

    for name, code in codes.items():
        assert marked(rows, code) == {name}, code
    # guide-v1: a spike below its marks is no reason, so the spike rows stay unmarked.
    assert marked(rows, "FEE_TVL_BELOW_THRESHOLD") == {"fee_tvl"}


def test_the_wait_after_an_exit_marks_the_volume_and_the_price_it_compared() -> None:
    rows = (
        EvidenceItem(name="reentry_last_exit_at", value="2026-10-18T11:50:00+00:00"),
        EvidenceItem(name="reentry_volume_per_minute", value="18000", threshold=">20000 (JUPITER)"),
        EvidenceItem(name="reentry_price", value="1", threshold=">1"),
        EvidenceItem(name="volume_per_minute", value="18000", threshold="14000"),
    )

    assert marked(rows, "NO_NEW_VOLUME_OR_HIGH") == {"reentry_volume_per_minute", "reentry_price"}


def test_the_legacy_spike_reason_still_marks_its_rows_below_their_minimum() -> None:
    rows = (
        EvidenceItem(name="short_ratio", value="1.5", threshold="4"),
        EvidenceItem(name="fee_tvl", value="0.01", threshold="0.002"),
    )

    assert marked(rows, "VOLUME_SIGNAL_BELOW_THRESHOLD") == {"short_ratio"}


def screen(**current: str) -> CandidateDecision:
    history = guide_history(**current)
    return PoolScreener().evaluate(
        history[-1],
        guide_token(total_fees_sol="45"),
        guide_features(history),
        GUIDE,
        context=DecisionContext(
            mode=TradingMode.SHADOW,
            ruleset_version=RULESET,
            config_version=CONFIG,
            correlation_id="cycle-1",
        ),
        estimated_price_impact=Decimal("0.001"),
    )


def test_a_real_savash_decision_is_marked_on_the_rows_that_decided_it() -> None:
    decision = screen(short="81000", hour="980000", hour_fees="9800", tvl="87000")
    reasons = [reason.value for reason in decision.reasons]

    assert "ARTIFICIAL_VOLUME_SUSPECTED" in reasons
    assert {"total_fees_sol", "required_total_fees_sol", "pool_hourly_turnover"} <= marked(
        decision.evidence, *reasons
    )
    assert "fee_efficiency" not in marked(decision.evidence, *reasons)
