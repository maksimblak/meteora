"""The Python adapter against the real executor from compose, which has no wallet.

Opt-in: it runs only with ``EXECUTOR_INTEGRATION_URL`` (and the executor's internal
token in ``EXECUTOR_INTEGRATION_TOKEN``), e.g. an executor started from compose with
``TRADING_MODE=APPROVAL`` and that token but without RPC, wallet or state file. Such
an executor has no execution engine: it is never ready and nothing can be sent.
"""

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from src.domain.models.trading import TradingMode
from src.execution.contracts import DlmmCommandV1
from src.execution.http_gateway import HttpExecutionGateway
from src.execution.service import GatewayRejected

URL = os.environ.get("EXECUTOR_INTEGRATION_URL")
pytestmark = pytest.mark.skipif(not URL, reason="EXECUTOR_INTEGRATION_URL is not configured")
CORPUS = Path(__file__).parents[2] / "contracts" / "execution" / "v1-corpus.json"


def command() -> DlmmCommandV1:
    payload = json.loads(CORPUS.read_text(encoding="utf-8"))["valid_commands"][0]
    return DlmmCommandV1.model_validate(payload).model_copy(
        update={"expires_at": datetime.now(UTC) + timedelta(minutes=5)}
    )


async def with_gateway[T](work: Callable[[HttpExecutionGateway], Awaitable[T]]) -> T:
    async with httpx.AsyncClient(base_url=str(URL), timeout=10) as client:
        gateway = HttpExecutionGateway(
            client, internal_token=os.environ.get("EXECUTOR_INTEGRATION_TOKEN", "unset")
        )
        return await work(gateway)


def test_an_executor_without_a_wallet_is_never_ready_for_a_money_mode() -> None:
    async def readiness(gateway: HttpExecutionGateway) -> list[bool]:
        return [await gateway.ready(mode) for mode in (TradingMode.APPROVAL, TradingMode.LIVE)]

    assert asyncio.run(with_gateway(readiness)) == [False, False]


def test_an_executor_without_a_wallet_refuses_and_records_nothing() -> None:
    value = command()

    async def exercise(gateway: HttpExecutionGateway) -> tuple[str, str, object]:
        with pytest.raises(GatewayRejected) as simulated:
            await gateway.simulate(value)
        with pytest.raises(GatewayRejected) as submitted:
            await gateway.submit(value, value.idempotency_key, None)
        return simulated.value.code, submitted.value.code, await gateway.lookup(value.command_id)

    simulation_code, submit_code, recorded = asyncio.run(with_gateway(exercise))

    assert simulation_code == submit_code == "EXECUTION_UNAVAILABLE"
    assert recorded is None

