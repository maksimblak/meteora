"""Domain records -> presentation models. Pure functions, no I/O."""

from collections.abc import Iterable, Sequence
from decimal import Decimal, InvalidOperation

from src.accounting.models import LedgerEntry
from src.api import labels
from src.api.schemas import (
    AlertView,
    DecisionDetail,
    DecisionSummary,
    EvidenceView,
    FactsView,
    KillSwitchStateView,
    LifecycleStepView,
    MetricView,
    ModeTransitionView,
    ModeView,
    Notice,
    PerformanceGroupView,
    PoolSummary,
    PositionSummary,
    ScoreComponentView,
    StrategyView,
    TokenRiskView,
    ValuationView,
    WindowView,
)
from src.domain.screening.models import EvidenceItem
from src.market.models import PoolSnapshot, TokenRiskSnapshot, WindowMetrics
from src.modes.models import CriticalAlert, ModeState, ModeTransition
from src.persistence.contracts import DecisionRecord, PlanRecord
from src.portfolio.models import (
    PerformanceGroup,
    PositionOutcome,
    PositionValuation,
    ShadowPosition,
)

# Evidence row -> the rejection code its threshold produces (spec 6.3, 6.6).
_TRIGGERS: dict[str, frozenset[str]] = {
    "market_cap": frozenset({"MARKET_CAP_BELOW_THRESHOLD"}),
    "token_age_seconds": frozenset({"TOKEN_TOO_YOUNG"}),
    "top10_holder_share": frozenset({"HOLDER_CONCENTRATION_HIGH"}),
    "volume_per_minute": frozenset({"VOLUME_PER_MINUTE_BELOW_THRESHOLD"}),
    "volume_to_liquidity": frozenset({"VOLUME_TO_LIQUIDITY_BELOW_THRESHOLD"}),
    "total_fees_sol": frozenset({"TOTAL_FEES_BELOW_THRESHOLD"}),
    # Selection as the author does it (G24, T18); fee organicity no longer decides anything.
    "hourly_volume_per_minute": frozenset({"HOURLY_VOLUME_PER_MINUTE_BELOW_THRESHOLD"}),
    "theme": frozenset({"THEME_EXCLUDED"}),
    "token_volume_rank": frozenset({"CANDIDATE_RANK_BELOW_LIMIT"}),
    "fee_tvl": frozenset({"FEE_TVL_BELOW_THRESHOLD"}),
    "developer_share": frozenset({"DEVELOPER_HOLDS_TOKENS"}),
    "tvl": frozenset({"LOW_TVL"}),
    "depth": frozenset({"LOW_DEPTH"}),
    "price_impact": frozenset({"PRICE_IMPACT_HIGH"}),
    "volume_integrity": frozenset({"ARTIFICIAL_VOLUME_CONFIRMED", "VOLUME_INTEGRITY_UNKNOWN"}),
    # GMGN holder structure (spec 6.7): each metric row is marked by its own code only.
    "insiders_share": frozenset({"INSIDERS_SHARE_HIGH"}),
    "bundling_share": frozenset({"BUNDLING_SHARE_HIGH"}),
    "phishing_share": frozenset({"PHISHING_SHARE_HIGH"}),
    "gmgn_top10_holder_share": frozenset({"GMGN_TOP10_HOLDER_SHARE_HIGH"}),
    "gmgn_status": frozenset({"GMGN_UNAVAILABLE"}),
    "depth_source": frozenset({"DEPTH_UNAVAILABLE"}),
}
# shadow-hypothesis-v1: several minimums share one code; the row that fell short of its own
# threshold is marked. guide-v1 (G24) has no such reason: the spike is the VOLUME_SPIKE flag
# and fee/TVL has its own code (``_TRIGGERS``).
_SPIKE_MINIMUMS = frozenset({"short_ratio", "long_ratio", "robust_z_score", "fee_tvl"})
_SPIKE_CODE = "VOLUME_SIGNAL_BELOW_THRESHOLD"
# ARTIFICIAL_VOLUME_SUSPECTED (G24): total fees below what the cap calls for at a tenfold
# hourly turnover marks these rows; GMGN's wash-trading verdict marks its own row.
_ARTIFICIAL_CODE = "ARTIFICIAL_VOLUME_SUSPECTED"
_ARTIFICIAL_FEES_ROWS = frozenset({"total_fees_sol", "required_total_fees_sol"})
_TURNOVER_ROW = "pool_hourly_turnover"
_WASH_ROW = "gmgn_wash_trading"
# GMGN_UNAVAILABLE (G18, G24): the status row when the reading is not fresh; with a fresh
# reading, the threshold metrics it lacks - the developer's share is one of them.
_GMGN_UNAVAILABLE = "GMGN_UNAVAILABLE"
_GMGN_STATUS_ROW = "gmgn_status"
_GMGN_REQUIRED_ROWS = frozenset(
    {
        "insiders_share",
        "bundling_share",
        "phishing_share",
        "gmgn_top10_holder_share",
        "developer_share",
    }
)
_FRESH = "FRESH"
_UNAVAILABLE = "UNAVAILABLE"
# A token without a name or ticker: its theme could not be checked (flag THEME_UNKNOWN in
# SHADOW, the missing signal ``theme`` behind GUIDE_SIGNAL_UNKNOWN in APPROVAL and LIVE).
_THEME_ROW = "theme"
_UNKNOWN = "UNKNOWN"
_GUIDE_UNKNOWN = "GUIDE_SIGNAL_UNKNOWN"
_THEME_UNKNOWN = "THEME_UNKNOWN"
# GMGN metrics (G18, spec 6.7); top-10 and total fees join them once GMGN reports any.
_GMGN_MARKERS = ("gmgn", "insider", "bundl", "phish", "developer", "dev_")
_GMGN_COMPANIONS = ("top10_holder_share", "total_fees_sol")


def notice(table: dict[str, str], code: str) -> Notice:
    return Notice(code=code, message=labels.label(table, code))


def readiness_notices(codes: Iterable[str]) -> list[Notice]:
    return [Notice(code=code, message=labels.readiness_label(code)) for code in codes]


def flag_notices(codes: Iterable[str]) -> list[Notice]:
    return [notice(labels.FLAG_NOTICES, code) for code in codes]


def mode_view(state: ModeState) -> ModeView:
    return ModeView(
        mode=state.mode,
        label=labels.label(labels.MODE, state.mode.value),
        version=state.version,
        promotion_authorized=state.promotion_authorized,
        kill_switch_active=state.kill_switch_active,
        daily_stop_date=state.daily_stop_date,
    )


def kill_switch_view(state: ModeState) -> KillSwitchStateView:
    policy = state.active_kill_switch_policy
    if policy is None:
        text = "выключен; политику открытых позиций оператор выбирает при включении"
    else:
        text = f"активен: {labels.label(labels.KILL_SWITCH_POLICY, policy.value)}"
    return KillSwitchStateView(active=state.kill_switch_active, policy=policy, label=text)


def alert_view(alert: CriticalAlert) -> AlertView:
    return AlertView(
        alert_id=alert.alert_id,
        code=alert.code,
        label=labels.alert_label(alert.code),
        message=alert.message,
        created_at=alert.created_at,
        open=alert.open,
    )


def transition_view(transition: ModeTransition) -> ModeTransitionView:
    return ModeTransitionView(
        transition_id=transition.transition_id,
        from_mode=transition.from_mode,
        to_mode=transition.to_mode,
        occurred_at=transition.occurred_at,
        promotion=transition.promotion_authorized,
        reason=transition.reason,
        evidence_id=transition.evidence_id,
    )


def window_view(window: WindowMetrics) -> WindowView:
    return WindowView(
        volume=window.volume,
        fees=window.fees,
        transaction_count=window.transaction_count,
        unique_participants=window.unique_participants,
    )


def pool_summary(snapshot: PoolSnapshot, decision: DecisionRecord | None) -> PoolSummary:
    day = snapshot.windows.get("24h")
    status = None if decision is None else decision.decision.status.value
    return PoolSummary(
        address=snapshot.pool.address.root,
        base_mint=snapshot.pool.base_mint.root,
        quote_mint=snapshot.pool.quote_mint.root,
        tvl=snapshot.tvl,
        volume_24h=None if day is None else day.volume,
        price=snapshot.price,
        bin_step=snapshot.bin_step,
        freshness=snapshot.freshness.value,
        data_quality=snapshot.data_quality.value,
        observed_at=snapshot.observed_at,
        decision_id=None if decision is None else decision.decision_id,
        decision_status=status,
        decision_label=None if status is None else labels.label(labels.CANDIDATE_STATUS, status),
    )


def decision_summary(record: DecisionRecord) -> DecisionSummary:
    decision = record.decision
    return DecisionSummary(
        decision_id=record.decision_id,
        pool_address=decision.pool_address.root,
        token_mint=decision.token_mint.root,
        status=decision.status.value,
        status_label=labels.label(labels.CANDIDATE_STATUS, decision.status.value),
        score=decision.score,
        reasons=[notice(labels.REJECTION, reason.value) for reason in decision.reasons],
        missing_signals=list(decision.missing_signals),
        flags=flag_notices(flag.value for flag in decision.flags),
        decided_at=decision.decision_at,
        mode=decision.mode,
        ruleset_version=decision.ruleset_version,
        config_version=decision.config_version,
        freshness=decision.features.freshness.value,
        correlation_id=decision.correlation_id,
    )


def evidence_views(
    items: Sequence[EvidenceItem], reasons: Iterable[str], flags: Iterable[str] = ()
) -> list[EvidenceView]:
    """Rows with the code that marks them; ``flags`` mark the rows a flag is about (the theme
    a nameless token could not be checked against)."""

    fired = set(reasons)
    raised = set(flags)
    turnover = next((item for item in items if item.name == _TURNOVER_ROW), None)
    cap_rule = (
        _ARTIFICIAL_CODE in fired and turnover is not None and _at_least_threshold(turnover)
    )
    status = next((item for item in items if item.name == _GMGN_STATUS_ROW), None)
    # A fresh GMGN reading is "unavailable" only for the metrics it lacks (G24: an unknown
    # developer share too); then those rows are marked, not the fresh status.
    gmgn_gaps = (
        _GMGN_UNAVAILABLE in fired and status is not None and status.value == _FRESH
    )
    return [
        EvidenceView(
            name=item.name,
            value=item.value,
            threshold=item.threshold,
            triggered=_triggered(item, fired, raised, cap_rule=cap_rule, gmgn_gaps=gmgn_gaps),
            note=labels.evidence_note(item.name, item.value, item.threshold),
        )
        for item in items
    ]


def gmgn_metrics(items: Sequence[EvidenceItem]) -> list[MetricView]:
    """GMGN metrics next to their thresholds, only when GMGN reported any (G18)."""

    def is_gmgn(name: str) -> bool:
        lowered = name.lower()
        return any(marker in lowered for marker in _GMGN_MARKERS)

    if not any(is_gmgn(item.name) for item in items):
        return []
    return [
        MetricView(name=item.name, value=item.value, threshold=item.threshold)
        for item in items
        if is_gmgn(item.name) or item.name in _GMGN_COMPANIONS
    ]


def decision_detail(record: DecisionRecord, plan: PlanRecord | None) -> DecisionDetail:
    decision = record.decision
    reasons = [reason.value for reason in decision.reasons]
    facts = record.facts
    return DecisionDetail(
        decision=decision_summary(record),
        evidence=evidence_views(
            decision.evidence, reasons, (flag.value for flag in decision.flags)
        ),
        score_components=[
            ScoreComponentView(
                name=component.name,
                normalized_value=component.normalized_value,
                weight=component.weight,
                contribution=component.contribution,
            )
            for component in decision.score_components
        ],
        gmgn=gmgn_metrics(decision.evidence),
        facts=FactsView(
            volume_short=facts.volume_short,
            volume_long=facts.volume_long,
            volume_24h=facts.volume_24h,
            tvl=facts.tvl,
            fee_tvl=facts.fee_tvl,
            price=facts.price,
            price_volatility=facts.price_volatility,
            pool_age_seconds=facts.pool_age_seconds,
            freshness=facts.freshness.value,
            source_age_seconds=facts.source_age_seconds,
            observed_at=facts.observed_at,
        ),
        token=token_view(record.token),
        volume_window_source=decision.features.volume_window_source,
        strategy=None if plan is None else strategy_view(plan),
    )


def token_view(token: TokenRiskSnapshot) -> TokenRiskView:
    return TokenRiskView(
        mint=token.mint.root,
        mint_authority=token.mint_authority.value,
        freeze_authority=token.freeze_authority.value,
        blacklisted=token.blacklisted.value,
        verified=token.verified.value,
        wash_trading=token.wash_trading.value,
        holder_concentration=token.holder_concentration,
        total_fees_sol=token.total_fees_sol,
        freshness=token.freshness.value,
        data_quality=token.data_quality.value,
        missing_signals=list(token.missing_signals),
        provider=token.provider,
    )


def strategy_view(record: PlanRecord) -> StrategyView:
    sized = record.sized_plan
    plan = sized.plan
    reason = None if plan.selection_reason is None else plan.selection_reason.value
    return StrategyView(
        plan_id=record.plan_id,
        strategy=plan.strategy.value,
        direction=plan.direction.value,
        selection_reason=reason,
        selection_label=None if reason is None else labels.label(labels.STRATEGY_SELECTION, reason),
        selection_evidence=evidence_views(plan.selection_evidence, ()),
        lower_bin_offset=plan.lower_bin_offset,
        upper_bin_offset=plan.upper_bin_offset,
        holding_horizon_seconds=int(plan.holding_horizon.total_seconds()),
        expires_at=plan.expires_at,
        size_base_units=sized.size_base_units,
        estimated_loss_base_units=sized.estimated_loss_base_units,
        binding_caps=[cap.value for cap in sized.binding_caps],
        binding_cap_notices=[
            Notice(code=cap.value, message=labels.admission_label(cap.value))
            for cap in sized.binding_caps
        ],
        admitted=record.admitted,
        admission_reasons=list(record.admission_reasons),
        admission_notices=[
            Notice(code=code, message=labels.admission_label(code))
            for code in record.admission_reasons
        ],
    )


def valuation_view(valuation: PositionValuation) -> ValuationView:
    return ValuationView(
        valued_at=valuation.valued_at,
        market_price=valuation.market_price,
        invested_base_units=valuation.invested_base_units,
        liquidity_value_base_units=valuation.liquidity_value_base_units,
        hodl_value_base_units=valuation.hodl_value_base_units,
        fee_income_base_units=valuation.fee_income_base_units,
        divergence_loss_base_units=valuation.divergence_loss_base_units,
        slippage_base_units=valuation.slippage_base_units,
        network_base_units=valuation.network_base_units,
        jito_base_units=valuation.jito_base_units,
        total_costs_base_units=valuation.total_costs_base_units,
        realized_pnl_base_units=valuation.realized_pnl_base_units,
        unrealized_pnl_base_units=valuation.unrealized_pnl_base_units,
        net_pnl_base_units=valuation.net_pnl_base_units,
        hodl_pnl_base_units=valuation.hodl_pnl_base_units,
        excess_vs_hodl_base_units=valuation.excess_vs_hodl_base_units,
    )


def position_summary(
    position: ShadowPosition,
    valuation: PositionValuation | None,
    outcome: PositionOutcome | None,
) -> PositionSummary:
    chosen = outcome.valuation if outcome is not None else valuation
    source = "OUTCOME" if outcome is not None else "MARKET" if valuation is not None else None
    return PositionSummary(
        position_id=position.position_id,
        pool_address=position.pool_address,
        status=position.status.value,
        mode=position.mode,
        ruleset_version=position.ruleset_version,
        opened_at=position.opened_at,
        closed_at=position.closed_at,
        invested_base_units=position.invested_base_units,
        net_pnl_base_units=None if chosen is None else chosen.net_pnl_base_units,
        hodl_pnl_base_units=None if chosen is None else chosen.hodl_pnl_base_units,
        excess_vs_hodl_base_units=None if chosen is None else chosen.excess_vs_hodl_base_units,
        valuation_source=source or "UNAVAILABLE",
    )


def lifecycle_view(entry: LedgerEntry) -> LifecycleStepView:
    return LifecycleStepView(
        kind=entry.kind.value,
        occurred_at=entry.occurred_at,
        price=entry.price,
        capital_delta_base_units=entry.capital_delta_base_units,
        realized_pnl_base_units=entry.realized_pnl_base_units,
        fee_income_base_units=entry.fee_income_base_units,
        slippage_base_units=entry.slippage_base_units,
        network_base_units=entry.network_base_units,
        jito_base_units=entry.jito_base_units,
    )


def performance_group_view(group: PerformanceGroup) -> PerformanceGroupView:
    return PerformanceGroupView(**group.model_dump())


def _triggered(
    item: EvidenceItem,
    fired: set[str],
    raised: set[str],
    *,
    cap_rule: bool = False,
    gmgn_gaps: bool = False,
) -> bool:
    if item.name in _SPIKE_MINIMUMS and _SPIKE_CODE in fired:
        value, threshold = _decimal(item.value), _decimal(item.threshold)
        if value is None or (threshold is not None and value < threshold):
            return True
    if cap_rule and (item.name in _ARTIFICIAL_FEES_ROWS or item.name == _TURNOVER_ROW):
        return True
    if item.name == _WASH_ROW:
        return _ARTIFICIAL_CODE in fired and item.value == "UNSAFE"
    if gmgn_gaps and item.name in _GMGN_REQUIRED_ROWS and item.value == _UNAVAILABLE:
        return True
    if gmgn_gaps and item.name == _GMGN_STATUS_ROW:
        return False
    if item.name == _THEME_ROW and item.value == _UNKNOWN and (
        _GUIDE_UNKNOWN in fired or _THEME_UNKNOWN in raised
    ):
        return True
    return bool(_TRIGGERS.get(item.name, frozenset()) & fired)


def _at_least_threshold(item: EvidenceItem) -> bool:
    value, threshold = _decimal(item.value), _decimal(item.threshold)
    return value is not None and threshold is not None and value >= threshold


def _decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None
