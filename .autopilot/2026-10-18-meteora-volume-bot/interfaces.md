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

## Реализованные интерфейсы — T01

- `create_app(*, config: AppConfig | None, readiness_probe: ReadinessProbe | None) -> FastAPI`
- `GET /health/live`, `GET /health/ready`, `GET /metrics`
- `AppConfig.load(yaml_path, environ)`, `AppConfig.redacted()`, `AppConfig.database_dsn()`
- `UnitOfWork` — async context manager
- `LeaseRepository.try_acquire`, `AuditRepository.append`, `OutboxRepository.append`

## Реализованные интерфейсы — T02

- `FeatureEngine.compute(history, rules) -> FeatureVector`
- `FeatureEngine.update(snapshot, rules) -> FeatureVector`
- `assess_market_quality(snapshot, features, policy) -> MarketQualityAssessment`
- `assess_hard_filters(token, features, policy) -> HardFilterAssessment`
- `UniverseSync(..., batch_size)` — курсорная пакетная синхронизация с ограниченной конкурентностью
- `UniverseSnapshotStore.begin(sync_id, observed_at) -> UniverseSnapshotStage`
- `UniverseSnapshotStage.stage(batch)`, `commit() -> duplicate_count`, `abort()`
- `MarketDataProvider.health`, `OnChainProvider.health`
- `ProviderHealth`, `ProviderState`, `EnhancedTokenSignals(source_at, freshness)`
- `FixtureMarketDataProvider`, `FixtureOnChainProvider`, `FixtureFailure`
- `ResilientHttpClient.get_json(...)`, `ResilientHttpClient.post_json(payload)`

## Реализованные интерфейсы — T03

- `VolumeFeatureEngine.compute`
- `PoolScreener.evaluate`, `assess_volume_integrity`
- `StrategyPlanner.plan`, `PositionMonitor.evaluate`
- `PositionSizer.size`
- `RiskEngine.pre_trade`, `RiskEngine.continuous`
- `PromotionPolicy.evaluate`

## Реализованные интерфейсы — T05

- `DlmmCommandV1`, `SimulationResultV1`, `GatewayReceiptV1`, `ChainReceiptV1`
- canonical command/simulation digests для exact-payload binding
- signed one-shot approval proof с TTL и durable claim
- `LivePromotionEvidence` — 10 approval cycles, reconciled receipts, risk/RPC/wallet/checklist/operator gate
- durable execution stores для approvals, receipts и idempotency/restart recovery
- multi-signature receipts с сохранением partial broadcast progress
- `ExecutionEngine.simulate`, `ExecutionEngine.submit`, `ExecutionEngine.status`, `ExecutionEngine.get`
- `buildRuntimeConfig`
- internal `POST /v1/simulations`, `POST /v1/commands`, `GET /v1/commands/{id}`
- `ExecutionService.prepare`, `ExecutionService.approve`, `ExecutionService.execute`, `ExecutionService.reconcile`
- `ExecutionStore`, `ExecutionGateway`

## Реализованные интерфейсы — T04

- `ShadowBroker.apply(command, market, cost_model) -> ShadowReceipt(phase=CONFIRMED, gateway_receipt: GatewayReceiptV1, fill_digest, event_types)`
- `AccountingService.mark`, `value_at`, `apply_fill(current, command, Fill) -> LifecycleStep`, `close`
- `PerformanceService.summary(window)`, `daily_observation(day, *, ...) -> DailyPromotionObservation`
- `AccountingRepository.commit(receipt, entries, outcome, events, *, expected_version)`, `ConcurrentPositionUpdate`
- `SqliteAccountingRepository(path)`, `InMemoryAccountingRepository()` — тот же код на `:memory:`; модуль `src/accounting/repository.py`, `src/portfolio/sqlite.py`
- `ModeService(repo, clock, ruleset_version)`: `transition`, `shadow_readiness`, `activate_kill_switch`, `release_kill_switch`, `resolve_alert`
- `ApprovalService.issue`, `ApprovalService.consume`
- `DailyRiskService.record`, `entries_allowed`, `risk_state`
- `EntryGate(repo, clock, RiskPolicy).admit(sized_plan, plan_id) -> EntryDecision`
- `PromotionEvidenceService.record_daily`, `build_shadow`
- `ModeRepository`: `commit_transition(t, *, expected_version, shadow_evidence_id, live_promotion_id)`, `record_risk_day`, `admit_entry`/`claim_entry`/`confirm_entry`/`fail_entry`/`cancel_plan`/`close_position`, `activate_kill_switch`, `release_kill_switch`, `raise_alert`/`resolve_alert`
- `SqliteModeRepository(path)`, `InMemoryModeRepository()`
- outbox (`AuditEvent`): `CommandConfirmed`, `PositionOpened`, `PositionClosed`, `ModeChanged`, `DailyStopTriggered`, `CriticalAlertRaised`
- durable-адаптер учёта и режимов — SQLite; отображение на PostgreSQL в T04 не входило

## Реализованные интерфейсы — T06

- `create_operator_app(*, config, environ, repositories, clock, policy, settings, approvals, catalog, probes) -> FastAPI`; ASGI-вход `src.api.main:app` (Dockerfile CMD и compose `api`)
- `build_approval_setup(...) -> ApprovalSetup(wiring, unavailable, client)`; `ApprovalWiring(service, approval_secret, simulation_ttl, readiness)`; `OperatorSettings.from_environment(config, environ)`; `DecisionCatalog.page/plan_for/latest_by_pool/latest_outcomes`; `WorkerHealthProbe`, `GatewayProbe`
- маршруты границы `api` плюс `GET /approvals`, `/approvals/{id}`, `/mode`, `/promotion`, `/promotion/evidence/{id}`, `/stream/status` (SSE `event: status`), `POST /promotion/evidence`; ошибки `{"error":{code,message,details}}`
- доступ: Bearer `APP_OPERATOR_TOKEN` или cookie `mvb_session` (HttpOnly, SameSite=Strict) + `X-CSRF-Token`/поле `csrf_token`; повторное подтверждение — `operator_token` + `confirm` (для approval — `confirm_irreversible`); аудит `ApprovalRequested(stage=approved|approval_rejected)` с отметками чеклиста
- env: `WORKER_HEALTH_URL`, `OPERATOR_APPROVAL_TTL_SECONDS`, `OPERATOR_SESSION_TTL_SECONDS`; compose `api`: `TRADING_MODE`, `EXECUTOR_URL`, `EXECUTOR_INTERNAL_TOKEN_FILE`, `EXECUTOR_APPROVAL_SECRET_FILE` пустые по умолчанию; `worker`: `WORKER_METRICS_HOST=0.0.0.0`, `expose: 9100` без `ports`
- веб: `/`, `/pools`, `/decisions`, `/positions`, `/performance`, `/settings`, `/approvals`, `/mode`, `/login`; шаблоны и статика в `src/web/templates`, `src/web/static`; метрики `meteora_api_*`
- метрики GMGN панель узнаёт по именам evidence `gmgn`, `insider`, `bundl`, `phish`, `developer`, `dev_`

## Реализованные интерфейсы — T09

- `HttpExecutionGateway(client, *, internal_token, mode_token=None)`: `simulate`, `submit`, `lookup(command_id) -> GatewayReceiptV1 | None`, `status` (всегда `GatewayRejected`: executor не отдаёт статус по подписи, сверка через `GET /v1/commands/{id}`), `ready(mode) -> bool`; `ExecutorContractError`
- `ExecutionService(..., receipts: ReceiptLookup | None, kill_switch: KillSwitchProbe | None, kill_switch_policy)`, `get_state(command_id)`; kill switch перепроверяется перед `gateway.submit`; `ExecutionOutcomeUnknown(ExecutionBlocked)`, `GatewayRejected(code)`
- `ExecutorSettings.from_environment(environ)`, `.live_promotion()`; env `EXECUTOR_URL`, `EXECUTOR_MODE_TOKEN(_FILE)`, `EXECUTOR_APPROVAL_SECRET(_FILE)`, `EXECUTOR_TIMEOUT_SECONDS`, `LIVE_PROMOTION_FILE`, `EXECUTOR_INTEGRATION_URL/TOKEN`
- worker: `BinAnchor`, `AnchorSource`, `snapshot_anchor`, `build_entry_command(..., anchor=None)` (не-SHADOW без active bin и decimals → `EntryCommandUnavailable`/`ENTRY_ANCHOR_UNAVAILABLE`), `CommandResolver(store)`, `ModeKillSwitch`, `GatewayFillBook.book(command_id, price)`, `execute_command`, `GatewaySubmitter(service, resolver, book)`, `GatewayEntrySettler.run()`, `GatewayPositionManager.book_confirmed_entries()/apply(actions)`; ключ идемпотентности действий позиции `position-action:{position_id}:{n}`, не более 5 попыток
- runtime: `GatewayWiring`, `build_gateway(config, environ, repositories, *, transport)`, `WorkerRuntime(..., gateway=, anchors=)` (параметры `execution_service` и `plan_resolver` убраны), `build_runtime(..., executor_transport=)`
- план после `SUBMITTING` с `ExecutionBlocked` остаётся EXECUTING до сверки
- частично прошедший REMOVE (FAILED, `transaction_count > 1`, есть подпись) → `PositionActionStatus.OPERATOR_REQUIRED` и critical alert `POSITION_PARTIALLY_EXECUTED`; автоматических попыток по позиции больше нет, в том числе после рестарта (состояние из durable execution store); ручного пути оператора в worker пока нет
- политика воркера берётся из `load_pipeline_policy(environ)` один раз в `build_runtime`, до открытия хранилищ

## Реализованные интерфейсы — T14

- `derive_short_window(current, previous, ShortWindowRules) -> PoolSnapshot`; `CounterMark(value, at, low_since, low_last)`, `ShortWindowDerivation.volume_mark/fees_mark`; причины `NEGATIVE_DELTA`, `INTERVAL_TOO_LONG`, `COUNTER_RESET`; `UniverseSync(..., previous=, short_window=)`
- `PoolSnapshot`: `cumulative_volume/fees`, `short_window`, `pool_blacklisted`, `base/quote_token_verified`, `base_freeze_authority_disabled`; COMPLETE требует только длинные окна; `WindowMetrics.protocol_fees`, `.total_fees`; `VolumeBucket`, `WindowOrigin`
- `MeteoraDataApiProvider.volume_history(pool, *, start, end)` (settle 2m, кэш, общий лимит); `VolumeFeatureEngine.compute(..., warmup=())`; признаки `short_window_origin`, `baseline_origin`, `short_burst_spread`
- `PoolScreener.evaluate(..., depth: DepthEvidence | None)`; `CandidateDecision.depth`; `VolumeIntegrityMode`; `merge_pool_token_signals(token, pool)`; флаги `DEPTH_UNAVAILABLE`, `VOLUME_PARTICIPANTS_UNAVAILABLE`; коды `BLACKLISTED_POOL`
- стратегия: `BID_ASK_FROM_LOCAL_HIGH` = `(-below, 0)`, `range_side=BID_ONLY`, всё в quote-токене; `ExitPolicy`/`PositionState.upside_breach_grace`, `upside_breach_fraction`; `PositionSignals.above_range_since`; `PositionMonitor.above_range`; `PositionState.volume_decay_ratio` из плана
- worker: `PipelinePolicy.{short_window, active_bin_tolerance, conservative_costs, conservative_depth_fraction}`; guide-v1: `bid_only_upside_grace=30m`, `bid_only_upside_exit=0.05`, `short_window_counter_reset_after=30m`; `universe_interval(env)`, `observation_gap()`; `quote_leg(size, lower, upper)`; `EntryExecutor(..., pool_states, wallets, conservative_shadow)`; причины `ACTIVE_BIN_DRIFT`, `ACTIVE_BIN_UNAVAILABLE`, `POOL_STATE_MISMATCH`, `WALLET_INSUFFICIENT_*_TOKEN`
- `CandidatePipeline(..., quotes, warmup, evidence)`; `DailyEvidence` — `record_daily` в конце UTC-суток (счётчик дня в памяти процесса); `PositionCycle(..., decisions, evidence)`; `WorkerRuntime(..., pool_states, quotes, warmup)`; `build_quotes`; `universe_filter`
- env `METEORA_UNIVERSE_FILTER` (`all` — полный sweep), `EXECUTOR_QUOTES_URL`; compose-сервис `executor-quotes` (`TRADING_MODE=SHADOW`, без кошелька)

## Реализованные интерфейсы — T11

- `PoolSnapshot.active_bin_id`, `base_decimals`, `quote_decimals`, `short_window_source` (все `None` по умолчанию)
- `GuideFeatureRules.maximum_volume_window="30m"`; `VolumeSpikeFeatures.volume_window_source`
- `MeteoraDataApiProvider(client, *, clock, stale_after, pools_path, page_size=1000, filter_by=None, rate_limiter=2 rps, page_concurrency=4, pool_states=None, maximum_rejected_share=0.5)`, `.sweep -> SweepReport(pages, reported_total, listed, accepted, rejected, anchored, complete, duplicates, recovered)`; курсор — номер страницы строкой; повтор пула помечает страницу сдвинутой, страницы k-1 и k перечитываются, недобор против total → `ProviderPayloadError`, DEGRADED, sync FAILED
- `LbPairState(active_bin_id, bin_step, token_x_mint, token_y_mint)`, протокол `PoolStateReader`; `SolanaRpcProvider.get_pool_states(pools) -> dict[str, LbPairState]`, `get_mint_decimals(mints) -> dict[str, int]`, `DLMM_PROGRAM_ID`; раскладка LB pair: active_id i32@76, bin_step u16@80, mints @88/@120
- `UniverseSync.run` сначала листает все страницы, потом берёт `observed_at`
- ручной smoke: `python -m src.providers.meteora_smoke` (`--max-pages --filter-by --page-size --requests-per-second --timeout --rpc`)
- живой API: окна `30m, 1h, 2h, 4h, 12h, 24h` у `volume`, `fees`, `protocol_fees`, `fee_tvl_ratio`; `cumulative_metrics.{volume, fees}` (не монотонны на уровне шума); `/pools/{address}/volume/history?timeframe=5m`, `/pools/{address}/ohlcv`; глубины по бинам нет; `fees` без `protocol_fees`

## Реализованные интерфейсы — T10

- новые необязательные поля (по умолчанию `None`): `PoolSnapshot.base_fee_rate`, `base_token_market_cap`; `TokenRiskSnapshot.earliest_activity_at`, `activity_history_complete`, `total_fees_sol`; `EnhancedTokenSignals.total_fees_sol`
- `SolanaRpcProvider(..., analyze_holders=True, activity_pages=1, liquidity_programs, program_authorities)` — доля top-10 без pool vaults и известных программ, возраст mint
- `GuideFeatureRules`; `VolumeFeatureRules.guide`; `VolumeSpikeFeatures.volume_per_minute`, `volume_to_liquidity`, `fee_efficiency`, `local_high_price`, `all_time_high_price`
- `GuideScreeningRules`; `ScreeningPolicy.guide`; `assess_guide_thresholds(pool, token, features, policy) -> GuideAssessment`; `VolumeRegime`; `RejectionCode.MARKET_CAP_BELOW_THRESHOLD`, `TOKEN_TOO_YOUNG`, `VOLUME_PER_MINUTE_BELOW_THRESHOLD`, `VOLUME_TO_LIQUIDITY_BELOW_THRESHOLD`, `ARTIFICIAL_VOLUME_SUSPECTED`, `TOTAL_FEES_BELOW_THRESHOLD`, `GUIDE_SIGNAL_UNKNOWN`
- `GuideStrategyRules`; `StrategyPolicy.guide`; `StrategySelectionReason`; `StrategyPlan.selection_reason`, `selection_evidence` (со значениями по умолчанию)
- `src/worker/policy.py`: `DEFAULT_RULESET_VERSION="guide-v1"`, `LEGACY_RULESET_VERSION`, `GuidePolicySettings`, `guide_policy(settings, *, ruleset_version, config_version)`, `default_policy()`, `load_pipeline_policy(environ)` — env `RULESET_VERSION` и `RULESET_OVERRIDES_FILE` (YAML: `config_version` + пороги); `hypothesis_policy()` — прежняя `shadow-hypothesis-v1`
- `PositionMonitor` считает край диапазона, только если цена двигалась к нему
- `DecisionFlag.TOTAL_FEES_UNAVAILABLE`; `CandidateDecision.flags: tuple[DecisionFlag, ...] = ()`; `GuideAssessment.flags`; evidence `total_fees_sol=("UNAVAILABLE", "30")` — флаг не блокирует ACCEPT и не входит в `missing_signals` (D03); approval preview читает его из `CandidateDecision.flags`

## Реализованные интерфейсы — T07

- `PostgresModeRepository`, `PostgresAccountingRepository(PostgresDatabase, *, owns_database)` — протоколы T04 и их представления на общей PostgreSQL (миграция `0002_worker_state`)
- `PostgresDatabase(url, *, schema)`: `run(work, lock=)`, `run_async`, `try_advisory_lock`, `ping`, `close`
- `open_repositories(config, *, environ, clock, schema) -> Repositories(mode, accounting, execution, leases, audit, universe, history, decisions, positions; outbox_relay(sink, clock, owner), retention(), ping(), close())` — единственная фабрика репозиториев для API и worker
- протоколы T04 синхронные, а драйвер — asyncpg через мост-поток: из async-кода (FastAPI) вызывать через threadpool
- `JobLeases.acquire/renew/release -> JobLease(name, owner, token, acquired_at, expires_at)`; `AuditLog.append`; `EventSink.publish`; `PostgresOutboxRelay.publish_pending() -> RelayReport`; `PostgresRetention.purge(now)`
- `SnapshotHistory.history/latest_universe/latest_sync_at`; `DecisionStore.record_decision(DecisionRecord)/record_plan(PlanRecord)`; `PositionContextStore.put/get/all`; `PostgresExecutionStore` = `ExecutionStore` + `in_phases`
- `Scheduler(jobs, leases, owner, clock, max_concurrency).tick/drain/run_forever`; `JobSpec(name, interval, handler, priority, lease_ttl, timeout)`
- `CandidatePipeline.evaluate(pool_address, observed_at) -> CandidateOutcome(decision_id, status, plan_id, entry, reasons)`
- `PositionCycle.run(position_id, observed_at) -> PositionReview`, `run_all() -> PortfolioCycleReport`
- `RecoveryService(..., executor_lease: ExecutorLease | None, stale_after: timedelta).reconcile(*, startup=False) -> RecoveryReport`; `ExecutorLease(leases, job_name, owner, ttl)`; EXECUTING-план закрывается только под lease `candidate_pipeline`, при `startup=True` без lease или если claim старше `stale_after`, иначе `KEPT_EXECUTING`
- `EntryExecutor` требует `EntryStore` = `ModeRepository` + `entry_plan()`; guard перед submit и fill требует статус EXECUTING (`PLAN_NOT_EXECUTING`); fill, отклонённый `confirm_entry`, поднимает critical alert `ENTRY_FILL_UNTRACKED`
- константы worker runtime `CANDIDATE_JOB`, `CANDIDATE_LEASE_TTL`; scheduler берёт все leases тика до запуска его задач
- `HealthMonitor.liveness()`, `readiness(mode) -> READY|DEGRADED|NOT_READY + reasons`; worker отдаёт `/health/live`, `/health/ready`, `/metrics`; запуск `python -m src.worker`
- новые outbox-события: `CandidateEvaluated`, `StrategyPlanned`, `RiskRejected`, `CommandSubmitted(stage=authorized)`, `PositionActionProposed`
- Python-клиента HTTP к TS executor нет: входы APPROVAL/LIVE отменяются с `EXECUTION_GATEWAY_NOT_CONFIGURED`, выход не-SHADOW позиции только поднимает alert

## Реализованные интерфейсы — T16

- `GmgnOpenApiProvider(client, *, api_key: SecretStr, clock, sleep, chain="sol", plan=GmgnPlan.FREE, stale_after, cache_ttl, timeout, max_wait, failure_threshold, recovery_timeout, cooldown_ceiling).get_token_signals(mint) -> EnhancedTokenSignals(provider="gmgn-openapi")`, `.health`; модуль `src/gmgn/openapi.py`
- `OpenApiSettings.from_environment(environ) -> OpenApiSettings | None`; `GmgnPlan(free|plus|pro).bucket`; `openapi_client(settings, *, timeout, transport=None)`; `ipv4_transport()`; `OPENAPI_URL` (по умолчанию `https://openapi.gmgn.ai`), `API_KEY_HEADER="X-APIKEY"` + timestamp + client_id — по исходникам GMGNAI/gmgn-skills (`src/config.ts`, `src/client/OpenApiClient.ts`)
- `build_gmgn(...) -> (EnhancedSignalSource | None, AsyncClient | None)` в `src/worker/runtime.py`: ключ задан → OpenAPI (даже при `GMGN_URL`); нет ключа, есть `GMGN_URL` → браузерный сервис; ничего → как раньше. Ключ передаётся только в worker, не в api
- env: `GMGN_API_KEY` или `GMGN_API_KEY_FILE` (не обе), `GMGN_API_URL`, `GMGN_API_PLAN=free|plus|pro` (free), `GMGN_API_CACHE_SECONDS` (300); общие с браузерным путём `GMGN_TIMEOUT_SECONDS` (для API 10) и `GMGN_STALE_AFTER_SECONDS`
- лимитер — 80% тарифа (Free: 4 единицы/с; на кандидата 2 запроса — `token/info` и `token/security`); только IPv4 (по IPv6 GMGN отвечает 401/403)
- причины `gmgn_status=UNAVAILABLE:<код>`: `AUTH_*`/`HTTP_401`/`HTTP_403`/`PLAN_UPGRADE_REQUIRED` (15 мин без запросов, потом одна проверка), `RATE_LIMIT_*`/`RATE_LIMITED` (тишина до max(`X-RateLimit-Reset`, `reset_at`)+1 с, не дольше 15 мин), `RATE_LIMIT_LOCAL_QUEUE`, `API_TIMEOUT`, `API_UNAVAILABLE`, `API_PAYLOAD_INVALID`, `API_MINT_MISMATCH`, `CIRCUIT_OPEN`
- `total_fee` из `token/info` → `total_fees_sol` (единица SOL выведена по совпадению с браузерным путём, документация её не называет); `token/security` для sol лишь дозаполняет пропуски из info
- ручной smoke: `uv run --frozen python -m src.gmgn.smoke --mint <адрес> [--plan] [--timeout]`, ключ из env
- ключ создаётся на gmgn.ai/ai (форма просит Ed25519 public key; для чтения токенов нужен только API key)

## Реализованные интерфейсы — T08

- документация: `README.md`, `docs/runbook.md`, `docs/configuration.md`, `docs/security.md`, `docs/adr/0001…0004` (гибридная граница FastAPI/TS, fail-closed по risk-данным, координация через PostgreSQL, live выключен по умолчанию); `research/` — исследование побайтово + `research/README.md`
- `scripts/verify-delivery.ps1 -Path <папка> [-Source] [-ResearchSha256]` → exit 0/1; `scripts/copy-delivery.ps1 -Source -Destination [-NoVerify]` — в цели ничего не удаляет, в конце запускает verify; правила — `scripts/delivery-rules.ps1`
- `scripts/acceptance-smoke.ps1 [-Port 18080] [-Keep]` — стек `-p meteora-acceptance`, overlay `tests/acceptance/compose.acceptance.yml` с сервисом `fixture-api` (`FIXTURE_FREEZE_AFTER`); env `ACCEPTANCE_COMPOSE=1`, `ACCEPTANCE_API_PORT`, `ACCEPTANCE_KEEP`, `ACCEPTANCE_PROJECT`; в каноническом check эти тесты пропускаются
- `.dockerignore` в корне

## Реализованные интерфейсы — F1

- `ShortWindowRules(window, interval_tolerance=30s, maximum_interval, …)`; `.minimum_interval` и `.window_length` — свойства; окно 5m только из интервала ≥ 4m30, частые свипы суммируются; `CounterReading(value, at)`; `CounterMark.earlier`, `.reset_from` (переживают to_payload/from_payload); `GuidePolicySettings.short_window_interval_tolerance`
- `PoolSnapshot.quote_price_usd` (Meteora Data API, цена по quote-минту; 0/битое → None); `DepthEvidence.quote_price_usd`, detail `QUOTE_PRICE_UNKNOWN`; размер котировки = USD-эталон × 10^decimals / цена quote-токена; depth в USD; ликвидность в базовых единицах quote-токена; без цены — вход закрыт
- guide-v1: top-10 ровно 0.30 → `HOLDER_CONCENTRATION_HIGH`
- `shadow_fee_income(position, command, market, at) -> Decimal` = стоимость транша / TVL × LP-комиссии пула, только если цена на момент fill в диапазоне

## Реализованные интерфейсы — F2

- `mode_state.kill_switch_policy text NULL` (миграция 0003, CHECK, downgrade); `ModeState.kill_switch_policy`, `.active_kill_switch_policy` (без записи → HOLD_AND_ALERT); `KillSwitchProbe.kill_switch_policy()`; монитор позиций, `GatewayPositionManager`, `ExecutionService` и экран подтверждений читают политику из состояния режима; ORDERLY_EXIT → CLOSE каждой позиции со свежей симуляцией (в APPROVAL — через подтверждение)
- `ExecutionService(kill_switch_policy=)` принимается и игнорируется (остался ради src/api/app.py:85 — убирает F3)
- `entry_impact_rejection(command, simulation)` — одно правило impact входа для execution и экрана подтверждений (коды PRICE_IMPACT_UNAVAILABLE / SLIPPAGE_BREACH); `ENTRY_OPERATIONS`, `EXIT_OPERATIONS`
- `src/modes/alerts.py`: `alert_subject(kind, value)` (тег `[position:…]`/`[command:…]` в тексте алерта), `SystemAlertResolver(repo, clock, ruleset).resolve(codes, subject, reason, *, raised_before=None)` и `.resolve_except(code, kind, keep, reason)` — системное закрытие один раз, на первой проводке подтверждённого действия, только алертов, поднятых до неё
- `position_action_key` / `position_action_command_id(position_id, attempt, preview=1)` → `position-action:{id}:{n}:{k}`; истёкший неодобренный выход не тратит попытку
- `GatewayPositionManager(…, cleared=)` (без `policy`), `PositionCycle(…, gateway_mode=)`, `RecoveryService(…, cleared=)` — recovery сначала сверяет UNKNOWN через lookup по command_id; `InFlightEntryUnresolved` — пул с планом PENDING/EXECUTING не получает второй план
- 409 `idempotency_conflict` / `approval_already_used` от `POST /v1/commands` → `ExecutionOutcomeUnknown` (UNKNOWN, план EXECUTING)
- `EXIT_REQUIRES_EXECUTION_GATEWAY` поднимается, только если шлюз не обслуживает режим позиции

## Реализованные интерфейсы — F3

- `src/core/secrets.py`: `SECRETS`, `SecretSetting`, `url_credentials` — один реестр секретов для `AppConfig.redacted()`, редактора логов (exc_info форматируется до редактора, в т.ч. uvicorn) и /settings; `AppConfig.secret_values()`, поля `executor_mode_token`, `executor_approval_secret`, `gmgn_api_key`
- `POST /api/v1/alerts/{id}/resolve` + форма у каждого алерта на `/` и `/settings` (сессия+CSRF или Bearer, причина, повторный токен, confirm); аудит `CriticalAlertRaised{stage=resolved|resolve_rejected}`; `AlertResolveRequest` → `AlertView`
- журнал решений: `DecisionJournal` + `DecisionQuery/Page/Outcome` в `src/persistence/contracts`, `PostgresDecisionStore` в `src/persistence/decisions.py`, `Repositories.journal`; `src/api` не импортирует sqlalchemy и `src.persistence.models` (AST-тест); `src/api/catalog.py` удалён
- `DecisionStore.record_evaluation(record, *, evaluated_at) -> RecordedDecision`, `record_decision(…, *, evaluated_at=None)`, `outcome_key()`; таблица `candidate_decision_heads` и индексы (seq), (pool_id, seq DESC) — миграция 0004; неизменный исход не пишется заново (принятый кандидат — каждый цикл)
- `RetentionPolicy.from_environment` (`HISTORY_RETENTION_HOURS=36`, не меньше 300 шагов universe), расширенный `RetentionReport`, батч relay 500; ротация docker-логов 3×10 МБ; `UniverseSummary`/`universe_summary()`
- api и worker получают переменные политики (RULESET_VERSION, RULESET_OVERRIDES_FILE, WORKER_UNIVERSE_INTERVAL_SECONDS, GMGN_REQUIRED_FOR_LIVE) из одного блока compose; `EffectiveConfig.kill_switch` — политика из состояния режима; `ExecutionService` без `kill_switch_policy`
- acceptance: fixture-api отдаёт price токенов из записей того же дня (pool_single, pools_page1/2), у HYPE цены нет

## Реализованные интерфейсы — T17

- контракт V2 расширен совместимо (старые payload и дайджесты не изменились): operation `SWAP` + `swap_direction=X_TO_Y`, `in_amount`, `max_price_impact_bps` (бинов нет, `position_address` — позиция, из которой вышли); `simulation.swap_quote{in,out,min_out,fee}_amount`, бины nullable; `receipt.settlement{fee_lamports, priority_fee_lamports, delta_x, delta_y, mint_x, mint_y}` (минты — из binding команды)
- executor: `computeUnitPrice`, `priorityFeePolicyFromEnvironment` (`PRIORITY_FEE_MAX_LAMPORTS=15000000`, `PRIORITY_FEE_PERCENTILE=75`, `PRIORITY_FEE_MIN_MICRO_LAMPORTS=1000`; потолок — на транзакцию), `walletSettlement`, `RpcTransport.settlement`; коды `PRICE_IMPACT_BREACH`, `SWAP_QUOTE_INCOMPLETE`
- Python: `ExecutionService.refresh_settlement`, `swap_rejection`, `SALE_OPERATIONS`; `SwapSettings.from_environment` (`SWAP_AFTER_EXIT`, `SWAP_MAX_PRICE_IMPACT_BPS=300`, `SWAP_SLIPPAGE_BPS=100`, `SWAP_AFTER_EXIT_MAX_SPLITS=4`, `SWAP_AFTER_EXIT_ATTEMPTS=5`, `SWAP_SETTLEMENT_TIMEOUT_SECONDS=600`) — пока читается из `os.environ` внутри `GatewayPositionManager`/`ShadowBroker`, compose не передаёт (хвост — таск 19)
- продажа после выхода: ключ `exit-sale:{position}:{exit_attempt}:{part}[:{preview}]`; продаётся `delta_x` из receipt выхода, частями ≤ полученного; статусы продажи `DISABLED/NOT_REQUIRED/PENDING/AWAITING_APPROVAL/SELLING/SOLD/UNSOLD`, `PositionActionStatus.SELLING`; алерты `EXIT_SALE_IMPACT_EXCEEDED`, `EXIT_SALE_FAILED`, `EXIT_SALE_AMOUNT_UNKNOWN`, `EXIT_SALE_GATEWAY_UNAVAILABLE`; details `SETTLEMENT_PENDING`, `SALE_SETTLEMENT_PENDING`, `REDUCTION_SALE_PENDING`, `NEXT_PART`, `AMOUNT_UNKNOWN`, `PRICE_IMPACT_EXCEEDED`, `ATTEMPTS_EXHAUSTED`, `GATEWAY_UNAVAILABLE`, `SALE_OUTCOME_UNKNOWN`; не-SHADOW CLOSE проводится после окончания продажи; `PositionClosed.exit_sale{status, returned/sold/unsold_base_units, detail}`
- учёт: `ShadowBroker(…, exit_sale=ExitSaleModel)`, `AccountingService.apply_sales(position, sales, correlation)`, `SaleFill`, `LedgerKind.SWAP`; `GatewayFillBook.fees(receipt)` — комиссии из settlement, если `mint_y == wSOL`, иначе модель издержек; `GatewayFillBook.book(command_id, price, *, sales=(), exit_sale=None)`; `GatewayPositionManager(…, swap=)`
- панель: `ApprovalPreview.swap: SwapView` — отдельная карточка продажи (тот же чеклист G17, что у выхода); `ApprovalRequested.swap`; kill switch и дневной стоп продажу не блокируют

## Реализованные интерфейсы — T18

- отбор guide-v1: обязателен устойчивый объём (средний за 1 ч и текущий 5m ≥ `minimum_hourly_volume_per_minute=14000`); всплеск — флаг `VOLUME_SPIKE` и компонент балла; компоненты балла `sustained_volume, volume_spike, fee_opportunity, liquidity_stability, volatility_fit, token_quality`; `GuideScoreWeights`
- `token_groups(snapshots) -> TokenGroup(base_mint, pools, volume_1h).selection_for()`; `TokenSelection`; основной пул — наибольшие LP-комиссии за 1 ч; `PoolScreener.evaluate(..., selection=)`, `assess_guide_thresholds(..., selection=)`; `CandidatePipeline(..., enhanced_signals=)`: `screen() -> ScreenedPool | CandidateOutcome`, `complete(screened, deep, rank, rank_limit)`, `evaluate(..., selection=)`; `PipelinePolicy.deep_candidate_limit` (guide-v1 50, legacy None)
- коды: `HOURLY_VOLUME_PER_MINUTE_BELOW_THRESHOLD`, `FEE_TVL_BELOW_THRESHOLD`, `THEME_EXCLUDED`, `TOKEN_ALREADY_HELD`, `DEVELOPER_HOLDS_TOKENS` (reject только в LIVE), `CANDIDATE_RANK_BELOW_LIMIT`; флаги `VOLUME_SPIKE`, `TOKEN_VOLUME_POOL_ONLY`, `SNIPERS_HIGH`, `TOKEN_REVIVED`, `THEME_UNKNOWN` (в APPROVAL/LIVE + missing `theme` → reject); неизвестная доля девелопера делает ответ GMGN неполным (`GMGN_UNAVAILABLE`)
- накрутка: требуемые total fees = 30 SOL × капа / 500k USD при обороте пула за 1 ч ≥ `maximum_hourly_turnover=10` × TVL → `ARTIFICIAL_VOLUME_SUSPECTED`; у fee_efficiency нет порога
- настройки guide-v1: `target_volume_per_minute=20000`, `total_fees_reference_market_cap=500000`, `excluded_theme_words` (подстрока, без учёта регистра), `maximum_sniper_count=20`, `revived_token_age=60d`, `maximum_deep_candidates=50`
- `PoolSnapshot.base_token_name/base_token_symbol`; `VolumeSpikeFeatures.hourly_volume_per_minute/hourly_turnover`
- `EnhancedTokenSignals` (из GMGN `token/info`): `volume_5m_usd`, `volume_1h_usd`, `price_usd`, `market_cap_usd` (= price × circulating_supply), `ath_price_usd`, `ath_market_cap_usd`, `sniper_count`, `sniper_share`, `developer_status: DeveloperStatus(HOLDING|SOLD) | None`, `created_at`, `wash_trading`
- (T18, продолжение ниже)

## Реализованные интерфейсы — T19

- `StrategyPlan.blocked_by: tuple[StrategyRejection]` (`RANGE_TOO_NARROW`, `NOT_NEAR_LOCAL_HIGH`), `.flags: tuple[StrategyFlag]` (`ATH_POOL_ONLY`); отказ стратегии сейчас уходит через `PositionSizer` → `RiskRejection` (решение ACCEPT, плана нет)
- односторонний Spot: только quote-токен равномерно ниже активного бина, глубина `spot_depth=0.60`; Bid-Ask ≥70% в одной позиции до `maximum_position_bins=70` (D10); вход Bid-Ask ≥ `bid_ask_local_high_fraction=0.90` хая за 1 ч; Spot на ATH — ATH токена GMGN или пула + капа ≥ `spot_ath_minimum_market_cap=1000000`
- `PipelinePolicy.liquidity_slippage=0.30`, `.active_bin_tolerance_for(lower, upper)`; executor `liquiditySlippage(command, binStep)` — бин-slippage SDK = допуск команды
- выходы: `ExitPolicy/PositionState.take_profit_fraction` (5% / 5% / 3%), `PositionReason.TAKE_PROFIT`, `.DEV_SOLD`; `PositionSignals.net_pnl_fraction`, `.developer_sold`; `developer_sold(entry, current)`; сроки 24 ч / 3 ч / 30 мин
- перечитывание пулов открытых позиций: `MeteoraDataApiProvider.current_snapshot(pool)`, `PoolSnapshotSource`, `PositionCycle(snapshots, snapshot_interval, developer, developer_interval, exit_sale)`, `WorkerSettings.position_snapshot_interval` (`WORKER_POSITION_SNAPSHOT_SECONDS=30`); перечитывание выводится от прошлого перечитывания (окно 5m непрерывно после свипа); хранится в памяти
- контекст не-SHADOW позиции считается от активного бина команды: entry = цена снимка × step^drift (`_position_context(..., drift)`)
- `CostModel.priority_fee_lamports`, `priority_fee_base_units(cost, market)` — в SHADOW = потолок на транзакцию, только пулы к wSOL; `ShadowBroker.preview_close(position, market, cost, *, bins, at)`
- `WorkerRuntime(pool_snapshots, developer, swap)`, `.swap`, `.developer` — `SwapSettings` через runtime; compose: `SWAP_*` → worker, `PRIORITY_FEE_*` → executor (профиль live), `PRIORITY_FEE_MAX_LAMPORTS` и в блоке политики api+worker

## Реализованные интерфейсы — F4

- продажа после выхода: алерты `EXIT_SALE_OUTCOME_UNKNOWN` (часть с неизвестным исходом после истечения/таймаута сверки; слот держится, ручного пути нет), `EXIT_SALE_BLOCKED` (detail `SALE_BLOCKED`); `POSITION_CONTEXT_MISSING` поднимает и продажа; тег алертов продажи `[exit-sale:{pos}:{attempt}]`; ключ дедупа `EXIT_SALE_GATEWAY_UNAVAILABLE` — по части; он закрывается проданной частью той же попытки и проводкой CLOSE, два новых — проводкой CLOSE
- executor: `SWAP_EXCEEDS_EXIT_RETURNS` (submit 409 — сумма частей SWAP по позиции > delta_x её подтверждённых выходов; в Python — отказ, не UNKNOWN), `SWAP_QUOTE_INSUFFICIENT_LIQUIDITY` (и ошибка симуляции SWAP; режет часть пополам до ceil(returned/2^max_splits), дальше UNSOLD); `ExecutionStateStore.byPosition`, `PersistedSubmission.operation/inAmount`; корпус `swap_simulations[3]`, `submit_refusals`
- вход не-SHADOW: `fresh_high_refusal(plan, market, drift)` → `NOT_NEAR_LOCAL_HIGH` по свежему активному бину; `PositionRecorder -> Decimal` — все пути проводят вход по `context.entry_price`
- тейк-профит не-SHADOW выключен: `PositionActionProposed.take_profit="UNMEASURED"`, лог `take_profit_unmeasured`
- SHADOW priority fee: `GuidePolicySettings.shadow_priority_fee_lamports=1_000_000` (стартовая оценка), потолок — `PRIORITY_FEE_MAX_LAMPORTS`; `guide_policy`/`hypothesis_policy(priority_fee_ceiling_lamports=)`; `CommandConfirmed.cost_model_flags=["PRIORITY_FEE_UNPRICED"]` для пулов не к wSOL

## Реализованные интерфейсы — F5

- `src/domain/screening/pairs.py`: `pair_token(PoolRef) -> PairToken(mint, shape: TOKEN_FIRST|TOKEN_SECOND|QUOTE_ONLY|NO_QUOTE)`, `QUOTE_MINTS` (wSOL/USDC/USDT, источник в коде); группы токенов и «токен уже занят» определяют монету через `pair_token` (SOL-MEME и MEME-SOL блокируют друг друга); guide-v1 отклоняет пулы `TOKEN_SECOND_IN_PAIR`, `PAIR_OF_QUOTE_TOKENS`, `PAIR_WITHOUT_QUOTE_TOKEN`, но держит их в группе токена; evidence `pool_pair`
- темы: сравнение по словам и склейкам с мем-приставками (`GuideScoreRules.theme_affixes`), `*` в конце слова — основа (заменяет «подстроку» из T18)
- ранг кандидатов: по объёму токена GMGN из кэша (`GmgnOpenApiProvider.cached_signals(mint)` — без запроса, только удачные чтения моложе min(cache_ttl, stale_after)), иначе по сумме пулов; `token_groups(..., token_volume_1h=)`, `TokenGroup.token_volume_1h/.rank_volume/.rank_source`, `TokenSelection.rank_source`; evidence `token_volume_rank_source`
- отказ стратегии: план строится до записи решения; `refuse_candidate(decision, reasons, evidence)` → REJECT с `NOT_NEAR_LOCAL_HIGH`/`RANGE_TOO_NARROW` (теперь RejectionCode), без plan_id (заменяет «через PositionSizer» из T19); `PositionSizer.size` на плане с blocked_by бросает ValueError

## Реализованные интерфейсы — T18 (продолжение)

- evidence: theme, market_cap_source, token_volume_source (GMGN|DLMM_POOLS|PENDING), token_pool_count, hourly_volume_per_minute, volume_spike, pool_base_fee_rate, pool_bin_step, pool_fees_1h, required_total_fees_sol, pool_hourly_turnover, gmgn_wash_trading, developer_status, gmgn_sniper_count, gmgn_sniper_share, gmgn_token_ath_price, gmgn_token_ath_market_cap, token_volume_rank


## Реализованные интерфейсы — T20

- чеклист по операции: `labels.ENTRY_CHECKLIST` (6 пунктов: прежние 4 + «график органичный», «тема не Илон/Трамп/политика/стрим»), `labels.EXIT_CHECKLIST` (4 пункта G17 — выход и SWAP), `CHECKLIST = ENTRY_CHECKLIST`; `ChecklistMarks.organic_chart`, `.theme_not_excluded` (bool, False); в аудите — отметки этой карточки
- подписи: `LEDGER_KIND`, `SALE_STATUS`, `SALE_DETAIL`, `POSITION_REASON`, `STRATEGY_FLAG`, `COST_MODEL_FLAGS`, `TAKE_PROFIT_STATE`; алерты `EXIT_SALE_*`; `evidence_note` подписывает ath_source, pool_pair, token_volume_source, token_volume_rank_source, market_cap_source, developer_status
- `presenters.evidence_views(items, reasons, flags=())` — при неполном ответе GMGN подсвечивается developer_share/insiders_share, при неизвестной теме — theme
- acceptance: `acceptance-rules.yaml` → `acceptance-relaxed-v2` (часовой минимум объёма и глубина Bid-Ask открыты под записанные данные); smoke проверяет ещё `PAIR_OF_QUOTE_TOKENS` и `THEME_EXCLUDED`

## Реализованные интерфейсы — T21

- посредник GMGN: `powershell -ExecutionPolicy Bypass -File scripts\gmgn-bridge.ps1` / `sh scripts/gmgn-bridge.sh` (внутри `uv run --frozen --no-dev python -m gmgn_service.bridge` в `services/gmgn`); видимый Chromium, постоянный профиль вне репозитория, одна вкладка gmgn.ai, `fetch` изнутри неё (`services/gmgn/gmgn_service/{bridge,tab,webapi}.py`)
- переменные: `GMGN_BRIDGE_TOKEN`/`_FILE` (секрет, ≥16 символов, в реестре `src/core/secrets.py`, поле `AppConfig.gmgn_bridge_token`), `GMGN_BRIDGE_HOST` (127.0.0.1; только loopback и частные адреса), `GMGN_BRIDGE_PORT` (8765), `GMGN_BRIDGE_PROFILE_DIR`, `GMGN_BRIDGE_REQUESTS_PER_MINUTE` (20); worker: `GMGN_URL=http://host.docker.internal:8765` + `GMGN_BRIDGE_TOKEN`/`_FILE`, compose `extra_hosts: host.docker.internal:host-gateway`; все маршруты посредника — Bearer; `/health/ready` → `{status, reasons, mode, login, paused}`
- контракт `gmgn-signals.v2` = v1 + `volume_5m_usd`, `volume_1h_usd`, `price_usd`, `market_cap_usd`, `ath_price_usd`, `ath_market_cap_usd`, `sniper_count`, `sniper_share`, `developer_status` (HOLDING|SOLD), `created_at`, `wash_trading`; `GmgnProvider(cache_ttl, token).cached_signals(mint)`; причины `LOGIN_REQUIRED`, `CHALLENGE`, `API_ERROR`, `API_TIMEOUT`, `API_UNAVAILABLE`, `API_PAYLOAD_INVALID`, `BROWSER_CLOSED`, `SERVICE_UNAUTHORIZED`; слежение за девелопером (DEV_SOLD) — для любого `GMGN_URL`
