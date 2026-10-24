# 07 — Worker orchestration, recovery и наблюдаемость

**Требования:** R15, R17, R18, R22, R23, R27i, R29i, R30i, G04, G12, G14, D01
**Зависит от:** 02, 03, 04, 05
**Зона:** `src/worker/` · `src/recovery/` · `src/observability/` · `src/persistence/` · `migrations/` · `tests/worker/` · `tests/recovery/` · `tests/observability/` · `tests/persistence/`
**Волна:** 5
**Ревью:** да — recovery, daily stop и денежный конвейер
**Модель:** сильная

## Что должно заработать

Отдельный worker по leases запускает universe sync, candidate pipeline и более частый position cycle; после прерывания восстанавливает незавершённые команды и сверяет on-chain состояние. Health/readiness, structured logs, metrics, alerts и outbox дают оператору доказуемое состояние без неаудируемых денежных действий.

## Из брифа, дословно

> «всё автоматизировать»
>
> «надо отслеживать там всякие объёмы и так далее»
>
> «будет куртиться в докере»

## Разделы спецификации

Истории 15, 17–18, 22–23, 27, 29–30, 36, 44, 46; §5.3–5.5; §6; §7.3; §8.4; границы `application.pipeline`, `ports.persistence` и события.

## Критерии приёмки

- [ ] scheduler запускает universe, candidate и position cycles с разными интервалами, bounded concurrency и приоритетом открытых позиций
- [ ] PostgreSQL leases/advisory locks не допускают двойной job; истёкший lease восстанавливается после падения owner
- [ ] recovery сначала сверяет pending/unknown commands с сетью и только затем планирует дальнейшее действие
- [ ] ошибка audit persistence блокирует денежное исполнение и поднимает critical alert
- [ ] daily stop, kill switch, mode и активные позиции переживают рестарт worker/API/PostgreSQL clients
- [ ] structured logs содержат correlation/causation IDs и проходят redaction tests на все секретные имена
- [ ] metrics покрывают provider latency/errors, freshness, queue/lease, candidates/rejections, executions, P&L и risk utilization
- [ ] liveness отражает процесс, readiness — пригодность соответствующего режима; provider degradation не выдаётся за healthy live
- [ ] outbox publish/retry идемпотентен, retention не удаляет immutable audit trail
- [ ] (D01) `ModeRepository` и `AccountingRepository` из T04 имеют PostgreSQL-реализацию в общей БД: Alembic-миграция поверх `0001_foundation`, outbox через `OutboxRepository` из T01, те же атомарность и CAS, что у SQLite-адаптера; общие контрактные тесты идут на обеих реализациях
- [ ] (D01) фабрика в `src/persistence/` по конфигурации отдаёт API и worker PostgreSQL-репозитории; SQLite — только для тестов и локальной отладки

