"""Health of the processes the API does not own: the worker and the execution gateway.

Provider health lives inside the worker, so the API reads the worker's own
``/health/ready`` (mode-specific readiness with reasons such as
``PROVIDER_DEGRADED:meteora``) instead of guessing it. A probe never raises.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, Protocol

import httpx

from src.domain.models.trading import TradingMode

ProbeStatus = Literal["OK", "DEGRADED", "DOWN", "NOT_CONFIGURED"]


@dataclass(frozen=True, slots=True)
class ProbeResult:
    status: ProbeStatus
    reasons: tuple[str, ...] = ()


class ServiceProbe(Protocol):
    @property
    def name(self) -> str: ...

    async def check(self) -> ProbeResult: ...


class GatewayReadiness(Protocol):
    async def ready(self, mode: TradingMode) -> bool: ...


class WorkerHealthProbe:
    def __init__(
        self,
        base_url: str | None,
        *,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/") if base_url else None
        self._timeout = timeout_seconds
        self._transport = transport

    @property
    def name(self) -> str:
        return "worker"

    async def check(self) -> ProbeResult:
        if self._base_url is None:
            return ProbeResult("NOT_CONFIGURED", ("WORKER_HEALTH_NOT_CONFIGURED",))
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url, timeout=self._timeout, transport=self._transport
            ) as client:
                response = await client.get("/health/ready")
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            return ProbeResult("DOWN", ("WORKER_UNREACHABLE",))
        if not isinstance(payload, dict):
            return ProbeResult("DOWN", ("WORKER_UNREACHABLE",))
        raw_reasons = payload.get("reasons")
        reasons = (
            tuple(str(item) for item in raw_reasons) if isinstance(raw_reasons, list) else ()
        )
        status = payload.get("status")
        if status == "READY":
            return ProbeResult("OK", reasons)
        if status == "DEGRADED":
            return ProbeResult("DEGRADED", reasons)
        return ProbeResult("DOWN", reasons or ("WORKER_UNREACHABLE",))


class GatewayProbe:
    def __init__(
        self,
        readiness: GatewayReadiness | None,
        mode: Callable[[], Awaitable[TradingMode]],
    ) -> None:
        self._readiness = readiness
        self._mode = mode

    @property
    def name(self) -> str:
        return "executor"

    async def check(self) -> ProbeResult:
        if self._readiness is None:
            return ProbeResult("NOT_CONFIGURED", ("EXECUTION_GATEWAY_NOT_CONFIGURED",))
        try:
            mode = await self._mode()
            ready = await self._readiness.ready(
                TradingMode.APPROVAL if mode is TradingMode.SHADOW else mode
            )
        except Exception:
            return ProbeResult("DOWN", ("EXECUTION_GATEWAY_NOT_READY",))
        return ProbeResult("OK") if ready else ProbeResult("DOWN", ("EXECUTION_GATEWAY_NOT_READY",))
