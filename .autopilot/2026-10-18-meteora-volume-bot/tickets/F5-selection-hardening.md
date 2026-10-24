# F5 — Ремонт после точечных ревью T18–T19: отбор и исход кандидата

**Требования:** G23, G24, G04, G06
**Зависит от:** 18, 19
**Зона:** `src/providers/meteora.py` · `src/domain/screening/` · `src/domain/risk/sizing.py` · `src/worker/pipeline.py` · `src/worker/runtime.py` · `src/gmgn/openapi.py` · `src/api/labels.py` (только подписи кодов этого таска) · `tests/providers/` · `tests/domain/` · `tests/gmgn/` · `tests/worker/test_t18_*.py` · `tests/worker/test_live_series.py` · `tests/worker/test_pipeline.py` · `tests/worker/test_live_pipeline.py` · новые `tests/worker/test_f5_*.py` · `tests/api/test_f3_labels.py` (не ослаблять)
**Волна:** 15
**Ревью:** да — ремонт ветки
**Модель:** сильная

## Что должно заработать

Правило «одна позиция на токен» нельзя обойти через пул, где монета стоит второй; фильтр тем не отсекает обычные монеты вроде WATERMELON или Trumpet; с ключом GMGN кандидаты ранжируются по объёму токена из GMGN, как в Trending; отказ стратегии виден в журнале как исход кандидата, а не «принят» без входа.

## Из брифа, дословно

> «Всё сейчас» (сверка с гайдом) · отбор — «Как у автора»

## Разделы спецификации

§6.8 («Отбор», «Тема и человеческие проверки», «Стратегия и вход»); `interfaces.md` — разделы T18, T19.

## Критерии приёмки (находки точечных ревью, дословно)

- [ ] (T18) src/providers/meteora.py:636 · base_mint всегда token_x: пул, где токен стоит в паре вторым (SOL-MEME), не попадает в группу токена и в TOKEN_ALREADY_HELD; через такой пул обходится «одна позиция на токен» · группировка и held видят токен с любой стороны пары, кроме quote-минтов (SOL, USDC и т. п. — список в коде с источником)
- [ ] (T18) src/domain/screening/models.py:309 + screener.py:317 · совпадение по подстроке: WATERMELON/Belong/Felony→elon, Natural Selection→election, MAGAZINE→maga, Computing/Rasputin→putin, Маска/Маскот→маск, Trumpet→trump, Mainstream/Upstream→stream, Musket→musk · эти имена не дают THEME_EXCLUDED, а ELONDOGE/BABYTRUMP/MAGA дают; тест на оба списка
- [ ] (T18) src/worker/runtime.py:503,569 + src/domain/screening/selection.py:85 · при ключе GMGN top-N ранжируется по сумме DLMM-пулов, а §6.8 — «по объёму токена за 1 ч» · ранжировать по объёму GMGN там, где он уже есть в кэше; без него — по сумме пулов
- [ ] (T18) src/gmgn/openapi.py:626 · комментарий «price.volume_* — USD по всем пулам токена» проверен только на однопульном pump-токене, запись WIF этому противоречит · комментарий называет источник «объём токена GMGN, как в Trending», без непроверенного «по всем пулам»
- [ ] (T19) src/domain/risk/sizing.py:30 · отказ стратегии уходит в RiskRejection: в журнале решение ACCEPT, а коды стратегии лежат в RiskReason; §6.8 — «плана нет» · исход кандидата показывает NOT_NEAR_LOCAL_HIGH/RANGE_TOO_NARROW (решение не ACCEPT без плана); русские подписи есть, test_f3_labels зелёный
