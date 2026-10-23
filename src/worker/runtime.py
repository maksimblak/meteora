"""Worker composition: repositories, providers, pipeline, jobs, recovery and health.

Startup order: refresh health -> **restart recovery** (network reconciliation before
any new action) -> lease-guarded scheduler. Jobs, by priority:

0. ``position_cycle``  - open positions first: marks, exits, daily stop;
1. ``recovery``        - periodic reconciliation of unresolved commands;
2. ``candidate_pipeline`` - opens the UTC risk day, then evaluates the universe;
3. ``universe_sync``   - paginated discovery, open-position pools first;
4. ``outbox_relay`` / 5. ``retention`` - publication and telemetry clean-up.

GMGN (G18): with ``GMGN_URL`` the candidate pipeline's token risk carries the reading of
the internal ``gmgn`` service (``GmgnProvider``); positions keep the plain provider, so
page loads stay with candidates. Without it nothing is asked and nothing changes, except
that the loaded guide-v1 rules keep LIVE closed until ``GMGN_REQUIRED_FOR_LIVE=false``.

The default stack is SHADOW-only: without an execution gateway, APPROVAL/LIVE
entries are cancelled as ``EXECUTION_GATEWAY_NOT_CONFIGURED`` and nothing is sent.
A gateway is wired only for ``TRADING_MODE`` APPROVAL or LIVE with ``EXECUTOR_URL``,
the internal token and PostgreSQL (the execution store must be durable). It then
serves exactly that mode: approved entries continue inside the entry job and
non-SHADOW exits inside the position job, both from durable execution records.
"""

import asyncio
import contextlib
import os
import signal
import socket
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol
from uuid import uuid4

import httpx

from src.core.clock import Clock, SystemClock
from src.core.config import AppConfig
from src.core.logging import configure_logging
from src.domain.models.primitives import PublicKey
from src.domain.models.trading import TradingMode
from src.execution.contracts import LivePromotionEvidence
from src.execution.http_gateway import HttpExecutionGateway, HttpQuoteClient
from src.execution.service import (
    ApprovalVerifier,
    ExecutionGateway,
    ExecutionService,
    HmacApprovalVerifier,
    ReceiptLookup,
)
from src.execution.settings import ExecutorSettings
from src.gmgn import GmgnProvider
from src.gmgn.enrichment import EnhancedSignalSource, EnrichedOnChainProvider
from src.market.contracts import MarketDataProvider, OnChainProvider, VolumeHistorySource
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
from src.persistence.factory import PersistenceBackend, Repositories, open_repositories
from src.providers.contracts import PoolStateReader, ProviderUnavailableError
from src.recovery.service import ExecutorLease, RecoveryService
from src.worker.evidence import DailyEvidence
from src.worker.execution import (
    AnchorSource,
    CommandResolver,
    CommandSubmitter,
    EntryExecutor,
    GatewayEntrySettler,
    GatewayFillBook,
    GatewayPositionManager,
    GatewaySubmitter,
    ModeKillSwitch,
    ShadowSubmitter,
    shadow_address,
    snapshot_anchor,
)
from src.worker.pipeline import CandidatePipeline, QuoteSource
from src.worker.policy import PipelinePolicy, load_pipeline_policy, universe_interval
from src.worker.portfolio import RiskDayKeeper, ShadowPortfolio
from src.worker.positions import PositionCycle
from src.worker.scheduler import JobContext, JobSpec, Scheduler

# Entries execute only inside this job; recovery treats its lease as "an executor
# may be running" and its TTL as the age after which a claim counts as abandoned.
CANDIDATE_JOB = "candidate_pipeline"
CANDIDATE_LEASE_TTL = timedelta(minutes=2)
# The executor readiness probe runs on the health tick, but not more often than this.
GATEWAY_READINESS_INTERVAL = timedelta(seconds=15)
UNIVERSE_FILTER_ENV = "METEORA_UNIVERSE_FILTER"
FULL_SWEEP = "all"
QUOTES_URL_ENV = "EXECUTOR_QUOTES_URL"
GMGN_URL_ENV = "GMGN_URL"
GMGN_TIMEOUT_ENV = "GMGN_TIMEOUT_SECONDS"
GMGN_STALE_AFTER_ENV = "GMGN_STALE_AFTER_SECONDS"

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
            # The same reading the policy's observation gap is derived from.
            universe_interval=universe_interval(environ),
            max_concurrency=_integer(environ, "WORKER_MAX_CONCURRENCY", 2),
            shadow_equity_base_units=_integer(environ, "SHADOW_EQUITY_BASE_UNITS", 1_000_000_000),
            metrics_host=environ.get("WORKER_METRICS_HOST") or "127.0.0.1",
            metrics_port=9100 if not port else (None if port == "0" else int(port)),
        )


class GatewayReadiness(Protocol):
    async def ready(self, mode: TradingMode) -> bool: ...


@dataclass(frozen=True, slots=True)
class GatewayWiring:
    """The execution gateway for exactly one non-SHADOW mode (``TRADING_MODE``)."""

    gateway: ExecutionGateway
    allowed_mode: TradingMode
    receipts: ReceiptLookup | None = None
    readiness: GatewayReadiness | None = None
    approval_verifier: ApprovalVerifier | None = None
    live_promotion: LivePromotionEvidence | None = None
    simulation_ttl: timedelta = timedelta(minutes=2)

    def __post_init__(self) -> None:
        if self.allowed_mode is TradingMode.SHADOW:
            raise ValueError("SHADOW never uses the execution gateway")


class UnavailableOnChainProvider:
    """Used when no Solana RPC is configured: every strict signal is ``UNKNOWN``.

    The snapshot says so (``source_configured=False``, D09): SHADOW goes on with the
    ``TOKEN_RISK_UNAVAILABLE`` flag, APPROVAL and LIVE are closed.
    """

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
            source_configured=False,
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
        gateway: GatewayWiring | None = None,
        anchors: AnchorSource = snapshot_anchor,
        wallet_address: str | None = None,
        pool_states: PoolStateReader | None = None,
        quotes: QuoteSource | None = None,
        warmup: VolumeHistorySource | None = None,
        gmgn: EnhancedSignalSource | None = None,
    ) -> None:
        """``pool_states`` re-reads the active bin before a non-SHADOW entry, ``quotes``
        measures candidates' depth (``POST /v1/quotes``) and ``warmup`` serves a new
        candidate's volume history (D07); each is optional and its absence fails closed.
        ``gmgn`` attaches GMGN metrics to candidates' token risk (G18)."""

        self.settings = settings
        self.policy = policy
        self.repositories = repositories
        self.clock = clock
        self.metrics = metrics or WorkerMetrics()
        self.health = health or HealthMonitor(clock)
        self._market = market
        self._onchain = onchain
        self.gmgn = gmgn
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
        self._gateway = gateway
        self._gateway_checked_at: datetime | None = None
        self.execution: ExecutionService | None = None
        self.gateway_entries: GatewayEntrySettler | None = None
        self.gateway_positions: GatewayPositionManager | None = None
        if gateway is not None:
            resolver = CommandResolver(repos.execution)
            self.execution = ExecutionService(
                gateway=gateway.gateway,
                store=repos.execution,
                plans=resolver,
                clock=clock,
                allowed_mode=gateway.allowed_mode,
                simulation_ttl=gateway.simulation_ttl,
                approval_verifier=gateway.approval_verifier,
                live_promotion=gateway.live_promotion,
                receipts=gateway.receipts,
                kill_switch=ModeKillSwitch(repos.mode),
                kill_switch_policy=policy.kill_switch_policy,
            )
            book = GatewayFillBook(repos.accounting, repos.execution, clock, policy.shadow_costs)
            submitters[gateway.allowed_mode] = GatewaySubmitter(self.execution, resolver, book)
            self.gateway_entries = GatewayEntrySettler(
                modes=repos.mode,
                executions=repos.execution,
                service=self.execution,
                book=book,
                contexts=repos.positions,
                alerts=self.alerts,
                clock=clock,
                metrics=self.metrics,
            )
            self.gateway_positions = GatewayPositionManager(
                modes=repos.mode,
                accounting=repos.accounting,
                contexts=repos.positions,
                history=repos.history,
                executions=repos.execution,
                service=self.execution,
                resolver=resolver,
                book=book,
                alerts=self.alerts,
                clock=clock,
                metrics=self.metrics,
                policy=policy,
            )
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
            anchors=anchors,
            pool_states=pool_states,
            wallets=onchain,
            conservative_shadow=ShadowSubmitter(
                repos.accounting, clock, policy.conservative_costs
            ),
        )
        self.evidence = DailyEvidence(
            modes=repos.mode,
            accounting=repos.accounting,
            portfolio=self.portfolio,
            clock=clock,
            ruleset_version=policy.ruleset_version,
            cycle_gap=max(timedelta(minutes=10), settings.candidate_interval * 3),
        )
        self.pipeline = CandidatePipeline(
            history=repos.history,
            # Only candidates reach the token-risk read, so only they cost a GMGN page.
            onchain=onchain
            if gmgn is None
            else EnrichedOnChainProvider(onchain, gmgn, clock=clock),
            decisions=repos.decisions,
            modes=repos.mode,
            entries=self.entries,
            portfolio=self.portfolio,
            audit=repos.audit,
            alerts=self.alerts,
            policy=policy,
            clock=clock,
            metrics=self.metrics,
            quotes=quotes,
            warmup=warmup,
            evidence=self.evidence,
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
            decisions=repos.decisions,
            evidence=self.evidence,
        )
        self.recovery = RecoveryService(
            modes=repos.mode,
            plans=repos.mode,
            accounting=repos.accounting,
            alerts=self.alerts,
            clock=clock,
            metrics=self.metrics,
            executions=repos.execution,
            execution_service=self.execution,
            executor_lease=ExecutorLease(
                repos.leases, CANDIDATE_JOB, f"{settings.owner}:recovery", CANDIDATE_LEASE_TTL
            ),
            stale_after=CANDIDATE_LEASE_TTL,
        )
        self.relay = repos.outbox_relay(sink or LoggingEventSink(), clock, owner=settings.owner)
        self.retention = repos.retention()
        self.health.set_execution_gateway(None if gateway is None else False)
        if market is not None:
            self.health.register_provider("meteora", lambda: market.health)
        self.health.register_provider("solana", lambda: onchain.health)
        if gmgn is not None:
            self.health.register_provider("gmgn", lambda: gmgn.health)
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
        if self.gateway_positions is not None:
            await self.gateway_positions.book_confirmed_entries()
        report = await self.positions.run_all(correlation_id=context.correlation_id)
        if self.gateway_positions is not None:
            await self.gateway_positions.apply(
                {item.position_id: item.action for item in report.reviews if not item.closed}
            )

    async def recovery_job(self, context: JobContext) -> None:
        await self.recovery.reconcile()
        self.alerts.flush()

    async def candidate_job(self, context: JobContext) -> None:
        self.keeper.ensure_open()
        # The first cycle of a UTC day hands the day before to the promotion evidence.
        self.evidence.close_finished_days()
        if self.gateway_entries is not None:
            # Approved and interrupted entries first, under this job's lease.
            await self.gateway_entries.run()
        snapshots = await self.repositories.history.latest_universe()
        held = await self._open_position_pools()
        addresses = [
            snapshot.pool.address.root
            for snapshot in snapshots
            if snapshot.pool.address.root not in held
        ]
        self.metrics.queue_depth("candidate_pipeline", len(addresses))
        decided_at = self.clock.now()
        last_sync = max((snapshot.observed_at for snapshot in snapshots), default=None)
        self.evidence.cycle(
            decided_at,
            fresh=last_sync is not None
            and decided_at - last_sync <= self.settings.universe_interval * 2,
            mode=self.repositories.mode.mode_state().mode,
        )
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
            # Each snapshot's short window comes from its predecessor's counters (D07).
            previous=self.repositories.history,
            short_window=self.policy.short_window,
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
        await self._refresh_gateway_readiness()
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
        if self.gmgn is not None:
            self.metrics.provider_state("gmgn", self.gmgn.health)

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

    async def _refresh_gateway_readiness(self) -> None:
        wiring = self._gateway
        if wiring is None:
            return
        now = self.clock.now()
        checked = self._gateway_checked_at
        if checked is not None and now - checked < GATEWAY_READINESS_INTERVAL:
            return
        self._gateway_checked_at = now
        ready = False
        if wiring.readiness is not None:
            try:
                ready = await wiring.readiness.ready(wiring.allowed_mode)
            except Exception as error:
                self._log.warning("gateway_readiness_failed", **error_fields(error))
        self.health.set_execution_gateway(ready)

    async def _open_position_pools(self) -> set[str]:
        open_ids = set(self.repositories.mode.open_positions())
        return {
            context.pool_address
            for context in await self.repositories.positions.all()
            if context.position_id in open_ids
        }

    def close(self) -> None:
        self.repositories.close()


def build_gateway(
    config: AppConfig,
    environ: Mapping[str, str],
    repositories: Repositories,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[GatewayWiring | None, httpx.AsyncClient | None]:
    """The HTTP gateway for ``TRADING_MODE``, or ``None``: SHADOW never gets one."""

    mode = config.requested_mode
    if mode is TradingMode.SHADOW:
        return None, None
    log = get_logger("worker.runtime")
    settings = ExecutorSettings.from_environment(environ)
    token = config.executor_internal_token
    if settings.url is None or token is None:
        missing = [
            name
            for name, value in (("EXECUTOR_URL", settings.url), ("EXECUTOR_INTERNAL_TOKEN", token))
            if value is None
        ]
        log.warning("execution_gateway_not_configured", mode=mode.value, missing=missing)
        return None, None
    if repositories.backend is not PersistenceBackend.POSTGRESQL:
        log.warning(
            "execution_gateway_refused", mode=mode.value, reason="DURABLE_EXECUTION_STORE_REQUIRED"
        )
        return None, None
    client = httpx.AsyncClient(
        base_url=settings.url, timeout=settings.timeout.total_seconds(), transport=transport
    )
    gateway = HttpExecutionGateway(
        client,
        internal_token=token.get_secret_value(),
        mode_token=None if settings.mode_token is None else settings.mode_token.get_secret_value(),
    )
    secret = settings.approval_secret
    wiring = GatewayWiring(
        gateway=gateway,
        allowed_mode=mode,
        receipts=gateway,
        readiness=gateway,
        approval_verifier=None
        if secret is None
        else HmacApprovalVerifier(secret.get_secret_value().encode("utf-8")),
        live_promotion=settings.live_promotion() if mode is TradingMode.LIVE else None,
    )
    return wiring, client


def build_runtime(
    config: AppConfig,
    environ: Mapping[str, str],
    *,
    clock: Clock | None = None,
    executor_transport: httpx.AsyncBaseTransport | None = None,
    market_transport: httpx.AsyncBaseTransport | None = None,
    rpc_transport: httpx.AsyncBaseTransport | None = None,
    gmgn_transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[WorkerRuntime, list[httpx.AsyncClient]]:
    # The rule bundle (RULESET_VERSION, RULESET_OVERRIDES_FILE) is resolved first: an
    # unknown or invalid ruleset stops the start before any store or client is opened.
    policy = load_pipeline_policy(environ)
    clock = clock or SystemClock()
    settings = WorkerSettings.from_environment(environ)
    metrics = WorkerMetrics()
    repositories = open_repositories(config, environ=environ, clock=clock)
    clients: list[httpx.AsyncClient] = []
    onchain: OnChainProvider = UnavailableOnChainProvider(clock)
    pool_states: PoolStateReader | None = None
    if config.solana_rpc_url:
        from src.providers.resilience import ResilientHttpClient
        from src.providers.solana import SolanaRpcProvider

        client = httpx.AsyncClient(base_url=config.solana_rpc_url, transport=rpc_transport)
        clients.append(client)
        rpc = SolanaRpcProvider(ResilientHttpClient(client), clock=clock)
        pool_states = rpc
        onchain = InstrumentedOnChainProvider(rpc, metrics, name="solana")
    market: MarketDataProvider | None = None
    warmup: VolumeHistorySource | None = None
    if config.meteora_data_api_base_url:
        from src.providers.meteora import MeteoraDataApiProvider
        from src.providers.resilience import ResilientHttpClient

        client = httpx.AsyncClient(
            base_url=config.meteora_data_api_base_url, transport=market_transport
        )
        clients.append(client)
        data_api = MeteoraDataApiProvider(
            ResilientHttpClient(client),
            clock=clock,
            filter_by=universe_filter(environ),
            pool_states=pool_states,
        )
        warmup = data_api
        market = InstrumentedMarketDataProvider(data_api, metrics, name="meteora")
    gateway, executor_client = build_gateway(
        config, environ, repositories, transport=executor_transport
    )
    if executor_client is not None:
        clients.append(executor_client)
    quotes, quote_client = build_quotes(config, environ, transport=executor_transport)
    if quote_client is not None:
        clients.append(quote_client)
    gmgn, gmgn_client = build_gmgn(environ, clock=clock, transport=gmgn_transport)
    if gmgn_client is not None:
        clients.append(gmgn_client)
    runtime = WorkerRuntime(
        settings=settings,
        policy=policy,
        repositories=repositories,
        market=market,
        onchain=onchain,
        clock=clock,
        metrics=metrics,
        gateway=gateway,
        wallet_address=config.wallet_public_key,
        pool_states=pool_states,
        quotes=quotes,
        warmup=warmup,
        gmgn=gmgn,
    )
    return runtime, clients


def universe_filter(environ: Mapping[str, str]) -> str | None:
    """``METEORA_UNIVERSE_FILTER``: the API-side narrowing of the universe (D07).

    Unset or empty: pools that traded in the last 24 hours. ``all``: the full sweep
    without a filter, chosen explicitly. Anything else is passed as ``filter_by``.
    """

    from src.providers.meteora import DEFAULT_UNIVERSE_FILTER

    raw = (environ.get(UNIVERSE_FILTER_ENV) or "").strip()
    if not raw:
        return DEFAULT_UNIVERSE_FILTER
    if raw.lower() == FULL_SWEEP:
        return None
    return raw


def build_quotes(
    config: AppConfig,
    environ: Mapping[str, str],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[QuoteSource | None, httpx.AsyncClient | None]:
    """The read-only quote client in every mode, SHADOW included: no wallet, no mode token.

    ``EXECUTOR_QUOTES_URL`` (the wallet-less ``executor-quotes`` service in compose), else
    ``EXECUTOR_URL``. Without a URL or the internal token there are no quotes and
    candidates are flagged ``DEPTH_UNAVAILABLE`` (SHADOW goes on conservatively,
    APPROVAL/LIVE closed).
    """

    settings = ExecutorSettings.from_environment(environ)
    url = environ.get(QUOTES_URL_ENV) or settings.url
    token = config.executor_internal_token
    if url is None or token is None:
        get_logger("worker.runtime").warning(
            "executor_quotes_not_configured",
            missing=[
                name
                for name, value in (
                    (QUOTES_URL_ENV, url),
                    ("EXECUTOR_INTERNAL_TOKEN", token),
                )
                if value is None
            ],
        )
        return None, None
    client = httpx.AsyncClient(
        base_url=url, timeout=settings.timeout.total_seconds(), transport=transport
    )
    return HttpQuoteClient(client, internal_token=token.get_secret_value()), client


def build_gmgn(
    environ: Mapping[str, str],
    *,
    clock: Clock,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[GmgnProvider | None, httpx.AsyncClient | None]:
    """The client of the internal ``gmgn`` service (compose profile ``gmgn``), or ``None``.

    ``GMGN_URL`` (e.g. ``http://gmgn:8080``) switches it on; ``GMGN_TIMEOUT_SECONDS``
    bounds one answer of the service, ``GMGN_STALE_AFTER_SECONDS`` the age of fresh data.
    """

    url = (environ.get(GMGN_URL_ENV) or "").strip()
    if not url:
        return None, None
    timeout = _seconds(environ, GMGN_TIMEOUT_ENV, 20)
    client = httpx.AsyncClient(base_url=url, transport=transport)
    provider = GmgnProvider(
        client,
        clock=clock,
        timeout=timeout.total_seconds(),
        stale_after=_seconds(environ, GMGN_STALE_AFTER_ENV, 900),
    )
    return provider, client


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
