"""Minimal internal HTTP endpoint of the worker: ``/health/live``, ``/health/ready``, ``/metrics``.

Built on asyncio streams to avoid an extra web stack inside the worker; it only
answers GET requests and never exposes configuration or secrets.
"""

import asyncio
import json
from collections.abc import Callable

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest

from src.domain.models.trading import TradingMode
from src.observability.health import HealthMonitor

_REASONS = {200: "OK", 404: "Not Found", 405: "Method Not Allowed", 503: "Service Unavailable"}


class ObservabilityServer:
    def __init__(
        self,
        health: HealthMonitor,
        registry: CollectorRegistry,
        mode: Callable[[], TradingMode],
    ) -> None:
        self._health = health
        self._registry = registry
        self._mode = mode
        self._server: asyncio.Server | None = None

    @property
    def port(self) -> int:
        if self._server is None:
            raise RuntimeError("server is not started")
        port: int = self._server.sockets[0].getsockname()[1]
        return port

    async def start(self, host: str, port: int) -> None:
        self._server = await asyncio.start_server(self._serve, host, port)

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = (await asyncio.wait_for(reader.readline(), timeout=5)).decode("latin-1")
            while (await asyncio.wait_for(reader.readline(), timeout=5)) not in (
                b"\r\n",
                b"\n",
                b"",
            ):
                pass
            parts = request_line.split()
            status, content_type, body = self._route(parts)
            head = (
                f"HTTP/1.1 {status} {_REASONS[status]}\r\n"
                f"Content-Type: {content_type}\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Connection: close\r\n\r\n"
            )
            writer.write(head.encode("latin-1") + body)
            await writer.drain()
        except (TimeoutError, ConnectionError):
            pass
        finally:
            writer.close()

    def _route(self, parts: list[str]) -> tuple[int, str, bytes]:
        if len(parts) < 2:
            return 405, "application/json", b'{"status": "bad_request"}'
        method, path = parts[0], parts[1].split("?", 1)[0]
        if method != "GET":
            return 405, "application/json", b'{"status": "method_not_allowed"}'
        if path == "/health/live":
            live = self._health.liveness()
            liveness = {"status": "ok" if live.alive else "stale", "reason": live.reason}
            return (200 if live.alive else 503), "application/json", _json(liveness)
        if path == "/health/ready":
            report = self._health.readiness(self._mode())
            readiness = {
                "status": report.status.value,
                "mode": report.mode.value,
                "reasons": list(report.reasons),
            }
            return (200 if report.ready else 503), "application/json", _json(readiness)
        if path == "/metrics":
            return 200, CONTENT_TYPE_LATEST, generate_latest(self._registry)
        return 404, "application/json", b'{"status": "not_found"}'


def _json(payload: object) -> bytes:
    return json.dumps(payload).encode("utf-8")
