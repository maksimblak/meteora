"""Read side of the operator panel: status, pools, decisions, positions, P&L, config.

Every read is safe to repeat and never changes state. Empty and first-run states are
explicit (``NOT_SYNCED``, ``WARMING_UP``, reject categories) instead of empty tables.
"""

from collections import Counter
from collections.abc import Sequence
from datetime import timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import ValidationError

from src.accounting.service import AccountingService, PerformanceService
from src.api import labels, presenters
from src.api.catalog import DecisionQuery
from src.api.context import OperatorContext
from src.api.errors import ApiError
from src.api.schemas import (
    DecisionDetail,
    DecisionSummary,
    EffectiveConfig,
    ModeJournal,
    ModeReadinessView,
    Notice,
    Page,
    PerformanceView,
    PoolDetail,
    PoolSummary,
    PositionDetail,
    PositionSummary,
    RangeView,
    ReasonCount,
    RiskUtilization,
    SecretStatus,
    ServiceHealth,
    SystemStatus,
    UniverseStatus,
    WarmupView,
)
from src.domain.features.models import SignalReadiness
from src.domain.models.primitives import PublicKey
from src.domain.models.trading import TradingMode
from src.domain.screening import CandidateStatus
from src.execution.contracts import ExecutionPhase
from src.market.models import PoolSnapshot
from src.persistence.factory import PersistenceBackend
from src.portfolio.models import PositionStatus, PositionValuation, ShadowPosition, TimeWindow

_URL_FIELDS = (
    "meteora_data_api_base_url",
    "solana_rpc_url",
    "solana_ws_url",
    "jito_block_engine_url",
)
# Secret names are listed by presence only (``NAME`` or ``NAME_FILE``), never by value.
_CONFIG_SECRETS = (
    ("APP_OPERATOR_TOKEN", "operator_token"),
    ("APP_SESSION_SECRET", "session_secret"),
    ("DATABASE_URL", "database_url"),
    ("HELIUS_API_KEY", "helius_api_key"),
    ("EXECUTOR_INTERNAL_TOKEN", "executor_internal_token"),
    ("TELEGRAM_BOT_TOKEN", "telegram_bot_token"),
)
_ENV_SECRETS = ("EXECUTOR_MODE_TOKEN", "EXECUTOR_APPROVAL_SECRET")
_APPROVAL_REQUIRED = (
    "APP_OPERATOR_TOKEN",
    "EXECUTOR_URL",
    "EXECUTOR_INTERNAL_TOKEN",
    "EXECUTOR_APPROVAL_SECRET",
    "SOLANA_RPC_URL",
    "WALLET_PUBLIC_KEY",
)
_LIVE_REQUIRED = (*_APPROVAL_REQUIRED, "EXECUTOR_MODE_TOKEN", "LIVE_PROMOTION_FILE")
_ANCHOR_NOTE = "ENTRY_ANCHOR_UNAVAILABLE"


def parse_public_key(value: str) -> str:
    try:
        return PublicKey.model_validate(value).root
    except ValidationError as error:
        raise ApiError(
            422,
            "INVALID_INPUT",
            "Некорректный адрес Solana",
            {"fields": [{"field": "address", "reason": "not a base58 public key"}]},
        ) from error


def anchor_available(snapshots: Sequence[PoolSnapshot]) -> bool:
    return any(
        item.active_bin_id is not None
        and item.base_decimals is not None
        and item.quote_decimals is not None
        for item in snapshots
    )


def anchor_notice() -> Notice:
    return presenters.readiness_notices((_ANCHOR_NOTE,))[0]


class OperatorReads:
    def __init__(self, context: OperatorContext) -> None:
        self._ctx = context

    # --- dashboard --------------------------------------------------------------------

    async def status(self, *, approval_blockers: Sequence[str] = ()) -> SystemStatus:
        ctx = self._ctx
        repositories = ctx.repositories
        now = ctx.clock.now()
        state = await ctx.mode_state()
        alerts = await ctx.sync(repositories.mode.alerts)
        open_alerts = sorted(
            (alert for alert in alerts if alert.open), key=lambda alert: alert.created_at
        )
        services = await self.services()
        universe = await self.universe_status()
        pending = await repositories.execution.in_phases((ExecutionPhase.AWAITING_APPROVAL,))
        readiness: list[str] = []
        for service in services:
            # SHADOW never uses the execution gateway: its absence is not a problem there.
            if service.status == "OK" or (
                service.name == "executor" and state.mode is TradingMode.SHADOW
            ):
                continue
            readiness.extend(reason.code for reason in service.reasons)
        if state.kill_switch_active:
            readiness.append("KILL_SWITCH_ACTIVE")
        if state.mode is TradingMode.APPROVAL:
            readiness.extend(approval_blockers)
        if state.mode is not TradingMode.SHADOW and not anchor_available(
            await repositories.history.latest_universe()
        ):
            readiness.append(_ANCHOR_NOTE)
        notices = presenters.readiness_notices(dict.fromkeys(readiness))
        return SystemStatus(
            generated_at=now,
            mode=presenters.mode_view(state),
            services=services,
            universe=universe,
            risk=await self.risk(state.daily_stop_active_on(ctx.today())),
            alerts=[presenters.alert_view(alert) for alert in reversed(open_alerts[-20:])],
            approvals_pending=len(pending),
            readiness=notices,
            ruleset_version=ctx.policy.ruleset_version,
            config_version=ctx.policy.config_version,
        )

    async def services(self) -> list[ServiceHealth]:
        ctx = self._ctx
        database_ok = await ctx.repositories.ping()
        services = [
            ServiceHealth(name="api", status="OK", reasons=[]),
            ServiceHealth(
                name="database",
                status="OK" if database_ok else "DOWN",
                reasons=[] if database_ok else presenters.readiness_notices(
                    ("PERSISTENCE_UNAVAILABLE",)
                ),
            ),
        ]
        for probe in ctx.probes:
            result = await probe.check()
            services.append(
                ServiceHealth(
                    name=probe.name,
                    status=result.status,
                    reasons=presenters.readiness_notices(result.reasons),
                )
            )
        return services

    async def universe_status(self) -> UniverseStatus:
        ctx = self._ctx
        history = ctx.repositories.history
        snapshots = await history.latest_universe()
        last_sync = await history.latest_sync_at()
        now = ctx.clock.now()
        addresses = {item.pool.address.root for item in snapshots}
        outcomes = [
            outcome
            for outcome in await ctx.catalog.latest_outcomes()
            if outcome.pool_address in addresses
        ]
        statuses = Counter(outcome.status.value for outcome in outcomes)
        reasons = Counter(
            reason
            for outcome in outcomes
            if outcome.status is not CandidateStatus.ACCEPT
            for reason in outcome.reasons
        )
        age = None if last_sync is None else max(int((now - last_sync).total_seconds()), 0)
        if last_sync is None:
            state, message = "NOT_SYNCED", (
                "Первая синхронизация universe ещё не завершена: идёт прогрев, сигналов и "
                "paper-сделок нет"
            )
        elif age is not None and age > ctx.settings.maximum_sync_age.total_seconds():
            state, message = "STALE", (
                "Последняя синхронизация устарела: worker повторит её по расписанию; "
                "проверьте его здоровье"
            )
        else:
            state, message = "FRESH", "Universe синхронизирован"
        if state != "NOT_SYNCED" and not snapshots:
            message = "Синхронизация прошла, но пригодных пулов нет"
        elif state != "NOT_SYNCED" and not statuses.get(CandidateStatus.ACCEPT.value):
            message += "; принятых кандидатов нет — причины отсева ниже"
        return UniverseStatus(
            state=state,
            message=message,
            pool_count=len(snapshots),
            last_sync_at=last_sync,
            sync_age_seconds=age,
            decision_counts=dict(sorted(statuses.items())),
            reject_reasons=[
                ReasonCount(code=code, label=labels.label(labels.REJECTION, code), count=count)
                for code, count in reasons.most_common()
            ],
        )

    async def risk(self, daily_stop_active: bool) -> RiskUtilization:
        ctx = self._ctx
        mode = ctx.repositories.mode
        today = ctx.today()
        day = await ctx.sync(mode.risk_day, today)
        open_positions = await ctx.sync(mode.open_positions)
        pending = await ctx.sync(mode.pending_count)
        fraction = ctx.policy.risk.daily_stop_fraction
        used: Decimal | None = None
        if day is not None:
            budget = day.starting_equity * fraction
            loss = max(-day.conservative_pnl, Decimal(0))
            used = (loss / budget).quantize(Decimal("0.0001")) if budget > 0 else None
        return RiskUtilization(
            open_positions=len(open_positions),
            pending_entries=pending,
            maximum_positions=ctx.policy.risk.maximum_open_positions,
            risk_day=None if day is None else day.risk_date,
            starting_equity=None if day is None else day.starting_equity,
            conservative_pnl=None if day is None else day.conservative_pnl,
            daily_stop_fraction=fraction,
            daily_stop_used=used,
            daily_stop_active=daily_stop_active
            or (day is not None and day.daily_stop_triggered),
        )

    # --- pools and decisions ----------------------------------------------------------

    async def pools(self, *, offset: int, limit: int, query: str | None) -> Page[PoolSummary]:
        snapshots = await self._ctx.repositories.history.latest_universe()
        needle = (query or "").strip()
        selected = [
            item
            for item in snapshots
            if not needle
            or needle in item.pool.address.root
            or needle in item.pool.base_mint.root
            or needle in item.pool.quote_mint.root
        ]
        selected.sort(key=_pool_order)
        window = selected[offset : offset + limit]
        latest = await self._ctx.catalog.latest_by_pool([item.pool.address.root for item in window])
        following = offset + limit
        return Page[PoolSummary](
            items=[
                presenters.pool_summary(item, latest.get(item.pool.address.root))
                for item in window
            ],
            next_cursor=str(following) if following < len(selected) else None,
            limit=limit,
        )

    async def pool(self, address: str) -> PoolDetail:
        ctx = self._ctx
        address = parse_public_key(address)
        now = ctx.clock.now()
        history = await ctx.repositories.history.history(
            address, until=now, limit=ctx.policy.features.maximum_history_points
        )
        current = next(
            (
                item
                for item in await ctx.repositories.history.latest_universe()
                if item.pool.address.root == address
            ),
            history[-1] if history else None,
        )
        if current is None:
            raise ApiError(404, "POOL_NOT_FOUND", "Пул не найден в universe и истории")
        decision = (await ctx.catalog.latest_by_pool([address])).get(address)
        required = ctx.policy.features.minimum_baseline_points
        if decision is not None:
            features = decision.decision.features
            accumulated = features.baseline_points
            warming = features.readiness is SignalReadiness.WARMING_UP
            state = "WARMING_UP" if warming else "READY"
        else:
            accumulated = max(len(history) - 1, 0)
            state = "WARMING_UP" if accumulated < required else "NO_DECISION"
        message = {
            "WARMING_UP": (
                f"Прогрев: накоплено {accumulated} из {required} базовых окон; до готовности "
                "истории сигналов и paper-сделок нет"
            ),
            "READY": "История достаточна для расчёта сигналов",
            "NO_DECISION": "История накоплена, решения ещё не было: ждёт цикла кандидатов",
        }[state]
        return PoolDetail(
            pool=presenters.pool_summary(current, decision),
            windows={
                name: presenters.window_view(window) for name, window in current.windows.items()
            },
            depth=current.depth,
            created_at=current.created_at,
            pool_age_seconds=int(current.pool_age.total_seconds()),
            missing_fields=list(current.missing_fields),
            provider=current.provider,
            short_window_source=current.short_window_source,
            history_points=len(history),
            warmup=WarmupView(
                state=state, accumulated=accumulated, required=required, message=message
            ),
            latest_decision=None if decision is None else presenters.decision_summary(decision),
        )

    async def decisions(
        self,
        *,
        cursor: int | None,
        limit: int,
        status: CandidateStatus | None,
        pool: str | None,
    ) -> Page[DecisionSummary]:
        pool_address = None if pool is None else parse_public_key(pool)
        page = await self._ctx.catalog.page(
            DecisionQuery(limit=limit, cursor=cursor, status=status, pool_address=pool_address)
        )
        return Page[DecisionSummary](
            items=[presenters.decision_summary(record) for record in page.items],
            next_cursor=None if page.next_cursor is None else str(page.next_cursor),
            limit=limit,
        )

    async def decision(self, decision_id: UUID) -> DecisionDetail:
        record = await self._ctx.repositories.decisions.decision(decision_id)
        if record is None:
            raise ApiError(404, "DECISION_NOT_FOUND", "Решение не найдено")
        plan = await self._ctx.catalog.plan_for(decision_id)
        return presenters.decision_detail(record, plan)

    # --- positions and performance ----------------------------------------------------

    async def positions(self, *, offset: int, limit: int) -> Page[PositionSummary]:
        ctx = self._ctx
        accounting = ctx.repositories.accounting
        positions = sorted(
            await ctx.sync(accounting.positions),
            key=lambda item: (item.status is not PositionStatus.OPEN, -item.opened_at.timestamp()),
        )
        outcomes = {item.position_id: item for item in await ctx.sync(accounting.outcomes)}
        window = positions[offset : offset + limit]
        items = []
        for position in window:
            valuation = (
                await self._valuation(position) if position.status is PositionStatus.OPEN else None
            )
            items.append(
                presenters.position_summary(
                    position, valuation, outcomes.get(position.position_id)
                )
            )
        following = offset + limit
        return Page[PositionSummary](
            items=items,
            next_cursor=str(following) if following < len(positions) else None,
            limit=limit,
        )

    async def position(self, position_id: str) -> PositionDetail:
        ctx = self._ctx
        accounting = ctx.repositories.accounting
        position = await ctx.sync(accounting.position_for, position_id)
        if position is None:
            raise ApiError(404, "POSITION_NOT_FOUND", "Позиция не найдена")
        outcome = next(
            (
                item
                for item in await ctx.sync(accounting.outcomes)
                if item.position_id == position_id
            ),
            None,
        )
        valuation = (
            outcome.valuation
            if outcome is not None
            else await self._valuation(position)
        )
        context = await ctx.repositories.positions.get(position_id)
        current = await self._latest(position.pool_address)
        price = None if current is None else current.price
        range_view = None
        if context is not None:
            range_view = RangeView(
                entry_price=context.entry_price,
                lower_price=context.lower_price,
                upper_price=context.upper_price,
                min_bin_id=context.min_bin_id,
                max_bin_id=context.max_bin_id,
                current_price=price,
                in_range=None
                if price is None
                else context.lower_price <= price <= context.upper_price,
            )
        ledger = [
            entry
            for entry in await ctx.sync(accounting.ledger_entries)
            if entry.position_id == position_id
        ]
        ledger.sort(key=lambda entry: entry.occurred_at)
        return PositionDetail(
            position=presenters.position_summary(
                position, None if outcome is not None else valuation, outcome
            ),
            range=range_view,
            valuation=None if valuation is None else presenters.valuation_view(valuation),
            lifecycle=[presenters.lifecycle_view(entry) for entry in ledger],
            decision_id=None if context is None else context.decision_id,
            plan_id=None if context is None else context.plan_id,
            correlation_id=position.correlation_id,
        )

    async def performance(self, days: int) -> PerformanceView:
        ctx = self._ctx
        now = ctx.clock.now()
        window = TimeWindow(start=now - timedelta(days=days), end=now + timedelta(seconds=1))
        summary = await ctx.sync(PerformanceService(ctx.repositories.accounting).summary, window)
        open_positions = await ctx.sync(ctx.repositories.mode.open_positions)
        return PerformanceView(
            window_start=window.start,
            window_end=window.end,
            base_units_per_quote_unit=ctx.policy.quote_base_units_per_unit,
            open_positions=len(open_positions),
            groups=[presenters.performance_group_view(group) for group in summary.groups],
        )

    # --- configuration and journal ----------------------------------------------------

    async def effective_config(self) -> EffectiveConfig:
        ctx = self._ctx
        config = ctx.config
        settings: dict[str, Any] = config.redacted()
        for field in _URL_FIELDS:
            settings[field] = mask_url(settings.get(field))
        settings["executor_url"] = mask_url(ctx.environ.get("EXECUTOR_URL"))
        settings["worker_health_url"] = mask_url(ctx.settings.worker_health_url)
        settings["persistence_backend"] = ctx.repositories.backend.value
        configured = {
            name: getattr(config, field) is not None for name, field in _CONFIG_SECRETS
        }
        for name in _ENV_SECRETS:
            configured[name] = bool(ctx.environ.get(name) or ctx.environ.get(f"{name}_FILE"))
        present = {
            **configured,
            "EXECUTOR_URL": bool(ctx.environ.get("EXECUTOR_URL")),
            "SOLANA_RPC_URL": config.solana_rpc_url is not None,
            "WALLET_PUBLIC_KEY": config.wallet_public_key is not None,
            "LIVE_PROMOTION_FILE": bool(ctx.environ.get("LIVE_PROMOTION_FILE")),
        }
        approval_notes: list[Notice] = []
        if config.requested_mode is TradingMode.SHADOW:
            approval_notes.append(
                Notice(
                    code="TRADING_MODE_SHADOW",
                    message="TRADING_MODE=SHADOW: gateway для APPROVAL/LIVE не подключается",
                )
            )
        if ctx.repositories.backend is not PersistenceBackend.POSTGRESQL:
            approval_notes.append(
                Notice(
                    code="DURABLE_EXECUTION_STORE_REQUIRED",
                    message="для APPROVAL/LIVE нужен PostgreSQL (DATABASE_URL)",
                )
            )
        if not anchor_available(await ctx.repositories.history.latest_universe()):
            approval_notes.append(anchor_notice())
        live_notes = [
            *approval_notes,
            Notice(
                code="WALLET_CHECKED_BY_EXECUTOR",
                message="ключ кошелька видит только executor: его готовность — в статусе",
            ),
        ]
        readiness = [
            ModeReadinessView(mode=TradingMode.SHADOW, ready=True, missing=[], notes=[]),
            _mode_readiness(TradingMode.APPROVAL, _APPROVAL_REQUIRED, present, approval_notes),
            _mode_readiness(TradingMode.LIVE, _LIVE_REQUIRED, present, live_notes),
        ]
        policy = ctx.policy
        return EffectiveConfig(
            settings=settings,
            secrets=[
                SecretStatus(name=name, configured=value)
                for name, value in sorted(configured.items())
            ],
            readiness=readiness,
            ruleset_version=policy.ruleset_version,
            config_version=policy.config_version,
            thresholds={
                "screening": policy.screening.model_dump(mode="json"),
                "strategy": policy.strategy.model_dump(mode="json"),
                "risk": policy.risk.model_dump(mode="json"),
                "features": policy.features.model_dump(mode="json"),
                "max_slippage_bps": policy.max_slippage_bps,
                "kill_switch_policy": policy.kill_switch_policy.value,
            },
        )

    async def mode_journal(self) -> ModeJournal:
        ctx = self._ctx
        state = await ctx.mode_state()
        transitions = await ctx.sync(ctx.repositories.mode.transitions)
        return ModeJournal(
            mode=presenters.mode_view(state),
            transitions=[presenters.transition_view(item) for item in reversed(transitions)],
        )

    # --- helpers ----------------------------------------------------------------------

    async def _latest(self, pool_address: str) -> PoolSnapshot | None:
        history = await self._ctx.repositories.history.history(
            pool_address, until=self._ctx.clock.now(), limit=1
        )
        return history[-1] if history else None

    async def _valuation(self, position: ShadowPosition) -> PositionValuation | None:
        market = await self._latest(position.pool_address)
        if market is None:
            return None
        try:
            return AccountingService().mark(position, market)
        except ValueError:
            return None


def mask_url(value: object) -> str | None:
    """Scheme and host only: credentials, paths and query strings may carry API keys."""

    if not isinstance(value, str) or not value:
        return None
    try:
        parts = urlsplit(value)
        port = f":{parts.port}" if parts.port else ""
    except ValueError:
        return "[скрыто]"
    if not parts.scheme or not parts.hostname:
        return "[скрыто]"
    hidden = parts.path not in ("", "/") or parts.query or parts.username or parts.password
    return f"{parts.scheme}://{parts.hostname}{port}" + ("/[скрыто]" if hidden else "")


def _mode_readiness(
    mode: TradingMode,
    required: Sequence[str],
    present: dict[str, bool],
    notes: list[Notice],
) -> ModeReadinessView:
    missing = [name for name in required if not present.get(name, False)]
    return ModeReadinessView(
        mode=mode, ready=not missing and not notes, missing=missing, notes=notes
    )


def _pool_order(snapshot: PoolSnapshot) -> tuple[Decimal, str]:
    day = snapshot.windows.get("24h")
    volume = day.volume if day is not None else Decimal(0)
    return (-volume, snapshot.pool.address.root)
