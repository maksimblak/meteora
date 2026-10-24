"""F3 (A4): a server error string that is, or contains, the API key never becomes a reason."""

import asyncio
import logging
from datetime import UTC, datetime

import httpx
import pytest
from pydantic import SecretStr

from src.core.clock import FrozenClock
from src.domain.models.primitives import PublicKey
from src.gmgn.openapi import OPENAPI_URL, GmgnOpenApiProvider

NOW = datetime(2026, 10, 24, 12, tzinfo=UTC)
WIF = PublicKey("EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm")
# Upper case on purpose: it looks exactly like an error code (``[A-Z][A-Z0-9_]*``).
KEY = "TESTGMGNKEY0123456789ABCDEFXYZ"  # a stand-in value, not a real key


class Sleeper:
    async def __call__(self, seconds: float) -> None:
        return None


def read(response: httpx.Response) -> tuple[str | None, str]:
    async def scenario() -> tuple[str | None, str]:
        transport = httpx.MockTransport(lambda request: response)
        async with httpx.AsyncClient(base_url=OPENAPI_URL, transport=transport) as client:
            provider = GmgnOpenApiProvider(
                client, api_key=SecretStr(KEY), clock=FrozenClock(NOW), sleep=Sleeper()
            )
            reading = await provider.get_token_signals(WIF)
            return reading.detail, str(provider.health)

    return asyncio.run(scenario())


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (httpx.Response(401, json={"code": 401, "error": KEY}), "HTTP_401"),
        (httpx.Response(403, json={"code": 403, "error": f"AUTH_{KEY}"}), "HTTP_403"),
        (httpx.Response(429, json={"code": 429, "error": KEY}), "RATE_LIMITED"),
        (httpx.Response(200, json={"code": 40001, "error": KEY}), "API_ERROR"),
        (httpx.Response(401, json={"code": 401, "error": KEY[:20]}), "HTTP_401"),
    ],
    ids=["401-equal", "403-contains", "429-equal", "200-error-equal", "401-key-prefix"],
)
def test_an_echoed_upper_case_key_is_not_a_reason_health_or_log_line(
    response: httpx.Response,
    reason: str,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    caplog.set_level(logging.DEBUG)

    detail, health = read(response)

    printed = capsys.readouterr()
    assert detail == reason
    for text in (str(detail), health, caplog.text, printed.out, printed.err):
        assert KEY not in text
        assert KEY[:20] not in text
