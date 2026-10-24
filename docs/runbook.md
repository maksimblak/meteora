# Runbook оператора

Что делать в штатной работе и в аварии. Все команды — PowerShell из папки проекта (там, где лежит `docker-compose.yml`). Панель: http://127.0.0.1:8000, вход — значение `APP_OPERATOR_TOKEN` из вашего `.env`.

> **Главное.** Бот не обещает доходность. Он открывает LP-позиции в пулах Meteora DLMM на всплесках объёма; позиция может потерять деньги и целиком превратиться в падающий токен. Реальные сделки возможны только после SHADOW-окна, допуска и вашего явного подтверждения.

## 1. Ежедневная проверка (2 минуты)

1. Главная страница `/`: режим, kill switch, дневной стоп, статус сервисов, свежесть universe (`FRESH` / `STALE` / `NOT_SYNCED`), открытые critical alerts (с их `alert_id` и формой закрытия, §8).
2. `docker compose ps` — `postgres`, `api`, `worker`, `executor-quotes` в состоянии `running`/`healthy`, `migrate` — `exited (0)`.
3. Ошибки последнего часа: `docker compose logs worker --since 1h | Select-String '"level": "error"'`.
4. Место на диске: размер тома базы — `docker system df -v | Select-String postgres-data`. Рост ограничен (R29i.2): решение по пулу записывается заново только при смене исхода (статус, причины, флаги, режим, версия правил) — неизменный пул на каждом цикле лишь отмечается как проверенный, без новой записи, события аудита и строки outbox; задача `retention` (раз в час) удаляет снимки пулов, версии universe, векторы признаков и решения без плана старше окна `HISTORY_RETENTION_HOURS` (36 ч; последняя версия universe, последний снимок каждого пула и действующее решение пула остаются), а опубликованные события outbox — старше 7 дней. Журнал аудита не чистится никогда: он растёт только на изменениях. Логи контейнеров ротируются (3 файла по 10 МБ на сервис). Ориентир по объёму — в README, «Что нужно».

Метрики Prometheus: api — http://127.0.0.1:8000/metrics (`meteora_api_*`); worker отдаёт свои (`meteora_provider_state`, `meteora_data_freshness_seconds`, `meteora_candidates_total`, `meteora_candidate_rejections_total`, `meteora_open_positions`, `meteora_pnl_base_units`, `meteora_risk_utilization_ratio`, `meteora_job_*`) только во внутренней сети: `docker compose exec worker python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:9100/metrics').read().decode())"`.

## 2. Режимы и promotion

```
SHADOW ──(evidence + токен + подтверждение)──> APPROVAL ──(LIVE evidence + токен + подтверждение)──> LIVE
   ^                                              |                                                 |
   └──────────── понижение и kill switch: всегда, сразу, без повторного токена ────────────────────┘
```

Режим хранится в PostgreSQL. `TRADING_MODE` в `.env` режим **не включает** — он только говорит, шлюз для какого режима подключить. Прямой переход `SHADOW → LIVE` запрещён.

### 2.1 SHADOW (по умолчанию)

Бот сканирует universe, считает признаки, принимает решения и ведёт paper-позиции с консервативной моделью издержек; денег нет, кошелёк не нужен. Доход paper-позиции от комиссий = стоимость позиции / TVL пула × LP-комиссии пула за период и только пока цена в диапазоне позиции. Каждое полное UTC-сутки работы worker записывается как наблюдение для допуска.

**Важно для Windows:** счёт суток живёт в памяти worker. Сутки засчитываются, только если worker работал с начала до конца суток без длинного перерыва. Перезапуск worker, перезагрузка или **сон компьютера**, обновление Docker Desktop — и эти сутки потеряны, а счёт подряд идущих дней начинается заново. На время SHADOW-окна отключите сон Windows и автоматические перезагрузки.

### 2.2 Допуск в APPROVAL (§7.1)

Условия проверяет код (`ModeService.shadow_readiness`), панель показывает причины отказа:

- не меньше 7 полных UTC-суток SHADOW подряд, заканчивающихся вчера (рекомендовано 14);
- не меньше 20 завершённых paper-позиций (иначе причина `COMPLETED_POSITIONS_BELOW_20`);
- положительный net expectancy после издержек;
- просадка не выше 4%, нет нарушений risk-инвариантов;
- покрытие данных не ниже 99%. Решения без источника token risk (`TOKEN_RISK_UNAVAILABLE`, нет `SOLANA_RPC_URL`) **не засчитываются** — без RPC допуск не наберётся (D09);
- нет открытых critical alerts (после разбора причины закройте их из панели, §8);
- evidence собран в текущем режиме и для текущей версии правил.

Порядок: войдите в панель → `/mode` → «Собрать evidence» → если `allowed`, в форме повышения до APPROVAL введите причину, токен ещё раз и поставьте галочку.

Перед повышением настройте APPROVAL-исполнение (иначе входы будут отменяться с `EXECUTION_GATEWAY_NOT_CONFIGURED`), см. §2.4.

### 2.3 Работа в APPROVAL

Каждый вход ждёт вас в `/approvals`. Карточка подтверждения показывает план, симуляцию executor, price impact котировки, метрики GMGN, если они пришли, или флаги (`total fees: нет данных — проверь в GMGN`, `GMGN недоступен — проверь вручную`) и обязательный ручной чеклист G17:

- «Twitter и хайп проверены»;
- «запас монет у девелопера проверен»;
- «нет гонки копий монеты (PvP)»;
- «кластеры на Bubblemaps проверены».

Без всех четырёх отметок, повторного ввода токена и галочки «подтверждаю необратимость» approval отклоняется. Approval живёт `OPERATOR_APPROVAL_TTL_SECONDS` (90 с по умолчанию) и не дольше симуляции (2 мин) и самой команды; просроченную карточку подтвердить уже нельзя (`SIMULATION_STALE`, `COMMAND_EXPIRED`), и она не считается в «ждут подтверждения» — если кандидат по-прежнему проходит фильтры, бот предложит новый вход на следующих циклах; пока по пулу есть вход в работе, второй план по нему не строится. Истёкшая неодобренная карточка выхода не тратит попытку (из 5): бот выпускает новую. Ответ executor `409` (`idempotency_conflict` / `approval_already_used`) — исход «неизвестен» со сверкой, а не провал. Отметки и комментарий пишутся в аудит.

Ограничения исполнения (D08): executor не делает обмен токенов. Bid-Ask от хая ставится целиком ниже цены и только в quote-токене (SOL); Spot-вход возможен, только если нужный токен уже есть в кошельке, иначе симуляция отклоняется (`INSUFFICIENT_FUNDS` / `WALLET_INSUFFICIENT_*`). Price impact берётся из котировки SDK и проверяется только на входе; выход никогда не блокируется price impact обмена, которого не происходит.

### 2.4 Что нужно для APPROVAL и LIVE

Compose-сервис `executor` (профиль `live`) не получает переменных из `docker-compose.yml`, а worker/api получают их лишь частично (`docs/configuration.md`). Поэтому денежная конфигурация задаётся вашим `docker-compose.override.yml` с секретными файлами. Каркас (все `[… — впиши]` замените своими значениями; файлы секретов держите вне Git):

```yaml
services:
  executor:
    environment:
      TRADING_MODE: APPROVAL                       # LIVE — только после §2.5
      SOLANA_RPC_URL: "[ВЫДЕЛЕННЫЙ RPC URL — впиши]"
      WALLET_PUBLIC_KEY: "[АДРЕС КОШЕЛЬКА — впиши]"
      WALLET_KEYPAIR_FILE: /run/secrets/wallet.json
      EXECUTOR_STATE_FILE: /var/lib/executor/state.json
      EXECUTOR_INTERNAL_TOKEN: ${EXECUTOR_INTERNAL_TOKEN:-}   # то же значение из .env
      EXECUTOR_MODE_TOKEN_FILE: /run/secrets/executor-mode-token
      EXECUTOR_APPROVAL_SECRET_FILE: /run/secrets/executor-approval-secret
    volumes:
      - "[ПАПКА С СЕКРЕТАМИ — впиши]:/run/secrets:ro"
      - "[ПАПКА ДЛЯ СОСТОЯНИЯ EXECUTOR — впиши]:/var/lib/executor"   # папка Windows: её же бэкапьте (§6)
  worker:
    environment:
      TRADING_MODE: APPROVAL
      EXECUTOR_URL: http://executor:3000
      WALLET_PUBLIC_KEY: "[АДРЕС КОШЕЛЬКА — впиши]"
      EXECUTOR_MODE_TOKEN_FILE: /run/secrets/executor-mode-token
      EXECUTOR_APPROVAL_SECRET_FILE: /run/secrets/executor-approval-secret
    volumes:
      - "[ПАПКА С СЕКРЕТАМИ — впиши]:/run/secrets:ro"
  api:
    environment:
      TRADING_MODE: APPROVAL
      EXECUTOR_URL: http://executor:3000
      EXECUTOR_INTERNAL_TOKEN_FILE: /run/secrets/executor-internal-token   # api получает токен только файлом
      EXECUTOR_APPROVAL_SECRET_FILE: /run/secrets/executor-approval-secret
    volumes:
      - "[ПАПКА С СЕКРЕТАМИ — впиши]:/run/secrets:ro"
```

`EXECUTOR_INTERNAL_TOKEN` задайте в `.env` значением (его получают worker, `executor-quotes` и, через подстановку выше, `executor`), а для api положите то же значение в файл `executor-internal-token`; `EXECUTOR_INTERNAL_TOKEN_FILE` в `.env` при этом оставьте пустым. Секрет approval — 32+ байт, один и тот же файл у executor, worker и api. Запуск: `docker compose --profile live up -d --build`. Проверка: `/settings` → готовность APPROVAL без недостающих переменных; `docker compose exec executor node -e "fetch('http://127.0.0.1:3000/health/ready').then(async r=>console.log(r.status, await r.text()))"` → `200 {"status":"ready","mode":"APPROVAL"}`. Этот каркас не проверялся на реальном кошельке в рамках сборки; перед первым запуском пройдите ручную проверку executor на devnet (`executor/README.md`, адрес пула `[DEVNET_POOL — впиши]` выбирает оператор).

### 2.5 Допуск в LIVE (§7.2)

LIVE требует файла LIVE promotion evidence (`LIVE_PROMOTION_FILE`, его читают executor, worker и api). Это JSON, который **оператор составляет сам** по итогам APPROVAL; система проверяет поля и срок действия, но 10 циклов и сверку receipts сама не пересчитывает — заполняйте честно:

```json
{
  "promotionId": "[UUID — впиши]",
  "rulesetVersion": "guide-v1",
  "approvalCycles": 10,
  "receiptsReconciled": true,
  "riskInvariantsPreserved": true,
  "dedicatedRpcReady": true,
  "walletReady": true,
  "checklistCompleted": true,
  "operatorConfirmationId": "[ВАШ ID ПОДТВЕРЖДЕНИЯ — впиши]",
  "promotedAt": "[ВРЕМЯ UTC, ISO 8601 — впиши]",
  "expiresAt": "[СРОК ДЕЙСТВИЯ UTC — впиши]"
}
```

Дальше: `TRADING_MODE: LIVE` у executor, worker и api, `LIVE_PROMOTION_FILE` всем троим, перезапуск, затем `/mode` → повышение до LIVE с `operatorConfirmationId` из файла, токеном и галочкой необратимости.

**GMGN и LIVE.** По умолчанию (`GMGN_REQUIRED_FOR_LIVE` пусто = `true`) вход в LIVE без свежих данных GMGN закрыт. Свежие данные даёт только официальный GMGN OpenAPI по вашему ключу `GMGN_API_KEY` (G20): запасной браузерный сервис упирается в 403 от Cloudflare (G18). Без ключа автоматический LIVE на практике закрыт; открыть его без GMGN можно, только осознанно выставив `GMGN_REQUIRED_FOR_LIVE=false` (worker и api) и проверяя метрики GMGN вручную.

Стартовый live cap ниже общих 10% (§7.2) отдельной настройкой не вынесен: лимиты риска одинаковы для всех режимов, размер входа задаёт `entry_notional_quote` в `RULESET_OVERRIDES_FILE`.

### 2.6 Понижение режима

`/mode` → форма понижения (токен повторно не нужен) или `POST /api/v1/mode/transitions` с `{"target_mode":"SHADOW","reason":"..."}` и Bearer-токеном. Применяется сразу к новым входам.

Открытый вопрос: если вы вручную понизили режим до SHADOW, выход позиции, открытой в APPROVAL, панель не одобряет (одобряются только аварийные выходы: под дневным стопом и под kill switch с политикой `ORDERLY_EXIT`). Спецификация этот случай не регулирует. Перед ручным понижением закройте APPROVAL-позиции или будьте готовы закрыть их вручную в интерфейсе Meteora.

## 3. Отказ провайдера

| Что отказало | Как видно | Что делает бот | Что делать вам |
|---|---|---|---|
| Meteora Data API | universe `STALE` через 15 мин, worker `DEGRADED` (`PROVIDER_DEGRADED:meteora`), sync `FAILED` в логах | частичная страница не заменяет последний целый snapshot; повторы с backoff и circuit breaker; решения на устаревших данных отклоняются | проверить сеть и https://dlmm.datapi.meteora.ag; ждать; при долгом отказе — понизить до SHADOW |
| Solana RPC | `PROVIDER_DEGRADED:solana`; решения с `TOKEN_RISK_UNKNOWN`/`TOKEN_RISK_UNAVAILABLE` | SHADOW продолжает с флагом, APPROVAL/LIVE — вход закрыт, worker `NOT_READY` | проверить `SOLANA_RPC_URL` и лимиты провайдера; без RPC SHADOW-дни не засчитываются в покрытие |
| `executor-quotes` / котировки | флаг `DEPTH_UNAVAILABLE` у кандидатов | SHADOW считает консервативно (полное проскальзывание, 5% TVL), APPROVAL/LIVE — вход закрыт | `docker compose logs executor-quotes`; нужны `SOLANA_RPC_URL` и `EXECUTOR_INTERNAL_TOKEN` |
| Денежный `executor` | `EXECUTION_GATEWAY_NOT_READY`, approvals «не настроены» | новые входы не отправляются; неизвестный результат отправки сверяется по цепи, не повторяется | `docker compose logs executor`; `/health/ready` executor покажет `missing: configuration/wallet/rpc/price_impact` |
| GMGN | флаг `GMGN_UNAVAILABLE` с причиной (`AUTH_*`, `RATE_LIMITED`, `API_TIMEOUT`, `CIRCUIT_OPEN`…); без ключа — всегда | SHADOW и APPROVAL продолжают с флагом, LIVE закрыт (при `GMGN_REQUIRED_FOR_LIVE=true`); отказ ключа — 15 мин без запросов, лимит — пауза до сброса | проверить ключ и тариф `GMGN_API_PLAN` (`docker compose exec worker uv run python -m src.gmgn.smoke --mint <адрес>`); без ключа это штатное состояние: браузерный путь упирается в 403 Cloudflare, обхода нет |

## 4. Устаревшие данные

- Universe `STALE`: последний успешный sync старше 15 минут. Причины: Data API недоступен, worker остановлен, `WORKER_UNIVERSE_INTERVAL_SECONDS` слишком большой.
- Снимок пула устарел — его решения отклоняются, позиции переоцениваются по последнему снимку; на устаревших данных бот не входит.
- Короткие окна считаются из разницы `cumulative_metrics` снимков (Data API не даёт окна 5m, D07). Окно 5m берётся только из интервала не короче 4 мин 30 с (`short_window_interval_tolerance` = 30 с); при более частом шаге соседние интервалы суммируются. Интервал длиннее шага + max(30 с, 10%) (при шаге короче 4 мин 30 с — ещё + 4 мин 30 с) даёт окно `UNKNOWN`. Возврат счётчика к уровню до сброса считается от метки до сброса.
- После простоя кандидаты прогреваются из `/pools/{address}/volume/history`; пул без истории остаётся `WARMING_UP`, сигналов по нему нет.
- Проверка: `/` (возраст sync), `/pools/{адрес}` (свежесть снимка), `docker compose logs worker --since 30m | Select-String universe`.

## 5. Перезапуск и восстановление

- Обычный перезапуск: `docker compose restart worker api`. Полный: `docker compose down` и `docker compose up -d` (**без `-v`**: `-v` удаляет том базы и всё состояние бота).
- При старте worker сверяет незавершённые команды (`RecoveryService.reconcile(startup=True)`): подтверждённые на цепи входы фиксируются, никогда не отправленные — закрываются, неизвестный результат сверяется с executor, а не отправляется заново. План в исполнении закрывается только под lease `candidate_pipeline` (2 мин).
- Режим, kill switch, дневной стоп, открытые позиции, решения и approvals переживают рестарт — они в PostgreSQL. Без `APP_SESSION_SECRET` после рестарта api нужно войти заново.
- Рестарт стоит текущих суток SHADOW-наблюдений (§2.1).
- Если после рестарта появился critical alert — это ситуация, которую бот не решает сам:

| Alert | Что случилось | Что делать |
|---|---|---|
| `POSITION_PARTIALLY_EXECUTED` | выход (REMOVE) прошёл частично: часть транзакций подтверждена; позиция в `OPERATOR_REQUIRED`, автоматических попыток больше нет, ручного пути в worker нет | открыть позицию по адресу кошелька в интерфейсе Meteora / эксплорере, довести выход вручную, затем закрыть alert (§8) |
| `POSITION_ACTION_ATTEMPTS_EXHAUSTED` | 5 попыток выхода через шлюз не удались | разобраться по логам executor, выйти вручную |
| `EXECUTION_RECOVERY_REQUIRED` | неясное состояние команды после рестарта (по одному на команду) | обычно ничего: когда сверка по command_id даёт исход, система закрывает alert сама («system: …»); если он висит — сверить подпись транзакции в эксплорере и закрыть alert (§8) |
| `ENTRY_FILL_UNTRACKED` | вход исполнен, но учёт его не принял | проверить позицию на цепи, при необходимости закрыть вручную |
| `EXIT_REQUIRES_EXECUTION_GATEWAY` | шлюз не обслуживает режим позиции (не-SHADOW) | вернуть конфигурацию executor; после первого успешного действия по позиции система закроет alert сама |
| `POSITION_ACTION_FAILED`, `EXIT_GATEWAY_UNAVAILABLE` | действие по позиции не прошло | система закрывает их сама после успешного действия по позиции; иначе разбор по логам executor |

## 6. Backup и restore PostgreSQL

В PowerShell 5.1 не перенаправляйте бинарный вывод через `>` — он портит дамп. Дамп пишется внутри контейнера и копируется наружу:

```powershell
New-Item -ItemType Directory -Force backups | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
docker compose exec -T postgres pg_dump -U meteora -d meteora -Fc -f /tmp/meteora.dump
docker compose cp postgres:/tmp/meteora.dump "backups/meteora-$stamp.dump"
docker compose exec -T postgres rm /tmp/meteora.dump
```

Восстановление (остановите всё, что пишет в базу):

```powershell
docker compose stop worker api
docker compose cp backups\meteora-ГГГГММДД-ЧЧММСС.dump postgres:/tmp/meteora.dump
docker compose exec -T postgres pg_restore -U meteora -d meteora --clean --if-exists /tmp/meteora.dump
docker compose exec -T postgres rm /tmp/meteora.dump
docker compose up -d
```

- Делайте бэкап перед обновлением кода, перед сменой версии правил и перед переходом в APPROVAL/LIVE.
- Восстановление откатывает **учёт бота**, но не цепь: если после бэкапа были реальные сделки, позиции на цепи останутся, а бот о них забудет. После restore в APPROVAL/LIVE сверьте кошелёк в эксплорере.
- Файл состояния executor (`EXECUTOR_STATE_FILE`) бэкапьте вместе с базой: в нём идемпотентность отправок.
- Дамп содержит журнал решений и адреса кошелька, но не секреты; храните его как приватные данные.

## 7. Дневной стоп (4%)

- Срабатывает, когда realized + conservative unrealized P&L за UTC-сутки достигает −4% от капитала на начало суток (в SHADOW — от `SHADOW_EQUITY_BASE_UNITS`).
- Последствия сразу: новые входы заблокированы, режим понижается до SHADOW, critical alert `DAILY_STOP_TRIGGERED`. Выход из уже открытых позиций не блокируется.
- Рестарт стоп не снимает. Входы снова разрешены со следующих UTC-суток (03:00 по Москве, 10:00 по Владивостоку). Повышение режима — только заново через evidence.
- Alert `DAILY_STOP_TRIGGERED` сам не закрывается и блокирует новый допуск в APPROVAL (`CRITICAL_ALERTS_OPEN`) и готовность APPROVAL/LIVE; закройте его из панели после разбора (§8).

## 8. Закрытие critical alert вручную

Открытый critical alert закрывает допуск в APPROVAL (`CRITICAL_ALERTS_OPEN`) и готовность APPROVAL/LIVE. Закрывайте его только после разбора причины. Защита — как у снятия kill switch: вход оператора (сессия + CSRF или Bearer), причина, повторный ввод токена и подтверждение.

- **Панель:** главная `/` или `/settings` → в таблице «Critical alerts» у каждого события видны код, русская подпись, сообщение и `alert_id`; войдя, раскройте «Закрыть событие» → причина → токен оператора ещё раз → галочка «причина события разобрана» → «Закрыть».
- **API:** `POST /api/v1/alerts/{alert_id}/resolve` с телом `{"reason":"...","operator_token":"<токен>","confirm":true}` и заголовком `Authorization: Bearer <токен>`; `alert_id` — в `GET /api/v1/system/status` (`alerts[].alert_id`). Ответы: `403 CSRF_FAILED` / `REAUTHENTICATION_FAILED` / `CONFIRMATION_REQUIRED`, `404 ALERT_NOT_FOUND`, `409 ALERT_RESOLVE_REJECTED` (уже закрыт или это alert активного kill switch).

Закрытие записывается в аудит до изменения состояния (событие `CriticalAlertRaised` того же alert с `stage=resolved`, причиной и меткой оператора без токена) и в сам alert. Alert `KILL_SWITCH_ACTIVE` закрывается только снятием kill switch (§9). После закрытия соберите evidence заново (§2.2): `CRITICAL_ALERTS_OPEN` из него уйдёт.

## 9. Аварийный kill switch

**Включить** (без повторного токена, сразу):

- панель: главная `/` или `/mode` → «Kill switch» → причина → политика открытых позиций → «Включить»;
- API: `Invoke-RestMethod -Method Post http://127.0.0.1:8000/api/v1/kill-switch/activate -Headers @{Authorization = "Bearer $env:APP_OPERATOR_TOKEN"} -ContentType "application/json" -Body '{"reason":"ручная остановка","policy":"HOLD_AND_ALERT"}'` (токен положите в переменную сессии PowerShell, не в историю команд).

Что происходит: неподтверждённые планы отменяются, новые входы блокируются, режим понижается до SHADOW, открывается alert `KILL_SWITCH_ACTIVE`; состояние и выбранная политика переживают рестарт (они в `mode_state`).

**Открытые позиции — по политике, выбранной при включении** (§7.3; одна и та же у worker, `ExecutionService` и экрана подтверждений; видна в `/settings` и `GET /api/v1/config/effective` → `kill_switch`):

- `HOLD_AND_ALERT` (по умолчанию): позиции не трогаются, бот только оповещает. Нужен выход — снимите kill switch или закройте позицию вручную в интерфейсе Meteora.
- `ORDERLY_EXIT`: каждая открытая позиция закрывается (CLOSE) через свежую симуляцию; позиция режима APPROVAL — только после вашего подтверждения в `/approvals` (несмотря на понижение до SHADOW). Panic close без свежей симуляции не выполняется намеренно.

Политика действует, пока kill switch включён; сменить её можно только снятием и повторным включением.

**Снять** (повторный токен + подтверждение): `/mode` → «Снять kill switch», либо API `POST /api/v1/kill-switch/release` с телом `{"reason":"...","operator_token":"<токен>","confirm":true}`. Режим после снятия остаётся SHADOW.

**Последний рубеж**, если панель недоступна: `docker compose stop worker executor` — останавливает всю автоматику. Позиции на цепи при этом остаются открытыми и без присмотра; выходите вручную в интерфейсе Meteora.

## 10. Обновление кода

1. Бэкап базы (§6).
2. Новая версия поверх старой папки; `.env` и `docker-compose.override.yml` сохраните.
3. `docker compose up -d --build` — сервис `migrate` применит миграции до запуска api и worker.
4. Смена порогов или версии правил = новая `config_version` / `RULESET_VERSION`; evidence, собранный для старой версии, для новой не годится.
5. Проверка поставки без запуска: `powershell -ExecutionPolicy Bypass -File scripts\verify-delivery.ps1 -Path <папка>`; приёмочный прогон стека: `scripts\acceptance-smoke.ps1`.
