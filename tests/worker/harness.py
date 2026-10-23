"""Shared in-memory wiring of the money pipeline for worker and recovery tests."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

from prometheus_client import CollectorRegistry

from src.accounting import InMemoryAccountingRepository
from src.core.clock import FrozenClock
from src.domain.models.primitives import PublicKey
from src.domain.models.trading import TradingMode
from src.market.models import (
    AuthorityStatus,
    DataQuality,
    Freshness,
    PoolRef,
    PoolSnapshot,
    RiskSignal,
    TokenRiskSnapshot,
    WindowMetrics,
)
from src.market.universe import InMemoryUniverseStore
from src.modes import InMemoryModeRepository
from src.observability.alerts import CriticalAlertReporter
from src.observability.metrics import WorkerMetrics
from src.persistence.factory import PersistenceBackend, Repositories
from src.persistence.memory import (
    InMemoryAuditLog,
    InMemoryDecisionStore,
    InMemoryJobLeases,
    InMemoryPositionContexts,
    InMemoryRecoverableExecutionStore,
    InMemorySnapshotHistory,
)
from src.providers.fixtures import FixtureFailure, FixtureOnChainProvider
from src.worker.execution import EntryExecutor, ShadowSubmitter
from src.worker.pipeline import CandidatePipeline
from src.worker.policy import PipelinePolicy, hypothesis_policy
from src.worker.portfolio import RiskDayKeeper, ShadowPortfolio
from src.worker.positions import PositionCycle

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
POOL = PoolRef(
    address=PublicKey("SysvarRent111111111111111111111111111111111"),
    base_mint=PublicKey("11111111111111111111111111111111"),
    quote_mint=PublicKey("So11111111111111111111111111111111111111112"),
)
WALLET = "SysvarC1ock11111111111111111111111111111111"
EQUITY = 1_000_000


def snapshot(
    at: datetime,
    *,
    short: str = "100",
    long: str = "600",
    tvl: str = "1000",
    depth: str = "500",
    price: str = "1",
) -> PoolSnapshot:
    metrics = {
        "5m": WindowMetrics(
            volume=Decimal(short), fees=Decimal("2"), transaction_count=100, unique_participants=20
        ),
        "30m": WindowMetrics(volume=Decimal(long), fees=Decimal("20")),
        "1h": WindowMetrics(volume=Decimal(long) * 2, fees=Decimal("40")),
        "4h": WindowMetrics(volume=Decimal(long) * 8, fees=Decimal("160")),
        "24h": WindowMetrics(volume=Decimal(long) * 48, fees=Decimal("960")),
    }
    return PoolSnapshot(
        pool=POOL,
        windows=metrics,
        tvl=Decimal(tvl),
        depth=Decimal(depth),
        price=Decimal(price),
        bin_step=25,
        created_at=NOW - timedelta(days=7),
        observed_at=at,
        source_at=at,
        ingested_at=at,
        freshness=Freshness.FRESH,
        data_quality=DataQuality.COMPLETE,
    )


def spike_history(**spike: str) -> list[PoolSnapshot]:
    """Six calm 5-minute observations and a volume spike at ``NOW`` (an ACCEPT)."""

    calm = [snapshot(NOW - timedelta(minutes=35 - index * 5)) for index in range(6)]
    return [*calm, snapshot(NOW, short="400", long="1200", **spike)]


def safe_token(at: datetime = NOW) -> TokenRiskSnapshot:
    return TokenRiskSnapshot(
        mint=POOL.base_mint,
        mint_authority=AuthorityStatus.REVOKED,
        freeze_authority=AuthorityStatus.REVOKED,
        blacklisted=RiskSignal.SAFE,
        verified=RiskSignal.SAFE,
        holder_concentration=Decimal("0.20"),
        wash_trading=RiskSignal.SAFE,
        observed_at=at,
        source_at=at,
        ingested_at=at,
        data_quality=DataQuality.COMPLETE,
    )


def harness_policy() -> PipelinePolicy:
    return hypothesis_policy(
        ruleset_version="rules-1",
        config_version="config-1",
        minimum_tvl=Decimal("1000"),
        minimum_depth=Decimal("500"),
        minimum_pool_age=timedelta(days=7),
        maximum_holder_concentration=Decimal("0.20"),
        maximum_price_impact=Decimal("0.02"),
        minimum_short_ratio=Decimal("4"),
        minimum_long_ratio=Decimal("2"),
        minimum_robust_z_score=Decimal("10"),
        minimum_fee_tvl=Decimal("0.02"),
        minimum_score=Decimal("0"),
        baseline_points=6,
        minimum_baseline_points=6,
        maximum_history_points=20,
        entry_notional_quote=Decimal("10"),
        quote_base_units_per_unit=Decimal(1_000_000),
    )


def memory_repositories(clock: FrozenClock) -> Repositories:
    """Every store of the worker in memory; a test models a restart by reusing it."""

    universe = InMemoryUniverseStore()
    return Repositories(
        backend=PersistenceBackend.SQLITE,
        mode=InMemoryModeRepository(),
        accounting=InMemoryAccountingRepository(),
        execution=InMemoryRecoverableExecutionStore(),
        leases=InMemoryJobLeases(clock),
        audit=InMemoryAuditLog(),
        universe=universe,
        history=InMemorySnapshotHistory(universe),
        decisions=InMemoryDecisionStore(),
        positions=InMemoryPositionContexts(),
    )


@dataclass
class Harness:
    clock: FrozenClock = field(default_factory=lambda: FrozenClock(NOW))
    modes: Any = field(default_factory=InMemoryModeRepository)
    accounting: Any = field(default_factory=InMemoryAccountingRepository)
    universe: InMemoryUniverseStore = field(default_factory=InMemoryUniverseStore)
    decisions: InMemoryDecisionStore = field(default_factory=InMemoryDecisionStore)
    audit: InMemoryAuditLog = field(default_factory=InMemoryAuditLog)
    contexts: InMemoryPositionContexts = field(default_factory=InMemoryPositionContexts)
    registry: CollectorRegistry = field(default_factory=CollectorRegistry)
    token_failure: FixtureFailure | None = None
    policy: PipelinePolicy = field(default_factory=harness_policy)
    wrap_shadow_submitter: Callable[[ShadowSubmitter], Any] | None = None
    token_risks: dict[str, TokenRiskSnapshot] | None = None
    quotes: Any = None
    warmup: Any = None

    def __post_init__(self) -> None:
        shadow = ShadowSubmitter(self.accounting, self.clock, self.policy.shadow_costs)
        submitter = (
            shadow if self.wrap_shadow_submitter is None else self.wrap_shadow_submitter(shadow)
        )
        self.metrics = WorkerMetrics(self.registry)
        self.history = InMemorySnapshotHistory(self.universe)
        self.onchain = FixtureOnChainProvider(
            token_risks=self.token_risks or {POOL.base_mint.root: safe_token()},
            failure=self.token_failure,
            clock=self.clock,
        )
        self.alerts = CriticalAlertReporter(self.modes, self.clock, self.metrics)
        self.portfolio = ShadowPortfolio(EQUITY, self.accounting, self.modes)
        self.keeper = RiskDayKeeper(self.modes, self.portfolio, self.clock, self.policy.risk)
        self.entries = EntryExecutor(
            modes=self.modes,
            audit=self.audit,
            submitters={TradingMode.SHADOW: submitter},
            contexts=self.contexts,
            alerts=self.alerts,
            clock=self.clock,
            metrics=self.metrics,
            policy=self.policy,
            wallet_address=WALLET,
            conservative_shadow=ShadowSubmitter(
                self.accounting, self.clock, self.policy.conservative_costs
            ),
        )
        self.pipeline = CandidatePipeline(
            history=self.history,
            onchain=self.onchain,
            decisions=self.decisions,
            modes=self.modes,
            entries=self.entries,
            portfolio=self.portfolio,
            audit=self.audit,
            alerts=self.alerts,
            policy=self.policy,
            clock=self.clock,
            metrics=self.metrics,
            quotes=self.quotes,
            warmup=self.warmup,
        )
        self.positions = PositionCycle(
            modes=self.modes,
            accounting=self.accounting,
            contexts=self.contexts,
            history=self.history,
            onchain=self.onchain,
            keeper=self.keeper,
            portfolio=self.portfolio,
            audit=self.audit,
            alerts=self.alerts,
            policy=self.policy,
            clock=self.clock,
            metrics=self.metrics,
            decisions=self.decisions,
        )

    def ingest(self, snapshots: list[PoolSnapshot]) -> None:
        async def stage_all() -> None:
            for item in snapshots:
                stage = await self.universe.begin(uuid4(), item.observed_at)
                await stage.stage((item,))
                await stage.commit()

        asyncio.run(stage_all())

    def event_types(self) -> list[str]:
        return [event.event_type.value for event in self.audit.events]
