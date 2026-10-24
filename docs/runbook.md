# Runbook оператора

Что делать в штатной работе и в аварии. Все команды — PowerShell из папки проекта (там, где лежит `docker-compose.yml`). Панель: http://127.0.0.1:8000, вход — значение `APP_OPERATOR_TOKEN` из вашего `.env`.

> **Главное.** Бот не обещает доходность. Он открывает LP-позиции в пулах Meteora DLMM на монетах с устойчивым объёмом; позиция может потерять деньги и целиком превратиться в падающий токен, а продажа этого токена после выхода фиксирует убыток, но не возвращает деньги. Тейк-профит — порог выхода, а не ожидаемый результат. Реальные сделки возможны только после SHADOW-окна, допуска и вашего явного подтверждения.

## 1. Ежедневная проверка (2 минуты)

1. Главная страница `/`: режим, kill switch, дневной стоп, статус сервисов, свежесть universe (`FRESH` / `STALE` / `NOT_SYNCED`), открытые critical alerts (с их `alert_id` и формой закрытия, §8).
2. `docker compose ps` — `postgres`, `api`, `worker`, `executor-quotes` в состоянии `running`/`healthy`, `migrate` — `exited (0)`.
3. Ошибки последнего часа: `docker compose logs worker --since 1h | Select-String '"level": "error"'`.
4. Место на диске: размер тома базы — `docker system df -v | Select-String postgres-data`. Рост ограничен (R29i.2): отклонённый пул записывается заново только при смене исхода (статус, причины, флаги, режим, версия правил) — неизменный пул на каждом цикле лишь отмечается как проверенный, без новой записи, события аудита и строки outbox. Исключение — **принятый кандидат: его решение пишется на каждом цикле** (из него строится план). Задача `retention` (раз в час) удаляет снимки пулов, версии universe, векторы признаков и решения без плана старше окна `HISTORY_RETENTION_HOURS` (36 ч; последняя версия universe, последний снимок каждого пула и действующее решение пула остаются); **решение с планом хранится всегда**. Опубликованные события outbox удаляются старше 7 дней. Журнал аудита не чистится никогда: он растёт только на изменениях. Логи контейнеров ротируются (3 файла по 10 МБ на сервис). Ориентир по объёму — в README, «Что нужно».

Метрики Prometheus: api — http://127.0.0.1:8000/metrics (`meteora_api_*`); worker отдаёт свои (`meteora_provider_state`, `meteora_data_freshness_seconds`, `meteora_candidates_total`, `meteora_candidate_rejections_total`, `meteora_open_positions`, `meteora_pnl_base_units`, `meteora_risk_utilization_ratio`, `meteora_job_*`) только во внутренней сети: `docker compose exec worker python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:9100/metrics').read().decode())"`.

## 2. Режимы и promotion

```
SHADOW ──(evidence + токен + подтверждение)──> APPROVAL ──(LIVE evidence + токен + подтверждение)──> LIVE
   ^                                              |                                                 |
   └──────────── понижение и kill switch: всегда, сразу, без повторного токена ────────────────────┘
```

Режим хранится в PostgreSQL. `TRADING_MODE` в `.env` режим **не включает** — он только говорит, шлюз для какого режима подключить. Прямой переход `SHADOW → LIVE` запрещён.

### 2.1 SHADOW (по умолчанию)

Бот сканирует universe, считает признаки, принимает решения и ведёт paper-позиции с консервативной моделью издержек; денег нет, кошелёк не нужен. Доход paper-позиции от комиссий = стоимость позиции / TVL пула × LP-комиссии пула за период и только пока цена в диапазоне позиции. Каждая транзакция — вход, выход и смоделированная продажа монеты после выхода — платит priority fee `shadow_priority_fee_lamports` = 1 000 000 lamports (0,001 SOL), но не больше `PRIORITY_FEE_MAX_LAMPORTS`. Это **стартовая оценка сборки**, а не рыночные данные: сверьте её с `priority_fee_lamports` в квитанциях APPROVAL и при расхождении задайте своё значение в `RULESET_OVERRIDES_FILE`. В пулах не к SOL priority fee нечем пересчитать в quote-токен — он не учитывается, исполнение помечено `PRIORITY_FEE_UNPRICED`. Тейк-профит работает только здесь, в SHADOW. Каждое полное UTC-сутки работы worker записывается как наблюдение для допуска.

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

Каждый вход, выход и каждая часть продажи монеты после выхода ждут вас в `/approvals`. Карточка подтверждения показывает план, симуляцию executor, price impact котировки, метрики GMGN, если они пришли, или флаги (`total fees: нет данных — проверь в GMGN`, `GMGN недоступен — проверь вручную`, «объём токена — сумма его DLMM-пулов…») и обязательный ручной чеклист. Для **входа** — шесть отметок:

- «Twitter и хайп проверены»;
- «запас монет у девелопера проверен»;
- «нет гонки копий монеты (PvP)»;
- «кластеры на Bubblemaps проверены»;
- «график органичный» (без резких искусственных свечей и «лесенки» накрутки — бот проверяет только total fees против капы и оборот пула);
- «тема не Илон/Трамп/политика/стрим» (бот сверяет имя и тикер со списком слов, но смысл картинки и описания не видит).

Для **выхода** и **продажи монеты** — первые четыре: они только снижают риск уже купленной монеты. Без всех отметок своей карточки, повторного ввода токена и галочки «подтверждаю необратимость» approval отклоняется (`CHECKLIST_INCOMPLETE` с перечнем недостающих). Approval живёт `OPERATOR_APPROVAL_TTL_SECONDS` (90 с по умолчанию) и не дольше симуляции (2 мин) и самой команды; просроченную карточку подтвердить уже нельзя (`SIMULATION_STALE`, `COMMAND_EXPIRED`), и она не считается в «ждут подтверждения» — если кандидат по-прежнему проходит фильтры, бот предложит новый вход на следующих циклах; пока по пулу есть вход в работе, второй план по нему не строится. Истёкшая неодобренная карточка выхода не тратит попытку (из 5): бот выпускает новую. Ответ executor `409` (`idempotency_conflict` / `approval_already_used`) — исход «неизвестен» со сверкой, а не провал. Отметки и комментарий пишутся в аудит.

Исполнение (D08, G22): и Bid-Ask, и Spot ставятся целиком ниже цены и только в quote-токене — обмена перед входом нет, в кошельке нужен только quote-токен (иначе `INSUFFICIENT_FUNDS` / `WALLET_INSUFFICIENT_QUOTE_TOKEN`). Price impact входа берётся из котировки SDK и не может быть выше slippage команды (`SLIPPAGE_BREACH`, без котировки — `PRICE_IMPACT_UNAVAILABLE`); выход никогда не блокируется price impact. Прежде чем построить команду входа в APPROVAL/LIVE, бот ещё раз проверяет по свежему активному бину пула, что цена не ниже 90% хая за час (`NOT_NEAR_LOCAL_HIGH`); цена входа в учёте — по этому бину. Монету, которую вернул выход, бот продаёт отдельными командами `SWAP` — см. §2.7.

### 2.4 Что нужно для APPROVAL и LIVE

Compose-сервис `executor` (профиль `live`) получает из `docker-compose.yml` только настройки priority fee (`PRIORITY_FEE_*`, см. ниже), а worker/api получают денежные переменные лишь частично (`docs/configuration.md`). Поэтому денежная конфигурация задаётся вашим `docker-compose.override.yml` с секретными файлами. Каркас (все `[… — впиши]` замените своими значениями; файлы секретов держите вне Git):

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

**Priority fee** (№ 49) — у каждой транзакции executor: входа, выхода и продажи. Цена compute unit — `PRIORITY_FEE_PERCENTILE` (75-й) перцентиль недавних priority fees сети по пулу, не ниже `PRIORITY_FEE_MIN_MICRO_LAMPORTS` (1000; это же цена, когда данных нет) и не дороже `PRIORITY_FEE_MAX_LAMPORTS` за транзакцию (15 000 000 lamports = 0,015 SOL, как у автора). При нагрузке сети транзакции с потолком могут не проходить — тогда поднимите `PRIORITY_FEE_MAX_LAMPORTS` в `.env` (одно значение читают executor, worker и api) и перезапустите стек. Фактический priority fee каждой транзакции приходит в квитанции (`settlement.priority_fee_lamports`) и идёт в учёт, если quote-токен — SOL; в SHADOW вместо него — оценка `shadow_priority_fee_lamports` (§2.1).

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

**В LIVE ручной чеклист никто не проходит.** Карточки подтверждения нет, поэтому то, что в APPROVAL проверяли вы, в LIVE либо делает бот, либо не делает никто:

| Пункт чеклиста | Что бот делает сам в LIVE |
|---|---|
| запас монет у девелопера | доля девелопера по GMGN больше 0 — отказ `DEVELOPER_HOLDS_TOKENS`; доля неизвестна — ответ GMGN неполный, вход закрыт (`GMGN_UNAVAILABLE`); в позиции — выход `DEV_SOLD`, если девелопер продал |
| тема не Илон/Трамп/политика/стрим | имя и тикер сверяются со списком слов и мем-склеек (`THEME_EXCLUDED`, в любом режиме); без имени и тикера вход закрыт (`GUIDE_SIGNAL_UNKNOWN`, сигнал `theme`) |
| график органичный | только косвенно: total fees против капы при обороте пула ≥ 10 × TVL и флаг wash trading GMGN (`ARTIFICIAL_VOLUME_SUSPECTED`); без числа сделок и участников LIVE закрыт (`VOLUME_PARTICIPANTS_UNAVAILABLE`) |
| Twitter и хайп, гонка копий (PvP), кластеры Bubblemaps | **не проверяется никем** |

Кроме того, в LIVE (и APPROVAL) вход закрыт без глубины по котировке (`DEPTH_UNAVAILABLE`), без риска токена по RPC (`TOKEN_RISK_UNAVAILABLE`) и без active bin и decimals (`ENTRY_ANCHOR_UNAVAILABLE`). Включая LIVE, вы соглашаетесь, что три непроверяемых пункта не проверяются.

**GMGN и LIVE.** По умолчанию (`GMGN_REQUIRED_FOR_LIVE` пусто = `true`) вход в LIVE без свежих данных GMGN закрыт. Свежие данные даёт только официальный GMGN OpenAPI по вашему ключу `GMGN_API_KEY` (G20): запасной браузерный сервис упирается в 403 от Cloudflare (G18). Без ключа автоматический LIVE на практике закрыт; открыть его без GMGN можно, только осознанно выставив `GMGN_REQUIRED_FOR_LIVE=false` (worker и api) и проверяя метрики GMGN вручную.

Стартовый live cap ниже общих 10% (§7.2) отдельной настройкой не вынесен: лимиты риска одинаковы для всех режимов. Фактический размер позиции задаёт лимит риска 1%, а не потолок 10%: расчётный убыток = размер × глубина диапазона (не меньше оценки по волатильности), поэтому позиция выходит около 1,3–3,5% капитала (Bid-Ask на 75% — около 1,3%, Spot на 60% — около 1,7%, Spot, которому 70 бинов дают меньшую глубину, — до ~3,5%). `entry_notional_quote` в `RULESET_OVERRIDES_FILE` — эталонный размер котировки глубины в USD, а не размер позиции.

### 2.6 Понижение режима

`/mode` → форма понижения (токен повторно не нужен) или `POST /api/v1/mode/transitions` с `{"target_mode":"SHADOW","reason":"..."}` и Bearer-токеном. Применяется сразу к новым входам.

Открытый вопрос: если вы вручную понизили режим до SHADOW, выход позиции, открытой в APPROVAL, панель не одобряет (одобряются только аварийные выходы: под дневным стопом и под kill switch с политикой `ORDERLY_EXIT`). Спецификация этот случай не регулирует. Перед ручным понижением закройте APPROVAL-позиции или будьте готовы закрыть их вручную в интерфейсе Meteora.

### 2.7 Продажа монеты после выхода (G22)

Bid-Ask стоит ниже цены в quote-токене; если цена упала в диапазон, выход (REMOVE/CLOSE) возвращает часть средств **монетой**. Бот продаёт эту монету в quote-токен в том же DLMM-пуле через официальный SDK (`SWAP_AFTER_EXIT`, по умолчанию включено; настройки `SWAP_*` — `docs/configuration.md`). Продажа фиксирует убыток, но не отменяет его.

- **Сколько продаётся:** ровно столько монеты, сколько вернул выход по расчёту сети (`delta_x` в квитанции), не больше: executor отказывает продаже сверх полученного позицией (`SWAP_EXCEEDS_EXIT_RETURNS`). Чужие монеты кошелька бот не трогает.
- **Лимиты одной команды:** price impact котировки ≤ `SWAP_MAX_PRICE_IMPACT_BPS` (300 = 3%), минимальный выход в сети — котировка минус `SWAP_SLIPPAGE_BPS` (100 = 1%): ниже минимума транзакция не исполнится. Impact выше лимита (`PRICE_IMPACT_BREACH`) или нехватка ликвидности в пуле (`SWAP_QUOTE_INSUFFICIENT_LIQUIDITY`) — часть делится пополам, не больше `SWAP_AFTER_EXIT_MAX_SPLITS` (4) раз, то есть самая малая часть — 1/16 полученного. Одновременно в работе только одна часть; неудачных частей — не больше `SWAP_AFTER_EXIT_ATTEMPTS` (5).
- **Режимы:** SHADOW моделирует продажу по котировке и комиссии пула в той же проводке, что и выход; APPROVAL — каждая часть отдельной карточкой (сумма, ожидаемый и минимальный выход, impact и лимит, чеклист выхода); LIVE — автоматически. Kill switch (любая политика) и дневной стоп продажу **не блокируют**: она снижает риск.
- **Слот позиции:** закрытие (CLOSE) не-SHADOW позиции проводится в учёте только после окончания продажи; до этого позиция занимает один из трёх слотов и отображается открытой. В учёт идут суммы из квитанций продажи; комиссии сети — из квитанции, если quote-токен — SOL, иначе из модели издержек.

Статусы продажи (поле `exit_sale` события `PositionClosed`: статус, сколько вернулось, продано и не продано): `DISABLED` — продажа выключена; `NOT_REQUIRED` — монеты не вернулось; `PENDING` — ждёт расчёта сети, сверки или шлюза; `AWAITING_APPROVAL` — карточка ждёт вас; `SELLING` — идут части; `SOLD` — продано; `UNSOLD` — монета осталась в кошельке.

**Если монета не продана** (alert `EXIT_SALE_IMPACT_EXCEEDED` — даже 1/16 выше лимита или в пуле нет ликвидности; `EXIT_SALE_FAILED` — исчерпаны попытки; `EXIT_SALE_AMOUNT_UNKNOWN` — сеть не сообщила, сколько монеты вернул выход, через `SWAP_SETTLEMENT_TIMEOUT_SECONDS` = 10 мин): продажа закончена со статусом `UNSOLD`, позиция закрывается в учёте, монета остаётся в кошельке. Учёт оценивает непроданную часть по цене выхода — фактический результат может оказаться хуже. Что делать: найти монету в кошельке (эксплорер, Phantom/Solflare), решить — продать вручную (интерфейс Meteora или агрегатор) или держать осознанно, затем закрыть alert из панели (§8) с причиной. Автоматически бот к этой монете не вернётся.

**Если исход части неизвестен** (alert `EXIT_SALE_OUTCOME_UNKNOWN`: команда истекла, а executor не знает её исход, или квитанция есть, но расчёта нет дольше 10 минут): бот только сверяет эту часть с executor и ничего не продаёт повторно; продажа и закрытие позиции стоят, **позиция держит свой слот до разбора — ручного пути в панели нет**. Проверьте подпись части в эксплорере. Когда executor узнаёт исход, продажа продолжается сама и alert закрывается проводкой CLOSE. Если исход так и не появится, слот остаётся занятым: новых входов будет на один меньше.

Прочие alert продажи: `EXIT_SALE_GATEWAY_UNAVAILABLE` — шлюз не ответил, продажа продолжится, когда он ответит (закрывается проданной частью той же попытки или проводкой CLOSE); `EXIT_SALE_BLOCKED` — часть отклонена до записи, нужна проверка по логам executor; `POSITION_CONTEXT_MISSING` — у позиции нет контекста, ни выход, ни продажа не строятся.

### 2.8 Как читать решения `guide-v1`

- **Отбор как у автора (G24).** Нужен устойчивый объём: средний объём токена в минуту за час и текущий (окно 5m) — оба не ниже 14 000 USD (`HOURLY_VOLUME_PER_MINUTE_BELOW_THRESHOLD`, `VOLUME_PER_MINUTE_BELOW_THRESHOLD`); всплеск — плюс к баллу и флаг `VOLUME_SPIKE`. Объём, капа и total fees — по токену: с ключом GMGN — объём токена GMGN, как в Trending (кэш 5 минут, так что «текущий» объём может отставать до 5 минут); без ключа — сумма DLMM-пулов токена на Meteora, флаг `TOKEN_VOLUME_POOL_ONLY` (сверьте объём в GMGN). Строки `token_volume_source`, `market_cap_source`, `token_volume_rank_source` в решении называют источник.
- **Один пул на токен.** В цикле оценивается основной пул токена — с наибольшими LP-комиссиями за час; остальные пулы токена в этом цикле не записываются, на панели у них остаётся прежнее решение. Позиция или вход в работе по токену в любом пуле (SOL-МОНЕТА и МОНЕТА-SOL — один токен) закрывают новый вход (`TOKEN_ALREADY_HELD`). Глубоко (GMGN, RPC, котировка) проверяются верхние 50 токенов цикла по часовому объёму (`CANDIDATE_RANK_BELOW_LIMIT`).
- **Пара.** Вход строится только в пуле «монета — SOL/USDC/USDT»: монета второй — `TOKEN_SECOND_IN_PAIR`, два quote-токена — `PAIR_OF_QUOTE_TOKENS`, нет quote-токена — `PAIR_WITHOUT_QUOTE_TOKEN` (строка `pool_pair`).
- **Тема и девелопер.** `THEME_EXCLUDED` — слово темы в имени или тикере (строка `theme` показывает какое); `THEME_UNKNOWN` — у токена нет ни имени, ни тикера. Доля девелопера > 0 — флаг и пункт чеклиста в SHADOW/APPROVAL, отказ в LIVE.
- **Отказ стратегии — отказ кандидата.** Если план не строится — цена ниже 90% хая за час (`NOT_NEAR_LOCAL_HIGH`) или 70 бинов пула не дают Bid-Ask глубины 70% (`RANGE_TOO_NARROW`, D10: bin step меньше ~176) — решение `REJECT` с этой причиной и без плана. В пулах с обычным для мемов bin step 80–100 Bid-Ask не строится, и кандидат проходит только как Spot (пробитие ATH при капе ≥ 1 млн USD или сверхобъём ≥ 100 000 USD в минуту).
- **Выходы.** Тейк-профит (5% / 3% net P&L с комиссиями, рост цены не засчитывается) — только SHADOW; сроки (24 ч / 3 ч / 30 мин), `DEV_SOLD` (с ключом GMGN, проверка не чаще раза в 5 минут), диапазон, объём и риск — во всех режимах. Пулы открытых позиций перечитываются каждые 30 с (`WORKER_POSITION_SNAPSHOT_SECONDS`); это перечитывание живёт в памяти worker и после рестарта начинается заново.

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
| `EXIT_SALE_IMPACT_EXCEEDED`, `EXIT_SALE_FAILED`, `EXIT_SALE_AMOUNT_UNKNOWN` | монета, которую вернул выход, не продана и осталась в кошельке | решить вручную: продать или держать; затем закрыть alert (§2.7, §8) |
| `EXIT_SALE_OUTCOME_UNKNOWN` | исход части продажи неизвестен; позиция держит слот, ручного пути в панели нет | проверить подпись в эксплорере; когда executor узнает исход, продажа продолжится сама (§2.7) |
| `EXIT_SALE_GATEWAY_UNAVAILABLE`, `EXIT_SALE_BLOCKED` | продажа ждёт шлюз / часть отклонена до записи | вернуть executor; система закрывает их проданной частью или проводкой CLOSE (§2.7) |

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
- Последствия сразу: новые входы заблокированы, режим понижается до SHADOW, critical alert `DAILY_STOP_TRIGGERED`. Выход из уже открытых позиций и продажа монеты после выхода (§2.7) не блокируются.
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

Политика действует, пока kill switch включён; сменить её можно только снятием и повторным включением. Продажу монеты, которую уже вернул выход (§2.7), kill switch не останавливает ни при какой политике: она снижает риск.

**Снять** (повторный токен + подтверждение): `/mode` → «Снять kill switch», либо API `POST /api/v1/kill-switch/release` с телом `{"reason":"...","operator_token":"<токен>","confirm":true}`. Режим после снятия остаётся SHADOW.

**Последний рубеж**, если панель недоступна: `docker compose stop worker executor` — останавливает всю автоматику. Позиции на цепи при этом остаются открытыми и без присмотра; выходите вручную в интерфейсе Meteora.

## 10. Обновление кода

1. Бэкап базы (§6).
2. Новая версия поверх старой папки; `.env` и `docker-compose.override.yml` сохраните.
3. `docker compose up -d --build` — сервис `migrate` применит миграции до запуска api и worker.
4. Смена порогов или версии правил = новая `config_version` / `RULESET_VERSION`; evidence, собранный для старой версии, для новой не годится.
5. Проверка поставки без запуска: `powershell -ExecutionPolicy Bypass -File scripts\verify-delivery.ps1 -Path <папка>`; приёмочный прогон стека: `scripts\acceptance-smoke.ps1`.
6. Канонический набор проверок `docker compose --profile test run --rm --build check` можно запускать, не останавливая бота (в том числе в дни SHADOW): он работает в своей базе `meteora_check` того же postgres и не пишет в рабочую `meteora`, поэтому evidence не портит и чистить после него ничего не нужно.
