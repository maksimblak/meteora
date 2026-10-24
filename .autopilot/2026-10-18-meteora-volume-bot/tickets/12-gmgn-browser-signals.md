# 12 — Метрики GMGN через браузер

**Требования:** G01, G06, G07, G16, G18, D09
**Зависит от:** 09, 10, 11, 14
**Зона:** `services/gmgn/` · `Dockerfile.gmgn` · `docker-compose.yml` · `src/gmgn/` · `src/market/models.py` · `src/domain/screening/` · `src/worker/policy.py` · `src/worker/runtime.py` · `src/providers/solana.py` · `tests/providers/` · `tests/gmgn/` · `.env.example`
**Волна:** 9
**Ревью:** да — внешний источник риска и fail-closed для live
**Модель:** сильная

## Что должно заработать

Для каждого кандидата бот узнаёт те же метрики, на которые смотрит автор гайда в GMGN: insiders, bundling, phishing, top-10, доля девелопера, total fees. Их добывает отдельный сервис с настоящим браузером без окна. Пороги гайда отсекают токены, а недоступность GMGN честно видна и в live закрывает вход.

## Из брифа, дословно

> «Метрики GMGN (insiders, bundling, phishing сделаем реверс»
>
> «По GMGN жду твой выбор: A (браузер)»

## Разделы спецификации

История 50; §6.3; §6.6 (поправка D03); §6.7 целиком; §8.1 (Playwright в сервисе `gmgn`); граница `adapters.providers`.

## Критерии приёмки

- [ ] сервис `gmgn` (свой `pyproject.toml` и lock в `services/gmgn/`, официальный Playwright и Chromium, `Dockerfile.gmgn`, профиль compose `gmgn`, по умолчанию выключен) открывает публичную страницу токена как посетитель и извлекает метрики из сетевых ответов самой страницы; основной `uv.lock` не меняется
- [ ] внутренний `GET /v1/tokens/{chain}/{mint}/signals` отдаёт нормализованные insiders, bundling, phishing, top-10, долю девелопера и total fees с `source_at` и freshness; `/health/live`, `/health/ready`; порт только во внутренней сети compose
- [ ] нет решения капч, сервисов-решателей, подделки отпечатков и stealth-плагинов; не прошёл challenge за таймаут — ответ «недоступно» с причиной; кэш на токен, ограниченная конкурентность, backoff, circuit breaker; аккаунт и куки пользователя не используются
- [ ] `GmgnProvider` (httpx) в пакете `src/gmgn/` — клиент внутреннего сервиса и enhanced-провайдер для worker; разбор ответа страницы покрыт тестами на записанных и очищенных ответах; изменение формата GMGN даёт degradation, а не падение
- [ ] пороги §6.7 в `guide-v1`: insiders < 10%, bundling < 70%, phishing < 30%, top-10 < 30% дают reject с отдельными кодами; доля девелопера — флаг и decision record; total fees ≥ 30 SOL применяется, когда значение есть (D03)
- [ ] нет свежих данных GMGN → флаг `GMGN_UNAVAILABLE`: SHADOW идёт дальше с флагом, LIVE закрыт, пока `GMGN_REQUIRED_FOR_LIVE` не выключена явно; тесты на все три режима
- [ ] worker подключает `GmgnProvider` только при включённом сервисе; без него всё работает как раньше; обычные тесты не ходят в сеть, живой прогон — ручная smoke-команда
- [ ] имена evidence метрик GMGN совпадают с теми, что распознаёт панель T06: `gmgn`, `insider`, `bundl`, `phish`, `developer`, `dev_`; флаг `GMGN_UNAVAILABLE` в `CandidateDecision.flags`; панель показывает их без правок
- [ ] (D09) отсутствие enhanced-провайдера не старит базовый token risk; нет RPC — `TOKEN_RISK_UNAVAILABLE` флагом в SHADOW и закрытый вход в APPROVAL/LIVE; настоящая устарелость полученных данных отклоняет; тест worker на реальном `SolanaRpcProvider` с заглушкой транспорта: чистый токен в SHADOW получает ACCEPT
