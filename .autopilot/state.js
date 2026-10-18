window.STATE =
{
  "version": 2,
  "slug": "meteora-volume-bot",
  "dir": "2026-10-18-meteora-volume-bot--wip",
  "title": "Meteora Volume Bot",
  "mode": "semi",
  "depth": "deep",
  "tier": "T3",
  "briefFile": "2026-10-18-brief.md",
  "memoryFile": "AGENTS.md",
  "memoryOwner": "autopilot",
  "skillDir": "C:\\Users\\Maks\\.agents\\skills\\autopilot",
  "baseCommit": null,
  "startedAt": "2026-10-18T13:35:16+03:00",
  "updatedAt": "2026-10-18T13:56:52+03:00",
  "finishedAt": null,
  "stages": [
    {
      "id": "preflight",
      "status": "done",
      "startedAt": "2026-10-18T13:35:16+03:00",
      "finishedAt": "2026-10-18T13:35:47+03:00"
    },
    {
      "id": "manifest",
      "status": "done",
      "startedAt": "2026-10-18T13:35:47+03:00",
      "finishedAt": "2026-10-18T13:37:21+03:00"
    },
    {
      "id": "briefing",
      "status": "done",
      "startedAt": "2026-10-18T13:37:21+03:00",
      "finishedAt": "2026-10-18T13:50:20+03:00"
    },
    {
      "id": "spec",
      "status": "done",
      "startedAt": "2026-10-18T13:50:20+03:00",
      "finishedAt": "2026-10-18T13:54:29+03:00"
    },
    {
      "id": "plan",
      "status": "active",
      "startedAt": "2026-10-18T13:54:29+03:00"
    },
    {
      "id": "build",
      "status": "pending"
    },
    {
      "id": "review",
      "status": "pending"
    },
    {
      "id": "final",
      "status": "pending"
    }
  ],
  "requirements": {
    "total": 46,
    "done": 0,
    "inTicket": 46,
    "inSpec": 0,
    "placeholder": 0,
    "deferred": 0,
    "dropped": 0
  },
  "tickets": [
    {
      "id": "01",
      "title": "Фундамент и контракты проекта",
      "requirements": [
        "R22",
        "R23",
        "R24",
        "R28i",
        "R30i",
        "R31i",
        "G13",
        "G14"
      ],
      "blockedBy": [],
      "wave": 1,
      "zone": [
        "pyproject.toml",
        "uv.lock",
        "executor/package*.json",
        "docker-compose.yml",
        "Dockerfile*",
        "src/core/",
        "src/domain/models/",
        "src/persistence/",
        "migrations/",
        "tests/foundation/"
      ],
      "review": "да — фундамент, схема и секреты",
      "model": "сильная",
      "status": "pending",
      "retries": 0,
      "repairs": 0
    },
    {
      "id": "02",
      "title": "Universe, рыночные данные и token intelligence",
      "requirements": [
        "R15",
        "R17",
        "R18",
        "G04",
        "G05",
        "G06",
        "G07",
        "G08"
      ],
      "blockedBy": [
        "01"
      ],
      "wave": 2,
      "zone": [
        "src/providers/",
        "src/market/",
        "tests/providers/",
        "tests/market/"
      ],
      "review": "да — общие данные и внешние интеграции",
      "model": "сильная",
      "status": "pending",
      "retries": 0,
      "repairs": 0
    },
    {
      "id": "03",
      "title": "Сигналы, стратегия и pre-trade risk",
      "requirements": [
        "R02",
        "R15",
        "R17",
        "R18",
        "R19",
        "R20",
        "R22",
        "R27i",
        "R29i",
        "G05",
        "G06",
        "G07",
        "G08",
        "G09",
        "G10",
        "G11",
        "G12"
      ],
      "blockedBy": [
        "02"
      ],
      "wave": 3,
      "zone": [
        "src/domain/features/",
        "src/domain/screening/",
        "src/domain/strategy/",
        "src/domain/risk/",
        "tests/domain/"
      ],
      "review": "да — денежные алгоритмы и риск",
      "model": "сильная",
      "status": "pending",
      "retries": 0,
      "repairs": 0
    },
    {
      "id": "04",
      "title": "Shadow broker, учёт и допуск режимов",
      "requirements": [
        "R19",
        "R26i",
        "R27i",
        "R29i",
        "R30i",
        "G01",
        "G02",
        "G03",
        "G09",
        "G10",
        "G11",
        "G12"
      ],
      "blockedBy": [
        "03"
      ],
      "wave": 4,
      "zone": [
        "src/accounting/",
        "src/portfolio/",
        "src/modes/",
        "tests/accounting/",
        "tests/modes/"
      ],
      "review": "да — P&L, режимы и аварийные лимиты",
      "model": "сильная",
      "status": "pending",
      "retries": 0,
      "repairs": 0
    },
    {
      "id": "05",
      "title": "Official SDK execution gateway и ручное подтверждение",
      "requirements": [
        "R16",
        "R18",
        "R26i",
        "R27i",
        "R28i",
        "R29i",
        "R30i",
        "G02",
        "G03"
      ],
      "blockedBy": [
        "01"
      ],
      "wave": 2,
      "zone": [
        "executor/",
        "src/execution/",
        "tests/execution/",
        "contracts/execution/"
      ],
      "review": "да — реальные деньги, секреты и on-chain интеграция",
      "model": "сильная",
      "status": "pending",
      "retries": 0,
      "repairs": 0
    },
    {
      "id": "06",
      "title": "FastAPI operator web-приложение",
      "requirements": [
        "R15",
        "R18",
        "R23",
        "R29i",
        "R32i",
        "G01",
        "G02",
        "G03",
        "G04",
        "G13"
      ],
      "blockedBy": [
        "02",
        "03",
        "04",
        "05"
      ],
      "wave": 5,
      "zone": [
        "src/api/",
        "src/web/",
        "templates/",
        "static/",
        "tests/api/",
        "tests/web/"
      ],
      "review": "да — доступ, approvals и переключение live",
      "model": "сильная",
      "status": "pending",
      "retries": 0,
      "repairs": 0
    },
    {
      "id": "07",
      "title": "Worker orchestration, recovery и наблюдаемость",
      "requirements": [
        "R15",
        "R17",
        "R18",
        "R22",
        "R23",
        "R27i",
        "R29i",
        "R30i",
        "G04",
        "G12",
        "G14"
      ],
      "blockedBy": [
        "02",
        "03",
        "04",
        "05"
      ],
      "wave": 5,
      "zone": [
        "src/worker/",
        "src/recovery/",
        "src/observability/",
        "tests/worker/",
        "tests/recovery/",
        "tests/observability/"
      ],
      "review": "да — recovery, daily stop и денежный конвейер",
      "model": "сильная",
      "status": "pending",
      "retries": 0,
      "repairs": 0
    },
    {
      "id": "08",
      "title": "Документация, приёмка и поставка",
      "requirements": [
        "R01",
        "R02",
        "R03",
        "R04",
        "R05",
        "R06",
        "R07",
        "R08",
        "R09",
        "R10",
        "R11",
        "R12",
        "R13",
        "R14",
        "R21",
        "R22",
        "R23",
        "R24",
        "R25",
        "R31i",
        "R32i",
        "G14"
      ],
      "blockedBy": [
        "06",
        "07"
      ],
      "wave": 6,
      "zone": [
        "README.md",
        "docs/",
        "scripts/",
        "tests/acceptance/",
        "research/",
        "LICENSE*"
      ],
      "review": "нет",
      "model": "обычная",
      "status": "pending",
      "retries": 0,
      "repairs": 0
    }
  ],
  "tests": null,
  "debt": {
    "placeholders": [],
    "assumptions": [],
    "emptyEnv": []
  },
  "additions": [],
  "coverage": {
    "found": 10,
    "fixed": 10,
    "deferred": 0,
    "items": []
  },
  "concerns": [],
  "blind": null,
  "beats": [
    "2026-10-18T13:35:16+03:00",
    "2026-10-18T13:35:47+03:00",
    "2026-10-18T13:37:09+03:00",
    "2026-10-18T13:37:21+03:00",
    "2026-10-18T13:37:41+03:00",
    "2026-10-18T13:50:14+03:00",
    "2026-10-18T13:50:20+03:00",
    "2026-10-18T13:54:21+03:00",
    "2026-10-18T13:54:29+03:00",
    "2026-10-18T13:56:43+03:00",
    "2026-10-18T13:56:48+03:00",
    "2026-10-18T13:56:52+03:00"
  ],
  "rev": 13,
  "waiting": null,
  "waits": [
    {
      "kind": "briefing",
      "from": "2026-10-18T13:37:41+03:00",
      "to": "2026-10-18T13:50:14+03:00"
    }
  ],
  "log": [
    {
      "t": "2026-10-18T13:35:16+03:00",
      "k": "init"
    },
    {
      "t": "2026-10-18T13:35:47+03:00",
      "k": "stage",
      "id": "manifest",
      "act": "start"
    },
    {
      "t": "2026-10-18T13:37:21+03:00",
      "k": "stage",
      "id": "briefing",
      "act": "start"
    },
    {
      "t": "2026-10-18T13:37:41+03:00",
      "k": "ask",
      "kind": "briefing",
      "note": "четыре решения — режим реальных сделок, охват пулов, риск-лимиты, формат запуска и управления"
    },
    {
      "t": "2026-10-18T13:50:14+03:00",
      "k": "answered",
      "kind": "briefing"
    },
    {
      "t": "2026-10-18T13:50:20+03:00",
      "k": "gate",
      "g": "G1",
      "ok": true,
      "open": 0,
      "total": 46
    },
    {
      "t": "2026-10-18T13:50:20+03:00",
      "k": "stage",
      "id": "spec",
      "act": "start"
    },
    {
      "t": "2026-10-18T13:54:21+03:00",
      "k": "gate",
      "g": "G2",
      "ok": true,
      "found": 10,
      "fixed": 10,
      "deferred": 0
    },
    {
      "t": "2026-10-18T13:54:29+03:00",
      "k": "stage",
      "id": "plan",
      "act": "start"
    },
    {
      "t": "2026-10-18T13:56:43+03:00",
      "k": "tier",
      "v": "T3"
    },
    {
      "t": "2026-10-18T13:56:48+03:00",
      "k": "tickets",
      "n": 8
    },
    {
      "t": "2026-10-18T13:56:52+03:00",
      "k": "gate",
      "g": "G3",
      "ok": true,
      "problems": 0
    }
  ],
  "reqList": [
    {
      "id": "R01",
      "text": "«https://teletype.in/@lexdollar/meteora3nax можешь прочитать»",
      "status": "in-ticket"
    },
    {
      "id": "R02",
      "text": "«можешь подробно очень изучить гайд»",
      "status": "in-ticket"
    },
    {
      "id": "R03",
      "text": "«https://www.youtube.com/watch?v=Pk0VGbWakr8»",
      "status": "in-ticket"
    },
    {
      "id": "R04",
      "text": "«https://www.youtube.com/watch?v=cNYYFKHzBqk»",
      "status": "in-ticket"
    },
    {
      "id": "R05",
      "text": "«и эти 2 видео тоже подробно»",
      "status": "in-ticket"
    },
    {
      "id": "R06",
      "text": "«в результате глубочайшего исследование»",
      "status": "in-ticket"
    },
    {
      "id": "R07",
      "text": "«напиши все макрдовн файл»",
      "status": "in-ticket"
    },
    {
      "id": "R08",
      "text": "«сделай папку на рабочем сстоле»",
      "status": "in-ticket"
    },
    {
      "id": "R09",
      "text": "«прод навзание метора»",
      "status": "in-ticket"
    },
    {
      "id": "R10",
      "text": "«сохрани туда файл»",
      "status": "in-ticket"
    },
    {
      "id": "R11",
      "text": "«ты создал папку метора»",
      "status": "in-ticket"
    },
    {
      "id": "R12",
      "text": "«закинул туда маркдовн»",
      "status": "in-ticket"
    },
    {
      "id": "R13",
      "text": "«только на ангилсиком языке»",
      "status": "in-ticket"
    },
    {
      "id": "R14",
      "text": "«папака на англиском навзание метора всм»",
      "status": "in-ticket"
    },
    {
      "id": "R15",
      "text": "«можем ли мы вот этого сделать автоматизированно?»",
      "status": "in-ticket"
    },
    {
      "id": "R16",
      "text": "«условно торговлю»",
      "status": "in-ticket"
    },
    {
      "id": "R17",
      "text": "«надо отслеживать там всякие объёмы и так далее»",
      "status": "in-ticket"
    },
    {
      "id": "R18",
      "text": "«всё автоматизировать»",
      "status": "in-ticket"
    },
    {
      "id": "R19",
      "text": "«чтобы зарабатывать на всяких всплесках объёмов»",
      "status": "in-ticket"
    },
    {
      "id": "R20",
      "text": "«по гайду же будем делать ?»",
      "status": "in-ticket"
    },
    {
      "id": "R21",
      "text": "«погнали»",
      "status": "in-ticket"
    },
    {
      "id": "R22",
      "text": "«все делай фундаменталько»",
      "status": "in-ticket"
    },
    {
      "id": "R23",
      "text": "«как сеньор»",
      "status": "in-ticket"
    },
    {
      "id": "R24",
      "text": "«весь коды»",
      "status": "in-ticket"
    },
    {
      "id": "R25",
      "text": "«в этой папке C:\\Users\\Maks\\Desktop\\Meteora должны быть»",
      "status": "in-ticket"
    },
    {
      "id": "R26i",
      "text": "(подразумевается) автоматическая торговля не должна начинаться реальными деньгами без безопасного режима проверки",
      "status": "in-ticket"
    },
    {
      "id": "R27i",
      "text": "(подразумевается) система должна ограничивать убыток и уметь аварийно остановиться",
      "status": "in-ticket"
    },
    {
      "id": "R28i",
      "text": "(подразумевается) приватный ключ кошелька и API-ключи не должны попадать в код или историю Git",
      "status": "in-ticket"
    },
    {
      "id": "R29i",
      "text": "(подразумевается) решения об открытии и закрытии позиции должны быть объяснимы и записаны",
      "status": "in-ticket"
    },
    {
      "id": "R30i",
      "text": "(подразумевается) после перезапуска бот не должен терять позиции и повторять опасные действия",
      "status": "in-ticket"
    },
    {
      "id": "R31i",
      "text": "(подразумевается) реализация должна иметь автоматические проверки ключевой логики",
      "status": "in-ticket"
    },
    {
      "id": "R32i",
      "text": "(подразумевается) проект должен содержать понятную инструкцию запуска и настройки",
      "status": "in-ticket"
    },
    {
      "id": "G01",
      "text": "«сначала 7–14 дней shadow/paper без денег»",
      "status": "in-ticket"
    },
    {
      "id": "G02",
      "text": "«затем сделки с ручным подтверждением»",
      "status": "in-ticket"
    },
    {
      "id": "G03",
      "text": "«после успешной проверки — автоматический live-режим»",
      "status": "in-ticket"
    },
    {
      "id": "G04",
      "text": "«Все DLMM-пулы с жёсткими фильтрами»",
      "status": "in-ticket"
    },
    {
      "id": "G05",
      "text": "«исключает низкую ликвидность»",
      "status": "in-ticket"
    },
    {
      "id": "G06",
      "text": "«исключает подозрительные токены»",
      "status": "in-ticket"
    },
    {
      "id": "G07",
      "text": "«исключает […] искусственный объём»",
      "status": "in-ticket"
    },
    {
      "id": "G08",
      "text": "«исключает […] слишком молодые и опасные пулы»",
      "status": "in-ticket"
    },
    {
      "id": "G09",
      "text": "«до 10% капитала в позиции»",
      "status": "in-ticket"
    },
    {
      "id": "G10",
      "text": "«риск до 1%»",
      "status": "in-ticket"
    },
    {
      "id": "G11",
      "text": "«максимум три позиции»",
      "status": "in-ticket"
    },
    {
      "id": "G12",
      "text": "«дневной стоп — 4%»",
      "status": "in-ticket"
    },
    {
      "id": "G13",
      "text": "«аппку сделать типо на фастапи»",
      "status": "in-ticket"
    },
    {
      "id": "G14",
      "text": "«будет куртиться в докере»",
      "status": "in-ticket"
    }
  ],
  "gates": {
    "G1": {
      "ok": true,
      "at": "2026-10-18T13:50:20+03:00",
      "open": 0,
      "total": 46
    },
    "G2": {
      "ok": true,
      "at": "2026-10-18T13:54:21+03:00",
      "found": 10,
      "fixed": 10,
      "deferred": 0
    },
    "G3": {
      "ok": true,
      "at": "2026-10-18T13:56:52+03:00",
      "problems": 0
    }
  },
  "result": null
}
