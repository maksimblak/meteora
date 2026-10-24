# 08 — Документация, приёмка и поставка

**Требования:** R01, R02, R03, R04, R05, R06, R07, R08, R09, R10, R11, R12, R13, R14, R21, R22, R23, R24, R25, R31i, R32i, G14
**Зависит от:** 06, 07, 09, 10, 11, 12, 13, 14, 15
**Зона:** `README.md` · `docs/` · `scripts/` · `tests/acceptance/` · `research/` · `LICENSE*` · root packaging files
**Волна:** 10
**Ревью:** нет
**Модель:** обычная

## Что должно заработать

Проект получает русскоязычный README/runbook, архитектурные решения, threat/risk model, конфигурационный справочник, операции backup/recovery/emergency stop и acceptance smoke. Глубокий исследовательский Markdown сохраняется в поставке. После оркестраторской проверки весь проект безопасно копируется в английскую папку на рабочем столе, не удаляя существующие файлы.

## Из брифа, дословно

> «в результате глубочайшего исследование напиши все макрдовн файл»
>
> «папака на англиском навзание метора всм»
>
> «весь коды в этой папке C:\Users\Maks\Desktop\Meteora должны быть»

## Разделы спецификации

Истории 1–14, 21–25, 31–32, 46; критерии §3; §8–11; исходный отчёт и все project interfaces.

## Критерии приёмки

- [ ] README проводит Windows-пользователя от Docker prerequisites и `.env` до shadow dashboard одной воспроизводимой последовательностью
- [ ] runbook покрывает modes/promotion, provider outage, stale data, restart recovery, backup/restore, daily stop и emergency kill switch
- [ ] ADR описывают hybrid FastAPI/TypeScript boundary, fail-closed risk data, PostgreSQL coordination и no-live-by-default
- [ ] security/threat document перечисляет wallet, operator token, CSRF, log redaction, Docker network и dependency risks
- [ ] исследовательский Markdown включён целиком, содержит три исходных материала и отделяет тезисы источников от проектных решений
- [ ] acceptance smoke поднимает Compose, проверяет migrations/health, ingest fixture, candidate rejection, shadow lifecycle, UI/API и restart recovery
- [ ] инструкция явно сообщает, что доходность не гарантируется и live включается только после gates/checklist
- [ ] packaging verification доказывает наличие всего source/tests/config/docs и отсутствие `.env`, keys, caches, build artifacts и Git internals
- [ ] оркестраторская финальная поставка нацелена на `C:\Users\Maks\Desktop\Meteora\MeteoraVolumeBot` и сохраняет соседний Markdown/пользовательские файлы

