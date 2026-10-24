window.STATE =
{
  "version": 2,
  "slug": "meteora-volume-bot",
  "dir": "2026-10-18-meteora-volume-bot",
  "title": "Meteora Volume Bot",
  "mode": "semi",
  "depth": "deep",
  "tier": "T3",
  "briefFile": "2026-10-18-brief.md",
  "memoryFile": "AGENTS.md",
  "memoryOwner": "autopilot",
  "skillDir": "C:\\Users\\Maks\\.agents\\skills\\autopilot",
  "baseCommit": "8059648",
  "startedAt": "2026-10-18T13:35:16+03:00",
  "updatedAt": "2026-10-24T20:50:34+02:00",
  "finishedAt": "2026-10-24T20:50:34+02:00",
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
      "status": "done",
      "startedAt": "2026-10-18T13:54:29+03:00",
      "finishedAt": "2026-10-18T13:57:53+03:00"
    },
    {
      "id": "build",
      "status": "done",
      "startedAt": "2026-10-18T13:57:53+03:00",
      "finishedAt": "2026-10-24T11:04:44+02:00"
    },
    {
      "id": "review",
      "status": "done",
      "startedAt": "2026-10-24T11:04:44+02:00",
      "finishedAt": "2026-10-24T18:19:08+02:00"
    },
    {
      "id": "final",
      "status": "done",
      "startedAt": "2026-10-24T18:19:08+02:00",
      "finishedAt": "2026-10-24T20:50:34+02:00"
    }
  ],
  "requirements": {
    "total": 57,
    "done": 55,
    "inTicket": 0,
    "inSpec": 1,
    "placeholder": 0,
    "deferred": 1,
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
      "status": "done",
      "startedAt": "2026-10-18T13:58:05+03:00",
      "finishedAt": "2026-10-18T14:55:00+03:00",
      "retries": 1,
      "repairs": 1,
      "repairFindings": [
        "миграции принимают DATABASE_URL_FILE; executor только внутри Compose; API по умолчанию loopback; JSON-логи без секретов; executor check проверяет реальные исходники; PostgreSQL integration покрывает repository и outbox"
      ],
      "tests": {
        "passed": 10,
        "failed": 0
      },
      "commit": "f6da0aa"
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
      "status": "done",
      "startedAt": "2026-10-18T14:55:11+03:00",
      "finishedAt": "2026-10-19T10:23:38+03:00",
      "retries": 2,
      "repairs": 2,
      "repairFindings": [
        "hard filters reject/UNKNOWN для token/artificial volume; глубокая immutable snapshot; stale enhanced risk не FRESH; universe batch и признаки incremental; normalized provider health наружу",
        "cursor batching всё ещё собирает полный universe в памяти; нужен cursor-driven staging begin/stage/commit без O(n²), плюс при возможности закрыть deterministic breaker/rate-limit tests и fixture malformed parity"
      ],
      "tests": {
        "passed": 20,
        "failed": 0
      },
      "commit": "530e526"
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
      "status": "done",
      "startedAt": "2026-10-19T10:24:32+03:00",
      "finishedAt": "2026-10-21T10:29:35+03:00",
      "retries": 2,
      "repairs": 2,
      "repairFindings": [
        "identity/time binding; WARMING_UP precedence; volume/count consistency; SizedPlan/pre-trade caps; loss ROUND_CEILING; horizon/exit-aware bins; complete position monitor",
        "exact provenance equality и full PoolRef history; independent volume/tx/participants contradictions; PositionMonitor max-severity чтобы EXIT не понижался missing fee"
      ],
      "tests": {
        "passed": 48,
        "failed": 0
      },
      "commit": "0ee2f01"
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
      "status": "done",
      "startedAt": "2026-10-23T13:42:14+03:00",
      "finishedAt": "2026-10-23T14:23:40+03:00",
      "retries": 0,
      "repairs": 3,
      "repairFindings": [
        "partial REMOVE realized P&L; atomic CAS lifecycle; persisted 7 full UTC-day evidence; block promotion on safety flags/versioned one-shot evidence; sticky atomic daily stop with persisted UTC equity; transactional kill switch/outbox/no new pending",
        "next UTC-day clears daily stop; atomic risk-day+mode+critical alert+outbox; SQLite admission atomic against kill across connections; kill outbox uses transaction-derived counts",
        "PENDING-план, допущенный до daily stop или понижения режима, не переходит в EXECUTING"
      ],
      "tests": {
        "passed": 153,
        "failed": 0
      },
      "commit": "dec291c"
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
      "status": "done",
      "startedAt": "2026-10-18T14:55:11+03:00",
      "finishedAt": "2026-10-20T12:36:48+03:00",
      "retries": 2,
      "repairs": 2,
      "repairFindings": [
        "approval и simulation не связаны с exact command; invalid/one-shot TTL approval; реальный price impact fail-closed; partial multi-tx signatures; durable recovery; LIVE promotion gate; Python/TypeScript contract parity",
        "до RPC durable сохранить derived signature и reconcile timeout без rebroadcast; operator_confirmation_id trim/nonblank на model и service gate; readiness degraded пока price impact недоступен"
      ],
      "tests": {
        "passed": 46,
        "failed": 0
      },
      "commit": "2e615bc"
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
        "G13",
        "G17",
        "G18"
      ],
      "blockedBy": [
        "02",
        "03",
        "04",
        "05",
        "07",
        "09",
        "10"
      ],
      "wave": 7,
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
      "status": "done",
      "startedAt": "2026-10-23T17:05:16+03:00",
      "finishedAt": "2026-10-23T18:12:51+03:00",
      "retries": 0,
      "repairs": 2,
      "repairFindings": [
        "BLOCKED вне зоны: точка входа src.api.main:app в Dockerfile и compose, переменные api и worker",
        "APP_OPERATOR_TOKEN(_FILE) и APP_SESSION_SECRET(_FILE) доходят до api в штатном compose; вход, kill switch и понижение работают без override"
      ],
      "tests": {
        "passed": 454,
        "failed": 0
      },
      "commit": "c128f10"
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
        "G14",
        "D01"
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
        "src/persistence/",
        "migrations/",
        "tests/worker/",
        "tests/recovery/",
        "tests/observability/",
        "tests/persistence/"
      ],
      "review": "да — recovery, daily stop и денежный конвейер",
      "model": "сильная",
      "status": "done",
      "startedAt": "2026-10-23T14:23:41+03:00",
      "finishedAt": "2026-10-23T15:55:27+03:00",
      "retries": 0,
      "repairs": 2,
      "repairFindings": [
        "канонический check зелёный: worker в compose запускает python -m src.worker, образ содержит contracts/",
        "recovery не переводит в FAILED исполняющийся EXECUTING-план; fill не фиксируется для плана вне EXECUTING"
      ],
      "tests": {
        "passed": 250,
        "failed": 0
      },
      "commit": "c2bd208"
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
        "07",
        "09",
        "10",
        "11",
        "12",
        "13",
        "14",
        "15"
      ],
      "wave": 10,
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
      "status": "done",
      "startedAt": "2026-10-24T10:00:46+02:00",
      "finishedAt": "2026-10-24T11:04:43+02:00",
      "retries": 0,
      "repairs": 0,
      "tests": {
        "passed": 670,
        "failed": 0
      },
      "commit": "080c240"
    },
    {
      "id": "09",
      "title": "Шлюз исполнения: Python-клиент executor и исполнение после approval",
      "requirements": [
        "R16",
        "R26i",
        "R27i",
        "R29i",
        "R30i",
        "G02",
        "G03",
        "D02"
      ],
      "blockedBy": [
        "05",
        "07"
      ],
      "wave": 6,
      "zone": [
        "src/execution/",
        "src/worker/execution.py",
        "src/worker/runtime.py",
        "tests/execution/",
        "tests/worker/"
      ],
      "review": "да — реальные деньги, approvals и on-chain путь",
      "model": "сильная",
      "status": "done",
      "startedAt": "2026-10-23T15:55:28+03:00",
      "finishedAt": "2026-10-23T17:05:15+03:00",
      "retries": 0,
      "repairs": 1,
      "repairFindings": [
        "частично прошедший REMOVE (FAILED, несколько транзакций, есть подпись) останавливает автоповторы по позиции и поднимает critical alert"
      ],
      "tests": {
        "passed": 361,
        "failed": 0
      },
      "commit": "3b29208"
    },
    {
      "id": "10",
      "title": "Стратегия и пороги по гайду LexDollar",
      "requirements": [
        "R02",
        "R17",
        "R20",
        "G07",
        "G15",
        "G16",
        "D03",
        "D05"
      ],
      "blockedBy": [
        "03",
        "07"
      ],
      "wave": 6,
      "zone": [
        "src/domain/",
        "src/providers/",
        "src/market/",
        "src/worker/policy.py",
        "tests/domain/",
        "tests/providers/",
        "tests/market/"
      ],
      "review": "да — денежные правила входа и выбор стратегии",
      "model": "сильная",
      "status": "done",
      "startedAt": "2026-10-23T15:55:28+03:00",
      "finishedAt": "2026-10-23T16:46:29+03:00",
      "retries": 0,
      "repairs": 2,
      "repairFindings": [
        "total fees условный порог (D03): нет значения — флаг TOTAL_FEES_UNAVAILABLE, не UNKNOWN",
        "SPOT_ATH_BREAKOUT только при доказанном ATH (непрерывная история); volume_to_liquidity нормирован на 5 минут"
      ],
      "tests": {
        "passed": 325,
        "failed": 0
      },
      "commit": "bf65add"
    },
    {
      "id": "11",
      "title": "Адаптер Meteora Data API под реальный формат",
      "requirements": [
        "R17",
        "G04",
        "D04",
        "D06"
      ],
      "blockedBy": [
        "02",
        "10"
      ],
      "wave": 7,
      "zone": [
        "src/providers/",
        "src/market/",
        "src/domain/features/",
        "tests/providers/",
        "tests/market/",
        "tests/domain/"
      ],
      "review": "да — общие рыночные данные, на них стоит каждое решение",
      "model": "сильная",
      "status": "done",
      "startedAt": "2026-10-23T16:46:31+03:00",
      "finishedAt": "2026-10-23T17:36:47+03:00",
      "retries": 0,
      "repairs": 1,
      "repairFindings": [
        "обход с повтором пула на двух страницах или с пулами меньше total не публикуется как полный"
      ],
      "tests": {
        "passed": 136,
        "failed": 0
      },
      "commit": "697aee2"
    },
    {
      "id": "12",
      "title": "Метрики GMGN через браузер",
      "requirements": [
        "G01",
        "G06",
        "G07",
        "G16",
        "G18",
        "D09"
      ],
      "blockedBy": [
        "09",
        "10",
        "11",
        "14"
      ],
      "wave": 9,
      "zone": [
        "services/gmgn/",
        "Dockerfile.gmgn",
        "docker-compose.yml",
        "src/gmgn/",
        "src/market/models.py",
        "src/domain/screening/",
        "src/worker/policy.py",
        "src/worker/runtime.py",
        "src/providers/solana.py",
        "tests/providers/",
        "tests/gmgn/",
        ".env.example"
      ],
      "review": "да — внешний источник риска и fail-closed для live",
      "model": "сильная",
      "status": "done",
      "startedAt": "2026-10-23T20:09:32+03:00",
      "finishedAt": "2026-10-23T21:24:23+03:00",
      "retries": 0,
      "repairs": 2,
      "repairFindings": [
        "GMGN_UNAVAILABLE и без подключённого сервиса (F1); D09: без enhanced и без RPC кандидаты в SHADOW не отклоняются",
        "решение с TOKEN_RISK_UNAVAILABLE не считается покрытым данными для §7.1 (pipeline._covered)"
      ],
      "tests": {
        "passed": 627,
        "failed": 0
      },
      "commit": "d7d3da5"
    },
    {
      "id": "13",
      "title": "Live-путь в executor: ключ позиции и price impact",
      "requirements": [
        "R16",
        "R27i",
        "R28i",
        "G02",
        "G03",
        "D06",
        "D07",
        "D08"
      ],
      "blockedBy": [
        "05",
        "09"
      ],
      "wave": 7,
      "zone": [
        "executor/",
        "contracts/execution/",
        "src/execution/",
        "src/worker/execution.py",
        "tests/execution/",
        "tests/worker/test_gateway_execution.py"
      ],
      "review": "да — реальные деньги, секреты и on-chain путь",
      "model": "сильная",
      "status": "done",
      "startedAt": "2026-10-23T17:05:16+03:00",
      "finishedAt": "2026-10-23T18:25:57+03:00",
      "retries": 0,
      "repairs": 2,
      "repairFindings": [
        "BLOCKED вне зоны: execution_store читает V2, recovery подтверждает по record.position_address, test_pipeline на V2",
        "settler берёт адрес позиции из receipt (record.position_address); код отказа executor до оператора; выход не блокируется impact несостоявшегося обмена (D08)"
      ],
      "tests": {
        "passed": 460,
        "failed": 0
      },
      "commit": "472e94d"
    },
    {
      "id": "14",
      "title": "Конвейер на живых данных",
      "requirements": [
        "R15",
        "R17",
        "G01",
        "G04",
        "G05",
        "G06",
        "G07",
        "G08",
        "G16",
        "D06",
        "D07",
        "D08"
      ],
      "blockedBy": [
        "11",
        "13"
      ],
      "wave": 8,
      "zone": [
        "src/domain/",
        "src/market/",
        "src/providers/",
        "src/worker/",
        "docker-compose.yml",
        "tests/domain/",
        "tests/market/",
        "tests/providers/",
        "tests/worker/"
      ],
      "review": "да — каждое решение о входе стоит на этих данных",
      "model": "сильная",
      "status": "done",
      "startedAt": "2026-10-23T18:25:59+03:00",
      "finishedAt": "2026-10-23T20:09:31+03:00",
      "retries": 0,
      "repairs": 2,
      "repairFindings": [
        "дефолтный Bid-Ask без бинов выше active (вход только в quote-токене); после NEGATIVE_DELTA прирост не засчитывается как объём",
        "BID_ONLY: цена выше верха не мгновенный RANGE_BREACH; интервал прироста от метки; выход из вечного UNKNOWN после сброса счётчика"
      ],
      "tests": {
        "passed": 569,
        "failed": 0
      },
      "commit": "76fd011"
    },
    {
      "id": "15",
      "title": "Панель подтверждения: выход не блокируется price impact",
      "requirements": [
        "R27i",
        "G02",
        "D08"
      ],
      "blockedBy": [
        "06",
        "13"
      ],
      "wave": 8,
      "zone": [
        "src/api/approvals.py",
        "tests/api/"
      ],
      "review": "да — approvals и выход из позиции",
      "model": "сильная",
      "status": "done",
      "startedAt": "2026-10-23T18:25:59+03:00",
      "finishedAt": "2026-10-23T18:49:15+03:00",
      "retries": 0,
      "repairs": 1,
      "repairFindings": [
        "выход APPROVAL-позиции одобряем при kill switch ORDERLY_EXIT и после дневного стопа; вход блокируется как раньше"
      ],
      "tests": {
        "passed": 52,
        "failed": 0
      },
      "commit": "96b7c33"
    },
    {
      "id": "16",
      "title": "Метрики GMGN через официальное OpenAPI",
      "requirements": [
        "G18",
        "G20"
      ],
      "blockedBy": [
        "12"
      ],
      "wave": 10,
      "zone": [
        "src/gmgn/",
        "src/worker/runtime.py",
        "tests/gmgn/",
        "docker-compose.yml",
        ".env.example"
      ],
      "review": "да — внешний источник риска, секретный ключ и fail-closed для live",
      "model": "сильная",
      "status": "done",
      "startedAt": "2026-10-24T10:09:14+02:00",
      "finishedAt": "2026-10-24T10:43:57+02:00",
      "retries": 0,
      "repairs": 0,
      "tests": {
        "passed": 670,
        "failed": 0
      },
      "commit": "31f2839"
    },
    {
      "id": "17",
      "title": "Продажа монеты после выхода и priority fee",
      "requirements": [
        "G22",
        "G23",
        "R16",
        "R27i",
        "D08"
      ],
      "blockedBy": [
        "03"
      ],
      "wave": 13,
      "zone": [
        "executor/",
        "contracts/",
        "src/execution/",
        "src/worker/execution.py",
        "src/accounting/",
        "src/api/approvals.py",
        "src/api/schemas.py",
        "src/web/templates/approval.html",
        ".env.example",
        "executor/tests/",
        "tests/execution/",
        "tests/accounting/",
        "tests/api/test_api_approvals.py",
        "tests/api/test_t17_*.py",
        "tests/worker/test_gateway_execution.py",
        "tests/worker/test_t17_*.py"
      ],
      "review": "да — реальные деньги, on-chain обмен",
      "model": "сильная",
      "status": "done",
      "startedAt": "2026-10-24T14:00:27+02:00",
      "finishedAt": "2026-10-24T15:24:27+02:00",
      "retries": 0,
      "repairs": 1,
      "repairFindings": [
        "подтверждённый вход берёт network/priority fee из cost model, а не из settlement receipt"
      ],
      "tests": {
        "passed": 1062,
        "failed": 0
      },
      "commit": "5baa2f5"
    },
    {
      "id": "18",
      "title": "Отбор как у автора: устойчивый объём, метрики по токену, основной пул, накрутка, тема",
      "requirements": [
        "G23",
        "G24",
        "G04",
        "G06",
        "G07",
        "G08",
        "G16"
      ],
      "blockedBy": [
        "03"
      ],
      "wave": 13,
      "zone": [
        "src/domain/screening/",
        "src/domain/features/",
        "src/market/",
        "src/providers/",
        "src/gmgn/",
        "src/worker/pipeline.py",
        "src/worker/policy.py",
        "src/worker/runtime.py",
        "src/worker/evidence.py",
        "tests/domain/",
        "tests/market/",
        "tests/providers/",
        "tests/gmgn/",
        "tests/worker/test_pipeline.py",
        "tests/worker/test_live_pipeline.py",
        "tests/worker/test_live_policy.py",
        "tests/worker/test_runtime_policy.py",
        "tests/worker/test_live_series.py",
        "tests/worker/test_live_runtime.py",
        "tests/worker/test_f1_*.py",
        "tests/worker/test_t18_*.py",
        "src/api/labels.py",
        "src/api/presenters.py"
      ],
      "review": "да — каждое решение о входе стоит на этом отборе",
      "model": "сильная",
      "status": "done",
      "startedAt": "2026-10-24T14:00:27+02:00",
      "finishedAt": "2026-10-24T15:11:24+02:00",
      "retries": 0,
      "repairs": 2,
      "repairFindings": [
        "test_f3_labels: 10 новых кодов T18 без русских подписей; устаревшие _TRIGGERS/_SPIKE_CODE в presenters",
        "LIVE: неизвестная доля девелопера и тема UNKNOWN не закрывают вход"
      ],
      "tests": {
        "passed": 1061,
        "failed": 0
      },
      "commit": "9ca88ff"
    },
    {
      "id": "19",
      "title": "Стратегия и выходы как у автора: односторонний Spot, глубина и вход Bid-Ask, ATH, тейк-профит, частые снимки",
      "requirements": [
        "G23",
        "G15",
        "R20",
        "R27i"
      ],
      "blockedBy": [
        "17",
        "18"
      ],
      "wave": 14,
      "zone": [
        "src/domain/strategy/",
        "src/domain/risk/",
        "src/worker/positions.py",
        "src/worker/runtime.py",
        "src/worker/policy.py",
        "src/worker/execution.py",
        "src/accounting/",
        "src/providers/meteora.py",
        "executor/src/",
        "docker-compose.yml",
        "SWAP_*",
        "PRIORITY_FEE_*",
        "src/execution/settings.py",
        "SwapSettings",
        "tests/domain/",
        "tests/worker/",
        "tests/accounting/",
        "tests/providers/test_meteora.py",
        "tests/foundation/test_f3_compose.py",
        "executor/tests/"
      ],
      "review": "да — денежные правила входа и выхода",
      "model": "сильная",
      "status": "done",
      "startedAt": "2026-10-24T15:24:29+02:00",
      "finishedAt": "2026-10-24T16:36:44+02:00",
      "retries": 0,
      "repairs": 1,
      "repairFindings": [
        "контекст не-SHADOW позиции от цены снимка, а не от активного бина команды; окно 5m перечитываний обнуляется после свипа"
      ],
      "tests": {
        "passed": 1123,
        "failed": 0
      },
      "commit": "edea32a"
    },
    {
      "id": "20",
      "title": "",
      "requirements": [
        "G23",
        "G22",
        "G17",
        "R32i",
        "R23"
      ],
      "blockedBy": [
        "17",
        "18",
        "19",
        "04",
        "05"
      ],
      "wave": 16,
      "zone": [
        "src/api/labels.py",
        "src/api/approvals.py",
        "src/api/presenters.py",
        "src/web/templates/",
        "README.md",
        "docs/",
        "docs/architecture.md",
        "tests/api/",
        "tests/web/",
        "tests/acceptance/",
        "scripts/acceptance-smoke.ps1"
      ],
      "review": "нет",
      "model": "сильная",
      "status": "done",
      "startedAt": "2026-10-24T17:46:23+02:00",
      "finishedAt": "2026-10-24T18:19:01+02:00",
      "retries": 0,
      "repairs": 0,
      "tests": {
        "passed": 1284,
        "failed": 0
      },
      "commit": "dd79331"
    },
    {
      "id": "21",
      "title": "GMGN через браузер-посредник",
      "requirements": [
        "G25",
        "G18",
        "G20",
        "G23"
      ],
      "blockedBy": [
        "06"
      ],
      "wave": 18,
      "zone": [
        "services/gmgn/**",
        "Dockerfile.gmgn",
        "src/gmgn/client.py",
        "src/gmgn/__init__.py",
        "src/gmgn/enrichment.py",
        "src/worker/runtime.py",
        "src/api/labels.py",
        "src/core/secrets.py",
        "tests/gmgn/",
        "tests/worker/",
        "tests/api/test_f3_labels.py",
        "docker-compose.yml",
        ".env.example",
        "scripts/gmgn-bridge.ps1",
        "scripts/gmgn-bridge.sh",
        "README.md",
        "docs/runbook.md",
        "docs/configuration.md",
        "docs/security.md"
      ],
      "review": "да — внешняя интеграция и граница безопасности",
      "model": "сильная",
      "status": "done",
      "retries": 0,
      "repairs": 1,
      "startedAt": "2026-10-24T19:42:46+02:00",
      "repairFindings": [
        "пауза после 429/403/challenge соблюдается и для загрузок из очереди; вход в документации — по желанию пользователя с предупреждением о риске"
      ],
      "finishedAt": "2026-10-24T20:49:00+02:00",
      "tests": {
        "passed": 1335,
        "failed": 0
      },
      "commit": "b64fe1a"
    },
    {
      "id": "F1",
      "title": "Ремонт ветки: данные о входе, единицы и доход SHADOW",
      "requirements": [
        "R17",
        "R19",
        "G05",
        "G07",
        "G16",
        "D07"
      ],
      "blockedBy": [],
      "wave": 11,
      "zone": [
        "src/market/",
        "src/domain/screening/",
        "src/providers/meteora.py",
        "tests/providers/test_meteora.py",
        "src/worker/pipeline.py",
        "src/worker/policy.py",
        "src/accounting/",
        "tests/market/",
        "tests/domain/",
        "tests/accounting/",
        "tests/worker/test_pipeline.py",
        "tests/worker/test_live_pipeline.py",
        "tests/worker/test_live_policy.py",
        "tests/worker/test_runtime_policy.py",
        "tests/worker/test_live_series.py",
        "tests/worker/test_f1_*.py"
      ],
      "review": "да — ремонт ветки",
      "model": "сильная",
      "status": "done",
      "startedAt": "2026-10-24T11:24:03+02:00",
      "finishedAt": "2026-10-24T12:10:19+02:00",
      "retries": 0,
      "repairs": 1,
      "repairFindings": [
        "зона расширена: src/providers/meteora.py читает token_y.price → PoolSnapshot.quote_price_usd (A3)"
      ],
      "tests": {
        "passed": 730,
        "failed": 0
      },
      "commit": "76b1b41"
    },
    {
      "id": "F2",
      "title": "Ремонт ветки: исполнение, алерты и kill switch",
      "requirements": [
        "R16",
        "R27i",
        "R30i",
        "G02",
        "G03",
        "D02"
      ],
      "blockedBy": [],
      "wave": 11,
      "zone": [
        "src/worker/runtime.py",
        "src/worker/execution.py",
        "src/worker/positions.py",
        "src/execution/",
        "src/recovery/",
        "src/modes/",
        "src/api/approvals.py",
        "src/api/schemas.py",
        "src/web/templates/dashboard.html",
        "src/web/templates/mode.html",
        "migrations/",
        "src/persistence/mode_repository.py",
        "src/persistence/models.py",
        "tests/persistence/test_postgres_infrastructure.py",
        "tests/persistence/test_repository_contracts.py",
        "tests/worker/harness.py",
        "tests/worker/test_gateway_execution.py",
        "tests/worker/test_gateway_runtime.py",
        "tests/worker/test_live_entries.py",
        "tests/worker/test_live_runtime.py",
        "tests/worker/test_positions.py",
        "tests/worker/test_restart.py",
        "tests/worker/test_daily_evidence.py",
        "tests/worker/test_scheduler.py",
        "tests/worker/test_token_risk_coverage.py",
        "tests/worker/test_f2_*.py",
        "tests/execution/",
        "tests/modes/",
        "tests/recovery/",
        "tests/api/"
      ],
      "review": "да — ремонт ветки",
      "model": "сильная",
      "status": "done",
      "startedAt": "2026-10-24T11:24:03+02:00",
      "finishedAt": "2026-10-24T12:23:50+02:00",
      "retries": 0,
      "repairs": 2,
      "repairFindings": [
        "зона расширена: политика kill switch в PostgreSQL — миграция 0003, ModeStateRecord, PostgresModeRepository (B3/R27i)",
        "SystemAlertResolver закрывает ещё актуальный EXIT_GATEWAY_UNAVAILABLE/POSITION_ACTION_FAILED на каждом цикле после подтверждённого REMOVE"
      ],
      "tests": {
        "passed": 732,
        "failed": 0
      },
      "commit": "c0566b2"
    },
    {
      "id": "F3",
      "title": "Ремонт ветки: панель, хранилище и секреты",
      "requirements": [
        "R28i",
        "R29i",
        "G13",
        "G14",
        "G20",
        "R32i"
      ],
      "blockedBy": [
        "01",
        "02"
      ],
      "wave": 12,
      "zone": [
        "src/gmgn/openapi.py",
        "src/core/",
        "src/api/",
        "src/web/",
        "src/persistence/",
        "src/worker/pipeline.py",
        "migrations/",
        "docker-compose.yml",
        "README.md",
        "docs/",
        "tests/gmgn/",
        "tests/foundation/",
        "tests/api/",
        "tests/web/",
        "tests/persistence/",
        "tests/observability/",
        ".env.example",
        "src/execution/service.py",
        "kill_switch_policy",
        "tests/execution/test_service.py",
        "tests/worker/test_pipeline.py",
        "tests/worker/test_f3_*.py",
        "tests/acceptance/",
        "scripts/acceptance-smoke.ps1"
      ],
      "review": "да — ремонт ветки",
      "model": "сильная",
      "status": "done",
      "startedAt": "2026-10-24T12:23:52+02:00",
      "finishedAt": "2026-10-24T14:00:25+02:00",
      "retries": 0,
      "repairs": 1,
      "repairFindings": [
        "acceptance smoke 3/6 после F1 (нет quote_price_usd в фикстурах, шаг 61 с не даёт окна 4m30); убрать параметр kill_switch_policy"
      ],
      "tests": {
        "passed": 941,
        "failed": 0
      },
      "commit": "eba8e67"
    },
    {
      "id": "F4",
      "title": "Ремонт после точечных ревью T17–T19: исполнение, продажа, учёт",
      "requirements": [
        "G22",
        "G23",
        "R16",
        "R27i",
        "R19"
      ],
      "blockedBy": [
        "17",
        "19"
      ],
      "wave": 15,
      "zone": [
        "src/worker/execution.py",
        "src/worker/positions.py",
        "src/worker/policy.py",
        "src/accounting/",
        "src/execution/",
        "executor/src/",
        "contracts/",
        "tests/worker/test_t17_*.py",
        "tests/worker/test_t19_*.py",
        "tests/worker/test_gateway_execution.py",
        "tests/worker/test_f4_*.py",
        "tests/accounting/",
        "tests/execution/",
        "executor/tests/"
      ],
      "review": "да — ремонт ветки",
      "model": "сильная",
      "status": "done",
      "startedAt": "2026-10-24T16:37:25+02:00",
      "finishedAt": "2026-10-24T17:35:24+02:00",
      "retries": 0,
      "repairs": 1,
      "repairFindings": [
        "повторная недоступность шлюза после проданной части не поднимает алерт (дедуп остаётся после системного закрытия)"
      ],
      "tests": {
        "passed": 1201,
        "failed": 0
      },
      "commit": "c19763b"
    },
    {
      "id": "F5",
      "title": "Ремонт после точечных ревью T18–T19: отбор и исход кандидата",
      "requirements": [
        "G23",
        "G24",
        "G04",
        "G06"
      ],
      "blockedBy": [
        "18",
        "19"
      ],
      "wave": 15,
      "zone": [
        "src/providers/meteora.py",
        "src/domain/screening/",
        "src/domain/risk/sizing.py",
        "src/worker/pipeline.py",
        "src/worker/runtime.py",
        "src/gmgn/openapi.py",
        "src/api/labels.py",
        "tests/providers/",
        "tests/domain/",
        "tests/gmgn/",
        "tests/worker/test_t18_*.py",
        "tests/worker/test_live_series.py",
        "tests/worker/test_pipeline.py",
        "tests/worker/test_live_pipeline.py",
        "tests/worker/test_f5_*.py",
        "tests/api/test_f3_labels.py"
      ],
      "review": "да — ремонт ветки",
      "model": "сильная",
      "status": "done",
      "startedAt": "2026-10-24T16:37:25+02:00",
      "finishedAt": "2026-10-24T17:17:37+02:00",
      "retries": 0,
      "repairs": 0,
      "tests": {
        "passed": 1200,
        "failed": 0
      },
      "commit": "c03e09c"
    },
    {
      "id": "F6",
      "title": "Ремонт после слепой приёмки: проверка не трогает рабочую базу",
      "requirements": [
        "R22",
        "R26i",
        "R30i",
        "G14"
      ],
      "blockedBy": [
        "20"
      ],
      "wave": 17,
      "zone": [
        "docker-compose.yml",
        "check",
        "tests/conftest.py",
        "tests/",
        "README.md",
        "docs/runbook.md"
      ],
      "review": "нет",
      "model": "сильная",
      "status": "done",
      "startedAt": "2026-10-24T18:47:44+02:00",
      "finishedAt": "2026-10-24T19:04:56+02:00",
      "retries": 0,
      "repairs": 0,
      "tests": {
        "passed": 1286,
        "failed": 0
      },
      "commit": "234d1dd"
    }
  ],
  "tests": {
    "passed": 1335,
    "failed": 0
  },
  "debt": {
    "placeholders": [
      "executor/README.md — адрес пула для ручной проверки на devnet [DEVNET_POOL — впиши] выбирает оператор",
      "README/docs — [… — впиши] для RPC URL, адреса кошелька, папок секретов и состояния executor, полей LIVE evidence, SHADOW_EQUITY — факты пользователя"
    ],
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
  "concerns": [
    "R28i: объект исключения с секретом в тексте может обойти JSON-redaction; перепроверить и закрыть на whole-branch security review",
    "Официальный Meteora/Solana npm runtime graph содержит 13 транзитивных advisory (7 moderate, 6 high), полного совместимого npm audit fix сейчас нет",
    "T02: Docker canonical check недоступен — Docker Desktop engine выключен; локально 20 тестов, Ruff и mypy зелёные",
    "T02: HALF_OPEN circuit breaker пока допускает несколько одновременных probe; перед production hardening добавить single-flight lock/state guard",
    "T05: official SDK runtime остаётся fail-closed и /health/ready=503, пока SDK boundary не предоставляет измеренный price impact; live promotion невозможен безопасно",
    "T04 src/modes/repository.py:564-569 — после claim_entry до submit kill switch никто не перечитывает; в src/execution проверки нет — нужна проверка внутри транзакции между claim и submit",
    "T04 src/modes/service.py:228-265 — ApprovalService дублирует одноразовый approval с TTL из T05; нужен один источник approvals через ExecutionService.approve",
    "T04 src/accounting/service.py:233, models.py:46-56 — close принимает ShadowReceipt вместо ExecutionReceipt; в Fill нет фактических сумм, live P&L не сверить с on-chain (§7.2)",
    "T04 src/accounting/models.py:28, service.py:296 — fee income передаёт вызывающий; нет модели начисления комиссий по PoolSnapshot, mark их не начисляет",
    "T04 src/accounting/service.py:396-404 — divergence как верхняя граница: shadow P&L пессимистичен, expectancy занижена; оформить решением",
    "T04 src/modes/service.py:64-66 — отметка о недостаточной статистике всё равно блокирует допуск (§7.1 допускает); decision_count не проверяется",
    "T04 src/modes/models.py:43 — G12.1: нет административного override дневного стопа с причиной, аудитом и outbox",
    "T04 src/modes/service.py:283-299 — realized/unrealized P&L за день передаёт вызывающий; в протоколе AccountingRepository нет запроса realized за UTC-день",
    "T04 src/accounting/models.py:102, src/portfolio/models.py:144 — realized_pnl_base_units означает две разные величины (до и после costs)",
    "T04 tests/modes/test_modes_and_stops.py:578, 700 — барьер гонки не форсируется; реальный sleep 0.5, красное доказательство вероятностное",
    "T04 src/modes/repository.py:455-461 — claim_entry отказывает, пока risk day текущих UTC-суток не открыт: план, допущенный до полуночи UTC, ждёт открытия суток worker'ом или истекает",
    "T04 src/modes/repository.py:260 — отмена PENDING при смене режима не отражена в ModeChanged (нет cancelled_plan_count); отменяются и при повышении режима",
    "T07 src/worker/portfolio.py:93 — equity snapshot только при открытии дня: starting_equity дня D не учитывает realized P&L дня D-1 (база дневного стопа §7.3)",
    "T07 src/worker/positions.py:182 — позиция переоценивается по снимку universe без проверки возраста; при лежащем провайдере HOLD и daily stop по старой цене",
    "T07 src/worker/positions.py:223,238 — kill switch с ORDERLY_EXIT не закрывает позиции, только ALERT/запись ORDERLY_EXIT_REQUIRED",
    "T07 src/worker/policy.py:101-105 — CostModel shadow: network_fee=0 и jito_tip=0, net P&L и promotion evidence без сетевых издержек (R19, §7.1)",
    "T07 src/worker/execution.py:219-221 — ExecutionBlocked после SUBMITTING превращается в FAILED и освобождает ёмкость, хотя транзакция могла уйти",
    "T07 src/observability/http.py:82 — при недоступной БД /health/ready падает исключением вместо 503 NOT_READY PERSISTENCE_UNAVAILABLE",
    "T07 src/persistence/factory.py — при APP_ENV=development (по умолчанию) SQLite audit не durable, readiness об этом молчит",
    "T07 мост-поток asyncpg блокирует event loop до command_timeout на каждый вызов протоколов T04; API звать через threadpool",
    "T07 tests/recovery/test_recovery_concurrency.py — нет тестов на PLAN_NOT_EXECUTING и ENTRY_FILL_UNTRACKED; ветку «recovery взял lease и закрыл план» не проверяет ни один тест (вариант executor_lease проходит за счёт проверки возраста)",
    "T07 src/worker/execution.py:426-434 — проверка статуса плана и fill не одна транзакция (хранилища режимов и учёта разные); окно закрыто тем, что recovery не трогает живой план",
    "T07 src/recovery/service.py:135-147 — возраст claim считается от admitted_at (в PendingPlan нет claimed_at); при зависании БД больше 2 мин чужой recovery отменит живой вход (без потери позиции)",
    "T10 src/domain/strategy/planner.py:134-139, src/worker/positions.py:102 — выход Spot на сверхобъёме «при первом падении объёма» не работает: монитор берёт отношение из политики (0.50), plan.invalidation никто не читает; без окна 5m VOLUME_DECAY не срабатывает и не даёт alert",
    "T10 tests/domain/test_guide_strategy.py:137 — тест выхода Spot проверяет только поле плана, не PositionMonitor",
    "T10 src/domain/screening/screener.py:385 — top-10 отклоняется при > 0.30, а §6.6 требует < 30%: ровно 0.30 проходит",
    "T10 src/providers/meteora.py:135 — запасной market_cap с верхнего уровня берётся без сверки mint",
    "T10 src/domain/screening/screener.py:463 — token_age_seconds в evidence не говорит, точный это возраст или нижняя оценка",
    "T10 src/providers/solana.py:102 — authority без аккаунта (Raydium CPMM, DAMM v2) не исключаются из top-10, концентрация завышается",
    "T09 src/worker/positions.py — на каждый EXIT не-SHADOW позиции по-прежнему поднимается critical alert EXIT_REQUIRES_EXECUTION_GATEWAY, хотя выход идёт через gateway",
    "T09 GatewayFillBook дублирует сборку ledger и событий ShadowBroker; модель количество×цена точна только при равных decimals (иначе ACCOUNTING_UNITS_UNSUPPORTED); комиссии в lamports не конвертируются",
    "T09 executor не отдаёт статус по подписи: при 404 на GET /v1/commands/{id} команда остаётся UNKNOWN до оператора",
    "T09 интеграционный тест против executor opt-in: профиль test в compose не поднимает executor",
    "T10 src/domain/features/models.py:45 — maximum_observation_gap (5m30s) не связан с WORKER_UNIVERSE_INTERVAL_SECONDS (runtime.py:149): интервал больше 330 с молча выключит Spot по ATH, меньше — допустит пропуск нескольких циклов",
    "T10 ath_coverage_tolerance — до 15 минут без наблюдений после создания пула: пик мемкоина на запуске может прийтись на это окно, и ATH окажется занижен",
    "T09 src/worker/positions.py:244-249 (зона T07) — после успешного выхода через gateway остаётся critical alert EXIT_REQUIRES_EXECUTION_GATEWAY; открытые alerts блокируют promotion (src/modes/repository.py:223), каждый выход в APPROVAL мешает переходу в LIVE",
    "T09 src/worker/execution.py:1305, 1133-1151 — исключение book в _settle после подтверждённого выхода вылетает из apply: alert не поднимается, остальные позиции в этом и следующих циклах не обрабатываются",
    "T09 src/execution/http_gateway.py:103-104 — 409 idempotency_conflict и approval_already_used дают FAILED и освобождают ёмкость, хотя команда могла уйти; должны оставлять UNKNOWN до сверки",
    "T09 src/worker/execution.py:180, 192-196 — amount_y = size_base_units // 2 в единицах quote-токена без проверки, что quote mint совпадает с валютой капитала (пул в USDC при капитале в lamports)",
    "T09 src/worker/execution.py:355-362 — fill учитывается по рыночной цене и модельным затратам, а не по симуляции или receipt; сверка учёта с receipts (§7.2) невозможна",
    "T09 src/worker/execution.py:338-457, src/accounting/service.py:300-301 — ledger и события собираются второй раз рядом с ShadowBroker, пути могут разойтись",
    "T09 tests/execution/test_http_gateway.py:296-320 — идемпотентность реализует мок, тест проверяет мок",
    "T09 tests/execution/test_executor_integration.py:24, docker-compose.yml:79-98 — интеграционный тест против executor opt-in, канонический check его пропускает",
    "T09 src/worker/execution.py — частично прошедшая часть REMOVE не попадает в учёт; позиция OPERATOR_REQUIRED, ручного пути оператора в worker нет; такая же частичная логика у входа (GatewayEntrySettler освобождает ёмкость) не тронута",
    "T09 src/worker/execution.py:1369-1378 — _partially_executed консервативен: первая транзакция упала, вторая не уходила — позиция всё равно уходит к оператору",
    "T11 src/providers/meteora.py:244 — ответ с total=0 и пустым data проходит как полный обход, пустая версия ложится поверх последней целой; нужно sync FAILED и DEGRADED",
    "T11 src/providers/meteora.py:357 — source_at = время прихода страницы: при stale_after 2 мин и 2 rps первые страницы станут STALE начиная примерно с 240 страниц",
    "T11 src/providers/meteora.py:488-494 — market_cap 0.0 и current_price 0.0 у токенов без цены идут как известный 0 и дают MARKET_CAP_BELOW_THRESHOLD вместо UNKNOWN",
    "T11 src/providers/meteora.py:471-477 — нет границы правдоподобия created_at (до запуска DLMM 2023-11 или позже source_at)",
    "T11 src/providers/meteora.py:281-301 — отказ RPC при чтении LB pair или mint глотается и не виден в health или SweepReport",
    "T11 tests/providers/test_meteora.py — нет тестов на STALE (старый удалён), на сдвиг страниц и на пустой universe",
    "T11 src/providers/meteora.py:278 — при перечитывании reported_total перезаписывается: новый пул в конце за время ремонта даёт ложный FAILED «missed 1 of N» (безопасная сторона)",
    "T11 src/providers/meteora.py — удаление пула до курсора проходит молча (total падает и сходится с числом различных); total, изменившийся между страницами одного обхода, должен вести к перечитыванию или неполному обходу; удаление+вставка по счётчикам не видны вообще",
    "T11 tests/providers/test_meteora.py — test_pages_are_fetched_ahead_with_bounded_concurrency флейкает на Windows (грубый таймер, 1 из 7–10)",
    "T06 src/api/catalog.py — запросы на чтение решений написаны в API поверх ORM-моделей T07, потому что в DecisionStore нет списка решений; граница ports.persistence нарушена, перенести в persistence",
    "T06 шаблоны и статика лежат в src/web/templates и src/web/static, а не в templates/ и static/ в корне (Dockerfile копирует только src)",
    "T06 нет кнопки «повторить синхронизацию»: у API нет канала команд к worker",
    "T06 метрики GMGN в панели распознаются по именам evidence (gmgn, insider, bundl, phish, developer, dev_) — T12 должен держаться этих имён",
    "T13 executor не делает swap: price impact — котировка SDK обменной части, кошелёк должен держать оба токена, иначе симуляция падает (ничего не отправляется)",
    "T13 V2 OPEN симулируется один раз: устаревшая симуляция становится EXPIRED и вход планируется заново; ACTIVE_BIN_TOLERANCE=3 — константа worker, а не настройка политики",
    "T13 леджер проводит V2-fill в форме V1, X пересчитан в decimals Y",
    "T06 docker-compose.yml:45-54 — LIVE_PROMOTION_FILE и EXECUTOR_MODE_TOKEN_FILE не передаются в api: /promotion и переход в LIVE в compose всегда отклоняются; TRADING_MODE уходит в api, но не в worker",
    "T06 src/api/catalog.py:15-26,122-198 — каталог читает ORM T07 мимо ports.persistence; чтение списка и последнего решения по пулу должно быть методами DecisionStore",
    "T06 src/api/catalog.py:128-147,182-195, reads.py:187 — нет индекса на candidate_decisions.seq и (pool_id, seq DESC): keyset и DISTINCT ON по всем решениям на каждом рендере /, /pools, /settings",
    "T06 src/api/presenters.py:57 — метрики GMGN ищутся по подстрокам имён evidence; нужен точный список, общий с T12",
    "T06 src/api/actions.py — ModeService.resolve_alert не выведен в API: открытый critical alert навсегда держит gate APPROVAL (CRITICAL_ALERTS_OPEN, §7.1)",
    "T06 src/web/templates/dashboard.html — нет действия «повторить синхронизацию» (§5.1 R17.1), показан только текст расписания",
    "T06 src/api/approvals.py:292-298 — сбой service.approve не через ExecutionBlocked оставляет в аудите stage=approved без approval_rejected; нет теста на 503 AUDIT_UNAVAILABLE",
    "T06 src/api/approvals.py:101,212 — словарь _locks растёт на каждый command_id и не чистится",
    "T06 src/web/routes.py:414-417 — GET //evil.example даёт 404 со ссылкой «Вернуться» href=//evil.example; back должен проходить через _safe_next",
    "T13 src/worker/execution.py:823, executor/src/engine.ts:1000 — impact на исполнении сравнивается с max_slippage_bps, ScreeningPolicy.maximum_price_impact не участвует; нужен один лимит или минимум из двух",
    "T13 src/execution/contracts.py:368,373,221 — Python принимает V2-receipt без signature/error и ошибку без retryable, TS такое отклоняет; нужен отрицательный кейс в v2-corpus",
    "T13 executor/src/sdk.ts:376 — готовность price_impact = наличие методов SDK (в образе всегда true); README не должен называть это «измерение доступно»",
    "T13 src/execution/http_gateway.py:15,17 — docstring говорит «V1 contract», а wire уже V2",
    "T13 src/worker/execution.py:918 — контекст несостоявшейся позиции остаётся навсегда (инертен, но не чистится)",
    "T13 нет теста на ветку CONFIRMED у GatewayEntrySettler для V2",
    "T06 docker-compose.yml:48-49 — комментарий не говорит, что *_FILE — путь внутри контейнера и требует монтирования; с путём хоста api падает при старте",
    "T06 tests/api/test_api_deployment.py:26 — Path(docker-compose.yml) относительно cwd, из другого каталога тест не найдёт файл",
    "T06 в штатном стеке с TRADING_MODE=SHADOW эффективный режим всегда SHADOW: понижение из APPROVAL проверить нечем без обвязки APPROVAL",
    "T13 executor/src/sdk.ts — simulationFailure читает только логи программ: InsufficientFundsForFee/Rent с пустыми логами уходит как TRANSACTION_SIMULATION_FAILED, а не INSUFFICIENT_FUNDS",
    "T13 src/execution — нет Python-теста на отклонение входа с null price impact (PRICE_IMPACT_UNAVAILABLE), хотя контракт V2 теперь пропускает null",
    "T13 src/worker/execution.py — ветка «CONFIRMED без адреса позиции → PENDING + ENTRY_POSITION_UNKNOWN» не покрыта тестом (для V2 практически недостижима)",
    "T15 src/api/approvals.py:129-134 — очередь показывает KILL_SWITCH_ACTIVE, DAILY_STOP_ACTIVE, ENTRY_ANCHOR_UNAVAILABLE как глобальные, будто невозможны и выходы",
    "T15 src/api/approvals.py:540-556, 498-504 — для выхода строки plan_diff active_bin_id и expires_at показывают «расходится»; для REMOVE/CLOSE нужно matches=None",
    "T15 src/api/approvals.py:78, 353-357 — правило операций входа и impact дублирует service.py:46, 854-864 (_ENTRY_OPERATIONS, _simulation_rejection); нужно одно определение",
    "T15 src/web/templates/approval.html:38 — для выхода max_slippage_bps подан как «лимит» impact, при null выводится «не измерен bps»",
    "T15 src/api/schemas.py:509, src/worker/positions.py:240 — политика kill switch, выбранная оператором при активации, ни на что не влияет: worker и service берут её из конфигурации деплоя",
    "T15 src/api/approvals.py — снятие MODE_NOT_APPROVAL ловит «стоп активен и режим SHADOW», а не кто понизил: после ручного SHADOW и затем дневного стопа выход APPROVAL-позиции одобряется",
    "T15 tests/api — нет теста на kill switch вместе с дневным стопом при HOLD_AND_ALERT (проба: блокируется, верно)",
    "T15 доверие к хранилищу исполнения: запись OPEN, переписанная в CLOSE, при ORDERLY_EXIT получает 200 APPROVED в desk (execute() потом не отправит из-за пересчёта дайджеста) — так было и до T15",
    "T14 src/worker/policy.py:70, src/market/windows.py:89 — minimum_interval 1 мин при окне 5m: дельта за 60 с растягивается ×5 (одна сделка 20k → 5m = 100k), на каждом рестарте и при cadence < 5 мин; окно короче цели не масштабировать вверх или собирать 5m суммой соседних дельт",
    "T14 src/worker/pipeline.py:331,349 — эталон котировки entry_notional_quote (100) в quote-токене пула: на SOL-пулах это 100 SOL, а не ~100 USD; depth в единицах quote сравнивается с порогами в USD; тестовые котировки не зависят от размера",
    "T14 src/worker/pipeline.py:352 — quoted_at позже features.decision_at не проверяется (look-ahead по котировке)",
    "T14 src/worker/evidence.py:145 — пулы, отсеянные дешёвым фильтром за пробел данных, не входят в знаменатель coverage: сутки с пробелами у всех пулов получают coverage = 1",
    "T14 src/worker/evidence.py:154 — decision_count считает каждую оценку каждого пула каждые 60 с (~3 млн в сутки), «выборка решений» теряет смысл",
    "T14 src/domain/strategy/planner.py:136-143 — если окно 5m у открытой позиции UNKNOWN, нет ни VOLUME_DECAY, ни алерта: Spot на сверхобъёме молча не выходит",
    "T14 src/worker/runtime.py:206 — без SOLANA_RPC_URL SHADOW даёт REJECT TOKEN_RISK_STALE, а §6.6 требует UNKNOWN с флагом",
    "T14 tests/worker/test_live_entries.py:192-199 закрепляет amount_x>0 для дефолтного входа; FlatQuotes (test_live_series.py:82) отвечает 10 bps на любой размер",
    "T14 executor-quotes: файл токена (*_FILE) смонтировать некуда — работает только переменная окружения",
    "T14 src/market/windows.py:457 — если сразу после COUNTER_RESET счётчик возвращается выше старой метки, прирост засчитывается как объём (900k держится 30 мин, затем 1.005M → окно 105 000 при ~5 000): ложный всплеск",
    "T14 src/market/windows.py:457 — внутри серии низких показаний нет допуска на шум: откат на −1 перезапускает отсчёт сброса; COUNTER_RESET ставится, даже если сбросились только комиссии",
    "T14 src/worker/positions.py — если T выхода по верху больше охвата истории (288 снимков × интервал), отсчёт упрётся в самый старый снимок и выход по T не наступит; проверки нет",
    "T12 src/worker/policy.py:412 — при RULESET_VERSION=shadow-hypothesis-v1 GMGN_REQUIRED_FOR_LIVE не читается: LIVE-гейт GMGN снят (закрыт только побочно)",
    "T12 tests/gmgn/test_gmgn_deployment.py:13 — три сторожа (пин playwright, нет playwright в основном lock, нет stealth/solver) пропускаются в каноническом check и в check-стадии Dockerfile.gmgn",
    "T12 services/gmgn/tests/fixtures/kimchi_token_page.json:20,110 — token_pool_fee_info и token_holder_stat побайтно скопированы с WIF: синтетика под видом записи; developer_share живьём виден только равным 0",
    "T12 services/gmgn/gmgn_service/page.py:116 — цикл _observe не покрыт тестами (BLOCKED/CAPTCHA, CHALLENGE_TIMEOUT, RATE_LIMITED/API_BLOCKED, settle)",
    "T12 src/gmgn/enrichment.py:53 — страница GMGN грузится параллельно с RPC, до token hard filters (§6.7: после); отказ RPC выбрасывает готовое чтение и пишет NOT_CONFIGURED",
    "T12 services/gmgn/gmgn_service/config.py:64, .env.example:76 — 10 переменных сервиса не названы ни в compose, ни в .env.example",
    "T12 src/api/labels.py:21,55, _TRIGGERS — нет русских подписей и подсветки для INSIDERS_SHARE_HIGH, BUNDLING_SHARE_HIGH, PHISHING_SHARE_HIGH, GMGN_TOP10_HOLDER_SHARE_HIGH, GMGN_UNAVAILABLE, DEVELOPER_HOLDS_TOKENS",
    "T12 временная недоступность RPC даёт терминальный TOKEN_RISK_STALE (fail-closed, в _DATA_GAPS), а §5.3 G06.1 требует UNKNOWN для недоступного источника — ярлык расходится со спекой",
    "T12 src/api/labels.py — нет русского текста для TOKEN_RISK_UNAVAILABLE",
    "T12 src/worker/pipeline.py — LIVE-решение, отклонённое по VOLUME_PARTICIPANTS_UNAVAILABLE или GMGN_UNAVAILABLE, засчитывается как покрытое данными (этих причин нет в _DATA_GAPS)",
    "T12 tests/worker/test_token_risk_coverage.py — метод evidence подменён: перевод covered=False в data_coverage не проверяется; хелперы импортируются из tests.gmgn.test_token_risk_sources",
    "src/core/logging.py:15 — редактор логов не знает значения GMGN_API_KEY (ключа нет в AppConfig); сейчас ключ нигде не логируется (T16)",
    "src/api/reads.py:60 — config/effective не показывает, задан ли ключ GMGN; ключ в api намеренно не передаётся (T16)",
    "src/gmgn/openapi.py — total_fee из token/info считается в SOL по совпадению с браузерным путём; официальная документация единицу не называет (T16)",
    "src/gmgn/openapi.py — тела ответов 429/403 взяты из документации, вживую не воспроизведены (T16)",
    "src/gmgn/openapi.py:78,515-521 — строка error от сервера, похожая на код ошибки, становится причиной: ключ вида [A-Z][A-Z0-9_]* попадёт в gmgn_status, health и лог gmgn_openapi_blocked; тест test_gmgn_openapi.py:46,680,699 зелёный только из-за ключа в нижнем регистре (ревью T16, безопасность)",
    "src/worker/runtime.py:800 vs 670 — OpenApiSettings.from_environment вызывается после open_repositories: неверная конфигурация GMGN роняет старт с открытыми хранилищами; правило T09 — проверять до открытия, как load_pipeline_policy (ревью T16)",
    "src/worker/positions.py:252, src/worker/execution.py:1526, src/api/approvals.py:343 — выбор политики kill switch ORDERLY_EXIT только пишется в журнал; worker и панель всегда берут HOLD_AND_ALERT, настройки нет (T08)",
    "src/modes/service.py:165 — ModeService.resolve_alert ни откуда не вызывается: critical alert (DAILY_STOP_TRIGGERED и др.) не закрыть из панели/API, навсегда блокирует допуск в APPROVAL; обход — CLI в runbook §8 (T08)",
    "src/execution/settings.py — LIVE_PROMOTION_FILE заполняет оператор: 10 циклов APPROVAL и сверку receipts система не пересчитывает (T08)",
    "docker-compose.yml — api не получает RULESET_VERSION/RULESET_OVERRIDES_FILE и считает evidence по своей копии правил; executor в профиле live не получает переменных (T08)",
    "src/persistence/outbox.py, RetentionPolicy — retention чистит только outbox; снимки и решения копятся без предела (T08)",
    "src/accounting/service.py — SHADOW fee income (F1/B7) грубый: диапазон проверяется только по цене в момент fill, комиссии пула — пропорционально наименьшему окну, покрывающему удержание; сеть и Jito в SHADOW по-прежнему 0 (C021), расхождение пессимистично (C010)",
    "src/modes/alerts.py — система находит свои алерты по тегу [position:…]/[command:…] в тексте сообщения: поля субъекта в CriticalAlert нет (F2)",
    "src/recovery/service.py — пока есть открытые алерты, recovery раз в минуту читает alerts() целиком (F2)",
    "src/worker — системный kill switch при нарушении инварианта берёт политику из правил, то есть HOLD_AND_ALERT (F2)",
    "src/market/windows.py — при каденсе universe короче 4.5 мин окно 5m бывает не на каждом свипе (F1)",
    "tests/worker/test_live_series.py — в записанной серии нет цен токенов; подставлены цены, записанные ~1.5 ч раньше в тот же день (F1)",
    "src/modes/alerts.py — тег [position:…] ищется подстрокой в message, а в POSITION_ACTION_FAILED попадает код ошибки executor (SimulationErrorV1.code без шаблона): код с «[position:Y]» привязал бы алерт к чужой позиции; executor свой, риск низкий (повторное ревью F2)",
    "src/worker/execution.py — системное закрытие алертов не повторяется: упади worker между проводкой и resolve или не прочитай алерты (alert_resolution_skipped) — старые алерты останутся открытыми, закрывать их оператору (повторное ревью F2, в безопасную сторону)",
    "src/worker/pipeline.py — принятый кандидат по-прежнему записывается каждый цикл (план требует свежих фактов); если запись аудита упала после записи решения, неизменный исход больше не даст события — поднимается AUDIT_PERSISTENCE_FAILED (F3)",
    "src/api — аудит закрытия алерта пишется как CriticalAlertRaised со stage=resolved: отдельного EventType нет, он в src/domain (F3)",
    "src/worker/pipeline.py — пулы токена после основного в цикле не записываются: на панели у них остаётся старая запись (T18)",
    "src/gmgn/openapi.py — кэш GMGN 300 с: «текущие 5m» по токену могут отставать до 5 минут; браузерный путь GMGN новых полей (объём, ATH, снайперы, статус дева) не даёт (T18)",
    "src/domain/screening — группировка по токену действует и для shadow-hypothesis-v1, но без лимита N; volume_to_liquidity остался по пулу (T18)",
    "src/worker/execution.py — не-SHADOW CLOSE проводится в учёте только после окончания продажи монеты: позиция остаётся открытой в modes и занимает слот на время продажи (T17)",
    "src/accounting — комиссии из receipt идут в учёт, только если quote = wSOL; иначе и в SHADOW — модель издержек; для wSOL delta_y у CLOSE включает возврат ренты позиции (T17)",
    "src/api/approvals.py — карточка продажи монеты после выхода требует тот же чеклист G17, что и выход (T17)",
    "src/domain/screening/models.py:309, screener.py:317 — фильтр тем по подстроке даёт ложные совпадения: WATERMELON/Belong/Felony→elon, Natural Selection→election, MAGAZINE→maga, Computing/Rasputin→putin, Маска/Маскот→маск, Trumpet→trump, Mainstream/Upstream→stream, Musket→musk; условие: эти имена не дают THEME_EXCLUDED, а ELONDOGE/BABYTRUMP/MAGA дают (ревью T18)",
    "src/worker/runtime.py:503,569, src/domain/screening/selection.py:85 — при ключе GMGN top-N ранжируется по сумме DLMM-пулов, а §6.8 требует объём токена за 1 ч; токен, торгующийся вне Meteora, может не попасть в глубокую проверку (ревью T18)",
    "src/gmgn/openapi.py:626 — комментарий «price.volume_* — USD по всем пулам токена» проверен только на однопульном токене; запись WIF (1h 1 733 USD) противоречит (ревью T18)",
    "src/providers/meteora.py:636 — base_mint всегда token_x: пул SOL-MEME (токен вторым) не попадает в группу токена и в TOKEN_ALREADY_HELD — «одна позиция на токен» обходится; условие: группировка и held видят токен с любой стороны пары, кроме quote-минтов (ревью T18, денежное)",
    "src/worker/execution.py:2141 — часть продажи в UNKNOWN/SUBMITTED, о которой executor не знает (lookup → None), бессрочно держит проводку CLOSE и слот без алерта; условие: после settlement_timeout или истечения команды — critical alert (ревью T17, денежное)",
    "src/worker/execution.py:2190,2158 — ExecutionBlocked из prepare без записи и CONTEXT_MISSING каждый цикл дают SELLING/PENDING без алерта и без счёта попыток; условие: critical alert или исход — неудачная часть (ревью T17)",
    "src/worker/execution.py:1633 — EXIT_SALE_GATEWAY_UNAVAILABLE нет в _CLEARED_BY_GATEWAY_SUCCESS: после успешной продажи алерт остаётся и держит гейт §7.1; условие: первая подтверждённая часть или проводка CLOSE закрывает его (ревью T17)",
    "executor/src/sdk.ts:329 — отказ котировки по ликвидности считается PRICE_IMPACT_UNAVAILABLE: часть повторяется того же размера до UNSOLD, а не делится (ревью T17)",
    "src/accounting/service.py:467 — в SHADOW impact продажи из cost_model.slippage_bps, а не из котировки; лимит impact и деление на части не моделируются (ревью T17)",
    "executor/src/engine.ts:1145 — executor не сверяет in_amount SWAP с delta_x подтверждённых REMOVE/CLOSE позиции: граница только в Python; условие: executor отклоняет SWAP сверх delta_x (ревью T17, безопасность в глубину)",
    "executor/src/sdk.ts:555 — потолок PRIORITY_FEE_MAX_LAMPORTS на одну транзакцию: REMOVE/CLOSE из N транзакций платит до N × потолок; документировать или считать на команду (ревью T17)",
    "src/accounting, src/worker/policy.py — в SHADOW priority fee моделируется по потолку PRIORITY_FEE_MAX_LAMPORTS на каждую транзакцию (≈0.045 SOL за круг) и только в пулах к wSOL: на позициях 1.3–3.5% капитала это может снова сделать net P&L SHADOW отрицательным и допуск в APPROVAL недостижимым; условие: SHADOW берёт реалистичную оценку (настройка, по умолчанию ниже потолка, например медиана/перцентиль сети), а потолок — только ограничение (T19, денежное)",
    "src/domain/strategy, src/domain/risk/sizing.py — план, не прошедший стратегию (RANGE_TOO_NARROW, NOT_NEAR_LOCAL_HIGH), отклоняется через PositionSizer: решение остаётся ACCEPT, плана нет, аудит RISK_REJECTED — на панели «принят» без входа (T19)",
    "executor/src/sdk.ts — потолок позиции 70 бинов: SDK 1.9.14 умеет до 1400 бинов только цепочкой зависимых транзакций; Bid-Ask ≥70% возможен лишь при bin step ≳176, на bin step 100 Bid-Ask отклоняется, Spot ≈50% (T19)",
    "src/worker/positions.py — тейк-профит по консервативной оценке видит только комиссии, рост цены не засчитывает; перечитывания пула хранятся в памяти и после рестарта начинаются заново (T19)",
    "src/domain/strategy/planner.py:428, src/worker/execution.py:944 — «≥90% хая» и RANGE_TOO_NARROW проверяются по цене плана, а допуск 30% пропускает вход до −34% от неё; условие: перед не-SHADOW командой условие хая перепроверяется по свежему активному бину (ревью T19, денежное)",
    "src/worker/positions.py:428-442 — в APPROVAL/LIVE тейк-профит считается по модели SHADOW (доля комиссий пула за интервал при цене в диапазоне в момент обзора) и может закрыть реальную позицию в минусе; условие: для не-SHADOW net P&L из фактической позиции или ограничение видно оператору (ревью T19, денежное)",
    "src/domain/risk/sizing.py:30 — отказ стратегии уходит в RiskRejection: в журнале решение ACCEPT, коды стратегии в RiskReason; §6.8 — «плана нет»; условие: исход кандидата показывает NOT_NEAR_LOCAL_HIGH/RANGE_TOO_NARROW (ревью T19)",
    "executor/src/sdk.ts:168 — бин-допуск SDK = active_bin_tolerance для всех OPEN/ADD, включая shadow-hypothesis-v1: 3 бина вместо 1% цены, не оформлено (ревью T19)",
    "src/worker/execution.py:801 — GatewaySubmitter при немедленном подтверждении (LIVE) пишет fill по цене снимка, а settler и restart — по context.entry_price с учётом дрейфа активного бина: цена входа в учёте зависит от пути (T19)",
    "src/market/models.py:464, src/api/labels.py (TOKEN_VOLUME_POOL_ONLY) — осталось непроверенное утверждение «по всем пулам/площадкам» об объёме GMGN (F5)",
    "src/worker/policy.py — theme_affixes (F5) нельзя переопределить через RULESET_OVERRIDES_FILE без правки policy.py (F5)",
    "src/domain/screening/pairs.py — в паре без quote-токена (JUP-MEME) занятым считается только token_x: legacy после входа в неё пустит вход в MEME-SOL; если у открытой позиции нет записи решения, проверка берёт position.base_mint (wSOL для legacy-позиции в SOL-MEME) (повторное ревью F5)",
    "src/worker/execution.py — неизвестная часть продажи (F4) только поднимает EXIT_SALE_OUTCOME_UNKNOWN, слот держится, пока исход не станет известен: ручного пути оператора в worker нет (F4)",
    "src/worker/execution.py — условие «≥90% хая» перепроверяется при построении команды, а между одобрением и отправкой действует только симметричный допуск executor (F4)",
    "src/worker/positions.py — тейк-профит для APPROVAL/LIVE выключен (take_profit=UNMEASURED), пока нет фактической стоимости позиции (F4)",
    "executor — сверка SWAP с delta_x переживает рестарт через FileExecutionStateStore, но теста на файловый store нет; путь 409 SWAP_EXCEEDS_EXIT_RETURNS → UNSOLD/EXIT_SALE_FAILED в Python без теста (повторное ревью F4)",
    "src/observability (CriticalAlertReporter) — дедуп алертов ключом в памяти процесса не сбрасывается системным закрытием: алерты, которые закрывает система (например EXIT_GATEWAY_UNAVAILABLE по попытке), полагаются на то, что после успеха их ключ не повторится (F4)",
    "src/worker — после stop/start стека acceptance DOGE-SOL 5 раз подряд открывалась и закрывалась по VOLUME_DECAY через 5–35 с: объём при входе и у монитора позиции расходятся, паузы перед повторным входом по тому же токену нет; вероятно, откат счётчиков fixture-api, на живом API не проверено",
    "pipeline — пулы, отсеянные дешёвыми фильтрами, показывают TOKEN_RISK_UNKNOWN (риск токена не читался) — шум для оператора",
    "docker-compose.yml:18 — комментарий «SHADOW books it» про потолок priority fee устарел после F4",
    "services/gmgn/gmgn_service/bridge.py:316-328 — _bind_hosts принимает любой частный адрес (192.168/16, 10/8): посредник может слушать локальную сеть (с токеном), шире критерия «loopback и адрес Docker»",
    "src/worker/runtime.py:912 — developer=gmgn включает опрос девелопера (DEV_SOLD) и для headless-сервиса v1, а не только при OpenAPI или посреднике",
    "src/gmgn/client.py:68 — bridge_token повторяет _secret из src/gmgn/openapi.py:140 (NAME/NAME_FILE)",
    "src/gmgn/client.py — кэш и cached_signals скопированы из GmgnOpenApiProvider (openapi.py:307), две копии одной семантики F5",
    "src/worker/runtime.py:1022 — поле лога token= маскируется редактором в [REDACTED], из лога не видно, задан ли токен посредника"
  ],
  "blind": {
    "checked": 17,
    "matched": 11,
    "mismatches": [
      "Автоматизация заработка — на живых данных за 21 мин 0 кандидатов: прогрев (~40 мин) не закончился, 2147 из 2213 пулов ниже порога автора 14 000 USD/мин; путь до позиции подтверждён только на записанных данных",
      "Ручной чеклист — виден только в APPROVAL, в SHADOW запуском не проверить",
      "GMGN — без ключа во всех решениях GMGN_UNAVAILABLE, с реальным ключом не проверено",
      "Продажа монеты после выхода — на живом прогоне позиций не было, видна только в коде и тестах",
      "Код на рабочем столе — копии ещё нет (делается после сдачи)",
      "GitHub — remote нет (делается в конце, после входа gh)",
      "Канонический check при запущенном стеке — 3 failed: check пишет в рабочую базу meteora → ремонт F6"
    ]
  },
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
    "2026-10-18T13:56:52+03:00",
    "2026-10-18T13:57:43+03:00",
    "2026-10-18T13:57:53+03:00",
    "2026-10-18T13:58:05+03:00",
    "2026-10-18T14:14:34+03:00",
    "2026-10-18T14:37:44+03:00",
    "2026-10-18T14:42:11+03:00",
    "2026-10-18T14:55:00+03:00",
    "2026-10-18T14:55:03+03:00",
    "2026-10-18T14:55:11+03:00",
    "2026-10-18T19:15:28+03:00",
    "2026-10-18T19:29:54+03:00",
    "2026-10-18T19:35:18+03:00",
    "2026-10-18T19:35:59+03:00",
    "2026-10-19T09:50:39+03:00",
    "2026-10-19T09:58:31+03:00",
    "2026-10-19T10:10:02+03:00",
    "2026-10-19T10:23:38+03:00",
    "2026-10-19T10:23:41+03:00",
    "2026-10-19T10:23:44+03:00",
    "2026-10-19T10:24:32+03:00",
    "2026-10-20T12:17:44+03:00",
    "2026-10-20T12:17:49+03:00",
    "2026-10-20T12:31:27+03:00",
    "2026-10-20T12:32:29+03:00",
    "2026-10-20T12:36:17+03:00",
    "2026-10-20T12:36:48+03:00",
    "2026-10-20T12:36:50+03:00",
    "2026-10-21T10:20:26+03:00",
    "2026-10-21T10:24:04+03:00",
    "2026-10-21T10:29:35+03:00",
    "2026-10-21T10:29:42+03:00",
    "2026-10-21T10:41:16+03:00",
    "2026-10-21T10:43:59+03:00",
    "2026-10-21T10:54:21+03:00",
    "2026-10-23T13:39:43+03:00",
    "2026-10-23T13:42:14+03:00",
    "2026-10-23T14:10:51+03:00",
    "2026-10-23T14:18:27+03:00",
    "2026-10-23T14:18:28+03:00",
    "2026-10-23T14:19:35+03:00",
    "2026-10-23T14:19:36+03:00",
    "2026-10-23T14:23:40+03:00",
    "2026-10-23T14:23:41+03:00",
    "2026-10-23T15:25:09+03:00",
    "2026-10-23T15:29:20+03:00",
    "2026-10-23T15:38:46+03:00",
    "2026-10-23T15:38:47+03:00",
    "2026-10-23T15:44:58+03:00",
    "2026-10-23T15:44:59+03:00",
    "2026-10-23T15:55:27+03:00",
    "2026-10-23T15:55:28+03:00",
    "2026-10-23T16:27:21+03:00",
    "2026-10-23T16:27:22+03:00",
    "2026-10-23T16:29:00+03:00",
    "2026-10-23T16:29:01+03:00",
    "2026-10-23T16:29:19+03:00",
    "2026-10-23T16:29:48+03:00",
    "2026-10-23T16:40:03+03:00",
    "2026-10-23T16:41:11+03:00",
    "2026-10-23T16:41:12+03:00",
    "2026-10-23T16:44:40+03:00",
    "2026-10-23T16:46:29+03:00",
    "2026-10-23T16:46:30+03:00",
    "2026-10-23T16:46:31+03:00",
    "2026-10-23T16:56:37+03:00",
    "2026-10-23T16:56:38+03:00",
    "2026-10-23T17:04:29+03:00",
    "2026-10-23T17:05:15+03:00",
    "2026-10-23T17:05:16+03:00",
    "2026-10-23T17:16:09+03:00",
    "2026-10-23T17:18:19+03:00",
    "2026-10-23T17:25:59+03:00",
    "2026-10-23T17:36:47+03:00",
    "2026-10-23T17:36:48+03:00",
    "2026-10-23T17:50:36+03:00",
    "2026-10-23T17:50:37+03:00",
    "2026-10-23T17:50:38+03:00",
    "2026-10-23T17:53:41+03:00",
    "2026-10-23T17:53:42+03:00",
    "2026-10-23T17:53:43+03:00",
    "2026-10-23T17:55:32+03:00",
    "2026-10-23T17:59:41+03:00",
    "2026-10-23T18:02:12+03:00",
    "2026-10-23T18:11:13+03:00",
    "2026-10-23T18:11:14+03:00",
    "2026-10-23T18:12:51+03:00",
    "2026-10-23T18:12:52+03:00",
    "2026-10-23T18:25:57+03:00",
    "2026-10-23T18:25:58+03:00",
    "2026-10-23T18:25:59+03:00",
    "2026-10-23T18:26:42+03:00",
    "2026-10-23T18:34:17+03:00",
    "2026-10-23T18:39:50+03:00",
    "2026-10-23T18:49:15+03:00",
    "2026-10-23T18:49:16+03:00",
    "2026-10-23T19:24:27+03:00",
    "2026-10-23T19:37:55+03:00",
    "2026-10-23T19:52:05+03:00",
    "2026-10-23T20:09:31+03:00",
    "2026-10-23T20:09:32+03:00",
    "2026-10-23T20:53:08+03:00",
    "2026-10-23T21:03:17+03:00",
    "2026-10-23T21:03:18+03:00",
    "2026-10-23T21:18:21+03:00",
    "2026-10-23T21:18:22+03:00",
    "2026-10-23T21:24:23+03:00",
    "2026-10-23T21:24:24+03:00",
    "2026-10-23T21:25:03+03:00",
    "2026-10-24T08:59:01+02:00",
    "2026-10-24T10:00:46+02:00",
    "2026-10-24T10:06:49+02:00",
    "2026-10-24T10:09:10+02:00",
    "2026-10-24T10:09:13+02:00",
    "2026-10-24T10:09:14+02:00",
    "2026-10-24T10:36:43+02:00",
    "2026-10-24T10:36:44+02:00",
    "2026-10-24T10:43:57+02:00",
    "2026-10-24T10:43:58+02:00",
    "2026-10-24T11:02:37+02:00",
    "2026-10-24T11:02:38+02:00",
    "2026-10-24T11:04:43+02:00",
    "2026-10-24T11:04:44+02:00",
    "2026-10-24T11:18:05+02:00",
    "2026-10-24T11:21:18+02:00",
    "2026-10-24T11:23:29+02:00",
    "2026-10-24T11:23:30+02:00",
    "2026-10-24T11:24:02+02:00",
    "2026-10-24T11:24:03+02:00",
    "2026-10-24T11:57:08+02:00",
    "2026-10-24T11:57:09+02:00",
    "2026-10-24T11:57:10+02:00",
    "2026-10-24T12:02:09+02:00",
    "2026-10-24T12:02:10+02:00",
    "2026-10-24T12:02:11+02:00",
    "2026-10-24T12:02:31+02:00",
    "2026-10-24T12:02:57+02:00",
    "2026-10-24T12:02:58+02:00",
    "2026-10-24T12:07:47+02:00",
    "2026-10-24T12:10:19+02:00",
    "2026-10-24T12:10:20+02:00",
    "2026-10-24T12:16:07+02:00",
    "2026-10-24T12:16:08+02:00",
    "2026-10-24T12:20:21+02:00",
    "2026-10-24T12:23:50+02:00",
    "2026-10-24T12:23:51+02:00",
    "2026-10-24T12:23:52+02:00",
    "2026-10-24T13:20:42+02:00",
    "2026-10-24T13:20:43+02:00",
    "2026-10-24T13:34:52+02:00",
    "2026-10-24T13:34:53+02:00",
    "2026-10-24T13:35:11+02:00",
    "2026-10-24T13:45:55+02:00",
    "2026-10-24T13:45:56+02:00",
    "2026-10-24T13:50:19+02:00",
    "2026-10-24T14:00:25+02:00",
    "2026-10-24T14:00:26+02:00",
    "2026-10-24T14:00:27+02:00",
    "2026-10-24T14:45:57+02:00",
    "2026-10-24T14:45:58+02:00",
    "2026-10-24T14:53:27+02:00",
    "2026-10-24T14:58:21+02:00",
    "2026-10-24T14:58:22+02:00",
    "2026-10-24T15:03:03+02:00",
    "2026-10-24T15:08:15+02:00",
    "2026-10-24T15:09:58+02:00",
    "2026-10-24T15:09:59+02:00",
    "2026-10-24T15:11:24+02:00",
    "2026-10-24T15:21:07+02:00",
    "2026-10-24T15:24:27+02:00",
    "2026-10-24T15:24:28+02:00",
    "2026-10-24T15:24:29+02:00",
    "2026-10-24T16:09:52+02:00",
    "2026-10-24T16:24:07+02:00",
    "2026-10-24T16:30:51+02:00",
    "2026-10-24T16:30:52+02:00",
    "2026-10-24T16:36:44+02:00",
    "2026-10-24T16:37:24+02:00",
    "2026-10-24T16:37:25+02:00",
    "2026-10-24T17:11:25+02:00",
    "2026-10-24T17:11:26+02:00",
    "2026-10-24T17:17:37+02:00",
    "2026-10-24T17:21:12+02:00",
    "2026-10-24T17:21:13+02:00",
    "2026-10-24T17:27:33+02:00",
    "2026-10-24T17:32:10+02:00",
    "2026-10-24T17:32:11+02:00",
    "2026-10-24T17:35:24+02:00",
    "2026-10-24T17:45:57+02:00",
    "2026-10-24T17:46:23+02:00",
    "2026-10-24T18:17:23+02:00",
    "2026-10-24T18:19:01+02:00",
    "2026-10-24T18:19:08+02:00",
    "2026-10-24T18:47:43+02:00",
    "2026-10-24T18:47:44+02:00",
    "2026-10-24T19:04:56+02:00",
    "2026-10-24T19:04:57+02:00",
    "2026-10-24T19:05:04+02:00",
    "2026-10-24T19:42:46+02:00",
    "2026-10-24T20:19:33+02:00",
    "2026-10-24T20:36:28+02:00",
    "2026-10-24T20:43:40+02:00",
    "2026-10-24T20:49:00+02:00",
    "2026-10-24T20:49:01+02:00",
    "2026-10-24T20:50:23+02:00",
    "2026-10-24T20:50:34+02:00"
  ],
  "rev": 267,
  "waiting": null,
  "waits": [
    {
      "kind": "briefing",
      "from": "2026-10-18T13:37:41+03:00",
      "to": "2026-10-18T13:50:14+03:00"
    },
    {
      "kind": "action",
      "from": "2026-10-24T10:06:49+02:00",
      "to": "2026-10-24T10:09:10+02:00"
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
    },
    {
      "t": "2026-10-18T13:57:53+03:00",
      "k": "stage",
      "id": "build",
      "act": "start"
    },
    {
      "t": "2026-10-18T13:58:05+03:00",
      "k": "ticket",
      "ids": [
        "01"
      ],
      "act": "start"
    },
    {
      "t": "2026-10-18T14:14:34+03:00",
      "k": "ticket",
      "ids": [
        "01"
      ],
      "act": "retry"
    },
    {
      "t": "2026-10-18T14:37:44+03:00",
      "k": "ticket",
      "ids": [
        "01"
      ],
      "act": "review"
    },
    {
      "t": "2026-10-18T14:42:11+03:00",
      "k": "ticket",
      "ids": [
        "01"
      ],
      "act": "repair",
      "note": "миграции принимают DATABASE_URL_FILE; executor только внутри Compose; API по умолчанию loopback; JSON-логи без секретов; executor check проверяет реальные исходники; PostgreSQL integration покрывает repository и outbox"
    },
    {
      "t": "2026-10-18T14:55:00+03:00",
      "k": "ticket",
      "ids": [
        "01"
      ],
      "act": "done",
      "tests": {
        "passed": 10,
        "failed": 0
      }
    },
    {
      "t": "2026-10-18T14:55:03+03:00",
      "k": "add",
      "key": "concerns",
      "n": 2
    },
    {
      "t": "2026-10-18T14:55:11+03:00",
      "k": "ticket",
      "ids": [
        "02",
        "05"
      ],
      "act": "start"
    },
    {
      "t": "2026-10-18T19:15:28+03:00",
      "k": "ticket",
      "ids": [
        "02",
        "05"
      ],
      "act": "retry"
    },
    {
      "t": "2026-10-18T19:29:54+03:00",
      "k": "ticket",
      "ids": [
        "02"
      ],
      "act": "review"
    },
    {
      "t": "2026-10-18T19:35:18+03:00",
      "k": "ticket",
      "ids": [
        "02"
      ],
      "act": "repair",
      "note": "hard filters reject/UNKNOWN для token/artificial volume; глубокая immutable snapshot; stale enhanced risk не FRESH; universe batch и признаки incremental; normalized provider health наружу"
    },
    {
      "t": "2026-10-18T19:35:59+03:00",
      "k": "ticket",
      "ids": [
        "05"
      ],
      "act": "review"
    },
    {
      "t": "2026-10-19T09:50:39+03:00",
      "k": "ticket",
      "ids": [
        "02"
      ],
      "act": "retry"
    },
    {
      "t": "2026-10-19T09:58:31+03:00",
      "k": "ticket",
      "ids": [
        "05"
      ],
      "act": "repair",
      "note": "approval и simulation не связаны с exact command; invalid/one-shot TTL approval; реальный price impact fail-closed; partial multi-tx signatures; durable recovery; LIVE promotion gate; Python/TypeScript contract parity"
    },
    {
      "t": "2026-10-19T10:10:02+03:00",
      "k": "ticket",
      "ids": [
        "02"
      ],
      "act": "repair",
      "note": "cursor batching всё ещё собирает полный universe в памяти; нужен cursor-driven staging begin/stage/commit без O(n²), плюс при возможности закрыть deterministic breaker/rate-limit tests и fixture malformed parity"
    },
    {
      "t": "2026-10-19T10:23:38+03:00",
      "k": "ticket",
      "ids": [
        "02"
      ],
      "act": "done",
      "tests": {
        "passed": 20,
        "failed": 0
      }
    },
    {
      "t": "2026-10-19T10:23:41+03:00",
      "k": "add",
      "key": "concerns",
      "n": 1
    },
    {
      "t": "2026-10-19T10:23:44+03:00",
      "k": "add",
      "key": "concerns",
      "n": 1
    },
    {
      "t": "2026-10-19T10:24:32+03:00",
      "k": "ticket",
      "ids": [
        "03"
      ],
      "act": "start"
    },
    {
      "t": "2026-10-20T12:17:44+03:00",
      "k": "ticket",
      "ids": [
        "03"
      ],
      "act": "retry"
    },
    {
      "t": "2026-10-20T12:17:49+03:00",
      "k": "ticket",
      "ids": [
        "05"
      ],
      "act": "retry"
    },
    {
      "t": "2026-10-20T12:31:27+03:00",
      "k": "ticket",
      "ids": [
        "05"
      ],
      "act": "repair",
      "note": "до RPC durable сохранить derived signature и reconcile timeout без rebroadcast; operator_confirmation_id trim/nonblank на model и service gate; readiness degraded пока price impact недоступен"
    },
    {
      "t": "2026-10-20T12:32:29+03:00",
      "k": "ticket",
      "ids": [
        "03"
      ],
      "act": "review"
    },
    {
      "t": "2026-10-20T12:36:17+03:00",
      "k": "ticket",
      "ids": [
        "03"
      ],
      "act": "repair",
      "note": "identity/time binding; WARMING_UP precedence; volume/count consistency; SizedPlan/pre-trade caps; loss ROUND_CEILING; horizon/exit-aware bins; complete position monitor"
    },
    {
      "t": "2026-10-20T12:36:48+03:00",
      "k": "ticket",
      "ids": [
        "05"
      ],
      "act": "done",
      "tests": {
        "passed": 46,
        "failed": 0
      }
    },
    {
      "t": "2026-10-20T12:36:50+03:00",
      "k": "add",
      "key": "concerns",
      "n": 1
    },
    {
      "t": "2026-10-21T10:20:26+03:00",
      "k": "ticket",
      "ids": [
        "03"
      ],
      "act": "retry"
    },
    {
      "t": "2026-10-21T10:24:04+03:00",
      "k": "ticket",
      "ids": [
        "03"
      ],
      "act": "repair",
      "note": "exact provenance equality и full PoolRef history; independent volume/tx/participants contradictions; PositionMonitor max-severity чтобы EXIT не понижался missing fee"
    },
    {
      "t": "2026-10-21T10:29:35+03:00",
      "k": "ticket",
      "ids": [
        "03"
      ],
      "act": "done",
      "tests": {
        "passed": 48,
        "failed": 0
      }
    },
    {
      "t": "2026-10-21T10:29:42+03:00",
      "k": "ticket",
      "ids": [
        "04"
      ],
      "act": "start"
    },
    {
      "t": "2026-10-21T10:41:16+03:00",
      "k": "ticket",
      "ids": [
        "04"
      ],
      "act": "review"
    },
    {
      "t": "2026-10-21T10:43:59+03:00",
      "k": "ticket",
      "ids": [
        "04"
      ],
      "act": "repair",
      "note": "partial REMOVE realized P&L; atomic CAS lifecycle; persisted 7 full UTC-day evidence; block promotion on safety flags/versioned one-shot evidence; sticky atomic daily stop with persisted UTC equity; transactional kill switch/outbox/no new pending"
    },
    {
      "t": "2026-10-21T10:54:21+03:00",
      "k": "ticket",
      "ids": [
        "04"
      ],
      "act": "repair",
      "note": "next UTC-day clears daily stop; atomic risk-day+mode+critical alert+outbox; SQLite admission atomic against kill across connections; kill outbox uses transaction-derived counts"
    },
    {
      "t": "2026-10-23T13:39:43+03:00",
      "k": "ticket",
      "ids": [
        "04"
      ],
      "act": "reset"
    },
    {
      "t": "2026-10-23T13:42:14+03:00",
      "k": "ticket",
      "ids": [
        "04"
      ],
      "act": "start"
    },
    {
      "t": "2026-10-23T14:10:51+03:00",
      "k": "ticket",
      "ids": [
        "04"
      ],
      "act": "review"
    },
    {
      "t": "2026-10-23T14:18:27+03:00",
      "k": "ticket",
      "ids": [
        "04"
      ],
      "act": "repair",
      "note": "PENDING-план, допущенный до daily stop или понижения режима, не переходит в EXECUTING"
    },
    {
      "t": "2026-10-23T14:18:28+03:00",
      "k": "add",
      "key": "concerns",
      "n": 10
    },
    {
      "t": "2026-10-23T14:19:35+03:00",
      "k": "tickets",
      "n": 8
    },
    {
      "t": "2026-10-23T14:19:36+03:00",
      "k": "gate",
      "g": "G3",
      "ok": true,
      "problems": 0
    },
    {
      "t": "2026-10-23T14:23:40+03:00",
      "k": "ticket",
      "ids": [
        "04"
      ],
      "act": "done",
      "tests": {
        "passed": 153,
        "failed": 0
      }
    },
    {
      "t": "2026-10-23T14:23:41+03:00",
      "k": "add",
      "key": "concerns",
      "n": 2
    },
    {
      "t": "2026-10-23T14:23:41+03:00",
      "k": "ticket",
      "ids": [
        "07"
      ],
      "act": "start"
    },
    {
      "t": "2026-10-23T15:25:09+03:00",
      "k": "ticket",
      "ids": [
        "07"
      ],
      "act": "repair",
      "note": "канонический check зелёный: worker в compose запускает python -m src.worker, образ содержит contracts/"
    },
    {
      "t": "2026-10-23T15:29:20+03:00",
      "k": "ticket",
      "ids": [
        "07"
      ],
      "act": "review"
    },
    {
      "t": "2026-10-23T15:38:46+03:00",
      "k": "ticket",
      "ids": [
        "07"
      ],
      "act": "repair",
      "note": "recovery не переводит в FAILED исполняющийся EXECUTING-план; fill не фиксируется для плана вне EXECUTING"
    },
    {
      "t": "2026-10-23T15:38:47+03:00",
      "k": "add",
      "key": "concerns",
      "n": 8
    },
    {
      "t": "2026-10-23T15:44:58+03:00",
      "k": "tickets",
      "n": 10
    },
    {
      "t": "2026-10-23T15:44:59+03:00",
      "k": "gate",
      "g": "G3",
      "ok": true,
      "problems": 0
    },
    {
      "t": "2026-10-23T15:55:27+03:00",
      "k": "ticket",
      "ids": [
        "07"
      ],
      "act": "done",
      "tests": {
        "passed": 250,
        "failed": 0
      }
    },
    {
      "t": "2026-10-23T15:55:27+03:00",
      "k": "add",
      "key": "concerns",
      "n": 3
    },
    {
      "t": "2026-10-23T15:55:28+03:00",
      "k": "ticket",
      "ids": [
        "09",
        "10"
      ],
      "act": "start"
    },
    {
      "t": "2026-10-23T16:27:21+03:00",
      "k": "tickets",
      "n": 11
    },
    {
      "t": "2026-10-23T16:27:22+03:00",
      "k": "gate",
      "g": "G3",
      "ok": true,
      "problems": 0
    },
    {
      "t": "2026-10-23T16:27:22+03:00",
      "k": "ticket",
      "ids": [
        "10"
      ],
      "act": "repair",
      "note": "total fees условный порог (D03): нет значения — флаг TOTAL_FEES_UNAVAILABLE, не UNKNOWN"
    },
    {
      "t": "2026-10-23T16:29:00+03:00",
      "k": "tickets",
      "n": 12
    },
    {
      "t": "2026-10-23T16:29:01+03:00",
      "k": "gate",
      "g": "G3",
      "ok": false,
      "problems": 6
    },
    {
      "t": "2026-10-23T16:29:19+03:00",
      "k": "tickets",
      "n": 12
    },
    {
      "t": "2026-10-23T16:29:19+03:00",
      "k": "gate",
      "g": "G3",
      "ok": true,
      "problems": 0
    },
    {
      "t": "2026-10-23T16:29:48+03:00",
      "k": "ticket",
      "ids": [
        "10"
      ],
      "act": "review"
    },
    {
      "t": "2026-10-23T16:40:03+03:00",
      "k": "ticket",
      "ids": [
        "10"
      ],
      "act": "repair",
      "note": "SPOT_ATH_BREAKOUT только при доказанном ATH (непрерывная история); volume_to_liquidity нормирован на 5 минут"
    },
    {
      "t": "2026-10-23T16:40:03+03:00",
      "k": "add",
      "key": "concerns",
      "n": 10
    },
    {
      "t": "2026-10-23T16:41:11+03:00",
      "k": "tickets",
      "n": 13
    },
    {
      "t": "2026-10-23T16:41:12+03:00",
      "k": "gate",
      "g": "G3",
      "ok": true,
      "problems": 0
    },
    {
      "t": "2026-10-23T16:44:40+03:00",
      "k": "ticket",
      "ids": [
        "09"
      ],
      "act": "review"
    },
    {
      "t": "2026-10-23T16:46:29+03:00",
      "k": "ticket",
      "ids": [
        "10"
      ],
      "act": "done",
      "tests": {
        "passed": 325,
        "failed": 0
      }
    },
    {
      "t": "2026-10-23T16:46:30+03:00",
      "k": "add",
      "key": "concerns",
      "n": 2
    },
    {
      "t": "2026-10-23T16:46:31+03:00",
      "k": "ticket",
      "ids": [
        "11"
      ],
      "act": "start"
    },
    {
      "t": "2026-10-23T16:56:37+03:00",
      "k": "ticket",
      "ids": [
        "09"
      ],
      "act": "repair",
      "note": "частично прошедший REMOVE (FAILED, несколько транзакций, есть подпись) останавливает автоповторы по позиции и поднимает critical alert"
    },
    {
      "t": "2026-10-23T16:56:38+03:00",
      "k": "add",
      "key": "concerns",
      "n": 8
    },
    {
      "t": "2026-10-23T17:04:29+03:00",
      "k": "tickets",
      "n": 13
    },
    {
      "t": "2026-10-23T17:04:29+03:00",
      "k": "gate",
      "g": "G3",
      "ok": true,
      "problems": 0
    },
    {
      "t": "2026-10-23T17:05:15+03:00",
      "k": "ticket",
      "ids": [
        "09"
      ],
      "act": "done",
      "tests": {
        "passed": 361,
        "failed": 0
      }
    },
    {
      "t": "2026-10-23T17:05:16+03:00",
      "k": "add",
      "key": "concerns",
      "n": 2
    },
    {
      "t": "2026-10-23T17:05:16+03:00",
      "k": "ticket",
      "ids": [
        "06",
        "13"
      ],
      "act": "start"
    },
    {
      "t": "2026-10-23T17:16:09+03:00",
      "k": "ticket",
      "ids": [
        "11"
      ],
      "act": "review"
    },
    {
      "t": "2026-10-23T17:18:19+03:00",
      "k": "tickets",
      "n": 14
    },
    {
      "t": "2026-10-23T17:18:19+03:00",
      "k": "gate",
      "g": "G3",
      "ok": true,
      "problems": 0
    },
    {
      "t": "2026-10-23T17:25:59+03:00",
      "k": "ticket",
      "ids": [
        "11"
      ],
      "act": "repair",
      "note": "обход с повтором пула на двух страницах или с пулами меньше total не публикуется как полный"
    },
    {
      "t": "2026-10-23T17:25:59+03:00",
      "k": "add",
      "key": "concerns",
      "n": 6
    },
    {
      "t": "2026-10-23T17:36:47+03:00",
      "k": "ticket",
      "ids": [
        "11"
      ],
      "act": "done",
      "tests": {
        "passed": 136,
        "failed": 0
      }
    },
    {
      "t": "2026-10-23T17:36:48+03:00",
      "k": "add",
      "key": "concerns",
      "n": 3
    },
    {
      "t": "2026-10-23T17:50:36+03:00",
      "k": "ticket",
      "ids": [
        "06"
      ],
      "act": "repair",
      "note": "BLOCKED вне зоны: точка входа src.api.main:app в Dockerfile и compose, переменные api и worker"
    },
    {
      "t": "2026-10-23T17:50:36+03:00",
      "k": "add",
      "key": "concerns",
      "n": 4
    },
    {
      "t": "2026-10-23T17:50:37+03:00",
      "k": "tickets",
      "n": 14
    },
    {
      "t": "2026-10-23T17:50:38+03:00",
      "k": "gate",
      "g": "G3",
      "ok": true,
      "problems": 0
    },
    {
      "t": "2026-10-23T17:53:41+03:00",
      "k": "ticket",
      "ids": [
        "13"
      ],
      "act": "repair",
      "note": "BLOCKED вне зоны: execution_store читает V2, recovery подтверждает по record.position_address, test_pipeline на V2"
    },
    {
      "t": "2026-10-23T17:53:41+03:00",
      "k": "add",
      "key": "debt.placeholders",
      "n": 1
    },
    {
      "t": "2026-10-23T17:53:42+03:00",
      "k": "add",
      "key": "concerns",
      "n": 3
    },
    {
      "t": "2026-10-23T17:53:43+03:00",
      "k": "tickets",
      "n": 14
    },
    {
      "t": "2026-10-23T17:53:43+03:00",
      "k": "gate",
      "g": "G3",
      "ok": true,
      "problems": 0
    },
    {
      "t": "2026-10-23T17:55:32+03:00",
      "k": "ticket",
      "ids": [
        "06"
      ],
      "act": "review"
    },
    {
      "t": "2026-10-23T17:59:41+03:00",
      "k": "ticket",
      "ids": [
        "13"
      ],
      "act": "review"
    },
    {
      "t": "2026-10-23T18:02:12+03:00",
      "k": "ticket",
      "ids": [
        "06"
      ],
      "act": "repair",
      "note": "APP_OPERATOR_TOKEN(_FILE) и APP_SESSION_SECRET(_FILE) доходят до api в штатном compose; вход, kill switch и понижение работают без override"
    },
    {
      "t": "2026-10-23T18:02:12+03:00",
      "k": "add",
      "key": "concerns",
      "n": 9
    },
    {
      "t": "2026-10-23T18:11:13+03:00",
      "k": "ticket",
      "ids": [
        "13"
      ],
      "act": "repair",
      "note": "settler берёт адрес позиции из receipt (record.position_address); код отказа executor до оператора; выход не блокируется impact несостоявшегося обмена (D08)"
    },
    {
      "t": "2026-10-23T18:11:13+03:00",
      "k": "add",
      "key": "concerns",
      "n": 6
    },
    {
      "t": "2026-10-23T18:11:14+03:00",
      "k": "tickets",
      "n": 14
    },
    {
      "t": "2026-10-23T18:11:14+03:00",
      "k": "gate",
      "g": "G3",
      "ok": true,
      "problems": 0
    },
    {
      "t": "2026-10-23T18:12:51+03:00",
      "k": "ticket",
      "ids": [
        "06"
      ],
      "act": "done",
      "tests": {
        "passed": 454,
        "failed": 0
      }
    },
    {
      "t": "2026-10-23T18:12:52+03:00",
      "k": "add",
      "key": "concerns",
      "n": 3
    },
    {
      "t": "2026-10-23T18:25:57+03:00",
      "k": "ticket",
      "ids": [
        "13"
      ],
      "act": "done",
      "tests": {
        "passed": 460,
        "failed": 0
      }
    },
    {
      "t": "2026-10-23T18:25:58+03:00",
      "k": "add",
      "key": "concerns",
      "n": 3
    },
    {
      "t": "2026-10-23T18:25:58+03:00",
      "k": "tickets",
      "n": 15
    },
    {
      "t": "2026-10-23T18:25:59+03:00",
      "k": "gate",
      "g": "G3",
      "ok": true,
      "problems": 0
    },
    {
      "t": "2026-10-23T18:25:59+03:00",
      "k": "ticket",
      "ids": [
        "14",
        "15"
      ],
      "act": "start"
    },
    {
      "t": "2026-10-23T18:26:42+03:00",
      "k": "gate",
      "g": "G3",
      "ok": true,
      "problems": 0
    },
    {
      "t": "2026-10-23T18:34:17+03:00",
      "k": "ticket",
      "ids": [
        "15"
      ],
      "act": "review"
    },
    {
      "t": "2026-10-23T18:39:50+03:00",
      "k": "ticket",
      "ids": [
        "15"
      ],
      "act": "repair",
      "note": "выход APPROVAL-позиции одобряем при kill switch ORDERLY_EXIT и после дневного стопа; вход блокируется как раньше"
    },
    {
      "t": "2026-10-23T18:39:50+03:00",
      "k": "add",
      "key": "concerns",
      "n": 5
    },
    {
      "t": "2026-10-23T18:49:15+03:00",
      "k": "ticket",
      "ids": [
        "15"
      ],
      "act": "done",
      "tests": {
        "passed": 52,
        "failed": 0
      }
    },
    {
      "t": "2026-10-23T18:49:16+03:00",
      "k": "add",
      "key": "concerns",
      "n": 3
    },
    {
      "t": "2026-10-23T18:49:16+03:00",
      "k": "add",
      "key": "report",
      "n": 1
    },
    {
      "t": "2026-10-23T19:24:27+03:00",
      "k": "ticket",
      "ids": [
        "14"
      ],
      "act": "review"
    },
    {
      "t": "2026-10-23T19:37:55+03:00",
      "k": "ticket",
      "ids": [
        "14"
      ],
      "act": "repair",
      "note": "дефолтный Bid-Ask без бинов выше active (вход только в quote-токене); после NEGATIVE_DELTA прирост не засчитывается как объём"
    },
    {
      "t": "2026-10-23T19:37:55+03:00",
      "k": "add",
      "key": "concerns",
      "n": 9
    },
    {
      "t": "2026-10-23T19:52:05+03:00",
      "k": "ticket",
      "ids": [
        "14"
      ],
      "act": "repair",
      "note": "BID_ONLY: цена выше верха не мгновенный RANGE_BREACH; интервал прироста от метки; выход из вечного UNKNOWN после сброса счётчика"
    },
    {
      "t": "2026-10-23T20:09:31+03:00",
      "k": "ticket",
      "ids": [
        "14"
      ],
      "act": "done",
      "tests": {
        "passed": 569,
        "failed": 0
      }
    },
    {
      "t": "2026-10-23T20:09:32+03:00",
      "k": "add",
      "key": "concerns",
      "n": 3
    },
    {
      "t": "2026-10-23T20:09:32+03:00",
      "k": "ticket",
      "ids": [
        "12"
      ],
      "act": "start"
    },
    {
      "t": "2026-10-23T20:53:08+03:00",
      "k": "ticket",
      "ids": [
        "12"
      ],
      "act": "review"
    },
    {
      "t": "2026-10-23T21:03:17+03:00",
      "k": "tickets",
      "n": 15
    },
    {
      "t": "2026-10-23T21:03:17+03:00",
      "k": "gate",
      "g": "G3",
      "ok": true,
      "problems": 0
    },
    {
      "t": "2026-10-23T21:03:18+03:00",
      "k": "ticket",
      "ids": [
        "12"
      ],
      "act": "repair",
      "note": "GMGN_UNAVAILABLE и без подключённого сервиса (F1); D09: без enhanced и без RPC кандидаты в SHADOW не отклоняются"
    },
    {
      "t": "2026-10-23T21:03:18+03:00",
      "k": "add",
      "key": "concerns",
      "n": 7
    },
    {
      "t": "2026-10-23T21:18:21+03:00",
      "k": "ticket",
      "ids": [
        "12"
      ],
      "act": "repair",
      "note": "решение с TOKEN_RISK_UNAVAILABLE не считается покрытым данными для §7.1 (pipeline._covered)"
    },
    {
      "t": "2026-10-23T21:18:22+03:00",
      "k": "add",
      "key": "concerns",
      "n": 2
    },
    {
      "t": "2026-10-23T21:18:22+03:00",
      "k": "add",
      "key": "report",
      "n": 1
    },
    {
      "t": "2026-10-23T21:24:23+03:00",
      "k": "ticket",
      "ids": [
        "12"
      ],
      "act": "done",
      "tests": {
        "passed": 627,
        "failed": 0
      }
    },
    {
      "t": "2026-10-23T21:24:24+03:00",
      "k": "add",
      "key": "concerns",
      "n": 2
    },
    {
      "t": "2026-10-23T21:25:03+03:00",
      "k": "ticket",
      "ids": [
        "08"
      ],
      "act": "start"
    },
    {
      "t": "2026-10-24T08:59:01+02:00",
      "k": "ticket",
      "ids": [
        "08"
      ],
      "act": "reset"
    },
    {
      "t": "2026-10-24T10:00:46+02:00",
      "k": "ticket",
      "ids": [
        "08"
      ],
      "act": "start"
    },
    {
      "t": "2026-10-24T10:06:49+02:00",
      "k": "ask",
      "kind": "action",
      "note": "GMGN: официальное OpenAPI вместо реверса — ждём выбор"
    },
    {
      "t": "2026-10-24T10:09:10+02:00",
      "k": "answered",
      "kind": "action"
    },
    {
      "t": "2026-10-24T10:09:10+02:00",
      "k": "tickets",
      "n": 16
    },
    {
      "t": "2026-10-24T10:09:13+02:00",
      "k": "gate",
      "g": "G3",
      "ok": true,
      "problems": 0
    },
    {
      "t": "2026-10-24T10:09:14+02:00",
      "k": "ticket",
      "ids": [
        "16"
      ],
      "act": "start"
    },
    {
      "t": "2026-10-24T10:36:43+02:00",
      "k": "ticket",
      "ids": [
        "16"
      ],
      "act": "review"
    },
    {
      "t": "2026-10-24T10:36:44+02:00",
      "k": "add",
      "key": "concerns",
      "n": 4
    },
    {
      "t": "2026-10-24T10:43:57+02:00",
      "k": "ticket",
      "ids": [
        "16"
      ],
      "act": "done",
      "tests": {
        "passed": 670,
        "failed": 0
      }
    },
    {
      "t": "2026-10-24T10:43:58+02:00",
      "k": "add",
      "key": "concerns",
      "n": 2
    },
    {
      "t": "2026-10-24T11:02:37+02:00",
      "k": "add",
      "key": "concerns",
      "n": 5
    },
    {
      "t": "2026-10-24T11:02:38+02:00",
      "k": "add",
      "key": "debt.placeholders",
      "n": 1
    },
    {
      "t": "2026-10-24T11:04:43+02:00",
      "k": "ticket",
      "ids": [
        "08"
      ],
      "act": "done",
      "tests": {
        "passed": 670,
        "failed": 0
      }
    },
    {
      "t": "2026-10-24T11:04:44+02:00",
      "k": "stage",
      "id": "review",
      "act": "start"
    },
    {
      "t": "2026-10-24T11:18:05+02:00",
      "k": "add",
      "key": "report",
      "n": 10
    },
    {
      "t": "2026-10-24T11:21:18+02:00",
      "k": "add",
      "key": "report",
      "n": 11
    },
    {
      "t": "2026-10-24T11:23:29+02:00",
      "k": "add",
      "key": "report",
      "n": 9
    },
    {
      "t": "2026-10-24T11:23:30+02:00",
      "k": "tickets",
      "n": 19
    },
    {
      "t": "2026-10-24T11:23:30+02:00",
      "k": "gate",
      "g": "G3",
      "ok": false,
      "problems": 2
    },
    {
      "t": "2026-10-24T11:24:02+02:00",
      "k": "tickets",
      "n": 19
    },
    {
      "t": "2026-10-24T11:24:02+02:00",
      "k": "gate",
      "g": "G3",
      "ok": false,
      "problems": 1
    },
    {
      "t": "2026-10-24T11:24:03+02:00",
      "k": "ticket",
      "ids": [
        "F1",
        "F2"
      ],
      "act": "start"
    },
    {
      "t": "2026-10-24T11:57:08+02:00",
      "k": "tickets",
      "n": 19
    },
    {
      "t": "2026-10-24T11:57:09+02:00",
      "k": "ticket",
      "ids": [
        "F1"
      ],
      "act": "repair",
      "note": "зона расширена: src/providers/meteora.py читает token_y.price → PoolSnapshot.quote_price_usd (A3)"
    },
    {
      "t": "2026-10-24T11:57:10+02:00",
      "k": "add",
      "key": "concerns",
      "n": 1
    },
    {
      "t": "2026-10-24T12:02:09+02:00",
      "k": "tickets",
      "n": 19
    },
    {
      "t": "2026-10-24T12:02:10+02:00",
      "k": "ticket",
      "ids": [
        "F2"
      ],
      "act": "repair",
      "note": "зона расширена: политика kill switch в PostgreSQL — миграция 0003, ModeStateRecord, PostgresModeRepository (B3/R27i)"
    },
    {
      "t": "2026-10-24T12:02:11+02:00",
      "k": "add",
      "key": "concerns",
      "n": 3
    },
    {
      "t": "2026-10-24T12:02:31+02:00",
      "k": "tickets",
      "n": 19
    },
    {
      "t": "2026-10-24T12:02:31+02:00",
      "k": "gate",
      "g": "G3",
      "ok": false,
      "problems": 1
    },
    {
      "t": "2026-10-24T12:02:57+02:00",
      "k": "ticket",
      "ids": [
        "F1"
      ],
      "act": "review"
    },
    {
      "t": "2026-10-24T12:02:58+02:00",
      "k": "add",
      "key": "concerns",
      "n": 2
    },
    {
      "t": "2026-10-24T12:07:47+02:00",
      "k": "ticket",
      "ids": [
        "F2"
      ],
      "act": "review"
    },
    {
      "t": "2026-10-24T12:10:19+02:00",
      "k": "ticket",
      "ids": [
        "F1"
      ],
      "act": "done",
      "tests": {
        "passed": 730,
        "failed": 0
      }
    },
    {
      "t": "2026-10-24T12:10:20+02:00",
      "k": "add",
      "key": "report",
      "n": 1
    },
    {
      "t": "2026-10-24T12:16:07+02:00",
      "k": "ticket",
      "ids": [
        "F2"
      ],
      "act": "repair",
      "note": "SystemAlertResolver закрывает ещё актуальный EXIT_GATEWAY_UNAVAILABLE/POSITION_ACTION_FAILED на каждом цикле после подтверждённого REMOVE"
    },
    {
      "t": "2026-10-24T12:16:08+02:00",
      "k": "add",
      "key": "concerns",
      "n": 1
    },
    {
      "t": "2026-10-24T12:20:21+02:00",
      "k": "ticket",
      "ids": [
        "F2"
      ],
      "act": "review"
    },
    {
      "t": "2026-10-24T12:23:50+02:00",
      "k": "ticket",
      "ids": [
        "F2"
      ],
      "act": "done",
      "tests": {
        "passed": 732,
        "failed": 0
      }
    },
    {
      "t": "2026-10-24T12:23:51+02:00",
      "k": "add",
      "key": "concerns",
      "n": 1
    },
    {
      "t": "2026-10-24T12:23:52+02:00",
      "k": "ticket",
      "ids": [
        "F3"
      ],
      "act": "start"
    },
    {
      "t": "2026-10-24T13:20:42+02:00",
      "k": "tickets",
      "n": 20
    },
    {
      "t": "2026-10-24T13:20:43+02:00",
      "k": "gate",
      "g": "G3",
      "ok": false,
      "problems": 1
    },
    {
      "t": "2026-10-24T13:34:52+02:00",
      "k": "tickets",
      "n": 20
    },
    {
      "t": "2026-10-24T13:34:53+02:00",
      "k": "ticket",
      "ids": [
        "F3"
      ],
      "act": "repair",
      "note": "acceptance smoke 3/6 после F1 (нет quote_price_usd в фикстурах, шаг 61 с не даёт окна 4m30); убрать параметр kill_switch_policy"
    },
    {
      "t": "2026-10-24T13:34:53+02:00",
      "k": "add",
      "key": "concerns",
      "n": 2
    },
    {
      "t": "2026-10-24T13:35:11+02:00",
      "k": "tickets",
      "n": 20
    },
    {
      "t": "2026-10-24T13:45:55+02:00",
      "k": "tickets",
      "n": 23
    },
    {
      "t": "2026-10-24T13:45:56+02:00",
      "k": "gate",
      "g": "G3",
      "ok": false,
      "problems": 1
    },
    {
      "t": "2026-10-24T13:50:19+02:00",
      "k": "ticket",
      "ids": [
        "F3"
      ],
      "act": "review"
    },
    {
      "t": "2026-10-24T14:00:25+02:00",
      "k": "ticket",
      "ids": [
        "F3"
      ],
      "act": "done",
      "tests": {
        "passed": 941,
        "failed": 0
      }
    },
    {
      "t": "2026-10-24T14:00:26+02:00",
      "k": "add",
      "key": "report",
      "n": 2
    },
    {
      "t": "2026-10-24T14:00:27+02:00",
      "k": "ticket",
      "ids": [
        "17",
        "18"
      ],
      "act": "start"
    },
    {
      "t": "2026-10-24T14:45:57+02:00",
      "k": "tickets",
      "n": 23
    },
    {
      "t": "2026-10-24T14:45:58+02:00",
      "k": "ticket",
      "ids": [
        "18"
      ],
      "act": "repair",
      "note": "test_f3_labels: 10 новых кодов T18 без русских подписей; устаревшие _TRIGGERS/_SPIKE_CODE в presenters"
    },
    {
      "t": "2026-10-24T14:53:27+02:00",
      "k": "ticket",
      "ids": [
        "18"
      ],
      "act": "review"
    },
    {
      "t": "2026-10-24T14:53:27+02:00",
      "k": "add",
      "key": "concerns",
      "n": 3
    },
    {
      "t": "2026-10-24T14:58:21+02:00",
      "k": "ticket",
      "ids": [
        "17"
      ],
      "act": "review"
    },
    {
      "t": "2026-10-24T14:58:22+02:00",
      "k": "add",
      "key": "concerns",
      "n": 3
    },
    {
      "t": "2026-10-24T15:03:03+02:00",
      "k": "ticket",
      "ids": [
        "18"
      ],
      "act": "repair",
      "note": "LIVE: неизвестная доля девелопера и тема UNKNOWN не закрывают вход"
    },
    {
      "t": "2026-10-24T15:03:03+02:00",
      "k": "add",
      "key": "concerns",
      "n": 4
    },
    {
      "t": "2026-10-24T15:08:15+02:00",
      "k": "ticket",
      "ids": [
        "18"
      ],
      "act": "review"
    },
    {
      "t": "2026-10-24T15:09:58+02:00",
      "k": "ticket",
      "ids": [
        "17"
      ],
      "act": "repair",
      "note": "подтверждённый вход берёт network/priority fee из cost model, а не из settlement receipt"
    },
    {
      "t": "2026-10-24T15:09:59+02:00",
      "k": "add",
      "key": "concerns",
      "n": 7
    },
    {
      "t": "2026-10-24T15:11:24+02:00",
      "k": "ticket",
      "ids": [
        "18"
      ],
      "act": "done",
      "tests": {
        "passed": 1061,
        "failed": 0
      }
    },
    {
      "t": "2026-10-24T15:21:07+02:00",
      "k": "ticket",
      "ids": [
        "17"
      ],
      "act": "review"
    },
    {
      "t": "2026-10-24T15:24:27+02:00",
      "k": "ticket",
      "ids": [
        "17"
      ],
      "act": "done",
      "tests": {
        "passed": 1062,
        "failed": 0
      }
    },
    {
      "t": "2026-10-24T15:24:28+02:00",
      "k": "tickets",
      "n": 23
    },
    {
      "t": "2026-10-24T15:24:29+02:00",
      "k": "ticket",
      "ids": [
        "19"
      ],
      "act": "start"
    },
    {
      "t": "2026-10-24T16:09:52+02:00",
      "k": "ticket",
      "ids": [
        "19"
      ],
      "act": "review"
    },
    {
      "t": "2026-10-24T16:09:52+02:00",
      "k": "add",
      "key": "concerns",
      "n": 4
    },
    {
      "t": "2026-10-24T16:24:07+02:00",
      "k": "ticket",
      "ids": [
        "19"
      ],
      "act": "repair",
      "note": "контекст не-SHADOW позиции от цены снимка, а не от активного бина команды; окно 5m перечитываний обнуляется после свипа"
    },
    {
      "t": "2026-10-24T16:24:07+02:00",
      "k": "add",
      "key": "concerns",
      "n": 4
    },
    {
      "t": "2026-10-24T16:30:51+02:00",
      "k": "ticket",
      "ids": [
        "19"
      ],
      "act": "review"
    },
    {
      "t": "2026-10-24T16:30:52+02:00",
      "k": "add",
      "key": "concerns",
      "n": 1
    },
    {
      "t": "2026-10-24T16:36:44+02:00",
      "k": "ticket",
      "ids": [
        "19"
      ],
      "act": "done",
      "tests": {
        "passed": 1123,
        "failed": 0
      }
    },
    {
      "t": "2026-10-24T16:37:24+02:00",
      "k": "tickets",
      "n": 25
    },
    {
      "t": "2026-10-24T16:37:25+02:00",
      "k": "gate",
      "g": "G3",
      "ok": false,
      "problems": 1
    },
    {
      "t": "2026-10-24T16:37:25+02:00",
      "k": "ticket",
      "ids": [
        "F4",
        "F5"
      ],
      "act": "start"
    },
    {
      "t": "2026-10-24T17:11:25+02:00",
      "k": "ticket",
      "ids": [
        "F5"
      ],
      "act": "review"
    },
    {
      "t": "2026-10-24T17:11:26+02:00",
      "k": "add",
      "key": "concerns",
      "n": 2
    },
    {
      "t": "2026-10-24T17:17:37+02:00",
      "k": "ticket",
      "ids": [
        "F5"
      ],
      "act": "done",
      "tests": {
        "passed": 1200,
        "failed": 0
      }
    },
    {
      "t": "2026-10-24T17:17:37+02:00",
      "k": "add",
      "key": "concerns",
      "n": 1
    },
    {
      "t": "2026-10-24T17:21:12+02:00",
      "k": "ticket",
      "ids": [
        "F4"
      ],
      "act": "review"
    },
    {
      "t": "2026-10-24T17:21:13+02:00",
      "k": "add",
      "key": "concerns",
      "n": 3
    },
    {
      "t": "2026-10-24T17:27:33+02:00",
      "k": "ticket",
      "ids": [
        "F4"
      ],
      "act": "repair",
      "note": "повторная недоступность шлюза после проданной части не поднимает алерт (дедуп остаётся после системного закрытия)"
    },
    {
      "t": "2026-10-24T17:27:33+02:00",
      "k": "add",
      "key": "concerns",
      "n": 1
    },
    {
      "t": "2026-10-24T17:32:10+02:00",
      "k": "ticket",
      "ids": [
        "F4"
      ],
      "act": "review"
    },
    {
      "t": "2026-10-24T17:32:11+02:00",
      "k": "add",
      "key": "concerns",
      "n": 1
    },
    {
      "t": "2026-10-24T17:35:24+02:00",
      "k": "ticket",
      "ids": [
        "F4"
      ],
      "act": "done",
      "tests": {
        "passed": 1201,
        "failed": 0
      }
    },
    {
      "t": "2026-10-24T17:35:24+02:00",
      "k": "ticket",
      "ids": [
        "20"
      ],
      "act": "start"
    },
    {
      "t": "2026-10-24T17:45:57+02:00",
      "k": "ticket",
      "ids": [
        "20"
      ],
      "act": "reset"
    },
    {
      "t": "2026-10-24T17:46:23+02:00",
      "k": "ticket",
      "ids": [
        "20"
      ],
      "act": "start"
    },
    {
      "t": "2026-10-24T18:17:23+02:00",
      "k": "add",
      "key": "concerns",
      "n": 3
    },
    {
      "t": "2026-10-24T18:19:01+02:00",
      "k": "ticket",
      "ids": [
        "20"
      ],
      "act": "done",
      "tests": {
        "passed": 1284,
        "failed": 0
      }
    },
    {
      "t": "2026-10-24T18:19:08+02:00",
      "k": "stage",
      "id": "final",
      "act": "start"
    },
    {
      "t": "2026-10-24T18:47:43+02:00",
      "k": "gate",
      "g": "G4",
      "ok": false,
      "checked": 17,
      "matched": 11
    },
    {
      "t": "2026-10-24T18:47:44+02:00",
      "k": "tickets",
      "n": 26
    },
    {
      "t": "2026-10-24T18:47:44+02:00",
      "k": "ticket",
      "ids": [
        "F6"
      ],
      "act": "start"
    },
    {
      "t": "2026-10-24T19:04:56+02:00",
      "k": "ticket",
      "ids": [
        "F6"
      ],
      "act": "done",
      "tests": {
        "passed": 1286,
        "failed": 0
      }
    },
    {
      "t": "2026-10-24T19:04:57+02:00",
      "k": "tests",
      "passed": 1286,
      "failed": 0
    },
    {
      "t": "2026-10-24T19:05:04+02:00",
      "k": "add",
      "key": "report",
      "n": 2
    },
    {
      "t": "2026-10-24T19:42:46+02:00",
      "k": "tickets",
      "n": 27
    },
    {
      "t": "2026-10-24T19:42:46+02:00",
      "k": "ticket",
      "ids": [
        "21"
      ],
      "act": "start"
    },
    {
      "t": "2026-10-24T20:19:33+02:00",
      "k": "add",
      "key": "report",
      "n": 2
    },
    {
      "t": "2026-10-24T20:36:28+02:00",
      "k": "ticket",
      "ids": [
        "21"
      ],
      "act": "review"
    },
    {
      "t": "2026-10-24T20:43:40+02:00",
      "k": "ticket",
      "ids": [
        "21"
      ],
      "act": "repair",
      "note": "пауза после 429/403/challenge соблюдается и для загрузок из очереди; вход в документации — по желанию пользователя с предупреждением о риске"
    },
    {
      "t": "2026-10-24T20:43:40+02:00",
      "k": "add",
      "key": "concerns",
      "n": 5
    },
    {
      "t": "2026-10-24T20:49:00+02:00",
      "k": "ticket",
      "ids": [
        "21"
      ],
      "act": "done",
      "tests": {
        "passed": 1335,
        "failed": 0
      }
    },
    {
      "t": "2026-10-24T20:49:01+02:00",
      "k": "tests",
      "passed": 1335,
      "failed": 0
    },
    {
      "t": "2026-10-24T20:50:23+02:00",
      "k": "add",
      "key": "report",
      "n": 2
    },
    {
      "t": "2026-10-24T20:50:34+02:00",
      "k": "finish",
      "result": "Бот ликвидности для DLMM-пулов Meteora по гайду LexDollar: SHADOW → подтверждение → LIVE, панель FastAPI в Docker, продажа монеты после выхода, метрики GMGN через браузер-посредник"
    }
  ],
  "reqList": [
    {
      "id": "R01",
      "text": "«https://teletype.in/@lexdollar/meteora3nax можешь прочитать»",
      "status": "done"
    },
    {
      "id": "R02",
      "text": "«можешь подробно очень изучить гайд»",
      "status": "done"
    },
    {
      "id": "R03",
      "text": "«https://www.youtube.com/watch?v=Pk0VGbWakr8»",
      "status": "done"
    },
    {
      "id": "R04",
      "text": "«https://www.youtube.com/watch?v=cNYYFKHzBqk»",
      "status": "done"
    },
    {
      "id": "R05",
      "text": "«и эти 2 видео тоже подробно»",
      "status": "done"
    },
    {
      "id": "R06",
      "text": "«в результате глубочайшего исследование»",
      "status": "done"
    },
    {
      "id": "R07",
      "text": "«напиши все макрдовн файл»",
      "status": "done"
    },
    {
      "id": "R08",
      "text": "«сделай папку на рабочем сстоле»",
      "status": "done"
    },
    {
      "id": "R09",
      "text": "«прод навзание метора»",
      "status": "done"
    },
    {
      "id": "R10",
      "text": "«сохрани туда файл»",
      "status": "done"
    },
    {
      "id": "R11",
      "text": "«ты создал папку метора»",
      "status": "done"
    },
    {
      "id": "R12",
      "text": "«закинул туда маркдовн»",
      "status": "done"
    },
    {
      "id": "R13",
      "text": "«только на ангилсиком языке»",
      "status": "done"
    },
    {
      "id": "R14",
      "text": "«папака на англиском навзание метора всм»",
      "status": "done"
    },
    {
      "id": "R15",
      "text": "«можем ли мы вот этого сделать автоматизированно?»",
      "status": "done"
    },
    {
      "id": "R16",
      "text": "«условно торговлю»",
      "status": "done"
    },
    {
      "id": "R17",
      "text": "«надо отслеживать там всякие объёмы и так далее»",
      "status": "done"
    },
    {
      "id": "R18",
      "text": "«всё автоматизировать»",
      "status": "done"
    },
    {
      "id": "R19",
      "text": "«чтобы зарабатывать на всяких всплесках объёмов»",
      "status": "done"
    },
    {
      "id": "R20",
      "text": "«по гайду же будем делать ?»",
      "status": "done"
    },
    {
      "id": "R21",
      "text": "«погнали»",
      "status": "done"
    },
    {
      "id": "R22",
      "text": "«все делай фундаменталько»",
      "status": "done"
    },
    {
      "id": "R23",
      "text": "«как сеньор»",
      "status": "done"
    },
    {
      "id": "R24",
      "text": "«весь коды»",
      "status": "done"
    },
    {
      "id": "R25",
      "text": "«в этой папке C:\\Users\\Maks\\Desktop\\Meteora должны быть»",
      "status": "done"
    },
    {
      "id": "R26i",
      "text": "(подразумевается) автоматическая торговля не должна начинаться реальными деньгами без безопасного режима проверки",
      "status": "done"
    },
    {
      "id": "R27i",
      "text": "(подразумевается) система должна ограничивать убыток и уметь аварийно остановиться",
      "status": "done"
    },
    {
      "id": "R28i",
      "text": "(подразумевается) приватный ключ кошелька и API-ключи не должны попадать в код или историю Git",
      "status": "done"
    },
    {
      "id": "R29i",
      "text": "(подразумевается) решения об открытии и закрытии позиции должны быть объяснимы и записаны",
      "status": "done"
    },
    {
      "id": "R30i",
      "text": "(подразумевается) после перезапуска бот не должен терять позиции и повторять опасные действия",
      "status": "done"
    },
    {
      "id": "R31i",
      "text": "(подразумевается) реализация должна иметь автоматические проверки ключевой логики",
      "status": "done"
    },
    {
      "id": "R32i",
      "text": "(подразумевается) проект должен содержать понятную инструкцию запуска и настройки",
      "status": "done"
    },
    {
      "id": "G01",
      "text": "«сначала 7–14 дней shadow/paper без денег»",
      "status": "done"
    },
    {
      "id": "G02",
      "text": "«затем сделки с ручным подтверждением»",
      "status": "done"
    },
    {
      "id": "G03",
      "text": "«после успешной проверки — автоматический live-режим»",
      "status": "done"
    },
    {
      "id": "G04",
      "text": "«Все DLMM-пулы с жёсткими фильтрами»",
      "status": "done"
    },
    {
      "id": "G05",
      "text": "«исключает низкую ликвидность»",
      "status": "done"
    },
    {
      "id": "G06",
      "text": "«исключает подозрительные токены»",
      "status": "done"
    },
    {
      "id": "G07",
      "text": "«исключает […] искусственный объём»",
      "status": "done"
    },
    {
      "id": "G08",
      "text": "«исключает […] слишком молодые и опасные пулы»",
      "status": "done"
    },
    {
      "id": "G09",
      "text": "«до 10% капитала в позиции»",
      "status": "done"
    },
    {
      "id": "G10",
      "text": "«риск до 1%»",
      "status": "done"
    },
    {
      "id": "G11",
      "text": "«максимум три позиции»",
      "status": "done"
    },
    {
      "id": "G12",
      "text": "«дневной стоп — 4%»",
      "status": "done"
    },
    {
      "id": "G13",
      "text": "«аппку сделать типо на фастапи»",
      "status": "done"
    },
    {
      "id": "G14",
      "text": "«будет куртиться в докере»",
      "status": "done"
    },
    {
      "id": "G15",
      "text": "«будем ли мы исправлять то что ты нашел что мы не продумали в боте отностильно видосов и статьи» — стратегия",
      "status": "done"
    },
    {
      "id": "G16",
      "text": "там же — пороги",
      "status": "done"
    },
    {
      "id": "G17",
      "text": "там же — ручной чеклист",
      "status": "done"
    },
    {
      "id": "G18",
      "text": "«Метрики GMGN (insiders, bundling, phishing сделаем реверс» + «По GMGN жду твой выбор: A (браузер)»",
      "status": "done"
    },
    {
      "id": "G20",
      "text": "«давай с тобой сделаем реверс с помощью мода» + на выбор способа: «Официальное API»",
      "status": "done"
    },
    {
      "id": "G22",
      "text": "«Бот не меняет токены может сделать чтобы менял как дуаешь ?» + «В этой сборке»; автопокупка для Spot — «Убрать»",
      "status": "done"
    },
    {
      "id": "G23",
      "text": "«мы все врено сделали отнисительно послежго гайда и двух видосов» + «Всё сейчас»",
      "status": "done"
    },
    {
      "id": "G24",
      "text": "«Как у автора» (отбор пулов)",
      "status": "done"
    },
    {
      "id": "G25",
      "text": "«сделай реверс» (после условия GMGN «Free API Key requires ≥$100 balance» — «но я не хочу класть деньги»)",
      "status": "done"
    },
    {
      "id": "G21",
      "text": "«можешь создать там ветку и запушить в конце ?» + «Новый приватный»",
      "status": "in-spec"
    },
    {
      "id": "G19",
      "text": "«можно будет какую ниюудь ии подклчюить типо кмим или квен которя будет соцести мониторить»",
      "status": "deferred",
      "why": "предложено отдельной второй сборкой после сдачи этой; нужны решения пользователя: какие соцсети, модель в облаке или локально; принцип — ИИ может только запретить вход или вызвать выход"
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
      "ok": false,
      "at": "2026-10-24T16:37:25+02:00",
      "problems": 1
    },
    "G4": {
      "ok": false,
      "at": "2026-10-24T18:47:43+02:00",
      "checked": 17,
      "matched": 11
    }
  },
  "result": "Бот ликвидности для DLMM-пулов Meteora по гайду LexDollar: SHADOW → подтверждение → LIVE, панель FastAPI в Docker, продажа монеты после выхода, метрики GMGN через браузер-посредник",
  "report": [
    "Если оператор сам понизил режим до симуляции, выход позиции, открытой в режиме подтверждения, панель не одобряет; спецификация этот случай не регулирует — стоит решить, как должно быть.",
    "Метрики GMGN: сервис построен по спецификации, но gmgn.ai через Cloudflare блокирует автоматический браузер (HTTP 403 «you have been blocked»); обхода нет, бот работает с пометкой «GMGN недоступен», автоматический live закрыт до GMGN_REQUIRED_FOR_LIVE=false.",
    "Проверки безопасности токена (права mint, держатели, возраст, капитализация, GMGN) делаются только для первой монеты пула; пул, где рискованная монета стоит второй, проверяется по безопасной — в live такой вход обычно не проходит проверку кошелька, но в статистику симуляции попадает.",
    "Если у открытой позиции неизвестен объём за 5 минут, бот не выходит по падению объёма и не предупреждает — Spot на сверхобъёме держится до 30 минут.",
    "Кратковременный сбой RPC окончательно отклоняет кандидата даже в симуляции (вместо «неизвестно»), а уже полученные данные GMGN при этом теряются.",
    "Пустой ответ Meteora API принимается как пустой рынок, а здоровье источника остаётся «в норме».",
    "Время котировки глубины не сверяется с моментом решения: решение может опираться на данные новее себя.",
    "Хранилища пулов DAMM v2 и Raydium CPMM не исключаются из top-10 держателей — концентрация завышается, и часть токенов после Meteora DBC/DAMM v2 ложно отклоняется.",
    "Дата создания пула не проверяется на правдоподобие: заведомо старая дата из API может пропустить порог возраста токена «≥ 2 ч».",
    "Исторический максимум (ATH) пула строится по 5-минутным снимкам — пик запуска может быть пропущен, и «пробитие ATH» для Spot окажется ложным.",
    "В записи решения возраст токена не помечен как точный или как нижняя оценка.",
    "В коде остались неиспользуемые вторые реализации признаков и фильтров, расходящиеся с рабочими (src/market/features.py, quality.py).",
    "Капитал и P&L считаются в единицах quote-токена каждого пула (SOL-пулы в лампортах, USDC — в микро-USDC) и складываются вместе, а база лимитов 10%/1%/4% в LIVE — бумажная SHADOW_EQUITY_BASE_UNITS, а не кошелёк: в live лимиты не соответствуют реальным деньгам.",
    "Живые сделки записываются по цене снимка и модельным издержкам, а не по суммам из чека транзакции — сверить учёт с блокчейном нельзя.",
    "Переход в LIVE доверяет файлу LIVE_PROMOTION_FILE, который заполняет оператор: 10 циклов подтверждения и сверку чеков система сама не считает.",
    "В режиме подтверждения одобренный вход исполняет только следующий цикл (раз в 60 с), а симуляция живёт 2 мин: одобрение позже ~60 с после симуляции тихо отменяет вход, без предупреждения.",
    "Дневной стоп оценивает позицию по последнему снимку любой давности, позицию без снимка не учитывает, а капитал начала дня берёт со снимка начала прошлого дня — стоп 4% может сработать позже, чем должен.",
    "Сбой записи учёта после подтверждённого выхода прерывает обработку остальных позиций в этом цикле без предупреждения; когда база оживает, всё догоняется.",
    "У оператора нет ручного пути: неизвестный исход без подписи, частично прошедший выход или 5 неудачных попыток выхода в live держат слот или позицию до правки базы вручную.",
    "Явная отметка «мало статистики» не пропускает в режим подтверждения, хотя спецификация это допускает; административного снятия дневного стопа с причиной нет.",
    "Покрытие данных для допуска в следующий режим завышено: пулы с пробелами не входят в знаменатель, live-отказы по GMGN считаются покрытыми.",
    "При RULESET_VERSION=shadow-hypothesis-v1 правило «LIVE без свежих данных GMGN закрыт» не действует.",
    "В официальном наборе библиотек Meteora/Solana 13 транзитивных предупреждений безопасности (6 высоких), совместимого исправления нет; секреты кошелька и токены executor, как проверено, в логи и ответы не попадают.",
    "База PostgreSQL работает без пароля в одной сети с контейнером-браузером GMGN и executor-quotes: взлом браузера дал бы полный контроль над режимом, подтверждениями и журналом.",
    "Панель не проверяет заголовок Host: через DNS rebinding чужой сайт в браузере оператора может читать данные панели и подбирать токен без ограничения попыток; сессия переживает смену токена и выход.",
    "При пустом рынке в панели нет кнопки «повторить синхронизацию» — остаётся ждать расписания.",
    "Карточка выхода показывает общие запреты и ложные «расходится» (active bin, срок), а price impact подаёт как «лимит».",
    "Если подтверждение падает не через ожидаемую ошибку, в аудите остаётся «подтверждено» без записи об отказе.",
    "Проверка готовности api всегда отвечает mode=SHADOW — мониторинг видит симуляцию, даже когда идут реальные сделки.",
    "При недоступной базе проверка готовности worker рвёт соединение вместо ответа 503, и панель пишет «worker не отвечает» вместо «нет базы».",
    "Экран настроек не показывает, задан ли GMGN_API_KEY — проверить можно только по пометкам в решениях.",
    "После сбоя внешнего источника данных в полуоткрытом состоянии проходят сразу несколько пробных запросов вместо одного.",
    "Доход от комиссий в симуляции оценивается грубо: при удержании дольше 5 минут берётся доля окна 30 минут, которое захватывает и всплеск до входа — когда объём после входа падает, доход может быть завышен примерно до 1,5 раза; точнее было бы по счётчику cumulative_fees.",
    "Журнал решений: если запись аудита упала, смена исхода пропадает из аудита навсегда — указатель последнего исхода сдвигается до записи аудита (поднимается алерт AUDIT_PERSISTENCE_FAILED); правильно сдвигать его только после записи аудита.",
    "План, не прошедший допуск (лимит позиций, дневной стоп, kill switch), держит своё решение в базе навсегда, а принятый кандидат записывается каждый цикл — рост базы меньше, чем был, но не нулевой.",
    "На тестовом стенде после перезапуска бот 5 раз подряд открыл и закрыл позицию по одному токену (DOGE-SOL) из-за «падения объёма» через 5–35 с: объём при входе и у монитора позиции расходится, паузы перед повторным входом нет; вероятно, это откат счётчиков тестового стенда, но в SHADOW стоит посмотреть.",
    "Пулы, отсеянные дешёвыми фильтрами, показывают в причинах «риск токена неизвестен» — он у них просто не читался; это шум в журнале решений.",
    "Живой прогон 75 мин (2026-10-24, суббота вечер, без RPC и без котировщика глубины): после прогрева 2216 пулов — 0 кандидатов, 0 позиций. По объёму ниже порогов автора (14 000 USD/мин за 5m и за час) 2170 из 2216; из прошедших объём почти все отсеяны по fee/TVL (2156 из 2216 ниже порога). При таких порогах кандидаты будут редкими, и 20 закрытых позиций за 7–14 дней SHADOW могут не набраться — это вопрос пакета 2 перекалибровки.",
    "В том же прогоне 1822 из 2216 пулов получили «данные рынка неполные» (INCOMPLETE_MARKET_DATA): в ответе Meteora Data API поля на месте, неполнота — в признаках бота, вероятно из-за не подключённого котировщика глубины (EXECUTOR_INTERNAL_TOKEN не задан); не разобрано — проверить в SHADOW с RPC и EXECUTOR_INTERNAL_TOKEN.",
    "Метрики GMGN без ключа теперь идут через браузер-посредник: видимое окно Chromium на компьютере, бот берёт данные изнутри вкладки gmgn.ai. Проверено вживую из Docker (объём токена, снайперы, инсайдеры, total fees, статус девелопера, ATH). Слепая приёмка шла до этого таска и видела «GMGN недоступен». Ограничения: окно должно быть открыто и ПК не спать; это против правил GMGN; первый запрос свежей сессии иногда получает 429 — пауза 15 мин.",
    "Проверить, проходит ли запрос с куками браузера вне браузера, не удалось: фильтр безопасности Claude Code заблокировал такую проверку; утверждение, что Cloudflare его не пустит, основано на устройстве защиты, а не на опыте — ручная проверка через Copy as cURL предложена пользователю."
  ]
}
