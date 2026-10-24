"""The shared ``gmgn-signals.v1`` corpus: what this service answers, byte for byte.

The worker's tests (``tests/gmgn/test_gmgn_provider.py``) parse the same files, so a change
of the answer breaks one side or the other, never silently both.
"""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from gmgn_service.bridge import BridgeFetcher, MinuteLimiter
from gmgn_service.config import ServiceSettings
from gmgn_service.contract import SignalsV2
from gmgn_service.extract import CapturedResponse, Extraction, extract_signals, parse_body
from gmgn_service.service import PageUnavailable, SignalService
from gmgn_service.webapi import WebAnswer, WebRequest

# One corpus for both sides: ``tests/gmgn/contract`` of the repository, copied to
# ``/app/contract`` in the service's check image.
_HERE = Path(__file__).resolve().parent
_CANDIDATES = [_HERE.parent / "contract"]
_CANDIDATES += [base / "tests" / "gmgn" / "contract" for base in _HERE.parents]
CONTRACT = next(path for path in _CANDIDATES if path.is_dir())
FIXTURES = Path(__file__).parent / "fixtures"
KIMCHI = "CSocnzcRpgSCr1naKcNTieWZUxb6rABe45j844m7pump"
RECORDED_AT = datetime(2026, 10, 23, 17, 33, 16, tzinfo=UTC)


class RecordedPage:
    """Replays the recorded Kimchi page, or the recorded Cloudflare block."""

    def __init__(self, blocked: bool) -> None:
        self.blocked = blocked

    @property
    def ready(self) -> bool:
        return True

    async def fetch(self, chain: str, mint: str) -> Extraction:
        if self.blocked:
            raise PageUnavailable("ACCESS_BLOCKED")
        page = json.loads((FIXTURES / "kimchi_token_page.json").read_text(encoding="utf-8"))
        responses = [
            CapturedResponse(
                item["method"], item["path"], item["status"], parse_body(json.dumps(item["body"]))
            )
            for item in page["responses"]
        ]
        return extract_signals(responses, chain=chain, mint=mint)


def answer(blocked: bool) -> dict[str, object]:
    service = SignalService(
        RecordedPage(blocked), settings=ServiceSettings(), now=lambda: RECORDED_AT
    )
    result = asyncio.run(service.signals("sol", KIMCHI))
    return json.loads(result.model_dump_json())


def test_an_available_answer_matches_the_shared_corpus() -> None:
    expected = json.loads((CONTRACT / "signals_v1_available.json").read_text(encoding="utf-8"))

    assert answer(blocked=False) == expected


def test_an_unavailable_answer_matches_the_shared_corpus() -> None:
    expected = json.loads((CONTRACT / "signals_v1_unavailable.json").read_text(encoding="utf-8"))

    assert answer(blocked=True) == expected


BRIDGE = json.loads((FIXTURES / "bridge_grok.json").read_text(encoding="utf-8"))
BRIDGE_AT = datetime.fromisoformat(BRIDGE["recorded_at"])


class RecordedTab:
    """Replays the bridge recording of 2026-10-24, or a Cloudflare challenge on the page."""

    def __init__(self, challenged: bool) -> None:
        self.challenged = challenged

    @property
    def ready(self) -> bool:
        return True

    async def prepare(self) -> None:
        if self.challenged:
            raise PageUnavailable("CHALLENGE", pause=True)

    def mark_blocked(self) -> None:
        return None

    async def refresh_login(self) -> str:
        return "LOGGED_OUT"

    async def request(self, request: WebRequest) -> WebAnswer:
        body = BRIDGE["answers"][request.name]
        return WebAnswer(200, "application/json; charset=utf-8", json.dumps(body))


def bridge_answer(challenged: bool) -> dict[str, object]:
    fetcher = BridgeFetcher(RecordedTab(challenged), limiter=MinuteLimiter(60))
    service = SignalService(
        fetcher, settings=ServiceSettings(max_pages=1), schema=SignalsV2, now=lambda: BRIDGE_AT
    )
    result = asyncio.run(service.signals("sol", BRIDGE["mint"]))
    return json.loads(result.model_dump_json())


def test_a_v2_bridge_answer_matches_the_shared_corpus() -> None:
    expected = json.loads((CONTRACT / "signals_v2_available.json").read_text(encoding="utf-8"))

    assert bridge_answer(challenged=False) == expected


def test_an_unavailable_v2_answer_matches_the_shared_corpus() -> None:
    expected = json.loads((CONTRACT / "signals_v2_unavailable.json").read_text(encoding="utf-8"))

    assert bridge_answer(challenged=True) == expected
