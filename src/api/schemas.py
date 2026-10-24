"""Versioned presentation models of ``/api/v1``.

They are built from domain records by ``src.api.presenters`` and never embed ORM rows,
provider payloads or SDK objects. Money stays ``Decimal`` (JSON strings) and every
timestamp is timezone-aware UTC.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from src.domain.models.trading import TradingMode
from src.modes.models import KillSwitchPolicy

Digest = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
Token = Annotated[str, StringConstraints(max_length=512)]


class ApiModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Notice(ApiModel):
    code: str
    message: str


# --- status ---------------------------------------------------------------------------


class ModeView(ApiModel):
    mode: TradingMode
    label: str
    version: int
    promotion_authorized: bool
    kill_switch_active: bool
    daily_stop_date: date | None


class ServiceHealth(ApiModel):
    name: str
    status: Literal["OK", "DEGRADED", "DOWN", "NOT_CONFIGURED"]
    reasons: list[Notice]


class ReasonCount(ApiModel):
    code: str
    label: str
    count: int


class UniverseStatus(ApiModel):
    state: Literal["NOT_SYNCED", "STALE", "FRESH"]
    message: str
    pool_count: int
    last_sync_at: datetime | None
    sync_age_seconds: int | None
    decision_counts: dict[str, int]
    reject_reasons: list[ReasonCount]


class RiskUtilization(ApiModel):
    open_positions: int
    pending_entries: int
    maximum_positions: int
    risk_day: date | None
    starting_equity: Decimal | None
    conservative_pnl: Decimal | None
    daily_stop_fraction: Decimal
    daily_stop_used: Decimal | None
    daily_stop_active: bool


class AlertView(ApiModel):
    alert_id: UUID
    code: str
    label: str = ""
    message: str
    created_at: datetime
    open: bool


class StatusPulse(ApiModel):
    """Light status pushed over SSE (``event: status``) to refresh open pages."""

    generated_at: datetime
    mode: ModeView
    open_alerts: int
    approvals_pending: int
    universe_state: Literal["NOT_SYNCED", "STALE", "FRESH"]
    last_sync_at: datetime | None


class SystemStatus(ApiModel):
    generated_at: datetime
    mode: ModeView
    services: list[ServiceHealth]
    universe: UniverseStatus
    risk: RiskUtilization
    alerts: list[AlertView]
    approvals_pending: int
    readiness: list[Notice]
    ruleset_version: str
    config_version: str


# --- pools and decisions --------------------------------------------------------------


class WindowView(ApiModel):
    volume: Decimal
    fees: Decimal | None
    transaction_count: int | None
    unique_participants: int | None


class PoolSummary(ApiModel):
    address: str
    base_mint: str
    quote_mint: str
    tvl: Decimal
    volume_24h: Decimal | None
    price: Decimal
    bin_step: int
    freshness: str
    data_quality: str
    observed_at: datetime
    decision_id: UUID | None
    decision_status: str | None
    decision_label: str | None


class WarmupView(ApiModel):
    state: Literal["WARMING_UP", "READY", "NO_DECISION"]
    accumulated: int
    required: int
    message: str


class EvidenceView(ApiModel):
    name: str
    value: str
    threshold: str | None
    triggered: bool
    # Russian explanation of a code in the row (GMGN status, why depth was not measured).
    note: str | None = None


class MetricView(ApiModel):
    name: str
    value: str
    threshold: str | None


class DecisionSummary(ApiModel):
    decision_id: UUID
    pool_address: str
    token_mint: str
    status: str
    status_label: str
    score: Decimal | None
    reasons: list[Notice]
    missing_signals: list[str]
    flags: list[Notice]
    decided_at: datetime
    mode: TradingMode
    ruleset_version: str
    config_version: str
    freshness: str
    correlation_id: str


class PoolDetail(ApiModel):
    pool: PoolSummary
    windows: dict[str, WindowView]
    depth: Decimal | None
    created_at: datetime
    pool_age_seconds: int
    missing_fields: list[str]
    provider: str
    short_window_source: str | None
    history_points: int
    warmup: WarmupView
    latest_decision: DecisionSummary | None


class ScoreComponentView(ApiModel):
    name: str
    normalized_value: Decimal
    weight: Decimal
    contribution: Decimal


class FactsView(ApiModel):
    volume_short: Decimal | None
    volume_long: Decimal | None
    volume_24h: Decimal | None
    tvl: Decimal
    fee_tvl: Decimal | None
    price: Decimal
    price_volatility: Decimal | None
    pool_age_seconds: int
    freshness: str
    source_age_seconds: int
    observed_at: datetime


class TokenRiskView(ApiModel):
    mint: str
    mint_authority: str
    freeze_authority: str
    blacklisted: str
    verified: str
    wash_trading: str
    holder_concentration: Decimal | None
    total_fees_sol: Decimal | None
    freshness: str
    data_quality: str
    missing_signals: list[str]
    provider: str


class StrategyView(ApiModel):
    plan_id: UUID
    strategy: str
    direction: str
    selection_reason: str | None
    selection_label: str | None
    selection_evidence: list[EvidenceView]
    lower_bin_offset: int
    upper_bin_offset: int
    holding_horizon_seconds: int
    expires_at: datetime
    size_base_units: int
    estimated_loss_base_units: int
    binding_caps: list[str]
    binding_cap_notices: list[Notice] = []
    admitted: bool
    admission_reasons: list[str]
    admission_notices: list[Notice] = []


class DecisionDetail(ApiModel):
    decision: DecisionSummary
    evidence: list[EvidenceView]
    score_components: list[ScoreComponentView]
    gmgn: list[MetricView]
    facts: FactsView
    token: TokenRiskView
    volume_window_source: str | None
    strategy: StrategyView | None


# --- positions and performance --------------------------------------------------------


class RangeView(ApiModel):
    entry_price: Decimal
    lower_price: Decimal
    upper_price: Decimal
    min_bin_id: int
    max_bin_id: int
    current_price: Decimal | None
    in_range: bool | None


class ValuationView(ApiModel):
    valued_at: datetime
    market_price: Decimal
    invested_base_units: Decimal
    liquidity_value_base_units: Decimal
    hodl_value_base_units: Decimal
    fee_income_base_units: Decimal
    divergence_loss_base_units: Decimal
    slippage_base_units: Decimal
    network_base_units: Decimal
    jito_base_units: Decimal
    total_costs_base_units: Decimal
    realized_pnl_base_units: Decimal
    unrealized_pnl_base_units: Decimal
    net_pnl_base_units: Decimal
    hodl_pnl_base_units: Decimal
    excess_vs_hodl_base_units: Decimal


class PositionSummary(ApiModel):
    position_id: str
    pool_address: str
    status: str
    mode: TradingMode
    ruleset_version: str
    opened_at: datetime
    closed_at: datetime | None
    invested_base_units: Decimal
    net_pnl_base_units: Decimal | None
    hodl_pnl_base_units: Decimal | None
    excess_vs_hodl_base_units: Decimal | None
    valuation_source: Literal["MARKET", "OUTCOME", "UNAVAILABLE"]


class LifecycleStepView(ApiModel):
    kind: str
    occurred_at: datetime
    price: Decimal
    capital_delta_base_units: Decimal
    realized_pnl_base_units: Decimal
    fee_income_base_units: Decimal
    slippage_base_units: Decimal
    network_base_units: Decimal
    jito_base_units: Decimal


class PositionDetail(ApiModel):
    position: PositionSummary
    range: RangeView | None
    valuation: ValuationView | None
    lifecycle: list[LifecycleStepView]
    decision_id: UUID | None
    plan_id: UUID | None
    correlation_id: str


class PerformanceGroupView(ApiModel):
    ruleset_version: str
    mode: TradingMode
    completed_positions: int
    winning_positions: int
    net_pnl_base_units: Decimal
    hodl_pnl_base_units: Decimal
    excess_vs_hodl_base_units: Decimal
    fee_income_base_units: Decimal
    divergence_loss_base_units: Decimal
    total_costs_base_units: Decimal
    expectancy_base_units: Decimal
    maximum_drawdown_base_units: Decimal
    first_closed_at: datetime
    last_closed_at: datetime


class PerformanceView(ApiModel):
    window_start: datetime
    window_end: datetime
    base_units_per_quote_unit: Decimal
    open_positions: int
    groups: list[PerformanceGroupView]


# --- configuration --------------------------------------------------------------------


class SecretStatus(ApiModel):
    name: str
    configured: bool
    # Services whose container receives the secret; ``configured`` is what this api sees.
    used_by: list[str] = []


class ModeReadinessView(ApiModel):
    mode: TradingMode
    ready: bool
    missing: list[str]
    notes: list[Notice]


class KillSwitchStateView(ApiModel):
    """The kill switch as the mode state records it (spec 7.3): its policy is the one the
    operator chose at activation, not a configured default."""

    active: bool
    policy: KillSwitchPolicy | None
    label: str


class EffectiveConfig(ApiModel):
    settings: dict[str, Any]
    secrets: list[SecretStatus]
    readiness: list[ModeReadinessView]
    ruleset_version: str
    config_version: str
    thresholds: dict[str, Any]
    kill_switch: KillSwitchStateView


# --- approvals ------------------------------------------------------------------------


class ChecklistItemView(ApiModel):
    key: str
    label: str


class SimulationField(ApiModel):
    name: str
    value: str


class PlanDiffRow(ApiModel):
    field: str
    planned: str | None
    command: str | None
    simulated: str | None
    matches: bool | None


class CostView(ApiModel):
    network_fee_lamports: str
    priority_fee_lamports: str
    compute_units: int
    price_impact_bps: int | None
    max_slippage_bps: int
    estimated_loss_base_units: int | None


class ApprovalSummary(ApiModel):
    command_id: UUID
    operation: str
    mode: TradingMode
    pool_address: str
    phase: str
    phase_label: str
    command_expires_at: datetime
    simulated_at: datetime


class ApprovalPreview(ApiModel):
    command_id: UUID
    phase: str
    phase_label: str
    irreversible: Literal[True] = True
    irreversibility_note: str
    operation: str
    strategy: str | None
    pool_address: str
    wallet_address: str
    position_address: str | None
    command_digest: str
    simulation_digest: str
    command_expires_at: datetime
    simulation_expires_at: datetime
    approval_expires_at: datetime
    seconds_left: int
    simulation_success: bool
    simulation: list[SimulationField]
    simulation_errors: list[Notice]
    costs: CostView
    plan_diff: list[PlanDiffRow]
    checklist: list[ChecklistItemView]
    notices: list[Notice]
    gmgn: list[MetricView]
    decision_id: UUID | None
    plan_id: UUID | None
    blockers: list[Notice]
    can_approve: bool


class ApprovalQueue(ApiModel):
    pending: list[ApprovalSummary]
    recent: list[ApprovalSummary]
    blockers: list[Notice]


class ChecklistMarks(ApiModel):
    twitter_hype: bool = False
    developer_supply: bool = False
    no_pvp_copies: bool = False
    bubblemaps_clusters: bool = False


class ApprovalRequest(ApiModel):
    approval_id: UUID
    command_digest: Digest
    simulation_digest: Digest
    checklist: ChecklistMarks = ChecklistMarks()
    comment: Annotated[str, StringConstraints(max_length=1000)] = ""
    operator_token: Token | None = None
    confirm_irreversible: bool = False


class ApprovalResult(ApiModel):
    command_id: UUID
    phase: str
    phase_label: str
    approval_id: UUID | None
    approved_at: datetime | None
    expires_at: datetime | None


# --- mode, kill switch and promotion --------------------------------------------------


class ModeTransitionRequest(ApiModel):
    target_mode: TradingMode
    reason: Reason
    evidence_id: UUID | None = None
    confirmation_id: Annotated[str, StringConstraints(max_length=128)] | None = None
    operator_token: Token | None = None
    confirm: bool = False


class ModeTransitionView(ApiModel):
    transition_id: UUID
    from_mode: TradingMode
    to_mode: TradingMode
    occurred_at: datetime
    promotion: bool
    reason: str
    evidence_id: str | None


class ModeTransitionResult(ApiModel):
    transition: ModeTransitionView
    mode: ModeView


class ModeJournal(ApiModel):
    mode: ModeView
    transitions: list[ModeTransitionView]


class KillSwitchActivateRequest(ApiModel):
    reason: Reason
    policy: KillSwitchPolicy = KillSwitchPolicy.HOLD_AND_ALERT


class KillSwitchReleaseRequest(ApiModel):
    reason: Reason
    operator_token: Token | None = None
    confirm: bool = False


class AlertResolveRequest(ApiModel):
    """Closing a critical alert: a reason for the audit trail and re-authentication (G13.2)."""

    reason: Reason
    operator_token: Token | None = None
    confirm: bool = False


class PositionActionView(ApiModel):
    position_id: str
    action: str


class KillSwitchResultView(ApiModel):
    activated_at: datetime
    policy: KillSwitchPolicy
    cancelled_plan_count: int
    position_actions: list[PositionActionView]
    mode: ModeView


class ShadowReadinessView(ApiModel):
    evidence_id: UUID
    generated_at: datetime
    allowed: bool
    reasons: list[Notice]
    full_shadow_days: int
    minimum_days: int
    recommended_days: int
    minimum_days_remaining: int
    recommended_days_remaining: int
    completed_positions: int
    decision_count: int
    insufficient_statistics: bool
    net_expectancy: Decimal
    maximum_drawdown: Decimal
    data_coverage: Decimal
    open_critical_alerts: int
    consumed: bool


class LiveReadinessView(ApiModel):
    evidence_present: bool
    ready: bool
    reasons: list[Notice]
    approval_cycles: int | None
    expires_at: datetime | None


class PromotionView(ApiModel):
    mode: ModeView
    observation_days: int
    minimum_days: int
    recommended_days: int
    live: LiveReadinessView


class Page[ItemT](ApiModel):
    items: list[ItemT]
    next_cursor: str | None
    limit: int = Field(ge=1)
