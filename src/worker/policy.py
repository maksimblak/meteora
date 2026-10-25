"""One versioned bundle of every rule the worker applies.

The defaults are explicit *hypotheses for shadow validation* (spec 6.2, 6.6), not
promises of profit; every decision record stores the ruleset and config version
that produced it, so changing any number here requires a new version string.

``guide-v1`` (the LexDollar guide, spec 6.6) is the default. ``shadow-hypothesis-v1``
is kept byte-for-byte so its decisions stay reproducible.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from src.accounting.models import CostModel
from src.domain.features import GuideFeatureRules, VolumeFeatureRules
from src.domain.risk.models import RiskPolicy
from src.domain.screening import (
    DEFAULT_THEME_WORDS,
    GmgnScreeningRules,
    GuideScreeningRules,
    ScreeningPolicy,
    VolumeIntegrityMode,
)
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
# G18: LIVE needs fresh GMGN data unless this is switched off explicitly ("false").
GMGN_REQUIRED_FOR_LIVE_ENV = "GMGN_REQUIRED_FOR_LIVE"
# The executor's priority fee ceiling per transaction (T17, No 49); SHADOW books no more.
PRIORITY_FEE_MAX_ENV = "PRIORITY_FEE_MAX_LAMPORTS"
DEFAULT_PRIORITY_FEE_LAMPORTS = 15_000_000
# SHADOW's estimate of the priority fee of one transaction (spec 6.8, No 49; F4). A starting
# estimate of this build, not market data: 1 000 000 lamports = 0.001 SOL, a fifteenth of the
# guide's ceiling of 0.015 SOL, which the executor reaches only when the network's fee
# percentile is at the cap. It is checked against the ``priority_fee_lamports`` of APPROVAL
# settlements and tuned as a versioned guide-v1 setting (``shadow_priority_fee_lamports``);
# ``PRIORITY_FEE_MAX_LAMPORTS`` stays only its upper bound.
DEFAULT_SHADOW_PRIORITY_FEE_LAMPORTS = 1_000_000
_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})
DEFAULT_UNIVERSE_INTERVAL = timedelta(minutes=5)
# Bins the pool may move between the snapshot an entry was planned on and the fresh read
# (and the executor's simulation) before the planned range counts as stale.
DEFAULT_ACTIVE_BIN_TOLERANCE = 3
# How much shorter than the 5m window the counters' interval may be and still make it (the
# sweeps' jitter, the slack of ``observation_gap``); a shorter increase is never stretched.
DEFAULT_SHORT_WINDOW_TOLERANCE = timedelta(seconds=30)


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
    interval_tolerance: timedelta = DEFAULT_SHORT_WINDOW_TOLERANCE,
    negative_noise_absolute: Decimal = Decimal("10"),
    negative_noise_relative: Decimal = Decimal("0.0000001"),
    counter_reset_after: timedelta = timedelta(minutes=30),
) -> ShortWindowRules:
    """A window spans at least its length less ``interval_tolerance`` and starts no further
    back than one cadence step (``observation_gap``). Sweeps that come sooner than the
    window are summed, so the newest start old enough may lie one more step back: the limit
    is then the shortest interval plus that gap."""

    rules = ShortWindowRules(
        interval_tolerance=interval_tolerance,
        negative_noise_absolute=negative_noise_absolute,
        negative_noise_relative=negative_noise_relative,
        counter_reset_after=counter_reset_after,
    )
    gap = observation_gap(interval)
    if interval < rules.minimum_interval:
        gap += rules.minimum_interval
    return replace(rules, maximum_interval=gap)


@dataclass(frozen=True, slots=True)
class PipelinePolicy:
    features: VolumeFeatureRules
    screening: ScreeningPolicy
    strategy: StrategyPolicy
    risk: RiskPolicy
    shadow_costs: CostModel
    # The reference size of the executor's depth quote, in USD like ``minimum_depth`` and the
    # TVL; it is turned into the pool's quote token by that token's USD price and decimals.
    entry_notional_quote: Decimal
    # Quote-token base units per USD for a source that measures depth itself and reports no
    # quote price (fixtures); Data API pools use their quote token's price and decimals.
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
    # G24: a cycle's candidates are ranked by their token's hourly volume and only this many
    # are checked in depth (token risk, GMGN, quote); ``None`` checks every candidate.
    deep_candidate_limit: int | None = None
    # Liquidity slippage (spec 6.8, No 50): the active bin may move this share of the
    # position's bins before the entry counts as stale; ``None``: ``active_bin_tolerance``.
    liquidity_slippage: Decimal | None = None

    def active_bin_tolerance_for(self, lower_bin_offset: int, upper_bin_offset: int) -> int:
        """Bins the active bin may move for a range of relative bins (``ACTIVE_BIN_DRIFT``):
        ``liquidity_slippage`` of its width, rounded down, at least one."""

        if self.liquidity_slippage is None:
            return self.active_bin_tolerance
        width = Decimal(upper_bin_offset - lower_bin_offset + 1)
        return max(int(width * self.liquidity_slippage), 1)

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
        if self.deep_candidate_limit is not None and self.deep_candidate_limit < 1:
            raise ValueError("deep_candidate_limit must be at least one")
        if self.liquidity_slippage is not None and not (
            Decimal(0) < self.liquidity_slippage <= Decimal(1)
        ):
            raise ValueError("liquidity_slippage must be a share of the range in (0, 1]")

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
    priority_fee_ceiling_lamports: int = 0,
) -> PipelinePolicy:
    """``shadow-hypothesis-v1``: volume-spike rules of spec 6.2-6.4 without guide signals.

    SHADOW books the build's priority fee estimate per transaction, capped by
    ``priority_fee_ceiling_lamports`` (the executor's ceiling; spec 6.8)."""

    priority = min(DEFAULT_SHADOW_PRIORITY_FEE_LAMPORTS, priority_fee_ceiling_lamports)
    return PipelinePolicy(
        short_window=_short_window_rules(universe_interval),
        conservative_costs=_shadow_costs(max_slippage_bps, priority),
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
        shadow_costs=_shadow_costs(priority_fee_lamports=priority),
        entry_notional_quote=entry_notional_quote,
        quote_base_units_per_unit=quote_base_units_per_unit,
        max_slippage_bps=max_slippage_bps,
        kill_switch_policy=kill_switch_policy,
    )


class GuidePolicySettings(BaseModel):
    """Every tunable number of ``guide-v1``; the defaults are the spec 6.6 table.

    Author thresholds: market cap, token age, top-10 share, volume per minute (working and
    overvolume), volume to liquidity and total fees; the author's selection (spec 6.8,
    G24): the hour's average volume per minute, total fees against the cap, the theme
    words, snipers, revivals and the deep-check limit. ``minimum_fee_efficiency`` now only
    feeds the Spot-on-ATH organicity of the strategy; screening no longer uses it. There is no
    fee/TVL minimum (removed 2026-10-25, ADR 0016: not the author's). Pool age only
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
    minimum_score: Decimal = Decimal("0.5")
    minimum_market_cap: Decimal = Decimal("500000")
    preferred_market_cap: Decimal = Decimal("1000000")
    minimum_token_age: timedelta = timedelta(hours=2)
    minimum_volume_per_minute: Decimal = Decimal("14000")
    overvolume_volume_per_minute: Decimal = Decimal("100000")
    minimum_volume_to_liquidity: Decimal = Decimal("0.5")
    minimum_fee_efficiency: Decimal = Decimal("0.5")
    minimum_total_fees_sol: Decimal = Decimal("30")
    # GMGN holder structure (spec 6.7): a share at or above its maximum rejects.
    maximum_insiders_share: Decimal = Decimal("0.10")
    maximum_bundling_share: Decimal = Decimal("0.70")
    maximum_phishing_share: Decimal = Decimal("0.30")
    maximum_gmgn_top10_holder_share: Decimal = Decimal("0.30")
    baseline_points: int = 12
    minimum_baseline_points: int = 6
    maximum_history_points: int = 288
    volume_window: str = "5m"
    organicity_window: str = "1h"
    local_high_lookback: timedelta = timedelta(hours=1)
    ath_coverage_tolerance: timedelta = timedelta(minutes=15)
    # The ATH observation gap and the short-window interval follow the universe interval
    # (``WORKER_UNIVERSE_INTERVAL_SECONDS``); they are not thresholds of their own.
    short_window_interval_tolerance: timedelta = DEFAULT_SHORT_WINDOW_TOLERANCE
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
    # Strategy and exits as the author does them (spec 6.8, T19): Bid-Ask held up to a day
    # (``holding_horizon``), from at least 90% of the hour's high, 70..80% deep in one SDK
    # position; one-sided Spot 60% deep, on an ATH only at a cap of 1M; take profit on net
    # P&L of 5% (Bid-Ask, Spot on ATH) or 3% (overvolume Spot); Spot on ATH held up to 3 h.
    holding_horizon: timedelta = timedelta(hours=24)
    bid_ask_minimum_depth: Decimal = Decimal("0.70")
    bid_ask_target_depth: Decimal = Decimal("0.75")
    bid_ask_maximum_depth: Decimal = Decimal("0.80")
    maximum_position_bins: int = 70
    bid_ask_local_high_fraction: Decimal = Decimal("0.90")
    spot_depth: Decimal = Decimal("0.60")
    spot_ath_minimum_market_cap: Decimal = Decimal("1000000")
    bid_ask_take_profit: Decimal = Decimal("0.05")
    spot_ath_take_profit: Decimal = Decimal("0.05")
    overvolume_take_profit: Decimal = Decimal("0.03")
    # The active bin may move this share of the position's bins between the plan and the
    # chain (``ACTIVE_BIN_DRIFT``, the SDK's bin slippage); ``None``: ``active_bin_tolerance``.
    liquidity_slippage: Decimal | None = Decimal("0.30")
    ath_breakout_margin: Decimal = Decimal("0")
    spot_holding_horizon: timedelta = timedelta(hours=3)
    overvolume_maximum_holding_period: timedelta = timedelta(minutes=30)
    overvolume_volume_decay_ratio: Decimal = Decimal("1")
    volume_decay_ratio: Decimal = Decimal("0.50")
    price_shock_fraction: Decimal = Decimal("0.80")
    # USD reference size of the depth quote (A3); see ``PipelinePolicy``.
    entry_notional_quote: Decimal = Decimal("100")
    quote_base_units_per_unit: Decimal = Decimal(1_000_000)
    max_slippage_bps: int = 100
    # Selection as the author does it (spec 6.8, G24). Sustained: the token's hourly
    # average per minute; the score's "good" rate (guide: 20k+ a minute); artificial volume:
    # total fees below 30 SOL x cap / reference cap at a tenfold hourly pool turnover.
    minimum_hourly_volume_per_minute: Decimal = Decimal("14000")
    target_volume_per_minute: Decimal = Decimal("20000")
    total_fees_reference_market_cap: Decimal = Decimal("500000")
    maximum_hourly_turnover: Decimal = Decimal("10")
    excluded_theme_words: tuple[str, ...] = DEFAULT_THEME_WORDS
    maximum_sniper_count: int = 20
    revived_token_age: timedelta = timedelta(days=60)
    maximum_deep_candidates: int = 50
    # SHADOW's priority fee per transaction (``DEFAULT_SHADOW_PRIORITY_FEE_LAMPORTS``: the
    # build's starting estimate), capped by the executor's ``PRIORITY_FEE_MAX_LAMPORTS``.
    shadow_priority_fee_lamports: int = DEFAULT_SHADOW_PRIORITY_FEE_LAMPORTS

    @field_validator("*", mode="before")
    @classmethod
    def _reject_binary_float(cls, value: Any) -> Any:
        if isinstance(value, (float, bool)):
            raise ValueError("binary float is forbidden; quote decimal thresholds as strings")
        return value

    @field_validator("shadow_priority_fee_lamports")
    @classmethod
    def _non_negative_priority_fee(cls, value: int) -> int:
        if value < 0:
            raise ValueError("shadow_priority_fee_lamports cannot be negative")
        return value


def guide_policy(
    settings: GuidePolicySettings | None = None,
    *,
    ruleset_version: str = GUIDE_RULESET_VERSION,
    config_version: str = DEFAULT_CONFIG_VERSION,
    kill_switch_policy: KillSwitchPolicy = KillSwitchPolicy.HOLD_AND_ALERT,
    universe_interval: timedelta = DEFAULT_UNIVERSE_INTERVAL,
    gmgn_required_for_live: bool | None = None,
    priority_fee_ceiling_lamports: int = 0,
) -> PipelinePolicy:
    """``guide-v1``: spec 6.2-6.4 plus the LexDollar thresholds and strategy choice (6.6).

    On live data (D07) artificial volume is judged on the signals the Data API has, and the
    observation gap of the ATH proof follows ``universe_interval``. ``gmgn_required_for_live``
    adds the GMGN rules of spec 6.7 with that LIVE requirement; ``None`` builds the bundle
    without them (the worker and the API always load them through ``load_pipeline_policy``).
    SHADOW books ``shadow_priority_fee_lamports`` per transaction, capped by
    ``priority_fee_ceiling_lamports`` (the executor's ceiling; spec 6.8).
    """

    values = settings or GuidePolicySettings()
    priority = min(values.shadow_priority_fee_lamports, priority_fee_ceiling_lamports)
    gmgn = (
        None
        if gmgn_required_for_live is None
        else GmgnScreeningRules(
            maximum_insiders_share=values.maximum_insiders_share,
            maximum_bundling_share=values.maximum_bundling_share,
            maximum_phishing_share=values.maximum_phishing_share,
            maximum_top10_holder_share=values.maximum_gmgn_top10_holder_share,
            required_for_live=gmgn_required_for_live,
            maximum_sniper_count=values.maximum_sniper_count,
        )
    )
    return PipelinePolicy(
        short_window=_short_window_rules(
            universe_interval,
            interval_tolerance=values.short_window_interval_tolerance,
            negative_noise_absolute=values.short_window_negative_noise,
            negative_noise_relative=values.short_window_negative_noise_relative,
            counter_reset_after=values.short_window_counter_reset_after,
        ),
        active_bin_tolerance=values.active_bin_tolerance,
        conservative_costs=_shadow_costs(values.conservative_slippage_bps, priority),
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
                gmgn=gmgn,
                minimum_hourly_volume_per_minute=values.minimum_hourly_volume_per_minute,
                target_volume_per_minute=values.target_volume_per_minute,
                total_fees_reference_market_cap=values.total_fees_reference_market_cap,
                maximum_hourly_turnover=values.maximum_hourly_turnover,
                excluded_theme_words=values.excluded_theme_words,
                revived_token_age=values.revived_token_age,
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
                bid_ask_local_high_fraction=values.bid_ask_local_high_fraction,
                spot_depth=values.spot_depth,
                spot_minimum_volume_per_minute=values.minimum_volume_per_minute,
                spot_minimum_fee_efficiency=values.minimum_fee_efficiency,
                spot_ath_minimum_market_cap=values.spot_ath_minimum_market_cap,
                overvolume_volume_per_minute=values.overvolume_volume_per_minute,
                ath_breakout_margin=values.ath_breakout_margin,
                spot_holding_horizon=values.spot_holding_horizon,
                overvolume_maximum_holding_period=values.overvolume_maximum_holding_period,
                overvolume_volume_decay_ratio=values.overvolume_volume_decay_ratio,
                bid_ask_take_profit=values.bid_ask_take_profit,
                spot_ath_take_profit=values.spot_ath_take_profit,
                overvolume_take_profit=values.overvolume_take_profit,
                bid_only_upside_grace=values.bid_only_upside_grace,
                bid_only_upside_exit=values.bid_only_upside_exit,
            ),
        ),
        risk=RiskPolicy(ruleset_version=ruleset_version, config_version=config_version),
        shadow_costs=_shadow_costs(priority_fee_lamports=priority),
        entry_notional_quote=values.entry_notional_quote,
        quote_base_units_per_unit=values.quote_base_units_per_unit,
        max_slippage_bps=values.max_slippage_bps,
        kill_switch_policy=kill_switch_policy,
        deep_candidate_limit=values.maximum_deep_candidates,
        liquidity_slippage=values.liquidity_slippage,
    )


def default_policy() -> PipelinePolicy:
    return guide_policy()


def load_pipeline_policy(environ: Mapping[str, str]) -> PipelinePolicy:
    """Select the rule bundle from configuration.

    ``RULESET_VERSION`` picks ``guide-v1`` (default) or ``shadow-hypothesis-v1``.
    ``RULESET_OVERRIDES_FILE`` points to YAML with ``config_version`` and a ``thresholds``
    mapping of ``GuidePolicySettings`` fields; tuned numbers must carry their own version.
    ``guide-v1`` always carries the GMGN rules (G18); ``GMGN_REQUIRED_FOR_LIVE=false``
    is the only way to let LIVE enter without fresh GMGN data. ``PRIORITY_FEE_MAX_LAMPORTS``
    (the executor's ceiling) caps the priority fee SHADOW books per transaction - the
    estimate ``shadow_priority_fee_lamports`` - in any ruleset.
    """

    version = environ.get(RULESET_VERSION_ENV) or DEFAULT_RULESET_VERSION
    overrides_path = environ.get(RULESET_OVERRIDES_ENV)
    interval = universe_interval(environ)
    ceiling = priority_fee_lamports(environ)
    if version == LEGACY_RULESET_VERSION:
        if overrides_path:
            raise ValueError("threshold overrides apply to guide-v1 only")
        return hypothesis_policy(universe_interval=interval, priority_fee_ceiling_lamports=ceiling)
    if version != GUIDE_RULESET_VERSION:
        raise ValueError(f"unknown ruleset version {version!r}")
    required = gmgn_required_for_live(environ)
    if not overrides_path:
        return guide_policy(
            universe_interval=interval,
            gmgn_required_for_live=required,
            priority_fee_ceiling_lamports=ceiling,
        )
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
    return guide_policy(
        settings,
        config_version=config_version,
        universe_interval=interval,
        gmgn_required_for_live=required,
        priority_fee_ceiling_lamports=ceiling,
    )


def priority_fee_lamports(environ: Mapping[str, str]) -> int:
    """The ceiling ``PRIORITY_FEE_MAX_LAMPORTS``: empty means the executor's default (0.015
    SOL); like the executor, anything but a whole number refuses to start."""

    raw = (environ.get(PRIORITY_FEE_MAX_ENV) or "").strip()
    if not raw:
        return DEFAULT_PRIORITY_FEE_LAMPORTS
    if not raw.isdigit():
        raise ValueError(f"{PRIORITY_FEE_MAX_ENV} must be a whole number of lamports")
    return int(raw)


def gmgn_required_for_live(environ: Mapping[str, str]) -> bool:
    """``GMGN_REQUIRED_FOR_LIVE``: unset or empty keeps LIVE closed without fresh GMGN data."""

    raw = (environ.get(GMGN_REQUIRED_FOR_LIVE_ENV) or "").strip().lower()
    if not raw or raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    raise ValueError(f"{GMGN_REQUIRED_FOR_LIVE_ENV} must be true or false")


def _shadow_costs(slippage_bps: int = 50, priority_fee_lamports: int = 0) -> CostModel:
    return CostModel(
        slippage_bps=slippage_bps,
        network_fee_base_units=Decimal(0),
        jito_tip_base_units=Decimal(0),
        priority_fee_lamports=priority_fee_lamports,
    )
