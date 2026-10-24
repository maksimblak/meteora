"""Pure reading of a gmgn.ai token page: its own network responses and its document state.

Where the metrics are (checked against the page on 2026-10-23, WIF and Kimchi):

* ``GET /api/v1/token_stat/{chain}/{mint}`` - fractions as decimal strings:
  ``top_rat_trader_percentage`` (the page's "insiders"), ``top_bundler_trader_percentage``
  ("Bundler"), ``top_entrapment_trader_percentage`` ("phishing wallets"),
  ``top_10_holder_rate`` ("Top 10"), ``creator_hold_rate`` and ``dev_team_hold_rate``
  ("DEV").
* ``POST /api/v1/mutil_window_token_info`` - per address: ``total_fee`` in SOL ("Total
  Fees" = priority + tip + trade fees) and ``dev.top_10_holder_rate``.

Anything else - another endpoint, another token, a non-zero ``code``, a value that is not
a fraction - is ignored, so a changed format shows up as a missing metric, never as zero.
"""

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

SHARE_SIGNALS = (
    "top10_holder_share",
    "insiders_share",
    "bundling_share",
    "phishing_share",
    "developer_share",
)
AMOUNT_SIGNALS = ("total_fees_sol",)
SIGNAL_NAMES = (*SHARE_SIGNALS, *AMOUNT_SIGNALS)

TOKEN_STAT = "token_stat"  # noqa: S105 - an endpoint name
TOKEN_INFO = "mutil_window_token_info"  # noqa: S105 - an endpoint name
# Endpoints whose bodies the browser keeps; every other response is not read at all.
INTERESTING_ENDPOINTS = (f"/api/v1/{TOKEN_STAT}/", f"/api/v1/{TOKEN_INFO}")

_STAT_FIELDS = {
    "insiders_share": ("top_rat_trader_percentage",),
    "bundling_share": ("top_bundler_trader_percentage",),
    "phishing_share": ("top_entrapment_trader_percentage",),
    "top10_holder_share": ("top_10_holder_rate",),
    # The larger of the two holdings: the developer share must never be under-reported.
    "developer_share": ("creator_hold_rate", "dev_team_hold_rate"),
}


@dataclass(frozen=True)
class CapturedResponse:
    """One network response of the page: URL path without the query, status, JSON body."""

    method: str
    path: str
    status: int
    body: Any


@dataclass(frozen=True)
class Extraction:
    signals: dict[str, Decimal | None]
    sources: tuple[str, ...] = field(default=())

    @property
    def missing(self) -> tuple[str, ...]:
        return tuple(name for name in SIGNAL_NAMES if self.signals.get(name) is None)

    @property
    def complete(self) -> bool:
        return not self.missing

    @property
    def empty(self) -> bool:
        return len(self.missing) == len(SIGNAL_NAMES)


class PageState(StrEnum):
    READY = "READY"
    CHALLENGE = "CHALLENGE"
    CAPTCHA = "CAPTCHA"
    BLOCKED = "BLOCKED"


def is_interesting(path: str) -> bool:
    return any(marker in path for marker in INTERESTING_ENDPOINTS)


def parse_body(text: str) -> Any:
    """JSON with exact decimals (``parse_float=Decimal``); ``None`` for anything else."""

    try:
        return json.loads(text, parse_float=Decimal)
    except (ValueError, RecursionError):
        return None


def extract_signals(responses: Iterable[CapturedResponse], *, chain: str, mint: str) -> Extraction:
    """The six metrics of ``mint``; a later answer of a polled endpoint wins."""

    signals: dict[str, Decimal | None] = dict.fromkeys(SIGNAL_NAMES)
    fallback_top10: Decimal | None = None
    sources: set[str] = set()
    stat_path = f"/api/v1/{TOKEN_STAT}/{chain}/{mint}"
    for response in responses:
        if response.status != 200:
            continue
        data = _success_data(response.body)
        if data is None:
            continue
        if response.path.rstrip("/").endswith(stat_path) and isinstance(data, Mapping):
            found = _stat_signals(data)
            if found:
                sources.add(TOKEN_STAT)
                signals.update(found)
        elif response.path.rstrip("/").endswith(f"/api/v1/{TOKEN_INFO}"):
            item = _token_item(data, mint)
            if item is None:
                continue
            fees = amount(item.get("total_fee"))
            dev = item.get("dev")
            top10 = fraction(dev.get("top_10_holder_rate")) if isinstance(dev, Mapping) else None
            if fees is not None or top10 is not None:
                sources.add(TOKEN_INFO)
            if fees is not None:
                signals["total_fees_sol"] = fees
            if top10 is not None:
                fallback_top10 = top10
    if signals["top10_holder_share"] is None:
        signals["top10_holder_share"] = fallback_top10
    return Extraction(signals=signals, sources=tuple(sorted(sources)))


def classify_page(title: str, text: str, frames: Sequence[str]) -> PageState:
    """What the visitor sees: the token page, a challenge in progress, a captcha or a block.

    A captcha (Cloudflare Turnstile asking to verify a human, GeeTest) is never solved and
    a block is final; only a challenge that clears by itself is waited for.
    """

    lowered_title = title.lower()
    lowered_text = text.lower()
    if (
        "attention required" in lowered_title
        or "you have been blocked" in lowered_text
        or "access denied" in lowered_title
    ):
        return PageState.BLOCKED
    if any("geetest.com" in frame for frame in frames) or "verify you are human" in lowered_text:
        return PageState.CAPTCHA
    if (
        "just a moment" in lowered_title
        or "checking your browser" in lowered_text
        or any("challenges.cloudflare.com" in frame for frame in frames)
    ):
        return PageState.CHALLENGE
    if "security check" in lowered_text and "captcha" in lowered_text:
        return PageState.CAPTCHA
    return PageState.READY


def _success_data(body: Any) -> Any:
    if not isinstance(body, Mapping) or body.get("code") != 0:
        return None
    return body.get("data")


def _stat_signals(data: Mapping[str, Any]) -> dict[str, Decimal]:
    found: dict[str, Decimal] = {}
    for name, fields in _STAT_FIELDS.items():
        values = [
            value for value in (fraction(data.get(key)) for key in fields) if value is not None
        ]
        if values:
            found[name] = max(values)
    return found


def _token_item(data: Any, mint: str) -> Mapping[str, Any] | None:
    items = data if isinstance(data, list) else [data]
    for item in items:
        if isinstance(item, Mapping) and item.get("address") == mint:
            return item
    return None


def exact_number(value: object) -> Decimal | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        number = value
    elif isinstance(value, int):
        number = Decimal(value)
    elif isinstance(value, str):
        try:
            number = Decimal(value.strip())
        except InvalidOperation:
            return None
    else:
        return None
    return number if number.is_finite() else None


def fraction(value: object) -> Decimal | None:
    number = exact_number(value)
    if number is None or not Decimal(0) <= number <= Decimal(1):
        return None
    return number


def amount(value: object) -> Decimal | None:
    number = exact_number(value)
    if number is None or number < 0:
        return None
    return number
