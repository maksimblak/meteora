# 04 — Shadow broker, учёт и допуск режимов

**Требования:** R19, R26i, R27i, R29i, R30i, G01, G02, G03, G09, G10, G11, G12
**Зависит от:** 03
**Зона:** `src/accounting/` · `src/portfolio/` · `src/modes/` · `tests/accounting/` · `tests/modes/`
**Волна:** 4
**Ревью:** да — P&L, режимы и аварийные лимиты
**Модель:** сильная

## Что должно заработать

Shadow broker исполняет plans без денег по консервативной модели costs, ведёт ledger и позиции, сравнивает net P&L с HODL и собирает promotion evidence. State machine разрешает только SHADOW → APPROVAL → LIVE и всегда допускает немедленное понижение/kill switch.

## Из брифа, дословно

> «сначала 7–14 дней shadow/paper без денег, затем сделки с ручным подтверждением, после успешной проверки — автоматический live-режим»
>
> «дневной стоп — 4%»

## Разделы спецификации

Истории 19, 26–27, 29–30, 33–35, 41–44; §5.7; §7; границы `application.accounting` и `domain.risk`.

## Критерии приёмки

- [ ] shadow open/mark/rebalance/close работают без executor и создают тот же lifecycle, что live receipts
- [ ] ledger считает fee income, divergence/impermanent loss, slippage, network/Jito estimates, net P&L и HODL benchmark
- [ ] promotion evidence показывает минимум 7 дней, рекомендацию 14, sample size, expectancy, drawdown, coverage и alerts
- [ ] прямой SHADOW → LIVE и повышение без evidence/operator confirmation отклоняются, а понижение всегда разрешено
- [ ] APPROVAL требует одноразовый approval с TTL; просроченный/повторный approval не меняет состояние
- [ ] daily stop использует equity начала UTC risk day, сохраняется после рестарта и блокирует новые входы при 4%
- [ ] kill switch отменяет неподтверждённые планы и применяет настроенную политику к открытым позициям без panic transaction
- [ ] повторная обработка shadow fill/receipt идемпотентна и не удваивает position или P&L
- [ ] performance summary показывает статистику отдельно по ruleset и режиму, не смешивая несовместимые периоды

