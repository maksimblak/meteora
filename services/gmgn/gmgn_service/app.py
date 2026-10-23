"""The internal HTTP surface of the ``gmgn`` service (compose network only, no auth).

* ``GET /v1/tokens/{chain}/{mint}/signals`` -> ``gmgn-signals.v1`` (always 200 for a valid
  token: an unreadable page is an ``UNAVAILABLE`` answer with its reason);
* ``GET /health/live`` - the process; ``GET /health/ready`` - a browser is running and the
  circuit is closed (503 with the reasons otherwise).
"""

import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from gmgn_service.config import ServiceSettings
from gmgn_service.contract import SignalsV1
from gmgn_service.service import SignalService

SUPPORTED_CHAINS = frozenset({"sol"})
_MINT = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def create_app(
    *, settings: ServiceSettings | None = None, service: SignalService | None = None
) -> FastAPI:
    """``service`` given: used as is (tests). Otherwise a browser is launched at startup."""

    resolved = settings or ServiceSettings.from_environment(os.environ)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if service is not None:
            app.state.service = service
            yield
            return
        from gmgn_service.page import BrowserPageFetcher

        fetcher = BrowserPageFetcher(resolved)
        await fetcher.start()
        app.state.service = SignalService(fetcher, settings=resolved)
        try:
            yield
        finally:
            await fetcher.close()

    app = FastAPI(title="gmgn-signals", lifespan=lifespan, docs_url=None, redoc_url=None)

    @app.get("/v1/tokens/{chain}/{mint}/signals", response_model=None)
    async def signals(chain: str, mint: str, request: Request) -> SignalsV1 | JSONResponse:
        if chain not in SUPPORTED_CHAINS:
            return _error(404, "CHAIN_UNSUPPORTED", f"chain {chain!r} is not supported")
        if not _MINT.fullmatch(mint):
            return _error(422, "MINT_INVALID", "mint must be a base58 public key")
        current: SignalService = request.app.state.service
        return await current.signals(chain, mint)

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "live"}

    @app.get("/health/ready", response_model=None)
    async def ready(request: Request) -> JSONResponse:
        current: SignalService = request.app.state.service
        is_ready, reasons = current.readiness()
        if is_ready:
            return JSONResponse({"status": "ready", "reasons": []})
        return JSONResponse({"status": "not_ready", "reasons": reasons}, status_code=503)

    return app


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)
