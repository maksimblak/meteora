"""One versioned bundle of every rule the worker applies.

The defaults are explicit *hypotheses for shadow validation* (spec 6.2, 6.6), not
promises of profit; every decision record stores the ruleset and config version
that produced it, so changing any number here requires a new version string.

``guide-v1`` (the LexDollar guide, spec 6.6) is the default. ``shadow-hypothesis-v1``
is kept byte-for-byte so its decisions stay reproducible.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from src.accounting.models import CostModel
from src.domain.features import GuideFeatureRules, VolumeFeatureRules
from src.domain.risk.models import RiskPolicy
from src.domain.screening import GuideScreeningRules, ScreeningPolicy
from src.domain.strategy import GuideStrategyRules, StrategyPolicy
from src.modes.models import KillSwitchPolicy

LEGACY_RULESET_VERSION = "shadow-hypothesis-v1"
GUIDE_RULESET_VERSION = "guide-v1"
DEFAULT_RULESET_VERSION = GUIDE_RULESET_VERSION
DEFAULT_CONFIG_VERSION = "worker-config-v1"
RULESET_VERSION_ENV = "RULESET_VERSION"
RULESET_OVERRIDES_ENV = "RULESET_OVERRIDES_FILE"


@dataclass(frozen=True, slots=True)
class PipelinePolicy:
    features: VolumeFeatureRules
    screening: ScreeningPolicy
    strategy: StrategyPolicy
    risk: RiskPolicy
    shadow_costs: CostModel
    entry_notional_quote: Decimal
    quote_base_units_per_unit: Decimal
    max_slippage_bps: int = 100
    kill_switch_policy: KillSwitchPolicy = KillSwitchPolicy.HOLD_AND_ALERT

    def __post_init__(self) -> None:
        versions = {
            (self.features.ruleset_version, self.features.config_version),
            (self.strategy.ruleset_version, self.strategy.config_version),
            (self.risk.ruleset_version, self.risk.config_version),
        }
        if len(versions) != 1:
            raise ValueError("feature, strategy and risk rules must share one version pair")
        if self.entry_notional_quote <= 0 or self.quote_base_units_per_unit <= 0:
            raise ValueError("entry notional and quote unit scale must be positive")
        if not 0 <= self.max_slippage_bps <= 10_000:
            raise ValueError("max_slippage_bps must be in [0, 10000]")

    @property
    def ruleset_version(self) -> str:
        return self.risk.ruleset_version

    @property
    def config_version(self) -> str:
        return self.risk.config_version


def hypothesis_policy(
    *,
    ruleset_version: str = LEGACY_RULESET_VERSION,
    config_version: str = DEFAULT_CONFIG_VERSION,
    minimum_tvl: Decimal = Decimal("10000"),
    minimum_depth: Decimal = Decimal("5000"),
    minimum_pool_age: timedelta = timedelta(days=3),
    maximum_holder_concentration: Decimal = Decimal("0.30"),
    maximum_price_impact: Decimal = Decimal("0.01"),
    minimum_short_ratio: Decimal = Decimal("3"),
    minimum_long_ratio: Decimal = Decimal("1.5"),
    minimum_robust_z_score: Decimal = Decimal("4"),
    minimum_fee_tvl: Decimal = Decimal("0.002"),
    minimum_score: Decimal = Decimal("0.5"),
    baseline_points: int = 12,
    minimum_baseline_points: int = 6,
    maximum_history_points: int = 288,
    entry_notional_quote: Decimal = Decimal("100"),
    quote_base_units_per_unit: Decimal = Decimal(1_000_000),
    max_slippage_bps: int = 100,
    kill_switch_policy: KillSwitchPolicy = KillSwitchPolicy.HOLD_AND_ALERT,
) -> PipelinePolicy:
    """``shadow-hypothesis-v1``: volume-spike rules of spec 6.2-6.4 without guide signals."""

    return PipelinePolicy(
        features=VolumeFeatureRules(
            ruleset_version=ruleset_version,
            config_version=config_version,
            baseline_points=baseline_points,
            minimum_baseline_points=minimum_baseline_points,
            maximum_history_points=maximum_history_points,
        ),
        screening=ScreeningPolicy(
            minimum_tvl=minimum_tvl,
            minimum_depth=minimum_depth,
            minimum_pool_age=minimum_pool_age,
            maximum_holder_concentration=maximum_holder_concentration,
            maximum_price_impact=maximum_price_impact,
            minimum_short_ratio=minimum_short_ratio,
            minimum_long_ratio=minimum_long_ratio,
            minimum_robust_z_score=minimum_robust_z_score,
            minimum_fee_tvl=minimum_fee_tvl,
            minimum_score=minimum_score,
        ),
        strategy=StrategyPolicy(ruleset_version=ruleset_version, config_version=config_version),
        risk=RiskPolicy(ruleset_version=ruleset_version, config_version=config_version),
        shadow_costs=_shadow_costs(),
        entry_notional_quote=entry_notional_quote,
        quote_base_units_per_unit=quote_base_units_per_unit,
        max_slippage_bps=max_slippage_bps,
        kill_switch_policy=kill_switch_policy,
    )


class GuidePolicySettings(BaseModel):
    """Every tunable number of ``guide-v1``; the defaults are the spec 6.6 table.

    Author thresholds: market cap, token age, top-10 share, volume per minute (working and
    overvolume), volume to liquidity, fee organicity and total fees. Pool age only
    guards history warm-up here: the guide trades tokens a few hours old, and token age
    has its own threshold. Price shock is widened to the deepest Bid-Ask range so the
    default strategy is invalidated by its range, not by the first 10% dip.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    minimum_tvl: Decimal = Decimal("10000")
    minimum_depth: Decimal = Decimal("5000")
    minimum_pool_age: timedelta = timedelta(minutes=30)
    maximum_holder_concentration: Decimal = Decimal("0.30")
    maximum_price_impact: Decimal = Decimal("0.01")
    minimum_short_ratio: Decimal = Decimal("3")
    minimum_long_ratio: Decimal = Decimal("1.5")
    minimum_robust_z_score: Decimal = Decimal("4")
    minimum_fee_tvl: Decimal = Decimal("0.002")
    minimum_score: Decimal = Decimal("0.5")
    minimum_market_cap: Decimal = Decimal("500000")
    preferred_market_cap: Decimal = Decimal("1000000")
    minimum_token_age: timedelta = timedelta(hours=2)
    minimum_volume_per_minute: Decimal = Decimal("14000")
    overvolume_volume_per_minute: Decimal = Decimal("100000")
    minimum_volume_to_liquidity: Decimal = Decimal("0.5")
    minimum_fee_efficiency: Decimal = Decimal("0.5")
    minimum_total_fees_sol: Decimal = Decimal("30")
    baseline_points: int = 12
    minimum_baseline_points: int = 6
    maximum_history_points: int = 288
    volume_window: str = "5m"
    organicity_window: str = "1h"
    local_high_lookback: timedelta = timedelta(hours=1)
    ath_coverage_tolerance: timedelta = timedelta(minutes=15)
    maximum_observation_gap: timedelta = timedelta(minutes=5, seconds=30)
    holding_horizon: timedelta = timedelta(hours=4)
    bid_ask_minimum_depth: Decimal = Decimal("0.70")
    bid_ask_target_depth: Decimal = Decimal("0.75")
    bid_ask_maximum_depth: Decimal = Decimal("0.80")
    maximum_position_bins: int = 70
    ath_breakout_margin: Decimal = Decimal("0")
    spot_holding_horizon: timedelta = timedelta(hours=1)
    overvolume_maximum_holding_period: timedelta = timedelta(minutes=30)
    overvolume_volume_decay_ratio: Decimal = Decimal("1")
    volume_decay_ratio: Decimal = Decimal("0.50")
    price_shock_fraction: Decimal = Decimal("0.80")
    entry_notional_quote: Decimal = Decimal("100")
    quote_base_units_per_unit: Decimal = Decimal(1_000_000)
    max_slippage_bps: int = 100

    @field_validator("*", mode="before")
    @classmethod
    def _reject_binary_float(cls, value: Any) -> Any:
        if isinstance(value, (float, bool)):
            raise ValueError("binary float is forbidden; quote decimal thresholds as strings")
        return value


def guide_policy(
    settings: GuidePolicySettings | None = None,
    *,
    ruleset_version: str = GUIDE_RULESET_VERSION,
    config_version: str = DEFAULT_CONFIG_VERSION,
    kill_switch_policy: KillSwitchPolicy = KillSwitchPolicy.HOLD_AND_ALERT,
) -> PipelinePolicy:
    """``guide-v1``: spec 6.2-6.4 plus the LexDollar thresholds and strategy choice (6.6)."""

    values = settings or GuidePolicySettings()
    return PipelinePolicy(
        features=VolumeFeatureRules(
            ruleset_version=ruleset_version,
            config_version=config_version,
            baseline_points=values.baseline_points,
            minimum_baseline_points=values.minimum_baseline_points,
            maximum_history_points=values.maximum_history_points,
            guide=GuideFeatureRules(
                volume_window=values.volume_window,
                organicity_window=values.organicity_window,
                local_high_lookback=values.local_high_lookback,
                ath_coverage_tolerance=values.ath_coverage_tolerance,
                maximum_observation_gap=values.maximum_observation_gap,
            ),
        ),
        screening=ScreeningPolicy(
            minimum_tvl=values.minimum_tvl,
            minimum_depth=values.minimum_depth,
            minimum_pool_age=values.minimum_pool_age,
            maximum_holder_concentration=values.maximum_holder_concentration,
            maximum_price_impact=values.maximum_price_impact,
            minimum_short_ratio=values.minimum_short_ratio,
            minimum_long_ratio=values.minimum_long_ratio,
            minimum_robust_z_score=values.minimum_robust_z_score,
            minimum_fee_tvl=values.minimum_fee_tvl,
            minimum_score=values.minimum_score,
            guide=GuideScreeningRules(
                minimum_market_cap=values.minimum_market_cap,
                preferred_market_cap=values.preferred_market_cap,
                minimum_token_age=values.minimum_token_age,
                minimum_volume_per_minute=values.minimum_volume_per_minute,
                overvolume_volume_per_minute=values.overvolume_volume_per_minute,
                minimum_volume_to_liquidity=values.minimum_volume_to_liquidity,
                minimum_fee_efficiency=values.minimum_fee_efficiency,
                minimum_total_fees_sol=values.minimum_total_fees_sol,
            ),
        ),
        strategy=StrategyPolicy(
            ruleset_version=ruleset_version,
            config_version=config_version,
            holding_horizon=values.holding_horizon,
            volume_decay_ratio=values.volume_decay_ratio,
            price_shock_fraction=values.price_shock_fraction,
            guide=GuideStrategyRules(
                bid_ask_minimum_depth=values.bid_ask_minimum_depth,
                bid_ask_target_depth=values.bid_ask_target_depth,
                bid_ask_maximum_depth=values.bid_ask_maximum_depth,
                maximum_position_bins=values.maximum_position_bins,
                spot_minimum_volume_per_minute=values.minimum_volume_per_minute,
                spot_minimum_fee_efficiency=values.minimum_fee_efficiency,
                overvolume_volume_per_minute=values.overvolume_volume_per_minute,
                ath_breakout_margin=values.ath_breakout_margin,
                spot_holding_horizon=values.spot_holding_horizon,
                overvolume_maximum_holding_period=values.overvolume_maximum_holding_period,
                overvolume_volume_decay_ratio=values.overvolume_volume_decay_ratio,
            ),
        ),
        risk=RiskPolicy(ruleset_version=ruleset_version, config_version=config_version),
        shadow_costs=_shadow_costs(),
        entry_notional_quote=values.entry_notional_quote,
        quote_base_units_per_unit=values.quote_base_units_per_unit,
        max_slippage_bps=values.max_slippage_bps,
        kill_switch_policy=kill_switch_policy,
    )


def default_policy() -> PipelinePolicy:
    return guide_policy()


def load_pipeline_policy(environ: Mapping[str, str]) -> PipelinePolicy:
    """Select the rule bundle from configuration.

    ``RULESET_VERSION`` picks ``guide-v1`` (default) or ``shadow-hypothesis-v1``.
    ``RULESET_OVERRIDES_FILE`` points to YAML with ``config_version`` and a ``thresholds``
    mapping of ``GuidePolicySettings`` fields; tuned numbers must carry their own version.
    """

    version = environ.get(RULESET_VERSION_ENV) or DEFAULT_RULESET_VERSION
    overrides_path = environ.get(RULESET_OVERRIDES_ENV)
    if version == LEGACY_RULESET_VERSION:
        if overrides_path:
            raise ValueError("threshold overrides apply to guide-v1 only")
        return hypothesis_policy()
    if version != GUIDE_RULESET_VERSION:
        raise ValueError(f"unknown ruleset version {version!r}")
    if not overrides_path:
        return guide_policy()
    document = yaml.safe_load(Path(overrides_path).read_text(encoding="utf-8")) or {}
    if not isinstance(document, dict):
        raise ValueError("ruleset overrides must be a mapping")
    unknown_keys = set(document) - {"config_version", "thresholds"}
    if unknown_keys:
        raise ValueError(f"unknown ruleset override keys: {sorted(unknown_keys)}")
    config_version = document.get("config_version")
    if not isinstance(config_version, str) or not config_version:
        raise ValueError("ruleset overrides require a config_version")
    if config_version == DEFAULT_CONFIG_VERSION:
        raise ValueError("tuned thresholds need a config_version other than the default")
    thresholds = document.get("thresholds") or {}
    if not isinstance(thresholds, dict):
        raise ValueError("thresholds must be a mapping")
    try:
        settings = GuidePolicySettings.model_validate(thresholds)
    except ValidationError as error:
        raise ValueError(f"invalid guide-v1 thresholds: {error}") from error
    return guide_policy(settings, config_version=config_version)


def _shadow_costs() -> CostModel:
    return CostModel(
        slippage_bps=50,
        network_fee_base_units=Decimal(0),
        jito_tip_base_units=Decimal(0),
    )
