"""The browser bridge (G25): GMGN metrics from a visible Chromium window on the user's PC.

    python -m gmgn_service.bridge      (scripts/gmgn-bridge.ps1 or scripts/gmgn-bridge.sh)

One window of the official Playwright Chromium with a persistent profile in the user's
folder (``tab.BrowserTab``); the worker in Docker asks ``GET /v1/tokens/sol/{mint}/signals``
at ``GMGN_URL`` and gets ``gmgn-signals.v2``. The data are ``fetch`` calls of gmgn.ai's own
web API made from inside its tab (``webapi``), in the user's session if the user signed in
there once by hand - the bridge never signs in, never reads, copies or logs a cookie, and the
profile never leaves the PC.

Bounds: one tab and one request at a time, at most ``GMGN_BRIDGE_REQUESTS_PER_MINUTE``
requests to gmgn.ai, a cache per token (``GMGN_CACHE_TTL_SECONDS``), tokens waiting
together share one batch ``mutil_window_token_info`` and Trending is asked at most every
``TRENDING_TTL``. A challenge, a captcha, a block or a 429 pauses everything for
``GMGN_BREAKER_COOLDOWN_SECONDS`` with that reason - nothing is retried differently.

Access: the server listens only on loopback and private addresses (``GMGN_BRIDGE_HOST``;
Docker Desktop reaches the host's loopback as ``host.docker.internal``, Docker on Linux the
``docker0`` address) and every route needs ``Authorization: Bearer <GMGN_BRIDGE_TOKEN>``.
"""

import asyncio
import contextlib
import hmac
import ipaddress
import logging
import os
import socket
import sys
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import SecretStr

from gmgn_service.app import SUPPORTED_CHAINS, error_response, valid_mint
from gmgn_service.config import ServiceSettings
from gmgn_service.contract import Signals, SignalsV2
from gmgn_service.service import PageUnavailable, SignalService
from gmgn_service.webapi import (
    BridgeReading,
    Refusal,
    WebAnswer,
    WebRequest,
    answer_data,
    bridge_signals,
    info_request,
    token_info_items,
    token_requests,
    trending_request,
    trending_wash,
)

TOKEN_ENV = "GMGN_BRIDGE_TOKEN"  # noqa: S105 - a variable name
HOST_ENV = "GMGN_BRIDGE_HOST"
PORT_ENV = "GMGN_BRIDGE_PORT"
PROFILE_ENV = "GMGN_BRIDGE_PROFILE_DIR"
RATE_ENV = "GMGN_BRIDGE_REQUESTS_PER_MINUTE"
DEFAULT_PORT = 8765
DEFAULT_REQUESTS_PER_MINUTE = 20
MINIMUM_TOKEN_LENGTH = 16
# Trending changes slowly; one answer serves every token read meanwhile.
TRENDING_TTL = 120.0
# A batch answer of mutil_window_token_info serves its other tokens this long.
BATCH_TTL = 60.0
BATCH_SIZE = 20
LOGIN_CHECK_SECONDS = 60.0

log = logging.getLogger("gmgn.bridge")
Sleep = Callable[[float], Awaitable[None]]


class WebSession(Protocol):
    """The gmgn.ai tab as the bridge uses it (``tab.BrowserTab``)."""

    @property
    def ready(self) -> bool: ...

    async def prepare(self) -> None:
        """The tab shows gmgn.ai; ``PageUnavailable`` (a challenge, a captcha, a block)."""

    async def request(self, request: WebRequest) -> WebAnswer: ...

    def mark_blocked(self) -> None:
        """The site refused a request: the tab is reloaded before the next load."""

    async def refresh_login(self) -> str:
        """``LOGGED_IN``, ``LOGGED_OUT`` or ``UNKNOWN`` - from the page, not from a cookie."""


class MinuteLimiter:
    """At most ``per_minute`` acquisitions in any 60 seconds; callers wait their turn."""

    def __init__(
        self,
        per_minute: int,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if per_minute < 1:
            raise ValueError(f"{RATE_ENV} must be at least one")
        self._per_minute = per_minute
        self._monotonic = monotonic
        self._sleep = sleep
        self._sent: list[float] = []
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = self._monotonic()
                self._sent = [moment for moment in self._sent if now - moment < 60.0]
                if len(self._sent) < self._per_minute:
                    self._sent.append(now)
                    return
                await self._sleep(self._sent[0] + 60.0 - now)


class BridgeFetcher:
    """Reads one token in the tab: the batch info, four token addresses and Trending.

    Loads run one at a time (one tab, requests in sequence). A refusal that pauses (block,
    challenge, rate limit) ends the load at once; any other refusal only leaves that
    answer's signals missing, and a load with nothing at all is ``PageUnavailable``.
    """

    def __init__(
        self,
        session: WebSession,
        *,
        limiter: MinuteLimiter,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._session = session
        self._limiter = limiter
        self._monotonic = monotonic
        self._lock = asyncio.Lock()
        self._wanted: OrderedDict[tuple[str, str], None] = OrderedDict()
        self._info: dict[tuple[str, str], tuple[float, Mapping[str, Any]]] = {}
        self._trending: dict[str, tuple[float, dict[str, bool]]] = {}

    @property
    def ready(self) -> bool:
        return self._session.ready

    def want(self, chain: str, mint: str) -> None:
        self._wanted[(chain, mint)] = None
        while len(self._wanted) > 4 * BATCH_SIZE:
            self._wanted.popitem(last=False)

    async def fetch(self, chain: str, mint: str) -> BridgeReading:
        async with self._lock:
            refusals: list[str] = []
            try:
                await self._session.prepare()
                info = await self._token_info(chain, mint, refusals)
                parts: dict[str, Any] = {}
                for request in token_requests(chain, mint):
                    parts[request.name] = await self._data(request, refusals)
                wash = await self._wash(chain, refusals)
            finally:
                self._wanted.pop((chain, mint), None)
        reading = bridge_signals(
            mint,
            info=info,
            stat=_mapping(parts.get("token_stat")),
            holder_stat=_mapping(parts.get("token_holder_stat")),
            fee_info=_mapping(parts.get("token_fee_info")),
            candles=_mapping(parts.get("token_mcap_candles")),
            wash=wash,
        )
        log.info(
            "read mint=%s sources=%d missing=%s refusals=%s",
            mint,
            len(reading.sources),
            ",".join(reading.missing) or "-",
            ",".join(sorted(set(refusals))) or "-",
        )
        if reading.empty:
            raise PageUnavailable(_main_reason(refusals))
        return reading

    async def _data(self, request: WebRequest, refusals: list[str]) -> Any:
        await self._limiter.acquire()
        data = answer_data(await self._session.request(request))
        if not isinstance(data, Refusal):
            return data
        refusals.append(data.reason)
        if data.pause:
            self._session.mark_blocked()
            log.warning("gmgn refused %s: %s - pause", request.name, data.reason)
            raise PageUnavailable(data.reason, pause=True)
        return None

    async def _token_info(
        self, chain: str, mint: str, refusals: list[str]
    ) -> Mapping[str, Any] | None:
        now = self._monotonic()
        self._info = {key: item for key, item in self._info.items() if now - item[0] < BATCH_TTL}
        cached = self._info.get((chain, mint))
        if cached is not None:
            return cached[1]
        others = [
            other
            for other_chain, other in self._wanted
            if other_chain == chain and other != mint and (chain, other) not in self._info
        ]
        batch = [mint, *others[: BATCH_SIZE - 1]]
        data = await self._data(info_request(chain, batch), refusals)
        items = token_info_items(data) if data is not None else {}
        for address in batch:
            if address in items:
                self._info[(chain, address)] = (now, items[address])
        return items.get(mint)

    async def _wash(self, chain: str, refusals: list[str]) -> dict[str, bool]:
        now = self._monotonic()
        cached = self._trending.get(chain)
        if cached is not None and now - cached[0] < TRENDING_TTL:
            return cached[1]
        data = await self._data(trending_request(chain), refusals)
        verdicts = trending_wash(data) if data is not None else {}
        # Kept even when empty: Trending is not asked again before its time.
        self._trending[chain] = (now, verdicts)
        return verdicts


def _mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _main_reason(refusals: list[str]) -> str:
    if not refusals:
        return "NO_SIGNALS"
    if "LOGIN_REQUIRED" in refusals:
        return "LOGIN_REQUIRED"
    return refusals[0]


# --- settings ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BridgeSettings:
    """The bridge's own settings plus the shared ones of ``ServiceSettings`` (one tab)."""

    service: ServiceSettings
    token: SecretStr
    profile_dir: Path
    hosts: tuple[str, ...] = ("127.0.0.1",)
    port: int = DEFAULT_PORT
    requests_per_minute: int = DEFAULT_REQUESTS_PER_MINUTE

    @classmethod
    def from_environment(cls, environ: Mapping[str, str]) -> "BridgeSettings":
        token = _secret(environ, TOKEN_ENV)
        if token is None or len(token.get_secret_value()) < MINIMUM_TOKEN_LENGTH:
            raise ValueError(
                f"set {TOKEN_ENV} (or {TOKEN_ENV}_FILE) to a random string of at least "
                f"{MINIMUM_TOKEN_LENGTH} characters - the same value the worker gets"
            )
        raw_profile = (environ.get(PROFILE_ENV) or "").strip()
        profile = (
            Path(raw_profile).expanduser()
            if raw_profile
            else cls.default_profile(platform=sys.platform, environ=environ, home=Path.home())
        )
        raw_port = (environ.get(PORT_ENV) or "").strip()
        raw_rate = (environ.get(RATE_ENV) or "").strip()
        rate = int(raw_rate) if raw_rate else DEFAULT_REQUESTS_PER_MINUTE
        if rate < 1:
            raise ValueError(f"{RATE_ENV} must be at least one")
        return cls(
            service=replace(ServiceSettings.from_environment(environ), max_pages=1),
            token=token,
            hosts=_bind_hosts(environ.get(HOST_ENV) or ""),
            port=int(raw_port) if raw_port else DEFAULT_PORT,
            profile_dir=_outside_repository(profile.resolve()),
            requests_per_minute=rate,
        )

    @staticmethod
    def default_profile(*, platform: str, environ: Mapping[str, str], home: Path) -> Path:
        """``%LOCALAPPDATA%\\MeteoraVolumeBot\\gmgn-bridge-profile`` on Windows, else
        ``$XDG_DATA_HOME/meteora-volume-bot/gmgn-bridge-profile`` (``~/.local/share``)."""

        if platform.startswith("win"):
            base = Path(environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
            return base / "MeteoraVolumeBot" / "gmgn-bridge-profile"
        base = Path(environ.get("XDG_DATA_HOME") or home / ".local" / "share")
        return base / "meteora-volume-bot" / "gmgn-bridge-profile"


def _secret(environ: Mapping[str, str], name: str) -> SecretStr | None:
    """``NAME`` or ``NAME_FILE``, never both - as every secret of the project."""

    direct = (environ.get(name) or "").strip()
    file_name = (environ.get(f"{name}_FILE") or "").strip()
    if direct and file_name:
        raise ValueError(f"set either {name} or {name}_FILE, not both")
    if direct:
        return SecretStr(direct)
    if file_name:
        value = Path(file_name).read_text(encoding="utf-8").strip()
        return SecretStr(value) if value else None
    return None


def _bind_hosts(raw: str) -> tuple[str, ...]:
    hosts = tuple(item.strip() for item in raw.split(",") if item.strip()) or ("127.0.0.1",)
    for host in hosts:
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            raise ValueError(f"{HOST_ENV} lists IP addresses only, e.g. 127.0.0.1") from None
        if address.is_unspecified or not (address.is_loopback or address.is_private):
            raise ValueError(
                f"{HOST_ENV}: only loopback or a private (Docker bridge) address, "
                "never all interfaces or a public one"
            )
    return hosts


def _outside_repository(profile: Path) -> Path:
    """The browser profile holds the session; it must never land in the repository."""

    project = Path(__file__).resolve().parents[1]
    roots = [
        project,
        *(path for path in project.parents if (path / "docker-compose.yml").is_file()),
    ]
    if any(profile == root or root in profile.parents for root in roots):
        raise ValueError(f"{PROFILE_ENV} must be outside the repository (it holds the session)")
    return profile


# --- HTTP -------------------------------------------------------------------------------


def create_bridge_app(*, token: SecretStr, service: SignalService, tab: WebSession) -> FastAPI:
    """The bridge's HTTP surface; every route answers 401 without the shared token."""

    app = FastAPI(title="gmgn-bridge", docs_url=None, redoc_url=None, openapi_url=None)
    expected = token.get_secret_value().encode("utf-8")

    @app.middleware("http")
    async def shared_token(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        header = request.headers.get("authorization", "")
        scheme, _, given = header.partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(
            given.strip().encode("utf-8"), expected
        ):
            return error_response(401, "UNAUTHORIZED", "the shared bridge token is required")
        return await call_next(request)

    @app.get("/v1/tokens/{chain}/{mint}/signals", response_model=None)
    async def signals(chain: str, mint: str) -> Signals | JSONResponse:
        if chain not in SUPPORTED_CHAINS:
            return error_response(404, "CHAIN_UNSUPPORTED", f"chain {chain!r} is not supported")
        if not valid_mint(mint):
            return error_response(422, "MINT_INVALID", "mint must be a base58 public key")
        answer = await service.signals(chain, mint)
        log.info("signals mint=%s status=%s reason=%s", mint, answer.status, answer.reason)
        return answer

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "live"}

    @app.get("/health/ready", response_model=None)
    async def ready() -> JSONResponse:
        is_ready, reasons = service.readiness()
        paused = service.paused()
        body = {
            "status": "ready" if is_ready else "not_ready",
            "reasons": reasons,
            "mode": "bridge",
            "login": await tab.refresh_login(),
            "paused": None
            if paused is None
            else {"reason": paused[0], "until": paused[1].isoformat()},
        }
        return JSONResponse(body, status_code=200 if is_ready else 503)

    return app


def bind_sockets(hosts: tuple[str, ...], port: int) -> list[socket.socket]:
    sockets: list[socket.socket] = []
    for host in hosts:
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        listener = socket.socket(family, socket.SOCK_STREAM)
        if sys.platform != "win32":
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((host, port))
        listener.listen(64)
        listener.setblocking(False)
        sockets.append(listener)
    return sockets


async def serve(settings: BridgeSettings) -> None:
    import uvicorn

    from gmgn_service.tab import BrowserTab

    tab = BrowserTab(
        base_url=settings.service.base_url,
        profile_dir=settings.profile_dir,
        page_timeout=settings.service.page_timeout,
    )
    sockets = bind_sockets(settings.hosts, settings.port)
    await tab.start()
    fetcher = BridgeFetcher(tab, limiter=MinuteLimiter(settings.requests_per_minute))
    service = SignalService(fetcher, settings=settings.service, schema=SignalsV2)
    app = create_bridge_app(token=settings.token, service=service, tab=tab)
    server = uvicorn.Server(uvicorn.Config(app, log_level="info", lifespan="off"))
    addresses = ", ".join(f"http://{host}:{settings.port}" for host in settings.hosts)
    log.info("bridge listening on %s; profile %s", addresses, settings.profile_dir)
    log.info("keep this window open and the PC awake; closing the window stops the bridge")

    async def watch() -> None:
        last = ""
        while not tab.closed.is_set():
            state = await tab.refresh_login()
            if state != last:
                last = state
                if state == "LOGGED_OUT":
                    log.info("login: LOGGED_OUT - data come without an account")
                else:
                    log.info("login: %s", state)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(tab.closed.wait(), LOGIN_CHECK_SECONDS)
        log.warning("the bridge window was closed - stopping; start the bridge again")
        server.should_exit = True

    watcher = asyncio.create_task(watch())
    try:
        await server.serve(sockets=sockets)
    finally:
        watcher.cancel()
        await tab.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        settings = BridgeSettings.from_environment(os.environ)
    except (OSError, ValueError) as error:
        log.error("bridge settings: %s", error)
        raise SystemExit(2) from None
    asyncio.run(serve(settings))


if __name__ == "__main__":
    main()
