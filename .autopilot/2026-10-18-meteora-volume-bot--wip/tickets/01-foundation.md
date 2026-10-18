# 01 — Фундамент и контракты проекта

**Требования:** R22, R23, R24, R28i, R30i, R31i, G13, G14
**Зависит от:** —
**Зона:** `pyproject.toml` · `uv.lock` · `executor/package*.json` · `docker-compose.yml` · `Dockerfile*` · `src/core/` · `src/domain/models/` · `src/persistence/` · `migrations/` · `tests/foundation/`
**Волна:** 1
**Ревью:** да — фундамент, схема и секреты
**Модель:** сильная

## Что должно заработать

Чистый репозиторий превращается в воспроизводимый Docker-проект: FastAPI skeleton, PostgreSQL, типизированная конфигурация, доменные типы, миграции, repositories, audit/outbox primitives и пустой TypeScript executor package. Один check-контейнер запускает lint, types, migrations и тесты обеих частей. Shadow-профиль стартует без ключей кошелька.

## Из брифа, дословно

> «все делай фундаменталько как сеньор»
>
> «Запуск и управление может аппку сделать типо на фастапи будет куртиться в докере»

## Разделы спецификации

Критерии §3; истории 22–24, 28, 30–31, 45–46; решения §8; `interfaces.md` целиком.

## Критерии приёмки

- [ ] `docker compose up --build` поднимает PostgreSQL и FastAPI health endpoints без wallet/RPC secrets
- [ ] миграции создают все согласованные сущности, audit/outbox и ограничения idempotency; повторный upgrade безопасен
- [ ] конфигурация строго валидируется, секретные поля маскируются, `.env.example` содержит только пустые имена переменных
- [ ] доменные money/time/public-key типы запрещают binary float и naive datetime на границах
- [ ] repository/UoW/lease contracts существуют и имеют PostgreSQL integration tests
- [ ] Python и TypeScript зависимости установлены один раз и зафиксированы lockfiles
- [ ] `docker compose --profile test run --rm check` выполняет migrations, Python lint/types/tests и executor lint/types/tests
- [ ] default mode и восстановление без сохранённого разрешения дают `SHADOW`, а тесты не способны отправить live-транзакцию

