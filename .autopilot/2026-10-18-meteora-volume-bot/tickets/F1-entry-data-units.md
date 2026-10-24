# F1 — Ремонт ветки: данные о входе, единицы и доход SHADOW

**Требования:** R17, R19, G05, G07, G16, D07
**Зависит от:** —
**Зона:** `src/market/` · `src/domain/screening/` · `src/providers/meteora.py` · `tests/providers/test_meteora.py` · `src/worker/pipeline.py` · `src/worker/policy.py` · `src/accounting/` · `tests/market/` · `tests/domain/` · `tests/accounting/` · `tests/worker/test_pipeline.py` · `tests/worker/test_live_pipeline.py` · `tests/worker/test_live_policy.py` · `tests/worker/test_runtime_policy.py` · `tests/worker/test_live_series.py` · новые `tests/worker/test_f1_*.py`
**Волна:** 11
**Ревью:** да — ремонт ветки
**Модель:** сильная

## Что должно заработать

Решение о входе стоит на честных числах: окно объёма 5m не раздувается после рестарта и сброса счётчика, глубина и ликвидность SOL-пулов меряются в одной единице с порогами, порог top-10 строгий как в гайде. Paper-позиция в SHADOW зарабатывает комиссии пропорционально своей доле — иначе допуск в APPROVAL недостижим в принципе.

## Из брифа, дословно

> «надо отслеживать там всякие объёмы и так далее»
>
> «чтобы зарабатывать на всяких всплесках объёмов»
>
> «исключает […] искусственный объём»

## Разделы спецификации

§6.1 (поправки D04, D07), §6.2, §6.3, §6.6, §7.1; границы `domain.market`, `domain.screening`, `application.accounting` в `interfaces.md`. Пробы ревьюеров, красные на HEAD: `C:\Users\Maks\AppData\Local\Temp\claude\C--Users-Maks\e031e0e7-d09d-43a0-9bde-5d6517e42951\scratchpad\probe_windows.py` (A1, A2) и папка `…\scratchpad\review_b\` (B7).

## Критерии приёмки (находки ревью ветки, дословно)

- [ ] A1 · Craft · src/market/windows.py:125-142, src/worker/policy.py:80 · дельта за интервал от 1 минуты линейно растягивается до окна 5m. Scheduler держит `_next_due` в памяти, поэтому после каждого рестарта universe_sync запускается сразу, и интервал до прошлого снимка получается 1–5 мин. Проба: 1M@t0 и 1.02M@t0+60s дают окно 5m = 100 000 вместо 20 000. Это раздувает short_ratio, z-score, volume_per_minute и может перевести пул в «сверхобъём» (C096) · условие: окно 5m не получается умножением дельты за интервал короче (5m − допуск); такой интервал даёт UNKNOWN INTERVAL_TOO_SHORT либо окно собирается суммой соседних дельт, покрывающих ≥5 мин; тест на тот же ряд — окна 100 000 нет.
- [ ] A2 · Craft · src/market/windows.py:161-164 · после COUNTER_RESET метка до сброса теряется. Если счётчик возвращается выше неё, весь разрыв засчитывается как объём. Проба: 1M@t0, 900k@t5…t35 (сброс), 1.005M@t40 дают окно 105 000 при реальных ~5 000, то есть ложный всплеск (C105) · условие: на этом ряду окно t40 — UNKNOWN или ≤ 5 000; показание выше метки до сброса не меряется от метки после сброса.
- [ ] A3 · Spec/Craft · src/worker/pipeline.py:334-356, :513-524, src/domain/screening/screener.py:648-660, src/worker/policy.py:270-271 · одна несогласованность единиц в четырёх местах: эталон котировки 100 берётся в quote-токене пула (на SOL-пулах это 100 SOL, а не ~100 USD); DepthEvidence.depth = min(TVL в USD, notional/impact в quote-токенах); эту глубину сравнивают с minimum_depth в USD; LiquidityState считается в шкале 1e6 (децималы USDC) для любого quote-токена, а консервативная ветка — от TVL в USD. Для SOL-пулов это почти всегда ложный reject, для дешёвых quote-токенов проверка мягче, чем задумано (C097) · условие: notional, depth, LiquidityState и minimum_depth/minimum_tvl в одной единице. Для SOL-пула размер котировки — USD-эталон, пересчитанный по token_y.price и quote_decimals, depth в USD, масштаб ликвидности по quote_decimals; тест на фикстуре SOL-пула.
- [ ] A5 · Spec · src/domain/screening/screener.py:687-691 · top-10 по RPC отклоняется при `> 0.30`, а §6.6 требует `< 30%`; GMGN-порог top-10 на :433 правильно использует `>=` (C031) · условие: в guide-v1 holder_concentration ровно 0.30 даёт HOLDER_CONCENTRATION_HIGH.
- [ ] B7 · Spec · src/accounting/service.py:290-297, 396-404; src/worker/policy.py:455-460 · в SHADOW доход от комиссий — константа 0 из CostModel, оценка расхождения не даёт роста, сеть и Jito = 0. Каждая paper-позиция закрывается с net P&L < 0, поэтому условие §7.1 «положительный net expectancy» не выполнимо в принципе и APPROVAL недостижим; R19.1 (fee income) закрыт только по букве (C009, C010, C021) · условие: SHADOW-позиция, стоявшая в диапазоне, пока пул зарабатывал комиссии, закрывается с fee_income > 0 пропорционально своей доле ликвидности; тест: позиция = 1% TVL, комиссии пула за удержание 1000 → fee income ≈ 10 и net P&L > 0 при неизменной цене.
