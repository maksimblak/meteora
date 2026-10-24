"""Russian operator texts for stable English codes (spec 9).

Codes stay the contract (API payloads, audit); this module is presentation only. Every
code the panel can show has a line here - screening reasons, decision flags, worker and
gateway readiness, executor refusals, entry admission, depth details, GMGN statuses,
critical alerts and code-valued evidence - and so does every code the worker writes into
events and logs for the operator (the sale after an exit, exit reasons, plan and cost
flags); a code that appears later without one is still shown verbatim rather than hidden.
"""

from collections.abc import Iterable

MODE = {
    "SHADOW": "SHADOW — симуляция без денег",
    "APPROVAL": "APPROVAL — каждая сделка с подтверждением",
    "LIVE": "LIVE — автоматическое исполнение",
}

CANDIDATE_STATUS = {
    "ACCEPT": "принят",
    "REJECT": "отклонён",
    "UNKNOWN": "нет данных (fail-closed)",
    "WARMING_UP": "прогрев",
}

KILL_SWITCH_POLICY = {
    "HOLD_AND_ALERT": "держать позиции и оповещать",
    "ORDERLY_EXIT": "упорядоченный выход через свежую симуляцию",
}

REJECTION = {
    "INPUT_IDENTITY_MISMATCH": "входные данные относятся к разным пулам",
    "SNAPSHOT_ALIGNMENT_MISMATCH": "снимки рынка не согласованы",
    "FUTURE_MARKET_DATA": "данные рынка из будущего",
    "FUTURE_TOKEN_RISK_DATA": "данные риска токена из будущего",
    "FUTURE_FEATURE_DATA": "признаки из будущего",
    "LOW_TVL": "TVL ниже минимума",
    "LOW_DEPTH": "глубина ниже минимума",
    "POOL_TOO_YOUNG": "пул слишком молодой",
    "HISTORY_WARMING_UP": "история ещё накапливается",
    "STALE_MARKET_DATA": "данные рынка устарели",
    "INCOMPLETE_MARKET_DATA": "данные рынка неполные",
    "MINT_AUTHORITY_PRESENT": "у токена есть mint authority",
    "FREEZE_AUTHORITY_PRESENT": "у токена есть freeze authority",
    "BLACKLISTED_TOKEN": "токен в чёрном списке",
    "UNVERIFIED_TOKEN": "токен не верифицирован",
    "HOLDER_CONCENTRATION_HIGH": "слишком высокая доля top-10 холдеров",
    "TOKEN_RISK_STALE": "данные риска токена устарели",
    "TOKEN_RISK_UNKNOWN": "риск токена неизвестен",
    "ARTIFICIAL_VOLUME_CONFIRMED": "подтверждён искусственный объём",
    "VOLUME_INTEGRITY_UNKNOWN": "органичность объёма неизвестна",
    "PRICE_IMPACT_HIGH": "слишком большой price impact",
    "VOLUME_SIGNAL_BELOW_THRESHOLD": "всплеск объёма ниже порога (shadow-hypothesis-v1)",
    "SCORE_BELOW_THRESHOLD": "score ниже порога",
    "MARKET_CAP_BELOW_THRESHOLD": "капитализация ниже порога",
    "TOKEN_TOO_YOUNG": "токен слишком молодой",
    "VOLUME_PER_MINUTE_BELOW_THRESHOLD": "объём в минуту ниже порога",
    "VOLUME_TO_LIQUIDITY_BELOW_THRESHOLD": "объём к ликвидности ниже порога",
    "ARTIFICIAL_VOLUME_SUSPECTED": (
        "подозрение на накрутку: total fees малы для такой капы при обороте пула за час "
        "≥ 10× TVL, или GMGN видит wash trading"
    ),
    "TOTAL_FEES_BELOW_THRESHOLD": "total fees токена ниже порога",
    "GUIDE_SIGNAL_UNKNOWN": "обязательный сигнал guide-v1 недоступен",
    "BLACKLISTED_POOL": "пул в чёрном списке Meteora",
    "DEPTH_UNAVAILABLE": "глубина и price impact не измерены котировкой (вход закрыт вне SHADOW)",
    "VOLUME_PARTICIPANTS_UNAVAILABLE": "нет числа сделок и участников (LIVE закрыт)",
    "INSIDERS_SHARE_HIGH": "доля инсайдеров (GMGN) не ниже порога",
    "BUNDLING_SHARE_HIGH": "доля бандлеров (GMGN) не ниже порога",
    "PHISHING_SHARE_HIGH": "доля фишинговых кошельков (GMGN) не ниже порога",
    "GMGN_TOP10_HOLDER_SHARE_HIGH": "доля top-10 по GMGN не ниже порога",
    "GMGN_UNAVAILABLE": "нет свежих данных GMGN (LIVE закрыт, пока GMGN_REQUIRED_FOR_LIVE)",
    "TOKEN_RISK_UNAVAILABLE": "источник риска токена (RPC) не настроен: вне SHADOW вход закрыт",
    # Selection as the author does it (G24, T18).
    "HOURLY_VOLUME_PER_MINUTE_BELOW_THRESHOLD": (
        "средний объём токена в минуту за час ниже порога — объём не устойчивый"
    ),
    "FEE_TVL_BELOW_THRESHOLD": "комиссии пула к TVL ниже порога",
    "THEME_EXCLUDED": "тема монеты в списке исключений (Илон, Трамп, политика, стримы)",
    "TOKEN_ALREADY_HELD": "по этому токену уже есть позиция или вход в работе (в любом пуле)",
    "DEVELOPER_HOLDS_TOKENS": "у девелопера есть доля токена — в LIVE вход закрыт",
    "CANDIDATE_RANK_BELOW_LIMIT": (
        "токен ниже лимита глубокой проверки по часовому объёму — в этом цикле не проверялся"
    ),
    # The pair (F5): prices, bins and quotes are taken from the first token of the pair.
    "TOKEN_SECOND_IN_PAIR": (
        "монета стоит в паре второй (первым — SOL, USDC или USDT): вход в такой пул "
        "в этой сборке не поддержан, пул считается пулом этой монеты"
    ),
    "PAIR_OF_QUOTE_TOKENS": "пара из двух quote-токенов (SOL, USDC, USDT) — это не монета",
    "PAIR_WITHOUT_QUOTE_TOKEN": "в паре нет SOL, USDC или USDT — вход не поддержан",
    # The strategy found no plan for a screened candidate (spec 6.8, F5).
    "NOT_NEAR_LOCAL_HIGH": "цена ниже 90% локального хая за час — бид-аск не с хаёв, плана нет",
    "RANGE_TOO_NARROW": (
        "bin step пула не даёт бид-аску глубины 70% в одной позиции SDK — пул не годится, "
        "плана нет"
    ),
}

# D03 / D07 / G18 / D09: the operator-facing line for each non-blocking decision flag.
FLAG_NOTICES = {
    "TOTAL_FEES_UNAVAILABLE": "total fees: нет данных — проверь в GMGN",
    "GMGN_UNAVAILABLE": "GMGN недоступен — проверь вручную",
    "DEPTH_UNAVAILABLE": (
        "глубина не измерена котировкой: SHADOW считает консервативный slippage, "
        "APPROVAL и LIVE закрыты"
    ),
    "VOLUME_PARTICIPANTS_UNAVAILABLE": (
        "нет числа сделок и участников: органичность объёма — по повторам всплесков и "
        "total fees к капе"
    ),
    "DEVELOPER_HOLDS_TOKENS": (
        "у девелопера есть доля токена — проверь вручную (в LIVE вход закрыт)"
    ),
    "TOKEN_RISK_UNAVAILABLE": "риск токена не проверен: RPC не настроен (SOLANA_RPC_URL)",
    # G24 (T18).
    "VOLUME_SPIKE": "всплеск объёма в окне 5m — плюс к баллу",
    "TOKEN_VOLUME_POOL_ONLY": (
        "объём токена — сумма его DLMM-пулов Meteora, а не объём токена GMGN, как в Trending "
        "(нет ключа GMGN или свежего ответа): сверь объём в GMGN"
    ),
    "SNIPERS_HIGH": "много снайперов по GMGN — автор советует подождать",
    "TOKEN_REVIVED": "токену 60+ дней: монета ожила, объёмы обычно быстро пропадают",
    "THEME_UNKNOWN": (
        "у токена нет ни имени, ни тикера: тема не проверена — APPROVAL и LIVE закрыты"
    ),
}

STRATEGY_SELECTION = {
    "LEGACY_NEUTRAL_SPOT": "нейтральный режим → Spot (прежние правила)",
    "LEGACY_DIRECTIONAL_BID_ASK": "направленный режим → Bid-Ask (прежние правила)",
    "BID_ASK_FROM_LOCAL_HIGH": "Bid-Ask от локального хая — выбор по умолчанию guide-v1",
    "SPOT_ATH_BREAKOUT": "Spot: пробитие ATH с подтверждённым объёмом",
    "SPOT_OVERVOLUME": "Spot: сверхобъём, короткое окно",
}

EXECUTION_PHASE = {
    "AWAITING_APPROVAL": "ждёт подтверждения оператора",
    "APPROVED": "подтверждено, ждёт отправки",
    "SIMULATED": "симулировано",
    "SUBMITTING": "отправляется",
    "SUBMITTED": "отправлено, ждёт подтверждения сети",
    "CONFIRMED": "подтверждено сетью",
    "FAILED": "не выполнено",
    "EXPIRED": "истекло",
    "UNKNOWN": "исход неизвестен — сверка с сетью",
}

READINESS = {
    "PERSISTENCE_UNAVAILABLE": "база данных недоступна",
    "PROVIDERS_NOT_CONFIGURED": "провайдеры данных не настроены",
    "UNIVERSE_STALE": "universe устарел или ещё не синхронизирован",
    "CRITICAL_ALERTS_OPEN": "есть открытые критические события",
    "EXECUTION_GATEWAY_NOT_CONFIGURED": "execution gateway не подключён",
    "EXECUTION_GATEWAY_NOT_READY": "execution gateway не готов",
    "WORKER_HEALTH_NOT_CONFIGURED": "адрес здоровья worker не задан (WORKER_HEALTH_URL)",
    "WORKER_UNREACHABLE": "worker не отвечает",
    "NO_HEARTBEAT": "worker ещё не прошёл ни одного цикла",
    "HEARTBEAT_STALE": "worker давно не проходил цикл — возможно, завис",
    "SHADOW_DAYS_BELOW_7": "меньше 7 полных дней shadow-наблюдений",
    "COMPLETED_POSITIONS_BELOW_20": "меньше 20 завершённых paper-позиций",
    "NET_EXPECTANCY_NOT_POSITIVE": "net expectancy не положительный",
    "DRAWDOWN_ABOVE_4_PERCENT": "просадка выше 4%",
    "DATA_COVERAGE_BELOW_99_PERCENT": "покрытие данными ниже 99%",
    "RISK_INVARIANT_BREACHED": "нарушены risk-инварианты",
    "EVIDENCE_NOT_FROM_SHADOW": "evidence собран не в SHADOW",
    "EVIDENCE_STALE": "evidence устарел — соберите заново",
    "EVIDENCE_ALREADY_CONSUMED": "evidence уже использован",
}

APPROVAL_BLOCKERS = {
    "OPERATOR_AUTH_NOT_CONFIGURED": "не задан токен оператора (APP_OPERATOR_TOKEN)",
    "TRADING_MODE_SHADOW": "TRADING_MODE=SHADOW: gateway для подтверждений не подключён",
    "APPROVAL_SECRET_NOT_CONFIGURED": "не задан секрет подписи approval (EXECUTOR_APPROVAL_SECRET)",
    "EXECUTION_GATEWAY_NOT_CONFIGURED": "execution gateway не подключён",
    "DURABLE_EXECUTION_STORE_REQUIRED": "для подтверждений нужен PostgreSQL (DATABASE_URL)",
    "MODE_NOT_APPROVAL": "система не в режиме APPROVAL",
    "COMMAND_NOT_APPROVAL_MODE": "команда построена не для режима APPROVAL",
    "NOT_AWAITING_APPROVAL": "команда не ждёт подтверждения",
    "KILL_SWITCH_ACTIVE": "активен kill switch",
    "DAILY_STOP_ACTIVE": "сработал дневной стоп 4%",
    "COMMAND_EXPIRED": "срок команды истёк",
    "SIMULATION_STALE": "симуляция устарела — нужна новая",
    "SIMULATION_FAILED": "симуляция завершилась ошибкой",
    # ``entry_impact_rejection`` (F2) and ``swap_rejection`` (T17): the execution service's
    # own codes for an entry and for the sale after an exit.
    "PRICE_IMPACT_UNAVAILABLE": (
        "price impact не измерен котировкой executor'а: вход и продажа без него не подтверждаются"
    ),
    "SLIPPAGE_BREACH": "price impact входа выше допустимого slippage команды",
    "PRICE_IMPACT_BREACH": (
        "price impact продажи выше лимита SWAP_MAX_PRICE_IMPACT_BPS — бот продаст часть поменьше"
    ),
    "SWAP_QUOTE_INCOMPLETE": (
        "котировка продажи неполная: продаёт не всю сумму, нулевой выход или минимальный "
        "выход шире допуска SWAP_SLIPPAGE_BPS"
    ),
    "ENTRY_ANCHOR_UNAVAILABLE": (
        "источник данных не даёт active bin и decimals токенов: вход в APPROVAL/LIVE закрыт "
        "(D06), новые сделки не строятся"
    ),
}

# Executor simulation/command refusals (D08) and the worker's own entry guards.
EXECUTOR = {
    "ACTIVE_BIN_DRIFT": "active bin ушёл дальше допуска от бина, на котором строилась команда",
    "ACTIVE_BIN_UNAVAILABLE": "active bin пула не прочитан — вход не строится",
    "POOL_STATE_MISMATCH": "состояние пула on-chain не совпало со снимком",
    "SLIPPAGE_BREACH": "price impact входа выше допустимого slippage команды",
    "PRICE_IMPACT_UNAVAILABLE": "price impact не измерен котировкой SDK",
    "WALLET_INSUFFICIENT_BASE_TOKEN": "в кошельке не хватает базового токена для входа",
    "WALLET_INSUFFICIENT_QUOTE_TOKEN": "в кошельке не хватает quote-токена для входа",
    "INSUFFICIENT_FUNDS": "в кошельке не хватает средств (токена, SOL на комиссию или rent)",
    "INSUFFICIENT_LIQUIDITY": "у пула не хватает ликвидности на эталонный размер",
    # The sale after an exit (G22, T17/F4): a refused part is retried at half its size.
    "SWAP_QUOTE_INSUFFICIENT_LIQUIDITY": (
        "котировка SDK: в пуле не хватает ликвидности на эту часть продажи — бот попробует "
        "часть вдвое меньше"
    ),
    "PRICE_IMPACT_BREACH": (
        "price impact котировки продажи выше лимита команды — бот попробует часть вдвое меньше"
    ),
    "SWAP_QUOTE_INCOMPLETE": "котировка продажи неполная — команда продажи не строится",
    "SWAP_EXCEEDS_EXIT_RETURNS": (
        "продажа больше, чем позиция вернула при выходе: executor отказал, ничего не отправлено"
    ),
    "TOKEN_DECIMALS_MISMATCH": "decimals токенов on-chain не совпали с командой",
    "MARKET_STATE_UNAVAILABLE": "executor не прочитал состояние пула",
    "POSITION_STATE_UNAVAILABLE": "executor не прочитал состояние позиции",
    "TRANSACTION_SIMULATION_FAILED": "симуляция транзакции в сети завершилась ошибкой",
    "SIMULATION_FAILED": "симуляция завершилась ошибкой",
    "SIMULATION_INCOMPLETE": "симуляция вернула неполный результат",
    "STALE_SIMULATION": "симуляция устарела",
    "FRESH_SIMULATION_REQUIRED": "нужна свежая симуляция перед отправкой",
    "COMMAND_EXPIRED": "срок команды истёк",
    "MODE_NOT_AUTHORIZED": "executor не обслуживает этот режим",
    "MODE_TOKEN_REQUIRED": "для LIVE нужен токен режима (EXECUTOR_MODE_TOKEN)",
    "LIVE_PROMOTION_REQUIRED": "нет действующего LIVE promotion evidence",
    "VALID_APPROVAL_REQUIRED": "нужно действующее подтверждение оператора",
    "APPROVAL_ALREADY_USED": "подтверждение уже использовано — исход сверяется",
    "IDEMPOTENCY_CONFLICT": "ключ идемпотентности занят другой командой — исход сверяется",
    "IDEMPOTENCY_MISMATCH": "повтор команды с другим содержимым отклонён",
    "SUBMISSION_DISABLED": "отправка транзакций в executor выключена",
    "SHADOW_NO_SUBMIT": "в SHADOW транзакции не отправляются",
    "PARTIAL_BROADCAST": "транзакции отправлены частично — нужна ручная сверка",
    "SUBMIT_OUTCOME_UNKNOWN": "исход отправки неизвестен — сверка с сетью",
    "BUILD_BINDING_MISMATCH": "собранная транзакция не совпала с командой",
    "POSITION_BINDING_MISMATCH": "позиция не совпала с командой",
    "SIMULATION_BINDING_MISMATCH": "симуляция не совпала с командой",
    "RECEIPT_COUNT_MISMATCH": "число квитанций не совпало с числом транзакций",
    "EMPTY_RECEIPT": "executor вернул пустую квитанцию",
    "QUOTE_FAILED": "котировка executor'а не получена",
    "ENTRY_ANCHOR_UNAVAILABLE": "нет active bin и decimals токенов: вход вне SHADOW не строится",
    "EXECUTION_GATEWAY_NOT_CONFIGURED": "execution gateway не подключён: вход вне SHADOW отменён",
}

# Entry admission (EntryGate) and the position sizer's reasons and caps (RiskReason).
ADMISSION = {
    "RISK_DAY_NOT_OPENED": "risk day ещё не открыт: входы ждут первой оценки портфеля",
    "KILL_SWITCH_ACTIVE": "активен kill switch",
    "DAILY_STOP_REACHED": "достигнут дневной стоп 4%",
    "POSITION_LIMIT_REACHED": "достигнут лимит одновременных позиций",
    "PLAN_EXPIRED": "план устарел до исполнения",
    "ZERO_CAPACITY": "лимиты не оставили места под позицию",
    "INVALID_LOSS_ESTIMATE": "оценка убытка некорректна",
    "ALLOCATION_CAP": "ограничено лимитом 10% капитала",
    "POSITION_RISK_CAP": "ограничено лимитом риска 1% на позицию",
    "LIQUIDITY_CAP": "ограничено ликвидностью пула",
    "BALANCE_CAP": "ограничено балансом",
    "SIZED_PLAN_INVARIANT_BREACH": "размер нарушил инвариант риска — вход отклонён",
    "PLAN_ALREADY_ADMITTED": "план уже допущен в этом цикле",
    # The strategy refused the plan (spec 6.8, T19).
    "NOT_NEAR_LOCAL_HIGH": "цена ниже 90% локального хая за час — бид-аск не с хаёв, плана нет",
    "RANGE_TOO_NARROW": (
        "bin step пула не даёт бид-аску глубины 70% в одной позиции SDK — пул не годится"
    ),
}

# ``depth_source`` detail: why depth and price impact were not measured by a quote (D07, F1).
DEPTH_DETAIL = {
    "QUOTE_PRICE_UNKNOWN": "нет цены quote-токена в USD: эталонный размер не пересчитать",
    "QUOTE_DECIMALS_UNKNOWN": "неизвестны decimals quote-токена",
    "QUOTES_NOT_CONFIGURED": "котировки executor'а не подключены (EXECUTOR_QUOTES_URL)",
    "NOT_A_CANDIDATE": "пул отсеян дешёвыми фильтрами — котировка не запрашивалась",
    "NOT_MEASURED": "глубина ещё не измерялась",
    "REFERENCE_SIZE_ZERO": "эталонный размер в единицах токена округлился до нуля",
    "INSUFFICIENT_LIQUIDITY": "у пула не хватает ликвидности на эталонный размер",
    "QUOTE_FAILED": "котировка executor'а не получена",
}

# Critical alerts by code; the alert's own message stays next to it.
ALERTS = {
    "DAILY_STOP_TRIGGERED": "сработал дневной стоп 4%: новые входы закрыты до следующего дня",
    "KILL_SWITCH_ACTIVE": "включён kill switch",
    "EXECUTION_RECOVERY_REQUIRED": "исход команды не сверен — нужна сверка",
    "DECISION_PERSISTENCE_FAILED": "решение не записано — цикл остановлен до денежного шага",
    "AUDIT_PERSISTENCE_FAILED": "аудит не записан — денежный шаг не выполнен",
    "PLAN_PERSISTENCE_FAILED": "план не записан — вход не выполнен",
    "ENTRY_FILL_UNTRACKED": "заполнение входа не попало в учёт",
    "ENTRY_POSITION_UNKNOWN": "executor не назвал адрес позиции входа",
    "EXIT_REQUIRES_EXECUTION_GATEWAY": "выход позиции требует execution gateway",
    "EXIT_GATEWAY_UNAVAILABLE": "execution gateway недоступен для выхода",
    "POSITION_ACTION_FAILED": "действие по позиции не выполнено",
    "POSITION_ACTION_ATTEMPTS_EXHAUSTED": "попытки действия по позиции исчерпаны",
    "POSITION_PARTIALLY_EXECUTED": "выход выполнен частично — нужны действия оператора",
    "GATEWAY_FILL_UNBOOKED": "подтверждённое исполнение не попало в учёт",
    "POSITION_CONTEXT_MISSING": (
        "нет контекста позиции: выход и продажа монеты после выхода не строятся"
    ),
    "EXECUTION_STATE_UNAVAILABLE": "состояние исполнения недоступно",
    # The sale of the coin an exit returned (G22, T17/F4). Until the sale ends the position
    # keeps its slot (CLOSE is booked after it); the unsold coin stays in the wallet.
    "EXIT_SALE_IMPACT_EXCEEDED": (
        "монета после выхода не продана: даже самая малая часть выше лимита price impact "
        "или в пуле нет ликвидности — решение за оператором"
    ),
    "EXIT_SALE_FAILED": (
        "монета после выхода не продана: попытки продажи исчерпаны — решение за оператором"
    ),
    "EXIT_SALE_AMOUNT_UNKNOWN": (
        "сеть не сообщила, сколько монеты вернул выход: продажа не начата, проверь кошелёк"
    ),
    "EXIT_SALE_GATEWAY_UNAVAILABLE": (
        "продажа монеты после выхода ждёт execution gateway — продолжится, когда он ответит"
    ),
    "EXIT_SALE_OUTCOME_UNKNOWN": (
        "исход части продажи неизвестен: продажа и закрытие позиции остановлены, слот "
        "занят — проверь кошелёк в эксплорере"
    ),
    "EXIT_SALE_BLOCKED": (
        "часть продажи отклонена до записи: монета не продана, нужна проверка оператора"
    ),
}

# Steps of a position's ledger (position page, ``LedgerKind``).
LEDGER_KIND = {
    "OPEN": "вход",
    "ADD": "добавление ликвидности",
    "REMOVE": "частичный выход",
    "CLOSE": "закрытие",
    "SWAP": "продажа монеты после выхода",
}

# Where the sale of the coin one confirmed exit returned stands (G22, ``SaleStatus``).
SALE_STATUS = {
    "DISABLED": "продажа выключена (SWAP_AFTER_EXIT=false): монета остаётся в кошельке",
    "NOT_REQUIRED": "выход не вернул монету — продавать нечего",
    "PENDING": "продажа ждёт: расчёта сети, сверки исхода или execution gateway",
    "AWAITING_APPROVAL": "часть продажи ждёт подтверждения оператора",
    "SELLING": "монета продаётся по частям",
    "SOLD": "монета продана в quote-токен",
    "UNSOLD": "монета не продана и осталась в кошельке — решение за оператором",
}

# ``SaleProgress.detail``: why a sale waits or how it ended (T17, F4).
SALE_DETAIL = {
    "SETTLEMENT_PENDING": "ждём, пока сеть сообщит, сколько монеты вернул выход",
    "SALE_SETTLEMENT_PENDING": "ждём расчёт сети по проданной части",
    "REDUCTION_SALE_PENDING": "закрытие ждёт конца продажи после частичного выхода",
    "NEXT_PART": "следующая часть продажи",
    "AMOUNT_UNKNOWN": "сеть не сообщила, сколько монеты вернул выход",
    "PRICE_IMPACT_EXCEEDED": "даже самая малая часть выше лимита price impact",
    "ATTEMPTS_EXHAUSTED": "попытки продажи исчерпаны",
    "GATEWAY_UNAVAILABLE": "execution gateway недоступен",
    "SALE_OUTCOME_UNKNOWN": "исход части продажи неизвестен — только сверка, без повтора",
    "SALE_BLOCKED": "часть продажи отклонена до записи",
    "CONTEXT_MISSING": "нет контекста позиции",
    "AWAITING_APPROVAL": "часть продажи ждёт подтверждения",
}

# Why the monitor reduces or closes a position (``PositionReason``; spec 6.5, 6.8).
POSITION_REASON = {
    "KILL_SWITCH_ACTIVE": "kill switch с политикой ORDERLY_EXIT",
    "RANGE_BREACH": "цена вышла из диапазона позиции",
    "MAX_HOLDING_PERIOD": "истёк срок удержания (Bid-Ask 24 ч, Spot на ATH 3 ч, сверхобъём 30 мин)",
    "VOLUME_DECAY": "объём упал против входа",
    "FEE_DECAY": "комиссии пула упали против входа",
    "TVL_COMPETITION": "в пул пришло много конкурирующей ликвидности",
    "NEAR_RANGE_EDGE": "цена у края диапазона",
    "PRICE_SHOCK": "ценовой шок",
    "TOKEN_RISK_DETERIORATED": "риск токена ухудшился",
    "TOKEN_RISK_UNKNOWN": "риск токена стал неизвестен",
    "MARKET_DATA_UNSAFE": "данные рынка ненадёжны",
    "FEE_SIGNAL_UNKNOWN": "сигнал комиссий неизвестен",
    "REBALANCE_AFTER_COSTS": "ребаланс выгоден после издержек",
    "CLOSE_BEATS_REBALANCE": "закрыть выгоднее, чем ребалансировать",
    "TAKE_PROFIT": (
        "тейк-профит: net P&L с комиссиями достиг цели плана (5% или 3% у сверхобъёма; "
        "только SHADOW)"
    ),
    "DEV_SOLD": "девелопер продал монету (GMGN: статус «продал» или доля упала вдвое)",
}

# What a plan was decided without (``StrategyFlag``, T19).
STRATEGY_FLAG = {
    "ATH_POOL_ONLY": "ATH взят у пула, а не у токена GMGN (нет ключа или свежего ответа)",
}

# ``CommandConfirmed.cost_model_flags`` (F4): costs SHADOW could not price.
COST_MODEL_FLAGS = {
    "PRIORITY_FEE_UNPRICED": (
        "priority fee не учтён: пул не к wSOL, комиссию в SOL нечем пересчитать в quote-токен"
    ),
}

# ``PositionActionProposed.take_profit`` (F4).
TAKE_PROFIT_STATE = {
    "UNMEASURED": (
        "тейк-профит не срабатывает: вне SHADOW фактический net P&L позиции не измеряется"
    ),
}

# Evidence rows whose value is a code: the Russian line next to it (G24, F5, T19).
_EVIDENCE_VALUES: dict[str, dict[str, str]] = {
    "ath_source": {
        "GMGN_TOKEN": "ATH токена из GMGN",
        **STRATEGY_FLAG,
    },
    "pool_pair": {
        "TOKEN_FIRST": "монета первой, quote-токен второй — рабочая пара",
        "TOKEN_SECOND": "монета второй (SOL-МОНЕТА): вход не поддержан",
        "QUOTE_ONLY": "оба токена — SOL/USDC/USDT, монеты нет",
        "NO_QUOTE": "в паре нет SOL, USDC или USDT",
    },
    "token_volume_source": {
        "GMGN": "объём токена GMGN, как в Trending",
        "DLMM_POOLS": "сумма DLMM-пулов токена на Meteora (без GMGN)",
        "PENDING": "GMGN спрашивается только после дешёвых фильтров — пока сумма пулов",
    },
    "token_volume_rank_source": {
        "GMGN": "ранг по объёму токена GMGN из кэша",
        "DLMM_POOLS": "ранг по сумме DLMM-пулов токена",
        "PENDING": "ранг по сумме DLMM-пулов токена",
    },
    "market_cap_source": {
        "GMGN": "капитализация из GMGN",
        "DATA_API": "капитализация из Meteora Data API",
        "UNKNOWN": "капитализацию не дал ни один источник",
    },
    "developer_status": {
        "HOLDING": "девелопер держит монету",
        "SOLD": "девелопер продал монету",
        "UNAVAILABLE": "статус девелопера неизвестен (нет свежего ответа GMGN)",
    },
}

# ``gmgn_status`` reasons (T16 OpenAPI and the browser service), by exact code or family.
_GMGN_REASONS = {
    "PLAN_UPGRADE_REQUIRED": "тариф GMGN не позволяет этот запрос (PLAN_UPGRADE_REQUIRED)",
    "RATE_LIMITED": "превышен лимит запросов GMGN — пауза до сброса",
    "RATE_LIMIT_LOCAL_QUEUE": "очередь лимитера переполнена — запрос не отправлялся",
    "API_TIMEOUT": "GMGN не ответил вовремя",
    "API_UNAVAILABLE": "GMGN недоступен",
    "API_PAYLOAD_INVALID": "ответ GMGN в незнакомом формате",
    "API_MINT_MISMATCH": "GMGN ответил про другой токен",
    "API_ERROR": "GMGN вернул ошибку",
    "CIRCUIT_OPEN": "запросы к GMGN приостановлены после серии отказов",
    "HTTP_401": "GMGN отклонил ключ (401) — проверьте GMGN_API_KEY",
    "HTTP_403": "GMGN отказал в доступе (403): ключ, IP или IPv6",
    "SERVICE_TIMEOUT": "сервис gmgn не ответил вовремя",
    "SERVICE_UNAVAILABLE": (
        "посредник GMGN (или сервис gmgn) недоступен: окно посредника закрыто, ПК спит "
        "или GMGN_URL неверен"
    ),
    "SERVICE_PAYLOAD_INVALID": "ответ сервиса gmgn в незнакомом формате",
    "SERVICE_SCHEMA_UNKNOWN": "сервис gmgn другой версии",
    "SERVICE_MINT_MISMATCH": "сервис gmgn ответил про другой токен",
    # T21 (G25): the browser bridge on the host and the headless page service.
    "SERVICE_UNAUTHORIZED": (
        "посредник GMGN отклонил запрос: GMGN_BRIDGE_TOKEN у worker и посредника разный"
    ),
    "LOGIN_REQUIRED": (
        "GMGN просит вход — войти можно только вручную в окне посредника, по вашему выбору"
    ),
    "CHALLENGE": "проверка Cloudflare в окне посредника не прошла — пауза, без обхода",
    "CHALLENGE_TIMEOUT": "проверка Cloudflare не прошла вовремя — пауза, без обхода",
    "CAPTCHA": "GMGN показал капчу — пауза; капчу бот не решает",
    "ACCESS_BLOCKED": "GMGN или Cloudflare заблокировал доступ — пауза",
    "API_BLOCKED": "GMGN отказал странице в данных (401/403)",
    "NETWORK_ERROR": "сетевая ошибка при запросе к GMGN",
    "PAGE_TIMEOUT": "страница GMGN не загрузилась вовремя",
    "NO_SIGNALS": "GMGN не отдал ни одной метрики токена",
    "FETCH_PENDING": "посредник ещё читает токен — ответ будет в следующем цикле",
    "QUEUE_FULL": "очередь посредника переполнена — токен спросят позже",
    "BROWSER_CLOSED": "окно посредника GMGN закрыто — запустите посредника снова",
    "BROWSER_ERROR": "сбой браузера посредника GMGN",
    "BROWSER_UNAVAILABLE": "браузер посредника GMGN не запущен",
}
_GMGN_FAMILIES = (
    ("AUTH_", "GMGN отклонил запрос авторизации (ключ, время или IP)"),
    ("RATE_LIMIT_", "превышен лимит запросов GMGN — пауза до сброса"),
)
_GMGN_STATES = {
    "FRESH": "данные GMGN свежие",
    "STALE": "данные GMGN устарели",
    "NOT_CONFIGURED": (
        "источник GMGN не настроен: ни GMGN_API_KEY, ни GMGN_URL (посредник или сервис gmgn)"
    ),
}
_GMGN_GENERIC = "GMGN недоступен"

# Readiness (/health/ready, gateway, approval desk) shows codes from any of these tables.
_READINESS_TABLES = (READINESS, APPROVAL_BLOCKERS, EXECUTOR)

# G17 (spec 6.6): what the bot cannot see; every mark is required for any approval - an
# entry, an exit and the sale after an exit.
EXIT_CHECKLIST = (
    ("twitter_hype", "Twitter и хайп проверены"),
    ("developer_supply", "Запас монет у девелопера проверен"),
    ("no_pvp_copies", "Нет гонки копий монеты (PvP)"),
    ("bubblemaps_clusters", "Кластеры на Bubblemaps проверены"),
)
# Spec 6.8 (G23, No 11-14): an entry asks for two more marks.
ENTRY_CHECKLIST = (
    *EXIT_CHECKLIST,
    ("organic_chart", "График органичный"),
    ("theme_not_excluded", "Тема не Илон/Трамп/политика/стрим"),
)
# Every mark an approval request can carry (the web form reads each of them).
CHECKLIST = ENTRY_CHECKLIST


def label(table: dict[str, str], code: str) -> str:
    return table.get(code, code)


def readiness_label(code: str) -> str:
    name, _, subject = code.partition(":")
    if name == "PROVIDER_DEGRADED" and subject:
        return f"провайдер деградировал: {subject}"
    for table in _READINESS_TABLES:
        if code in table:
            return table[code]
    return code


def executor_label(code: str) -> str:
    return EXECUTOR.get(code) or APPROVAL_BLOCKERS.get(code, code)


def admission_label(code: str) -> str:
    return ADMISSION.get(code) or REJECTION.get(code) or executor_label(code)


def alert_label(code: str) -> str:
    return ALERTS.get(code) or executor_label(code)


def gmgn_status_label(value: str) -> str:
    """``FRESH``, ``STALE``, ``NOT_CONFIGURED`` or ``UNAVAILABLE:<reason>`` in Russian."""

    if value in _GMGN_STATES:
        return _GMGN_STATES[value]
    _, _, reason = value.partition(":")
    if reason in _GMGN_REASONS:
        return f"{_GMGN_GENERIC}: {_GMGN_REASONS[reason]}"
    for prefix, text in _GMGN_FAMILIES:
        if reason.startswith(prefix):
            return f"{_GMGN_GENERIC}: {text}"
    return f"{_GMGN_GENERIC} ({reason or value})"


def evidence_note(name: str, value: str, threshold: str | None) -> str | None:
    """A Russian explanation of a code in an evidence row; ``None`` for plain numbers."""

    if name == "gmgn_status":
        return gmgn_status_label(value)
    if name == "depth_source" and threshold:
        return DEPTH_DETAIL.get(threshold) or executor_label(threshold)
    return _EVIDENCE_VALUES.get(name, {}).get(value)


def flag_notices(codes: Iterable[str]) -> list[str]:
    return [FLAG_NOTICES.get(code, code) for code in codes]
