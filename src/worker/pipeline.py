"""``CandidatePipeline.evaluate``: snapshots -> features -> screening -> plan -> size -> entry.

Every evaluation is recorded *before* any money step: the decision record (facts,
features, thresholds, reasons, versions, correlation ID) and its audit event must
persist, otherwise the cycle stops with a critical alert. Identifiers are
deterministic per (pool, decision time, ruleset, config), so a retried cycle
returns the same decision and never admits a second plan.

Live data (D07): a pool is a *candidate* only after the cheap filters - every hard
filter the snapshot and features alone can decide, run with the token not yet checked.
Only a candidate costs requests: a warming-up one gets its volume history, then token
risk (RPC) and a read-only executor quote of the reference size for depth and price
impact. The decision instant is the cycle's, moved forward to the token observation
when that arrived during the cycle (never more than ``EVALUATION_LATENCY``), so no
input is newer than the decision.
"""

from collections.abc import Awaitable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from src.core.clock import Clock
from src.domain.features import SignalReadiness, VolumeFeatureEngine, VolumeSpikeFeatures
from src.domain.models.events import EventType
from src.domain.models.trading import TradingMode
from src.domain.risk.models import LiquidityState, RiskRejection
from src.domain.risk.sizing import PositionSizer
from src.domain.screening import (
    CandidateDecision,
    CandidateStatus,
    DecisionContext,
    DecisionFlag,
    DepthEvidence,
    DepthSource,
    PoolScreener,
    RejectionCode,
    merge_pool_token_signals,
)
from src.domain.strategy import StrategyPlanner
from src.execution.contracts import QuoteDirection, QuoteRequestV1, QuoteResultV1
from src.market.contracts import OnChainProvider, VolumeHistorySource
from src.market.models import (
    AuthorityStatus,
    DataQuality,
    Freshness,
    PoolSnapshot,
    RiskSignal,
    TokenRiskSnapshot,
    VolumeBucket,
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
from src.worker.evidence import DailyEvidence
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
# A token observation this much after the cycle instant still belongs to the cycle.
EVALUATION_LATENCY = timedelta(minutes=2)
# A decision with any of these lacked data the rules need (promotion data coverage).
_DATA_GAPS = frozenset(
    {
        RejectionCode.INCOMPLETE_MARKET_DATA,
        RejectionCode.STALE_MARKET_DATA,
        RejectionCode.FUTURE_MARKET_DATA,
        RejectionCode.FUTURE_TOKEN_RISK_DATA,
        RejectionCode.TOKEN_RISK_STALE,
        RejectionCode.TOKEN_RISK_UNKNOWN,
        RejectionCode.VOLUME_INTEGRITY_UNKNOWN,
        RejectionCode.GUIDE_SIGNAL_UNKNOWN,
        RejectionCode.DEPTH_UNAVAILABLE,
    }
)
_BPS = Decimal(10_000)
_MINIMUM_IMPACT = Decimal(1) / _BPS


class QuoteSource(Protocol):
    """Read-only executor quote (``POST /v1/quotes``); raises when there is no answer."""

    async def quote(self, request: QuoteRequestV1) -> QuoteResultV1: ...


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
        quotes: QuoteSource | None = None,
        warmup: VolumeHistorySource | None = None,
        evidence: DailyEvidence | None = None,
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
        self._quotes = quotes
        self._warmup = warmup
        self._evidence = evidence
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
        engine = VolumeFeatureEngine()
        mode = self._modes.mode_state().mode
        features = engine.compute(history, policy.features, decision_at=decision_at)
        unchecked = merge_pool_token_signals(_unchecked_token(market, decision_at), market)
        candidate = self._is_candidate(market, unchecked, features, correlation)
        buckets: tuple[VolumeBucket, ...] = ()
        if candidate and features.readiness is SignalReadiness.WARMING_UP:
            buckets = await self._volume_history(market, decision_at)
            if buckets:
                features = engine.compute(
                    history, policy.features, decision_at=decision_at, warmup=buckets
                )
                candidate = self._is_candidate(market, unchecked, features, correlation)
            candidate = candidate and features.readiness is SignalReadiness.READY
        token = unchecked
        depth = _snapshot_depth(market, "NOT_A_CANDIDATE")
        if candidate:
            token = merge_pool_token_signals(await self._token_risk(market, decision_at), market)
            evaluated_at = _evaluation_instant(decision_at, token)
            if evaluated_at != decision_at:
                features = engine.compute(
                    history, policy.features, decision_at=evaluated_at, warmup=buckets
                )
            depth = await self._depth(market)
        decision = PoolScreener().evaluate(
            market,
            token,
            features,
            policy.screening,
            context=self._context(mode, correlation),
            estimated_price_impact=self._price_impact(market, depth),
            depth=depth,
        )
        decision_id = uuid5(
            NAMESPACE_URL,
            f"meteora-volume-bot:decision:{pool_address}:{decision_at.isoformat()}:"
            f"{policy.ruleset_version}:{policy.config_version}",
        )
        record = DecisionRecord(
            decision_id=decision_id,
            decision=decision,
            facts=_facts(market, features, features.decision_at),
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
        if self._evidence is not None:
            self._evidence.decision(decision_at, candidate=candidate, covered=_covered(decision))
        outcome = CandidateOutcome(
            pool_address, str(correlation), decision_id=decision_id, status=decision.status
        )
        if decision.status is not CandidateStatus.ACCEPT:
            return outcome
        return await self._plan_and_enter(
            outcome, decision, market, correlation, evaluated_event.event_id, depth
        )

    def _context(self, mode: TradingMode, correlation: UUID) -> DecisionContext:
        return DecisionContext(
            mode=mode,
            ruleset_version=self._policy.ruleset_version,
            config_version=self._policy.config_version,
            correlation_id=str(correlation),
        )

    def _is_candidate(
        self,
        market: PoolSnapshot,
        unchecked: TokenRiskSnapshot,
        features: VolumeSpikeFeatures,
        correlation: UUID,
    ) -> bool:
        """Cheap filters: no hard filter the snapshot and features decide has failed.

        Evaluated as SHADOW with the token unchecked and depth unmeasured, so only
        mode-independent, request-free reasons can reject; a reject here is final.
        """

        unmeasured = _snapshot_depth(market, "NOT_MEASURED")
        preliminary = PoolScreener().evaluate(
            market,
            unchecked,
            features,
            self._policy.screening,
            context=self._context(TradingMode.SHADOW, correlation),
            estimated_price_impact=self._price_impact(market, unmeasured),
            depth=unmeasured,
        )
        return preliminary.status is not CandidateStatus.REJECT

    async def _volume_history(
        self, market: PoolSnapshot, decision_at: datetime
    ) -> tuple[VolumeBucket, ...]:
        """Enough closed short buckets for the baseline of a warming-up candidate."""

        source = self._warmup
        if source is None:
            return ()
        rules = self._policy.features
        span = _window_minutes(rules.long_window) + _window_minutes(rules.short_window) * (
            rules.baseline_points + 2
        )
        try:
            return await source.volume_history(
                market.pool, start=decision_at - timedelta(minutes=span), end=decision_at
            )
        except ProviderError as error:
            self._log.warning("volume_history_unavailable", **error_fields(error))
            return ()

    async def _depth(self, market: PoolSnapshot) -> DepthEvidence | None:
        """Depth and price impact of the reference size from the executor's quote (D07)."""

        measured = _snapshot_depth(market, "QUOTES_NOT_CONFIGURED")
        if measured is None or self._quotes is None:
            return measured
        decimals = market.quote_decimals
        if decimals is None:
            return DepthEvidence(source=DepthSource.UNAVAILABLE, detail="QUOTE_DECIMALS_UNKNOWN")
        notional = self._policy.entry_notional_quote
        in_amount = _floor(notional * Decimal(10) ** decimals)
        if in_amount <= 0:
            return DepthEvidence(source=DepthSource.UNAVAILABLE, detail="REFERENCE_SIZE_ZERO")
        request = QuoteRequestV1(
            pool_address=market.pool.address.root,
            direction=QuoteDirection.Y_TO_X,
            in_amount=str(in_amount),
        )
        try:
            result = await self._quotes.quote(request)
        except Exception as error:
            code = getattr(error, "code", "QUOTE_FAILED")
            self._log.warning("quote_unavailable", code=code, **error_fields(error))
            return DepthEvidence(source=DepthSource.UNAVAILABLE, detail=str(code))
        if result.success and result.price_impact_bps is not None:
            impact = Decimal(result.price_impact_bps) / _BPS
            return DepthEvidence(
                source=DepthSource.QUOTE,
                depth=min(market.tvl, notional / max(impact, _MINIMUM_IMPACT)),
                price_impact=impact,
                reference_notional=notional,
                measured_at=result.quoted_at,
            )
        code = result.error.code if result.error is not None else "QUOTE_FAILED"
        if code == "INSUFFICIENT_LIQUIDITY":
            # A measured answer: the pool cannot fill the reference size at all.
            return DepthEvidence(
                source=DepthSource.QUOTE,
                reference_notional=notional,
                detail=code,
                measured_at=result.quoted_at,
            )
        return DepthEvidence(source=DepthSource.UNAVAILABLE, detail=code)

    async def _plan_and_enter(
        self,
        outcome: CandidateOutcome,
        decision: CandidateDecision,
        market: PoolSnapshot,
        correlation: UUID,
        causation_id: UUID,
        depth: DepthEvidence | None,
    ) -> CandidateOutcome:
        policy = self._policy
        now = self._clock.now()
        decision_id = outcome.decision_id
        assert decision_id is not None
        portfolio = self._portfolio.state(now)
        plan = StrategyPlanner().plan(decision, market, portfolio, policy.strategy)
        sized = PositionSizer().size(
            plan, portfolio, self._liquidity(market, depth), policy.risk
        )
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
            entry_short_volume=decision.features.current_short_volume,
            conservative=DecisionFlag.DEPTH_UNAVAILABLE in decision.flags,
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

    def _price_impact(self, market: PoolSnapshot, depth: DepthEvidence | None) -> Decimal:
        """Impact of the reference size: quoted, modeled from the snapshot depth, or the
        worst admissible one when nothing measured it (the conservative model)."""

        if depth is not None:
            if depth.source is DepthSource.UNAVAILABLE:
                return self._policy.screening.maximum_price_impact
            return depth.price_impact if depth.price_impact is not None else Decimal(1)
        if market.depth is None or market.depth <= 0:
            return Decimal(1)
        return min(Decimal(1), self._policy.entry_notional_quote / market.depth)

    def _liquidity(self, market: PoolSnapshot, depth: DepthEvidence | None) -> LiquidityState:
        if depth is None:
            absorbed = market.depth if market.depth is not None else Decimal(0)
        elif depth.source is DepthSource.UNAVAILABLE:
            absorbed = market.tvl * self._policy.conservative_depth_fraction
        else:
            absorbed = depth.depth if depth.depth is not None else Decimal(0)
        scale = self._policy.quote_base_units_per_unit
        return LiquidityState(
            depth_base_units=_floor(absorbed * scale),
            price_impact_cap_base_units=_floor(
                absorbed * self._policy.screening.maximum_price_impact * scale
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


def _window_minutes(name: str) -> int:
    return {"5m": 5, "30m": 30, "1h": 60, "4h": 240, "24h": 1_440}[name]


def _snapshot_depth(market: PoolSnapshot, detail: str) -> DepthEvidence | None:
    """``None`` when the snapshot measured depth itself; otherwise nothing measured it yet."""

    if market.depth is not None:
        return None
    return DepthEvidence(source=DepthSource.UNAVAILABLE, detail=detail)


def _unchecked_token(market: PoolSnapshot, decision_at: datetime) -> TokenRiskSnapshot:
    """The token before its risk is read: every signal unknown, nothing stale or unsafe."""

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
        freshness=Freshness.FRESH,
        data_quality=DataQuality.UNAVAILABLE,
        missing_signals=_UNKNOWN_SIGNALS,
        provider="not_checked",
    )


def _evaluation_instant(decision_at: datetime, token: TokenRiskSnapshot) -> datetime:
    """The cycle instant, or the token observation that arrived during the cycle."""

    observed = max(token.observed_at, token.source_at, token.ingested_at)
    if decision_at < observed <= decision_at + EVALUATION_LATENCY:
        return observed
    return decision_at


def _covered(decision: CandidateDecision) -> bool:
    """The decision had every signal its rules need (§7.1 data coverage)."""

    return (
        decision.status is not CandidateStatus.WARMING_UP
        and not _DATA_GAPS.intersection(decision.reasons)
        and DecisionFlag.DEPTH_UNAVAILABLE not in decision.flags
    )
