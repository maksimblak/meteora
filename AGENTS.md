<!-- autopilot:start -->
# Meteora Volume Bot

Бот ликвидности для DLMM-пулов Meteora (Solana) по гайду LexDollar для одного оператора: Python-worker отбирает монеты и ведёт LP-позиции, FastAPI-панель управляет режимами, TypeScript-executor один держит кошелёк; всё в Docker на Windows, деньги — только после SHADOW → APPROVAL → LIVE.

## Команды

PowerShell 5.1, из корня репозитория. Прогнано 2026-10-24, кроме помеченного «чтением».

- `.env`: `Copy-Item .env.example .env`, затем вписать `APP_OPERATOR_TOKEN` и `APP_SESSION_SECRET`; случайная строка: `$b = New-Object byte[] 32; [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b); -join ($b | ForEach-Object { $_.ToString("x2") })`
- Стек: `docker compose up --build -d` → панель http://127.0.0.1:8000; норма — `migrate` Exited (0), `api` и `executor-quotes` healthy, `worker` Up.
- Логи / остановка: `docker compose logs -f worker`; `docker compose down` (без `-v`).
- Полная проверка (миграции, ruff, mypy, pytest на PostgreSQL, vitest), можно при поднятом стеке — у check своя база: `docker compose --profile test run --rm --build check` → exit 0.
- Python на хосте: `uv sync --frozen`
- Один тест-файл: `uv run --frozen pytest tests/domain/test_guide_screening.py -q -p no:cacheprovider --basetemp "$env:TEMP\mvb-pytest"`
- Один тест executor (чтением `executor/package.json`; Node 22, `executor/node_modules` уже есть): `npm test --prefix executor -- tests/quotes.test.ts`
- Приёмка (≈10 мин; отдельный проект `meteora-acceptance` на записанных данных, `.env` не читает, рабочий стек не трогает): `powershell -ExecutionPolicy Bypass -File scripts\acceptance-smoke.ps1`
- Ключ GMGN: `docker compose exec -T worker uv run python -m src.gmgn.smoke --mint <адрес токена>` (без ключа — exit 2 «GMGN_API_KEY не задан»).
- Посредник GMGN без ключа (на хосте, видимое окно Chromium; прогнан T21): `powershell -ExecutionPolicy Bypass -File scripts\gmgn-bridge.ps1` (Ubuntu с рабочим столом — `sh scripts/gmgn-bridge.sh`); worker ходит к нему по `GMGN_URL=http://host.docker.internal:8765` с `GMGN_BRIDGE_TOKEN`.

## Где что

- `src/worker/runtime.py` — сборка worker (`python -m src.worker`): env → провайдеры, шлюз, задачи scheduler и их интервалы.
- `src/worker/policy.py` — все числа `guide-v1` (`GuidePolicySettings`) и загрузка `RULESET_VERSION` / `RULESET_OVERRIDES_FILE`.
- `src/domain/screening/screener.py` (коды отказов и флаги — `models.py` рядом) — отбор; `src/domain/strategy/planner.py` — стратегия, диапазон, выходы.
- `src/worker/pipeline.py` / `positions.py` / `execution.py` — цикл кандидата, цикл позиций, денежный путь (`execution.py` — 2,3 тыс. строк, самый рискованный).
- `src/api/app.py` (ASGI `src.api.main:app`), `src/api/routes.py` (`/api/v1`), `src/web/routes.py` + `src/web/templates/`; русские подписи кодов — `src/api/labels.py`.
- `src/providers/meteora.py`, `src/providers/solana.py`, `src/gmgn/openapi.py`, `src/providers/jupiter.py`, `src/providers/rugcheck.py` — внешние данные; что решают Jupiter и RugCheck в отборе — `src/domain/screening/sources.py`.
- `executor/src/` (`server.ts`, `engine.ts`, `sdk.ts`) и общий корпус контрактов Python ↔ executor `contracts/execution/`.
- `migrations/versions/` (0001–0004) и `src/persistence/factory.py` — схема и единственная фабрика хранилищ для api и worker.

Архитектура (процессы, поток данных, границы, соглашения) — `docs/architecture.md`.
Словарь предметных слов — `CONTEXT.md`.
Почему так решено — `docs/adr/` (0001–0014).
Эксплуатация — `docs/runbook.md`; все переменные и пороги — `docs/configuration.md`; контракты executor и devnet — `executor/README.md`.

## Подводные камни

- `.git` принадлежит пользователю CodexSandboxOffline: `git clone` из этой папки падает «dubious ownership» — нужен `safe.directory` (точную команду git печатает в тексте ошибки).
- pytest на хосте без `-p no:cacheprovider --basetemp <своя папка>` падает: `.pytest-*` в корне и `work/pytest-*` недоступны. Тесты на PostgreSQL там пропускаются (нет `TEST_DATABASE_URL`) — полный прогон только через `check`.
- Код запечён в образ, bind-mount нет: без `--build` стек крутит старый код.
- `docker compose down -v` стирает базу: историю, позиции, журнал аудита.
- Первые ~40 мин после старта кандидатов нет: пулы копят историю (`WARMING_UP`).
- Пороги автора строгие: на живых данных большинство пулов отсеивается по устойчивому объёму 14 000 USD/мин — пустой список кандидатов не баг. Смягчены пороги только для smoke (`tests/acceptance/acceptance-rules.yaml`).
- Позиция не шире 70 бинов (ADR 0010): Bid-Ask ≥70% вниз — только в пулах с bin step ≳176, иначе `RANGE_TOO_NARROW`; поднимать `maximum_position_bins` нельзя.
- Тейк-профит работает только в SHADOW; в APPROVAL/LIVE `take_profit=UNMEASURED`.
- Без `SOLANA_RPC_URL` обзор показывает worker DEGRADED (`PROVIDER_DEGRADED:solana`) и executor NOT_CONFIGURED — так задумано, SHADOW идёт с флагами.
- `TRADING_MODE` режим не включает: режим живёт в БД. `POST /api/v1/mode/transitions` SHADOW→APPROVAL даёт 409, пока нет ≥7 суток SHADOW, ≥20 закрытых позиций, положительного ожидания и покрытия данных ≥99% (`src/modes/models.py`).
- SHADOW-сутки идут в допуск, только если worker проработал их целиком: рестарт или сон ПК стоят текущих суток (`src/worker/evidence.py`).
- Новое число правил — новая `config_version` в `RULESET_OVERRIDES_FILE` (`worker-config-v1` файл не принимает), десятичные — строками; `shadow-hypothesis-v1` заморожена; `RiskPolicy` ослабить нельзя (валидаторы).
- Новый код отказа, флага или алерта без строки в `src/api/labels.py` роняет `tests/api/test_f3_labels.py`; `src/api/` не импортирует SQLAlchemy (`tests/api/test_f3_boundaries.py`).
- Репозитории режима и учёта синхронные (поток-мост к asyncpg): из async-кода api — через `OperatorContext.sync`.
- Контракт executor меняется сразу в `src/execution/contracts.py`, `executor/src/contracts.ts` и `contracts/execution/*.json`; чужой `schema_version` отвергается.
- `services/gmgn/` — отдельный uv-проект вне `check` (тесты — `uv run` в `services/gmgn` или stage `check` в `Dockerfile.gmgn`). Headless-режим в Docker gmgn.ai блокирует (403); рабочие пути — `GMGN_API_KEY` или режим bridge (видимое окно на хосте, `fetch` изнутри вкладки; адреса — `.autopilot/2026-10-18-meteora-volume-bot/gmgn-web-api-2026-10-24.md`). Вынос cookie из окна в бот не делать — это обход Cloudflare; после 429/403 посредник стоит на паузе 15 мин, первый запрос свежей сессии иногда получает 429.
- `src/core/worker.py` — мёртвая заглушка T01; настоящий worker — `src/worker/__main__.py`.
- `Desktop\Meteora\MeteoraVolumeBot` — копия-поставка (`scripts/copy-delivery.ps1`); правь здесь, не там.
- Диск C: почти полон: сборка образов может упасть по месту (стек ~2,5 ГБ, профиль `gmgn` ещё ~4 ГБ). Bash-инструмент агента здесь не работает — только PowerShell (нет `&&`).

## Окружение

Имена из `.env.example`; значения — только у оператора. Полный справочник с умолчаниями — `docs/configuration.md`.

- `.env` нужен compose только для подстановки `${X:-}`: в контейнер попадает лишь перечисленное у сервиса, остальное — через `docker-compose.override.yml`. `DATABASE_URL` и `APP_ENV` зашиты в compose.
- Секреты задаются `NAME` или `NAME_FILE`, не обоими (иначе Python не стартует); реестр — `src/core/secrets.py`.
- `APP_OPERATOR_TOKEN`, `APP_SESSION_SECRET` — вход оператора и подпись сессии/CSRF; без токена панель только читает.
- `SOLANA_RPC_URL` — token risk, active bin и decimals, котировки; без него `TOKEN_RISK_UNAVAILABLE`, вход в APPROVAL/LIVE закрыт.
- `EXECUTOR_INTERNAL_TOKEN` (+ `EXECUTOR_QUOTES_URL`) — доступ worker к `executor-quotes`; без него `DEPTH_UNAVAILABLE`.
- `METEORA_DATA_API_BASE_URL`, `METEORA_UNIVERSE_FILTER` — источник universe; пусто = пулы с объёмом за 24 ч, `all` = полный sweep.
- `GMGN_API_KEY` (+ `GMGN_API_URL`, `GMGN_API_PLAN`, `GMGN_API_CACHE_SECONDS`, `GMGN_TIMEOUT_SECONDS`, `GMGN_STALE_AFTER_SECONDS`) — метрики GMGN, только worker; `GMGN_URL` — сервис `gmgn` или посредник (+ `GMGN_BRIDGE_TOKEN`/`_FILE`; у посредника ещё `GMGN_BRIDGE_HOST`, `GMGN_BRIDGE_PORT`, `GMGN_BRIDGE_PROFILE_DIR`, `GMGN_BRIDGE_REQUESTS_PER_MINUTE`); `GMGN_REQUIRED_FOR_LIVE=false` открывает LIVE без GMGN; `GMGN_PAGE_TIMEOUT_SECONDS`, `GMGN_CACHE_TTL_SECONDS`, `GMGN_MAX_PAGES`, `GMGN_BREAKER_COOLDOWN_SECONDS` — сам сервис `gmgn`.
- `JUPITER_API_URL`, `RUGCHECK_API_URL` — бесплатные источники токена (ADR 0014), compose включает оба по умолчанию, пустое значение в `.env` выключает; `JUPITER_API_KEY`/`_FILE` (необязательный бесплатный ключ, вдвое больше лимит), `JUPITER_CACHE_SECONDS`, `JUPITER_MAX_TOKENS`, `RUGCHECK_CACHE_SECONDS` — только worker. Без compose (тесты, `build_runtime`) оба выключены.
- `RULESET_VERSION`, `RULESET_OVERRIDES_FILE`, `WORKER_UNIVERSE_INTERVAL_SECONDS`, `PRIORITY_FEE_MAX_LAMPORTS` — политика; compose даёт их и worker, и api — в override задавать обоим одинаково.
- `TRADING_MODE`, `EXECUTOR_URL`, `EXECUTOR_MODE_TOKEN`, `EXECUTOR_APPROVAL_SECRET`, `EXECUTOR_TIMEOUT_SECONDS`, `WALLET_PUBLIC_KEY`, `WALLET_KEYPAIR_FILE`, `EXECUTOR_STATE_FILE`, `LIVE_PROMOTION_FILE` — только APPROVAL/LIVE (шлюз и executor профиля `live`).
- `SWAP_*` — продажа монеты после выхода (worker); `PRIORITY_FEE_PERCENTILE`, `PRIORITY_FEE_MIN_MICRO_LAMPORTS` — цена compute unit (executor).
- `WORKER_*`, `SHADOW_EQUITY_BASE_UNITS`, `HISTORY_RETENTION_HOURS` — ритм задач, стартовый капитал SHADOW, окно хранения телеметрии; `WORKER_HEALTH_URL`, `OPERATOR_SESSION_TTL_SECONDS`, `OPERATOR_APPROVAL_TTL_SECONDS` — api; `APP_LOG_LEVEL` — уровень логов.
- Тесты и отладка: `TEST_DATABASE_URL` (PostgreSQL-тесты), `EXECUTOR_INTEGRATION_URL` / `EXECUTOR_INTEGRATION_TOKEN`, `LOCAL_SQLITE_PATH` (без PostgreSQL, не в production).
- Читаются, но ни на что не влияют: `SOLANA_WS_URL`, `JITO_BLOCK_ENGINE_URL`, `HELIUS_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `APP_TIMEZONE`; `POSTGRES_*` compose не использует.

## Как здесь работает Autopilot

Сборка ведётся навыком `/autopilot`. Требования, спецификация и таски — в `.autopilot/`.
Прогресс — `.autopilot/dashboard.html`. Требование из `manifest.md` может снять
только пользователь. Прерванную сборку продолжает «продолжи автопилот».

### Правка без автопилота

Мелкий баг или доводка после сборки — обычная сессия, без прогона.

Сначала прочитай:
1. `.autopilot/PROGRESS.md` — что не доделано, известные баги, о чём договорились с пользователем.
2. Что правили после последней сборки — первая строка таблицы в «История git».
3. Только если правка их касается: почему сделано так — `docs/adr/`; что значат слова — `CONTEXT.md`;
   что именно просили — `manifest.md` в папке сборки (указана в начале `PROGRESS.md`).

После правки:
- коммит — по правилам из «История git»;
- строка в «Правки без автопилота» в `PROGRESS.md`: дата — что — почему;
- починенный пункт из «Не доделано» или «Известные баги» — удали;
- договорились с пользователем о том, что следующая сборка не должна пересматривать, —
  строка в «Договорились», его словами.

### История git

Целиком не читай — бери срез под вопрос:

| Вопрос | Команда |
|---|---|
| что правили после последней сборки | `git log --oneline "$(git log -1 --format=%h --grep='^Autopilot: сборка сдана')"..HEAD -- . ':(exclude).autopilot'` |
| всё, что делали ради требования | `git log --oneline --grep="R02"` |
| история файла | `git log --oneline -10 -- <файл>` |
| когда появился или исчез код | `git log --oneline -S "<фрагмент>"` |
| почему строка такая | `git blame -L <с>,<по> <файл>`, затем `git show <sha>` |
| что внутри коммита | `git show --stat <sha>` |

Срез длиннее ~30 коммитов — отдай его субагенту и возьми выжимку в десять строк.

Коммит: заголовок — что (`fix: даты в одном формате`), тело — почему, трейлер
`--trailer "Requirements: R02"`, если правка касается требования из `manifest.md`.
Заголовок `Autopilot: сборка сдана` пишет только автопилот — по нему находят последнюю сборку.
<!-- autopilot:end -->
