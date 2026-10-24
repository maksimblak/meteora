"""T18: the G24 selection numbers are guide-v1 settings with spec 6.8 defaults."""

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from src.domain.screening import DEFAULT_THEME_WORDS
from src.worker.policy import hypothesis_policy, load_pipeline_policy


def test_the_guide_defaults_are_the_spec_6_8_starting_values() -> None:
    policy = load_pipeline_policy({})
    guide = policy.screening.guide
    assert guide is not None and guide.gmgn is not None

    assert guide.minimum_hourly_volume_per_minute == Decimal("14000")
    assert guide.minimum_volume_per_minute == Decimal("14000")
    assert guide.total_fees_reference_market_cap == Decimal("500000")
    assert guide.minimum_total_fees_sol == Decimal("30")
    assert guide.maximum_hourly_turnover == Decimal("10")
    assert guide.revived_token_age == timedelta(days=60)
    assert guide.gmgn.maximum_sniper_count == 20
    # F5: "stream*" is a stem (Streamer, Стример), not a substring (Mainstream).
    assert {"elon", "илон", "trump", "трамп", "maga", "stream*", "стрим*"} <= set(
        guide.excluded_theme_words
    )
    assert guide.excluded_theme_words == DEFAULT_THEME_WORDS
    assert policy.deep_candidate_limit == 50
    # shadow-hypothesis-v1 keeps its own rules: no guide selection, no deep-check limit.
    assert hypothesis_policy().deep_candidate_limit is None


def test_every_selection_number_and_the_theme_list_can_be_tuned(tmp_path: Path) -> None:
    overrides = tmp_path / "rules.yaml"
    overrides.write_text(
        'config_version: "owner-g24"\n'
        "thresholds:\n"
        '  minimum_hourly_volume_per_minute: "20000"\n'
        '  target_volume_per_minute: "30000"\n'
        '  total_fees_reference_market_cap: "1000000"\n'
        '  maximum_hourly_turnover: "8"\n'
        "  maximum_sniper_count: 5\n"
        "  maximum_deep_candidates: 10\n"
        "  revived_token_age: P90D\n"
        "  excluded_theme_words: [doge, кот]\n",
        encoding="utf-8",
    )

    policy = load_pipeline_policy({"RULESET_OVERRIDES_FILE": str(overrides)})
    guide = policy.screening.guide
    assert guide is not None and guide.gmgn is not None

    assert guide.minimum_hourly_volume_per_minute == Decimal("20000")
    assert guide.target_volume_per_minute == Decimal("30000")
    assert guide.total_fees_reference_market_cap == Decimal("1000000")
    assert guide.maximum_hourly_turnover == Decimal("8")
    assert guide.gmgn.maximum_sniper_count == 5
    assert guide.revived_token_age == timedelta(days=90)
    assert guide.excluded_theme_words == ("doge", "кот")
    assert policy.deep_candidate_limit == 10
