"""``CandidatePipeline.evaluate``: snapshots -> features -> screening -> plan -> size -> entry.

Every evaluation is recorded *before* any money step: the decision record (facts,
features, thresholds, reasons, versions, correlation ID) and its audit event must
persist, otherwise the cycle stops with a critical alert. Identifiers are
deterministic per (pool, decision time, ruleset, config), so a retried cycle
returns the same decision and never admits a second plan.
"""

from collections.abc import Awaitable
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import ROUND_FLOOR, Decimal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from src.core.clock import Clock
from src.domain.features import VolumeFeatureEngine, VolumeSpikeFeatures
from src.domain.models.events import EventType
from src.domain.risk.models import LiquidityState, RiskRejection
from src.domain.risk.sizing import PositionSizer
from src.domain.screening import CandidateDecision, CandidateStatus, DecisionContext, PoolScreener
from src.domain.strategy import StrategyPlanner
from src.market.contracts import OnChainProvider
from src.market.models import (
    AuthorityStatus,
    DataQuality,
    Freshness,
    PoolSnapshot,
    RiskSignal,
    TokenRiskSnapshot,
)
from src.modes.repository import ModeRepository
from src.modes.service import EntryGate
from src.observability.alerts import CriticalAlertReporter
from src.observability.logging import cycle_context, error_fields, get_logger
from src.observability.metrics import WorkerMetrics
from src.persistence.contracts import (
    AuditLog,
    DecisionFacts,
    DecisionRecord,
    DecisionStore,
    PlanRecord,
    SnapshotHistory,
)
from src.providers.contracts import ProviderError
from src.worker.execution import EntryExecutor, EntryResult, audit_event
from src.worker.policy import PipelinePolicy
from src.worker.portfolio import ShadowPortfolio

_UNKNOWN_SIGNALS = (
    "blacklisted",
    "freeze_authority",
    "holder_concentration",
    "mint_authority",
    "verified",
    "wash_trading",
)


@dataclass(frozen=True, slots=True)
class CandidateOutcome:
    pool_address: str
    correlation_id: str
    decision_id: UUID | None = None
    status: CandidateStatus | None = None
    plan_id: UUID | None = None
    entry: EntryResult | None = None
    reasons: tuple[str, ...] = ()


class PersistenceFailure(RuntimeError):
    """A record that must precede any money step could not be persisted."""


class CandidatePipeline:
    def __init__(
        self,
        *,
        history: SnapshotHistory,
        onchain: OnChainProvider,
        decisions: DecisionStore,
        modes: ModeRepository,
        entries: EntryExecutor,
        portfolio: ShadowPortfolio,
        audit: AuditLog,
        alerts: CriticalAlertReporter,
        policy: PipelinePolicy,
        clock: Clock,
        metrics: WorkerMetrics,
    ) -> None:
        self._history = history
        self._onchain = onchain
        self._decisions = decisions
        self._modes = modes
        self._entries = entries
        self._portfolio = portfolio
        self._audit = audit
        self._alerts = alerts
        self._policy = policy
        self._clock = clock
        self._metrics = metrics
        self._log = get_logger("worker.candidates")

    async def evaluate(
        self,
        pool_address: str,
        observed_at: datetime,
        *,
        correlation_id: UUID | None = None,
        causation_id: UUID | None = None,
    ) -> CandidateOutcome:
        correlation = correlation_id or uuid4()
        with cycle_context(
            correlation_id=correlation, causation_id=causation_id, pool_address=pool_address
        ):
            try:
                return await self._evaluate(pool_address, observed_at, correlation, causation_id)
            except PersistenceFailure as failure:
                return CandidateOutcome(pool_address, str(correlation), reasons=(str(failure),))

    async def _evaluate(
        self,
        pool_address: str,
        decision_at: datetime,
        correlation: UUID,
        causation_id: UUID | None,
    ) -> CandidateOutcome:
        policy = self._policy
        history = await self._history.history(
            pool_address, until=decision_at, limit=policy.features.maximum_history_points
        )
        if not history:
            return CandidateOutcome(pool_address, str(correlation), reasons=("NO_HISTORY",))
        market = history[-1]
        token = await self._token_risk(market, decision_at)
        features = VolumeFeatureEngine().compute(history, policy.features, decision_at=decision_at)
        mode = self._modes.mode_state().mode
        decision = PoolScreener().evaluate(
            market,
            token,
            features,
            policy.screening,
            context=DecisionContext(
                mode=mode,
                ruleset_version=policy.ruleset_version,
                config_version=policy.config_version,
                correlation_id=str(correlation),
            ),
            estimated_price_impact=self._price_impact(market),
        )
        decision_id = uuid5(
            NAMESPACE_URL,
            f"meteora-volume-bot:decision:{pool_address}:{decision_at.isoformat()}:"
            f"{policy.ruleset_version}:{policy.config_version}",
        )
        record = DecisionRecord(
            decision_id=decision_id,
            decision=decision,
            facts=_facts(market, features, decision_at),
            market=market,
            token=token,
            thresholds=policy.screening,
            recorded_at=self._clock.now(),
        )
        evaluated_event = audit_event(
            EventType.CANDIDATE_EVALUATED,
            self._clock.now(),
            decision_id,
            correlation,
            causation_id,
            {
                "decision_id": str(decision_id),
                "pool_address": pool_address,
                "status": decision.status.value,
                "score": None if decision.score is None else str(decision.score),
                "reasons": [reason.value for reason in decision.reasons],
                "missing_signals": list(decision.missing_signals),
                "ruleset_version": decision.ruleset_version,
                "config_version": decision.config_version,
            },
        )
        await self._persist("DECISION_PERSISTENCE_FAILED", self._decisions.record_decision(record))
        await self._persist("AUDIT_PERSISTENCE_FAILED", self._audit.append(evaluated_event))
        self._metrics.candidate(
            decision.status.value, (reason.value for reason in decision.reasons)
        )
        outcome = CandidateOutcome(
            pool_address, str(correlation), decision_id=decision_id, status=decision.status
        )
        if decision.status is not CandidateStatus.ACCEPT:
            return outcome
        return await self._plan_and_enter(
            outcome, decision, market, correlation, evaluated_event.event_id
        )

    async def _plan_and_enter(
        self,
        outcome: CandidateOutcome,
        decision: CandidateDecision,
        market: PoolSnapshot,
        correlation: UUID,
        causation_id: UUID,
    ) -> CandidateOutcome:
        policy = self._policy
        now = self._clock.now()
        decision_id = outcome.decision_id
        assert decision_id is not None
        portfolio = self._portfolio.state(now)
        plan = StrategyPlanner().plan(decision, market, portfolio, policy.strategy)
        sized = PositionSizer().size(plan, portfolio, self._liquidity(market), policy.risk)
        plan_id = uuid5(NAMESPACE_URL, f"meteora-volume-bot:plan:{decision_id}")
        if isinstance(sized, RiskRejection):
            reasons = tuple(reason.value for reason in sized.reasons)
            await self._audit_rejection(plan_id, correlation, causation_id, reasons)
            return replace(outcome, plan_id=plan_id, reasons=reasons)
        try:
            entry = EntryGate(self._modes, self._clock, policy.risk).admit(sized, plan_id)
        except ValueError as error:
            if "already admitted" in str(error):
                return replace(outcome, plan_id=plan_id, reasons=("PLAN_ALREADY_ADMITTED",))
            raise
        plan_record = PlanRecord(
            plan_id=plan_id,
            decision_id=decision_id,
            sized_plan=sized,
            admitted=entry.admitted,
            admission_reasons=entry.reasons,
            mode=decision.mode,
            recorded_at=now,
        )
        try:
            await self._persist("PLAN_PERSISTENCE_FAILED", self._decisions.record_plan(plan_record))
        except PersistenceFailure:
            if entry.admitted:
                self._modes.cancel_plan(plan_id)
            raise
        if not entry.admitted:
            await self._audit_rejection(plan_id, correlation, causation_id, entry.reasons)
            return replace(outcome, plan_id=plan_id, reasons=entry.reasons)
        planned = audit_event(
            EventType.STRATEGY_PLANNED,
            now,
            plan_id,
            correlation,
            causation_id,
            {
                "plan_id": str(plan_id),
                "decision_id": str(decision_id),
                "strategy": sized.plan.strategy.value,
                "size_base_units": sized.size_base_units,
                "estimated_loss_base_units": sized.estimated_loss_base_units,
                "binding_caps": [reason.value for reason in sized.binding_caps],
                "expires_at": sized.plan.expires_at.isoformat(),
            },
        )
        try:
            await self._persist("AUDIT_PERSISTENCE_FAILED", self._audit.append(planned))
        except PersistenceFailure:
            self._modes.cancel_plan(plan_id)
            raise
        result = await self._entries.execute(
            plan_id=plan_id,
            decision_id=decision_id,
            sized=sized,
            market=market,
            correlation_id=correlation,
            causation_id=planned.event_id,
        )
        reasons = () if result.reason is None else (result.reason,)
        return replace(outcome, plan_id=plan_id, entry=result, reasons=reasons)

    async def _persist(self, code: str, write: Awaitable[object]) -> None:
        try:
            await write
        except Exception as error:
            self._log.critical("persistence_failed", code=code, **error_fields(error))
            self._alerts.raise_alert(code, "a pre-execution record could not be persisted")
            raise PersistenceFailure(code) from error

    async def _audit_rejection(
        self,
        plan_id: UUID,
        correlation: UUID,
        causation_id: UUID,
        reasons: tuple[str, ...],
    ) -> None:
        await self._persist(
            "AUDIT_PERSISTENCE_FAILED",
            self._audit.append(
                audit_event(
                    EventType.RISK_REJECTED,
                    self._clock.now(),
                    plan_id,
                    correlation,
                    causation_id,
                    {"stage": "admission", "plan_id": str(plan_id), "reasons": list(reasons)},
                )
            ),
        )

    async def _token_risk(self, market: PoolSnapshot, decision_at: datetime) -> TokenRiskSnapshot:
        try:
            return await self._onchain.get_token_risk(market.pool.base_mint)
        except ProviderError as error:
            self._log.warning("token_risk_unavailable", **error_fields(error))
            return TokenRiskSnapshot(
                mint=market.pool.base_mint,
                mint_authority=AuthorityStatus.UNKNOWN,
                freeze_authority=AuthorityStatus.UNKNOWN,
                blacklisted=RiskSignal.UNKNOWN,
                verified=RiskSignal.UNKNOWN,
                holder_concentration=None,
                wash_trading=RiskSignal.UNKNOWN,
                observed_at=decision_at,
                source_at=decision_at,
                ingested_at=decision_at,
                freshness=Freshness.STALE,
                data_quality=DataQuality.UNAVAILABLE,
                missing_signals=_UNKNOWN_SIGNALS,
                provider="unavailable",
            )

    def _price_impact(self, market: PoolSnapshot) -> Decimal:
        if market.depth is None or market.depth <= 0:
            return Decimal(1)
        return min(Decimal(1), self._policy.entry_notional_quote / market.depth)

    def _liquidity(self, market: PoolSnapshot) -> LiquidityState:
        depth = market.depth if market.depth is not None else Decimal(0)
        scale = self._policy.quote_base_units_per_unit
        return LiquidityState(
            depth_base_units=_floor(depth * scale),
            price_impact_cap_base_units=_floor(
                depth * self._policy.screening.maximum_price_impact * scale
            ),
        )


def _facts(
    market: PoolSnapshot, features: VolumeSpikeFeatures, decision_at: datetime
) -> DecisionFacts:
    day = market.windows.get("24h")
    return DecisionFacts(
        pool_address=market.pool.address.root,
        volume_short=features.current_short_volume,
        volume_long=features.current_long_volume,
        volume_24h=day.volume if day is not None else None,
        tvl=market.tvl,
        fee_tvl=features.fee_tvl,
        price=market.price,
        price_volatility=features.price_volatility,
        pool_age_seconds=int(market.pool_age.total_seconds()),
        freshness=features.freshness,
        source_age_seconds=int((decision_at - features.source_at).total_seconds()),
        observed_at=market.observed_at,
    )


def _floor(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_FLOOR))
