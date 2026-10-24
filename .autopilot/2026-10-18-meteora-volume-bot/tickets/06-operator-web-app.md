# 06 — FastAPI operator web-приложение

**Требования:** R15, R18, R23, R29i, R32i, G01, G02, G03, G04, G13, G17, G18
**Зависит от:** 02, 03, 04, 05, 07, 09, 10
**Зона:** `src/api/` · `src/web/` · `templates/` · `static/` · `tests/api/` · `tests/web/`
**Волна:** 7
**Поправка после T04:** репозитории API берёт из фабрики `src/persistence/` (T07, D01), а синхронные протоколы вызывает через threadpool. Approvals идут через `ExecutionService.approve` из T05 и gateway из T09. В T04 есть свой `ApprovalService`, это известное дублирование, его разберёт ревью ветки, поверх него не строить.
**Ревью:** да — доступ, approvals и переключение live
**Модель:** сильная

## Что должно заработать

FastAPI предоставляет русскоязычную операторскую панель и versioned API: статус/прогрев, пулы и причины фильтрации, решения, позиции, P&L, очередь approvals, promotion evidence, mode transitions и kill switch. Просмотр безопасен, а денежные действия требуют operator auth и CSRF/re-auth.

## Из брифа, дословно

> «Запуск и управление может аппку сделать типо на фастапи»
>
> «Все DLMM-пулы с жёсткими фильтрами»

## Разделы спецификации

Истории 15, 18, 23, 29, 32–36, 45; §5.1–5.2, §5.6–5.7; §7; §8.1, §8.4; граница `api`.

## Критерии приёмки

- [ ] dashboard показывает mode, kill switch, service/provider health, sync/progress, active positions, risk utilization и alerts
- [ ] pools/decisions screens пагинируются и раскрывают evidence, missing signals, reject reasons, ruleset и freshness
- [ ] positions/performance показывают lifecycle, range, valuations, costs, net P&L и HODL comparison
- [ ] первый запуск и пустой universe имеют понятные состояния прогрева/причин, а не пустую таблицу или ложный сигнал
- [ ] approval preview показывает необратимость, simulation, expiry, costs и plan diff; double/expired submit безопасен
- [ ] mode promotion и kill-switch release требуют operator token, CSRF и повторного подтверждения; downgrade/activate доступны немедленно
- [ ] effective config endpoint/UI маскирует секреты и показывает readiness/недостающие имена без значений
- [ ] API models не раскрывают ORM/provider/SDK entities; OpenAPI и error envelope стабильны
- [ ] browser/API tests покрывают unauthorized, invalid input, refresh, duplicate action, stale approval и provider degradation
- [ ] (G17) approval preview показывает ручной чеклист §6.6 — Twitter и хайп, запас у девелопера, PvP, кластеры Bubblemaps; approval без всех отметок отклоняется, отметки и время пишутся в audit
- [ ] (D03) при флаге `TOTAL_FEES_UNAVAILABLE` approval preview показывает строку «total fees: нет данных — проверь в GMGN»
- [ ] (G18) при флаге `GMGN_UNAVAILABLE` approval preview показывает «GMGN недоступен — проверь вручную»; при наличии метрик GMGN (insiders, bundling, phishing, top-10, доля девелопера, total fees) — показывает их рядом с порогами
- [ ] (G15–G16) решения показывают выбранную стратегию и её причину, версию правил и сработавшие пороги `guide-v1`

