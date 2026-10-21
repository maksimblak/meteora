"""Hard filters run before a transparent weighted score."""

from decimal import Decimal

from src.domain.features import SignalReadiness, VolumeSpikeFeatures
from src.domain.models.trading import TradingMode
from src.domain.screening.models import (
    CandidateDecision,
    CandidateStatus,
    DecisionContext,
    EvidenceItem,
    IntegrityStatus,
    RejectionCode,
    ScoreComponent,
    ScreeningPolicy,
    VolumeIntegrityAssessment,
)
from src.market.models import (
    AuthorityStatus,
    DataQuality,
    Freshness,
    PoolSnapshot,
    RiskSignal,
    TokenRiskSnapshot,
)


def assess_volume_integrity(
    token: TokenRiskSnapshot,
    features: VolumeSpikeFeatures,
    policy: ScreeningPolicy,
) -> VolumeIntegrityAssessment:
    """Require corroboration; a large spike alone is never labelled fake volume."""

    reasons: list[str] = []
    missing: list[str] = []
    if token.wash_trading is RiskSignal.UNSAFE:
        reasons.append("provider_wash_trading")

    count = features.transaction_count
    participants = features.unique_participants
    if count is None:
        missing.append("transaction_count")
    if participants is None:
        missing.append("unique_participants")
    if token.wash_trading is RiskSignal.UNKNOWN:
        missing.append("wash_trading")

    volume = features.current_short_volume
    if volume is not None and volume > 0 and count == 0:
        reasons.append("positive_volume_without_transactions")
    if volume is not None and volume > 0 and participants == 0:
        reasons.append("positive_volume_without_participants")
    if volume == 0 and count is not None and count > 0:
        reasons.append("transactions_without_volume")
    if participants == 0 and count is not None and count > 0:
        reasons.append("transactions_without_participants")
    if count is not None and participants is not None and participants > count:
        reasons.append("participants_exceed_transactions")
    if count is not None and participants is not None and participants > 0:
        concentration = Decimal(count) / Decimal(participants)
        if (
            participants < policy.minimum_unique_participants
            and concentration > policy.maximum_transactions_per_participant
        ):
            reasons.append("concentrated_participant_activity")
    if (
        features.turnover is not None
        and features.turnover > policy.maximum_turnover
        and participants is not None
        and participants < policy.minimum_unique_participants
    ):
        reasons.append("turnover_without_participant_breadth")

    if reasons:
        status = IntegrityStatus.CONFIRMED
    elif missing:
        status = IntegrityStatus.UNKNOWN
    else:
        status = IntegrityStatus.CLEAR
    return VolumeIntegrityAssessment(
        status=status,
        reasons=tuple(sorted(set(reasons))),
        missing_signals=tuple(sorted(set(missing))),
    )


class PoolScreener:
    def evaluate(
        self,
        pool: PoolSnapshot,
        token: TokenRiskSnapshot,
        features: VolumeSpikeFeatures,
        policy: ScreeningPolicy,
        *,
        context: DecisionContext,
        estimated_price_impact: Decimal,
    ) -> CandidateDecision:
        if isinstance(estimated_price_impact, (float, bool)):
            raise ValueError("estimated_price_impact must use Decimal")
        if estimated_price_impact < 0:
            raise ValueError("estimated_price_impact cannot be negative")
        if context.ruleset_version != features.ruleset_version:
            raise ValueError("context and feature ruleset versions differ")
        if context.config_version != features.config_version:
            raise ValueError("context and feature config versions differ")
        integrity = assess_volume_integrity(token, features, policy)
        reasons = self._hard_filter_reasons(
            pool,
            token,
            features,
            policy,
            integrity,
            estimated_price_impact,
        )
        missing = set(features.missing_signals)
        missing.update(token.missing_signals)
        missing.update(integrity.missing_signals)
        evidence = _evidence(pool, token, features, integrity, estimated_price_impact, policy)

        warming = features.readiness is SignalReadiness.WARMING_UP
        unknown_codes = {
            RejectionCode.TOKEN_RISK_UNKNOWN,
            RejectionCode.VOLUME_INTEGRITY_UNKNOWN,
        }
        non_terminal_codes = unknown_codes | {RejectionCode.HISTORY_WARMING_UP}
        explicit_danger = any(reason not in non_terminal_codes for reason in reasons)
        if explicit_danger:
            status = CandidateStatus.REJECT
            score = None
            components: tuple[ScoreComponent, ...] = ()
        elif warming:
            status = CandidateStatus.WARMING_UP
            score = None
            components = ()
        elif reasons and context.mode is TradingMode.SHADOW:
            status = CandidateStatus.UNKNOWN
            score, components = _score(features, token, policy)
        elif reasons:
            status = CandidateStatus.REJECT
            score = None
            components = ()
        else:
            score, components = _score(features, token, policy)
            if score < policy.minimum_score:
                reasons.append(RejectionCode.SCORE_BELOW_THRESHOLD)
                status = CandidateStatus.REJECT
            else:
                status = CandidateStatus.ACCEPT

        return CandidateDecision(
            status=status,
            pool_address=pool.pool.address,
            token_mint=token.mint,
            decision_at=features.decision_at,
            mode=context.mode,
            estimated_price_impact=estimated_price_impact,
            score=score,
            reasons=tuple(reasons),
            missing_signals=tuple(sorted(missing)),
            evidence=evidence,
            score_components=components,
            features=features,
            ruleset_version=context.ruleset_version,
            config_version=context.config_version,
            correlation_id=context.correlation_id,
        )

    @staticmethod
    def _hard_filter_reasons(
        pool: PoolSnapshot,
        token: TokenRiskSnapshot,
        features: VolumeSpikeFeatures,
        policy: ScreeningPolicy,
        integrity: VolumeIntegrityAssessment,
        estimated_price_impact: Decimal,
    ) -> list[RejectionCode]:
        reasons: list[RejectionCode] = []
        feature_pool_identity = (
            features.pool_address,
            features.base_mint,
            features.quote_mint,
        )
        pool_identity = (pool.pool.address, pool.pool.base_mint, pool.pool.quote_mint)
        if feature_pool_identity != pool_identity or token.mint != pool.pool.base_mint:
            reasons.append(RejectionCode.INPUT_IDENTITY_MISMATCH)
        if (
            pool.observed_at,
            pool.source_at,
            pool.ingested_at,
        ) != (
            features.observed_at,
            features.source_at,
            features.ingested_at,
        ):
            reasons.append(RejectionCode.SNAPSHOT_ALIGNMENT_MISMATCH)
        decision_at = features.decision_at
        if (
            pool.observed_at > decision_at
            or pool.source_at > decision_at
            or pool.ingested_at > decision_at
        ):
            reasons.append(RejectionCode.FUTURE_MARKET_DATA)
        if (
            token.observed_at > decision_at
            or token.source_at > decision_at
            or token.ingested_at > decision_at
        ):
            reasons.append(RejectionCode.FUTURE_TOKEN_RISK_DATA)
        if (
            features.observed_at > decision_at
            or features.source_at > decision_at
            or features.ingested_at > decision_at
        ):
            reasons.append(RejectionCode.FUTURE_FEATURE_DATA)
        if pool.tvl < policy.minimum_tvl:
            reasons.append(RejectionCode.LOW_TVL)
        if pool.depth is None or pool.depth < policy.minimum_depth:
            reasons.append(RejectionCode.LOW_DEPTH)
        if pool.pool_age < policy.minimum_pool_age:
            reasons.append(RejectionCode.POOL_TOO_YOUNG)
        if features.readiness is SignalReadiness.WARMING_UP:
            reasons.append(RejectionCode.HISTORY_WARMING_UP)
        if pool.freshness is Freshness.STALE or features.freshness is Freshness.STALE:
            reasons.append(RejectionCode.STALE_MARKET_DATA)
        if pool.data_quality is not DataQuality.COMPLETE or (
            features.data_quality is not DataQuality.COMPLETE
            and features.readiness is SignalReadiness.READY
        ):
            reasons.append(RejectionCode.INCOMPLETE_MARKET_DATA)
        if token.mint_authority is AuthorityStatus.PRESENT:
            reasons.append(RejectionCode.MINT_AUTHORITY_PRESENT)
        if token.freeze_authority is AuthorityStatus.PRESENT:
            reasons.append(RejectionCode.FREEZE_AUTHORITY_PRESENT)
        if token.blacklisted is RiskSignal.UNSAFE:
            reasons.append(RejectionCode.BLACKLISTED_TOKEN)
        if token.verified is RiskSignal.UNSAFE:
            reasons.append(RejectionCode.UNVERIFIED_TOKEN)
        if (
            token.holder_concentration is not None
            and token.holder_concentration > policy.maximum_holder_concentration
        ):
            reasons.append(RejectionCode.HOLDER_CONCENTRATION_HIGH)
        if token.freshness is Freshness.STALE:
            reasons.append(RejectionCode.TOKEN_RISK_STALE)
        if token.data_quality is not DataQuality.COMPLETE or token.missing_signals:
            reasons.append(RejectionCode.TOKEN_RISK_UNKNOWN)
        if integrity.status is IntegrityStatus.CONFIRMED:
            reasons.append(RejectionCode.ARTIFICIAL_VOLUME_CONFIRMED)
        elif integrity.status is IntegrityStatus.UNKNOWN:
            reasons.append(RejectionCode.VOLUME_INTEGRITY_UNKNOWN)
        if estimated_price_impact > policy.maximum_price_impact:
            reasons.append(RejectionCode.PRICE_IMPACT_HIGH)
        if features.readiness is SignalReadiness.READY and (
            features.short_ratio is None
            or features.long_ratio is None
            or features.robust_z_score is None
            or features.fee_tvl is None
            or features.short_ratio < policy.minimum_short_ratio
            or features.long_ratio < policy.minimum_long_ratio
            or features.robust_z_score < policy.minimum_robust_z_score
            or features.fee_tvl < policy.minimum_fee_tvl
        ):
            reasons.append(RejectionCode.VOLUME_SIGNAL_BELOW_THRESHOLD)
        return reasons


def _score(
    features: VolumeSpikeFeatures,
    token: TokenRiskSnapshot,
    policy: ScreeningPolicy,
) -> tuple[Decimal, tuple[ScoreComponent, ...]]:
    values = (
        (
            "volume_acceleration",
            _cap(features.short_ratio, policy.target_short_ratio),
            policy.weights.volume_acceleration,
        ),
        (
            "robust_volume",
            _cap(features.robust_z_score, policy.target_robust_z_score),
            policy.weights.robust_volume,
        ),
        (
            "long_confirmation",
            _cap(features.long_ratio, policy.target_long_ratio),
            policy.weights.long_confirmation,
        ),
        (
            "fee_opportunity",
            _cap(_net_edge(features.fee_tvl, policy.assumed_cost_rate), policy.target_fee_tvl),
            policy.weights.fee_opportunity,
        ),
        (
            "liquidity_stability",
            _stability(features.tvl_change),
            policy.weights.liquidity_stability,
        ),
        (
            "volatility_fit",
            _volatility_fit(features.price_volatility, policy.target_volatility),
            policy.weights.volatility_fit,
        ),
        ("token_quality", _token_quality(token), policy.weights.token_quality),
    )
    components = tuple(
        ScoreComponent(
            name=name,
            normalized_value=value,
            weight=weight,
            contribution=value * weight,
        )
        for name, value, weight in values
    )
    return sum((item.contribution for item in components), Decimal(0)), components


def _cap(value: Decimal | None, target: Decimal) -> Decimal:
    if value is None or value <= 0:
        return Decimal(0)
    return min(value / target, Decimal(1))


def _net_edge(fee_tvl: Decimal | None, cost: Decimal) -> Decimal:
    if fee_tvl is None:
        return Decimal(0)
    return max(fee_tvl - cost, Decimal(0))


def _stability(change: Decimal | None) -> Decimal:
    if change is None:
        return Decimal(0)
    return max(Decimal(0), Decimal(1) - min(abs(change), Decimal(1)))


def _volatility_fit(volatility: Decimal | None, target: Decimal) -> Decimal:
    if volatility is None:
        return Decimal(0)
    distance = abs(volatility - target) / target
    return max(Decimal(0), Decimal(1) - min(distance, Decimal(1)))


def _token_quality(token: TokenRiskSnapshot) -> Decimal:
    return Decimal(1) if token.data_quality is DataQuality.COMPLETE else Decimal(0)


def _evidence(
    pool: PoolSnapshot,
    token: TokenRiskSnapshot,
    features: VolumeSpikeFeatures,
    integrity: VolumeIntegrityAssessment,
    price_impact: Decimal,
    policy: ScreeningPolicy,
) -> tuple[EvidenceItem, ...]:
    return (
        EvidenceItem(name="tvl", value=str(pool.tvl), threshold=str(policy.minimum_tvl)),
        EvidenceItem(name="depth", value=str(pool.depth), threshold=str(policy.minimum_depth)),
        EvidenceItem(
            name="short_ratio",
            value=str(features.short_ratio),
            threshold=str(policy.minimum_short_ratio),
        ),
        EvidenceItem(
            name="long_ratio",
            value=str(features.long_ratio),
            threshold=str(policy.minimum_long_ratio),
        ),
        EvidenceItem(
            name="robust_z_score",
            value=str(features.robust_z_score),
            threshold=str(policy.minimum_robust_z_score),
        ),
        EvidenceItem(
            name="fee_tvl", value=str(features.fee_tvl), threshold=str(policy.minimum_fee_tvl)
        ),
        EvidenceItem(
            name="price_impact", value=str(price_impact), threshold=str(policy.maximum_price_impact)
        ),
        EvidenceItem(name="volume_integrity", value=integrity.status.value),
        EvidenceItem(name="token_data_quality", value=token.data_quality.value),
    )
