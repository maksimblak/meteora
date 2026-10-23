"""Russian operator texts for stable English codes. Unknown codes are shown verbatim.

Codes stay the contract (API payloads, audit); this module is presentation only.
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
    "VOLUME_SIGNAL_BELOW_THRESHOLD": "всплеск объёма ниже порога",
    "SCORE_BELOW_THRESHOLD": "score ниже порога",
    "MARKET_CAP_BELOW_THRESHOLD": "капитализация ниже порога",
    "TOKEN_TOO_YOUNG": "токен слишком молодой",
    "VOLUME_PER_MINUTE_BELOW_THRESHOLD": "объём в минуту ниже порога",
    "VOLUME_TO_LIQUIDITY_BELOW_THRESHOLD": "объём к ликвидности ниже порога",
    "ARTIFICIAL_VOLUME_SUSPECTED": "подозрение на искусственный объём (комиссии малы)",
    "TOTAL_FEES_BELOW_THRESHOLD": "total fees токена ниже порога",
    "GUIDE_SIGNAL_UNKNOWN": "обязательный сигнал guide-v1 недоступен",
}

# D03 / G18: the operator-facing line for each non-blocking decision flag, by code.
FLAG_NOTICES = {
    "TOTAL_FEES_UNAVAILABLE": "total fees: нет данных — проверь в GMGN",
    "GMGN_UNAVAILABLE": "GMGN недоступен — проверь вручную",
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
    "PRICE_IMPACT_UNMEASURED": "price impact не измерен executor'ом",
    "PRICE_IMPACT_ABOVE_LIMIT": "price impact выше допустимого slippage",
    "ENTRY_ANCHOR_UNAVAILABLE": (
        "источник данных не даёт active bin и decimals токенов: вход в APPROVAL/LIVE закрыт "
        "(D06), новые сделки не строятся"
    ),
}

# G17 (spec 6.6): what the bot cannot see; every mark is required for an approval.
CHECKLIST = (
    ("twitter_hype", "Twitter и хайп проверены"),
    ("developer_supply", "Запас монет у девелопера проверен"),
    ("no_pvp_copies", "Нет гонки копий монеты (PvP)"),
    ("bubblemaps_clusters", "Кластеры на Bubblemaps проверены"),
)


def label(table: dict[str, str], code: str) -> str:
    return table.get(code, code)


def readiness_label(code: str) -> str:
    name, _, subject = code.partition(":")
    if name == "PROVIDER_DEGRADED" and subject:
        return f"провайдер деградировал: {subject}"
    return READINESS.get(code) or APPROVAL_BLOCKERS.get(code, code)


def flag_notices(codes: Iterable[str]) -> list[str]:
    return [FLAG_NOTICES.get(code, code) for code in codes]
