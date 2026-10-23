"""Metrics from the token page's own network responses, on recorded and cleaned pages."""

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from gmgn_service.extract import (
    CapturedResponse,
    PageState,
    classify_page,
    extract_signals,
    parse_body,
)

FIXTURES = Path(__file__).parent / "fixtures"
WIF = "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm"
KIMCHI = "CSocnzcRpgSCr1naKcNTieWZUxb6rABe45j844m7pump"


def recorded(name: str) -> list[CapturedResponse]:
    page = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return [
        CapturedResponse(
            method=item["method"],
            path=item["path"],
            status=item["status"],
            # Re-parsed from text as the browser delivers it: numbers stay exact.
            body=parse_body(json.dumps(item["body"])),
        )
        for item in page["responses"]
    ]


def changed(
    responses: list[CapturedResponse], path_part: str, **data: Any
) -> list[CapturedResponse]:
    result = []
    for item in responses:
        if path_part in item.path and isinstance(item.body, dict):
            body = json.loads(json.dumps(item.body, default=str))
            if isinstance(body["data"], list):
                body["data"][0].update(data)
            else:
                body["data"].update(data)
            item = CapturedResponse(
                item.method, item.path, item.status, parse_body(json.dumps(body))
            )
        result.append(item)
    return result


def test_the_recorded_wif_page_gives_what_the_page_shows() -> None:
    """The page showed: Top 10 32.17%, DEV 0%, insiders 0%, phishing 73.8%, bundler 0.3%,
    Total Fees 1,222.42 (2026-10-23)."""

    extraction = extract_signals(recorded("wif_token_page.json"), chain="sol", mint=WIF)

    assert extraction.signals == {
        "top10_holder_share": Decimal("0.3217"),
        "insiders_share": Decimal("0.0001"),
        "bundling_share": Decimal("0.0028"),
        "phishing_share": Decimal("0.738"),
        "developer_share": Decimal("0"),
        "total_fees_sol": Decimal("1222.4250124741914"),
    }
    assert extraction.missing == ()
    assert extraction.sources == ("mutil_window_token_info", "token_stat")


def test_the_recorded_kimchi_page_gives_what_the_page_shows() -> None:
    """The page showed: Top 10 79.04%, DEV 0%, insiders 75.3%, phishing 80.8%,
    bundler 11.4%, Total Fees 1,922.30 (2026-10-23)."""

    extraction = extract_signals(recorded("kimchi_token_page.json"), chain="sol", mint=KIMCHI)

    assert extraction.signals["insiders_share"] == Decimal("0.7533")
    assert extraction.signals["bundling_share"] == Decimal("0.1139")
    assert extraction.signals["phishing_share"] == Decimal("0.8081")
    assert extraction.signals["top10_holder_share"] == Decimal("0.7904")
    assert extraction.signals["developer_share"] == Decimal("0")
    assert extraction.signals["total_fees_sol"] == Decimal("1922.2996902657378")


def test_responses_about_another_token_are_ignored() -> None:
    extraction = extract_signals(recorded("wif_token_page.json"), chain="sol", mint=KIMCHI)

    assert set(extraction.missing) == set(extraction.signals)
    assert extraction.sources == ()


def test_the_developer_share_is_the_larger_of_creator_and_dev_team_holdings() -> None:
    responses = changed(
        recorded("wif_token_page.json"),
        "token_stat",
        creator_hold_rate="0.012",
        dev_team_hold_rate="0.034",
    )

    extraction = extract_signals(responses, chain="sol", mint=WIF)

    assert extraction.signals["developer_share"] == Decimal("0.034")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("top_rat_trader_percentage", "75.33"),  # a percent instead of a fraction
        ("top_rat_trader_percentage", "n/a"),
        ("top_rat_trader_percentage", None),
        ("top_rat_trader_percentage", {"value": "0.1"}),
        ("top_rat_trader_percentage", "-0.1"),
    ],
)
def test_a_changed_metric_format_drops_only_that_metric(field: str, value: object) -> None:
    responses = changed(recorded("kimchi_token_page.json"), "token_stat", **{field: value})

    extraction = extract_signals(responses, chain="sol", mint=KIMCHI)

    assert extraction.signals["insiders_share"] is None
    assert extraction.missing == ("insiders_share",)
    assert extraction.signals["bundling_share"] == Decimal("0.1139")


def test_a_renamed_or_failed_endpoint_degrades_to_missing_metrics() -> None:
    responses = [
        item
        if "token_stat" not in item.path
        else CapturedResponse(item.method, item.path, item.status, {"code": 40001, "data": None})
        for item in recorded("kimchi_token_page.json")
    ]

    extraction = extract_signals(responses, chain="sol", mint=KIMCHI)

    assert extraction.signals["total_fees_sol"] == Decimal("1922.2996902657378")
    # The token info carries its own top-10 figure; the holder-structure shares are gone.
    assert extraction.signals["top10_holder_share"] == Decimal("0.7904")
    assert set(extraction.missing) == {
        "insiders_share",
        "bundling_share",
        "phishing_share",
        "developer_share",
    }


def test_the_latest_answer_of_a_polled_endpoint_wins() -> None:
    first = recorded("kimchi_token_page.json")
    later = changed(first, "token_stat", top_bundler_trader_percentage="0.2")
    responses = [*first, *[item for item in later if "token_stat" in item.path]]

    extraction = extract_signals(responses, chain="sol", mint=KIMCHI)

    assert extraction.signals["bundling_share"] == Decimal("0.2")


def test_non_json_and_binary_floats_are_never_trusted() -> None:
    assert parse_body("<html>Just a moment...</html>") is None
    body = parse_body('{"code": 0, "data": {"top_10_holder_rate": 0.3217}}')
    assert isinstance(body, dict)
    assert body["data"]["top_10_holder_rate"] == Decimal("0.3217")


def test_the_recorded_cloudflare_block_of_headless_chromium_is_recognized() -> None:
    """What gmgn.ai answered the default Playwright Chromium on 2026-10-23: HTTP 403."""

    blocked = json.loads((FIXTURES / "blocked_page.json").read_text(encoding="utf-8"))

    state = classify_page(blocked["title"], blocked["text"], blocked["frames"])

    assert state is PageState.BLOCKED


@pytest.mark.parametrize(
    ("title", "text", "frames", "state"),
    [
        (
            "Just a moment...",
            "Checking your browser before accessing gmgn.ai",
            [],
            PageState.CHALLENGE,
        ),
        (
            "Just a moment...",
            "Verify you are human by completing the action below.",
            ["https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b/turnstile/if/ov2"],
            PageState.CAPTCHA,
        ),
        (
            "GMGN.AI",
            "Please complete the security check",
            ["https://static.geetest.com/v4/x"],
            PageState.CAPTCHA,
        ),
        (
            "$WIF $197.03M | GMGN.AI",
            "Топ 10 32.17%",
            ["https://gmgn.ai/sol/token/x"],
            PageState.READY,
        ),
    ],
    ids=["interstitial", "turnstile", "geetest", "token-page"],
)
def test_challenge_and_captcha_pages_are_told_apart_from_the_token_page(
    title: str, text: str, frames: list[str], state: PageState
) -> None:
    assert classify_page(title, text, frames) is state
