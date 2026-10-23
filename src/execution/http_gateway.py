"""``ExecutionGateway`` over the internal TypeScript executor API (contracts V1).

Routes: ``POST /v1/simulations``, ``POST /v1/commands`` (``Idempotency-Key``) and
``GET /v1/commands/{id}``, each authenticated by the internal token. Every answer is
validated against the V1 models; an unknown version or schema is never trusted.

Failure taxonomy that ``ExecutionService`` relies on:

* ``GatewayRejected`` - the executor provably did nothing: it refused the request
  (4xx), had no execution engine (503 ``execution_unavailable``) or was unreachable
  before the request left this process;
* ``ExecutionOutcomeUnknown`` - a submit that may have been received: timeout or drop
  after sending, any other 5xx, or a receipt outside the V1 contract; the caller
  reconciles through ``lookup`` and never resends;
* ``ExecutorContractError`` - a simulation or lookup answer outside the V1 contract.

Token values never appear in errors or logs.
"""

import re
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
from pydantic import BaseModel, ValidationError

from src.domain.models.trading import TradingMode
from src.execution.contracts import (
    ChainReceiptV1,
    DlmmCommandV1,
    GatewayReceiptV1,
    OperatorApproval,
    SimulationResultV1,
)
from src.execution.service import ExecutionBlocked, ExecutionOutcomeUnknown, GatewayRejected

# Raised before the request could leave this process: nothing reached the executor.
_NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
_ERROR_CODE = re.compile(r"[a-z0-9_]{1,64}")
# A health probe must never stall the worker loop behind a long money-call timeout.
_READINESS_TIMEOUT = 2.0


class ExecutorContractError(ExecutionBlocked):
    """The executor answered with an unknown contract version or an invalid schema."""


class HttpExecutionGateway:
    """``ExecutionGateway`` and ``ReceiptLookup`` for one executor base URL."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        internal_token: str,
        mode_token: str | None = None,
    ) -> None:
        if not internal_token.strip():
            raise ValueError("the executor internal token is required")
        self._client = client
        self._internal_token = internal_token
        self._mode_token = mode_token

    async def simulate(self, command: DlmmCommandV1) -> SimulationResultV1:
        response = await self._send(
            self._request("POST", "/v1/simulations", json=_command_payload(command))
        )
        if response.status_code != 200:
            raise _rejection(response)
        result = _parse(SimulationResultV1, response)
        if result.command_id != command.command_id:
            raise ExecutorContractError("executor simulated another command")
        return result

    async def submit(
        self,
        command: DlmmCommandV1,
        idempotency_key: str,
        approval: OperatorApproval | None,
    ) -> GatewayReceiptV1:
        headers = {"Idempotency-Key": idempotency_key}
        if self._mode_token is not None:
            headers["x-mode-token"] = self._mode_token
        if approval is not None:
            headers.update(_approval_headers(approval))
        try:
            request = self._request(
                "POST", "/v1/commands", json=_command_payload(command), headers=headers
            )
        except (UnicodeEncodeError, ValueError) as error:
            raise GatewayRejected("INVALID_REQUEST", "submit request cannot be encoded") from error
        try:
            response = await self._client.send(request)
        except _NOT_SENT as error:
            raise GatewayRejected(
                "EXECUTOR_UNREACHABLE", "execution gateway is unreachable"
            ) from error
        except httpx.HTTPError as error:
            raise ExecutionOutcomeUnknown(
                "submit outcome is unknown: the executor did not answer"
            ) from error
        if 400 <= response.status_code < 500 or _engine_unavailable(response):
            raise _rejection(response)
        if response.status_code not in (200, 202):
            raise ExecutionOutcomeUnknown(
                f"submit outcome is unknown: the executor answered HTTP {response.status_code}"
            )
        try:
            receipt = _parse(GatewayReceiptV1, response)
        except ExecutorContractError as error:
            raise ExecutionOutcomeUnknown(
                "submit outcome is unknown: the receipt violates the V1 contract"
            ) from error
        if receipt.command_id != command.command_id or receipt.idempotency_key != idempotency_key:
            raise ExecutionOutcomeUnknown(
                "submit outcome is unknown: the receipt answers another command"
            )
        return receipt

    async def lookup(self, command_id: UUID) -> GatewayReceiptV1 | None:
        """``GET /v1/commands/{id}``: the executor's receipt, reconciled with the chain."""

        response = await self._send(self._request("GET", f"/v1/commands/{command_id}"))
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise _rejection(response)
        receipt = _parse(GatewayReceiptV1, response)
        if receipt.command_id != command_id:
            raise ExecutorContractError("executor answered for another command")
        return receipt

    async def status(self, signature: str) -> ChainReceiptV1:
        del signature
        raise GatewayRejected(
            "STATUS_BY_SIGNATURE_UNAVAILABLE",
            "the executor exposes command status only; reconcile through lookup",
        )

    async def ready(self, mode: TradingMode) -> bool:
        """True only when the executor reports itself ready for exactly ``mode``."""

        try:
            response = await self._client.get("/health/ready", timeout=_READINESS_TIMEOUT)
        except httpx.HTTPError:
            return False
        if response.status_code != 200:
            return False
        try:
            body = response.json()
        except ValueError:
            return False
        return (
            isinstance(body, dict)
            and body.get("status") == "ready"
            and body.get("mode") == mode.value
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Request:
        return self._client.build_request(
            method,
            path,
            json=json,
            headers={"x-internal-token": self._internal_token, **(headers or {})},
        )

    async def _send(self, request: httpx.Request) -> httpx.Response:
        """Send a request that can never broadcast: any transport failure is a rejection."""

        try:
            return await self._client.send(request)
        except httpx.HTTPError as error:
            raise GatewayRejected(
                "EXECUTOR_UNREACHABLE", "execution gateway is unreachable"
            ) from error


def _command_payload(command: DlmmCommandV1) -> dict[str, Any]:
    # The executor's schema is strict: an absent optional field is omitted, never null.
    return command.model_dump(mode="json", exclude_none=True)


def _approval_headers(approval: OperatorApproval) -> dict[str, str]:
    return {
        "x-approval-id": str(approval.approval_id),
        "x-approval-approved-at": _timestamp(approval.approved_at),
        "x-approval-expires-at": _timestamp(approval.expires_at),
        "x-operator-id": approval.operator_id,
        "x-command-digest": approval.command_digest,
        "x-simulation-digest": approval.simulation_digest,
        "x-approval-proof": approval.proof,
    }


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _engine_unavailable(response: httpx.Response) -> bool:
    return response.status_code == 503 and _error_code(response) == "EXECUTION_UNAVAILABLE"


def _error_code(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        body = None
    code = body.get("error") if isinstance(body, dict) else None
    if isinstance(code, str) and _ERROR_CODE.fullmatch(code):
        return code.upper()
    return f"HTTP_{response.status_code}"


def _rejection(response: httpx.Response) -> GatewayRejected:
    code = _error_code(response)
    return GatewayRejected(code, f"executor rejected the request: {code}")


def _parse[Model: BaseModel](model: type[Model], response: httpx.Response) -> Model:
    try:
        payload = response.json()
    except ValueError as error:
        raise ExecutorContractError("executor answer is not JSON") from error
    try:
        return model.model_validate(payload)
    except ValidationError as error:
        raise ExecutorContractError(
            f"executor answer violates the {model.__name__} contract"
        ) from error
