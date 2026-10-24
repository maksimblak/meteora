"""The Python adapter of the TS executor API, driven through ``httpx.MockTransport``.

No test here opens a socket: the transport is an in-process stand-in for the
executor's ``/v1/simulations``, ``/v1/commands``, ``/v1/commands/{id}`` and
``/v1/quotes`` routes. The wire speaks execution contract V2 and quote contract V1.
"""

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest

from src.core.clock import FrozenClock
from src.domain.models.trading import TradingMode
from src.execution.contracts import (
    DlmmCommandV1,
    DlmmCommandV2,
    ExecutionPhase,
    GatewayReceiptV2,
    GatewayState,
    LivePromotionEvidence,
    OperatorApproval,
    QuoteRequestV1,
    QuoteResultV1,
    SimulationResultV2,
    bind_simulation_result_v2,
    canonical_command_digest,
)
from src.execution.http_gateway import (
    ExecutorContractError,
    HttpExecutionGateway,
    HttpQuoteClient,
    QuoteUnavailable,
)
from src.execution.service import (
    ExecutionOutcomeUnknown,
    ExecutionService,
    GatewayRejected,
    InMemoryExecutionStore,
    StaticPlanResolver,
)

CONTRACTS = Path(__file__).parents[2] / "contracts" / "execution"
CORPUS = json.loads((CONTRACTS / "v2-corpus.json").read_text(encoding="utf-8"))
RETIRED = json.loads((CONTRACTS / "v1-corpus.json").read_text(encoding="utf-8"))
QUOTES = json.loads((CONTRACTS / "quote-v1-corpus.json").read_text(encoding="utf-8"))
EXECUTOR_POSITION = "SysvarRent111111111111111111111111111111111"
INTERNAL_CREDENTIAL = "internal-test-value"
MODE_CREDENTIAL = "mode-test-value"
NOW = datetime(2026, 10, 18, 12, tzinfo=UTC)
PLAN_ID = UUID("30000000-0000-4000-8000-000000000009")

type Handler = Callable[[httpx.Request], httpx.Response]


def gateway(handler: Handler) -> tuple[HttpExecutionGateway, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(record), base_url="http://executor.internal"
    )
    gw = HttpExecutionGateway(
        client, internal_token=INTERNAL_CREDENTIAL, mode_token=MODE_CREDENTIAL
    )
    return gw, seen


def corpus_command(index: int = 0) -> DlmmCommandV2:
    return DlmmCommandV2.model_validate(CORPUS["valid_commands"][index])


class ClearKillSwitch:
    async def kill_switch_active(self) -> bool:
        return False


def live_command() -> DlmmCommandV2:
    return corpus_command(0).model_copy(
        update={
            "mode": TradingMode.LIVE,
            "expires_at": NOW + timedelta(minutes=5),
            "idempotency_key": "entry:live-test",
        }
    )


def simulated(command: DlmmCommandV2) -> SimulationResultV2:
    return bind_simulation_result_v2(
        command_id=command.command_id,
        command_digest=canonical_command_digest(command),
        success=True,
        simulated_at=NOW,
        blockhash="blockhash",
        last_valid_block_height=10,
        compute_units=1,
        network_fee_lamports="5000",
        priority_fee_lamports="0",
        price_impact_bps=10,
        min_bin_id=command.min_bin_id,
        max_bin_id=command.max_bin_id,
        position_address=EXECUTOR_POSITION,
        active_bin_id=command.active_bin_id,
        token_x_decimals=command.token_x_decimals,
        token_y_decimals=command.token_y_decimals,
        errors=(),
    )


def receipt_for(
    command: DlmmCommandV2, simulation: SimulationResultV2, state: GatewayState
) -> dict[str, Any]:
    return GatewayReceiptV2(
        command_id=command.command_id,
        command_digest=simulation.command_digest,
        simulation_digest=simulation.simulation_digest,
        idempotency_key=command.idempotency_key,
        state=state,
        position_address=EXECUTOR_POSITION,
        signature="signature-1",
        signatures=("signature-1",),
        transaction_count=1,
        submitted_at=NOW,
        attempt_count=1,
    ).model_dump(mode="json")


def live_service(gw: HttpExecutionGateway, store: InMemoryExecutionStore) -> ExecutionService:
    command = live_command()
    return ExecutionService(
        gateway=gw,
        receipts=gw,
        kill_switch=ClearKillSwitch(),
        store=store,
        plans=StaticPlanResolver({PLAN_ID: command}),
        clock=FrozenClock(NOW),
        allowed_mode=TradingMode.LIVE,
        live_promotion=LivePromotionEvidence(
            promotion_id=UUID(int=5),
            ruleset_version=command.ruleset_version,
            approval_cycles=10,
            receipts_reconciled=True,
            risk_invariants_preserved=True,
            dedicated_rpc_ready=True,
            wallet_ready=True,
            checklist_completed=True,
            operator_confirmation_id="operator",
            promoted_at=NOW - timedelta(hours=1),
            expires_at=NOW + timedelta(hours=1),
        ),
    )


# --- contract corpus ----------------------------------------------------------------------


def test_corpus_commands_are_sent_as_strict_v2_payloads_with_the_internal_token() -> None:
    simulations: dict[str, dict[str, Any]] = {}
    for item in CORPUS["valid_simulations"]:
        simulations.setdefault(item["command_id"], item)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        known = simulations.get(body["command_id"])
        if known is None:
            return httpx.Response(409, json={"error": "simulation_failed"})
        return httpx.Response(200, json=known)

    gw, seen = gateway(handler)

    async def run() -> list[SimulationResultV2 | None]:
        results: list[SimulationResultV2 | None] = []
        for index in range(len(CORPUS["valid_commands"])):
            try:
                results.append(await gw.simulate(corpus_command(index)))
            except GatewayRejected:
                results.append(None)
        return results

    results = asyncio.run(run())

    for request, payload, digest in zip(
        seen, CORPUS["valid_commands"], CORPUS["canonical_command_digests"], strict=True
    ):
        body = json.loads(request.content)
        assert (request.method, request.url.path) == ("POST", "/v1/simulations")
        assert request.headers["x-internal-token"] == INTERNAL_CREDENTIAL
        assert set(body) == set(payload)
        assert None not in body.values()
        assert body["schema_version"] == 2
        assert canonical_command_digest(DlmmCommandV2.model_validate(body)) == digest
    assert "position_address" not in json.loads(seen[0].content)
    assert [None if r is None else r.model_dump(mode="json") for r in results] == [
        SimulationResultV2.model_validate(simulations[payload["command_id"]]).model_dump(
            mode="json"
        )
        if payload["command_id"] in simulations
        else None
        for payload in CORPUS["valid_commands"]
    ]


@pytest.mark.parametrize("case", CORPUS["invalid_simulations"], ids=lambda case: case["case"])
def test_simulation_answers_outside_the_v2_contract_are_rejected(case: dict[str, Any]) -> None:
    gw, _ = gateway(lambda request: httpx.Response(200, json=case["payload"]))

    with pytest.raises(ExecutorContractError):
        asyncio.run(gw.simulate(corpus_command(0)))


def test_a_simulation_for_another_command_is_rejected() -> None:
    other = CORPUS["valid_simulations"][2]
    gw, _ = gateway(lambda request: httpx.Response(200, json=other))

    with pytest.raises(ExecutorContractError):
        asyncio.run(gw.simulate(corpus_command(0)))


def test_corpus_gateway_receipts_round_trip_through_command_lookup() -> None:
    receipts = {item["command_id"]: item for item in CORPUS["valid_gateway_receipts"]}

    def handler(request: httpx.Request) -> httpx.Response:
        command_id = request.url.path.removeprefix("/v1/commands/")
        known = receipts.get(command_id)
        if known is None:
            return httpx.Response(404, json={"error": "command_not_found"})
        return httpx.Response(200, json=known)

    gw, seen = gateway(handler)

    async def run() -> list[GatewayReceiptV2 | None]:
        ids = [*receipts, "10000000-0000-4000-8000-00000000ffff"]
        return [await gw.lookup(UUID(command_id)) for command_id in ids]

    found = asyncio.run(run())

    assert found == [
        *(GatewayReceiptV2.model_validate(item) for item in receipts.values()),
        None,
    ]
    assert all(request.method == "GET" for request in seen)
    assert all(request.headers["x-internal-token"] == INTERNAL_CREDENTIAL for request in seen)


@pytest.mark.parametrize(
    "case", CORPUS["invalid_gateway_receipts"], ids=lambda case: case["case"]
)
def test_receipt_answers_outside_the_v2_contract_are_rejected(case: dict[str, Any]) -> None:
    gw, _ = gateway(lambda request: httpx.Response(200, json=case["payload"]))

    with pytest.raises(ExecutorContractError):
        asyncio.run(gw.lookup(UUID("10000000-0000-4000-8000-000000000201")))


# --- the retired V1 contract ----------------------------------------------------------------


def test_a_v1_command_is_refused_before_anything_is_sent() -> None:
    gw, seen = gateway(lambda request: httpx.Response(500))
    legacy = DlmmCommandV1.model_validate(RETIRED["valid_commands"][0])

    async def run() -> list[str]:
        codes = []
        for call in (gw.simulate(legacy), gw.submit(legacy, legacy.idempotency_key, None)):
            with pytest.raises(GatewayRejected) as raised:
                await call
            codes.append(raised.value.code)
        return codes

    assert asyncio.run(run()) == ["UNSUPPORTED_CONTRACT_VERSION"] * 2
    assert seen == []


def test_v1_answers_from_the_executor_are_explicitly_rejected() -> None:
    simulation, receipt = RETIRED["valid_simulations"][0], RETIRED["valid_gateway_receipts"][0]
    command = corpus_command(0).model_copy(update={"command_id": UUID(simulation["command_id"])})

    def answer(payload: dict[str, Any]) -> HttpExecutionGateway:
        return gateway(lambda request: httpx.Response(200, json=payload))[0]

    with pytest.raises(ExecutorContractError, match="contract version 1"):
        asyncio.run(answer(simulation).simulate(command))
    with pytest.raises(ExecutorContractError, match="contract version 1"):
        asyncio.run(answer(receipt).lookup(UUID(receipt["command_id"])))
    with pytest.raises(ExecutionOutcomeUnknown):
        asyncio.run(answer(receipt).submit(command, command.idempotency_key, None))


# --- submit -------------------------------------------------------------------------------


def test_submit_carries_the_idempotency_key_mode_token_and_bound_approval() -> None:
    payload = CORPUS["valid_gateway_receipts"][0]
    gw, seen = gateway(lambda request: httpx.Response(202, json=payload))
    command = corpus_command(0)
    approval = OperatorApproval(
        approval_id=UUID("40000000-0000-4000-8000-000000000001"),
        approved_at=datetime(2030, 1, 2, 3, 0, 0, tzinfo=UTC),
        expires_at=datetime(2030, 1, 2, 3, 1, 0, tzinfo=UTC),
        operator_id="local-operator",
        command_digest=payload["command_digest"],
        simulation_digest=payload["simulation_digest"],
        proof="e" * 64,
    )

    receipt = asyncio.run(gw.submit(command, command.idempotency_key, approval))

    assert receipt == GatewayReceiptV2.model_validate(payload)
    (request,) = seen
    assert (request.method, request.url.path) == ("POST", "/v1/commands")
    assert DlmmCommandV2.model_validate(json.loads(request.content)) == command
    assert {
        name: request.headers[name]
        for name in (
            "idempotency-key",
            "x-internal-token",
            "x-mode-token",
            "x-approval-id",
            "x-approval-approved-at",
            "x-approval-expires-at",
            "x-operator-id",
            "x-command-digest",
            "x-simulation-digest",
            "x-approval-proof",
        )
    } == {
        "idempotency-key": "entry:plan-201",
        "x-internal-token": INTERNAL_CREDENTIAL,
        "x-mode-token": MODE_CREDENTIAL,
        "x-approval-id": "40000000-0000-4000-8000-000000000001",
        "x-approval-approved-at": "2030-01-02T03:00:00.000Z",
        "x-approval-expires-at": "2030-01-02T03:01:00.000Z",
        "x-operator-id": "local-operator",
        "x-command-digest": payload["command_digest"],
        "x-simulation-digest": payload["simulation_digest"],
        "x-approval-proof": "e" * 64,
    }


def test_the_same_idempotency_key_never_creates_a_second_transaction() -> None:
    """Mirrors the executor contract: a known key returns its first receipt."""

    command = live_command()
    simulation = simulated(command)
    broadcasts: dict[str, dict[str, Any]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        key = request.headers["idempotency-key"]
        if key not in broadcasts:
            broadcasts[key] = receipt_for(command, simulation, GatewayState.SUBMITTED)
        return httpx.Response(202, json=broadcasts[key])

    gw, seen = gateway(handler)

    async def run() -> tuple[GatewayReceiptV2, GatewayReceiptV2]:
        first = await gw.submit(command, command.idempotency_key, None)
        second = await gw.submit(command, command.idempotency_key, None)
        return first, second

    first, second = asyncio.run(run())

    assert first == second
    assert len(broadcasts) == 1
    assert {request.headers["idempotency-key"] for request in seen} == {command.idempotency_key}


def test_executor_rejections_are_definitive_and_never_leak_the_token() -> None:
    gw, _ = gateway(lambda request: httpx.Response(401, json={"error": "mode_token_required"}))
    command = corpus_command(0)

    with pytest.raises(GatewayRejected) as raised:
        asyncio.run(gw.submit(command, command.idempotency_key, None))

    assert raised.value.code == "MODE_TOKEN_REQUIRED"
    assert INTERNAL_CREDENTIAL not in str(raised.value) and MODE_CREDENTIAL not in str(raised.value)


@pytest.mark.parametrize("conflict", ["idempotency_conflict", "approval_already_used"])
def test_a_conflict_with_an_existing_submission_is_an_unknown_outcome(conflict: str) -> None:
    """engine.ts 543/677/778: the executor already holds a submission under this key or
    this approval, so the command may be on its way - never "nothing was sent"."""

    command = live_command()
    simulation = simulated(command)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/simulations":
            return httpx.Response(200, json=simulation.model_dump(mode="json"))
        if request.method == "POST":
            return httpx.Response(409, json={"error": conflict})
        return httpx.Response(404, json={"error": "command_not_found"})

    gw, seen = gateway(handler)
    store = InMemoryExecutionStore()
    service = live_service(gw, store)

    async def run() -> ExecutionPhase:
        with pytest.raises(ExecutionOutcomeUnknown) as raised:
            await gw.submit(command, command.idempotency_key, None)
        assert not isinstance(raised.value, GatewayRejected)
        await service.prepare(PLAN_ID)
        with pytest.raises(ExecutionOutcomeUnknown):
            await service.execute(command.command_id)
        record = await store.get(command.command_id)
        assert record is not None
        return record.phase

    assert asyncio.run(run()) is ExecutionPhase.UNKNOWN
    submits = [r for r in seen if (r.method, r.url.path) == ("POST", "/v1/commands")]
    assert len(submits) == 2  # one direct call, one through the service; never a resend


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ReadTimeout("timed out after the request was written"),
        httpx.RemoteProtocolError("connection dropped"),
        httpx.Response(500, json={"error": "executor_error"}),
        httpx.Response(202, json={"schema_version": 3}),
    ],
    ids=["read-timeout", "dropped-connection", "server-error", "invalid-receipt"],
)
def test_an_uncertain_submit_is_never_reported_as_not_sent(
    failure: Exception | httpx.Response,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(failure, httpx.Response):
            return failure
        raise failure

    gw, _ = gateway(handler)
    command = corpus_command(0)

    with pytest.raises(ExecutionOutcomeUnknown) as raised:
        asyncio.run(gw.submit(command, command.idempotency_key, None))

    assert not isinstance(raised.value, GatewayRejected)


def test_an_unreachable_executor_is_a_definitive_rejection() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    gw, _ = gateway(handler)
    command = corpus_command(0)

    with pytest.raises(GatewayRejected):
        asyncio.run(gw.submit(command, command.idempotency_key, None))


def test_status_by_signature_is_not_exposed_by_the_executor() -> None:
    gw, seen = gateway(lambda request: httpx.Response(200, json={}))

    with pytest.raises(GatewayRejected):
        asyncio.run(gw.status("signature-1"))

    assert seen == []


# --- through ExecutionService --------------------------------------------------------------


def test_timeout_after_send_is_unknown_and_reconciled_by_status_without_resending() -> None:
    command = live_command()
    simulation = simulated(command)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/simulations":
            return httpx.Response(200, json=simulation.model_dump(mode="json"))
        if request.method == "POST":
            raise httpx.ReadTimeout("the executor did not answer in time")
        return httpx.Response(200, json=receipt_for(command, simulation, GatewayState.CONFIRMED))

    gw, seen = gateway(handler)
    store = InMemoryExecutionStore()
    service = live_service(gw, store)

    async def run() -> tuple[ExecutionPhase, GatewayReceiptV2]:
        await service.prepare(PLAN_ID)
        with pytest.raises(ExecutionOutcomeUnknown):
            await service.execute(command.command_id)
        record = await store.get(command.command_id)
        assert record is not None
        phase = record.phase
        return phase, await service.execute(command.command_id)

    phase_after_timeout, receipt = asyncio.run(run())

    assert phase_after_timeout is ExecutionPhase.UNKNOWN
    assert receipt.state is GatewayState.CONFIRMED
    submits = [r for r in seen if (r.method, r.url.path) == ("POST", "/v1/commands")]
    lookups = [r for r in seen if r.method == "GET"]
    assert len(submits) == 1
    assert [r.url.path for r in lookups] == [f"/v1/commands/{command.command_id}"]


def test_unknown_command_at_the_executor_stays_unknown_and_is_never_resent() -> None:
    command = live_command()
    simulation = simulated(command)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/simulations":
            return httpx.Response(200, json=simulation.model_dump(mode="json"))
        if request.method == "POST":
            raise httpx.RemoteProtocolError("connection dropped")
        return httpx.Response(404, json={"error": "command_not_found"})

    gw, seen = gateway(handler)
    store = InMemoryExecutionStore()
    service = live_service(gw, store)

    async def run() -> ExecutionPhase:
        await service.prepare(PLAN_ID)
        for _ in range(2):
            with pytest.raises(ExecutionOutcomeUnknown):
                await service.execute(command.command_id)
        record = await store.get(command.command_id)
        assert record is not None
        return record.phase

    assert asyncio.run(run()) is ExecutionPhase.UNKNOWN
    assert len([r for r in seen if (r.method, r.url.path) == ("POST", "/v1/commands")]) == 1


def test_a_rejected_submit_is_terminal_and_nothing_is_left_unknown() -> None:
    command = live_command()
    simulation = simulated(command)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/simulations":
            return httpx.Response(200, json=simulation.model_dump(mode="json"))
        return httpx.Response(409, json={"error": "stale_simulation"})

    gw, _ = gateway(handler)
    store = InMemoryExecutionStore()
    service = live_service(gw, store)

    async def run() -> ExecutionPhase:
        await service.prepare(PLAN_ID)
        with pytest.raises(GatewayRejected):
            await service.execute(command.command_id)
        record = await store.get(command.command_id)
        assert record is not None
        return record.phase

    assert asyncio.run(run()) is ExecutionPhase.FAILED


# --- readiness ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("answer", "ready"),
    [
        (httpx.Response(200, json={"status": "ready", "mode": "APPROVAL"}), True),
        (httpx.Response(200, json={"status": "ready", "mode": "SHADOW"}), False),
        (httpx.Response(503, json={"status": "not_ready"}), False),
        (httpx.ConnectError("refused"), False),
    ],
    ids=["ready-for-mode", "ready-for-another-mode", "not-ready", "unreachable"],
)
def test_executor_readiness_is_specific_to_the_mode(
    answer: httpx.Response | Exception, ready: bool
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/health/ready"
        if isinstance(answer, Exception):
            raise answer
        return answer

    gw, _ = gateway(handler)

    assert asyncio.run(gw.ready(TradingMode.APPROVAL)) is ready


# --- read-only quotes ------------------------------------------------------------------------


def quote_client(handler: Handler) -> tuple[HttpQuoteClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(record), base_url="http://executor.internal"
    )
    return HttpQuoteClient(client, internal_token=INTERNAL_CREDENTIAL), seen


def test_a_quote_is_a_strict_v1_request_answered_by_the_shared_result() -> None:
    request = QuoteRequestV1.model_validate(QUOTES["valid_requests"][0])
    answer = QUOTES["valid_results"][0]
    client, seen = quote_client(lambda _: httpx.Response(200, json=answer))

    result = asyncio.run(client.quote(request))

    assert result == QuoteResultV1.model_validate(answer)
    (sent,) = seen
    assert (sent.method, sent.url.path) == ("POST", "/v1/quotes")
    assert sent.headers["x-internal-token"] == INTERNAL_CREDENTIAL
    assert "x-mode-token" not in sent.headers
    assert json.loads(sent.content) == QUOTES["valid_requests"][0]


@pytest.mark.parametrize(
    ("answer", "code"),
    [
        (httpx.Response(503, json={"error": "quotes_unavailable"}), "QUOTES_UNAVAILABLE"),
        (httpx.Response(400, json={"error": "unsupported_contract_version"}),
         "UNSUPPORTED_CONTRACT_VERSION"),
        (httpx.Response(200, json=QUOTES["invalid_results"][1]["payload"]),
         "EXECUTOR_CONTRACT_VIOLATION"),
        (httpx.Response(200, json=QUOTES["valid_results"][1]), "EXECUTOR_CONTRACT_VIOLATION"),
        (httpx.ConnectError("refused"), "EXECUTOR_UNREACHABLE"),
    ],
    ids=["no-quoter", "version", "invalid-result", "answer-for-another-request", "unreachable"],
)
def test_a_quote_that_is_not_a_measured_answer_is_unavailable(
    answer: httpx.Response | Exception, code: str
) -> None:
    request = QuoteRequestV1.model_validate(QUOTES["valid_requests"][0])

    def handler(_: httpx.Request) -> httpx.Response:
        if isinstance(answer, Exception):
            raise answer
        return answer

    client, _ = quote_client(handler)

    with pytest.raises(QuoteUnavailable) as raised:
        asyncio.run(client.quote(request))

    assert raised.value.code == code
    assert INTERNAL_CREDENTIAL not in str(raised.value)
