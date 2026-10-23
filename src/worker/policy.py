"""One versioned bundle of every rule the worker applies.

The defaults are explicit *hypotheses for shadow validation* (spec 6.2, 6.6), not
promises of profit; every decision record stores the ruleset and config version
that produced it, so changing any number here requires a new version string.

``guide-v1`` (the LexDollar guide, spec 6.6) is the default. ``shadow-hypothesis-v1``
is kept byte-for-byte so its decisions stay reproducible.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from src.accounting.models import CostModel
from src.domain.features import GuideFeatureRules, VolumeFeatureRules
from src.domain.risk.models import RiskPolicy
from src.domain.screening import GuideScreeningRules, ScreeningPolicy, VolumeIntegrityMode
from src.domain.strategy import GuideStrategyRules, StrategyPolicy
from src.market.windows import ShortWindowRules
from src.modes.models import KillSwitchPolicy

LEGACY_RULESET_VERSION = "shadow-hypothesis-v1"
GUIDE_RULESET_VERSION = "guide-v1"
DEFAULT_RULESET_VERSION = GUIDE_RULESET_VERSION
DEFAULT_CONFIG_VERSION = "worker-config-v1"
RULESET_VERSION_ENV = "RULESET_VERSION"
RULESET_OVERRIDES_ENV = "RULESET_OVERRIDES_FILE"
UNIVERSE_INTERVAL_ENV = "WORKER_UNIVERSE_INTERVAL_SECONDS"
DEFAULT_UNIVERSE_INTERVAL = timedelta(minutes=5)
# Bins the pool may move between the snapshot an entry was planned on and the fresh read
# (and the executor's simulation) before the planned range counts as stale.
DEFAULT_ACTIVE_BIN_TOLERANCE = 3


def universe_interval(environ: Mapping[str, str]) -> timedelta:
    """The universe cadence: snapshots of one pool are this far apart (plus slack)."""

    raw = environ.get(UNIVERSE_INTERVAL_ENV)
    seconds = int(raw) if raw else int(DEFAULT_UNIVERSE_INTERVAL.total_seconds())
    if seconds <= 0:
        raise ValueError(f"{UNIVERSE_INTERVAL_ENV} must be a positive number of seconds")
    return timedelta(seconds=seconds)


def observation_gap(interval: timedelta) -> timedelta:
    """Largest gap between adjacent snapshots that is still one cadence step.

    One source for both the ATH coverage proof and the short-window derivation, so neither
    is configured apart from the universe interval: the interval plus max(30 s, 10%).
    """

    return interval + max(timedelta(seconds=30), interval / 10)


def _short_window_rules(
    interval: timedelta,
    *,
    negative_noise_absolute: Decimal = Decimal("10"),
    negative_noise_relative: Decimal = Decimal("0.0000001"),
    counter_reset_after: timedelta = timedelta(minutes=30),
) -> ShortWindowRules:
    gap = observation_gap(interval)
    return ShortWindowRules(
        minimum_interval=min(timedelta(minutes=1), gap),
        maximum_interval=gap,
        negative_noise_absolute=negative_noise_absolute,
        negative_noise_relative=negative_noise_relative,
        counter_reset_after=counter_reset_after,
    )


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
    short_window: ShortWindowRules = field(
        default_factory=lambda: _short_window_rules(DEFAULT_UNIVERSE_INTERVAL)
    )
    active_bin_tolerance: int = DEFAULT_ACTIVE_BIN_TOLERANCE
    # SHADOW without a quote (DEPTH_UNAVAILABLE, D07): the fill pays the worst slippage the
    # command tolerates and the pool is assumed to absorb only this share of its TVL at the
    # maximum price impact.
    conservative_costs: CostModel = field(default_factory=lambda: _shadow_costs(100))
    conservative_depth_fraction: Decimal = Decimal("0.05")
    # Native SOL kept for fees and rent when the wallet's SOL funds a wSOL leg.
    wallet_native_reserve_lamports: int = 20_000_000

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
        if self.active_bin_tolerance < 0:
            raise ValueError("active_bin_tolerance must not be negative")
        if not Decimal(0) < self.conservative_depth_fraction <= Decimal(1):
            raise ValueError("conservative_depth_fraction must be in (0, 1]")
        if not (
            self.shadow_costs.slippage_bps
            <= self.conservative_costs.slippage_bps
            <= self.max_slippage_bps
        ):
            raise ValueError(
                "conservative slippage must lie between the shadow model and the tolerance"
            )
        if self.wallet_native_reserve_lamports < 0:
            raise ValueError("wallet_native_reserve_lamports must not be negative")

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
    universe_interval: timedelta = DEFAULT_UNIVERSE_INTERVAL,
) -> PipelinePolicy:
    """``shadow-hypothesis-v1``: volume-spike rules of spec 6.2-6.4 without guide signals."""

    return PipelinePolicy(
        short_window=_short_window_rules(universe_interval),
        conservative_costs=_shadow_costs(max_slippage_bps),
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
    # The ATH observation gap and the short-window interval follow the universe interval
    # (``WORKER_UNIVERSE_INTERVAL_SECONDS``); they are not thresholds of their own.
    short_window_negative_noise: Decimal = Decimal("10")
    short_window_negative_noise_relative: Decimal = Decimal("0.0000001")
    short_window_counter_reset_after: timedelta = timedelta(minutes=30)
    bid_only_upside_grace: timedelta = timedelta(minutes=30)
    bid_only_upside_exit: Decimal = Decimal("0.05")
    maximum_burst_spread: Decimal = Decimal("0.02")
    minimum_fee_support: Decimal = Decimal("0.5")
    active_bin_tolerance: int = DEFAULT_ACTIVE_BIN_TOLERANCE
    # Without a quote, SHADOW fills pay the full slippage tolerance (``max_slippage_bps``).
    conservative_slippage_bps: int = 100
    conservative_depth_fraction: Decimal = Decimal("0.05")
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
    universe_interval: timedelta = DEFAULT_UNIVERSE_INTERVAL,
) -> PipelinePolicy:
    """``guide-v1``: spec 6.2-6.4 plus the LexDollar thresholds and strategy choice (6.6).

    On live data (D07) artificial volume is judged on the signals the Data API has, and the
    observation gap of the ATH proof follows ``universe_interval``.
    """

    values = settings or GuidePolicySettings()
    return PipelinePolicy(
        short_window=_short_window_rules(
            universe_interval,
            negative_noise_absolute=values.short_window_negative_noise,
            negative_noise_relative=values.short_window_negative_noise_relative,
            counter_reset_after=values.short_window_counter_reset_after,
        ),
        active_bin_tolerance=values.active_bin_tolerance,
        conservative_costs=_shadow_costs(values.conservative_slippage_bps),
        conservative_depth_fraction=values.conservative_depth_fraction,
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
                maximum_observation_gap=observation_gap(universe_interval),
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
            volume_integrity=VolumeIntegrityMode.AVAILABLE_SIGNALS,
            maximum_burst_spread=values.maximum_burst_spread,
            minimum_fee_support=values.minimum_fee_support,
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
                bid_only_upside_grace=values.bid_only_upside_grace,
                bid_only_upside_exit=values.bid_only_upside_exit,
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
    interval = universe_interval(environ)
    if version == LEGACY_RULESET_VERSION:
        if overrides_path:
            raise ValueError("threshold overrides apply to guide-v1 only")
        return hypothesis_policy(universe_interval=interval)
    if version != GUIDE_RULESET_VERSION:
        raise ValueError(f"unknown ruleset version {version!r}")
    if not overrides_path:
        return guide_policy(universe_interval=interval)
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
    return guide_policy(settings, config_version=config_version, universe_interval=interval)


def _shadow_costs(slippage_bps: int = 50) -> CostModel:
    return CostModel(
        slippage_bps=slippage_bps,
        network_fee_base_units=Decimal(0),
        jito_tip_base_units=Decimal(0),
    )
