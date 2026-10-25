"""Display filters of the panel: grouped numbers, ages, short addresses, colours of codes."""

from decimal import Decimal
from enum import StrEnum
from typing import get_args

import pytest

from src.api.schemas import ServiceHealth, UniverseStatus
from src.domain.models.trading import TradingMode
from src.domain.screening import CandidateStatus
from src.market.models import AuthorityStatus, DataQuality, Freshness, RiskSignal
from src.web.routes import _ago, _grouped, _short_address, _sign, _tone

NNBSP = " "


@pytest.mark.parametrize(
    ("text", "shown"),
    [
        ("0", "0"),
        ("999", "999"),
        ("1000", f"1{NNBSP}000"),
        ("-1234567.891", f"-1{NNBSP}234{NNBSP}567.891"),
        ("1000000000", f"1{NNBSP}000{NNBSP}000{NNBSP}000"),
        # Not a plain number: shown as is, never mangled.
        ("—", "—"),
        ("12%", "12%"),
        ("1.2.3", "1.2.3"),
    ],
)
def test_grouping_splits_thousands_and_leaves_anything_else_alone(text: str, shown: str) -> None:
    assert _grouped(text) == shown


@pytest.mark.parametrize(
    ("seconds", "shown"),
    [
        (None, "—"),
        (0, "0 с"),
        (59, "59 с"),
        (60, "1 мин 0 с"),
        (3599, "59 мин 59 с"),
        (3600, "1 ч 0 мин"),
        (90061, "25 ч 1 мин"),
        # A clock a little ahead of the database is "just now", not a negative age.
        (-3, "0 с"),
    ],
)
def test_ages_read_at_a_glance(seconds: int | None, shown: str) -> None:
    assert _ago(seconds) == shown


def test_addresses_shorten_to_four_and_four_and_short_values_stay_whole() -> None:
    assert _short_address("So11111111111111111111111111111111111111112") == "So11…1112"
    assert _short_address("short") == "short"
    assert _short_address(None) == "—"
    assert _short_address("") == "—"


@pytest.mark.parametrize(
    ("value", "css"),
    [(Decimal("5"), "pos"), (Decimal("-0.1"), "neg"), (Decimal("0"), ""), (None, ""), (3, "pos")],
)
def test_money_sign_colours_gains_and_losses_only(value: Decimal | int | None, css: str) -> None:
    assert _sign(value) == css


@pytest.mark.parametrize(
    "enum",
    [TradingMode, CandidateStatus, Freshness, DataQuality, RiskSignal, AuthorityStatus],
)
def test_every_status_code_the_panel_shows_has_a_colour(enum: type[StrEnum]) -> None:
    uncoloured = [member.value for member in enum if _tone(member) == "plain"]

    assert uncoloured == []


def test_service_and_universe_states_have_a_colour() -> None:
    service = get_args(ServiceHealth.model_fields["status"].annotation)
    universe = get_args(UniverseStatus.model_fields["state"].annotation)

    assert [code for code in (*service, *universe) if _tone(code) == "plain"] == []


def test_a_dangerous_token_signal_is_red_and_a_safe_one_green() -> None:
    assert _tone(AuthorityStatus.PRESENT) == "bad"
    assert _tone(RiskSignal.UNSAFE) == "bad"
    assert _tone(DataQuality.INVALID) == "bad"
    assert _tone(AuthorityStatus.REVOKED) == "good"
    assert _tone(RiskSignal.SAFE) == "good"
    # Unknown is a warning, never louder than a confirmed danger.
    assert _tone(RiskSignal.UNKNOWN) == "warn"
