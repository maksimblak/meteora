# Архитектура

Поток данных, границы модулей и соглашения. Собрано из `interfaces.md` сборки и сверено с кодом 2026-10-24; где код разошёлся с исходным планом — раздел «Расхождения» в конце. Почему так решено — `docs/adr/`; что значат слова — `CONTEXT.md`.

## Процессы (`docker-compose.yml`)

| Сервис | Вход | Роль |
|---|---|---|
| `postgres` | `postgres:16` | единственное общее состояние и координация (ADR 0003); порт не опубликован |
| `migrate` | `alembic upgrade head` | применяет `migrations/versions/0001…0004` и выходит с кодом 0; `api` и `worker` ждут его |
| `api` | `uvicorn src.api.main:app` | панель (`/`, `/pools`, `/decisions`, `/positions`, `/performance`, `/settings`, `/approvals`, `/mode`, `/login`) и `/api/v1`; `127.0.0.1:8000`; health worker читает с `http://worker:9100` |
| `worker` | `python -m src.worker` | весь конвейер по расписанию; `/health/*` и `/metrics` на 9100 только внутри сети compose |
| `executor-quotes` | `Dockerfile.executor`, `TRADING_MODE=SHADOW` | только read-only `POST /v1/quotes` (глубина кандидатов); кошелька нет по построению |
| `executor` | тот же образ, профиль `live` | денежный executor; единственный процесс с ключом кошелька; окружение — через `docker-compose.override.yml` |
| `gmgn` | `Dockerfile.gmgn`, профиль `gmgn` | браузерный источник GMGN (`services/gmgn/`): headless в Docker gmgn.ai блокирует (403); режим bridge — на хосте, видимое окно, `GMGN_URL=http://host.docker.internal:8765` (ADR 0013) |
| `check` | профиль `test` | миграции, ruff, mypy, pytest (с `TEST_DATABASE_URL`), `npm run check --prefix executor` |

Код запечён в образы (`COPY src` в `Dockerfile`), bind-mount нет: правка кода видна в стеке только после `--build`.

## Модули `src/` и границы

| Пакет | Владеет | Наружу |
|---|---|---|
| `domain/features` | признаки объёма из истории снимков | `VolumeFeatureEngine.compute` |
| `domain/screening` | жёсткие фильтры, балл, коды `RejectionCode` / `DecisionFlag`, группировка по монете (`selection.py`), сторона пары (`pairs.py`) | `PoolScreener.evaluate → CandidateDecision`, `token_groups`, `pair_token` |
| `domain/strategy` | выбор Bid-Ask / Spot, диапазон бинов, выходы | `StrategyPlanner.plan`, `PositionMonitor.evaluate` |
| `domain/risk` | лимиты 10% / 1% / 3 позиции / 4% / 10% ликвидности, promotion | `PositionSizer.size`, `RiskEngine`, `PromotionPolicy`, `RiskPolicy` |
| `market` | типы рынка (`PoolSnapshot`, `TokenRiskSnapshot`, `EnhancedTokenSignals`, `Freshness`, `DataQuality`), протоколы провайдеров (`contracts.py`), синхронизация universe, производные короткие окна (`windows.py`) | `UniverseSync.run`, `MarketDataProvider`, `OnChainProvider` |
| `providers` | Meteora Data API (`meteora.py`), Solana RPC (`solana.py`), Jupiter Tokens API v2 (`jupiter.py`) и RugCheck (`rugcheck.py`) — ADR 0014, лимиты/ретраи/breaker (`resilience.py`), синтетические фикстуры | реализации протоколов `market`; URL, сырой JSON и ключи наружу не выходят |
| `gmgn` | GMGN OpenAPI (`openapi.py`, основной), клиент сервиса-браузера (`client.py`, запасной) | `EnrichedOnChainProvider` — подмешивает GMGN к token risk только кандидатов |
| `accounting`, `portfolio` | shadow-заполнения, комиссии, IL, издержки, P&L, HODL-бенчмарк | `ShadowBroker`, `AccountingService`, `PerformanceService` |
| `modes` | режимы, promotion gates, одноразовые approvals, дневной стоп, вход, kill switch, алерты | `ModeService`, `ApprovalService`, `DailyRiskService`, `EntryGate`, `PromotionEvidenceService`; протокол `ModeRepository` |
| `execution` | контракты executor (`contracts.py`), state machine команды, TTL approval, свежесть симуляции, идемпотентность, сверка | `ExecutionService.prepare/approve/execute/reconcile`, `HttpExecutionGateway`, `HttpQuoteClient` |
| `persistence` | SQLAlchemy-модели, Postgres-адаптеры, outbox, аудит, leases, retention | `open_repositories()` в `factory.py` — единственная фабрика для api и worker; протоколы — `contracts.py` |
| `recovery` | разбор того, что оставил прерванный worker | `RecoveryService.reconcile` |
| `worker` | сборка процесса (`runtime.py`), расписание (`scheduler.py`), циклы кандидатов (`pipeline.py`) и позиций (`positions.py`), денежный путь (`execution.py`), правила (`policy.py`), дневные evidence (`evidence.py`) | `build_runtime`, `load_pipeline_policy`, `build_gateway` |
| `api`, `web` | HTTP-контракты, auth (Bearer / cookie `mvb_session` + `X-CSRF-Token`), approvals, русские подписи (`labels.py`), presenters, SSE `/api/v1/stream/status` и веб-поток обзора `/stream/overview` (HTML из `_overview.html`); шаблоны `src/web/templates`, статика `src/web/static` | `create_operator_app` |
| `observability`, `core` | health/readiness, метрики, алерты, переживающие сбой БД, HTTP-сервер worker; конфиг, реестр секретов, логи с вырезанием секретов, часы | `HealthMonitor`, `AppConfig.load`, `SECRETS` |

Правила зависимостей (держатся кодом и тестами):

- `src/domain/` импортирует только stdlib, pydantic, `src.domain.*` и `src.market.models` — ни FastAPI, ни SQLAlchemy, ни SDK, ни HTTP.
- `src/api/` не импортирует `sqlalchemy`, `src.persistence.models`, `src.persistence.postgres` — AST-тест `tests/api/test_f3_boundaries.py`; журнал решений читается через протокол `DecisionJournal`.
- api берёт политику и сборку шлюза у worker (`src.worker.policy.load_pipeline_policy`, `src.worker.runtime.build_gateway`): пороги в `/settings` и promotion evidence — те же, что применяет worker.
- Ключ кошелька существует только в `executor`; Python видит лишь публичный адрес.

## Поток данных: задачи worker

Старт: `refresh_health` → `RecoveryService.reconcile(startup=True)` → `Scheduler.run_forever`. Каждая задача берётся под lease (строка `system_leases` + advisory lock), так что второй worker не дублирует работу. Приоритет — порядок запуска в тике.

| Пр. | Задача | Шаг | Что делает |
|---|---|---|---|
| 0 | `position_cycle` | 15 с | пул каждой открытой позиции перечитывается из Data API раз в 30 с; `PositionMonitor` → выход; SHADOW-выходы через `ShadowBroker`; дневной стоп 4%; политика kill switch из `mode_state`; не-SHADOW выходы и продажа после выхода — `GatewayPositionManager` |
| 1 | `recovery` | 1 мин | сначала сеть (сверка SUBMITTING/SUBMITTED/UNKNOWN по `command_id`), потом решения по EXECUTING-планам; ничего не отправляет повторно |
| 2 | `candidate_pipeline` | 60 с | см. ниже; только внутри этой задачи исполняются входы (lease TTL 2 мин) |
| 3 | `universe_sync` | 5 мин | `MeteoraDataApiProvider` листает все страницы → `UniverseSync` → атомарная публикация (неполный sync не заменяет последний полный); короткое окно 5m — разница `cumulative_metrics` с предыдущим снимком (`src/market/windows.py`, ADR 0008) |
| 4 | `outbox_relay` | 10 с | outbox → `EventSink` (по умолчанию в лог); at-least-once, идемпотентно по `event_id` |
| 5 | `retention` | 1 ч | удаляет телеметрию старше `HISTORY_RETENTION_HOURS`; `audit_events` не чистится никогда |

Цикл кандидатов (`WorkerRuntime.candidate_job` → `CandidatePipeline`):

1. Открыть UTC-сутки риска (`RiskDayKeeper.ensure_open`), закрыть прошлые сутки в evidence, продолжить одобренные/прерванные входы (`GatewayEntrySettler`).
2. Последний universe → Jupiter читает верхние `JUPITER_MAX_TOKENS` монет пачками по 100 (до момента решения) → группы по монете (`token_groups`, сторона пары не quote-токен); пулы с открытой позицией или входом в полёте пропускаются, их монета — `TOKEN_ALREADY_HELD`.
3. `screen` — дешёвые фильтры без запросов (с показанием Jupiter: объём и капа токена, органичность, mint/freeze), основной пул монеты первым (наибольшие LP-комиссии за час).
4. Ранг по часовому объёму (GMGN из кэша, иначе Jupiter, иначе сумма пулов); глубоко — первые `maximum_deep_candidates` (50), остальные `CANDIDATE_RANK_BELOW_LIMIT`.
5. `complete`: прогрев истории из `volume/history`, token risk (RPC + GMGN) и отчёт RugCheck, котировка `executor-quotes` → `PoolScreener.evaluate` → `StrategyPlanner.plan` (отказ стратегии = REJECT, причины — `CandidateOutcome.refusals`) → решение записывается **до** любого денежного шага → `PositionSizer.size` → `EntryGate.admit` (PENDING-план) → `EntryExecutor`. Отказ одним `RANGE_TOO_NARROW` → `screen` + `complete` для следующего пула монеты, чей bin step вмещает Bid-Ask (`bid_ask_range_fits`), с тем же рангом.

## Денежный путь

- Режим хранится в БД (`mode_state`); свежая база — SHADOW. Повышение — только `ModeService` через панель/API с gates (`src/modes/models.py`: ≥7 суток SHADOW, ≥20 закрытых позиций, покрытие данных ≥0.99; LIVE — ещё 10 циклов APPROVAL и файл LIVE evidence). Понижение и kill switch доступны всегда. `TRADING_MODE` режим не включает — только выбирает, для какого режима собрать шлюз.
- Порядок входа (`src/worker/execution.py`): `claim_entry` (PENDING → EXECUTING под проверкой kill switch / дневного стопа) → повторное чтение режима → аудит `CommandSubmitted` и контекст позиции → submit (shadow-fill или `gateway.submit`; kill switch перечитывается прямо перед ним) → settle. Неизвестный исход держит план EXECUTING до сверки и никогда не отправляется заново.
- SHADOW: `ShadowSubmitter` + `ShadowBroker`, денег и шлюза нет. APPROVAL/LIVE: `GatewaySubmitter` → `ExecutionService` → `HttpExecutionGateway` → executor. Без `EXECUTOR_URL`, внутреннего токена или PostgreSQL шлюз не собирается, входы отменяются `EXECUTION_GATEWAY_NOT_CONFIGURED`.
- APPROVAL: worker готовит команду и симуляцию → фаза `AWAITING_APPROVAL` → оператор в `/approvals` (повторный токен, чеклист; аудит пишется до смены состояния) → `ExecutionService.approve` с HMAC-доказательством на `EXECUTOR_APPROVAL_SECRET` → worker исполняет в `candidate_pipeline` / `position_cycle`.
- Executor (`executor/src/`): `server.ts` — маршруты Fastify и сборка конфигурации, `engine.ts` — идемпотентность, состояние отправок в `EXECUTOR_STATE_FILE`, отказ SHADOW-отправки, `sdk.ts` — официальный `@meteora-ag/dlmm`, priority fee, `quotes.ts` — котировки. OPEN создаёт ключ новой позиции у executor (ADR 0007); позиция не шире 70 бинов (ADR 0010); выход не блокируется price impact (ADR 0009); обмен — только продажа после выхода в том же пуле (ADR 0011).
- Контракт Python ↔ executor — V2 (`DlmmCommandV2`, `SimulationResultV2`, `GatewayReceiptV2`) и котировки `QuoteRequestV1`; другой `schema_version` → `400 unsupported_contract_version`. Общий корпус `contracts/execution/*.json` проверяют обе стороны: `tests/execution/test_contracts.py` и `executor/tests/contracts.test.ts`.

## Хранение

- Одна база PostgreSQL; схема — только Alembic (`migrations/versions/`), `src/persistence/models.py` её зеркалит.
- Протоколы `ModeRepository` / `AccountingRepository` синхронные, драйвер — asyncpg на собственном потоке-мосте (`PostgresDatabase`, `src/persistence/postgres.py`). Из async-кода api — через `OperatorContext.sync` (threadpool).
- Каждая мутация режима — одна транзакция под `pg_advisory_xact_lock`: guard'ы денег читаются после всех прежних писателей и пишутся вместе с алертом, аудитом и outbox.
- Решения неизменяемы, id детерминированы по (пул, время решения, правила, конфиг): повтор цикла не создаёт второй план. Неизменившийся исход не пишется заново — двигается `candidate_decision_heads` (миграция 0004); принятый кандидат пишется каждый цикл.
- `LOCAL_SQLITE_PATH` — SQLite-адаптеры для отладки вне Docker (universe, решения и исполнение — в памяти); при `APP_ENV=production` запрещён. Без `DATABASE_URL` и без него процесс не стартует.

## Соглашения

- Деньги — целые base units или `Decimal`; binary float запрещён (`BaseUnits`, `DecimalAmount`, валидатор `GuidePolicySettings`). Числа Data API приходят float и превращаются в `Decimal` через кратчайшую строку в адаптере.
- Время — только timezone-aware UTC (`UtcTimestamp`); в тестах — `FrozenClock` (`src/core/clock.py`).
- Команда, способная изменить сеть, несёт `command_id`, `idempotency_key`, `expires_at`, `mode`, `ruleset_version`, `correlation_id`.
- Fail-closed (ADR 0002): неизвестный обязательный risk-сигнал закрывает вход в APPROVAL/LIVE; SHADOW идёт дальше с флагом (`TOKEN_RISK_UNAVAILABLE`, `DEPTH_UNAVAILABLE`, `GMGN_UNAVAILABLE`), а такие решения не засчитываются в допуск.
- Правила версионированы: каждое решение хранит `ruleset_version` и `config_version`; изменение любого числа — новая версия. `guide-v1` по умолчанию, `shadow-hypothesis-v1` заморожена побайтово ради воспроизводимости. Лимиты `RiskPolicy` ограничены валидаторами сверху — ослабить нельзя.
- События — типизированный outbox (`EventType` в `src/domain/models/events.py`), конверт: `event_id`, `occurred_at`, `aggregate_id`, `correlation_id`, `causation_id`, `schema_version`. Запись в outbox — в той же транзакции, что и изменение.
- Коды (`RejectionCode`, флаги, алерты, отказы executor) — английские и стабильные, это контракт API и аудита. Русский текст — только в `src/api/labels.py` и шаблонах; `tests/api/test_f3_labels.py` требует строку для каждого кода.
- Секреты — один реестр `src/core/secrets.py`: `NAME` или `NAME_FILE`, не оба; значения вырезаются из логов и маскируются в `/settings`. Новый секрет добавляется только туда.
- Пометки вида `G24`, `D07`, `T19`, `F5`, `spec 6.8` в коде — пункты требований, спецификации и тасков сборки в `.autopilot/`; тесты названы по ним же (`test_t18_*`, `test_f5_*`).

## Тестовые швы

- `src/providers/fixtures.py` — синтетические снимки и token risk; `tests/providers/fixtures/meteora_live/` — записанные ответы живого Data API и RPC (их же отдаёт `fixture-api` приёмочного прогона).
- `tests/worker/harness.py` — денежный конвейер в памяти; `tests/api/support.py` — приложение с поддельным шлюзом.
- `src/persistence/testing.py` — каждому тесту своя мигрированная схема PostgreSQL; включается `TEST_DATABASE_URL`, без неё такие тесты пропускаются.
- Opt-in: `ACCEPTANCE_COMPOSE=1` (`scripts/acceptance-smoke.ps1`, overlay `tests/acceptance/compose.acceptance.yml`, смягчённые пороги `tests/acceptance/acceptance-rules.yaml`), `EXECUTOR_INTEGRATION_URL` (контракт против запущенного executor).
- Executor: SDK-пул, RPC и кошелёк — подделки; тест не открывает сеть и не отправляет транзакцию.

## Расхождения с исходным планом (`interfaces.md`)

- Контракт исполнения — V2; V1 выведен из работы и остался в `contracts/execution/v1-corpus.json`, чтобы обе стороны доказывали, что его отвергают.
- `domain.market` из плана живёт в `src/market/`, а не в `src/domain/`.
- `InMemoryExecutionGateway` и `FakeClock` не существуют: часы — `FrozenClock`, поддельные шлюзы — в тестах.
- `ExecutionGateway.status(signature)` у HTTP-шлюза всегда отказывает (`STATUS_BY_SIGNATURE_UNAVAILABLE`); сверка — через `lookup(command_id)` = `GET /v1/commands/{id}`.
- `src/persistence/uow.py` (`SqlAlchemyUnitOfWork`) и `src/core/worker.py` — остатки T01; приложение берёт хранилища из `open_repositories`, настоящий worker — `src/worker/__main__.py`.
