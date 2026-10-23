"""Prometheus metrics of the operator API on the process registry served by ``/metrics``.

Latency and errors are labelled by route template (never by raw path), so the
label set stays bounded. Freshness, queue and alert gauges follow the status pulse.
Provider latency and circuit-breaker states are the worker's own metrics.
"""

from time import perf_counter

from prometheus_client import Counter, Gauge, Histogram
from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_SECONDS = Histogram(
    "meteora_api_request_seconds", "Operator API request latency", ("method", "route")
)
REQUEST_ERRORS = Counter(
    "meteora_api_errors_total",
    "Operator API responses with status >= 400",
    ("method", "route", "status"),
)
UNIVERSE_AGE_SECONDS = Gauge(
    "meteora_api_universe_age_seconds",
    "Age of the last complete universe sync seen by the API; -1 before the first sync",
)
APPROVALS_PENDING = Gauge(
    "meteora_api_approvals_pending", "Commands awaiting the operator's approval"
)
OPEN_CRITICAL_ALERTS = Gauge("meteora_api_open_critical_alerts", "Open critical alerts")


class RequestMetrics:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = perf_counter()
        status = 500

        async def tracked(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, tracked)
        finally:
            route = getattr(scope.get("route"), "path", None) or "unmatched"
            method = str(scope.get("method", "GET"))
            REQUEST_SECONDS.labels(method, route).observe(perf_counter() - started)
            if status >= 400:
                REQUEST_ERRORS.labels(method, route, str(status)).inc()
