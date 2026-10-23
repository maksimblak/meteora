import asyncio
import io
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from prometheus_client import CollectorRegistry, generate_latest

from src.core.clock import FrozenClock
from src.core.config import AppConfig
from src.core.logging import configure_logging
from src.domain.models.primitives import PublicKey
from src.domain.models.trading import TradingMode
from src.market.models import PoolPage, PoolRef, ProviderHealth, ProviderState
from src.modes import InMemoryModeRepository
from src.observability.alerts import CriticalAlertReporter
from src.observability.health import HealthMonitor, ReadinessStatus
from src.observability.http import ObservabilityServer
from src.observability.instrumented import InstrumentedMarketDataProvider
from src.observability.logging import cycle_context, error_fields, get_logger
from src.observability.metrics import WorkerMetrics
from src.providers.fixtures import FixtureFailure, FixtureMarketDataProvider

NOW = datetime(2026, 10, 20, 12, tzinfo=UTC)
SECRET_ENVIRONMENT = (
    "DATABASE_URL",
    "APP_OPERATOR_TOKEN",
    "APP_SESSION_SECRET",
    "HELIUS_API_KEY",
    "EXECUTOR_INTERNAL_TOKEN",
    "TELEGRAM_BOT_TOKEN",
    "WALLET_KEYPAIR_FILE",
)
HEALTHY = ProviderHealth(ProviderState.HEALTHY, 0, None, None)
DEGRADED = ProviderHealth(ProviderState.DEGRADED, 2, "timeout", None)


def records(stream: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


# --- structured logs --------------------------------------------------------------------


def test_cycle_logs_carry_correlation_and_causation_ids() -> None:
    stream = io.StringIO()
    configure_logging(AppConfig.load(environ={}), stream=stream)
    correlation, causation = uuid4(), uuid4()

    with cycle_context(correlation_id=correlation, causation_id=causation, job="universe_sync"):
        get_logger("worker.test").info("cycle_started", pools=3)
    get_logger("worker.test").info("outside")

    inside, outside = records(stream)
    assert inside["correlation_id"] == str(correlation)
    assert inside["causation_id"] == str(causation)
    assert inside["job"] == "universe_sync"
    assert "correlation_id" not in outside


@pytest.mark.parametrize("name", SECRET_ENVIRONMENT)
def test_logs_redact_every_secret_variable(name: str) -> None:
    secret = (
        f"postgresql+asyncpg://user:pw-{uuid4().hex}@db/x"
        if name == "DATABASE_URL"
        else (f"secret-{uuid4().hex}")
    )
    stream = io.StringIO()
    configure_logging(AppConfig.load(environ={name: secret}), stream=stream)
    log = get_logger("worker.redaction")

    with cycle_context(correlation_id=uuid4()):
        log.info(f"leak {secret}", detail={"nested": [secret]}, value=secret)
        log.error("failure", **error_fields(RuntimeError(f"boom {secret}")))
        log.info("by key", **{"api_key": "x", "token": "y", "private_key": "z", "password": "w"})

    output = stream.getvalue()
    assert secret not in output
    lines = records(stream)
    assert len(lines) == 3
    assert all("correlation_id" in line for line in lines)
    assert lines[2]["api_key"] == lines[2]["password"] == "[REDACTED]"


# --- metrics ------------------------------------------------------------------------------


def test_metrics_cover_every_required_family() -> None:
    registry = CollectorRegistry()
    metrics = WorkerMetrics(registry)

    metrics.provider_request("meteora", "list_pools", 0.25, "ok")
    metrics.provider_request("solana", "get_token_risk", 1.5, "PROVIDER_TIMEOUT")
    metrics.provider_state("meteora", HEALTHY)
    metrics.data_freshness("universe", 42)
    metrics.queue_depth("candidate_pipeline", 7)
    metrics.lease("universe_sync", held=True)
    metrics.lease_contended("universe_sync")
    metrics.job_run("universe_sync", "SUCCEEDED", 0.5)
    metrics.candidate("REJECT", ("LOW_TVL", "STALE_MARKET_DATA"))
    metrics.execution(TradingMode.SHADOW, "CONFIRMED")
    metrics.open_positions(2)
    metrics.pnl(Decimal("-12.5"), Decimal("-3"))
    metrics.risk_utilization(daily_loss=Decimal("0.5"), positions=Decimal("0.66"))
    metrics.outbox(published=3, failed=1, pending=4)
    metrics.critical_alerts(1)

    text = generate_latest(registry).decode()
    for family in (
        "meteora_provider_request_seconds",
        "meteora_provider_errors_total",
        "meteora_provider_state",
        "meteora_data_freshness_seconds",
        "meteora_job_queue_depth",
        "meteora_job_lease_held",
        "meteora_job_lease_contention_total",
        "meteora_job_runs_total",
        "meteora_candidates_total",
        "meteora_candidate_rejections_total",
        "meteora_executions_total",
        "meteora_open_positions",
        "meteora_pnl_base_units",
        "meteora_risk_utilization_ratio",
        "meteora_outbox_events_total",
        "meteora_outbox_pending",
        "meteora_critical_alerts_open",
    ):
        assert family in text, family
    assert (
        'meteora_provider_errors_total{error_code="PROVIDER_TIMEOUT",provider="solana"} 1.0' in text
    )
    assert 'meteora_candidate_rejections_total{reason="LOW_TVL"} 1.0' in text
    assert 'meteora_pnl_base_units{kind="realized"} -12.5' in text


def test_instrumented_provider_records_latency_and_error_codes() -> None:
    registry = CollectorRegistry()
    metrics = WorkerMetrics(registry)
    pool = PoolRef(
        address=PublicKey("SysvarRent111111111111111111111111111111111"),
        base_mint=PublicKey("11111111111111111111111111111111"),
        quote_mint=PublicKey("So11111111111111111111111111111111111111112"),
    )
    healthy = InstrumentedMarketDataProvider(
        FixtureMarketDataProvider(pages=[PoolPage(items=(pool,))], snapshots=[]),
        metrics,
        name="meteora",
    )
    failing = InstrumentedMarketDataProvider(
        FixtureMarketDataProvider(
            pages=[PoolPage(items=())], snapshots=[], failure=FixtureFailure.TIMEOUT
        ),
        metrics,
        name="meteora_backup",
    )

    asyncio.run(healthy.list_pools(None))
    with pytest.raises(Exception, match="timeout"):
        asyncio.run(failing.list_pools(None))

    text = generate_latest(registry).decode()
    assert (
        'meteora_provider_request_seconds_count{operation="list_pools",provider="meteora"} 1.0'
        in text
    )
    assert (
        'meteora_provider_errors_total{error_code="PROVIDER_TIMEOUT",provider="meteora_backup"} 1.0'
        in text
    )
    assert failing.health.state is ProviderState.OPEN


# --- liveness and readiness ------------------------------------------------------------------


def healthy_monitor(clock: FrozenClock) -> HealthMonitor:
    monitor = HealthMonitor(clock, heartbeat_timeout=timedelta(seconds=60))
    monitor.heartbeat()
    monitor.set_persistence(True)
    monitor.register_provider("meteora", lambda: HEALTHY)
    monitor.register_provider("solana", lambda: HEALTHY)
    monitor.set_last_sync(clock.now())
    monitor.set_open_critical_alerts(0)
    monitor.set_execution_gateway(True)
    return monitor


def test_liveness_reflects_the_process_heartbeat_only() -> None:
    clock = FrozenClock(NOW)
    monitor = healthy_monitor(clock)
    monitor.set_persistence(False)

    assert monitor.liveness().alive is True
    clock.advance(timedelta(seconds=61))
    stale = monitor.liveness()
    assert stale.alive is False
    assert stale.reason == "HEARTBEAT_STALE"


def test_provider_degradation_never_reports_healthy_live() -> None:
    clock = FrozenClock(NOW)
    monitor = healthy_monitor(clock)
    assert monitor.readiness(TradingMode.LIVE).status is ReadinessStatus.READY

    monitor.register_provider("solana", lambda: DEGRADED)
    live = monitor.readiness(TradingMode.LIVE)
    shadow = monitor.readiness(TradingMode.SHADOW)

    assert (live.ready, live.status) == (False, ReadinessStatus.NOT_READY)
    assert "PROVIDER_DEGRADED:solana" in live.reasons
    assert (shadow.ready, shadow.status) == (True, ReadinessStatus.DEGRADED)
    assert "PROVIDER_DEGRADED:solana" in shadow.reasons


def test_readiness_is_mode_specific() -> None:
    clock = FrozenClock(NOW)
    monitor = healthy_monitor(clock)
    monitor.set_execution_gateway(None)
    monitor.set_open_critical_alerts(1)

    assert monitor.readiness(TradingMode.SHADOW).ready is True
    approval = monitor.readiness(TradingMode.APPROVAL)
    assert approval.ready is False
    assert {"EXECUTION_GATEWAY_NOT_CONFIGURED", "CRITICAL_ALERTS_OPEN"} <= set(approval.reasons)

    monitor.set_persistence(False)
    assert monitor.readiness(TradingMode.SHADOW).status is ReadinessStatus.NOT_READY
    clock.advance(timedelta(hours=1))
    monitor.set_persistence(True)
    assert "UNIVERSE_STALE" in monitor.readiness(TradingMode.SHADOW).reasons


def test_worker_http_endpoint_serves_liveness_readiness_and_metrics() -> None:
    async def scenario() -> tuple[str, str, str]:
        clock = FrozenClock(NOW)
        monitor = healthy_monitor(clock)
        monitor.set_persistence(False)
        registry = CollectorRegistry()
        WorkerMetrics(registry).open_positions(1)
        server = ObservabilityServer(monitor, registry, lambda: TradingMode.SHADOW)
        await server.start("127.0.0.1", 0)
        try:
            responses = []
            for path in ("/health/live", "/health/ready", "/metrics"):
                reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
                writer.write(f"GET {path} HTTP/1.1\r\nHost: test\r\n\r\n".encode())
                await writer.drain()
                responses.append((await reader.read()).decode())
                writer.close()
                await writer.wait_closed()
            return responses[0], responses[1], responses[2]
        finally:
            await server.close()

    live, ready, metrics = asyncio.run(scenario())
    assert live.startswith("HTTP/1.1 200") and '"status": "ok"' in live
    assert ready.startswith("HTTP/1.1 503") and "PERSISTENCE_UNAVAILABLE" in ready
    assert metrics.startswith("HTTP/1.1 200") and "meteora_open_positions 1.0" in metrics


# --- critical alerts -------------------------------------------------------------------------


def test_alert_is_buffered_when_durable_store_fails_and_flushed_later() -> None:
    repository = InMemoryModeRepository()
    clock = FrozenClock(NOW)
    registry = CollectorRegistry()

    class Failing:
        def raise_alert(self, alert: object) -> None:
            raise OSError("database unavailable")

    failing = CriticalAlertReporter(Failing(), clock, WorkerMetrics(registry))  # type: ignore[arg-type]
    alert = failing.raise_alert("AUDIT_PERSISTENCE_FAILED", "audit write failed")
    assert failing.pending == (alert,)

    recovered = CriticalAlertReporter(repository, clock, WorkerMetrics(CollectorRegistry()))
    recovered.adopt(failing.pending)
    assert recovered.flush() == 1
    assert [item.code for item in repository.alerts()] == ["AUDIT_PERSISTENCE_FAILED"]
    assert recovered.pending == ()
    assert "meteora_critical_alerts_buffered 1.0" in generate_latest(registry).decode()


def test_recurring_conditions_do_not_flood_alerts_during_or_after_an_outage() -> None:
    repository = InMemoryModeRepository()
    clock = FrozenClock(NOW)

    class Flaky:
        healthy = False

        def raise_alert(self, alert: object) -> None:
            if not self.healthy:
                raise OSError("database unavailable")
            repository.raise_alert(alert)  # type: ignore[arg-type]

    store = Flaky()
    reporter = CriticalAlertReporter(store, clock, WorkerMetrics(CollectorRegistry()))  # type: ignore[arg-type]
    for _ in range(50):
        reporter.raise_alert("AUDIT_PERSISTENCE_FAILED", "audit write failed")
    assert len(reporter.pending) == 1

    store.healthy = True
    assert reporter.flush() == 1
    for _ in range(5):
        reporter.raise_alert(
            "EXECUTION_RECOVERY_REQUIRED", "1 command unknown", dedupe_key="recovery:cmd-1"
        )
    assert [alert.code for alert in repository.alerts()] == [
        "AUDIT_PERSISTENCE_FAILED",
        "EXECUTION_RECOVERY_REQUIRED",
    ]
