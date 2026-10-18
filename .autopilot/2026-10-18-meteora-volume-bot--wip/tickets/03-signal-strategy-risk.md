# 03 — Сигналы, стратегия и pre-trade risk

**Требования:** R02, R15, R17, R18, R19, R20, R22, R27i, R29i, G05, G06, G07, G08, G09, G10, G11, G12
**Зависит от:** 02
**Зона:** `src/domain/features/` · `src/domain/screening/` · `src/domain/strategy/` · `src/domain/risk/` · `tests/domain/`
**Волна:** 3
**Ревью:** да — денежные алгоритмы и риск
**Модель:** сильная

## Что должно заработать

На сохранённой истории рассчитывается устойчивый volume-spike signal, затем hard filters, объяснимый score, Spot/Bid-Ask plan, диапазон и размер позиции. Все решения детерминированы версией правил; недостаток данных отклоняет live-кандидата, а лимиты 10%/1%/3/4% являются инвариантами.

## Из брифа, дословно

> «чтобы зарабатывать на всяких всплесках объёмов»
>
> «по гайду же будем делать ?»
>
> «до 10% капитала в позиции, риск до 1%; максимум три позиции; дневной стоп — 4%»

## Разделы спецификации

Истории 2, 15, 17–20, 22, 27, 29, 37–44; §5; §6.2–6.5; §7.3; границы `domain.screening`, `domain.strategy`, `domain.risk`.

## Критерии приёмки

- [ ] feature engine считает short/long ratios, rolling median, MAD/IQR score, TVL/fee context и freshness без look-ahead
- [ ] hard filters выдают стабильный набор reason codes для liquidity, age, token authority, blacklist, data quality и volume-integrity failures
- [ ] fake-volume detector различает подтверждённую аномалию и недостаток данных; live при `UNKNOWN` закрыт
- [ ] strategy planner выбирает Spot/Bid-Ask, bins, horizon, invalidation и exit policy из типизированных входов
- [ ] sizing берёт минимум из allocation, risk, liquidity и balance caps; property tests доказывают пределы 10% и 1%
- [ ] четвёртая позиция и новый вход после 4% daily drawdown отклоняются до исполнения
- [ ] каждый decision record содержит inputs, evidence, reasons, ruleset/config version и correlation ID
- [ ] golden fixtures дают повторяемый результат, а пороговые boundary tests покрывают значения ровно на/рядом с лимитами
- [ ] тесты документируют расхождение опасных эвристик гайда с безопасными рабочими правилами

