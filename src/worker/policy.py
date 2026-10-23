"""One versioned bundle of every rule the worker applies.

The defaults are explicit *hypotheses for shadow validation* (spec 6.2), not
promises of profit; every decision record stores the ruleset and config version
that produced it, so changing any number here requires a new version string.
"""

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from src.accounting.models import CostModel
from src.domain.features import VolumeFeatureRules
from src.domain.risk.models import RiskPolicy
from src.domain.screening import ScreeningPolicy
from src.domain.strategy import StrategyPolicy
from src.modes.models import KillSwitchPolicy

DEFAULT_RULESET_VERSION = "shadow-hypothesis-v1"
DEFAULT_CONFIG_VERSION = "worker-config-v1"


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
    ruleset_version: str = DEFAULT_RULESET_VERSION,
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
        shadow_costs=CostModel(
            slippage_bps=50,
            network_fee_base_units=Decimal(0),
            jito_tip_base_units=Decimal(0),
        ),
        entry_notional_quote=entry_notional_quote,
        quote_base_units_per_unit=quote_base_units_per_unit,
        max_slippage_bps=max_slippage_bps,
        kill_switch_policy=kill_switch_policy,
    )
