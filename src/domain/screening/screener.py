"""Hard filters run before a transparent weighted score."""

import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from src.domain.features import SignalReadiness, VolumeSpikeFeatures
from src.domain.models.trading import TradingMode
from src.domain.screening.models import (
    CandidateDecision,
    CandidateStatus,
    DecisionContext,
    DecisionFlag,
    DepthEvidence,
    DepthSource,
    EvidenceItem,
    GmgnScreeningRules,
    GuideAssessment,
    GuideScreeningRules,
    IntegrityStatus,
    RejectionCode,
    ScoreComponent,
    ScreeningPolicy,
    TokenSelection,
    VolumeIntegrityAssessment,
    VolumeIntegrityMode,
    VolumeRegime,
)
from src.market.models import (
    AuthorityStatus,
    DataQuality,
    EnhancedTokenSignals,
    Freshness,
    PoolSnapshot,
    RiskSignal,
    TokenRiskSnapshot,
    WindowOrigin,
)


def assess_volume_integrity(
    token: TokenRiskSnapshot,
    features: VolumeSpikeFeatures,
    policy: ScreeningPolicy,
) -> VolumeIntegrityAssessment:
    """Require corroboration; a large spike alone is never labelled fake volume.

    ``PARTICIPANTS``: transaction and participant counts and a wash-trading verdict are
    mandatory. ``AVAILABLE_SIGNALS`` (D07, the Data API has no counts): burst
    self-similarity is mandatory (G24 dropped fee organicity); counts are checked when present,
    and their absence (with no wash-trading verdict either) is the
    ``VOLUME_PARTICIPANTS_UNAVAILABLE`` flag instead of an unknown.
    """

    reasons: list[str] = []
    missing: list[str] = []
    flags: list[DecisionFlag] = []
    if token.wash_trading is RiskSignal.UNSAFE:
        reasons.append("provider_wash_trading")

    count = features.transaction_count
    participants = features.unique_participants
    if policy.volume_integrity is VolumeIntegrityMode.AVAILABLE_SIGNALS:
        reasons.extend(_available_signal_reasons(features, policy, missing))
        if (count is None or participants is None) and token.wash_trading is RiskSignal.UNKNOWN:
            flags.append(DecisionFlag.VOLUME_PARTICIPANTS_UNAVAILABLE)
    else:
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
        flags=tuple(flags),
    )


def _available_signal_reasons(
    features: VolumeSpikeFeatures, policy: ScreeningPolicy, missing: list[str]
) -> list[str]:
    """Artificial volume on the signals a source without participant counts still has.

    Self-similar bursts only. Fees over ``volume x base fee`` cannot expose wash trading in
    DLMM - every swap pays at least the base fee, so the ratio is one or more by
    construction (G24); the author's own signal, total fees too low for the token's cap at
    a tenfold hourly turnover, is judged with the guide thresholds instead.
    """

    reasons: list[str] = []
    spread = features.short_burst_spread
    if spread is None:
        missing.append("short_burst_spread")
    elif spread <= policy.maximum_burst_spread:
        reasons.append("self_similar_bursts")
    return reasons


# Signals a policy may accept as missing: covered by a flag, never by UNKNOWN.
def _flagged_token_signals(policy: ScreeningPolicy) -> frozenset[str]:
    if policy.volume_integrity is VolumeIntegrityMode.AVAILABLE_SIGNALS:
        return frozenset({"wash_trading"})
    return frozenset()


def merge_pool_token_signals(token: TokenRiskSnapshot, pool: PoolSnapshot) -> TokenRiskSnapshot:
    """The token's risk with the Data API's own flags for the pool and its base token (D07).

    ``is_blacklisted`` of the pool is the blacklist signal, ``is_verified`` the verification
    and ``freeze_authority_disabled`` the freeze authority of the base mint. They fill what
    the other sources left unknown; where both report, the more dangerous reading wins.
    """

    if token.mint != pool.pool.base_mint:
        return token
    blacklisted = _merge_signal(token.blacklisted, _danger(pool.pool_blacklisted))
    verified = _merge_signal(token.verified, _danger(_negated(pool.base_token_verified)))
    freeze = _merge_authority(token.freeze_authority, pool.base_freeze_authority_disabled)
    if (blacklisted, verified, freeze) == (
        token.blacklisted,
        token.verified,
        token.freeze_authority,
    ):
        return token
    resolved = {
        name
        for name, value in (
            ("blacklisted", blacklisted),
            ("verified", verified),
            ("freeze_authority", freeze),
        )
        if value not in (RiskSignal.UNKNOWN, AuthorityStatus.UNKNOWN)
    }
    missing = tuple(name for name in token.missing_signals if name not in resolved)
    quality = token.data_quality
    if quality is DataQuality.PARTIAL and not missing:
        quality = DataQuality.COMPLETE
    data = token.model_dump()
    data.update(
        blacklisted=blacklisted,
        verified=verified,
        freeze_authority=freeze,
        missing_signals=missing,
        data_quality=quality,
        provider=f"{token.provider}+{pool.provider}",
    )
    return TokenRiskSnapshot.model_validate(data)


def merge_enhanced_signals(
    token: TokenRiskSnapshot, signals: EnhancedTokenSignals
) -> TokenRiskSnapshot:
    """The token's risk with an enhanced provider's reading attached (GMGN, G18).

    The reading is kept as received, with its own source time and freshness, for the GMGN
    thresholds and the decision record. Only a fresh reading fills what the token lacks:
    the total fees of the conditional D03 threshold. The token's own risk signals,
    freshness and missing signals stay as they are, and its RPC top-10 share is
    cross-checked against the reading's own figure, never replaced by it. ``ingested_at``
    moves up to the reading's source time, so no input of the token is newer than it.
    """

    data = token.model_dump()
    data.update(
        enhanced=signals,
        provider=f"{token.provider}+{signals.provider}",
        ingested_at=max(token.ingested_at, signals.source_at),
    )
    if (
        signals.freshness is Freshness.FRESH
        and token.total_fees_sol is None
        and signals.total_fees_sol is not None
    ):
        data["total_fees_sol"] = signals.total_fees_sol
    return TokenRiskSnapshot.model_validate(data)


def _negated(value: bool | None) -> bool | None:
    return None if value is None else not value


def _danger(value: bool | None) -> RiskSignal:
    if value is None:
        return RiskSignal.UNKNOWN
    return RiskSignal.UNSAFE if value else RiskSignal.SAFE


def _merge_signal(current: RiskSignal, reported: RiskSignal) -> RiskSignal:
    if RiskSignal.UNSAFE in (current, reported):
        return RiskSignal.UNSAFE
    if RiskSignal.SAFE in (current, reported):
        return RiskSignal.SAFE
    return RiskSignal.UNKNOWN


def _merge_authority(current: AuthorityStatus, disabled: bool | None) -> AuthorityStatus:
    if current is AuthorityStatus.PRESENT or disabled is False:
        return AuthorityStatus.PRESENT
    if current is AuthorityStatus.REVOKED or disabled is True:
        return AuthorityStatus.REVOKED
    return AuthorityStatus.UNKNOWN


# GMGN and the selection report 5-minute and one-hour volumes.
_SHORT_MINUTES = Decimal(5)
_HOUR_MINUTES = Decimal(60)


class TokenVolumeSource(StrEnum):
    """Where the token-level volume of a decision came from (G24)."""

    GMGN = "GMGN"  # a fresh enhanced reading over every pool of the token
    DLMM_POOLS = "DLMM_POOLS"  # the sum of the token's DLMM pools (``TOKEN_VOLUME_POOL_ONLY``)
    PENDING = "PENDING"  # the enhanced source is asked only after the cheap filters


@dataclass(frozen=True, slots=True)
class _TokenVolume:
    source: TokenVolumeSource
    per_minute: Decimal | None
    hourly_per_minute: Decimal | None
    dlmm_pools: int


def _fresh_reading(token: TokenRiskSnapshot) -> EnhancedTokenSignals | None:
    reading = token.enhanced
    return reading if reading is not None and reading.freshness is Freshness.FRESH else None


def _token_volume(
    token: TokenRiskSnapshot, features: VolumeSpikeFeatures, selection: TokenSelection
) -> _TokenVolume:
    """The token's volume as the author reads it in GMGN: over every pool of the token.

    A fresh enhanced reading with both windows is that figure. Without one the token's
    DLMM pools are summed - this pool's own windows plus its siblings' - and the decision is
    flagged ``TOKEN_VOLUME_POOL_ONLY``; on other venues the token may trade far more. While
    an enhanced source is still to be asked (``enhanced_pending``), nothing is decided.
    """

    pools = 1 + selection.sibling_pools
    reading = _fresh_reading(token)
    short = reading.volume_5m_usd if reading is not None else None
    hour = reading.volume_1h_usd if reading is not None else None
    if short is not None and hour is not None:
        return _TokenVolume(
            TokenVolumeSource.GMGN, short / _SHORT_MINUTES, hour / _HOUR_MINUTES, pools
        )
    if selection.enhanced_pending:
        return _TokenVolume(TokenVolumeSource.PENDING, None, None, pools)
    own_now = features.volume_per_minute
    own_hour = features.hourly_volume_per_minute
    return _TokenVolume(
        TokenVolumeSource.DLMM_POOLS,
        None if own_now is None else own_now + selection.sibling_volume_5m / _SHORT_MINUTES,
        None if own_hour is None else own_hour + selection.sibling_volume_1h / _HOUR_MINUTES,
        pools,
    )


def _volume_spike(features: VolumeSpikeFeatures, policy: ScreeningPolicy) -> bool | None:
    """Spec 6.2's spike: 5m against the baseline median, robust z and 30m confirmation."""

    ratio, robust, confirmation = features.short_ratio, features.robust_z_score, features.long_ratio
    if features.readiness is not SignalReadiness.READY or None in (ratio, robust, confirmation):
        return None
    assert ratio is not None and robust is not None and confirmation is not None
    return (
        ratio >= policy.minimum_short_ratio
        and robust >= policy.minimum_robust_z_score
        and confirmation >= policy.minimum_long_ratio
    )


def _normalized_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")


def _theme(pool: PoolSnapshot, words: tuple[str, ...]) -> str | None:
    """The first excluded word found in the base token's name or ticker, if any."""

    texts = [
        _normalized_text(text)
        for text in (pool.base_token_name, pool.base_token_symbol)
        if text is not None
    ]
    for word in words:
        needle = _normalized_text(word.strip())
        if any(needle in text for text in texts):
            return word
    return None


def assess_guide_thresholds(
    pool: PoolSnapshot,
    token: TokenRiskSnapshot,
    features: VolumeSpikeFeatures,
    policy: ScreeningPolicy,
    selection: TokenSelection | None = None,
) -> GuideAssessment:
    """Apply the guide-v1 table (spec 6.6) and the author's selection (spec 6.8, G24).

    A signal no source reported is UNKNOWN. Volume, market cap and total fees are the
    token's (see ``_token_volume``); a fresh GMGN market cap replaces the Data API's.
    """

    rules = policy.guide
    if rules is None:
        raise ValueError("policy has no guide thresholds")
    chosen = selection or TokenSelection()
    reasons: list[RejectionCode] = []
    missing: list[str] = []
    evidence: list[EvidenceItem] = []
    flags: list[DecisionFlag] = []
    reading = _fresh_reading(token)

    theme = _theme(pool, rules.excluded_theme_words)
    if theme is not None:
        reasons.append(RejectionCode.THEME_EXCLUDED)
    named = pool.base_token_name is not None or pool.base_token_symbol is not None
    if not named:
        # Nothing to match the theme against: SHADOW goes on with the flag, APPROVAL and
        # LIVE treat it as the missing signal ``theme`` (see ``PoolScreener.evaluate``).
        flags.append(DecisionFlag.THEME_UNKNOWN)
    evidence.append(
        EvidenceItem(name="theme", value=theme or ("none" if named else "UNKNOWN"))
    )

    gmgn_cap = reading.market_cap_usd if reading is not None else None
    cap = gmgn_cap if gmgn_cap is not None else pool.base_token_market_cap
    if cap is None:
        missing.append("market_cap")
    elif cap < rules.minimum_market_cap:
        reasons.append(RejectionCode.MARKET_CAP_BELOW_THRESHOLD)
    evidence.append(_item("market_cap", cap, rules.minimum_market_cap))
    if gmgn_cap is not None:
        cap_source = "GMGN"
    else:
        cap_source = "DATA_API" if cap is not None else "UNKNOWN"
    evidence.append(EvidenceItem(name="market_cap_source", value=cap_source))
    evidence.append(
        EvidenceItem(
            name="market_cap_preferred",
            value="unknown" if cap is None else str(cap >= rules.preferred_market_cap).lower(),
            threshold=str(rules.preferred_market_cap),
        )
    )

    age, exact = _token_age(pool, token, features.decision_at)
    if age < rules.minimum_token_age:
        if exact:
            reasons.append(RejectionCode.TOKEN_TOO_YOUNG)
        else:
            missing.append("token_age")
    if age >= rules.revived_token_age:
        flags.append(DecisionFlag.TOKEN_REVIVED)
    evidence.append(
        EvidenceItem(
            name="token_age_seconds",
            value=str(int(age.total_seconds())),
            threshold=str(int(rules.minimum_token_age.total_seconds())),
        )
    )
    evidence.append(
        _item("top10_holder_share", token.holder_concentration, policy.maximum_holder_concentration)
    )

    volume = _token_volume(token, features, chosen)
    if volume.source is TokenVolumeSource.DLMM_POOLS:
        flags.append(DecisionFlag.TOKEN_VOLUME_POOL_ONLY)
    evidence.append(EvidenceItem(name="token_volume_source", value=volume.source.value))
    evidence.append(EvidenceItem(name="token_pool_count", value=str(volume.dlmm_pools)))
    per_minute = volume.per_minute
    hourly = volume.hourly_per_minute
    if volume.source is TokenVolumeSource.PENDING:
        missing.append("token_volume")
        regime = VolumeRegime.UNKNOWN
    elif per_minute is None:
        missing.append("volume_per_minute")
        regime = VolumeRegime.UNKNOWN
    elif per_minute < rules.minimum_volume_per_minute:
        reasons.append(RejectionCode.VOLUME_PER_MINUTE_BELOW_THRESHOLD)
        regime = VolumeRegime.BELOW
    elif per_minute >= rules.overvolume_volume_per_minute:
        regime = VolumeRegime.OVERVOLUME
    else:
        regime = VolumeRegime.WORKING
    if volume.source is not TokenVolumeSource.PENDING:
        if hourly is None:
            missing.append("hourly_volume_per_minute")
        elif hourly < rules.minimum_hourly_volume_per_minute:
            reasons.append(RejectionCode.HOURLY_VOLUME_PER_MINUTE_BELOW_THRESHOLD)
    evidence.append(_item("volume_per_minute", per_minute, rules.minimum_volume_per_minute))
    evidence.append(
        _item("hourly_volume_per_minute", hourly, rules.minimum_hourly_volume_per_minute)
    )
    evidence.append(
        EvidenceItem(
            name="volume_regime",
            value=regime.value,
            threshold=(
                f"working>={rules.minimum_volume_per_minute}; "
                f"overvolume>={rules.overvolume_volume_per_minute}"
            ),
        )
    )
    spike = _volume_spike(features, policy)
    if spike:
        flags.append(DecisionFlag.VOLUME_SPIKE)
    evidence.append(
        EvidenceItem(
            name="volume_spike",
            value="UNKNOWN" if spike is None else str(spike).lower(),
            threshold=(
                f"short>={policy.minimum_short_ratio}; "
                f"robust_z>={policy.minimum_robust_z_score}; "
                f"long>={policy.minimum_long_ratio}"
            ),
        )
    )

    turnover = features.volume_to_liquidity
    if turnover is None:
        missing.append("volume_to_liquidity")
    elif turnover < rules.minimum_volume_to_liquidity:
        reasons.append(RejectionCode.VOLUME_TO_LIQUIDITY_BELOW_THRESHOLD)
    evidence.append(_item("volume_to_liquidity", turnover, rules.minimum_volume_to_liquidity))
    # The pool that is evaluated: the token's main pool (largest LP fees in the hour).
    hour = pool.windows.get("1h")
    evidence.extend(
        (
            EvidenceItem(
                name="pool_base_fee_rate",
                value="UNKNOWN" if pool.base_fee_rate is None else str(pool.base_fee_rate),
            ),
            EvidenceItem(name="pool_bin_step", value=str(pool.bin_step)),
            EvidenceItem(
                name="pool_fees_1h",
                value="UNKNOWN" if hour is None or hour.fees is None else str(hour.fees),
            ),
            # Information only since G24: fees over ``volume x base fee`` is >= 1 in DLMM.
            _reported("fee_efficiency", features.fee_efficiency, missing="UNKNOWN"),
        )
    )

    # D03: a conditional threshold. Applied when an enhanced provider reports it; when no
    # source does, the decision is flagged for a manual check instead of failing closed.
    total_fees = token.total_fees_sol
    if total_fees is None:
        flags.append(DecisionFlag.TOTAL_FEES_UNAVAILABLE)
    elif total_fees < rules.minimum_total_fees_sol:
        reasons.append(RejectionCode.TOTAL_FEES_BELOW_THRESHOLD)
    evidence.append(
        EvidenceItem(
            name="total_fees_sol",
            value="UNAVAILABLE" if total_fees is None else str(total_fees),
            threshold=str(rules.minimum_total_fees_sol),
        )
    )
    reasons.extend(_artificial_volume(token, features, rules, cap, evidence))

    if missing:
        reasons.append(RejectionCode.GUIDE_SIGNAL_UNKNOWN)
    return GuideAssessment(
        reasons=tuple(dict.fromkeys(reasons)),
        missing_signals=tuple(sorted(missing)),
        evidence=tuple(evidence),
        volume_regime=regime,
        flags=tuple(flags),
        hourly_volume_per_minute=hourly,
        volume_spike=spike,
    )


def _artificial_volume(
    token: TokenRiskSnapshot,
    features: VolumeSpikeFeatures,
    rules: GuideScreeningRules,
    cap: Decimal | None,
    evidence: list[EvidenceItem],
) -> list[RejectionCode]:
    """The author's signal of inorganic volume (video 2, Savash: 980k an hour on 87k).

    Total fees too low for the cap - the author's 30 SOL at a 500k cap, scaled with the
    cap (``minimum_total_fees_sol x cap / total_fees_reference_market_cap``; both sides are
    SOL, so no SOL price is needed) - while the pool turned over at least
    ``maximum_hourly_turnover`` times its TVL in the hour. GMGN's own wash-trading verdict
    rejects too. Without total fees the rule waits (D03 flag).
    """

    reasons: list[RejectionCode] = []
    required = (
        None
        if cap is None
        else rules.minimum_total_fees_sol * cap / rules.total_fees_reference_market_cap
    )
    turnover = features.hourly_turnover
    total_fees = token.total_fees_sol
    if (
        total_fees is not None
        and required is not None
        and turnover is not None
        and total_fees < required
        and turnover >= rules.maximum_hourly_turnover
    ):
        reasons.append(RejectionCode.ARTIFICIAL_VOLUME_SUSPECTED)
    evidence.append(
        EvidenceItem(
            name="required_total_fees_sol",
            value="UNKNOWN" if required is None else str(required),
            threshold=(
                f"{rules.minimum_total_fees_sol} x market_cap / "
                f"{rules.total_fees_reference_market_cap}"
            ),
        )
    )
    evidence.append(
        _item("pool_hourly_turnover", turnover, rules.maximum_hourly_turnover)
    )
    reading = token.enhanced
    if reading is not None:
        fresh = reading.freshness is Freshness.FRESH
        if fresh and reading.wash_trading is RiskSignal.UNSAFE:
            reasons.append(RejectionCode.ARTIFICIAL_VOLUME_SUSPECTED)
        evidence.append(
            EvidenceItem(
                name="gmgn_wash_trading",
                value=reading.wash_trading.value if fresh else _UNAVAILABLE,
            )
        )
    return reasons


_UNAVAILABLE = "UNAVAILABLE"


def _gmgn_assessment(
    token: TokenRiskSnapshot, rules: GmgnScreeningRules, mode: TradingMode
) -> tuple[list[RejectionCode], list[EvidenceItem], list[DecisionFlag]]:
    """GMGN holder structure (spec 6.7): reasons, evidence and flags of one token.

    Only a fresh reading is applied. A reading that is stale, unavailable or misses a
    threshold metric (e.g. a changed GMGN format) is ``GMGN_UNAVAILABLE``; the metrics it
    did report still reject. A token no enhanced source read (the service is not wired)
    is ``GMGN_UNAVAILABLE`` too, with ``gmgn_status=NOT_CONFIGURED``: never "all clear".
    Outside LIVE the flag changes no status; LIVE is closed while ``required_for_live``.
    """

    closes_live = mode is TradingMode.LIVE and rules.required_for_live
    reading = token.enhanced
    if reading is None:
        return (
            [RejectionCode.GMGN_UNAVAILABLE] if closes_live else [],
            [EvidenceItem(name="gmgn_status", value="NOT_CONFIGURED")],
            [DecisionFlag.GMGN_UNAVAILABLE],
        )
    fresh = reading.freshness is Freshness.FRESH
    if fresh:
        status = "FRESH"
    elif reading.detail:
        status = f"{_UNAVAILABLE}:{reading.detail}"
    else:
        status = Freshness.STALE.value
    reported = (
        reading.insiders_share,
        reading.bundling_share,
        reading.phishing_share,
        reading.top10_holder_share,
        reading.developer_share,
        reading.total_fees_sol,
    )
    has_values = any(value is not None for value in reported)
    evidence = [
        EvidenceItem(name="gmgn_status", value=status),
        EvidenceItem(
            name="gmgn_source_at",
            value=reading.source_at.isoformat() if fresh or has_values else _UNAVAILABLE,
        ),
    ]
    reasons: list[RejectionCode] = []
    flags: list[DecisionFlag] = []
    complete = fresh
    for name, value, maximum, code in (
        (
            "insiders_share",
            reading.insiders_share,
            rules.maximum_insiders_share,
            RejectionCode.INSIDERS_SHARE_HIGH,
        ),
        (
            "bundling_share",
            reading.bundling_share,
            rules.maximum_bundling_share,
            RejectionCode.BUNDLING_SHARE_HIGH,
        ),
        (
            "phishing_share",
            reading.phishing_share,
            rules.maximum_phishing_share,
            RejectionCode.PHISHING_SHARE_HIGH,
        ),
        (
            "gmgn_top10_holder_share",
            reading.top10_holder_share,
            rules.maximum_top10_holder_share,
            RejectionCode.GMGN_TOP10_HOLDER_SHARE_HIGH,
        ),
    ):
        used = value if fresh else None
        if used is None:
            complete = False
        elif used >= maximum:
            # The guide asks for "below the maximum": the maximum itself already fails.
            reasons.append(code)
        evidence.append(
            EvidenceItem(
                name=name, value=_UNAVAILABLE if used is None else str(used), threshold=str(maximum)
            )
        )
    developer = reading.developer_share if fresh else None
    if developer is None:
        # G24: LIVE rejects a developer share above zero, so an unknown share leaves the
        # reading incomplete like any threshold metric (LIVE closed while required).
        complete = False
    evidence.append(
        EvidenceItem(
            name="developer_share",
            value=_UNAVAILABLE if developer is None else str(developer),
            threshold="0",
        )
    )
    if developer is not None and developer > 0:
        flags.append(DecisionFlag.DEVELOPER_HOLDS_TOKENS)
        if mode is TradingMode.LIVE:
            # LIVE has no operator to tick "the developer's supply is checked" (G24, No 6).
            reasons.append(RejectionCode.DEVELOPER_HOLDS_TOKENS)
    status_value = reading.developer_status if fresh else None
    evidence.append(
        EvidenceItem(
            name="developer_status",
            value=_UNAVAILABLE if status_value is None else status_value.value,
        )
    )
    snipers = reading.sniper_count if fresh else None
    if snipers is not None and snipers > rules.maximum_sniper_count:
        flags.append(DecisionFlag.SNIPERS_HIGH)
    evidence.extend(
        (
            EvidenceItem(
                name="gmgn_sniper_count",
                value=_UNAVAILABLE if snipers is None else str(snipers),
                threshold=str(rules.maximum_sniper_count),
            ),
            _reported("gmgn_sniper_share", reading.sniper_share if fresh else None),
            # The token's own all-time high (G24): the Spot-on-ATH rule reads it (T19).
            _reported("gmgn_token_ath_price", reading.ath_price_usd if fresh else None),
            _reported("gmgn_token_ath_market_cap", reading.ath_market_cap_usd if fresh else None),
        )
    )
    if not complete:
        flags.append(DecisionFlag.GMGN_UNAVAILABLE)
        if closes_live:
            reasons.append(RejectionCode.GMGN_UNAVAILABLE)
    return reasons, evidence, flags


def _token_age(
    pool: PoolSnapshot, token: TokenRiskSnapshot, decision_at: datetime
) -> tuple[timedelta, bool]:
    """Lower bound on token age and whether it is exact.

    A pool cannot predate its base mint, so pool age bounds token age from below; the
    oldest mint activity from RPC is exact only when the signature history was exhausted.
    A fresh enhanced reading's creation time (GMGN ``creation_timestamp``) is exact.
    """

    bounds = [decision_at - pool.created_at]
    if token.earliest_activity_at is not None:
        bounds.append(decision_at - token.earliest_activity_at)
    reading = _fresh_reading(token)
    created = reading.created_at if reading is not None else None
    if created is not None and created <= decision_at:
        bounds.append(decision_at - created)
    return max(bounds), token.activity_history_complete or (
        created is not None and created <= decision_at
    )


def _item(name: str, value: Decimal | None, threshold: Decimal) -> EvidenceItem:
    return EvidenceItem(
        name=name, value="UNKNOWN" if value is None else str(value), threshold=str(threshold)
    )


def _reported(name: str, value: Decimal | None, *, missing: str = _UNAVAILABLE) -> EvidenceItem:
    return EvidenceItem(name=name, value=missing if value is None else str(value))


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
        depth: DepthEvidence | None = None,
        selection: TokenSelection | None = None,
    ) -> CandidateDecision:
        """``depth``: measured depth and impact (D07); ``None`` uses the snapshot's depth.

        ``selection``: what the cycle knows about the base token beyond this pool (G24);
        ``None`` is a token with this one pool, not held, not ranked."""

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
            depth,
        )
        flagged = _flagged_token_signals(policy)
        missing = set(features.missing_signals)
        missing.update(set(token.missing_signals) - flagged)
        missing.update(integrity.missing_signals)
        evidence = _evidence(
            pool, token, features, integrity, estimated_price_impact, policy, depth
        )
        flags: list[DecisionFlag] = []
        guide: GuideAssessment | None = None
        if policy.guide is not None:
            guide = assess_guide_thresholds(pool, token, features, policy, selection)
            reasons.extend(guide.reasons)
            missing.update(guide.missing_signals)
            evidence = (*evidence, *guide.evidence)
            flags.extend(guide.flags)
            if policy.guide.gmgn is not None:
                gmgn_reasons, gmgn_evidence, gmgn_flags = _gmgn_assessment(
                    token, policy.guide.gmgn, context.mode
                )
                reasons.extend(gmgn_reasons)
                evidence = (*evidence, *gmgn_evidence)
                flags.extend(gmgn_flags)
        flags.extend(integrity.flags)
        if DecisionFlag.THEME_UNKNOWN in flags and context.mode is not TradingMode.SHADOW:
            missing.add("theme")
            reasons.append(RejectionCode.GUIDE_SIGNAL_UNKNOWN)
        if depth is not None and depth.source is DepthSource.UNAVAILABLE:
            flags.append(DecisionFlag.DEPTH_UNAVAILABLE)
            if context.mode is not TradingMode.SHADOW:
                reasons.append(RejectionCode.DEPTH_UNAVAILABLE)
        if (
            DecisionFlag.VOLUME_PARTICIPANTS_UNAVAILABLE in flags
            and context.mode is TradingMode.LIVE
        ):
            reasons.append(RejectionCode.VOLUME_PARTICIPANTS_UNAVAILABLE)
        if not token.source_configured:
            flags.append(DecisionFlag.TOKEN_RISK_UNAVAILABLE)
            if context.mode is not TradingMode.SHADOW:
                reasons.append(RejectionCode.TOKEN_RISK_UNAVAILABLE)
        if selection is not None:
            # One position per token (G24, No 24): any pool of a token already held.
            if selection.held:
                reasons.append(RejectionCode.TOKEN_ALREADY_HELD)
            if selection.rank is not None:
                evidence = (
                    *evidence,
                    EvidenceItem(
                        name="token_volume_rank",
                        value=str(selection.rank),
                        threshold=None
                        if selection.rank_limit is None
                        else str(selection.rank_limit),
                    ),
                )
                if selection.rank_limit is not None and selection.rank > selection.rank_limit:
                    reasons.append(RejectionCode.CANDIDATE_RANK_BELOW_LIMIT)
        reasons = list(dict.fromkeys(reasons))

        warming = features.readiness is SignalReadiness.WARMING_UP
        unknown_codes = {
            RejectionCode.TOKEN_RISK_UNKNOWN,
            RejectionCode.TOKEN_RISK_UNAVAILABLE,
            RejectionCode.VOLUME_INTEGRITY_UNKNOWN,
            RejectionCode.GUIDE_SIGNAL_UNKNOWN,
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
            score, components = _score(features, token, policy, flagged, guide)
        elif reasons:
            status = CandidateStatus.REJECT
            score = None
            components = ()
        else:
            score, components = _score(features, token, policy, flagged, guide)
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
            flags=tuple(dict.fromkeys(flags)),
            depth=depth,
        )

    @staticmethod
    def _hard_filter_reasons(
        pool: PoolSnapshot,
        token: TokenRiskSnapshot,
        features: VolumeSpikeFeatures,
        policy: ScreeningPolicy,
        integrity: VolumeIntegrityAssessment,
        estimated_price_impact: Decimal,
        depth: DepthEvidence | None,
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
        if depth is None:
            if pool.depth is None or pool.depth < policy.minimum_depth:
                reasons.append(RejectionCode.LOW_DEPTH)
            if estimated_price_impact > policy.maximum_price_impact:
                reasons.append(RejectionCode.PRICE_IMPACT_HIGH)
        elif depth.source is not DepthSource.UNAVAILABLE:
            # Measured (a quote that cannot fill the reference size has neither value).
            if depth.depth is None or depth.depth < policy.minimum_depth:
                reasons.append(RejectionCode.LOW_DEPTH)
            if depth.price_impact is None or depth.price_impact > policy.maximum_price_impact:
                reasons.append(RejectionCode.PRICE_IMPACT_HIGH)
        # Data API flags of the pool and both tokens (G06, D07); ``None`` is no evidence.
        if pool.pool_blacklisted is True:
            reasons.append(RejectionCode.BLACKLISTED_POOL)
        if pool.base_token_verified is False or pool.quote_token_verified is False:
            reasons.append(RejectionCode.UNVERIFIED_TOKEN)
        if pool.base_freeze_authority_disabled is False:
            reasons.append(RejectionCode.FREEZE_AUTHORITY_PRESENT)
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
        if _holder_concentration_high(token.holder_concentration, policy):
            reasons.append(RejectionCode.HOLDER_CONCENTRATION_HIGH)
        # D09: a token with no configured source carries no data that could be stale or
        # unresolved; it is the TOKEN_RISK_UNAVAILABLE flag instead (see ``evaluate``).
        if token.source_configured and token.freshness is Freshness.STALE:
            reasons.append(RejectionCode.TOKEN_RISK_STALE)
        if token.source_configured and not _token_resolved(token, policy):
            reasons.append(RejectionCode.TOKEN_RISK_UNKNOWN)
        if integrity.status is IntegrityStatus.CONFIRMED:
            reasons.append(RejectionCode.ARTIFICIAL_VOLUME_CONFIRMED)
        elif integrity.status is IntegrityStatus.UNKNOWN:
            reasons.append(RejectionCode.VOLUME_INTEGRITY_UNKNOWN)
        if features.readiness is not SignalReadiness.READY:
            return reasons
        if policy.guide is not None:
            # guide-v1 (G24): the spike is a score bonus and a flag, not a filter; the fee
            # opportunity of the pool keeps its minimum under its own code.
            if features.fee_tvl is None or features.fee_tvl < policy.minimum_fee_tvl:
                reasons.append(RejectionCode.FEE_TVL_BELOW_THRESHOLD)
        elif (
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


def _holder_concentration_high(share: Decimal | None, policy: ScreeningPolicy) -> bool:
    """The top-10 share fails its maximum. guide-v1 asks for a share *below* it (spec 6.6,
    like the GMGN top-10 threshold), so the maximum itself fails; ``shadow-hypothesis-v1``
    keeps its inclusive maximum so its decisions stay reproducible."""

    if share is None:
        return False
    if policy.guide is not None:
        return share >= policy.maximum_holder_concentration
    return share > policy.maximum_holder_concentration


def _token_resolved(token: TokenRiskSnapshot, policy: ScreeningPolicy) -> bool:
    """Every mandatory token signal is known; a flagged one (wash trading) may be missing."""

    unresolved = set(token.missing_signals) - _flagged_token_signals(policy)
    if unresolved:
        return False
    return token.data_quality is DataQuality.COMPLETE or bool(token.missing_signals)


def _score(
    features: VolumeSpikeFeatures,
    token: TokenRiskSnapshot,
    policy: ScreeningPolicy,
    flagged: frozenset[str] = frozenset(),
    guide: GuideAssessment | None = None,
) -> tuple[Decimal, tuple[ScoreComponent, ...]]:
    if policy.guide is not None and guide is not None:
        return _guide_score(features, token, policy, policy.guide, flagged, guide)
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
        ("token_quality", _token_quality(token, flagged), policy.weights.token_quality),
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


def _guide_score(
    features: VolumeSpikeFeatures,
    token: TokenRiskSnapshot,
    policy: ScreeningPolicy,
    rules: GuideScreeningRules,
    flagged: frozenset[str],
    guide: GuideAssessment,
) -> tuple[Decimal, tuple[ScoreComponent, ...]]:
    """guide-v1 score (G24): the token's sustained hourly volume per minute against the
    author's "good" figure carries it; a spike adds its weight as a bonus."""

    weights = rules.weights
    values = (
        (
            "sustained_volume",
            _cap(guide.hourly_volume_per_minute, rules.target_volume_per_minute),
            weights.sustained_volume,
        ),
        ("volume_spike", Decimal(1) if guide.volume_spike else Decimal(0), weights.volume_spike),
        (
            "fee_opportunity",
            _cap(_net_edge(features.fee_tvl, policy.assumed_cost_rate), policy.target_fee_tvl),
            weights.fee_opportunity,
        ),
        ("liquidity_stability", _stability(features.tvl_change), weights.liquidity_stability),
        (
            "volatility_fit",
            _volatility_fit(features.price_volatility, policy.target_volatility),
            weights.volatility_fit,
        ),
        ("token_quality", _token_quality(token, flagged), weights.token_quality),
    )
    components = tuple(
        ScoreComponent(
            name=name, normalized_value=value, weight=weight, contribution=value * weight
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


def _token_quality(token: TokenRiskSnapshot, flagged: frozenset[str]) -> Decimal:
    if token.data_quality is DataQuality.COMPLETE:
        return Decimal(1)
    if token.missing_signals and set(token.missing_signals) <= flagged:
        return Decimal(1)
    return Decimal(0)


def _evidence(
    pool: PoolSnapshot,
    token: TokenRiskSnapshot,
    features: VolumeSpikeFeatures,
    integrity: VolumeIntegrityAssessment,
    price_impact: Decimal,
    policy: ScreeningPolicy,
    depth: DepthEvidence | None,
) -> tuple[EvidenceItem, ...]:
    measured = pool.depth if depth is None else depth.depth
    windows = (
        EvidenceItem(
            name="short_window_source",
            value=_origin_text(features.short_window_origin, pool),
        ),
        EvidenceItem(
            name="baseline_source",
            value="UNKNOWN" if features.baseline_origin is None else features.baseline_origin.value,
        ),
    )
    sources = (
        EvidenceItem(
            name="depth_source",
            value=DepthSource.SNAPSHOT.value if depth is None else depth.source.value,
            threshold=None if depth is None else depth.detail,
        ),
    )
    return (
        EvidenceItem(name="tvl", value=str(pool.tvl), threshold=str(policy.minimum_tvl)),
        EvidenceItem(name="depth", value=str(measured), threshold=str(policy.minimum_depth)),
        *sources,
        *windows,
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


def _origin_text(origin: WindowOrigin | None, pool: PoolSnapshot) -> str:
    """Where the current short window came from, with the derivation's interval or gap."""

    derivation = pool.short_window
    if origin is None:
        if derivation is not None and derivation.reason is not None:
            return f"UNKNOWN:{derivation.reason}"
        return "UNKNOWN"
    if (
        origin is WindowOrigin.CUMULATIVE_DELTA
        and derivation is not None
        and derivation.interval_seconds is not None
    ):
        return f"{origin.value}:{derivation.interval_seconds}s"
    return origin.value
