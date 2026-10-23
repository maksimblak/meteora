"""Prometheus metrics of the worker: providers, freshness, jobs, candidates, money and risk.

Floats appear only here, at the telemetry boundary; domain values stay ``Decimal``.
"""

from collections.abc import Iterable
from decimal import Decimal

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

from src.domain.models.trading import TradingMode
from src.market.models import ProviderHealth, ProviderState


class WorkerMetrics:
    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry or CollectorRegistry()
        r = self.registry
        self._provider_latency = Histogram(
            "meteora_provider_request_seconds",
            "Provider request latency",
            ("provider", "operation"),
            registry=r,
        )
        self._provider_errors = Counter(
            "meteora_provider_errors_total",
            "Provider request failures by normalized error code",
            ("provider", "error_code"),
            registry=r,
        )
        self._provider_state = Gauge(
            "meteora_provider_state",
            "1 for the current circuit-breaker state of each provider",
            ("provider", "state"),
            registry=r,
        )
        self._freshness = Gauge(
            "meteora_data_freshness_seconds",
            "Age of the newest successfully ingested data",
            ("source",),
            registry=r,
        )
        self._queue = Gauge(
            "meteora_job_queue_depth", "Work items waiting per job", ("job",), registry=r
        )
        self._lease = Gauge(
            "meteora_job_lease_held",
            "1 while this worker holds the job lease",
            ("job",),
            registry=r,
        )
        self._contention = Counter(
            "meteora_job_lease_contention_total",
            "Job runs skipped because another worker holds the lease",
            ("job",),
            registry=r,
        )
        self._job_runs = Counter(
            "meteora_job_runs_total", "Finished job runs by outcome", ("job", "outcome"), registry=r
        )
        self._job_duration = Histogram(
            "meteora_job_duration_seconds", "Job run duration", ("job",), registry=r
        )
        self._candidates = Counter(
            "meteora_candidates_total", "Candidate decisions by status", ("status",), registry=r
        )
        self._rejections = Counter(
            "meteora_candidate_rejections_total",
            "Candidate rejection and uncertainty reasons",
            ("reason",),
            registry=r,
        )
        self._executions = Counter(
            "meteora_executions_total",
            "Entry and exit execution outcomes",
            ("mode", "outcome"),
            registry=r,
        )
        self._positions = Gauge("meteora_open_positions", "Open positions", registry=r)
        self._pnl = Gauge(
            "meteora_pnl_base_units", "Daily P&L in base units", ("kind",), registry=r
        )
        self._risk = Gauge(
            "meteora_risk_utilization_ratio",
            "Share of a risk limit in use (1 = limit reached)",
            ("limit",),
            registry=r,
        )
        self._outbox = Counter(
            "meteora_outbox_events_total", "Outbox publication results", ("result",), registry=r
        )
        self._outbox_pending = Gauge(
            "meteora_outbox_pending", "Outbox events not yet published", registry=r
        )
        self._alerts = Gauge("meteora_critical_alerts_open", "Open critical alerts", registry=r)
        self._alerts_buffered = Gauge(
            "meteora_critical_alerts_buffered",
            "Critical alerts not yet persisted because storage failed",
            registry=r,
        )

    def provider_request(self, provider: str, operation: str, seconds: float, outcome: str) -> None:
        self._provider_latency.labels(provider, operation).observe(seconds)
        if outcome != "ok":
            self._provider_errors.labels(provider, outcome).inc()

    def provider_state(self, provider: str, health: ProviderHealth) -> None:
        for state in ProviderState:
            self._provider_state.labels(provider, state.value).set(
                1 if health.state is state else 0
            )

    def data_freshness(self, source: str, age_seconds: float) -> None:
        self._freshness.labels(source).set(age_seconds)

    def queue_depth(self, job: str, depth: int) -> None:
        self._queue.labels(job).set(depth)

    def lease(self, job: str, *, held: bool) -> None:
        self._lease.labels(job).set(1 if held else 0)

    def lease_contended(self, job: str) -> None:
        self._contention.labels(job).inc()

    def job_run(self, job: str, outcome: str, seconds: float) -> None:
        self._job_runs.labels(job, outcome).inc()
        self._job_duration.labels(job).observe(seconds)

    def candidate(self, status: str, reasons: Iterable[str]) -> None:
        self._candidates.labels(status).inc()
        for reason in reasons:
            self._rejections.labels(reason).inc()

    def execution(self, mode: TradingMode, outcome: str) -> None:
        self._executions.labels(mode.value, outcome).inc()

    def open_positions(self, count: int) -> None:
        self._positions.set(count)

    def pnl(self, realized: Decimal, conservative_unrealized: Decimal) -> None:
        self._pnl.labels("realized").set(float(realized))
        self._pnl.labels("conservative_unrealized").set(float(conservative_unrealized))

    def risk_utilization(self, *, daily_loss: Decimal, positions: Decimal) -> None:
        self._risk.labels("daily_loss").set(float(daily_loss))
        self._risk.labels("open_positions").set(float(positions))

    def outbox(self, *, published: int, failed: int, pending: int) -> None:
        self._outbox.labels("published").inc(published)
        self._outbox.labels("failed").inc(failed)
        self._outbox_pending.set(pending)

    def critical_alerts(self, open_count: int) -> None:
        self._alerts.set(open_count)

    def buffered_alerts(self, count: int) -> None:
        self._alerts_buffered.set(count)
