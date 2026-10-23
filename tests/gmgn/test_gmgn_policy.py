"""GMGN rules in the loaded guide-v1 bundle and the GMGN_REQUIRED_FOR_LIVE switch (G18)."""

from decimal import Decimal
from pathlib import Path

import pytest

from src.worker.policy import guide_policy, load_pipeline_policy


def test_the_loaded_guide_bundle_carries_the_gmgn_thresholds_required_for_live() -> None:
    guide = load_pipeline_policy({}).screening.guide

    assert guide is not None and guide.gmgn is not None
    assert guide.gmgn.maximum_insiders_share == Decimal("0.10")
    assert guide.gmgn.maximum_bundling_share == Decimal("0.70")
    assert guide.gmgn.maximum_phishing_share == Decimal("0.30")
    assert guide.gmgn.maximum_top10_holder_share == Decimal("0.30")
    assert guide.gmgn.required_for_live is True


@pytest.mark.parametrize(
    ("raw", "required"),
    [("", True), ("true", True), ("1", True), ("false", False), ("0", False), ("no", False)],
)
def test_only_an_explicit_switch_releases_live_from_gmgn(raw: str, required: bool) -> None:
    guide = load_pipeline_policy({"GMGN_REQUIRED_FOR_LIVE": raw}).screening.guide

    assert guide is not None and guide.gmgn is not None
    assert guide.gmgn.required_for_live is required


def test_an_unreadable_switch_stops_the_start() -> None:
    with pytest.raises(ValueError, match="GMGN_REQUIRED_FOR_LIVE"):
        load_pipeline_policy({"GMGN_REQUIRED_FOR_LIVE": "maybe"})


def test_gmgn_thresholds_are_tuned_with_a_config_version_like_the_others(tmp_path: Path) -> None:
    overrides = tmp_path / "gmgn.yaml"
    overrides.write_text(
        'config_version: "owner-gmgn"\n'
        "thresholds:\n"
        '  maximum_insiders_share: "0.05"\n'
        '  maximum_bundling_share: "0.50"\n'
        '  maximum_phishing_share: "0.20"\n'
        '  maximum_gmgn_top10_holder_share: "0.25"\n',
        encoding="utf-8",
    )

    tuned = load_pipeline_policy({"RULESET_OVERRIDES_FILE": str(overrides)})

    assert tuned.config_version == "owner-gmgn"
    guide = tuned.screening.guide
    assert guide is not None and guide.gmgn is not None
    assert (
        guide.gmgn.maximum_insiders_share,
        guide.gmgn.maximum_bundling_share,
        guide.gmgn.maximum_phishing_share,
        guide.gmgn.maximum_top10_holder_share,
    ) == (Decimal("0.05"), Decimal("0.50"), Decimal("0.20"), Decimal("0.25"))


def test_the_previous_ruleset_and_a_directly_built_bundle_have_no_gmgn_rules() -> None:
    legacy = load_pipeline_policy({"RULESET_VERSION": "shadow-hypothesis-v1"})
    direct = guide_policy()

    assert legacy.screening.guide is None
    assert direct.screening.guide is not None and direct.screening.guide.gmgn is None
    released = guide_policy(gmgn_required_for_live=False).screening.guide
    assert released is not None and released.gmgn is not None
    assert released.gmgn.required_for_live is False
