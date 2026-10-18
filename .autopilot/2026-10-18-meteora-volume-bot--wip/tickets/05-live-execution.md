# 05 — Official SDK execution gateway и ручное подтверждение

**Требования:** R16, R18, R26i, R27i, R28i, R29i, R30i, G02, G03
**Зависит от:** 01
**Зона:** `executor/` · `src/execution/` · `tests/execution/` · `contracts/execution/`
**Волна:** 2
**Ревью:** да — реальные деньги, секреты и on-chain интеграция
**Модель:** сильная

## Что должно заработать

Изолированный TypeScript gateway на официальном Meteora DLMM SDK умеет подготовить и симулировать open/add/remove/close команды, отправить разрешённую команду и сверить статус. Python orchestration хранит approval/command state, обеспечивает TTL, idempotency и recovery. Default/test startup физически не отправляет транзакции.

## Из брифа, дословно

> «Ну, условно торговлю»
>
> «затем сделки с ручным подтверждением, после успешной проверки — автоматический live-режим»

## Разделы спецификации

Истории 16, 18, 26–30, 34–35; §5.2–5.4, §5.6–5.7; §6.4–6.5; §7; §8.5; границы `application.execution` и `executor`.

## Критерии приёмки

- [ ] versioned contract покрывает Spot/Bid-Ask open, add, remove/close, simulate, submit и status без утечки SDK-типов в Python domain
- [ ] official SDK строит команды и возвращает normalized simulation, fees, price impact, bin/range и errors
- [ ] submit требует внутренний token, разрешённый mode, неистёкший plan/approval, свежую успешную simulation и idempotency key
- [ ] неизвестный/повторный idempotency key ведёт к lookup/reconciliation, а не безусловной повторной отправке
- [ ] wallet key загружается только gateway из secret path; ключ и serialized secret никогда не появляются в response/log/test snapshot
- [ ] timeout, stale blockhash, simulation failure, slippage breach и unknown receipt имеют конечный state machine и bounded retry
- [ ] общий JSON contract corpus проходит на Python и TypeScript сторонах
- [ ] integration tests используют fake RPC/SDK transport и доказывают ноль сетевых submit в SHADOW/default/test
- [ ] readiness остаётся false для live без dedicated RPC/wallet, но gateway health не ломает shadow-приложение

