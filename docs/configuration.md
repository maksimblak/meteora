# Конфигурационный справочник

Все переменные окружения, которые реально читает код (Python: `src/`, executor: `executor/src/server.ts`, сервис GMGN: `services/gmgn/gmgn_service/config.py`), и файл порогов `RULESET_OVERRIDES_FILE`. Значений секретов здесь нет и быть не должно: в `.env` вы вписываете их сами.

## Как настройка попадает в контейнеры

1. `docker compose` читает `.env` из корня проекта **только для подстановки** `${ИМЯ:-}` в `docker-compose.yml`. Директивы `env_file` нет: в контейнер попадает лишь то, что явно перечислено у сервиса.
2. Поэтому у каждой переменной ниже есть колонка «compose»: **да** — достаточно вписать значение в `.env`; **нет** — переменная читается кодом, но `docker-compose.yml` её не передаёт; чтобы её задать, нужен свой `docker-compose.override.yml` (Compose подхватывает его автоматически рядом с основным файлом; в Git его не кладите, если там пути к секретам).
3. Пустое значение = «не задано». Подстановка `${X:-}` превращает отсутствующую переменную в пустую строку, и код считает её отсутствующей.
4. Секреты (их единый список — реестр `src/core/secrets.py`: `DATABASE_URL`, `APP_OPERATOR_TOKEN`, `APP_SESSION_SECRET`, `HELIUS_API_KEY`, `EXECUTOR_INTERNAL_TOKEN`, `EXECUTOR_MODE_TOKEN`, `EXECUTOR_APPROVAL_SECRET`, `GMGN_API_KEY`, `GMGN_BRIDGE_TOKEN`, `JUPITER_API_KEY`, `TELEGRAM_BOT_TOKEN`, `WALLET_KEYPAIR_FILE`) задаются **либо** `ИМЯ`, **либо** `ИМЯ_FILE` (путь к файлу **внутри контейнера**; файл монтируется через override). Python отказывается стартовать, если заданы оба; executor при обоих берёт прямое значение. Пустой файл секрета — ошибка.
5. `DATABASE_URL` и `APP_ENV` для Python-сервисов зашиты в `docker-compose.yml` (`postgresql+asyncpg://meteora@postgres:5432/meteora`, `development`); значения из `.env` для них не используются.

Пример override для настроек, которых нет в compose (впишите свои значения):

```yaml
# docker-compose.override.yml — рядом с docker-compose.yml, не коммитить
services:
  worker:
    environment:
      RULESET_OVERRIDES_FILE: /run/config/rules.yaml
      SHADOW_EQUITY_BASE_UNITS: "[СУММА В BASE UNITS — впиши]"
    volumes:
      - ./config/rules.yaml:/run/config/rules.yaml:ro
  api:
    environment:
      RULESET_OVERRIDES_FILE: /run/config/rules.yaml
    volumes:
      - ./config/rules.yaml:/run/config/rules.yaml:ro
```

Проверить, что получилось: `docker compose config` (покажет итоговые переменные; осторожно — выведет и секреты из `.env`).

## Приложение и вход оператора (api, worker)

| Переменная | Кто читает | По умолчанию | compose | Что делает |
|---|---|---|---|---|
| `APP_OPERATOR_TOKEN` / `_FILE` | api | не задан → все изменяющие действия закрыты (`503`) | да (api) | токен оператора: вход в панель, Bearer для API, повторное подтверждение денежных действий |
| `APP_SESSION_SECRET` / `_FILE` | api | случайный на каждый запуск → после рестарта api вход заново | да (api) | ключ подписи cookie сессии и CSRF-токенов |
| `OPERATOR_SESSION_TTL_SECONDS` | api | 43200 (12 ч) | нет | срок жизни веб-сессии |
| `OPERATOR_APPROVAL_TTL_SECONDS` | api | 90, максимум 600 | нет | сколько живёт выданный approval (и не дольше команды и симуляции) |
| `WORKER_HEALTH_URL` | api | — | зашит: `http://worker:9100` | откуда api берёт health worker для панели |
| `APP_ENV` | api, worker, migrate | `development` | зашит | `development` / `test` / `production`; в `production` запрещён SQLite |
| `APP_LOG_LEVEL` | api, worker | `INFO` | нет | уровень JSON-логов |
| `APP_TIMEZONE` | api, worker | `UTC` | нет | проверяется как IANA-зона; сейчас ни на что не влияет — все времена в UTC |

## База данных

| Переменная | Кто читает | По умолчанию | compose | Что делает |
|---|---|---|---|---|
| `DATABASE_URL` / `_FILE` | api, worker, migrate, check | — | зашит | DSN PostgreSQL (`postgresql+asyncpg://…`); без него и без `LOCAL_SQLITE_PATH` процесс не стартует |
| `LOCAL_SQLITE_PATH` | api, worker | — | нет | только отладка вне Docker: SQLite вместо PostgreSQL, запрещён при `APP_ENV=production` |
| `TEST_DATABASE_URL` | тесты | — | только сервис `check` | включает интеграционные тесты на PostgreSQL (каждый тест — своя схема) |
| `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` | — | — | нет | перечислены в `.env.example`, но compose их не использует: база `meteora`, пользователь `meteora`, вход `trust` без пароля, порт не опубликован |

## Данные рынка и Solana (worker)

| Переменная | Кто читает | По умолчанию | compose | Что делает |
|---|---|---|---|---|
| `METEORA_DATA_API_BASE_URL` | worker | в compose `https://dlmm.datapi.meteora.ag`; вне compose пусто = universe не синхронизируется | да | публичный Meteora DLMM Data API, ключ не нужен |
| `METEORA_UNIVERSE_FILTER` | worker | пусто = `volume_24h>0` (около 2 тыс. из ~139 тыс. пулов, D07) | да | `all` — полный sweep всех пулов; иное значение уходит в `filter_by` как есть |
| `SOLANA_RPC_URL` | worker, executor, executor-quotes | пусто | да | RPC: token risk (mint/freeze authority, держатели, возраст), active bin и decimals из LB pair, котировки глубины. Без него token risk `UNKNOWN`, решения с флагом `TOKEN_RISK_UNAVAILABLE` не засчитываются в допуск (D09), вход в APPROVAL/LIVE закрыт |
| `SOLANA_WS_URL`, `JITO_BLOCK_ENGINE_URL`, `HELIUS_API_KEY` / `_FILE` | api, worker | — | нет | читаются в конфигурацию и показываются в `/settings` (секрет — замаскирован), но **ни на что не влияют**: WebSocket, Jito-отправки и Helius-адаптера в этой сборке нет |
| `TELEGRAM_BOT_TOKEN` / `_FILE`, `TELEGRAM_CHAT_ID` | api, worker | — | нет | зарезервировано под уведомления; адаптера уведомлений в сборке нет |

## Worker: ритм, капитал, правила

| Переменная | По умолчанию | compose | Что делает |
|---|---|---|---|
| `WORKER_UNIVERSE_INTERVAL_SECONDS` | 300 | да (worker и api) | шаг sweep universe. От него зависят производное окно 5m (берётся только из интервала не короче 4 мин 30 с; более частые свипы суммируются; самый длинный допустимый интервал — шаг + max(30 с, 10%), а при шаге короче 4 мин 30 с — ещё + 4 мин 30 с), допуск разрыва наблюдений и нижняя граница окна хранения истории (300 шагов) |
| `WORKER_CANDIDATE_INTERVAL_SECONDS` | 60 | нет | как часто оцениваются все пулы последнего snapshot |
| `WORKER_POSITION_INTERVAL_SECONDS` | 15 | нет | как часто переоцениваются открытые позиции |
| `WORKER_POSITION_SNAPSHOT_SECONDS` | 30 | да (worker) | как часто пул каждой открытой позиции перечитывается из Data API (`/pools/{address}`), чаще universe; окно 5m считается по правилам F1 от прошлого перечитывания. Живёт в памяти worker: после рестарта начинается заново |
| `WORKER_MAX_CONCURRENCY` | 2 | нет | сколько задач scheduler запускает одновременно |
| `SHADOW_EQUITY_BASE_UNITS` | 1 000 000 000 | нет | стартовый капитал SHADOW в base units quote-токена (от него считаются 10% / 1% / 4%) |
| `WORKER_METRICS_HOST` / `WORKER_METRICS_PORT` | `127.0.0.1` / 9100 (`0` — выключить) | host зашит `0.0.0.0` | health и метрики worker внутри сети compose |
| `RULESET_VERSION` | `guide-v1` | да (worker и api) | `guide-v1` или прежняя `shadow-hypothesis-v1`; другое значение — отказ старта |
| `RULESET_OVERRIDES_FILE` | — | да (worker и api; путь внутри контейнера, сам файл монтируется override) | YAML с настроенными порогами `guide-v1`, см. ниже |
| `HISTORY_RETENTION_HOURS` | 36 | да (worker) | окно хранения телеметрии: снимки пулов, версии universe, векторы признаков и решения без плана старше окна удаляет задача `retention` (раз в час, пачками). Не короче 300 шагов universe. Остаются всегда: последняя версия universe, последний снимок каждого пула, действующее решение пула, решения с планом и весь журнал аудита |
| `TRADING_MODE` | `SHADOW` | да | для какого режима подключить шлюз исполнения (`APPROVAL`/`LIVE`). **Режим не включает**: режим хранится в БД и повышается только через panel/API с gates |

Всё, что читает политика конвейера (`RULESET_VERSION`, `RULESET_OVERRIDES_FILE`, `WORKER_UNIVERSE_INTERVAL_SECONDS`, `GMGN_REQUIRED_FOR_LIVE`, `PRIORITY_FEE_MAX_LAMPORTS`), compose передаёт **и worker, и api** из одного блока: api по той же политике показывает пороги в `/settings` и собирает promotion evidence, поэтому дни наблюдений и пороги в панели совпадают с теми, по которым работает worker. Если задаёте эти переменные в своём override, задавайте их обоим сервисам одинаково.

## Шлюз исполнения: Python-сторона (worker, api)

| Переменная | Кто читает | По умолчанию | compose | Что делает |
|---|---|---|---|---|
| `EXECUTOR_URL` | worker, api | — | да (оба) | адрес денежного executor, например `http://executor:3000`; без него шлюз APPROVAL/LIVE не собирается |
| `EXECUTOR_QUOTES_URL` | worker | в compose `http://executor-quotes:3000`, иначе `EXECUTOR_URL` | да | read-only котировки глубины в любом режиме; без URL или внутреннего токена — флаг `DEPTH_UNAVAILABLE` |
| `EXECUTOR_INTERNAL_TOKEN` / `_FILE` | worker, api (только `_FILE`) | — | worker: оба; api: только `_FILE` | заголовок `x-internal-token` к executor |
| `EXECUTOR_MODE_TOKEN` / `_FILE` | worker (api и worker читают его в реестр секретов: значение вырезается из логов) | — | нет | заголовок `x-mode-token` при отправке команды |
| `EXECUTOR_APPROVAL_SECRET` / `_FILE` | api, worker | — | api: только `_FILE`; worker: нет | HMAC-секрет подписи approval (32+ байт); без него approvals в панели «не настроены» |
| `EXECUTOR_TIMEOUT_SECONDS` | worker, api | 30 | нет | таймаут HTTP к executor |
| `LIVE_PROMOTION_FILE` | worker, api, executor | — | нет | JSON с LIVE promotion evidence, см. `docs/runbook.md` |
| `WALLET_PUBLIC_KEY` | worker, executor | — | нет | публичный адрес кошелька (worker пишет его в команды; executor сверяет с файлом ключа) |
| `EXECUTOR_INTEGRATION_URL`, `EXECUTOR_INTEGRATION_TOKEN` | тесты | — | нет | включают контрактный тест против запущенного executor |

## Executor (TypeScript, сервисы `executor` и `executor-quotes`)

| Переменная | По умолчанию | compose | Что делает |
|---|---|---|---|
| `TRADING_MODE` | `SHADOW` | `executor-quotes`: зашит `SHADOW`; `executor`: **нет** | в `SHADOW` executor никогда не отправляет транзакции и не грузит кошелёк |
| `SOLANA_RPC_URL` | — | `executor-quotes`: да; `executor`: нет | RPC для котировок и исполнения; без него `/v1/quotes` → `503 quotes_unavailable` |
| `EXECUTOR_INTERNAL_TOKEN` / `_FILE` | — | `executor-quotes`: да; `executor`: нет | обязателен для всех `/v1/*` |
| `EXECUTOR_MODE_TOKEN` / `_FILE` | — | нет | обязателен для отправки |
| `EXECUTOR_APPROVAL_SECRET` / `_FILE` | — | нет | проверка approval (APPROVAL) |
| `WALLET_KEYPAIR_FILE`, `WALLET_PUBLIC_KEY` | — | нет | файл ключа кошелька и сверка его адреса |
| `EXECUTOR_STATE_FILE` | — | нет | файл durable-состояния отправок и идемпотентности (APPROVAL/LIVE) |
| `LIVE_PROMOTION_FILE` | — | нет | LIVE: без валидного файла engine не собирается |
| `PRIORITY_FEE_MAX_LAMPORTS` | 15 000 000 (0,015 SOL) | `executor`: да (и worker, api — блок политики) | потолок priority fee **на одну транзакцию**; цена compute unit режется так, чтобы транзакция не заплатила больше. Поднимайте при нагрузке сети, если транзакции не проходят |
| `PRIORITY_FEE_PERCENTILE` | 75 | `executor`: да | перцентиль недавних priority fees сети по пулу (`getRecentPrioritizationFees`), 1–100 |
| `PRIORITY_FEE_MIN_MICRO_LAMPORTS` | 1000 | `executor`: да | нижняя цена compute unit в micro-lamports; она же, когда сеть не дала данных |

Compose-сервис `executor` (профиль `live`) получает из `docker-compose.yml` только `PRIORITY_FEE_*` — остальное окружение и секретные файлы для APPROVAL/LIVE задаются через `docker-compose.override.yml`. Неверное значение `PRIORITY_FEE_*` (не целое или вне диапазона) не даёт executor стартовать. Фактический priority fee каждой транзакции приходит в квитанции (`settlement.priority_fee_lamports`). Подробности контрактов и ручной проверки на devnet — `executor/README.md`.

## Продажа монеты после выхода (worker, G22)

После подтверждённого выхода (REMOVE/CLOSE) монета, которую вернула позиция, продаётся в quote-токен в том же DLMM-пуле отдельными командами `SWAP` (порядок работы и что делать с непроданной монетой — `docs/runbook.md`, §2.7). Числа — стартовые значения сборки, не гайда. Пусто = значение по умолчанию; нечитаемое значение останавливает worker при старте.

| Переменная | По умолчанию | compose | Что делает |
|---|---|---|---|
| `SWAP_AFTER_EXIT` | `true` | да (worker) | `false` — не продавать: монета остаётся в кошельке (статус продажи `DISABLED`) |
| `SWAP_MAX_PRICE_IMPACT_BPS` | 300 (3%) | да | лимит price impact котировки одной команды продажи; выше — часть делится пополам (`PRICE_IMPACT_BREACH`) |
| `SWAP_SLIPPAGE_BPS` | 100 (1%) | да | минимальный выход в сети = котировка минус этот процент; ниже транзакция не исполнится |
| `SWAP_AFTER_EXIT_MAX_SPLITS` | 4 | да | сколько раз часть можно поделить пополам (самая малая — 1/2^N полученного); дальше монета остаётся (`EXIT_SALE_IMPACT_EXCEEDED`) |
| `SWAP_AFTER_EXIT_ATTEMPTS` | 5 | да | сколько неудачных частей до отказа от продажи (`EXIT_SALE_FAILED`) |
| `SWAP_SETTLEMENT_TIMEOUT_SECONDS` | 600 | да | сколько ждать, пока сеть сообщит, сколько монеты вернул выход и сколько продала часть; дальше — `EXIT_SALE_AMOUNT_UNKNOWN` или `EXIT_SALE_OUTCOME_UNKNOWN` |

Продажа идёт во всех режимах: SHADOW моделирует её по котировке и комиссии пула, APPROVAL — каждая часть отдельной карточкой подтверждения, LIVE — автоматически. Kill switch и дневной стоп её не блокируют. Executor отказывает продаже сверх того, что вернули подтверждённые выходы позиции (`SWAP_EXCEEDS_EXIT_RETURNS`).

## GMGN (G18, G20, G25)

Источник метрик GMGN кандидатов (insiders, bundling, phishing, top-10, доля девелопера, total fees; у OpenAPI и посредника — ещё объём токена за 5 мин и час, как в Trending, цена и капитализация токена, ATH, снайперы, статус девелопера и wash trading) выбирает worker: есть ключ — официальный GMGN OpenAPI (основной путь, G20); нет ключа, но задан `GMGN_URL` — посредник в окне Chromium на вашем ПК (G25, `http://host.docker.internal:8765`) или headless-сервис `gmgn` (`http://gmgn:8080`); ни того ни другого — GMGN не спрашивается, решения с флагом `GMGN_UNAVAILABLE`. Ключ и токен посредника получает только worker, не api.

| Переменная | По умолчанию | compose | Что делает |
|---|---|---|---|
| `GMGN_API_KEY` / `_FILE` | — | да (worker) | ключ GMGN OpenAPI (создаётся на gmgn.ai/ai; для чтения токенов нужен только API key). Секрет |
| `GMGN_API_URL` | `https://openapi.gmgn.ai` | да | адрес OpenAPI |
| `GMGN_API_PLAN` | `free` | да | тариф `free` / `plus` / `pro`; клиент держит 80% лимита тарифа (на кандидата два запроса: `token/info`, `token/security`) |
| `GMGN_API_CACHE_SECONDS` | 300 | да | кэш ответа на токен (не дольше `GMGN_STALE_AFTER_SECONDS`): «текущий» объём токена может отставать на столько же; ранг кандидатов берёт объём GMGN только из кэша, без новых запросов; монитор позиции смотрит девелопера (`DEV_SOLD`) не чаще |
| `GMGN_URL` | пусто | да | путь без ключа: посредник — `http://host.docker.internal:8765`, headless-сервис с `--profile gmgn` — `http://gmgn:8080`; при заданном ключе игнорируется |
| `GMGN_REQUIRED_FOR_LIVE` | пусто = `true` | да (worker и api) | `true`: без свежих данных GMGN вход в LIVE закрыт. `false` — единственный способ открыть LIVE без GMGN, ставится осознанно |
| `GMGN_TIMEOUT_SECONDS` | OpenAPI 10, сервис 20 | да | таймаут одного ответа |
| `GMGN_STALE_AFTER_SECONDS` | 900 | да | старше — данные считаются устаревшими |

OpenAPI работает только с IPv4 (по IPv6 GMGN отвечает 401/403 — клиент сам ходит через IPv4). Отказ ключа (`AUTH_*`, `HTTP_401/403`, `PLAN_UPGRADE_REQUIRED`) — 15 минут без запросов, лимит (`RATE_LIMITED`) — пауза до сброса, не дольше 15 минут; во всех случаях решение получает `GMGN_UNAVAILABLE` с причиной, кандидат не «чистый». Проверка ключа вручную: `docker compose exec worker uv run python -m src.gmgn.smoke --mint <адрес токена>`.

**Посредник GMGN** (G25; не в Docker — на вашем ПК, `scripts/gmgn-bridge.ps1` или `scripts/gmgn-bridge.sh`; скрипт берёт эти имена из окружения, иначе из `.env`, значения не печатает; запуск и разбор отказов — `docs/runbook.md`, §3.1):

| Переменная | По умолчанию | Что делает |
|---|---|---|
| `GMGN_BRIDGE_TOKEN` / `_FILE` | — (обязателен) | тот же токен, что у worker; `_FILE` — только из окружения процесса (в `.env` он называл бы путь внутри контейнера) |
| `GMGN_BRIDGE_HOST` | `127.0.0.1` | адреса, на которых слушает посредник, через запятую: только loopback и частные адреса (мост Docker); `0.0.0.0` и публичные не принимаются. Docker Desktop доходит до loopback хоста как `host.docker.internal`; на Ubuntu с Docker Engine скрипт добавляет адрес `docker0` |
| `GMGN_BRIDGE_PORT` | 8765 | порт |
| `GMGN_BRIDGE_PROFILE_DIR` | `%LOCALAPPDATA%\MeteoraVolumeBot\gmgn-bridge-profile` (Ubuntu: `~/.local/share/meteora-volume-bot/gmgn-bridge-profile`) | профиль браузера с вашей сессией GMGN; внутри репозитория не принимается |
| `GMGN_BRIDGE_REQUESTS_PER_MINUTE` | 20 | потолок запросов к gmgn.ai в минуту (на токен около 5: пачка `mutil_window_token_info` на ждущие токены, `token_stat`, `token_holder_stat`, `token_fee_info`, свечи капы `1d`; Trending — не чаще раза в 2 минуты) |

Общие с headless-сервисом настройки посредник тоже читает: `GMGN_CACHE_TTL_SECONDS` (кэш ответа на токен), `GMGN_BREAKER_COOLDOWN_SECONDS` (пауза после проверки Cloudflare, капчи, блока или 429 — сразу, без трёх отказов подряд; после серии других отказов — как у сервиса), `GMGN_PAGE_TIMEOUT_SECONDS`, `GMGN_RESPONSE_WAIT_SECONDS`, `GMGN_BASE_URL`. Вкладка одна, запросы к gmgn.ai идут по одному. Ответ — `gmgn-signals.v2` (v1 и рыночные поля); worker принимает и v1, и v2.

Сервис `gmgn` (профиль `gmgn`, выключен по умолчанию; headless):

| Переменная | По умолчанию | compose |
|---|---|---|
| `GMGN_PAGE_TIMEOUT_SECONDS` | 45 | да |
| `GMGN_CACHE_TTL_SECONDS` | 600 | да |
| `GMGN_MAX_PAGES` | 2 | да |
| `GMGN_BREAKER_COOLDOWN_SECONDS` | 900 | да |
| `GMGN_BASE_URL` (только https), `GMGN_HOST`, `GMGN_PORT`, `GMGN_SETTLE_SECONDS` (8), `GMGN_MAX_PENDING` (16), `GMGN_RESPONSE_WAIT_SECONDS` (10), `GMGN_BACKOFF_SECONDS` (60), `GMGN_BACKOFF_MAX_SECONDS` (1800), `GMGN_BREAKER_THRESHOLD` (3) | в скобках | нет |

**Ограничения.** Headless-сервис построен, но gmgn.ai через Cloudflare отдаёт headless-браузеру 403, и сервис отвечает `UNAVAILABLE` (причина `ACCESS_BLOCKED` или `API_BLOCKED`). Посредник работает только в видимом окне, пока оно открыто и ПК не спит; так пользоваться GMGN — против его правил. Обхода защит нет и не будет: никаких решателей капч, плагинов маскировки, подмены отпечатков и выноса cookie (§6.7). Без ключа и без посредника бот работает с флагом `GMGN_UNAVAILABLE`: в APPROVAL карточка подтверждения пишет «GMGN недоступен — проверь вручную», а автоматический LIVE закрыт, пока `GMGN_REQUIRED_FOR_LIVE` не выставлен в `false`. Единица `total_fee` (SOL) выведена сборкой по совпадению со страницей токена — документация GMGN её не называет.

## Бесплатные источники токена: Jupiter и RugCheck (T22, ADR 0014)

Без ключей и денег. Jupiter Tokens API v2 читается на весь цикл кандидатов до решений (пачками по 100 токенов): объём токена по всем площадкам (если нет свежего GMGN), капитализация, сделки и трейдеры за 5 минут, органичность (`low` — отказ как накрутка), mint/freeze, доля девелопера. RugCheck — только для кандидатов глубокой проверки: любой риск `danger` — отказ `RUGCHECK_DANGER`. Оба необязательны: сбой или выключенный источник ничего не закрывает, решение показывает `jupiter_status` / `rugcheck_status`.

| Переменная | По умолчанию | compose | Что делает |
|---|---|---|---|
| `JUPITER_API_URL` | compose: `https://api.jup.ag`; без compose — выключен | да (worker) | адрес Jupiter; пустая строка `JUPITER_API_URL=` в `.env` выключает источник |
| `JUPITER_API_KEY` / `_FILE` | — | да (worker) | необязательный бесплатный ключ (portal.jup.ag): 1 запрос в секунду вместо 0,5 (клиент держит 80%). Идёт только в заголовке `x-api-key`. Секрет |
| `JUPITER_CACHE_SECONDS` | 120 | да | показание старше — спрашивается снова в следующем цикле; отбору оно отдаётся до max(5 мин, 2× этого значения), чтобы дожить до последнего пула цикла и пережить отказ повторного запроса |
| `JUPITER_MAX_TOKENS` | 500 | да | сколько верхних токенов цикла спрашивать (100 на запрос): 500 — 5 запросов, около 10 секунд без ключа |
| `RUGCHECK_API_URL` | compose: `https://api.rugcheck.xyz`; без compose — выключен | да (worker) | адрес RugCheck; `RUGCHECK_API_URL=` выключает |
| `RUGCHECK_CACHE_SECONDS` | 1800 | да | сколько держится отчёт по токену (сбой — 2 минуты); не чаще запроса в секунду |

## Пороги `guide-v1` и `RULESET_OVERRIDES_FILE`

`guide-v1` — версия правил по гайду LexDollar (§6.6, G15–G16) и сверке с гайдом и видео (§6.8, G23–G24), выбрана по умолчанию. Все числа — **стартовая гипотеза для SHADOW**, а не обещание результата; числа без опоры в источнике — стартовые значения сборки. Версия правил и версия конфигурации пишутся в каждое решение.

Файл переопределений (YAML), путь в `RULESET_OVERRIDES_FILE`:

```yaml
config_version: "my-tuning-1"   # обязательна и не может быть "worker-config-v1"
thresholds:                     # только поля GuidePolicySettings; неизвестное поле — отказ старта
  minimum_market_cap: "750000"  # десятичные числа — строками: binary float запрещён
  minimum_token_age: "PT3H"     # длительности — ISO 8601 (PT30M, PT2H)
```

Переопределения работают только для `guide-v1`; с `RULESET_VERSION=shadow-hypothesis-v1` файл — ошибка старта. Невалидный файл останавливает worker до открытия хранилищ.

Основные пороги (полный список — `GuidePolicySettings` в `src/worker/policy.py`):

| Поле | Значение | Смысл и источник |
|---|---|---|
| `minimum_market_cap` / `preferred_market_cap` | 500 000 / 1 000 000 USD | капа токена; статья §4, видео 2 |
| `minimum_token_age` | 2 ч (`PT2H`) | чеклист статьи; без RPC оценивается снизу по возрасту пула |
| `maximum_holder_concentration` | 0.30 | top-10 без pool vaults и известных программ; чеклист статьи (нужен RPC). Порог включительный: ровно 30% — уже отказ `HOLDER_CONCENTRATION_HIGH` |
| `minimum_volume_per_minute` / `overvolume_volume_per_minute` | 14 000 / 100 000 USD | текущий объём токена в минуту (окно 5m) и сверхобъём; видео 1 и 2. Окно пула — производное короткое окно из `cumulative_metrics` (D07) |
| `minimum_hourly_volume_per_minute` | 14 000 USD | **устойчивый объём** (G24): средний объём токена в минуту за последний час; ниже — `HOURLY_VOLUME_PER_MINUTE_BELOW_THRESHOLD`. Объём по токену: с ключом GMGN — объём токена GMGN, как в Trending; без ключа — сумма DLMM-пулов токена (флаг `TOKEN_VOLUME_POOL_ONLY`) |
| `target_volume_per_minute` | 20 000 USD | «хороший» объём в минуту для балла (гайд: 20k+ в минуту); не порог |
| `maximum_deep_candidates` | 50 | сколько токенов цикла (по часовому объёму; ранг по объёму GMGN из кэша, иначе по пулам) проверяются глубоко; остальные — `CANDIDATE_RANK_BELOW_LIMIT` |
| `minimum_volume_to_liquidity` | 0.5 | объём короткого окна к ликвидности; чеклист статьи |
| `minimum_total_fees_sol` / `total_fees_reference_market_cap` / `maximum_hourly_turnover` | 30 SOL / 500 000 USD / 10 | накрутка (G24, видео 2): требуемые total fees = 30 SOL × капа / 500 000; если total fees GMGN ниже требуемого **и** оборот пула за час ≥ 10 × TVL — `ARTIFICIAL_VOLUME_SUSPECTED` (как и флаг wash trading GMGN). Total fees — только у GMGN; **условный** (D03): нет значения — флаг `TOTAL_FEES_UNAVAILABLE` и строка в карточке подтверждения |
| `minimum_fee_efficiency` | 0.5 | органичность комиссий против base fee пула; в отборе больше не порог (в DLMM это отношение по построению около 1 и выше), нужна только Spot-стратегии |
| `excluded_theme_words` | Илон, Маск, Трамп, MAGA, Меланья, Байден, Камала, Обама, Путин, Зеленск\*, политик\*, президент\*, выборы, стрим\*, Twitch — по-русски и по-английски | тема в имени или тикере (`THEME_EXCLUDED`): сравнение по словам без учёта регистра и по склейкам с мем-приставками (`BABYTRUMP`, `ELONDOGE`), обычные слова (`WATERMELON`, `Trumpet`) проходят; `*` в конце слова — основа. Список заменяется целиком (YAML-список строк) |
| `maximum_sniper_count` / `revived_token_age` | 20 / 60 дней (`P60D`) | флаги без отказа: снайперов по GMGN больше 20 (`SNIPERS_HIGH`), монета старше 60 дней (`TOKEN_REVIVED`) |
| `maximum_insiders_share` / `maximum_bundling_share` / `maximum_phishing_share` / `maximum_gmgn_top10_holder_share` | 0.10 / 0.70 / 0.30 / 0.30 | метрики GMGN (§6.7); порог включительный (равное порогу значение уже отклоняет); без свежих данных GMGN не применяются, вместо них флаг `GMGN_UNAVAILABLE` |
| `minimum_tvl` / `minimum_depth` / `maximum_price_impact` | 10 000 / 5 000 / 0.01 | ликвидность и глубина; глубина — из котировки `executor-quotes` (D07), без неё `DEPTH_UNAVAILABLE` |
| `minimum_pool_age` | 30 мин | только прогрев истории пула |
| `minimum_short_ratio` / `minimum_long_ratio` / `minimum_robust_z_score` | 3 / 1.5 / 4 | всплеск объёма (§6.2): в `guide-v1` больше не фильтр, а флаг `VOLUME_SPIKE` и компонент балла |
| `minimum_score` | 0.5 | итоговый балл (`SCORE_BELOW_THRESHOLD`). Порога комиссий пула к TVL в `guide-v1` нет с 2026-10-25 (ADR 0016: у автора его нет) — fee/TVL только строка решения и компонент балла; `minimum_fee_tvl` в файле правил больше не принимается. Старые решения с `FEE_TVL_BELOW_THRESHOLD` остаются в журнале |
| `baseline_points` / `minimum_baseline_points` | 12 / 6 | сколько окон истории нужно для базы; новый кандидат прогревается из `volume/history` |
| `bid_ask_minimum_depth` / `bid_ask_target_depth` / `bid_ask_maximum_depth` | 0.70 / 0.75 / 0.80 | Bid-Ask от цены вниз (G15), только quote-токен; если 70 бинов пула не дают минимума — `RANGE_TOO_NARROW`, плана нет |
| `maximum_position_bins` | 70 | ширина позиции в бинах — одна транзакция OPEN официального SDK (D10). Bid-Ask на 70% проходит только при bin step ≳ 176; шире 70 бинов SDK строит позицию цепочкой транзакций, восстановления после её обрыва в executor нет, поэтому поднимать значение **нельзя** |
| `bid_ask_local_high_fraction` | 0.90 | вход Bid-Ask с хаёв: цена не ниже 90% хая за `local_high_lookback` (1 ч), иначе `NOT_NEAR_LOCAL_HIGH`; в APPROVAL/LIVE перепроверяется по свежему активному бину |
| `spot_depth` | 0.60 | Spot односторонний: только quote-токен, равномерно от цены вниз; глубина — сколько дают 70 бинов, но не больше 60% |
| `spot_ath_minimum_market_cap` | 1 000 000 USD | Spot на пробитии ATH — только «хайкап». ATH токена берётся из GMGN, без ключа — ATH пула (флаг `ATH_POOL_ONLY`) |
| `liquidity_slippage` | 0.30 | slippage ликвидности как у автора: активный бин может сдвинуться на 30% ширины диапазона между планом и сетью (`ACTIVE_BIN_DRIFT`; это же бин-slippage SDK у executor) |
| `bid_ask_take_profit` / `spot_ath_take_profit` / `overvolume_take_profit` | 0.05 / 0.05 / 0.03 | тейк-профит по net P&L позиции с комиссиями (консервативный учёт: рост цены не засчитывается). **Только SHADOW**: в APPROVAL/LIVE не измеряется (`take_profit=UNMEASURED`) |
| `holding_horizon` / `spot_holding_horizon` / `overvolume_maximum_holding_period` | 24 ч / 3 ч / 30 мин | максимальное удержание: Bid-Ask / Spot на ATH / Spot на сверхобъёме |
| `bid_only_upside_grace` / `bid_only_upside_exit` | 30 мин / 0.05 | выход по верху: цена над диапазоном дольше 30 мин или ушла выше на 5% |
| `volume_decay_ratio` / `overvolume_volume_decay_ratio` | 0.50 / 1 | выход при падении объёма вдвое против входа; у Spot на сверхобъёме — при первом падении |
| `price_shock_fraction` | 0.80 | ценовой шок, равный глубине самого широкого Bid-Ask |
| `shadow_priority_fee_lamports` | 1 000 000 (0,001 SOL) | priority fee одной транзакции в SHADOW (вход, выход, продажа), не больше `PRIORITY_FEE_MAX_LAMPORTS`. **Стартовая оценка сборки**, не рыночные данные: сверяется с `priority_fee_lamports` квитанций APPROVAL. В пулах не к SOL не учитывается (флаг `PRIORITY_FEE_UNPRICED`) |
| `entry_notional_quote` / `max_slippage_bps` | 100 / 100 | эталонный размер котировки глубины **в USD** (не размер позиции — его задаёт лимит риска 1%) и допустимое проскальзывание (price impact) входа. Размер в токене = USD × 10^decimals / цена quote-токена в USD (`quote_price_usd` из Data API); глубина тоже в USD. Нет цены quote-токена — `depth_source` с причиной `QUOTE_PRICE_UNKNOWN`, вход закрыт. `quote_base_units_per_unit` используется только для фикстур, которые сами меряют глубину |
| `short_window_interval_tolerance` | 30 с (`PT30S`) | насколько интервал между снимками может быть короче окна 5m и всё ещё дать его (4 мин 30 с); возврат счётчика к уровню до сброса считается от метки до сброса |

Лимиты риска (`RiskPolicy`, файлом не меняются): позиция до 10% капитала, расчётный риск до 1%, не больше трёх позиций, дневной стоп 4% (realized + conservative unrealized от капитала на начало UTC-суток), не больше 10% ликвидности пула. Связывает обычно лимит риска: расчётный убыток = размер × доля потери при инвалидации (глубина диапазона, не меньше оценки по волатильности), поэтому размер ≈ 1% капитала / глубина — около **1,3–3,5% капитала** (Bid-Ask на 75% — ~1,3%, Spot на 60% — ~1,7%, Spot, которому 70 бинов дают ~30%, — ~3,5%), а не 10%. Политику kill switch для открытых позиций (`HOLD_AND_ALERT` или `ORDERLY_EXIT`) оператор выбирает при включении kill switch; она хранится в `mode_state` (миграция 0003) и одна для worker, `ExecutionService` и экрана подтверждений (`docs/runbook.md`, §9). Конфигурацией она не задаётся.
