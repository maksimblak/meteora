# 02 — Universe, рыночные данные и token intelligence

**Требования:** R15, R17, R18, G04, G05, G06, G07, G08
**Зависит от:** 01
**Зона:** `src/providers/` · `src/market/` · `tests/providers/` · `tests/market/`
**Волна:** 2
**Ревью:** да — общие данные и внешние интеграции
**Модель:** сильная

## Что должно заработать

Worker получает все DLMM-пулы через Meteora Data API, сохраняет цельные snapshots, накапливает временные окна и обогащает токены on-chain risk-сигналами. Rate limiting, retries, freshness и fail-closed поведение одинаково работают для реального и fixture-провайдера.

## Из брифа, дословно

> «надо отслеживать там всякие объёмы и так далее»
>
> «Все DLMM-пулы с жёсткими фильтрами»
>
> «исключает низкую ликвидность, подозрительные токены, искусственный объём, слишком молодые и опасные пулы»

## Разделы спецификации

Истории 15, 17–18, 36–40; §5.1, §5.3–5.5; §6.1; граница `domain.market` и `adapters.providers`.

## Критерии приёмки

- [ ] cursor/page sync обходит весь доступный universe и атомарно публикует только завершённую snapshot-версию
- [ ] snapshots нормализуют 5m/30m/1h/4h/24h volume, TVL, fees, price, bin step, pool age и timestamps без float для денег
- [ ] Solana RPC adapter извлекает mint/freeze authorities и нормализует token risk; enhanced provider является опциональным интерфейсом
- [ ] источники соблюдают timeout, rate limit, bounded exponential backoff и circuit breaker, не скрывая stale/partial данные
- [ ] `UNKNOWN` обязательного risk-сигнала сохраняется отдельно и не превращается в ложное `SAFE`
- [ ] низкие TVL/depth, недостаточный возраст и неполная история имеют стабильные машинные reason codes
- [ ] fixture providers воспроизводят success, pagination, 429, timeout, malformed payload, stale snapshot и provider outage
- [ ] повторный ingest того же observation идемпотентен и не создаёт дубликаты истории

