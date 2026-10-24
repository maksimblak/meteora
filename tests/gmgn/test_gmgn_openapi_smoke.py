"""The manual OpenAPI smoke command (G20), exercised offline on the recorded WIF answers."""

import asyncio

import httpx
import pytest
from pydantic import SecretStr

from src.core.clock import FrozenClock
from src.gmgn.smoke import format_result, main, run_smoke
from tests.gmgn.test_gmgn_openapi import NOW, WIF, Api, error

KEY = "test-gmgn-key-smoke-000111222"  # a stand-in value, not a real key


def smoke(api: Api) -> str:
    async def scenario() -> str:
        async with httpx.AsyncClient(
            base_url="https://openapi.gmgn.ai", transport=httpx.MockTransport(api)
        ) as client:
            result = await run_smoke(
                client, api_key=SecretStr(KEY), mint=WIF, clock=FrozenClock(NOW)
            )
        return format_result(result)

    return asyncio.run(scenario())


def test_the_smoke_pass_prints_the_six_metrics_and_never_the_key() -> None:
    api = Api(FrozenClock(NOW))

    text = smoke(api)

    assert [request.url.path for request in api.requests] == [
        "/v1/token/info",
        "/v1/token/security",
    ]
    assert "FRESH" in text
    for line in (
        "insiders_share: 0.0001",
        "bundling_share: 0.0027",
        "phishing_share: 0.7367",
        "top10_holder_share: 0.323",
        "developer_share: 0",
        "total_fees_sol: 1223.6575276103256",
    ):
        assert line in text
    assert KEY not in text


def test_a_refused_key_is_reported_with_its_reason() -> None:
    api = Api(FrozenClock(NOW), info=(error(401, {"code": 401, "error": "AUTH_KEY_INVALID"}),))

    text = smoke(api)

    assert "UNAVAILABLE:AUTH_KEY_INVALID" in text
    assert KEY not in text


def test_without_a_key_the_command_stops_before_any_request(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(["--mint", WIF.root], environ={})

    assert code == 2
    assert "GMGN_API_KEY" in capsys.readouterr().err
