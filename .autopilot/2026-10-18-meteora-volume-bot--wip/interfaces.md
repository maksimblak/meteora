# Интерфейсы проекта

## Правила проекта

- Доменная логика не импортирует FastAPI, SQLAlchemy, Meteora SDK или конкретного RPC-провайдера.
- Денежные значения — integer base units или `Decimal`; float запрещён на границах домена.
- Все timestamps — timezone-aware UTC; пользовательский интерфейс может форматировать их в локальный часовой пояс.
- Любая команда, способная изменить on-chain состояние, имеет `command_id`, `idempotency_key`, `expires_at`, `mode`, `ruleset_version` и `correlation_id`.
- Ошибка или неизвестное значение обязательного live risk-сигнала закрывает вход (`fail-closed`).
- Секретный материал существует только внутри execution gateway или secret provider и не входит в DTO, события или логи.
- Понижение режима и kill switch всегда доступны; повышение режима проходит promotion policy.

## Среда сборки

- Python 3.12, FastAPI 0.1xx/0.2xx stable line resolved by `uv`, Pydantic 2, SQLAlchemy 2, Alembic, asyncpg, httpx, tenacity, structlog, Prometheus client, Jinja2, PyYAML and orjson. Exact transitive versions are frozen in `uv.lock` by ticket 01.
- Node.js 22 LTS, TypeScript 5, Fastify 5, Zod, official `@meteora-ag/dlmm`, `@solana/web3.js`, pino and Vitest. Exact versions are frozen in `executor/package-lock.json` by ticket 01 after compatibility resolution.
- PostgreSQL 16 and Docker Compose v2.
- Install: `uv sync --frozen --all-extras` for Python and `npm ci --prefix executor` for TypeScript after lockfiles exist.
- Canonical full check: `docker compose --profile test run --rm check` — migrations, Python lint/types/tests and executor lint/types/tests in one command.
- Single Python test: `uv run pytest <test-file> -q`. Single executor test: `npm test --prefix executor -- <test-file>`.
- Local runtime: `docker compose up --build`; shadow-only profile must not require wallet secrets.
- Ticket 01 owns dependency installation and lockfiles. Later tickets may not add dependencies; a missing dependency is reported as `BLOCKED` for an orchestrated repair.
- Executors must not edit `.autopilot/`, `AGENTS.md`, `CLAUDE.md`, the original research report, or files outside this repository. They must not create or copy anything into the Desktop delivery path; final packaging is orchestrator-owned.
- No live transaction may be sent by tests, development commands, acceptance checks or default startup.

## Границы, решённые в спецификации

### `domain.market`

**Владеет:** типами `PoolRef`, `PoolSnapshot`, `TokenRiskSnapshot`, `FeatureVector`, `Freshness`, `DataQuality`.

**Открывает:**

- `MarketDataProvider.list_pools(cursor: str | None) -> PoolPage`
- `MarketDataProvider.get_pool_snapshot(pool: PoolRef, at: datetime) -> PoolSnapshot`
- `OnChainProvider.get_token_risk(mint: PublicKey) -> TokenRiskSnapshot`
- `OnChainProvider.get_wallet_state(wallet: PublicKey) -> WalletState`
- `FeatureEngine.compute(history: Sequence[PoolSnapshot], rules: FeatureRules) -> FeatureVector`

**Скрывает:** HTTP endpoints, provider payloads, pagination quirks, retry policy and caching.

### `domain.screening`

**Владеет:** hard filters, volume anomaly rules, score composition and rejection taxonomy.

**Открывает:**

- `PoolScreener.evaluate(pool: PoolSnapshot, token: TokenRiskSnapshot, features: FeatureVector, policy: ScreeningPolicy) -> CandidateDecision`
- `CandidateDecision(status, score, reasons, missing_signals, evidence, ruleset_version)`

**Скрывает:** weights, threshold implementation and provider-specific signal mapping.

### `domain.strategy`

**Владеет:** strategy selection, bin range, holding horizon, invalidation and exit intent.

**Открывает:**

- `StrategyPlanner.plan(candidate: CandidateDecision, market: PoolSnapshot, portfolio: PortfolioState, policy: StrategyPolicy) -> StrategyPlan`
- `PositionMonitor.evaluate(position: PositionState, market: PoolSnapshot, risk: RiskState) -> PositionAction`

**Скрывает:** formulas used to convert volatility to bins and Spot/Bid-Ask heuristics.

### `domain.risk`

**Владеет:** 10% allocation cap, 1% risk cap, three-position cap, 4% daily stop, kill switch, liquidity cap and promotion rules.

**Открывает:**

- `PositionSizer.size(plan: StrategyPlan, portfolio: PortfolioState, liquidity: LiquidityState, policy: RiskPolicy) -> SizedPlan | RiskRejection`
- `RiskEngine.pre_trade(plan: SizedPlan, state: RiskState) -> RiskDecision`
- `RiskEngine.continuous(positions: Sequence[PositionState], state: RiskState) -> Sequence[RiskAction]`
- `PromotionPolicy.evaluate(from_mode: TradingMode, evidence: PromotionEvidence) -> PromotionDecision`

**Скрывает:** limit aggregation and conservative-loss calculation details.

### `application.pipeline`

**Владеет:** ordering of discovery → snapshots → features → screening → planning → sizing → shadow/approval/live routing.

**Открывает:**

- `UniverseSync.run(sync_id: UUID) -> SyncResult`
- `CandidatePipeline.evaluate(pool_id: PoolId, observed_at: datetime) -> CandidateDecisionId`
- `PositionCycle.run(position_id: PositionId, observed_at: datetime) -> PositionActionId`
- `RecoveryService.reconcile() -> RecoveryReport`

**Скрывает:** transaction boundaries, leases, batching and retry scheduling.

### `application.execution`

**Владеет:** execution state machine, approval TTL, simulation freshness, reconciliation and idempotency.

**Открывает:**

- `ExecutionService.prepare(plan_id: UUID) -> ExecutionPreview`
- `ExecutionService.approve(command_id: UUID, approval: OperatorApproval) -> ExecutionState`
- `ExecutionService.execute(command_id: UUID) -> ExecutionReceipt`
- `ExecutionService.reconcile(command_id: UUID) -> ExecutionState`
- `ExecutionGateway.simulate(command: DlmmCommand) -> SimulationResult`
- `ExecutionGateway.submit(command: DlmmCommand, idempotency_key: str) -> GatewayReceipt`
- `ExecutionGateway.status(signature: str) -> ChainReceipt`

**Скрывает:** SDK objects, blockhash renewal, transaction encoding, Jito/RPC broadcast and wallet material.

### `application.accounting`

**Владеет:** shadow fills, fees, IL/divergence loss, network costs, realized/unrealized P&L and HODL benchmark.

**Открывает:**

- `ShadowBroker.apply(command: DlmmCommand, market: PoolSnapshot, cost_model: CostModel) -> ShadowReceipt`
- `AccountingService.mark(position: PositionState, market: PoolSnapshot) -> PositionValuation`
- `AccountingService.close(position: PositionState, receipt: ExecutionReceipt) -> PositionOutcome`
- `PerformanceService.summary(window: TimeWindow) -> PerformanceSummary`

**Скрывает:** ledger postings and valuation formulas.

### `ports.persistence`

**Владеет:** repository contracts and unit-of-work boundary.

**Открывает:**

- `UnitOfWork` with repositories for pools, snapshots, decisions, plans, positions, executions, modes, risk days and alerts
- `LeaseRepository.try_acquire(name: str, owner: str, ttl: timedelta) -> Lease | None`
- `AuditRepository.append(event: AuditEvent) -> None`

**Скрывает:** SQLAlchemy models, migrations, indexes and PostgreSQL locks.

### `adapters.providers`

**Владеет:** Meteora Data API, Solana RPC and optional enhanced transaction provider implementations.

**Открывает:** только интерфейсы из `domain.market` и normalized provider health.

**Скрывает:** API keys, URLs, raw JSON, rate limits, retries and circuit breakers.

### `executor` (TypeScript service)

**Владеет:** official Meteora DLMM SDK integration, transaction construction, simulation, submission and status lookup.

**Открывает во внутренней сети:**

- `POST /v1/simulations`
- `POST /v1/commands` with `Idempotency-Key`
- `GET /v1/commands/{id}`
- `GET /health/live`
- `GET /health/ready`

Payloads implement versioned `DlmmCommandV1`, `SimulationResultV1`, `GatewayReceiptV1`; unknown versions are rejected.

**Скрывает:** wallet loading, SDK clients, serialized transactions, Jito tips and RPC failover.

### `api`

**Владеет:** FastAPI HTTP/web contracts, operator authentication, CSRF and presentation models.

**Открывает:**

- `GET /api/v1/system/status`
- `GET /api/v1/pools`, `GET /api/v1/pools/{id}`
- `GET /api/v1/decisions`, `GET /api/v1/decisions/{id}`
- `GET /api/v1/positions`, `GET /api/v1/positions/{id}`
- `GET /api/v1/performance`
- `GET /api/v1/config/effective` (redacted)
- `POST /api/v1/approvals/{command_id}`
- `POST /api/v1/mode/transitions`
- `POST /api/v1/kill-switch/activate`
- `POST /api/v1/kill-switch/release`
- `GET /health/live`, `GET /health/ready`, `GET /metrics`
- web routes `/`, `/pools`, `/decisions`, `/positions`, `/performance`, `/settings`

**Скрывает:** database entities, provider payloads and secret-bearing configuration.

## События

Typed outbox events: `PoolSnapshotRecorded`, `CandidateEvaluated`, `StrategyPlanned`, `RiskRejected`, `ApprovalRequested`, `CommandSimulated`, `CommandSubmitted`, `CommandConfirmed`, `PositionOpened`, `PositionActionProposed`, `PositionClosed`, `DailyStopTriggered`, `ModeChanged`, `CriticalAlertRaised`.

Каждое событие содержит `event_id`, `occurred_at`, `aggregate_id`, `correlation_id`, `causation_id`, `schema_version`. Публикация наружу опциональна; запись в transactional outbox обязательна.

## Тестовые швы

- `FakeClock` — прогрев, TTL approval, risk day и backoff без ожидания реального времени.
- `FixtureMarketDataProvider` — фиксированные snapshots для spike/filter/strategy tests.
- `FixtureOnChainProvider` — mint authority, balances и provider degradation.
- `InMemoryExecutionGateway` — simulation, failure, unknown receipt и idempotency без сети.
- PostgreSQL integration fixture — migrations, locks, leases, outbox и restart recovery.
- Executor contract fixtures — общий JSON corpus проверяется Python и TypeScript сторонами.
- Golden decision records — одинаковые входы и ruleset дают одинаковые reasons/score/plan.

