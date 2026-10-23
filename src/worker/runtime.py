"""Worker composition: repositories, providers, pipeline, jobs, recovery and health.

Startup order: refresh health -> **restart recovery** (network reconciliation before
any new action) -> lease-guarded scheduler. Jobs, by priority:

0. ``position_cycle``  - open positions first: marks, exits, daily stop;
1. ``recovery``        - periodic reconciliation of unresolved commands;
2. ``candidate_pipeline`` - opens the UTC risk day, then evaluates the universe;
3. ``universe_sync``   - paginated discovery, open-position pools first;
4. ``outbox_relay`` / 5. ``retention`` - publication and telemetry clean-up.

The default stack is SHADOW-only: without an execution gateway, APPROVAL/LIVE
entries are cancelled as ``EXECUTION_GATEWAY_NOT_CONFIGURED`` and nothing is sent.
"""

import asyncio
import contextlib
import os
import signal
import socket
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from uuid import uuid4

import httpx

from src.core.clock import Clock, SystemClock
from src.core.config import AppConfig
from src.core.logging import configure_logging
from src.domain.models.primitives import PublicKey
from src.domain.models.trading import TradingMode
from src.execution.service import ExecutionService
from src.market.contracts import MarketDataProvider, OnChainProvider
from src.market.models import (
    AuthorityStatus,
    DataQuality,
    Freshness,
    ProviderHealth,
    ProviderState,
    RiskSignal,
    TokenRiskSnapshot,
    WalletState,
)
from src.market.universe import SyncStatus, UniverseSync
from src.observability.alerts import CriticalAlertReporter
from src.observability.health import HealthMonitor
from src.observability.http import ObservabilityServer
from src.observability.instrumented import (
    InstrumentedMarketDataProvider,
    InstrumentedOnChainProvider,
)
from src.observability.logging import error_fields, get_logger
from src.observability.metrics import WorkerMetrics
from src.observability.sinks import LoggingEventSink
from src.persistence.contracts import EventSink
from src.persistence.factory import Repositories, open_repositories
from src.providers.contracts import ProviderUnavailableError
from src.recovery.service import ExecutorLease, RecoveryService
from src.worker.execution import (
    CommandSubmitter,
    EntryExecutor,
    GatewaySubmitter,
    RegisteredPlanResolver,
    ShadowSubmitter,
    shadow_address,
)
from src.worker.pipeline import CandidatePipeline
from src.worker.policy import PipelinePolicy, hypothesis_policy
from src.worker.portfolio import RiskDayKeeper, ShadowPortfolio
from src.worker.positions import PositionCycle
from src.worker.scheduler import JobContext, JobSpec, Scheduler

# Entries execute only inside this job; recovery treats its lease as "an executor
# may be running" and its TTL as the age after which a claim counts as abandoned.
CANDIDATE_JOB = "candidate_pipeline"
CANDIDATE_LEASE_TTL = timedelta(minutes=2)

_UNKNOWN_SIGNALS = (
    "blacklisted",
    "freeze_authority",
    "holder_concentration",
    "mint_authority",
    "verified",
    "wash_trading",
)


def _seconds(environ: Mapping[str, str], name: str, default: int) -> timedelta:
    raw = environ.get(name)
    value = int(raw) if raw else default
    if value <= 0:
        raise ValueError(f"{name} must be a positive number of seconds")
    return timedelta(seconds=value)


def _integer(environ: Mapping[str, str], name: str, default: int) -> int:
    raw = environ.get(name)
    return int(raw) if raw else default


@dataclass(frozen=True, slots=True)
class WorkerSettings:
    position_interval: timedelta = timedelta(seconds=15)
    candidate_interval: timedelta = timedelta(seconds=60)
    universe_interval: timedelta = timedelta(minutes=5)
    recovery_interval: timedelta = timedelta(minutes=1)
    outbox_interval: timedelta = timedelta(seconds=10)
    retention_interval: timedelta = timedelta(hours=1)
    max_concurrency: int = 2
    candidate_concurrency: int = 4
    universe_concurrency: int = 8
    universe_batch_size: int = 50
    shadow_equity_base_units: int = 1_000_000_000
    metrics_host: str = "127.0.0.1"
    metrics_port: int | None = 9100
    owner: str = field(
        default_factory=lambda: f"{socket.gethostname()}-{os.getpid()}-{uuid4().hex[:8]}"
    )

    @classmethod
    def from_environment(cls, environ: Mapping[str, str]) -> "WorkerSettings":
        port = environ.get("WORKER_METRICS_PORT")
        return cls(
            position_interval=_seconds(environ, "WORKER_POSITION_INTERVAL_SECONDS", 15),
            candidate_interval=_seconds(environ, "WORKER_CANDIDATE_INTERVAL_SECONDS", 60),
            universe_interval=_seconds(environ, "WORKER_UNIVERSE_INTERVAL_SECONDS", 300),
            max_concurrency=_integer(environ, "WORKER_MAX_CONCURRENCY", 2),
            shadow_equity_base_units=_integer(environ, "SHADOW_EQUITY_BASE_UNITS", 1_000_000_000),
            metrics_host=environ.get("WORKER_METRICS_HOST") or "127.0.0.1",
            metrics_port=9100 if not port else (None if port == "0" else int(port)),
        )


class UnavailableOnChainProvider:
    """Used when no Solana RPC is configured: every strict signal is ``UNKNOWN``."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock

    @property
    def health(self) -> ProviderHealth:
        return ProviderHealth(ProviderState.DEGRADED, 0, "not_configured", None)

    async def get_token_risk(self, mint: PublicKey) -> TokenRiskSnapshot:
        now = self._clock.now()
        return TokenRiskSnapshot(
            mint=mint,
            mint_authority=AuthorityStatus.UNKNOWN,
            freeze_authority=AuthorityStatus.UNKNOWN,
            blacklisted=RiskSignal.UNKNOWN,
            verified=RiskSignal.UNKNOWN,
            holder_concentration=None,
            wash_trading=RiskSignal.UNKNOWN,
            observed_at=now,
            source_at=now,
            ingested_at=now,
            freshness=Freshness.STALE,
            data_quality=DataQuality.UNAVAILABLE,
            missing_signals=_UNKNOWN_SIGNALS,
            provider="not_configured",
        )

    async def get_wallet_state(self, wallet: PublicKey) -> WalletState:
        raise ProviderUnavailableError("Solana RPC is not configured")


class WorkerRuntime:
    def __init__(
        self,
        *,
        settings: WorkerSettings,
        policy: PipelinePolicy,
        repositories: Repositories,
        market: MarketDataProvider | None,
        onchain: OnChainProvider,
        clock: Clock,
        metrics: WorkerMetrics | None = None,
        health: HealthMonitor | None = None,
        sink: EventSink | None = None,
        execution_service: ExecutionService | None = None,
        plan_resolver: RegisteredPlanResolver | None = None,
        wallet_address: str | None = None,
    ) -> None:
        self.settings = settings
        self.policy = policy
        self.repositories = repositories
        self.clock = clock
        self.metrics = metrics or WorkerMetrics()
        self.health = health or HealthMonitor(clock)
        self._market = market
        self._onchain = onchain
        self._log = get_logger("worker.runtime")
        repos = repositories
        self.alerts = CriticalAlertReporter(
            repos.mode, clock, self.metrics, ruleset_version=policy.ruleset_version
        )
        self.portfolio = ShadowPortfolio(
            settings.shadow_equity_base_units, repos.accounting, repos.mode
        )
        self.keeper = RiskDayKeeper(repos.mode, self.portfolio, clock, policy.risk)
        submitters: dict[TradingMode, CommandSubmitter] = {
            TradingMode.SHADOW: ShadowSubmitter(repos.accounting, clock, policy.shadow_costs)
        }
        if execution_service is not None:
            gateway = GatewaySubmitter(execution_service, plan_resolver or RegisteredPlanResolver())
            submitters[TradingMode.APPROVAL] = gateway
            submitters[TradingMode.LIVE] = gateway
        wallet = wallet_address or shadow_address("meteora-volume-bot:shadow-wallet")
        self.entries = EntryExecutor(
            modes=repos.mode,
            audit=repos.audit,
            submitters=submitters,
            contexts=repos.positions,
            alerts=self.alerts,
            clock=clock,
            metrics=self.metrics,
            policy=policy,
            wallet_address=wallet,
        )
        self.pipeline = CandidatePipeline(
            history=repos.history,
            onchain=onchain,
            decisions=repos.decisions,
            modes=repos.mode,
            entries=self.entries,
            portfolio=self.portfolio,
            audit=repos.audit,
            alerts=self.alerts,
            policy=policy,
            clock=clock,
            metrics=self.metrics,
        )
        self.positions = PositionCycle(
            modes=repos.mode,
            accounting=repos.accounting,
            contexts=repos.positions,
            history=repos.history,
            onchain=onchain,
            keeper=self.keeper,
            portfolio=self.portfolio,
            audit=repos.audit,
            alerts=self.alerts,
            policy=policy,
            clock=clock,
            metrics=self.metrics,
        )
        self.recovery = RecoveryService(
            modes=repos.mode,
            plans=repos.mode,
            accounting=repos.accounting,
            alerts=self.alerts,
            clock=clock,
            metrics=self.metrics,
            executions=repos.execution,
            execution_service=execution_service,
            executor_lease=ExecutorLease(
                repos.leases, CANDIDATE_JOB, f"{settings.owner}:recovery", CANDIDATE_LEASE_TTL
            ),
            stale_after=CANDIDATE_LEASE_TTL,
        )
        self.relay = repos.outbox_relay(sink or LoggingEventSink(), clock, owner=settings.owner)
        self.retention = repos.retention()
        self.health.set_execution_gateway(None if execution_service is None else True)
        if market is not None:
            self.health.register_provider("meteora", lambda: market.health)
        self.health.register_provider("solana", lambda: onchain.health)
        self.scheduler = Scheduler(
            self.jobs(),
            repos.leases,
            owner=settings.owner,
            clock=clock,
            max_concurrency=settings.max_concurrency,
            metrics=self.metrics,
        )

    def jobs(self) -> list[JobSpec]:
        settings = self.settings
        return [
            JobSpec("position_cycle", settings.position_interval, self.position_job, priority=0),
            JobSpec("recovery", settings.recovery_interval, self.recovery_job, priority=1),
            JobSpec(
                CANDIDATE_JOB,
                settings.candidate_interval,
                self.candidate_job,
                priority=2,
                lease_ttl=CANDIDATE_LEASE_TTL,
            ),
            JobSpec("universe_sync", settings.universe_interval, self.universe_job, priority=3),
            JobSpec("outbox_relay", settings.outbox_interval, self.outbox_job, priority=4),
            JobSpec("retention", settings.retention_interval, self.retention_job, priority=5),
        ]

    # --- jobs ---------------------------------------------------------------------------

    async def position_job(self, context: JobContext) -> None:
        await self.positions.run_all(correlation_id=context.correlation_id)

    async def recovery_job(self, context: JobContext) -> None:
        await self.recovery.reconcile()
        self.alerts.flush()

    async def candidate_job(self, context: JobContext) -> None:
        self.keeper.ensure_open()
        snapshots = await self.repositories.history.latest_universe()
        held = await self._open_position_pools()
        addresses = [
            snapshot.pool.address.root
            for snapshot in snapshots
            if snapshot.pool.address.root not in held
        ]
        self.metrics.queue_depth("candidate_pipeline", len(addresses))
        decided_at = self.clock.now()
        slots = asyncio.Semaphore(self.settings.candidate_concurrency)
        remaining = len(addresses)

        async def evaluate(address: str) -> None:
            nonlocal remaining
            async with slots:
                try:
                    await self.pipeline.evaluate(
                        address, decided_at, causation_id=context.correlation_id
                    )
                except Exception as error:
                    self._log.error("candidate_failed", pool_address=address, **error_fields(error))
                finally:
                    remaining -= 1
                    self.metrics.queue_depth("candidate_pipeline", remaining)

        await asyncio.gather(*(evaluate(address) for address in addresses))

    async def universe_job(self, context: JobContext) -> None:
        if self._market is None:
            self._log.warning("universe_sync_skipped", reason="MARKET_PROVIDER_NOT_CONFIGURED")
            return
        result = await UniverseSync(
            self._market,
            self.repositories.universe,
            clock=self.clock,
            max_concurrency=self.settings.universe_concurrency,
            batch_size=self.settings.universe_batch_size,
            priority_addresses=await self._open_position_pools(),
        ).run(context.correlation_id)
        if result.status is SyncStatus.COMPLETED:
            self.health.set_last_sync(self.clock.now())
        self._log.info(
            "universe_sync_finished",
            status=result.status.value,
            pools=result.pool_count,
            duplicates=result.duplicate_count,
            error_code=result.error_code,
        )

    async def outbox_job(self, context: JobContext) -> None:
        if self.relay is None:
            return
        report = await self.relay.publish_pending()
        self.metrics.outbox(
            published=report.published,
            failed=report.failed,
            pending=await self.relay.pending_count(),
        )

    async def retention_job(self, context: JobContext) -> None:
        if self.retention is not None:
            await self.retention.purge(self.clock.now())

    # --- lifecycle --------------------------------------------------------------------

    async def refresh_health(self) -> None:
        self.health.heartbeat()
        reachable = await self.repositories.ping()
        self.health.set_persistence(reachable)
        if not reachable:
            return
        try:
            self.alerts.flush()
            self.health.set_open_critical_alerts(self.repositories.mode.open_alert_count())
            last_sync = await self.repositories.history.latest_sync_at()
        except Exception as error:
            self._log.error("health_refresh_failed", **error_fields(error))
            self.health.set_persistence(False)
            return
        self.health.set_last_sync(last_sync)
        if last_sync is not None:
            self.metrics.data_freshness("universe", (self.clock.now() - last_sync).total_seconds())
        if self._market is not None:
            self.metrics.provider_state("meteora", self._market.health)
        self.metrics.provider_state("solana", self._onchain.health)

    async def run(self, stop: asyncio.Event) -> None:
        await self.refresh_health()
        report = await self.recovery.reconcile(startup=True)
        self._log.info("startup_recovery", operator_required=len(report.operator_required))
        server: ObservabilityServer | None = None
        if self.settings.metrics_port is not None:
            server = ObservabilityServer(
                self.health,
                self.metrics.registry,
                lambda: self.repositories.mode.mode_state().mode,
            )
            await server.start(self.settings.metrics_host, self.settings.metrics_port)
        try:
            await self.scheduler.run_forever(stop, on_tick=self.refresh_health)
        finally:
            if server is not None:
                await server.close()

    async def _open_position_pools(self) -> set[str]:
        open_ids = set(self.repositories.mode.open_positions())
        return {
            context.pool_address
            for context in await self.repositories.positions.all()
            if context.position_id in open_ids
        }

    def close(self) -> None:
        self.repositories.close()


def build_runtime(
    config: AppConfig,
    environ: Mapping[str, str],
    *,
    clock: Clock | None = None,
) -> tuple[WorkerRuntime, list[httpx.AsyncClient]]:
    clock = clock or SystemClock()
    settings = WorkerSettings.from_environment(environ)
    metrics = WorkerMetrics()
    repositories = open_repositories(config, environ=environ, clock=clock)
    clients: list[httpx.AsyncClient] = []
    market: MarketDataProvider | None = None
    if config.meteora_data_api_base_url:
        from src.providers.meteora import MeteoraDataApiProvider
        from src.providers.resilience import ResilientHttpClient

        client = httpx.AsyncClient(base_url=config.meteora_data_api_base_url)
        clients.append(client)
        market = InstrumentedMarketDataProvider(
            MeteoraDataApiProvider(ResilientHttpClient(client), clock=clock),
            metrics,
            name="meteora",
        )
    onchain: OnChainProvider = UnavailableOnChainProvider(clock)
    if config.solana_rpc_url:
        from src.providers.resilience import ResilientHttpClient
        from src.providers.solana import SolanaRpcProvider

        client = httpx.AsyncClient(base_url=config.solana_rpc_url)
        clients.append(client)
        onchain = InstrumentedOnChainProvider(
            SolanaRpcProvider(ResilientHttpClient(client), clock=clock), metrics, name="solana"
        )
    runtime = WorkerRuntime(
        settings=settings,
        policy=hypothesis_policy(),
        repositories=repositories,
        market=market,
        onchain=onchain,
        clock=clock,
        metrics=metrics,
        wallet_address=config.wallet_public_key,
    )
    return runtime, clients


async def run_worker() -> None:
    config = AppConfig.load()
    configure_logging(config)
    runtime, clients = build_runtime(config, os.environ)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in ("SIGTERM", "SIGINT"):
        if hasattr(signal, name):
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.add_signal_handler(getattr(signal, name), stop.set)
    try:
        await runtime.run(stop)
    finally:
        for client in clients:
            await client.aclose()
        runtime.close()
